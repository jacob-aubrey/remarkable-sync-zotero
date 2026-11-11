"""Explicit, offline reconciliation of a selected library and cached tablet data.

Results are suggestions. Scanning never registers sources, changes mappings,
uploads files, edits PDFs, or assumes cached cloud data is a complete inventory.
"""
from collections import defaultdict
from copy import copy
import hashlib
import json
import os
from pathlib import Path
import re

from .storage import canonical


def _check(cancelled):
    if cancelled and (cancelled.is_set() if hasattr(cancelled, "is_set") else cancelled()):
        raise InterruptedError("Library comparison stopped")


def _digest(stream, cancelled):
    result = hashlib.sha256()
    while True:
        _check(cancelled)
        chunk = stream.read(1024 * 1024)
        if not chunk:
            return result.hexdigest()
        result.update(chunk)


def _title(value):
    value = re.sub(r"\s*\[reMarkable [^]]+\]$", "", str(value or ""), flags=re.I)
    value = re.sub(r"\.pdf$", "", value, flags=re.I)
    return " ".join(re.findall(r"\w+", value.casefold()))


def _doi(value):
    return re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", str(value or "").strip(), flags=re.I).casefold()


def _within(path, root):
    try:
        return Path(path).resolve().is_relative_to(root)
    except (OSError, ValueError):
        return False


def _row(kind, title, local_paths=(), cloud_ids=(), reason="", confidence="review", **extra):
    local_paths, cloud_ids = sorted(set(map(str, local_paths))), sorted(set(map(str, cloud_ids)))
    identity = json.dumps([kind, local_paths, cloud_ids, extra.get("folder_ids", [])], sort_keys=True)
    return {"id": hashlib.sha256(identity.encode()).hexdigest()[:24], "kind": kind,
            "title": title, "local_paths": local_paths, "cloud_ids": cloud_ids,
            "reason": reason, "confidence": confidence, **extra}


def _local_info(path, document, cancelled, inspect_pdf=None):
    _check(cancelled)
    with path.open("rb") as stream:
        digest = _digest(stream, cancelled)
    metadata = {}
    if document:
        try:
            metadata = json.loads(document.get("metadata", "{}"))
        except (TypeError, ValueError):
            pass
    metadata = metadata if isinstance(metadata, dict) else {}
    annotations, read_error = None, ""
    try:
        if inspect_pdf:
            checked_digest, inspected_metadata, annotations, read_error = inspect_pdf(path)
            if checked_digest != digest:
                raise OSError("PDF changed during comparison; run the check again.")
            metadata = {**inspected_metadata, **metadata}
        else:
            metadata, annotations, read_error = _inspect_locally(path, metadata, cancelled)
    except InterruptedError:
        raise
    except Exception as exc:
        read_error = "Could not inspect PDF annotations: " + str(exc)
    _check(cancelled)
    title = metadata.get("title") or path.stem
    return {"kind": "local", "path": canonical(path), "digest": digest, "title": title,
            "metadata": metadata, "annotation_count": annotations, "read_error": read_error,
            "project_ids": document.get("project_ids", []) if document else []}


def _inspect_locally(path, metadata, cancelled):
    import pymupdf as fitz
    annotations, read_error = None, ""
    with fitz.open(path) as pdf:
        if pdf.needs_pass:
            read_error = "This PDF requires a password; its annotations were not inspected."
        else:
            metadata = {**pdf.metadata, **metadata}
            annotations = 0
            for page in pdf:
                _check(cancelled)
                annotations += sum(1 for _ in page.annots() or ())
    return metadata, annotations, read_error


def _archive_info(store, cloud_id, cancelled):
    # Catalog IDs are data, never filenames supplied without validation.
    if not cloud_id or cloud_id in {".", ".."} or any(c in cloud_id for c in "/\\:"):
        return "", False, "Cached document ID cannot be used as an archive filename."
    try:
        from .remarkable_sources import source_info
        info = source_info(store, cloud_id, cancelled)
        if info is None:
            return "", False, "The tablet's original PDF has not been cached for comparison."
        return info["pdf_hash"], info["annotations_cached"], ""
    except InterruptedError:
        raise
    except Exception as exc:
        return "", False, "Could not compare the cached tablet original: " + str(exc)


