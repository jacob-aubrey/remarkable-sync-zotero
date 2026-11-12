"""Phone references transition to local filing without cloud access."""
import unittest
from unittest.mock import patch
from tests import test_zotero_worker as worker_tests


class LocalIntakeTests(unittest.TestCase):
    setUp = worker_tests.WorkerTests.setUp
    add_pdf = worker_tests.WorkerTests.add_pdf

    def test_phone_reference_retires_when_pdf_arrives(self):
        self.zot.items['PARENT23'] = {'key': 'PARENT23', 'itemType': 'journalArticle',
            'title': 'Shared on phone', 'url': 'https://example.org/article', 'collections': []}
        self.worker.cloud_factory = lambda: self.fail('Local discovery contacted cloud')
        result = self.worker.scan_inbox(discover=True)
        row = next(r for r in result['reviews'] if r['kind'] == 'reference')
        self.assertEqual(row['payload']['url'], 'https://example.org/article')
        self.assertTrue(self.zot.closed)
        self.add_pdf()
        with patch.object(self.worker, '_offer') as offer:
            result = self.worker.scan_inbox(discover=True)
            self.assertEqual(offer.call_args.args[:2], ('zotero', 'SOURCE23'))
        self.assertFalse(any(r['kind'] == 'reference' for r in result['reviews']))

    def test_dismissal_survives_metadata_edits_but_pdf_still_enters_review(self):
        self.zot.items['PARENT23'] = {'key': 'PARENT23', 'itemType': 'webpage', 'title': 'Saved link', 'collections': []}
        row = self.worker.scan_inbox(discover=True)['reviews'][0]
        self.worker.dismiss_review(row['id'], row['digest'])
        self.zot.items['PARENT23']['title'] = 'Updated title'
        self.assertEqual(self.worker.scan_inbox(discover=True)['reviews'], [])
        self.add_pdf()
        with patch.object(self.worker, '_offer') as offer:
            self.worker.scan_inbox(discover=True)
            offer.assert_called_once()

    def test_pdf_keeps_reference_inbox_order_through_preparation(self):
        self.zot.items['PARENT23'] = {'key': 'PARENT23', 'itemType': 'journalArticle',
            'title': 'Shared on phone', 'collections': []}
        original = self.worker.scan_inbox(discover=True)['reviews'][0]
        self.zot.items['OTHER234'] = {'key': 'OTHER234', 'itemType': 'journalArticle',
            'title': 'Arrived later', 'collections': []}
        self.worker.scan_inbox(discover=True)
        self.add_pdf()
        proposal = {'title': 'Downloaded paper', 'details': {}, 'parent': 'PARENT23'}
        with patch.object(self.worker, '_proposal', return_value=proposal):
            preparing = self.worker.scan_inbox(discover=True)
            self.assertTrue(preparing['native_files_settling'])
            row = next(row for row in preparing['reviews'] if row['id'] == original['id'])
            self.assertEqual(row['payload']['pdf_state'], 'preparing')
            self.assertEqual(row['payload']['preparing_attachment'], 'SOURCE23')
            with patch('note_organizer.zotero_review.time.time', return_value=9999999999):
                settled = self.worker.scan_inbox(discover=True)['reviews']
        self.assertNotIn(original['id'], {row['id'] for row in settled})
        filing = next(row for row in settled if row['identity'] == 'SOURCE23')
        other = next(row for row in settled if row['identity'] == 'OTHER234')
        self.assertEqual(filing['inbox_order'], original['inbox_order'])
        self.assertLess(filing['inbox_order'], other['inbox_order'])

    def test_deleted_reference_is_retired_and_restore_returns_it(self):
        parent = {'key': 'PARENT23', 'itemType': 'webpage', 'title': 'Saved link', 'collections': []}
        self.zot.items['PARENT23'] = parent
        self.assertEqual(len(self.worker.scan_inbox(discover=True)['reviews']), 1)
        del self.zot.items['PARENT23']
        self.assertEqual(self.worker.scan_inbox(discover=True)['reviews'], [])
        self.zot.items['PARENT23'] = parent
        self.assertEqual(len(self.worker.scan_inbox(discover=True)['reviews']), 1)

    def test_paused_tablet_sync_does_not_stop_local_discovery(self):
        self.worker.stop.set()
        self.worker.pause_pending.set()
        self.add_pdf()
        with patch.object(self.worker, '_offer') as offer:
            self.worker.dispatch('scan_inbox', {'discover': True})
            offer.assert_called_once()
        self.assertTrue(self.worker.stop.is_set())
        self.assertTrue(self.worker.pause_pending.is_set())

    def test_paused_download_becomes_ready_for_confirmation_without_sync(self):
        self.add_pdf()
        self.worker.stop.set()
        with patch.object(self.worker, '_proposal', return_value={'title': 'Downloaded paper', 'details': {}, 'parent': 'PARENT23'}):
            initial = self.worker.scan_inbox(discover=True)
            self.assertTrue(initial['native_files_settling'])
            self.assertEqual(initial['reviews'], [])
            with patch('note_organizer.zotero_review.time.time', return_value=9999999999):
                result = self.worker.scan_inbox(discover=True)
        self.assertFalse(result['native_files_settling'])
        row = next(r for r in result['reviews'] if r['kind'] == 'zotero')
        self.assertIn(row['payload']['guidance_state'], {'ready', 'repair'})
        self.assertTrue(self.worker.stop.is_set())

    def test_sync_policy_does_not_classify_link_only_reference_as_conflict(self):
        self.zot.items['PARENT23'] = {'key': 'PARENT23', 'itemType': 'webpage', 'title': 'Saved link'}
        before = self.worker.scan_inbox(discover=True)['reviews'][0]
        self.worker.refresh_review_policy(self.zot)
        self.assertEqual(self.worker.review.row(before['id'])['payload'], before['payload'])

    def test_native_failure_keeps_pending_reference(self):
        self.zot.items['PARENT23'] = {'key': 'PARENT23', 'itemType': 'webpage', 'title': 'Saved link'}
        before = self.worker.scan_inbox(discover=True)['reviews']
        with patch.object(self.zot, 'review_action', side_effect=OSError('offline')):
            with self.assertRaises(OSError):
                self.worker.scan_inbox(discover=True)
        self.assertEqual(self.worker.status()['reviews'], before)

    def test_reference_distinguishes_absent_unavailable_and_unverified_pdf(self):
        self.zot.items['PARENT23'] = {'key': 'PARENT23', 'itemType': 'patent', 'title': 'Test patent'}
        catalog = self.zot.review_action('catalog')
        with patch.object(self.zot, 'review_action', return_value=catalog):
            original = self.worker.scan_inbox(discover=True)['reviews'][0]
            self.assertEqual(original['payload']['pdf_state'], 'none')
            for state in ('unavailable', 'unknown', 'unsupported'):
                catalog['pdf_inventory'] = {'PARENT23': {state: ['SOURCE23']}}
                current = self.worker.scan_inbox(discover=True)['reviews'][0]
                self.assertEqual(current['id'], original['id'])
                self.assertEqual(current['payload']['pdf_state'], state)
            catalog['pdf_inventory'] = {'PARENT23': {'managed': ['SOURCE23'], 'unavailable': ['MISSING2']}}
            self.assertEqual(self.worker.scan_inbox(discover=True)['reviews'], [])
        self.assertEqual(self.worker.review.row(original['id'])['status'], 'superseded')

    def test_managed_pdf_and_trashed_spare_do_not_offer_two_inbox_entries(self):
        spare = self.add_pdf()
        native_source = self.zot.items['SOURCE23']
        native_source['deleted'] = True
        self.zot.items['PARENT23']['itemType'] = 'patent'
        self.zot.items['VIEW2345'] = {**native_source, 'key': 'VIEW2345', 'deleted': False,
            'title': 'Main text (reMarkable notes)', 'note': 'Notable pilot: managed view'}
        view = self.zot.file_path('VIEW2345')
        view.write_bytes(worker_tests.pdf_bytes())
        self.worker.state.write('documents', {'tablet': {'parent': 'PARENT23', 'view_key': 'VIEW2345'}})
        queued = self.worker.review.offer('zotero', 'SOURCE23', worker_tests.file_sha(spare), {
            'path': str(spare), 'parent': 'PARENT23', 'title': 'Original patent',
        })
        before = self.worker.review.row(queued)
        with patch.object(self.worker, '_offer') as offer:
            for _ in range(2):
                self.assertEqual(self.worker.scan_inbox(discover=True)['reviews'], [])
            offer.assert_not_called()
        self.assertEqual(self.worker.review.row(queued), {**before, 'status': 'resolved'})
        self.assertTrue(spare.is_file())
        self.assertTrue(view.is_file())

    def test_existing_review_for_managed_pdf_retires_only_after_exact_ownership_check(self):
        path = self.add_pdf()
        self.zot.items['SOURCE23']['note'] = 'Notable pilot: old connection'
        digest = worker_tests.file_sha(path)
        identity = self.worker.review.offer('zotero', 'SOURCE23', digest,
            {'path': str(path), 'parent': 'PARENT23', 'title': 'Saved patent'})
        with self.assertRaisesRegex(worker_tests.PilotError, 'managed by reMarkable Sync'):
            self.worker.approve(identity, digest, path.name, 'Saved patent', 'patent', 'ROOT2345')
        review = self.worker.scan_inbox(discover=True)['reviews'][0]
        self.assertEqual(review['id'], identity)
        self.assertEqual(review['payload']['source_availability'], 'managed')
        self.assertEqual(review['payload']['guidance_state'], 'repair')
        self.worker.state.write('documents', {'tablet': {'parent': 'PARENT23', 'view_key': 'SOURCE23'}})
        self.assertEqual(self.worker.scan_inbox(discover=True)['reviews'], [])
        resolved = {key: value for key, value in review.items() if key != 'inbox_order'}
        self.assertEqual(self.worker.review.row(identity), {**resolved, 'status': 'resolved'})
        self.assertEqual(worker_tests.file_sha(path), digest)

    def test_archived_link_parent_without_a_local_pdf_needs_no_reference_placeholder(self):
        from note_organizer.zotero_bridge import BridgeState
        self.zot.items['PARENT23'] = {'key': 'PARENT23', 'itemType': 'patent', 'title': 'Archived patent'}
        row = self.worker.scan_inbox(discover=True)['reviews'][0]
        identity = 'a' * 32
        self.worker.config['connection_history'] = [{'connection_id': identity}]
        archived = BridgeState(self.worker.directory / 'connections' / identity / 'bridge')
        archived.write('documents', {'older-tablet': {'parent': 'PARENT23', 'view_key': 'VIEW2345'}})
        self.assertEqual(self.worker.scan_inbox(discover=True)['reviews'], [])
        self.assertEqual(self.worker.review.row(row['id'])['status'], 'superseded')

    def test_supported_office_attachment_does_not_also_prompt_for_pdf(self):
        self.add_pdf()
        catalog = self.zot.review_action('catalog')
        catalog['attachments'][0]['path'] = str(self.root / 'Conference presentation.pptx')
        with (patch.object(self.zot, 'review_action', return_value=catalog),
              patch.object(self.worker, '_offer') as offer):
            self.assertEqual(self.worker.scan_inbox(discover=True)['reviews'], [])
        self.assertEqual(offer.call_args.args[:2], ('zotero', 'SOURCE23'))

    def test_unverified_pending_source_is_preserved_as_repair_without_a_second_reference(self):
        path = self.add_pdf()
        identity = self.worker.review.offer('zotero', 'SOURCE23', worker_tests.file_sha(path),
            {'path': str(path), 'parent': 'PARENT23', 'title': 'Saved patent'})
        catalog = self.zot.review_action('catalog')
        catalog['attachments'].clear()
        with patch.object(self.zot, 'review_action', return_value=catalog):
            for state, availability in [('unknown', 'unknown'), ('unavailable', 'missing')]:
                catalog['pdf_inventory'] = {'PARENT23': {state: ['SOURCE23']}}
                rows = self.worker.scan_inbox(discover=True)['reviews']
                self.assertEqual([(row['id'], row['kind']) for row in rows], [(identity, 'zotero')])
                payload = rows[0]['payload']
                self.assertEqual(payload['guidance_state'], 'repair')
                self.assertEqual(payload['source_availability'], availability)
                self.assertFalse(payload['source_removed'])
        self.assertTrue(path.is_file())

    def test_existing_filing_waits_for_changed_file_then_recovers(self):
        path = self.add_pdf()
        original = path.read_bytes()
        review_id = self.worker.review.offer('zotero', 'SOURCE23', worker_tests.file_sha(path),
            {'path': str(path), 'parent': 'PARENT23', 'title': 'Saved paper'})
        path.write_bytes(b'%PDF-incomplete')
        with patch.object(self.worker.review, 'observe_ready', side_effect=[False, True, True]):
            settling = self.worker.scan_inbox(discover=True)
            self.assertTrue(settling['native_files_settling'])
            self.assertEqual(settling['reviews'][0]['payload']['source_availability'], 'settling')
            incomplete = self.worker.scan_inbox(discover=True)
            self.assertFalse(incomplete['native_files_settling'])
            self.assertEqual(incomplete['reviews'][0]['payload']['source_availability'], 'unreadable')
            self.assertTrue(incomplete['inbox_issues'])
            path.write_bytes(original)
            restored = self.worker.scan_inbox(discover=True)['reviews']
        self.assertEqual([(row['id'], row['kind']) for row in restored], [(review_id, 'zotero')])
        self.assertFalse(restored[0]['payload'].get('local_recheck_error'))


class IncrementalDigestTests(unittest.TestCase):
    def test_repeated_bridge_reuses_observation_but_changed_file_is_hashed(self):
        from tests import test_zotero_bridge as bridge_tests
        from note_organizer.zotero_bridge import Bridge
        from note_organizer.zotero_bulk import digest_file
        fixture = bridge_tests.BridgeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.add_desktop()
        cache = {}
        def engine():
            return Bridge(fixture.zot, fixture.cloud, fixture.state, 'ROOT2345', fixture.cloud.root.id, file_digests=cache)
        with patch('note_organizer.zotero_bulk.digest_file', wraps=digest_file) as digest:
            engine().run()
            first = digest.call_count
            self.assertGreater(first, 0)
            engine().run()
            self.assertEqual(digest.call_count, first)
            path = fixture.zot.file_path('SOURCE23')
            path.write_bytes(path.read_bytes() + b'\n% external edit\n')
            engine().run()
            self.assertGreater(digest.call_count, first)
            previous = digest.call_count
            cache.clear()  # Explicit full verification establishes fresh byte evidence.
            engine().run()
            self.assertGreater(digest.call_count, previous)


if __name__ == '__main__': unittest.main()
