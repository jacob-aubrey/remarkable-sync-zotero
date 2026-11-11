"""Rebuildable reading PDFs containing verified PC and tablet annotations.

Original PDFs and tablet layer archives are never changed. A paper relationship
alone is not permission to overlay two editions: the printed pages and their
positions must also match exactly. Cache files are expendable only while their
bytes still match the app's manifest.
"""
from collections import Counter
from concurrent.futures.process import BrokenProcessPool
from functools import wraps
import hashlib
import json
from pathlib import Path
import re
import threading
import time
from types import SimpleNamespace
import uuid

import pymupdf as fitz

from .remarkable_cloud import CloudError, _delete_guarded, _guarded_file, atomic_write
from .remarkable_pdf import RENDER_REVISION, UnsupportedAnnotations, annotated_pdf, page_map
from .pdf_margins import expand_annotation_margins
from .remarkable_sources import source_bundle, source_info
from .storage import canonical


CACHE_LIMIT_BYTES = 128 * 1024 * 1024
COMBINED_REVISION = 3
_cache_lock = threading.RLock()
_cache_maintenance = {}
_CACHE_NAME = re.compile(r"[a-f0-9]{64}(?:-[a-f0-9]{32})?\.pdf")


def _serialized(function):
    @wraps(function)
    def call(*args, **kwargs):
        with _cache_lock:
            return function(*args, **kwargs)
    return call


def _check(cancelled):
    if cancelled and (cancelled.is_set() if hasattr(cancelled, "is_set") else cancelled()):
        raise InterruptedError("Combined PDF preparation stopped")


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _read(path, cancelled=None):
    result = bytearray()
    with Path(path).open("rb") as stream:
        while True:
            _check(cancelled)
            chunk = stream.read(1024 * 1024)
            if not chunk:
                return bytes(result)
            result.extend(chunk)


def _sources(store, value):
    """Resolve without creating optional registry tables on a reading action."""
    value = str(value)
    with store.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_sources'").fetchone():
            return []
        row = db.execute("SELECT paper_id FROM paper_sources WHERE (kind='local' AND locator=?) "
                         "OR (kind='cloud' AND locator=?) OR paper_id=? OR id=? LIMIT 1",
                         (canonical(value), value, value, value)).fetchone()
        if not row:
            return []
        result = []
        for source in db.execute("SELECT * FROM paper_sources WHERE paper_id=? ORDER BY kind,locator", (row[0],)):
            record = dict(source)
            try:
                metadata = json.loads(record["metadata"])
            except (TypeError, ValueError):
                metadata = {}
            record["metadata"] = metadata if isinstance(metadata, dict) else {}
            result.append(record)
        return result


def _is_render(source):
    return source["metadata"].get("role") == "tablet_render" or bool(source["metadata"].get("managed_cloud_id"))


def _resource_signature(document, value, active=()):
    """Compare resource values independently of rewritten indirect-object IDs."""
    def reference(match):
        number = int(match.group(1))
        if number in active:
            raise ValueError("Cyclic page resources need visual verification")
        text = document.xref_object(number)
        stream = document.xref_stream(number)
        if stream is not None:
            # The parser returned decoded bytes. Lossless re-encoding changes
            # these storage attributes without changing the appearance/data.
            text = "<<" + " ".join("/" + key + " " + document.xref_get_key(number, key)[1]
                                  for key in sorted(document.xref_get_keys(number))
                                  if key not in {"Length", "Filter", "DecodeParms"}) + ">>"
        resolved = _resource_signature(document, text, (*active, number))
        return "<" + _digest(resolved.encode() + (stream or b"")) + ">"
    return re.sub(r"\b(\d+)\s+\d+\s+R\b", reference, value)