def _registered_sources(store):
    """Reading an old library must not create the optional registry tables."""
    with store.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_sources'").fetchone():
            return {}
        return {(row["kind"], row["locator"]): dict(row) for row in db.execute("SELECT * FROM paper_sources")}


def _source_descriptor(source, registered):
    result = {key: source[key] for key in ("kind", "digest", "title", "metadata") if key in source}
    key = "path" if source["kind"] == "local" else "cloud_id"
    result[key] = source[key]
    if source.get("project_ids"):
        result["project_ids"] = source["project_ids"]
    known = registered.get((source["kind"], source[key]))
    if known:
        result.update(source_id=known["id"], paper_id=known["paper_id"])
        try:
            prior_metadata = json.loads(known.get("metadata", "{}"))
        except (TypeError, ValueError):
            prior_metadata = {}
        if isinstance(prior_metadata, dict):
            result["metadata"] = {**prior_metadata, **result.get("metadata", {})}
            if prior_metadata.get("role") == "tablet_render" and known.get("digest") != source.get("digest"):
                # A scanner cannot certify that a manually changed render kept
                # the renderer's previous page/provenance guarantees.
                result["metadata"]["page_mapping_identity"] = False
    return result


class ReconciliationRows(list):
    """Ordinary findings plus the in-memory inputs of their completed check."""
    def __init__(self, rows, snapshot):
        super().__init__(rows)
        self.snapshot = snapshot


class ReviewSnapshot:
    def __init__(self, local, cloud, registered, is_si, cloud_root=None):
        self.registered, self.is_si, self.cloud_root = registered, is_si, cloud_root
        self.sources = {(item["kind"], item.get("path") or item["cloud_id"]): item for item in [*local, *cloud]}
        self.order = {key: index for index, key in enumerate(self.sources)}
        self.keys, self.index = {}, defaultdict(set)
        self.removed = frozenset()
        for source_id, item in self.sources.items():
            role = is_si(item)
            keys = set()
            if item.get("digest"):
                keys.add(("digest", item["digest"]))
            if _title(item["title"]):
                keys.add(("title", _title(item["title"]), role))
            if _doi(item.get("metadata", {}).get("doi")):
                keys.add(("doi", _doi(item["metadata"]["doi"]), role))
            known = registered.get(source_id, {})
            if known.get("paper_id"):
                keys.add(("paper", known["paper_id"]))
            if item["kind"] == "local":
                keys.add(("path", item["path"]))
                keys.add(("title", _title(Path(item["path"]).stem), role))
            elif item.get("render_path"):
                keys.add(("path", canonical(item["render_path"])))
            self.keys[source_id] = keys
            for key in keys:
                self.index[key].add(source_id)

    def after_delete(self, rows, deleted):
        """Recompare only the connected paper groups, using cached records."""
        deleted = {canonical(path) for path in deleted}
        affected, pending = set(), [("local", path) for path in deleted]
        while pending:
            source_id = pending.pop()
            if source_id in affected or source_id in self.removed:
                continue
            affected.add(source_id)
            for key in self.keys.get(source_id, ()):
                pending.extend(self.index[key] - affected - self.removed)
        local_paths = {key for kind, key in affected if kind == "local"}
        cloud_ids = {key for kind, key in affected if kind == "cloud"}
        retained, errors = [], []
        for row in rows:
            # Scan locators are already absolute/resolved. Normalize casing
            # without asking the filesystem about every unaffected row again.
            row_paths = {os.path.normcase(os.path.abspath(path)) for path in row.get("local_paths", [])}
            related = local_paths.intersection(row_paths) or cloud_ids.intersection(row.get("cloud_ids", []))
            if not related:
                retained.append(row)
            elif row["kind"] == "local_read_error" and not deleted.intersection(row_paths):
                errors.append(row)
        removed = self.removed | {("local", path) for path in deleted}
        # Preserve the scan's ordering for deterministic variant grouping.
        local, cloud = [], []
        for source_id in sorted(affected - removed, key=lambda key: self.order.get(key, -1)):
            item = self.sources.get(source_id)
            if item is not None:
                (local if item["kind"] == "local" else cloud).append(item)
        changed = _compare_sources(local, cloud, self.registered, self.is_si, self.cloud_root)
        updated = copy(self)
        updated.removed = frozenset(removed)
        return ReconciliationRows([*retained, *errors, *changed], updated)


