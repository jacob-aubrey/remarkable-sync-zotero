"""Turn downloaded PDF stroke layers into a separate, annotated PDF.

The original PDF and cloud data are never rewritten. v6 scenes are read with
rmscene. Unknown page formats fail closed instead of dropping annotations.
"""
from io import BytesIO
import json
import math
import re
from collections import Counter

import pymupdf as fitz
from .remarkable_cloud import trace_event
from .remarkable_marks import TextMark, group_text_highlights, handwriting_regions, zotero_card_key


RENDER_REVISION = 6


class UnsupportedAnnotations(ValueError):
    pass


def page_map(content, pdf_pages):
    if "cPages" in content:
        page_collection = content["cPages"]
        pages = page_collection.get("pages") if isinstance(page_collection, dict) else None
        if not isinstance(pages, list):
            raise UnsupportedAnnotations("Unsupported PDF page map")
        if any(not isinstance(p, dict) for p in pages):
            raise UnsupportedAnnotations("Invalid PDF page record")
        pages = [p for p in pages if not p.get("deleted", {}).get("value", False)]
        if any("idx" in p for p in pages):
            # Partial ordering data must not silently change source page positions.
            indexes = [p.get("idx", {}).get("value") if isinstance(p.get("idx"), dict) else None for p in pages]
            if any(not isinstance(index, str) or not index for index in indexes) or len(set(indexes)) != len(indexes):
                raise UnsupportedAnnotations("Incomplete or duplicate PDF page ordering")
            pages = sorted(pages, key=lambda p: p["idx"]["value"])
        result = [(p["id"], p.get("redir", {}).get("value", -1)) for p in pages]
    else:
        ids = content.get("pages", [])
        redirects = content.get("redirectionPageMap")
        if not ids:
            raise UnsupportedAnnotations("PDF has no page identifiers")
        if redirects is not None and len(redirects) != len(ids):
            raise UnsupportedAnnotations("Incomplete PDF page redirection map")
        result = [(pid, redirects[i] if redirects is not None else i) for i, pid in enumerate(ids)]
    if not result or any(not isinstance(pid, str) or not pid for pid, _ in result):
        raise UnsupportedAnnotations("Empty or invalid PDF page identifiers")
    if len({p[0] for p in result}) != len(result):
        raise UnsupportedAnnotations("Empty or duplicate PDF page identifiers")
    for _, index in result:
        if type(index) is not int or index < -1 or index >= pdf_pages:
            raise UnsupportedAnnotations("Invalid source PDF page reference")
    return result


COLORS = {0: (0, 0, 0), 1: (.5, .5, .5), 2: (1, 1, 1), 3: (1, .84, 0),
          4: (0, .63, 0), 5: (1, .41, .71), 6: (.25, .41, .88), 7: (.88, 0, 0),
          8: (.63, .63, .63), 9: (1, .84, 0), 10: (0, .75, 0),
          11: (0, .81, .82), 12: (1, 0, 1), 13: (1, .84, 0)}


def color(item):
    rgba = getattr(item, "color_rgba", None)
    # Older indexed highlights contain a black RGBA placeholder. Color 9 uses
    # RGBA as its actual color; legacy yellow/pink still use their palette index.
    if rgba and (int(item.color) == 9 or any(rgba[:3])):
        return tuple(v / 255 for v in rgba[:3])
    if int(item.color) not in COLORS:
        raise UnsupportedAnnotations("Unknown Paper Pro pen color")
    return COLORS[int(item.color)]


