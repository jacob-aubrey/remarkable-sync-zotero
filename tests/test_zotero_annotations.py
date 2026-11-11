from io import BytesIO
import json
import unittest

import pymupdf as fitz
from rmscene import read_blocks, write_blocks
from rmscene.crdt_sequence import CrdtSequenceItem
from rmscene.scene_items import GlyphRange, Line, Pen, PenColor, Point, Rectangle
from rmscene.scene_stream import SceneGlyphItemBlock, SceneLineItemBlock
from rmscene.tagged_block_common import CrdtId

from note_organizer.remarkable_marks import TextMark, group_text_highlights
from note_organizer.remarkable_pdf import annotated_pdf
from tests.test_remarkable import fixture


def letter_page():
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((60, 100), ['A highlighted passage continues here',
                                 'on the next full line of the passage',
                                 'with a final shorter line'], fontsize=11)
    page.insert_text((325, 100), ['Text in another column goes here', 'and remains separate'], fontsize=11)
    return doc


def line_mark(page, identity, line, start=None, stroke=(1, .84, 0), *, block=0, first=0, last=None):
    words = [word for word in page.get_text('words') if tuple(word[5:7]) == (block, line)][first:last]
    rect = fitz.Rect(words[0][:4]) | fitz.Rect(words[-1][:4])
    text = ' '.join(word[4] for word in words)
    return TextMark(identity, (rect,), text, stroke, start, len(text))


class AnnotationCardsTests(unittest.TestCase):
    def test_only_contiguous_same_color_passages_group(self):
        with letter_page() as doc:
            page = doc[0]
            top, middle, last = (line_mark(page, str(n), n) for n in range(3))
            column = line_mark(page, 'column', 0, block=1)
            [group] = group_text_highlights(page, [last, top, column, middle])
            self.assertEqual(group.ids, ('0', '1', '2'))
            self.assertEqual(group.rects, top.rects + middle.rects + last.rects)
            self.assertEqual(group.text, ' '.join((top.text, middle.text, last.text)))
            self.assertEqual(group_text_highlights(page, [top, last]), [])
            self.assertEqual(group_text_highlights(page, [top, line_mark(page, 'pink', 1, stroke=(1, .4, .7))]), [])
            self.assertEqual(group_text_highlights(page, [top, line_mark(page, 'gap', 1, first=2)]), [])
            self.assertEqual(group_text_highlights(page, [top, column]), [])

    def test_grouped_pdf_and_handwriting_cards_preserve_mark_geometry(self):
        item, bundle = fixture()
        layer = next(name for name in bundle if name.endswith('.rm'))
        page_id = layer.rsplit('/', 1)[1].removesuffix('.rm')
        blocks = [block for block in read_blocks(BytesIO(bundle[layer]))
                  if not isinstance(block, (SceneGlyphItemBlock, SceneLineItemBlock))]
        previous = CrdtId(0, 0)

        def append(block_type, value):
            nonlocal previous
            identity = CrdtId(1, 200 + len(blocks))
            blocks.append(block_type(CrdtId(0, 11), CrdtSequenceItem(identity, previous, CrdtId(0, 0), 0, value)))
            previous = identity

        with letter_page() as source:
            bundle[item.id + '.pdf'] = source.tobytes()
            for index in range(3):
                mark = line_mark(source[0], str(index), index)
                rect = mark.rects[0]
                boxes = [Rectangle((rect.x0 - 300) / .3177, rect.y0 / .3177,
                                   rect.width / .3177, rect.height / .3177)]
                append(SceneGlyphItemBlock, GlyphRange(None, mark.length, mark.text, PenColor.YELLOW, boxes))

        for x in (440, 463, 700):
            points = [Point(x, 600, 0, 0, 16, 90), Point(x + 10, 623, 0, 0, 16, 90)]
            append(SceneLineItemBlock, Line(PenColor.BLUE, Pen.FINELINER_2, points, 2, 0))
        stream = BytesIO()
        write_blocks(stream, blocks, options={'version': '3.1'})
        bundle[layer] = stream.getvalue()

        def rendered(**options):
            return fitz.open(stream=annotated_pdf(item, bundle, **options), filetype='pdf')

        with rendered() as plain, rendered(group_highlights=True, handwriting_cards=True) as combined:
            first, second = plain[0], combined[0]
            original = list(first.annots(types=[fitz.PDF_ANNOT_HIGHLIGHT]))
            [grouped] = second.annots(types=[fitz.PDF_ANNOT_HIGHLIGHT])
            self.assertEqual(grouped.vertices, [vertex for mark in original for vertex in mark.vertices])
            self.assertEqual(grouped.info['id'], original[0].info['id'])
            self.assertEqual(json.loads(combined.xref_get_key(grouped.xref, 'RMMembers')[1]),
                             [mark.info['id'] for mark in original])
            self.assertEqual([mark.vertices for mark in first.annots(types=[fitz.PDF_ANNOT_INK])],
                             [mark.vertices for mark in second.annots(types=[fitz.PDF_ANNOT_INK])])
            [card] = second.annots(types=[fitz.PDF_ANNOT_SQUARE])
            self.assertEqual(card.info['content'], 'Handwritten note')
            self.assertIn(page_id, card.info['id'])
            self.assertRegex(combined.xref_get_key(card.xref, 'Zotero:Key')[1], r'^[23456789A-NP-Z]{8}$')
            self.assertEqual(card.opacity, 0)

            allowed = [mark.info['id'] for mark in original[:2]]
            with rendered(group_highlights=True, approved_highlight_groups=[allowed]) as repaired:
                page = repaired[0]
                merged, separate = list(page.annots(types=[fitz.PDF_ANNOT_HIGHLIGHT]))
                self.assertEqual(merged.vertices, original[0].vertices + original[1].vertices)
                self.assertEqual(separate.vertices, original[2].vertices)

            overlap = [mark.info['id'] for mark in original[1:]]
            with rendered(approved_highlight_groups=[allowed, overlap]) as conflicting:
                self.assertEqual([mark.vertices for mark in conflicting[0].annots(types=[fitz.PDF_ANNOT_HIGHLIGHT])],
                                 [mark.vertices for mark in original])

        with rendered() as plain, rendered(handwriting_cards=True) as with_cards:
            self.assertEqual(plain[0].get_pixmap().samples, with_cards[0].get_pixmap().samples)
