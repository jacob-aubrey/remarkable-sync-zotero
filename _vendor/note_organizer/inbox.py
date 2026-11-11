"""Persistent, opt-in download Inbox; no Tk or cloud-transfer side effects.

Naming calls can perform explicitly configured DOI/AI requests. Merely scanning
the Inbox never sends document text or creates cloud documents. ``post`` runs
on the calling worker thread and must be marshalled by the UI.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import threading
import time
import uuid
import zipfile

from .storage import canonical, timestamp
from .remarkable_cloud import CloudError, _delete_guarded, _guarded_file
from .sorting.document_types import is_processable_document
from .sorting.key_protection import (
    _as_bool, _protect_for_current_windows_user, _unprotect_for_current_windows_user,
)


class DownloadNotReady(ValueError):
    """A download is incomplete, changing, or changed while under review."""


DEFAULT_CONFIG = {
    "enabled": False,
    "watch_folder": "",
    "library_root": "",
    "naming_mode": "Automatic",
    "filename_format": "smart",
    "custom_filename_template": "",
    "online_metadata_lookup_enabled": True,
    "gemini_enabled": False,
    "gemini_model": "gemini-3.5-flash-lite",
    "allow_cloud_ai_for_word_documents": False,
    "allow_cloud_ai_for_presentation_documents": False,
}
_BOOLEAN_FIELDS = {key for key, value in DEFAULT_CONFIG.items() if isinstance(value, bool)}
_PATH_FIELDS = {"watch_folder", "library_root"}
MAX_IMPORT_CONFIG_BYTES = 1024 * 1024


def file_signature(path):
    stat = Path(path).stat()
    return json.dumps([stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns])


def _contained(path, root):
    return Path(path).resolve().is_relative_to(Path(root).resolve())


def _digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _finish_verified_copy(source, target, signature, digest, *, source_required=True):
    """Delete the verified source while its verified copy remains protected.

    Both guards deny already-open writers, new writes, and pathname replacement.
    The hashes and deletion use those same handles; unsupported platforms retain
    both locations for review rather than relying on a check-then-unlink gap.
    """
    def hold_target(kernel, handle):
        if not source.exists():
            if source_required:
                raise DownloadNotReady("The source changed just before filing completed. Both locations were retained for review.")
            return

        def remove_source(source_kernel, source_handle):
            if file_signature(source) != signature:
                raise DownloadNotReady("The source changed just before filing completed. Both copies were retained for review.")
            _delete_guarded(source_kernel, source_handle)

        _guarded_file(source, digest, remove_source, item_label="Inbox source")

    try:
        _guarded_file(target, digest, hold_target, item_label="filed document")
    except CloudError as error:
        raise DownloadNotReady(f"Filing could not be completed safely. {error} Both locations were retained for review.") from error


def _complete_document(path):
    """Reject obvious partial downloads before a file enters the review queue."""
    path = Path(path)
    size = path.stat().st_size
    if size <= 0:
        return False
    with path.open("rb") as stream:
        header = stream.read(8)
        if path.suffix.casefold() == ".pdf":
            if not header.startswith(b"%PDF-"):
                return False
            stream.seek(max(0, size - 2048))
            # An earlier EOF can occur inside an incremental update; a paused
            # download after that old marker still must not be treated as done.
            return stream.read().rstrip().endswith(b"%%EOF")
        if path.suffix.casefold() == ".ppt":
            return header == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            required = "word/document.xml" if path.suffix.casefold() == ".docx" else "ppt/presentation.xml"
            return required in names and "[Content_Types].xml" in names
    except (OSError, zipfile.BadZipFile):
        return False


def _clean_config(data, base=None):
    from .sorting.core_logic import FILENAME_FORMATS, validate_filename_template
    result = {**DEFAULT_CONFIG, **(base or {})}
    if base and base.get("api_key_protected") and "gemini_model" not in base:
        result["gemini_model"] = "gemini-2.5-flash"
    for key in DEFAULT_CONFIG:
        if key not in data:
            continue
        value = data[key]
        if key in _BOOLEAN_FIELDS:
            result[key] = _as_bool(value, DEFAULT_CONFIG[key])
        elif key in _PATH_FIELDS:
            result[key] = str(Path(str(value)).expanduser().resolve()) if value else ""
        else:
            result[key] = str(value or "")
    result["naming_mode"] = "Basic" if result["naming_mode"] == "Basic" else "Automatic"
    import re
    if not re.fullmatch(r"gemini-[a-zA-Z0-9._-]{1,100}", result["gemini_model"]):
        raise ValueError("Enter a Gemini model ID, such as gemini-3.5-flash-lite.")
    if result["filename_format"] not in FILENAME_FORMATS:
        raise ValueError("Choose a supported filename format.")
    if result["filename_format"] == "custom":
        validate_filename_template(result["custom_filename_template"])
    watch, library = result["watch_folder"], result["library_root"]
    if watch and library and canonical(watch) == canonical(library):
        raise ValueError("The download Inbox and sorted library must be different folders.")
    if watch and library and _contained(library, watch):
        raise ValueError("The sorted library cannot be inside the download Inbox.")
    if watch and library and _contained(watch, library):
        raise ValueError("The download Inbox cannot be inside the sorted library.")
    if result["enabled"]:
        if not watch or not library:
            raise ValueError("Choose the download Inbox and library folders before enabling watching.")
        if not Path(watch).is_dir() or not Path(library).is_dir():
            raise ValueError("The download Inbox and library folders must exist before enabling watching.")
    return result


class InboxController:
    def __init__(self, store, data_dir, post=None):
        self.store = store
        self.data_dir = Path(data_dir).resolve()
        self.post = post or (lambda event, payload: None)
        self._lock = threading.RLock()
        self._observed = {}
        self._stop = threading.Event()
        self._thread = None
        self._executor = None
        self._closed = False
        self._initialized = False
        self.activity_state = None
        self._saved_config = {}
        self._metadata_generation = 0
        self.stable_seconds = 2.0
        # Construction and config() must remain safe on Tk even when another
        # process holds an exclusive database lock. Schema/settings reads run
        # only when a background workflow first uses this controller.

    def set_config_snapshot(self, saved):
        """Hydrate cached UI settings from an already-read LibraryView snapshot."""
        self._saved_config = dict(saved) if isinstance(saved, dict) else {}

    def initialize(self):
        """Lazy worker initialization; never call this method on the Tk thread."""
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            with self.store.connect() as db:
                db.executescript("""
                CREATE TABLE IF NOT EXISTS inbox_items (
                    path TEXT PRIMARY KEY, signature TEXT NOT NULL,
                    status TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}',
                    proposed_filename TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS inbox_jobs (
                    id TEXT PRIMARY KEY, source TEXT NOT NULL, path TEXT NOT NULL,
                    signature TEXT NOT NULL, digest TEXT NOT NULL DEFAULT '',
                    details TEXT NOT NULL DEFAULT '{}', state TEXT NOT NULL,
                    created_at TEXT NOT NULL, error TEXT NOT NULL DEFAULT '');
                CREATE INDEX IF NOT EXISTS inbox_job_state ON inbox_jobs(state);
                """)
                # A process interrupted during extraction must not strand a paper
                # forever in the transient preparing state after the next launch.
                db.execute("UPDATE inbox_items SET status='waiting' WHERE status='preparing'")
            self.set_config_snapshot(self.store.preference("inbox", {}))
            self._initialized = True

    def _emit(self, event, payload):
        try:
            self.post(event, payload)
        except Exception:
            # A UI callback failure does not undo an already committed filing.
            logging.exception("Inbox event delivery failed: %s", event)

    def config(self):
        saved = self._saved_config
        result = {**DEFAULT_CONFIG, **{key: saved[key] for key in DEFAULT_CONFIG if key in saved}}
        if saved.get("api_key_protected") and "gemini_model" not in saved:
            result["gemini_model"] = "gemini-2.5-flash"
        result["api_key_present"] = bool(saved.get("api_key_protected"))
        return result

    def save_config(self, data, *, persist_preferences=None):
        """Commit the settings draft and review invalidation as one transaction.

        An optional ``persist_preferences(db)`` callback can save the other
        Preferences tabs in the same transaction. Cached settings and naming
        generations are published only after that transaction has committed.
        """
        self.initialize()
        with self._lock:
            saved = self.store.preference("inbox", {})
            result = _clean_config(data, saved)
            # Plaintext Gemini keys never enter the stored preference or public
            # result dictionaries.
            result.pop("api_key", None)
            result.pop("api_key_present", None)
            if "api_key" in data:
                secret = str(data["api_key"] or "").strip()
                result.pop("api_key_protected", None)
                if secret:
                    result["api_key_protected"] = _protect_for_current_windows_user(secret)
            if "api_key_protected" in data:
                raise ValueError("Use the API-key setting or explicit sorter import, not protected-key text.")
            if result["gemini_enabled"]:
                if not result.get("api_key_protected"):
                    raise ValueError("Save your own Gemini key before enabling Gemini naming.")
                try:
                    if not _unprotect_for_current_windows_user(result["api_key_protected"]):
                        raise ValueError("The saved key is empty.")
                except Exception:
                    raise ValueError("The saved Gemini key is unavailable for this Windows account. Enter your own key before enabling Gemini naming.") from None
            self._validate_exclusions(result)
            metadata_options = {"naming_mode", "online_metadata_lookup_enabled",
                                "gemini_enabled", "gemini_model",
                                "allow_cloud_ai_for_word_documents", "allow_cloud_ai_for_presentation_documents",
                                "api_key_protected"}
            metadata_changed = any(saved.get(key, DEFAULT_CONFIG.get(key)) != result.get(key, DEFAULT_CONFIG.get(key))
                                   for key in metadata_options)
            with self.store.connect() as db:
                db.execute("INSERT OR REPLACE INTO preferences(key,value) VALUES(?,?)",
                           ("inbox", json.dumps(result)))
                if metadata_changed:
                    self._invalidate_reviews(db)
                if persist_preferences is not None:
                    persist_preferences(db)
            self.set_config_snapshot(result)
            if metadata_changed:
                self._metadata_generation += 1
            self._observed.clear()
        self._emit("inbox_config_changed", self.config())
        try:
            self.stop_watching()
            self.start()
        except Exception as error:
            # The draft is already committed. A watcher startup failure must
            # not masquerade as a failed save or promise that Cancel restores it.
            self._emit("inbox_error", {"error": f"Preferences were saved, but automatic Inbox checks could not restart: {error}"})
        return self.config()

    @staticmethod
    def _invalidate_reviews(db):
        db.execute("""UPDATE inbox_items SET status='ready',details='{}',
                      proposed_filename='',error='' WHERE status IN ('review','error','preparing')""")

    def _key(self):
        if not self.config()["gemini_enabled"]:
            return ""
        saved = self.store.preference("inbox", {})
        try:
            return _unprotect_for_current_windows_user(saved["api_key_protected"]) if saved.get("api_key_protected") else ""
        except Exception:
            return ""

    def remove_api_key(self):
        """Remove this library's saved key without applying any settings draft."""
        self.initialize()
        with self._lock:
            saved = dict(self.store.preference("inbox", {}))
            had_key = bool(saved.pop("api_key_protected", None) or saved.get("api_key"))
            saved.pop("api_key", None)
            saved["gemini_enabled"] = False
            metadata_changed = had_key and saved.get("naming_mode", "Automatic") != "Basic"
            with self.store.connect() as db:
                db.execute("INSERT OR REPLACE INTO preferences(key,value) VALUES(?,?)",
                           ("inbox", json.dumps(saved)))
                if metadata_changed:
                    self._invalidate_reviews(db)
            self.set_config_snapshot(saved)
            if metadata_changed:
                self._metadata_generation += 1
        result = self.config()
        self._emit("inbox_config_changed", result)
        return result

    def _excluded_roots(self):
        config = self.store.preference("remarkable", {})
        review = config.get("review_dir") or str(self.data_dir / "reMarkable Review")
        return [self.data_dir, Path(review).resolve(), (self.store.path.parent / "remarkable").resolve()]

    def _validate_exclusions(self, config):
        for name in ("watch_folder", "library_root"):
            value = config.get(name)
            if value and any(_contained(value, root) for root in self._excluded_roots()):
                raise ValueError("Choose a folder outside the app's data, cache, and reMarkable review areas.")

    def _managed(self, path):
        if any(_contained(path, root) for root in self._excluded_roots()):
            return True
        value = canonical(path)
        for item in self.store.remarkable_items().values():
            if any(item.get(key) and canonical(item[key]) == value for key in ("path", "review_pdf")):
                return True
        return False

    def _source(self, path):
        self.initialize()
        path = Path(path)
        watch = self.config()["watch_folder"]
        if not watch or not _contained(path, watch):
            raise ValueError("Choose a document in the configured download Inbox.")
        if path.is_symlink() or not path.is_file() or not is_processable_document(path):
            raise ValueError("Choose a supported PDF, Word, or PowerPoint document.")
        if self._managed(path):
            raise ValueError("Managed tablet downloads and app cache files cannot re-enter the sorting Inbox.")
        return path.resolve()

    def _row(self, path):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM inbox_items WHERE path=?", (canonical(path),)).fetchone()
        if not row:
            return None
        row = dict(row)
        row["details"] = json.loads(row["details"])
        row["name"] = Path(row["path"]).name
        return row

    def _save_item(self, path, signature, status, details=None, proposed_filename="", error=""):
        with self.store.connect() as db:
            db.execute("""INSERT OR REPLACE INTO inbox_items
                (path,signature,status,details,proposed_filename,error,updated_at)
                VALUES (?,?,?,?,?,?,?)""", (canonical(path), signature, status,
                    json.dumps(details or {}), proposed_filename, error, timestamp()))

    def list_items(self):
        self.initialize()
        watch = self.config()["watch_folder"]
        if not watch:
            return []
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM inbox_items WHERE status NOT IN ('filed','missing') ORDER BY path").fetchall()
        errors = {}
        for job in self.list_recovery_errors():
            errors.setdefault(job["source"], []).append(job)
        result = []
        for value in rows:
            if not _contained(value["path"], watch):
                continue
            row = dict(value)
            row["name"] = Path(row["path"]).name
            row["details"] = json.loads(row["details"])
            row["recovery_errors"] = errors.get(row["path"], [])
            result.append(row)
        return result

    def refresh(self):
        """Discover files without extracting content or contacting services."""
        self.initialize()
        config = self.config()
        watch = config["watch_folder"]
        if not watch or not Path(watch).is_dir():
            recovered, errors = self._recover_jobs()
            self._notify_recovery(recovered, errors)
            return []
        self._validate_exclusions(config)
        with self._lock:
            now, present = time.monotonic(), set()
            # Only the download Inbox itself is watched. Final papers in nested
            # destination folders never become new incoming download jobs.
            for path in Path(watch).iterdir():
                if not path.is_file() or path.is_symlink() or not is_processable_document(path) or self._managed(path):
                    continue
                try:
                    key, signature = canonical(path), file_signature(path)
                    present.add(key)
                    previous = self._row(path)
                    observation = self._observed.get(key)
                    if observation is None or observation[0] != signature:
                        self._observed[key] = (signature, now)
                        stable = False
                    else:
                        stable = now - observation[1] >= self.stable_seconds
                    if previous and previous["signature"] == signature and previous["status"] in {"review", "skipped", "preparing", "error"}:
                        continue
                    complete = stable and _complete_document(path)
                    status = "ready" if complete else "waiting"
                    if not previous or previous["signature"] != signature or previous["status"] != status:
                        self._save_item(path, signature, status, error="" if complete else "Waiting for the download to finish.")
                except (OSError, ValueError):
                    continue
            with self.store.connect() as db:
                for row in db.execute("SELECT path FROM inbox_items WHERE status NOT IN ('filed','missing')").fetchall():
                    if _contained(row["path"], watch) and row["path"] not in present:
                        db.execute("UPDATE inbox_items SET status='missing',updated_at=? WHERE path=?", (timestamp(), row["path"]))
            recovered, errors = self._recover_jobs()
        self._notify_recovery(recovered, errors)
        rows = self.list_items()
        self._emit("inbox_changed", {"count": len(rows)})
        return rows

    def prepare(self, path, force=False):
        """Extract one reviewed file; call from a worker, never directly from Tk."""
        from .sorting.core_logic import get_basic_document_details, get_document_details, build_proposed_filename
        from .supplement_library import source_details
        path = self._source(path)
        with self._lock:
            config = self.config()
            generation = self._metadata_generation
            row = self._row(path)
            signature = file_signature(path)
            if not row or row["signature"] != signature or row["status"] == "waiting":
                self.refresh()
                row = self._row(path)
            if not row or row["signature"] != signature or row["status"] == "waiting" or not _complete_document(path):
                raise DownloadNotReady("The file is still downloading or has changed. Refresh after the download finishes.")
            if row["status"] == "preparing":
                raise DownloadNotReady("This file is already being prepared.")
            if row["status"] == "review" and row["details"] and not force:
                # The user may have identified this file from the parent SI
                # dialog after its first Inbox review was already cached.
                details = source_details(self.store, path) or row["details"]
                proposal = build_proposed_filename(details, path.suffix,
                    filename_format=config["filename_format"], custom_template=config["custom_filename_template"])
                if proposal != row["proposed_filename"] or details != row["details"]:
                    self._save_item(path, signature, "review", details, proposal)
                return {**details, "path": str(path), "signature": signature,
                        "proposed_filename": proposal}
            self._save_item(path, signature, "preparing")
        try:
            details = source_details(self.store, path)
            if details is None and config["naming_mode"] == "Basic":
                details = get_basic_document_details(path)
            elif details is None:
                suffix = path.suffix.casefold()
                cloud = config["gemini_enabled"] and (suffix == ".pdf" or
                         (suffix == ".docx" and config["allow_cloud_ai_for_word_documents"]) or
                         (suffix == ".pptx" and config["allow_cloud_ai_for_presentation_documents"]))
                details = get_document_details(path, self._key(), allow_cloud_ai=cloud,
                    allow_online_metadata_lookup=config["online_metadata_lookup_enabled"], model=config["gemini_model"])
            if not details:
                raise ValueError("Document information could not be read. You can still name the file manually.")
            if file_signature(path) != signature:
                raise DownloadNotReady("The file changed while its information was being read. Review the new version.")
            proposal = build_proposed_filename(details, path.suffix,
                filename_format=config["filename_format"], custom_template=config["custom_filename_template"])
            with self._lock:
                if generation != self._metadata_generation:
                    raise DownloadNotReady("Naming settings changed while this document was being identified. Review it again.")
                if file_signature(path) != signature:
                    raise DownloadNotReady("The file changed while its information was being read. Review the new version.")
                current = self.config()
                if any(current[key] != config[key] for key in ("filename_format", "custom_filename_template")):
                    proposal = build_proposed_filename(details, path.suffix,
                        filename_format=current["filename_format"], custom_template=current["custom_filename_template"])
                self._save_item(path, signature, "review", details, proposal)
            result = {**details, "path": str(path), "signature": signature, "proposed_filename": proposal}
            self._emit("inbox_prepared", result)
            return result
        except Exception as error:
            with self._lock:
                # A committed settings change already invalidated this review.
                # An old worker must not replace a newer ready/preparing row.
                if generation == self._metadata_generation:
                    self._save_item(path, signature, "error", error=str(error))
            raise

    def _submit(self, function, *args):
        with self._lock:
            if self._closed:
                raise RuntimeError("The Inbox is closed.")
            if self._executor is None:
                self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="Inbox-review")
            return self._executor.submit(function, *args)

    def prepare_async(self, path, force=False):
        def work():
            self.activity_state = {"phase": "Identifying Inbox document", "detail": Path(path).name}
            try:
                return self.prepare(path, force=force)
            except Exception as error:
                self._emit("inbox_error", {"path": str(path), "error": str(error)})
                raise
            finally:
                self.activity_state = None
        return self._submit(work)

    def _exclusive_copy(self, source, desired, auto_suffix=False):
        """Publish a complete copy without overwriting a simultaneous creation."""
        desired = Path(desired)
        temporary = desired.parent / (".inbox-copy-" + uuid.uuid4().hex + ".tmp")
        candidate, index = desired, 1
        try:
            digest = hashlib.sha256()
            with temporary.open("xb") as stream, Path(source).open("rb") as incoming:
                for chunk in iter(lambda: incoming.read(1024 * 1024), b""):
                    stream.write(chunk)
                    digest.update(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            shutil.copystat(source, temporary)
            while True:
                try:
                    if os.name == "nt":
                        # On Windows rename always refuses an existing target.
                        os.rename(temporary, candidate)
                    else:
                        # POSIX rename replaces targets, so use an exclusive
                        # hard-link publication within this same filesystem.
                        os.link(temporary, candidate)
                        temporary.unlink()
                    break
                except FileExistsError:
                    if not auto_suffix:
                        raise FileExistsError("A document with that name already exists. Choose another name or review the existing paper.") from None
                    index += 1
                    candidate = desired.with_name(f"{desired.stem} ({index}){desired.suffix}")
            return candidate, digest.hexdigest()
        finally:
            temporary.unlink(missing_ok=True)

    def add_files(self, paths):
        self.initialize()
        config = self.config()
        watch = config["watch_folder"]
        if not watch or not Path(watch).is_dir():
            raise ValueError("Choose an existing download Inbox in Settings first.")
        self._validate_exclusions(config)
        copied = []
        for value in paths:
            source = Path(value)
            if source.is_symlink() or not source.is_file() or not is_processable_document(source):
                raise ValueError("Only supported PDF, Word, or PowerPoint documents can be added.")
            if self._managed(source):
                raise ValueError("Managed tablet downloads and app cache files cannot re-enter the sorting Inbox.")
            if source.parent.resolve() == Path(watch).resolve():
                copied.append(str(source.resolve()))
                continue
            signature = file_signature(source)
            target, _ = self._exclusive_copy(source, Path(watch) / source.name, auto_suffix=True)
            if file_signature(source) != signature:
                raise DownloadNotReady("The source file changed while being copied. Both copies were retained for review; wait for its download to finish.")
            copied.append(str(target))
        self.refresh()
        return copied

    def file_item(self, path, filename, destination, expected_signature=None):
        """Persist intent, file without overwrite, then emit a confirmed job."""
        from .sorting.core_logic import validate_document_filename
        path = self._source(path)
        filename = validate_document_filename(filename, path.suffix)
        config = self.config()
        if not config["library_root"]:
            raise ValueError("Choose the sorted library root in Settings first.")
        folder = Path(destination).resolve()
        library = Path(config["library_root"]).resolve()
        if not library.is_dir() or not _contained(folder, library):
            raise ValueError("Choose a destination inside an existing sorted library.")
        if _contained(folder, config["watch_folder"]) or self._managed(folder):
            raise ValueError("Choose a destination outside the download Inbox, cache, and tablet review areas.")
        with self._lock:
            row = self._row(path)
            signature = file_signature(path)
            if (not row or row["signature"] != signature or row["status"] not in {"review", "error"}
                    or (expected_signature is not None and expected_signature != signature)):
                raise DownloadNotReady("The file changed or is not ready. Refresh and review it before filing.")
            if not _complete_document(path):
                raise DownloadNotReady("The download is incomplete. Wait for it to finish before filing.")
            # Folders are created only on the explicit File action, never while
            # scanning, reviewing metadata, or importing settings.
            current = library
            for component in folder.relative_to(library).parts:
                current = current / component
                if not _contained(current, library) or self._managed(current):
                    raise ValueError("The destination must remain inside the sorted library.")
                current.mkdir(exist_ok=True)
                if not current.is_dir() or not _contained(current, library):
                    raise ValueError("The destination changed. Choose a folder inside the sorted library.")
            job_id, target = uuid.uuid4().hex, folder / filename
            with self.store.connect() as db:
                tracked = db.execute("SELECT digest FROM supplement_files WHERE path=?", (canonical(target),)).fetchone()
                if tracked and tracked[0] != _digest(path):
                    raise ValueError("That location is still associated with a different supplementary file. Choose another filename.")
                db.execute("""INSERT INTO inbox_jobs (id,source,path,signature,details,state,created_at)
                    VALUES (?,?,?,?,?,'copying',?)""", (job_id, canonical(path), canonical(target),
                        signature, json.dumps(row["details"]), timestamp()))
            try:
                target, digest = self._exclusive_copy(path, target)
                if file_signature(path) != signature:
                    raise DownloadNotReady("The file changed during filing. Both copies were retained for review; review the new version.")
                # Copy completion is journalled before removing the inbox source.
                # A crash here can leave two copies, never zero copies.
                with self.store.connect() as db:
                    db.execute("UPDATE inbox_jobs SET digest=?,state='copied' WHERE id=?", (digest, job_id))
                _finish_verified_copy(path, target, signature, digest)
                with self.store.connect() as db:
                    db.execute("UPDATE inbox_jobs SET state='confirmed' WHERE id=?", (job_id,))
                    db.execute("UPDATE inbox_items SET status='filed',updated_at=? WHERE path=?", (timestamp(), canonical(path)))
                    from .supplement_library import filed
                    filed(db, path, target, digest, row["details"])
            except Exception as error:
                with self.store.connect() as db:
                    db.execute("UPDATE inbox_jobs SET state='error',error=? WHERE id=?", (str(error), job_id))
                raise
        result = {"job_id": job_id, "source": canonical(path), "path": canonical(target),
                  "details": row["details"], "digest": digest, "is_pdf": target.suffix.casefold() == ".pdf"}
        self._emit("inbox_filed", result)
        self.refresh()
        return result

    def _recover_jobs(self):
        """Finish a durable copy after a crash, only when both hashes agree."""
        recovered, errors = [], []
        with self._lock:
            with self.store.connect() as db:
                rows = db.execute("SELECT * FROM inbox_jobs WHERE state IN ('copying','copied') ORDER BY created_at").fetchall()
                for row in rows:
                    source, target = Path(row["source"]), Path(row["path"])
                    try:
                        if row["state"] == "copying":
                            # Ownership of an unjournalled target is ambiguous.
                            # Preserve every file and explicitly surface review.
                            state, error = "error", "Interrupted filing: the source and any destination copy were retained for review."
                        elif not target.is_file() or _digest(target) != row["digest"]:
                            state, error = "error", "The filed copy changed before recovery. Files were retained for review."
                        elif source.exists() and file_signature(source) != row["signature"]:
                            state, error = "error", "The download changed after copying. Both files were retained for review."
                        else:
                            _finish_verified_copy(source, target, row["signature"], row["digest"], source_required=False)
                            state, error = "confirmed", ""
                    except DownloadNotReady as problem:
                        state, error = "error", str(problem)
                    except OSError as problem:
                        # Keep the copy retryable, but persist the reason so a
                        # locked file does not disappear from recovery details.
                        state = "copied"
                        error = (f"Filing recovery could not finish safely: {problem}. "
                                 "Existing files were retained. Notable will retry automatically.")
                    db.execute("UPDATE inbox_jobs SET state=?,error=? WHERE id=?", (state, error, row["id"]))
                    if state == "confirmed":
                        db.execute("UPDATE inbox_items SET status='filed' WHERE path=?", (row["source"],))
                        from .supplement_library import filed
                        filed(db, source, target, row["digest"], json.loads(row["details"]))
                        recovered.append(self._filing_result(row))
                    elif error and (row["error"] != error or row["state"] != state):
                        errors.append(self._recovery_error(row, error))
        return recovered, errors

    def _notify_recovery(self, recovered, errors):
        for result in recovered:
            self._emit("inbox_filed", result)
        for error in errors:
            self._emit("inbox_recovery_error", error)

    @staticmethod
    def _filing_result(row):
        return {"job_id": row["id"], "source": row["source"], "path": row["path"],
                "details": json.loads(row["details"]), "digest": row["digest"],
                "is_pdf": Path(row["path"]).suffix.casefold() == ".pdf"}

    @staticmethod
    def _recovery_error(row, error=None):
        return {"job_id": row["id"], "source": row["source"], "path": row["path"],
                "error": row["error"] if error is None else error,
                "created_at": row["created_at"],
                "is_pdf": Path(row["path"]).suffix.casefold() == ".pdf"}

    def list_recovery_errors(self):
        """Return durable filing issues, including those with missing sources."""
        self.initialize()
        with self.store.connect() as db:
            rows = db.execute("""SELECT * FROM inbox_jobs WHERE state='error'
                OR (state='copied' AND error!='') ORDER BY created_at""").fetchall()
        return [self._recovery_error(row) for row in rows]

    def pending_filings(self):
        self.initialize()
        with self._lock:
            _, errors = self._recover_jobs()
            with self.store.connect() as db:
                rows = db.execute("SELECT * FROM inbox_jobs WHERE state='confirmed' ORDER BY created_at").fetchall()
        # Startup consumes the returned filings directly; emitting them as well
        # would queue the same handoff twice. New errors still need attention.
        self._notify_recovery([], errors)
        return [self._filing_result(row) for row in rows]

    def acknowledge_filing(self, job_id):
        self.initialize()
        with self.store.connect() as db:
            db.execute("UPDATE inbox_jobs SET state='handed_off' WHERE id=? AND state='confirmed'", (job_id,))

    def skip(self, path):
        path = self._source(path)
        with self._lock:
            row = self._row(path)
            self._save_item(path, file_signature(path), "skipped", row["details"] if row else {},
                row["proposed_filename"] if row else "")
        self._emit("inbox_changed", {"path": str(path)})

    def start(self):
        if self._closed or not self.config()["enabled"]:
            return False
        if self._thread and self._thread.is_alive() and not self._stop.is_set():
            return True
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._watch, args=(self._stop,), name="Inbox-watch", daemon=True)
        self._thread.start()
        return True

    def _watch(self, stop):
        while not stop.is_set():
            try:
                self.refresh()
            except Exception as error:
                self._emit("inbox_error", {"error": str(error)})
            stop.wait(2.5)

    def stop_watching(self, wait=True):
        self._stop.set()
        thread = self._thread
        if wait and thread and thread is not threading.current_thread():
            thread.join(timeout=3)
        if not thread or not thread.is_alive():
            self._thread = None

    def stop(self, wait=True):
        self._closed = True
        self.stop_watching(wait=wait)
        if self._executor:
            self._executor.shutdown(wait=wait, cancel_futures=True)
            if wait:
                self._executor = None

    def wait_stopped(self):
        """Finish all Inbox cleanup off the UI thread during app shutdown."""
        self.stop(wait=True)
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join()

    def preview_sorter_settings(self, path):
        """Read an explicit legacy file; preview never decrypts or mutates keys."""
        data = self._read_import(path)
        return {**self._import_fields(data, Path(path).parent), "enabled": False,
                "source_config_path": str(Path(path).resolve()),
                "api_key_present": bool(data.get("api_key_protected") or data.get("api_key"))}

    @staticmethod
    def _read_import(path):
        path = Path(path)
        if not path.is_file() or path.stat().st_size > MAX_IMPORT_CONFIG_BYTES:
            raise ValueError("Choose a sorter JSON settings file smaller than 1 MB.")
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("The sorter settings file must contain a JSON object.")
        return data

    @staticmethod
    def _import_fields(data, source_dir):
        fields = {key: data[key] for key in DEFAULT_CONFIG if key in data and key != "enabled"}
        fields["library_root"] = data.get("library_root", data.get("sorted_folder", ""))
        for key in _PATH_FIELDS:
            if fields.get(key) and not Path(str(fields[key])).is_absolute():
                fields[key] = str(Path(source_dir) / str(fields[key]))
        fields["enabled"] = False
        fields["gemini_enabled"] = False
        return _clean_config(fields)

    def import_sorter_settings(self, path, include_key=False):
        data = self._read_import(path)
        fields = self._import_fields(data, Path(path).parent)
        if include_key:
            protected = data.get("api_key_protected")
            fields["api_key"] = (_unprotect_for_current_windows_user(str(protected))
                if protected else str(data.get("api_key") or ""))
        return self.save_config(fields)
