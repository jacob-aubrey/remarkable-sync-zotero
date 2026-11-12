"""Explicit, resumable publication of prepared PDFs through Zotero's local API.

Zotero owns bibliography after creation. This module owns only its marked PDF
attachments. It never opens Zotero's database or invokes the tablet/cloud client.
Protocol implementation: Pyzotero 1.16.0 (BlueOak-1.0.0).
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
import uuid
from urllib.parse import urlsplit
from urllib.request import url2pathname
from .remarkable_cloud import trace_event

INDEX_REVISION = 2
ZOTERO_REQUEST_TIMEOUT = 30

class PilotError(RuntimeError):
    pass


class TransportInterrupted(PilotError):
    """A connection interruption is resumable, not a defective paper."""
    pass


class ReviewChanged(PilotError):
    """Native operation refused stale review without applying its mutation."""
    pass


class AuthorizationRequired(PilotError):
    pass


def stable_key(namespace, kind, identity):
    alphabet = "23456789ABCDEFGHIJKLMNPQRSTUVWXYZ"
    digest = hashlib.sha256(json.dumps([namespace, kind, identity]).encode()).digest()
    return "".join(alphabet[value % len(alphabet)] for value in digest[:8])


def marker(namespace, identity):
    return "Notable pilot: " + hashlib.sha256(json.dumps([namespace, identity]).encode()).hexdigest()


@dataclass(frozen=True)
class Paper:
    source_id: str
    path: Path
    sha256: str
    metadata: dict = field(default_factory=dict)
    reference_id: str = ""
    role: str = "main"
    annotations: tuple = ()
    source_attachment_key: str = ""
    filename: str = ""

    @property
    def reference(self):
        return self.reference_id or self.source_id


def load_manifest(path):
    path = Path(path).resolve()
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if value.get("schema") != 1 or not isinstance(value.get("namespace"), str) or not value["namespace"].strip():
        raise PilotError("Choose a schema 1 manifest with a stable namespace.")
    rows = value.get("papers")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise PilotError("A pilot manifest must select between 1 and 100 PDFs.")
    papers, seen = [], set()
    for row in rows:
        source = row.get("source_id")
        if not isinstance(source, str) or not source.strip() or source in seen:
            raise PilotError("Every selected PDF needs a unique, stable source_id.")
        seen.add(source)
        digest = row.get("sha256", "")
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise PilotError("Each PDF needs its reviewed SHA-256 digest.")
        metadata = row.get("metadata", {})
        if not isinstance(metadata, dict) or any(k in metadata for k in
                ("key", "version", "deleted", "parentItem", "collections", "relations", "note")):
            raise PilotError("Metadata must be a bibliographic record, without item identities or relationships.")
        if metadata.get("itemType") in {"attachment", "annotation", "note"}:
            raise PilotError("A reference must use a bibliographic item type.")
        role = row.get("role", "main")
        if role not in {"main", "si"} or (role == "si" and not row.get("reference_id")):
            raise PilotError("An SI attachment needs role 'si' and its main reference_id.")
        reference = row.get("reference_id", source)
        if not isinstance(reference, str) or not reference:
            raise PilotError("reference_id must be a non-empty string.")
        annotations = row.get("annotations", [])
        if not isinstance(annotations, list) or any(not isinstance(a, dict) or any(
                not isinstance(a.get(k, ""), str) for k in ("quote", "comment", "ocr")) for a in annotations):
            raise PilotError("Saved annotation text must use quote/comment/ocr string fields.")
        papers.append(Paper(source, (path.parent / row["pdf"]).resolve(), digest,
                            metadata, reference, role, tuple(annotations)))
    mains = [p.reference for p in papers if p.role == "main"]
    if len(mains) != len(set(mains)) or any(p.reference not in mains for p in papers):
        raise PilotError("Select exactly one main PDF per reference, including the parent of every SI file.")
    return value["namespace"], sorted(papers, key=lambda p: p.role != "main")


class Ledger:
    """Separate helper state; each intent survives a process interruption."""
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "pilot.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS binding (id INTEGER PRIMARY KEY CHECK(id=1), server TEXT, namespace TEXT);
                CREATE TABLE IF NOT EXISTS publications (
                    source TEXT PRIMARY KEY, published INTEGER DEFAULT 0,
                    md5 TEXT DEFAULT '', pending_md5 TEXT DEFAULT '', indexed_sha TEXT DEFAULT '');
                CREATE TABLE IF NOT EXISTS pilot_collections (key TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS text_indexes (source TEXT PRIMARY KEY, content_sha TEXT);
                CREATE TABLE IF NOT EXISTS reader_state (source TEXT PRIMARY KEY, pdf_sha TEXT);
                CREATE TABLE IF NOT EXISTS upload_intents (source TEXT PRIMARY KEY, receipt TEXT);
            """)

    @contextmanager
    def connect(self):
        callback = getattr(self, 'trace', None)
        identity, started = uuid.uuid4().hex, time.monotonic()
        role = self.directory.name if self.directory.name in {'bridge', 'review', 'publisher', 'edition-recovery', 'source-imports'} else 'helper_state'
        trace_event(callback, 'database_started', request_id=identity, role=role)
        try:
            db = sqlite3.connect(self.path, timeout=1)
            try:
                db.row_factory = sqlite3.Row
                with db:
                    yield db
            finally:
                db.close()
        except Exception as error:
            trace_event(callback, 'database_failed', request_id=identity, role=role, error_type=type(error).__name__)
            raise
        else:
            trace_event(callback, 'database_finished', request_id=identity, role=role,
                        duration_ms=round((time.monotonic() - started) * 1000))

    @contextmanager
    def lock(self):
        # A separate DB holds the process lock while intents commit in the ledger.
        db = sqlite3.connect(self.directory / "run-lock.sqlite3", timeout=.1)
        try:
            db.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            db.close()
            raise PilotError("This pilot state directory is already in use.") from None
        try:
            yield
        finally:
            db.rollback()
            db.close()

    def bind(self, server, namespace):
        with self.connect() as db:
            old = db.execute("SELECT server,namespace FROM binding WHERE id=1").fetchone()
            if old and tuple(old) != (server, namespace):
                raise PilotError("This state belongs to another Zotero instance or source namespace. Use a separate pilot state directory.")
            db.execute("INSERT OR IGNORE INTO binding VALUES(1,?,?)", (server, namespace))

    def collection_seen(self, key, *, save=False):
        with self.connect() as db:
            if save:
                db.execute("INSERT OR IGNORE INTO pilot_collections VALUES(?)", (key,))
            return db.execute("SELECT 1 FROM pilot_collections WHERE key=?", (key,)).fetchone() is not None

    def get(self, source):
        with self.connect() as db:
            row = db.execute("SELECT * FROM publications WHERE source=?", (source,)).fetchone()
        return dict(row) if row else {"published": 0, "md5": "", "pending_md5": "", "indexed_sha": ""}

    def index_digest(self, source, value=None):
        with self.connect() as db:
            if value is not None:
                db.execute("INSERT OR REPLACE INTO text_indexes VALUES(?,?)", (source, value))
            row = db.execute("SELECT content_sha FROM text_indexes WHERE source=?", (source,)).fetchone()
            return row[0] if row else ""

    def reader_revision(self, source, value=None):
        with self.connect() as db:
            if value is not None:
                db.execute("INSERT OR REPLACE INTO reader_state VALUES(?,?)", (source, value))
            row = db.execute("SELECT pdf_sha FROM reader_state WHERE source=?", (source,)).fetchone()
            return row[0] if row else ""

    def save(self, source, **changes):
        allowed = {"published", "md5", "pending_md5", "indexed_sha"}
        if changes.keys() - allowed:
            raise ValueError("Invalid publication state")
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO publications(source) VALUES(?)", (source,))
            for name, value in changes.items():
                db.execute(f"UPDATE publications SET {name}=? WHERE source=?", (value, source))

    def upload_intent(self, source, receipt=None, *, clear=False):
        """Retain account-sync metadata until native replacement is complete."""
        with self.connect() as db:
            if clear:
                db.execute("DELETE FROM upload_intents WHERE source=?", (source,))
            elif receipt is not None:
                db.execute("INSERT OR REPLACE INTO upload_intents VALUES(?,?)", (source, json.dumps(receipt)))
            row = db.execute("SELECT receipt FROM upload_intents WHERE source=?", (source,)).fetchone()
            return json.loads(row[0]) if row else None


