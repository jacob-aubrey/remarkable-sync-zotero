"""Local, evidence-labelled paper keywords and opt-in topic suggestions."""
import re
import unicodedata
from pathlib import Path

VERSION = 4
HEADING = re.compile(r"^\s*(?:(?:author(?:s)?['’]?\s+)?key[ -]?words?|index\s+terms)\s*(?:[:：—–-]\s*(.*))?$", re.I)
STOP = re.compile(r'^\s*(?:abstract|introduction|background|references|acknowledg|copyright|received|accepted|\d+[.\s]+[A-Z])', re.I)
GENERIC = {'paper', 'study', 'results', 'research', 'article', 'copyright', 'elsevier', 'springer', 'abstract', 'introduction', 'conclusion',
           'in', 'and', 'the', 'of', 'with', 'remarkable imported pdf', 'remarkable', 'pdf'}
BULLETS = '•◼▪●►▶'
SEPARATORS = f'[,;；|{BULLETS}]' + r'|(?<!\w)·|·(?!\w)|\s+\.\s+'
KEYWORD_TOKENS = re.compile(SEPARATORS + r"""|[()\[\]{}"'“”‘’]""")
BRACKETS = {'(': ')', '[': ']', '{': '}'}
QUOTES = {'"': '"', "'": "'", '“': '”', '‘': '’'}
CLIPPED = re.compile(r'\b(?:and|or|of|for|with|the)\s*$', re.I)
REFERENCE = re.compile(r'^\s*(?:\[\d+\]\.?\s+|\([a-z]\)\s+|\((?:[A-Za-z][\w. -]*\s+)(?:19|20)\d{2}\b)', re.I)
BODY_FRAGMENT = re.compile(
    r'^\s*\d{1,3}\s+(?:the|in|or|and|of|for|with|to)\b|'
    r'^\s*(?:this|these|the)\s+\w+(?:\s+\w+){0,2}\s+(?:show|shows|indicate|indicates|suggest|suggests|demonstrate|demonstrates)\b|'
    r'\b(?:we|they|it|this|these|there)\s+(?:is|are|was|were|have|has|can|could|will|would|may|must|found)\b', re.I)
DATE = re.compile(
    r'\b(?:\d{1,2}\s+[A-Za-z]{3,9}\s+(?:19|20)\d{2}|'
    r'(?:19|20)\d{2}[-/]\d{1,2}[-/]\d{1,2})\b', re.I)
AFFILIATION = re.compile(r'\b(?:university|hospital|department of)\b', re.I)
METADATA = 'PDF/document keyword metadata'
PRINTED = 'Printed keyword/index-term section'


def normal(value):
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split()).strip('"\'“”‘’ ')


def clean(value):
    value = ' '.join(unicodedata.normalize('NFKC', value).split()).strip(' \t,;·:—–' + BULLETS)
    if len(value) > 2 and QUOTES.get(value[0]) == value[-1]:
        value = value[1:-1].strip()
    if not 2 <= len(value) <= 100 or len(value.split()) > 10 or not any(c.isalpha() for c in value):
        return ''
    if any(ord(c) < 32 for c in value) or any(c in value for c in ('@', '://')) or value.endswith('.') or normal(value) in GENERIC:
        return ''
    return value


def keyword_parts(text):
    """Split a keyword list at separators outside quotes and brackets."""
    start, quote, brackets = 0, None, []
    for match in KEYWORD_TOKENS.finditer(text):
        token = match.group()
        if quote:
            if token == quote:
                quote = None
        elif token in QUOTES and not text[start:match.start()].strip():
            quote = QUOTES[token]
        elif token in BRACKETS:
            brackets.append(BRACKETS[token])
        elif brackets and token == brackets[-1]:
            brackets.pop()
        elif not brackets and re.fullmatch(SEPARATORS, token):
            yield text[start:match.start()]
            start = match.end()
    yield text[start:]


def fragment_reason(tag):
    tag = tag.strip()
    if not tag or not any(char.isalpha() for char in tag):
        return 'This label contains no keyword text.'
    if '\ufffd' in tag or '©' in tag or REFERENCE.search(tag):
        return 'This label appears to contain a citation, a list marker, or damaged text.'
    if BODY_FRAGMENT.search(tag):
        return 'This label reads like part of a sentence.'
    if tag.endswith(('-', '‐', '‑', '–', '—')) or CLIPPED.search(tag):
        return 'This phrase appears to end before the keyword is complete.'
    if (tag[0] in '"“' or tag[-1] in '"”') and QUOTES.get(tag[0]) != tag[-1]:
        return 'This label has only part of a quoted keyword.'
    brackets = []
    for char in tag:
        if char in BRACKETS:
            brackets.append(BRACKETS[char])
        elif char in BRACKETS.values() and (not brackets or brackets.pop() != char):
            return 'This label has an unmatched bracket or parenthesis.'
    if brackets:
        return 'This label has an unmatched bracket or parenthesis.'
    return ''


