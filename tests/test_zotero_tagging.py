from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

import pymupdf

from note_organizer.zotero_tagging import (
    METADATA, PRINTED, analyse, append_tags, backfill_keywords, cleanup_candidates, document_keywords,
    keyword_candidates, stated_keywords,
)
from note_organizer.zotero_pilot import PilotError
from tests import test_zotero_worker as fixtures
from tests import test_zotero_bulk as bulk_fixtures


def make_pdf(path):
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((40, 80), 'Spectral CT for nanoparticle contrast\nKeywords: Spectral CT; nanoparticles\n\nIntroduction\nSpectral CT measures iodine concentration. Nanoparticles improve contrast.')
        document.save(path)
    return path


class PaperTagTests(unittest.TestCase):
    def test_explicit_keyword_section_stops_at_the_body(self):
        rows = stated_keywords('Title\nKeywords: Spectral CT; Nanoparticles\n\nIntroduction\nOther content', vocabulary=['spectral CT'])
        self.assertEqual([row['tag'] for row in rows], ['spectral CT', 'Nanoparticles'])
        self.assertTrue(all(row['source'].startswith('Printed') for row in rows))

    def test_keyword_metadata_is_distinguished_and_duplicates_canonicalized(self):
        rows = stated_keywords('Keywords: CT; nanoparticles', 'CT, Nanoparticles', ['Nanoparticles'])
        self.assertEqual([row['tag'] for row in rows], ['CT', 'Nanoparticles'])
        self.assertTrue(all('metadata' in row['source'] for row in rows))

    def test_text_without_keyword_heading_is_never_automatic(self):
        self.assertEqual(stated_keywords('We discuss spectral CT and nanoparticles.'), [])

    def test_quotes_and_chemical_commas_do_not_create_fragments(self):
        rows, review = keyword_candidates('', '"Computed tomography"; "Contrast agents, nanoparticles"; '
                                           'methyl(3,3,3-trifluoropropyl)siloxane; {M6O19}-type POM; CuSO4·5H2O',
                                           ['"Computed tomography"'])
        self.assertEqual([row['tag'] for row in rows], ['Computed tomography', 'Contrast agents, nanoparticles',
            'methyl(3,3,3-trifluoropropyl)siloxane', '{M6O19}-type POM', 'CuSO4·5H2O'])
        self.assertEqual(review, [])
        old = {'tag': '3-trifluoropropyl)siloxane', 'source': METADATA,
               'evidence': 'Cyclotetrasiloxane; methyl(3,3,3-trifluoropropyl)siloxane; X-ray structure'}
        repair, = cleanup_candidates({'keywords': [old]}).values()
        self.assertEqual(repair['replacement'], 'methyl(3,3,3-trifluoropropyl)siloxane')
        split, = cleanup_candidates({'keywords': [{'tag': 'CT . Tin filtration', 'source': PRINTED,
                                                 'evidence': 'Keywords: CT . Tin filtration'}]}).values()
        self.assertEqual(split['replacements'], ['CT', 'Tin filtration'])

    def test_citations_and_body_text_are_not_automatic_keywords(self):
        rows = stated_keywords('Keywords are important for indexing; this article describes how.\n\n'
                               'Keywords\nAbstract\n\nA completely separate list, another paragraph')
        self.assertEqual(rows, [])
        rows, review = keyword_candidates('Keywords: Spectral CT; [11]. To; (d) linear interpola-; '
                                           '26 in cardiac calcium scoring; these results show one more thing')
        self.assertEqual([row['tag'] for row in rows], ['Spectral CT'])
        self.assertEqual(len(review), 4)
        self.assertEqual(append_tags([], ['Spectral CT', '(Invest Radiol 2004', '3-trifluoropropyl)siloxane']),
                         [{'tag': 'Spectral CT', 'type': 1}])

    def test_dot_delimiters_split_without_splitting_decimals_or_dotted_names(self):
        keywords, review = keyword_candidates('Keywords: CT . Tin filtration . 1.5 T MRI . E. coli')
        self.assertEqual([row['tag'] for row in keywords], ['CT', 'Tin filtration', '1.5 T MRI', 'E. coli'])
        self.assertEqual(review, [])

    def test_mixed_metadata_and_clipped_phrases_require_review(self):
        keywords, review = keyword_candidates(
            'Keywords: Contrast imaging; Models and\n\nIntroduction',
            '05 Feb 2015, Investigative Radiology, Example University, Contrast imaging',
        )
        self.assertEqual([(row['tag'], row['source']) for row in keywords], [('Contrast imaging', PRINTED)])
        self.assertEqual([row['tag'] for row in review],
                         ['05 Feb 2015', 'Investigative Radiology', 'Example University', 'Models and'])
        self.assertTrue(all(row['reason'] and row['evidence'] for row in review))
        self.assertEqual([row['tag'] for row in stated_keywords('Keywords: Imaging models and\nalgorithms')],
                         ['Imaging models and algorithms'])

    def test_additions_preserve_existing_types_and_do_not_duplicate_case_variants(self):
        existing = [{'tag': 'Spectral CT', 'type': 0}, {'tag': 'Existing', 'type': 1}]
        tags = append_tags(existing, ['spectral ct', 'Nanoparticles'], ['Existing', 'Iodine'])
        self.assertEqual(tags, existing + [{'tag': 'Nanoparticles', 'type': 1}, {'tag': 'Iodine', 'type': 0}])

    def test_pdf_analysis_is_local_and_keeps_bytes_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = make_pdf(Path(directory) / 'paper.pdf')
            before = path.read_bytes()
            result = analyse(path, ['iodine concentration'])
            self.assertEqual([row['tag'] for row in result['keywords']], ['Spectral CT', 'nanoparticles'])
            self.assertIn('iodine concentration', [row['tag'] for row in result['suggestions']])
            self.assertEqual(path.read_bytes(), before)

    def test_missing_or_scanned_document_does_not_invent_keywords(self):
        self.assertEqual(document_keywords(Path('missing-file.pdf')), [])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'scan.pdf'
            with pymupdf.open() as document:
                document.new_page(); document.save(path)
            result = analyse(path)
            self.assertEqual(result['keywords'], [])
            self.assertEqual(result['suggestions'], [])