def scene(data):
    from rmscene import read_blocks
    from rmscene.scene_stream import (build_tree, UnreadableBlock, SceneItemBlock,
                                     SceneTextItemBlock, SceneTombstoneItemBlock)
    from rmscene.scene_tree import SceneTree
    if b"version=6" not in data[:43]:
        raise UnsupportedAnnotations("This annotation layer is not a supported v6 scene")
    blocks = list(read_blocks(BytesIO(data)))
    if any(isinstance(block, UnreadableBlock) for block in blocks):
        raise UnsupportedAnnotations("The PDF contains annotation blocks the reader cannot interpret")
    # Scene metadata extensions are retained in the source archive. Unknown
    # annotation fields or skipped items could lose marks, so require review.
    if any(isinstance(block, SceneItemBlock) and (block.extra_data or block.extra_value_data) for block in blocks):
        raise UnsupportedAnnotations("The PDF contains newer annotation fields the reader cannot interpret")
    if any(isinstance(block, SceneTextItemBlock) for block in blocks):
        raise UnsupportedAnnotations("The PDF contains annotation items the renderer cannot interpret")
    blocks = resolved_deletions(blocks)
    tree = SceneTree()
    build_tree(tree, blocks)
    return tree


def resolved_deletions(blocks):
    """Adapt parsed CRDT deletion runs before rmscene builds its scene tree.

    rmscene 0.8 parses tombstones but build_tree omits them. Use its existing
    expand_text_item helper for the same compressed ID-run representation. The
    deleted placeholders retain ordering links and never become visible marks.
    Raw source blocks are not modified or discarded from retained archives.
    """
    from dataclasses import replace
    from rmscene.scene_stream import SceneItemBlock, SceneLineItemBlock, SceneTombstoneItemBlock
    from rmscene.text import expand_text_item
    removed = {}
    for block in blocks:
        if not isinstance(block, SceneItemBlock):
            continue
        length = block.item.deleted_length
        if isinstance(block, SceneTombstoneItemBlock) and (length <= 0 or block.item.value is not None):
            raise UnsupportedAnnotations("An invalid tablet deletion record requires review")
        if length <= 0:
            continue
        if length > 100_000:
            raise UnsupportedAnnotations("A tablet deletion run exceeds the supported scene budget")
        for item in expand_text_item(replace(block.item, value="")):
            identity = (block.parent_id, item.item_id)
            placeholder = SceneLineItemBlock(block.parent_id, replace(item, value=None))
            if identity in removed and removed[identity] != placeholder:
                raise UnsupportedAnnotations("Overlapping tablet deletion runs require review")
            removed[identity] = placeholder
    if not removed:
        return blocks
    result = [block for block in blocks if not isinstance(block, SceneItemBlock)
              or ((block.parent_id, block.item.item_id) not in removed and not block.item.deleted_length)]
    result.extend(removed.values())
    return result


def visible_items(group):
    from rmscene.scene_items import Group
    if not group.visible.value:
        return
    if group.anchor_id is not None:
        raise UnsupportedAnnotations("Text-anchored strokes require an additional layout adapter")
    sequence = {node.item_id: node for node in group.children.sequence_items()}
    for item_id in group.children:
        node = sequence[item_id]
        if node.deleted_length or node.value is None:
            continue
        if isinstance(node.value, Group):
            yield from visible_items(node.value)
        else:
            yield str(node.item_id), node.value


def stamp_id(document, annot, value):
    # Stable scene IDs keep annotation history stable across cloud updates.
    document.xref_set_key(annot.xref, "NM", fitz.get_pdf_str(value))


def _scene_transform(page, tree):
    paper_size = getattr(tree.scene_info, "paper_size", None) or (1404, 1872)
    scale = {(1404, 1872): .3177, (1620, 2160): .3144}.get(tuple(paper_size))
    if scale is None:
        raise UnsupportedAnnotations(f"Unsupported annotation coordinate grid: {paper_size}")
    width, derotation = page.rect.width, page.derotation_matrix

    def point(x, y):
        if not math.isfinite(x) or not math.isfinite(y):
            raise UnsupportedAnnotations("Invalid stroke coordinates")
        return fitz.Point(x * scale + width / 2, y * scale) * derotation

    return point, scale


def _glyph_rect(box, point):
    return fitz.Rect(point(box.x, box.y), point(box.x + box.w, box.y + box.h)).normalize()