def _inherited_page_value(page, key):
    document, number, seen = page.parent, page.xref, set()
    while number:
        if number in seen:
            raise ValueError("Cyclic inherited PDF page properties")
        seen.add(number)
        kind, value = document.xref_get_key(number, key)
        if kind != "null":
            return value
        kind, parent = document.xref_get_key(number, "Parent")
        if kind == "null":
            return "null"
        if kind != "xref":
            raise ValueError("Invalid inherited PDF page properties")
        number = int(parent.split()[0])
    return "null"


def _same_content_objects(local_page, original_page):
    if local_page.read_contents() != original_page.read_contents():
        return False
    try:
        # Identical drawing commands can print differently after changing an
        # inherited resource, transparency group, or document layer visibility.
        for key in ("Resources", "Group", "UserUnit"):
            if _resource_signature(local_page.parent, _inherited_page_value(local_page, key)) != _resource_signature(
                    original_page.parent, _inherited_page_value(original_page, key)):
                return False
        for key in ("OCProperties", "OutputIntents"):
            local_value = local_page.parent.xref_get_key(local_page.parent.pdf_catalog(), key)[1]
            original_value = original_page.parent.xref_get_key(original_page.parent.pdf_catalog(), key)[1]
            if _resource_signature(local_page.parent, local_value) != _resource_signature(original_page.parent, original_value):
                return False
        return True
    except (RuntimeError, ValueError):
        return False


def _same_printed_pages(pc, baseline, cancelled):
    """Exact visible-page proof allows annotation-only PC edits, never fuzzy text.

    Geometry and structured text must agree, along with either identical decoded
    page content/resources or every rendered pixel with native annotations hidden.
    Flattened marks are printed content and therefore cause review. This
    intentionally rejects some harmless PDF rewrites.
    """
    if len(pc) != len(baseline):
        return False
    for local_page, original_page in zip(pc, baseline):
        _check(cancelled)
        geometry = lambda page: (tuple(page.mediabox), tuple(page.cropbox), tuple(page.rect), page.rotation)
        if geometry(local_page) != geometry(original_page):
            return False
        if local_page.get_text("rawdict") != original_page.get_text("rawdict"):
            return False
        if _same_content_objects(local_page, original_page):
            continue
        local_image = local_page.get_pixmap(alpha=False, annots=False)
        original_image = original_page.get_pixmap(alpha=False, annots=False)
        if (local_image.width, local_image.height, local_image.n, local_image.samples) != (
                original_image.width, original_image.height, original_image.n, original_image.samples):
            return False
    return True


def _annotation_signature(document, annot):
    info = annot.info
    # Appearance streams and embedded data can change while the basic note
    # properties stay equal. Both imported-note validation and stable-ID
    # deduplication use this full signature, excluding bookkeeping references.
    extra = {key: _resource_signature(document, document.xref_get_key(annot.xref, key)[1])
             for key in ("AP", "AS", "DA", "DS", "RC", "LE", "Q", "IT", "Name", "OC",
                         "FS", "Sound", "Movie", "3DD", "RichMediaContent")}
    return json.dumps({"type": annot.type, "rect": tuple(annot.rect), "vertices": annot.vertices,
                       "colors": annot.colors, "border": annot.border, "opacity": annot.opacity,
                       "flags": annot.flags, "blendmode": annot.blendmode,
                       "info": {key: info.get(key, "") for key in ("content", "subject", "title")},
                       "quote": document.xref_get_key(annot.xref, "RMQuote"), "extra": extra}, sort_keys=True)


def _baseline_annotations_preserved(pc, baseline, cancelled):
    """A missing imported note has unknown ancestry and cannot be discarded."""
    for local_page, original_page in zip(pc, baseline):
        _check(cancelled)
        original = Counter(_annotation_signature(baseline, note) for note in original_page.annots() or ())
        if not original:
            continue
        current = Counter(_annotation_signature(pc, note) for note in local_page.annots() or ())
        if original - current:
            return False
    return True


