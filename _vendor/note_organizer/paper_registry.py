"""Additive paper identities; links never replace PDFs, notes, or scan history.

Constructing ``Registry`` is an explicit write operation. Importing this module
and running the reconciliation scanner do not create or migrate tables.
"""
import hashlib
import json
from pathlib import Path
import uuid

from .storage import canonical, digest_file, timestamp


def annotation_identity(source_id, key):
    """Keep independently annotated variants in separate annotation namespaces."""
    return "source-" + hashlib.sha256(
        json.dumps([str(source_id), str(key)], ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def ensure_document_sources(db):
    """Keep document/source relationships stable when their locator moves."""
    db.execute("""CREATE TABLE IF NOT EXISTS paper_document_sources (
        document_id TEXT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
        source_id TEXT NOT NULL REFERENCES paper_sources(id))""")
    db.execute("""INSERT OR IGNORE INTO paper_document_sources
        SELECT d.id,s.id FROM documents d JOIN paper_sources s ON s.kind='local' AND s.locator=d.path""")


def _mark_papers_dirty(db, paper_ids):
    placeholders = ",".join("?" for _ in paper_ids)
    db.execute("""UPDATE projects SET dirty=1 WHERE id IN (
        SELECT d.project_id FROM documents d JOIN paper_sources s ON s.kind='local' AND s.locator=d.path
        WHERE s.paper_id IN (""" + placeholders + """) AND d.hidden=0
        UNION SELECT d.project_id FROM documents d JOIN paper_document_sources m ON m.document_id=d.id
        JOIN paper_sources s ON s.id=m.source_id WHERE s.paper_id IN (""" + placeholders + ") AND d.hidden=0)",
               (*paper_ids, *paper_ids))


class Registry:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS paper_registry (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS paper_sources (
                    id TEXT PRIMARY KEY,
                    paper_id TEXT NOT NULL REFERENCES paper_registry(id),
                    kind TEXT NOT NULL, locator TEXT NOT NULL,
                    digest TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '',
                    metadata TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL,
                    UNIQUE(kind,locator));
                CREATE TABLE IF NOT EXISTS paper_source_revisions (
                    id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL REFERENCES paper_sources(id),
                    digest TEXT NOT NULL, metadata TEXT NOT NULL, first_seen TEXT NOT NULL,
                    UNIQUE(source_id,digest));
                CREATE TABLE IF NOT EXISTS paper_source_projects (
                    source_id TEXT NOT NULL REFERENCES paper_sources(id),
                    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    PRIMARY KEY(source_id,project_id));
                CREATE TABLE IF NOT EXISTS paper_link_history (
                    id INTEGER PRIMARY KEY, source_id TEXT NOT NULL,
                    previous_paper_id TEXT NOT NULL, paper_id TEXT NOT NULL,
                    at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS paper_link_operations (
                    id INTEGER PRIMARY KEY, paper_id TEXT NOT NULL,
                    before_relationships TEXT NOT NULL, after_relationships TEXT NOT NULL,
                    history_cutoff INTEGER NOT NULL, at TEXT NOT NULL,
                    undone_at TEXT NOT NULL DEFAULT '');
                CREATE INDEX IF NOT EXISTS paper_sources_by_paper ON paper_sources(paper_id);
            """)
            ensure_document_sources(db)
            columns = {row[1] for row in db.execute("PRAGMA table_info(paper_link_operations)")}
            for name in ("citation_before", "citation_after"):
                if name not in columns:
                    db.execute(f"ALTER TABLE paper_link_operations ADD COLUMN {name} TEXT NOT NULL DEFAULT '{{}}'")

    @staticmethod
    def _decode(row):
        if row is None:
            return None
        result = dict(row)
        result["metadata"] = json.loads(result["metadata"])
        if result["kind"] == "local":
            result["path"] = result["locator"]
        elif result["kind"] == "cloud":
            result["cloud_id"] = result["locator"]
        return result

    def _register(self, kind, locator, digest, title, metadata, project_id=None, paper_id=None):
        if not locator:
            raise ValueError("A source path or cloud document ID is required.")
        metadata = json.dumps(metadata or {}, sort_keys=True, ensure_ascii=False)
        at = timestamp()
        with self.store.connect() as db:
            old = db.execute("SELECT * FROM paper_sources WHERE kind=? AND locator=?",
                             (kind, locator)).fetchone()
            if old:
                source_id, current_paper = old["id"], old["paper_id"]
                if paper_id and current_paper != paper_id:
                    raise ValueError("This source already belongs to another paper. Confirm a paper link first.")
            else:
                source_id, current_paper = str(uuid.uuid4()), paper_id or str(uuid.uuid4())
                if paper_id:
                    if not db.execute("SELECT 1 FROM paper_registry WHERE id=?", (paper_id,)).fetchone():
                        raise ValueError("The selected paper identity does not exist.")
                else:
                    db.execute("INSERT INTO paper_registry VALUES(?,?,?)", (current_paper, title, at))
            if paper_id:
                from .citations import reviewed_records, CitationConflict
                linked_ids = {row[0] for row in db.execute("SELECT m.document_id FROM paper_document_sources m JOIN paper_sources s ON s.id=m.source_id WHERE s.paper_id=?", (paper_id,))}
                new_ids = {row[0] for row in db.execute("SELECT id FROM documents WHERE path=?", (locator,))} if kind == "local" else set()
                records = reviewed_records(db, linked_ids | new_ids)
                if len(records) > 1:
                    raise CitationConflict(records)
                if records:
                    db.executemany("INSERT OR REPLACE INTO paper_citations VALUES(?,?)", [(document_id, json.dumps(records[0], ensure_ascii=False)) for document_id in new_ids])
            # Empty values do not erase known source information on a title-only refresh.
            digest = digest or (old["digest"] if old else "")
            title = title or (old["title"] if old else "")
            if old and metadata == "{}":
                metadata = old["metadata"]
            db.execute("""INSERT INTO paper_sources VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET digest=excluded.digest,title=excluded.title,
                metadata=excluded.metadata,updated_at=excluded.updated_at""",
                (source_id, current_paper, kind, locator, digest, title, metadata, at))
            if digest:
                db.execute("INSERT OR IGNORE INTO paper_source_revisions VALUES(?,?,?,?,?)",
                           (str(uuid.uuid4()), source_id, digest, metadata, at))
            if project_id is not None:
                db.execute("INSERT OR IGNORE INTO paper_source_projects VALUES(?,?)", (source_id, project_id))
            if kind == "local":
                db.execute("""INSERT OR IGNORE INTO paper_document_sources
                    SELECT id,? FROM documents WHERE path=?""", (source_id, locator))
            if paper_id:
                from .supplement_library import check_merge
                check_merge(db, db.execute("SELECT * FROM paper_sources WHERE paper_id=?", (current_paper,)).fetchall())
            if not old or any(old[key] != value for key, value in (("digest", digest), ("title", title), ("metadata", metadata))):
                # A generated variant can be registered after its first scan.
                # Identity/provenance changes therefore need an export even
                # when the saved annotation payloads have not changed.
                _mark_papers_dirty(db, (current_paper,))
            return self._decode(db.execute("SELECT * FROM paper_sources WHERE id=?", (source_id,)).fetchone())

    def register_local(self, path, digest=None, project_id=None, title="", metadata=None, paper_id=None):
        """Register an individual source; identical independent copies stay distinct."""
        return self._register("local", canonical(path), digest_file(path) if digest is None else digest,
                              title or Path(path).stem, metadata, project_id, paper_id)

    def link_local(self, paper_id, path, digest=None, project_id=None, title="", metadata=None):
        """Attach a generated tablet variant to its explicitly known source paper."""
        return self.register_local(path, digest, project_id, title, metadata, paper_id)

    def register_cloud(self, cloud_id, digest="", title="", metadata=None):
        """Cloud UUID is stable through renames and folder moves."""
        return self._register("cloud", str(cloud_id), digest, title, metadata)

    def link_cloud(self, paper_id, cloud_id, digest="", title="", metadata=None):
        """Attach a newly uploaded document, retaining an existing link if present."""
        return self._register("cloud", str(cloud_id), digest, title, metadata, paper_id=paper_id)

    def source(self, source_id):
        with self.store.connect() as db:
            return self._decode(db.execute("SELECT * FROM paper_sources WHERE id=?", (source_id,)).fetchone())

    def sources(self, paper_id=None):
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM paper_sources" + (" WHERE paper_id=?" if paper_id else "")
                              + " ORDER BY kind,locator", (paper_id,) if paper_id else ())
            return [self._decode(row) for row in rows]

    def paper_for_cloud(self, cloud_id):
        with self.store.connect() as db:
            row = db.execute("SELECT paper_id FROM paper_sources WHERE kind='cloud' AND locator=?",
                             (str(cloud_id),)).fetchone()
            return row[0] if row else None

    def local_for_cloud(self, cloud_id):
        paper_id = self.paper_for_cloud(cloud_id)
        return [row for row in self.sources(paper_id) if row["kind"] == "local"] if paper_id else []

    def projects(self, source_id):
        with self.store.connect() as db:
            return [row[0] for row in db.execute(
                "SELECT project_id FROM paper_source_projects WHERE source_id=? ORDER BY project_id", (source_id,))]

    def revisions(self, source_id):
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM paper_source_revisions WHERE source_id=? ORDER BY first_seen,id",
                              (source_id,))
            return [{**dict(row), "metadata": json.loads(row["metadata"])} for row in rows]

    def relocate_local(self, source_id, new_path):
        """An explicit rename updates the locator, retaining identity and revisions."""
        with self.store.connect() as db:
            old = db.execute("SELECT * FROM paper_sources WHERE id=?", (source_id,)).fetchone()
            if not old or old["kind"] != "local":
                raise ValueError("Choose an existing local source.")
            locator = canonical(new_path)
            occupied = db.execute("SELECT id FROM paper_sources WHERE kind='local' AND locator=?", (locator,)).fetchone()
            if occupied and occupied[0] != source_id:
                raise ValueError("The new path already belongs to another source.")
            db.execute("UPDATE paper_sources SET locator=?,updated_at=? WHERE id=?", (locator, timestamp(), source_id))
            from .supplement_library import move_paths
            move_paths(db, old["locator"], locator)
        return self.source(source_id)

    def link_sources(self, source_ids, paper_id=None, *, citation=None):
        """Confirm shared paper identities, retaining all already-linked variants.

        Selecting sources confirms their entire existing paper relationship,
        including unselected original PDFs, tablet renders and cloud IDs. Those
        variants must stay together after the confirmation; note stores remain
        separate and every reassignment is recorded in link history.
        """
        source_ids = list(dict.fromkeys(str(value) for value in source_ids))
        if len(source_ids) < 2:
            raise ValueError("Choose at least two sources to link.")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = [db.execute("SELECT * FROM paper_sources WHERE id=?", (source_id,)).fetchone()
                    for source_id in source_ids]
            if any(row is None for row in rows):
                raise ValueError("A selected source no longer exists.")
            paper_id = paper_id or rows[0]["paper_id"]
            if not db.execute("SELECT 1 FROM paper_registry WHERE id=?", (paper_id,)).fetchone():
                raise ValueError("The selected paper identity does not exist.")
            at = timestamp()
            changed = False
            affected_papers = {paper_id, *(row["paper_id"] for row in rows)}
            placeholders = ",".join("?" for _ in affected_papers)
            related_sources = db.execute("SELECT * FROM paper_sources WHERE paper_id IN (" + placeholders + ")",
                                         tuple(affected_papers)).fetchall()
            from .supplement_library import check_merge
            check_merge(db, related_sources)
            from .citations import reviewed_records, CitationConflict, normalize_record
            document_ids = [row[0] for row in db.execute(
                "SELECT document_id FROM paper_document_sources WHERE source_id IN (" +
                ",".join("?" for _ in related_sources) + ")", [row["id"] for row in related_sources])]
            records = reviewed_records(db, document_ids)
            if len(records) > 1 and citation is None:
                raise CitationConflict(records)
            canonical_citation = normalize_record(citation) if citation is not None else records[0] if records else None
            if canonical_citation is not None and not canonical_citation["title"]:
                raise ValueError("Review a paper title before choosing its canonical citation.")
            citation_before = {doc: (row[0] if (row := db.execute("SELECT payload FROM paper_citations WHERE document_id=?", (doc,)).fetchone()) else None) for doc in document_ids}
            citation_after = json.dumps(canonical_citation, ensure_ascii=False) if canonical_citation else None
            if citation_after:
                db.executemany("INSERT OR REPLACE INTO paper_citations VALUES(?,?)", [(doc, citation_after) for doc in document_ids])
                from .supplement_library import index_doi
                for doc in document_ids:
                    index_doi(db, doc, canonical_citation)
            before = {row["id"]: row["paper_id"] for row in related_sources}
            for row in related_sources:
                if row["paper_id"] != paper_id:
                    changed = True
                    db.execute("INSERT INTO paper_link_history(source_id,previous_paper_id,paper_id,at) VALUES(?,?,?,?)",
                               (row["id"], row["paper_id"], paper_id, at))
                    db.execute("UPDATE paper_sources SET paper_id=?,updated_at=? WHERE id=?", (paper_id, at, row["id"]))
            if changed:
                after = {source_id: paper_id for source_id in before}
                cutoff = db.execute("SELECT COALESCE(MAX(id),0) FROM paper_link_history").fetchone()[0]
                db.execute("""INSERT INTO paper_link_operations(paper_id,before_relationships,
                    after_relationships,history_cutoff,at) VALUES(?,?,?,?,?)""",
                           (paper_id, json.dumps(before, sort_keys=True), json.dumps(after, sort_keys=True), cutoff, at))
                db.execute("UPDATE paper_link_operations SET citation_before=?,citation_after=? WHERE id=last_insert_rowid()",
                           (json.dumps(citation_before), json.dumps(citation_after)))
                # Linking changes notebook grouping and inherited-note counts,
                # even when none of the underlying annotation payloads changed.
                _mark_papers_dirty(db, tuple(affected_papers))
        return paper_id

    @staticmethod
    def _link_operation(row):
        if row is None or row["undone_at"]:
            return None
        result = dict(row)
        for key in ("before_relationships", "after_relationships"):
            result[key] = json.loads(result[key])
        result["source_count"] = len(result["before_relationships"])
        result["changed_source_count"] = sum(paper_id != result["paper_id"]
                                             for paper_id in result["before_relationships"].values())
        return result

    def latest_link_operation(self):
        """Return the most recent new-style link, unless it was already undone.

        Legacy history does not contain a complete pre-link relationship and
        cannot safely serve as an undo snapshot.
        """
        with self.store.connect() as db:
            return self._link_operation(db.execute("SELECT * FROM paper_link_operations ORDER BY id DESC LIMIT 1").fetchone())

    def undo_latest_link(self, operation_id=None):
        """Restore a persisted relationship snapshot; all note stores stay intact.

        Any later membership change in an affected paper blocks undo rather
        than silently detaching a newly linked variant.
        """
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            operation = self._link_operation(db.execute("SELECT * FROM paper_link_operations ORDER BY id DESC LIMIT 1").fetchone())
            if operation is None:
                raise ValueError("There is no recent paper link available to undo.")
            if operation_id is not None and int(operation_id) != operation["id"]:
                raise ValueError("That paper link is no longer the latest operation. Refresh the library first.")
            before, after = operation["before_relationships"], operation["after_relationships"]
            affected_papers = {operation["paper_id"], *before.values()}
            placeholders = ",".join("?" for _ in affected_papers)
            current = {row["id"]: row["paper_id"] for row in db.execute(
                "SELECT id,paper_id FROM paper_sources WHERE paper_id IN (" + placeholders + ")", tuple(affected_papers))}
            changed_history = any(row["source_id"] in after or row["previous_paper_id"] in affected_papers
                                  or row["paper_id"] in affected_papers for row in db.execute(
                                      "SELECT * FROM paper_link_history WHERE id>?", (operation["history_cutoff"],)))
            if current != after or changed_history:
                raise ValueError("Paper identities changed after this link. Undo was stopped; all sources and notes were retained.")
            at = timestamp()
            for source_id, previous_paper in before.items():
                if previous_paper != after[source_id]:
                    db.execute("INSERT INTO paper_link_history(source_id,previous_paper_id,paper_id,at) VALUES(?,?,?,?)",
                               (source_id, after[source_id], previous_paper, at))
                    db.execute("UPDATE paper_sources SET paper_id=?,updated_at=? WHERE id=?", (previous_paper, at, source_id))
            db.execute("UPDATE paper_link_operations SET undone_at=? WHERE id=?", (at, operation["id"]))
            citation_after = json.loads(operation["citation_after"])
            for doc, payload in json.loads(operation["citation_before"]).items():
                current_citation = db.execute("SELECT payload FROM paper_citations WHERE document_id=?", (doc,)).fetchone()
                # Edits made after linking remain available on both restored papers.
                if current_citation and current_citation[0] == citation_after:
                    db.execute("DELETE FROM paper_citations WHERE document_id=?", (doc,))
                    if payload is not None:
                        db.execute("INSERT INTO paper_citations VALUES(?,?)", (doc, payload))
                    from .supplement_library import index_doi
                    index_doi(db, doc)
            _mark_papers_dirty(db, tuple(affected_papers))
            return {**operation, "undone_at": at, "restored_paper_ids": sorted(affected_papers)}


def link_registry_sources(store, source_ids, paper_id=None, *, citation=None):
    """Entry point for a user-confirmed link, never a scanner side effect."""
    return Registry(store).link_sources(source_ids, paper_id, citation=citation)