class TagWorkflowTests(unittest.TestCase):
    setUp = fixtures.WorkerTests.setUp
    add_pdf = fixtures.WorkerTests.add_pdf

    def test_legacy_cleanup_previews_evidence_and_removes_only_selected_automatic_tags(self):
        self.add_pdf()
        record = self.zot.items['PARENT23']
        record['tags'] = [{'tag': '05 Feb 2015', 'type': 1}, {'tag': 'Models and', 'type': 1},
                          {'tag': 'Example University', 'type': 0}, {'tag': 'Spectral CT', 'type': 1}]
        keywords = [{'tag': tag, 'source': METADATA, 'evidence': '05 Feb 2015, Example University'}
                    for tag in ('05 Feb 2015', 'Example University')]
        keywords.append({'tag': 'Models and', 'source': PRINTED, 'evidence': 'Keywords: Models and'})
        self.worker.state.write('documents', {'tablet': {'parent': 'PARENT23',
                                'keyword_check': {'version': 2, 'keywords': keywords}}})
        before = list(record['tags'])
        catalog = self.zot.review_action('catalog')
        catalog['parents']['PARENT23']['version'] = 1
        with patch.object(self.zot, 'review_action', return_value=catalog):
            preview, = self.worker.preview_keyword_cleanup()['papers']
        self.assertEqual(preview['token'], self.worker.preview_keyword_cleanup()['papers'][0]['token'])
        self.assertEqual(record['tags'], before)
        self.assertEqual([row['tag'] for row in preview['tags']], ['05 Feb 2015', 'Models and'])
        with self.assertRaises(PilotError):
            self.worker.remove_reviewed_keywords('PARENT23', preview['token'], ['Example University'])
        record['tags'].append({'tag': 'New manual edit'})
        with self.assertRaises(PilotError):
            self.worker.remove_reviewed_keywords('PARENT23', preview['token'], ['05 Feb 2015'])
        preview, = self.worker.preview_keyword_cleanup()['papers']
        self.worker.remove_reviewed_keywords('PARENT23', preview['token'], ['05 Feb 2015'])
        self.assertEqual([tag['tag'] for tag in self.zot.items['PARENT23']['tags']],
                         ['Models and', 'Example University', 'Spectral CT', 'New manual edit'])

    def test_backfill_upgrade_keeps_cleanup_evidence_and_respects_prior_removals(self):
        path = self.add_pdf()
        path.unlink()
        make_pdf(path)
        record = self.zot.items['PARENT23']
        record['tags'] = [{'tag': 'Models and', 'type': 1}]
        row = {'keyword_check': {'version': 2, 'keywords': [
            {'tag': 'Spectral CT', 'source': PRINTED, 'evidence': 'Keywords: Spectral CT'},
            {'tag': 'Models and', 'source': PRINTED, 'evidence': 'Keywords: Models and'},
        ]}}
        report = backfill_keywords(self.zot, 'PARENT23', path, row, lambda: None)
        self.assertEqual([entry['tag'] for entry in report['cleanup']], ['Models and'])
        self.assertEqual([tag['tag'] for tag in self.zot.items['PARENT23']['tags']], ['Models and', 'nanoparticles'])

    def test_automatic_suggestions_reuse_current_analysis_and_do_not_write_tags(self):
        path = self.add_pdf()
        progress = []
        self.worker.progress = progress.append
        report = {'keywords': [], 'suggestions': [], 'notice': 'Local suggestions'}
        with patch('note_organizer.zotero_tagging.analyse', return_value=report) as analyse:
            first = self.worker.suggest_paper_tags(key='SOURCE23', quiet=True)
            again = self.worker.suggest_paper_tags(key='SOURCE23', quiet=True)
            self.assertTrue(again['cached'])
            self.assertEqual(first['proposal']['token'], again['proposal']['token'])
            self.assertEqual(analyse.call_count, 1)
            self.assertEqual(progress, [])
            self.assertEqual(self.zot.items['PARENT23'].get('tags', []), [])
            self.zot.items['PARENT23']['tags'] = [{'tag': 'Manual edit', 'type': 0}]
            self.worker.suggest_paper_tags(key='SOURCE23', quiet=True)
            self.assertEqual(analyse.call_count, 2)
            path.write_bytes(path.read_bytes() + b'changed')
            self.worker.suggest_paper_tags(key='SOURCE23', quiet=True)
            self.assertEqual(analyse.call_count, 3)
            self.assertEqual(self.zot.items['PARENT23']['tags'], [{'tag': 'Manual edit', 'type': 0}])

    def test_native_suggestions_require_explicit_selection_and_preserve_tags(self):
        path = self.add_pdf(); path.unlink(); make_pdf(path)
        self.zot.items['PARENT23']['tags'] = [{'tag': 'Curated', 'type': 0}]
        before = self.zot.items['PARENT23'].copy()
        value = self.worker.suggest_paper_tags(key='SOURCE23')['proposal']
        self.assertEqual(self.zot.items['PARENT23'], before)
        self.worker.save_paper_tags(value['target'], value['token'], [])
        tags = self.zot.items['PARENT23']['tags']
        self.assertIn({'tag': 'Curated', 'type': 0}, tags)
        self.assertIn({'tag': 'Spectral CT', 'type': 1}, tags)
        self.assertFalse({row['tag'] for row in value['suggestions']} & {row['tag'] for row in tags})

    def test_stale_file_or_tags_and_unoffered_suggestions_are_rejected(self):
        path = self.add_pdf(); path.unlink(); make_pdf(path)
        proposal = self.worker.suggest_paper_tags(key='SOURCE23')['proposal']
        with self.assertRaises(PilotError):
            self.worker.save_paper_tags(proposal['target'], proposal['token'], ['Invented tag'])
        self.zot.items['PARENT23']['tags'] = [{'tag': 'New manual edit'}]
        with self.assertRaises(PilotError):
            self.worker.save_paper_tags(proposal['target'], proposal['token'], [])
        proposal = self.worker.suggest_paper_tags(key='SOURCE23')['proposal']
        path.write_bytes(path.read_bytes() + b'changed')
        with self.assertRaises(PilotError):
            self.worker.save_paper_tags(proposal['target'], proposal['token'], [])

    def test_cloud_review_choices_do_not_import_or_write_tags_until_filing(self):
        path = make_pdf(self.root / 'cloud-paper.pdf')
        cloud = self.cloud.upload_pdf(path.read_bytes(), 'Tablet paper', self.croot.id, str(uuid.uuid4()), self.croot.id)
        self.worker.sync()
        row = next(row for row in self.worker.review.rows() if row['identity'] == cloud.id)
        proposal = self.worker.suggest_paper_tags(identity=row['id'])['proposal']
        chosen = [proposal['suggestions'][0]['tag']] if proposal['suggestions'] else []
        result = self.worker.save_paper_tags(proposal['target'], proposal['token'], chosen)
        self.assertTrue(result['deferred_until_filing'])
        self.assertFalse(self.zot.items)
        self.worker.approve(row['id'], row['digest'], 'Paper.pdf', 'Paper', 'journal_article', 'ROOT2345')
        reference = next(row for row in self.zot.items.values() if row.get('itemType') == 'journalArticle')
        self.assertIn('Spectral CT', [tag['tag'] for tag in reference['tags']])
        for tag in chosen:
            self.assertIn({'tag': tag, 'type': 0}, reference['tags'])


class KeywordImportTests(unittest.TestCase):
    setUp = bulk_fixtures.BulkTests.setUp

    def test_explicit_import_adds_stated_keywords_but_no_suggested_topics(self):
        path = make_pdf(self.source / 'Paper.pdf')
        original = path.read_bytes()
        self.worker.scan_library(str(self.source), 'ROOT2345', lookup=False)
        plan = self.worker.bulk.read()
        self.worker.import_library(plan['id'])
        tags = next(row['tags'] for row in self.zot.items.values() if row.get('itemType') == 'document')
        self.assertEqual(tags, [{'tag': 'Spectral CT', 'type': 1}, {'tag': 'nanoparticles', 'type': 1}])
        self.assertEqual(path.read_bytes(), original)


