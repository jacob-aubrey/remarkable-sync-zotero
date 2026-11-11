"""One worker owns all extraction and export; Tk only receives messages."""
import json
import logging
import os
from contextlib import contextmanager, nullcontext
from pathlib import Path
import queue
import threading
import time
from concurrent.futures import Future, TimeoutError

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .extraction import extract, signature, scan_fingerprint
from .exporting import export_notebook, EXPORT_FORMAT_REVISION
from .storage import canonical, timestamp, digest_file
from .remarkable_sync import RemarkableSync, excluded, excluded_roots, review_kind


class EventHandler(FileSystemEventHandler):
    def __init__(self, service):
        self.service = service

    def on_created(self, event):
        if event.is_directory:
            self.service.request_sync()
        else:
            self.service.enqueue(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self.service.enqueue(event.src_path)

    def on_moved(self, event):
        if event.is_directory:
            self.service.request_sync()
        elif any(Path(p).suffix.lower() == ".pdf" for p in (event.src_path, event.dest_path)):
            self.service.enqueue(event.dest_path)
            self.service.request_sync()

    def on_deleted(self, event):
        if event.is_directory or Path(event.src_path).suffix.lower() == ".pdf":
            self.service.request_sync()


class Service:
    def __init__(self, store, debounce=2.0, interval=300, *, pdf_worker=None):
        self.store = store
        self.pdf_worker = pdf_worker
        self.debounce, self.interval = debounce, interval
        self.messages = queue.Queue()
        self.commands = queue.Queue()
        self.pending = {}
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.observer = None
        self.watched_roots = None
        self.thread = None
        self.queued_checks = set()
        self.cloud_busy = False
        self.sync_requested = False
        self.scan_context = None
        self.cloud_paths = set()
        self.cloud_projects = set()
        self.activity_state = None
        self.remarkable = RemarkableSync(store, self.stop_event, self.post)
        if self.pdf_worker is not None:
            self.remarkable.renderer = self.pdf_worker.render
            self.remarkable.extractor = self.pdf_worker.extract
            self.remarkable.combined_renderer = getattr(self.pdf_worker, "render_combined", None)
            self.remarkable.tablet_renderer = getattr(self.pdf_worker, "tablet_only", None)

    def post(self, text, refresh=False):
        if self.cloud_busy and self.activity_state and self.activity_state["phase"] == "Checking reMarkable" and text:
            self.activity_state = {**self.activity_state, "detail": text}
        self.messages.put((text, refresh))

    @contextmanager
    def activity(self, phase, detail="", current=None, total=None):
        # Assign complete snapshots; the interface can read this without
        # sharing a lock or touching the worker's database connections.
        previous = self.activity_state
        self.activity_state = {"phase": phase, "detail": detail, "current": current, "total": total}
        try:
            yield
        finally:
            self.activity_state = previous

    def start(self):
        self.thread = threading.Thread(target=self.run, name="PDF notebook worker", daemon=False)
        # Queue startup work before starting the worker, so its initial cloud
        # check and local discovery share one scan instead of racing each other.
        self.command("watch")
        self.request_sync()
        self.thread.start()

    def command(self, name, *args):
        if name in {"remarkable_sync", "remarkable_browse"}:
            with self.lock:
                if name in self.queued_checks:
                    return
                self.queued_checks.add(name)
        self.commands.put((name, args))
        self.wake.set()

    def request_sync(self):
        with self.lock:
            self.sync_requested = True
        self.wake.set()

    def enqueue(self, path):
        if Path(path).suffix.lower() != ".pdf" or excluded(self.store, path):
            return
        with self.lock:
            self.pending[canonical(path)] = (time.monotonic() + self.debounce, 0)
        self.wake.set()

    def due(self, now):
        with self.lock:
            ready = [(p, attempt) for p, (deadline, attempt) in self.pending.items() if deadline <= now]
            for p, _ in ready:
                del self.pending[p]
        return ready

    def watch(self):
        excluded_paths = excluded_roots(self.store)
        roots = tuple(p["root"] for p in self.store.projects()
                      if not any(Path(p["root"]).resolve().is_relative_to(root) for root in excluded_paths)
                      and Path(p["root"]).is_dir())
        if self.observer and roots == self.watched_roots and self.observer.is_alive():
            return
        if self.observer:
            self.observer.stop()
            self.observer.join(timeout=3)
        self.observer = Observer()
        handler = EventHandler(self)
        complete = True
        for root in roots:
            try:
                self.observer.schedule(handler, root, recursive=True)
            except OSError as exc:
                complete = False
                self.post(f"Watching unavailable for {root}: {exc}. Periodic scans remain active.")
        try:
            self.observer.start()
            self.watched_roots = roots if complete else None
        except OSError as exc:
            self.watched_roots = None
            self.post(f"Live watching failed: {exc}. Periodic scans remain active.")

    @contextmanager
    def scan_batch(self, projects=None):
        """Keep small routing/error snapshots for one serialized scan batch."""
        previous = self.scan_context
        projects = self.store.projects() if projects is None else projects
        self.scan_context = {"projects": projects, "excluded": excluded_roots(self.store),
                             "cloud_items": self.store.remarkable_items(),
                             "errors": {p["id"]: {r["path"] for r in self.store.errors(p["id"])}
                                        for p in projects}}
        try:
            with self.store.supplement_batch():
                yield
        finally:
            self.scan_context = previous

    def is_excluded(self, path):
        if self.scan_context is None:
            return excluded(self.store, path)
        path = Path(path).resolve()
        return any(path.is_relative_to(root) for root in self.scan_context["excluded"])

    def projects_for(self, path):
        if self.is_excluded(path):
            return []
        # A paper can belong to both a parent and child project, independently.
        projects = self.scan_context["projects"] if self.scan_context else self.store.projects()
        return [p for p in projects if Path(path).is_relative_to(Path(p["root"]))]

    def retained_reading_copy(self, path):
        """Keep an unedited legacy render for link review without rescanning it."""
        items = self.scan_context["cloud_items"] if self.scan_context else self.store.remarkable_items()
        for item in items.values():
            if item.get("notes_migrated") and item.get("legacy_path") == canonical(path):
                return digest_file(path) == item.get("legacy_digest")
        return False

    def scan(self, project, path, force=False):
        if self.is_excluded(path) or self.retained_reading_copy(path):
            return
        settings = json.loads(project["settings"])
        fingerprint = scan_fingerprint(settings)
        old = self.store.document_at(project["id"], path)
        errors = (self.scan_context["errors"][project["id"]] if self.scan_context else
                  {r["path"] for r in self.store.errors(project["id"])})
        previous_error = canonical(path) in errors
        if not force and not previous_error and old and not old["missing"] and not old["hidden"] and old["signature"] == signature(path) and old["settings"] == fingerprint:
            return
        phase = "Updating saved notes" if old and old["settings"] != fingerprint else "Reading PDF"
        self.post(f"{phase}: {Path(path).name}…")
        started = time.perf_counter()
        batch = self.activity_state or {}
        with self.activity(phase, Path(path).name, current=batch.get("current"), total=batch.get("total")):
            # Desktop launches isolate native PDF work in one shared process;
            # standalone services retain the directly callable extractor.
            digest, sig, metadata, entries = (self.pdf_worker.extract(path, settings) if self.pdf_worker is not None else
                                              extract(path, settings, self.stop_event.is_set))
            counts = self.store.save_scan(project["id"], path, digest, sig, fingerprint, metadata, entries)
            self.store.scan_result(project["id"], path)
            self.post(f"{Path(path).name}: {len(entries)} annotations; {counts['added']} added, "
                      f"{counts['updated']} updated, {counts['removed']} removed.", True)
            for warning in metadata.get("warnings", []):
                self.post(f"{Path(path).name}: {warning}")
            errors.discard(canonical(path))
            logging.info("PDF extraction finished: annotations=%d elapsed=%.3fs", len(entries), time.perf_counter() - started)
        # PyMuPDF stays on this single worker. Yield between documents so a
        # one-time extraction upgrade cannot monopolize the interpreter.
        self.stop_event.wait(.001)

    def scan_path(self, path, attempt=0):
        with self.scan_batch() if self.scan_context is None else nullcontext():
            self._scan_path(path, attempt)

    def _scan_path(self, path, attempt):
        for project in self.projects_for(path):
            try:
                self.scan(project, path)
            except Exception as exc:
                if self.stop_event.is_set():
                    return
                logging.exception("Scan failed: %s", path)
                self.scan_context["errors"][project["id"]].add(canonical(path))
                self.store.scan_result(project["id"], path, str(exc))
                self.post(f"Could not read {Path(path).name}: {exc}", True)
                if attempt < 4 and Path(path).exists():
                    with self.lock:
                        if path not in self.pending:
                            self.pending[path] = (time.monotonic() + min(60, 2 ** (attempt+1)), attempt+1)

    def sync(self, project_id=None, force=False):
        """Scan available PDFs and report folder, read, and export issues apart."""
        started = time.perf_counter()
        projects = [p for p in self.store.projects() if not project_id or p["id"] == project_id]
        try:
            with self.activity("Checking local PDFs"), self.scan_batch(projects):
                self._sync(projects, force)
        finally:
            logging.info("Local scan finished: folders=%d elapsed=%.3fs", len(projects), time.perf_counter() - started)

    def _sync(self, projects, force):
        summary = {"at": timestamp(), "unavailable_folders": 0, "unreadable_folders": 0,
                   "pdf_errors": 0, "export_waiting": 0, "details": [], "cancelled": False}
        checked, discovered = 0, 0
        inventory = []
        self.activity_state = {"phase": "Finding local PDFs", "detail": "", "current": None, "total": None}
        for project in projects:
            if self.stop_event.is_set():
                return
            if self.is_excluded(project["root"]):
                continue
            if not Path(project["root"]).is_dir():
                summary["unavailable_folders"] += 1
                summary["details"].append({"kind": "unavailable_folder", "path": project["root"],
                                           "message": "Watched folder unavailable; saved notes are retained."})
                self.post(f"Folder unavailable: {project['root']}. Saved notes retained.")
                continue
            paths, walk_errors = [], []
            for folder, dirs, files in os.walk(project["root"], onerror=walk_errors.append):
                dirs[:] = [d for d in dirs if d not in {"Notebook backups", "notes_media", ".git"}
                           and not self.is_excluded(Path(folder) / d)]
                for name in files:
                    if self.stop_event.is_set():
                        return
                    if Path(name).suffix.lower() != ".pdf":
                        continue
                    path = canonical(Path(folder) / name)
                    if self.is_excluded(path) or self.retained_reading_copy(path):
                        continue
                    paths.append(path)
                    discovered += 1
                    self.activity_state = {"phase": "Finding local PDFs", "detail": name,
                                           "current": discovered, "total": None}
            inventory.append((project, paths, walk_errors))
        # Process the same inventory we counted, without walking folders twice.
        for project, paths, walk_errors in inventory:
            for path in paths:
                if self.stop_event.is_set():
                    return
                name = Path(path).name
                self.activity_state = {"phase": "Checking local PDFs", "detail": name,
                                       "current": checked, "total": discovered}
                try:
                    self.scan(project, path, force)
                except Exception as exc:
                    if self.stop_event.is_set():
                        return
                    summary["pdf_errors"] += 1
                    summary["details"].append({"kind": "pdf_error", "path": path, "message": str(exc)})
                    logging.exception("Scan failed: %s", path)
                    self.scan_context["errors"][project["id"]].add(path)
                    self.store.scan_result(project["id"], path, str(exc))
                    self.post(f"Could not read {name}: {exc}")
                    self.enqueue(path)
                if self.stop_event.is_set():
                    return
                checked += 1
                self.activity_state = {"phase": "Checking local PDFs", "detail": name,
                                       "current": checked, "total": discovered}
            if self.stop_event.is_set():
                return
            if walk_errors:
                summary["unreadable_folders"] += len(walk_errors)
                summary["details"].extend({"kind": "unreadable_folder", "path": str(exc.filename or project["root"]),
                                           "message": str(exc)} for exc in walk_errors)
                self.post(f"Some folders could not be read under {project['root']}; saved notes retained.")
            else:
                with self.activity("Refreshing saved tablet notes", Path(project["root"]).name):
                    self.sync_cloud_notes(project, force)
                self.store.mark_missing(project["id"], set(paths))
        self.activity_state = {"phase": "Checking local PDFs", "detail": "",
                               "current": checked, "total": discovered}
        self.export_pending()
        if self.stop_event.is_set():
            return
        for project in self.store.projects():
            if project["dirty"] or project["error"] or not Path(project["output"]).exists():
                summary["export_waiting"] += 1
                message = project["error"] or ("Notebook awaits an available watched folder."
                                               if not Path(project["root"]).is_dir() else "Notebook has not been saved yet.")
                summary["details"].append({"kind": "export_waiting", "path": project["output"], "message": message})
        self.store.set_preference("sync_summary", summary)
        issues = []
        for key, label in (("unavailable_folders", "unavailable folders"), ("unreadable_folders", "unreadable folders"),
                           ("pdf_errors", "PDF read errors"), ("export_waiting", "notebooks waiting to save")):
            if summary[key]:
                issues.append(f"{label}: {summary[key]}")
        self.post("Sync finished" + ("; " + "; ".join(issues) + ". See details." if issues else "."), True)

    def sync_cloud_notes(self, project, force=False):
        """Rebuild saved tablet notes after settings/folder changes, offline."""
        from types import SimpleNamespace
        from .cloud_notes import save_cloud_notes, document_path
        from .remarkable_sources import source_bundle, source_info
        for cloud_id, item in self.store.remarkable_items().items():
            primary = item.get("main_path", "")
            if (item.get("storage_version") != 2 or item.get("state") not in {"synced", "cloud unavailable"} or not primary
                    or review_kind(item) in {"conversion", "local_conflict"}
                    or not Path(primary).is_relative_to(Path(project["root"]))):
                continue
            old = self.store.document_at(project["id"], document_path(self.store, cloud_id))
            fingerprint = scan_fingerprint(json.loads(project["settings"]))
            if not force and old and old["settings"] == fingerprint and old["signature"] == item["hash"]:
                continue
            try:
                info = source_info(self.store, cloud_id, revision=item["hash"])
                if not info or info.get("cloud_hash") != item["hash"]:
                    raise ValueError("The retained source revision needs review before refreshing saved tablet notes.")
                save_cloud_notes(self.store, SimpleNamespace(id=cloud_id, hash=item["hash"], name=item["name"]),
                    source_bundle(self.store, cloud_id, revision=info["cloud_hash"]), primary, old=item,
                    extractor=self.pdf_worker.extract if self.pdf_worker else None,
                    tablet_renderer=getattr(self.pdf_worker, "tablet_only", None), cancelled=self.stop_event.is_set)
            except InterruptedError:
                raise
            except Exception as exc:
                self.post(f"Tablet notes retained; refresh needs review for {item['name']}: {exc}", True)

    def export_pending(self):
        self.store.flush_supplements()
        with self.activity("Saving notebooks"):
            self._export_pending()

    def upgrade_export_format(self):
        """Refresh generated notebooks once per layout change, without rescanning PDFs."""
        if self.store.preference("export_format_revision", 1) == EXPORT_FORMAT_REVISION:
            return
        with self.activity("Updating notebook format"):
            for project in self.store.projects():
                self.store.mark_dirty(project["id"])
            # Offline folders stay dirty until their normal export can succeed.
            self.store.set_preference("export_format_revision", EXPORT_FORMAT_REVISION)

    def export_images(self, entries):
        # Legacy highlight crops can be large. Fetch image payloads only for
        # drawings; printed highlights and typed comments use text/source links.
        return [{**e, "image": (e.get("image") or self.store.entry_image(
                    e["document_id"], e["key"], expected_digest=e.get("source_digest"),
                    expected_note_revision=e.get("source_note_revision")))
                 if e["kind"] == "drawing" else ""} for e in entries]

    def _export_pending(self):
        projects = [project for project in self.store.projects()
                    if (project["dirty"] or not Path(project["output"]).exists())
                    and Path(project["root"]).is_dir()]
        for completed, project in enumerate(projects):
            if self.stop_event.is_set():
                return
            # A watched drive can disappear after the inventory was collected.
            if not Path(project["root"]).is_dir():
                continue
            try:
                started = time.perf_counter()
                self.activity_state = {"phase": "Saving notebook", "detail": Path(project["output"]).name,
                                       "current": completed, "total": len(projects)}
                entries = self.export_images(self.store.entries(project_id=project["id"], include_images=False))
                value, backup = export_notebook(project["output"], f"Notes on {Path(project['root']).name}", entries, project["export_hash"],
                                               layout=json.loads(project["settings"]).get("notebook_style", "Reading"))
                self.store.export_result(project["id"], value)
                self.post(f"Notebook saved: {project['output']}", True)
                if backup:
                    self.post(f"A previous complete notebook copy was preserved at {backup}", True)
                logging.info("Notebook export finished: annotations=%d elapsed=%.3fs", len(entries), time.perf_counter() - started)
            except Exception as exc:
                logging.exception("Export failed")
                error = f"Export pending: {exc}. Close Word if it has this file open; retry is automatic."
                self.store.export_result(project["id"], error=error)
                self.post(error, True)
            finally:
                self.activity_state = {"phase": "Saving notebooks", "detail": Path(project["output"]).name,
                                       "current": completed + 1, "total": len(projects)}

    def check_cloud(self, force=False, listing_only=False):
        self.cloud_busy = True
        self.messages.put(("", True))
        started = time.perf_counter()
        try:
            before = self.store.remarkable_items() if not listing_only else {}
            project_ids = {p["id"] for p in self.store.projects()}
            with self.activity("Checking reMarkable"):
                changed = self.remarkable.sync(force=force, listing_only=listing_only)
                if not listing_only and not self.stop_event.is_set():
                    self.remarkable.process_outbox()
            if not listing_only:
                # The cloud engine already knows which outputs changed. Compare
                # its small manifests instead of walking every watched folder.
                paths_changed = False
                for item_id, item in self.store.remarkable_items().items():
                    old = before.get(item_id, {})
                    if item.get("state") == "synced" and item.get("path") and item.get("identity_registered") is not False and any(
                            item.get(key) != old.get(key) for key in ("path", "digest", "hash", "render_revision", "identity_registered")):
                        self.cloud_paths.add(item["path"])
                        paths_changed = True
                new_projects = {p["id"] for p in self.store.projects() if p["id"] not in project_ids}
                self.cloud_projects.update(new_projects)
                if changed or paths_changed or new_projects:
                    self.watch()
                changed = changed or paths_changed or bool(new_projects)
            return changed
        finally:
            self.cloud_busy = False
            self.messages.put(("", True))
            logging.info("reMarkable check finished: listing_only=%s elapsed=%.3fs", listing_only, time.perf_counter() - started)

    def filed_pdf(self, result):
        """Hand a confirmed Inbox filing to identity, local notes, and outbox."""
        from .paper_registry import Registry
        path = Path(result["path"]).resolve()
        if path.suffix.lower() != ".pdf" or excluded(self.store, path):
            return
        if not path.is_file():
            raise ValueError("The filed PDF is unavailable. Its saved Inbox job can be retried.")
        projects = self.projects_for(path)
        new_project = not projects
        if not projects:
            project_id = self.store.add_project(path.parent)
            projects = [self.store.project(project_id)]
        registry = Registry(self.store)
        for project in projects:
            registry.register_local(path, digest=result.get("digest"), project_id=project["id"],
                                    title=result.get("details", {}).get("title", ""),
                                    metadata=result.get("details", {}))
        self.remarkable.queue_pdf(path)
        self.watch()
        self.enqueue(path)
        if new_project:
            self.request_sync()
        # Store a durable acknowledgement only after identity and outbound
        # intent were saved. Replayed handoffs remain idempotent.
        if result.get("job_id"):
            handled = self.store.preference("inbox_handoffs", [])
            if result["job_id"] not in handled:
                self.store.set_preference("inbox_handoffs", handled + [result["job_id"]])
        self.post("Filed paper added to the library and queued for note analysis.", True)

    def save_preferences(self, draft, viewer, settings, integration_changes):
        """Wait for one serialized, atomic preferences commit."""
        if self.stop_event.is_set():
            raise InterruptedError("Preferences were not saved because the app is closing.")
        values = (dict(draft), dict(viewer), dict(settings), dict(integration_changes))
        if self.thread is None or not self.thread.is_alive() or threading.current_thread() == self.thread:
            return self.apply_preferences(*values)
        future = Future()
        self.command("preferences", values, future)
        return self.wait_for_action(future)

    def wait_for_action(self, future):
        while True:
            try:
                return future.result(timeout=.25)
            except TimeoutError:
                if future.done():
                    return future.result()
                if self.stop_event.is_set() and future.cancel():
                    raise InterruptedError("This action was not applied because the app is closing.")

    def paper_identity_action(self, action, values):
        if self.stop_event.is_set():
            raise InterruptedError("The app is closing; paper relationships were not changed.")
        if self.thread is None or not self.thread.is_alive() or threading.current_thread() == self.thread:
            return self.apply_paper_identity(action, values)
        future = Future()
        self.command("paper_identity", action, values, future)
        return self.wait_for_action(future)

    def storage_action(self, action, values):
        """Serialize an explicitly reviewed cleanup with sync and notebook writes."""
        if self.stop_event.is_set():
            raise InterruptedError("The app is closing; extra PDFs were retained.")
        if action in {"backup", "compact", "restore", "archive_cleanup", "recycle_pdf", "recycle_pdfs"}:
            if self.thread is not None and self.thread.is_alive() and threading.current_thread() != self.thread:
                future = Future()
                self.command("storage_maintenance", action, values, future)
                return self.wait_for_action(future)
            from . import maintenance
            inbox = getattr(self, "inbox", None)
            with inbox._lock if inbox is not None else nullcontext():
                if action == "recycle_pdf":
                    from .library_files import recycle_pdf
                    return recycle_pdf(self.store, values)
                if action == "recycle_pdfs":
                    from .library_files import recycle_pdfs
                    def progress(current, total, path):
                        self.activity_state = {"phase": "Removing reviewed PDFs", "current": current,
                                               "total": total, "detail": Path(path).name}
                    with self.activity("Removing reviewed PDFs", current=0, total=len(values)):
                        return recycle_pdfs(self.store, values, progress)
                if action == "archive_cleanup":
                    from .archive_storage import cleanup
                    return cleanup(self.store, values)
                if action == "restore":
                    return maintenance.restore(*values)
                return getattr(maintenance, action)(self.store, values)
        if action != "cleanup":
            raise ValueError("Choose a supported storage action.")
        from .source_storage import execute_legacy_cleanup
        if self.thread is None or not self.thread.is_alive() or threading.current_thread() == self.thread:
            return execute_legacy_cleanup(self.store, values, external_links_acknowledged=True)
        future = Future()
        self.command("storage_cleanup", values, future)
        return self.wait_for_action(future)

    def apply_paper_identity(self, action, values):
        if action == "link":
            from .reconciliation import link_registry_sources
            if isinstance(values, dict):
                return link_registry_sources(self.store, values["sources"], citation=values["citation"])
            return link_registry_sources(self.store, values)
        if action == "undo":
            from .paper_registry import Registry
            return Registry(self.store).undo_latest_link(values)
        raise ValueError("Choose a supported paper relationship action.")

    def apply_preferences(self, draft, viewer, settings, integration_changes):
        from .viewers import VIEWERS
        settings = {**self.store.default_settings(), **settings}
        if settings["grouping"] not in {"Nearby blocks", "Nearby lines", "Whole page"}:
            raise ValueError("Choose a supported drawing grouping mode.")
        if not 4 <= float(settings["gap"]) <= 100 or not 8 <= float(settings["context_padding"]) <= 200:
            raise ValueError("Use a stroke distance from 4 to 100 and context margin from 8 to 200.")
        if type(settings["ocr"]) is not bool or settings["notebook_style"] not in {"Reading", "Detailed"}:
            raise ValueError("Choose valid transcription and Word layout settings.")
        settings["gap"], settings["context_padding"] = float(settings["gap"]), float(settings["context_padding"])
        if viewer.get("viewer") not in VIEWERS:
            raise ValueError("Choose a supported PDF viewer.")
        allowed = {"integration_enabled", "send_new_pdfs", "mirror_pc_folders", "mirror_cloud_folders"}
        if any(key not in allowed or type(value) is not bool for key, value in integration_changes.items()):
            raise ValueError("Choose valid reMarkable connection settings.")
        def persist(db):
            values = {"pdf_viewer": viewer, "default_settings": settings}
            if integration_changes:
                row = db.execute("SELECT value FROM preferences WHERE key='remarkable'").fetchone()
                config = json.loads(row[0]) if row else {}
                values["remarkable"] = {**config, **integration_changes}
            db.executemany("INSERT OR REPLACE INTO preferences(key,value) VALUES(?,?)",
                           [(key, json.dumps(value)) for key, value in values.items()])
        inbox = getattr(self, "inbox", None)
        if inbox is not None:
            result = inbox.save_config(draft, persist_preferences=persist)
        else:
            with self.store.connect() as db:
                persist(db)
            result = {}
        self.post("Preferences saved.", True)
        return result

    def run(self):
        # Apply commands, cloud downloads, scans and exports serially: no writer
        # can race a settings change or overwrite a PDF another operation reads.
        next_sync = time.monotonic() + self.interval
        next_cloud = time.monotonic()
        next_export = time.monotonic() + 30
        try:
            if not self.stop_event.is_set():
                self.upgrade_export_format()
            while not self.stop_event.is_set():
                name = ""
                local_scans, full_scan, full_force = {}, False, False
                try:
                    while not self.commands.empty() and not self.stop_event.is_set():
                        name, args = self.commands.get_nowait()
                        with self.lock:
                            self.queued_checks.discard(name)
                        if name == "paper_identity":
                            action, values, future = args
                            if not future.set_running_or_notify_cancel():
                                continue
                            try:
                                with self.activity("Saving paper relationships"):
                                    result = self.apply_paper_identity(action, values)
                            except Exception as exc:
                                future.set_exception(exc)
                            else:
                                future.set_result(result)
                        elif name == "storage_maintenance":
                            action, values, future = args
                            if not future.set_running_or_notify_cancel():
                                continue
                            try:
                                phase = {"backup": "Creating and verifying a library backup",
                                         "compact": "Backing up and compacting the library",
                                         "restore": "Restoring a library backup",
                                         "archive_cleanup": "Cleaning up duplicate recovery archives",
                                         "recycle_pdf": "Removing the reviewed PDF", "recycle_pdfs": "Removing reviewed PDFs"}.get(action, "Library maintenance")
                                with self.activity(phase):
                                    result = self.storage_action(action, values)
                            except Exception as exc:
                                future.set_exception(exc)
                            else:
                                future.set_result(result)
                        elif name == "storage_cleanup":
                            values, future = args
                            if not future.set_running_or_notify_cancel():
                                continue
                            try:
                                with self.activity("Removing verified extra PDF copies"):
                                    result = self.storage_action("cleanup", values)
                            except Exception as exc:
                                future.set_exception(exc)
                            else:
                                future.set_result(result)
                        elif name == "preferences":
                            values, future = args
                            if not future.set_running_or_notify_cancel():
                                continue
                            try:
                                with self.activity("Saving preferences"):
                                    result = self.apply_preferences(*values)
                            except Exception as exc:
                                future.set_exception(exc)
                            else:
                                future.set_result(result)
                            next_cloud = time.monotonic()
                        elif name == "watch":
                            self.watch()
                        elif name == "remarkable_connect":
                            self.remarkable.connect(*args)
                        elif name == "remarkable_disconnect":
                            self.remarkable.disconnect()
                        elif name == "remarkable_sync":
                            self.check_cloud(force=True)
                            next_cloud = time.monotonic() + self.interval
                        elif name == "remarkable_browse":
                            self.check_cloud(force=True, listing_only=True)
                        elif name == "remarkable_upload":
                            self.remarkable.queue_pdf(*args)
                            self.remarkable.process_outbox()
                        elif name == "remarkable_outbox":
                            with self.activity("Sending to reMarkable"):
                                self.remarkable.process_outbox(retry_review=True)
                        elif name == "remarkable_retry_transfer":
                            self.remarkable.retry_transfer(args[0], accept_changes=args[1], accept_destination=args[2])
                            self.remarkable.process_outbox()
                        elif name == "remarkable_cancel_transfer":
                            self.remarkable.cancel_transfer(*args)
                            self.post("Transfer cancelled. Local papers and tablet documents were retained.", True)
                        elif name == "filed_pdf":
                            self.filed_pdf(args[0])
                            next_cloud = time.monotonic()
                        elif name == "remarkable_settings":
                            # Merge at execution time so queued UI changes do not
                            # overwrite newer settings with an older snapshot.
                            config = self.remarkable.config()
                            config.update(args[0])
                            self.store.set_preference("remarkable", config)
                            next_cloud = time.monotonic()
                            self.post("reMarkable settings saved.", True)
                        elif name == "remarkable_map":
                            folder_id, path = args
                            config = self.remarkable.config()
                            mappings = dict(config["mappings"])
                            if path is None:
                                mappings.pop(folder_id, None)
                            else:
                                if (not config.get("pc_root") or
                                        not Path(path).resolve().is_relative_to(Path(config["pc_root"]).resolve()) or
                                        excluded(self.store, path)):
                                    raise ValueError("Choose a folder inside the linked PC library root. Cloud folder links cannot expand its scope.")
                                mappings[folder_id] = path
                            config["mappings"] = mappings
                            self.store.set_preference("remarkable", config)
                            next_cloud = time.monotonic()
                            self.post("reMarkable folder mapping saved.", True)
                        elif name == "remarkable_unlink":
                            self.remarkable.unlink_folder(*args)
                            next_cloud = time.monotonic()
                        elif name == "sync":
                            project_id = args[0] if args else None
                            force = args[1] if len(args) > 1 else False
                            if project_id is None:
                                full_scan, full_force = True, full_force or force
                            else:
                                local_scans[project_id] = local_scans.get(project_id, False) or force
                        elif name == "rebuild":
                            self.store.mark_dirty(args[0])
                            self.export_pending()
                        elif name == "export_pending":
                            self.export_pending()
                            self.post("", True)
                        elif name == "settings":
                            self.store.set_settings(*args)
                            local_scans.setdefault(args[0], False)
                        elif name == "defaults":
                            # Defaults affect subsequently added folders. Each
                            # existing folder retains its own processing settings.
                            settings = {**self.store.default_settings(), **args[0]}
                            if settings["grouping"] not in {"Nearby blocks", "Nearby lines", "Whole page"}:
                                raise ValueError("Choose a supported drawing grouping mode.")
                            if not 4 <= float(settings["gap"]) <= 100 or not 8 <= float(settings["context_padding"]) <= 200:
                                raise ValueError("Use a stroke distance from 4 to 100 and context margin from 8 to 200.")
                            if not isinstance(settings["ocr"], bool) or settings["notebook_style"] not in {"Reading", "Detailed"}:
                                raise ValueError("Choose valid OCR and notebook layout settings.")
                            settings["gap"], settings["context_padding"] = float(settings["gap"]), float(settings["context_padding"])
                            self.store.set_preference("default_settings", settings)
                            self.post("Processing defaults saved for new folders. Existing folder settings are retained.", True)
                        elif name == "viewer":
                            self.store.set_preference("pdf_viewer", args[0])
                            self.post("PDF viewer preference saved.", True)
                        elif name == "remove":
                            self.store.remove_project(args[0])
                            self.watch()
                            self.post("Folder removed from the library. Exported files remain on disk.", True)
                        elif name == "relocate":
                            inbox = getattr(self, "inbox", None)
                            with inbox._lock if inbox is not None else nullcontext():
                                self.store.relocate_project(*args)
                                if inbox is not None:
                                    inbox.set_config_snapshot(self.store.preference("inbox", {}))
                            self.watch()
                            self.request_sync()
                            self.post("Moved folder located. Saved notes, history and folder settings were retained.", True)
                        elif name == "add":
                            if excluded(self.store, args[0]):
                                self.post("The reMarkable review and cache areas are excluded from note analysis.", True)
                                continue
                            project_id = self.store.add_project(args[0])
                            self.watch()
                            local_scans.setdefault(project_id, False)
                        elif name == "export_view":
                            target, entries = args
                            # The Notes UI keeps lightweight rows; load crop
                            # images only on the export worker when needed.
                            started = time.perf_counter()
                            with self.activity("Exporting selected notes", Path(target).name, total=len(entries)):
                                entries = self.export_images(entries)
                                _, backup = export_notebook(target, "Selected research notes", entries)
                            logging.info("Selected-note export finished: annotations=%d elapsed=%.3fs", len(entries), time.perf_counter() - started)
                            self.post(f"Exported {len(entries)} annotations to {target}" +
                                      (f". Previous file preserved at {backup}" if backup else ""))
                    now = time.monotonic()
                    if now >= next_cloud:
                        next_cloud = now + self.interval
                        try:
                            self.check_cloud()
                        except InterruptedError:
                            if not self.stop_event.is_set():
                                raise
                        except Exception as exc:
                            self.store.set_preference("remarkable_error", str(exc))
                            self.post(f"reMarkable sync will retry: {exc}", True)
                    # Read discovery requests after cloud writes and merge them
                    # with queued commands: startup needs exactly one walk.
                    with self.lock:
                        full_scan |= self.sync_requested
                        self.sync_requested = False
                    full_scan |= now >= next_sync
                    if full_scan and not self.stop_event.is_set():
                        self.sync(force=full_force)
                        next_sync = time.monotonic() + self.interval
                    for project_id, force in {**{pid: False for pid in self.cloud_projects}, **local_scans}.items():
                        if self.stop_event.is_set():
                            break
                        if not full_scan or (force and not full_force):
                            self.sync(project_id, force)
                    if not full_scan and self.cloud_paths and not self.stop_event.is_set():
                        with self.scan_batch():
                            for path in sorted(self.cloud_paths):
                                if self.stop_event.is_set():
                                    break
                                self.scan_path(path)
                    self.cloud_paths.clear()
                    self.cloud_projects.clear()
                    for path, attempt in self.due(time.monotonic()):
                        if self.stop_event.is_set():
                            break
                        self.scan_path(path, attempt)
                    if now >= next_export:
                        self.export_pending()
                        next_export = time.monotonic() + 30
                    # Batch export after a burst has settled.
                    with self.lock:
                        quiet = not self.pending
                    if quiet and any(p["dirty"] and not p["error"] for p in self.store.projects()):
                        self.export_pending()
                except Exception as exc:
                    if isinstance(exc, InterruptedError) and self.stop_event.is_set():
                        break
                    if name.startswith("remarkable_"):
                        self.store.set_preference("remarkable_error", str(exc))
                    logging.exception("Background operation failed")
                    self.post(f"Background operation failed: {exc}", True)
                self.wake.wait(.25)
                self.wake.clear()
        finally:
            try:
                if self.observer:
                    with self.activity("Stopping folder watching"):
                        self.observer.stop()
                        self.observer.join(timeout=3)
            finally:
                if self.pdf_worker is not None:
                    with self.activity("Closing the PDF processing worker"):
                        self.pdf_worker.shutdown(wait=True)

    def stop(self):
        self.stop_event.set()
        self.wake.set()
        if self.pdf_worker is not None:
            self.pdf_worker.cancel()