def _highlight_groups(page, items, page_id, point, approved=None):
    from rmscene.scene_items import GlyphRange
    marks = [TextMark(f"remarkable-{page_id}-{identity}",
                      tuple(_glyph_rect(box, point) for box in item.rectangles),
                      item.text, color(item), item.start, item.length)
             for identity, item in items if isinstance(item, GlyphRange)]
    if approved is None:
        return group_text_highlights(page, marks)
    by_id = {mark.id: mark for mark in marks}
    candidates = [group for members in dict.fromkeys(map(tuple, approved))
                  if all(identity in by_id for identity in members)
                  for group in group_text_highlights(page, [by_id[identity] for identity in members])
                  if group.ids == members]
    memberships = Counter(identity for group in candidates for identity in group.ids)
    return [group for group in candidates if all(memberships[identity] == 1 for identity in group.ids)]


def scene_highlight_groups(page, tree, page_id):
    """Preview groups on the original page without rendering or writing a PDF."""
    point, _ = _scene_transform(page, tree)
    return _highlight_groups(page, visible_items(tree.root), page_id, point)


def apply_scene(document, page, page_id, tree, *, expand_margins=True,
                group_highlights=False, handwriting_document=None, approved_highlight_groups=()):
    from rmscene.scene_items import Line, GlyphRange, Pen
    project, scale = _scene_transform(page, tree)
    bounds = page.rect * page.derotation_matrix
    offset = fitz.Point(0, 0)

    def require_visible(rect):
        if (rect & bounds).is_empty:
            raise UnsupportedAnnotations("Tablet annotations fall outside the visible PDF page; tablet margins or coordinate calibration need review")

    def point(x, y):
        return project(x, y) + offset

    items = list(visible_items(tree.root))
    groups = (_highlight_groups(page, items, page_id, project, approved_highlight_groups or None)
              if group_highlights or approved_highlight_groups else [])
    grouped = {group.ids[0]: group for group in groups}
    joined = {identity for group in groups for identity in group.ids[1:]}
    strokes = []
    text = ""
    if tree.root_text:
        from rmscene.text import TextDocument
        text = "\n".join(str(p) for p in TextDocument.from_scene_item(tree.root_text).contents).strip()
    extent = fitz.Rect(bounds)
    for _, item in items:
        if isinstance(item, GlyphRange):
            for box in item.rectangles:
                rect = fitz.Rect(point(box.x, box.y), point(box.x + box.w, box.y + box.h))
                rect.normalize()
                padding = max(2, rect.height / 4)
                extent |= fitz.Rect(rect.x0 - padding, rect.y0 - padding,
                                    rect.x1 + padding, rect.y1 + padding)
        elif isinstance(item, Line):
            for pen_point in item.points:
                position = point(pen_point.x, pen_point.y)
                # Include the stroke's appearance, rather than only its centerline.
                extent |= fitz.Rect(position.x - 8, position.y - 8, position.x + 8, position.y + 8)
    if text:
        anchor = point(tree.root_text.pos_x, tree.root_text.pos_y)
        extent |= fitz.Rect(anchor.x, anchor.y, anchor.x + 20, anchor.y + 20)
    left, top, right, bottom = (math.ceil(max(0, value)) for value in
                               (bounds.x0 - extent.x0, bounds.y0 - extent.y0,
                                extent.x1 - bounds.x1, extent.y1 - bounds.y1))
    if left + right > bounds.width or top + bottom > bounds.height:
        raise UnsupportedAnnotations("Tablet annotations fall outside the supported PDF margin area; coordinate calibration needs review")
    if not expand_margins:
        # The combined reader first compares annotations in their original
        # coordinates, then expands the finished page once for both sources.
        # Use the validated planned canvas when checking visibility, so marks
        # wholly within an added margin remain available for that final step.
        bounds = extent
    elif any((left, top, right, bottom)):
        if page.rotation or page.cropbox != page.mediabox:
            raise UnsupportedAnnotations("Tablet margins on a rotated or cropped PDF page need an additional layout adapter")
        # Extend only the converted copy. PDF content and existing annotations
        # shift together; scene coordinates receive the same left/top offset.
        media = page.mediabox
        page.set_mediabox(fitz.Rect(media.x0 - left, media.y0 - bottom,
                                  media.x1 + right, media.y1 + top))
        offset = fitz.Point(left, top)
        bounds = page.rect

    for iid, item in items:
        identity = f"remarkable-{page_id}-{iid}"
        annot = None
        if isinstance(item, GlyphRange):
            if identity in joined:
                continue
            rects = []
            for box in item.rectangles:
                rect = fitz.Rect(point(box.x, box.y), point(box.x + box.w, box.y + box.h))
                rect.normalize()
                if rect.is_empty:
                    raise UnsupportedAnnotations("Invalid text highlight rectangle")
                rects.append(rect)
            if not rects:
                raise UnsupportedAnnotations("A text highlight has no page region")
            group = grouped.get(identity)
            if group:
                rects = [fitz.Rect(rect.tl + offset, rect.br + offset) for rect in group.rects]
            annot = page.add_highlight_annot(rects)
            annot.set_info(subject="reMarkable highlight")
            # Retain the selected quote without creating a viewer comment popup.
            document.xref_set_key(annot.xref, "RMQuote", fitz.get_pdf_str(group.text if group else item.text))
            if group:
                document.xref_set_key(annot.xref, "RMMembers", fitz.get_pdf_str(json.dumps(group.ids)))
            # Dark highlight colors must remain translucent over printed text.
            annot.set_opacity(.35)
        elif isinstance(item, Line):
            if item.tool in {Pen.ERASER, Pen.ERASER_AREA}:
                raise UnsupportedAnnotations("An unresolved eraser stroke requires review")
            if not item.points:
                continue
            points = [point(p.x, p.y) for p in item.points]
            if len(points) == 1:
                points.append(points[0] + (.1, .1))
            annot = page.add_ink_annot([[tuple(p) for p in points]])
            width = sum(p.width for p in item.points) / len(points)
            width = max(.5, min(width * .8, 5)) * scale
            if Pen.is_highlighter(item.tool):
                width = max(10, min(sum(p.width for p in item.points) / len(points) * 2, 40)) * scale
                annot.set_opacity(.35)
                annot.set_blendmode("Multiply")
            annot.set_border(width=width)
            annot.set_info(subject="reMarkable pen")
            if handwriting_document and not Pen.is_highlighter(item.tool):
                radius = width / 2
                strokes.append((iid, fitz.Rect(min(p.x for p in points) - radius, min(p.y for p in points) - radius,
                                               max(p.x for p in points) + radius, max(p.y for p in points) + radius)))
        else:
            raise UnsupportedAnnotations("Unsupported annotation scene item")
        # Partially visible strokes remain intact; wholly invisible marks would
        # otherwise be skipped by the PDF extractor and disappear from Notes.
        require_visible(annot.rect)
        annot.set_colors(stroke=color(item))
        stamp_id(document, annot, identity)
        annot.update()
    for iid, region in handwriting_regions(strokes, page.rect * page.derotation_matrix):
        anchor = page.add_rect_annot(region)
        anchor.set_border(width=0)
        anchor.set_info(content="Handwritten note", subject="reMarkable handwriting")
        anchor.update(opacity=0)
        identity = f"remarkable-handwriting-{handwriting_document}-{page_id}-{iid}"
        stamp_id(document, anchor, identity)
        document.xref_set_key(anchor.xref, "Zotero:Key", fitz.get_pdf_str(zotero_card_key(identity)))
    if text:
        anchor = point(tree.root_text.pos_x, tree.root_text.pos_y)
        if not bounds.contains(anchor):
            raise UnsupportedAnnotations("A tablet text note falls outside the visible PDF page; tablet margins or coordinate calibration need review")
        annot = page.add_text_annot(anchor, text)
        annot.set_info(subject="reMarkable typed note")
        stamp_id(document, annot, "remarkable-" + page_id + "-text")
        annot.update()