def _compare_sources(local, cloud, registered, is_si, cloud_root=None, cancelled=None):
    """Compare cached records; never enumerate or inspect PDF files."""
    rows = []
    by_digest = defaultdict(list)
    for item in local:
        by_digest[item["digest"]].append(item)
    for copies in by_digest.values():
        if len(copies) > 1:
            rows.append(_row("local_duplicate", copies[0]["title"], [item["path"] for item in copies],
                             reason="These local PDFs contain exactly the same bytes. Review which copies to keep; none will be removed.",
                             confidence="exact_bytes", sources=[_source_descriptor(item, registered) for item in copies],
                             actions=["link_papers", "keep_copies"]))
    local_candidates = defaultdict(dict)
    for item in local:
        title, doi = _title(item["title"]), _doi(item["metadata"].get("doi"))
        if title:
            local_candidates[("title", title, is_si(item))][item["path"]] = item
        if doi:
            local_candidates[("doi", doi, is_si(item))][item["path"]] = item
    suggested_groups = set()
    for (basis, _, _role), values in local_candidates.items():
        copies = list(values.values())
        paths = tuple(sorted(values))
        if len(copies) < 2 or len({item["digest"] for item in copies}) < 2 or paths in suggested_groups:
            continue
        suggested_groups.add(paths)
        rows.append(_row("local_possible_variants", copies[0]["title"], paths,
                         reason="These PDFs have the same " + basis + " but different bytes. They may hold independent annotations or different editions. Review before linking; keep their notes separate.",
                         confidence=basis + "_candidate", sources=[_source_descriptor(item, registered) for item in copies],
                         actions=["link_papers", "keep_copies"]))
    if cloud_root is None:
        return rows
    local = [item for item in local if Path(item["path"]).is_relative_to(cloud_root)]
    by_digest = defaultdict(list)
    for item in local:
        by_digest[item["digest"]].append(item)
    managed = {}
    for item in cloud:
        if item["render_path"]:
            managed[canonical(item["render_path"])] = item
    titles, dois = defaultdict(list), defaultdict(list)
    for item in local:
        if item["path"] not in managed:
            titles[(_title(item["title"]), is_si(item))].append(item)
            titles[(_title(Path(item["path"]).stem), is_si(item))].append(item)
            if _doi(item["metadata"].get("doi")):
                dois[(_doi(item["metadata"]["doi"]), is_si(item))].append(item)
    matched_paths = set(managed)
    for item in cloud:
        _check(cancelled)
        originals = [candidate for candidate in by_digest.get(item["digest"], []) if candidate["path"] not in managed] if item["digest"] else []
        known_cloud = registered.get(("cloud", item["cloud_id"]))
        linked = [candidate for candidate in local if known_cloud and
                  registered.get(("local", candidate["path"]), {}).get("paper_id") == known_cloud["paper_id"]]
        candidates = originals or linked
        basis = "exact_original" if originals else "confirmed_link" if linked else ""
        if not candidates:
            doi = _doi(item.get("metadata", {}).get("doi"))
            candidates = dois.get((doi, is_si(item)), []) if doi else []
            basis = "doi_candidate" if candidates else ""
            if not candidates:
                candidates = titles.get((_title(item["title"]), is_si(item)), []) if _title(item["title"]) else []
                basis = "title_candidate" if candidates else ""
        candidates = list({candidate["path"]: candidate for candidate in candidates}.values())
        if candidates:
            matched_paths.update(candidate["path"] for candidate in candidates)
            local_paths = [candidate["path"] for candidate in candidates]
            if item["render_path"] and item["render_path"] in {candidate["path"] for candidate in local}:
                local_paths.append(item["render_path"])
            annotation_variants = item["strokes_cached"] and any(candidate["annotation_count"] for candidate in candidates)
            if basis == "exact_original":
                reason = ("The local PDF exactly matches the cached tablet original."
                          if item["original_hash_source"] == "archive" else
                          "The local PDF exactly matches the original PDF hash recorded in the cached cloud listing.")
                reason += " Confirm a shared paper identity; both copies and their notes are retained."
            elif basis == "confirmed_link":
                reason = "These sources already share a confirmed paper identity. Their PDFs and annotation histories remain separate."
            else:
                reason = "The " + ("DOI" if basis == "doi_candidate" else "title") + " suggests the same paper, but the PDF bytes have not matched. Review before linking."
            if annotation_variants:
                reason += " The PC PDF has annotations and the tablet archive contains stroke data; keep both annotation variants."
            if item["archive_error"]:
                reason += " " + item["archive_error"]
            rows.append(_row("annotation_variants" if annotation_variants else "paper_match" if basis in {"exact_original", "confirmed_link"} else "possible_paper_match",
                             item["title"], local_paths, [item["cloud_id"]], reason, basis,
                             sources=[_source_descriptor(candidate, registered) for candidate in candidates]
                                     + [_source_descriptor(item, registered)],
                             cached=True, basis=basis, annotation_variants=bool(annotation_variants),
                             actions=["keep_copies"] if basis == "confirmed_link" else ["link_papers", "keep_copies"]))
        else:
            reason = "This paper exists in cached tablet records, with no matching local original found in the selected folder. The live tablet has not been checked."
            if item["archive_error"]:
                reason += " " + item["archive_error"]
            rows.append(_row("cloud_cached_only", item["title"], cloud_ids=[item["cloud_id"]], reason=reason,
                             confidence="cached_only", cached=True, sources=[_source_descriptor(item, registered)],
                             actions=["review_download", "keep_copies"]))
    for item in local:
        if item["path"] not in matched_paths:
            rows.append(_row("local_not_compared", item["title"], [item["path"]],
                             reason="No match was found in the cached tablet data. This does not prove the paper is missing from reMarkable; refresh or review before sending it.",
                             confidence="cached_only", cached=True, sources=[_source_descriptor(item, registered)],
                             actions=["review_upload", "keep_copies"]))
    return rows


