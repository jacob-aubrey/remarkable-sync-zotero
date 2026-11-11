"""Reversible tag write on the previously recorded synthetic PC fixture only."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from note_organizer.remarkable_cloud import CloudClient, TokenVault, atomic_write
from note_organizer.remarkable_pdf import page_map
from note_organizer.zotero_bridge import descendants
from note_organizer.zotero_tags import tablet_tags, render_fingerprint


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['scope-record', 'vault', 'cache', 'output']:
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    scope = json.loads(args.scope_record.read_text())
    if scope.get('label') != 'Notable Zotero validation - safe to remove':
        raise ValueError('Recorded synthetic scope required')
    cloud = CloudClient(TokenVault(args.vault).load(), cache_dir=args.cache)
    catalog = cloud.library()
    root = next(r for r in catalog if r.id == scope['root'])
    if root.name != scope['label'] or root.deleted:
        raise ValueError('Synthetic root changed')
    folders = descendants([r for r in catalog if r.kind == 'CollectionType' and not r.deleted],
                          root.id, lambda r: r.id, lambda r: r.parent)
    choices = [r for r in catalog if r.is_pdf and not r.deleted and r.parent in folders
               and r.id != scope['tablet_pdf'] and r.name.startswith('Synthetic PC paper')]
    if len(choices) != 1:
        raise ValueError('Unique synthetic PC fixture required')
    item = choices[0]
    before = cloud.pdf_bundle(item)
    content = json.loads(before[item.id + '.content'])
    if content.get('tags') != [] or content.get('pageTags') != []:
        raise ValueError('Fixture already has tags; no write attempted')
    import pymupdf
    with pymupdf.open(stream=before[item.id + '.pdf'], filetype='pdf') as pdf:
        pid = page_map(content, len(pdf))[0][0]
    args.output.mkdir(parents=True, exist_ok=False)
    atomic_write(args.output / 'intent.json', json.dumps({'item': item.id, 'hash': item.hash,
                 'tags': [], 'page_tags': {}}).encode(), require_absent=True)
    changed = None
    report = {'scope': 'Recorded synthetic PC fixture only; physical handwriting fixture excluded', 'checks': []}
    try:
        changed = cloud.update_item(item.id, expected_hash=item.hash, scope_id=root.id,
                                    tags=['Notable tag validation'], page_tags={pid: ['Notable page validation']})
        actual = next(r for r in cloud.library() if r.id == item.id)
        if actual.hash != changed.hash:
            raise ValueError('Fixture changed concurrently')
        after = cloud.pdf_bundle(actual)
        assert tablet_tags(json.loads(after[item.id + '.content'])) == ({'Notable tag validation'}, {pid: {'Notable page validation'}})
        report['checks'].append({'name': 'live_cloud_document_and_UUID_page_tags_read_back', 'status': 'PASS'})
        assert render_fingerprint(item.id, before) == render_fingerprint(item.id, after)
        assert all(after[n] == v for n, v in before.items() if not n.endswith(('.content', '.metadata')))
        report['checks'].append({'name': 'PDF_pen_layers_and_other_content_preserved', 'status': 'PASS'})
    finally:
        if changed:
            restored = cloud.update_item(item.id, expected_hash=changed.hash, scope_id=root.id, tags=[], page_tags={})
            restored_bundle = cloud.pdf_bundle(restored)
            assert all(restored_bundle[n] == v for n, v in before.items() if not n.endswith('.metadata'))
            atomic_write(args.output / 'restored.json', json.dumps({'item': item.id, 'restored': True}).encode())
    final = {r.id: r.hash for r in cloud.library()}
    assert set(final) == {r.id for r in catalog}
    assert all(final[r.id] == r.hash for r in catalog if r.id != item.id)
    report['checks'].append({'name': 'initial_tags_restored_and_every_other_cloud_item_unchanged', 'status': 'PASS'})
    atomic_write(args.output / 'results.json', json.dumps(report, indent=2).encode())
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