def restore_navigation(original, output, pages, cancelled=lambda: False, *, diagnostic=None):
    """Rebuild destinations after page order, deletions, and added note pages."""
    destinations = {}
    unresolved_links = unresolved_bookmarks = 0
    for index, (_, source_index) in enumerate(pages):
        if source_index >= 0:
            destinations.setdefault(source_index, index)

    def native_matrix(page):
        # MuPDF returns navigation in displayed page coordinates. In 1.28.2,
        # transformation_matrix omits the crop offset on rotated pages, while
        # insert_link uses that matrix. Derive the reader's actual transform
        # from its effective CropBox, then use the insertion API's own matrix.
        media, crop = page.mediabox, page.cropbox
        visible = fitz.Rect(crop.x0, media.y1 - crop.y1, crop.x1, media.y1 - crop.y0) & media
        rotation = {0: fitz.Matrix(1, 0, 0, 1, 0, 0),
                    90: fitz.Matrix(0, 1, -1, 0, visible.height, 0),
                    180: fitz.Matrix(-1, 0, 0, -1, visible.width, visible.height),
                    270: fitz.Matrix(0, -1, 1, 0, 0, visible.width)}[page.rotation]
        return ~(fitz.Matrix(1, 0, 0, -1, -visible.x0, visible.y1) * rotation)

    def inserted_position(value, source_index, index):
        return value * native_matrix(original[source_index]) * output[index].transformation_matrix

    def named_target(source_xref, fallback):
        # Link dictionaries expose URI-escaped names; read the decoded native
        # PDF primitive to preserve Unicode and literal backslashes exactly.
        kind, value = original.xref_get_key(source_xref, 'A/D') if source_xref else ('null', 'null')
        if kind == 'null' and source_xref:
            kind, value = original.xref_get_key(source_xref, 'Dest')
        if kind == 'string':
            return fitz.get_pdf_str(value)
        if kind == 'name':
            return value
        return fitz.get_pdf_str(fallback)

    for index, (_, source_index) in enumerate(pages):
        if cancelled():
            raise InterruptedError("reMarkable PDF conversion stopped")
        if source_index < 0:
            continue
        for source_link in original[source_index].get_links():
            link = {key: value for key, value in source_link.items() if key not in {"xref", "id"}}
            unresolved_target = None
            if link.get('kind') == fitz.LINK_NAMED and 'nameddest' in link:
                if link.get('page', -1) < 0:
                    # Keep a source's already unresolved target verbatim. It
                    # supplies no destination to guess and must not block ink.
                    unresolved_links += 1
                    unresolved_target = link['nameddest']
                else:
                    # Page copying does not copy the document-level Names tree.
                    # Named targets use native PDF coordinates, unlike GOTO.
                    link.pop('nameddest', None)
                    link['kind'] = fitz.LINK_GOTO
                    link['to'] = fitz.Point(link.get('to', (0, 0))) * ~native_matrix(original[link['page']])
            link["from"] = inserted_position(fitz.Rect(link["from"]), source_index, index)
            if link.get("kind") == fitz.LINK_GOTO:
                target = link.get("page", -1)
                if target not in destinations:
                    continue  # A deleted destination must not point at another paper page.
                link["page"] = destinations[target]
                link["to"] = inserted_position(fitz.Point(link.get("to", (0, 0))), target, link["page"])
            page = output[index]
            if unresolved_target is None:
                page.insert_link(link)
            else:
                # insert_link's named-target template interpolates a literal
                # PDF string without escaping. Set the original target with
                # the library's PDF-string encoder, including Unicode/parens.
                before = {row[0] for row in page.annot_xrefs() if row[1] == fitz.PDF_ANNOT_LINK}
                page.insert_link({**link, 'nameddest': 'notable-unresolved-target'})
                added = {row[0] for row in page.annot_xrefs() if row[1] == fitz.PDF_ANNOT_LINK} - before
                if len(added) != 1:
                    raise UnsupportedAnnotations('A named source link could not be retained safely.')
                source_xref = source_link.get('xref')
                action = original.xref_get_key(source_xref, 'A/S') if source_xref else ('null', 'null')
                if action == ('name', '/Named'):
                    kind, name = original.xref_get_key(source_xref, 'A/N')
                    if kind != 'name':
                        raise UnsupportedAnnotations('A named source action could not be retained safely.')
                    value = f'<</S/Named/N{name}>>'
                else:
                    value = f'<</S/GoTo/D{named_target(source_xref, unresolved_target)}>>'
                output.xref_set_key(added.pop(), 'A', value)
    toc, native_destinations = [], []
    for level, title, page, destination in original.get_toc(simple=False):
        if cancelled():
            raise InterruptedError("reMarkable PDF conversion stopped")
        destination = dict(destination)
        unresolved_name = None
        if destination.get('kind') == fitz.LINK_NAMED and 'nameddest' in destination:
            if page < 1:
                unresolved_bookmarks += 1
                unresolved_name = destination['nameddest']
            else:
                destination.pop('nameddest', None)
                destination['kind'] = fitz.LINK_GOTO
                destination['to'] = fitz.Point(destination.get('to', (0, 0))) * ~native_matrix(original[page - 1])
        source_xref = destination.pop("xref", None)
        native_destination = (source_xref, None, unresolved_name, None) if unresolved_name is not None else None
        if destination.get("kind") == fitz.LINK_GOTO:
            source_index = page - 1
            if source_index not in destinations:
                continue
            page = destinations[source_index] + 1
            destination["page"] = page - 1
            point = fitz.Point(destination.get("to", (0, 0))) * native_matrix(original[source_index])
            native_destination = (source_xref, page - 1, point, destination.get("zoom", 0))
        # Removing a parent destination can otherwise leave an invalid level gap.
        level = min(level, toc[-1][0] + 1 if toc else 1)
        toc.append([level, title, page, destination])
        native_destinations.append(native_destination)
    if toc:
        output.set_toc(toc)
        outline_xrefs = output.get_outline_xrefs()
        # set_toc's crop/rotation round trip drifts on the installed 1.28.2.
        # Keep its hierarchy/style handling, then restore native PDF targets.
        # Page copies keep native content coordinates even when margins grow.
        for index, destination in enumerate(native_destinations):
            if destination is None:
                continue
            source_xref, page, point, zoom = destination
            if page is None:
                # set_toc cannot resolve a name absent from the source tree.
                # Retain the original named target without inventing a page.
                output.xref_set_key(outline_xrefs[index], 'A', 'null')
                output.xref_set_key(outline_xrefs[index], 'Dest', named_target(source_xref, point))
                continue
            kind, value = original.xref_get_key(source_xref, "A/D") if source_xref else ("null", "null")
            if kind == "null" and source_xref:
                kind, value = original.xref_get_key(source_xref, "Dest")
            array = re.fullmatch(r"\[\s*\d+\s+\d+\s+R\s+(.+)\]", value, re.S) if kind == "array" else None
            target = array[1].strip() if array else f"/XYZ {point.x:.8f} {point.y:.8f} {zoom}"
            output.xref_set_key(outline_xrefs[index], "A",
                               f"<</S/GoTo/D[{output.page_xref(page)} 0 R {target}]>>")
    if unresolved_links or unresolved_bookmarks:
        trace_event(diagnostic, 'navigation_preserved_unresolved', links=unresolved_links,
                    bookmarks=unresolved_bookmarks)


