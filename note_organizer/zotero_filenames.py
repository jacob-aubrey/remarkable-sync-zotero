"""Names proposed from a Zotero citation, with no PDF or network lookup."""
import html
import re

from .sorting.core_logic import (
    _normalize_year, build_proposed_filename, sanitize_filename_part, validate_document_filename,
)
from .zotero_review import fingerprint

PLACEHOLDER = re.compile(r'(?<![a-z0-9])unknown[ _-]?(?:journal|year|author)(?![a-z0-9])', re.I)


def citation_snapshot(record):
    return fingerprint({field: record.get(field) or '' for field in (
        'title', 'itemType', 'creators', 'publicationTitle', 'date', 'volume', 'issue', 'journalAbbreviation',
    )})


def title_filename(title, suffix, *, supplement=False):
    title = html.unescape(re.sub(r'<[^>]*>', '', title)).strip()
    if title.lower().endswith(suffix.lower()):
        title = title[:-len(suffix)].rstrip()
    stem = sanitize_filename_part(title)[:210].strip(' ._-')
    if not stem or PLACEHOLDER.search(stem) or re.fullmatch(r'[a-f0-9]{64}', stem, re.I):
        return ''
    name = build_proposed_filename({'title': stem, 'is_supplementary_material': supplement}, suffix,
                                   filename_format='custom', custom_template='{title}')
    try:
        return validate_document_filename(name, suffix)
    except ValueError:
        return validate_document_filename('Paper_' + name, suffix)


def citation_filename(record, templates, *, supplement=False):
    title = record.get('title', '')
    creators = [c.get('name') or ' '.join(filter(None, (c.get('firstName'), c.get('lastName'))))
                for c in record.get('creators', []) if c.get('creatorType') == 'author']
    creators = [name for name in creators if name]
    journal = record.get('publicationTitle', '').strip()
    year = _normalize_year(record.get('date'))
    if record.get('itemType') == 'journalArticle' and creators and journal and year != 'Unknown':
        details = {'title': title, 'creators': creators, 'primary_creator': creators[0], 'journal': journal,
                   'year': year, 'document_type': 'journal_article', 'is_supplementary_material': supplement,
                   'volume': record.get('volume', ''), 'issue': record.get('issue', ''),
                   'journal_abbreviation': record.get('journalAbbreviation', '')}
        template = templates.get('journal_article', '')
        name = build_proposed_filename(details, '.pdf', filename_format='custom' if template else 'smart',
                                       custom_template=template)
        if not PLACEHOLDER.search(name):
            try:
                return validate_document_filename(name, '.pdf'), 'Zotero citation'
            except ValueError:
                pass
    return title_filename(title, '.pdf', supplement=supplement), 'Zotero title'
