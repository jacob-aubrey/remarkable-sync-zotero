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
            self.worker.scan_inbox(discover=True)
            with patch('note_organizer.zotero_review.time.time', return_value=9999999999):
                result = self.worker.scan_inbox(discover=True)
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
