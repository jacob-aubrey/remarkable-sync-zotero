import copy, json, tempfile, unittest, uuid
from pathlib import Path
from unittest.mock import patch
from tests import test_zotero_worker as worker_fixtures
from note_organizer.zotero_worker import Worker
from note_organizer.zotero_pilot import PilotError
from note_organizer import zotero_connections as connections

class ConnectionTests(unittest.TestCase):
    setUp = worker_fixtures.WorkerTests.setUp
    add_pdf = worker_fixtures.WorkerTests.add_pdf

    def roots(self, folder=None):
        return {'zotero_root': 'ROOT2345', 'tablet_root': (folder or self.croot).id,
                'default_folder': (folder or self.croot).id, 'ocr': False}

    def switch(self, folder):
        self.worker.pause_sync()
        values = self.roots(folder)
        preview = self.worker.preview_connection(**values)
        return self.worker.configure(confirmation=preview['token'], **values)

    def linked(self):
        self.add_pdf()
        with patch.object(self.worker.review, 'observe_ready', return_value=True): self.worker.sync()
        row = self.worker.review.rows()[0]
        self.worker.approve(row['id'], row['digest'], 'Reviewed.pdf', 'Dissertation', 'thesis_dissertation', 'ROOT2345')
        self.worker.sync()
        return next(iter(self.worker.state.read('documents')))

    def test_switch_without_sync_preserves_cloud_library_items_and_pairing(self):
        self.worker.sync()
        new = self.cloud.create_folder('New', '', str(uuid.uuid4()))
        tablet = {r.id:r.hash for r in self.cloud.library()}
        items = copy.deepcopy(self.zot.items)
        credential = self.worker.directory/'tablet-token.dpapi'; credential.write_bytes(b'encrypted-test-token')
        state = self.switch(new)
        self.assertEqual(state['config']['tablet_root'],new.id)
        self.assertFalse(state['config']['automatic'])
        self.assertTrue(state['config']['sync_paused'])
        self.assertEqual(tablet,{r.id:r.hash for r in self.cloud.library()})
        self.assertEqual(items,self.zot.items)
        self.assertEqual(credential.read_bytes(),b'encrypted-test-token')

    def test_root_only_binding_can_switch_and_sync_new_namespace(self):
        self.worker.sync()
        new = self.cloud.create_folder('New', '', str(uuid.uuid4()))
        old_dir = self.worker.connection_dir
        self.switch(new); self.worker.sync()
        self.assertNotEqual(self.worker.connection_dir,old_dir)
        self.assertEqual(self.worker.state.read('folders')['ROOT2345']['cloud'],new.id)

    def test_old_subfolders_and_new_descendants_are_not_copied_to_new_scope(self):
        old = self.cloud.create_folder('Bible Notes child',self.croot.id,str(uuid.uuid4()),self.croot.id)
        self.worker.sync()
        mapping = self.worker.state.read('folders')
        old_key = next(k for k,v in mapping.items() if v['cloud']==old.id)
        self.zot.collection_records['CHLD2345']={'key':'CHLD2345','name':'Later child','parentCollection':old_key}
        new = self.cloud.create_folder('Literature','',str(uuid.uuid4()))
        self.switch(new); self.worker.sync()
        self.assertEqual(len(self.worker.state.read('folders')),1)
        self.assertFalse([r for r in self.cloud.library() if r.parent==new.id])
        self.assertIn(old_key,self.zot.collection_records)

    def test_papers_tags_approvals_and_retained_sources_stay_with_old_connection(self):
        identity = self.linked()
        docs = self.worker.state.read('documents')
        docs[identity]['page_tags']={'stable-page':['Methods']}; self.worker.state.write('documents',docs)
        old_state, old_review = self.worker.state,self.worker.review
        old_review.offer('cloud','pending','digest',{'path':'test.pdf'})
        before_files = {str(p):p.read_bytes() for p in old_state.directory.rglob('*') if p.is_file()}
        new = self.cloud.create_folder('New','',str(uuid.uuid4()))
        self.switch(new)
        self.assertEqual(self.worker.state.read('documents',{}),{})
        self.assertEqual(self.worker.review.rows(),[])
        self.assertEqual(old_state.read('documents'),docs)
        self.assertEqual(len(old_review.rows()),1)
        self.assertTrue(all(Path(p).read_bytes()==data for p,data in before_files.items()))
        self.worker.sync()
        self.assertFalse(self.worker.state.read('documents'))
        self.assertFalse([r for r in self.cloud.library() if r.parent==new.id and r.is_pdf])
        self.assertEqual(self.worker.review.rows(),[])

    def test_return_to_previous_pair_restores_original_connection_and_page_tags(self):
        identity = self.linked(); old_dir = self.worker.connection_dir
        docs = self.worker.state.read('documents'); docs[identity]['page_tags']={'page':['Search']}; self.worker.state.write('documents',docs)
        new = self.cloud.create_folder('New','',str(uuid.uuid4()))
        self.switch(new)
        state = self.switch(self.croot)
        self.assertEqual(self.worker.connection_dir,old_dir)
        self.assertEqual(state['links'][0]['page_tags'],{'page':['Search']})
        self.worker.sync()
        self.assertEqual(len([r for r in self.cloud.library() if r.is_pdf]),1)

    def test_disconnect_works_offline_and_clears_active_reviews_without_erasing_them(self):
        identity = self.linked(); old_dir = self.worker.connection_dir
        self.worker.zotero_factory=lambda: (_ for _ in ()).throw(AssertionError('Offline'))
        self.worker.cloud_factory=lambda: (_ for _ in ()).throw(AssertionError('Offline'))
        preview=self.worker.preview_disconnect()
        state=self.worker.disconnect_folder(preview['token'])
        self.assertFalse(state['config']['tablet_root'])
        self.assertFalse(state['config']['automatic'])
        self.assertFalse(state['links'])
        self.assertIn(identity,connections.values(self.worker.directory,{'connection_id':'legacy'})['documents'])
        self.assertTrue((old_dir/'bridge/pilot.sqlite3').exists())
        self.worker.sync()

    def test_changed_metadata_invalidates_preview_before_configuration_commit(self):
        new=self.cloud.create_folder('New','',str(uuid.uuid4()))
        preview=self.worker.preview_connection(**self.roots(new))
        self.zot.collection_records['ROOT2345']['name']='Changed after preview'
        with self.assertRaises(PilotError): self.worker.configure(confirmation=preview['token'],**self.roots(new))
        self.assertEqual(self.worker.config['tablet_root'],self.croot.id)

    def test_changed_review_invalidates_disconnect_confirmation(self):
        preview=self.worker.preview_disconnect()
        self.worker.review.offer('cloud','new','digest',{'path':'test.pdf'})
        with self.assertRaises(PilotError): self.worker.disconnect_folder(preview['token'])
        self.assertEqual(self.worker.config['tablet_root'],self.croot.id)

    def test_confirmation_is_required_for_existing_connection(self):
        new=self.cloud.create_folder('New','',str(uuid.uuid4()))
        with self.assertRaises(PilotError): self.worker.configure(**self.roots(new))
        self.assertEqual(self.worker.config['tablet_root'],self.croot.id)

    def test_failed_atomic_commit_preserves_current_state_and_can_retry(self):
        self.worker.sync(); old_dir=self.worker.connection_dir
        new=self.cloud.create_folder('New','',str(uuid.uuid4()))
        preview=self.worker.preview_connection(**self.roots(new))
        with patch('note_organizer.zotero_worker.atomic_write',side_effect=OSError('Simulated disk failure')):
            with self.assertRaises(OSError): self.worker.configure(confirmation=preview['token'],**self.roots(new))
        self.assertEqual(self.worker.connection_dir,old_dir)
        self.assertEqual(self.worker.config['tablet_root'],self.croot.id)
        self.worker.configure(confirmation=preview['token'],**self.roots(new))

    def test_saving_preferences_preserves_active_and_paused_connections(self):
        self.worker.sync(); old_dir=self.worker.connection_dir
        self.worker.configure(automatic=True,**self.roots())
        self.assertEqual(self.worker.connection_dir,old_dir)
        self.assertFalse(self.worker.config.get('sync_paused'))
        self.worker.pause_sync()
        self.worker.configure(**{**self.roots(), 'ocr': True})
        self.assertTrue(self.worker.config['sync_paused'])
        with patch.object(self.worker,'cloud',side_effect=AssertionError('No automatic network')):
            self.worker.sync(automatic=True)
        self.worker.sync()
        self.assertFalse(self.worker.config['sync_paused'])

    def test_first_connection_enables_automatic_sync_and_records_the_last_check(self):
        self.worker.config = {}
        result = self.worker.configure(automatic=True, **self.roots())
        self.assertFalse(result['config']['sync_paused'])
        self.assertIsNone(result['last_sync_at'])
        result = self.worker.sync(automatic=True)
        self.assertGreater(result['last_sync_at'], 0)
        restored = Worker(self.worker.directory, zotero_factory=lambda: self.zot, cloud_factory=lambda: self.cloud)
        self.assertEqual(restored.status()['last_sync_at'], result['last_sync_at'])

    def test_restart_recovers_durable_pause_request(self):
        self.worker.save(); (self.worker.directory/'pause-requested.json').write_text('{}')
        restored=Worker(self.worker.directory,zotero_factory=lambda:self.zot,cloud_factory=lambda:self.cloud)
        self.assertTrue(restored.config['sync_paused'])
        self.assertFalse((restored.directory/'pause-requested.json').exists())

    def test_state_path_rejects_traversal(self):
        with self.assertRaises(PilotError): connections.state_directory(self.worker.directory,{'connection_id':'../bad'})

    def test_invalid_default_does_not_change_saved_roots(self):
        new=self.cloud.create_folder('New','',str(uuid.uuid4()))
        with self.assertRaises(PilotError): self.worker.preview_connection(tablet_root=new.id,default_folder=self.croot.id)
        self.assertEqual(self.worker.config['tablet_root'],self.croot.id)

    def test_disconnected_reconnect_restores_old_connection_after_restart(self):
        self.worker.sync(); old_dir=self.worker.connection_dir
        preview=self.worker.preview_disconnect(); self.worker.disconnect_folder(preview['token'])
        self.worker=Worker(self.worker.directory,zotero_factory=lambda:self.zot,cloud_factory=lambda:self.cloud)
        self.switch(self.croot)
        self.assertEqual(self.worker.connection_dir,old_dir)
        self.worker.sync()

    def test_automatic_request_respects_disabled_preference_after_manual_sync(self):
        self.worker.config.update(automatic=False, sync_paused=False)
        with patch.object(self.worker, 'cloud', side_effect=AssertionError('Automatic sync is disabled')):
            self.worker.sync(automatic=True)

    def test_page_tag_change_invalidates_folder_confirmation(self):
        identity = self.linked()
        new = self.cloud.create_folder('New', '', str(uuid.uuid4()))
        preview = self.worker.preview_connection(**self.roots(new))
        documents = self.worker.state.read('documents')
        documents[identity]['page_tags'] = {'page': ['Edited while preview was open']}
        self.worker.state.write('documents', documents)
        with self.assertRaises(PilotError):
            self.worker.configure(confirmation=preview['token'], **self.roots(new))

    def test_paused_metadata_requests_clear_cancel_flag_without_resuming_sync(self):
        new = self.cloud.create_folder('New', '', str(uuid.uuid4()))
        def checked_cloud():
            self.assertFalse(self.worker.stop.is_set(), 'A new read must not inherit the cancelled transfer')
            return self.cloud
        self.worker.cloud_factory = checked_cloud
        self.worker.pause_sync()
        self.worker.folders()
        self.assertTrue(self.worker.config['sync_paused'])
        preview = self.worker.preview_connection(**self.roots(new))
        self.worker.configure(confirmation=preview['token'], **self.roots(new))
        self.assertTrue(self.worker.config['sync_paused'])

    def test_local_reindex_remains_usable_while_transfers_are_paused(self):
        identity = self.linked()
        key = self.worker.state.read('documents')[identity]['view_key']
        self.worker.pause_sync()
        def index(*args):
            self.assertFalse(self.worker.stop.is_set())
            self.assertTrue(self.worker.config['sync_paused'])
            return 0
        with patch.object(self.zot, 'index', side_effect=index):
            result = self.worker.reindex(key)
        self.assertTrue(result['config']['sync_paused'])
