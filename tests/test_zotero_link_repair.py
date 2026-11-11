"""Native-merge repairs preserve PDFs, tablet identity, notes, and receipts."""
import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from note_organizer.zotero_worker import Worker, file_sha
from note_organizer.zotero_pilot import Ledger, PilotError, marker
from note_organizer.zotero_bridge import Bridge
from tests.test_cloud_outbox import pdf_bytes


class LinkRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'connected.pdf'
        self.path.write_bytes(pdf_bytes())
        self.cloud_id = '0a000000-0000-4000-8000-000000000002'
        self.cloud_root_id = '0a000000-0000-4000-8000-000000000001'
        self.parent = {'key': 'SURVIV23', 'itemType': 'journalArticle', 'title': 'Native merge survivor', 'collections': ['ROOT2345']}
        self.record = {'key': 'VIEW2345', 'itemType': 'attachment', 'parentItem': 'SURVIV23', 'contentType': 'application/pdf'}
        self.records = {'VIEW2345': self.record}
        self.inventory = [{'key': 'NOTE2345', 'signature': 'native-note-and-annotation-state'}]
        self.tablet = SimpleNamespace(id=self.cloud_id, parent=self.cloud_root_id, deleted=False, hash='cloud-revision')
        self.tablet_pdf = self.path.read_bytes()
        self.native_calls = []
        self.native = SimpleNamespace(server_id='synthetic-server', close=lambda: None,
            review_action=self.native_action, get=lambda kind, key: copy.deepcopy(self.records.get(key)),
            file_path=lambda key: self.path)
        self.cloud_root = SimpleNamespace(id=self.cloud_root_id, parent='', deleted=False)
        self.cloud_items = [self.cloud_root, self.tablet]
        self.cloud = SimpleNamespace(library=lambda: self.cloud_items,
            pdf_bundle=lambda item: {self.cloud_id + '.pdf': self.tablet_pdf})
        self.worker = Worker(self.root / 'helper', zotero_factory=lambda: self.native, cloud_factory=lambda: self.cloud)
        self.worker.config.update(zotero_root='ROOT2345', tablet_root=self.cloud_root_id)
        bridge = Bridge(self.native, self.cloud, self.worker.state, 'ROOT2345', self.cloud_root_id)
        self.owner_marker = marker(bridge.namespace, self.cloud_id)
        self.record.update(linkMode='imported_file', note=self.owner_marker)
        self.link = {'parent': 'FORMER23', 'view_key': 'VIEW2345', 'origin': 'cloud', 'baseline': file_sha(self.path),
            'collection': 'ROOT2345', 'name': 'Existing tablet name', 'revision': 'cloud-revision', 'uploaded': True}
        self.worker.state.write('documents', {self.cloud_id: self.link})
        self.original = self.worker.state.directory / 'sources' / 'remarkable' / 'baselines' / (self.link['baseline'] + '.pdf')
        self.original.parent.mkdir(parents=True); self.original.write_bytes(self.path.read_bytes())
        self.publisher = Ledger(self.worker.state.directory / 'publisher')
        self.publisher.save(self.cloud_id, published=1, md5=hashlib.md5(self.path.read_bytes()).hexdigest())

    def native_action(self, action, data):
        self.native_calls.append(action)
        self.assertEqual(action, 'inspect_review_reference', 'Repair must never write to Zotero')
        return {'requested': data['key'], 'parent': copy.deepcopy(self.parent), 'inventory': copy.deepcopy(self.inventory)}

    def test_automatic_merge_recovery_clears_only_stale_parent_notice(self):
        self.records['FORMER23'] = {'key': 'FORMER23', 'itemType': 'journalArticle', 'deleted': True}
        self.worker.review.confirm_zotero_deleted(self.tablet, self.link, self.records['FORMER23'])
        before = self.path.read_bytes(); receipt = self.publisher.get(self.cloud_id)
        self.assertTrue(self.worker._reconcile_merged_parent(self.cloud_id, self.native))
        self.assertEqual(self.worker.state.read('documents')[self.cloud_id]['parent'], 'SURVIV23')
        self.assertEqual(self.worker.review.local_deletions(), [])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.publisher.get(self.cloud_id), receipt)

    def test_automatic_merge_recovery_rejects_changed_or_deleted_pdf(self):
        self.records['FORMER23'] = {'key': 'FORMER23', 'itemType': 'journalArticle', 'deleted': True}
        self.path.write_bytes(b'changed annotation bytes')
        self.assertFalse(self.worker._reconcile_merged_parent(self.cloud_id, self.native))
        self.assertEqual(self.worker.state.read('documents')[self.cloud_id]['parent'], 'FORMER23')
        self.record['deleted'] = True
        self.assertFalse(self.worker._reconcile_merged_parent(self.cloud_id, self.native))

    def preview(self):
        return self.worker.preview_link_repair(self.cloud_id)['link_repair']

    def test_preview_is_read_only_and_confirmation_changes_only_parent_and_audit(self):
        before = self.worker.state.path.read_bytes()
        pdf = self.path.read_bytes()
        receipt = self.publisher.get(self.cloud_id)
        self.worker.review.link_control(self.cloud_id, paused=True)
        preview = self.preview()
        self.assertEqual(before, self.worker.state.path.read_bytes())
        self.assertEqual(preview['parent'], 'SURVIV23')
        self.worker.repair_link(self.cloud_id, preview['token'])
        repaired = self.worker.state.read('documents')[self.cloud_id]
        self.assertEqual(repaired.pop('parent'), 'SURVIV23')
        history = repaired.pop('repair_history')
        self.assertEqual(history[0]['from'], 'FORMER23')
        self.assertEqual(repaired, {k: v for k, v in self.link.items() if k != 'parent'})
        self.assertEqual(self.publisher.get(self.cloud_id), receipt)
        self.assertEqual(self.path.read_bytes(), pdf)
        self.assertTrue(self.worker.review.link_control(self.cloud_id)['paused'])
        self.assertEqual(self.native_calls, ['inspect_review_reference', 'inspect_review_reference'])

    def test_cloud_origin_uses_retained_source_baseline_without_desktop_originals(self):
        self.assertFalse((self.worker.state.directory / 'originals').exists())
        self.assertTrue(self.original.is_file())
        preview = self.preview()
        self.worker.repair_link(self.cloud_id, preview['token'])
        self.assertEqual(self.worker.state.read('documents')[self.cloud_id]['parent'], 'SURVIV23')
        self.assertEqual(file_sha(self.original), self.link['baseline'])
        self.assertFalse((self.worker.state.directory / 'originals').exists())

    def test_desktop_and_legacy_origins_reuse_their_existing_baseline_store(self):
        desktop = self.worker.state.directory / 'originals' / self.original.name
        desktop.parent.mkdir()
        self.original.replace(desktop)
        for origin in ['zotero', 'cloud']:
            with self.subTest(origin=origin):
                self.worker.state.write('documents', {self.cloud_id: {**self.link, 'origin': origin}})
                self.assertEqual(self.preview()['parent'], 'SURVIV23')
        desktop.write_bytes(b'corrupt original')
        with self.assertRaisesRegex(PilotError, 'retained original could not be verified'):
            self.preview()

    def test_deleted_tablet_root_or_ancestor_blocks_repair(self):
        self.cloud_root.deleted = True
        with self.assertRaisesRegex(PilotError, 'sync root is missing'):
            self.preview()
        self.cloud_root.deleted = False
        folder = SimpleNamespace(id='nested-folder', parent=self.cloud_root_id, deleted=True)
        self.cloud_items.append(folder)
        self.tablet.parent = folder.id
        with self.assertRaisesRegex(PilotError, 'outside its linked folders'):
            self.preview()
        folder.deleted = False
        self.assertEqual(self.preview()['parent'], 'SURVIV23')
        self.tablet.deleted = True
        with self.assertRaisesRegex(PilotError, 'outside its linked folders'):
            self.preview()

    def test_managed_view_requires_its_ownership_marker_and_stored_file_mode(self):
        for field, value in [('note', ''), ('note', 'Notable pilot: a different connection'), ('linkMode', 'linked_file')]:
            with self.subTest(field=field, value=value):
                previous = self.record[field]
                self.record[field] = value
                with self.assertRaisesRegex(PilotError, 'ownership or storage mode'):
                    self.preview()
                self.record[field] = previous

    def test_separate_source_requires_the_existing_view_source_marker(self):
        self.records['SOURCE23'] = {'key': 'SOURCE23', 'itemType': 'attachment',
            'parentItem': 'SURVIV23', 'contentType': 'application/pdf', 'linkMode': 'imported_file'}
        self.worker.state.write('documents', {self.cloud_id: {**self.link, 'source_key': 'SOURCE23'}})
        with self.assertRaisesRegex(PilotError, 'source ownership changed'):
            self.preview()
        self.record['note'] += '<p>Notable source attachment: SOURCE23</p>'
        self.assertEqual(self.preview()['parent'], 'SURVIV23')

    def test_confirmed_repair_uses_the_existing_bridge_process_lock(self):
        preview = self.preview()
        before = self.worker.state.read('documents')
        with self.worker.state.lock():
            with self.assertRaisesRegex(PilotError, 'already in use'):
                self.worker.repair_link(self.cloud_id, preview['token'])
        self.assertEqual(self.worker.state.read('documents'), before)

    def test_rejects_stale_native_notes_tablet_and_local_connection(self):
        for change in ['notes', 'tablet', 'parent', 'pause', 'link']:
            with self.subTest(change=change):
                preview = self.preview()
                restore = (copy.deepcopy(self.inventory), self.tablet.hash, copy.deepcopy(self.parent), self.worker.state.read('documents'))
                if change == 'notes': self.inventory[0]['signature'] = 'changed'
                if change == 'tablet': self.tablet.hash = 'changed'
                if change == 'parent': self.parent['title'] = 'Changed native title'
                if change == 'pause': self.worker.review.link_control(self.cloud_id, paused=True)
                if change == 'link': self.worker.state.write('documents', {self.cloud_id: {**self.link, 'name': 'Changed name'}})
                before = self.worker.state.path.read_bytes()
                with self.assertRaises(PilotError): self.worker.repair_link(self.cloud_id, preview['token'])
                self.assertEqual(self.worker.state.path.read_bytes(), before)
                self.inventory, self.tablet.hash, self.parent, links = restore
                self.worker.state.write('documents', links)
                self.worker.review.link_control(self.cloud_id, paused=False)

    def test_blocks_missing_or_changed_pdf_and_unfinished_publication(self):
        preview = self.preview()
        self.path.write_bytes(self.path.read_bytes() + b'changed bytes')
        with self.assertRaisesRegex(PilotError, 'PDF changed'): self.worker.repair_link(self.cloud_id, preview['token'])
        self.path.write_bytes(self.tablet_pdf)
        self.record['deleted'] = True
        with self.assertRaisesRegex(PilotError, 'removed or replaced'): self.preview()
        self.record.pop('deleted')
        self.publisher.save(self.cloud_id, pending_md5='pending')
        with self.assertRaisesRegex(PilotError, 'unfinished publication'): self.preview()
        self.publisher.save(self.cloud_id, pending_md5='')
        self.publisher.upload_intent(self.cloud_id, {'retained': 'receipt'})
        with self.assertRaisesRegex(PilotError, 'unfinished publication'): self.preview()
        self.assertEqual(self.publisher.upload_intent(self.cloud_id), {'retained': 'receipt'})

    def test_blocks_other_connection_wrong_scope_and_changed_tablet_original(self):
        self.worker.state.write('documents', {self.cloud_id: self.link, 'another-tablet': {'parent': 'SURVIV23'}})
        with self.assertRaisesRegex(PilotError, 'Another tablet connection'): self.preview()
        self.worker.state.write('documents', {self.cloud_id: self.link})
        self.parent['collections'] = []
        with self.assertRaisesRegex(PilotError, 'outside its linked collection'): self.preview()
        self.parent['collections'] = ['ROOT2345']
        self.tablet_pdf += b'changed'
        with self.assertRaisesRegex(PilotError, 'tablet original changed'): self.preview()
        self.assertEqual(self.worker.state.read('documents')[self.cloud_id], self.link)

    def test_rejects_healthy_connection_instead_of_faking_a_repair(self):
        self.parent['key'] = 'FORMER23'
        with self.assertRaisesRegex(PilotError, 'already uses'): self.preview()


if __name__ == '__main__':
    unittest.main()
