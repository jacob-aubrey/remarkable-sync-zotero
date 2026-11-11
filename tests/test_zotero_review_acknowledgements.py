"""Intentional library arrangements are scoped evidence, never paper consent."""
import copy
from pathlib import Path
import tempfile
import unittest

from note_organizer.zotero_bulk import digest_file, review_policy
from note_organizer.zotero_pilot import PilotError
from note_organizer.zotero_review import ReviewStore, fingerprint


class ReviewAcknowledgementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.review = ReviewStore(self.root / 'helper')
        self.catalog = {'parents': {}, 'attachments': []}
        self.documents = {}
        self.parent('PARENT23', title='A shared scientific publication about imaging')
        self.parent('OTHER234', title='A shared scientific publication about imaging')
        self.path = self.file('SOURCE23', 'PARENT23', b'first paper')
        self.file('COPY2345', 'OTHER234', b'different edition')
        self.row = {'id': 'initial', 'kind': 'zotero', 'identity': 'SOURCE23',
                    'digest': digest_file(self.path), 'payload': {'path': str(self.path),
                    'parent': 'PARENT23', 'attachment': 'SOURCE23', 'details': {}}}

    def parent(self, key, **changes):
        self.catalog['parents'][key] = {'key': key, 'itemType': 'journalArticle',
            'title': key, 'date': '2024', 'DOI': '10.1000/test', 'collections': [], **changes}

    def file(self, key, parent, contents):
        path = self.root / (key + '.pdf')
        path.write_bytes(contents)
        self.catalog['attachments'].append({'path': str(path), 'data': {'key': key,
            'itemType': 'attachment', 'parentItem': parent, 'filename': path.name,
            'title': 'Full Text', 'linkMode': 'imported_file'}})
        return path

    def policy(self, row=None):
        row = row or self.row
        findings = {}
        issues = review_policy([row], self.catalog, self.documents, {}, findings=findings,
                               acknowledgements=self.review.acknowledgement_tokens())
        return issues[row['id']], {value['code']: value for value in findings[row['id']]}

    def save(self):
        issues, findings = self.policy()
        payload = {**self.row['payload'], 'issues': issues, 'findings': list(findings.values()),
                   'routine': not issues}
        payload['review_token'] = fingerprint(payload)
        identity = self.review.offer(self.row['kind'], self.row['identity'], self.row['digest'], payload)
        return self.review.row(identity)

    def acknowledge(self, code='possible_duplicate_references', *, keep_separate=True):
        row = self.save()
        finding = next(value for value in row['payload']['findings'] if value['code'] == code)
        self.review.acknowledge_finding(row['id'], row['digest'], code,
            finding['acknowledgement']['token'], expected_review_token=row['payload']['review_token'],
            keep_separate=keep_separate)
        return row, finding

    def test_explicit_acknowledgement_does_not_approve_or_dismiss_paper(self):
        row, finding = self.acknowledge()
        self.assertEqual(self.review.row(row['id']), row)
        self.assertEqual(self.review.acknowledgement_tokens(), {finding['acknowledgement']['token']})
        issues, refreshed = self.policy()
        self.assertEqual(issues, [])
        self.assertTrue(refreshed['possible_duplicate_references']['acknowledgement']['acknowledged'])
        self.assertEqual(self.review.row(row['id'])['status'], 'pending')

    def test_default_policy_remains_unchanged_and_requires_explicit_choice(self):
        issues, findings = self.policy()
        self.assertEqual(issues, ['Another Zotero reference may describe the same work; check the references or versions.'])
        self.assertTrue(findings['possible_duplicate_references']['acknowledgement']['eligible'])
        self.assertFalse(findings['possible_duplicate_references']['acknowledgement']['acknowledged'])

    def test_acknowledging_reference_overlap_keeps_content_and_file_blockers(self):
        second = Path(self.catalog['attachments'][1]['path'])
        second.write_bytes(self.path.read_bytes())
        self.catalog['attachments'][0]['data']['linkMode'] = 'linked_file'
        self.acknowledge()
        issues, findings = self.policy()
        self.assertIn('Identical content belongs to several Zotero attachments.', issues)
        self.assertIn('This PDF is linked to an external Windows file; store a Zotero copy before tablet updates.', issues)
        self.assertFalse(findings['repeated_pdf_content']['acknowledgement']['acknowledged'])

    def test_acknowledging_repeated_content_does_not_confirm_attachment_roles(self):
        self.catalog['parents'].pop('OTHER234')
        self.catalog['attachments'][1]['data']['parentItem'] = 'PARENT23'
        Path(self.catalog['attachments'][1]['path']).write_bytes(self.path.read_bytes())
        self.acknowledge('repeated_pdf_content')
        issues, findings = self.policy()
        self.assertEqual(issues, ['This reference has multiple PDFs; choose its original or supporting file before linking.'])
        self.assertNotIn('acknowledgement', findings['multiple_pdf_attachments'])

    def test_same_exact_participants_share_acknowledgement_across_review_rows(self):
        self.acknowledge()
        other = copy.deepcopy(self.row)
        other.update(id='other-review', identity='COPY2345', digest=digest_file(Path(self.catalog['attachments'][1]['path'])))
        other['payload'].update(path=self.catalog['attachments'][1]['path'], attachment='COPY2345', parent='OTHER234')
        issues, findings = self.policy(other)
        self.assertEqual(issues, [])
        self.assertTrue(findings['possible_duplicate_references']['acknowledgement']['acknowledged'])

    def test_catalog_and_row_order_do_not_change_arrangement_identity(self):
        _, before = self.acknowledge()
        self.catalog['attachments'].reverse()
        self.catalog['parents'] = dict(reversed(list(self.catalog['parents'].items())))
        _, findings = self.policy()
        after = findings['possible_duplicate_references']['acknowledgement']
        self.assertEqual(after['token'], before['acknowledgement']['token'])
        self.assertTrue(after['acknowledged'])

    def test_exact_repeated_content_set_shares_across_rows_but_new_copy_reopens(self):
        second = Path(self.catalog['attachments'][1]['path'])
        second.write_bytes(self.path.read_bytes())
        self.acknowledge('repeated_pdf_content')
        other = copy.deepcopy(self.row)
        other.update(id='other-review', identity='COPY2345')
        other['payload'].update(path=str(second), attachment='COPY2345', parent='OTHER234')
        _, findings = self.policy(other)
        self.assertTrue(findings['repeated_pdf_content']['acknowledgement']['acknowledged'])
        self.parent('THIRD234', title='An independent reference')
        self.file('THIRDATT', 'THIRD234', self.path.read_bytes())
        issues, findings = self.policy(other)
        self.assertFalse(findings['repeated_pdf_content']['acknowledgement']['acknowledged'])
        self.assertIn('Identical content belongs to several Zotero attachments.', issues)

    def test_unrelated_reading_changes_do_not_reopen_repeated_content(self):
        Path(self.catalog['attachments'][1]['path']).write_bytes(self.path.read_bytes())
        self.acknowledge('repeated_pdf_content')
        self.catalog['parents']['PARENT23'].update(tags=[{'tag': 'read'}], lastRead='later', version=25)
        self.catalog['attachments'][1]['data'].update(filename='New readable filename.pdf', title='Custom title', version=26)
        _, findings = self.policy()
        self.assertTrue(findings['repeated_pdf_content']['acknowledgement']['acknowledged'])

    def test_tags_read_state_collections_and_display_labels_do_not_reopen(self):
        self.acknowledge()
        self.catalog['parents']['PARENT23'].update(tags=[{'tag': 'read'}], lastRead='later',
                                                  collections=['COLL2345'], version=900)
        self.catalog['attachments'][0]['data'].update(title='Custom paper label', filename='readable.pdf',
                                                     lastRead='later', tags=[{'tag': 'reading'}], version=901)
        issues, findings = self.policy()
        self.assertEqual(issues, [])
        self.assertTrue(findings['possible_duplicate_references']['acknowledgement']['acknowledged'])

    def test_changed_citation_or_attachment_content_reopens_without_changing_keys(self):
        for change in ('date', 'edition', 'title', 'content', 'relations'):
            with self.subTest(change=change):
                before = copy.deepcopy(self.catalog)
                original_bytes = self.path.read_bytes()
                self.acknowledge()
                if change == 'content':
                    self.path.write_bytes(b'a replaced publication')
                elif change == 'relations':
                    self.catalog['parents']['OTHER234']['relations'] = {'dc:relation': ['http://zotero.org/users/local/items/LINK2345']}
                else:
                    self.catalog['parents']['OTHER234'][change] = 'Corrected ' + change
                issues, findings = self.policy()
                self.assertIn('Another Zotero reference may describe the same work; check the references or versions.', issues)
                self.assertFalse(findings['possible_duplicate_references']['acknowledgement']['acknowledged'])
                self.catalog = before
                self.path.write_bytes(original_bytes)

    def test_new_duplicate_reference_or_changed_attachment_ownership_reopens(self):
        self.acknowledge()
        self.parent('THIRD234')
        issues, findings = self.policy()
        self.assertFalse(findings['possible_duplicate_references']['acknowledgement']['acknowledged'])
        self.assertTrue(issues)
        self.catalog['parents'].pop('THIRD234')
        self.catalog['attachments'][1]['data']['parentItem'] = 'PARENT23'
        issues, findings = self.policy()
        self.assertFalse(findings['possible_duplicate_references']['acknowledgement']['acknowledged'])

    def test_repeated_content_original_baseline_context_reopens_when_current_bytes_change(self):
        second = Path(self.catalog['attachments'][1]['path'])
        second.write_bytes(self.path.read_bytes())
        self.documents = {'tablet-copy': {'view_key': 'COPY2345', 'baseline': self.row['digest']}}
        self.acknowledge('repeated_pdf_content')
        second.write_bytes(b'new annotation history')
        issues, findings = self.policy()
        finding = findings['repeated_pdf_content']
        self.assertIn('Identical content belongs to several Zotero attachments.', issues)
        self.assertFalse(finding['acknowledgement']['acknowledged'])
        self.assertEqual({value['key']: value['match_basis'] for value in finding['attachments']},
                         {'COPY2345': 'original_baseline', 'SOURCE23': 'current_bytes'})

    def test_revoke_changes_only_the_scoped_arrangement_record(self):
        self.acknowledge()
        row, _ = self.acknowledge(keep_separate=False)
        self.assertEqual(self.review.row(row['id']), row)
        self.assertEqual(self.review.acknowledgement_tokens(), set())
        self.assertTrue(self.policy()[0])

    def test_stale_row_digest_review_token_finding_token_and_applying_are_rejected(self):
        for changed in ('digest', 'review_token', 'finding_token', 'applying'):
            with self.subTest(changed=changed):
                row = self.save()
                finding = row['payload']['findings'][0]
                digest, review_token, token = row['digest'], row['payload']['review_token'], finding['acknowledgement']['token']
                if changed == 'digest':
                    digest = 'new-content'
                elif changed == 'review_token':
                    review_token = 'old-review'
                elif changed == 'finding_token':
                    token = 'old-evidence'
                else:
                    self.review.begin(row['id'], row['digest'], {'collection': 'HOME2345'})
                before = self.review.row(row['id'])
                with self.assertRaises(PilotError):
                    self.review.acknowledge_finding(row['id'], digest, finding['code'], token,
                                                    expected_review_token=review_token)
                self.assertEqual(self.review.row(row['id']), before)
                self.assertEqual(self.review.acknowledgement_tokens(), set())

    def test_sync_file_and_role_conditions_cannot_be_acknowledged(self):
        row = self.save()
        for code in ('mapping_conflict', 'local_file_unavailable', 'multiple_pdf_attachments',
                     'multiple_identical_counterparts', 'supporting_information_parent'):
            with self.subTest(code=code), self.assertRaises(PilotError):
                self.review.acknowledge_finding(row['id'], row['digest'], code, 'any-token',
                                                expected_review_token=row['payload']['review_token'])
        self.assertEqual(self.review.acknowledgement_tokens(), set())


if __name__ == '__main__':
    unittest.main()
