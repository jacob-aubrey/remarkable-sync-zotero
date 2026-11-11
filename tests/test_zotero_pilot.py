"""Failure recovery and ownership contracts for the optional Zotero publisher."""
import copy
from dataclasses import replace
import hashlib
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import pymupdf

from note_organizer.zotero_pilot import Ledger, LocalZotero, Paper, PilotError, load_manifest, publish, searchable_content


class FakeZotero:
    server_id = "synthetic-instance"

    def __init__(self):
        self.items, self.collections, self.files = {}, {}, {}
        self.writes = self.uploads = self.indexes = self.authorizations = 0
        self.fail_upload = self.fail_index = False
        self.index_contents = {}

    def authorize(self):
        self.authorizations += 1

    def get(self, kind, key):
        return copy.deepcopy((self.collections if kind == "collection" else self.items).get(key))

    def create(self, kind, record):
        self.writes += 1
        target = self.collections if kind == "collection" else self.items
        target[record["key"]] = copy.deepcopy(record)
        return self.get(kind, record["key"])

    def file_md5(self, key):
        return hashlib.md5(self.files[key]).hexdigest() if key in self.files else ""

    def upload(self, key, path, previous_md5):
        if self.file_md5(key) != previous_md5:
            raise PilotError("Concurrent file change")
        self.uploads += 1
        self.files[key] = path.read_bytes()
        if self.fail_upload:
            self.fail_upload = False
            raise OSError("Lost confirmation after server committed")

    def index(self, key, path, saved_annotations=()):
        self.indexes += 1
        if self.fail_index:
            self.fail_index = False
            raise OSError("Index unavailable")
        self.index_contents[key] = searchable_content(path, saved_annotations)[0]
        return []

    def index_digest(self, key):
        return hashlib.sha256(self.index_contents.get(key, "").encode()).hexdigest()

    def children(self, key):
        return [copy.deepcopy(row) for row in self.items.values() if row.get("parentItem") == key]

    def reader_annotations(self, key):
        return [row for row in self.children(key) if row.get("itemType") == "annotation"]

    def native_annotation_count(self, key):
        return len(self.reader_annotations(key))


class ZoteroPilotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.client = FakeZotero()
        self.ledger = Ledger(self.root / "helper")
        self.paper = self.pdf("paper", "Original printed text")

    def pdf(self, source, text, **changes):
        path = self.root / (source + ".pdf")
        with pymupdf.open() as doc:
            page = doc.new_page()
            page.insert_text((70, 90), text)
            doc.save(path)
        return Paper(source, path, hashlib.sha256(path.read_bytes()).hexdigest(),
                     {"itemType": "thesis", "title": "Example thesis", "university": "Example University"}, **changes)

    def run_publish(self, papers=None, apply=True):
        return publish(self.client, self.ledger, "fixture", papers or [self.paper], apply=apply)

    def test_plan_does_not_authorize_or_write_zotero(self):
        result = self.run_publish(apply=False)
        self.assertEqual(result["papers"][0]["action"], "create")
        self.assertEqual((self.client.writes, self.client.uploads, self.client.authorizations), (0, 0, 0))

    def test_repeat_reuses_keys_and_preserves_zotero_metadata(self):
        before = self.paper.path.read_bytes()
        result = self.run_publish()["papers"][0]
        self.client.items[result["reference_key"]]["title"] = "Title reviewed in Zotero"
        again = self.run_publish()["papers"][0]
        self.assertEqual(again["attachment_key"], result["attachment_key"])
        self.assertEqual(again["action"], "unchanged")
        self.assertEqual((self.client.writes, self.client.uploads, self.client.indexes), (3, 1, 1))
        self.assertEqual(self.client.items[result["reference_key"]]["title"], "Title reviewed in Zotero")
        self.assertEqual(self.paper.path.read_bytes(), before)

    def test_revision_updates_same_attachment_and_keeps_previous_bytes(self):
        result = self.run_publish()["papers"][0]
        old = self.paper.sha256
        self.paper.path.unlink()
        self.paper = self.pdf("paper", "Updated PDF with new tablet note")
        again = self.run_publish()["papers"][0]
        self.assertEqual(again["action"], "update")
        self.assertEqual(again["attachment_key"], result["attachment_key"])
        self.assertTrue((self.ledger.directory / "versions" / old / "Current.pdf").exists())
        self.assertEqual(len(self.client.items), 2)

    def test_lost_upload_confirmation_recovers_without_duplicate(self):
        self.client.fail_upload = True
        with self.assertRaises(OSError):
            self.run_publish()
        result = self.run_publish()["papers"][0]
        self.assertEqual(result["action"], "unchanged")
        self.assertEqual(self.client.uploads, 1)
        self.assertEqual(len(self.client.items), 2)

    def test_index_failure_retries_index_without_upload(self):
        self.client.fail_index = True
        with self.assertRaises(OSError):
            self.run_publish()
        self.run_publish()
        self.assertEqual((self.client.uploads, self.client.indexes), (1, 2))

    def test_external_pdf_edits_are_preserved(self):
        key = self.run_publish()["papers"][0]["attachment_key"]
        self.client.files[key] = b"independent user edit"
        with self.assertRaisesRegex(PilotError, "edited outside"):
            self.run_publish()
        self.assertEqual(self.client.files[key], b"independent user edit")

    def test_native_reader_annotations_prevent_geometry_changing_replacement(self):
        key = self.run_publish()["papers"][0]["attachment_key"]
        self.client.items["NATIVE23"] = {"itemType": "annotation", "parentItem": key}
        self.paper.path.unlink()
        self.paper = self.pdf("paper", "New tablet revision")
        with self.assertRaisesRegex(PilotError, "Zotero reader annotations"):
            self.run_publish()
        self.assertEqual(self.client.uploads, 1)

    def test_reader_refresh_failure_resumes_without_uploading_pdf_twice(self):
        self.client.can_refresh_reader = True
        self.client.refresh_reader = Mock(side_effect=[OSError("Lost reader confirmation"), None])
        with self.assertRaises(OSError):
            self.run_publish()
        self.run_publish()
        self.run_publish()
        self.assertEqual(self.client.uploads, 1)
        self.assertEqual(self.client.refresh_reader.call_count, 2)
        self.assertEqual(self.ledger.reader_revision(self.paper.source_id), self.paper.sha256)

    def test_uncertain_annotation_ownership_needs_support_extension(self):
        client = LocalZotero.__new__(LocalZotero)
        client._reader_support = False
        client.reader_annotations = Mock(return_value=[{"itemType": "annotation"}])
        with self.assertRaisesRegex(PilotError, "Companion Support"):
            client.native_annotation_count("SELECT23")

    def test_deleted_item_is_not_recreated(self):
        key = self.run_publish()["papers"][0]["attachment_key"]
        del self.client.items[key]
        with self.assertRaisesRegex(PilotError, "missing"):
            self.run_publish()
        self.assertNotIn(key, self.client.items)

    def test_deleted_collection_is_not_recreated(self):
        key = self.run_publish()["collection_key"]
        del self.client.collections[key]
        with self.assertRaisesRegex(PilotError, "collection is missing"):
            self.run_publish()
        self.assertNotIn(key, self.client.collections)

    def test_wrong_server_is_rejected_before_writes(self):
        self.run_publish()
        self.client.server_id = "different-instance"
        writes = self.client.writes
        with self.assertRaisesRegex(PilotError, "another Zotero"):
            self.run_publish()
        self.assertEqual(self.client.writes, writes)

    def test_si_is_sibling_under_same_parent_not_duplicate_reference(self):
        si = self.pdf("si", "Supplementary information", reference_id="paper", role="si")
        result = self.run_publish([self.paper, si])["papers"]
        self.assertEqual(result[0]["reference_key"], result[1]["reference_key"])
        self.assertEqual(len(self.client.items), 3)

    def test_changed_prepared_file_is_not_uploaded(self):
        self.paper.path.write_bytes(b"changed since review")
        with self.assertRaisesRegex(PilotError, "changed"):
            self.run_publish()
        self.assertEqual(self.client.writes, 0)

    def test_changed_ownership_blocks_update(self):
        item = self.run_publish()["papers"][0]
        self.client.items[item["attachment_key"]]["note"] = "independent attachment"
        with self.assertRaisesRegex(PilotError, "ownership"):
            self.run_publish()

    def test_run_lock_prevents_simultaneous_publish(self):
        with self.ledger.lock():
            with self.assertRaisesRegex(PilotError, "already in use"):
                self.run_publish()

    def test_client_reuses_in_memory_permission_instead_of_prompting_again(self):
        client = LocalZotero.__new__(LocalZotero)
        client.zot = Mock(local_api_key="synthetic-grant")
        client.authorize()
        client.zot.authorize_local.assert_not_called()

    def test_manifest_rejects_unparented_si_and_duplicate_sources(self):
        manifest = {"schema": 1, "namespace": "fixture", "papers": [{
            "source_id": "si", "pdf": str(self.paper.path), "sha256": self.paper.sha256,
            "reference_id": "missing", "role": "si"}]}
        path = self.root / "manifest.json"
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(PilotError, "main PDF"):
            load_manifest(path)
        manifest["papers"] *= 2
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(PilotError, "unique"):
            load_manifest(path)

    def test_corrected_transcript_reindexes_without_reupload(self):
        self.paper = replace(self.paper, annotations=({"ocr": "nanoparticle"},))
        self.run_publish()
        self.paper = replace(self.paper, annotations=({"ocr": "zirconium nanoparticle"},))
        self.run_publish()
        self.assertEqual((self.client.uploads, self.client.indexes), (1, 2))

    def test_native_reindex_does_not_permanently_erase_transcript(self):
        self.paper = replace(self.paper, annotations=({"ocr": "savedtranscript"},))
        key = self.run_publish()["papers"][0]["attachment_key"]
        self.client.index_contents[key] = "Native printed-PDF-only rebuild"
        self.run_publish()
        self.assertIn("savedtranscript", self.client.index_contents[key])
        self.assertEqual((self.client.uploads, self.client.indexes), (1, 2))

    def test_highlight_comments_and_saved_transcripts_enter_search_index(self):
        path = self.root / "annotations.pdf"
        with pymupdf.open() as doc:
            page = doc.new_page()
            page.insert_text((60, 100), "highlighted scientific result")
            highlight = page.add_highlight_annot(page.search_for("scientific"))
            doc.xref_set_key(highlight.xref, "RMQuote", pymupdf.get_pdf_str("uniquehighlightquotation"))
            note = page.add_text_annot((120, 160), "uniquetypedcomment")
            note.update()
            doc.save(path)
        text, count, empty = searchable_content(path, ({"ocr": "savedhandwritingtranscript"},))
        for value in ("uniquehighlightquotation", "uniquetypedcomment", "savedhandwritingtranscript"):
            self.assertIn(value, text)
        self.assertEqual((count, empty), (1, []))

    def test_tablet_highlights_preserve_source_selection_boundaries(self):
        from rmscene import read_blocks, write_blocks
        from rmscene.crdt_sequence import CrdtSequenceItem
        from rmscene.scene_items import GlyphRange, PenColor, Rectangle
        from rmscene.scene_stream import SceneGlyphItemBlock, SceneLineItemBlock
        from rmscene.tagged_block_common import CrdtId

        from note_organizer.remarkable_pdf import annotated_pdf
        from tests.test_remarkable import fixture

        item, bundle = fixture(deleted_ink=True)
        layer = next(name for name in bundle if name.endswith(".rm"))
        blocks = [block for block in read_blocks(BytesIO(bundle[layer]))
                  if not isinstance(block, (SceneGlyphItemBlock, SceneLineItemBlock))]
        selections = ("One continuous selection", "Separate line", "Another mark")
        rects = ([Rectangle(-700, 265, 170, 32), Rectangle(-700, 303, 150, 32)],
                 [Rectangle(-700, 341, 220, 32)], [Rectangle(-700, 379, 220, 32)])
        start = 0
        previous = CrdtId(0, 0)
        for index, (quote, boxes) in enumerate(zip(selections, rects), 200):
            glyph = GlyphRange(start, len(quote), quote, PenColor.GREEN, boxes)
            identity = CrdtId(1, index)
            blocks.append(SceneGlyphItemBlock(CrdtId(0, 11), CrdtSequenceItem(
                identity, previous, CrdtId(0, 0), 0, glyph)))
            previous = identity
            start += len(quote) + 2  # Adjacent text offsets across a line break.
        stream = BytesIO()
        write_blocks(stream, blocks, options={"version": "3.1"})
        bundle[layer] = stream.getvalue()

        with pymupdf.open(stream=annotated_pdf(item, bundle), filetype="pdf") as doc:
            page = doc[0]
            highlights = [(doc.xref_get_key(annot.xref, "RMQuote")[1], len(annot.vertices) // 4)
                          for annot in page.annots() if annot.type[1] == "Highlight"]
        self.assertCountEqual(highlights, zip(selections, (2, 1, 1)))

    def test_prepare_carries_only_selected_papers_saved_transcripts(self):
        from note_organizer.storage import Store, DEFAULT_SETTINGS
        from note_organizer.paper_registry import Registry
        from tools.zotero_pilot import prepare
        store = Store(self.root / "notable" / "library.sqlite3")
        registry = Registry(store)
        selected = registry.register_local(self.paper.path)
        other = self.pdf("other", "Different paper")
        registry.register_local(other.path)
        project = store.add_project(self.root)
        for paper, transcript in ((self.paper, "selectedtranscript"), (other, "privatetranscript")):
            store.save_scan(project, paper.path, paper.sha256, "fixture", json.dumps(DEFAULT_SETTINGS), {}, [{
                "key": "fixture", "kind": "drawing", "page": 1, "rect": [0, 0, 20, 20],
                "ocr": transcript, "quote": "", "comment": "", "image": ""}])
        before = store.path.read_bytes()
        manifest = prepare(store.path, selected["paper_id"], self.root / "prepared")
        _, papers = load_manifest(manifest)
        self.assertEqual([a["ocr"] for a in papers[0].annotations], ["selectedtranscript"])
        self.assertEqual(store.path.read_bytes(), before)
