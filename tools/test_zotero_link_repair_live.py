"""Verify parent-only connection repair after a real merge in disposable Zotero."""
import copy
import hashlib
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import test_zotero_plugin_live as live
from note_organizer.zotero_worker import Worker, file_sha
from note_organizer.zotero_pilot import Ledger, marker as managed_marker
from note_organizer.zotero_bridge import Bridge


BRANCH = """
          else if (command.action === 'merge_repair_fixture') {
            const {mergeItems} = ChromeUtils.importESModule('chrome://zotero/content/mergeItems.mjs');
            const survivor = await Zotero.Items.getByLibraryAndKeyAsync(Zotero.Libraries.userLibraryID, 'RPRSUR23');
            const removed = await Zotero.Items.getByLibraryAndKeyAsync(Zotero.Libraries.userLibraryID, 'RPRFRM23');
            await mergeItems(survivor, [removed]); response.merged = removed.deleted;
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
    return None


if __name__ == '__main__':
    marker = "          else if (command.action === 'reindex')"
    assert marker in live.BOOTSTRAP
    live.BOOTSTRAP = live.BOOTSTRAP.replace(marker, BRANCH + marker, 1)
    live.native_checks = repair_checks
    live.main()
