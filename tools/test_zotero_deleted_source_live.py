"""Retire a trashed spare intake review using a disposable, real Zotero library.

With --xpi, the installed bundled worker performs the local recheck through
the plugin RPC; source Worker is used only to seed and inspect the test state.
Without --xpi, the source worker performs the local recheck. Native records, PDFs, bridge
links, publication receipts, and unrelated applying journals must stay intact.
The common harness can install the exact release XPI for the native adapter.
"""
import copy
import hashlib
import json
from pathlib import Path
import sys
import zipfile
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import test_zotero_plugin_live as live
from note_organizer.zotero_worker import Worker, file_sha
from note_organizer.zotero_bulk import review_policy
from note_organizer.zotero_pilot import Ledger, marker as managed_marker
from note_organizer.zotero_bridge import Bridge


BRANCH = """
          else if (command.action === 'deleted_stop_helper') {
            clearTimeout(ui.timer);
            if (ui.process) await ui.closeProcess(ui.process, true, 'synthetic_deleted_source_seed');
            ui.view?.close(); ui.view = null;
          }
          else if (command.action === 'deleted_frozen_recheck') {
            if (Zotero.Prefs.get('notable.developmentWorker', true)) throw Error('Expected the actual bundled worker, not a development override');
            response.result = await ui.rpc('recheck_review');
            const info = ui.processTrace(ui.process);
            response.runtime = {id: info.runtime_id, executable_md5: info.executable_md5};
            const executable = PathUtils.join(ui.statePath, 'runtime', info.runtime_id, 'notable-worker.exe');
            response.actualExecutableMD5 = await Zotero.Utilities.Internal.md5Async(executable);
          }
          else if (command.action === 'trash_spare_fixture') {
            const spare = await Zotero.Items.getByLibraryAndKeyAsync(Zotero.Libraries.userLibraryID, 'DELSPA23');
            if (!spare || !spare.isPDFAttachment()) throw Error('Synthetic spare PDF is unavailable');
            await Zotero.DB.executeTransaction(async () => { await Zotero.Items.trash([spare.id]); });
            response.deleted = !!spare.deleted;
          }
"""