def _remove_proven_inherited_marks(data, pc_data, cancelled):
    """Keep every PC mark; omit only an identical new tablet mark with its ID."""
    with fitz.open(stream=pc_data, filetype="pdf") as pc, fitz.open(stream=data, filetype="pdf") as combined:
        changed, added = False, 0
        for pc_page, page in zip(pc, combined):
            _check(cancelled)
            inherited, existing_counts = {}, Counter()
            for annot in pc_page.annots() or ():
                identifier = annot.info.get("id", "")
                if identifier.startswith("remarkable-"):
                    signature = _annotation_signature(pc, annot)
                    inherited.setdefault(identifier, set()).add(signature)
                    existing_counts[(identifier, signature)] += 1
            seen = Counter()
            for annot in list(page.annots() or ()):
                identifier = annot.info.get("id", "")
                if not identifier.startswith("remarkable-"):
                    continue
                signature = _annotation_signature(combined, annot)
                key = identifier, signature
                seen[key] += 1
                # insert_pdf retains baseline annotations before scene output.
                if seen[key] <= existing_counts[key]:
                    continue
                if identifier in inherited:
                    if signature not in inherited[identifier]:
                        raise CloudError("A tablet note was also edited on the PC. Review these two versions before combining them.")
                    page.delete_annot(annot)
                    changed = True
                else:
                    added += 1
        return (combined.tobytes(garbage=4, deflate=True) if changed else data), added


def _validate_pair(pc_data, item, bundle, cancelled=None):
    _check(cancelled)
    try:
        original_data = bundle[item.id + ".pdf"]
        content = json.loads(bundle[item.id + ".content"])
        if content.get("fileType") != "pdf":
            raise CloudError("Only imported PDFs can be combined.")
        with fitz.open(stream=original_data, filetype="pdf") as baseline:
            if baseline.needs_pass:
                raise CloudError("The tablet's original PDF needs a password; review is required.")
            mapping = page_map(content, len(baseline))
            if [index for _, index in mapping] != list(range(len(baseline))):
                raise CloudError("Tablet pages were added, removed or reordered. Review the page mapping before combining PC and tablet notes.")
            if pc_data is not None:
                with fitz.open(stream=pc_data, filetype="pdf") as pc:
                    if pc.needs_pass:
                        raise CloudError("The PC PDF needs a password; review is required.")
                    if _digest(pc_data) != _digest(original_data):
                        if not _same_printed_pages(pc, baseline, cancelled):
                            raise CloudError("The PC and tablet PDFs have different printed pages. Review the editions before combining their notes.")
                        if not _baseline_annotations_preserved(pc, baseline, cancelled):
                            raise CloudError("The tablet's original PDF has notes that are missing or different in the PC PDF. Review both sources before combining their notes.")
        return original_data
    except (CloudError, InterruptedError, BrokenProcessPool):
        raise
    except (UnsupportedAnnotations, KeyError, TypeError, ValueError, RuntimeError) as exc:
        raise CloudError("The saved tablet annotations need review before a combined PDF can be prepared: " + str(exc)) from exc


def _combined_bytes(pc_data, item, bundle, cancelled=None):
    original_data = _validate_pair(pc_data, item, bundle, cancelled)
    current = dict(bundle)
    current[item.id + ".pdf"] = pc_data if pc_data is not None else original_data
    try:
        data = annotated_pdf(item, current, lambda: bool(cancelled and (
            cancelled.is_set() if hasattr(cancelled, "is_set") else cancelled())), expand_margins=False)
        data, added = _remove_proven_inherited_marks(data, current[item.id + ".pdf"], cancelled)
        data, expanded = _normalized_margins(data, cancelled)
        return data, added, expanded
    except (CloudError, InterruptedError, BrokenProcessPool):
        raise
    except (UnsupportedAnnotations, RuntimeError, ValueError) as exc:
        raise CloudError("The saved tablet annotations need review before a combined PDF can be prepared: " + str(exc)) from exc


