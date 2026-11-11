"""Review evidence preserves exact native identities without changing filing policy."""
import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from note_organizer.zotero_bulk import digest_file, review_policy
from note_organizer.zotero_review import fingerprint
from note_organizer.zotero_worker import Worker


class ReviewFindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.catalog = {'attachments': [], 'parents': {}}

    def parent(self, key, *, title='A scientific publication about nanoparticle imaging', doi='', collections=()):
        self.catalog['parents'][key] = {'key': key, 'itemType': 'journalArticle', 'title': title,
                                        'date': '2024', 'DOI': doi, 'collections': list(collections)}

    def file(self, key, parent, data=b'original paper bytes', *, title='Full Text', linked=False):
        path = self.root / (key + '.pdf')
        path.write_bytes(data)
        self.catalog['attachments'].append({'path': str(path), 'data': {
            'key': key, 'parentItem': parent, 'itemType': 'attachment', 'title': title,
            'filename': path.name, 'linkMode': 'linked_file' if linked else 'imported_file'}})
        return path

    def row(self, path, key='SOURCE23', parent='PARENT23', **payload):
        return {'id': 'review-one', 'kind': 'zotero', 'identity': key, 'digest': digest_file(path),
                'payload': {'path': str(path), 'attachment': key, 'parent': parent, 'details': {}, **payload}}

    def check(self, row, *, documents=None, mappings=None):
        before = copy.deepcopy((row, self.catalog, documents, mappings))
        findings = {}
        policy = review_policy([row], self.catalog, documents or {}, mappings or {}, findings=findings)
        legacy = review_policy([row], self.catalog, documents or {}, mappings or {})
        self.assertEqual(policy, legacy)
        self.assertEqual((row, self.catalog, documents, mappings), before, 'Presentation evidence must not mutate library inputs')
        return policy[row['id']], {finding['code']: finding for finding in findings[row['id']]}

    def test_reference_candidates_retain_exact_keys_and_matching_DOI_evidence(self):
        self.parent('PARENT23', doi='10.1000/example')
        self.parent('OTHER234', title='An intentionally different version title', doi='https://doi.org/10.1000/example')
        self.parent('UNRELATED', title='A different paper', doi='10.1000/different')
        path = self.file('SOURCE23', 'PARENT23')
        issues, findings = self.check(self.row(path))
        self.assertEqual(issues, ['Another Zotero reference may describe the same work; check the references or versions.'])
        finding = findings['possible_duplicate_references']
        self.assertEqual([row['key'] for row in finding['references']], ['OTHER234', 'PARENT23'])
        self.assertEqual(finding['evidence'], ['Matching DOI: 10.1000/example'])
        self.assertEqual((finding['domain'], finding['severity']), ('library', 'decision'))
        self.assertIn('not proof', finding['explanation'])

    def test_normalized_title_candidates_have_their_own_evidence(self):
        self.parent('PARENT23')
        self.parent('OTHER234', title='A SCIENTIFIC PUBLICATION: about nanoparticle imaging')
        path = self.file('SOURCE23', 'PARENT23')
        _, findings = self.check(self.row(path))
        self.assertEqual(findings['possible_duplicate_references']['evidence'], ['Matching normalized reference titles.'])

    def test_retained_baseline_match_is_not_presented_as_identical_annotated_files(self):
        self.parent('PARENT23')
        self.parent('OTHER234', title='Other citation')
        path = self.file('SOURCE23', 'PARENT23')
        self.file('VIEW2345', 'OTHER234', b'original plus tablet annotations')
        documents = {'tablet-document': {'view_key': 'VIEW2345', 'baseline': digest_file(path)}}
        issues, findings = self.check(self.row(path), documents=documents)
        self.assertIn('Identical content belongs to several Zotero attachments.', issues)
        finding = findings['repeated_pdf_content']
        self.assertIn('original PDF', finding['summary'])
        self.assertIn('current annotated files may differ', finding['explanation'])
        self.assertEqual({row['key']: row['match_basis'] for row in finding['attachments']},
                         {'SOURCE23': 'current_bytes', 'VIEW2345': 'original_baseline'})
        self.assertEqual({row['key'] for row in finding['references']}, {'PARENT23', 'OTHER234'})

    def test_current_byte_matches_do_not_imply_interchangeable_notes_and_annotations(self):
        self.parent('PARENT23')
        self.parent('OTHER234', title='Other citation')
        path = self.file('SOURCE23', 'PARENT23')
        self.file('COPY2345', 'OTHER234')
        _, findings = self.check(self.row(path))
        finding = findings['repeated_pdf_content']
        self.assertIn('same file bytes', finding['summary'])
        self.assertEqual({row['match_basis'] for row in finding['attachments']}, {'current_bytes'})
        self.assertIn('Zotero annotations are interchangeable', finding['explanation'])
        self.assertIn('does not establish', finding['explanation'])

    def test_multiple_PDFs_explain_legitimate_roles_and_retain_attachment_keys(self):
        self.parent('PARENT23')
        path = self.file('SOURCE23', 'PARENT23')
        self.file('SUPP2345', 'PARENT23', b'supporting information', title='Supporting Information')
        issues, findings = self.check(self.row(path))
        self.assertEqual(issues, ['This reference has multiple PDFs; choose its original or supporting file before linking.'])
        finding = findings['multiple_pdf_attachments']
        self.assertIn('valid main paper, supplement, or manuscript', finding['explanation'])
        self.assertEqual({row['key'] for row in finding['attachments']}, {'SOURCE23', 'SUPP2345'})
        self.assertEqual(finding['references'][0]['key'], 'PARENT23')

    def test_missing_queued_source_does_not_turn_one_live_pdf_into_multiple_pdfs(self):
        self.parent('PARENT23')
        path = self.file('SOURCE23', 'PARENT23')
        row = self.row(path)
        self.catalog['attachments'].clear()
        self.file('VIEW2345', 'PARENT23', b'annotated surviving PDF', title='Main text (reMarkable notes)')
        _, findings = self.check(row)
        self.assertNotIn('multiple_pdf_attachments', findings)

    def test_trashed_pdf_is_not_counted_as_current_multiple_pdf_evidence(self):
        self.parent('PARENT23')
        path = self.file('SOURCE23', 'PARENT23')
        self.file('VIEW2345', 'PARENT23', b'annotated surviving PDF')
        self.catalog['attachments'][0]['data']['deleted'] = True
        _, findings = self.check(self.row(path))
        self.assertNotIn('multiple_pdf_attachments', findings)
        self.assertNotIn('repeated_pdf_content', findings)

    def test_two_live_pdfs_still_have_role_guidance_if_old_queued_source_is_missing(self):
        self.parent('PARENT23')
        old_path = self.file('SOURCE23', 'PARENT23')
        self.catalog['attachments'].clear()
        self.file('MAIN2345', 'PARENT23', b'current paper')
        self.file('SUPP2345', 'PARENT23', b'current supplement')
        _, findings = self.check(self.row(old_path))
        self.assertEqual(findings['multiple_pdf_attachments']['evidence'], ['2 existing PDF attachments under this reference.'])
        self.assertEqual({r['key'] for r in findings['multiple_pdf_attachments']['attachments']}, {'MAIN2345', 'SUPP2345'})

    def test_only_connected_collection_choices_are_returned(self):
        self.parent('PARENT23', collections=['HOME2345', 'HOME6789', 'RESEARCH'])
        path = self.file('SOURCE23', 'PARENT23')
        _, findings = self.check(self.row(path), mappings={'HOME2345': {'name': 'Reading'}, 'HOME6789': {'name': 'Research'}})
        finding = findings['multiple_tablet_destinations']
        self.assertEqual(finding['collections'], [{'key': 'HOME2345', 'name': 'Reading'}, {'key': 'HOME6789', 'name': 'Research'}])
        self.assertIn('memberships are valid', finding['explanation'])
        self.assertNotIn('RESEARCH', str(finding['collections']))

    def test_incoming_cloud_PDF_does_not_inflate_existing_attachment_count(self):
        self.parent('PARENT23')
        self.file('EXIST234', 'PARENT23')
        incoming = self.root / 'incoming.pdf'
        incoming.write_bytes(b'new tablet PDF')
        row = self.row(incoming)
        row['kind'], row['identity'] = 'cloud', 'tablet-document'
        row['payload'].pop('attachment')
        _, findings = self.check(row)
        finding = findings['multiple_pdf_attachments']
        self.assertEqual(finding['evidence'], ['1 existing PDF attachment under this reference.'])
        self.assertEqual([item['key'] for item in finding['attachments']], ['EXIST234'])

    def test_missing_citation_fields_alone_remain_routine_without_findings(self):
        self.parent('PARENT23')
        path = self.file('SOURCE23', 'PARENT23')
        issues, findings = self.check(self.row(path, details={'year': 'Unknown', 'journal': 'Unknown'}))
        self.assertEqual((issues, findings), ([], {}))

    def test_file_and_filing_conditions_have_repair_or_decision_context(self):
        self.parent('PARENT23')
        path = self.file('SOURCE23', 'PARENT23', linked=True)
        row = self.row(path, repair_required='A saved filing step needs recovery.',
                       possible_tablet_matches=[{'title': 'Same name on tablet'}],
                       details={'review_reasons': ['Could not read this encrypted PDF.'], 'filename_needs_review': True,
                                'is_supplementary_material': True})
        # Keep the catalog row but make its actual file unavailable.
        path.unlink()
        _, findings = self.check(row)
        self.assertEqual(set(findings), {'repair_required', 'local_file_unavailable', 'readability_check',
                         'tablet_name_content_mismatch', 'readable_filename_needed', 'supporting_information_parent'})
        for code in ['repair_required', 'local_file_unavailable', 'readability_check']:
            self.assertEqual(findings[code]['severity'], 'repair')
        self.assertEqual(findings['tablet_name_content_mismatch']['domain'], 'sync')

    def test_linked_file_and_multiple_native_counterparts_keep_their_exact_targets(self):
        self.parent('PARENT23')
        self.parent('OTHER234', title='Other citation')
        path = self.file('SOURCE23', 'PARENT23', linked=True)
        self.file('COPY2345', 'OTHER234', b'different content')
        _, findings = self.check(self.row(path, matches=[{'key': 'SOURCE23'}, {'key': 'COPY2345'}]))
        self.assertEqual(findings['external_linked_file']['attachments'][0]['key'], 'SOURCE23')
        self.assertEqual({item['key'] for item in findings['multiple_identical_counterparts']['attachments']},
                         {'SOURCE23', 'COPY2345'})

    def test_mapping_conflict_requires_no_file_path_and_preserves_legacy_precedence(self):
        self.parent('PARENT23')
        row = {'id': 'conflict', 'identity': 'cloud', 'kind': 'conflict', 'digest': 'state',
               'payload': {'type': 'paper', 'zotero': 'PARENT23', 'repair_required': 'Older repair condition'}}
        issues, findings = self.check(row)
        self.assertEqual(issues, ['A linked document or folder has conflicting changes.'])
        self.assertEqual(list(findings), ['mapping_conflict'])
        self.assertEqual(findings['mapping_conflict']['references'][0]['key'], 'PARENT23')

    def test_worker_saves_findings_in_the_existing_token_and_preserves_active_recovery(self):
        self.parent('PARENT23', doi='10.1000/example')
        self.parent('OTHER234', doi='10.1000/example')
        path = self.file('SOURCE23', 'PARENT23')
        row = self.row(path)
        worker = Worker(self.root / 'helper')
        identity = worker.review.offer(row['kind'], row['identity'], row['digest'], row['payload'])
        worker.refresh_review_policy(SimpleNamespace(), self.catalog)
        saved = worker.review.row(identity)
        payload = saved['payload']
        self.assertFalse(payload['routine'])
        self.assertEqual(payload['findings'][0]['code'], 'possible_duplicate_references')
        self.assertEqual(payload['review_token'], fingerprint({key: value for key, value in payload.items() if key != 'review_token'}))
        worker.review.begin(identity, row['digest'], {})
        before = worker.review.row(identity)
        self.catalog['parents']['OTHER234']['title'] = 'Corrected during recovery'
        worker.refresh_review_policy(SimpleNamespace(), self.catalog)
        self.assertEqual(worker.review.row(identity), before, 'Do not replace an applying review journal or its original token')


if __name__ == '__main__':
    unittest.main()
