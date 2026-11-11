"""Offline cloud sources: shared raw PDF baselines and separate tablet layers.

Generated reading PDFs stay separate. Legacy .rmdoc archives remain readable
and are never removed by migration. Only an explicit export embeds a baseline
and its layers together again.
"""
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
import zipfile

from .remarkable_cloud import CloudError, atomic_write
from .storage import timestamp


def _root(store):
    return store.path.parent / "remarkable"


def _safe_id(item_id):
    if not isinstance(item_id, str) or not item_id or item_id in {".", ".."} or any(c in item_id for c in "/\\:\x00\r\n"):
        raise CloudError("Invalid cached cloud document ID")
    return item_id


def _hash_stream(stream, cancelled=None):
    result = hashlib.sha256()
    while True:
        if cancelled and (cancelled.is_set() if hasattr(cancelled, "is_set") else cancelled()):
            raise InterruptedError("Cloud source check stopped")
        data = stream.read(1024 * 1024)
        if not data:
            return result.hexdigest()
        result.update(data)


def _checked_path(root, relative):
    if not isinstance(relative, str) or not relative:
        raise CloudError("Incomplete cached cloud source reference")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise CloudError("Cloud source reference is outside the private source folder")
    return path


def _manifest_digest(manifest):
    value = {key: item for key, item in manifest.items() if key != "manifest_hash"}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _validate_manifest(root, result, item_id, *, revision=None, historical=False):
    if not isinstance(result, dict):
        raise CloudError("Invalid cached cloud source manifest")
    if result.get("version") != 1 or result.get("id") != item_id:
        raise CloudError("Unsupported cached cloud source manifest")
    if any(not isinstance(result.get(key), str) or not re.fullmatch(r"[a-f0-9]{64}", result[key])
           for key in ("pdf_hash", "layers_hash")):
        raise CloudError("Invalid cached cloud source content hashes")
    if not isinstance(result.get("cloud_hash"), str) or not re.fullmatch(r"[a-f0-9]{64}", result["cloud_hash"]):
        raise CloudError("Invalid cached cloud source revision hash")
    if revision is not None and result["cloud_hash"] != revision:
        raise CloudError("Cached cloud source revision does not match its history filename")
    if historical or "manifest_hash" in result:
        if result.get("manifest_hash") != _manifest_digest(result):
            raise CloudError("Cached cloud source manifest failed its integrity check")
    _checked_path(root, result.get("baseline"))
    _checked_path(root, result.get("layers"))
    if result["baseline"] != "baselines/" + result["pdf_hash"] + ".pdf":
        raise CloudError("The cached PDF baseline does not match its content hash")
    if result["layers"] != "layers/" + item_id + "/" + result["layers_hash"] + ".zip":
        raise CloudError("The cached tablet layer reference does not match its content hash")
    if result.get("pdf_file") != item_id + ".pdf":
        raise CloudError("Invalid cached original PDF identity")
    return result


def _revision_path(store, item_id, revision):
    if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{64}", revision):
        raise CloudError("Invalid cloud source revision; choose a saved revision hash")
    root = _root(store)
    relative = "sources/history/" + _safe_id(item_id) + "/" + revision + ".json"
    return _checked_path(root, relative)


def _manifest(store, item_id, *, revision=None):
    root = _root(store)
    latest = _checked_path(root, "sources/" + _safe_id(item_id) + ".json")
    path = _revision_path(store, item_id, revision) if revision is not None else latest
    historical = revision is not None
    if not path.is_file():
        if revision is None:
            return None
        # Libraries saved before revision history can still read their known
        # latest version. An anonymous legacy archive cannot prove a revision.
        if not latest.is_file():
            raise CloudError("That cloud source revision is not saved locally")
        path, historical = latest, False
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise CloudError("The cached cloud source manifest could not be read") from exc
    return _validate_manifest(root, result, item_id, revision=revision, historical=historical)


def _preserve_manifest(store, manifest):
    """Publish an immutable revision before changing the latest pointer."""
    manifest = {**manifest, "manifest_hash": _manifest_digest(manifest)}
    item_id, revision = manifest["id"], manifest["cloud_hash"]
    root = _root(store)
    _validate_manifest(root, manifest, item_id, revision=revision, historical=True)
    path = _revision_path(store, item_id, revision)
    if path.exists():
        saved = _manifest(store, item_id, revision=revision)
        # Repeating the same revision must not rewrite immutable history just
        # because the current clock changed. Its payload references must agree.
        stable = lambda value: {key: item for key, item in value.items() if key not in {"at", "manifest_hash"}}
        if stable(saved) != stable(manifest):
            raise CloudError("A saved cloud revision has different source references; both copies were preserved for review")
        return saved
    try:
        atomic_write(path, json.dumps(manifest, indent=2).encode("utf-8"), require_absent=True)
    except CloudError:
        if not path.exists():
            raise
        saved = _manifest(store, item_id, revision=revision)
        if {key: value for key, value in saved.items() if key not in {"at", "manifest_hash"}} != {
                key: value for key, value in manifest.items() if key not in {"at", "manifest_hash"}}:
            raise CloudError("A different saved cloud revision appeared during publication; it was preserved")
        return saved
    return manifest


