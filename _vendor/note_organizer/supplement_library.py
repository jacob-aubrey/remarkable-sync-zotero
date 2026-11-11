"""Local SI relationships, shared naming, and durable filing integration.

An attachment is an edge between documents, never a shared paper identity.
Filename similarity produces review candidates only; automatic association
requires an explicit parent DOI and one unambiguous library paper.
"""
import json
import os
from pathlib import Path

from .storage import canonical, digest_file
from .sorting.metadata_lookup import normalize_doi

FILE_TYPES = {".pdf", ".zip", ".docx", ".xlsx", ".ppt", ".pptx", ".csv", ".tsv", ".txt", ".json", ".sdf", ".mol"}


def parent_details(record):
    authors = record.get("authors") or record.get("creators") or []
    if isinstance(authors, str):
        authors = authors.split(";")
    doi = normalize_doi(record.get("doi") or (record.get("identifier") or {}).get("doi"))
    return {**record, "creators": authors, "primary_creator": authors[0] if authors else "",
            "document_type": "journal_article", "is_multiple_creators": len(authors) > 1,
            "is_supplementary_material": True, "supplemental_status": "confirmed",
            "supplemental_parent_doi": doi, "identifier": {"doi": doi},
            "parent_metadata_verified": True}


def naming_details(store, document_id):
    from .citations import citation_for
    details = parent_details(citation_for(store, document_id))
    documents = store.search_documents(document_id=document_id, whole_paper=True)
    with store.connect() as db:
        has_jobs = db.execute("SELECT 1 FROM sqlite_master WHERE name='inbox_jobs'").fetchone()
        for document in documents:
            candidates = [json.loads(document["metadata"])]
            if has_jobs:
                job = db.execute("SELECT details FROM inbox_jobs WHERE path=? AND state IN ('confirmed','handed_off') ORDER BY created_at DESC LIMIT 1", (document["path"],)).fetchone()
                if job:
                    candidates.insert(0, json.loads(job[0]))
            for candidate in candidates:
                # Keep a reviewed abbreviation only while it describes the
                # current journal; never invent one or reuse it after an edit.
                if candidate.get("journal_abbreviation") and str(candidate.get("journal", "")).casefold() == details.get("journal", "").casefold():
                    details["journal_abbreviation"] = candidate["journal_abbreviation"]
                    return details
    return details


def proposed_name(store, document_id, suffix):
    from .sorting.core_logic import build_proposed_filename
    if suffix.lower() not in FILE_TYPES:
        raise ValueError("Choose a supported supplementary file extension.")
    config = store.preference("inbox", {})
    details = naming_details(store, document_id)
    # Data/archives share the formatter without expanding the sorter's inputs.
    name = build_proposed_filename(details, ".pdf", filename_format=config.get("filename_format", "smart"),
                                   custom_template=config.get("custom_filename_template", ""))
    stem, maximum = Path(name).stem, 220 - len(suffix)
    if len(stem) > maximum:
        stem = stem[:-3][:maximum - 3].rstrip(" ._") + "_SI"
    return stem + suffix.lower()


def move_paths(db, old_path, new_path):
    old, new = canonical(old_path), canonical(new_path)
    if old == new:
        return
    previous = db.execute("SELECT digest FROM supplement_files WHERE path=?", (old,)).fetchone()
    occupied = db.execute("SELECT digest FROM supplement_files WHERE path=?", (new,)).fetchone()
    if previous and occupied and previous[0] != occupied[0]:
        raise ValueError("The new location belongs to a different supplementary file. Existing relationships were retained.")
    # Previous releases retained the supplied path's letter case.
    for row in db.execute("SELECT document_id,path,url FROM paper_attachments").fetchall():
        if row["path"] != old and canonical(row["path"]) == old:
            db.execute("INSERT OR IGNORE INTO paper_attachments VALUES(?,?,?)", (row["document_id"], old, row["url"]))
            db.execute("DELETE FROM paper_attachments WHERE document_id=? AND path=?", (row["document_id"], row["path"]))
    db.execute("""INSERT INTO paper_attachments(document_id,path,url)
        SELECT document_id,?,url FROM paper_attachments WHERE path=?
        ON CONFLICT(document_id,path) DO UPDATE SET url=CASE WHEN excluded.url!='' THEN excluded.url ELSE paper_attachments.url END""", (new, old))
    db.execute("DELETE FROM paper_attachments WHERE path=?", (old,))
    row = db.execute("SELECT * FROM supplement_files WHERE path=?", (old,)).fetchone()
    if row:
        db.execute("INSERT OR REPLACE INTO supplement_files VALUES(?,?,?)", (new, row["digest"], row["details"]))
        db.execute("DELETE FROM supplement_files WHERE path=?", (old,))