def keyword_without_markup(tag):
    tag = tag.strip()
    if tag and (tag[0] in BULLETS or (len(tag) > 2 and QUOTES.get(tag[0]) == tag[-1])):
        value = clean(tag)
        if value and not fragment_reason(value):
            return value
    return ''


def read_document(path):
    """Bounded local text; no OCR, model download, lookup, or document write."""
    path = Path(path)
    if path.suffix.lower() == '.pdf':
        import pymupdf
        with pymupdf.open(path) as document:
            if document.needs_pass:
                raise ValueError('This PDF needs a password before its tags can be checked.')
            text = '\n\n'.join(document[index].get_text(sort=True) for index in range(min(6, len(document))))[:40000]
            # Keep PDF text blocks separate: reading across columns joins the
            # abstract to its keyword list and manufactures sentence fragments.
            front = '\n\n'.join(block[4].rstrip('\n') for index in range(min(3, len(document)))
                                for block in document[index].get_text('blocks') if block[6] == 0)[:32000]
            return text, front, (document.metadata or {}).get('keywords', '')
    from .sorting.core_logic import extract_document
    extraction = extract_document(path)
    return extraction.text[:40000], extraction.text[:24000], extraction.metadata.get('keywords', '')


def review_reason(tag, source, evidence):
    if source == METADATA and (DATE.search(evidence) or len(AFFILIATION.findall(evidence)) >= 2):
        return 'The PDF keyword field also contains publication or affiliation details.'
    if source in {METADATA, PRINTED}:
        if re.search(r'\s+\.\s+', tag):
            return 'Several keywords were stored together as one tag.'
        return fragment_reason(tag) or fragment_reason(clean(tag))
    return ''


def keyword_candidates(front, metadata='', vocabulary=()):
    canonical = {normal(value): value for value in vocabulary if value == clean(value) and not fragment_reason(value)}
    keywords, review = {}, {}

    def add(text, source, evidence):
        # Publisher metadata can encode keyword bullets as font control bytes.
        text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', ';', text)
        text = re.sub(r'(\w)-\s*\n(\w)', r'\1\2', text)
        text = re.sub(r'\s*\n\s*', ' ', text)
        for part in keyword_parts(text):
            value = clean(part)
            if value:
                key = normal(value)
                row = {'tag': canonical.get(key, value), 'source': source, 'evidence': evidence[:280]}
                reason = review_reason(part, source, evidence)
                if reason:
                    review.setdefault(key, {**row, 'reason': reason})
                else:
                    keywords.setdefault(key, row)

    if metadata and len(metadata) < 2000:
        add(metadata, METADATA, metadata)
    lines = front.splitlines()
    for index, line in enumerate(lines):
        match = HEADING.match(line)
        if not match:
            continue
        inline = match.group(1) or ''
        parts = [inline] if inline.strip() else []
        for offset, following in enumerate(lines[index + 1:index + 16], index + 1):
            if not following.strip():
                next_line = next((v for v in lines[offset + 1:offset + 4] if v.strip()), '')
                if next_line.lstrip().startswith(tuple(BULLETS + '·')):
                    continue
                if not parts and next_line and not STOP.match(next_line) and len(list(keyword_parts(next_line))) > 1:
                    continue
                break
            if parts and following.lstrip().startswith(tuple(BULLETS + '·')):
                parts.append(following)
                continue
            if STOP.match(following) or len(following) > 200 or following.strip().endswith('.') or BODY_FRAGMENT.search(following):
                break
            if (parts and not re.search(SEPARATORS, following)
                    and not parts[-1].rstrip().endswith((',', ';', '；', ' .'))
                    and not CLIPPED.search(parts[-1])):
                break  # Do not turn the next short body line into an automatic tag.
            parts.append(following)
        text = '\n'.join(parts)
        add(text, PRINTED, line + '\n' + text)
    return list(keywords.values())[:40], [row for key, row in review.items() if key not in keywords][:40]


def stated_keywords(front, metadata='', vocabulary=()):
    return keyword_candidates(front, metadata, vocabulary)[0]


def cleanup_candidates(report):
    candidates = {normal(row['tag']): row for row in report.get('cleanup', [])}
    for row in report.get('keywords', []):
        if 'source' not in row or 'evidence' not in row:
            continue  # Older keyword records have no evidence to review.
        reason = review_reason(row['tag'], row['source'], row['evidence'])
        if reason:
            candidate = {**row, 'reason': reason}
            if re.search(r'\s+\.\s+', row['tag']) and row['tag'] in row['evidence']:
                parts = [clean(part) for part in keyword_parts(row['tag'])]
                if len(parts) > 1 and all(part and not fragment_reason(part) for part in parts):
                    candidate['replacements'] = parts
                    candidate['reason'] = 'Restore the individual keywords separated by dots in the source.'
            if row['source'] == METADATA and ('bracket' in reason or 'quoted keyword' in reason):
                # Recover the full value only when the saved metadata contains
                # one unambiguous keyword the old separator would have split.
                replacements = set()
                for part in keyword_parts(row['evidence']):
                    full = clean(part)
                    if (full and normal(full) != normal(row['tag']) and not review_reason(part, METADATA, row['evidence'])
                        and any(normal(chunk) == normal(row['tag']) for chunk in re.split(SEPARATORS, part))):
                        replacements.add(full)
                if len(replacements) == 1:
                    candidate['replacement'] = replacements.pop()
                    candidate['reason'] = 'Restore the full keyword from the PDF metadata.'
            candidates.setdefault(normal(row['tag']), candidate)
    return candidates


