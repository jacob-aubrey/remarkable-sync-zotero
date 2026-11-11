"""Upgrade durable issue-only Review queues without syncing or losing consent."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from note_organizer.zotero_pilot import AuthorizationRequired, PilotError
from note_organizer.zotero_review import fingerprint
from note_organizer.zotero_worker import (REVIEW_GUIDANCE_VERSION, Worker,
    citation_signature, file_sha, review_guidance_ready)
from tests.test_cloud_outbox import pdf_bytes
from tests.test_zotero_review_recheck import ReadOnlyNative


class ReviewUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / 'helper'
        self.native = ReadOnlyNative()
        self.native.catalog['parents']['PARENT23'] = {
            'key': 'PARENT23', 'itemType': 'journalArticle', 'title': 'A targeted nanoparticle imaging publication',
            'DOI': '10.1000/synthetic', 'date': '2024', 'publicationTitle': 'Advanced Science',
            'creators': [{'firstName': 'Nivetha', 'lastName': 'Gunaseelan'}], 'collections': ['ROOT2345']}
        self.path = self.root / 'Readable original.pdf'
        self.path.write_bytes(pdf_bytes())
        self.native.catalog['attachments'].append({'path': str(self.path), 'data': {
            'key': 'SOURCE23', 'itemType': 'attachment', 'parentItem': 'PARENT23', 'title': 'Full Text',
            'filename': self.path.name, 'contentType': 'application/pdf', 'linkMode': 'imported_file'}})
        self.worker = self.new_worker()
        self.worker.config.update(zotero_root='ROOT2345', tablet_root='tablet-root', sync_paused=True)
        self.worker.save()
        self.worker.state.write('folders', {'ROOT2345': {'cloud': 'tablet-root', 'name': 'Literature'}})

    def new_worker(self):
        return Worker(self.directory, zotero_factory=lambda: self.native,
            cloud_factory=lambda: self.fail('A Review upgrade constructed a tablet cloud client'))

    def legacy(self, *, kind='zotero', identity='SOURCE23', status='pending', missing_path=False, **updates):
        # Deliberately insert an old on-disk SQLite row directly. No current
        # proposal/policy code or UI mount is allowed to prepare its payload.
        payload = {'source': kind, 'parent': 'PARENT23', 'path': str(self.path),
            'title': 'Older local title', 'filename': 'Gunaseelan_et_al_UnknownJournal_UnknownYear.pdf',
            'document_type': 'journal_article', 'details': {'primary_creator': 'Gunaseelan',
                'document_type': 'journal_article', 'year': 'Unknown', 'journal': 'Unknown'},
            'issues': ['Another Zotero reference may describe the same work; check the references or versions.'],
            'routine': False, 'review_token': 'saved-before-structured-guidance',
            'proposal_revision': 2, 'metadata_signature': citation_signature(self.native.catalog['parents']['PARENT23']),
            'source_collections': ['ROOT2345']}
        if kind == 'cloud':
            payload.update(cloud=identity, cloud_hash='retained-observed-tablet-revision', tablet_parent='tablet-root')
            payload['parent'] = ''
        if missing_path:
            payload.pop('path')
        payload.update(updates)
        key = fingerprint([kind, identity])[:24]
        approval = {'saved_filing_choice': 'preserve this exact history'}
        with self.worker.review.connect() as db:
            db.execute('INSERT INTO review_queue(id,kind,identity,digest,payload,status,approval) VALUES(?,?,?,?,?,?,?)',
                (key, kind, identity, file_sha(self.path), json.dumps(payload), status, json.dumps(approval)))
        return self.worker.review.row(key)

    def test_targeted_case_recheck_leaves_other_pending_payload_unchanged(self):
        first = self.legacy()
        other = self.legacy(identity='OTHER234', kind='cloud')
        before = self.worker.review.row(other['id'])
        result = self.worker.recheck_review(identities=[first['id']])
        self.assertEqual(result['review_recheck']['checked_ids'], [first['id']])
        self.assertEqual(self.worker.review.row(other['id']), before)
        self.assertTrue(review_guidance_ready(self.worker.review.row(first['id'])['payload']))
        with self.assertRaises(PilotError):
            self.worker.recheck_review(identities=[first['id'], first['id']])

    def test_status_reports_durable_old_queue_without_preparing_or_mutating_it(self):
        row = self.legacy()
        self.worker = self.new_worker()  # Real process restart reads the database.
        before = self.worker.review.row(row['id'])
        result = self.worker.status()
        self.assertEqual(result['review_guidance'], {'version': REVIEW_GUIDANCE_VERSION,
            'needs_upgrade': [row['id']], 'ready': 0, 'repair': 0})
        self.assertEqual(self.worker.review.row(row['id']), before)
        self.assertEqual(self.native.calls, [])
        self.assertNotIn('findings', result['reviews'][0]['payload'])

    def test_upgrade_reextracts_old_unknown_names_even_with_current_metadata_signature(self):
        self.native.catalog['parents']['OTHER234'] = {
            **copy.deepcopy(self.native.catalog['parents']['PARENT23']), 'key': 'OTHER234'}
        row = self.legacy()
        self.worker = self.new_worker()
        bridge = self.worker.state.path.read_bytes()
        original = self.path.read_bytes()
        config = copy.deepcopy(self.worker.config)
        with patch.object(self.worker, '_proposal', wraps=self.worker._proposal) as proposal, \
             patch.object(self.worker, '_catalog', side_effect=AssertionError('Source discovery')), \
             patch.object(self.worker, 'cleanup_intake', side_effect=AssertionError('Intake cleanup')), \
             patch('note_organizer.zotero_worker.Bridge', side_effect=AssertionError('Bridge sync')):
            result = self.worker.dispatch('recheck_review', {})
        current = self.worker.review.row(row['id'])
        self.assertEqual(proposal.call_count, 1, 'A legacy proposal needs one current regeneration, even at the same digest/signature')
        self.assertTrue(review_guidance_ready(current['payload']))
        self.assertEqual(current['payload']['guidance_state'], 'ready')
        self.assertNotIn('Unknown', current['payload']['filename'])
        self.assertEqual(current['payload']['details']['year'], '2024')
        self.assertEqual(current['payload']['details']['journal'], 'Advanced Science')
        finding = next(f for f in current['payload']['findings'] if f['code'] == 'possible_duplicate_references')
        self.assertEqual({ref['key'] for ref in finding['references']}, {'PARENT23', 'OTHER234'})
        self.assertTrue(finding['acknowledgement']['eligible'])
        self.assertEqual(current['status'], 'pending')
        self.assertEqual(current['digest'], row['digest'])
        self.assertEqual(current['approval'], row['approval'])
        self.assertNotEqual(current['payload']['review_token'], row['payload']['review_token'])
        self.assertEqual(result['review_guidance']['needs_upgrade'], [])
        self.assertEqual(self.worker.state.path.read_bytes(), bridge)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.worker.config, config)

    def test_zero_findings_marker_survives_restart_and_repeated_checks_are_stable(self):
        row = self.legacy()
        self.worker.recheck_review()
        current = self.worker.review.row(row['id'])
        self.assertEqual(current['payload']['findings'], [])
        self.assertTrue(current['payload']['routine'])
        self.worker = self.new_worker()
        self.assertEqual(self.worker.status()['review_guidance']['needs_upgrade'], [])
        with patch.object(self.worker, '_proposal', side_effect=AssertionError('An unchanged ready proposal was regenerated')):
            result = self.worker.recheck_review()
        self.assertEqual(self.worker.review.row(row['id']), current)
        self.assertEqual(result['review_recheck']['changed_ids'], [])

    def test_legacy_missing_path_is_recovered_from_exact_native_attachment_key(self):
        row = self.legacy(missing_path=True, details=None)
        self.worker = self.new_worker()
        self.worker.recheck_review()
        current = self.worker.review.row(row['id'])
        self.assertEqual(current['payload']['path'], str(self.path))
        self.assertEqual(current['payload']['attachment'], 'SOURCE23')
        self.assertTrue(review_guidance_ready(current['payload']))
        self.assertEqual(current['payload']['guidance_state'], 'ready')
        self.assertNotIn('local_recheck_error', current['payload'])

    def test_missing_legacy_native_source_becomes_current_repair_with_saved_navigation(self):
        row = self.legacy(missing_path=True, details=None)
        self.native.catalog['attachments'].clear()
        self.worker = self.new_worker()
        self.worker.recheck_review()
        current = self.worker.review.row(row['id'])
        self.assertEqual(current['status'], 'pending')
        self.assertEqual(current['digest'], row['digest'])
        self.assertEqual(current['approval'], row['approval'])
        self.assertTrue(review_guidance_ready(current['payload']))
        self.assertEqual(current['payload']['guidance_state'], 'repair')
        repair = next(f for f in current['payload']['findings'] if f['code'] == 'local_review_unavailable')
        self.assertEqual([ref['key'] for ref in repair['references']], ['PARENT23'])
        self.assertEqual([attachment['key'] for attachment in repair['attachments']], ['SOURCE23'])
        self.assertFalse(current['payload']['routine'])
        self.worker = self.new_worker()
        self.assertEqual(self.worker.status()['review_guidance'], {
            'version': REVIEW_GUIDANCE_VERSION, 'needs_upgrade': [], 'ready': 0, 'repair': 1})

    def test_missing_cached_tablet_path_does_not_abort_healthy_native_upgrade(self):
        good = self.legacy()
        bad = self.legacy(kind='cloud', identity='synthetic-tablet-document', missing_path=True, details=None)
        self.worker = self.new_worker()
        result = self.worker.recheck_review()
        healthy = self.worker.review.row(good['id'])
        missing = self.worker.review.row(bad['id'])
        self.assertEqual(result['review_recheck']['checked'], 2)
        self.assertEqual(result['review_guidance']['needs_upgrade'], [])
        self.assertEqual(healthy['payload']['guidance_state'], 'ready')
        self.assertEqual(missing['payload']['guidance_state'], 'repair')
        self.assertIn('no saved source path', missing['payload']['local_recheck_error'])
        repair = next(f for f in missing['payload']['findings'] if f['code'] == 'local_review_unavailable')
        self.assertEqual(repair['attachments'], [], 'A tablet UUID is never a native Zotero attachment key')
        self.assertEqual(missing['approval'], bad['approval'])

    def test_restored_legacy_source_gets_current_proposal_after_saved_repair(self):
        row = self.legacy()
        original = self.path.read_bytes()
        self.path.unlink()
        self.worker.recheck_review()
        self.assertEqual(self.worker.review.row(row['id'])['payload']['guidance_state'], 'repair')
        self.path.write_bytes(original)
        self.worker = self.new_worker()
        self.worker.recheck_review()
        current = self.worker.review.row(row['id'])
        self.assertEqual(current['payload']['guidance_state'], 'ready')
        self.assertNotIn('Unknown', current['payload']['filename'])
        self.assertNotIn('local_recheck_error', current['payload'])
        self.assertEqual(current['approval'], row['approval'])

    def test_permission_failure_keeps_old_evidence_unmarked_and_retryable(self):
        row = self.legacy()
        self.worker = self.new_worker()
        before = self.worker.review.row(row['id'])
        with patch.object(self.native, 'review_action', side_effect=AuthorizationRequired('Connect to Zotero in Setup.')):
            with self.assertRaises(AuthorizationRequired):
                self.worker.recheck_review()
        self.assertEqual(self.worker.review.row(row['id']), before)
        self.assertEqual(self.worker.status()['review_guidance']['needs_upgrade'], [row['id']])
        self.assertEqual(self.native.closes, 1)

    def test_applying_approved_ignored_and_mapping_snapshots_are_not_migrated(self):
        rows = [self.legacy(status='applying'),
                self.legacy(kind='inbox', identity='approved-source', status='approved'),
                self.legacy(kind='cloud', identity='ignored-tablet', status='ignored'),
                self.legacy(kind='conflict', identity='mapping-conflict', type='paper', zotero='PARENT23')]
        self.worker = self.new_worker()
        before = {row['id']: self.worker.review.row(row['id']) for row in rows}
        self.assertEqual(self.worker.status()['review_guidance']['needs_upgrade'], [])
        self.worker.recheck_review()
        self.assertEqual({row['id']: self.worker.review.row(row['id']) for row in rows}, before)
        self.assertEqual(self.native.calls, [])

    def test_legacy_inbox_null_details_is_safe_to_upgrade_without_native_import(self):
        row = self.legacy(kind='inbox', identity='saved-intake-file', details=None, parent='')
        self.worker = self.new_worker()
        result = self.worker.recheck_review()
        current = self.worker.review.row(row['id'])
        self.assertTrue(review_guidance_ready(current['payload']))
        self.assertIsInstance(current['payload']['details'], dict)
        self.assertEqual(current['status'], 'pending')
        self.assertEqual(current['approval'], row['approval'])
        self.assertEqual(result['review_guidance']['needs_upgrade'], [])


if __name__ == '__main__':
    unittest.main()