def remember_file(db, path, digest, details):
    db.execute("INSERT OR REPLACE INTO supplement_files VALUES(?,?,?)",
               (canonical(path), digest, json.dumps(details, ensure_ascii=False)))


def source_details(store, path):
    """Reuse reviewed parent metadata only for an unchanged attached file."""
    with store.connect() as db:
        row = db.execute("SELECT * FROM supplement_files WHERE path=?", (canonical(path),)).fetchone()
        attached = [item for item in db.execute("SELECT document_id,path FROM paper_attachments") if canonical(item["path"]) == canonical(path)]
    if not attached or (row and row["digest"] != digest_file(path)):
        return None
    groups = {frozenset(item["id"] for item in store.search_documents(document_id=attachment["document_id"], whole_paper=True))
              for attachment in attached}
    if len(groups) != 1:
        return None
    return naming_details(store, attached[0]["document_id"])


def attach_file(store, document_id, path, url=""):
    path = Path(path).resolve()
    if not path.is_file() or path.suffix.lower() not in FILE_TYPES:
        raise ValueError("Choose an existing supported supplementary file.")
    documents = store.search_documents(document_id=document_id, whole_paper=True)
    if not documents:
        raise ValueError("This paper is no longer in the library.")
    digest = digest_file(path)
    if any(canonical(row["path"]) == canonical(path) or row["digest"] == digest for row in documents):
        raise ValueError("The article or one of its linked copies cannot be its own supplementary file.")
    details = naming_details(store, document_id)
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='paper_sources'").fetchone():
            child = db.execute("SELECT paper_id FROM paper_sources WHERE kind='local' AND locator=?", (canonical(path),)).fetchone()
            if child and any(db.execute("SELECT 1 FROM paper_sources WHERE kind='local' AND locator=? AND paper_id=?", (canonical(row["path"]), child[0])).fetchone() for row in documents):
                raise ValueError("This file already shares the article's paper identity. Undo that paper link before attaching it as SI.")
        # Reject cycles even if the PDFs have not been registered yet.
        edges = {}
        for row in db.execute("SELECT d.path AS parent,a.path AS child FROM paper_attachments a JOIN documents d ON d.id=a.document_id"):
            edges.setdefault(canonical(row["parent"]), set()).add(canonical(row["child"]))
        parents, pending, visited = {canonical(row["path"]) for row in documents}, [canonical(path)], set()
        while pending:
            child = pending.pop()
            if child in parents:
                raise ValueError("This attachment would create a circular supplementary relationship.")
            if child not in visited:
                visited.add(child)
                pending.extend(edges.get(child, ()))
        db.executemany("""INSERT INTO paper_attachments VALUES(?,?,?) ON CONFLICT(document_id,path)
            DO UPDATE SET url=CASE WHEN excluded.url!='' THEN excluded.url ELSE paper_attachments.url END""",
                       [(row["id"], canonical(path), url) for row in documents])
        remember_file(db, path, digest, details)
    return path


def attachments(store, document_id):
    result = {}
    with store.connect() as db:
        for document in store.search_documents(document_id=document_id, whole_paper=True):
            for row in db.execute("SELECT path,url FROM paper_attachments WHERE document_id=?", (document["id"],)):
                result[canonical(row["path"])] = {"path": str(Path(row["path"]).resolve()), "url": row["url"]}
    # Upgrade old, manually reviewed attachments lazily, outside schema setup.
    missing = []
    with store.connect() as db:
        for path in result:
            if not db.execute("SELECT 1 FROM supplement_files WHERE path=?", (path,)).fetchone() and Path(path).is_file():
                missing.append(path)
    if missing:
        from .citations import citation_for
        details = parent_details(citation_for(store, document_id))
        for path in missing:
            try:
                digest = digest_file(path)
            except OSError:
                continue
            with store.connect() as db:
                remember_file(db, path, digest, details)
    return list(result.values())


