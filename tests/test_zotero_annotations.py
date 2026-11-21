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
    def test_pdf_ink_preserves_drawing_widths_on_both_coordinate_grids(self):
        for paper_size, scale in (((1404, 1872), .3177), ((1620, 2160), .3144)):
            with self.subTest(paper_size=paper_size):
                item, bundle = fixture(paper_size=paper_size)
                layer = next(name for name in bundle if name.endswith('.rm'))
                blocks = [block for block in read_blocks(BytesIO(bundle[layer]))
                          if not isinstance(block, SceneLineItemBlock)]
                previous = CrdtId(1, 100)
                for index, width in enumerate((8, 16, 24)):
                    y = 500 + index * 100
                    line = Line(PenColor.BLACK, Pen.FINELINER_2,
                                [Point(x, y, 0, 0, width, 90) for x in (100, 300)], 2, 0)
                    identity = CrdtId(1, 101 + index)
                    blocks.append(SceneLineItemBlock(CrdtId(0, 11),
                                  CrdtSequenceItem(identity, previous, CrdtId(0, 0), 0, line)))
                    previous = identity

                stream = BytesIO()
                write_blocks(stream, blocks, options={'version': '3.3'})
                bundle[layer] = stream.getvalue()

                with fitz.open(stream=annotated_pdf(item, bundle), filetype='pdf') as pdf:
                    page = pdf[0]
                    ink = sorted(page.annots(types=[fitz.PDF_ANNOT_INK]),
                                 key=lambda mark: mark.vertices[0][0][1])
                    self.assertEqual(len(ink), 3)
                    coverage = []
                    for mark, width in zip(ink, (2, 4, 6)):
                        self.assertAlmostEqual(mark.border['width'], width * scale, places=5)
                        (x0, y), (x1, _) = mark.vertices[0]
                        x = (x0 + x1) / 2
                        pixmap = page.get_pixmap(matrix=fitz.Matrix(8, 8), colorspace=fitz.csGRAY,
                                                 clip=fitz.Rect(x - 1, y - 4, x + 1, y + 4))
                        coverage.append(sum(255 - value for value in pixmap.samples))
                    self.assertGreater(coverage[0], 0)
                    self.assertLess(coverage[0], coverage[1])
                    self.assertLess(coverage[1], coverage[2])

    def test_wrapped_text_groups_across_tablet_crlf_gaps_and_ragged_line_ends(self):
        with fitz.open() as doc:
            page = doc.new_page(width=600, height=800)
            page.insert_text((60, 100), ['Short first highlighted line',
                                       'A somewhat longer highlighted second line',
                                       'And the concluding line'], fontsize=11)
            marks = []
            start = 0
            for index in range(3):
                mark = line_mark(page, str(index), index, start=start)
                marks.append(mark)
                start += mark.length + 3  # CR, LF, and a separating space
            [group] = group_text_highlights(page, marks)
            self.assertEqual(group.ids, ('0', '1', '2'))
            self.assertEqual(group.text, ' '.join(mark.text for mark in marks))
            self.assertEqual(group.rects, tuple(rect for mark in marks for rect in mark.rects))

    def test_handwriting_interior_word_is_absorbed_without_combining_separate_notes(self):
        import random
        from note_organizer.remarkable_marks import handwriting_regions
        strokes = [('top', fitz.Rect(10, 10, 70, 20)),
                   ('left', fitz.Rect(10, 20, 20, 80)),
                   ('right', fitz.Rect(60, 20, 70, 80)),
                   ('bottom', fitz.Rect(10, 70, 70, 80)),
                   ('metrics', fitz.Rect(40, 40, 45, 45)),
                   ('separate', fitz.Rect(160, 150, 170, 160))]
        page = fitz.Rect(0, 0, 600, 800)
        expected = sorted(tuple(rect) for _, rect in handwriting_regions(strokes, page))
        self.assertEqual(len(expected), 2)
        self.assertTrue(fitz.Rect(expected[0]).contains(strokes[4][1]))
        rng = random.Random(8)
        for _ in range(25):
            rng.shuffle(strokes)
            self.assertEqual(sorted(tuple(rect) for _, rect in handwriting_regions(strokes, page)), expected)

    def test_handwriting_word_spacing_is_more_forgiving_but_distant_notes_stay_separate(self):
        from note_organizer.remarkable_marks import handwriting_regions
        groups = handwriting_regions([('first', fitz.Rect(10, 10, 20, 25)),
                                      ('next-word', fitz.Rect(34, 12, 60, 28)),
                                      ('following-line', fitz.Rect(12, 44, 50, 58)),
                                      ('other-note', fitz.Rect(110, 12, 150, 28))], fitz.Rect(0, 0, 600, 800))
        self.assertEqual(len(groups), 2)
        self.assertTrue(groups[0][1].contains(fitz.Rect(10, 10, 60, 28)))

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
            self.assertEqual(group_text_highlights(page, [top, line_mark(page, 'gap', 1, first=2)]), [])
            self.assertEqual(group_text_highlights(page, [top, column]), [])

    def test_overlapping_different_color_word_pass_merges_with_whole_sentence(self):
        with letter_page() as doc:
            page = doc[0]
            whole = line_mark(page, 'whole', 0)
            words = [word for word in page.get_text('words') if tuple(word[5:7]) == (0, 0)]
            word_rect = fitz.Rect(words[1][:4])
            accidental = TextMark('accidental', (word_rect,), words[1][4],
                                  (1, .4, .7), whole.start, len(words[1][4]))
            [group] = group_text_highlights(page, [whole, accidental])
            self.assertEqual(set(group.ids), {'whole', 'accidental'})
            self.assertEqual(group.text, whole.text)
            self.assertIn(word_rect, group.rects)

    def test_gap_between_two_passes_is_closed_by_transitive_bridge_highlight(self):
        with letter_page() as doc:
            page = doc[0]
            left = line_mark(page, 'left', 0, first=0, last=2)
            bridge = line_mark(page, 'bridge', 0, first=1, last=3)
            right = line_mark(page, 'right', 0, first=2)
            [group] = group_text_highlights(page, [right, left, bridge])
            self.assertEqual(group.ids, ('left', 'bridge', 'right'))
            self.assertEqual(group.text, 'A highlighted passage continues here')
            self.assertEqual(set(group.rects), set(left.rects + bridge.rects + right.rects))

    def test_wrap_joins_across_pdf_blocks_and_retains_repeated_words(self):
        with fitz.open() as doc:
            page = doc.new_page()
            page.insert_text((60, 100), 'A passage can end with a')
            page.insert_text((63, 118), 'a word that starts the next line')
            page.insert_text((63, 160), 'A separate paragraph')
            a, b, c = (line_mark(page, str(i), 0, block=i) for i in range(3))
            [group] = group_text_highlights(page, [c, b, a])
            self.assertEqual(group.ids, ('0', '1'))
            self.assertEqual(group.text, 'A passage can end with a a word that starts the next line')
            page.set_rotation(90)
            self.assertEqual(group_text_highlights(page, [c, b, a]), [group])

    def test_whitespace_and_freehand_bridges_join_different_colors(self):
        with letter_page() as doc:
            page = doc[0]
            left = line_mark(page, 'left', 0, first=0, last=2)
            right = line_mark(page, 'right', 0, first=2, stroke=(1, .4, .7))
            a, b = left.rects[0], right.rects[0]
            gap = fitz.Rect(a.x1 - .35, a.y0 + 3, b.x0 + .35, a.y1 - 3)
            for freehand in (False, True):
                bridge = TextMark('bridge', (gap,), '', (0, 1, 0), None, 0, freehand)
                [group] = group_text_highlights(page, [right, bridge, left])
                self.assertEqual(group.ids, ('left', 'bridge', 'right'))
                self.assertEqual(group.text, 'A highlighted passage continues here')
                self.assertEqual(group.ink_ids, ('bridge',) if freehand else ())
            self.assertEqual(group_text_highlights(page, [left, right]), [])

    def test_multiline_selection_joins_an_overlay_on_its_middle_line(self):
        with letter_page() as doc:
            page = doc[0]
            marks = [line_mark(page, str(i), i) for i in range(3)]
            text = ' '.join(mark.text for mark in marks)
            entire = TextMark('entire', tuple(mark.rects[0] for mark in marks), text, (1, 1, 0), None, 0)
            middle = line_mark(page, 'middle', 1, first=1, last=4, stroke=(0, 1, 0))
            [group] = group_text_highlights(page, [middle, entire])
            self.assertEqual(group.ids, ('entire', 'middle'))
            self.assertEqual(group.text, text)

    def test_list_label_and_body_fragments_form_one_selection(self):
        with fitz.open() as doc:
            page = doc.new_page()
            for point, text in (((60, 100), '1.'), ((82, 100), 'The first item wraps'),
                                ((82, 113), 'to its next line.'), ((60, 126), '2.'),
                                ((82, 126), 'Numbered item two')):
                page.insert_text(point, text, fontsize=11)
            labels = [line_mark(page, str(index), index) for index in range(5)]
            intro = TextMark('intro', (labels[0].rects[0] | labels[1].rects[0],),
                             '1. The first item wraps', (1, .84, 0), None, 0)
            rest = TextMark('rest', tuple(mark.rects[0] for mark in labels[1:]),
                            'The first item wraps to its next line. 2. Numbered item two', (1, .84, 0), None, 0)
            [group] = group_text_highlights(page, [rest, intro])
            self.assertEqual(group.text, '1. The first item wraps to its next line. 2. Numbered item two')

    def test_column_wrap_requires_consecutive_tablet_offsets(self):
        with fitz.open() as doc:
            page = doc.new_page()
            page.insert_text((55, 600), 'The selected phrase ends', fontsize=11)
            page.insert_text((305, 115), 'in the second column.', fontsize=11)
            left = line_mark(page, 'left', 0, start=10)
            right = line_mark(page, 'right', 0, start=10 + left.length + 2, block=1)
            [group] = group_text_highlights(page, [right, left])
            self.assertEqual(group.ids, ('left', 'right'))
            self.assertEqual(group.text, 'The selected phrase ends in the second column.')
            unrelated = line_mark(page, 'right', 0, start=100, block=1)
            self.assertEqual(group_text_highlights(page, [left, unrelated]), [])

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
            cards = list(second.annots(types=[fitz.PDF_ANNOT_SQUARE]))
            self.assertEqual(len(cards), 2)  # Nearby handwriting, plus the isolated stroke.
            card = cards[0]
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
        with rendered() as plain, rendered(group_highlights=True) as with_group:
            before, grouped = plain[0].get_pixmap(), with_group[0].get_pixmap()
            for x, y in ((65, 93), (65, 109), (65, 124), (30, 30)):
                self.assertEqual(before.pixel(x, y), grouped.pixel(x, y))

    def test_same_color_overlap_is_painted_once_and_different_colors_blend(self):
        from shapely import box
        from note_organizer.remarkable_pdf import _disjoint_quads, _highlight_appearance, _paint_once

        a, b = fitz.Rect(10, 20, 70, 40), fitz.Rect(50, 20, 110, 40)
        self.assertEqual(_disjoint_quads([a, b]), [a, fitz.Rect(70, 20, 110, 40)])
        yellow, pink = (1, .84, 0), (1, .41, .71)
        for second in (yellow, pink):
            with self.subTest(second=second), fitz.open() as pdf:
                page = pdf.new_page(width=130, height=80)
                annotation = page.add_highlight_annot(_disjoint_quads([a, b]))
                covered = {}
                paints = [(stroke, _paint_once(covered, stroke, box(*rect)))
                          for rect, stroke in ((a, yellow), (b, second))]
                _highlight_appearance(pdf, page, annotation, paints)
                with fitz.open(stream=pdf.tobytes(), filetype='pdf') as reopened:
                    pixmap = reopened[0].get_pixmap()
                    left, overlap, right = (pixmap.pixel(x, 30) for x in (25, 60, 90))
                if second == yellow:
                    self.assertEqual(left, overlap)
                    self.assertEqual(right, overlap)
                else:
                    self.assertLess(overlap[1], min(left[1], right[1]))
                    self.assertLess(overlap[2], min(left[2], right[2]))

    def test_every_drawing_tool_gets_a_card_without_changing_page_or_snapped_text(self):
        for tool in Pen:
            if tool in {Pen.ERASER, Pen.ERASER_AREA}:
                continue
            with self.subTest(tool=tool):
                item, bundle = fixture()
                layer = next(name for name in bundle if name.endswith('.rm'))
                blocks = list(read_blocks(BytesIO(bundle[layer])))
                strokes = [block for block in blocks if isinstance(block, SceneLineItemBlock)]
                self.assertTrue(strokes)
                # A single closed stroke surrounding most of a figure/page,
                # larger than both of the previous region-size cutoffs.
                strokes[0].item.value.tool = tool
                strokes[0].item.value.points = [Point(x, y, 0, 0, 16, 90) for x, y in
                    [(-600, 400), (600, 400), (600, 1600), (-600, 1600), (-600, 400)]]
                blocks = [block for block in blocks if not isinstance(block, SceneLineItemBlock) or block is strokes[0]]
                stream = BytesIO(); write_blocks(stream, blocks, options={'version': '3.1'})
                bundle[layer] = stream.getvalue()
                with fitz.open(stream=annotated_pdf(item, bundle), filetype='pdf') as plain, \
                     fitz.open(stream=annotated_pdf(item, bundle, handwriting_cards=True), filetype='pdf') as converted:
                    page, original = converted[0], plain[0]
                    [card] = page.annots(types=[fitz.PDF_ANNOT_SQUARE])
                    [ink] = page.annots(types=[fitz.PDF_ANNOT_INK])
                    self.assertTrue(all(card.rect.contains(point) for point in ink.vertices[0]))
                    self.assertEqual(card.info['content'], 'Handwritten note')
                    self.assertEqual(page.get_pixmap().samples, original.get_pixmap().samples)
                    self.assertEqual([a.vertices for a in page.annots(types=[fitz.PDF_ANNOT_HIGHLIGHT])],
                                     [a.vertices for a in original.annots(types=[fitz.PDF_ANNOT_HIGHLIGHT])])

    def test_single_dot_is_included_in_image_notes(self):
        from note_organizer.remarkable_marks import handwriting_regions
        page = fitz.Rect(0, 0, 600, 800)
        [(identity, region)] = handwriting_regions([('dot', fitz.Rect(40, 40, 41, 41))], page)
        self.assertEqual(identity, 'dot')
        self.assertTrue(region.contains(fitz.Rect(40, 40, 41, 41)))

    def test_actual_stroke_path_distinguishes_diagonal_ink_from_enclosure(self):
        from note_organizer.remarkable_marks import handwriting_regions
        page = fitz.Rect(0, 0, 600, 800)
        box = fitz.Rect(9, 9, 221, 221)
        note = ('note', fitz.Rect(175, 45, 193, 55), [(176, 46), (187, 52), (192, 54)])
        diagonal = ('diagonal', box, [(10, 10), (220, 220)])
        enclosure = ('enclosure', box, [(10, 10), (220, 10), (220, 220), (10, 220), (10, 12)])
        self.assertEqual([key for key, _ in handwriting_regions([diagonal, note], page)], ['diagonal', 'note'])
        self.assertEqual([key for key, _ in handwriting_regions([enclosure, note], page)], ['enclosure'])
