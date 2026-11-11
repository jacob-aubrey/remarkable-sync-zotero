"""A Review recheck reads native evidence and never performs a disguised sync."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
import pymupdf
from unittest.mock import patch

from note_organizer.zotero_pilot import PilotError, TransportInterrupted
from note_organizer.zotero_worker import Worker, file_sha
from tests.test_cloud_outbox import pdf_bytes


def changed_pdf(text):
    with pymupdf.open(stream=pdf_bytes(), filetype='pdf') as document:
        document[0].insert_text((72, 140), text)
        return document.tobytes()


class ReadOnlyNative:
    server_id = 'test-native-library'

    def __init__(self):
        self.catalog = {'attachments': [], 'parents': {}}
        self.removed_records = {}
        self.collection_rows = [{'key': 'ROOT2345', 'name': 'Literature', 'parentCollection': False}]
        self.calls = []
        self.closes = 0

    def review_action(self, action, data=None):
        self.calls.append(action)
        if action != 'catalog':
            raise AssertionError('A recheck called a native mutation: ' + action)
        return copy.deepcopy(self.catalog)

    def collections(self):
        self.calls.append('collections')
        return copy.deepcopy(self.collection_rows)

    def close(self):
        self.closes += 1

    def get(self, kind, key):
        if kind != 'item':
            raise AssertionError(kind)
        if key in self.catalog['parents']:
            return copy.deepcopy(self.catalog['parents'][key])
        return next((copy.deepcopy(entry['data']) for entry in self.catalog['attachments'] if entry['data']['key'] == key), copy.deepcopy(self.removed_records.get(key)))

    def file_path(self, key):
        return Path(next(entry['path'] for entry in self.catalog['attachments'] if entry['data']['key'] == key))

    def authorize(self):
        raise AssertionError('A recheck requested write authorization')


class ReviewRecheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.native = ReadOnlyNative()
        self.worker = Worker(self.root / 'helper', zotero_factory=lambda: self.native,
                             cloud_factory=lambda: self.fail('A local recheck constructed a cloud client'))
        self.worker.config.update(zotero_root='ROOT2345', tablet_root='tablet-root', sync_paused=True)
        self.worker.save()
        self.worker.state.write('folders', {'ROOT2345': {'cloud': 'tablet-root', 'name': 'Literature'}})

    def paper(self, key='SOURCE23', parent='PARENT23', *, kind='zotero'):
        path = self.root / (key + '.pdf')
        path.write_bytes(pdf_bytes())
        self.native.catalog['parents'].setdefault(parent, {
            'key': parent, 'itemType': 'journalArticle', 'title': 'A publication on targeted nanoparticle imaging',
            'publicationTitle': 'Example Journal', 'date': '2023', 'collections': ['ROOT2345'],
            'creators': [{'firstName': 'Nivetha', 'lastName': 'Gunaseelan'}]})
        self.native.catalog['attachments'].append({'path': str(path), 'data': {
            'key': key, 'itemType': 'attachment', 'parentItem': parent, 'title': 'Full Text',
            'filename': path.name, 'contentType': 'application/pdf', 'linkMode': 'imported_file'}})
        if kind == 'zotero':
            metadata = self.native.catalog['parents'][parent]
            with patch.object(self.worker.review, 'observe_ready', return_value=True):
                self.worker._offer(kind, key, path, metadata=metadata, attachment=key)
        else:
            payload = {**self.worker._proposal(path), 'path': str(path), 'source': kind,
                       'proposal_revision': 2, 'cloud_hash': 'retained-tablet-state', 'tablet_parent': 'tablet-root'}
            self.worker.review.offer(kind, key, file_sha(path), payload)
        self.worker.refresh_review_policy(self.native, self.native.catalog)
        row = next(row for row in self.worker.review.rows() if row['identity'] == key)
        self.native.calls.clear()
        return row, path

    def test_rpc_reads_local_evidence_without_sync_cloud_cleanup_or_library_writes(self):
        row, path = self.paper()
        config = copy.deepcopy(self.worker.config)
        bridge = self.worker.state.path.read_bytes()
        original = path.read_bytes()
        last = copy.deepcopy(self.worker.last)
        with patch.object(self.worker, '_catalog', side_effect=AssertionError('catalog discovery')) as discovery, \
             patch.object(self.worker, 'cleanup_intake', side_effect=AssertionError('cleanup')) as cleanup, \
             patch('note_organizer.zotero_worker.Bridge', side_effect=AssertionError('bridge')) as bridge_class:
            result = self.worker.dispatch('recheck_review', {})
        self.assertEqual(self.native.calls, ['catalog', 'collections'])
        self.assertEqual(self.native.closes, 1)
        self.assertEqual(self.worker.config, config)
        self.assertEqual(self.worker.last, last)
        self.assertEqual(self.worker.state.path.read_bytes(), bridge)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(result['review_recheck']['checked_ids'], [row['id']])
        self.assertFalse(result['review_recheck']['cloud_checked'])
        discovery.assert_not_called(); cleanup.assert_not_called(); bridge_class.assert_not_called()

    def test_native_metadata_edits_refresh_fields_and_token_without_renaming_file(self):
        row, path = self.paper()
        self.native.catalog['parents']['PARENT23'].update(title='Corrected native title', date='2024', publicationTitle='Advanced Science')
        self.worker.recheck_review(row['id'])
        current = self.worker.review.row(row['id'])
        self.assertEqual(current['digest'], row['digest'])
        self.assertNotEqual(current['payload']['review_token'], row['payload']['review_token'])
        self.assertEqual(current['payload']['title'], 'Corrected native title')
        self.assertEqual(current['payload']['details']['journal'], 'Advanced Science')
        self.assertEqual(current['payload']['details']['year'], '2024')
        self.assertTrue(path.exists())
        self.assertEqual(path.name, 'SOURCE23.pdf')

    def test_parent_change_is_read_by_attachment_key_and_does_not_rebind_bridge(self):
        row, _ = self.paper()
        parent = copy.deepcopy(self.native.catalog['parents']['PARENT23'])
        parent.update(key='OTHER234', collections=[])
        self.native.catalog['parents']['OTHER234'] = parent
        self.native.catalog['attachments'][0]['data']['parentItem'] = 'OTHER234'
        before = self.worker.state.path.read_bytes()
        self.worker.recheck_review()
        current = self.worker.review.row(row['id'])
        self.assertEqual(current['payload']['parent'], 'OTHER234')
        self.assertEqual(current['payload']['source_collections'], [])
        self.assertNotEqual(current['payload']['review_token'], row['payload']['review_token'])
        self.assertEqual(before, self.worker.state.path.read_bytes())

    def test_native_file_rename_refreshes_saved_path_by_key(self):
        row, path = self.paper()
        renamed = path.with_name('Readable renamed paper.pdf')
        path.rename(renamed)
        self.native.catalog['attachments'][0]['path'] = str(renamed)
        self.native.catalog['attachments'][0]['data']['filename'] = renamed.name
        self.worker.recheck_review()
        current = self.worker.review.row(row['id'])
        self.assertEqual(current['payload']['path'], str(renamed))
        self.assertEqual(current['digest'], row['digest'])
        self.assertNotIn('local_recheck_error', current['payload'])

    def test_removed_attachment_does_not_use_leftover_pdf_or_create_replacement(self):
        row, path = self.paper()
        self.native.catalog['attachments'].clear()
        self.worker.recheck_review()
        current = self.worker.review.row(row['id'])
        self.assertTrue(path.exists())
        self.assertIn('no longer available', current['payload']['local_recheck_error'])
        self.assertFalse(current['payload']['routine'])
        self.assertIn('local_review_unavailable', [f['code'] for f in current['payload']['findings']])
        self.assert_approval_blocked(current)

    def assert_approval_blocked(self, row):
        with patch.object(self.worker, 'zotero', side_effect=AssertionError('Approval tried to open native write path')):
            with self.assertRaises(PilotError):
                self.worker.approve(row['id'], row['digest'], row['payload']['filename'], row['payload']['title'],
                                    row['payload']['document_type'], 'ROOT2345', review_token=row['payload']['review_token'])

    def deleted_source(self):
        row, path = self.paper()
        entry = self.native.catalog['attachments'].pop()
        self.native.removed_records[row['identity']] = {**entry['data'], 'deleted': True}
        return row, path, entry

    def test_background_policy_refresh_cannot_hide_deleted_source_migration(self):
        from note_organizer.zotero_worker import REVIEW_GUIDANCE_VERSION, review_guidance_ready
        row, _, _ = self.deleted_source()
        self.worker.refresh_review_policy(self.native, self.native.catalog)
        refreshed = self.worker.review.row(row['id'])
        self.assertEqual(refreshed['status'], 'pending')
        self.assertEqual(refreshed['payload']['guidance_version'], REVIEW_GUIDANCE_VERSION - 1)
        self.assertFalse(review_guidance_ready(refreshed['payload']))
        self.assertEqual(self.worker.status()['review_guidance']['needs_upgrade'], [row['id']])
        result = self.worker.recheck_review(identity=row['id'])
        self.assertEqual(result['review_recheck']['resolved_ids'], [row['id']])
        self.assertEqual(self.worker.review.row(row['id']), {**refreshed, 'status': 'resolved'})

    def test_background_policy_retains_current_guidance_for_classified_unavailable_source(self):
        from note_organizer.zotero_worker import REVIEW_GUIDANCE_VERSION, review_guidance_ready
        row, _ = self.paper(); self.native.catalog['attachments'].clear()
        self.worker.recheck_review(identity=row['id'])
        self.worker.refresh_review_policy(self.native, self.native.catalog)
        current = self.worker.review.row(row['id'])
        self.assertEqual(current['payload']['guidance_version'], REVIEW_GUIDANCE_VERSION)
        self.assertTrue(review_guidance_ready(current['payload']))
        self.assertEqual(self.worker.status()['review_guidance']['needs_upgrade'], [])
        self.assertEqual(current['payload']['source_availability'], 'missing')

    def test_confirmed_deleted_spare_retires_review_and_preserves_managed_copy_and_history(self):
        row, path, _ = self.deleted_source()
        view = self.root / 'Linked notes.pdf'; view.write_bytes(changed_pdf('Retained tablet notes'))
        self.native.catalog['attachments'].append({'path': str(view), 'data': {
            'key': 'VIEW2345', 'itemType': 'attachment', 'parentItem': 'PARENT23',
            'title': 'Main text (reMarkable notes)', 'filename': view.name, 'contentType': 'application/pdf'}})
        documents = {'tablet': {'parent': 'PARENT23', 'view_key': 'VIEW2345', 'origin': 'cloud', 'baseline': row['digest']}}
        self.worker.state.write('documents', documents)
        before_bridge = self.worker.state.path.read_bytes()
        before_view = view.read_bytes()
        result = self.worker.recheck_review(identity=row['id'])
        current = self.worker.review.row(row['id'])
        self.assertEqual(current, {**row, 'status': 'resolved'}, 'Only pending status changes; saved evidence and approval history remain intact')
        self.assertEqual(result['reviews'], [])
        self.assertEqual(result['review_recheck']['checked_ids'], [row['id']])
        self.assertEqual(result['review_recheck']['resolved_ids'], [row['id']])
        self.assertEqual(result['review_recheck']['changed_ids'], [row['id']])
        self.assertEqual(result['review_recheck']['unavailable_ids'], [])
        self.assertEqual(self.worker.state.path.read_bytes(), before_bridge)
        self.assertEqual(view.read_bytes(), before_view)
        self.assertTrue(path.exists(), 'Retirement never deletes retained files')
        self.assertEqual(self.native.removed_records[row['identity']]['deleted'], True)

    def test_retired_source_returns_after_native_restoration(self):
        row, _, entry = self.deleted_source()
        self.worker.recheck_review(identity=row['id'])
        self.assertEqual(self.worker.review.row(row['id'])['status'], 'resolved')
        self.native.catalog['attachments'].append(entry)
        stale = {**row['payload'], 'source_removed': True, 'source_availability': 'trashed', 'local_recheck_error': 'Old unavailable source'}
        self.worker.review.offer('zotero', row['identity'], row['digest'], stale)
        self.worker.recheck_review(identity=row['id'])
        restored = self.worker.review.row(row['id'])
        self.assertEqual(restored['status'], 'pending')
        self.assertNotIn('source_removed', restored['payload'])
        self.assertNotIn('source_availability', restored['payload'])
        self.assertNotIn('local_recheck_error', restored['payload'])

    def test_merely_absent_source_is_marked_historical_without_retirement(self):
        row, _ = self.paper(); self.native.catalog['attachments'].clear()
        result = self.worker.recheck_review(identity=row['id'])
        current = self.worker.review.row(row['id'])
        self.assertEqual(current['status'], 'pending')
        self.assertEqual(result['review_recheck']['resolved_ids'], [])
        repair = next(f for f in current['payload']['findings'] if f['code'] == 'local_review_unavailable')
        self.assertEqual(repair['attachments'][0]['availability'], 'missing')
        self.assertEqual(current['payload']['source_availability'], 'missing')

    def test_deleted_source_with_active_link_is_not_retired_and_is_marked_trashed(self):
        row, _, _ = self.deleted_source()
        self.worker.state.write('documents', {'tablet': {'source_key': row['identity']}})
        result = self.worker.recheck_review(identity=row['id'])
        current = self.worker.review.row(row['id'])
        self.assertEqual(current['status'], 'pending')
        self.assertEqual(result['review_recheck']['resolved_ids'], [])
        repair = next(f for f in current['payload']['findings'] if f['code'] == 'local_review_unavailable')
        self.assertEqual(repair['attachments'][0]['availability'], 'trashed')
        self.assertIn('still owns a tablet connection', repair['explanation'])

    def test_source_used_by_another_interrupted_filing_is_not_retired(self):
        row, _, _ = self.deleted_source()
        recovery = self.worker.review.offer('cloud', 'recovering-tablet', row['digest'], {'path': row['payload']['path']})
        self.worker.review.begin(recovery, row['digest'], {'source': row['identity'], 'saved_step': 'Retain publication'})
        before = self.worker.review.row(recovery)
        result = self.worker.recheck_review(identity=row['id'])
        self.assertEqual(self.worker.review.row(row['id'])['status'], 'pending')
        self.assertEqual(self.worker.review.row(recovery), before)
        self.assertEqual(result['review_recheck']['resolved_ids'], [])

    def test_only_boolean_native_deletion_proof_can_retire_a_review(self):
        row, _, entry = self.deleted_source()
        self.native.removed_records[row['identity']] = {**entry['data'], 'deleted': 'false'}
        result = self.worker.recheck_review(identity=row['id'])
        self.assertEqual(self.worker.review.row(row['id'])['status'], 'pending')
        self.assertEqual(result['review_recheck']['resolved_ids'], [])

    def test_busy_bridge_state_refuses_review_retirement(self):
        row, _, _ = self.deleted_source()
        with self.worker.state.lock():
            with self.assertRaisesRegex(PilotError, 'already in use'):
                self.worker.recheck_review(identity=row['id'])
        self.assertEqual(self.worker.review.row(row['id']), row)

    def test_deleted_source_owned_by_archived_connection_is_not_retired(self):
        from note_organizer.zotero_bridge import BridgeState
        row, _, _ = self.deleted_source()
        identity = 'a' * 32
        self.worker.config.update(server_id=self.native.server_id,
            connection_history=[{'connection_id': identity, 'server_id': self.native.server_id}])
        archived = BridgeState(self.worker.directory / 'connections' / identity / 'bridge')
        archived.write('documents', {'older-tablet': {'view_key': row['identity']}})
        before = archived.path.read_bytes()
        result = self.worker.recheck_review(identity=row['id'])
        self.assertEqual(self.worker.review.row(row['id'])['status'], 'pending')
        self.assertEqual(result['review_recheck']['resolved_ids'], [])
        self.assertEqual(archived.path.read_bytes(), before)

    def test_deleted_applying_source_keeps_its_exact_recovery_journal(self):
        row, _, _ = self.deleted_source()
        self.worker.review.begin(row['id'], row['digest'], {'source': row['identity'], 'saved_step': 'Retain journal'})
        before = self.worker.review.row(row['id'])
        with patch.object(self.native, 'get', side_effect=AssertionError('Applying source was inspected')):
            result = self.worker.recheck_review()
        self.assertEqual(self.worker.review.row(row['id']), before)
        self.assertEqual(result['review_recheck']['resolved_ids'], [])
        self.assertEqual(result['review_recheck']['preserved_applying'], 1)

    def test_native_restore_during_recheck_refuses_retirement(self):
        row, _, entry = self.deleted_source()
        with patch.object(self.native, 'get', side_effect=[{**entry['data'], 'deleted': True}, entry['data']]):
            with self.assertRaisesRegex(PilotError, 'connection changed'):
                self.worker.recheck_review(identity=row['id'])
        self.assertEqual(self.worker.review.row(row['id']), row)

    def test_retirement_compare_and_swap_preserves_concurrently_changed_review(self):
        row, _, _ = self.deleted_source()
        newer = {**row['payload'], 'newer_evidence': True}
        with self.worker.review.connect() as db:
            import json
            db.execute('UPDATE review_queue SET payload=? WHERE id=?', (json.dumps(newer), row['id']))
        with self.assertRaisesRegex(PilotError, 'review changed'):
            self.worker._resolve_source_review(row)
        self.assertEqual(self.worker.review.row(row['id'])['payload'], newer)
        self.assertEqual(self.worker.review.row(row['id'])['status'], 'pending')

    def test_deleted_unlinked_source_can_close_without_approval_and_restoration_reopens(self):
        row, _ = self.paper()
        entry = self.native.catalog['attachments'].pop()
        self.worker.recheck_review(identity=row['id'])
        current = self.worker.review.row(row['id'])
        self.assertTrue(current['payload']['source_removed'])
        self.worker.close_removed_source(row['id'], current['digest'], current['payload']['review_token'])
        self.assertEqual(self.worker.review.row(row['id'])['status'], 'resolved')
        self.assertEqual(self.worker.review.row(row['id'])['approval'], {})
        self.native.catalog['attachments'].append(entry)
        self.worker.review.offer('zotero', row['identity'], row['digest'], row['payload'])
        self.assertEqual(self.worker.review.row(row['id'])['status'], 'pending')

    def test_deleted_source_close_refuses_live_source_or_tablet_ownership(self):
        row, _ = self.paper()
        entry = self.native.catalog['attachments'].pop()
        self.worker.recheck_review(identity=row['id'])
        current = self.worker.review.row(row['id'])
        args = (row['id'], current['digest'], current['payload']['review_token'])
        self.native.catalog['attachments'].append(entry)
        with self.assertRaisesRegex(PilotError, 'present in Zotero'):
            self.worker.close_removed_source(*args)
        self.native.catalog['attachments'].clear()
        self.worker.state.write('documents', {'tablet': {'view_key': row['identity']}})
        with self.assertRaisesRegex(PilotError, 'owns a tablet connection'):
            self.worker.close_removed_source(*args)
        self.assertEqual(self.worker.review.row(row['id'])['status'], 'pending')

    def test_unavailable_parent_is_explicit_and_never_recreated(self):
        row, _ = self.paper()
        self.native.catalog['parents'].clear()
        self.worker.recheck_review()
        current = self.worker.review.row(row['id'])
        self.assertIn('parent reference is unavailable', current['payload']['local_recheck_error'])
        self.assertEqual(current['payload']['parent'], 'PARENT23')
        self.assert_approval_blocked(current)

    def test_missing_file_can_be_restored_without_losing_review_identity(self):
        row, path = self.paper()
        original = path.read_bytes()
        path.unlink()
        self.worker.recheck_review()
        self.assertIn(row['id'], self.worker.review_recheck['unavailable_ids'])
        path.write_bytes(original)
        self.worker.recheck_review()
        current = self.worker.review.row(row['id'])
        self.assertNotIn('local_recheck_error', current['payload'])
        self.assertEqual(current['digest'], row['digest'])
        self.assertEqual(current['status'], 'pending')

    def test_changed_native_pdf_resets_digest_and_keeps_tablet_comparison_stale_until_sync(self):
        row, path = self.paper()
        with self.worker.review.connect() as db:
            db.execute("UPDATE review_queue SET approval=? WHERE id=?", ('{"draft":"old bytes"}', row['id']))
        path.write_bytes(changed_pdf('Changed source PDF'))
        self.worker.recheck_review()
        current = self.worker.review.row(row['id'])
        self.assertNotEqual(current['digest'], row['digest'])
        self.assertEqual(current['approval'], {})
        self.assertTrue(current['payload']['tablet_evidence_stale'])
        self.assertEqual(current['payload']['matches'], [])
        self.assertIn('Sync now', current['payload']['local_recheck_error'])
        self.assert_approval_blocked(current)
        self.worker.recheck_review()
        self.assertTrue(self.worker.review.row(row['id'])['payload']['tablet_evidence_stale'])
        self.assert_approval_blocked(self.worker.review.row(row['id']))

    def test_explicit_recheck_rehashes_same_size_pdf_with_restored_timestamp(self):
        row, path = self.paper()
        before = path.stat()
        original = path.read_bytes()
        modified = original.replace(b'842', b'843', 1)
        self.assertNotEqual(original, modified)
        path.write_bytes(modified)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(path.stat().st_size, before.st_size)
        self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)
        self.worker.recheck_review()
        current = self.worker.review.row(row['id'])
        self.assertEqual(current['digest'], file_sha(path))
        self.assertNotEqual(current['digest'], row['digest'])
        self.assertTrue(current['payload']['tablet_evidence_stale'])

    def test_changed_cached_tablet_pdf_keeps_original_digest_and_requires_repair(self):
        row, path = self.paper(kind='cloud')
        path.write_bytes(changed_pdf('Tampered cached tablet original'))
        self.worker.recheck_review()
        current = self.worker.review.row(row['id'])
        self.assertEqual(current['digest'], row['digest'])
        self.assertIn('changed locally', current['payload']['local_recheck_error'])
        self.assert_approval_blocked(current)

    def test_applying_journal_is_opaque_even_when_native_attachment_disappears(self):
        row, _ = self.paper()
        self.worker.review.begin(row['id'], row['digest'], {'source': 'SOURCE23', 'step': 'interrupted-write'})
        before = self.worker.review.row(row['id'])
        self.native.catalog['attachments'].clear()
        result = self.worker.recheck_review()
        self.assertEqual(self.worker.review.row(row['id']), before)
        self.assertEqual(result['review_recheck']['preserved_applying'], 1)
        self.assertEqual(self.native.calls, [])

    def test_mapping_conflict_snapshot_is_unchanged_and_never_reads_cloud(self):
        identity = self.worker.review.offer('conflict', 'tablet-document', 'conflict-digest', {
            'type': 'paper', 'zotero': 'PARENT23', 'desktop': ['Desktop name', 'ROOT2345'],
            'tablet': ['Tablet name', 'ROOT2345'], 'version': 7})
        before = self.worker.review.row(identity)
        result = self.worker.recheck_review()
        self.assertEqual(self.worker.review.row(identity), before)
        self.assertEqual(result['review_recheck']['skipped_conflicts'], 1)
        self.assertEqual(result['review_recheck']['checked'], 0)
        self.assertEqual(self.native.calls, [])

    def test_single_review_recheck_keeps_other_review_payload_unchanged(self):
        row, _ = self.paper()
        other, _ = self.paper(key='COPY2345', parent='OTHER234')
        before = self.worker.review.row(other['id'])
        self.worker.recheck_review(row['id'])
        self.assertEqual(self.worker.review.row(other['id']), before)
        self.assertEqual(self.worker.review_recheck['checked_ids'], [row['id']])

    def test_native_transport_failure_leaves_saved_reviews_untouched_and_closes_client(self):
        row, _ = self.paper()
        before = self.worker.review.row(row['id'])
        with patch.object(self.native, 'review_action', side_effect=TransportInterrupted('offline')):
            with self.assertRaises(TransportInterrupted):
                self.worker.recheck_review()
        self.assertEqual(self.worker.review.row(row['id']), before)
        self.assertEqual(self.native.closes, 1)

    def test_cancellation_before_local_changes_preserves_queue(self):
        row, _ = self.paper()
        before = self.worker.review.row(row['id'])
        self.worker.stop.set()
        with self.assertRaises(InterruptedError):
            self.worker.recheck_review()
        self.assertEqual(self.worker.review.row(row['id']), before)
        self.assertEqual(self.native.closes, 1)

    def test_repeated_unchanged_checks_keep_the_same_review_token(self):
        row, _ = self.paper()
        self.worker.recheck_review()
        first = self.worker.review.row(row['id'])
        self.worker.recheck_review()
        second = self.worker.review.row(row['id'])
        self.assertEqual(first, second)
        self.assertEqual(self.worker.review_recheck['changed_ids'], [])

    def test_catalog_offer_refreshes_parent_even_when_native_citation_fields_are_identical(self):
        row, path = self.paper()
        other = {**copy.deepcopy(self.native.catalog['parents']['PARENT23']), 'key': 'OTHER234'}
        self.native.catalog['parents']['OTHER234'] = other
        with patch.object(self.worker.review, 'observe_ready', return_value=True):
            self.worker._offer('zotero', 'SOURCE23', path, metadata=other, attachment='SOURCE23')
        self.assertEqual(self.worker.review.row(row['id'])['payload']['parent'], 'OTHER234')

    def test_catalog_offer_refreshes_native_renamed_path_with_unchanged_bytes(self):
        row, path = self.paper()
        renamed = path.with_name('Native rename.pdf')
        path.rename(renamed)
        with patch.object(self.worker.review, 'observe_ready', return_value=True):
            self.worker._offer('zotero', 'SOURCE23', renamed,
                metadata=self.native.catalog['parents']['PARENT23'], attachment='SOURCE23')
        self.assertEqual(self.worker.review.row(row['id'])['payload']['path'], str(renamed))

    def test_approval_rechecks_selected_evidence_and_refuses_a_changed_citation(self):
        row, _ = self.paper()
        self.worker.recheck_review(identity=row['id'])
        row = self.worker.review.row(row['id'])
        self.native.catalog['parents']['PARENT23']['date'] = '2025'
        with self.assertRaisesRegex(PilotError, 'Zotero evidence changed'):
            self.worker.approve(row['id'], row['digest'], row['payload']['filename'], row['payload']['title'],
                row['payload']['document_type'], 'ROOT2345', review_token=row['payload']['review_token'])
        self.assertEqual(self.worker.review.row(row['id'])['status'], 'pending')
        self.assertEqual(self.worker.review_recheck['checked_ids'], [row['id']])

    def test_approve_rejects_same_bytes_attachment_reparented_after_display(self):
        row, _ = self.paper()
        other = {**copy.deepcopy(self.native.catalog['parents']['PARENT23']), 'key': 'OTHER234'}
        self.native.catalog['parents']['OTHER234'] = other
        self.native.catalog['attachments'][0]['data']['parentItem'] = 'OTHER234'
        before = self.worker.review.row(row['id'])
        with patch.object(self.worker.review, 'begin', side_effect=AssertionError('Stale review began applying')):
            with self.assertRaisesRegex(PilotError, 'reference changed'):
                self.worker.approve(row['id'], row['digest'], row['payload']['filename'], row['payload']['title'],
                    row['payload']['document_type'], 'ROOT2345', review_token=row['payload']['review_token'])
        self.assertEqual(self.worker.review.row(row['id']), before)

    def test_approve_rejects_native_file_rename_after_display(self):
        row, path = self.paper()
        renamed = path.with_name('User renamed paper.pdf')
        path.rename(renamed)
        self.native.catalog['attachments'][0]['path'] = str(renamed)
        with patch.object(self.worker.review, 'begin', side_effect=AssertionError('Stale path began applying')):
            with self.assertRaisesRegex(PilotError, 'file name or location changed'):
                self.worker.approve(row['id'], row['digest'], row['payload']['filename'], row['payload']['title'],
                    row['payload']['document_type'], 'ROOT2345', review_token=row['payload']['review_token'])
        self.assertTrue(renamed.exists())
        self.assertEqual(self.worker.review.row(row['id'])['status'], 'pending')

    def test_approve_never_recreates_reference_removed_after_display(self):
        row, _ = self.paper()
        self.native.catalog['parents'].clear()
        with patch.object(self.worker.review, 'begin', side_effect=AssertionError('Missing reference began applying')):
            with self.assertRaisesRegex(PilotError, 'will not be recreated'):
                self.worker.approve(row['id'], row['digest'], row['payload']['filename'], row['payload']['title'],
                    row['payload']['document_type'], 'ROOT2345', review_token=row['payload']['review_token'])
        self.assertEqual(self.worker.review.row(row['id'])['status'], 'pending')

    def test_unknown_or_approved_identity_is_rejected_without_native_access(self):
        with self.assertRaises(PilotError):
            self.worker.recheck_review('no-such-review')
        self.assertEqual(self.native.calls, [])

    def test_acknowledgement_rechecks_evidence_without_filing_or_dismissing_review(self):
        row, _ = self.paper()
        self.native.catalog['parents']['OTHER234'] = {
            **copy.deepcopy(self.native.catalog['parents']['PARENT23']), 'key': 'OTHER234'}
        self.worker.recheck_review()
        row = self.worker.review.row(row['id'])
        finding = next(f for f in row['payload']['findings'] if f['code'] == 'possible_duplicate_references')
        result = self.worker.acknowledge_review_finding(row['id'], row['digest'], finding['code'],
            finding['acknowledgement']['token'], row['payload']['review_token'])
        current = self.worker.review.row(row['id'])
        self.assertEqual(current['status'], 'pending')
        self.assertEqual(current['approval'], {})
        acknowledged = next(f for f in current['payload']['findings'] if f['code'] == finding['code'])
        self.assertTrue(acknowledged['acknowledgement']['acknowledged'])
        self.assertTrue(result['review_recheck']['checked'])

    def test_shared_arrangement_acknowledgement_updates_each_independent_review(self):
        first, _ = self.paper()
        second, _ = self.paper(key='COPY2345', parent='OTHER234')
        # Use the same real PDF bytes, not matching names, to establish overlap.
        first_path = Path(self.worker.review.row(first['id'])['payload']['path'])
        second_path = Path(self.worker.review.row(second['id'])['payload']['path'])
        second_path.write_bytes(first_path.read_bytes())
        self.worker.recheck_review()
        first = self.worker.review.row(first['id'])
        finding = next(f for f in first['payload']['findings'] if f['code'] == 'repeated_pdf_content')
        self.worker.acknowledge_review_finding(first['id'], first['digest'], finding['code'],
            finding['acknowledgement']['token'], first['payload']['review_token'])
        for identity in (first['id'], second['id']):
            current = self.worker.review.row(identity)
            repeated = next(f for f in current['payload']['findings'] if f['code'] == 'repeated_pdf_content')
            self.assertTrue(repeated['acknowledgement']['acknowledged'])
            self.assertEqual(current['status'], 'pending')
            self.assertEqual(current['approval'], {})

    def test_acknowledgement_fails_if_native_metadata_changed_since_display(self):
        row, _ = self.paper()
        self.native.catalog['parents']['OTHER234'] = {
            **copy.deepcopy(self.native.catalog['parents']['PARENT23']), 'key': 'OTHER234'}
        self.worker.recheck_review()
        row = self.worker.review.row(row['id'])
        finding = next(f for f in row['payload']['findings'] if f['code'] == 'possible_duplicate_references')
        self.native.catalog['parents']['OTHER234']['date'] = '2025'
        with self.assertRaises(PilotError):
            self.worker.acknowledge_review_finding(row['id'], row['digest'], finding['code'],
                finding['acknowledgement']['token'], row['payload']['review_token'])
        self.assertEqual(self.worker.review.acknowledgement_tokens(), set())
        self.assertEqual(self.worker.review.row(row['id'])['status'], 'pending')


if __name__ == '__main__':
    unittest.main()