def migrate_filings(db):
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='inbox_jobs'").fetchone():
        for row in db.execute("SELECT path,digest,details FROM inbox_jobs WHERE state IN ('confirmed','handed_off') AND digest!=''").fetchall():
            if json.loads(row["details"]).get("is_supplementary_material"):
                db.execute("INSERT OR IGNORE INTO supplement_files VALUES(?,?,?)", (canonical(row["path"]), row["digest"], row["details"]))
    # Existing pending relationships are verified outside schema transactions.


def index_doi(db, document_id, metadata=None):
    """Maintain a small normalized parent lookup independently of note payloads."""
    row = db.execute("SELECT d.path,d.metadata,c.payload FROM documents d LEFT JOIN paper_citations c ON c.document_id=d.id WHERE d.id=?", (document_id,)).fetchone()
    if row is None:
        return
    record = json.loads(row[2] or row[1]) if metadata is None else metadata
    doi = normalize_doi(record.get("doi") or (record.get("identifier") or {}).get("doi"))
    if not doi and db.execute("SELECT 1 FROM sqlite_master WHERE name='inbox_jobs'").fetchone():
        job = db.execute("SELECT details FROM inbox_jobs WHERE path=? AND state IN ('confirmed','handed_off') ORDER BY created_at DESC LIMIT 1", (row[0],)).fetchone()
        if job:
            record = json.loads(job[0])
            doi = normalize_doi(record.get("doi") or (record.get("identifier") or {}).get("doi"))
    db.execute("INSERT OR REPLACE INTO document_dois VALUES(?,?)", (document_id, doi))


def bind_pending(db, verified=None):
    """Retry after scans/citation edits: a parent can arrive after its SI."""
    files = db.execute("SELECT * FROM supplement_files").fetchall()
    if not files:
        return
    has_registry = db.execute("SELECT 1 FROM sqlite_master WHERE name='paper_document_sources'").fetchone()
    identities = dict(db.execute("SELECT ds.document_id,s.paper_id FROM paper_document_sources ds JOIN paper_sources s ON s.id=ds.source_id")) if has_registry else {}
    si_paths = {row["path"] for row in files}
    si_paths.update(row[0] for row in db.execute("SELECT DISTINCT path FROM paper_attachments"))
    parents = []
    dois = {normalize_doi(json.loads(item["details"]).get("supplemental_parent_doi")) for item in files}
    dois.discard("")
    if not dois:
        return
    for row in db.execute("SELECT d.*,c.payload AS citation FROM documents d JOIN document_dois di ON di.document_id=d.id LEFT JOIN paper_citations c ON c.document_id=d.id WHERE d.hidden=0 AND d.missing=0 AND di.doi IN (" + ",".join("?" for _ in dois) + ")", tuple(dois)):
        meta = json.loads(row["metadata"])
        if row["path"] in si_paths or meta.get("is_supplementary_material"):
            continue
        record = json.loads(row["citation"]) if row["citation"] else meta
        doi = normalize_doi(record.get("doi") or (record.get("identifier") or {}).get("doi"))
        if not doi and db.execute("SELECT 1 FROM sqlite_master WHERE name='inbox_jobs'").fetchone():
            job = db.execute("SELECT details FROM inbox_jobs WHERE path=? AND state IN ('confirmed','handed_off') ORDER BY created_at DESC LIMIT 1", (row["path"],)).fetchone()
            if job:
                reviewed = json.loads(job[0])
                if reviewed.get("is_supplementary_material"):
                    continue
                doi = normalize_doi(reviewed.get("doi") or (reviewed.get("identifier") or {}).get("doi"))
        parents.append((row["id"], identities.get(row["id"], row["path"]), doi, row["digest"]))
    for item in files:
        # A manual relationship is authoritative; do not attach to another edition.
        if db.execute("SELECT 1 FROM paper_attachments WHERE path=?", (item["path"],)).fetchone():
            continue
        details = json.loads(item["details"])
        doi = normalize_doi(details.get("supplemental_parent_doi"))
        matches = [(doc, group) for doc, group, parent_doi, digest in parents
                   if doi and doi == parent_doi and digest != item["digest"]]
        if len({group for _, group in matches}) == 1:
            if not verified or verified.get(item["path"]) != item["digest"]:
                continue
            if has_registry:
                source = db.execute("SELECT paper_id FROM paper_sources WHERE kind='local' AND locator=?", (item["path"],)).fetchone()
                if source and source[0] in {group for _, group in matches}:
                    continue
            db.executemany("INSERT OR IGNORE INTO paper_attachments VALUES(?,?,'')", [(doc, item["path"]) for doc, _ in matches])