def scan_reconciliation(store, config, cancelled=None, *, local_root=None, inspect_pdf=None):
    """Hash a selected root and compare it to cached originals, without network.

    ``local_root`` or ``config['pc_root']`` must explicitly select the root. An
    unset reMarkable integration checks local duplicates only. A cached catalog
    never proves that an unmatched paper is absent from the live tablet.
    """
    configured_root = local_root or config.get("pc_root")
    if not configured_root:
        raise ValueError("Choose the local folder to compare first.")
    root = Path(configured_root).resolve()
    if not root.is_dir():
        raise ValueError("The selected local folder does not exist.")
    _check(cancelled)
    registered = _registered_sources(store)
    with store.connect() as db:
        si_paths = {canonical(row[0]) for row in db.execute("SELECT path FROM supplement_files")}
        si_paths.update(canonical(row[0]) for row in db.execute("SELECT path FROM paper_attachments"))
        si_digests = {row[0] for row in db.execute("SELECT digest FROM supplement_files") if row[0]}
    si_identities = {row["paper_id"] for (kind, locator), row in registered.items()
                     if (kind == "local" and locator in si_paths) or json.loads(row.get("metadata", "{}")).get("is_supplementary_material")}

    def is_si(item):
        key = item.get("path") or item.get("cloud_id")
        known = registered.get((item["kind"], key), {})
        meta = json.loads(known.get("metadata", "{}"))
        return bool(key in si_paths or item.get("digest") in si_digests or known.get("paper_id") in si_identities or item.get("metadata", {}).get("is_supplementary_material") or meta.get("is_supplementary_material"))
    documents = {}
    for document in store.documents():
        if _within(document["path"], root):
            record = documents.setdefault(document["path"], {**document, "project_ids": []})
            record["project_ids"].append(document["project_id"])
    excluded_roots = [store.path.parent / "remarkable"]
    if config.get("review_dir"):
        excluded_roots.append(Path(config["review_dir"]))
    local, rows, directories = [], [], set()
    for path in root.rglob("*"):
        _check(cancelled)
        if any(part in {"Notebook backups", "notes_media", ".git"} for part in path.relative_to(root).parts):
            continue
        if not _within(path, root) or any(_within(path, excluded_root.resolve()) for excluded_root in excluded_roots):
            continue
        if path.is_dir():
            directories.add(canonical(path))
        elif path.is_file() and path.suffix.casefold() == ".pdf":
            try:
                info = _local_info(path, documents.get(canonical(path)), cancelled, inspect_pdf)
                local.append(info)
                if info["read_error"]:
                    rows.append(_row("local_read_error", info["title"], [info["path"]], reason=info["read_error"]))
            except OSError as exc:
                rows.append(_row("local_read_error", path.stem, [canonical(path)], reason=str(exc)))
    def local_result():
        return ReconciliationRows(rows + _compare_sources(local, [], registered, is_si),
                                  ReviewSnapshot(local, [], registered, is_si))
    integration_enabled = config.get("integration_enabled", config.get("cloud_enabled", config.get("enabled", False)))
    _check(cancelled)
    if not integration_enabled:
        return local_result()
    if not config.get("pc_root"):
        rows.append(_row("comparison_info", "Choose the tablet's linked PC folder",
                         reason="Local duplicate checks are complete. Select the linked PC root before comparing tablet records."))
        return local_result()
    cloud_root = Path(config["pc_root"]).resolve()
    if not root.is_relative_to(cloud_root) and not cloud_root.is_relative_to(root):
        rows.append(_row("comparison_info", "Local comparison only",
                         reason="This folder is outside the tablet's linked PC root. Only local duplicates were checked."))
        return local_result()
    directories = {path for path in directories if _within(path, cloud_root)}
    catalog = store.preference("remarkable_folders", [])
    catalog = catalog if isinstance(catalog, list) else []
    folders = {str(folder["id"]): folder for folder in catalog if isinstance(folder, dict) and folder.get("id")}
    scope_id = config.get("source_folder_id", "")
    source_folder = folders.get(scope_id, {})
    source_name = source_folder.get("name") or config.get("source_folder_name", "Literature")
    cloud = []
    manifests = store.remarkable_items()
    listing = store.preference("remarkable_documents", [])
    listing = listing if isinstance(listing, list) else []
    cloud_records = dict(manifests)
    for item in listing:
        if isinstance(item, dict) and item.get("id"):
            cloud_id = str(item["id"])
            cloud_records[cloud_id] = {**manifests.get(cloud_id, {}), **item}
    for cloud_id, payload in cloud_records.items():
        _check(cancelled)
        parent = payload.get("parent", "")
        parent_folder = folders.get(parent, {})
        ancestor_ids = parent_folder.get("ancestor_ids", [])
        path_parts = str(payload.get("cloud_path", "")).strip("/").split("/")
        if scope_id:
            in_scope = parent == scope_id or scope_id in ancestor_ids
            # Older records can lack the parent catalog. Their display path is
            # only enough for a cached suggestion, never for a write operation.
            if parent not in folders:
                source_path = str(source_folder.get("path", "")).strip("/").split("/")
                in_scope = bool(source_folder.get("path") and path_parts[:len(source_path)] == source_path)
        else:
            in_scope = bool(source_name and path_parts and path_parts[0].casefold() == source_name.casefold())
        if not in_scope:
            continue
        digest, strokes, error = _archive_info(store, str(cloud_id), cancelled)
        catalog_digest = payload.get("pdf_hash", "")
        if catalog_digest:
            if digest and digest != catalog_digest:
                strokes = False
                error = "The cached archive is an older revision than the cached cloud listing. Refresh its annotations before comparing notes."
            digest = catalog_digest
        metadata = payload.get("metadata", {})
        metadata = metadata if isinstance(metadata, dict) else {}
        cloud.append({"kind": "cloud", "cloud_id": str(cloud_id), "digest": digest,
                      "title": payload.get("name") or Path(payload.get("cloud_path", "Paper")).name,
                      "metadata": metadata, "parent": parent,
                      "strokes_cached": strokes, "archive_error": error,
                      "render_path": payload.get("path", ""), "render_digest": payload.get("digest", ""),
                      "state": payload.get("state", ""), "cloud_path": payload.get("cloud_path", ""),
                      "original_hash_source": "catalog" if catalog_digest else "archive"})
    rows.extend(_compare_sources(local, cloud, registered, is_si, cloud_root, cancelled))
    _folder_rows(cloud_root if cloud_root.is_relative_to(root) else root,
                 directories, folders, scope_id, config, rows, cancelled)
    _check(cancelled)
    return ReconciliationRows(rows, ReviewSnapshot(local, cloud, registered, is_si, cloud_root))