def _repair_copy_source(data, cancelled):
    """Rebuild broken object references only when all visible content survives."""
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(BytesIO(data))
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    stream = BytesIO()
    writer.write(stream)
    repaired = stream.getvalue()
    with fitz.open(stream=data, filetype='pdf') as before, fitz.open(stream=repaired, filetype='pdf') as after:
        if (len(before) != len(after) or before.get_toc() != after.get_toc()
                or before.get_page_labels() != after.get_page_labels()
                or before.embfile_names() != after.embfile_names()):
            raise UnsupportedAnnotations('The source PDF needs repair; its navigation could not be preserved.')
        for index in range(len(before)):
            if cancelled():
                raise InterruptedError('reMarkable PDF conversion stopped')
            left, right = before[index], after[index]
            if (left.rect != right.rect or left.mediabox != right.mediabox or left.cropbox != right.cropbox
                    or left.rotation != right.rotation or list(left.widgets() or []) or list(right.widgets() or [])):
                raise UnsupportedAnnotations('The source PDF needs repair; its page geometry or forms require review.')
            links = lambda page: [{key: str(value) for key, value in link.items() if key not in {'xref', 'id'}} for link in page.get_links()]
            annotations = lambda page: [(a.type, tuple(a.rect), a.info, a.vertices, a.colors, a.opacity) for a in page.annots() or []]
            if links(left) != links(right) or annotations(left) != annotations(right):
                raise UnsupportedAnnotations('The source PDF needs repair; its links or annotations require review.')
            left_image, right_image = left.get_pixmap(alpha=False), right.get_pixmap(alpha=False)
            if (left_image.width, left_image.height, left_image.samples) != (right_image.width, right_image.height, right_image.samples):
                raise UnsupportedAnnotations('The source PDF needs repair; rebuilding it changed its appearance.')
        for name in before.embfile_names():
            if before.embfile_get(name) != after.embfile_get(name):
                raise UnsupportedAnnotations('The source PDF needs repair; an embedded file changed.')
    return repaired


