"""SQLite is the source of truth; Word files are replaceable exports."""
from contextlib import contextmanager, closing, nullcontext
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import uuid
from .search import compile_search, normalize
from .image_storage import intern as intern_image, expand as expand_image, image as stored_image
from .paper_projection import (load_paper_sources, annotated_documents, inherited_duplicate_ids,
                               project_papers)


def canonical(path):
    return os.path.normcase(str(Path(path).resolve()))


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def digest_file(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def entry_sort_key(entry):
    rect = entry["rect"]
    return (entry["path"].casefold(), entry["page"], tuple(entry.get("order", (rect[1], rect[0], 0))), rect[1], rect[0])


DEFAULT_SETTINGS = {"grouping": "Nearby blocks", "gap": 24, "ocr": False,
                    "context_padding": 36, "notebook_style": "Reading"}

SEARCH_FIELDS = {
    "quote": "Highlights and marked text",
    "comment": "Typed notes and replies", "ocr": "Handwriting transcription (OCR)",
    "context": "Surrounding printed text", "color": "Annotation color",
}
DEFAULT_SEARCH_FIELDS = tuple(key for key in SEARCH_FIELDS if key != "context")
NOTE_TYPES = ("All notes", "Highlights and markup", "Drawings and handwriting", "Typed notes")


def search_values(entry):
    """Return the same original field text used by filtering and previews."""
    values = {key: entry.get(key, "") for key in ("quote", "comment", "ocr", "context", "color")}
    return {key: str(value) for key, value in values.items()}


def matches_search(entry, query, fields, match_mode, note_type, matcher=None):
    kind = entry.get("kind", "")
    if note_type == "Highlights and markup" and kind not in {"highlight", "underline", "strikeout", "squiggly"}:
        return False
    if note_type == "Drawings and handwriting" and kind != "drawing":
        return False
    if note_type == "Typed notes" and not entry.get("comment"):
        return False
    if not query.strip() and len(query) <= 4096:
        return True
    matcher = matcher or compile_search(query, match_mode)
    values = search_values(entry)
    haystacks = [normalize(values.get(key, "")) for key in fields if key in SEARCH_FIELDS]
    return matcher(haystacks)


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self._supplements = threading.local()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY, root TEXT UNIQUE NOT NULL,
                    output TEXT NOT NULL, settings TEXT NOT NULL,
                    dirty INTEGER NOT NULL DEFAULT 1, export_hash TEXT DEFAULT '',
                    exported_at TEXT DEFAULT '', error TEXT DEFAULT '');
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    path TEXT NOT NULL, digest TEXT NOT NULL, signature TEXT NOT NULL,
                    settings TEXT NOT NULL, metadata TEXT NOT NULL, scanned_at TEXT NOT NULL,
                    missing INTEGER DEFAULT 0, UNIQUE(project_id,path));
                CREATE TABLE IF NOT EXISTS entries (
                    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                    key TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(document_id,key));
                CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                    key TEXT NOT NULL, action TEXT NOT NULL, at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS preferences (key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS scan_errors (
                    project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
                    path TEXT NOT NULL, error TEXT NOT NULL, at TEXT NOT NULL,
                    PRIMARY KEY(project_id,path));
                CREATE TABLE IF NOT EXISTS remarkable_items (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS document_project ON documents(project_id);
                CREATE TABLE IF NOT EXISTS pdf_search_cache (
                    document_id TEXT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
                    digest TEXT NOT NULL, version TEXT NOT NULL, pages TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS paper_citations (
                    document_id TEXT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
                    payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS paper_attachments (
                    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                    path TEXT NOT NULL, url TEXT NOT NULL DEFAULT '', PRIMARY KEY(document_id,path));
                CREATE TABLE IF NOT EXISTS supplement_files (
                    path TEXT PRIMARY KEY, digest TEXT NOT NULL, details TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS history_by_document ON history(document_id,id DESC);
                CREATE INDEX IF NOT EXISTS supplement_by_digest ON supplement_files(digest);
                CREATE INDEX IF NOT EXISTS attachment_by_path ON paper_attachments(path);
                CREATE INDEX IF NOT EXISTS document_digest ON documents(project_id,digest);
                CREATE TABLE IF NOT EXISTS note_images (
                    digest TEXT PRIMARY KEY, encoding TEXT NOT NULL, data BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS document_dois (
                    document_id TEXT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE, doi TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS documents_by_doi ON document_dois(doi);
                CREATE TABLE IF NOT EXISTS document_labels (
                    document_id TEXT REFERENCES documents(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL CHECK(kind IN ('tag','collection')), value TEXT NOT NULL,
                    PRIMARY KEY(document_id,kind,value));
                CREATE INDEX IF NOT EXISTS labels_by_value ON document_labels(kind,value);
            """)
            if "hidden" not in {r[1] for r in db.execute("PRAGMA table_info(documents)")}:
                db.execute("ALTER TABLE documents ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
            if "note_revision" not in {r[1] for r in db.execute("PRAGMA table_info(documents)")}:
                db.execute("ALTER TABLE documents ADD COLUMN note_revision INTEGER NOT NULL DEFAULT 0")
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_sources'").fetchone():
                from .paper_registry import ensure_document_sources
                ensure_document_sources(db)
            from .supplement_library import migrate_filings
            migrate_filings(db)
            from .supplement_library import index_doi
            for row in db.execute("SELECT id FROM documents WHERE id NOT IN (SELECT document_id FROM document_dois)").fetchall():
                index_doi(db, row[0])
            from .search_index import initialize
            initialize(db)
        # Legacy filing association performs I/O only after schema commits.
        from .supplement_library import reconcile
        reconcile(self)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def projects(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM projects ORDER BY root")]

    def preference(self, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM preferences WHERE key=?", (key,)).fetchone()
        try:
            return json.loads(row[0]) if row else default
        except (ValueError, TypeError):
            return default

    def set_preference(self, key, value):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO preferences(key,value) VALUES(?,?)", (key, json.dumps(value)))

    def remarkable_items(self):
        with self.connect() as db:
            return {r["id"]: json.loads(r["payload"]) for r in db.execute("SELECT * FROM remarkable_items")}

    def save_remarkable_item(self, item_id, payload):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO remarkable_items VALUES(?,?)", (item_id, json.dumps(payload)))

    def relocate_source(self, old_path, new_path):
        with self.connect() as db:
            from .supplement_library import move_paths
            move_paths(db, old_path, new_path)
            for row in db.execute("SELECT id,project_id FROM documents WHERE path=?", (canonical(old_path),)).fetchall():
                root = db.execute("SELECT root FROM projects WHERE id=?", (row["project_id"],)).fetchone()[0]
                if Path(new_path).resolve().is_relative_to(Path(root)):
                    db.execute("UPDATE documents SET path=? WHERE id=?", (canonical(new_path), row["id"]))
                else:
                    db.execute("UPDATE documents SET hidden=1 WHERE id=?", (row["id"],))
                db.execute("UPDATE projects SET dirty=1 WHERE id=?", (row["project_id"],))

    def relocate_project(self, project_id, new_root):
        """Reconnect a moved folder without moving files or rebuilding its history.

        Nested watched roots move with the selected subtree. An output outside
        that subtree stays associated with its existing project.
        """
        new_root = canonical(new_root)
        if not Path(new_root).is_dir():
            raise ValueError("Choose the existing folder at its new location.")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            project = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if project is None:
                raise ValueError("The watched folder no longer exists in the library.")
            old_root = Path(project["root"])
            if str(old_root) == new_root:
                return dict(project)
            destination = Path(new_root)
            if destination.is_relative_to(old_root) or old_root.is_relative_to(destination):
                raise ValueError("Choose the moved folder, outside its previous root.")

            def relocated(path):
                resolved = Path(canonical(path))
                return canonical(destination / resolved.relative_to(old_root)) if resolved.is_relative_to(old_root) else path

            projects = db.execute("SELECT * FROM projects").fetchall()
            moving = {row["id"]: row for row in projects if Path(row["root"]).is_relative_to(old_root)}
            roots = {row["root"]: row["id"] for row in projects if row["id"] not in moving}
            for row in moving.values():
                if relocated(row["root"]) in roots:
                    raise ValueError("A folder at the new location is already watched.")
            documents = db.execute("SELECT id,project_id,path,metadata FROM documents").fetchall()
            moving_documents = [row for row in documents if row["project_id"] in moving
                                and Path(row["path"]).is_relative_to(old_root)]
            moving_ids = {row["id"] for row in moving_documents}
            occupied_documents = {(row["project_id"], row["path"]) for row in documents if row["id"] not in moving_ids}
            if any((row["project_id"], relocated(row["path"])) in occupied_documents for row in moving_documents):
                raise ValueError("A saved document already occupies a relocated path.")
            sources = []
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_sources'").fetchone():
                local_sources = db.execute("SELECT id,locator FROM paper_sources WHERE kind='local'").fetchall()
                sources = [row for row in local_sources if Path(row["locator"]).is_relative_to(old_root)]
                moving_sources = {row["id"] for row in sources}
                occupied = {row["locator"] for row in local_sources if row["id"] not in moving_sources}
                if any(relocated(row["locator"]) in occupied for row in sources):
                    raise ValueError("A registered source already occupies a relocated path.")
            errors = [row for row in db.execute("SELECT * FROM scan_errors") if row["project_id"] in moving]
            for row in moving.values():
                db.execute("UPDATE projects SET root=?,output=?,dirty=1 WHERE id=?",
                           (relocated(row["root"]), relocated(row["output"]), row["id"]))
            for row in moving_documents:
                path = relocated(row["path"])
                db.execute("UPDATE documents SET path=?,missing=? WHERE id=?", (path, int(not Path(path).is_file()), row["id"]))
            for row in documents:
                metadata = json.loads(row["metadata"])
                if not metadata.get("managed_cloud_id"):
                    continue
                updated = dict(metadata)
                for key in ("primary_path", "logical_folder"):
                    if metadata.get(key):
                        updated[key] = relocated(metadata[key])
                if updated != metadata:
                    db.execute("UPDATE documents SET metadata=?,missing=0 WHERE id=?",
                               (json.dumps(updated, sort_keys=True), row["id"]))
                    db.execute("UPDATE projects SET dirty=1 WHERE id=?", (row["project_id"],))
            for row in documents:
                if row["project_id"] not in moving and Path(row["path"]).is_relative_to(old_root):
                    db.execute("UPDATE documents SET missing=? WHERE id=?", (int(not Path(row["path"]).is_file()), row["id"]))
                    db.execute("UPDATE projects SET dirty=1 WHERE id=?", (row["project_id"],))
            for row in sources:
                db.execute("UPDATE paper_sources SET locator=?,updated_at=? WHERE id=?",
                           (relocated(row["locator"]), timestamp(), row["id"]))
            from .supplement_library import move_paths
            attachment_paths = {row[0] for row in db.execute("SELECT path FROM paper_attachments")}
            attachment_paths.update(row[0] for row in db.execute("SELECT path FROM supplement_files"))
            for path in attachment_paths:
                if relocated(path) != path:
                    move_paths(db, path, relocated(path))
            for row in errors:
                db.execute("DELETE FROM scan_errors WHERE project_id=? AND path=?", (row["project_id"], row["path"]))
                db.execute("INSERT INTO scan_errors VALUES(?,?,?,?)",
                           (row["project_id"], relocated(row["path"]), row["error"], row["at"]))
            for key, field in (("remarkable", "pc_root"), ("inbox", "library_root")):
                saved = db.execute("SELECT value FROM preferences WHERE key=?", (key,)).fetchone()
                if saved is None:
                    continue
                config = json.loads(saved["value"])
                if not isinstance(config, dict):
                    continue
                updated = dict(config)
                if isinstance(config.get(field), str) and config[field]:
                    updated[field] = relocated(config[field])
                if key == "remarkable" and isinstance(config.get("mappings"), dict):
                    updated["mappings"] = {folder_id: relocated(path) if isinstance(path, str) and path else path
                                           for folder_id, path in config["mappings"].items()}
                if updated != config:
                    db.execute("UPDATE preferences SET value=? WHERE key=?", (json.dumps(updated), key))
            for row in db.execute("SELECT * FROM remarkable_items").fetchall():
                manifest = json.loads(row["payload"])
                if not isinstance(manifest, dict):
                    continue
                updated = dict(manifest)
                for key in ("path", "review_pdf", "main_path", "legacy_path"):
                    if isinstance(manifest.get(key), str) and manifest[key]:
                        updated[key] = relocated(manifest[key])
                if isinstance(manifest.get("recovery_paths"), list):
                    updated["recovery_paths"] = [relocated(path) if isinstance(path, str) and path else path
                                                  for path in manifest["recovery_paths"]]
                if updated != manifest:
                    db.execute("UPDATE remarkable_items SET payload=? WHERE id=?", (json.dumps(updated), row["id"]))
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inbox_jobs'").fetchone():
                for row in db.execute("SELECT id,path FROM inbox_jobs").fetchall():
                    path = relocated(row["path"])
                    if path != row["path"]:
                        db.execute("UPDATE inbox_jobs SET path=? WHERE id=?", (path, row["id"]))
            from .remarkable_outbox import rebase_outbox_paths
            rebase_outbox_paths(db, old_root, destination)
            return dict(db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone())

    def project(self, project_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            return dict(row) if row else None

    def default_settings(self):
        """Global defaults apply to new watched folders; existing choices stay put."""
        saved = self.preference("default_settings", {})
        saved = saved if isinstance(saved, dict) else {}
        return {**DEFAULT_SETTINGS, **{key: saved[key] for key in DEFAULT_SETTINGS if key in saved}}

    def add_project(self, root, output=None):
        root = canonical(root)
        output = output or str(Path(root) / f"Notes on {Path(root).name}.generated.docx")
        settings = json.dumps(self.default_settings(), sort_keys=True)
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO projects(root,output,settings) VALUES(?,?,?)",
                       (root, str(output), settings))
            return db.execute("SELECT id FROM projects WHERE root=?", (root,)).fetchone()[0]

    def remove_project(self, project_id):
        with self.connect() as db:
            db.execute("DELETE FROM projects WHERE id=?", (project_id,))

    def set_settings(self, project_id, settings):
        with self.connect() as db:
            db.execute("UPDATE projects SET settings=? WHERE id=?",
                       (json.dumps(settings, sort_keys=True), project_id))
            db.execute("UPDATE projects SET dirty=1 WHERE id=?", (project_id,))

    def mark_dirty(self, project_id):
        with self.connect() as db:
            db.execute("UPDATE projects SET dirty=1 WHERE id=?", (project_id,))

    def export_result(self, project_id, file_hash=None, error=""):
        with self.connect() as db:
            if file_hash is not None:
                db.execute("UPDATE projects SET dirty=0,export_hash=?,exported_at=?,error='' WHERE id=?",
                           (file_hash, timestamp(), project_id))
            else:
                db.execute("UPDATE projects SET dirty=1,error=? WHERE id=?", (error, project_id))

    def documents(self, project_id=None):
        with self.connect() as db:
            sql = "SELECT d.*, (SELECT count(*) FROM entries e WHERE e.document_id=d.id) AS count FROM documents d"
            rows = db.execute(sql + " WHERE hidden=0" + (" AND project_id=?" if project_id else "") + " ORDER BY path",
                              (project_id,) if project_id else ())
            return [dict(r) for r in rows]

    def document_at(self, project_id, path):
        with self.connect() as db:
            row = db.execute("SELECT * FROM documents WHERE project_id=? AND path=?",
                             (project_id, canonical(path))).fetchone()
            return dict(row) if row else None

    def search_documents(self, project_id=None, document_id=None, *, whole_paper=False):
        """Resolve a chosen logical paper to its independently retained sources."""
        with self.connect() as db:
            rows = annotated_documents(self.documents(project_id), load_paper_sources(db))
        if document_id:
            selected = next((row for row in rows if row["id"] == document_id), None)
            if not selected:
                return []
            paper_id = selected.get("logical_paper_id")
            rows = [row for row in rows if row["id"] == document_id or
                    (paper_id and row.get("logical_paper_id") == paper_id or
                     whole_paper and canonical(row["path"]) == canonical(selected["path"])) and
                    (whole_paper or row["project_id"] == selected["project_id"])]
        return rows

    def cached_pdf_pages(self, document_id, digest, version):
        from .search_index import read, write
        indexed = read(self, document_id, digest, version, "", "All words")
        if indexed is not None:
            return indexed[0]
        with self.connect() as db:
            row = db.execute("SELECT pages FROM pdf_search_cache WHERE document_id=? AND digest=? AND version=?",
                             (document_id, digest, version)).fetchone()
            if not row:
                return None
            pages = json.loads(row[0])
            if not isinstance(pages, list):
                return None
            # Promote the pre-FTS cache lazily. This keeps startup cheap while
            # allowing existing libraries to use their already extracted text.
            write(db, document_id, digest, version, pages)
            return pages

    def cache_pdf_pages(self, document_id, digest, version, pages):
        with self.connect() as db:
            # A scan or folder removal during extraction must not resurrect an old index.
            saved = db.execute("""INSERT OR REPLACE INTO pdf_search_cache SELECT id,digest,?,? FROM documents
                          WHERE id=? AND digest=? AND hidden=0""",
                       (version, "[]", document_id, digest))
            if saved.rowcount:
                from .search_index import write
                write(db, document_id, digest, version, pages)

    def scan_result(self, project_id, path, error=""):
        with self.connect() as db:
            if error:
                db.execute("INSERT OR REPLACE INTO scan_errors VALUES(?,?,?,?)",
                           (project_id, canonical(path), error, timestamp()))
            else:
                db.execute("DELETE FROM scan_errors WHERE project_id=? AND path=?", (project_id, canonical(path)))

    def errors(self, project_id):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM scan_errors WHERE project_id=?", (project_id,))]

    def save_scan(self, project_id, path, digest, signature, settings, metadata, entries, *, connection=None):
        """Replace a complete successful scan in one transaction. Failed scans never erase notes."""
        path = canonical(path)
        with self.connect() if connection is None else nullcontext(connection) as db:
            old = db.execute("SELECT * FROM documents WHERE project_id=? AND path=?", (project_id, path)).fetchone()
            if not old:
                # A unique identical file with a vanished old path is a rename; copies remain independent.
                candidates = db.execute("SELECT * FROM documents WHERE project_id=? AND digest=?",
                                        (project_id, digest)).fetchall()
                candidates = [r for r in candidates if not json.loads(r["metadata"]).get("managed_cloud_id")
                              and not Path(r["path"]).exists()]
                if len(candidates) == 1:
                    old = candidates[0]
            doc_id = old["id"] if old else str(uuid.uuid4())
            previous = {r["key"]: json.dumps(intern_image(db, json.loads(r["payload"])), sort_keys=True, ensure_ascii=False) for r in db.execute(
                "SELECT key,payload FROM entries WHERE document_id=?", (doc_id,))}
            current = {}
            for entry in entries:
                if entry["key"] in current:
                    raise ValueError("Duplicate annotation identity; the previous saved notes were retained.")
                current[entry["key"]] = json.dumps(intern_image(db, entry), sort_keys=True, ensure_ascii=False)
            changed = (old is None or old["path"] != path or old["metadata"] != json.dumps(metadata, sort_keys=True)
                       or old["digest"] != digest or bool(old["missing"]) or bool(old["hidden"]))
            if old and old["path"] != path and db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_sources'").fetchone():
                source = db.execute("SELECT id FROM paper_sources WHERE kind='local' AND locator=?", (old["path"],)).fetchone()
                occupied = db.execute("SELECT id FROM paper_sources WHERE kind='local' AND locator=?", (path,)).fetchone()
                if source and occupied and source["id"] != occupied["id"]:
                    raise ValueError("The renamed path already belongs to another paper source; saved notes were retained.")
                if source:
                    db.execute("UPDATE paper_sources SET locator=?,updated_at=? WHERE id=?", (path, timestamp(), source["id"]))
            db.execute("""INSERT INTO documents(id,project_id,path,digest,signature,settings,metadata,scanned_at)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET path=excluded.path,digest=excluded.digest,
                signature=excluded.signature,settings=excluded.settings,metadata=excluded.metadata,
                scanned_at=excluded.scanned_at,missing=0,hidden=0""",
                (doc_id, project_id, path, digest, signature, settings, json.dumps(metadata, sort_keys=True), timestamp()))
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_document_sources'").fetchone():
                source = db.execute("SELECT id FROM paper_sources WHERE kind='local' AND locator=?", (path,)).fetchone()
                if source:
                    db.execute("""INSERT INTO paper_document_sources VALUES(?,?)
                        ON CONFLICT(document_id) DO UPDATE SET source_id=excluded.source_id""", (doc_id, source["id"]))
            counts = {"added": 0, "updated": 0, "removed": 0}
            for key in previous.keys() | current.keys():
                if previous.get(key) == current.get(key):
                    continue
                action = "removed" if key not in current else "added" if key not in previous else "updated"
                counts[action] += 1
                db.execute("INSERT INTO history(document_id,key,action,at,payload) VALUES(?,?,?,?,?)",
                           (doc_id, key, action, timestamp(), current.get(key, previous.get(key))))
            db.execute("DELETE FROM entries WHERE document_id=?", (doc_id,))
            db.executemany("INSERT INTO entries(document_id,key,payload) VALUES(?,?,?)",
                           [(doc_id, key, payload) for key, payload in current.items()])
            if any(counts.values()):
                # Text/OCR may change even when the PDF digest and note count
                # stay equal. Readers use this small revision instead of blobs.
                db.execute("UPDATE documents SET note_revision=note_revision+1 WHERE id=?", (doc_id,))
            if changed or any(counts.values()):
                db.execute("UPDATE projects SET dirty=1 WHERE id=?", (project_id,))
            from .supplement_library import index_doi
            index_doi(db, doc_id)
        if connection is None:
            pending = getattr(self._supplements, "pending", None)
            if pending is None:
                from .supplement_library import reconcile
                reconcile(self, [(path, digest)])
            else:
                pending.append((path, digest))
        return counts

    @contextmanager
    def supplement_batch(self):
        previous = getattr(self._supplements, "pending", None)
        self._supplements.pending = [] if previous is None else previous
        try:
            yield
        finally:
            if previous is None:
                self.flush_supplements()
            self._supplements.pending = previous

    def flush_supplements(self):
        pending = getattr(self._supplements, "pending", None)
        if pending:
            from .supplement_library import reconcile
            reconcile(self, pending)
            pending.clear()

    def mark_missing(self, project_id, seen):
        with self.connect() as db:
            for row in db.execute("SELECT path FROM scan_errors WHERE project_id=?", (project_id,)).fetchall():
                if row["path"] not in seen:
                    db.execute("DELETE FROM scan_errors WHERE project_id=? AND path=?", (project_id, row["path"]))
            for row in db.execute("SELECT id,path,missing,metadata FROM documents WHERE project_id=?", (project_id,)).fetchall():
                metadata = json.loads(row["metadata"])
                # A managed tablet source can be reconstructed offline. Its
                # stable locator is deliberately not a permanent reading PDF.
                missing = 0 if metadata.get("managed_cloud_id") else int(row["path"] not in seen)
                if row["missing"] != missing:
                    db.execute("UPDATE documents SET missing=? WHERE id=?", (missing, row["id"]))
                    db.execute("UPDATE projects SET dirty=1 WHERE id=?", (project_id,))

    def entries(self, project_id=None, document_id=None, query="", fields=None,
                match_mode="All words", note_type="All notes", include_images=True, cancelled=None):
        """Read note text first; the reader can request a selected image separately."""
        if cancelled and cancelled():
            raise InterruptedError("Note query cancelled")
        matcher = compile_search(query, match_mode) if query.strip() or len(query) > 4096 else None
        filter_entries = matcher is not None or note_type != "All notes"
        with self.connect() as db:
            sources = load_paper_sources(db)
            paper_documents, paper_titles, selected_ids = [], {}, None
            if sources:
                scope_project = project_id
                selected = db.execute("SELECT * FROM documents WHERE id=? AND hidden=0", (document_id,)).fetchone() if document_id else None
                if selected and scope_project is None:
                    scope_project = selected["project_id"]
                document_sql = "SELECT id,path,project_id,digest,metadata,missing FROM documents WHERE hidden=0"
                if scope_project is not None:
                    document_sql += " AND project_id=?"
                paper_documents = annotated_documents(
                    [dict(row) for row in db.execute(document_sql, (scope_project,) if scope_project is not None else ())], sources)
                for paper in project_papers(paper_documents):
                    if paper.get("logical_paper_id"):
                        paper_titles[(paper["project_id"], paper["logical_paper_id"])] = paper["logical_paper_title"]
                selected_source = ((sources.get(("document", selected["id"])) or sources.get(selected["path"]))
                                   if selected else None)
                if selected_source:
                    selected_ids = [document["id"] for document in paper_documents
                                    if document.get("logical_paper_id") == selected_source["paper_id"]
                                    and document["project_id"] == selected["project_id"]]
            document_index = {document["id"]: document for document in paper_documents}
            managed_originals = set()
            for document in paper_documents:
                metadata = json.loads(document["metadata"])
                if metadata.get("managed_cloud_id") and metadata.get("primary_path") and document.get("logical_paper_id"):
                    managed_originals.add((document["project_id"], document["logical_paper_id"],
                                           canonical(metadata["primary_path"])))
            # Only a verified managed tablet source establishes a shared page
            # target. Independent legacy copies keep their separate page keys.
            combined_original_ids = {document["id"] for document in paper_documents
                if document.get("paper_source", {}).get("metadata", {}).get("role") != "tablet_render"
                and (document["project_id"], document.get("logical_paper_id"), canonical(document["path"]))
                    in managed_originals}
            # Let SQLite discard large base64 images before copying JSON into Python.
            payload = "e.payload" if include_images else "json_remove(e.payload, '$.image') AS payload"
            sql = f"SELECT {payload},d.path,d.id AS document_id,d.metadata,d.missing,d.digest AS source_digest,d.note_revision AS source_note_revision,c.payload AS citation FROM entries e JOIN documents d ON d.id=e.document_id LEFT JOIN paper_citations c ON c.document_id=d.id WHERE d.hidden=0"
            args = []
            if project_id:
                sql += " AND d.project_id=?"
                args.append(project_id)
            if document_id:
                if selected_ids:
                    sql += " AND d.id IN (" + ",".join("?" for _ in selected_ids) + ")"
                    args.extend(selected_ids)
                else:
                    sql += " AND d.id=?"
                    args.append(document_id)
            results, metadata = [], {}
            for row in db.execute(sql, args):
                if cancelled and cancelled():
                    raise InterruptedError("Note query cancelled")
                e = expand_image(db, json.loads(row["payload"]), include_images)
                doc_id = row["document_id"]
                if doc_id not in metadata:
                    metadata[doc_id] = json.loads(row["metadata"])
                    if row["citation"]:
                        citation = json.loads(row["citation"])
                        metadata[doc_id].update({key: value for key, value in citation.items() if key != "pages"})
                        metadata[doc_id]["citation_pages"] = citation.get("pages", "")
                        metadata[doc_id]["author"] = "; ".join(citation.get("authors", []))
                e.update(path=row["path"], document_id=doc_id, metadata=metadata[doc_id], missing=row["missing"],
                         source_digest=row["source_digest"], source_note_revision=row["source_note_revision"])
                if metadata[doc_id].get("managed_cloud_id"):
                    primary = metadata[doc_id].get("primary_path", "")
                    e.update(source_path=row["path"], path=primary or row["path"],
                             combined_view_path=primary or row["path"], combined_view_page=e["page"],
                             source_link_path=primary, source_link_page=e["page"])
                elif doc_id in combined_original_ids:
                    e.update(source_link_path=row["path"], source_link_page=e["page"])
                document = document_index.get(doc_id, {})
                if document.get("logical_paper_id"):
                    e.update(logical_paper_id=document["logical_paper_id"],
                             logical_paper_title=metadata[doc_id].get("title") if row["citation"] else paper_titles.get((document["project_id"], document["logical_paper_id"]), ""))
                results.append(e)
            omitted = inherited_duplicate_ids(results, paper_documents) if paper_documents else set()
            results = [entry for entry in results if (entry["document_id"], entry["key"]) not in omitted
                       and (not filter_entries or matches_search(entry, query, SEARCH_FIELDS if fields is None else fields,
                                                                 match_mode, note_type, matcher))]
            if cancelled and cancelled():
                raise InterruptedError("Note query cancelled")
            return sorted(results, key=entry_sort_key)

    def entry_image(self, document_id, key, *, expected_digest=None, expected_note_revision=None):
        """Fetch only the image currently needed for the note preview or export."""
        with self.connect() as db:
            row = db.execute("""SELECT json_extract(e.payload, '$.image') AS image,
                json_extract(e.payload, '$.image_ref') AS image_ref,d.digest,d.note_revision
                FROM entries e JOIN documents d ON d.id=e.document_id
                WHERE e.document_id=? AND e.key=? AND d.hidden=0""", (document_id, key)).fetchone()
            value = (stored_image(db, row["image_ref"]) if row["image_ref"] else row["image"]) if row else ""
        guarded = expected_digest is not None or expected_note_revision is not None
        if (guarded and row is None or row is not None and (
                expected_digest is not None and row["digest"] != expected_digest or
                expected_note_revision is not None and row["note_revision"] != expected_note_revision)):
            raise ValueError("Saved notes changed while loading this image. Refresh Notes and try again.")
        return value or ""

    def history(self, document_id, include_images=True):
        with self.connect() as db:
            payload = "payload" if include_images else "json_remove(payload,'$.image') AS payload"
            rows = [dict(r) for r in db.execute(f"SELECT id,document_id,key,action,at,{payload} FROM history WHERE document_id=? ORDER BY id DESC", (document_id,))]
            for row in rows:
                if '"image_ref"' in row["payload"]:
                    row["payload"] = json.dumps(expand_image(db, json.loads(row["payload"]), include_images), sort_keys=True, ensure_ascii=False)
            return rows

    def import_legacy(self, paths):
        with self.connect() as db:
            if db.execute("SELECT 1 FROM preferences WHERE key='legacy_imported'").fetchone():
                return
        for path in paths:
            if not Path(path).is_file():
                continue
            try:
                with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as legacy:
                    rows = legacy.execute("SELECT root_path FROM watched_folders").fetchall()
                for (root,) in rows:
                    self.add_project(root)
            except sqlite3.Error:
                import logging
                logging.exception("Cannot import legacy settings from %s", path)
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO preferences VALUES('legacy_imported','1')")