def _folder_rows(root, directories, folders, scope_id, config, rows, cancelled):
    from .remarkable_sync import folder_destination
    mapped_paths = set()
    for folder_id, folder in folders.items():
        _check(cancelled)
        if folder_id == scope_id or scope_id not in folder.get("ancestor_ids", []):
            continue
        try:
            destination = folder_destination(folder, list(folders.values()), config)
        except (OSError, ValueError):
            destination = None
        if destination and _within(destination, root):
            mapped_paths.add(canonical(destination))
            if destination.name.casefold() != str(folder.get("name", "")).casefold():
                rows.append(_row("folder_name_difference", folder.get("name", "Folder"), [canonical(destination)],
                                 reason="These folders are already linked but use different names. Keeping different names is supported.",
                                 confidence="confirmed_link", folder_ids=[folder_id], cached=True,
                                 actions=["keep_mapping", "review_mapping"]))
        else:
            same_name = sorted(path for path in directories if Path(path).name.casefold() == str(folder.get("name", "")).casefold())
            rows.append(_row("cloud_folder_unmapped", folder.get("name", "Folder"), same_name,
                             reason="This cached tablet folder has no resolved local destination. Link an existing folder, or review creating one; matching names alone do not confirm a link.",
                             confidence="cached_only", folder_ids=[folder_id], cached=True,
                             actions=["review_mapping", "review_create_local_folder"]))
    if scope_id and scope_id in folders:
        for path in sorted(directories - mapped_paths):
            rows.append(_row("local_folder_not_compared", Path(path).name, [path],
                             reason="This local folder has no resolved counterpart in the cached tablet folder catalog. Review the mapping before creating a tablet folder.",
                             confidence="cached_only", cached=True, actions=["review_mapping", "review_create_cloud_folder"]))


