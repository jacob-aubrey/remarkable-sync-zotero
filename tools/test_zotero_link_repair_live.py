"""Verify connection repair after real merges in disposable Zotero."""
import hashlib
import json
from pathlib import Path
import sys
import time
import uuid
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import test_zotero_plugin_live as live
from note_organizer.zotero_worker import Worker, file_sha
from note_organizer.zotero_pilot import Ledger, LocalZotero, PilotError, marker as managed_marker
from note_organizer.zotero_bridge import Bridge


BRANCH = """
          else if (command.action === 'merge_repair_fixture') {
            const {mergeItems} = ChromeUtils.importESModule('chrome://zotero/content/mergeItems.mjs');
            const survivor = await Zotero.Items.getByLibraryAndKeyAsync(Zotero.Libraries.userLibraryID, command.survivor || 'RPRSUR23');
            const removed = await Zotero.Items.getByLibraryAndKeyAsync(Zotero.Libraries.userLibraryID, command.removed || 'RPRFRM23');
            await mergeItems(survivor, [removed]); response.merged = removed.deleted;
          } else if (command.action === 'undo_merge_fixture') {
            await Zotero.UndoHistory.undo();
          }
"""


def repair_checks(client, root, checked):
    client.authorize()
    client.create('collection', {'key': 'RPRCL234', 'name': 'Synthetic merge-repair fixture'})
    for key in ['RPRSUR23', 'RPRFRM23']:
        client.create('item', {'key': key, 'itemType': 'journalArticle', 'title': 'Synthetic merge repair', 'collections': ['RPRCL234']})
    client.create('item', {'key': 'RPRPDF23', 'itemType': 'attachment', 'parentItem': 'RPRFRM23',
        'title': 'Synthetic annotated copy', 'contentType': 'application/pdf', 'linkMode': 'imported_file', 'filename': 'Synthetic.pdf'})
    client.create('item', {'key': 'RPRNTE23', 'itemType': 'note', 'parentItem': 'RPRFRM23', 'note': '<p>Native note retained through repair</p>'})
    path = root / 'synthetic-repair.pdf'; path.write_bytes(live.pdf_bytes()); client.upload('RPRPDF23', path, '')
    assert live.command(root, 'merge_repair_fixture')['merged']
    checked('real_native_merge_moves_exact_PDF_and_note_to_survivor')
    keys = ['RPRSUR23', 'RPRFRM23', 'RPRPDF23', 'RPRNTE23']
    sha = file_sha(client.file_path('RPRPDF23'))
    tablet = SimpleNamespace(id='0a000000-0000-4000-8000-000000000002', parent='0a000000-0000-4000-8000-000000000001', hash='stable-tablet-and-notes', deleted=False)
    cloud = SimpleNamespace(library=lambda: [SimpleNamespace(id='0a000000-0000-4000-8000-000000000001', parent='', deleted=False), tablet],
        pdf_bundle=lambda item: {tablet.id + '.pdf': path.read_bytes()})
    native = SimpleNamespace(server_id=client.server_id, review_action=client.review_action, get=client.get,
        file_path=client.file_path, close=lambda: None)
    worker = Worker(root / 'repair-helper', zotero_factory=lambda: native, cloud_factory=lambda: cloud)
    worker.config.update(zotero_root='RPRCL234', tablet_root='0a000000-0000-4000-8000-000000000001')
    bridge = Bridge(native, cloud, worker.state, 'RPRCL234', worker.config['tablet_root'])
    view = client.get('item', 'RPRPDF23')
    view['note'] = managed_marker(bridge.namespace, tablet.id)
    client.update(view)
    before = {key: client.get('item', key) for key in keys}
    link = {'parent': 'RPRFRM23', 'view_key': 'RPRPDF23', 'origin': 'cloud', 'baseline': sha,
        'collection': 'RPRCL234', 'name': 'Unchanged tablet name', 'revision': tablet.hash, 'uploaded': True}
    worker.state.write('documents', {tablet.id: link})
    originals = worker.state.directory / 'sources' / 'remarkable' / 'baselines'
    originals.mkdir(parents=True); (originals / (sha + '.pdf')).write_bytes(path.read_bytes())
    assert not (worker.state.directory / 'originals').exists(), 'Cloud-origin fixture must use the actual retained source layout'
    ledger = Ledger(worker.state.directory / 'publisher'); ledger.save(tablet.id, published=1, md5=hashlib.md5(path.read_bytes()).hexdigest())
    receipt = ledger.get(tablet.id)
    preview = worker.preview_link_repair(tablet.id)['link_repair']
    assert preview['parent'] == 'RPRSUR23' and worker.state.read('documents')[tablet.id] == link
    checked('preview_follows_actual_native_relation_without_rebinding_or_library_writes')
    worker.repair_link(tablet.id, preview['token'])
    repaired = worker.state.read('documents')[tablet.id]
    assert repaired['parent'] == 'RPRSUR23'
    assert {key: value for key, value in repaired.items() if key not in {'parent', 'repair_history'}} == {key: value for key, value in link.items() if key != 'parent'}
    assert ledger.get(tablet.id) == receipt and file_sha(client.file_path('RPRPDF23')) == sha
    assert {key: client.get('item', key) for key in keys} == before
    checked('confirmed_repair_preserves_native_records_PDF_bytes_notes_tablet_ID_baseline_and_publication_receipt')
    sync_merge_checks(client, root, checked)
    return None


