"""Review and retire legacy reading copies without changing research sources.

Planning is read-only. Cleanup is a separate, explicit action: it verifies the
same source revision, paper identity, migrated note records and current notebook
links again, then removes only the unchanged app-owned reading copy. Unknown
external exports cannot be inventoried, so the caller must acknowledge that old
links in those files may stop working. A blocked item is always left intact.
"""
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET
import zipfile

from .remarkable_cloud import CloudError, _delete_guarded, _guarded_file
from .remarkable_sources import source_info
from .storage import canonical, digest_file, timestamp


def _hash_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _object(value):
    try:
        result = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        return {}
    return result if isinstance(result, dict) else {}


def _table(db, name):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _file_proof(path):
    """Reject directory redirects; the deletion guard later checks its handle."""
    path = Path(path)
    # Path equality follows the host's casing rules; resolve() also catches
    # redirected parent directories such as Windows junctions.
    if path.is_symlink() or path.resolve() != path.absolute():
        raise ValueError("The file path is redirected through a link or junction.")
    if not path.is_file():
        raise ValueError("The file is unavailable.")
    return {"path": canonical(path), "digest": digest_file(path), "bytes": path.stat().st_size}


def _linked_path(target, notebook):
    """Normalize local DOCX hyperlink targets, including escaped Windows paths."""
    parsed = urlsplit(target)
    if parsed.scheme and parsed.scheme.casefold() != "file":
        return None
    if parsed.scheme:
        value = unquote(parsed.path)
        if parsed.netloc and parsed.netloc.casefold() != "localhost":
            value = "//" + parsed.netloc + value
        if re.match(r"^/[A-Za-z]:[/\\]", value):
            value = value[1:]
    else:
        value = unquote(target.split("#", 1)[0])
    return canonical(Path(notebook).parent / value)


def _notebook_proof(project, legacy_path):
    output = Path(project["output"])
    if not output.exists():
        return {"path": canonical(output), "missing": True, "links_legacy": False}
    result = _file_proof(output)
    # Inspect the same byte snapshot whose digest is later guarded. Reading the
    # hyperlink XML through a second file open could inspect a different edit.
    snapshot = output.read_bytes()
    result.update(digest=hashlib.sha256(snapshot).hexdigest(), bytes=len(snapshot))
    result["links_legacy"] = False
    with zipfile.ZipFile(BytesIO(snapshot)) as archive:
        # Links may occur in headers, footers and other document parts too.
        for name in archive.namelist():
            if not name.endswith(".rels"):
                continue
            for relationship in ET.fromstring(archive.read(name)):
                if relationship.get("TargetMode", "").casefold() != "external":
                    continue
                if _linked_path(relationship.get("Target", ""), output) == legacy_path:
                    result["links_legacy"] = True
    return result