def _normalized_margins(data, cancelled=None):
    """Expand a disposable copy only after source-coordinate proof/deduplication."""
    _check(cancelled)
    try:
        with fitz.open(stream=data, filetype="pdf") as document:
            if document.needs_pass:
                raise CloudError("The PDF needs a password before its margin notes can be displayed.")
            expanded = expand_annotation_margins(document, cancelled)
            _check(cancelled)
            return (document.tobytes(garbage=4, deflate=True) if expanded else data), expanded
    except (CloudError, InterruptedError, BrokenProcessPool):
        raise
    except (RuntimeError, ValueError) as exc:
        raise CloudError("The PDF margin notes need review before a complete reading PDF can be prepared: " + str(exc)) from exc


@_serialized
def render_combined_pdf(local_path, item, bundle, *, cancelled=None):
    """Render from an immutable bundle and an optional verified current PC PDF."""
    pc_data = _read(local_path, cancelled) if local_path is not None else None
    return _combined_bytes(pc_data, item, bundle, cancelled)[0]


def _cache_root(store):
    return store.path.parent / "remarkable" / "reading-cache"


def _cache_records(root, *, key=None, verify=True):
    records = []
    if not root.is_dir():
        return records
    for path in root.glob((key + "*.pdf") if key else "*.pdf"):
        if not _CACHE_NAME.fullmatch(path.name):
            continue
        manifest = path.with_suffix(".json")
        try:
            manifest_data = manifest.read_bytes()
            info = json.loads(manifest_data)
            if info.get("version") not in {1, 2, COMBINED_REVISION} or not isinstance(info.get("key"), str):
                continue
            if path.name.split("-", 1)[0].removesuffix(".pdf") != info["key"]:
                continue
            if key is not None and info["key"] != key:
                continue
            if verify and _digest(path.read_bytes()) != info.get("digest"):
                continue  # A user-edited cache file is preserved, never reused.
            info["_manifest_digest"] = _digest(manifest_data)
            info["_last_used_ns"] = manifest.stat().st_mtime_ns
            records.append((path, manifest, info, path.stat()))
        except (OSError, ValueError, TypeError):
            continue
    return records


def prune_reading_cache(store, *, limit_bytes=CACHE_LIMIT_BYTES, keep=(), max_removals=None):
    """Delete only integrity-verified generated PDFs; edited/unknown files stay."""
    with _cache_lock:
        kept = {canonical(path) for path in keep}
        records = _cache_records(_cache_root(store), verify=False)
        total, removed = sum(stat.st_size for _, _, _, stat in records), 0
        for path, manifest, info, stat in sorted(records, key=lambda row: row[2]["_last_used_ns"]):
            if total <= max(0, limit_bytes):
                break
            if max_removals is not None and removed >= max_removals:
                break
            if canonical(path) in kept:
                continue
            try:
                # Verify and delete through the same Windows file handle while
                # denying writers/renames. A separate hash then unlink has a
                # race that could discard a newly edited reading copy.
                _guarded_file(path, info["digest"], _delete_guarded, item_label="reading cache PDF")
                total -= stat.st_size
                removed += 1
                _guarded_file(manifest, info["_manifest_digest"], _delete_guarded,
                              item_label="reading cache manifest")
            except (OSError, CloudError):
                pass  # Open Windows PDF viewers may temporarily hold a file.
        return removed