def link_registry_sources(store, sources, paper_id=None, *, citation=None):
    """Confirm a scanner suggestion without modifying its independent PDFs/notes.

    Accept existing registry source IDs or the scanner's source descriptors.
    Local descriptors are rehashed to reject stale review results.
    """
    from .paper_registry import Registry
    sources = list(sources)
    if len(sources) < 2:
        raise ValueError("Choose at least two paper sources to link.")
    for source in sources:
        if isinstance(source, dict) and source.get("kind") == "local":
            with Path(source["path"]).open("rb") as stream:
                actual = _digest(stream, None)
            if source.get("digest") and actual != source["digest"]:
                raise ValueError("A reviewed PDF changed. Run the comparison again before linking.")
        elif isinstance(source, dict) and source.get("kind") != "cloud":
            raise ValueError("Choose a local PDF or a cloud paper source.")
    registry = Registry(store)
    source_ids = []
    for source in sources:
        if not isinstance(source, dict):
            source_ids.append(str(source))
        elif source["kind"] == "local":
            project_ids = source.get("project_ids") or [None]
            for project_id in project_ids:
                registered = registry.register_local(source["path"], digest=source.get("digest"), project_id=project_id,
                    title=source.get("title", ""), metadata=source.get("metadata"))
            source_ids.append(registered["id"])
        else:
            source_ids.append(registry.register_cloud(source["cloud_id"], digest=source.get("digest", ""),
                title=source.get("title", ""), metadata=source.get("metadata"))["id"])
    return registry.link_sources(source_ids, paper_id, citation=citation)
