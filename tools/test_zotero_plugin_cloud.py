"""Exercise metadata-only changes on a previously recorded synthetic cloud fixture.

Never creates a validation root or selects a user paper by approximate name.
The physical pen-stroke fixture is excluded. No credentials or note text appear
in reports, and no item is permanently deleted.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from note_organizer.remarkable_cloud import CloudClient, TokenVault, atomic_write
from note_organizer.zotero_bridge import descendants


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scope-record', type=Path, required=True)
    parser.add_argument('--vault', type=Path, required=True)
    parser.add_argument('--cache', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    scope = json.loads(args.scope_record.read_text(encoding='utf-8'))
    if scope.get('label') != 'Notable Zotero validation - safe to remove':
        raise ValueError('A previously recorded synthetic validation scope is required')
    token = TokenVault(args.vault).load()
    if not token:
        raise ValueError('An existing encrypted validation grant is required')
    cloud = CloudClient(token, cache_dir=args.cache)
    catalog = cloud.library()
    root = next(r for r in catalog if r.id == scope['root'])
    if root.name != scope['label'] or root.deleted:
        raise ValueError('Validation root changed; no cloud write was attempted')
    folders = descendants([r for r in catalog if r.kind == 'CollectionType' and not r.deleted], root.id, lambda r: r.id, lambda r: r.parent)
    candidates = [r for r in catalog if r.is_pdf and not r.deleted and r.parent in folders
                  and r.id != scope['tablet_pdf'] and r.name.startswith('Synthetic PC paper')]
    if len(candidates) != 1:
        raise ValueError('The exact recorded synthetic PC fixture is unavailable or ambiguous')
    item = candidates[0]
    initial = {r.id: r.hash for r in catalog}
    before = cloud.pdf_bundle(item)
    args.output.mkdir(parents=True, exist_ok=False)
    journal = {'root': root.id, 'item': item.id, 'name': item.name, 'parent': item.parent, 'initial_hash': item.hash}
    atomic_write(args.output / 'intent.json', json.dumps(journal).encode(), require_absent=True)
    report = {'scope': 'Only the recorded synthetic PC cloud fixture', 'checks': []}
    def checked(name):
        report['checks'].append({'name': name, 'status': 'PASS'})
        atomic_write(args.output / 'results.json', json.dumps(report, indent=2).encode())
    changed = cloud.update_item(item.id, expected_hash=item.hash, name=item.name + ' - plugin validation', scope_id=root.id)
    assert changed.name.endswith(' - plugin validation')
    checked('live_cloud_rename_commits_to_same_document_ID')
    moved = cloud.update_item(item.id, expected_hash=changed.hash, parent=root.id, scope_id=root.id)
    assert moved.parent == root.id
    checked('live_cloud_move_preserves_document_ID')
    removed = cloud.update_item(item.id, expected_hash=moved.hash, parent='trash', scope_id=root.id)
    assert removed.deleted and removed.parent == 'trash'
    checked('live_cloud_trash_is_confirmed_metadata_not_missing_ID')
    restored = cloud.update_item(item.id, expected_hash=removed.hash, name=item.name, parent=item.parent, scope_id=root.id, restore=True)
    assert not restored.deleted and restored.name == item.name and restored.parent == item.parent
    checked('live_cloud_explicit_restore_returns_to_original_name_and_folder')
    after = cloud.pdf_bundle(restored)
    assert set(after) == set(before)
    assert all(after[name] == data for name, data in before.items() if not name.endswith('.metadata'))
    checked('every_PDF_annotation_and_content_source_file_is_byte_identical')
    final = {r.id: r.hash for r in cloud.library()}
    assert set(final) == set(initial)
    assert all(final[identity] == digest for identity, digest in initial.items() if identity != item.id)
    checked('all_other_cloud_items_including_physical_handwriting_fixture_unchanged')
    atomic_write(args.output / 'completed.json', json.dumps({'item': item.id, 'hash': restored.hash, 'restored': True}).encode())
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