def sync_merge_checks(client, root, checked):
    client.authorize()
    original = root / 'Audit.pdf'
    original.write_bytes(live.pdf_bytes())
    for index, collision in enumerate([False, True], 2):
        suffix = str(index)
        folder, linked, duplicate, source, other_pdf = [prefix + suffix for prefix in
            ['AUDCLL2', 'AUDLNK2', 'AUDDUP2', 'AUDPDF2', 'AUDALT2']]
        title = f'Synthetic duplicate audit {index}'
        cloud = live.MemoryCloud()
        croot = cloud.create_folder('Synthetic library', '', str(uuid.uuid4()))
        client.create('collection', {'key': folder, 'name': title})
        client.create('item', {'key': linked, 'itemType': 'journalArticle', 'title': title,
                               'collections': [folder], 'tags': [{'tag': 'Linked tag'}]})

        def pdf(key, parent, path=original):
            client.create('item', {'key': key, 'parentItem': parent, 'itemType': 'attachment', 'title': 'Audit PDF',
                                   'contentType': 'application/pdf', 'linkMode': 'imported_file', 'filename': 'Audit.pdf'})
            client.upload(key, path, '')

        pdf(source, linked)
        worker = Worker(root / f'worker-{index}',
                        zotero_factory=lambda: LocalZotero(int(client.endpoint.split(':')[2].split('/')[0]),
                                                            grant_store=client.grant_store, interactive=False),
                        cloud_factory=lambda: cloud)
        worker.config = {'server_id': client.server_id, 'zotero_root': folder, 'tablet_root': croot.id,
                         'default_folder': croot.id, 'ocr': False}
        worker.sync()
        time.sleep(2.1)
        worker.sync()
        row = next(row for row in worker.review.rows() if row['identity'] == source)
        worker.approve(row['id'], row['digest'], 'Audit.pdf', title, 'journal_article', folder, [])
        worker.sync()
        cloud_id, before = next(iter(worker.state.read('documents').items()))
        key = before.get('view_key') or source
        remote = lambda: next(item for item in cloud.library() if item.id == cloud_id)
        client.create('item', {'key': duplicate, 'itemType': 'journalArticle', 'title': title,
                               'tags': [{'tag': 'Duplicate tag'}]})
        if collision:
            current = root / 'Exact managed copy.pdf'
            current.write_bytes(client.file_path(key).read_bytes())
            pdf(other_pdf, duplicate, current)
        tablet_before = remote()
        live.command(root, 'merge_repair_fixture', survivor=duplicate, removed=linked)
        merged = client.get('item', duplicate)
        assert {tag['tag'] for tag in merged['tags']} == {'Linked tag', 'Duplicate tag'}, merged['tags']
        assert folder in merged['collections']
        if collision:
            # Simulate a notice saved by an older version before upgrading.
            worker.review.confirm_zotero_deleted(tablet_before, before, client.get('item', linked))
            decision = worker.review.local_deletions()[0]
            try:
                worker.local_delete_decision(decision['id'], 'trash_tablet')
            except PilotError as error:
                assert 'merged or reconnected' in str(error), error
            else:
                raise AssertionError('The pre-upgrade trash decision should be invalidated')
            assert not remote().deleted
            checked('stale_pre_upgrade_trash_choice_is_rejected_after_a_real_merge')
        worker.sync()
        after = worker.state.read('documents')[cloud_id]
        snapshot = {'old_pdf': client.get('item', key), 'link_parent': after['parent'],
                    'deletions': worker.review.local_deletions(), 'tablet_parent': remote().parent,
                    'warnings': worker.status()['status'].get('result', {}).get('review', [])}
        if not collision:
            assert after['parent'] == duplicate, snapshot
            assert not snapshot['deletions'] and not remote().deleted
            checked('sync_automatically_follows_verified_surviving_pdf_and_reference')
            assert {tag['name'] for tag in json.loads(cloud.pdf_bundle(remote())[cloud_id + '.content'])['tags']} == {
                'Linked tag', 'Duplicate tag'}
            checked('native_merge_unions_paper_tags_and_syncs_them_to_the_existing_tablet_document')
            # The successful sync can create additional native undo entries.
            for _ in range(8):
                if not client.get('item', linked).get('deleted'):
                    break
                live.command(root, 'undo_merge_fixture')
            restored = client.get('item', linked)
            assert restored and not restored.get('deleted'), restored
            assert client.get('item', key)['parentItem'] == linked
            worker.sync()
            undo_after = worker.state.read('documents')[cloud_id]
            snapshot['undo'] = {'link_parent': undo_after['parent'],
                                'pdf_parent': client.get('item', key)['parentItem'],
                                'deletions': worker.review.local_deletions(),
                                'warnings': worker.status()['status'].get('result', {}).get('review', [])}
            assert undo_after['parent'] == linked, snapshot['undo']
            checked('native_undo_after_plugin_repair_restores_the_original_link')
        else:
            assert snapshot['old_pdf'].get('deleted'), snapshot
            assert after['parent'] == duplicate and after['view_key'] == other_pdf and not remote().deleted, snapshot
            assert not snapshot['deletions'], snapshot
            assert (remote().name, remote().parent) == (tablet_before.name, tablet_before.parent)
            checked('native_identical_pdf_merge_rebinds_the_exact_attachment_and_preserves_the_tablet_copy')
            worker.apply_tag_edit('saved-before-merge', ['legacy', folder, croot.id], cloud_id, key, linked,
                                  before['page_map'][0], before['page_map_digest'], {'before': [], 'after': ['Late tag']},
                                  {'before': [], 'after': ['Late page tag']})
            assert 'Late tag' in {tag['tag'] for tag in client.get('item', duplicate)['tags']}
            assert worker.state.read('documents')[cloud_id]['page_tags'][before['page_map'][0]] == ['Late page tag']
            checked('queued_tag_additions_follow_a_verified_reference_and_pdf_replacement')
    check_two_links(client, root, checked)