def save_source_bundle(store, item, bundle):
    """Publish references only after every source byte was saved successfully."""
    item_id = _safe_id(item.id)
    if not isinstance(item.hash, str) or not re.fullmatch(r"[a-f0-9]{64}", item.hash):
        raise CloudError("Invalid cloud document revision hash")
    original_name = item_id + ".pdf"
    if original_name not in bundle or item_id + ".content" not in bundle:
        raise CloudError("Incomplete cloud source bundle")
    root = _root(store)
    previous = _manifest(store, item_id)
    digest = hashlib.sha256(bundle[original_name]).hexdigest()
    baseline = _checked_path(root, "baselines/" + digest + ".pdf")
    if baseline.exists():
        with baseline.open("rb") as stream:
            if _hash_stream(stream) != digest:
                raise CloudError("A saved baseline failed its integrity check; it was preserved for review.")
    else:
        atomic_write(baseline, bundle[original_name], require_absent=True)
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(bundle.items()):
            if name != original_name:
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, data)
    layers = stream.getvalue()
    layer_hash = hashlib.sha256(layers).hexdigest()
    # Content addressing also shares unchanged layers and detects a corrupt
    # local archive without replacing it. Revision IDs live in the manifest.
    layer_path = _checked_path(root, "layers/" + item_id + "/" + layer_hash + ".zip")
    if layer_path.exists():
        with layer_path.open("rb") as source:
            if _hash_stream(source) != layer_hash:
                raise CloudError("Saved tablet layers failed their integrity check; review needed.")
    else:
        atomic_write(layer_path, layers, require_absent=True)
    result = {"version": 1, "id": item_id, "cloud_hash": item.hash,
              "pdf_hash": digest, "pdf_file": original_name,
              "baseline": baseline.relative_to(root).as_posix(),
              "layers": layer_path.relative_to(root).as_posix(), "layers_hash": layer_hash,
              "files": [{"id": name, "size": len(data)} for name, data in sorted(bundle.items())],
              "at": timestamp()}
    if previous is not None:
        _preserve_manifest(store, previous)
    result = _preserve_manifest(store, result)
    atomic_write(_checked_path(root, "sources/" + item_id + ".json"), json.dumps(result, indent=2).encode("utf-8"))
    return result


def source_info(store, item_id, cancelled=None, *, revision=None):
    """Verify a cached baseline and inspect small layer metadata without loading it."""
    root, item_id = _root(store), _safe_id(item_id)
    manifest = _manifest(store, item_id, revision=revision)
    if manifest:
        baseline = _checked_path(root, manifest["baseline"])
        layers = _checked_path(root, manifest["layers"])
        with baseline.open("rb") as stream:
            digest = _hash_stream(stream, cancelled)
        if digest != manifest["pdf_hash"]:
            raise CloudError("Cached PDF baseline failed its integrity check")
        with layers.open("rb") as stream:
            if _hash_stream(stream, cancelled) != manifest["layers_hash"]:
                raise CloudError("Cached tablet layers failed their integrity check")
        with zipfile.ZipFile(layers) as archive:
            names = archive.namelist()
            content = json.loads(archive.read(item_id + ".content"))
            annotations = any((entry.filename.endswith(".rm") or ".highlights/" in entry.filename)
                              and entry.file_size > 0 for entry in archive.infolist())
        return {"pdf_hash": digest, "content": content, "annotations_cached": annotations,
                "layer_files": names, "layers_path": str(layers), "baseline_path": str(baseline),
                "pdf_file": manifest["pdf_file"], "layers_hash": manifest["layers_hash"],
                "source_path": str(_revision_path(store, item_id, revision) if revision is not None
                                   and _revision_path(store, item_id, revision).is_file()
                                   else root / "sources" / (item_id + ".json")),
                "cloud_hash": manifest.get("cloud_hash", ""), "legacy": False}
    archive_path = _checked_path(root, "sources/" + item_id + ".rmdoc")
    if not archive_path.is_file():
        return None
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        if item_id + ".pdf" not in names:
            raise CloudError("The legacy cloud archive has no original PDF")
        with archive.open(item_id + ".pdf") as stream:
            digest = _hash_stream(stream, cancelled)
        content = json.loads(archive.read(item_id + ".content")) if item_id + ".content" in names else {}
        annotations = any((entry.filename.endswith(".rm") or ".highlights/" in entry.filename)
                          and entry.file_size > 0 for entry in archive.infolist())
    return {"pdf_hash": digest, "content": content, "annotations_cached": annotations,
            "layer_files": [name for name in names if name != item_id + ".pdf"],
            "layers_path": str(archive_path), "source_path": str(archive_path), "legacy": True}


def source_bundle(store, item_id, cancelled=None, *, revision=None):
    """Reconstruct original PDF and exact tablet files entirely offline."""
    info = source_info(store, item_id, cancelled, revision=revision)
    if info is None:
        raise CloudError("This cloud document has not been saved locally yet")
    layer_data = Path(info["layers_path"]).read_bytes()
    if info.get("layers_hash") and hashlib.sha256(layer_data).hexdigest() != info["layers_hash"]:
        raise CloudError("Cached tablet layers changed during reconstruction")
    with zipfile.ZipFile(BytesIO(layer_data)) as archive:
        result = {name: archive.read(name) for name in archive.namelist()}
    if not info["legacy"]:
        data = Path(info["baseline_path"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != info["pdf_hash"]:
            raise CloudError("Cached baseline changed during reconstruction")
        result[info["pdf_file"]] = data
    return result


def export_source_archive(store, item_id, path):
    """Explicit portable .rmdoc export; ordinary sync stores only references."""
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in source_bundle(store, item_id).items():
            archive.writestr(name, data)
    atomic_write(path, stream.getvalue(), require_absent=True)
    return Path(path)


def source_storage_usage(store):
    """Account for private source files without scanning or changing user PDFs."""
    root = _root(store)
    result = {}
    for name in ("baselines", "layers", "sources", "blobs", "previous", "reading-cache"):
        folder = root / name
        result[name] = sum(file.stat().st_size for file in folder.rglob("*") if file.is_file()) if folder.exists() else 0
    result["total"] = sum(result.values())
    return result
