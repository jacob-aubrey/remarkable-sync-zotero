"""reMarkable sync v3/v4 client with additive, generation-checked writes.

Protocol reference: https://github.com/SamMorrowDrums/remarkable-mcp
Writes create items only; they never replace existing documents or delete data.
"""
import base64
import copy
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import uuid


def trace_event(callback, event, **fields):
    """Optional diagnostics may never alter a data operation."""
    try:
        if callback:
            callback(event, **fields)
    except Exception:
        pass


PAIR_URL = "https://my.remarkable.com/device/desktop/connect"
AUTH = "https://webapp-prod.cloud.remarkable.engineering/token/json/2"
SYNC = "https://internal.cloud.remarkable.com/sync"


class CloudError(RuntimeError):
    pass


class CloudConflict(CloudError):
    """The cloud root changed before a proposed additive write committed."""
    pass


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, new_url):
        if urlsplit(new_url).scheme != "https":
            raise CloudError("reMarkable returned an insecure redirect")
        if request.get_method() == "PUT" and code in {307, 308}:
            # urllib's stock redirect handler refuses PUT. Signed GCS upload
            # redirects preserve the method/body and discard bearer credentials
            # when moving to a different host.
            redirected = urllib.request.Request(new_url, data=request.data,
                headers={key: value for key, value in request.header_items() if key.lower() != "host"}, method="PUT")
        else:
            redirected = super().redirect_request(request, fp, code, msg, headers, new_url)
        if redirected and urlsplit(request.full_url).hostname != urlsplit(new_url).hostname:
            redirected.remove_header("Authorization")
        return redirected


def _publish_absent(temporary, path):
    """Publish without a check/replace race; the filesystem decides absence."""
    try:
        if os.name == "nt":
            # Windows rename refuses an existing destination, including one
            # created immediately before this call.
            os.rename(temporary, path)
        else:
            os.link(temporary, path)
    except FileExistsError:
        raise CloudError("A local file appeared during publication; it was preserved. Review its current copy and retry.") from None


def _preserved_paths(path):
    path = Path(path)
    return [str(entry) for entry in path.parent.iterdir()
            if entry.name.startswith(path.name + ".") and entry.name.endswith(".preserved")]


