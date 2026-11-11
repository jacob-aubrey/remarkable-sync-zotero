"""Durable additive transfers and conservative folder creation.

No startup walk queues papers. Only an explicit confirmed filing calls
queue_pdf. Cloud IDs are saved before sending, so ambiguous responses can be
retried without creating a second tablet document.
"""
import hashlib
import json
from pathlib import Path
import uuid

from .remarkable_cloud import CloudError
from .storage import canonical, digest_file, timestamp


def inside(path, root):
    return bool(root and Path(path).resolve().is_relative_to(Path(root).resolve()))


def safe_folder(name):
    return bool(name and name not in {".", ".."} and name.rstrip(". ") == name
                and not any(c in name for c in '<>:"/\\|?*\x00\r\n')
                and not any(ord(c) < 32 for c in name)
                and name.split(".")[0].upper() not in
                {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)],
                 *[f"LPT{i}" for i in range(1, 10)]})


def rebase_outbox_paths(db, old_root, new_root):
    """Rebase saved local intent inside a caller's relocation transaction."""
    row = db.execute("SELECT value FROM preferences WHERE key='remarkable_outbox'").fetchone()
    if row is None:
        return
    jobs = json.loads(row[0])
    old_root, new_root = Path(old_root).resolve(), Path(new_root).resolve()
    for job in jobs:
        path = Path(job.get("path", "")).resolve()
        if not path.is_relative_to(old_root):
            continue
        job["path"] = canonical(new_root / path.relative_to(old_root))
        if job.get("state") not in {"sent", "cancelled"}:
            job.update(state="review", reason_kind="destination_change",
                       reason="The local library moved. Confirm this transfer's destination before retrying.",
                       updated_at=timestamp())
    db.execute("UPDATE preferences SET value=? WHERE key='remarkable_outbox'", (json.dumps(jobs),))


