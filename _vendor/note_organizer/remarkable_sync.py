"""Incremental cloud-to-PC PDF mirroring with conservative folder routing."""
import hashlib
from concurrent.futures.process import BrokenProcessPool
from io import BytesIO
import json
from pathlib import Path
import re
from types import SimpleNamespace
import zipfile

from .remarkable_cloud import CloudClient, CloudError, TokenVault, atomic_write
from .remarkable_pdf import annotated_pdf, page_map, RENDER_REVISION
from .remarkable_sources import save_source_bundle, source_bundle, source_storage_usage
from .storage import canonical, digest_file, timestamp


def default_config(data):
    return {"integration_enabled": False, "enabled": False, "pc_root": "", "mappings": {},
            "send_new_pdfs": False, "mirror_pc_folders": False, "mirror_cloud_folders": False,
            "blob_cache_mb": 64,
            "source_folder_name": "Literature", "source_folder_id": "",
            "review_dir": str(Path(data) / "reMarkable Review")}


def is_integration_enabled(config):
    """An explicit master switch wins; preserve configured legacy libraries.

    Call with saved preferences, before filling defaults. New users receive no
    cloud activity. Existing paired libraries keep their opt-in configuration.
    """
    if not isinstance(config, dict):
        return False
    if "integration_enabled" in config:
        return config["integration_enabled"] is True
    return bool(config.get("enabled") or config.get("pc_root") or
                config.get("source_folder_id") or config.get("mappings"))


def effective_config(store):
    """Works with both the worker's Store and a read-only LibraryView."""
    saved = store.preference("remarkable", {})
    if not isinstance(saved, dict):
        saved = {}
    return {**default_config(store.path.parent), **saved,
            "integration_enabled": is_integration_enabled(saved)}


def excluded_roots(store):
    config = {**default_config(store.path.parent), **store.preference("remarkable", {})}
    return [Path(config["review_dir"]).resolve(), (store.path.parent / "remarkable").resolve()]


def excluded(store, path):
    return any(Path(path).resolve().is_relative_to(root) for root in excluded_roots(store))


def folder_chain(item, by_id):
    """Return live ancestors in root order; trashed ancestry excludes the item."""
    chain, seen = [], {item.id}
    parent = item.parent
    while parent:
        if parent == "trash":
            return None
        if parent in seen or parent not in by_id:
            raise CloudError("Cloud folder hierarchy is incomplete or cyclic; saved files are retained.")
        seen.add(parent)
        folder = by_id[parent]
        if folder.deleted:
            return None
        if folder.kind != "CollectionType":
            raise CloudError("Cloud PDF has an invalid parent folder")
        chain.append(folder)
        parent = folder.parent
    return list(reversed(chain))


def existing_child(root, name):
    # Never interpret tablet folder names as paths or sanitize them into a match.
    if not name or name in {".", ".."} or any(c in name for c in '/\\:'):
        return None
    matches = [p for p in root.iterdir() if p.is_dir() and p.name.casefold() == name.casefold()]
    if len(matches) != 1 or not matches[0].resolve().is_relative_to(root.resolve()):
        return None
    return matches[0].resolve()


def destination(chain, config):
    """Apply the deepest explicit mapping, then match every remaining folder."""
    mappings = config.get("mappings", {})
    start, root = 0, config.get("pc_root", "")
    for index, folder in enumerate(chain):
        if folder.id in mappings:
            start, root = index + 1, mappings[folder.id]
    if not root or not Path(root).is_dir():
        return None
    root = Path(root).resolve()
    for folder in chain[start:]:
        root = existing_child(root, folder.name)
        if root is None:
            return None
    return root


def scoped_folders(catalog, source_id):
    """Filter the folder catalog by IDs, including the selected source itself."""
    if not source_id:
        return []
    return [folder for folder in catalog if folder["id"] == source_id
            or source_id in folder.get("ancestor_ids", [])]


def folder_destination(folder, catalog, config):
    """Resolve a catalog folder using the same routing as PDF downloads."""
    source_id = config.get("source_folder_id", "")
    if not source_id:
        return None
    ancestors = folder.get("ancestor_ids", [])
    if folder["id"] == source_id:
        descendants = []
    elif source_id in ancestors:
        descendants = ancestors[ancestors.index(source_id) + 1:] + [folder["id"]]
    else:
        return None
    by_id = {entry["id"]: entry for entry in catalog}
    if any(item_id not in by_id for item_id in descendants):
        return None
    chain = [SimpleNamespace(id=item_id, name=by_id[item_id]["name"]) for item_id in descendants]
    root = config.get("mappings", {}).get(source_id, config.get("pc_root", ""))
    target = destination(chain, {**config, "pc_root": root})
    pc_root = config.get("pc_root", "")
    if not target or not pc_root or not target.is_relative_to(Path(pc_root).resolve()):
        return None
    return target