def filed(db, source, target, digest, details):
    """Part of the Inbox confirmation transaction, including crash recovery."""
    move_paths(db, source, target)
    if details.get("is_supplementary_material"):
        remember_file(db, target, digest, details)
    for row in db.execute("SELECT id FROM documents WHERE path=?", (canonical(target),)).fetchall():
        index_doi(db, row[0], details)
    bind_pending(db, {canonical(target): digest})


def reconcile(store, scans=()):
    """One discovery per scan batch, with all filesystem work outside DB writes."""
    with store.connect() as db:
        files = [dict(row) for row in db.execute("SELECT * FROM supplement_files")]
        attached = {row[0] for row in db.execute("SELECT DISTINCT path FROM paper_attachments")}
        roots = [row[0] for row in db.execute("SELECT root FROM projects")]
    if not files:
        return
    moves, verified = [], {}
    missing_by_digest = {}
    for row in files:
        if not Path(row["path"]).exists():
            missing_by_digest.setdefault(row["digest"], []).append(row["path"])
        elif row["path"] not in attached:
            try:
                verified[row["path"]] = digest_file(row["path"])
            except OSError:
                pass
    # Build a digest catalog once, only when a missing SI matches a scanned PDF.
    targets = {digest for _, digest in scans if len(missing_by_digest.get(digest, [])) == 1}
    if targets:
        sizes = {Path(path).stat().st_size for path, digest in scans if digest in targets and Path(path).is_file()}
        catalog = {digest: set() for digest in targets}
        visited = set()
        errors = []
        for root in roots:
            try:
                if not Path(root).is_dir():
                    errors.append(root)
                for folder, dirs, names in os.walk(root, onerror=errors.append):
                    dirs[:] = [name for name in dirs if name not in {".git", "notes_media", "Notebook backups"}
                               and not Path(folder, name).is_symlink()
                               and not Path(folder, name).resolve().is_relative_to(store.path.parent.resolve())]
                    for name in names:
                        candidate = Path(folder, name)
                        key = canonical(candidate)
                        if candidate.suffix.lower() != ".pdf" or candidate.is_symlink() or key in visited:
                            continue
                        visited.add(key)
                        if candidate.stat().st_size not in sizes:
                            continue
                        digest = digest_file(candidate)
                        if digest in catalog:
                            catalog[digest].add(key)
            except OSError:
                errors.append(root)
        if not errors:
            for digest, matches in catalog.items():
                if len(matches) == 1:
                    target = next(iter(matches))
                    moves.append((missing_by_digest[digest][0], target, digest))
                    verified[target] = digest
    with store.connect() as db:
        for old, target, digest in moves:
            current = db.execute("SELECT digest FROM supplement_files WHERE path=?", (old,)).fetchone()
            if current and current[0] == digest:
                move_paths(db, old, target)
        bind_pending(db, verified)


def library_files(store, document_id, cancelled=lambda: False):
    documents = store.search_documents(document_id=document_id, whole_paper=True)
    roots = [Path(row["root"]) for row in store.projects()]
    roots.extend(Path(row["path"]).parent for row in documents)
    config = store.preference("inbox", {})
    if config.get("library_root"):
        roots.append(Path(config["library_root"]))
    roots = sorted({Path(canonical(root)) for root in roots}, key=lambda root: len(root.parts))
    unique_roots = []
    for root in roots:
        if not any(root.is_relative_to(parent) for parent in unique_roots):
            unique_roots.append(root)
    paths = {canonical(row["path"]) for row in attachments(store, document_id) if Path(row["path"]).is_file()}
    excluded = [store.path.parent / "remarkable"]
    remarkable = store.preference("remarkable", {})
    if remarkable.get("review_dir"):
        excluded.append(Path(remarkable["review_dir"]).resolve())
    for root in unique_roots:
        if any(root.is_relative_to(area) for area in excluded):
            continue
        for folder, directories, filenames in os.walk(root, followlinks=False):
            if cancelled():
                raise InterruptedError("Library search cancelled")
            directories[:] = [name for name in directories if name not in {".git", "notes_media", "Notebook backups"}
                              and not Path(folder, name).is_symlink()
                              and not any(Path(folder, name).resolve().is_relative_to(area) for area in excluded)]
            for name in filenames:
                path = Path(folder, name)
                if path.suffix.lower() in FILE_TYPES and not path.is_symlink():
                    paths.add(canonical(path))
    return sorted(paths)


