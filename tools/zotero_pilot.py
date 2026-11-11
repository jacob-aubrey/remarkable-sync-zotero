"""Developer pilot: prepare selected Notable PDFs; plan/apply publication to Zotero."""
import argparse
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from note_organizer.zotero_pilot import Ledger, LocalZotero, PilotError, load_manifest, publish


def read_source(library):
    from note_organizer.storage import Store
    class SourceView(Store):
        def __init__(self, path):
            self.path = Path(path).resolve(strict=True)

        @contextmanager
        def connect(self):
            db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=1)
            db.row_factory = sqlite3.Row
            try:
                yield db
            finally:
                db.close()
    return SourceView(library)


def saved_text(store, paper_id):
    """Saved text is selected by recorded source identity, not matching titles."""
    with store.connect() as db:
        rows = db.execute("""SELECT DISTINCT e.document_id,e.key,e.payload,d.digest
            FROM entries e JOIN documents d ON d.id=e.document_id
            LEFT JOIN paper_document_sources m ON m.document_id=d.id
            WHERE d.hidden=0 AND (
                m.source_id IN (SELECT id FROM paper_sources WHERE paper_id=?)
                OR d.path IN (SELECT locator FROM paper_sources WHERE paper_id=? AND kind='local'))
            ORDER BY e.document_id,e.key""", (paper_id, paper_id)).fetchall()
        annotations = []
        for row in rows:
            saved = json.loads(row["payload"])
            annotations.append({**{field: str(saved.get(field) or "") for field in ("quote", "comment", "ocr")},
                "page": saved.get("page"), "source_digest": row["digest"], "source_annotation": row["key"]})
    return annotations


def tablet_saved_text(library, cloud_id):
    store = read_source(library)
    with store.connect() as db:
        rows = db.execute("SELECT DISTINCT paper_id FROM paper_sources WHERE kind='cloud' AND locator=?", (cloud_id,)).fetchall()
    if len(rows) > 1:
        raise PilotError("The tablet document has more than one logical paper identity. Review its source links.")
    return saved_text(store, rows[0][0]) if rows else []


def prepare(library, paper_id, output, *, metadata=None, namespace="notable-pilot"):
    """Read the selected logical paper using the existing combined-PDF engine."""
    from note_organizer.combined_pdf import export_combined
    store = read_source(library)
    with store.connect() as db:
        record = db.execute("SELECT title FROM paper_registry WHERE id=?", (paper_id,)).fetchone()
        if not record:
            raise PilotError("Select an existing logical paper ID from the Notable library.")
    annotations = saved_text(store, paper_id)
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    pdf = output / ("prepared-" + uuid.uuid4().hex + ".pdf")
    # Existing guarded renderer checks original editions, page maps and sources.
    # It can populate its rebuildable reading cache, but the DB is read-only.
    export_combined(store, paper_id, pdf)
    manifest = {"schema": 1, "namespace": namespace, "papers": [{
        "source_id": paper_id, "pdf": pdf.name,
        "sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
        "metadata": {"title": record[0] or "Selected research document", **(metadata or {})},
        "annotations": annotations,
    }]}
    target = output / (pdf.stem + ".json")
    target.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare", help="Export one selected logical paper; no Zotero connection")
    prep.add_argument("--library", required=True, type=Path)
    prep.add_argument("--paper-id", required=True)
    prep.add_argument("--output", required=True, type=Path)
    prep.add_argument("--metadata", type=Path, help="Optional native Zotero bibliographic fields as JSON")
    prep.add_argument("--namespace", default="notable-pilot")
    send = commands.add_parser("publish", help="Plan by default; --apply requests Zotero's write permission")
    send.add_argument("manifest", type=Path)
    send.add_argument("--state-dir", required=True, type=Path)
    send.add_argument("--port", type=int, default=23119)
    send.add_argument("--apply", action="store_true")
    send.add_argument("--tessdata", type=Path, help="Optional local language data for index-only printed OCR")
    args = parser.parse_args(argv)
    client = None
    try:
        if args.command == "prepare":
            metadata = json.loads(args.metadata.read_text(encoding="utf-8-sig")) if args.metadata else {}
            if not isinstance(metadata, dict):
                raise PilotError("Metadata must be a JSON object.")
            print(json.dumps({"manifest": str(prepare(args.library, args.paper_id, args.output,
                                                      metadata=metadata, namespace=args.namespace))}))
        else:
            namespace, papers = load_manifest(args.manifest)
            from note_organizer.printed_ocr import PrintedOCR
            ocr = PrintedOCR(args.tessdata, args.state_dir / "ocr-cache") if args.tessdata else None
            client = LocalZotero(args.port, printed_ocr=ocr)
            print(json.dumps(publish(client, Ledger(args.state_dir), namespace, papers,
                                     apply=args.apply), indent=2))
        return 0
    except Exception:
        # HTTP exceptions may contain credential headers/body. Keep diagnostics
        # here predictable and never print raw API exceptions or API keys.
        error = sys.exception()
        message = str(error) if isinstance(error, PilotError) else (
            "The pilot could not complete. Check Zotero 10/local API permission, the selected files, "
            "and the separate pilot environment. Retained PDFs and state allow retrying the same manifest.")
        print(json.dumps({"error": message}), file=sys.stderr)
        return 1
    finally:
        if client:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
