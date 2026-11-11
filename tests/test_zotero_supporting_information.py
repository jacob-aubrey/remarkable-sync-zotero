"""Supplements reuse native Zotero child attachments without changing citations."""
import copy
import unittest
import uuid
from unittest.mock import patch
import pymupdf

from note_organizer.zotero_pilot import PilotError
from note_organizer.zotero_worker import file_sha
from tests.test_cloud_outbox import pdf_bytes
from tests import test_zotero_worker


def supplement_pdf(label):
    with pymupdf.open(stream=pdf_bytes(), filetype='pdf') as document:
        document.set_metadata({'title': label})
        return document.tobytes()


class SupportingInformationTests(unittest.TestCase):
    def setUp(self):
        fixture = test_zotero_worker.WorkerTests('test_new_tablet_document_is_not_imported_until_approved')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture, self.worker, self.zot = fixture, fixture.worker, fixture.zot
        self.original = fixture.add_pdf()
        self.main = copy.deepcopy(self.zot.items['PARENT23'])
        self.item = fixture.cloud.upload_pdf(supplement_pdf('Supplement'), 'Extra SI', fixture.croot.id, str(uuid.uuid4()), fixture.croot.id)
        with patch.object(self.worker.review, 'observe_ready', return_value=True):
            self.worker.sync()
        self.row = next(row for row in self.worker.review.rows() if row['kind'] == 'cloud')

    def approve(self, **changes):
        values = dict(identity=self.row['id'], digest=self.row['digest'], filename='Extra_SI.pdf', title='Supplement title',
                      document_type='supporting_information', collection='ROOT2345', main_reference='PARENT23', main_reference_metadata=self.main)
        values.update(changes)
        return self.worker.approve(**values)

    def test_tablet_SI_is_a_child_of_the_selected_main_paper_and_preserves_citation(self):
        digest = file_sha(self.original)
        self.approve()
        self.worker.sync()
        main = self.zot.items['PARENT23']
        for key in ['title', 'itemType', 'university']:
            self.assertEqual(main[key], self.main[key])
        self.assertEqual(file_sha(self.original), digest)
        self.assertEqual(len([r for r in self.zot.items.values() if r.get('parentItem') == 'PARENT23']), 2)
        self.assertEqual(self.worker.state.read('documents')[self.item.id]['parent'], 'PARENT23')

    def test_missing_or_stale_main_paper_is_rejected_before_any_writes(self):
        writes = self.zot.writes
        with self.assertRaisesRegex(PilotError, 'Choose the main paper'):
            self.approve(main_reference='')
        self.assertEqual(self.zot.writes, writes)
        self.zot.items['PARENT23']['title'] = 'Changed while waiting'
        with self.assertRaisesRegex(PilotError, 'main paper changed'):
            self.approve()
        self.assertEqual(self.zot.writes, writes)
        self.assertEqual(self.worker.review.row(self.row['id'])['status'], 'pending')

    def test_tag_or_collection_changes_do_not_invalidate_an_unchanged_citation(self):
        self.zot.items['PARENT23']['tags'] = [{'tag': 'New tag'}]
        self.zot.items['PARENT23']['version'] = 44
        self.approve()
        self.assertEqual(self.zot.items['PARENT23']['title'], self.main['title'])

    def test_detected_SI_can_be_corrected_by_selecting_journal_article(self):
        payload = self.row['payload']
        payload['details']['is_supplementary_material'] = True
        self.worker.review.offer('cloud', self.item.id, self.row['digest'], payload)
        self.approve(document_type='journal_article', main_reference='')
        self.assertEqual(self.worker.review.row(self.row['id'])['status'], 'approved')

    def test_existing_Zotero_SI_moves_as_an_attachment_and_keeps_its_previous_reference(self):
        self.zot.items['SUPREF23'] = {'key': 'SUPREF23', 'itemType': 'document', 'title': 'Old SI placeholder', 'collections': ['ROOT2345']}
        self.zot.items['SUPPL234'] = {'key': 'SUPPL234', 'itemType': 'attachment', 'parentItem': 'SUPREF23',
            'title': 'Extra SI', 'contentType': 'application/pdf', 'linkMode': 'imported_file'}
        path = self.zot.file_path('SUPPL234'); path.write_bytes(supplement_pdf('Other supplement'))
        with patch.object(self.worker.review, 'observe_ready', return_value=True):
            self.worker.sync()
        self.row = next(row for row in self.worker.review.rows() if row['identity'] == 'SUPPL234')
        digest = file_sha(path)
        self.approve()
        self.assertEqual(self.zot.items['SUPPL234']['parentItem'], 'PARENT23')
        self.assertEqual(file_sha(path), digest)
        self.assertEqual(self.zot.items['PARENT23']['title'], self.main['title'])
        self.assertEqual(self.zot.items['SUPREF23']['title'], 'Old SI placeholder')

    def test_manual_SI_filename_preview_marks_the_supplement(self):
        result = self.worker.preview(self.row['id'], 'supporting_information', 'Supplement')
        self.assertTrue(result['filename'].endswith('_SI.pdf'), result)
