"""Extract annotation text and drawing previews without modifying the PDF."""
import base64
from collections import Counter
from contextlib import contextmanager
import heapq
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import quote as encode_annotation_id

import pymupdf as fitz

from .storage import digest_file


GRAPHICS = {"ink", "line", "polyline", "polygon", "square", "circle", "stamp", "caret"}
MARKUP = {"highlight", "underline", "squiggly", "strikeout"}
EXTRACTION_REVISION = 7


def scan_fingerprint(settings):
    return json.dumps({**{k: v for k, v in settings.items() if k != "notebook_style"},
                       "extractor_revision": EXTRACTION_REVISION,
                       "preview_policy": "drawings"}, sort_keys=True)


def signature(path):
    s = Path(path).stat()
    return f"{s.st_size}:{s.st_mtime_ns}"


def annotation_key(annot):
    value = (annot.info or {}).get("id")
    # Reserve the fallback namespace, and escape group delimiters so a single
    # annotation named "a+b" cannot collide with a group containing a and b.
    return (encode_annotation_id(str(value), safe="-._~") if value and not str(value).startswith("xref-")
            else f"xref-{annot.xref}")


def grouping_rect(annot):
    # Ink vertices avoid large PDF appearance padding around each letter.
    if annot.type[1].lower() == "ink" and annot.vertices:
        points = [p for stroke in annot.vertices for p in stroke]
        if points:
            xs, ys = zip(*points)
            return fitz.Rect(min(xs), min(ys), max(xs) + .1, max(ys) + .1)
    return fitz.Rect(annot.rect)


def group_graphics(annotations, mode="Nearby blocks", gap=24):
    """Connected nearby strokes, including adjacent letters and arrow endpoints.

    Compare actual member rectangles, not the expanding cluster bounding box:
    empty space within a large arrow cannot swallow an unrelated note.
    """
    if not annotations:
        return []
    if mode == "Whole page":
        return [annotations]
    rects = [grouping_rect(a) for a in annotations]
    parents = list(range(len(rects)))

    def root(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    # Sweep left to right: distant strokes need no pairwise comparison. Keep
    # actual members in the active set so long arrows retain the same grouping.
    active, ends = {}, []
    vertical_gap = gap if mode == "Nearby blocks" else min(5, gap / 3)
    for i in sorted(range(len(rects)), key=lambda i: (rects[i].x0, i)):
        a = rects[i]
        while ends and ends[0][0] < a.x0:
            _, j = heapq.heappop(ends)
            active.pop(j, None)
        for j in active:
            b = rects[j]
            dx = max(0, a.x0 - b.x1, b.x0 - a.x1)
            dy = max(0, a.y0 - b.y1, b.y0 - a.y1)
            # A smaller vertical reach in line mode keeps adjacent lines separate.
            if dx <= gap and dy <= vertical_gap:
                parents[root(i)] = root(j)
        active[i] = a
        heapq.heappush(ends, (a.x1 + gap, i))
    groups = {}
    for i, annot in enumerate(annotations):
        groups.setdefault(root(i), []).append(annot)
    return sorted(groups.values(), key=lambda g: (min(a.rect.y0 for a in g), min(a.rect.x0 for a in g)))


def drawing_page_bounds(page, groups=None):
    """Recover margin handwriting connected to a visible drawing block."""
    bounds = page.rect * page.derotation_matrix
    if groups is None:
        graphics = [a for a in page.annots() or [] if a.type[1].lower() in GRAPHICS]
        groups = group_graphics(graphics)
    extent = fitz.Rect(bounds)
    for members in groups:
        rect = fitz.Rect(members[0].rect)
        for member in members[1:]:
            rect |= member.rect
        if rect.intersects(bounds):
            extent |= fitz.Rect(rect.x0 - 5, rect.y0 - 5, rect.x1 + 5, rect.y1 + 5)
    # Implausible coordinates must not create enormous raster allocations.
    limits = fitz.Rect(bounds.x0 - bounds.width, bounds.y0 - bounds.height,
                       bounds.x1 + bounds.width, bounds.y1 + bounds.height)
    return extent & limits


@contextmanager
def preview_canvas(page, rect):
    """Render off-page strokes on a temporary copy in source coordinates."""
    bounds = page.rect * page.derotation_matrix
    if bounds.contains(rect):
        yield page, rect
        return
    left, top, right, bottom = (math.ceil(max(0, value)) for value in
                               (bounds.x0 - rect.x0, bounds.y0 - rect.y0,
                                rect.x1 - bounds.x1, rect.y1 - bounds.y1))
    if max(left, right) > bounds.width + 1 or max(top, bottom) > bounds.height + 1:
        raise ValueError("Annotation preview exceeds the supported PDF margin area.")
    with fitz.open() as copy:
        copy.insert_pdf(page.parent, from_page=page.number, to_page=page.number)
        canvas = copy[0]
        media, crop = canvas.mediabox, canvas.cropbox
        canvas.set_mediabox(fitz.Rect(media.x0 - left, media.y0 - bottom,
                                     media.x1 + right, media.y1 + top))
        canvas.set_cropbox(fitz.Rect(crop.x0 - left, crop.y0,
                                    crop.x1 + right, crop.y1 + top + bottom))
        yield canvas, rect + (left, top, left, top)


def padded_rect(page, rect, padding, bounds=None):
    bounds = bounds if bounds is not None else page.rect * page.derotation_matrix
    return fitz.Rect(rect.x0-padding, rect.y0-padding, rect.x1+padding, rect.y1+padding) & bounds


def context_rect(page, rect, padding, lines, bounds=None):
    crop = padded_rect(page, rect, padding, bounds)
    initial = fitz.Rect(crop)
    for line in lines:
        # Expand whole intersecting lines so a crop does not end halfway through a word.
        if line.intersects(initial):
            crop |= line
    return padded_rect(page, crop, 5, bounds)


def snapshot(page, rect):
    # Annotation coordinates are unrotated; rendering clips are in rotated page space.
    with preview_canvas(page, rect) as (canvas, region):
        clip = region * canvas.rotation_matrix
        scale = min(2, 4096 / max(clip.width, clip.height), math.sqrt(8_000_000 / clip.get_area()))
        pix = canvas.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip, annots=True)
        return base64.b64encode(pix.tobytes("png")).decode("ascii")


