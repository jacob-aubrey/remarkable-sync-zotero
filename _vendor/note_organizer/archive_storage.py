"""Retire only recovery archives proven identical to retained source assets."""
import hashlib
from pathlib import Path
import zipfile
from .remarkable_sources import source_info
from .remarkable_cloud import _guarded_file, _delete_guarded


def digest(stream):
    return hashlib.file_digest(stream, "sha256").hexdigest()


def plan(store):
    rows = []
    saved = store.remarkable_items()
    for path in (store.path.parent / "remarkable" / "sources").glob("*.rmdoc"):
        row = {"path": str(path.resolve()), "bytes": path.stat().st_size, "ready": False, "reason": "Keep: migration needs review."}
        item_id = path.stem
        try:
            item = saved.get(item_id, {})
            if item.get("storage_version") != 2 or not item.get("notes_migrated"):
                rows.append(row)
                continue
            revisions = list(dict.fromkeys([item.get("legacy_cloud_hash"), item.get("hash"), None]))
            for revision in revisions:
                info = source_info(store, item_id, revision=revision)
                if not info or info.get("legacy"):
                    continue
                with zipfile.ZipFile(path) as raw, zipfile.ZipFile(info["layers_path"]) as layers:
                    if len(raw.namelist()) != len(set(raw.namelist())) or len(layers.namelist()) != len(set(layers.namelist())):
                        continue
                    original = item_id + ".pdf"
                    if set(raw.namelist()) != {original, *layers.namelist()}:
                        continue
                    with raw.open(original) as source:
                        if digest(source) != info["pdf_hash"]:
                            continue
                    identical = True
                    for name in layers.namelist():
                        with raw.open(name) as first, layers.open(name) as second:
                            if digest(first) != digest(second):
                                identical = False
                                break
                    if not identical:
                        continue
                with path.open("rb") as source:
                    row["digest"] = digest(source)
                row.update(ready=True, baseline=info["baseline_path"], baseline_digest=info["pdf_hash"],
                           layers=info["layers_path"], layers_digest=info["layers_hash"],
                           reason="Verified duplicate: every original byte is retained in shared baseline/layer storage; notes are migrated.")
                break
        except Exception as error:
            row["reason"] = "Keep: " + str(error)
        rows.append(row)
    return rows


def cleanup(store, values):
    from .maintenance import backup
    reviewed = {row["path"]: row for row in values["rows"]}
    current = {row["path"]: row for row in plan(store)}
    for path, row in reviewed.items():
        if not row.get("ready") or not current.get(path, {}).get("ready") or row.get("digest") != current[path].get("digest"):
            raise ValueError("A reviewed recovery archive changed. Review again; every archive was retained.")
    if not reviewed:
        raise ValueError("Select verified duplicate recovery archives first.")
    backup(store, values["backup"])
    removed, retained = [], []
    for path, row in reviewed.items():
        try:
            # Hold all three verified assets simultaneously. Writers and
            # replacements are denied until the archive has been retired.
            def baseline_guard(kernel, handle):
                def layers_guard(kernel, handle):
                    return _guarded_file(Path(path), row["digest"], _delete_guarded, item_label="recovery archive")
                return _guarded_file(Path(row["layers"]), row["layers_digest"], layers_guard, item_label="retained tablet layers")
            _guarded_file(Path(row["baseline"]), row["baseline_digest"], baseline_guard, item_label="retained PDF baseline")
            removed.append(path)
        except Exception as error:
            retained.append((path, str(error)))
    return {"removed": removed, "retained": retained, "backup": str(values["backup"])}
