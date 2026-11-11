"""Reveal off-page annotations without flattening or moving the PDF's objects.

Call this only on a disposable reading/export document, after source matching
and annotation deduplication. Native PDF coordinates remain unchanged; moving
the page boxes changes only the origin from which viewers display those objects.
"""
import math
import re

import pymupdf as fitz


_PADDING = 6
_MAX_PAGE_DIMENSION = 14_400
_NUMBER = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
_REFERENCE = re.compile(r"\s*(\d+)\s+\d+\s+R\s*")
_HIDDEN_FLAGS = fitz.PDF_ANNOT_IS_INVISIBLE | fitz.PDF_ANNOT_IS_HIDDEN | fitz.PDF_ANNOT_IS_NO_VIEW


def _check(cancelled):
    if cancelled and (cancelled.is_set() if hasattr(cancelled, "is_set") else cancelled()):
        raise InterruptedError("PDF margin expansion cancelled")


def _valid(rect):
    return all(math.isfinite(value) for value in rect) and not rect.is_infinite and not rect.is_empty


def _pdf_rect(document, xref):
    kind, value = document.xref_get_key(xref, "Rect")
    seen = set()
    while kind == "xref":
        reference = _REFERENCE.fullmatch(value)
        target = int(reference[1]) if reference else 0
        if not 0 < target < document.xref_length() or target in seen or len(seen) >= 32:
            raise ValueError("An annotation has an invalid rectangle reference; its margins need review.")
        seen.add(target)
        value = document.xref_object(target, compressed=True).strip()
        kind = "xref" if _REFERENCE.fullmatch(value) else "array" if value.startswith("[") else "unknown"
    if kind != "array":
        raise ValueError("An annotation has no usable PDF rectangle; its margins need review.")
    parts = _NUMBER.findall(value)
    if (not value.startswith("[") or not value.endswith("]") or "[" in value[1:-1] or "]" in value[1:-1]
            or len(parts) != 4 or _NUMBER.sub("", value).strip(" []\t\r\n")):
        raise ValueError("An annotation has an unsupported PDF rectangle; its margins need review.")
    rect = fitz.Rect(*(float(part) for part in parts))
    rect.normalize()
    if not _valid(rect):
        raise ValueError("An annotation has invalid coordinates; its margins need review.")
    return rect


def _box_value(rect):
    # PDF decimal syntax does not allow Python's scientific notation.
    return "[" + " ".join(format(value, ".8f").rstrip("0").rstrip(".") or "0" for value in rect) + "]"


def _nonstandard_units(document, page):
    # Some producers put UserUnit on a page-tree parent even though readers
    # differ in whether they inherit it. Treat either placement cautiously.
    xref, visited = page.xref, set()
    while xref and xref not in visited:
        if not 0 < xref < document.xref_length() or len(visited) >= 32:
            return True
        visited.add(xref)
        kind, value = document.xref_get_key(xref, "UserUnit")
        if kind != "null":
            try:
                return kind not in ("int", "real") or float(value) != 1
            except ValueError:
                return True
        kind, value = document.xref_get_key(xref, "Parent")
        if kind != "xref":
            return False
        reference = _REFERENCE.fullmatch(value)
        if not reference:
            return True
        xref = int(reference[1])
    return bool(xref)


def _upright_native_rect(rect, crop, rotation):
    """Compute NoRotate bounds without MuPDF's scaled-page coordinate bug."""
    local = fitz.Rect(rect.x0 - crop.x0, crop.y1 - rect.y1,
                      rect.x1 - crop.x0, crop.y1 - rect.y0)
    matrix = {
        0: fitz.Matrix(1, 0, 0, 1, 0, 0),
        90: fitz.Matrix(0, 1, -1, 0, crop.height, 0),
        180: fitz.Matrix(-1, 0, 0, -1, crop.width, crop.height),
        270: fitz.Matrix(0, -1, 1, 0, 0, crop.width),
    }[rotation]
    anchor = local.tl * matrix
    upright = fitz.Rect(anchor.x, anchor.y, anchor.x + local.width, anchor.y + local.height) * ~matrix
    return fitz.Rect(crop.x0 + upright.x0, crop.y1 - upright.y1,
                     crop.x0 + upright.x1, crop.y1 - upright.y0)