def review_kind(payload):
    """Classify older manifests so unchanged review files do not keep retrying."""
    if payload.get("reason_kind"):
        return payload["reason_kind"]
    reason = payload.get("reason", "")
    if reason.startswith("Annotation conversion needs review:"):
        return "conversion"
    if reason.startswith("A local PDF conflicts"):
        return "local_conflict"
    return "routing"


def conflict_fingerprint(target, previous_path):
    """Notice when a local conflict is edited or removed, so sync can retry."""
    paths = {canonical(target)}
    if previous_path:
        paths.add(previous_path)
    state = {}
    for path in sorted(paths):
        try:
            stat = Path(path).stat()
            state[path] = (stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            state[path] = "missing"
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()


def safe_name(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).rstrip(". ")
    if name.lower().endswith(".pdf"):
        name = name[:-4]
    name = name[:110].strip() or "Paper"
    if name.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]}:
        name = "_" + name
    return name


def archive_bytes(bundle):
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in bundle.items():
            archive.writestr(name, data)
    return stream.getvalue()


def retained_recovery_paths(paths):
    """Keep known recovery evidence unless its file is confirmed absent."""
    retained = []
    for path in paths if isinstance(paths, list) else []:
        if not isinstance(path, str) or not path:
            continue
        try:
            Path(path).stat()
        except FileNotFoundError:
            continue
        except OSError:
            # An inaccessible preserved original still needs review.
            pass
        if path not in retained:
            retained.append(path)
    return retained


