import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pymupdf

from note_organizer.handwriting_ocr import HandwritingOCR
from note_organizer.zotero_pilot import searchable_content


class HandwritingOCRTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.models = self.root / "models"
        self.models.mkdir()
        self.files = {"test-model.onnx": hashlib.sha256(b"fixture").hexdigest()}
        (self.models / "test-model.onnx").write_bytes(b"fixture")
        self.addCleanup(patch.stopall)
        patch("note_organizer.handwriting_ocr.MODEL_FILES", self.files).start()
        patch("note_organizer.handwriting_ocr.versions", return_value={"fixture": "1"}).start()
        self.path = self.root / "paper.pdf"
        with pymupdf.open() as document:
            page = document.new_page()
            page.insert_text((50, 80), "printedbaseline")
            ink = page.add_ink_annot([[(80, 150), (110, 160), (130, 140)]])
            document.xref_set_key(ink.xref, "NM", pymupdf.get_pdf_str("stable-tablet-stroke"))
            ink.update()
            document.new_page()
            document.save(self.path)
        self.engine = HandwritingOCR(self.models, self.root / "cache")
        self.lines = [{"text": "recognizedhandwriting", "confidence": .95,
                       "box": [[1, 1], [100, 1], [100, 20], [1, 20]]}]

    def test_model_corruption_stops_before_recognition(self):
        (self.models / "test-model.onnx").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "checksum"):
            HandwritingOCR(self.models, self.root / "other-cache")

    def test_recognition_uses_only_ink_keeps_source_and_provenance(self):
        before = self.path.read_bytes()
        images = []
        def recognize(image):
            images.append(image)
            return self.lines
        with patch.object(self.engine, "_recognize", side_effect=recognize) as call:
            texts = self.engine.content(self.path)
        self.assertEqual(call.call_count, 1)
        self.assertIn("unverified", texts[0])
        self.assertIn("recognizedhandwriting", texts[0])
        self.assertEqual(texts[1], "")
        self.assertEqual(self.path.read_bytes(), before)
        cached = json.loads(next((self.root / "cache").rglob("*.json")).read_text())
        record = cached["records"][0]
        self.assertFalse(record["reviewed"])
        self.assertEqual(record["annotation_ids"], ["stable-tablet-stroke"])
        self.assertEqual(record["lines"], self.lines)
        self.assertEqual(record["image_sha256"], hashlib.sha256(images[0]).hexdigest())

    def test_cache_prevents_recognition_and_corrupt_cache_rebuilds(self):
        with patch.object(self.engine, "_recognize", return_value=self.lines) as call:
            first = self.engine.content(self.path)
            self.assertEqual(self.engine.content(self.path), first)
            self.assertEqual(call.call_count, 1)
            target = next((self.root / "cache").rglob("*.json"))
            value = json.loads(target.read_text())
            value["records"][0]["text"] = "corrupt stale text"
            target.write_text(json.dumps(value))
            self.assertEqual(self.engine.content(self.path), first)
            self.assertEqual(call.call_count, 2)

    def test_failed_engine_retries_and_does_not_commit_empty_cache(self):
        with patch.object(self.engine, "_recognize", side_effect=RuntimeError("private details")) as call:
            for _ in range(2):
                with self.assertRaisesRegex(ValueError, "retained ink is unchanged") as caught:
                    self.engine.content(self.path)
                self.assertNotIn("private details", str(caught.exception))
            self.assertEqual(call.call_count, 2)
        self.assertFalse((self.root / "cache").exists())

    def test_cancel_keeps_source_without_partial_cache(self):
        self.engine.cancelled = lambda: True
        with self.assertRaises(InterruptedError):
            self.engine.content(self.path)
        self.assertFalse((self.root / "cache").exists())

    def test_no_result_is_reported_without_erasing_ink(self):
        before = self.path.read_bytes()
        with patch.object(self.engine, "_recognize", return_value=[]):
            self.assertEqual(self.engine.content(self.path), ["", ""])
            self.assertEqual(self.engine.warnings[0]["page"], 1)
        with patch.object(self.engine, "_recognize", side_effect=AssertionError("unexpected rerun")):
            self.engine.content(self.path)
            self.assertTrue(self.engine.warnings)
        self.assertEqual(self.path.read_bytes(), before)

    def test_changed_pdf_is_recognized_again(self):
        with patch.object(self.engine, "_recognize", return_value=self.lines) as call:
            self.engine.content(self.path)
            with pymupdf.open(self.path) as document:
                document[0].add_ink_annot([[(100, 200), (130, 220)]])
                changed = document.tobytes()
            self.path.write_bytes(changed)
            self.engine.content(self.path)
            self.assertEqual(call.call_count, 2)

    def test_model_profile_change_invalidates_transcript_cache(self):
        with patch.object(self.engine, "_recognize", return_value=self.lines):
            self.engine.content(self.path)
        with patch("note_organizer.handwriting_ocr.versions", return_value={"fixture": "2"}):
            other = HandwritingOCR(self.models, self.root / "cache")
        self.assertNotEqual(other.profile, self.engine.profile)
        with patch.object(other, "_recognize", return_value=self.lines) as recognize:
            other.content(self.path)
        self.assertEqual(recognize.call_count, 1)

    def test_optional_transcription_joins_saved_notes_and_print_without_pdf_write(self):
        before = self.path.read_bytes()
        with patch.object(self.engine, "_recognize", return_value=self.lines):
            text, count, empty = searchable_content(self.path, ({"ocr": "reviewedtranscript"},), handwriting_ocr=self.engine)
        self.assertIn("recognizedhandwriting", text)
        self.assertIn("reviewedtranscript", text)
        self.assertIn("printedbaseline", text)
        self.assertEqual((count, empty), (2, [2]))
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
