"""Conservative trigram candidates; the existing query parser remains authoritative."""
from .search import normalize, tokenize


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS pdf_index_versions (
            document_id TEXT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
            digest TEXT NOT NULL, version TEXT NOT NULL, page_count INTEGER NOT NULL, has_text INTEGER NOT NULL,
            first_rowid INTEGER NOT NULL DEFAULT 0, last_rowid INTEGER NOT NULL DEFAULT 0);
        CREATE VIRTUAL TABLE IF NOT EXISTS pdf_text_index USING fts5(
            document_id UNINDEXED, page UNINDEXED, label UNINDEXED, text UNINDEXED, normalized,
            tokenize='trigram');
        CREATE TRIGGER IF NOT EXISTS delete_pdf_index AFTER DELETE ON documents BEGIN
            DELETE FROM pdf_text_index WHERE document_id=old.id;
        END;
    """)
    columns = {row[1] for row in db.execute("PRAGMA table_info(pdf_index_versions)")}
    for name in ("first_rowid", "last_rowid"):
        if name not in columns:
            db.execute(f"ALTER TABLE pdf_index_versions ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")


def candidates(query, mode):
    if not query.strip():
        return None
    tokens = [("TERM", normalize(query))] if mode == "Exact phrase" else tokenize(query)
    if any(kind == "NOT" for kind, _ in tokens):
        return None  # Negative-only branches can match pages containing no terms.
    terms = [word for kind, word in tokens if kind == "TERM"]
    if not terms or any(len(term) < 3 for term in terms):
        return None
    # OR is deliberately broader than AND; final matching preserves separate
    # fields, phrase boundaries and the application's substring semantics.
    return " OR ".join('normalized : "' + term.replace('"', '""') + '"' for term in terms)


def write(db, document_id, digest, version, pages):
    previous = db.execute("SELECT first_rowid,last_rowid FROM pdf_index_versions WHERE document_id=?", (document_id,)).fetchone()
    if previous:
        if previous[0] and previous[1]:
            db.execute("DELETE FROM pdf_text_index WHERE rowid BETWEEN ? AND ? AND document_id=?", (*previous, document_id))
        else:
            db.execute("DELETE FROM pdf_text_index WHERE document_id=?", (document_id,))
    first = last = 0
    for page in pages:
        last = db.execute("INSERT INTO pdf_text_index VALUES(?,?,?,?,?)",
                          (document_id, page["page"], page.get("pdf_label", ""), page["text"], normalize(page["text"]))).lastrowid
        if not first:
            first = last
    db.execute("INSERT OR REPLACE INTO pdf_index_versions VALUES(?,?,?,?,?,?,?)",
               (document_id, digest, version, len(pages), int(any(page["text"].strip() for page in pages)), first or 0, last or 0))


def read(store, document_id, digest, version, query, mode):
    with store.connect() as db:
        info = db.execute("SELECT page_count,has_text,first_rowid,last_rowid FROM pdf_index_versions WHERE document_id=? AND digest=? AND version=?",
                          (document_id, digest, version)).fetchone()
        if info is None:
            return None
        sql = "SELECT COUNT(*), COUNT(DISTINCT page), MAX(CASE WHEN trim(text) <> '' THEN 1 ELSE 0 END) FROM pdf_text_index WHERE document_id=?"
        params = [document_id]
        if info[2] and info[3]:
            sql += " AND rowid BETWEEN ? AND ?"
            params.extend([info[2], info[3]])
        count, distinct_pages, has_text = db.execute(sql, params).fetchone()
        if count != info[0] or distinct_pages != info[0] or bool(has_text) != bool(info[1]):
            return None
        term = candidates(query, mode)
        sql = "SELECT page,label,text FROM pdf_text_index WHERE document_id=?"
        params = [document_id]
        if info[2] and info[3]:
            sql += " AND rowid BETWEEN ? AND ?"
            params.extend([info[2], info[3]])
        if term:
            sql += " AND pdf_text_index MATCH ?"
            params.append(term)
        sql += " ORDER BY CAST(page AS INTEGER)"
        pages = [{"page": int(row[0]), "pdf_label": row[1], "text": row[2]} for row in db.execute(sql, params)]
        return pages, info[0], bool(info[1])