def _clip_original_contents(document, page, crop):
    contents = page.get_contents()
    if not contents:
        return
    prefix = document.get_new_xref()
    document.update_object(prefix, "<<>>")
    values = " ".join(format(value, ".8f").rstrip("0").rstrip(".") or "0"
                      for value in (crop.x0, crop.y0, crop.width, crop.height))
    document.update_stream(prefix, ("q\n" + values + " re W n\n").encode("ascii"))
    suffix = document.get_new_xref()
    document.update_object(suffix, "<<>>")
    document.update_stream(suffix, b"\nQ\n")
    document.xref_set_key(page.xref, "Contents", "[" + " ".join(
        f"{xref} 0 R" for xref in (prefix, *contents, suffix)) + "]")


def expand_annotation_margins(document: fitz.Document, cancelled=None) -> bool:
    """Add white page margins containing every visible native annotation.

    The operation retains page order, rotation, labels, metadata, annotation
    appearances and identities, links, and the original searchable content.
    Existing content is clipped to its old visible page, so expanding a crop
    does not reveal previously hidden article material or printer's marks.
    Hidden/invisible/NoView annotations and link hotspots do not grow pages.

    No-op pages are untouched. Invalid coordinates or an expansion greater
    than one original page in either direction require review instead of
    silently clipping marks or allocating an impractically large canvas.
    Cancellation accepts either a callable or an Event and raises InterruptedError.
    """
    plans = []
    for page in document:
        _check(cancelled)
        media, crop = fitz.Rect(page.mediabox), fitz.Rect(page.cropbox)
        if not _valid(media) or not _valid(crop):
            raise ValueError("The PDF has invalid page bounds; its margins need review.")
        # cropbox uses an unrotated top-left convention, even for rotated pages;
        # mediabox and the PDF /Rect use native bottom-left PDF coordinates.
        original = fitz.Rect(crop.x0, media.y1 - crop.y1, crop.x1, media.y1 - crop.y0) & media
        # A declared CropBox may exceed MediaBox; readers show only their
        # intersection, and annotation coordinates use that effective origin.
        if not _valid(original):
            raise ValueError("The PDF has invalid visible page bounds; its margins need review.")
        nonstandard_units = _nonstandard_units(document, page)
        extent = fitz.Rect(original)
        outside = False
        for annot in page.annots() or ():
            _check(cancelled)
            if annot.flags & _HIDDEN_FLAGS:
                continue
            rect = _pdf_rect(document, annot.xref)
            # NoRotate text icons may have a displayed rectangle differing from
            # their stored /Rect. Include both, without rewriting either one.
            # Derive the upright icon's bounds in native coordinates. MuPDF's
            # annot.rect/rotation_matrix can use the declared CropBox instead
            # of its effective MediaBox intersection on unusual producer PDFs.
            if annot.flags & fitz.PDF_ANNOT_IS_NO_ROTATE:
                rect |= _upright_native_rect(rect, original, page.rotation)
            if not original.contains(rect):
                outside = True
                extent |= fitz.Rect(rect.x0 - _PADDING, rect.y0 - _PADDING,
                                    rect.x1 + _PADDING, rect.y1 + _PADDING)
        if not outside:
            continue
        if nonstandard_units:
            raise ValueError("PDF margins on pages with nonstandard UserUnit scaling need review.")
        left, bottom, right, top = (math.ceil(max(0, value)) for value in (
            original.x0 - extent.x0, original.y0 - extent.y0,
            extent.x1 - original.x1, extent.y1 - original.y1))
        expanded = fitz.Rect(original.x0 - left, original.y0 - bottom,
                             original.x1 + right, original.y1 + top)
        if (max(left, right) > original.width or max(top, bottom) > original.height
                or max(expanded.width, expanded.height) > _MAX_PAGE_DIMENSION):
            raise ValueError("Annotations extend beyond the supported PDF margin area; review is required.")
        grown_media = media | expanded
        if max(grown_media.width, grown_media.height) > _MAX_PAGE_DIMENSION:
            raise ValueError("The expanded PDF page would be too large; its margins need review.")
        plans.append((page.number, original, grown_media, expanded))

    # Validate every page before mutating any page. Sources are never passed to
    # this helper, but failing closed also makes disposable callers predictable.
    for number, original, media, crop in plans:
        _check(cancelled)
        page = document[number]
        _clip_original_contents(document, page, original)
        document.xref_set_key(page.xref, "MediaBox", _box_value(media))
        document.xref_set_key(page.xref, "CropBox", _box_value(crop))
    _check(cancelled)
    return bool(plans)