def check_two_links(client, root, checked):
    import pymupdf
    folder = 'AUDCLL24'
    cloud = live.MemoryCloud()
    croot = cloud.create_folder('Synthetic two-link library', '', str(uuid.uuid4()))
    client.create('collection', {'key': folder, 'name': 'Synthetic two-link fixture'})
    for index, title in enumerate(['Distinct linked alpha paper', 'Distinct linked beta paper'], 4):
        parent, attachment = f'AUDLNK2{index}', f'AUDPDF2{index}'
        path = root / f'{title}.pdf'
        with pymupdf.open() as doc:
            doc.new_page().insert_text((40, 40), title)
            doc.save(path)
        client.create('item', {'key': parent, 'itemType': 'journalArticle', 'title': title, 'collections': [folder]})
        client.create('item', {'key': attachment, 'parentItem': parent, 'itemType': 'attachment', 'title': title,
                               'contentType': 'application/pdf', 'linkMode': 'imported_file', 'filename': path.name})
        client.upload(attachment, path, '')
    worker = Worker(root / 'worker-two-links',
                    zotero_factory=lambda: LocalZotero(int(client.endpoint.split(':')[2].split('/')[0]),
                                                        grant_store=client.grant_store, interactive=False),
                    cloud_factory=lambda: cloud)
    worker.config = {'server_id': client.server_id, 'zotero_root': folder, 'tablet_root': croot.id,
                     'default_folder': croot.id, 'ocr': False}
    worker.sync()
    time.sleep(2.1)
    worker.sync()
    for attachment in ['AUDPDF24', 'AUDPDF25']:
        row = next(row for row in worker.review.rows() if row['identity'] == attachment)
        title = client.get('item', attachment)['title']
        worker.approve(row['id'], row['digest'], title + '.pdf', title, 'journal_article', folder, [])
    worker.sync()
    links = worker.state.read('documents')
    assert len(links) == 2, links
    removed_id = next(key for key, link in links.items() if link['parent'] == 'AUDLNK25')
    survivor = client.get('item', 'AUDLNK24')
    removed = client.get('item', 'AUDLNK25')
    removed['title'] = survivor['title']
    client.update(removed)
    live.command(root, 'merge_repair_fixture', survivor='AUDLNK24', removed='AUDLNK25')
    assert client.get('item', 'AUDPDF25')['parentItem'] == 'AUDLNK24'
    worker.sync()
    after = worker.state.read('documents')
    assert {identity: link['parent'] for identity, link in after.items()} == {
        identity: link['parent'] for identity, link in links.items()}
    assert not worker.review.local_deletions()
    assert any(row.get('merge') and row['cloud'] == removed_id for row in worker.last['result']['review'])
    assert len([item for item in cloud.library() if item.is_pdf and not item.deleted]) == 2
    rendered = live.command(root, 'render', snapshot=worker.status(), tab='sync-review')
    assert 'Show surviving reference in Zotero' in rendered['buttons'], rendered['buttons']
    assert 'Move reMarkable copy to Trash' not in rendered['buttons'], rendered['buttons']
    checked('merging_two_linked_references_keeps_both_tablet_documents_but_leaves_one_connection_held')


if __name__ == '__main__':
    marker = "          else if (command.action === 'reindex')"
    assert marker in live.BOOTSTRAP
    live.BOOTSTRAP = live.BOOTSTRAP.replace(marker, BRANCH + marker, 1)
    live.native_checks = repair_checks
    live.main()
