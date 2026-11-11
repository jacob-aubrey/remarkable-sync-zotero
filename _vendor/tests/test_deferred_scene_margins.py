"""Tablet marks retain source coordinates until the combined canvas is built."""
from dataclasses import replace
from io import BytesIO
import unittest

import fitz
from rmscene import read_blocks, write_blocks
from rmscene.crdt_sequence import CrdtSequenceItem
from rmscene.scene_items import Point
from rmscene.scene_stream import RootTextBlock, SceneGlyphItemBlock, SceneLineItemBlock
from rmscene.tagged_block_common import CrdtId

from note_organizer.remarkable_pdf import annotated_pdf, UnsupportedAnnotations
from tests.test_remarkable import fixture


def margin_fixture(rotation=0, cropped=False, paper_size=(1404, 1872)):
    item, bundle = fixture(paper_size=paper_size)
    pdf_name = item.id + ".pdf"
    with fitz.open(stream=bundle[pdf_name], filetype="pdf") as source:
        page = source[0]
        note = page.add_text_annot((100, 140), "PC source comment")
        note.update()
        highlight = page.add_highlight_annot(fitz.Rect(60, 87, 215, 104))
        highlight.set_info(content="PC source highlight")
        highlight.update()
        ink = page.add_ink_annot([[(200, 200), (240, 220)]])
        ink.set_info(content="PC source ink")
        ink.update()
        if cropped:
            page.set_cropbox(fitz.Rect(20, 30, 580, 770))
        page.set_rotation(rotation)
        width, height = page.rect.width, page.rect.height
        derotation = page.derotation_matrix
        bundle[pdf_name] = source.tobytes()

    displayed_strokes = (
        ((-45, height * .30), (-30, height * .32)),
        ((width * .30, -45), (width * .32, -30)),
        ((width + 30, height * .40), (width + 45, height * .42)),
        ((width * .50, height + 30), (width * .52, height + 45)),
    )
    scale = .3177 if paper_size == (1404, 1872) else .3144
    name = next(name for name in bundle if name.endswith(".rm"))
    blocks = list(read_blocks(BytesIO(bundle[name])))
    template = next(block for block in blocks if isinstance(block, SceneLineItemBlock))
    blocks[:] = [block for block in blocks if not isinstance(
        block, (RootTextBlock, SceneGlyphItemBlock, SceneLineItemBlock))]
    for number, stroke in enumerate(displayed_strokes):
        points = [Point((x - width / 2) / scale, y / scale, 0, 0, 16, 90) for x, y in stroke]
        line = replace(template.item.value, points=points)
        node = CrdtSequenceItem(CrdtId(1, 200 + number), CrdtId(0, 0), CrdtId(0, 0), 0, line)
        blocks.append(SceneLineItemBlock(template.parent_id, node))
    stream = BytesIO()
    write_blocks(stream, blocks, options={"version": "3.1"})
    bundle[name] = stream.getvalue()
    expected = [[fitz.Point(*position) * derotation for position in stroke] for stroke in displayed_strokes]
    return item, bundle, expected


class DeferredSceneMarginTests(unittest.TestCase):
    def assert_points_close(self, actual, expected):
        self.assertEqual(len(actual), len(expected))
        for point, expected_point in zip(actual, expected):
            for coordinate, expected_coordinate in zip(point, expected_point):
                self.assertAlmostEqual(coordinate, expected_coordinate, places=3)

    def test_deferred_four_sided_margin_ink_keeps_original_boxes_native_marks_and_text(self):
        for rotation in (0, 90, 180, 270):
            for cropped in (False, True):
                for paper_size in ((1404, 1872), (1620, 2160)):
                    with self.subTest(rotation=rotation, cropped=cropped, paper_size=paper_size):
                        item, bundle, expected = margin_fixture(rotation, cropped, paper_size)
                        source_bytes = bundle[item.id + ".pdf"]
                        converted = annotated_pdf(item, bundle, expand_margins=False)
                        self.assertEqual(bundle[item.id + ".pdf"], source_bytes)
                        with fitz.open(stream=source_bytes, filetype="pdf") as source, \
                                fitz.open(stream=converted, filetype="pdf") as output:
                            page, before = output[0], source[0]
                            self.assertEqual(page.mediabox, before.mediabox)
                            self.assertEqual(page.cropbox, before.cropbox)
                            self.assertEqual(page.rotation, before.rotation)
                            self.assertEqual(page.get_text("words"), before.get_text("words"))
                            native_before = {note.info["content"]: note for note in before.annots()}
                            native_after = {note.info["content"]: note for note in page.annots()
                                            if note.info.get("content") in native_before}
                            self.assertEqual(set(native_after), set(native_before))
                            for content, note in native_before.items():
                                after = native_after[content]
                                self.assertEqual(note.rect, after.rect)
                                self.assertEqual(note.vertices, after.vertices)
                            tablet = sorted((note for note in page.annots()
                                             if note.info.get("subject") == "reMarkable pen"),
                                            key=lambda note: note.info["id"])
                            self.assertEqual(len(tablet), 4)
                            unrotated_page = page.rect * page.derotation_matrix
                            for note, stroke in zip(tablet, expected):
                                self.assert_points_close(note.vertices[0], stroke)
                                self.assertFalse(note.rect.intersects(unrotated_page))

    def test_default_conversion_still_expands_margins(self):
        item, bundle, _ = margin_fixture()
        with fitz.open(stream=annotated_pdf(item, bundle), filetype="pdf") as output:
            page = output[0]
            self.assertGreater(page.rect.width, 600)
            self.assertGreater(page.rect.height, 800)
            self.assertTrue(all(page.rect.contains(note.rect) for note in page.annots()))

    def test_deferred_text_note_wholly_beyond_right_edge_is_retained(self):
        item, bundle = fixture()
        name = next(name for name in bundle if name.endswith(".rm"))
        blocks = list(read_blocks(BytesIO(bundle[name])))
        typed = next(block for block in blocks if isinstance(block, RootTextBlock))
        typed.value.pos_x = (650 - 300) / .3177
        typed.value.pos_y = 150 / .3177
        stream = BytesIO()
        write_blocks(stream, blocks, options={"version": "3.1"})
        bundle[name] = stream.getvalue()
        with fitz.open(stream=annotated_pdf(item, bundle, expand_margins=False), filetype="pdf") as output:
            page = output[0]
            note = next(note for note in page.annots() if note.info.get("subject") == "reMarkable typed note")
            self.assertEqual(note.info["content"], "Typed margin note")
            self.assertGreater(note.rect.x0, page.rect.x1)
            self.assertEqual(page.rect, fitz.Rect(0, 0, 600, 800))

    def test_deferred_outlier_still_requires_review(self):
        item, bundle, _ = margin_fixture()
        name = next(name for name in bundle if name.endswith(".rm"))
        blocks = list(read_blocks(BytesIO(bundle[name])))
        line = next(block for block in blocks if isinstance(block, SceneLineItemBlock)).item.value
        for point in line.points:
            point.x = point.y = 100000
        stream = BytesIO()
        write_blocks(stream, blocks, options={"version": "3.1"})
        bundle[name] = stream.getvalue()
        with self.assertRaisesRegex(UnsupportedAnnotations, "outside the supported PDF margin area"):
            annotated_pdf(item, bundle, expand_margins=False)


if __name__ == "__main__":
    unittest.main()
