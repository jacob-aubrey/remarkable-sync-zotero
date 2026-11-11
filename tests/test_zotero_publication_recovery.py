"""Reproductions of upload receipts, partial indexing, and PDF navigation."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

import pymupdf

from note_organizer.remarkable_pdf import annotated_pdf, UnsupportedAnnotations
from note_organizer.zotero_pilot import Ledger, Paper, publish
from tests.test_zotero_pilot import FakeZotero


class ReceiptClient(FakeZotero):
    def __init__(self):
        super().__init__()
        self.remote_hash, self.remote_mtime = "a" * 32, 1234567890000
        self.finish_failures = 0
        self.finishes = 0
        self.receipts = []

    def prepare_file_upload(self, key, previous_md5):
        return {"key": key, "md5": previous_md5, "authorization_md5": self.remote_hash,
                "remote_md5": self.remote_hash, "remote_mtime": self.remote_mtime}

    def upload(self, key, path, previous_md5, *, upload_intent=None):
        self.receipts.append(dict(upload_intent))
        super().upload(key, path, previous_md5)
        # Zotero's local API records the staged bytes as if account-synced.
        self.remote_hash, self.remote_mtime = self.file_md5(key), 9999999999999

    def finish_file_upload(self, key, md5, receipt):
        self.finishes += 1
        if self.finish_failures:
            self.finish_failures -= 1
            raise OSError("Native receipt interrupted")
        assert self.file_md5(key) == md5
        self.remote_hash, self.remote_mtime = receipt["remote_md5"], receipt["remote_mtime"]
        self.sync_state = "to_upload"


class PublicationRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "paper.pdf"
        with pymupdf.open() as document:
            document.new_page().insert_text((60, 80), "Research document")
            document.save(self.path)
        self.paper = Paper("paper", self.path, hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.ledger = Ledger(self.root / "state")

    def run_publication(self, client):
        return publish(client, self.ledger, "synthetic", [self.paper], apply=True)

    def test_upload_restores_account_precondition_and_marks_new_bytes_for_sync(self):
        client = ReceiptClient()
        self.run_publication(client)
        self.assertEqual((client.remote_hash, client.remote_mtime), ("a" * 32, 1234567890000))
        self.assertEqual(client.sync_state, "to_upload")
        self.assertEqual(client.uploads, 1)
        self.assertIsNone(self.ledger.upload_intent("paper"))

    def test_lost_native_completion_retries_receipt_without_reupload(self):
        client = ReceiptClient()
        client.finish_failures = 1
        with self.assertRaisesRegex(OSError, "receipt"):
            self.run_publication(client)
        persisted = Ledger(self.root / "state")
        self.assertEqual(persisted.upload_intent("paper")["remote_md5"], "a" * 32)
        self.ledger = persisted
        self.run_publication(client)
        self.assertEqual((client.uploads, client.finishes), (1, 2))
        self.assertEqual(client.remote_hash, "a" * 32)
        self.assertIsNone(self.ledger.upload_intent("paper"))

    def test_lost_upload_response_reuses_receipt_and_finishes_account_state(self):
        client = ReceiptClient()
        client.fail_upload = True
        with self.assertRaises(OSError):
            self.run_publication(client)
        self.run_publication(client)
        self.assertEqual((client.uploads, client.finishes), (1, 1))
        self.assertEqual(client.sync_state, "to_upload")

    def test_partial_ocr_is_retried_on_next_unchanged_publication(self):
        client = FakeZotero()
        original_index = client.index
        def index(*args):
            client.index_complete = client.indexes > 0
            client.index_diagnostics = [{"incomplete": not client.index_complete,
                                         "printed_ocr": [{"page": 1, "reason": "Retry OCR"}]}]
            return original_index(*args)
        client.index = index
        first = self.run_publication(client)
        self.assertTrue(first["papers"][0]["index_warnings"]["incomplete"])
        self.assertEqual(self.ledger.get("paper")["indexed_sha"], "")
        self.run_publication(client)
        self.assertEqual((client.uploads, client.indexes), (1, 2))
        self.run_publication(client)
        self.assertEqual(client.indexes, 2)


class PDFNavigationRecoveryTests(unittest.TestCase):
    def bundle(self, order):
        with pymupdf.open() as source:
            for number in range(3):
                source.new_page().insert_text((50, 70), f"Source page {number + 1}")
            source[0].insert_link({"kind": pymupdf.LINK_GOTO, "from": pymupdf.Rect(30, 30, 100, 50),
                                   "page": 2, "to": pymupdf.Point(10, 20)})
            source[0].insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(30, 60, 100, 80),
                                   "uri": "https://example.org/research"})
            source.set_toc([[1, "Beginning", 1], [2, "Results", 3], [1, "Deleted chapter", 2]])
            data = source.tobytes()
        records = [{"id": f"page-{number}", "redir": {"value": number}, "idx": {"value": str(index)}}
                   for index, number in enumerate(order)]
        content = {"fileType": "pdf", "cPages": {"pages": records}}
        return {"paper.pdf": data, "paper.content": json.dumps(content).encode()}

    def test_bookmarks_and_cross_page_and_external_links_survive_reordered_added_pages(self):
        bundle = self.bundle([2, -1, 0])
        before = dict(bundle)
        data = annotated_pdf(SimpleNamespace(id="paper"), bundle)
        self.assertEqual(bundle, before)
        with pymupdf.open(stream=data, filetype="pdf") as document:
            self.assertEqual(document.get_toc(), [[1, "Beginning", 3], [2, "Results", 1]])
            links = document[2].get_links()
            internal = next(link for link in links if link["kind"] == pymupdf.LINK_GOTO)
            self.assertEqual(internal["page"], 0)
            self.assertEqual(internal["to"], pymupdf.Point(10, 20))
            self.assertTrue(any(link.get("uri") == "https://example.org/research" for link in links))

    def test_deleted_page_historical_layers_do_not_block_live_render(self):
        bundle = self.bundle([0, 2])
        content = json.loads(bundle["paper.content"])
        content["cPages"]["pages"].append({"id": "old-page", "deleted": {"value": True}})
        bundle["paper.content"] = json.dumps(content).encode()
        bundle["paper/old-page.rm"] = b"historical unsupported scene must not be interpreted"
        bundle["paper.highlights/old-page.json"] = b"{}"
        with pymupdf.open(stream=annotated_pdf(SimpleNamespace(id="paper"), bundle), filetype="pdf") as document:
            self.assertEqual(len(document), 2)
        bundle["paper/unknown-page.rm"] = b"unmapped live content"
        with self.assertRaises(UnsupportedAnnotations):
            annotated_pdf(SimpleNamespace(id="paper"), bundle)

    def test_cancellation_during_navigation_reconstruction_stops_publication(self):
        bundle = self.bundle([0, 1, 2])
        checks = []
        def cancelled():
            checks.append(True)
            return len(checks) >= 6
        with self.assertRaises(InterruptedError):
            annotated_pdf(SimpleNamespace(id="paper"), bundle, cancelled)
        self.assertEqual(len(checks), 6)

    def test_navigation_preserves_exact_coordinates_on_rotated_cropped_reordered_pages(self):
        for rotation in (0, 90, 180, 270):
            for cropped in (False, True):
                for nonzero_origin in (False, True):
                    with self.subTest(rotation=rotation, cropped=cropped, nonzero_origin=nonzero_origin):
                        with pymupdf.open() as source:
                            source.new_page(width=400, height=600)
                            source.new_page(width=400, height=600)
                            for page in source:
                                if nonzero_origin:
                                    source.xref_set_key(page.xref, "MediaBox", "[20 30 420 630]")
                                    page = source.reload_page(page)
                                if cropped:
                                    media = page.mediabox
                                    source.xref_set_key(page.xref, "CropBox",
                                        f"[{media.x0 + 30} {media.y0 + 50} {media.x1 - 30} {media.y1 - 50}]")
                                    page = source.reload_page(page)
                                page.set_rotation(rotation if page.number == 0 else (rotation + 90) % 360)
                            source[0].insert_link({"kind": pymupdf.LINK_GOTO, "from": pymupdf.Rect(40, 60, 100, 80),
                                "page": 1, "to": pymupdf.Point(70, 100)})
                            source[0].insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(40, 90, 100, 110),
                                "uri": "https://example.org/paper"})
                            source.set_toc([[1, "First", 1], [1, "Second", 2]])
                            data = source.tobytes()
                        bundle = {"paper.pdf": data, "paper.content": json.dumps({"fileType": "pdf",
                            "pages": ["second", "first"], "redirectionPageMap": [1, 0]}).encode()}
                        with pymupdf.open(stream=data, filetype="pdf") as original, pymupdf.open(
                                stream=annotated_pdf(SimpleNamespace(id="paper"), bundle), filetype="pdf") as output:
                            before_links, after_links = original[0].get_links(), output[1].get_links()
                            self.assertEqual(len(before_links), len(after_links))
                            for before, after in zip(before_links, after_links):
                                self.assertEqual(before["kind"], after["kind"])
                                self.assertEqual(before["from"], after["from"])
                                if before["kind"] == pymupdf.LINK_GOTO:
                                    self.assertEqual(after["page"], 0)
                                    self.assertEqual(before["to"], after["to"])
                                else:
                                    self.assertEqual(before["uri"], after["uri"])
                            for before, after in zip(original.get_toc(False), output.get_toc(False)):
                                self.assertEqual(before[:2], after[:2])
                                self.assertEqual(after[2], 3 - before[2])
                                self.assertEqual(before[3]["to"], after[3]["to"])

    def test_link_hotspots_and_bookmark_targets_follow_expanded_tablet_margins(self):
        from tests.test_deferred_scene_margins import margin_fixture
        item, bundle, _ = margin_fixture()
        with pymupdf.open(stream=bundle[item.id + ".pdf"], filetype="pdf") as source:
            source[0].insert_link({"kind": pymupdf.LINK_GOTO, "from": pymupdf.Rect(30, 40, 100, 60),
                "page": 0, "to": pymupdf.Point(80, 120)})
            source.set_toc([[1, "Scientific result", 1]])
            bundle[item.id + ".pdf"] = source.tobytes()
        with pymupdf.open(stream=bundle[item.id + ".pdf"], filetype="pdf") as original, pymupdf.open(
                stream=annotated_pdf(item, bundle), filetype="pdf") as output:
            before, after = original[0], output[0]
            shift = pymupdf.Point(before.mediabox.x0 - after.mediabox.x0, after.mediabox.y1 - before.mediabox.y1)
            self.assertGreater(shift.x, 0)
            self.assertGreater(shift.y, 0)
            old_link, new_link = before.get_links()[0], after.get_links()[0]
            self.assertEqual(new_link["from"], old_link["from"] + (shift.x, shift.y, shift.x, shift.y))
            self.assertEqual(new_link["to"], old_link["to"] + shift)
            self.assertEqual(output.get_toc(False)[0][3]["to"], original.get_toc(False)[0][3]["to"] + shift)