def annotated_pdf(item, bundle, cancelled=lambda: False, *, expand_margins=True, diagnostic=None,
                  group_highlights=False, handwriting_cards=False, approved_highlight_groups=()):
    options = dict(expand_margins=expand_margins, diagnostic=diagnostic,
                   group_highlights=group_highlights, handwriting_cards=handwriting_cards,
                   approved_highlight_groups=approved_highlight_groups)
    try:
        return _annotated_pdf(item, bundle, cancelled, **options)
    except RuntimeError as error:
        if 'source object number out of range' not in str(error):
            raise
        # Some readable publisher PDFs contain dangling, unused references.
        # Keep the raw original and use the existing pypdf dependency to rebuild
        # a rendering source. Verify every page before applying any tablet ink.
        try:
            repaired = _repair_copy_source(bundle[item.id + '.pdf'], cancelled)
            return _annotated_pdf(item, {**bundle, item.id + '.pdf': repaired}, cancelled,
                                  **options)
        except InterruptedError:
            raise
        except (ValueError, RuntimeError, KeyError) as repair_error:
            raise UnsupportedAnnotations('The source PDF could not be copied safely. Its original and tablet notes were retained for repair.') from repair_error


def _annotated_pdf(item, bundle, cancelled=lambda: False, *, expand_margins=True, diagnostic=None,
                   group_highlights=False, handwriting_cards=False, approved_highlight_groups=()):
    content = json.loads(bundle[item.id + ".content"])
    if content.get("fileType") != "pdf":
        raise UnsupportedAnnotations("Only imported PDFs are supported")
    with fitz.open(stream=bundle[item.id + ".pdf"], filetype="pdf") as original, fitz.open() as output:
        if original.needs_pass:
            raise UnsupportedAnnotations("The source PDF is password protected")
        pages = page_map(content, len(original))
        trace_event(diagnostic, 'render_pdf_started', tablet_key=item.id, pages=len(pages))
        last_copy = max((index for index, (_, source) in enumerate(pages) if source >= 0), default=-1)
        # Tombstoned page sources remain in cloud bundles for history. They
        # are known sources, but must never be rendered as live pages.
        records = content.get("cPages", {}).get("pages", []) if isinstance(content.get("cPages"), dict) else []
        historical_ids = {row.get("id") for row in records if isinstance(row, dict) and row.get("deleted", {}).get("value", False)}
        known_layers = {item.id + "/" + pid + ".rm" for pid, _ in pages}
        known_layers.update(item.id + "/" + pid + ".rm" for pid in historical_ids if isinstance(pid, str))
        if any(name.endswith(".rm") and name not in known_layers for name in bundle):
            raise UnsupportedAnnotations("Annotation layers exist outside the PDF's page map")
        known_highlights = {item.id + ".highlights/" + pid + ".json" for pid, _ in pages}
        known_highlights.update(item.id + ".highlights/" + pid + ".json" for pid in historical_ids if isinstance(pid, str))
        if any(".highlights/" in name and name.endswith(".json") and name not in known_highlights for name in bundle):
            raise UnsupportedAnnotations("Highlight data exists outside the PDF's page map")
        labels = []
        for index, (pid, source_index) in enumerate(pages):
            if cancelled():
                raise InterruptedError("reMarkable PDF conversion stopped")
            data = bundle.get(item.id + "/" + pid + ".rm")
            trace_event(diagnostic, 'render_page_started', tablet_key=item.id, page=index + 1, total=len(pages))
            tree = scene(data) if data else None
            if source_index >= 0:
                # Retain MuPDF's graft map between copies from this source.
                # Re-copying shared resources makes later global deduplication
                # expensive and can hold the GIL long enough to lose heartbeats.
                output.insert_pdf(original, from_page=source_index, to_page=source_index, links=False,
                                  final=int(index == last_copy))
                label = original[source_index].get_label() or str(source_index + 1)
            else:
                size = getattr(tree.scene_info, "paper_size", None) if tree else None
                w, h = size or (1404, 1872)
                scale = .3144 if (w, h) == (1620, 2160) else .3177
                output.new_page(width=w * scale, height=h * scale)
                label = f"Added {index + 1}"
            labels.append({"startpage": index, "prefix": label, "style": ""})
            page = output[index]
            if tree:
                apply_scene(output, page, pid, tree, expand_margins=expand_margins, group_highlights=group_highlights,
                            handwriting_document=item.id if handwriting_cards else None,
                            approved_highlight_groups=approved_highlight_groups)
            legacy = bundle.get(item.id + ".highlights/" + pid + ".json")
            if legacy:
                raise UnsupportedAnnotations("Legacy JSON highlights need an additional annotation adapter")
            trace_event(diagnostic, 'render_page_finished', tablet_key=item.id, page=index + 1, total=len(pages))
        if cancelled():
            raise InterruptedError("reMarkable PDF conversion stopped")
        trace_event(diagnostic, 'render_navigation_started', tablet_key=item.id)
        restore_navigation(original, output, pages, cancelled,
                           diagnostic=lambda event, **fields: trace_event(diagnostic, event, tablet_key=item.id, **fields))
        trace_event(diagnostic, 'render_navigation_finished', tablet_key=item.id)
        output.set_page_labels(labels)
        metadata = dict(original.metadata)
        metadata["keywords"] = (metadata.get("keywords", "") + "; reMarkable imported PDF").strip("; ")
        output.set_metadata(metadata)
        if cancelled():
            raise InterruptedError("reMarkable PDF conversion stopped")
        # Compact unused objects without the quadratic object/stream duplicate
        # comparisons at levels 3/4. Shared source resources already use one
        # graft map. Page content stays intact. Rendering revision 6 also
        # invalidates old views whose named navigation targets were lost.
        trace_event(diagnostic, 'render_save_started', tablet_key=item.id, pages=len(pages), xrefs=output.xref_length())
        result = output.tobytes(garbage=2, deflate=True)
        trace_event(diagnostic, 'render_save_finished', tablet_key=item.id, bytes=len(result))
        trace_event(diagnostic, 'render_pdf_finished', tablet_key=item.id, pages=len(pages))
        return result