class Outbox:
    def __init__(self, sync):
        self.sync, self.store = sync, sync.store

    def jobs(self):
        return self.store.preference("remarkable_outbox", [])

    def save(self, jobs):
        self.store.set_preference("remarkable_outbox", jobs)

    def patch(self, job_id, **changes):
        jobs = self.jobs()
        for job in jobs:
            if job["id"] == job_id:
                job.update(changes, updated_at=timestamp())
                self.save(jobs)
                self.sync.post("", True)
                return job
        raise CloudError("Transfer job is unavailable")

    def cancel(self, job_id):
        job = next((job for job in self.jobs() if job["id"] == job_id), None)
        if not job or job.get("state") == "sent":
            raise CloudError("Choose an unfinished transfer to cancel.")
        return self.patch(job_id, state="cancelled", reason_kind="cancelled",
                          reason="Transfer cancelled. Local files were retained; any earlier cloud commit is retained too.")

    def destination_changed(self, job, config, parent_id=None):
        if job.get("source_folder_id") and job["source_folder_id"] != config.get("source_folder_id"):
            return True
        if job.get("pc_root") and job["pc_root"] != canonical(config.get("pc_root", "")):
            return True
        return bool(parent_id and job.get("folder_id") and job["folder_id"] != parent_id)

    def retry(self, job_id, *, accept_changes=False, accept_destination=False):
        """Explicitly review current bytes/destination without losing remote IDs."""
        from .remarkable_sync import excluded
        from .paper_registry import Registry
        job = next((job for job in self.jobs() if job["id"] == job_id), None)
        if not job or job.get("state") in {"sent", "cancelled"}:
            raise CloudError("Choose an unfinished transfer to retry.")
        config, path = self.sync.config(), Path(job["path"])
        root_name = Path(config.get("pc_root") or "linked PC root").name
        if not inside(path, config.get("pc_root", "")) or excluded(self.store, path):
            raise CloudError(f"Locate this transfer inside {root_name} before retrying.")
        if not (path.is_file() if job.get("kind") == "pdf" else path.is_dir()):
            raise CloudError("The local file or folder is still missing. Restore it before retrying.")
        parent = self.route(path.parent, config, self.store.preference("remarkable_folders", []))
        if (job.get("reason_kind") == "destination_change" or self.destination_changed(job, config, parent)) and not accept_destination:
            raise CloudError("The queued destination changed. Confirm the current source and PC folder before retrying.")
        if accept_destination and not parent:
            # Source selection can precede its next catalog refresh. Confirm
            # against current metadata without sending or rendering PDFs.
            self.sync.sync(force=True, listing_only=True)
            config = self.sync.config()
            parent = self.route(path.parent, config, self.store.preference("remarkable_folders", []))
            if not parent:
                raise CloudError("Choose the current cloud destination before confirming this transfer.")
        digest = digest_file(path) if job.get("kind") == "pdf" else None
        if digest is not None and digest != job["digest"] and not accept_changes:
            raise CloudError("The PDF changed after filing. Confirm sending its current contents before retrying.")
        changes = {"state": "queued" if parent else "needs_mapping", "folder_id": parent or job.get("folder_id", ""),
                   "source_folder_id": config.get("source_folder_id", ""),
                   "pc_root": canonical(config["pc_root"]), "reason_kind": "",
                   "reason": "" if parent else "Link this PC folder to one cloud folder before sending."}
        if digest is not None and digest != job["digest"]:
            local = Registry(self.store).register_local(path, digest=digest)
            changes.update(digest=digest, paper_id=local["paper_id"])
            if job.get("attempted_at") or job.get("state") in {"sending", "waiting"}:
                # The old ID may already exist remotely. Keep its provenance
                # and start a separate additive intent for the reviewed bytes.
                replacement = {**job, **changes, "id": str(uuid.uuid4()), "cloud_id": str(uuid.uuid4()),
                               "created_at": timestamp(), "replaces_job_id": job["id"]}
                for key in ("attempted_at", "cloud_hash", "sent_at", "matching_cloud_ids"):
                    replacement.pop(key, None)
                self.cancel(job_id)
                jobs = self.jobs()
                jobs.append(replacement)
                self.save(jobs)
                return replacement
        return self.patch(job_id, **changes)

    def register_committed(self, item, digest, *, metadata=None):
        """Saved UUID and PDF hash supply provenance after lost confirmation."""
        from .paper_registry import Registry
        registry = Registry(self.store)
        job = next((job for job in self.jobs() if job.get("kind") == "pdf"
                    and job.get("cloud_id") == item.id and job.get("digest") == digest
                    and (job.get("attempted_at") or job.get("state") in {"sending", "waiting", "sent"})), None)
        if not job:
            return registry.register_cloud(item.id, digest=digest, title=item.name, metadata=metadata)
        local = next((source for source in registry.sources() if source["kind"] == "local"
                      and source["path"] == job["path"]), None)
        paper_id = local["paper_id"] if local else job["paper_id"]
        cloud = next((source for source in registry.sources() if source["kind"] == "cloud"
                      and source["cloud_id"] == item.id), None)
        if cloud and cloud["paper_id"] != paper_id:
            if not local:
                raise CloudError("Restore the queued original before repairing its tablet link.")
            registry.link_sources([local["id"], cloud["id"]], paper_id=paper_id)
        return registry.link_cloud(paper_id, item.id, digest=digest, title=item.name, metadata=metadata)

    def route(self, path, config, catalog):
        from .remarkable_sync import folder_destination, scoped_folders
        if not inside(path, config.get("pc_root", "")):
            return None
        candidates = []
        for folder in scoped_folders(catalog, config.get("source_folder_id", "")):
            destination = folder_destination(folder, catalog, config)
            if destination and inside(destination, config.get("pc_root", "")) and canonical(destination) == canonical(path):
                candidates.append(folder["id"])
        return candidates[0] if len(candidates) == 1 else None

    def queue_pdf(self, path):
        from .remarkable_sync import excluded
        from .paper_registry import Registry
        config = self.sync.config()
        path = Path(path).resolve()
        if not config["integration_enabled"] or not config["send_new_pdfs"]:
            return None
        if path.suffix.lower() != ".pdf" or not inside(path, config.get("pc_root", "")) or excluded(self.store, path):
            return None
        if not path.is_file():
            raise CloudError("The newly filed paper is unavailable; retry when its local file is ready.")
        # Downloaded renders/review files never re-enter the upload stream.
        if any(canonical(path) == manifest.get(key) for manifest in self.store.remarkable_items().values()
               for key in ("path", "review_pdf")):
            return None
        registry = Registry(self.store)
        local = registry.register_local(path)
        if local.get("metadata", {}).get("role") == "tablet_render":
            return None
        linked = [source for source in registry.sources(local["paper_id"]) if source["kind"] == "cloud"]
        if linked:
            self.sync.post("This paper is already linked to reMarkable. Its original was retained.", True)
            return None
        digest = digest_file(path)
        jobs = self.jobs()
        existing = next((job for job in jobs if job.get("kind") == "pdf" and
                         (job.get("paper_id") == local["paper_id"] or job.get("digest") == digest)
                         and job.get("state") != "cancelled"), None)
        if existing:
            return existing
        destination = self.route(path.parent, config, self.store.preference("remarkable_folders", []))
        job = {"id": str(uuid.uuid4()), "kind": "pdf", "cloud_id": str(uuid.uuid4()),
               "paper_id": local["paper_id"], "path": canonical(path), "digest": digest,
               "source_folder_id": config.get("source_folder_id", ""), "pc_root": canonical(config["pc_root"]),
               "name": path.stem, "folder_id": destination or "", "created_at": timestamp(),
               "state": "queued" if destination else "needs_mapping",
               "reason": "" if destination else "Link this PC folder to a reMarkable folder before sending."}
        jobs.append(job)
        self.save(jobs)
        self.sync.post("Paper queued for reMarkable." if destination else
                       "Paper sorted locally; its reMarkable destination needs folder mapping.", True)
        return job

    def queue_folder(self, path, parent_id):
        config = self.sync.config()
        jobs = self.jobs()
        found = next((job for job in jobs if job.get("kind") == "folder" and job.get("path") == canonical(path)
                      and job.get("state") != "cancelled"), None)
        if found:
            return found
        job = {"id": str(uuid.uuid4()), "kind": "folder", "cloud_id": str(uuid.uuid4()),
               "path": canonical(path), "name": Path(path).name, "folder_id": parent_id,
               "source_folder_id": config.get("source_folder_id", ""), "pc_root": canonical(config["pc_root"]),
               "state": "queued" if parent_id else "needs_mapping", "created_at": timestamp(),
               "reason": "" if parent_id else "The parent PC folder needs a reMarkable link."}
        jobs.append(job)
        self.save(jobs)
        return job

    def observe_folders(self, catalog):
        from .remarkable_sync import scoped_folders, folder_destination, excluded
        config = self.sync.config()
        root = config.get("pc_root", "")
        source_id = config.get("source_folder_id", "")
        if not config["integration_enabled"] or not root or not source_id or not Path(root).is_dir():
            return
        scoped = scoped_folders(catalog, source_id)
        root_key = hashlib.sha256(json.dumps([canonical(root), source_id]).encode()).hexdigest()
        baselines = self.store.preference("remarkable_folder_baselines", {})
        saved = baselines.get(root_key, {})
        reviews = []
        # The baseline is taken on first enable, so old mismatches remain review
        # choices instead of producing duplicate differently named folders.
        cloud_ids = {folder["id"] for folder in scoped}
        if config["mirror_cloud_folders"]:
            previous_cloud = set(saved.get("cloud", cloud_ids))
            pending_cloud = set(saved.get("cloud_pending", [])) | (cloud_ids - previous_cloud)
            for folder in sorted(scoped, key=lambda folder: len(folder.get("ancestor_ids", []))):
                if folder["id"] not in pending_cloud:
                    continue
                current = folder_destination(folder, catalog, config)
                if current and inside(current, root):
                    config["mappings"] = {**config["mappings"], folder["id"]: canonical(current)}
                    self.store.set_preference("remarkable", config)
                    pending_cloud.discard(folder["id"])
                    continue
                parent = next((entry for entry in scoped if entry["id"] == folder["parent"]), None)
                destination = folder_destination(parent, catalog, config) if parent else None
                if not destination or not inside(destination, root) or not safe_folder(folder["name"]):
                    reviews.append({"id": folder["id"], "name": folder["name"], "kind": "cloud_folder",
                                    "reason": "Choose a PC folder: the parent is unlinked or the name cannot be used on Windows."})
                    continue
                target = destination / folder["name"]
                if target.exists() or excluded(self.store, target) or not inside(target, root):
                    reviews.append({"id": folder["id"], "name": folder["name"], "kind": "cloud_folder",
                                    "reason": "A local name collision needs review before creating this folder."})
                    continue
                target.mkdir()
                config["mappings"] = {**config["mappings"], folder["id"]: canonical(target)}
                self.store.set_preference("remarkable", config)
                pending_cloud.discard(folder["id"])
            saved["cloud"], saved["cloud_pending"] = sorted(cloud_ids), sorted(pending_cloud & cloud_ids)
        else:
            saved.pop("cloud", None)
            saved.pop("cloud_pending", None)
        if config["mirror_pc_folders"]:
            try:
                actual_paths = {canonical(path): path for path in Path(root).rglob("*") if path.is_dir()
                                and inside(path, root) and not excluded(self.store, path)
                                and not any(part in {".git", "Notebook backups", "notes_media"}
                                            for part in path.relative_to(root).parts)}
                paths = set(actual_paths)
            except OSError as exc:
                self.sync.post(f"Folder mirroring needs another check: {exc}", True)
                return
            previous_pc = set(saved.get("pc", paths))
            for path in sorted(paths - previous_pc, key=lambda path: len(Path(path).parts)):
                current = self.route(path, config, catalog)
                if current:
                    config["mappings"] = {**config["mappings"], current: canonical(path)}
                    self.store.set_preference("remarkable", config)
                    continue
                self.queue_folder(actual_paths[path], self.route(Path(path).parent, config, catalog) or "")
            saved["pc"] = sorted(paths)
        else:
            saved.pop("pc", None)
        baselines[root_key] = saved
        self.store.set_preference("remarkable_folder_baselines", baselines)
        self.store.set_preference("remarkable_folder_review", reviews)

    def process(self, refresh=True, retry_review=False):
        from .paper_registry import Registry
        config = self.sync.config()
        if not config["integration_enabled"]:
            return False
        if retry_review:
            for job in self.jobs():
                if job.get("state") == "review":
                    try:
                        self.retry(job["id"])
                    except CloudError as exc:
                        self.sync.post(f"Transfer review needed for {job['name']}: {exc}", True)
        registry = Registry(self.store)
        cached_catalog = self.store.preference("remarkable_folders", [])
        # A confirmed reconciliation link can resolve a duplicate transfer
        # without sending anything. Consult the local locator because its paper
        # identity may have changed during that explicit link operation.
        for job in self.jobs():
            if job.get("state") not in {"sent", "cancelled"} and self.destination_changed(job, config):
                self.patch(job["id"], state="review", reason_kind="destination_change",
                           reason="The cloud source or PC root changed. Confirm the new destination before retrying.")
                continue
            if job.get("kind") == "folder" and job.get("state") == "review":
                if job.get("reason_kind") == "destination_change":
                    continue
                current = self.route(job["path"], config, cached_catalog)
                if current:
                    self.patch(job["id"], state="sent", cloud_id=current,
                               reason="Existing cloud folder linked; no creation needed.")
                continue
            if job.get("kind") != "pdf" or job.get("state") in {"sent", "cancelled"}:
                continue
            local = next((source for source in registry.sources() if source["kind"] == "local"
                          and source["path"] == job["path"]), None)
            linked = [source for source in registry.sources(local["paper_id"]) if source["kind"] == "cloud"
                      and source.get("digest") == job["digest"]] if local else []
            if len(linked) == 1:
                self.patch(job["id"], state="sent", cloud_id=linked[0]["cloud_id"],
                           paper_id=local["paper_id"], reason="Existing tablet copy linked; no upload needed.")
        enabled_jobs = [job for job in self.jobs() if job.get("state") not in {"sent", "cancelled", "review"}
                        and config["send_new_pdfs" if job.get("kind") == "pdf" else "mirror_pc_folders"]]
        if not enabled_jobs:
            return False
        if refresh:
            self.sync.sync(force=True, listing_only=True)
        client = self.sync.ensure_client()
        config = self.sync.config()
        catalog = self.store.preference("remarkable_folders", [])
        changed = False
        # Create queued parents before their children/documents. Persist links
        # immediately so restart recovery can continue the same job IDs.
        for job in sorted(enabled_jobs, key=lambda job: (job.get("kind") != "folder", len(Path(job["path"]).parts))):
            if self.sync.stop.is_set():
                raise InterruptedError("reMarkable transfer stopped")
            path = Path(job["path"])
            if self.destination_changed(job, config):
                self.patch(job["id"], state="review", reason_kind="destination_change",
                           reason="The cloud source or PC root changed. Confirm the new destination before retrying.")
                continue
            if not inside(path, config.get("pc_root", "")):
                root_name = Path(config.get("pc_root") or "linked PC root").name
                self.patch(job["id"], state="review", reason_kind="outside_root",
                           reason=f"This paper or folder is outside {root_name}; no transfer was made.")
                continue
            if not (path.is_file() if job.get("kind") == "pdf" else path.is_dir()):
                self.patch(job["id"], state="review", reason_kind="local_missing", reason="Local file or folder is missing. Locate it before retrying.")
                continue
            if job.get("kind") == "pdf" and digest_file(path) != job["digest"]:
                self.patch(job["id"], state="review", reason_kind="local_changed", reason="Local PDF changed after filing; review it before sending.")
                continue
            parent_id = self.route(path.parent, config, catalog)
            if self.destination_changed(job, config, parent_id):
                self.patch(job["id"], state="review", reason_kind="destination_change",
                           reason="The linked destination changed. Confirm its current cloud folder before retrying.")
                continue
            if not parent_id:
                self.patch(job["id"], state="needs_mapping", reason="Link this PC folder to one cloud folder before sending.")
                continue
            try:
                self.patch(job["id"], state="sending", folder_id=parent_id, reason="", reason_kind="",
                           source_folder_id=config.get("source_folder_id", ""), pc_root=canonical(config["pc_root"]),
                           attempted_at=job.get("attempted_at") or timestamp())
                if job.get("kind") == "folder":
                    current_route = self.route(path, config, catalog)
                    if current_route:
                        config["mappings"] = {**config["mappings"], current_route: canonical(path)}
                        self.store.set_preference("remarkable", config)
                        self.patch(job["id"], state="sent", cloud_id=current_route, reason="Existing cloud folder linked.")
                        continue
                    if any(folder["parent"] == parent_id and folder["name"].casefold() == job["name"].casefold()
                           for folder in catalog):
                        self.patch(job["id"], state="review", reason_kind="duplicate_folder", reason="A cloud folder with this name already exists; choose its link before creating another.")
                        continue
                    item = client.create_folder(job["name"], parent_id, job["cloud_id"], config["source_folder_id"])
                    parent = next(folder for folder in catalog if folder["id"] == parent_id)
                    catalog.append({"id": item.id, "name": item.name, "parent": parent_id,
                                    "ancestor_ids": parent["ancestor_ids"] + [parent_id],
                                    "path": parent["path"] + "/" + item.name})
                    self.store.set_preference("remarkable_folders", catalog)
                    config["mappings"] = {**config["mappings"], item.id: canonical(path)}
                    self.store.set_preference("remarkable", config)
                else:
                    existing_cloud = [document for document in self.store.preference("remarkable_documents", [])
                                      if document.get("in_scope") and document.get("pdf_hash") == job["digest"]
                                      and document["id"] != job["cloud_id"]]
                    if existing_cloud:
                        self.patch(job["id"], state="review", reason_kind="duplicate_pdf", reason="An identical source PDF is already on reMarkable. Review and link it instead of creating a duplicate.",
                                   matching_cloud_ids=[document["id"] for document in existing_cloud])
                        continue
                    data = path.read_bytes()
                    if hashlib.sha256(data).hexdigest() != job["digest"]:
                        raise CloudError("The local PDF changed while preparing its upload; review needed.")
                    item = client.upload_pdf(data, job["name"], parent_id, job["cloud_id"], config["source_folder_id"])
                    self.register_committed(item, job["digest"], metadata={"parent": item.parent, "uploaded_from": job["path"]})
                self.patch(job["id"], state="sent", reason="", cloud_hash=item.hash, sent_at=timestamp())
                changed = True
            except InterruptedError:
                raise
            except Exception as exc:
                # May have committed remotely: retry uses the same saved ID.
                self.patch(job["id"], state="waiting", reason=str(exc))
                self.sync.post(f"reMarkable transfer waiting for {job['name']}: {exc}", True)
        if changed:
            self.sync.post("reMarkable transfers finished. Local originals were retained.", True)
        return changed