class RemarkableSync:
    def __init__(self, store, stop, post, client_factory=CloudClient):
        self.store, self.stop, self.post = store, stop, post
        self.private = store.path.parent / "remarkable"
        self.vault = TokenVault(self.private / "device-token.dpapi")
        self.client_factory = client_factory
        self.client = None
        self.renderer = None
        self.extractor = None
        self.combined_renderer = None
        self.tablet_renderer = None

    def config(self):
        return effective_config(self.store)

    def require_enabled(self):
        if not self.config()["integration_enabled"]:
            raise CloudError("Enable reMarkable cloud services in Settings first.")

    def ensure_client(self):
        self.require_enabled()
        if self.client is None:
            token = self.vault.load()
            if not token:
                raise CloudError("Connect reMarkable with a pairing code first.")
            self.client = self.client_factory(token, cache_dir=self.private / "blobs", stop=self.stop)
            self.configure_client()
        return self.client

    def configure_client(self):
        """The blob cache contains disposable data, never retained annotations."""
        if isinstance(self.client, CloudClient):
            try:
                budget = max(0, min(1024, int(self.config()["blob_cache_mb"]))) * 1024 * 1024
            except (ValueError, TypeError):
                budget = 64 * 1024 * 1024
            self.client.cache_limit_bytes = budget
            self.client.baseline_dir = self.private / "baselines"
            self.client.prune_cache()

    def jobs(self):
        from .remarkable_outbox import Outbox
        return Outbox(self).jobs()

    def queue_pdf(self, path):
        from .remarkable_outbox import Outbox
        return Outbox(self).queue_pdf(path)

    def process_outbox(self, refresh=True, retry_review=False):
        from .remarkable_outbox import Outbox
        return Outbox(self).process(refresh=refresh, retry_review=retry_review)

    def retry_transfer(self, job_id, *, accept_changes=False, accept_destination=False):
        from .remarkable_outbox import Outbox
        return Outbox(self).retry(job_id, accept_changes=accept_changes, accept_destination=accept_destination)

    def cancel_transfer(self, job_id):
        from .remarkable_outbox import Outbox
        return Outbox(self).cancel(job_id)

    def unlink_folder(self, folder_id):
        """Remove a route without touching downloaded files, notes or pairing."""
        config = self.config()
        mappings = dict(config["mappings"])
        mappings.pop(folder_id, None)
        config["mappings"] = mappings
        if folder_id == config.get("source_folder_id"):
            config["pc_root"] = ""
            config["enabled"] = False
        self.store.set_preference("remarkable", config)
        self.post("reMarkable folder unlinked. Existing PDFs and notes are retained.", True)

    def connect(self, code):
        self.require_enabled()
        client = self.client_factory(cache_dir=self.private / "blobs", stop=self.stop)
        token = client.pair(code)
        self.vault.save(token)
        self.client = client
        self.configure_client()
        self.post("reMarkable paired. Choose your PC library root or folder mappings, then enable automatic sync.", True)
        self.sync(force=True, listing_only=True)

    def disconnect(self):
        config = self.config()
        config["enabled"] = False
        self.store.set_preference("remarkable", config)
        self.vault.forget()
        self.client = None
        self.post("reMarkable disconnected. Downloaded PDFs and notes are retained.", True)

    def sync(self, force=False, listing_only=False):
        """Download selected PDFs; manual sync also retries unchanged reviews."""
        config = self.config()
        if not config["integration_enabled"]:
            return False
        if not config["enabled"] and not force:
            if config.get("pc_root") and (config["mirror_pc_folders"] or config["mirror_cloud_folders"]):
                listing_only = True
            else:
                return False
        if not config.get("source_folder_id") and not config.get("source_folder_name"):
            raise CloudError("Choose a cloud source folder before syncing. The whole library cannot be selected implicitly.")
        if self.client is None:
            token = self.vault.load()
            if not token:
                if force:
                    raise CloudError("Connect reMarkable with a pairing code first.")
                return False
            self.client = self.client_factory(token, cache_dir=self.private / "blobs", stop=self.stop)
        self.configure_client()
        self.post("Checking reMarkable cloud for changed PDFs…")
        items = self.client.library()
        if self.stop.is_set():
            raise InterruptedError("reMarkable sync stopped")
        by_id = {item.id: item for item in items}
        # Validate all parent chains before changing any local files.
        chains = {item.id: folder_chain(item, by_id) for item in items if not item.deleted}
        folders = [{"id": item.id, "name": item.name, "parent": item.parent,
                    "ancestor_ids": [folder.id for folder in chains[item.id]],
                    "path": "/" + "/".join(f.name for f in chains[item.id] + [item])}
                   for item in items if item.kind == "CollectionType" and item.id in chains and chains[item.id] is not None]
        self.store.set_preference("remarkable_folders", folders)
        scope_id = config.get("source_folder_id", "")
        scope_name = config.get("source_folder_name", "")
        if scope_id:
            scope = by_id.get(scope_id)
            if not scope or scope.deleted or scope.kind != "CollectionType" or chains.get(scope_id) is None:
                raise CloudError("The selected cloud source folder is unavailable. Choose its replacement in reMarkable settings.")
        elif scope_name:
            matches = [i for i in items if not i.deleted and i.kind == "CollectionType"
                       and i.name.casefold() == scope_name.casefold() and chains[i.id] is not None]
            if len(matches) != 1:
                raise CloudError(f"Expected one cloud folder named {scope_name!r}; found {len(matches)}. Choose the folder explicitly before syncing.")
            scope_id = matches[0].id
            config["source_folder_id"] = scope_id
            self.store.set_preference("remarkable", config)
        def in_scope(item_id):
            chain = chains.get(item_id)
            return chain is not None and (item_id == scope_id or any(f.id == scope_id for f in chain))
        self.store.set_preference("remarkable_documents", [{"id": item.id, "name": item.name,
            "parent": item.parent, "hash": item.hash,
            "pdf_hash": next((file.get("hash", "") for file in item.files if file["id"] == item.id + ".pdf"), ""),
            "cloud_path": "/" + "/".join([folder.name for folder in chains[item.id]] + [item.name]),
            "in_scope": in_scope(item.id)} for item in items
            if not item.deleted and item.is_pdf and chains.get(item.id) is not None])
        source_pdf_count = sum(item.is_pdf and in_scope(item.id) for item in items if not item.deleted)
        self.store.set_preference("remarkable_last_check", {"at": timestamp(), "source_folder_id": scope_id,
                                                           "source_pdf_count": source_pdf_count})
        from .remarkable_outbox import Outbox
        Outbox(self).observe_folders(folders)
        # Observation can add stable mappings for new mirrored folders.
        config = self.config()
        if listing_only:
            self.store.set_preference("remarkable_error", "")
            self.post(f"reMarkable folders refreshed: {source_pdf_count} PDFs in the selected source.", True)
            return False
        previous = self.store.remarkable_items()
        changed = False
        downloaded = reviewed = failed = 0
        for item in items:
            if self.stop.is_set():
                raise InterruptedError("reMarkable sync stopped")
            if item.deleted or not item.is_pdf or not in_scope(item.id):
                continue
            chain = chains[item.id]
            position = next(i for i, f in enumerate(chain) if f.id == scope_id)
            # The selected source maps to the PC root without repeating its name.
            routed_chain = chain[position + 1:]
            route_config = {**config, "pc_root": config.get("mappings", {}).get(scope_id, config["pc_root"])}
            folder = destination(routed_chain, route_config)
            reason = "" if folder else "No matching PC folder. Map this cloud folder to a PC folder."
            if folder and excluded(self.store, folder):
                folder, reason = None, "The destination is inside the excluded review or cache area."
            if folder and (not config.get("pc_root") or not folder.is_relative_to(Path(config["pc_root"]).resolve())):
                root_name = Path(config.get("pc_root") or "linked PC root").name
                folder, reason = None, f"Choose a folder inside {root_name}. A separate mapping cannot expand cloud sync outside that root."
            review = Path(config["review_dir"]) / item.id
            target = (folder if folder else review) / f"{safe_name(item.name)} [reMarkable {item.id[:8]}].pdf"
            planned_target = target
            old = previous.get(item.id, {})
            recovery_paths = retained_recovery_paths(old.get("recovery_paths", []))
            if "recovery_paths" in old and old["recovery_paths"] != recovery_paths:
                old = {**old, "recovery_paths": recovery_paths}
                self.store.save_remarkable_item(item.id, old)
            source_path = "/" + "/".join([f.name for f in chain] + [item.name])
            # Folder renames can change the displayed path without changing the
            # document hash. Routing identity controls when reviews retry.
            route_fingerprint = hashlib.sha256(json.dumps({
                "source_id": scope_id, "target": canonical(target), "reason": reason,
            }, sort_keys=True).encode()).hexdigest()
            source_metadata = {"name": item.name, "parent": item.parent, "cloud_path": source_path}
            unchanged = old.get("hash") == item.hash and old.get("render_revision") == RENDER_REVISION
            if (unchanged and old.get("storage_version") == 2 and old.get("state") == "synced"
                    and old.get("identity_registered") is True and old.get("notes_migrated") is True
                    and folder and old.get("main_path") and Path(old["main_path"]).is_file()
                    and Path(old["main_path"]).parent == folder
                    and digest_file(old["main_path"]) == old.get("main_digest")):
                if any(old.get(key) != value for key, value in source_metadata.items()):
                    self.store.save_remarkable_item(item.id, {**old, **source_metadata})
                continue
            if unchanged and old.get("identity_registered") is not True:
                try:
                    retained_bundle = source_bundle(self.store, item.id, revision=item.hash)
                except (CloudError, OSError, ValueError):
                    # Older manifests may lack retained sources. A normal
                    # verified download can supply the missing provenance.
                    unchanged = False
                else:
                    try:
                        self.register_download(item, retained_bundle, old)
                        old = {**old, "identity_registered": True}
                        self.store.save_remarkable_item(item.id, old)
                    except InterruptedError:
                        raise
                    except Exception as exc:
                        failed += 1
                        self.post(f"reMarkable identity retry needed for {item.name}: {exc}", True)
                        continue
            if unchanged and old.get("storage_version") == 2 and old.get("state") == "synced" and old.get("path") == canonical(target) and target.exists() and digest_file(target) == old.get("digest"):
                if any(old.get(key) != value for key, value in source_metadata.items()):
                    self.store.save_remarkable_item(item.id, {**old, **source_metadata})
                continue
            if unchanged and old.get("state") == "review" and old.get("review_pdf") and Path(old["review_pdf"]).exists() and not force:
                kind = review_kind(old)
                saved_route = old.get("route_fingerprint")
                same_route = saved_route == route_fingerprint
                if saved_route is None:
                    # Legacy conversion/conflict reviews already have a valid
                    # destination; routing reviews retry once a match appears.
                    same_route = not folder or kind in {"conversion", "local_conflict"}
                current_conflict = conflict_fingerprint(planned_target, old.get("path")) if kind == "local_conflict" else ""
                if kind == "local_conflict" and old.get("conflict_fingerprint"):
                    same_route &= current_conflict == old["conflict_fingerprint"]
                if same_route:
                    refreshed = {**old, **source_metadata, "reason_kind": kind,
                                 "route_fingerprint": route_fingerprint}
                    if kind == "local_conflict":
                        refreshed["conflict_fingerprint"] = current_conflict
                    if kind == "routing":
                        refreshed["reason"] = reason
                    if refreshed != old:
                        self.store.save_remarkable_item(item.id, refreshed)
                    continue
            self.post(f"Downloading reMarkable PDF: {item.name}…")
            try:
                bundle = None
                if not old:
                    # A previous attempt can have retained sources before a
                    # transient extraction/identity failure. Retry offline
                    # from those exact bytes rather than downloading again.
                    from .remarkable_sources import source_info
                    info = source_info(self.store, item.id)
                    if info and info.get("cloud_hash") == item.hash:
                        bundle = source_bundle(self.store, item.id, revision=info["cloud_hash"])
                if bundle is None:
                    bundle = self.client.pdf_bundle(item)
                # Preserve the exact source and stroke data for repair/reconversion.
                source_manifest = save_source_bundle(self.store, item, bundle)
                try:
                    rendered = (self.renderer(item, bundle) if self.renderer is not None else
                                annotated_pdf(item, bundle, self.stop.is_set))
                except (InterruptedError, BrokenProcessPool):
                    # A failed process is transient, not unsupported content.
                    # Keep the successful manifest so the next check retries.
                    raise
                except Exception as exc:
                    folder = None
                    target = review / f"{safe_name(item.name)} [original PDF].pdf"
                    reason = f"Annotation conversion needs review: {exc}"
                    reason_kind = "conversion"
                    rendered = bundle[item.id + ".pdf"]
                else:
                    reason_kind = "routing" if not folder else ""
                if folder:
                    from .cloud_notes import adopt_paper
                    try:
                        adopted = adopt_paper(self, item, bundle, folder, old)
                    except InterruptedError:
                        raise
                    except CloudError as exc:
                        # The original and exact tablet layers stay intact.
                        # An uncertain match never becomes a combined overlay.
                        folder, reason_kind = None, "local_conflict"
                        reason = f"Combined notes need review: {exc}"
                        target = review / f"{safe_name(item.name)} [cloud copy].pdf"
                    else:
                        payload = {"hash": item.hash, **source_metadata,
                                   "render_revision": RENDER_REVISION, "at": timestamp(),
                                   "state": "synced", "reason": "", "reason_kind": "",
                                   "route_fingerprint": route_fingerprint,
                                   "source_manifest": str(self.private / "sources" / (item.id + ".json")),
                                   "original_pdf_digest": source_manifest["pdf_hash"],
                                   "identity_registered": True, **adopted}
                        if recovery_paths:
                            payload["recovery_paths"] = recovery_paths
                        self.store.save_remarkable_item(item.id, payload)
                        downloaded += 1
                        changed = True
                        continue
                # Successful adoption returned above. Unmatched or unsupported
                # sources receive an inspectable review copy, never a second
                # reading PDF in the user's research folder.
                if target.exists() and (old.get("review_pdf") != canonical(target) or old.get("review_digest") != digest_file(target)):
                    number = 1
                    while target.exists():
                        target = review / f"{safe_name(item.name)} [{item.hash[:12]} cloud copy {number}].pdf"
                        number += 1
                target.parent.mkdir(parents=True, exist_ok=True)
                expected = old.get("review_digest") if target.exists() and old.get("review_pdf") == canonical(target) else None
                retained = atomic_write(target, rendered, expected_digest=expected, require_absent=not target.exists())
                if retained is not None:
                    retained_path = canonical(retained)
                    recovery_paths = list(dict.fromkeys([*recovery_paths, retained_path]))
                    self.post(f"reMarkable PDF updated. Its previous complete copy is retained for recovery at {retained_path}.", True)
                payload = {"hash": item.hash, "name": item.name, "parent": item.parent,
                           "cloud_path": source_path, "render_revision": RENDER_REVISION, "at": timestamp(),
                           "state": "review", "reason": reason,
                           "reason_kind": reason_kind, "route_fingerprint": route_fingerprint,
                           "source_manifest": str(self.private / "sources" / (item.id + ".json")),
                            "original_pdf_digest": source_manifest["pdf_hash"]}
                if old.get("storage_version") == 2:
                    payload.update({key: old[key] for key in ("storage_version", "main_path", "main_digest",
                        "notes_migrated", "legacy_path", "legacy_digest", "legacy_cloud_hash", "legacy_document_ids") if key in old})
                if recovery_paths or "recovery_paths" in old:
                    payload["recovery_paths"] = recovery_paths
                if reason_kind == "local_conflict":
                    payload["conflict_fingerprint"] = conflict_fingerprint(planned_target, old.get("path"))
                payload.update(review_pdf=canonical(target), review_digest=hashlib.sha256(rendered).hexdigest())
                if old.get("path"):
                    payload.update(path=old["path"], digest=old.get("digest", ""))
                atomic_write(review / "source.json", json.dumps({"cloud_id": item.id,
                    "source_manifest": payload["source_manifest"], "original_pdf_digest": source_manifest["pdf_hash"]}, indent=2).encode())
                atomic_write(review / "review.json", json.dumps(payload, indent=2).encode())
                reviewed += 1
                payload["identity_registered"] = False
                self.store.save_remarkable_item(item.id, payload)
                self.register_download(item, bundle, payload)
                self.store.save_remarkable_item(item.id, {**payload, "identity_registered": True})
                changed = True
            except InterruptedError:
                raise
            except Exception as exc:
                failed += 1
                # A failed download must never replace the last good manifest.
                self.post(f"reMarkable PDF retry needed for {item.name}: {exc}", True)
        active_ids = {item.id for item in items if item.is_pdf and not item.deleted and in_scope(item.id)}
        for item_id, old in previous.items():
            if item_id not in active_ids and old.get("state") != "cloud unavailable":
                self.store.save_remarkable_item(item_id, {**old, "state": "cloud unavailable",
                                                        "reason_kind": review_kind(old),
                                                        "reason": "Outside the selected source, removed, or trashed in the cloud; local copy retained."})
        self.store.set_preference("remarkable_last_sync", {"at": timestamp(), "downloaded": downloaded,
                                                          "reviewed": reviewed, "failed": failed})
        self.store.set_preference("remarkable_error", "")
        self.store.set_preference("remarkable_storage_usage", source_storage_usage(self.store))
        self.post(f"reMarkable sync finished: {downloaded} PDFs updated, {reviewed} for review, {failed} to retry.", True)
        return changed

    def register_download(self, item, bundle, payload):
        """Retain cloud identity even when routing/conversion requires review."""
        from .paper_registry import Registry
        from .remarkable_outbox import Outbox
        registry = Registry(self.store)
        cloud_source = Outbox(self).register_committed(item, hashlib.sha256(bundle[item.id + ".pdf"]).hexdigest(),
                                 metadata={"cloud_hash": item.hash,
                                    "parent": item.parent, "path": payload.get("cloud_path", "")})
        if payload.get("state") == "synced" and payload.get("storage_version") != 2:
            from .cloud_notes import document_path
            anchor = canonical(document_path(self.store, item.id))
            migrated = next((source for source in registry.sources(cloud_source['paper_id'])
                if source['kind'] == 'local' and source['locator'] == anchor
                and source['metadata'].get('tablet_layers_only') is True
                and source['metadata'].get('cloud_id') == item.id), None)
            if migrated:
                # Note migration can commit before sync-record publication is
                # interrupted. Do not recreate its retired legacy source on a
                # retry; adoption will finish publishing the complete record.
                return
        path = payload.get("path") if payload.get("state") == "synced" else payload.get("review_pdf")
        if path:
            # This path was just generated from this exact cloud item. Earlier
            # scans may already have registered it before identity support was
            # added; its provenance safely supplies the relationship.
            existing = next((source for source in registry.sources() if source["kind"] == "local"
                             and source["path"] == canonical(path)), None)
            if existing and existing["paper_id"] != cloud_source["paper_id"]:
                registry.link_sources([existing["id"], cloud_source["id"]], paper_id=cloud_source["paper_id"])
            mapping_identity = False
            try:
                import pymupdf as fitz
                with fitz.open(stream=bundle[item.id + ".pdf"], filetype="pdf") as original:
                    mapped = page_map(json.loads(bundle[item.id + ".content"]), original.page_count)
                    mapping_identity = ([index for _, index in mapped] == list(range(original.page_count))
                                        and payload.get("reason_kind") != "conversion")
            except Exception:
                # Missing evidence preserves independent inherited notes.
                pass
            registry.register_local(path, digest=payload.get("digest") if payload.get("state") == "synced"
                                    else payload.get("review_digest"), title=item.name,
                                    metadata={"cloud_id": item.id, "role": "tablet_render",
                                              "page_mapping_identity": mapping_identity,
                                              "original_pdf_digest": cloud_source["digest"]},
                                    paper_id=registry.paper_for_cloud(item.id))