class LocalZotero:
    """Reuse Pyzotero with localhost pinning and optional encrypted grants."""
    def __init__(self, port=23119, *, printed_ocr=None, handwriting_ocr=None, grant_store=None, interactive=True, trace=None):
        self.trace = trace
        self.printed_ocr = printed_ocr
        self.handwriting_ocr = handwriting_ocr
        self.index_diagnostics = []
        self.grant_store, self.interactive = grant_store, interactive
        try:
            import httpx2
            from pyzotero import Zotero
        except ImportError as error:
            self._trace('dependency_missing', module=error.name or 'zotero_runtime', optional=False)
            raise PilotError("The bundled Zotero integration is incomplete. Repair or reinstall the plugin; ordinary Sync does not require handwriting OCR.") from error
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise PilotError("Invalid local Zotero port.")
        self.endpoint = f"http://127.0.0.1:{port}/api"
        self.http = httpx2.Client(trust_env=False, timeout=ZOTERO_REQUEST_TIMEOUT, follow_redirects=False, headers={'Connection': 'close'},
                                 event_hooks={"request": [self.guard]})
        self._trace_client(self.http)
        self.zot = Zotero("0", "user", local=True, client=self.http, upload_timeout=60)
        self.zot.endpoint = self.endpoint
        try:
            response = self.http.get(self.endpoint + "/", timeout=ZOTERO_REQUEST_TIMEOUT)
            response.raise_for_status()
            self.server_id = response.headers.get("Zotero-Server-ID", "")
            if not self.server_id:
                raise PilotError("This pilot needs Zotero 10 with local API communication enabled.")
            self.zot.server_id = self.server_id
            if grant_store:
                try:
                    saved = grant_store.load()
                    if saved:
                        saved = json.loads(saved)
                        if not isinstance(saved, dict):
                            raise ValueError("Invalid saved grant")
                        if saved.get("server") == self.server_id:
                            key = saved.get("key")
                            if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9]{32}", key):
                                raise ValueError("Invalid saved grant")
                            self.zot.local_api_key = key
                except (OSError, ValueError):
                    if not interactive:
                        raise AuthorizationRequired("Stored Zotero permission is unavailable. Run Connect again.") from None
                    grant_store.forget()
        except BaseException:
            self.close()
            raise

    def guard(self, request):
        endpoint, candidate = urlsplit(self.endpoint), urlsplit(str(request.url))
        if ((candidate.scheme, candidate.netloc) != (endpoint.scheme, endpoint.netloc)
                or not candidate.path.startswith("/api/")):
            raise PilotError("Zotero returned a request outside its selected localhost API.")

    def _trace(self, event, **fields):
        try:
            callback = getattr(self, 'trace', None)
            if callback:
                callback(event, **fields)
        except Exception:
            pass

    def _trace_client(self, client):
        if not getattr(self, 'trace', None):
            return client
        original = client.send
        def send(request, *args, **kwargs):
            identity, started = uuid.uuid4().hex, time.monotonic()
            # Path only: no query, headers, credentials or request bodies.
            self._trace('zotero_request_started', request_id=identity, verb=request.method,
                        route=urlsplit(str(request.url)).path, timeout_s=ZOTERO_REQUEST_TIMEOUT,
                        effective_timeouts=request.extensions.get('timeout'), streamed=bool(kwargs.get('stream')))
            try:
                result = original(request, *args, **kwargs)
            except Exception as error:
                self._trace('zotero_request_failed', request_id=identity, error_type=type(error).__name__,
                            duration_ms=round((time.monotonic() - started) * 1000))
                raise
            self._trace('zotero_request_finished', request_id=identity, status=result.status_code,
                        duration_ms=round((time.monotonic() - started) * 1000))
            return result
        client.send = send
        return client

    def close(self):
        self.http.close()

    def authorize(self):
        # Zotero displays its normal consent dialog. Only remembered grants may
        # enter the caller's encrypted vault; keys are never printed.
        # Reuse the grant inside this process instead of prompting for each
        # publication/index repair and hitting Zotero's prompt rate limit.
        if not self.zot.local_api_key:
            if not getattr(self, "interactive", True):
                raise AuthorizationRequired("Connect to Zotero once interactively before running in the background.")
            grant = self.zot.authorize_local("Notable Zotero Pilot")
            if getattr(self, "grant_store", None) and grant.get("remember"):
                self.grant_store.save(json.dumps({"server": self.server_id, "key": grant["key"]}))

    def collections(self):
        return [row["data"] for row in self.zot.everything(self.zot.collections())]

    def collection_items(self, key):
        return [row["data"] for row in self.zot.everything(self.zot.collection_items_top(key))]

    def children(self, key):
        return [row["data"] for row in self.zot.everything(self.zot.children(key))]

    def reader_annotations(self, key):
        # /children follows bibliographic attachment/note descendants. Query
        # annotation items explicitly, then select their exact attachment ID.
        return [row["data"] for row in self.zot.everything(self.zot.items(itemType="annotation"))
                if row["data"].get("parentItem") == key and not row["data"].get("deleted")]

    @property
    def can_refresh_reader(self):
        if not hasattr(self, "_reader_support"):
            response = self.http.get(self.endpoint + "/notable/status",
                                     headers={"Zotero-Server-ID": self.server_id}, timeout=ZOTERO_REQUEST_TIMEOUT)
            if response.status_code == 404:
                self._reader_support = False
            else:
                response.raise_for_status()
                self._reader_support = response.json().get("schema") == 1
        return self._reader_support

    def native_annotation_count(self, key):
        if self.can_refresh_reader:
            response = self.http.get(f"{self.endpoint}/notable/annotations/{key}",
                                     headers={"Zotero-Server-ID": self.server_id}, timeout=ZOTERO_REQUEST_TIMEOUT)
            response.raise_for_status()
            return response.json()["native"]
        if self.reader_annotations(key):
            raise PilotError("Install Notable Companion Support in Zotero to distinguish existing PDF annotations from desktop edits before refreshing.")
        return 0

    def native_annotation_text(self, key):
        if not self.can_refresh_reader:
            return []
        response = self.http.get(f"{self.endpoint}/notable/annotations/{key}",
                                 headers={"Zotero-Server-ID": self.server_id}, timeout=ZOTERO_REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.json().get("nativeText", [])

    def refresh_reader(self, key):
        response = self.http.post(f"{self.endpoint}/notable/annotations/{key}", json={},
            headers={"Zotero-Server-ID": self.server_id, "Zotero-API-Key": self.zot.local_api_key},
            timeout=ZOTERO_REQUEST_TIMEOUT)
        if response.status_code in {401, 403}:
            raise AuthorizationRequired("Zotero write permission is unavailable. Run Connect again.")
        if response.status_code == 409:
            raise PilotError("This PDF has Zotero reader annotations. Review them before refreshing its tablet view.")
        response.raise_for_status()

    def update(self, record):
        if not self.zot.update_item(record):
            raise PilotError("The Zotero record changed during synchronization. Retry after reviewing it.")

    def update_collection(self, record):
        if not self.zot.update_collection(record):
            raise PilotError("The Zotero collection changed. Refresh before retrying.")

    def all_attachments(self):
        return [row["data"] for row in self.zot.everything(self.zot.items(itemType="attachment"))
                if not row["data"].get("deleted")]

    def review_action(self, action, payload=None):
        import httpx2
        import time
        self.authorize()
        retryable = action in {'catalog', 'inventory', 'bulk_import', 'bulk_import_status', 'prepare_upload', 'finish_upload', 'reparent',
                               'attach_intake_pdf'}
        for attempt in range(3 if retryable else 1):
            try:
                # Keep long import polling separate from the SDK connection.
                # A stale pooled socket must not poison every remaining paper.
                with self._trace_client(httpx2.Client(trust_env=False, timeout=ZOTERO_REQUEST_TIMEOUT, follow_redirects=False,
                                   event_hooks={'request': [self.guard]})) as connection:
                    response = connection.post(self.endpoint + '/notable/action', json={**(payload or {}), 'action': action},
                        headers={'Zotero-Server-ID': self.server_id, 'Zotero-API-Key': self.zot.local_api_key,
                                 'Connection': 'close'})
                if response.status_code in {401, 403}:
                    raise AuthorizationRequired('Zotero permission is unavailable. Connect again from Notable settings.')
                if response.status_code == 409:
                    if action == 'create_collection':
                        raise PilotError('The collection could not be created. Refresh folders and retry.')
                    raise ReviewChanged('The selected paper or its notes changed. Refresh the review before proceeding.')
                if response.status_code >= 500 or response.status_code == 429:
                    raise ValueError('Temporary local API interruption')
                if response.status_code >= 400:
                    raise PilotError('Zotero could not complete this reviewed action. The original files were retained.')
                return response.json()
            except (httpx2.TransportError, ValueError) as error:
                if not retryable or attempt == 2:
                    self._trace('zotero_action_failed', action=action, attempt=attempt + 1, error_type=type(error).__name__)
                    raise TransportInterrupted('The connection to Zotero was interrupted. The batch is saved; retry to resume without duplicates.') from None
                self._trace('zotero_retry', action=action, attempt=attempt + 1, delay_s=.25 * 2 ** attempt, error_type=type(error).__name__)
                time.sleep(.25 * 2 ** attempt)

    def file_path(self, key):
        response = self.http.get(f"{self.endpoint}/users/0/items/{key}/file/view/url",
                                 headers={"Zotero-Server-ID": self.server_id}, timeout=ZOTERO_REQUEST_TIMEOUT)
        response.raise_for_status()
        uri = urlsplit(response.text.strip())
        if uri.scheme != "file" or uri.netloc not in {"", "localhost"}:
            raise PilotError("The selected attachment is not a local file.")
        path = Path(url2pathname(uri.path))
        if str(path).startswith("\\\\"):
            raise PilotError("The companion cannot use network-share attachments.")
        return path

    def get(self, kind, key):
        from pyzotero import ResourceNotFoundError
        try:
            result = self.zot.collection(key) if kind == "collection" else self.zot.item(key)
            return result["data"]
        except ResourceNotFoundError:
            return None

    def create(self, kind, record):
        record = {"version": 0, **record}
        result = (self.zot.create_collections([record]) if kind == "collection"
                  else self.zot.create_items([record]))
        if result.get("failed"):
            failure = next(iter(result["failed"].values()))
            reason = str(failure.get("message", "Invalid record"))[:250]
            raise PilotError(f"Zotero rejected the pilot {kind}: {reason}")
        return self.get(kind, record["key"])

    def file_md5(self, key):
        from pyzotero import ResourceNotFoundError
        # Read the current bytes, rather than relying on a cached synced MD5.
        try:
            response = self.http.get(f"{self.endpoint}/users/0/items/{key}/file/view/url",
                                     headers={"Zotero-Server-ID": self.server_id}, timeout=ZOTERO_REQUEST_TIMEOUT)
            if response.status_code == 404:
                return ""
            response.raise_for_status()
        except ResourceNotFoundError:
            return ""
        uri = urlsplit(response.text.strip())
        if uri.scheme != "file" or uri.netloc not in {"", "localhost"}:
            raise PilotError("The pilot attachment is not a local file.")
        path = Path(url2pathname(uri.path))
        if str(path).startswith("\\\\"):
            raise PilotError("The pilot cannot use network-share attachments.")
        try:
            with path.open("rb") as stream:
                return hashlib.file_digest(stream, "md5").hexdigest()
        except FileNotFoundError:
            # A newly created native attachment can advertise its intended
            # storage URL before its first upload creates the file. Previously
            # published items still fail the caller's missing-file guard.
            return ""

    def prepare_file_upload(self, key, previous_md5):
        receipt = self.review_action("prepare_upload", {"key": key, "md5": previous_md5})
        if receipt.get("md5") != previous_md5 or self.file_md5(key) != previous_md5:
            raise PilotError("The imported PDF changed before its tablet update.")
        return {"key": key, **receipt}

    def finish_file_upload(self, key, md5, receipt):
        result = self.review_action("finish_upload", {"key": key, "md5": md5,
            "remote_md5": receipt["remote_md5"], "remote_mtime": receipt["remote_mtime"]})
        if result.get("md5") != md5 or result.get("sync_state") != "to_upload":
            raise PilotError("The updated PDF is waiting for its account-sync receipt. Retry synchronization.")

    def upload(self, key, path, previous_md5, *, upload_intent=None):
        from pyzotero import Zupload
        import shutil
        import tempfile
        adapter = self

        class LocalUpload(Zupload):
            def _upload_file(self, authdata, *args, **kwargs):
                # Pyzotero's upload step uses a separate HTTP client.
                # Validate its server-provided URL before allowing that step.
                adapter.guard(type("Request", (), {"url": authdata["url"]})())
                return super()._upload_file(authdata, *args, **kwargs)

        # The native local upload endpoint writes the supplied basename while
        # retaining the attachment's recorded path. Match that recorded name;
        # otherwise an upload can succeed but leave an inaccessible attachment.
        record = self.get("item", key)
        filename = (record or {}).get("filename") or Path(path).name
        if Path(filename).name != filename or "/" in filename or "\\" in filename:
            raise PilotError("The attachment has an invalid stored filename.")
        receipt = upload_intent or self.prepare_file_upload(key, previous_md5)
        if receipt.get("key") != key or self.file_md5(key) != previous_md5:
            raise PilotError("The imported PDF changed before its tablet update.")
        with tempfile.TemporaryDirectory(prefix="notable-upload-") as temporary:
            staged = Path(temporary) / filename
            shutil.copyfile(path, staged)
            result = LocalUpload(self.zot, [{"key": key, "filename": str(staged),
                                            "md5": receipt["authorization_md5"] or None}]).upload()
        if result["failure"] or not (result["success"] or result["unchanged"]):
            raise PilotError("The attachment upload did not complete. Retry the same manifest.")
        if upload_intent is None:
            self.finish_file_upload(key, hashlib.md5(Path(path).read_bytes()).hexdigest(), receipt)

    def index(self, key, path, saved_annotations=()):
        self.index_complete = False
        diagnostic = lambda event, **fields: self._trace(event, attachment_key=key, **fields)
        diagnostic('index_started')
        for recognizer in (self.printed_ocr, self.handwriting_ocr):
            if recognizer is not None:
                try:
                    recognizer.diagnostic = diagnostic
                except Exception:
                    pass
        text, count, empty_pages = searchable_content(path, saved_annotations, printed_ocr=self.printed_ocr,
                                                    handwriting_ocr=self.handwriting_ocr, diagnostic=diagnostic)
        native_text = self.native_annotation_text(key)
        if native_text:
            text += "\n" + "\n".join(str(row.get(field) or "") for row in native_text for field in ("text", "comment"))
        if not self.zot.set_fulltext(key, {"content": text,
                                          "indexedPages": count, "totalPages": count}):
            raise PilotError("The attachment text index was not accepted. Retry to finish indexing.")
        if self.index_digest(key) != hashlib.sha256(text.encode()).hexdigest():
            raise PilotError("The text index changed before verification. Retry to finish indexing.")
        self.index_complete = not self.printed_ocr or getattr(self.printed_ocr, 'complete', True)
        self.index_diagnostics.append({"attachment": key, "incomplete": not self.index_complete,
            "printed_ocr": list(self.printed_ocr.warnings) if self.printed_ocr else [],
            "handwriting": list(self.handwriting_ocr.warnings) if self.handwriting_ocr else []})
        diagnostic('index_finished', pages=count, empty_pages=len(empty_pages))
        return empty_pages

    @property
    def index_profile(self):
        printed = self.printed_ocr.profile if self.printed_ocr else "native-text"
        if self.handwriting_ocr:
            return checksum_profile([printed, self.handwriting_ocr.profile])
        return printed

    def index_digest(self, key):
        from pyzotero import ResourceNotFoundError
        try:
            content = self.zot.fulltext_item(key)["content"]
        except ResourceNotFoundError:
            return ""
        return hashlib.sha256(content.encode()).hexdigest()


def checksum_profile(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def searchable_content(path, saved_annotations=(), *, printed_ocr=None, handwriting_ocr=None, diagnostic=None):
    """Library retrieval includes printed text, quotes, comments and saved OCR.

    New machine transcripts require the explicitly selected local recognizer.
    Ink remains in the PDF; recognition provenance stays in its separate cache.
    This does not create native Zotero handwritten annotation objects.
    """
    import pymupdf
    from .extraction import MARKUP, highlighted_words
    texts, empty = [], []
    if printed_ocr:
        trace_event(diagnostic, 'printed_ocr_started')
    recognized = printed_ocr.content(path) if printed_ocr else None
    if printed_ocr:
        trace_event(diagnostic, 'printed_ocr_finished', pages=len(recognized))
    with pymupdf.open(path) as document:
        count = len(document)
        for index, page in enumerate(document):
            trace_event(diagnostic, 'pdf_page_started', page=index + 1, total=count)
            text = recognized[index] if recognized is not None else page.get_text()
            texts.append(text)
            if not text.strip():
                empty.append(index + 1)
            for annotation in page.annots() or ():
                info = annotation.info or {}
                if info.get("content"):
                    texts.append(info["content"])
                if annotation.type[1].lower() in MARKUP:
                    kind, quotation = document.xref_get_key(annotation.xref, "RMQuote")
                    if kind != "string":
                        quotation = " ".join(word[4] for word in highlighted_words(page, annotation))
                    if quotation:
                        texts.append(quotation)
            trace_event(diagnostic, 'pdf_page_finished', page=index + 1, total=count)
    for annotation in saved_annotations:
        texts.extend(str(annotation.get(field) or "") for field in ("quote", "comment", "ocr"))
    if handwriting_ocr:
        trace_event(diagnostic, 'handwriting_ocr_started')
        texts.extend(handwriting_ocr.content(path))
        trace_event(diagnostic, 'handwriting_ocr_finished')
    return "\n".join(texts), count, empty


def stage(ledger, paper):
    from .remarkable_cloud import atomic_write
    import pymupdf
    data = paper.path.read_bytes()
    if hashlib.sha256(data).hexdigest() != paper.sha256:
        raise PilotError("A prepared PDF changed. Prepare/review a new manifest before publishing.")
    with pymupdf.open(stream=data, filetype="pdf") as document:
        if document.needs_pass or not len(document):
            raise PilotError("The selected PDF is encrypted or empty.")
    target = ledger.directory / "versions" / paper.sha256 / "Current.pdf"
    if not target.exists():
        atomic_write(target, data, require_absent=True)
    elif hashlib.sha256(target.read_bytes()).hexdigest() != paper.sha256:
        raise PilotError("A retained pilot PDF version changed; it was not overwritten.")
    return target, hashlib.md5(data).hexdigest()


def upload_attachment(client, ledger, source, key, path, current, wanted):
    """Replace stored bytes and resume the account-sync receipt independently."""
    receipt = ledger.upload_intent(source)
    if receipt and receipt.get("key") != key:
        raise PilotError("The pending upload belongs to another attachment. Review its mapping.")
    if current != wanted:
        if hasattr(client, "prepare_file_upload"):
            prepared = client.prepare_file_upload(key, current)
            if receipt:
                # A repeated local upload retains the account's original
                # conflict precondition, rather than its intermediate hash.
                prepared.update({name: receipt[name] for name in ("remote_md5", "remote_mtime")})
            receipt = prepared
            ledger.upload_intent(source, receipt)
            client.upload(key, path, current, upload_intent=receipt)
        else:
            client.upload(key, path, current)
    if client.file_md5(key) != wanted:
        raise PilotError("Upload verification failed; retry the same document to recover.")
    if receipt:
        client.finish_file_upload(key, wanted, receipt)
        ledger.upload_intent(source, clear=True)


def reparent_attachment(client, key, parent):
    """The local item JSON API does not reliably apply parentItem changes."""
    item = client.get('item', key)
    if not item or item.get('deleted'):
        raise PilotError('The PDF is unavailable. Its reference was not changed.')
    if item.get('parentItem') == parent:
        return
    path = client.file_path(key)
    digest = hashlib.md5(path.read_bytes()).hexdigest()
    client.review_action('reparent', {'key': key, 'parent': parent, 'previous_parent': item.get('parentItem', ''), 'md5': digest})
    verified = client.get('item', key)
    if verified.get('parentItem') != parent or hashlib.md5(client.file_path(key).read_bytes()).hexdigest() != digest:
        raise PilotError('The PDF reference move did not verify. Its files were retained.')


def publish(client, ledger, namespace, papers, *, apply=False, collection_key=None, reference_keys=None, attachment_keys=None):
    """Plan without Zotero writes; apply only stable-key, owned attachments.

    Metadata/tags/collection membership on existing parent records are never
    replaced. A missing previously published item or edited PDF needs review.
    """
    with ledger.lock():
        ledger.bind(client.server_id, namespace)
        reference_keys = reference_keys or {}
        attachment_keys = attachment_keys or {}
        root_key = collection_key or stable_key(namespace, "collection", "pilot")
        root = client.get("collection", root_key)
        if not root and ledger.collection_seen(root_key):
            raise PilotError("The previously created pilot collection is missing. Restore it before retrying.")
        if collection_key and not root:
            raise PilotError("The selected Zotero collection is missing. It was not recreated.")
        if root and (root.get("deleted") or (not collection_key and (
                root.get("name") != "Notable Pilot" or root.get("parentCollection")))):
            raise PilotError("The pilot collection changed identity or location. Review it before retrying.")
        identities = [("reference", p.reference) for p in papers] + [("attachment", p.source_id) for p in papers]
        identities = {(kind, identity) for kind, identity in identities} | {("collection", "pilot")}
        if len({stable_key(namespace, kind, identity) for kind, identity in identities}) != len(identities):
            raise PilotError("Pilot identifiers collided. No Zotero changes were made.")
        reports, authorized = [], False

        def write_permission():
            nonlocal authorized
            if not authorized:
                client.authorize()
                authorized = True

        for paper in papers:
            path, wanted = stage(ledger, paper)
            parent_key = reference_keys.get(paper.reference) or stable_key(namespace, "reference", paper.reference)
            adopted = paper.reference in reference_keys
            key = attachment_keys.get(paper.source_id) or stable_key(namespace, "attachment", paper.source_id)
            parent, attachment = client.get("item", parent_key), client.get("item", key)
            state = ledger.get(paper.source_id)
            upload_intent = ledger.upload_intent(paper.source_id)
            if upload_intent and upload_intent.get("key") != key:
                raise PilotError("The pending upload belongs to another attachment. Review its mapping.")
            native_text = client.native_annotation_text(key) if attachment and hasattr(client, "native_annotation_text") else []
            index_token = hashlib.sha256(json.dumps([INDEX_REVISION, paper.sha256, paper.annotations,
                                                     native_text,
                                                     getattr(client, "index_profile", "native-text")],
                                                     sort_keys=True).encode()).hexdigest()
            owned = marker(namespace, paper.reference)
            if adopted and (not parent or parent.get("itemType") in {"attachment", "annotation", "note"}
                            or root_key not in parent.get("collections", [])):
                raise PilotError("An explicitly linked Zotero reference is missing or left its mapped collection.")
            if parent and (parent.get("deleted") or (not adopted and owned not in parent.get("extra", "").splitlines())):
                raise PilotError("A pilot reference was deleted or its ownership marker changed. Review it before retrying.")
            if attachment and (attachment.get("deleted") or attachment.get("parentItem") != parent_key
                    or marker(namespace, paper.source_id) not in attachment.get("note", "")
                    or attachment.get("linkMode") != "imported_file"):
                raise PilotError("A pilot attachment was moved, deleted, or changed ownership. It was retained.")
            if state["published"] and (not parent or not attachment):
                raise PilotError("A previously published item is missing. Restore it; the pilot will not recreate deleted work.")
            current = client.file_md5(key) if attachment else ""
            if current and current not in {wanted, state["md5"], state["pending_md5"]}:
                raise PilotError("The Zotero PDF was edited outside this helper. It was preserved for review.")
            if not current and state["published"]:
                raise PilotError("A published Zotero PDF is missing. Restore it before publishing.")
            if current != wanted and attachment and client.native_annotation_count(key):
                raise PilotError("This PDF has Zotero reader annotations. Review them before replacing its tablet view.")
            action = "create" if not attachment else "unchanged" if current == wanted else "update"
            source_link = ("Notable source attachment: " + paper.source_attachment_key) if paper.source_attachment_key else ""
            needs_link = bool(source_link and attachment and source_link not in attachment.get("note", ""))
            if needs_link and "Notable source attachment:" in attachment.get("note", ""):
                raise PilotError("The tablet view's original Zotero attachment link changed. Review its mapping.")
            result = {"source_id": paper.source_id, "reference_key": parent_key,
                      "attachment_key": key, "action": action, "applied": False}
            reports.append(result)
            if not apply:
                continue
            needs_reader = (getattr(client, "can_refresh_reader", False)
                            and ledger.reader_revision(paper.source_id) != paper.sha256)
            if (action == "unchanged" and state["indexed_sha"] == index_token
                    and not needs_reader and not needs_link and not upload_intent):
                expected_index = ledger.index_digest(paper.source_id)
                if expected_index and client.index_digest(key) == expected_index:
                    result["applied"] = True
                    continue
            write_permission()
            if not root:
                root = client.create("collection", {"key": root_key, "name": "Notable Pilot"})
                if not root:
                    raise PilotError("Pilot collection creation was not confirmed; retry this manifest.")
            ledger.collection_seen(root_key, save=True)
            if not parent:
                record = {"itemType": "document", "title": paper.path.stem, **paper.metadata,
                          "key": parent_key, "collections": [root_key]}
                record["extra"] = (str(record.get("extra", "")) + "\n" + owned).strip()
                parent = client.create("item", record)
                if not parent:
                    raise PilotError("Reference creation was not confirmed; retry this manifest.")
            if not attachment:
                attachment = client.create("item", {"key": key, "itemType": "attachment",
                    "parentItem": parent_key, "linkMode": "imported_file", "contentType": "application/pdf",
                    "title": "Supporting information" if paper.role == "si" else "Main text (reMarkable notes)",
                    "filename": paper.filename or "Current.pdf", "note": marker(namespace, paper.source_id)
                    + ("<p>" + source_link + "</p>" if source_link else "")})
                if not attachment:
                    raise PilotError("Attachment creation was not confirmed; retry this manifest.")
            elif needs_link:
                attachment["note"] = attachment.get("note", "") + "<p>" + source_link + "</p>"
                client.update(attachment)
            if current != wanted:
                ledger.save(paper.source_id, pending_md5=wanted)
            upload_attachment(client, ledger, paper.source_id, key, path, current, wanted)
            ledger.save(paper.source_id, published=1, md5=wanted, pending_md5="")
            if needs_reader:
                client.refresh_reader(key)
                ledger.reader_revision(paper.source_id, paper.sha256)
            # Retry indexing independently after a crash or failed API response.
            result["pages_without_text"] = client.index(key, path, paper.annotations)
            ledger.index_digest(paper.source_id, client.index_digest(key))
            # Native text remains searchable after an OCR error, but that
            # partial index must not suppress recognition on the next sync.
            ledger.save(paper.source_id, indexed_sha=index_token if getattr(client, 'index_complete', True) else '')
            diagnostics = getattr(client, 'index_diagnostics', [])
            if diagnostics:
                result['index_warnings'] = diagnostics[-1]
            result["applied"] = True
        return {"server_id": client.server_id, "collection_key": root_key,
                "mode": "apply" if apply else "plan", "papers": reports}
