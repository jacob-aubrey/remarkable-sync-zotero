"""Read-only previews and guarded repairs for the selected Zotero library."""
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from types import SimpleNamespace
import uuid

from .remarkable_cloud import atomic_write
from .zotero_pilot import PilotError
from .zotero_review import fingerprint


UNKNOWN = re.compile(r'(?<![a-z0-9])unknown(?:[ _-]?(?:journal|year|author))?(?![a-z0-9])', re.I)


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def read_json(path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else default


def write_json(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8'))


def unknown_names(client, links):
    catalog = client.review_action('catalog', {'names_only': True})
    matches = defaultdict(list)
    parents = catalog['parents']
    for key, parent in parents.items():
        if UNKNOWN.search(parent.get('title', '')):
            matches[key].append({'where': 'Zotero title', 'name': parent['title']})
    for entry in catalog['attachments']:
        data = entry['data']
        filename = Path(entry['path']).name
        key = data.get('parentItem') or data['key']
        if data.get('parentItem') and key not in parents:
            continue
        if UNKNOWN.search(Path(filename).stem):
            matches[key].append({'where': 'Zotero filename', 'name': filename, 'attachment': data['key']})
        title = data.get('title', '')
        if title != filename and UNKNOWN.search(title):
            matches[key].append({'where': 'Zotero attachment title', 'name': title, 'attachment': data['key']})
    for identity, row in links.items():
        parent = row.get('parent')
        if parent in parents and UNKNOWN.search(row.get('name', '')):
            matches[parent].append({'where': 'Saved reMarkable name', 'name': row['name'], 'link': identity})
    rows = {}
    for key, findings in matches.items():
        findings.sort(key=lambda row: (row['where'], row['name'], row.get('attachment', row.get('link', ''))))
        title = parents.get(key, {}).get('title', '') or findings[0]['name']
        rows[key] = {'key': key, 'title': title, 'findings': findings, 'reference': key in parents,
                     'token': fingerprint([client.server_id, key, title, findings])}
    return rows


def name_review(worker, *, selected=None, dismiss=None):
    client = worker.zotero()
    try:
        path = worker.directory / 'unknown-name-review.json'
        queues = read_json(path, {})
        queue = queues.setdefault(client.server_id, {})
        matches = unknown_names(client, worker.state.read('documents', {}))
        if dismiss is not None:
            if dismiss in queue:
                del queue[dismiss]
                write_json(path, queues)
        if selected is not None:
            if not isinstance(selected, list) or any(not isinstance(row, dict) for row in selected):
                raise PilotError('Choose papers from the current name search.')
            for row in selected:
                match = matches.get(row.get('key'))
                if not match or match['token'] != row.get('token'):
                    raise PilotError('A name changed after the search. Refresh the results before queueing.')
            for row in selected:
                match = matches[row['key']]
                queue.setdefault(match['key'], {**match, 'queued_at': now()})
            write_json(path, queues)
        rows = [{**match, 'queued': key in queue} for key, match in matches.items()]
        queued = [{**(matches.get(key) or saved), 'queued_at': saved['queued_at'],
                   'status': 'needs_review' if key in matches else 'no_longer_matches'}
                  for key, saved in queue.items()]
        return {'matches': sorted(rows, key=lambda row: row['title'].casefold()),
                'queued': sorted(queued, key=lambda row: row['title'].casefold())}
    finally:
        client.close()


def _tags(value):
    return sorted(({'tag': row['tag'], 'type': row.get('type', 0)} for row in value), key=lambda row: row['tag'])


def repair_keywords(worker, selected):
    """Journal exact per-paper changes before asking Zotero to apply them."""
    from .zotero_tagging import clean, normal
    if (not isinstance(selected, list) or not selected or any(not isinstance(row, dict)
            or not isinstance(row.get('parent'), str) or not isinstance(row.get('token'), str)
            or not isinstance(row.get('tags'), list) or not row['tags']
            or any(not isinstance(tag, str) for tag in row['tags']) for row in selected)
            or len({row['parent'] for row in selected}) != len(selected)):
        raise PilotError('Choose automatic tags from the current review.')
    reports = defaultdict(list)
    for row in worker.state.read('documents', {}).values():
        if row.get('parent') and row.get('keyword_check'):
            reports[row['parent']].append(row['keyword_check'])
    client = worker.zotero()
    try:
        changes = {}
        for row in selected:
            key = row['parent']
            current = client.get('item', key)
            if not current or current.get('deleted'):
                raise PilotError('A selected paper is unavailable. Refresh the automatic tag review.')
            preview = worker._keyword_cleanup_preview(current, reports[key])
            chosen = set(row['tags'])
            proposals = {tag['tag']: tag.get('replacements') or ([tag['replacement']] if tag.get('replacement') else [])
                         for tag in preview['tags']}
            if preview['token'] != row['token'] or not chosen <= proposals.keys():
                raise PilotError('A selected paper or its tags changed. Refresh the review before applying repairs.')
            before = _tags(current.get('tags', []))
            after = [tag for tag in before if tag['type'] != 1 or tag['tag'] not in chosen]
            retained = {normal(tag['tag']) for tag in after if tag['tag'] == clean(tag['tag'])}
            renamed = 0
            for tag in sorted(chosen):
                replacements = proposals[tag]
                if not replacements:
                    continue
                renamed += 1
                for replacement in replacements:
                    normalized = normal(replacement)
                    if normalized not in retained:
                        after.append({'tag': replacement, 'type': 1})
                        retained.add(normalized)
            changes[key] = {'before': before, 'after': _tags(after), 'title': preview['title'], 'state': 'pending',
                            'removed': len(chosen) - renamed, 'renamed': renamed}
        client.authorize()
        identity = uuid.uuid4().hex
        path = worker.directory / 'tag-repairs' / (identity + '.json')
        path.parent.mkdir(exist_ok=True)
        journal = {'id': identity, 'created_at': now(), 'server': client.server_id, 'papers': changes}
        write_json(path, journal)
        for key, change in changes.items():
            current = client.get('item', key)
            if not current or current.get('deleted') or _tags(current.get('tags', [])) != change['before']:
                change['state'] = 'needs_review'
            else:
                change['state'] = 'applying'
                write_json(path, journal)
                current['tags'] = change['after']
                client.update(current)
                change['state'] = 'applied'
            write_json(path, journal)
        return {'id': identity, 'removed': sum(row['removed'] for row in changes.values() if row['state'] == 'applied'),
                'renamed': sum(row['renamed'] for row in changes.values() if row['state'] == 'applied'),
                'needs_review': sum(row['state'] != 'applied' for row in changes.values())}
    finally:
        client.close()


def undo_keyword_repair(worker, identity):
    if not isinstance(identity, str) or not re.fullmatch(r'[a-f0-9]{32}', identity):
        raise PilotError('Choose a saved tag repair.')
    path = worker.directory / 'tag-repairs' / (identity + '.json')
    journal = read_json(path, {})
    client = worker.zotero()
    try:
        if journal.get('server') != client.server_id:
            raise PilotError('This repair belongs to a different Zotero library.')
        client.authorize()
        restored, skipped = 0, 0
        for key, change in journal['papers'].items():
            if change['state'] == 'undone':
                continue
            if change['state'] not in ('applied', 'applying'):
                skipped += 1
                continue
            current = client.get('item', key)
            if not current or current.get('deleted'):
                skipped += 1
                continue
            tags = _tags(current.get('tags', []))
            if tags == change['after']:
                current['tags'] = change['before']
                client.update(current)
                restored += sum(tag not in change['after'] for tag in change['before'])
            elif tags != change['before']:
                skipped += 1
                continue
            change['state'] = 'undone'
            write_json(path, journal)
        return {'restored': restored, 'skipped': skipped}
    finally:
        client.close()


def keyword_repairs(worker):
    client = worker.zotero()
    try:
        rows = []
        for path in sorted((worker.directory / 'tag-repairs').glob('*.json'), reverse=True):
            journal = read_json(path, {})
            if journal.get('server') == client.server_id and any(
                    row['state'] != 'undone' for row in journal.get('papers', {}).values()):
                rows.append({'id': journal['id'], 'created_at': journal['created_at'],
                             'papers': len(journal['papers'])})
        return sorted(rows, key=lambda row: row['created_at'], reverse=True)
    finally:
        client.close()


def _highlight_preview(worker, identity, link):
    import pymupdf
    from .remarkable_pdf import page_map, scene, scene_highlight_groups
    from .remarkable_sources import source_bundle

    if not link.get('revision') or link.get('unavailable'):
        raise PilotError('Sync this paper before reviewing its retained highlights.')
    store = SimpleNamespace(path=worker.state.directory / 'sources' / 'library.sqlite3')
    bundle = source_bundle(store, identity, worker.stop.is_set, revision=link['revision'])
    groups = []
    with pymupdf.open(stream=bundle[identity + '.pdf'], filetype='pdf') as pdf:
        content = json.loads(bundle[identity + '.content'])
        for page, (pid, original) in enumerate(page_map(content, len(pdf)), 1):
            if worker.stop.is_set():
                raise InterruptedError('Highlight review paused.')
            layer = bundle.get(identity + '/' + pid + '.rm')
            if original < 0 or not layer:
                continue
            groups.extend({'members': list(group.ids), 'text': group.text, 'page': page}
                          for group in scene_highlight_groups(pdf[original], scene(layer), pid))
    return {'identity': identity, 'groups': groups, 'approved': link.get('approved_highlight_groups', []),
            'token': fingerprint([worker.config.get('connection_id'), identity, link['revision'],
                                  link.get('approved_highlight_groups'), link.get('group_highlights'), groups])}


def highlight_review(worker, identity, *, token=None, selected=None, undo=False):
    with worker.state.lock():
        links = worker.state.read('documents', {})
        link = links.get(identity)
        if not link or not link.get('view_key'):
            raise PilotError('Choose a currently linked paper.')
        preview = _highlight_preview(worker, identity, link)
        if selected is None and not undo:
            return {**preview, 'can_undo': bool(link.get('highlight_repair'))}
        if token != preview['token']:
            raise PilotError('The paper or its highlight groups changed. Preview the current groups first.')
        if undo:
            repair = link.get('highlight_repair')
            if not repair or repair['revision'] != link['revision']:
                raise PilotError('The previous highlight repair belongs to an older tablet revision.')
            for key, value in repair['before'].items():
                if value is None:
                    link.pop(key, None)
                else:
                    link[key] = value
            link.pop('highlight_repair')
        else:
            if not isinstance(selected, list) or any(type(index) is not int or not 0 <= index < len(preview['groups'])
                                                      for index in selected):
                raise PilotError('Choose groups from the current highlight preview.')
            link['highlight_repair'] = {'revision': link['revision'], 'time': now(),
                'before': {key: link.get(key) for key in ('approved_highlight_groups', 'group_highlights')}}
            link['approved_highlight_groups'] = [preview['groups'][index]['members'] for index in sorted(set(selected))]
            link['group_highlights'] = False
        worker.state.write('documents', links)
        return {'queued': True}


def _pdf_path(client, key):
    if not isinstance(key, str) or not re.fullmatch(r'[23456789A-NP-Z]{8}', key):
        raise PilotError('Choose a Zotero PDF attachment.')
    record = client.get('item', key)
    if not record or record.get('deleted') or record.get('itemType') != 'attachment' or record.get('contentType') != 'application/pdf':
        raise PilotError('The Zotero PDF attachment is unavailable.')
    path = Path(client.file_path(key))
    if not path.is_file():
        raise PilotError('Download the Zotero attachment before reviewing its pages.')
    return path


def compare_pdfs(worker, left, right):
    import pymupdf

    client = worker.zotero()
    try:
        sides, maps = [], []
        for key in (left, right):
            path = _pdf_path(client, key)
            sample, locations = '', defaultdict(list)
            with pymupdf.open(path) as pdf:
                if pdf.needs_pass:
                    raise PilotError('Unlock the PDF before comparing it.')
                pages = len(pdf)
                for index in range(min(pages, 250)):
                    if worker.stop.is_set():
                        raise InterruptedError('PDF comparison paused.')
                    page = pdf[index]
                    words = ' '.join(page.get_text(sort=True).split())
                    if words:
                        locations[hashlib.sha256(words.encode()).hexdigest()].append(index + 1)
                        if not sample:
                            sample = words[:260]
                sides.append({'key': key, 'filename': path.name, 'pages': pages,
                              'compared': min(pages, 250), 'text': sample})
            maps.append(locations)
        pairs = sorted([pages[0], maps[1][mark][0]] for mark, pages in maps[0].items()
                       if len(pages) == len(maps[1].get(mark, ())) == 1)
        return {'pdfs': sides, 'matches': len(pairs), 'page_map': pairs[:20]}
    finally:
        client.close()


def export_annotations(worker, key, target):
    client = worker.zotero()
    try:
        path = _pdf_path(client, key)
        rows = client.reader_annotations(key)
        lines = ['# Annotations', '', path.name, '']
        for row in sorted(rows, key=lambda row: (row.get('annotationSortIndex', ''), row['key'])):
            position = json.loads(row.get('annotationPosition') or '{}')
            page = position.get('pageIndex', 0) + 1
            if not re.fullmatch(r'[23456789A-NP-Z]{8}', row['key']):
                continue
            source = f'zotero://open-pdf/library/items/{key}?page={page}&annotation={row["key"]}'
            lines.extend([f'## Page {row.get("annotationPageLabel") or page}', '', f'[Open in Zotero]({source})', ''])
            for field in ('annotationText', 'annotationComment'):
                if row.get(field):
                    lines.extend([row[field], ''])
        from .handwriting_ocr import checksum
        with path.open('rb') as document:
            digest = hashlib.file_digest(document, 'sha256').hexdigest()
        cached = sorted((worker.directory / 'handwriting-cache').glob('*/' + digest + '.json'),
                        key=lambda entry: entry.stat().st_mtime, reverse=True)
        for entry in cached:
            try:
                record = read_json(entry, {})
                if (record.get('pdf_sha256') != digest or record.get('profile') != entry.parent.name
                        or checksum(record['records']) != record.get('records_sha256')):
                    continue
                lines.extend(['## Cached handwriting transcription', '',
                              'Machine-read handwriting can contain mistakes.', ''])
                for page in record['records']:
                    if page.get('text') and type(page.get('page')) is int:
                        lines.extend([f'[Open page {page["page"]} in Zotero](zotero://open-pdf/library/items/{key}?page={page["page"]})',
                                      '', page['text'], ''])
                break
            except (OSError, ValueError, KeyError, TypeError):
                continue
        target = Path(target).resolve()
        if target.suffix.casefold() != '.md' or target.is_relative_to(worker.directory):
            raise PilotError('Choose a new Markdown file outside the plugin folder.')
        atomic_write(target, '\n'.join(lines).encode('utf-8'), require_absent=True)
        return {'path': str(target), 'annotations': len(rows)}
    finally:
        client.close()


def sync_timeline(worker):
    labels = {'operation_started': 'Sync started', 'operation_finished': 'Sync finished',
              'sync_result': 'Sync recorded its results', 'error': 'Sync reported a problem'}
    events = []
    for suffix in ('.1', ''):
        path = worker.directory / ('sync-trace.jsonl' + suffix)
        if not path.is_file():
            continue
        with path.open('rb') as stream:
            offset = max(0, path.stat().st_size - 131072)
            stream.seek(offset)
            if offset:
                stream.readline()
            for line in stream:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get('event') == 'operation_started' and event.get('method') == 'sync':
                    operation = event.get('operation')
                    events.append((operation, {'time': event.get('time'), 'label': labels['operation_started']}))
                elif (events and event.get('operation') == events[-1][0] and event.get('event') in labels
                      and event.get('event') != 'operation_started'):
                    events.append((events[-1][0], {'time': event.get('time'), 'label': labels[event['event']]}))
    return {'events': [row for _, row in events[-18:]], 'last_sync': worker.state.read('last_sync_at')}