def _guarded_file(path, expected_digest, action, item_label="PDF"):
    """Operate on one verified Windows file, denying writers and renames.

    Sharing restrictions also reject an already open writable/mapped file.
    Digest checks and rename/delete operations use the same file handle.
    Platforms without these mandatory restrictions fail closed.
    """
    if os.name != "nt":
        raise CloudError(f"Protected replacement requires Windows file sharing; the local {item_label} was retained.")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    read = kernel.ReadFile
    read.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                     ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    read.restype = wintypes.BOOL
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    handle = create(str(Path(path).absolute()), 0x80000000 | 0x10000, 1, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        retained = _preserved_paths(path)
        if retained:
            raise CloudError(f"The local {item_label} is unavailable. Previous complete {item_label} copies are preserved at " + "; ".join(retained))
        raise CloudError(f"The local {item_label} is missing or open for editing; it was retained. Close its editor and retry.")
    try:
        digest = hashlib.sha256()
        buffer, count = ctypes.create_string_buffer(1024 * 1024), wintypes.DWORD()
        while True:
            if not read(handle, buffer, len(buffer), ctypes.byref(count), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if not count.value:
                break
            digest.update(buffer.raw[:count.value])
        if digest.hexdigest() != expected_digest:
            raise CloudError(f"The local {item_label} changed during publication; it was preserved. Review its current copy and retry.")
        return action(kernel, handle)
    finally:
        close(handle)


def _rename_guarded(kernel, handle, destination):
    name = str(Path(destination).absolute())
    name_units = len(name.encode("utf-16-le")) // 2

    class RenameInfo(ctypes.Structure):
        _fields_ = [("flags", wintypes.DWORD), ("root", wintypes.HANDLE),
                    ("length", wintypes.DWORD), ("name", wintypes.WCHAR * (name_units + 1))]

    info = RenameInfo(0, None, len(name.encode("utf-16-le")), name)
    function = kernel.SetFileInformationByHandle
    function.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    function.restype = wintypes.BOOL
    if not function(handle, 3, ctypes.byref(info), ctypes.sizeof(info)):
        raise ctypes.WinError(ctypes.get_last_error())


def _delete_guarded(kernel, handle):
    info = wintypes.BOOL(True)
    function = kernel.SetFileInformationByHandle
    function.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    function.restype = wintypes.BOOL
    if not function(handle, 4, ctypes.byref(info), ctypes.sizeof(info)):
        raise ctypes.WinError(ctypes.get_last_error())


def _replace_verified(temporary, path, expected_digest, item_label="PDF"):
    def replace(kernel, handle):
        preserved = path.with_name(path.name + "." + uuid.uuid4().hex + ".preserved")
        _rename_guarded(kernel, handle, preserved)
        try:
            _publish_absent(temporary, path)
        except BaseException:
            # Restore through the original handle, never replacing a file that
            # appeared while the verified original was moved aside. A crash or
            # collision leaves the complete original at the preserved sibling.
            try:
                _rename_guarded(kernel, handle, path)
            except OSError:
                raise CloudError(f"A local file appeared during publication and was retained. The previous {item_label} is preserved at {preserved}.") from None
            raise
        try:
            _delete_guarded(kernel, handle)
        except OSError:
            # Successful publication remains valid; retaining the old complete
            # output is preferable to treating cleanup as a failed transfer.
            # Return its location so the caller can persist and show recovery.
            return preserved
        return None
    return _guarded_file(path, expected_digest, replace, item_label=item_label)


def archive_verified(path, destination, expected_digest):
    """Move a managed output without deleting edits made after a digest check."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _guarded_file(path, expected_digest, lambda kernel, handle: _rename_guarded(kernel, handle, destination))


def publish_staged(temporary, path, expected_digest=None, require_absent=False, item_label="PDF"):
    """Flush a staged file, then publish with the requested preservation guard.

    The caller owns staging and cleanup. A returned path is a complete previous
    copy retained after successful publication when cleanup could not finish.
    """
    if require_absent and expected_digest is not None:
        raise CloudError(f"The local {item_label} changed availability during publication; review its current copy and retry.")
    temporary, path = Path(temporary), Path(path)
    with temporary.open("r+b") as stream:
        stream.flush()
        os.fsync(stream.fileno())
    if expected_digest is not None:
        return _replace_verified(temporary, path, expected_digest, item_label=item_label)
    elif require_absent:
        retained = _preserved_paths(path)
        if retained:
            raise CloudError(f"An interrupted replacement needs review. Previous complete {item_label} copies are preserved at " + "; ".join(retained))
        _publish_absent(temporary, path)
    else:
        # Short-lived Windows scanner/sync locks can block a metadata rename.
        # Retry only the same atomic operation; never remove the destination or
        # weaken the separate digest-guarded user-file replacement path above.
        for attempt in range(5):
            try:
                os.replace(temporary, path)
                break
            except PermissionError as error:
                if getattr(error, "winerror", None) not in {5, 32, 33} or attempt == 4:
                    raise
                time.sleep(.025 * 2 ** attempt)


def atomic_write(path, data, expected_digest=None, require_absent=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(data)
            stream.flush()
        return publish_staged(temporary, path, expected_digest=expected_digest, require_absent=require_absent)
    finally:
        temporary.unlink(missing_ok=True)


def _dpapi(data, decrypt=False):
    """Protect pairing credentials with the current Windows user account."""
    if os.name != "nt":
        raise CloudError("Persistent reMarkable pairing currently requires Windows credential protection.")

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    function = getattr(ctypes.windll.crypt32, "CryptUnprotectData" if decrypt else "CryptProtectData")
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise CloudError("Windows could not unlock reMarkable pairing. Pair again using this Windows account.")
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        free = ctypes.windll.kernel32.LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        free(output.data)


class TokenVault:
    def __init__(self, path):
        self.path = Path(path)

    def save(self, token):
        atomic_write(self.path, _dpapi(token.encode("utf-8")))

    def load(self):
        return _dpapi(self.path.read_bytes(), decrypt=True).decode("utf-8") if self.path.exists() else ""

    def forget(self):
        self.path.unlink(missing_ok=True)


@dataclass
class CloudItem:
    id: str
    hash: str
    name: str
    parent: str
    kind: str
    deleted: bool
    files: list

    @property
    def is_pdf(self):
        return self.kind == "DocumentType" and any(f["id"] == self.id + ".pdf" for f in self.files)


def parse_index(data):
    lines = data.decode("utf-8").splitlines()
    if not lines or lines[0] not in {"3", "4"}:
        raise CloudError("Unsupported reMarkable cloud index format; saved files are retained.")
    entries, seen = [], set()
    for line in lines[1:]:
        if not line:
            continue
        fields = line.split(":")
        if lines[0] == "4" and len(fields) == 4 and fields[:2] == ["0", "."]:
            if not all(value.isdigit() for value in fields[2:]):
                raise CloudError("Invalid cloud root totals")
            continue
        if (len(fields) != 5 or not re.fullmatch(r"[a-f0-9]{64}", fields[0])
                or not fields[3].isdigit() or not fields[4].isdigit()):
            raise CloudError("Invalid reMarkable cloud index; saved files are retained.")
        if fields[2] in seen:
            raise CloudError("Duplicate item in cloud index")
        seen.add(fields[2])
        entries.append({"hash": fields[0], "id": fields[2], "type": fields[1],
                        "subfiles": int(fields[3]), "size": int(fields[4])})
    return entries


def serialize_index(entries, root=False):
    ordered = sorted(entries, key=lambda entry: entry["id"])
    lines = ["4" if root else "3"]
    if root:
        lines.append(f"0:.:{len(ordered)}:{sum(entry['size'] for entry in ordered)}")
    for entry in ordered:
        if any(c in entry["id"] for c in "\r\n:"):
            raise CloudError("Invalid cloud index filename")
        lines.append(f"{entry['hash']}:0:{entry['id']}:{entry['subfiles']}:{entry['size']}")
    return ("\n".join(lines) + "\n").encode()


def blob_matches(data, digest, filename):
    if not filename.endswith(".docSchema"):
        return hashlib.sha256(data).hexdigest() == digest
    # Document indexes and older root indexes use a composite hash rather than
    # SHA256(serialized bytes). Schema-4 roots use the ordinary content hash.
    try:
        entries = parse_index(data)
    except (CloudError, UnicodeError):
        return False
    if filename == "root.docSchema" and data.splitlines()[0] == b"4":
        actual = hashlib.sha256(data).hexdigest()
    else:
        actual = hashlib.sha256(b"".join(bytes.fromhex(entry["hash"])
                                       for entry in sorted(entries, key=lambda entry: entry["id"]))).hexdigest()
    return actual == digest


_CRC32C_TABLE = []
for _number in range(256):
    _value = _number
    for _bit in range(8):
        _value = (_value >> 1) ^ (0x82F63B78 if _value & 1 else 0)
    _CRC32C_TABLE.append(_value)


def crc32c_header(data):
    """GCS requires Castagnoli CRC32C, which differs from zlib.crc32."""
    value = 0xffffffff
    for byte in data:
        value = (value >> 8) ^ _CRC32C_TABLE[(value ^ byte) & 255]
    return "crc32c=" + base64.b64encode((value ^ 0xffffffff).to_bytes(4, "big")).decode()


class CloudClient:
    """Pair once, renew sessions, and fetch immutable cloud blobs with checks."""
    def __init__(self, token="", cache_dir=None, stop=None, cache_limit_bytes=64 * 1024 * 1024, progress=None, trace=None):
        self.device_token = token
        self.user_token = ""
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.cache_limit_bytes = cache_limit_bytes
        self.baseline_dir = None
        self.progress = progress
        self.trace = trace
        self._cache_size = None
        self.stop = stop or threading.Event()
        self.opener = urllib.request.build_opener(_SafeRedirect())

    def _progress(self, message):
        callback = getattr(self, 'progress', None)
        if callback:
            callback(message)

    def check_stop(self):
        if self.stop.is_set():
            raise InterruptedError("reMarkable sync stopped")

    def _trace(self, event, **fields):
        try:
            if self.trace:
                self.trace(event, **fields)
        except Exception:
            pass

    def _http(self, url, token="", body=None, filename=None, method=None, extra_headers=None):
        headers = {"User-Agent": "PDF-Note-Organizer", "Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        if filename:
            if "\r" in filename or "\n" in filename:
                raise CloudError("Invalid cloud filename")
            headers["rm-filename"] = filename
        headers.update(extra_headers or {})
        for attempt in range(3):
            self.check_stop()
            request = urllib.request.Request(url, data=body, headers=headers, method=method)
            request_id, started = uuid.uuid4().hex, time.monotonic()
            self._trace('cloud_request_started', request_id=request_id, verb=request.get_method(), attempt=attempt + 1)
            try:
                with self.opener.open(request, timeout=20) as response:
                    chunks = []
                    while True:
                        self.check_stop()
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        chunks.append(chunk)
                    value = b"".join(chunks)
                    self._trace('cloud_request_finished', request_id=request_id, bytes=len(value),
                                duration_ms=round((time.monotonic() - started) * 1000))
                    return value
            except urllib.error.HTTPError as exc:
                status = exc.code
                self._trace('cloud_request_failed', request_id=request_id, status=status, error_type=type(exc).__name__)
                retry = exc.headers.get("Retry-After", "")
                exc.close()
                if status in {401, 403}:
                    raise CloudError("reMarkable authorization expired. Reconnect using a new pairing code.") from None
                if method == "PUT" and url.endswith("/v3/root") and status in {409, 412, 428}:
                    raise CloudConflict("Cloud library changed during upload; retry against its current generation.") from None
                if status not in {429, 500, 502, 503, 504} or attempt == 2:
                    raise CloudError(f"reMarkable request failed (HTTP {status}); saved files are retained.") from None
                delay = min(30, max(2 ** (attempt + 1), float(retry) if retry.isdigit() else 0))
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                self._trace('cloud_request_failed', request_id=request_id, error_type=type(error).__name__,
                            duration_ms=round((time.monotonic() - started) * 1000))
                if attempt == 2:
                    raise CloudError("reMarkable cloud is unreachable. Automatic sync will retry; saved files are retained.") from None
                delay = 2 ** (attempt + 1)
            self._trace('cloud_retry', request_id=request_id, attempt=attempt + 1, delay_s=delay)
            if self.stop.wait(delay):
                self.check_stop()
            self._progress(f'Waiting for reMarkable; retrying the current request ({attempt + 2} of 3)…')

    def pair(self, code):
        """Exchange a single-use pairing code for a device token."""
        code = code.strip()
        if not re.fullmatch(r"[A-Za-z0-9]{8}", code):
            raise CloudError("Enter the eight-character, single-use code from the reMarkable pairing page.")
        body = json.dumps({"code": code, "deviceDesc": "desktop-windows", "deviceID": str(uuid.uuid4())}).encode()
        self.device_token = self._http(AUTH + "/device/new", body=body).decode().strip()
        if not self.device_token:
            raise CloudError("Pairing returned no token. Generate a new code and try again.")
        return self.device_token

    def _renew(self):
        self.user_token = self._http(AUTH + "/user/new", self.device_token, body=b"").decode().strip()
        if not self.user_token:
            raise CloudError("reMarkable returned no session token. Reconnect with a new code.")

    def get(self, url, filename=None):
        if not self.user_token:
            self._renew()
        try:
            return self._http(url, self.user_token, filename=filename)
        except CloudError as exc:
            if "authorization expired" not in str(exc):
                raise
            self._renew()
            return self._http(url, self.user_token, filename=filename)

    def blob(self, digest, filename):
        """Reuse small cached files and verify downloaded file content hashes."""
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise CloudError("Invalid cloud content hash")
        if filename.endswith(".pdf") and self.baseline_dir:
            baseline = Path(self.baseline_dir) / (digest + ".pdf")
            if baseline.is_file():
                data = baseline.read_bytes()
                if hashlib.sha256(data).hexdigest() != digest:
                    raise CloudError("A retained PDF baseline failed its integrity check; review needed.")
                return data
        cached = self.cache_dir / digest if self.cache_dir else None
        if cached and cached.exists():
            data = cached.read_bytes()
            if blob_matches(data, digest, filename):
                try:
                    cached.touch()
                except OSError:
                    pass
                return data
        data = self.get(SYNC + "/v3/files/" + digest, filename)
        if not blob_matches(data, digest, filename):
            raise CloudError("Cloud download failed its integrity check; previous copy retained.")
        if (cached and not filename.endswith((".pdf", ".rm")) and self.cache_limit_bytes > 0
                and len(data) <= min(4 * 1024 * 1024, self.cache_limit_bytes)):
            previous_size = cached.stat().st_size if cached.exists() else 0
            atomic_write(cached, data)
            if getattr(self, '_cache_size', None) is None:
                self.prune_cache()
            else:
                self._cache_size += len(data) - previous_size
                if self._cache_size > self.cache_limit_bytes:
                    self.prune_cache()
        return data

    def prune_cache(self):
        """Bound disposable blobs; baselines, strokes, exports and notes stay."""
        if not self.cache_dir or not self.cache_dir.is_dir():
            self._cache_size = 0
            return 0
        files = []
        for path in self.cache_dir.iterdir():
            if re.fullmatch(r"[a-f0-9]{64}", path.name) and path.is_file():
                try:
                    stat = path.stat()
                    files.append((stat.st_mtime_ns, stat.st_size, path))
                except OSError:
                    continue
        total = sum(size for _, size, _ in files)
        removed = 0
        for _, size, path in sorted(files):
            if total <= max(0, self.cache_limit_bytes):
                break
            try:
                path.unlink()
            except OSError:
                continue
            total -= size
            removed += 1
        self._cache_size = total
        return removed

    def library(self):
        """List metadata for scope discovery; PDF contents are fetched separately."""
        self._progress('Reading reMarkable’s file list…')
        root = json.loads(self.get(SYNC + "/v4/root"))
        previous = getattr(self, '_catalog_items', {})
        if getattr(self, '_catalog_hash', None) == root['hash']:
            self.check_stop()
            self._progress('reMarkable’s file list is up to date.')
            return copy.deepcopy(list(previous.values()))
        entries = parse_index(self.blob(root["hash"], "root.docSchema"))
        items = {}
        reported = 0.0
        for number, entry in enumerate(entries, 1):
            self.check_stop()
            now = time.monotonic()
            if number == 1 or number == len(entries) or now - reported >= 1:
                self._progress(f'Reading reMarkable’s file list: {number} of {len(entries)}')
                reported = now
            identity = entry['id']
            uuid.UUID(identity)
            cached = previous.get(identity)
            if cached and cached.hash == entry['hash']:
                items[identity] = cached
                continue
            files = parse_index(self.blob(entry["hash"], entry["id"] + ".docSchema"))
            meta = next((f for f in files if f["id"] == entry["id"] + ".metadata"), None)
            if meta is None:
                raise CloudError("Cloud item has no metadata; library listing is incomplete.")
            metadata = json.loads(self.blob(meta["hash"], meta["id"]))
            items[identity] = CloudItem(identity, entry['hash'], metadata.get('visibleName', identity),
                                       metadata.get('parent', ''), metadata['type'],
                                       bool(metadata.get('deleted', False)), files)
        self.check_stop()
        self._catalog_hash, self._catalog_items = root['hash'], items
        self._progress(f'Finished reading reMarkable’s file list: {len(items)} files.')
        return copy.deepcopy(list(items.values()))

    def pdf_bundle(self, item):
        """Retain all raw document files, including the PDF and stroke layers."""
        bundle = {}
        for file in item.files:
            self.check_stop()
            bundle[file["id"]] = self.blob(file["hash"], file["id"])
        if item.id + ".pdf" not in bundle or item.id + ".content" not in bundle:
            raise CloudError("Incomplete PDF download; previous copy retained.")
        return bundle

    def put(self, url, data, filename, content_type="application/octet-stream", integrity=True):
        if not self.user_token:
            self._renew()
        headers = {"Content-Type": content_type}
        if integrity:
            headers["x-goog-hash"] = crc32c_header(data)
        try:
            return self._http(url, self.user_token, body=data, filename=filename,
                              method="PUT", extra_headers=headers)
        except CloudError as exc:
            if "authorization expired" not in str(exc):
                raise
            self._renew()
            return self._http(url, self.user_token, body=data, filename=filename,
                              method="PUT", extra_headers=headers)

    def _file_entry(self, filename, data):
        digest = hashlib.sha256(data).hexdigest()
        self.put(SYNC + "/v3/files/" + digest, data, filename)
        return {"id": filename, "hash": digest, "type": "0", "subfiles": 0, "size": len(data)}

    def _item_files(self, entry):
        return parse_index(self.blob(entry["hash"], entry["id"] + ".docSchema"))

    def _item_metadata(self, entry):
        files = self._item_files(entry)
        meta = next((file for file in files if file["id"] == entry["id"] + ".metadata"), None)
        if meta is None:
            raise CloudError("Cloud item has no metadata; creation paused.")
        return json.loads(self.blob(meta["hash"], meta["id"]))

    def _validate_parent(self, entries, parent_id, scope_id):
        by_id = {entry["id"]: entry for entry in entries}
        seen, parent, scoped = set(), parent_id, not scope_id
        while parent:
            if parent == "trash" or parent in seen or parent not in by_id:
                raise CloudError("Upload folder is unavailable; paper retained locally.")
            seen.add(parent)
            metadata = self._item_metadata(by_id[parent])
            if metadata.get("deleted") or metadata.get("type") != "CollectionType":
                raise CloudError("Upload destination is not a live cloud folder.")
            scoped |= parent == scope_id
            parent = metadata.get("parent", "")
        if not scoped:
            raise CloudError("Upload destination moved outside the linked cloud source.")

    def _commit_new_item(self, item_id, name, parent_id, kind, bundle, scope_id=""):
        """A retry with the same ID recognizes an already committed item.

        The root is reread after generation conflicts. Existing items are never
        replaced, including when the tablet already added annotations to them.
        """
        uuid.UUID(item_id)
        new_entry = None
        for attempt in range(5):
            self.check_stop()
            root = json.loads(self.get(SYNC + "/v4/root"))
            if type(root.get("generation")) is not int or root["generation"] < 0:
                raise CloudError("Cloud root has no generation; creation paused.")
            entries = parse_index(self.blob(root["hash"], "root.docSchema"))
            self._validate_parent(entries, parent_id, scope_id)
            existing = next((entry for entry in entries if entry["id"] == item_id), None)
            if existing:
                metadata = self._item_metadata(existing)
                if metadata.get("deleted") or metadata.get("type") != kind:
                    raise CloudError("Queued cloud ID now belongs to a different or deleted item; review needed.")
                self._validate_parent(entries, metadata.get("parent", ""), scope_id)
                files = self._item_files(existing)
                if kind == "DocumentType":
                    original = next((file for file in files if file["id"] == item_id + ".pdf"), {})
                    if original.get("hash") != hashlib.sha256(bundle[item_id + ".pdf"]).hexdigest():
                        raise CloudError("Queued cloud ID has a different PDF; review needed.")
                elif metadata.get("parent", "") != parent_id or metadata.get("visibleName") != name:
                    raise CloudError("Queued cloud folder changed; review its link before retrying.")
                return CloudItem(item_id, existing["hash"], metadata.get("visibleName", name),
                                 metadata.get("parent", ""), kind, False, files)
            if new_entry is None:
                files = [self._file_entry(filename, data) for filename, data in bundle.items()]
                doc_body = serialize_index(files)
                doc_hash = hashlib.sha256(b"".join(bytes.fromhex(file["hash"])
                                                 for file in sorted(files, key=lambda file: file["id"]))).hexdigest()
                self.put(SYNC + "/v3/files/" + doc_hash, doc_body, item_id + ".docSchema")
                new_entry = {"id": item_id, "hash": doc_hash, "type": "0",
                             "subfiles": len(files), "size": sum(file["size"] for file in files)}
            root_body = serialize_index(entries + [new_entry], root=True)
            root_hash = hashlib.sha256(root_body).hexdigest()
            self.put(SYNC + "/v3/files/" + root_hash, root_body, "root.docSchema", "text/plain; charset=UTF-8")
            try:
                committed = json.loads(self.put(SYNC + "/v3/root", json.dumps({"hash": root_hash,
                    "generation": root["generation"], "broadcast": True}).encode(), "roothash",
                    "application/json", integrity=False))
                if committed.get("hash") != root_hash:
                    raise CloudError("Cloud commit was not confirmed; retry will check its saved document ID.")
                return CloudItem(item_id, doc_hash, name, parent_id, kind, False, files)
            except CloudConflict:
                if attempt == 4:
                    raise
                if self.stop.wait(min(4, attempt + 1)):
                    self.check_stop()

    def create_folder(self, name, parent_id, item_id, scope_id=""):
        if not name.strip() or any(c in name for c in "\r\n"):
            raise CloudError("Choose a nonempty folder name")
        now = str(int(time.time() * 1000))
        metadata = {"createdTime": now, "lastModified": now, "new": False, "parent": parent_id,
                    "pinned": False, "source": "", "type": "CollectionType", "visibleName": name}
        return self._commit_new_item(item_id, name, parent_id, "CollectionType",
                                    {item_id + ".metadata": json.dumps(metadata, sort_keys=True).encode()}, scope_id)

    def update_item(self, item_id, *, expected_hash, name=None, parent=None, scope_id="", restore=False,
                    tags=None, page_tags=None):
        """Rename/move/trash/restore metadata without rewriting document layers.

        A caller must have observed this exact revision. An unrelated library
        commit is retried; a change to this item requires renewed review. A
        confirmed trash entry can be restored only to a validated live parent.
        """
        uuid.UUID(item_id)
        if not re.fullmatch(r"[a-f0-9]{64}", expected_hash or ""):
            raise CloudError("Select a current cloud revision before changing it.")
        if name is not None and (not isinstance(name, str) or not name.strip()
                                 or len(name) > 240 or any(c in name for c in "\r\n\x00")):
            raise CloudError("Choose a nonempty document or folder name.")
        prepared, desired = None, None
        for attempt in range(5):
            self.check_stop()
            root = json.loads(self.get(SYNC + "/v4/root"))
            if type(root.get("generation")) is not int or root["generation"] < 0:
                raise CloudError("Cloud root has no generation; change paused.")
            entries = parse_index(self.blob(root["hash"], "root.docSchema"))
            entry = next((row for row in entries if row["id"] == item_id), None)
            if not entry:
                raise CloudError("The cloud item is missing. It was not recreated.")
            # A lost successful response is recognized by the exact prepared
            # hash; the operation is never replayed onto newer tablet edits.
            if prepared and entry["hash"] == prepared["hash"]:
                return CloudItem(item_id, entry["hash"], desired["visibleName"], desired.get("parent", ""),
                                 desired["type"], bool(desired.get("deleted")), self._item_files(entry))
            if entry["hash"] != expected_hash:
                raise CloudConflict("This cloud item changed. Refresh and review before retrying.")
            metadata = self._item_metadata(entry)
            trashed = bool(metadata.get("deleted")) or metadata.get("parent") == "trash"
            if trashed and not restore:
                raise CloudError("This item is in tablet Trash. Choose Restore explicitly.")
            if restore and not trashed:
                raise CloudError("This cloud item is not a confirmed trash entry.")
            if not trashed:
                self._validate_parent(entries, metadata.get("parent", ""), scope_id)
            destination = metadata.get("parent", "") if parent is None else parent
            if destination != "trash":
                self._validate_parent(entries, destination, scope_id)
            if destination == item_id:
                raise CloudError("A folder cannot be moved into itself.")
            # Reject moving a folder under its own descendant.
            by_id, cursor, seen = {row["id"]: row for row in entries}, destination, set()
            while cursor and cursor != "trash":
                if cursor == item_id or cursor in seen:
                    raise CloudError("A folder cannot be moved into its own subtree.")
                seen.add(cursor)
                cursor = self._item_metadata(by_id[cursor]).get("parent", "")
            if prepared is None:
                desired = {**metadata, "parent": destination, "deleted": destination == "trash",
                           "lastModified": str(int(time.time() * 1000))}
                if name is not None:
                    desired["visibleName"] = name.strip()
                files = [file for file in self._item_files(entry) if file["id"] != item_id + ".metadata"]
                if tags is not None or page_tags is not None:
                    from .tablet_tags import names, tablet_tags
                    import pymupdf
                    from .remarkable_pdf import page_map
                    content_file = next((f for f in files if f['id'] == item_id + '.content'), None)
                    pdf_file = next((f for f in files if f['id'] == item_id + '.pdf'), None)
                    if desired['type'] != 'DocumentType' or not content_file or not pdf_file:
                        raise CloudError('Tags can only be synchronized for a PDF.')
                    content = json.loads(self.blob(content_file['hash'], content_file['id']))
                    tablet_tags(content)  # Fail closed on an unrecognized tag schema.
                    now = int(time.time() * 1000)
                    if tags is not None:
                        retained = {t['name']: t for t in content.get('tags') or []}
                        content['tags'] = [retained.get(t, {'name': t, 'timestamp': now}) for t in names(tags)]
                    if page_tags is not None:
                        if not isinstance(page_tags, dict):
                            raise CloudError('Page tags need stable page identities.')
                        with pymupdf.open(stream=self.blob(pdf_file['hash'], pdf_file['id']), filetype='pdf') as pdf:
                            active = {pid for pid, _ in page_map(content, len(pdf))}
                        retained = {(t['pageId'], t['name']): t for t in content.get('pageTags') or []}
                        updated_tags = []
                        for pid, values in sorted(page_tags.items()):
                            for tag in names(values):
                                if pid not in active and (pid, tag) not in retained:
                                    raise CloudError('A tagged tablet page was removed. Review its pending tags.')
                                updated_tags.append(retained.get((pid, tag), {'name': tag, 'pageId': pid, 'timestamp': now}))
                        content['pageTags'] = updated_tags
                    files = [f for f in files if f['id'] != content_file['id']]
                    files.append(self._file_entry(content_file['id'], json.dumps(content, sort_keys=True).encode()))
                files.append(self._file_entry(item_id + ".metadata", json.dumps(desired, sort_keys=True).encode()))
                doc_body = serialize_index(files)
                doc_hash = hashlib.sha256(b"".join(bytes.fromhex(file["hash"])
                    for file in sorted(files, key=lambda file: file["id"]))).hexdigest()
                self.put(SYNC + "/v3/files/" + doc_hash, doc_body, item_id + ".docSchema")
                prepared = {**entry, "hash": doc_hash, "subfiles": len(files),
                            "size": sum(file["size"] for file in files)}
            body = serialize_index([prepared if row["id"] == item_id else row for row in entries], root=True)
            root_hash = hashlib.sha256(body).hexdigest()
            self.put(SYNC + "/v3/files/" + root_hash, body, "root.docSchema", "text/plain; charset=UTF-8")
            try:
                committed = json.loads(self.put(SYNC + "/v3/root", json.dumps({"hash": root_hash,
                    "generation": root["generation"], "broadcast": True}).encode(), "roothash",
                    "application/json", integrity=False))
                if committed.get("hash") != root_hash:
                    raise CloudError("Cloud change was not confirmed. Refresh to recover its result.")
                return CloudItem(item_id, prepared["hash"], desired["visibleName"], destination,
                                 desired["type"], bool(desired.get("deleted")), files)
            except CloudConflict:
                if attempt == 4:
                    raise

    def upload_pdf(self, data, name, parent_id, item_id, scope_id=""):
        import pymupdf as fitz
        try:
            with fitz.open(stream=data, filetype="pdf") as pdf:
                if pdf.needs_pass or pdf.page_count < 1:
                    raise ValueError("PDF is locked or has no pages")
                count = pdf.page_count
        except Exception as exc:
            raise CloudError(f"Cannot send this PDF: {exc}") from exc
        now = str(int(time.time() * 1000))
        pages = [str(uuid.uuid5(uuid.UUID(item_id), str(number))) for number in range(count)]
        content = {"coverPageNumber": -1, "documentMetadata": {}, "dummyDocument": False,
                   "extraMetadata": {}, "fileType": "pdf", "fontName": "", "formatVersion": 1,
                   "lineHeight": -1, "margins": 100, "orientation": "portrait", "pageCount": count,
                   "pageTags": [], "pages": pages, "tags": [], "textScale": 1}
        metadata = {"createdTime": now, "deleted": False, "lastModified": now, "lastOpened": now,
                    "lastOpenedPage": 0, "metadatamodified": False, "modified": False, "new": False,
                    "parent": parent_id, "pinned": False, "source": "", "synced": False,
                    "type": "DocumentType", "version": 1, "visibleName": name}
        bundle = {item_id + ".pdf": data, item_id + ".content": json.dumps(content, sort_keys=True).encode(),
                  item_id + ".metadata": json.dumps(metadata, sort_keys=True).encode(),
                  item_id + ".pagedata": ("Blank\n" * count).encode()}
        return self._commit_new_item(item_id, name, parent_id, "DocumentType", bundle, scope_id)
