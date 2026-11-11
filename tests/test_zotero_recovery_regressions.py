"""Recovery reproductions from the October 2026 independent plugin audits."""
import copy
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid

from note_organizer.zotero_pilot import Ledger, PilotError
from note_organizer.zotero_worker import creator_record
from tests import test_zotero_worker as fixtures
from tests import test_zotero_bulk as bulk_fixtures


class WorkerRecoveryTests(unittest.TestCase):
    setUp = fixtures.WorkerTests.setUp
    add_pdf = fixtures.WorkerTests.add_pdf

    def interrupted_intake_upload(self, *, lose_upload=False, lose_finish=False):
        inbox = self.root / 'Intake'; inbox.mkdir()
        path = inbox / 'Paper.pdf'; path.write_bytes(fixtures.pdf_bytes())
        self.worker.config['inbox'] = str(inbox)
        with patch.object(self.worker.review, 'observe_ready', return_value=True):
            self.worker.sync()
        row = next(row for row in self.worker.review.rows() if row['kind'] == 'inbox')
        original_upload = self.zot.upload
        remote = {'md5': 'a' * 32, 'mtime': 1234567890000, 'finish_failures': int(lose_finish)}
        prepared, finished = [], []
        def prepare(key, previous):
            receipt = {'key': key, 'md5': previous, 'authorization_md5': remote['md5'],
                       'remote_md5': remote['md5'], 'remote_mtime': remote['mtime']}
            prepared.append(receipt)
            return receipt
        def upload(key, source, previous, *, upload_intent=None):
            self.assertIsNotNone(upload_intent)
            original_upload(key, source, previous)
            remote.update(md5=self.zot.file_md5(key), mtime=9999999999999)
        def finish(key, wanted, receipt):
            finished.append(receipt)
            if remote['finish_failures']:
                remote['finish_failures'] -= 1
                raise OSError('Lost native completion response')
            self.assertEqual(self.zot.file_md5(key), wanted)
            remote.update(md5=receipt['remote_md5'], mtime=receipt['remote_mtime'], sync_state='to_upload')
        self.zot.prepare_file_upload = prepare
        self.zot.upload = upload
        self.zot.finish_file_upload = finish
        self.zot.fail_upload = lose_upload
        args = (row['id'], row['digest'], 'Paper.pdf', 'Paper', 'journal_article', 'ROOT2345')
        with self.assertRaises(OSError):
            self.worker.approve(*args)
        current = self.worker.review.row(row['id'])
        source = current['approval']['source']
        ledger = Ledger(self.worker.connection_dir / 'source-imports')
        self.assertEqual(ledger.upload_intent(source)['remote_md5'], 'a' * 32)
        self.worker.approve(*args)
        self.assertEqual(self.worker.review.row(row['id'])['status'], 'approved')
        self.assertEqual(self.zot.uploads, 1)
        self.assertEqual(len(prepared), 1)
        self.assertEqual(len(finished), 2 if lose_finish else 1)
        self.assertIsNone(ledger.upload_intent(source))
        self.assertEqual(remote['sync_state'], 'to_upload')
        self.assertEqual(remote['md5'], 'a' * 32)
        self.assertEqual(len([item for item in self.zot.items.values() if item['itemType'] == 'attachment']), 1)
        self.assertEqual(len([item for item in self.zot.items.values() if item['itemType'] != 'attachment']), 1)

    def test_intake_lost_upload_response_finishes_receipt_without_another_import(self):
        self.interrupted_intake_upload(lose_upload=True)

    def test_intake_lost_finish_response_finishes_receipt_without_reupload(self):
        self.interrupted_intake_upload(lose_finish=True)

    def test_missing_local_pdf_does_not_abort_other_catalog_reviews(self):
        self.add_pdf().unlink()
        self.zot.items['SECOND23'] = {**self.zot.items['SOURCE23'], 'key': 'SECOND23'}
        self.zot.file_path('SECOND23').write_bytes(fixtures.pdf_bytes())
        with patch.object(self.worker.review, 'observe_ready', return_value=True):
            result = self.worker.sync()
        self.assertEqual([row['identity'] for row in self.worker.review.rows()], ['SECOND23'])
        self.assertIn('SOURCE23', str(result['status']['result']['review']))

    def test_changed_then_removed_intake_retires_old_reviews(self):
        intake = self.root / 'Inbox'; intake.mkdir()
        path = intake / 'Paper.pdf'; path.write_bytes(fixtures.pdf_bytes())
        self.worker.config['inbox'] = str(intake)
        with patch.object(self.worker.review, 'observe_ready', return_value=True):
            self.worker._catalog(self.zot, self.cloud)
            first = self.worker.review.rows()[0]
            path.write_bytes(fixtures.pdf_bytes())
            self.worker._catalog(self.zot, self.cloud)
            self.assertEqual(len(self.worker.review.rows()), 1)
            self.assertEqual(self.worker.review.row(first['id'])['status'], 'superseded')
            path.unlink()
            self.worker._catalog(self.zot, self.cloud)
        self.assertEqual(self.worker.review.rows(), [])
        self.assertEqual(len(self.worker.review.rows(pending_only=False)), 2)

    def test_completed_counterpart_does_not_block_interrupted_approval_retry(self):
        path = self.add_pdf()
        item = self.cloud.upload_pdf(path.read_bytes(), 'Tablet paper', self.croot.id, str(uuid.uuid4()), self.croot.id)
        with patch.object(self.worker.review, 'observe_ready', return_value=True):
            self.worker.sync()
        row = next(row for row in self.worker.review.rows() if row['kind'] == 'cloud')
        original_finish = self.worker.review.finish
        def interrupted(identity, **kwargs):
            if identity == row['id']:
                raise OSError('Interrupted after the counterpart committed')
            return original_finish(identity, **kwargs)
        args = (row['id'], row['digest'], 'Reviewed.pdf', 'Dissertation', 'thesis_dissertation', 'ROOT2345')
        with patch.object(self.worker.review, 'finish', side_effect=interrupted):
            with self.assertRaises(OSError):
                self.worker.approve(*args, match='SOURCE23')
        self.assertEqual(self.worker.review.row(row['id'])['status'], 'applying')
        self.worker.approve(*args, match='SOURCE23')
        self.assertEqual(self.worker.review.row(row['id'])['status'], 'approved')
        self.assertIn(item.id, self.worker.state.read('documents'))

    def interrupted_matched_registration(self):
        path = self.add_pdf()
        item = self.cloud.upload_pdf(path.read_bytes(), 'Tablet paper', self.croot.id, str(uuid.uuid4()), self.croot.id)
        with patch.object(self.worker.review, 'observe_ready', return_value=True):
            self.worker.sync()
        row = next(row for row in self.worker.review.rows() if row['kind'] == 'zotero')
        args = (row['id'], row['digest'], 'Reviewed.pdf', 'Dissertation', 'thesis_dissertation', 'ROOT2345')
        with patch.object(self.worker.review, 'complete_counterpart', side_effect=OSError('Interrupted after link registration')):
            with self.assertRaises(OSError):
                self.worker.approve(*args, match=item.id)
        original = self.worker.state.directory / 'originals' / (row['digest'] + '.pdf')
        original.unlink()  # Legacy builds registered links before retaining bytes.
        self.worker._catalog(self.zot, self.cloud)
        current = self.worker.review.row(row['id'])
        self.assertEqual(current['status'], 'applying')
        self.assertEqual(current['payload']['matches'], [])
        return args, item, path, original

    def test_interrupted_match_recovers_after_discovery_hides_registered_counterpart(self):
        args, item, path, original = self.interrupted_matched_registration()
        before_items = set(self.zot.items)
        before_cloud = {row.id for row in self.cloud.library()}
        self.worker.approve(*args, match=item.id)
        self.assertEqual(self.worker.review.row(args[0])['status'], 'approved')
        self.assertEqual(original.read_bytes(), path.read_bytes())
        self.assertEqual(set(self.zot.items), before_items)
        self.assertEqual({row.id for row in self.cloud.library()}, before_cloud)
        self.assertEqual(next(row for row in self.worker.review.rows(pending_only=False) if row['kind'] == 'cloud')['status'], 'approved')

    def test_interrupted_match_cannot_change_saved_filing_choices(self):
        args, item, path, original = self.interrupted_matched_registration()
        changed = list(args); changed[2] = 'A different name.pdf'
        before = self.zot.writes
        with self.assertRaisesRegex(PilotError, 'byte-verified match'):
            self.worker.approve(*changed, match=item.id)
        self.assertEqual(self.zot.writes, before)
        self.assertFalse(original.exists())

    def test_interrupted_match_cannot_claim_a_link_owned_by_another_source(self):
        args, item, path, original = self.interrupted_matched_registration()
        links = self.worker.state.read('documents'); links[item.id]['source_key'] = 'OTHER234'
        self.worker.state.write('documents', links)
        before = self.zot.writes
        with self.assertRaisesRegex(PilotError, 'byte-verified match'):
            self.worker.approve(*args, match=item.id)
        self.assertEqual(self.zot.writes, before)

    def test_explicit_reconnect_keeps_old_state_separate(self):
        self.worker.config['server_id'] = 'old-server'
        self.worker.state.write('documents', {'old-paper': {'name': 'Old paper'}})
        old_state = self.worker.state
        result = self.worker.connect()
        self.assertTrue(result['server_changed'])
        self.assertEqual(self.worker.config['tablet_root'], '')
        self.assertFalse(self.worker.config['automatic'])
        self.assertNotEqual(self.worker.state.directory, old_state.directory)
        self.assertIn('old-paper', old_state.read('documents'))
        self.assertFalse(self.worker.state.read('documents', {}))

    def test_zotero_freeform_date_and_author_clear_obsolete_extraction_warnings(self):
        details = {'title': 'Extracted', 'document_type': 'journal_article', 'year': 'Unknown',
                   'primary_creator': 'Unknown', 'creators': [], 'journal': '',
                   'review_reasons': ['No reliable author or responsible organization was found.',
                                      'No reliable publication or release year was found.',
                                      'Missing citation fields: author, year, journal. Review and complete the proposed name.']}
        with patch('note_organizer.sorting.core_logic.get_basic_document_details', return_value=copy.deepcopy(details)):
            proposal = self.worker._proposal(self.root / 'Paper.pdf', {'title': 'Paper', 'date': 'January 2024',
                'publicationTitle': 'Journal', 'creators': [{'firstName': 'Ada', 'lastName': 'Lovelace'}]})
        self.assertEqual(proposal['details']['year'], '2024')
        self.assertEqual(proposal['details']['review_reasons'], [])
        self.assertIn('2024', proposal['filename'])

    def test_person_names_use_structured_fields_but_organizations_do_not(self):
        self.assertEqual(creator_record('Ada Lovelace', 'author'),
                         {'creatorType': 'author', 'firstName': 'Ada', 'lastName': 'Lovelace'})
        self.assertEqual(creator_record('Lovelace, Ada', 'author')['lastName'], 'Lovelace')
        self.assertEqual(creator_record('Example Research Group', 'author')['name'], 'Example Research Group')

    def test_restoring_reference_in_zotero_clears_deletion_notice_and_suspension(self):
        self.add_pdf()
        with patch.object(self.worker.review, 'observe_ready', return_value=True):
            self.worker.sync()
        row = self.worker.review.rows()[0]
        self.worker.approve(row['id'], row['digest'], 'Paper.pdf', 'Paper', 'journal_article', 'ROOT2345')
        self.worker.sync()
        identity = next(iter(self.worker.state.read('documents')))
        self.zot.items['PARENT23']['deleted'] = True
        self.worker.sync()
        self.assertEqual(len(self.worker.review.local_deletions()), 1)
        self.zot.items['PARENT23']['deleted'] = False
        self.worker.sync()
        self.assertEqual(self.worker.review.local_deletions(), [])
        self.assertNotIn(identity, self.worker.review.suspended_documents())

    def test_local_preferences_save_without_cloud_catalog(self):
        with patch.object(self.worker, 'cloud', side_effect=AssertionError('Offline cloud queried')):
            self.worker.configure(ocr=True, automatic=False, templates={'journal_article': '{title}'},
                                  zotero_root='ROOT2345', tablet_root=self.croot.id, default_folder=self.croot.id)
        self.assertTrue(self.worker.config['ocr'])

    def test_intake_rejects_empty_zotero_storage_directory(self):
        data = self.root / 'Zotero'; data.mkdir()
        storage = data / 'storage'; storage.mkdir()
        original = self.zot.review_action
        def catalog(action, params=None):
            result = original(action, params)
            if action == 'catalog':
                result['data_directory'] = str(data)
            return result
        with patch.object(self.zot, 'review_action', side_effect=catalog):
            with self.assertRaisesRegex(PilotError, 'outside Zotero'):
                self.worker.configure(inbox=str(storage))

    def test_missing_link_is_not_counted_as_connected(self):
        self.worker.state.write('documents', {'missing': {'name': 'Missing', 'unavailable': True, 'revision': 'old'}})
        status = self.worker.status()
        self.assertEqual(status['filing_summary']['linked'], 0)
        self.assertEqual(status['filing_summary']['pdfs_received'], 0)
        self.assertEqual(status['connection']['linked_papers'], 0)

    def test_supplement_heuristic_can_be_overridden_with_article_type(self):
        self.cloud.upload_pdf(fixtures.pdf_bytes(), 'Misleading_SI', self.croot.id, str(uuid.uuid4()), self.croot.id)
        self.worker.sync()
        row = self.worker.review.rows()[0]
        self.worker.approve(row['id'], row['digest'], 'Article.pdf', 'Article', 'journal_article', 'ROOT2345')
        self.assertEqual(self.worker.review.row(row['id'])['status'], 'approved')

    def test_routine_batch_recovers_an_applying_row_with_the_same_approval(self):
        self.add_pdf()
        with patch.object(self.worker.review, 'observe_ready', return_value=True):
            self.worker.sync()
        row = self.worker.review.rows()[0]
        payload = row['payload']
        entry = {'id': row['id'], 'digest': row['digest'], 'review_token': payload['review_token']}
        with patch.object(self.worker, 'apply_document_keywords', side_effect=OSError('Interrupted tag write')):
            self.worker.approve_routine([entry], 'ROOT2345')
        self.assertEqual(self.worker.review.row(row['id'])['status'], 'applying')
        self.worker.approve_routine([entry], 'ROOT2345')
        self.assertEqual(self.worker.review.row(row['id'])['status'], 'approved')

    def test_routine_batch_does_not_change_recorded_applying_choices(self):
        self.add_pdf()
        with patch.object(self.worker.review, 'observe_ready', return_value=True):
            self.worker.sync()
        row = self.worker.review.rows()[0]
        self.worker.review.begin(row['id'], row['digest'], {'filename': 'Different approved name.pdf'})
        entry = {'id': row['id'], 'digest': row['digest'], 'review_token': row['payload']['review_token']}
        self.worker.approve_routine([entry], 'ROOT2345')
        self.assertEqual(self.worker.review.row(row['id'])['status'], 'applying')
        self.assertEqual(self.worker.review.row(row['id'])['approval']['filename'], 'Different approved name.pdf')

    def test_sync_surfaces_partial_ocr_as_page_numbers_without_private_details(self):
        warnings = {'attachment': 'VIEW2345', 'incomplete': True,
                    'printed_ocr': [{'page': 2, 'reason': 'private C:/secret/path token=secret'}], 'handwriting': []}
        result = {'review': [], 'actions': [{'cloud': 'paper', 'index_warnings': warnings}]}
        with patch('note_organizer.zotero_worker.Bridge') as bridge:
            bridge.return_value.run.return_value = result
            status = self.worker.sync()
        review = status['status']['result']['review'][0]
        self.assertIn('page(s) 2', review['reason'])
        self.assertNotIn('secret', review['reason'])
        self.assertEqual(status['status']['result']['partial_indexes'], 1)


