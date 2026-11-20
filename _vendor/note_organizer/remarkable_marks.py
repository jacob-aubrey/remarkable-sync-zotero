"""Conservative grouping for marks in a reMarkable PDF scene."""
from collections import defaultdict
from dataclasses import dataclass
from hashlib import sha256

import pymupdf as fitz


@dataclass(frozen=True)
class TextMark:
    id: str
    rects: tuple
    text: str
    stroke: tuple
    start: int | None
    length: int
    freehand: bool = False


@dataclass(frozen=True)
class HighlightGroup:
    ids: tuple
    rects: tuple
    text: str
    stroke: tuple
    ink_ids: tuple = ()


def _text_lines(page):
    lines = {}
    for word in page.get_text('words'):
        lines.setdefault(tuple(word[5:7]), []).append(word)
    return {key: sorted(words, key=lambda word: word[7]) for key, words in lines.items()}


def _word_bounds(words):
    return fitz.Rect(min(word[0] for word in words), min(word[1] for word in words),
                     max(word[2] for word in words), max(word[3] for word in words))


def _visual_lines(page):
    """Join fragments of the same printed line, including list labels and formulas."""
    lines, bounds, sources = {}, {}, {}
    for key, words in _text_lines(page).items():
        rect = _word_bounds(words)
        previous = next(reversed(lines), None)
        if previous and previous[0] == key[0]:
            prior = bounds[previous]
            height = max(prior.height, rect.height)
            gap = max(rect.x0 - prior.x1, prior.x0 - rect.x1, 0)
            if abs(rect.y1 - prior.y1) <= height * .32 and gap <= height * 2.5:
                lines[previous].extend(words)
                lines[previous].sort(key=lambda word: word[0])
                bounds[previous] |= rect
                sources[previous].append(key)
                continue
        lines[key], bounds[key], sources[key] = words, rect, [key]
    return lines, bounds, sources


def _locate(rect, lines, bounds=None):
    hits = []
    for key, words in lines.items():
        indices = [index for index, word in enumerate(words)
                   if rect.contains(((word[0] + word[2]) / 2, (word[1] + word[3]) / 2))]
        if indices:
            if indices != list(range(indices[0], indices[-1] + 1)):
                return None
            hits.append((*key, indices[0], indices[-1]))
    if len(hits) == 1:
        return hits[0]
    if hits or bounds is None:
        return None
    # A small snapped box can cover only a superscript or a partial word.
    for key, words in lines.items():
        line = bounds[key]
        intersection = rect & line
        if intersection.width <= .1 or intersection.height < min(rect.height, line.height) * .5:
            continue
        indices = [index for index, word in enumerate(words)
                   if min(rect.x1, word[2]) - max(rect.x0, word[0]) > .1]
        if indices:
            hits.append((*key, indices[0], indices[-1]))
    return hits[0] if len(hits) == 1 else None


def _same_column(first, second, height, left_block, right_block):
    return (first.x0 < second.x1 and second.x0 < first.x1
            and abs(second.x0 - first.x0) <= height * (2.1 if left_block == right_block else .55))


def _line_successors(lines, bounds):
    keys = sorted(lines, key=lambda key: (bounds[key].y0, bounds[key].x0, key))
    successors = {}
    for index, key in enumerate(keys):
        first = bounds[key]
        candidates = []
        for other in keys[index + 1:]:
            second = bounds[other]
            step = second.y0 - first.y0
            if step > first.height * 2.1:
                break
            height = max(first.height, second.height)
            if (.55 * height <= step <= 1.6 * height
                    and _same_column(first, second, height, key[0], other[0])):
                candidates.append(other)
        if candidates:
            successors[key] = candidates[0]
    return successors


def _column_wraps(lines, bounds):
    """Candidate transitions in PDF reading order, confirmed by tablet offsets."""
    return {(left, right) for left, right in zip(lines, list(lines)[1:])
            if left[0] != right[0] and bounds[left].x1 < bounds[right].x0
            and bounds[left].y0 - bounds[right].y1 > 2 * bounds[left].height}


def _consecutive(left, right, lines, successors, wraps=()):
    lb, ll, _, end = left
    rb, rl, start, _ = right
    if (lb, ll) == (rb, rl):
        return start == end + 1
    return (end == len(lines[(lb, ll)]) - 1 and start == 0
            and (successors.get((lb, ll)) == (rb, rl) or ((lb, ll), (rb, rl)) in wraps))