def _plan(store, cloud_id, payload, db, projects):
    path = payload.get("legacy_path") or (payload.get("path") if payload.get("storage_version") != 2 else "")
    if not path:
        return None
    if not isinstance(path, str):
        return {"cloud_id": cloud_id, "name": str(payload.get("name", cloud_id)), "path": "", "bytes": 0,
                "status": "blocked", "reasons": ["The saved reading-copy path is invalid; repair its source record first."]}
    result = {"cloud_id": cloud_id, "name": payload.get("name", Path(path).stem),
              "path": canonical(path), "digest": payload.get("legacy_digest", payload.get("digest", "")),
              "main_path": payload.get("main_path", payload.get("path", "")),
              "status": "blocked", "reasons": [], "bytes": 0}
    reasons, proofs = result["reasons"], []

    def block(message):
        reasons.append(message)

    if payload.get("legacy_removed_at") and not Path(path).exists():
        result.update(status="removed", reasons=["This legacy reading copy was already removed after review."])
        return result
    try:
        legacy = _file_proof(path)
    except (OSError, ValueError) as exc:
        block(f"The legacy reading copy cannot be verified: {exc}")
        return result
    result["bytes"] = legacy["bytes"]
    proofs.append(legacy)
    if legacy["digest"] != result["digest"]:
        block("The reading copy was changed outside Notable; its edits must be reviewed.")
    if not Path(path).name.casefold().endswith(f" [remarkable {str(cloud_id)[:8]}].pdf"):
        block("This filename is not a legacy Notable reading copy.")
    if payload.get("storage_version") != 2 or payload.get("notes_migrated") is not True:
        block("Saved notes have not finished moving to the new private source records.")
    if payload.get("state") != "synced":
        block("This paper still needs sync or source review.")

    try:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", str(cloud_id)):
            raise ValueError("The saved cloud document identity is invalid.")
        revision = payload.get("legacy_cloud_hash")
        if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{64}", revision):
            raise ValueError("The reading copy has no verified tablet revision; keep it for history review.")
        source_root = store.path.parent / "remarkable" / "sources"
        manifest_path = source_root / "history" / str(cloud_id) / (revision + ".json")
        # Older libraries may only retain their current manifest. The source
        # reader permits that fallback only when its revision matches exactly.
        if not manifest_path.is_file():
            manifest_path = source_root / (str(cloud_id) + ".json")
        before = _file_proof(manifest_path)
        info = source_info(store, cloud_id, revision=revision)
        if not info:
            raise ValueError("No retained tablet source is available.")
        if info.get("legacy"):
            raise ValueError("The retained source must first be upgraded and verified.")
        if info["pdf_hash"] != payload.get("original_pdf_digest"):
            raise ValueError("The retained original PDF does not match this paper's source record.")
        if info.get("cloud_hash") != revision or canonical(info["source_path"]) != before["path"]:
            raise ValueError("The retained tablet revision changed while checking cleanup; refresh the list.")
        for key, expected in (("baseline_path", info["pdf_hash"]), ("layers_path", info["layers_hash"]),
                              ("source_path", before["digest"])):
            proof = _file_proof(info[key])
            if proof["digest"] != expected:
                raise ValueError("The retained source changed while checking cleanup; refresh the list.")
            proofs.append(proof)
    except (CloudError, OSError, ValueError, TypeError, KeyError, zipfile.BadZipFile) as exc:
        info = None
        block(f"The retained tablet source cannot replace this copy safely: {exc}")

    main = None
    try:
        if not result["main_path"] or canonical(result["main_path"]) == legacy["path"]:
            raise ValueError("A separate main research PDF has not been selected.")
        main = _file_proof(result["main_path"])
        result["main_path"], result["main_digest"] = main["path"], main["digest"]
        proofs.append(main)
        if Path(main["path"]).is_relative_to((store.path.parent / "remarkable").resolve()):
            raise ValueError("The selected main PDF is inside Notable's private source storage.")
        if main["digest"] != payload.get("main_digest", payload.get("digest")):
            raise ValueError("The main PDF changed after migration; refresh its source review first.")
    except (OSError, ValueError, TypeError) as exc:
        block(f"The main paper cannot be verified: {exc}")

    sources, documents, saved_rows, affected = [], [], [], set()
    if not _table(db, "paper_sources") or not _table(db, "paper_document_sources"):
        block("Paper source identities have not been created yet.")
    else:
        sources = [dict(row) for row in db.execute("SELECT * FROM paper_sources")]
        cloud = next((row for row in sources if row["kind"] == "cloud" and row["locator"] == str(cloud_id)), None)
        legacy_source = next((row for row in sources if row["kind"] == "local" and row["locator"] == legacy["path"]), None)
        anchor = canonical(store.path.parent / "remarkable" / "documents" / (str(cloud_id) + ".pdf"))
        migrated = next((row for row in sources if row["kind"] == "local" and row["locator"] == anchor), None)
        if not cloud or not migrated or cloud["paper_id"] != migrated["paper_id"]:
            block("The private tablet source has not retained this paper's identity.")
        elif _object(migrated["metadata"]).get("cloud_id") != str(cloud_id) or _object(migrated["metadata"]).get("role") != "tablet_render":
            block("The private tablet source has no verified renderer provenance.")
        if legacy_source:
            block("The old reading path is still registered as an active paper source.")
        if main:
            primary = next((row for row in sources if row["kind"] == "local" and row["locator"] == main["path"]), None)
            if not primary or not cloud or primary["paper_id"] != cloud["paper_id"] or _object(primary["metadata"]).get("role") == "tablet_render":
                block("The main PDF is not registered as this paper's independent original.")
        old_ids = payload.get("legacy_document_ids")
        if not isinstance(old_ids, list) or any(not isinstance(value, str) for value in old_ids):
            block("The list of pre-migration saved note records is missing.")
            old_ids = []
        documents = [dict(row) for row in db.execute("SELECT * FROM documents WHERE path=?", (legacy["path"],))]
        if documents:
            block("Saved note records still depend on the legacy reading path.")
        for document_id in sorted(set(old_ids)):
            row = db.execute("SELECT d.*,m.source_id FROM documents d LEFT JOIN paper_document_sources m ON m.document_id=d.id WHERE d.id=?",
                             (document_id,)).fetchone()
            if row is None:
                block("A pre-migration saved note record is missing; keep the reading copy.")
                continue
            row = dict(row)
            metadata = _object(row["metadata"])
            project = next((project for project in projects if project["id"] == row["project_id"]), None)
            logical_folder = metadata.get("logical_folder")
            visible = (isinstance(logical_folder, str) and bool(logical_folder) and project
                       and Path(canonical(logical_folder)).is_relative_to(Path(project["root"])))
            if (row["path"] != anchor or row["hidden"] or row["missing"] or not migrated
                    or row["source_id"] != migrated["id"] or metadata.get("managed_cloud_id") != str(cloud_id)
                    or not main or not isinstance(metadata.get("primary_path"), str)
                    or canonical(metadata.get("primary_path", "")) != main["path"] or not visible):
                block("A saved note record has not retained its visible folder and private tablet identity.")
            affected.add(row["project_id"])
            documents.append(row)
            saved_rows.extend([dict(value) for value in db.execute("SELECT * FROM entries WHERE document_id=? ORDER BY key", (document_id,))])
            saved_rows.extend([dict(value) for value in db.execute("SELECT * FROM history WHERE document_id=? ORDER BY id", (document_id,))])

    notebooks = []
    for project in projects:
        try:
            proof = _notebook_proof(project, legacy["path"])
            notebooks.append(proof)
            if proof["links_legacy"]:
                block(f"Rebuild the generated notebook first: {project['output']}")
            if project["id"] in affected:
                if project["dirty"] or project.get("error"):
                    block(f"A generated notebook is still waiting to save: {project['output']}")
                if proof.get("missing") or proof.get("digest") != project.get("export_hash"):
                    block(f"The generated notebook has not been verified after migration: {project['output']}")
        except (OSError, ValueError, zipfile.BadZipFile, ET.ParseError) as exc:
            block(f"A generated notebook cannot be checked: {project['output']}: {exc}")
    proofs.extend({key: row[key] for key in ("path", "digest", "bytes")} for row in notebooks if "digest" in row)
    result["document_ids"] = sorted({value for value in payload.get("legacy_document_ids", []) if isinstance(value, str)}) if isinstance(payload.get("legacy_document_ids"), list) else []
    result["notebooks"] = notebooks
    result["reasons"] = list(dict.fromkeys(reasons))
    result["status"] = "blocked" if result["reasons"] else "ready"
    result["proof"] = _hash_json({"cloud_id": cloud_id, "payload": payload, "files": proofs,
                                  "sources": sources, "documents": documents, "notes": saved_rows,
                                  "projects": projects, "notebooks": notebooks})
    result["guard_files"] = list({row["path"]: row for row in proofs}.values())
    return result