class BulkRecoveryTests(unittest.TestCase):
    setUp = bulk_fixtures.BulkTests.setUp
    scan = bulk_fixtures.BulkTests.scan
    apply = bulk_fixtures.BulkTests.apply

    def test_keyword_failure_retries_existing_import_without_duplicate(self):
        bulk_fixtures.pdf(self.source / 'Paper.pdf', 'A scientific paper')
        with patch.object(self.worker, 'apply_document_keywords', side_effect=OSError('Temporary tag write failure')):
            self.apply(self.scan())
        self.assertEqual(self.zot.imports, 1)
        with patch.object(self.worker, 'apply_document_keywords', wraps=self.worker.apply_document_keywords) as keywords:
            self.apply(self.scan())
        self.assertEqual(keywords.call_count, 1)
        self.assertEqual(self.zot.imports, 1)
        self.assertEqual(self.worker.bulk.read()['rows'][0]['status'], 'complete')

    def test_changed_visible_lookup_settings_reject_old_scan_before_writes(self):
        bulk_fixtures.pdf(self.source / 'Paper.pdf', 'A scientific paper')
        plan = self.scan()
        with self.assertRaises(PilotError):
            self.worker.import_library(plan['id'], root=str(self.source), collection='ROOT2345', lookup=True)
        self.assertEqual(self.zot.imports, 0)
        with self.assertRaises(PilotError):
            self.worker.retry_bulk(plan['id'], plan['rows'][0]['id'], lookup=True)
        self.assertEqual(self.zot.imports, 0)

    def test_progress_counts_only_unfinished_eligible_rows(self):
        for number in range(3):
            bulk_fixtures.pdf(self.source / f'Paper {number}.pdf', f'A distinct scientific study {number}')
        plan = self.scan()
        plan['rows'][0]['issues'] = ['Held for explicit review']
        plan['rows'][1]['status'] = 'complete'
        self.worker.bulk.save(plan)
        messages = []; self.worker.progress = messages.append
        self.apply(plan)
        self.assertEqual([m for m in messages if m.startswith('Importing')], ['Importing / reusing document 1 of 1…'])

    def test_stop_during_native_import_is_paused_without_file_issue(self):
        bulk_fixtures.pdf(self.source / 'Paper.pdf', 'A scientific paper')
        plan = self.scan()
        original = self.zot.review_action
        def pending(action, data=None):
            if action == 'bulk_import':
                result = original(action, data)
                self.worker.stop.set()
                return {'pending': True}
            return original(action, data)
        with patch.object(self.zot, 'review_action', side_effect=pending):
            self.apply(plan)
        self.assertEqual(self.worker.last['state'], 'paused')
        self.assertEqual(self.worker.bulk.read()['rows'][0]['issues'], [])
        self.apply(self.worker.bulk.read())
        self.assertEqual(self.zot.imports, 1)
        self.assertEqual(self.worker.bulk.read()['rows'][0]['status'], 'complete')


if __name__ == '__main__':
    unittest.main()