def _cached_reading(store, key, create, cancelled, cache_limit_bytes):
    root = _cache_root(store)
    for path, manifest_path, manifest, _ in _cache_records(root, key=key):
        if manifest["key"] == key:
            _check(cancelled)
            # Access recency belongs to the cache metadata. Changing the PDF's
            # version on each note click invalidates live browser range reads
            # and needlessly reloads the document, losing its zoom/position.
            manifest_path.touch()
            now = time.monotonic()
            if now - _cache_maintenance.get(str(root), -120) >= 120:
                prune_reading_cache(store, limit_bytes=cache_limit_bytes, keep=[path], max_removals=16)
                if len(_cache_maintenance) >= 32:
                    _cache_maintenance.clear()
                _cache_maintenance[str(root)] = now
            return path
    data = create()
    if data is None:
        return None
    _check(cancelled)
    path = root / (key + ".pdf")
    if path.exists() or path.with_suffix(".json").exists():
        path = root / (key + "-" + uuid.uuid4().hex + ".pdf")
    try:
        atomic_write(path, data, require_absent=True)
    except CloudError:
        if not path.exists():
            raise
        # Another app process or user may publish during our absence check.
        path = root / (key + "-" + uuid.uuid4().hex + ".pdf")
        atomic_write(path, data, require_absent=True)
    manifest = {"version": COMBINED_REVISION, "key": key, "digest": _digest(data)}
    atomic_write(path.with_suffix(".json"), json.dumps(manifest, indent=2).encode(), require_absent=True)
    prune_reading_cache(store, limit_bytes=cache_limit_bytes, keep=[path])
    return path


def _materialize_original(store, path, data, cancelled, cache_limit_bytes):
    data = _read(path, cancelled) if data is None else data
    key = _digest(json.dumps({"version": COMBINED_REVISION, "native": _digest(data)}, sort_keys=True).encode())
    def create():
        normalized, expanded = _normalized_margins(data, cancelled)
        return normalized if expanded or path is None else None
    return _cached_reading(store, key, create, cancelled, cache_limit_bytes) or path


@_serialized
def materialize_combined(store, path_or_paper_id, *, cancelled=None, cache_limit_bytes=CACHE_LIMIT_BYTES):
    """Return the main original or one private, rebuildable combined reading PDF.

    The caller should prepare this on a worker, because verification and rendering
    can take time. Unavailable or ambiguous note sources raise a review message.
    """
    _check(cancelled)
    sources = _sources(store, path_or_paper_id)
    if not sources:
        path = Path(path_or_paper_id)
        if not path.is_file():
            raise CloudError("The paper's source PDF is unavailable. Review its location first.")
        return _materialize_original(store, path, None, cancelled, cache_limit_bytes)
    local_sources = [source for source in sources if source["kind"] == "local" and not _is_render(source)]
    available = [(source, Path(source["locator"])) for source in local_sources if Path(source["locator"]).is_file()]
    missing = [source for source in local_sources if not Path(source["locator"]).is_file()]
    local_data = [(source, path, _read(path, cancelled)) for source, path in available]
    if len({_digest(data) for _, _, data in local_data}) > 1:
        raise CloudError("This paper has multiple PC PDF versions with separate notes. Review which versions belong together before combining them.")
    primary, pc_data = (local_data[0][1], local_data[0][2]) if local_data else (None, None)
    clouds = [source for source in sources if source["kind"] == "cloud"]
    if not clouds:
        if primary is None or missing:
            raise CloudError("A linked PC PDF is unavailable. Review its location before preparing all of this paper's notes.")
        return _materialize_original(store, primary, pc_data, cancelled, cache_limit_bytes)
    saved_items = store.remarkable_items()
    cached = []
    for source in clouds:
        _check(cancelled)
        saved = saved_items.get(source["locator"], {})
        reason = str(saved.get("reason", ""))
        if (saved.get("reason_kind", reason) in {"conversion", "local_conflict"}
                or reason.startswith(("Annotation conversion needs review:", "A local PDF conflicts"))):
            raise CloudError("This paper has tablet annotations or a local PDF conflict waiting for review. Resolve it before preparing a combined PDF.")
        accepted_revision = (saved.get("hash") if saved.get("storage_version") == 2
                             and saved.get("notes_migrated") else None)
        info = source_info(store, source["locator"], cancelled, revision=accepted_revision)
        if info is None:
            raise CloudError("This paper's tablet notes have not been saved locally. Sync it before preparing a combined PDF.")
        cached.append((source, info))
    if missing and any(source.get("digest") not in {info["pdf_hash"] for _, info in cached} for source in missing):
        raise CloudError("A linked PC PDF with independent notes is unavailable. Restore its location before combining this paper's notes.")
    annotated = [(source, info) for source, info in cached if info["annotations_cached"]]
    if len(annotated) > 1:
        raise CloudError("This paper is linked to multiple tablet documents with separate notes. Review those copies before combining them.")
    if pc_data is None and len({info["pdf_hash"] for _, info in cached}) > 1:
        raise CloudError("This paper has multiple tablet PDF versions and no available PC original. Review those versions before preparing a combined PDF.")
    selected = annotated[0] if annotated else cached[0]
    source, info = selected
    # Check every linked baseline, including a blank tablet duplicate. Linking
    # cannot silently authorize putting the current notes on another edition.
    for candidate, details in cached:
        # The latest pointer may advance in another app process after inspection.
        # Reconstruct the exact immutable revision used for this cache key.
        bundle = source_bundle(store, candidate["locator"], cancelled, revision=details.get("cloud_hash"))
        if _digest(bundle[candidate["locator"] + ".pdf"]) != details["pdf_hash"]:
            raise CloudError("The saved cloud source changed during preparation. Retry before combining these notes.")
        if candidate is source:
            selected_bundle = bundle
        _validate_pair(pc_data, SimpleNamespace(id=candidate["locator"]), bundle, cancelled)
    if not annotated:
        original = primary or (Path(info["baseline_path"]) if info.get("baseline_path") else None)
        data = pc_data if pc_data is not None else selected_bundle[source["locator"] + ".pdf"]
        return _materialize_original(store, original, data, cancelled, cache_limit_bytes)
    key = _digest(json.dumps({"version": COMBINED_REVISION, "renderer": RENDER_REVISION,
                              "pc": _digest(pc_data) if pc_data is not None else None,
                              "sources": [(entry["locator"], details["pdf_hash"],
                                           details.get("layers_hash") or _digest(_read(details["layers_path"], cancelled)),
                                           details.get("cloud_hash"))
                                          for entry, details in cached]}, sort_keys=True).encode())
    original = primary or (Path(info["baseline_path"]) if info.get("baseline_path") else None)
    def create():
        data, added, expanded = _combined_bytes(pc_data, SimpleNamespace(id=source["locator"]), selected_bundle, cancelled)
        return data if added or expanded or original is None else None
    return _cached_reading(store, key, create, cancelled, cache_limit_bytes) or original