def deleted_source_checks(client, root, checked):
    client.authorize()
    collection, parent, spare, view = 'DELCL234', 'DELPAR23', 'DELSPA23', 'DELVIE23'
    tablet_root = '0b000000-0000-4000-8000-000000000001'
    tablet_id = '0b000000-0000-4000-8000-000000000002'
    client.create('collection', {'key': collection, 'name': 'Synthetic deleted-spare fixture'})
    client.create('item', {'key': parent, 'itemType': 'journalArticle',
        'title': 'Synthetic paper with a removed spare PDF', 'collections': [collection], 'date': '2024'})
    source = root / 'synthetic-spare.pdf'; source.write_bytes(live.pdf_bytes())
    for key, title in [(spare, 'Spare PDF without notes'), (view, 'Main text (reMarkable notes)')]:
        client.create('item', {'key': key, 'itemType': 'attachment', 'parentItem': parent,
            'title': title, 'contentType': 'application/pdf', 'linkMode': 'imported_file', 'filename': key + '.pdf'})
        client.upload(key, source, '')
    paths = {key: client.file_path(key) for key in [spare, view]}
    native = SimpleNamespace(server_id=client.server_id, review_action=client.review_action, get=client.get,
        file_path=client.file_path, collections=client.collections, close=lambda: None)

    def forbidden_cloud():
        raise AssertionError('A local deleted-source recheck must not construct a cloud client')

    packaged = any(value == '--xpi' or value.startswith('--xpi=') for value in sys.argv[1:])
    if packaged:
        live.command(root, 'deleted_stop_helper')
    state_directory = root / 'profile/notable' if packaged else root / 'deleted-source-helper'
    worker = Worker(state_directory, zotero_factory=lambda: native, cloud_factory=forbidden_cloud)
    worker.config.update(server_id=client.server_id, zotero_root=collection, tablet_root=tablet_root,
                         automatic=False, sync_paused=True)
    worker.save()
    bridge = Bridge(native, SimpleNamespace(), worker.state, collection, tablet_root)
    linked = client.get('item', view)
    linked['note'] = '<p>' + managed_marker(bridge.namespace, tablet_id) + '</p><p>Retained synthetic note</p>'
    client.update(linked)
    digest = file_sha(paths[spare])
    link = {'parent': parent, 'view_key': view, 'origin': 'cloud', 'baseline': digest,
        'collection': collection, 'name': 'Keep this tablet document', 'revision': 'unchanged-tablet-and-notes',
        'uploaded': True, 'role': 'main', 'page_tags': {'synthetic-page': ['Retain this tag']}}
    worker.state.write('documents', {tablet_id: link})
    worker.state.write('folders', {collection: {'cloud': tablet_root, 'name': 'Synthetic linked folder'}})
    publisher = Ledger(worker.state.directory / 'publisher')
    publisher.save(tablet_id, published=1, md5=hashlib.md5(paths[view].read_bytes()).hexdigest(), indexed_sha=digest)
    publisher.reader_revision(tablet_id, digest)
    publisher.upload_intent(tablet_id, {'synthetic': 'Retain this pending receipt untouched'})

    old_payload = {'source': 'zotero', 'parent': parent, 'attachment': spare, 'path': str(paths[spare]),
        'title': 'Synthetic paper with a removed spare PDF', 'filename': 'Original spare name.pdf',
        'document_type': 'journal_article', 'details': {}, 'matches': [], 'possible_tablet_matches': [],
        'routine': False, 'proposal_revision': 2, 'guidance_version': 1, 'guidance_state': 'repair',
        'review_token': 'legacy-v1-saved-review', 'issues': ['This reference has multiple PDFs.'],
        'findings': [{'code': 'multiple_pdf_attachments', 'domain': 'library', 'severity': 'decision',
            'summary': 'Identify the role of this PDF among the reference attachments',
            'explanation': 'Older saved warning before the spare was removed.', 'next_step': 'Inspect the PDFs.',
            'evidence': ['2 existing PDF attachments under this reference.'],
            'references': [{'key': parent, 'title': 'Synthetic paper'}],
            'attachments': [{'key': spare, 'parent': parent, 'filename': spare + '.pdf', 'match_basis': 'context'},
                {'key': view, 'parent': parent, 'filename': view + '.pdf', 'match_basis': 'context'}]}]}
    review_id = worker.review.offer('zotero', spare, digest, old_payload)
    applying_id = worker.review.offer('zotero', 'DELAPP23', digest, {
        **copy.deepcopy(old_payload), 'title': 'Unrelated interrupted filing', 'attachment': 'DELAPP23', 'parent': 'OTHER234'})
    worker.review.begin(applying_id, digest, {'filename': 'Previously approved.pdf', 'title': 'Unrelated saved title',
        'checkpoint': {'native_commit': True, 'must_remain_byte_equivalent': [1, 2, 3]}})
    applying_before = worker.review.row(applying_id)
    with worker.review.connect() as db:
        applying_raw_before = dict(db.execute('SELECT * FROM review_queue WHERE id=?', (applying_id,)).fetchone())

    assert live.command(root, 'trash_spare_fixture')['deleted']
    assert client.get('item', spare)['deleted']
    catalog_before = client.review_action('catalog')
    current_pdfs = [entry for entry in catalog_before['attachments']
        if entry['data'].get('parentItem') == parent and Path(entry['path']).suffix.lower() == '.pdf']
    assert [entry['data']['key'] for entry in current_pdfs] == [view], current_pdfs
    checked('real_native_Trash_leaves_exactly_one_current_PDF_and_retains_the_linked_view')
    findings = {}
    review_policy([worker.review.row(review_id)], catalog_before, worker.state.read('documents'),
        worker.state.read('folders'), findings=findings)
    assert not any(finding['code'] == 'multiple_pdf_attachments' for finding in findings[review_id])
    checked('deleted_saved_source_is_not_counted_as_a_second_live_PDF')

    def capture():
        return {'native': {key: client.get('item', key) for key in [parent, spare, view]},
            'files': {key: {'exists': path.is_file(), 'sha256': file_sha(path) if path.is_file() else None}
                for key, path in paths.items()},
            'input': {'exists': source.is_file(), 'sha256': file_sha(source)},
            'bridge': file_sha(worker.state.path), 'publisher': file_sha(publisher.path),
            'documents': worker.state.read('documents'), 'receipt': publisher.upload_intent(tablet_id)}

    before = capture()
    assert before['files'][spare]['exists'] and before['files'][view]['exists']
    execution = {'mode': 'source-worker'}
    if packaged:
        assert not (state_directory / 'tablet-token.dpapi').exists(), 'Disposable profile must not have tablet credentials'
        executed = live.command(root, 'deleted_frozen_recheck')
        result = executed['result']
        with zipfile.ZipFile(root / 'profile/extensions/notable-companion@local.invalid.xpi') as archive:
            manifest = json.loads(archive.read('runtime-manifest.json'))
        executable = next(entry for entry in manifest['files'] if entry['path'] == 'notable-worker.exe')
        assert executed['runtime']['id'] == manifest['id']
        assert executed['runtime']['executable_md5'] == executable['md5'] == executed['actualExecutableMD5']
        execution = {'mode': 'bundled-frozen-worker', 'runtime_id': manifest['id'],
                     'executable_md5': executed['actualExecutableMD5'], 'rpc_method': 'recheck_review'}
        checked('deleted_source_recheck_executes_the_installed_bundled_worker_with_no_development_override')
        assert not (state_directory / 'tablet-token.dpapi').exists()
    else:
        result = worker.recheck_review()
    saved = worker.review.row(review_id)
    assert saved['status'] == 'resolved' and saved['approval'] == {}, saved
    assert saved['payload'] == old_payload, 'Resolved history must retain the original saved evidence'
    assert result['review_recheck']['resolved_ids'] == [review_id]
    assert review_id not in {row['id'] for row in result['reviews']}
    assert worker.review.row(applying_id) == applying_before, 'An unrelated applying journal was changed'
    assert worker.review.row(applying_id)['status'] == 'applying'
    with worker.review.connect() as db:
        assert dict(db.execute('SELECT * FROM review_queue WHERE id=?', (applying_id,)).fetchone()) == applying_raw_before, 'The applying journal’s raw stored columns changed'
    checked('local_recheck_closes_only_the_deleted_unlinked_spare_and_preserves_the_applying_journal')
    assert capture() == before, 'Local recheck changed a native item, PDF, source, bridge link, or publication receipt'
    assert client.review_action('catalog') == catalog_before
    assert worker.state.read('documents')[tablet_id]['view_key'] == view
    checked('native_records_PDF_bytes_source_files_tablet_identity_bridge_and_publisher_remain_unchanged')
    (root / 'deleted-source-evidence.json').write_text(json.dumps({'resolved_review': review_id,
        'preserved_applying_review': applying_id, 'live_attachment': view, 'trashed_attachment': spare,
        'execution': execution, 'recheck': result['review_recheck'], 'native_and_file_snapshots_equal': True,
        'bridge_and_publication_snapshots_equal': True}, indent=2), encoding='utf-8')
    if packaged:
        # Reopen only after preservation checks; the common harness captures
        # its final screenshot from the ordinary plugin window.
        live.command(root, 'open')
    return None


if __name__ == '__main__':
    marker = "          else if (command.action === 'reindex')"
    assert marker in live.BOOTSTRAP
    live.BOOTSTRAP = live.BOOTSTRAP.replace(marker, BRANCH + marker, 1)
    live.native_checks = deleted_source_checks
    live.main()
