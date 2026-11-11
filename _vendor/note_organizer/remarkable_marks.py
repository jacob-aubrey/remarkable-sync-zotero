"""Conservative grouping for marks in a reMarkable PDF scene."""
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


@dataclass(frozen=True)
class HighlightGroup:
    ids: tuple
    rects: tuple
    text: str
    stroke: tuple


def _text_lines(page):
    lines = {}
    for word in page.get_text('words'):
        lines.setdefault(tuple(word[5:7]), []).append(word)
    return {key: sorted(words, key=lambda word: word[7]) for key, words in lines.items()}


def _locate(rect, lines):
    hits = []
    for key, words in lines.items():
        indices = [index for index, word in enumerate(words)
                   if rect.contains(((word[0] + word[2]) / 2, (word[1] + word[3]) / 2))]
        if indices:
            if indices != list(range(indices[0], indices[-1] + 1)):
                return None
            hits.append((*key, indices[0], indices[-1]))
    return hits[0] if len(hits) == 1 else None


def _consecutive(left, right, lines, bounds):
    lb, ll, _, end = left
    rb, rl, start, _ = right
    if lb != rb:
        return False
    if ll == rl:
        return start == end + 1
    first, second = lines[(lb, ll)], lines[(rb, rl)]
    if rl != ll + 1 or end != len(first) - 1 or start != 0:
        return False
    a, b = bounds[(lb, ll)], bounds[(rb, rl)]
    height = max(a.height, b.height)
    column_left, column_right = bounds[lb]
    # A close baseline, a wrapped line, and the same column are required.
    return (height * .55 <= b.y0 - a.y0 <= height * 1.55
            and b.x0 < a.x1 and a.x0 < b.x1
            and b.x0 - column_left <= height * .85
            and column_right - a.x1 <= height * 2)


def _can_join(left, left_hit, right, right_hit, lines, bounds):
    if left.stroke != right.stroke or not _consecutive(left_hit, right_hit, lines, bounds):
        return False
    if left.start is not None and right.start is not None:
        gap = right.start - left.start - left.length
        if not 0 <= gap <= (1 if left_hit[:2] == right_hit[:2] else 2):
            return False
    if left_hit[:2] == right_hit[:2]:
        return right.rects[0].x0 - left.rects[-1].x1 <= bounds[left_hit[:2]].height * .5
    return True


def group_text_highlights(page, marks):
    """Return multi-mark passages. Uncertain marks keep their own IDs and geometry."""
    if len(marks) < 2 or page.rotation:
        return []
    lines = _text_lines(page)
    bounds = {}
    for key, words in lines.items():
        rect = fitz.Rect(words[0][:4])
        for word in words[1:]:
            rect |= fitz.Rect(word[:4])
        bounds[key] = rect
        x0, x1 = bounds.get(key[0], (rect.x0, rect.x1))
        bounds[key[0]] = min(x0, rect.x0), max(x1, rect.x1)

    located = []
    for mark in marks:
        hits = [_locate(rect, lines) for rect in mark.rects]
        if not hits or not mark.text.strip() or not all(hits):
            continue
        if all(_consecutive(left, right, lines, bounds) for left, right in zip(hits, hits[1:])):
            located.append((mark, hits))
    located.sort(key=lambda entry: entry[1][0])
    runs = []
    for mark, hits in located:
        if runs:
            previous, previous_hits = runs[-1][-1]
            if _can_join(previous, previous_hits[-1], mark, hits[0], lines, bounds):
                runs[-1].append((mark, hits))
                continue
        runs.append([(mark, hits)])

    groups = []
    for run in runs:
        if len(run) < 2:
            continue
        text = run[0][0].text.strip()
        for (previous, hits), (mark, following) in zip(run, run[1:]):
            tail = lines[hits[-1][:2]][hits[-1][3]][4]
            wrapped = hits[-1][:2] != following[0][:2]
            hyphenated = wrapped and tail.endswith(('-', '\u00ad')) and (
                previous.text.rstrip().endswith(tail) or previous.text.rstrip().endswith(tail[:-1]))
            text += ('' if hyphenated else ' ') + mark.text.strip()
        groups.append(HighlightGroup(tuple(mark.id for mark, _ in run),
                                     tuple(rect for mark, _ in run for rect in mark.rects),
                                     text, run[0][0].stroke))
    return groups


def handwriting_regions(strokes, page):
    """Find bounded multi-stroke regions without changing the original ink."""
    if len(strokes) < 2:
        return []
    groups = []
    for identity, rect in strokes:
        if rect.width > page.width * .6 or rect.height > page.height * .35:
            continue
        members = [(identity, rect)]
        combined = fitz.Rect(rect)
        for index in range(len(groups) - 1, -1, -1):
            other, boundary = groups[index]
            extent = combined | boundary
            if (extent.width <= page.width * .6 and extent.height <= page.height * .35
                    and any(_near_ink(rect, existing) for _, existing in other)):
                members = other + members
                combined = extent
                groups.pop(index)
        groups.append((members, combined))

    regions = []
    for members, rect in groups:
        if len(members) < 2:
            continue
        padded = (rect + (-4, -4, 4, 4)) & page
        if not padded.is_empty:
            regions.append((members[0][0], padded))
    return regions


def _near_ink(a, b):
    dx = max(0, a.x0 - b.x1, b.x0 - a.x1)
    dy = max(0, a.y0 - b.y1, b.y0 - a.y1)
    return dx <= 10 and dy <= 7 and dx + dy <= 12


def zotero_card_key(identity):
    alphabet = '23456789ABCDEFGHIJKLMNPQRSTUVWXYZ'
    digest = sha256(identity.encode()).digest()
    return ''.join(alphabet[value % len(alphabet)] for value in digest[:8])