def plan_legacy_cleanup(store):
    """List recoverable old reading copies and concrete blockers, without writes."""
    with store.connect() as db:
        projects = [dict(row) for row in db.execute("SELECT * FROM projects ORDER BY id")]
        results = []
        for row in db.execute("SELECT * FROM remarkable_items ORDER BY id").fetchall():
            plan = _plan(store, row["id"], _object(row["payload"]), db, projects)
            if plan:
                results.append(plan)
        return results


def _with_guards(files, callback):
    """Keep verified dependencies protected until the final removal completes."""
    if not files:
        return callback()
    first, *remaining = files
    return _guarded_file(first["path"], first["digest"],
                         lambda kernel, handle: _with_guards(remaining, callback), item_label="source or notebook")


def execute_legacy_cleanup(store, reviewed_plans, *, external_links_acknowledged=False):
    """Remove reviewed copies only; preserve all sources, note records and history.

    Each plan must be the unchanged result of :func:`plan_legacy_cleanup`.
    The explicit acknowledgment concerns old links in user-created exports or
    notebook backups, whose locations Notable cannot know. No files are removed
    when the acknowledgment is absent. One blocked copy does not stop another.
    """
    if external_links_acknowledged is not True:
        raise ValueError("Review the warning about old links in exported notebooks and backups before cleanup.")
    results, selected = [], set()
    for reviewed in reviewed_plans:
        cloud_id = reviewed.get("cloud_id") if isinstance(reviewed, dict) else None
        result = {"cloud_id": cloud_id, "path": reviewed.get("path", "") if isinstance(reviewed, dict) else "", "status": "blocked"}
        try:
            if not cloud_id or cloud_id in selected or reviewed.get("status") != "ready" or not reviewed.get("proof"):
                raise ValueError("Choose one ready, reviewed reading copy per paper.")
            selected.add(cloud_id)
            with store.connect() as db:
                db.execute("BEGIN IMMEDIATE")

                def current_plan():
                    row = db.execute("SELECT payload FROM remarkable_items WHERE id=?", (cloud_id,)).fetchone()
                    if not row:
                        raise ValueError("The paper source record is no longer available.")
                    payload = _object(row["payload"])
                    projects = [dict(row) for row in db.execute("SELECT * FROM projects ORDER BY id")]
                    current = _plan(store, cloud_id, payload, db, projects)
                    if current and current["status"] != "ready":
                        raise ValueError("Cleanup paused: " + " ".join(current["reasons"]))
                    if not current or current.get("proof") != reviewed["proof"]:
                        raise ValueError("The source, notes or notebook changed after review. Refresh the cleanup list.")
                    return current, payload

                current, payload = current_plan()
                dependencies = [row for row in current["guard_files"] if row["path"] != current["path"]]

                def remove_locked():
                    def remove(kernel, handle):
                        # The database write lock keeps source/note identities
                        # stable. Each file guard independently rechecks its
                        # reviewed digest on the protected handle; reopening
                        # those handles through Python would violate Windows
                        # sharing rules, and adds no further protection.
                        _delete_guarded(kernel, handle)
                        payload["legacy_removed_at"] = timestamp()
                        db.execute("UPDATE remarkable_items SET payload=? WHERE id=?", (json.dumps(payload), cloud_id))
                        result.update(path=current["path"], status="removed", bytes=current["bytes"])
                    return _guarded_file(current["path"], current["digest"], remove, item_label="legacy reading copy")

                _with_guards(dependencies, remove_locked)
        except (CloudError, OSError, ValueError, KeyError) as exc:
            result["reason"] = str(exc)
        results.append(result)
    return results
