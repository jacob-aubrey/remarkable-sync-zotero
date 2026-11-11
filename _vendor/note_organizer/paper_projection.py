"""Read-only logical paper views and conservative inherited-mark suppression.

Source files, document records, annotation payloads and history remain intact.
Only a proven inherited textual annotation is omitted from the combined view.
"""
from collections import defaultdict
import json
import re


TEXT_MARKS = {"highlight", "underline", "squiggly", "strikeout", "text", "freetext"}
MARK_FIELDS = ("key", "kind", "page", "page_count", "pdf_label", "label", "rect", "quote", "comment",
               "ocr", "context", "color", "created", "modified")


def load_paper_sources(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_sources'").fetchone():
        return {}
    sources = [dict(row) for row in db.execute("SELECT * FROM paper_sources")]
    cloud = {(row["paper_id"], row["locator"]): row for row in sources if row["kind"] == "cloud"}
    result = {}
    for source in sources:
        if source["kind"] != "local":
            continue
        try:
            metadata = json.loads(source["metadata"])
        except (TypeError, ValueError):
            metadata = {}
        metadata = metadata if isinstance(metadata, dict) else {}
        baseline = cloud.get((source["paper_id"], metadata.get("cloud_id")), {})
        result[source["locator"]] = {**source, "metadata": metadata,
                                     "cloud_original_digest": baseline.get("digest", "")}
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_document_sources'").fetchone():
        by_id = {source["id"]: source for source in result.values()}
        for row in db.execute("SELECT * FROM paper_document_sources"):
            if row["source_id"] in by_id:
                result[("document", row["document_id"])] = by_id[row["source_id"]]
    return result


def logical_title(document):
    try:
        metadata = json.loads(document.get("metadata", "{}"))
        title = metadata.get("title", "") if isinstance(metadata, dict) else ""
    except (TypeError, ValueError):
        title = ""
    source = document.get("paper_source", {})
    return title or source.get("title", "")


def annotated_documents(documents, source_links):
    result = []
    for document in documents:
        source = source_links.get(("document", document["id"])) or source_links.get(document["path"])
        metadata = json.loads(document.get("metadata", "{}"))
        if metadata.get("managed_cloud_id") and metadata.get("primary_path"):
            document = {**document, "source_path": document["path"], "path": metadata["primary_path"]}
        result.append({**document, "paper_source": source, "logical_paper_id": source["paper_id"]}
                      if source else dict(document))
    return result


def _fingerprint(entry):
    if entry.get("kind") not in TEXT_MARKS:
        return None
    # xref numbers may be rewritten. Grouped annotation keys cannot prove that
    # a group contains the same ink, so they are retained as independent notes.
    match = re.fullmatch(r"(\d+):([^+]+)", str(entry.get("key", "")))
    if not match or match.group(2).startswith("xref-"):
        return None
    if not isinstance(entry.get("page"), int) or int(match.group(1)) != entry["page"] - 1:
        return None
    if not isinstance(entry.get("page_count"), int) or entry["page_count"] < entry["page"]:
        return None
    rect = entry.get("rect")
    if not isinstance(rect, (list, tuple)) or len(rect) != 4:
        return None
    return json.dumps([entry.get(field, "") for field in MARK_FIELDS], sort_keys=True, ensure_ascii=False)


def inherited_duplicate_ids(entries, documents):
    """Return (document_id, key) for provably inherited textual tablet marks."""
    by_id = {document["id"]: document for document in documents}
    baselines = defaultdict(set)
    for entry in entries:
        document = by_id.get(entry.get("document_id"), {})
        source = document.get("paper_source") or {}
        if not source or source["metadata"].get("role") == "tablet_render":
            continue
        fingerprint = _fingerprint(entry)
        if fingerprint and document.get("digest"):
            baselines[(document["project_id"], source["paper_id"], document["digest"])].add(fingerprint)
    omitted = set()
    for entry in entries:
        document = by_id.get(entry.get("document_id"), {})
        source = document.get("paper_source") or {}
        metadata = source.get("metadata", {})
        if metadata.get("role") != "tablet_render" or metadata.get("page_mapping_identity") is not True:
            continue
        # Registry provenance must describe the same render that was scanned.
        if source.get("digest") != document.get("digest"):
            continue
        digest = source.get("cloud_original_digest", "")
        if not digest or metadata.get("original_pdf_digest", digest) != digest:
            continue
        fingerprint = _fingerprint(entry)
        if fingerprint and fingerprint in baselines.get((document["project_id"], source["paper_id"], digest), set()):
            omitted.add((entry["document_id"], entry["key"]))
    return omitted


def lightweight_note_fingerprints(db, documents):
    """Read only fields used in inheritance proof, and only linked variants."""
    groups = defaultdict(list)
    for document in documents:
        if document.get("logical_paper_id"):
            groups[(document["project_id"], document["logical_paper_id"])].append(document)
    selected = [document["id"] for group in groups.values() if len(group) > 1 and any(
        member["paper_source"]["metadata"].get("role") == "tablet_render"
        and member["paper_source"]["metadata"].get("page_mapping_identity") is True for member in group)
        for document in group]
    rows = []
    # Stay below SQLite's conservative parameter limit for older installations.
    for start in range(0, len(selected), 400):
        ids = selected[start:start + 400]
        fields = ",".join("'" + field + "'," + ("e.key" if field == "key" else
                           "json_extract(e.payload, '$." + field + "')") for field in MARK_FIELDS)
        sql = ("SELECT e.document_id,json_object(" + fields + ") AS fingerprint FROM entries e WHERE e.document_id IN ("
               + ",".join("?" for _ in ids) + ") AND json_extract(e.payload, '$.kind') IN ("
               + ",".join("?" for _ in TEXT_MARKS) + ")")
        for row in db.execute(sql, ids + list(TEXT_MARKS)):
            rows.append({**json.loads(row["fingerprint"]), "document_id": row["document_id"]})
    return rows


def project_papers(documents, entry_fingerprints=()):
    groups, result = defaultdict(list), []
    omitted = inherited_duplicate_ids(entry_fingerprints, documents)
    omitted_counts = defaultdict(int)
    for document_id, _ in omitted:
        omitted_counts[document_id] += 1
    for document in documents:
        if document.get("logical_paper_id"):
            groups[(document["project_id"], document["logical_paper_id"])].append(document)
        else:
            result.append(dict(document))
    for (_, paper_id), variants in groups.items():
        representative = min(variants, key=lambda document: (
            bool(document.get("missing")), document["paper_source"]["metadata"].get("role") == "tablet_render",
            document["path"].casefold(), document["id"]))
        result.append({**representative, "logical_paper_id": paper_id,
                       "logical_paper_title": logical_title(representative),
                       "variant_document_ids": sorted(document["id"] for document in variants),
                       "variant_paths": [document["path"] for document in variants],
                       "count": sum(document.get("count", 0) - omitted_counts[document["id"]] for document in variants),
                       "inherited_duplicate_count": sum(omitted_counts[document["id"]] for document in variants)})
    return sorted(result, key=lambda document: document["path"])