def _can_join(left, right, lines, bounds, successors, wraps):
    left_mark, left_hits = left
    right_mark, right_hits = right
    left_hit, right_hit = left_hits[-1], right_hits[0]
    have_offsets = left_mark.start is not None and right_mark.start is not None
    if left_mark.stroke != right_mark.stroke or not _consecutive(
            left_hit, right_hit, lines, successors, wraps if have_offsets else ()):
        return False
    if have_offsets:
        gap = right_mark.start - left_mark.start - left_mark.length
        # The tablet can count a line break as CR, LF, and a separating space.
        # Word adjacency above still prevents jumping over unhighlighted text.
        if not 0 <= gap <= (1 if left_hit[:2] == right_hit[:2] else 4):
            return False
    if left_hit[:2] == right_hit[:2]:
        return right_mark.rects[0].x0 - left_mark.rects[-1].x1 <= bounds[left_hit[:2]].height * (
            1.8 if have_offsets else .5)
    return True


def _overlap(a, b):
    intersection = a & b
    return intersection.width > .1 and intersection.height >= min(a.height, b.height) * .55


def _overlap_line_hits(left, right):
    a, a_hits = left
    b, b_hits = right
    return any(x[:2] == y[:2] and _overlap(r, s)
               for r, x in zip(a.rects, a_hits) for s, y in zip(b.rects, b_hits))


def _bridge_links(bridge, neighbors):
    """An individually unlocatable pass may connect two ends of one line."""
    if len(bridge.rects) != 1:
        return []
    rect = bridge.rects[0]
    by_line = defaultdict(list)
    for index, (mark, hits) in enumerate(neighbors):
        for other, hit in zip(mark.rects, hits):
            if _overlap(rect + (-.25, 0, .25, 0), other):
                by_line[hit[:2]].append((index, other))
    for line, matches in by_line.items():
        left = min(matches, key=lambda entry: entry[1].x0)
        right = max(matches, key=lambda entry: entry[1].x1)
        if left[0] != right[0] and left[1].x1 < right[1].x0:
            return line, list(dict.fromkeys(index for index, _ in matches))
    return None


def _characters(page, sources):
    raw = page.get_text('rawdict', flags=fitz.TEXTFLAGS_RAWDICT & ~fitz.TEXT_PRESERVE_IMAGES)
    fragments = {(block['number'], line_index): [char for span in line['spans'] for char in span['chars']]
                 for block in raw['blocks'] if block['type'] == 0
                 for line_index, line in enumerate(block['lines'])}
    return {key: [fragments.get(source, ()) for source in members] for key, members in sources.items()}


def _passage_text(chars, run, lines, bounds, wraps):
    # Use text positions to remove duplicate coverage while retaining real
    # repeated words. Individual member rectangles can begin mid-word.
    regions = defaultdict(list)
    for mark, hits in run:
        for rect, hit in zip(mark.rects, hits):
            regions[hit[:2]].append(rect)
    parts = []
    across_columns = any(a in regions and b in regions for a, b in wraps)
    ordered = ([key for key in lines if key in regions] if across_columns else
               sorted(regions, key=lambda key: (bounds[key].y0, bounds[key].x0, key)))
    for key in ordered:
        def covered(box):
            x = (box[0] + box[2]) * .5
            return any(rect.x0 <= x <= rect.x1 for rect in regions[key])

        selected = ' '.join(''.join(char['c'] if covered(char['bbox']) else ' ' for char in fragment)
                            for fragment in chars.get(key, ()))
        if not selected.strip():
            selected = ' '.join(word[4] for word in lines[key] if covered(word))
        parts.append(' '.join(selected.split()))
    return ' '.join(filter(None, parts)).replace('\u00ad ', '').replace('\u00ad', '')


def _covers_text_line(rect, hit, bounds):
    line = bounds[hit[:2]]
    return (hit[3] > hit[2] and rect.width >= 1.4 * line.height
            and line.x0 - 4 <= rect.x0 < rect.x1 <= line.x1 + 4)


