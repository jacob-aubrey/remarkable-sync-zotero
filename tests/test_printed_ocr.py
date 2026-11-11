import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pymupdf

from note_organizer.printed_ocr import PrintedOCR
from note_organizer.zotero_pilot import searchable_content


class PrintedOCRTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.models = self.root / "models"
        self.models.mkdir()
        (self.models / "eng.traineddata").write_bytes(b"fake model for isolated cache contracts")
        self.path = self.root / "scan.pdf"
        with pymupdf.open() as source, pymupdf.open() as output:
            page = source.new_page(width=400, height=500)
            page.insert_text((40, 100), "nanoparticle gold silver", fontsize=18)
            image = page.get_pixmap(dpi=144).tobytes("png")
            for rotation in (0, 90):
                target = output.new_page(width=400, height=500)
                target.insert_image(target.rect, stream=image)
                target.set_rotation(rotation)
            output.save(self.path)
        self.engine = PrintedOCR(self.models, self.root / "cache")

    def test_ocr_is_cached_rotation_normalized_and_original_bytes_unchanged(self):
        before = self.path.read_bytes()
        rotations = []
        def recognize(page, **kwargs):
            rotations.append(page.rotation)
            return page.get_textpage()
        with patch.object(pymupdf.Page, "get_textpage_ocr", recognize):
            self.engine.content(self.path)
        with patch.object(pymupdf.Page, "get_textpage_ocr", side_effect=AssertionError("cache miss")):
            self.engine.content(self.path)
        self.assertEqual(rotations, [0, 0])
        self.assertEqual(self.path.read_bytes(), before)

    def test_model_changes_invalidate_cache(self):
        (self.models / "eng.traineddata").write_bytes(b"updated model")
        other = PrintedOCR(self.models, self.root / "cache")
        self.assertNotEqual(self.engine.profile, other.profile)

    def test_failed_ocr_retries_and_keeps_annotations(self):
        with patch.object(pymupdf.Page, "get_textpage_ocr", side_effect=RuntimeError("failed")) as call:
            self.engine.content(self.path)
            self.engine.content(self.path)
        self.assertEqual(call.call_count, 4)
        self.assertTrue(self.engine.warnings)
        self.assertFalse(list((self.root / "cache").rglob("*.json")))
        self.assertFalse(self.engine.complete)

    def test_small_screenshot_on_an_otherwise_empty_page_receives_ocr(self):
        image = self.root / "screenshot.pdf"
        with pymupdf.open(self.path) as scanned, pymupdf.open() as output:
            page = output.new_page(width=1000, height=1000)
            page.insert_image(pymupdf.Rect(100, 100, 400, 400), stream=scanned[0].get_pixmap().tobytes("png"))
            output.save(image)
        with patch.object(pymupdf.Page, "get_textpage_ocr", autospec=True,
                          side_effect=lambda page, **kwargs: page.get_textpage()) as recognize:
            self.engine.content(image)
        self.assertEqual(recognize.call_count, 1)
        self.assertTrue(self.engine.complete)
        self.assertTrue(list((self.root / "cache").rglob("*.json")))

    def test_cancel_does_not_cache_incomplete_document(self):
        self.engine.cancelled = lambda: True
        with self.assertRaises(InterruptedError):
            self.engine.content(self.path)
        self.assertFalse((self.root / "cache").exists())

    def test_index_combines_recognized_body_with_saved_annotations(self):
        with patch.object(self.engine, "content", return_value=["nanoparticle gold", "silver"]):
            text, count, missing = searchable_content(self.path, ({"ocr": "saved handwriting"},), printed_ocr=self.engine)
        self.assertIn("nanoparticle gold", text)
        self.assertIn("saved handwriting", text)
        self.assertEqual((count, missing), (2, []))

    def test_real_english_binding_recognizes_both_rotations_without_source_changes(self):
        data = Path(__file__).resolve().parents[1] / ".test-tmp/ocr-route-preflight/lang"
        if not (data / "eng.traineddata").exists():
            self.skipTest("Optional official English model not installed")
        engine = PrintedOCR(data, self.root / "real-cache")
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        texts = engine.content(self.path)
        for text in texts:
            self.assertIn("nanoparticle", text.lower())
            self.assertIn("silver", text.lower())
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)
