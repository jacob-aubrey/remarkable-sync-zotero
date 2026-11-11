"""Naming proposals use printed identity and refresh only pending suggestions."""
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import pymupdf

from note_organizer.zotero_worker import Worker


class NamingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.worker = Worker(self.root / 'state')
        self.path = self.root / 'nm5c00178.pdf'
        with pymupdf.open() as document:
            page = document.new_page()
            page.insert_text((50, 50), 'Atomic Precision in Personalized Oncology: AI-Designed\nNanomedicines Enabling N-of-1 Cancer Therapy\nMiles Pourbaghi and Michelle S. Bradbury*\nCite This: ACS Nano Med.2026,1,784-790\nABSTRACT: A synthetic research-paper fixture.\nhttps://doi.org/10.1021/acsnanomed.5c00178')
            document.set_metadata({'title': 'nm5c00178 1..7', 'author': ''})
            document.save(self.path)

    def test_production_metadata_title_and_tight_printed_citation(self):
        before = self.path.read_bytes()
        proposal = self.worker._proposal(self.path)
        self.assertEqual(proposal['title'], 'Atomic Precision in Personalized Oncology: AI-Designed Nanomedicines Enabling N-of-1 Cancer Therapy')
        self.assertEqual(proposal['document_type'], 'journal_article')
        self.assertEqual(proposal['details']['journal'], 'ACS Nano Med.')
        self.assertEqual(proposal['details']['creators'], ['Miles Pourbaghi', 'Michelle S. Bradbury'])
        self.assertEqual(proposal['filename'], 'Pourbaghi_and_Bradbury_et_al_ACS_Nano_Med_2026.pdf')
        self.assertEqual(self.path.read_bytes(), before)

    def test_authoritative_zotero_fields_and_template_win(self):
        self.worker.config['templates'] = {'journal_article': '{author_last}_{year}_{journal}'}
        proposal = self.worker._proposal(self.path, {'title': 'Reviewed title', 'itemType': 'journalArticle', 'publicationTitle': 'Reviewed Journal',
            'date': '2027', 'creators': [{'firstName': 'Ada', 'lastName': 'Lovelace'}]})
        self.assertEqual(proposal['title'], 'Reviewed title')
        self.assertEqual(proposal['filename'], 'Lovelace_2027_Reviewed_Journal.pdf')

    def test_custom_template_can_use_directly_printed_journal_abbreviation(self):
        self.worker.config['templates'] = {'journal_article': '{author_last}_{journal_abbreviation}_{year}'}
        self.assertEqual(self.worker._proposal(self.path)['filename'], 'Pourbaghi_ACS_Nano_Med_2026.pdf')

    def offer(self):
        with patch.object(self.worker.review, 'observe_ready', return_value=True):
            self.worker._offer('inbox', 'same-file', self.path, guidance_version=2, guidance_state='ready', findings=[])
        return self.worker.review.rows(pending_only=False)[0]

    def test_template_change_refreshes_pending_proposal_but_keeps_approved_decision(self):
        row = self.offer()
        with patch.object(self.worker, '_proposal', wraps=self.worker._proposal) as proposal:
            self.offer()
            self.assertEqual(proposal.call_count, 0)
            self.worker.config['templates'] = {'journal_article': '{author_last}_{year}'}
            changed = self.offer()
            self.assertEqual(proposal.call_count, 1)
        self.assertEqual(changed['payload']['filename'], 'Pourbaghi_2026.pdf')
        self.assertNotEqual(changed['payload']['review_token'], row['payload']['review_token'])
        self.assertEqual(changed['payload']['previous_proposal'], {key: row['payload'][key] for key in ('title', 'filename', 'document_type')})
        self.worker.review.begin(changed['id'], changed['digest'], {'filename': 'My reviewed name.pdf'})
        applying = self.worker.review.row(changed['id'])
        self.worker.config['templates']['journal_article'] = '{title}'
        self.offer()
        self.assertEqual(self.worker.review.row(changed['id']), applying)
        self.worker.review.finish(changed['id'])
        approved = self.worker.review.row(changed['id'])
        self.worker.config['templates']['journal_article'] = '{year}_{author_last}'
        self.offer()
        self.assertEqual(self.worker.review.row(changed['id']), approved)

    def test_local_recheck_refreshes_template_and_records_previous_suggestions(self):
        from tests.test_zotero_worker import ReviewZotero
        native = ReviewZotero(self.root)
        self.worker.zotero_factory = lambda: native
        row = self.offer()
        self.worker.config['templates'] = {'journal_article': '{year}_{author_last}'}
        self.worker.recheck_review()
        changed = self.worker.review.row(row['id'])
        self.assertEqual(changed['payload']['filename'], '2026_Pourbaghi.pdf')
        self.assertEqual(changed['payload']['previous_proposal'], {key: row['payload'][key] for key in ('title', 'filename', 'document_type')})
        self.assertEqual(changed['digest'], row['digest'])

    def test_useful_embedded_title_is_retained(self):
        with pymupdf.open(self.path) as document:
            document.set_metadata({'title': 'A reviewed bibliographic title'})
            changed = self.root / 'reviewed.pdf'
            document.save(changed)
        self.assertEqual(self.worker._proposal(changed)['title'], 'A reviewed bibliographic title')