def group_text_highlights(page, marks):
    """Return multi-mark passages. Uncertain marks keep their own IDs and geometry."""
    if not marks:
        return []
    lines, bounds, sources = _visual_lines(page)

    located = []
    bridges = []
    successors = _line_successors(lines, bounds)
    wraps = _column_wraps(lines, bounds)
    for mark in marks:
        hits = [_locate(rect, lines, None if mark.freehand else bounds) for rect in mark.rects]
        if (hits and all(hits) and (mark.text.strip() or mark.freehand)
                and all(left[:2] == right[:2] or _consecutive(left, right, lines, successors, wraps)
                        for left, right in zip(hits, hits[1:]))
                and (not mark.freehand or _covers_text_line(mark.rects[0], hits[0], bounds))):
            located.append((mark, hits))
        else:
            bridges.append(mark)
    located.sort(key=lambda entry: (bounds[entry[1][0][:2]].y0,
                                    entry[0].rects[0].x0, entry[0].id))

    # Build connected components rather than only extending the previous mark.
    # An overlap in the middle can therefore bridge two disjoint passes.
    primary_count = len(located)
    parent = list(range(primary_count))

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left, right):
        left, right = find(left), find(right)
        if left != right:
            parent[right] = left

    for left_index, left in enumerate(located):
        for right_index in range(left_index + 1, primary_count):
            right = located[right_index]
            if (_overlap_line_hits(left, right)
                    or _can_join(left, right, lines, bounds, successors, wraps)
                    or _can_join(right, left, lines, bounds, successors, wraps)):
                union(left_index, right_index)

    for bridge in bridges:
        linked = _bridge_links(bridge, located[:primary_count])
        if linked:
            line, links = linked
            parent.append(links[0])
            located.append((bridge, ((*line, 0, 0),)))
            for index in links[1:]:
                union(links[0], index)

    components = {}
    for index, entry in enumerate(located):
        components.setdefault(find(index), []).append(entry)
    groups = []
    chars = None
    reading_order = {key: index for index, key in enumerate(lines)}
    for run in components.values():
        if len(run) < 2 and not run[0][0].freehand:
            continue
        rows = {hit[:2] for _, hits in run for hit in hits}
        across_columns = any(a in rows and b in rows for a, b in wraps)
        run.sort(key=lambda entry: (
            reading_order[entry[1][0][:2]] if across_columns else bounds[entry[1][0][:2]].y0,
            entry[0].rects[0].x0, entry[0].id))
        if chars is None:
            chars = _characters(page, sources)
        text = _passage_text(chars, run, lines, bounds, wraps)
        if text:
            first = next((mark for mark, _ in run if not mark.freehand), run[0][0])
            groups.append(HighlightGroup((first.id,) + tuple(mark.id for mark, _ in run if mark is not first),
                                         tuple(rect for mark, _ in run for rect in mark.rects),
                                         text, first.stroke, tuple(mark.id for mark, _ in run if mark.freehand)))
    return groups


def handwriting_regions(strokes, page):
    """Cover every visible freehand mark, including single strokes and figure outlines."""
    if not strokes:
        return []
    from shapely import LineString, Point

    groups = []
    for order, (identity, rect, *path) in enumerate(strokes):
        coordinates = [tuple(point) for point in path[0]] if path else []
        geometry = (LineString(coordinates).simplify(.45) if len(coordinates) > 1 else
                    Point(coordinates[0]) if coordinates else None)
        members = [(order, identity, rect, geometry)]
        combined = fitz.Rect(rect)
        changed = True
        while changed:
            changed = False
            for index in range(len(groups) - 1, -1, -1):
                other, boundary = groups[index]
                if (any(_near_ink(a, b, path_a, path_b)
                        for _, _, a, path_a in members for _, _, b, path_b in other)
                        or (combined.contains(boundary) and _encloses(members, other))
                        or (boundary.contains(combined) and _encloses(other, members))):
                    members = other + members
                    combined |= boundary
                    groups.pop(index)
                    # A newly connected stroke can bridge an earlier component.
                    changed = True
        groups.append((members, combined))

    regions = []
    for members, rect in groups:
        padded = (rect + (-4, -4, 4, 4)) & page
        if not padded.is_empty:
            regions.append((min(members, key=lambda member: member[0])[1], padded))
    return regions


def _near_ink(a, b, path_a, path_b):
    dx = max(0, a.x0 - b.x1, b.x0 - a.x1)
    dy = max(0, a.y0 - b.y1, b.y0 - a.y1)
    if dx > 14 or dy > 18 or dx + dy > 24:
        return False
    if path_a is None or path_b is None:
        return True
    from shapely import dwithin

    return dwithin(path_a, path_b, 18)


def _encloses(outer, inner):
    from shapely import LineString, Point, polygonize, unary_union

    paths = [path for *_, path in outer if path is not None and path.geom_type == 'LineString']
    targets = [path for *_, path in inner if path is not None]
    if not targets or not paths:
        return not targets and all(path is None for *_, path in outer)
    linework = paths.copy()
    for path in paths:
        first, last = path.coords[0], path.coords[-1]
        if first != last and Point(first).distance(Point(last)) <= 3:
            linework.append(LineString([first, last]))
    faces = polygonize([unary_union(linework)])
    return any(all(face.covers(target) for target in targets) for face in faces.geoms)


def zotero_card_key(identity):
    alphabet = '23456789ABCDEFGHIJKLMNPQRSTUVWXYZ'
    digest = sha256(identity.encode()).digest()
    return ''.join(alphabet[value % len(alphabet)] for value in digest[:8])
