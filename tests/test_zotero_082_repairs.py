import copy
import uuid
from pathlib import Path
import unittest
from unittest.mock import patch

import pymupdf

from note_organizer.zotero_tagging import document_keywords, keyword_report, clean, backfill_keywords, stated_keywords
from tests import test_zotero_bulk as bulk
from tests import test_zotero_worker as worker


class KeywordRepairs(unittest.TestCase):
    def test_publisher_control_bullets_separate_keywords_and_never_become_tags(self):
        rows = stated_keywords('', 'In-stent restenosis \x02 Ultra-high-resolution')
        self.assertEqual([r['tag'] for r in rows], ['In-stent restenosis', 'Ultra-high-resolution'])
        self.assertEqual(clean('rest\x02enosis'), '')

    def test_two_columns_do_not_mix_keyword_list_with_abstract(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'paper.pdf'
            with pymupdf.open() as doc:
                page = doc.new_page()
                page.insert_textbox((20, 30, 270, 160), 'Abstract\nMethods: unrelated body text\nMore unrelated text', fontsize=10)
                page.insert_textbox((300, 30, 570, 160), 'Key Words:\nhigh-Z contrast media, coronary stenosis,\ncalcification, plaques, phantom', fontsize=10)
                doc.set_metadata({'keywords': 'reMarkable imported PDF'})
                doc.save(path)
            self.assertEqual([r['tag'] for r in document_keywords(path)],
                             ['high-Z contrast media', 'coronary stenosis', 'calcification', 'plaques', 'phantom'])

    def test_missing_scan_and_no_stated_keywords_are_distinct(self):
        self.assertEqual(keyword_report(Path('not-here.pdf'))['status'], 'extraction_failed')
        self.assertEqual(clean('"imaging"'), 'imaging')
        self.assertEqual(clean('reMarkable imported PDF'), '')

    def test_keyword_bullets_continue_across_pdf_block_boundaries(self):
        rows = stated_keywords('Key Words: iodine ◼ coronary CT\n\n◼ atherosclerosis ◼ plaque\n\nIntroduction\nBody')
        self.assertEqual([r['tag'] for r in rows], ['iodine', 'coronary CT', 'atherosclerosis', 'plaque'])


class FolderRepairs(unittest.TestCase):
    setUp = bulk.BulkTests.setUp

    def test_routine_filing_keeps_the_unique_imported_source_folder(self):
        bulk.pdf(self.source / 'Original folder' / 'Paper.pdf', 'A sufficiently specific research paper')
        self.worker.scan_library(str(self.source), 'ROOT2345', lookup=False)
        self.worker.import_library(self.worker.bulk.read()['id'])
        with patch.object(self.worker.review, 'observe_ready', return_value=True):
            self.worker.sync()
        row = next(r for r in self.worker.review.rows() if r['kind'] == 'zotero')
        original = next(r['key'] for r in self.zot.collection_records.values() if r['name'] == 'Original folder')
        legacy_payload = {**row['payload'], 'source_collections': []}
        self.worker.review.offer(row['kind'], row['identity'], row['digest'], legacy_payload)
        self.worker.refresh_review_policy(self.zot)
        row = self.worker.review.row(row['id'])
        self.assertEqual(row['payload']['source_collections'], [original])
        self.worker.approve_routine([{'id': row['id'], 'digest': row['digest'], 'review_token': row['payload']['review_token']}], 'ROOT2345')
        self.assertEqual(self.worker.review.row(row['id'])['approval']['collection'], original)

    def test_nested_duplicate_copies_reuse_case_variant_and_preserve_all_memberships(self):
        path = bulk.pdf(self.source / 'Topic' / 'Subtopic' / 'Paper.pdf', 'A sufficiently specific title')
        other = self.source / 'Other' / 'Copy.pdf'; other.parent.mkdir(); other.write_bytes(path.read_bytes())
        self.zot.collection_records['TOPIC234'] = {'key': 'TOPIC234', 'name': 'topic', 'parentCollection': 'ROOT2345'}
        self.worker.scan_library(str(self.source), 'ROOT2345', lookup=False)
        self.worker.import_library(self.worker.bulk.read()['id'])
        self.assertEqual(self.zot.imports, 1)
        self.assertEqual(sum(r['name'].casefold() == 'topic' for r in self.zot.collection_records.values()), 1)
        parent = next(r for r in self.zot.items.values() if r['itemType'] != 'attachment')
        names = {self.zot.collection_records[key]['name'] for key in parent['collections']}
        self.assertEqual(names, {'Subtopic', 'Other'})
        subtopic = next(r for r in self.zot.collection_records.values() if r['name'] == 'Subtopic')
        self.assertEqual(subtopic['parentCollection'], 'TOPIC234')


class RenameRepairs(unittest.TestCase):
    setUp = worker.WorkerTests.setUp
    add_pdf = worker.WorkerTests.add_pdf

    def connected_copy_and_second_reference(self):
        path = self.add_pdf(); raw = path.read_bytes()
        tablet = self.cloud.upload_pdf(raw, 'Existing', self.croot.id, str(uuid.uuid4()), self.croot.id)
        with patch.object(self.worker.review, 'observe_ready', return_value=True):
            self.worker.sync()
        first = next(r for r in self.worker.review.rows() if r['kind'] == 'zotero')
        self.worker.approve(first['id'], first['digest'], 'First.pdf', 'Dissertation', 'journal_article', 'ROOT2345', match=tablet.id)
        self.worker.sync()
        self.zot.items['OTHER234'] = {'key': 'OTHER234', 'itemType': 'journalArticle', 'title': 'Another citation', 'collections': ['ROOT2345']}
        self.zot.items['SECOND23'] = {'key': 'SECOND23', 'itemType': 'attachment', 'parentItem': 'OTHER234', 'contentType': 'application/pdf', 'linkMode': 'imported_file'}
        self.zot.file_path('SECOND23').write_bytes(raw)
        with patch.object(self.worker.review, 'observe_ready', return_value=True):
            self.worker._catalog(self.zot, self.cloud)
        return tablet, next(r for r in self.worker.review.rows() if r['identity'] == 'SECOND23')

    def test_approved_identical_copy_does_not_create_another_tablet_pdf(self):
        tablet, row = self.connected_copy_and_second_reference()
        self.worker.approve(row['id'], row['digest'], 'Second.pdf', 'Another citation', 'journal_article', 'ROOT2345')
        result = self.worker.sync()
        self.assertEqual(sum(r.is_pdf for r in self.cloud.library()), 1)
        self.assertIn('already has a connected tablet copy', str(result['status']['result']['review']))

    def test_relinking_registered_counterpart_keeps_managed_pdf_and_its_bytes(self):
        tablet, row = self.connected_copy_and_second_reference()
        previous = copy.deepcopy(self.worker.state.read('documents')[tablet.id])
        view = previous['view_key']; before = self.zot.file_path(view).read_bytes()
        payload = {**row['payload'], 'matches': [{'key': tablet.id, 'parent': self.croot.id}]}
        self.worker.review.offer('zotero', row['identity'], row['digest'], payload)
        self.worker.approve(row['id'], row['digest'], 'Second.pdf', 'Another citation', 'journal_article', 'ROOT2345', match=tablet.id)
        current = self.worker.state.read('documents')[tablet.id]
        self.assertEqual(current['view_key'], view)
        self.assertEqual(current['rendered'], previous['rendered'])
        self.assertEqual(current['parent'], 'OTHER234')
        self.assertEqual(self.zot.items[view]['parentItem'], 'OTHER234')
        self.assertEqual(self.zot.file_path(view).read_bytes(), before)
        self.assertNotIn('tag_state', current)

    def test_native_metadata_auto_rename_does_not_leave_filing_using_old_path(self):
        old = self.add_pdf()
        with patch.object(self.worker.review, 'observe_ready', return_value=True):
            self.worker._catalog(self.zot, self.cloud)
        row = self.worker.review.rows()[0]
        actual = {'path': old}
        get_path = self.zot.file_path
        update = self.zot.update
        def file_path(key):
            return actual['path'] if key == 'SOURCE23' else get_path(key)
        def auto_rename(record):
            update(record)
            if record['key'] == 'PARENT23' and actual['path'] == old:
                target = old.with_name('Native automatic rename.pdf')
                old.rename(target); actual['path'] = target
        with patch.object(self.zot, 'file_path', side_effect=file_path), patch.object(self.zot, 'update', side_effect=auto_rename):
            self.worker.approve(row['id'], row['digest'], 'Chosen journal.pdf', 'Clean title', 'journal_article', 'ROOT2345')
        self.assertEqual(self.worker.review.row(row['id'])['status'], 'approved')
        self.assertFalse(old.exists())
        self.assertTrue(actual['path'].exists())

    def test_backfill_does_not_readd_a_tag_deliberately_removed_after_first_check(self):
        path = self.add_pdf()
        state = {}
        with patch('note_organizer.zotero_tagging.keyword_report', return_value={'version': 2, 'status': 'found', 'keywords': [{'tag': 'iodine'}]}):
            backfill_keywords(self.zot, 'PARENT23', path, state, lambda: None)
            self.zot.items['PARENT23']['tags'] = []
            backfill_keywords(self.zot, 'PARENT23', path, state, lambda: None)
        self.assertEqual(self.zot.items['PARENT23']['tags'], [])
