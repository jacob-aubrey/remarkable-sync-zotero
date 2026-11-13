"""Exercise reviewed user actions across real queue/bridge/cloud state."""
import copy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from note_organizer.zotero_worker import Worker, file_sha
from note_organizer.zotero_review import ReviewStore
from note_organizer.zotero_pilot import PilotError
from note_organizer.remarkable_cloud import CloudError
from tests.test_cloud_outbox import MemoryCloud, pdf_bytes
from tests.test_zotero_bridge import BridgeZotero


class ReviewZotero(BridgeZotero):
    def __init__(self, path):
        super().__init__(path)
        self.collection_records = self.collections
        del self.collections
        self.closed = False

    def close(self):
        self.closed = True

    def update_collection(self, record):
        self.collection_records[record['key']] = copy.deepcopy(record)

    def review_action(self, action, data=None):
        data = data or {}
        if action == 'catalog':
            return {'attachments': [{'data': copy.deepcopy(r), 'path': str(self.file_path(r['key']))}
                    for r in self.items.values() if r.get('itemType') == 'attachment' and not r.get('deleted')],
                    'parents': {k: copy.deepcopy(r) for k, r in self.items.items() if r.get('itemType') not in {'attachment', 'note', 'annotation'}}}
        if action == 'rename':
            self.items[data['key']]['filename'] = data['filename']
            return {'key': data['key']}
        if action == 'reparent':
            item = self.items[data['key']]
            if item.get('parentItem', '') not in {data['previous_parent'], data['parent']}:
                raise PilotError('Attachment changed')
            item['parentItem'] = data['parent']
            return {'key': data['key'], 'parent': data['parent']}
        if action == 'inventory':
            return {'inventory': [{'key': r['key']} for r in self.items.values() if r.get('parentItem') == data['parent']]}
        if action == 'inspect_link_merge':
            return {'parent': None, 'attachments': {}}
        if action == 'adopt':
            self.items[data['key']]['note'] = data['marker']
            return {'adopted': True, 'native': 0}
        raise AssertionError(action)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.zot = ReviewZotero(self.root)
        self.cloud = MemoryCloud()
        self.croot = self.cloud.create_folder('Tablet', '', str(uuid.uuid4()))
        self.worker = Worker(self.root / 'helper', zotero_factory=lambda: self.zot, cloud_factory=lambda: self.cloud)
        self.worker.config = {'zotero_root': 'ROOT2345', 'tablet_root': self.croot.id, 'default_folder': self.croot.id, 'ocr': False}

    def test_local_inbox_watch_without_clients(self):
        inbox = self.root / 'ToSort'
        inbox.mkdir()
        self.worker.config = {'inbox': str(inbox), 'automatic': False, 'sync_paused': True}
        path = inbox / 'download.pdf'
        path.write_bytes(pdf_bytes())
        (inbox / 'download.crdownload').write_bytes(b'partial')
        with patch.object(self.worker, '_proposal', return_value={'title': 'Download'}):
            self.assertEqual(self.worker.scan_inbox()['reviews'], [])
            with patch('note_organizer.zotero_review.time.time', return_value=9999999999):
                result = self.worker.scan_inbox()
            self.assertEqual(len(result['reviews']), 1)
            self.assertEqual(result['reviews'][0]['payload']['path'], str(path))
        path.unlink()
        self.assertEqual(self.worker.scan_inbox()['reviews'], [])

    def test_inbox_watch_retries_unavailable_folder_and_paused_dispatch(self):
        inbox = self.root / 'ToSort'
        self.worker.config = {'inbox': str(inbox), 'sync_paused': True}
        self.worker.pause_pending.set()
        result = self.worker.dispatch('scan_inbox', {})
        self.assertEqual(len(result['inbox_issues']), 1)
        inbox.mkdir()
        self.assertEqual(self.worker.dispatch('scan_inbox', {})['inbox_issues'], [])
        self.assertTrue(self.worker.pause_pending.is_set())

    def add_pdf(self):
        self.zot.items['PARENT23'] = {'key': 'PARENT23', 'itemType': 'thesis', 'title': 'Dissertation', 'collections': ['ROOT2345'], 'university': 'Example'}
        self.zot.items['SOURCE23'] = {'key': 'SOURCE23', 'itemType': 'attachment', 'parentItem': 'PARENT23', 'contentType': 'application/pdf', 'title': 'Main PDF', 'linkMode': 'imported_file'}
        path = self.zot.file_path('SOURCE23')
        path.write_bytes(pdf_bytes())
        return path

    def test_missing_citation_keeps_existing_readable_filename_without_guessing_fields(self):
        path = self.root / 'Gunaseelan_et_al_Advanced_Science_2024.pdf'
        details = {'title': 'Targeted K-edge nanoprobes', 'document_type': 'journal_article',
                   'primary_creator': 'Nivetha Gunaseelan', 'creators': ['Nivetha Gunaseelan'],
                   'year': '12/2', 'journal': 'Unknown', 'review_reasons': []}
        with patch('note_organizer.sorting.core_logic.get_basic_document_details', return_value=details):
            proposal = self.worker._proposal(path)
        self.assertEqual(proposal['filename'], path.name)
        self.assertEqual(proposal['details']['journal'], 'Unknown')
        self.assertEqual(proposal['details']['year'], '12/2')

    def test_filing_citation_and_tags_use_explicit_edits_and_keep_existing_data(self):
        self.add_pdf()
        self.zot.items['PARENT23']['tags'] = [{'tag': 'Existing tag', 'type': 1}]
        self.zot.items['PARENT23']['extra'] = 'Existing citation notes'
        with patch.object(self.worker.review, 'observe_ready', return_value=True):
            status = self.worker.scan_inbox(discover=True)
        row = next(row for row in status['reviews'] if row['kind'] == 'zotero')
        edits = {'creators': 'Aubrey, Jane', 'date': '2022-09-03', 'venue': 'Reviewed University', 'doi': 'https://doi.org/10.1000/FILING'}
        filename = self.worker.preview(row['id'], 'book', 'Reviewed title', citation=edits)['filename']
        self.assertIn('Book', filename)
        self.assertIn('2022', filename)
        self.worker.approve(row['id'], row['digest'], filename, 'Reviewed title', 'book', 'ROOT2345',
                            citation=edits, tags=['New tag', 'Existing tag'])
        citation = self.zot.items['PARENT23']
        self.assertEqual(citation['title'], 'Reviewed title')
        self.assertEqual(citation['itemType'], 'thesis')
        self.assertEqual(citation['creators'], [{'creatorType': 'author', 'firstName': 'Jane', 'lastName': 'Aubrey'}])
        self.assertEqual(citation['date'], '2022-09-03')
        self.assertEqual(citation['university'], 'Reviewed University')
        self.assertEqual(citation['DOI'], '10.1000/filing')
        self.assertEqual(citation['extra'], 'Existing citation notes')
        self.assertEqual([tag for tag in citation['tags'] if tag['tag'] in {'Existing tag', 'New tag'}],
                         [{'tag': 'Existing tag', 'type': 1}, {'tag': 'New tag'}])

    def test_filename_preview_does_not_reintroduce_unknown_placeholders(self):
        path = self.add_pdf()
        details = {'title': 'Targeted K-edge nanoprobes', 'document_type': 'journal_article',
                   'primary_creator': 'Nivetha Gunaseelan', 'year': 'Unknown', 'journal': 'Unknown'}
        identity = self.worker.review.offer('zotero', 'SOURCE23', file_sha(path), {
            'path': str(path), 'details': details, 'filename': 'Gunaseelan_et_al_Advanced_Science_2024.pdf'})
        self.assertEqual(self.worker.preview(identity, 'journal_article', details['title'])['filename'],
                         'Gunaseelan_et_al_Advanced_Science_2024.pdf')

    def test_authoritative_Zotero_citation_still_generates_a_complete_name(self):
        path = self.root / 'Readable original.pdf'
        details = {'title': 'Targeted K-edge nanoprobes', 'document_type': 'journal_article',
                   'primary_creator': 'Unknown', 'year': 'Unknown', 'journal': 'Unknown', 'review_reasons': []}
        with patch('note_organizer.sorting.core_logic.get_basic_document_details', return_value=details):
            proposal = self.worker._proposal(path, {'date': '2024-12-02', 'publicationTitle': 'Advanced Science',
                'creators': [{'firstName': 'Nivetha', 'lastName': 'Gunaseelan'}]})
        self.assertNotIn('Unknown', proposal['filename'])
        self.assertIn('2024', proposal['filename'])

    def test_new_import_waits_for_review_and_only_approved_digest_uploads(self):
        path = self.add_pdf()
        with patch.object(self.worker.review, 'observe_ready', return_value=True):
            self.worker.sync()
        self.assertFalse(self.worker.state.read('documents', {}))
        review = self.worker.review.rows()[0]
        self.worker.approve(review['id'], review['digest'], 'Reviewed.pdf', 'Dissertation', 'thesis_dissertation', 'ROOT2345')
        self.worker.sync()
        self.assertEqual(len(self.worker.state.read('documents')), 1)
        self.assertEqual(self.zot.items['PARENT23']['university'], 'Example')
        self.assertEqual(self.zot.items['SOURCE23']['filename'], 'Reviewed.pdf')

    def test_changed_file_invalidates_approval_before_any_write(self):
        path = self.add_pdf()
        with patch.object(self.worker.review, 'observe_ready', return_value=True):
            self.worker.sync()
        review = self.worker.review.rows()[0]
        path.write_bytes(path.read_bytes() + b'changed')
        before = self.zot.writes
        with self.assertRaises(PilotError):
            self.worker.approve(review['id'], review['digest'], 'Reviewed.pdf', 'Dissertation', 'thesis_dissertation', 'ROOT2345')
        self.assertEqual(self.zot.writes, before)
        self.assertFalse(self.worker.state.read('documents', {}))

    def test_new_tablet_document_is_not_imported_until_approved(self):
        item = self.cloud.upload_pdf(pdf_bytes(), 'Tablet paper', self.croot.id, str(uuid.uuid4()), self.croot.id)
        self.worker.sync()
        self.assertFalse(self.zot.items)
        review = self.worker.review.rows()[0]
        self.assertEqual(review['identity'], item.id)
        self.worker.approve(review['id'], review['digest'], 'Tablet reviewed.pdf', 'Tablet reviewed', 'journal_article', 'ROOT2345')
        self.worker.sync()
        self.assertEqual(len(self.worker.state.read('documents')), 1)
        self.assertEqual(len([r for r in self.zot.items.values() if r.get('itemType') == 'attachment']), 1)

    def test_unchanged_pending_tablet_paper_reuses_extraction_and_download_but_refreshes_matches(self):
        data = pdf_bytes()
        item = self.cloud.upload_pdf(data, 'Tablet paper', self.croot.id, str(uuid.uuid4()), self.croot.id)
        with patch.object(self.worker, '_proposal', wraps=self.worker._proposal) as proposal:
            self.worker._catalog(self.zot, self.cloud)
            self.assertEqual(proposal.call_count, 1)
            with patch.object(self.cloud, 'pdf_bundle', side_effect=AssertionError('Unchanged paper downloaded again')):
                self.add_pdf().write_bytes(data)
                with patch.object(self.worker.review, 'observe_ready', return_value=False):
                    self.worker._catalog(self.zot, self.cloud)
            self.assertEqual(proposal.call_count, 1)
        row = next(r for r in self.worker.review.rows() if r['identity'] == item.id)
        self.assertEqual(row['payload']['matches'][0]['key'], 'SOURCE23')

    def test_pending_tablet_rename_and_template_change_refresh_the_proposal(self):
        item = self.cloud.upload_pdf(pdf_bytes(), 'Old name', self.croot.id, str(uuid.uuid4()), self.croot.id)
        with patch.object(self.worker, '_proposal', wraps=self.worker._proposal) as proposal:
            self.worker._catalog(self.zot, self.cloud)
            self.cloud.update_item(item.id, expected_hash=item.hash, name='New name', scope_id=self.croot.id)
            self.worker._catalog(self.zot, self.cloud)
            self.worker.config['templates'] = {'unknown': '{title}'}
            self.worker._catalog(self.zot, self.cloud)
            self.assertEqual(proposal.call_count, 3)
        self.assertEqual(self.worker.review.rows()[0]['payload']['title'], 'New name')

    def test_changed_saved_review_pdf_is_retained_and_blocks_reuse(self):
        self.cloud.upload_pdf(pdf_bytes(), 'Tablet paper', self.croot.id, str(uuid.uuid4()), self.croot.id)
        self.worker._catalog(self.zot, self.cloud)
        path = Path(self.worker.review.rows()[0]['payload']['path'])
        path.write_bytes(path.read_bytes() + b'changed')
        self.worker._catalog(self.zot, self.cloud)
        self.assertIn('saved review PDF changed', self.worker.catalog_issues[0]['reason'])
        self.assertTrue(path.read_bytes().endswith(b'changed'))

    def test_bad_tablet_document_does_not_block_an_independent_approved_transfer(self):
        good = self.cloud.upload_pdf(pdf_bytes(), 'Good paper', self.croot.id, str(uuid.uuid4()), self.croot.id)
        self.worker.sync()
        row = next(r for r in self.worker.review.rows() if r['identity'] == good.id)
        self.worker.approve(row['id'], row['digest'], 'Good.pdf', 'Good paper', 'journal_article', 'ROOT2345')
        bad = self.cloud.upload_pdf(pdf_bytes(), 'Bad paper', self.croot.id, str(uuid.uuid4()), self.croot.id)
        original = self.cloud.pdf_bundle
        def download(item):
            if item.id == bad.id:
                raise CloudError('Unsupported document bundle')
            return original(item)
        with patch.object(self.cloud, 'pdf_bundle', side_effect=download) as downloads:
            result = self.worker.sync()
        self.assertEqual(sum(call.args[0].id == bad.id for call in downloads.call_args_list), 1)
        self.assertTrue(self.worker.state.read('documents')[good.id]['revision'])
        self.assertEqual(result['status']['state'], 'needs_review')
        self.assertTrue(any(r.get('cloud') == bad.id for r in result['status']['result']['review']))

    def test_expired_cloud_authorization_stops_catalog_instead_of_becoming_a_paper_issue(self):
        self.cloud.upload_pdf(pdf_bytes(), 'Paper', self.croot.id, str(uuid.uuid4()), self.croot.id)
        with patch.object(self.cloud, 'pdf_bundle', side_effect=CloudError('reMarkable authorization expired')):
            with self.assertRaisesRegex(CloudError, 'authorization expired'):
                self.worker.sync()
        self.assertFalse(self.worker.catalog_issues)
        self.assertFalse(self.zot.items)

    def test_cancel_during_catalog_stops_before_reading_another_tablet_paper(self):
        self.cloud.upload_pdf(pdf_bytes(), 'Tablet paper', self.croot.id, str(uuid.uuid4()), self.croot.id)
        self.worker.stop.set()
        with patch.object(self.worker, '_proposal', side_effect=AssertionError('Stopped operation read a paper')):
            with self.assertRaises(InterruptedError):
                self.worker._catalog(self.zot, self.cloud)

    def test_one_tablet_home_allows_multiple_research_collections(self):
        self.add_pdf()
        self.zot.collection_records['TOPIC234'] = {'key': 'TOPIC234', 'name': 'Research topic', 'parentCollection': False}
        with patch.object(self.worker.review, 'observe_ready', return_value=True): self.worker.sync()
        review = self.worker.review.rows()[0]
        self.worker.approve(review['id'], review['digest'], 'Reviewed.pdf', 'Dissertation', 'thesis_dissertation', 'ROOT2345', ['TOPIC234'])
        self.worker.sync(); self.worker.sync()
        self.assertEqual(len(self.worker.state.read('documents')), 1)
        self.assertEqual(set(self.zot.items['PARENT23']['collections']), {'ROOT2345', 'TOPIC234'})

    def test_single_sided_folder_rename_propagates_without_changing_document(self):
        child = self.cloud.create_folder('Topic', self.croot.id, str(uuid.uuid4()), self.croot.id)
        self.worker.sync()
        mappings = self.worker.state.read('folders')
        key = next(k for k, r in mappings.items() if r['cloud'] == child.id)
        self.zot.collection_records[key]['name'] = 'New topic'
        self.worker.sync()
        self.assertEqual(next(i for i in self.cloud.library() if i.id == child.id).name, 'New topic')
        current = next(i for i in self.cloud.library() if i.id == child.id)
        self.cloud.update_item(current.id, expected_hash=current.hash, name='Tablet rename', scope_id=self.croot.id)
        self.worker.sync()
        self.assertEqual(self.zot.collection_records[key]['name'], 'Tablet rename')

    def test_confirm_tablet_folder_deletion_preserves_branch_and_keeps_syncing(self):
        folder = self.cloud.create_folder('Test', self.croot.id, str(uuid.uuid4()), self.croot.id)
        nested = self.cloud.create_folder('Nested', folder.id, str(uuid.uuid4()), self.croot.id)
        self.worker.sync()
        maps = self.worker.state.read('folders')
        key = next(k for k, link in maps.items() if link['cloud'] == folder.id)
        child = next(k for k, link in maps.items() if link['cloud'] == nested.id)
        self.add_pdf()
        self.zot.items['PARENT23']['collections'] = [child]
        with patch.object(self.worker.review, 'observe_ready', return_value=True):
            self.worker.sync()
        row = next(row for row in self.worker.review.rows() if row['kind'] == 'zotero')
        self.worker.approve(row['id'], row['digest'], 'Reviewed.pdf', 'Dissertation', 'thesis_dissertation', child)
        self.worker.sync()
        documents = self.worker.state.read('documents')
        paper = next(item for item in self.cloud.library() if item.id in documents)
        self.cloud.update_item(paper.id, expected_hash=paper.hash, parent='trash', scope_id=self.croot.id)
        self.cloud.update_item(folder.id, expected_hash=folder.hash, parent='trash', scope_id=self.croot.id)
        unrelated = self.cloud.create_folder('Still syncing', self.croot.id, str(uuid.uuid4()), self.croot.id)
        replacement = self.cloud.create_folder('Test', self.croot.id, str(uuid.uuid4()), self.croot.id)

        result = self.worker.sync()
        self.assertEqual(len(result['folder_issues']), 1)
        issue = result['folder_issues'][0]
        self.assertEqual((issue['tablet_name'], issue['tablet_state']), ('Test', 'trash'))
        self.assertEqual(set(result['blocked_collections']), {key, child})
        self.assertTrue(result['last_sync_at'])
        self.assertEqual(self.worker.state.read('documents'), documents)
        self.assertFalse(result['deletions'], 'Folder deletion must not become a per-paper deletion')
        self.assertIn(unrelated.id, {link['cloud'] for link in result['mappings'].values()})
        self.assertNotIn(replacement.id, {link['cloud'] for link in result['mappings'].values()})

        stale = self.worker.confirm_folder_change({**issue, 'tablet_name': 'Older snapshot'})
        self.assertEqual(stale['folder_change'], 'changed')
        cloud_hash, writes = self.cloud.root_hash, self.zot.writes
        confirmed = self.worker.confirm_folder_change(issue)
        self.assertEqual(confirmed['folder_change'], 'confirmed')
        self.assertFalse(confirmed['folder_issues'])
        self.assertEqual((self.cloud.root_hash, self.zot.writes), (cloud_hash, writes))
        self.assertIn(child, self.zot.collection_records)
        self.assertEqual(self.zot.items['PARENT23']['collections'], [child])
        self.assertTrue(self.zot.file_path('SOURCE23').is_file())
        restarted = Worker(self.worker.directory, zotero_factory=lambda: self.zot, cloud_factory=lambda: self.cloud)
        restarted.config = self.worker.config
        self.assertFalse(restarted.sync()['folder_issues'])
        self.assertEqual(self.cloud.root_hash, cloud_hash)

    def test_mirror_deleted_zotero_collection_requires_current_tablet_branch(self):
        folder = self.cloud.create_folder('Matching folder', self.croot.id, str(uuid.uuid4()), self.croot.id)
        other = self.cloud.create_folder('Other folder', self.croot.id, str(uuid.uuid4()), self.croot.id)
        self.worker.sync()
        key = next(k for k, link in self.worker.state.read('folders').items() if link['cloud'] == folder.id)
        del self.zot.collection_records[key]
        issue = self.worker.sync()['folder_issues'][0]
        self.assertEqual(issue['mirror']['side'], 'tablet')
        self.assertEqual(issue['mirror']['children'], 0)
        nested = self.cloud.create_folder('Added since review', folder.id, str(uuid.uuid4()), self.croot.id)
        checked = self.worker.confirm_folder_change(issue, 'mirror')
        self.assertEqual(checked['folder_change'], 'changed')
        self.assertFalse(next(item for item in self.cloud.library() if item.id == folder.id).deleted)
        self.assertEqual(checked['folder_issues'][0]['mirror']['children'], 1)
        result = self.worker.confirm_folder_change(checked['folder_issues'][0], 'mirror')
        self.assertEqual(result['folder_choice'], 'mirror')
        self.assertFalse(result['folder_issues'])
        records = {item.id: item for item in self.cloud.library()}
        self.assertEqual(records[folder.id].parent, 'trash')
        self.assertEqual(records[nested.id].parent, folder.id)
        self.assertFalse(records[other.id].deleted)
        self.assertFalse(self.worker.sync()['folder_issues'])

    def test_mirror_deleted_tablet_folder_forwards_the_confirmed_zotero_items(self):
        folder = self.cloud.create_folder('Matching folder', self.croot.id, str(uuid.uuid4()), self.croot.id)
        self.worker.sync()
        self.cloud.update_item(folder.id, expected_hash=folder.hash, parent='trash', scope_id=self.croot.id)
        issue = self.worker.sync()['folder_issues'][0]
        plan = {'token': 'reviewed-native-snapshot', 'items': [{'key': 'PARENT23', 'trash': False, 'remove': []}]}
        with patch.object(self.zot, 'review_action', return_value={'trashed': True}) as action:
            self.assertEqual(self.worker.confirm_folder_change(issue, 'mirror', zotero_plan=plan)['folder_choice'], 'mirror')
        operation, payload = action.call_args.args
        self.assertEqual((operation, payload['key'], payload['plan']), ('trash_linked_collection', issue['zotero'], plan))

    def test_restore_tablet_folder_keeps_same_connection_and_nested_folder(self):
        folder = self.cloud.create_folder('Moved to Trash', self.croot.id, str(uuid.uuid4()), self.croot.id)
        nested = self.cloud.create_folder('Nested', folder.id, str(uuid.uuid4()), self.croot.id)
        self.worker.sync()
        original = self.worker.state.read('folders')
        self.cloud.update_item(folder.id, expected_hash=folder.hash, parent='trash', scope_id=self.croot.id)
        issue = self.worker.sync()['folder_issues'][0]
        self.assertEqual(issue['undo']['side'], 'tablet')
        result = self.worker.confirm_folder_change(issue, 'restore')
        self.assertFalse(result['folder_issues'])
        self.assertFalse(result['blocked_collections'])
        current = {row.id: row for row in self.cloud.library()}
        self.assertEqual(current[folder.id].parent, self.croot.id)
        self.assertEqual(current[nested.id].parent, folder.id)
        self.assertEqual(self.worker.state.read('folders'), original)

    def test_concurrent_folder_edits_require_explicit_choice(self):
        child = self.cloud.create_folder('Topic', self.croot.id, str(uuid.uuid4()), self.croot.id)
        nested = self.cloud.create_folder('Nested', child.id, str(uuid.uuid4()), self.croot.id)
        other = self.cloud.create_folder('Other', self.croot.id, str(uuid.uuid4()), self.croot.id)
        self.worker.sync()
        keys = {r['cloud']: k for k, r in self.worker.state.read('folders').items()}
        key = keys[child.id]
        self.zot.collection_records[key]['name'] = 'Desktop choice'
        self.cloud.update_item(child.id, expected_hash=child.hash, name='Tablet choice', scope_id=self.croot.id)
        self.cloud.update_item(other.id, expected_hash=other.hash, name='Unrelated change', scope_id=self.croot.id)
        new_child = self.cloud.create_folder('Added in conflict', child.id, str(uuid.uuid4()), self.croot.id)
        status = self.worker.sync()
        self.assertEqual(status['blocked_collections'], sorted((key, keys[nested.id])))
        self.assertEqual(self.zot.collection_records[keys[other.id]]['name'], 'Unrelated change')
        self.assertNotIn(new_child.id, {link['cloud'] for link in status['mappings'].values()})
        review = next(r for r in self.worker.review.rows() if r['kind'] == 'conflict')
        self.assertEqual(next(i for i in self.cloud.library() if i.id == child.id).name, 'Tablet choice')
        self.worker.resolve_conflict(review['id'], review['digest'], 'tablet')
        status = self.worker.sync()
        self.assertEqual(self.zot.collection_records[key]['name'], 'Tablet choice')
        self.assertFalse(status['blocked_collections'])
        self.assertIn(new_child.id, {link['cloud'] for link in status['mappings'].values()})

    def test_folder_conflict_can_be_resolved_by_matching_names_in_both_apps(self):
        child = self.cloud.create_folder('Topic', self.croot.id, str(uuid.uuid4()), self.croot.id)
        self.worker.sync()
        key = next(k for k, link in self.worker.state.read('folders').items() if link['cloud'] == child.id)
        self.zot.collection_records[key]['name'] = 'Zotero name'
        self.cloud.update_item(child.id, expected_hash=child.hash, name='Tablet name', scope_id=self.croot.id)
        self.assertTrue(self.worker.sync()['blocked_collections'])
        self.zot.collection_records[key]['name'] = 'Tablet name'
        status = self.worker.sync()
        self.assertFalse(status['blocked_collections'])
        self.assertFalse(status['reviews'])

    def test_worker_keeps_cloud_catalog_until_pairing_changes(self):
        self.worker.cloud_factory = None
        with patch('note_organizer.zotero_worker.TokenVault.load', side_effect=['first', 'first', 'second']):
            first = self.worker.cloud()
            self.assertIs(self.worker.cloud(), first)
            self.assertIsNot(self.worker.cloud(), first)

    def test_templates_are_validated_before_saving(self):
        with self.assertRaises(ValueError):
            self.worker.configure(templates={'journal_article': '{made_up}'})
        self.assertNotIn('templates', self.worker.config)

    def test_match_from_PC_links_existing_tablet_copy_without_duplicate(self):
        path = self.add_pdf()
        existing = self.cloud.upload_pdf(path.read_bytes(), 'Existing tablet paper', self.croot.id, str(uuid.uuid4()), self.croot.id)
        with patch.object(self.worker.review, 'observe_ready', return_value=True): self.worker.sync()
        row = next(r for r in self.worker.review.rows() if r['kind'] == 'zotero')
        self.assertEqual(row['payload']['matches'][0]['key'], existing.id)
        self.worker.approve(row['id'], row['digest'], 'Linked.pdf', 'Dissertation', 'thesis_dissertation', 'ROOT2345', match=existing.id)
        self.worker.sync()
        self.assertEqual(len(self.worker.state.read('documents')), 1)
        self.assertEqual(len([i for i in self.cloud.library() if i.is_pdf]), 1)
        self.assertFalse(self.worker.review.rows())

    def test_match_from_tablet_adopts_existing_PC_reference(self):
        path = self.add_pdf()
        existing = self.cloud.upload_pdf(path.read_bytes(), 'Existing tablet paper', self.croot.id, str(uuid.uuid4()), self.croot.id)
        with patch.object(self.worker.review, 'observe_ready', return_value=True): self.worker.sync()
        row = next(r for r in self.worker.review.rows() if r['kind'] == 'cloud')
        self.worker.approve(row['id'], row['digest'], 'Linked.pdf', 'Dissertation', 'thesis_dissertation', 'ROOT2345', match='SOURCE23')
        self.worker.sync()
        link = self.worker.state.read('documents')[existing.id]
        self.assertEqual(link['parent'], 'PARENT23')
        self.assertEqual(link['source_key'], 'SOURCE23')
        self.assertEqual(len([r for r in self.zot.items.values() if r.get('itemType') == 'thesis']), 1)

    def test_tablet_filename_fallback_uses_readable_name_instead_of_download_digest(self):
        self.cloud.upload_pdf(pdf_bytes(), 'Readable tablet paper', self.croot.id, str(uuid.uuid4()), self.croot.id)
        self.worker.sync()
        row = next(r for r in self.worker.review.rows() if r['kind'] == 'cloud')
        self.assertEqual(row['payload']['filename'], 'Readable_tablet_paper.pdf')

    def test_cloud_fallback_does_not_repeat_extension_or_reuse_a_hash_label(self):
        path = self.root / ('a' * 64 + '.pdf')
        details = {'title': 'Meaningful extracted title', 'document_type': 'unknown', 'primary_creator': '', 'year': ''}
        with patch('note_organizer.sorting.core_logic.get_basic_document_details', side_effect=lambda _: dict(details)):
            self.assertEqual(self.worker._proposal(path, {'title': 'Readable title.pdf'})['filename'], 'Readable_title.pdf')
            recovered = self.worker._proposal(path, {'title': 'b' * 64})
            self.assertEqual(recovered['filename'], 'Meaningful_extracted_title.pdf')
            self.assertEqual(recovered['title'], 'Meaningful extracted title')
            details['title'] = 'a' * 64
            unknown = self.worker._proposal(path, {'title': 'b' * 64})
            self.assertEqual(unknown['filename'], 'Paper.pdf')
            self.assertTrue(unknown['details']['filename_needs_review'])

    def test_routine_tablet_filing_keeps_existing_folder_without_a_Zotero_match(self):
        child = self.cloud.create_folder('Topic', self.croot.id, str(uuid.uuid4()), self.croot.id)
        item = self.cloud.upload_pdf(pdf_bytes(), 'Tablet paper', child.id, str(uuid.uuid4()), self.croot.id)
        self.worker.sync()
        row = next(r for r in self.worker.review.rows() if r['identity'] == item.id)
        self.worker.approve_routine([{'id': row['id'], 'digest': row['digest'],
                                     'review_token': row['payload']['review_token']}], 'ROOT2345')
        self.assertEqual(next(i for i in self.cloud.library() if i.id == item.id).parent, child.id)
        topic = next(k for k, v in self.worker.state.read('folders').items() if v['cloud'] == child.id)
        approval = self.worker.review.row(row['id'])['approval']
        self.assertEqual(approval['collection'], topic)

    def test_new_exact_match_since_review_holds_batch_and_does_not_create_placeholder(self):
        data = pdf_bytes()
        item = self.cloud.upload_pdf(data, 'Tablet paper', self.croot.id, str(uuid.uuid4()), self.croot.id)
        self.worker.sync()
        row = next(r for r in self.worker.review.rows() if r['identity'] == item.id)
        self.add_pdf().write_bytes(data)  # PC import happened after tablet review was prepared.
        self.worker.approve_routine([{'id': row['id'], 'digest': row['digest'],
                                     'review_token': row['payload']['review_token']}], 'ROOT2345')
        current = self.worker.review.row(row['id'])
        self.assertEqual(current['payload']['matches'][0]['key'], 'SOURCE23')
        self.assertEqual(current['status'], 'pending')
        self.assertEqual(set(self.zot.items), {'PARENT23', 'SOURCE23'})
        self.worker.approve_routine([{'id': current['id'], 'digest': current['digest'],
                                     'review_token': current['payload']['review_token']}], 'ROOT2345')
        self.assertEqual(self.worker.state.read('documents')[item.id]['source_key'], 'SOURCE23')

    def test_explicit_separate_batch_cannot_clone_an_existing_pdf(self):
        data = pdf_bytes()
        item = self.cloud.upload_pdf(data, 'Tablet paper', self.croot.id, str(uuid.uuid4()), self.croot.id)
        self.worker.sync()
        row = next(r for r in self.worker.review.rows() if r['identity'] == item.id)
        self.add_pdf().write_bytes(data)
        before = set(self.zot.items)
        with self.assertRaisesRegex(PilotError, 'already exists in Zotero'):
            self.worker.approve(row['id'], row['digest'], 'Paper.pdf', 'Paper', 'journal_article', 'ROOT2345', _batch=True)
        self.assertEqual(set(self.zot.items), before)

    def test_tablet_exact_match_retains_curated_Zotero_citation_title_and_tags(self):
        path = self.add_pdf()
        self.zot.items['PARENT23']['tags'] = [{'tag': 'Curated', 'type': 1}]
        self.cloud.upload_pdf(path.read_bytes(), 'A short tablet label', self.croot.id, str(uuid.uuid4()), self.croot.id)
        with patch.object(self.worker.review, 'observe_ready', return_value=True):
            self.worker.sync()
        row = next(r for r in self.worker.review.rows() if r['kind'] == 'cloud')
        self.worker.approve(row['id'], row['digest'], 'Readable.pdf', 'A short tablet label', 'journal_article',
                            'ROOT2345', match='SOURCE23')
        self.assertEqual(self.zot.items['PARENT23']['title'], 'Dissertation')
        self.assertEqual(self.zot.items['PARENT23']['university'], 'Example')
        self.assertIn({'tag': 'Curated', 'type': 1}, self.zot.items['PARENT23']['tags'])

    def test_tablet_changes_since_review_are_rejected_before_Zotero_writes(self):
        item = self.cloud.upload_pdf(pdf_bytes(), 'Tablet paper', self.croot.id, str(uuid.uuid4()), self.croot.id)
        self.worker.sync()
        row = next(r for r in self.worker.review.rows() if r['kind'] == 'cloud')
        self.cloud.update_item(item.id, expected_hash=item.hash, name='User changed label', scope_id=self.croot.id)
        before = self.zot.writes
        with self.assertRaisesRegex(PilotError, 'tablet document changed'):
            self.worker.approve(row['id'], row['digest'], 'Old.pdf', 'Old', 'journal_article', 'ROOT2345')
        self.assertEqual(self.zot.writes, before)

    def test_legacy_approved_placeholder_is_held_before_publishing_an_existing_PDF_again(self):
        data = pdf_bytes()
        item = self.cloud.upload_pdf(data, 'Readable tablet title', self.croot.id, str(uuid.uuid4()), self.croot.id)
        self.worker.sync()
        row = next(r for r in self.worker.review.rows() if r['kind'] == 'cloud')
        self.worker.approve(row['id'], row['digest'], 'Readable.pdf', 'Readable tablet title', 'journal_article', 'ROOT2345')
        placeholder = self.worker.review.row(row['id'])['approval']['parent']
        self.add_pdf().write_bytes(data)
        self.worker.sync()
        current = self.worker.review.row(row['id'])
        self.assertEqual(current['status'], 'pending')
        self.assertIn('avoid a duplicate', current['payload']['repair_required'])
        self.assertEqual(current['payload']['matches'][0]['key'], 'SOURCE23')
        self.assertIn(placeholder, self.zot.items)  # Never silently delete an existing reference.
        self.assertNotIn(item.id, self.worker.state.read('documents', {}))
        self.assertEqual(len([r for r in self.zot.items.values() if r.get('itemType') == 'attachment']), 1)
        self.worker.sync()
        current = self.worker.review.row(row['id'])
        self.assertFalse(current['payload']['routine'])
        self.assertEqual(current['payload']['previous_approval']['parent'], placeholder)

    def test_legacy_approved_digest_name_reopens_with_its_original_readable_title(self):
        item = self.cloud.upload_pdf(pdf_bytes(), 'Readable tablet title', self.croot.id, str(uuid.uuid4()), self.croot.id)
        self.worker.sync()
        row = next(r for r in self.worker.review.rows() if r['kind'] == 'cloud')
        self.worker.approve(row['id'], row['digest'], row['digest'] + '.pdf', 'Readable tablet title', 'journal_article', 'ROOT2345')
        self.worker.sync()
        current = self.worker.review.row(row['id'])
        self.assertEqual(current['status'], 'pending')
        self.assertEqual(current['payload']['title'], 'Readable tablet title')
        self.assertEqual(current['payload']['filename'], 'Readable_tablet_title.pdf')
        self.assertNotIn(item.id, self.worker.state.read('documents', {}))

    def test_cancelled_batch_keeps_unfinished_reviews_and_reports_paused(self):
        self.cloud.upload_pdf(pdf_bytes(), 'First tablet paper', self.croot.id, str(uuid.uuid4()), self.croot.id)
        self.worker.sync()
        row = next(r for r in self.worker.review.rows() if r['kind'] == 'cloud')
        entries = [{'id': row['id'], 'digest': row['digest'], 'review_token': row['payload']['review_token']}]
        with patch.object(self.worker, 'approve', side_effect=InterruptedError('Stopped')):
            result = self.worker.approve_routine(entries, 'ROOT2345')
        self.assertEqual(result['status']['state'], 'paused')
        self.assertIn('1 remaining', result['status']['message'])
        self.assertEqual(self.worker.review.row(row['id'])['status'], 'pending')
        self.assertTrue(self.worker.review.row(row['id'])['payload']['routine'])

    def test_filing_summary_separates_approved_linked_and_received(self):
        self.cloud.upload_pdf(pdf_bytes(), 'Tablet paper', self.croot.id, str(uuid.uuid4()), self.croot.id)
        self.worker.sync()
        row = self.worker.review.rows()[0]
        self.worker.approve(row['id'], row['digest'], 'Readable.pdf', 'Readable title', 'journal_article', 'ROOT2345')
        self.assertEqual(self.worker.status()['filing_summary'], {'approved_waiting_sync': 1, 'linked': 0, 'pdfs_received': 0})
        self.worker.sync()
        self.assertEqual(self.worker.status()['filing_summary'], {'approved_waiting_sync': 0, 'linked': 1, 'pdfs_received': 1})

    def test_unlink_keeps_both_copies_and_stops_processing(self):
        self.add_pdf()
        with patch.object(self.worker.review, 'observe_ready', return_value=True): self.worker.sync()
        row = self.worker.review.rows()[0]
        self.worker.approve(row['id'], row['digest'], 'Reviewed.pdf', 'Dissertation', 'thesis_dissertation', 'ROOT2345')
        self.worker.sync()
        identity = next(iter(self.worker.state.read('documents')))
        before = {i.id: i.hash for i in self.cloud.library()}
        self.worker.link(identity, True)
        self.worker.sync()
        self.assertEqual({i.id: i.hash for i in self.cloud.library()}, before)
        self.assertIn(identity, self.worker.review.suspended_documents())
        self.assertIn('PARENT23', self.zot.items)
        self.worker.link(identity, False)
        self.assertNotIn(identity, self.worker.review.suspended_documents())

    def test_Zotero_reference_trash_offers_to_keep_or_trash_tablet_copy(self):
        self.add_pdf()
        with patch.object(self.worker.review, 'observe_ready', return_value=True): self.worker.sync()
        row = self.worker.review.rows()[0]
        self.worker.approve(row['id'], row['digest'], 'Reviewed.pdf', 'Dissertation', 'thesis_dissertation', 'ROOT2345')
        self.worker.sync()
        identity = next(iter(self.worker.state.read('documents')))
        self.zot.items['PARENT23']['deleted'] = 1
        self.worker.sync()
        decision = self.worker.review.local_deletions()[0]
        self.assertFalse(next(i for i in self.cloud.library() if i.id == identity).deleted)
        self.worker.local_delete_decision(decision['id'], 'trash_tablet')
        self.assertTrue(next(i for i in self.cloud.library() if i.id == identity).deleted)
        self.assertTrue(self.worker.review.link_control(identity)['paused'])

    def test_citation_change_while_reviewing_requires_fresh_consent(self):
        self.add_pdf()
        with patch.object(self.worker.review, 'observe_ready', return_value=True): self.worker.sync()
        row = self.worker.review.rows()[0]
        self.zot.items['PARENT23']['title'] = 'User corrected title'
        with self.assertRaises(PilotError):
            self.worker.approve(row['id'], row['digest'], 'Reviewed.pdf', 'Dissertation', 'thesis_dissertation', 'ROOT2345')
        self.assertEqual(self.worker.review.row(row['id'])['status'], 'pending')
        with patch.object(self.worker.review, 'observe_ready', return_value=True): self.worker.sync()
        self.assertEqual(self.worker.review.row(row['id'])['payload']['title'], 'User corrected title')


if __name__ == '__main__':
    unittest.main()
