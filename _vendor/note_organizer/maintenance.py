"""Verified portable backups and explicit, history-preserving storage maintenance."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import tempfile
import uuid
import zipfile


def stats(store):
    with store.connect() as db:
        page = db.execute("PRAGMA page_size").fetchone()[0]
        return {"database_bytes": store.path.stat().st_size,
                "reclaimable_bytes": db.execute("PRAGMA freelist_count").fetchone()[0] * page,
                "images": db.execute("SELECT count(*) FROM note_images").fetchone()[0],
                "notes": db.execute("SELECT count(*) FROM entries").fetchone()[0],
                "revisions": db.execute("SELECT count(*) FROM history").fetchone()[0],
                "inline_images": sum(db.execute(f"SELECT count(*) FROM {table} WHERE json_type(payload,'$.image')='text'").fetchone()[0]
                                     for table in ("entries", "history"))}


def verify_backup(path):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Backup contains duplicate paths.")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("format") != 1 or "library.sqlite3" not in manifest.get("files", {}):
            raise ValueError("This is not a supported library backup.")
        if set(names) != {"manifest.json", *manifest["files"]}:
            raise ValueError("Backup file list differs from its manifest.")
        for member in [*manifest.get("folders", {}), *manifest.get("originals", {})]:
            parts = PurePosixPath(member)
            if not member.startswith("recovered-originals/") or parts.is_absolute() or any(part in {"..", "."} or ":" in part or "\\" in part for part in parts.parts):
                raise ValueError("Backup contains an unsafe recovery path.")
        for name, digest in manifest["files"].items():
            parts = PurePosixPath(name)
            if parts.is_absolute() or any(part in {"..", "."} or ":" in part or "\\" in part for part in parts.parts):
                raise ValueError("Backup contains an unsafe path.")
            with archive.open(name) as source:
                if hashlib.file_digest(source, "sha256").hexdigest() != digest:
                    raise ValueError(f"Backup verification failed: {name}")
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "library.sqlite3"
            with archive.open("library.sqlite3") as source, database.open("wb") as target:
                import shutil
                shutil.copyfileobj(source, target)
            with closing(sqlite3.connect(database)) as db:
                if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or db.execute("PRAGMA foreign_key_check").fetchone():
                    raise ValueError("Backup database failed its integrity checks.")
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "note_images" in tables:
                    for key, encoding, data in db.execute("SELECT * FROM note_images"):
                        if hashlib.sha256(encoding.encode() + b":" + data).hexdigest() != key:
                            raise ValueError("A saved note image failed its content check.")
                    for table in ("entries", "history"):
                        if db.execute(f"SELECT 1 FROM {table} e LEFT JOIN note_images i ON i.digest=json_extract(e.payload,'$.image_ref') WHERE json_type(e.payload,'$.image_ref') IS NOT NULL AND i.digest IS NULL LIMIT 1").fetchone():
                            raise ValueError("A saved note refers to a missing image.")
        return manifest


def backup(store, target, include_originals=True):
    target = Path(target).resolve()
    private = store.path.parent.resolve()
    if target.is_relative_to(private) and target.relative_to(private).parts[0] != "backups":
        raise ValueError("Choose a backup outside the active library, or inside its backups subfolder.")
    if target.exists():
        raise ValueError("Choose a new backup filename; existing backups are preserved.")
    manifest = {"format": 1, "created_at": datetime.now(timezone.utc).isoformat(), "files": {}, "originals": {},
                "library_root": str(store.path.parent.resolve()), "folders": {}}
    with tempfile.TemporaryDirectory(prefix="notable-backup-") as temporary:
        snapshot = Path(temporary) / "library.sqlite3"
        with store.connect() as source, closing(sqlite3.connect(snapshot)) as destination:
            source.backup(destination)
        files = [(snapshot, "library.sqlite3")]
        # DB is a SQLite snapshot. Private immutable baselines/layers/recovery
        # sources are included; disposable reading PDFs and logs are omitted.
        for path in store.path.parent.rglob("*"):
            if path.is_symlink() or not path.is_file() or path == store.path:
                continue
            relative = path.relative_to(store.path.parent)
            if any(part in {"reading-cache", "backups", "tmp", ".git"} for part in relative.parts):
                continue
            if path.suffix.lower() == ".log" or path.name in {"instance.lock", "library.sqlite3-wal", "library.sqlite3-shm", "library.sqlite3-journal"} or path == target:
                continue
            files.append((path, relative.as_posix()))
        if include_originals:
            with closing(sqlite3.connect(snapshot)) as db:
                originals = {row[0] for row in db.execute("SELECT path FROM documents WHERE hidden=0")}
                originals.update(row[0] for row in db.execute("SELECT path FROM paper_attachments"))
                roots = sorted({Path(row[0]).resolve() for row in db.execute("SELECT root FROM projects")}, key=lambda root: len(root.parts))
            minimal = []
            for root in roots:
                if not any(root.is_relative_to(parent) for parent in minimal):
                    minimal.append(root)
            for index, root in enumerate(minimal):
                manifest["folders"][f"recovered-originals/folder-{index}"] = str(root)
            for number, name in enumerate(sorted(originals)):
                path = Path(name)
                if path.is_file() and not path.resolve().is_relative_to(store.path.parent.resolve()):
                    root = next((root for root in minimal if path.resolve().is_relative_to(root)), None)
                    member = (f"recovered-originals/folder-{minimal.index(root)}/" + path.resolve().relative_to(root).as_posix()) if root else f"recovered-originals/attachments/{number}/{path.name}"
                    manifest["originals"][member] = name
                    files.append((path, member))
        partial = target.with_name("." + target.name + "." + uuid.uuid4().hex + ".partial")
        created = False
        try:
            with zipfile.ZipFile(partial, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
                created = True
                for path, member in files:
                    before = path.stat()
                    digest = hashlib.sha256()
                    with path.open("rb") as source, archive.open(member, "w", force_zip64=True) as output:
                        while block := source.read(1024 * 1024):
                            digest.update(block)
                            output.write(block)
                    after = path.stat()
                    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                        raise OSError(f"File changed during backup; retry after sync finishes: {path}")
                    manifest["files"][member] = digest.hexdigest()
                archive.writestr("manifest.json", json.dumps(manifest, indent=2))
            verify_backup(partial)
            if os.name == "nt":
                # Windows rename refuses an existing destination, including
                # on OneDrive volumes where hard links are unavailable.
                os.rename(partial, target)
            else:
                os.link(partial, target)
        finally:
            if created:
                partial.unlink(missing_ok=True)
    return target


def restore(archive_path, destination):
    """Restore to a new folder; an active or existing library is never overwritten."""
    manifest = verify_backup(archive_path)
    destination = Path(destination).resolve()
    destination.mkdir()  # Exclusive: refuses existing folders, including active libraries.
    try:
        with zipfile.ZipFile(archive_path) as archive:
            for name in manifest["files"]:
                target = destination.joinpath(*PurePosixPath(name).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(name) as source, target.open("xb") as output:
                    import shutil
                    shutil.copyfileobj(source, output)
                with target.open("rb") as source:
                    if hashlib.file_digest(source, "sha256").hexdigest() != manifest["files"][name]:
                        raise ValueError("Backup changed during restoration; the partial recovery folder was retained.")
        _rebase_restored_library(destination, manifest)
        (destination / "backup-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    except Exception:
        # Leave partial recovery available for inspection; never delete a user's folder.
        raise
    return destination


def _rebase_restored_library(destination, manifest):
    """Restored libraries use their own private assets and original snapshots."""
    mappings = [(manifest.get("library_root", ""), str(destination))]
    mappings += [(old, str(destination.joinpath(*PurePosixPath(member).parts))) for member, old in manifest.get("folders", {}).items()]
    mappings += [(old, str(destination.joinpath(*PurePosixPath(member).parts))) for member, old in manifest.get("originals", {}).items()]
    mappings = sorted([(old, new) for old, new in mappings if old], key=lambda pair: len(pair[0]), reverse=True)
    for member in manifest.get("folders", {}):
        destination.joinpath(*PurePosixPath(member).parts).mkdir(parents=True, exist_ok=True)
    def rebase(value):
        if isinstance(value, dict):
            return {rebase(key): rebase(item) for key, item in value.items()}
        if isinstance(value, list):
            return [rebase(item) for item in value]
        if isinstance(value, str):
            for old, new in mappings:
                if value.casefold() == old.casefold():
                    return new
                if value.casefold().startswith(old.casefold().rstrip("/\\") + "\\") or value.casefold().startswith(old.casefold().rstrip("/\\") + "/"):
                    return str(Path(new) / value[len(old):].lstrip("/\\"))
        return value
    with closing(sqlite3.connect(destination / "library.sqlite3")) as db, db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        # These tables contain paths/JSON; FTS shadow tables are deliberately excluded.
        for table in tables & {"projects", "documents", "preferences", "remarkable_items", "paper_sources", "paper_attachments", "supplement_files", "scan_errors", "inbox_items", "inbox_jobs", "paper_source_revisions", "paper_link_operations"}:
            columns = [row[1] for row in db.execute(f"PRAGMA table_info({table})") if row[2].upper() == "TEXT"]
            for row in db.execute(f"SELECT rowid,{','.join(columns)} FROM {table}").fetchall():
                changes = {}
                for column, value in zip(columns, row[1:]):
                    if not isinstance(value, str):
                        continue
                    try:
                        parsed = json.loads(value)
                    except (ValueError, TypeError):
                        updated = rebase(value)
                    else:
                        revised = rebase(parsed)
                        updated = json.dumps(revised, ensure_ascii=False) if revised != parsed else value
                    if updated != value:
                        changes[column] = updated
                if changes:
                    db.execute(f"UPDATE {table} SET " + ",".join(f"{column}=?" for column in changes) + " WHERE rowid=?", (*changes.values(), row[0]))
        db.execute("UPDATE projects SET dirty=1,export_hash='',exported_at=''")
        # A restored snapshot must not replay remote transfers or resume
        # watching the original download Inbox before its setup is reviewed.
        for key, switches in (("inbox", ("enabled",)), ("remarkable", ("integration_enabled", "send_new_pdfs", "mirror_pc_folders", "mirror_cloud_folders"))):
            row = db.execute("SELECT value FROM preferences WHERE key=?", (key,)).fetchone()
            if row:
                config = json.loads(row[0])
                config.update({switch: False for switch in switches})
                db.execute("UPDATE preferences SET value=? WHERE key=?", (json.dumps(config), key))
        db.execute("DELETE FROM pdf_search_cache")
        if "pdf_index_versions" in tables:
            db.execute("DELETE FROM pdf_index_versions")
            db.execute("DELETE FROM pdf_text_index")
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Restored library failed its integrity check.")


def compact(store, backup_path):
    from .image_storage import migrate
    backup(store, backup_path)
    with store.connect() as db:
        changed = migrate(db)
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("Library integrity check failed; migration was rolled back.")
    with closing(sqlite3.connect(store.path, timeout=30)) as db:
        db.execute("VACUUM")
        db.execute("PRAGMA optimize")
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Compacted library failed verification. Your verified backup is available.")
    return changed, stats(store)