def find_existing(store, document_id, cancelled=lambda: False):
    from .citations import citation_for
    record = citation_for(store, document_id)
    documents = store.search_documents(document_id=document_id, whole_paper=True)
    parent_paths = {canonical(row["path"]) for row in documents}
    expected = Path(proposed_name(store, document_id, ".pdf")).stem.casefold()
    stems = [Path(row["path"]).stem.casefold() + "_si" for row in documents]
    saved = {canonical(row["path"]): row for row in attachments(store, document_id)}
    with store.connect() as db:
        known = {row["path"]: dict(row) for row in db.execute("SELECT * FROM supplement_files")}
    paths = library_files(store, document_id, cancelled)
    # Repair non-PDF attachments too when searching a moved library.
    for old, row in list(known.items()):
        if old not in saved or Path(old).exists():
            continue
        matches = []
        for path in paths:
            if cancelled():
                raise InterruptedError("Library search cancelled")
            if path in parent_paths:
                continue
            try:
                if digest_file(path) == row["digest"]:
                    matches.append(path)
            except OSError:
                continue
        if len(matches) == 1:
            with store.connect() as db:
                move_paths(db, old, matches[0])
            known[matches[0]] = known.pop(old)
            saved[matches[0]] = saved.pop(old)
    result = []
    for path in paths:
        if path in parent_paths:
            continue
        stem = Path(path).stem.casefold()
        details = json.loads(known[path]["details"]) if path in known else {}
        parent_doi = normalize_doi(details.get("supplemental_parent_doi"))
        reason = ""
        if path in saved:
            reason = "Already attached"
        elif parent_doi and parent_doi == record["doi"]:
            try:
                unchanged = digest_file(path) == known[path]["digest"]
            except OSError:
                unchanged = False
            reason = "Reviewed parent DOI" if unchanged else "Previously reviewed SI — file changed"
        elif any(stem == value or stem.startswith(value + " (") or stem.startswith(value + "_") for value in [expected, *stems]):
            reason = "Similar SI filename — review"
        if reason:
            display_path = Path(path).resolve()
            result.append({"path": str(display_path), "label": display_path.name, "source": reason, "url": ""})
    return result


def check_merge(db, sources):
    """Protect the relationship even when linking through another UI/API."""
    paths = {canonical(row["locator"]) for row in sources if row["kind"] == "local"}
    digests = {row["digest"] for row in sources if row["digest"]}
    identities = {row["paper_id"] for row in sources}
    for row in db.execute("""SELECT d.path AS parent,a.path AS child FROM paper_attachments a
        JOIN documents d ON d.id=a.document_id"""):
        parent = canonical(row["parent"]) in paths
        if not parent:
            linked = db.execute("SELECT paper_id FROM paper_sources WHERE kind='local' AND locator=?", (canonical(row["parent"]),)).fetchone()
            parent = linked is not None and linked[0] in identities
        saved_child = db.execute("SELECT digest FROM supplement_files WHERE path=?", (canonical(row["child"]),)).fetchone()
        if parent and (canonical(row["child"]) in paths or (saved_child and saved_child[0] in digests)):
            raise ValueError("Supporting information must remain a separate document. Attach it to its parent instead of linking paper identities.")
    metadata = []
    for row in sources:
        meta = json.loads(row["metadata"])
        if row["kind"] == "local":
            saved = db.execute("SELECT details FROM supplement_files WHERE path=?", (canonical(row["locator"]),)).fetchone()
            if saved:
                meta = {**meta, **json.loads(saved[0])}
        metadata.append(meta)
    si = [meta for meta in metadata if meta.get("is_supplementary_material")]
    for details in si:
        parent_doi = normalize_doi(details.get("supplemental_parent_doi"))
        if parent_doi and any(not meta.get("is_supplementary_material")
                              and parent_doi == normalize_doi(meta.get("doi") or (meta.get("identifier") or {}).get("doi")) for meta in metadata):
            raise ValueError("Supporting information and its parent article cannot share a paper identity.")
