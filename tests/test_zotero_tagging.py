from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

import pymupdf

from note_organizer.zotero_tagging import (
    METADATA, PRINTED, analyse, append_tags, backfill_keywords, cleanup_candidates, document_keywords,
    keyword_candidates, keyword_report, linked_keyword_report, stated_keywords,
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

    def keyword_warning_link(self):
        path = self.add_pdf()
        with pymupdf.open() as scan:
            scan.new_page()
            data = scan.tobytes()
        path.write_bytes(data)
        digest = fixtures.file_sha(path)
        original = self.worker.state.directory / 'sources' / 'remarkable' / 'baselines' / (digest + '.pdf')
        original.parent.mkdir(parents=True, exist_ok=True)
        original.write_bytes(data)
        link = {'parent': 'PARENT23', 'view_key': 'SOURCE23', 'baseline': digest,
                'keyword_check': {'status': 'needs_text_recognition', 'version': 4, 'sha256': digest}}
        self.worker.state.write('documents', {'tablet': link, 'another-tablet': {**link, 'view_key': ''}})
        return path, original

    def test_recheck_uses_the_pdf_in_zotero_and_keeps_other_warnings(self):
        path, original = self.keyword_warning_link()
        self.worker.last = {'result': {'review': [{'cloud': 'tablet', 'reason': 'Unrelated device warning'}]}}
        self.assertEqual(len(self.worker.status()['status']['result']['review']), 3)
        path.unlink()
        make_pdf(path)
        writes = self.zot.writes
        with patch.object(self.worker, 'cloud', side_effect=AssertionError('The local check must not access reMarkable')):
            result = self.worker.dispatch('recheck_keyword_warnings', {'cloud': 'tablet'})
        self.assertEqual(result['keyword_recheck'], {'checked': 1, 'resolved': 1, 'unavailable': 0})
        self.assertEqual(self.zot.writes, writes)
        reviews = result['status']['result']['review']
        self.assertEqual([issue['cloud'] for issue in reviews], ['tablet', 'another-tablet'])
        self.assertEqual(reviews[0]['reason'], 'Unrelated device warning')
        self.assertIsNone(result['last_sync_at'])

        links = self.worker.state.read('documents', {})
        backfill_keywords(self.zot, 'PARENT23', original, links['tablet'],
                          lambda: self.worker.state.write('documents', links))
        self.assertEqual([tag['tag'] for tag in self.zot.items['PARENT23']['tags']], ['Spectral CT', 'nanoparticles'])
        self.assertEqual(links['tablet']['keyword_check']['attachment'], 'SOURCE23')

    def test_existing_zotero_tags_clear_unreadable_warnings_without_rewriting_tags(self):
        path, original = self.keyword_warning_link()
        parent = self.zot.items['PARENT23']
        parent['tags'] = [{'tag': 'Iodine', 'type': 1}, {'tag': 'Materials', 'type': 0}]
        writes = self.zot.writes
        with patch('note_organizer.zotero_tagging.linked_keyword_report', side_effect=AssertionError('Tags already cover this warning')):
            result = self.worker.recheck_keyword_warnings()
        self.assertEqual(result['keyword_recheck']['resolved'], 2)
        self.assertEqual(result['status'].get('result', {}).get('review', []), [])
        self.assertEqual(self.zot.writes, writes)
        self.assertEqual([tag['tag'] for tag in parent['tags']], ['Iodine', 'Materials'])
        links = self.worker.state.read('documents', {})
        backfill_keywords(self.zot, 'PARENT23', original, links['tablet'],
                          lambda: self.worker.state.write('documents', links))
        self.assertEqual(links['tablet']['keyword_check']['status'], 'needs_text_recognition')
        self.assertEqual(self.worker.status()['status'].get('result', {}).get('review', []), [])
        parent['tags'].clear()
        result = self.worker.recheck_keyword_warnings()
        self.assertEqual(result['keyword_recheck']['checked'], 2)
        self.assertEqual(len(result['status']['result']['review']), 2)

    def test_recheck_retires_a_trashed_reference_warning_and_recovers_when_restored(self):
        self.keyword_warning_link()
        parent = self.zot.items['PARENT23']
        parent['deleted'] = True
        result = self.worker.recheck_keyword_warnings(cloud='tablet')
        self.assertEqual(result['keyword_recheck'], {'checked': 1, 'resolved': 1, 'unavailable': 0})
        self.assertEqual([row['cloud'] for row in result['status']['result']['review']], ['another-tablet'])
        self.assertTrue(self.worker.state.documents_with_keyword_reviews()['tablet']['keyword_parent_unavailable'])

        parent.pop('deleted')
        result = self.worker.recheck_keyword_warnings()
        self.assertEqual({row['cloud'] for row in result['status']['result']['review']}, {'tablet', 'another-tablet'})
        self.assertNotIn('keyword_parent_unavailable', self.worker.state.documents_with_keyword_reviews()['tablet'])

    def test_recheck_keeps_warning_if_attachment_is_reparented_during_extraction(self):
        path, _ = self.keyword_warning_link()
        path.unlink()
        make_pdf(path)

        def reparent(document, vocabulary):
            report = keyword_report(document, vocabulary)
            if document == path:
                self.zot.items['SOURCE23']['parentItem'] = 'OTHERREF'
            return report

        with patch('note_organizer.zotero_tagging.keyword_report', side_effect=reparent):
            result = self.worker.recheck_keyword_warnings(cloud='tablet')
        self.assertEqual(result['keyword_recheck'], {'checked': 0, 'resolved': 0, 'unavailable': 1})
        self.assertEqual(len(result['status']['result']['review']), 2)

    def test_recheck_uses_reference_tags_added_while_reading(self):
        self.keyword_warning_link()

        def tag_during_check(client, record, original, link):
            report = linked_keyword_report(client, record, original, link)
            self.zot.items['PARENT23']['tags'] = [{'tag': 'Added while checking', 'type': 0}]
            return report

        with patch('note_organizer.zotero_tagging.linked_keyword_report', side_effect=tag_during_check):
            result = self.worker.recheck_keyword_warnings(cloud='tablet')
        self.assertEqual(result['keyword_recheck']['resolved'], 1)
        self.assertEqual([row['cloud'] for row in result['status']['result']['review']], ['another-tablet'])

    def test_recheck_does_not_overwrite_a_concurrent_relink(self):
        path, _ = self.keyword_warning_link()
        path.unlink()
        make_pdf(path)

        def relink(client, record, original, link):
            report = linked_keyword_report(client, record, original, link)
            saved = self.worker.state.read('documents')
            saved['tablet']['parent'] = 'NEWREF23'
            saved['another-tablet']['name'] = 'Changed outside recheck'
            self.worker.state.write('documents', saved)
            return report

        with patch('note_organizer.zotero_tagging.linked_keyword_report', side_effect=relink):
            result = self.worker.recheck_keyword_warnings(cloud='tablet')
        self.assertEqual(result['keyword_recheck'], {'checked': 0, 'resolved': 0, 'unavailable': 1})
        saved = self.worker.state.read('documents')
        self.assertEqual(saved['tablet']['parent'], 'NEWREF23')
        self.assertEqual(saved['another-tablet']['name'], 'Changed outside recheck')
        self.assertNotIn('keyword_review', saved['tablet'])

    def test_recheck_finishes_while_sync_holds_its_lock_and_saves_other_papers(self):
        self.keyword_warning_link()
        state = self.worker.state
        original = state.read('documents')
        self.zot.items['PARENT23']['tags'] = [{'tag': 'Already tagged'}]
        with state.lock():
            result = self.worker.recheck_keyword_warnings(cloud='tablet')
            self.assertEqual(result['keyword_recheck'], {'checked': 1, 'resolved': 1, 'unavailable': 0})
            self.assertEqual(original, state.read('documents'))
            original['another-tablet']['name'] = 'Newer sync name'
            state.write('documents', original)
        links = state.documents_with_keyword_reviews()
        self.assertTrue(links['tablet']['keyword_has_zotero_tags'])
        self.assertEqual(links['another-tablet']['name'], 'Newer sync name')
        self.assertEqual([row['cloud'] for row in self.worker.status()['status']['result']['review']], ['another-tablet'])
        original['tablet']['keyword_epoch'] = 'newer collected sync evidence'
        state.write('documents', original)
        self.assertNotIn('keyword_has_zotero_tags', state.documents_with_keyword_reviews()['tablet'])

    def test_recheck_retries_only_the_paper_with_newer_sync_evidence(self):
        self.keyword_warning_link()
        calls = []

        def advance_revision(client, record, original, row):
            calls.append(row.get('view_key'))
            if len(calls) == 1:
                current = self.worker.state.read('documents')
                current['tablet']['revision'] = 'Published during recheck'
                self.worker.state.write('documents', current)
            return linked_keyword_report(client, record, original, row)

        with patch('note_organizer.zotero_tagging.linked_keyword_report', side_effect=advance_revision):
            result = self.worker.recheck_keyword_warnings()
        self.assertEqual(calls, ['SOURCE23', '', 'SOURCE23'])
        self.assertEqual(result['keyword_recheck'], {'checked': 2, 'resolved': 0, 'unavailable': 0})

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

    def test_backfill_preserves_reference_edits_made_while_reading(self):
        path = self.add_pdf()
        path.unlink()
        make_pdf(path)

        def edit_reference(client, record, original, link, previous):
            report = linked_keyword_report(client, record, original, link, previous)
            self.zot.items['PARENT23'].update(title='Native title edit', tags=[{'tag': 'Native tag edit', 'type': 0}])
            return report

        with patch('note_organizer.zotero_tagging.linked_keyword_report', side_effect=edit_reference):
            checked = backfill_keywords(self.zot, 'PARENT23', path, {}, lambda: None)
        item = self.zot.items['PARENT23']
        self.assertEqual(item['title'], checked['title'])
        self.assertEqual(checked['title'], 'Native title edit')
        self.assertEqual([tag['tag'] for tag in item['tags']], ['Native tag edit', 'Spectral CT', 'nanoparticles'])

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