@_serialized
def export_combined(store, path_or_paper_id, destination, *, expected_digest=None,
                    require_absent=True, cancelled=None):
    """Create an explicitly requested durable PDF, never replace a note source."""
    destination = Path(destination).resolve()
    private = (store.path.parent / "remarkable").resolve()
    if destination.is_relative_to(private):
        raise CloudError("Choose an export location outside the app's private source and reading cache folders.")
    if Path(path_or_paper_id).is_file() and canonical(destination) == canonical(path_or_paper_id):
        raise CloudError("Choose a new export filename so the original PDF stays intact.")
    with store.connect() as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_sources'").fetchone():
            if db.execute("SELECT 1 FROM paper_sources WHERE kind='local' AND locator=?", (canonical(destination),)).fetchone():
                raise CloudError("Choose a new export filename. An original or linked note source cannot be replaced by a combined export.")
        if db.execute("SELECT 1 FROM documents WHERE path=?", (canonical(destination),)).fetchone():
            raise CloudError("Choose a new export filename. A saved PDF note source cannot be replaced by a combined export.")
    source = materialize_combined(store, path_or_paper_id, cancelled=cancelled)
    if canonical(destination) == canonical(source):
        raise CloudError("Choose a new export filename so the original PDF stays intact.")
    data = _read(source, cancelled)
    _check(cancelled)
    atomic_write(destination, data, expected_digest=expected_digest, require_absent=require_absent)
    return destination
