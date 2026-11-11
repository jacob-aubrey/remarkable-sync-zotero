"""Bulk intake preserves originals and asks only about material exceptions."""
import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import pymupdf

from note_organizer.zotero_bulk import digest_file, review_policy
from note_organizer.zotero_pilot import PilotError, TransportInterrupted, stable_key
from note_organizer.zotero_worker import Worker
from tests.test_zotero_worker import ReviewZotero
from tests.test_cloud_outbox import MemoryCloud


def pdf(path, title, *, scan=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as doc:
        page = doc.new_page()
        if not scan:
            page.insert_text((40, 80), title)
        doc.set_metadata({'title': title})
        doc.save(path)
    return path


class BulkZotero(ReviewZotero):
    def __init__(self, root):
        super().__init__(root)
        self.imports = 0
        self.lose_response = False

    def review_action(self, action, data=None):
        if action != 'bulk_import':
            return super().review_action(action, data)
        source = Path(data['source'])
        self.assert_source = source.read_bytes()
        key = data.get('existing') or stable_key('bulk-test', 'attachment', data['digest'])
        parent = self.items.get(key, {}).get('parentItem') or stable_key('bulk-test', 'parent', data['digest'])
        if key not in self.items:
            self.imports += 1
            self.items[parent] = {'key': parent, 'itemType': 'document', 'title': data['title'], 'collections': []}
            self.items[key] = {'key': key, 'itemType': 'attachment', 'parentItem': parent,
                               'title': source.name, 'filename': source.name, 'contentType': 'application/pdf', 'linkMode': 'imported_file'}
            self.file_path(key).write_bytes(source.read_bytes())
        self.items[parent]['collections'] = sorted(set(self.items[parent]['collections'] + [data['collection']]))
        if self.lose_response:
            self.lose_response = False
            raise OSError('Synthetic response lost after import committed')
        return {'attachment': key, 'parent': parent, 'path': str(self.file_path(key)),
                'data': copy.deepcopy(self.items[key]), 'reference': copy.deepcopy(self.items[parent]), 'issues': []}


class BulkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'Windows literature'; self.source.mkdir()
        self.zot = BulkZotero(self.root)
        self.cloud = MemoryCloud()
        self.worker = Worker(self.root / 'helper', zotero_factory=lambda: self.zot, cloud_factory=lambda: self.cloud)
        croot = self.cloud.create_folder('Tablet', '', '11111111-1111-4111-8111-111111111111')
        self.worker.config = {'zotero_root': 'ROOT2345', 'tablet_root': croot.id, 'default_folder': croot.id, 'ocr': False}

    def scan(self):
        self.worker.scan_library(str(self.source), 'ROOT2345', lookup=False)
        return self.worker.bulk.read()

    def apply(self, plan):
        self.worker.import_library(plan['id'])
        return self.worker.bulk.read()

    def test_scan_reports_each_file_including_duplicates_without_importing(self):
        first = pdf(self.source / 'A.pdf', 'First paper')
        (self.source / 'B.pdf').write_bytes(first.read_bytes())
        pdf(self.source / 'C.pdf', 'Other paper')
        messages = []
        self.worker.progress = messages.append
        plan = self.scan()
        progress = [message for message in messages if message.startswith('Scanning file')]
        self.assertEqual(progress, ['Scanning file 1 of 3: A.pdf', 'Scanning file 2 of 3: B.pdf', 'Scanning file 3 of 3: C.pdf'])
        self.assertEqual(len(plan['rows']), 2)
        self.assertEqual(self.zot.imports, 0)

    def test_five_hundred_papers_import_together_without_citation_perfection_or_source_changes(self):
        originals = {}
        for number in range(500):
            path = pdf(self.source / ('Category ' + str(number % 7)) / f'Paper {number:04}.pdf', f'Scientific paper number {number:04}')
            originals[path] = digest_file(path)
        # A duplicate under another category is one import, not another paper.
        duplicate = self.source / 'Other category' / 'Repeated.pdf'; duplicate.parent.mkdir()
        duplicate.write_bytes(next(iter(originals)).read_bytes())
        originals[duplicate] = digest_file(duplicate)
        plan = self.scan()
        self.assertEqual(len(plan['rows']), 500)
        self.assertFalse(any(r['issues'] for r in plan['rows']))
        self.assertEqual(sum(len(r['copies']) for r in plan['rows']), 501)
        self.apply(plan)
        self.assertEqual(self.zot.imports, 500)
        self.assertTrue(all(r['status'] == 'complete' for r in self.worker.bulk.read()['rows']))
        self.assertEqual(originals, {path: digest_file(path) for path in originals})
        self.apply(self.scan())
        self.assertEqual(self.zot.imports, 500)

    def test_existing_exact_pdf_is_reused_preserving_curated_metadata_and_collection(self):
        source = pdf(self.source / 'Existing.pdf', 'An existing scientific paper')
        self.zot.items['PARENT23'] = {'key': 'PARENT23', 'itemType': 'thesis', 'title': 'Curated dissertation title', 'university': 'University', 'collections': ['TOPIC234']}
        self.zot.items['SOURCE23'] = {'key': 'SOURCE23', 'itemType': 'attachment', 'parentItem': 'PARENT23', 'contentType': 'application/pdf'}
        self.zot.file_path('SOURCE23').write_bytes(source.read_bytes())
        plan = self.scan(); self.assertEqual(plan['rows'][0]['existing'], 'SOURCE23')
        self.apply(plan)
        self.assertEqual(self.zot.imports, 0)
        self.assertEqual(self.zot.items['PARENT23']['title'], 'Curated dissertation title')
        self.assertEqual(self.zot.items['PARENT23']['university'], 'University')
        self.assertEqual(set(self.zot.items['PARENT23']['collections']), {'ROOT2345', 'TOPIC234'})

    def test_changed_source_is_held_before_import(self):
        source = pdf(self.source / 'Paper.pdf', 'An unchanged scientific paper')
        plan = self.scan(); source.write_bytes(source.read_bytes() + b'changed')
        self.apply(plan)
        self.assertEqual(self.zot.imports, 0)
        self.assertIn('source changed', self.worker.bulk.read()['rows'][0]['issues'][0])

    def test_same_title_different_content_is_an_exception_not_an_automatic_merge(self):
        pdf(self.source / 'A.pdf', 'Scientific study of nanoparticles and gold')
        pdf(self.source / 'B.pdf', 'Scientific study of nanoparticles and gold')
        plan = self.scan()
        self.assertTrue(all(row['issues'] for row in plan['rows']))
        self.apply(plan); self.assertEqual(self.zot.imports, 0)

    def test_unreadable_and_unassigned_SI_are_held_but_image_only_pdf_is_routine(self):
        (self.source / 'Broken.pdf').write_bytes(b'not a PDF')
        pdf(self.source / 'Paper_SI.pdf', 'Supporting information for a study')
        pdf(self.source / 'Scan.pdf', 'Scanned old paper', scan=True)
        rows = {Path(row['path']).name: row for row in self.scan()['rows']}
        self.assertTrue(rows['Broken.pdf']['issues'])
        self.assertTrue(rows['Paper_SI.pdf']['issues'])
        self.assertFalse(rows['Scan.pdf']['issues'])

    def test_response_lost_after_commit_recovers_by_content_without_another_copy(self):
        source = pdf(self.source / 'Paper.pdf', 'A paper with interrupted import')
        self.zot.lose_response = True
        self.apply(self.scan())
        self.assertEqual(self.zot.imports, 1)
        self.assertTrue(self.worker.bulk.read()['rows'][0]['issues'])
        self.apply(self.scan())
        self.assertEqual(self.zot.imports, 1)
        self.assertTrue(source.exists())

    def test_stale_plan_id_is_rejected(self):
        pdf(self.source / 'Paper.pdf', 'A scientific paper')
        first = self.scan(); self.scan()
        with self.assertRaises(PilotError):
            self.worker.import_library(first['id'])
        self.assertEqual(self.zot.imports, 0)

    def test_routine_batch_accepts_missing_authors_year_and_DOI_and_rejects_stale_snapshot(self):
        source = pdf(self.source / 'Paper.pdf', 'A scientific paper without citation details')
        self.apply(self.scan())
        with patch.object(self.worker.review, 'observe_ready', return_value=True):
            self.worker.sync()
        row = self.worker.review.rows()[0]
        self.assertTrue(row['payload']['routine'], row['payload']['issues'])
        self.assertEqual(row['payload']['filename'], self.zot.file_path(row['identity']).name)
        entries = [{'id': row['id'], 'digest': row['digest'], 'review_token': row['payload']['review_token']}]
        self.worker.approve_routine([{**entries[0], 'review_token': 'old snapshot'}], 'ROOT2345')
        self.assertEqual(self.worker.review.row(row['id'])['status'], 'pending')
        self.worker.approve_routine(entries, 'ROOT2345')
        self.assertEqual(self.worker.review.row(row['id'])['status'], 'approved')
        self.worker.sync()
        self.assertEqual(len(self.worker.state.read('documents')), 1)

    def test_PC_import_refreshes_existing_tablet_review_match_without_cloud_rescan(self):
        source = pdf(self.source / 'Paper.pdf', 'A scientific paper')
        self.cloud.upload_pdf(source.read_bytes(), 'Tablet label', self.worker.config['tablet_root'],
                              '22222222-2222-4222-8222-222222222222', self.worker.config['tablet_root'])
        self.worker.sync()
        row = next(r for r in self.worker.review.rows() if r['kind'] == 'cloud')
        self.assertEqual(row['payload']['matches'], [])
        plan = self.scan()
        with patch.object(self.cloud, 'library', side_effect=AssertionError('Import refreshed via cloud')):
            self.apply(plan)
        refreshed = self.worker.review.row(row['id'])
        self.assertEqual(len(refreshed['payload']['matches']), 1)
        self.assertEqual(refreshed['payload']['matches'][0]['key'], self.worker.bulk.read()['rows'][0]['attachment'])
        self.assertNotEqual(refreshed['payload']['review_token'], row['payload']['review_token'])

    def test_routine_policy_holds_native_multiple_pdf_versions(self):
        source = pdf(self.source / 'Paper.pdf', 'Scientific paper about nanomaterials')
        self.apply(self.scan())
        parent = next(r['key'] for r in self.zot.items.values() if r['itemType'] != 'attachment')
        self.zot.items['OTHER234'] = {'key': 'OTHER234', 'itemType': 'attachment', 'parentItem': parent, 'contentType': 'application/pdf'}
        pdf(self.zot.file_path('OTHER234'), 'Another version of this same paper')
        with patch.object(self.worker.review, 'observe_ready', return_value=True):
            self.worker.sync()
        self.assertTrue(all(not row['payload']['routine'] for row in self.worker.review.rows()))

    def test_connection_interruption_pauses_batch_without_false_file_issues_and_resumes_same_plan(self):
        for n in range(3):
            pdf(self.source / f'Paper {n}.pdf', f'A different scientific study number {n}')
        plan = self.scan()
        original = self.zot.review_action
        interrupted = False
        def lost(action, data=None):
            nonlocal interrupted
            result = original(action, data)
            if action == 'bulk_import' and not interrupted:
                interrupted = True
                raise TransportInterrupted('Connection interrupted; retry this batch.')
            return result
        with patch.object(self.zot, 'review_action', side_effect=lost):
            self.apply(plan)
        saved = self.worker.bulk.read()
        self.assertEqual(self.worker.last['state'], 'waiting')
        self.assertTrue(all(not row['issues'] for row in saved['rows']))
        self.assertEqual(self.zot.imports, 1)
        self.apply(saved)
        self.assertTrue(all(row['status'] == 'complete' for row in self.worker.bulk.read()['rows']))
        self.assertEqual(self.zot.imports, 3)

    def test_reusing_already_filed_content_needs_no_native_write_or_metadata_lookup(self):
        pdf(self.source / 'Paper.pdf', 'A scientific paper already filed in the collection')
        self.apply(self.scan())
        plan = self.scan()
        original = self.zot.review_action
        def no_write(action, data=None):
            self.assertNotEqual(action, 'bulk_import')
            return original(action, data)
        with patch.object(self.zot, 'review_action', side_effect=no_write):
            self.apply(plan)
        self.assertEqual(self.worker.bulk.read()['rows'][0]['status'], 'complete')
        self.assertEqual(self.zot.imports, 1)

    def test_slow_native_job_pauses_for_resume_instead_of_flagging_the_PDF(self):
        pdf(self.source / 'Slow.pdf', 'A readable synthetic study with a delayed native import')
        plan = self.scan()
        original = self.zot.review_action
        def delayed(action, data=None):
            if action == 'bulk_import':
                original(action, data)
                return {'pending': True}
            return original(action, data)
        with patch.object(self.zot, 'review_action', side_effect=delayed), patch('note_organizer.zotero_bulk.time', SimpleNamespace(monotonic=Mock(side_effect=[0, 121]))):
            self.apply(plan)
        saved = self.worker.bulk.read()
        self.assertEqual(self.worker.last['state'], 'waiting')
        self.assertEqual(saved['rows'][0]['issues'], [])
        self.apply(saved)
        self.assertEqual(self.worker.bulk.read()['rows'][0]['status'], 'complete')
        self.assertEqual(self.zot.imports, 1)


if __name__ == '__main__':
    unittest.main()