def ink_only(page, members, rect):
    """Isolate strokes for optional OCR; context image always uses the original PDF."""
    with fitz.open() as canvas:
        bounds = page.rect * page.derotation_matrix
        blank = canvas.new_page(width=bounds.width, height=bounds.height)
        for a in members:
            if a.type[1].lower() != "ink":
                continue
            ink = blank.add_ink_annot(a.vertices)
            ink.set_colors(stroke=a.colors.get("stroke") or (0, 0, 0))
            ink.set_border(width=(a.border or {}).get("width", 1) or 1)
            ink.update()
        return snapshot(blank, rect)


def ocr_image(image):
    executable = shutil.which("tesseract")
    if not executable:
        return "", "OCR unavailable: install Tesseract or leave transcription off."
    try:
        with tempfile.TemporaryDirectory(prefix="pdf-notes-ocr-") as folder:
            path = Path(folder) / "ink.png"
            path.write_bytes(base64.b64decode(image))
            result = subprocess.run([executable, str(path), "stdout", "--psm", "6"],
                                    capture_output=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode:
                return "", "OCR failed; the original annotation image is retained."
            return result.stdout.decode("utf-8", errors="replace").strip(), ""
    except (OSError, subprocess.TimeoutExpired):
        return "", "OCR failed or timed out; the original annotation image is retained."


def highlighted_words(page, annot, ordered_words=None):
    verts = annot.vertices or []
    rects = [fitz.Quad(verts[i:i+4]).rect for i in range(0, len(verts)-3, 4)] or [annot.rect]
    # The extraction loop passes page words once; standalone callers retain the
    # same behavior without holding text from unrelated pages in memory.
    words = ordered_words
    if words is None:
        words = sorted(page.get_text("words"), key=lambda w: (w[5], w[6], w[7]))
    selected, seen = [], set()
    for rect in rects:
        if rect.is_empty:
            continue
        for w in words:
            # Most words are far from this quad. Test numeric bounds first
            # instead of allocating/intersecting a MuPDF rectangle for each.
            x0, y0, x1, y1 = w[:4]
            if x1 <= rect.x0 or x0 >= rect.x1 or y1 <= rect.y0 or y0 >= rect.y1:
                continue
            area = max(0, x1-x0) * max(0, y1-y0)
            overlap = ((min(x1, rect.x1)-max(x0, rect.x0))
                       * (min(y1, rect.y1)-max(y0, rect.y0)))
            if area and overlap / area >= .35:
                identity = tuple(w[:8])
                if identity not in seen:
                    selected.append(w)
                    seen.add(identity)
    return selected


def reading_order(page_words, selected_words, rect):
    if selected_words:
        return list(min(tuple(w[5:8]) for w in selected_words))
    if page_words:
        # Anchor drawings / sticky notes to the nearest actual text, keeping column flow.
        def distance(w):
            box = fitz.Rect(w[:4])
            dx = max(0, box.x0 - rect.x1, rect.x0 - box.x1)
            dy = max(0, box.y0 - rect.y1, rect.y0 - box.y1)
            return (dx*dx + dy*dy, w[5], w[6], w[7])
        return list(min(page_words, key=distance)[5:8])
    return [rect.y0, rect.x0, 0]


def color_label(annot):
    rgb = annot.colors.get("stroke") or annot.colors.get("fill")
    if not rgb or len(rgb) != 3:
        return ""
    palette = {"Yellow": (1, 1, 0), "Green": (0, 1, 0), "Blue": (0, .5, 1),
               "Pink": (1, .4, .7), "Red": (1, 0, 0), "Orange": (1, .6, 0), "Black": (0, 0, 0)}
    return min(palette, key=lambda c: sum((a-b)**2 for a, b in zip(rgb, palette[c])))


def extract(path, settings, cancelled=None):
    before = signature(path)
    digest = digest_file(path)
    entries, warnings = [], []
    with fitz.open(path) as doc:
        if doc.needs_pass:
            raise ValueError("Password-protected PDF: save an unlocked copy for annotation extraction.")
        metadata = dict(doc.metadata or {})
        metadata["pages"] = len(doc)
        # DOI is extracted only when visibly present; authors/year are never guessed.
        first_text = "\n".join(doc[i].get_text() for i in range(min(2, len(doc))))
        doi = re.search(r"\b10\.\d{4,9}/[^\s<>\"]+", first_text, re.IGNORECASE)
        if doi:
            metadata["doi"] = doi.group().rstrip(".,;)")
        for index, page in enumerate(doc):
            if cancelled and cancelled():
                raise InterruptedError("Scan stopped")
            annotations = list(page.annots() or [])
            if not annotations:
                continue
            keys = {a.xref: annotation_key(a) for a in annotations}
            repeated = Counter(keys.values())
            # Some PDF editors copy /NM along with an annotation. Those IDs
            # cannot identify separate notes; xrefs retain every physical mark.
            keys = {a.xref: f"xref-{a.xref}" if repeated[keys[a.xref]] > 1 else keys[a.xref]
                    for a in annotations}
            # One text-only parse serves line bounds and every highlight. The
            # default dictionary flags would unnecessarily decode embedded images.
            textpage = page.get_textpage(flags=fitz.TEXTFLAGS_WORDS)
            lines = [fitz.Rect(line["bbox"])
                     for block in page.get_text("dict", textpage=textpage)["blocks"]
                     for line in block.get("lines", [])]
            # Preserve MuPDF's text-flow order. Sorting by page coordinates
            # interleaves adjacent columns and makes match navigation differ
            # from PDF Find.
            page_words = page.get_text("words", textpage=textpage, sort=False)
            contexts = {}
            label = page.get_label() or ""
            by_xref = {a.xref: a for a in annotations}
            replies = {}
            consumed = set()
            for a in annotations:
                parent = a.irt_xref
                visited = {a.xref}
                while parent in by_xref and parent not in visited:
                    visited.add(parent)
                    next_parent = by_xref[parent].irt_xref
                    if not next_parent:
                        break
                    parent = next_parent
                if parent in by_xref and parent != a.xref and a.type[1].lower() in {"text", "freetext"}:
                    replies.setdefault(parent, []).append((a.info or {}).get("content", ""))
                    consumed.add(a.xref)
            graphics = [a for a in annotations if a.type[1].lower() in GRAPHICS]
            groups = group_graphics(graphics, settings.get("grouping", "Nearby blocks"), settings.get("gap", 24))
            drawing_bounds = drawing_page_bounds(page, groups)
            handled_graphics = {a.xref for a in graphics}
            units = [(g, "drawing") for g in groups]
            units += [([a], a.type[1].lower()) for a in annotations if a.xref not in handled_graphics | consumed]
            for members, kind in units:
                if cancelled and cancelled():
                    raise InterruptedError("Scan stopped")
                a = members[0]
                if kind in {"popup", "link"}:
                    continue
                rect = fitz.Rect(a.rect)
                for member in members[1:]:
                    rect |= member.rect
                # Invisible off-page marks cannot be rendered; keep reading the
                # other annotations instead of rejecting the entire paper.
                bounds = page.rect * page.derotation_matrix
                if (rect & bounds).is_empty:
                    warning = f"Page {index+1}: an annotation outside the visible page was skipped."
                    if warning not in warnings:
                        warnings.append(warning)
                    continue
                comments = []
                for member in members:
                    for value in [(member.info or {}).get("content", "")] + replies.get(member.xref, []):
                        value = value.strip()
                        if value and value not in comments:
                            comments.append(value)
                selected_words = highlighted_words(page, a, page_words) if kind in MARKUP else []
                quote = " ".join(w[4] for w in selected_words)
                if kind in MARKUP and (a.info or {}).get("subject") == "reMarkable highlight":
                    # Keep the tablet quotation separate from genuine PDF
                    # comments. Older imports stored that quotation in Contents.
                    quote_type, tablet_quote = doc.xref_get_key(a.xref, "RMQuote")
                    if quote_type == "string":
                        quote = tablet_quote or quote
                    elif quote_type == "null":
                        legacy_quote = (a.info or {}).get("content", "")
                        quote = legacy_quote or quote
                        comments = [c for c in comments if c != legacy_quote.strip()]
                preview_bounds = drawing_bounds if kind == "drawing" else None
                crop = context_rect(page, rect, settings.get("context_padding", 36), lines, preview_bounds)
                if kind == "drawing":
                    initial_crop = fitz.Rect(crop)
                    complete = fitz.Rect(crop)
                    for group in groups:
                        if any(member.rect.intersects(initial_crop) for member in group):
                            for member in group:
                                complete |= member.rect
                    if complete != crop:
                        crop = context_rect(page, complete, 0, lines, drawing_bounds)
                if kind == "drawing" and settings.get("grouping") == "Whole page":
                    crop = drawing_bounds
                if kind == "drawing" and not drawing_bounds.contains(rect):
                    warnings.append(f"Page {index+1}: a drawing exceeds the supported preview margins; inspect its source PDF.")
                # Navigation always uses the physical position. PDF labels are
                # separate metadata and do not establish journal pagination.
                crop_key = tuple(crop)
                if crop_key not in contexts:
                    # Keep MuPDF's original clipped-text semantics. Reusing a
                    # full-page TextPage here can change partially clipped words.
                    # Follow the PDF text flow down each column, as PDF Find
                    # does. Sorting by coordinates interleaves neighboring
                    # columns and makes surrounding-text hit arrows jump rows.
                    contexts[crop_key] = page.get_text("text", clip=crop, sort=False).strip()
                entry = {"key": f"{index}:" + "+".join(sorted(keys[m.xref] for m in members)),
                         "page": index + 1, "page_count": len(doc), "pdf_label": label,
                         "label": label or str(index+1),
                         "kind": kind, "rect": list(rect), "crop": list(crop), "quote": quote,
                         "order": reading_order(page_words, selected_words, rect),
                         "comment": "\n".join(comments), "color": color_label(a),
                         "created": (a.info or {}).get("creationDate", ""),
                         "modified": (a.info or {}).get("modDate", ""), "ocr": "", "image": "",
                         "context": contexts[crop_key]}
                # Highlights and typed notes keep text and source links. Only
                # drawings need an image to preserve their handwritten content.
                if kind == "drawing":
                    entry["image"] = snapshot(page, crop)
                if kind == "drawing" and settings.get("ocr") and any(m.type[1].lower() == "ink" for m in members):
                    entry["ocr"], warning = ocr_image(ink_only(page, members, padded_rect(page, rect, 8, drawing_bounds)))
                    if warning and warning not in warnings:
                        warnings.append(warning)
                if kind in MARKUP and not quote:
                    warning = f"Page {index+1}: highlighted text unavailable; open the source PDF to view this mark."
                    if warning not in warnings:
                        warnings.append(warning)
                entries.append(entry)
    if signature(path) != before or digest_file(path) != digest:
        raise OSError("PDF changed during extraction; waiting for a stable saved copy.")
    metadata["warnings"] = warnings
    if "reMarkable imported PDF" in metadata.get("keywords", ""):
        warnings.append("reMarkable pen placement uses calibrated coordinates; check a representative page against the tablet.")
    return digest, before, metadata, entries