def document_keywords(path, vocabulary=()):
    return keyword_report(path, vocabulary)['keywords']


def keyword_report(path, vocabulary=()):
    try:
        text, front, metadata = read_document(path)
        rows = stated_keywords(front, metadata, vocabulary)
        return {'keywords': rows, 'status': 'found' if rows else 'no_stated_keywords' if text.strip() else 'needs_text_recognition',
                'version': VERSION}
    except Exception as error:
        return {'keywords': [], 'status': 'extraction_failed', 'detail': str(error)[:240], 'version': VERSION}


def backfill_keywords(client, parent, path, row, save):
    """Check an original once per extractor version; preserve manual removals."""
    import hashlib
    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    previous = row.get('keyword_check', {})
    if previous.get('version') == VERSION and previous.get('sha256') == digest and previous.get('status') != 'extraction_failed':
        return previous
    record = client.get('item', parent)
    if not record or record.get('deleted'):
        raise ValueError('The paper is unavailable for its keyword check.')
    report = keyword_report(path, [value['tag'] for value in record.get('tags', [])])
    if hashlib.sha256(Path(path).read_bytes()).hexdigest() != digest:
        raise ValueError('The original PDF changed while checking keywords.')
    existing = record.get('tags', [])
    current = {normal(value['tag']) for value in existing}
    removed = {normal(value['tag']) for value in previous.get('keywords', [])} - current
    keywords = [value['tag'] for value in report['keywords'] if normal(value['tag']) not in removed]
    tags = append_tags(existing, keywords)
    if tags != existing:
        record['tags'] = tags
        client.authorize()
        client.update(record)
    reliable = {normal(value['tag']) for value in report['keywords']}
    cleanup = [value for key, value in cleanup_candidates(previous).items() if key not in reliable and key in current]
    row['keyword_check'] = {**report, 'sha256': digest, 'cleanup': cleanup}
    save()
    return row['keyword_check']


def analyse(path, vocabulary=(), existing=()):
    text, front, metadata = read_document(path)
    vocabulary = sorted(set(vocabulary))
    keywords, review = keyword_candidates(front, metadata, vocabulary)
    excluded = {normal(tag) for tag in existing} | {normal(row['tag']) for row in keywords}
    suggestions = [row for row in review if normal(row['tag']) not in excluded]
    seen = excluded | {normal(row['tag']) for row in suggestions}
    review_count = len(suggestions)
    folded = normal(text)
    canonical = {normal(value): value for value in vocabulary if value == clean(value) and not fragment_reason(value)}
    for value in vocabulary:
        key = normal(value)
        if value == canonical.get(key) and key not in seen and re.search(r'(?<!\w)' + re.escape(key) + r'(?!\w)', folded):
            suggestions.append({'tag': value, 'source': 'Existing vocabulary found in this paper', 'evidence': value})
            seen.add(key)
        if len(suggestions) >= review_count + 5:
            break
    if text.strip():
        import yake
        for term, _score in yake.KeywordExtractor(lan='en', n=3, top=30, dedup_lim=.85).extract_keywords(text[:20000]):
            value = clean(term)
            key = normal(value)
            if not value or key in seen or len(value) < 4 or fragment_reason(value):
                continue
            value = canonical.get(key, value)
            suggestions.append({'tag': value, 'source': 'Local text suggestion (YAKE, English)', 'evidence': term})
            seen.add(key)
            if len(suggestions) >= review_count + 10:
                break
    return {'keywords': keywords, 'suggestions': suggestions,
            'notice': 'Suggestions use local English text statistics; review them before adding. No document text is uploaded.'
                      if text.strip() else 'No readable text was found. Existing tags remain available; add tags manually for scanned papers.'}


def append_tags(existing, keywords=(), accepted=()):
    result = [dict(row) for row in existing]
    seen = {normal(row['tag']) for row in result}
    for values, kind in [(keywords, 1), (accepted, 0)]:
        for value in values:
            if kind == 1 and fragment_reason(value):
                continue
            value = clean(value)
            if value and normal(value) not in seen:
                result.append({'tag': value, 'type': kind})
                seen.add(normal(value))
    return result
