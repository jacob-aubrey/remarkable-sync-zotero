"""Real cloud CAS writer, stable page identities, and interrupted tag sync."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

from note_organizer.remarkable_cloud import CloudConflict, CloudError
from note_organizer.zotero_pilot import PilotError
from note_organizer.zotero_tags import reconcile, render_fingerprint, tablet_tags
from note_organizer.zotero_worker import Worker
from tests.test_cloud_outbox import MemoryCloud
from tests.test_remarkable import fixture
from tests.test_zotero_worker import ReviewZotero


class TagTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cloud = MemoryCloud()
        self.folder = self.cloud.create_folder('Literature', '', str(uuid.uuid4()))
        item, bundle = fixture(self.folder.id)
        content = json.loads(bundle[item.id + '.content'])
        self.pid = content['cPages']['pages'][0]['id']
        content['unknownFutureField'] = {'retain': [1, 2]}
        content['tags'] = [{'name': 'Tablet', 'timestamp': 123, 'unknown': 'retain'}]
        content['pageTags'] = [{'name': 'Method', 'pageId': self.pid, 'timestamp': 456}]
        bundle[item.id + '.content'] = json.dumps(content).encode()
        bundle[item.id + '.metadata'] = json.dumps({'visibleName': item.name, 'parent': self.folder.id,
                                                    'type': 'DocumentType'}).encode()
        self.item = self.cloud._commit_new_item(item.id, item.name, self.folder.id, 'DocumentType', bundle, self.folder.id)
        self.zot = ReviewZotero(self.root)
        self.zot.items['PARENT23'] = {'key': 'PARENT23', 'itemType': 'journalArticle', 'title': 'A paper',
                                    'tags': [{'tag': 'Publisher', 'type': 1}]}
        self.row = {'name': item.name, 'revision': self.item.hash}
        self.bridge = SimpleNamespace(zot=self.zot, cloud=self.cloud, croot=self.folder.id)
        self.saves = []

    def sync(self):
        item = next(r for r in self.cloud.library() if r.id == self.item.id)
        reconcile(self.bridge, 'PARENT23', item, self.cloud.pdf_bundle(item), self.row,
                  lambda: self.saves.append(copy.deepcopy(self.row)))

    def current(self):
        item = next(r for r in self.cloud.library() if r.id == self.item.id)
        return item, self.cloud.pdf_bundle(item)

    def test_whole_paper_union_includes_publisher_keywords_keeps_type_and_scopes_pages(self):
        self.sync()
        self.assertEqual(self.zot.items['PARENT23']['tags'], [{'tag': 'Publisher', 'type': 1}, {'tag': 'Tablet'}])
        self.assertEqual(self.row['page_tags'], {self.pid: ['Method']})
        item, bundle = self.current()
        content = json.loads(bundle[item.id + '.content'])
        self.assertEqual(tablet_tags(content), ({'Publisher', 'Tablet'}, {self.pid: {'Method'}}))
        self.assertEqual(content['unknownFutureField'], {'retain': [1, 2]})
        self.assertEqual(next(t for t in content['tags'] if t['name'] == 'Tablet'), {'name': 'Tablet', 'timestamp': 123, 'unknown': 'retain'})
        self.assertIs(type(next(t for t in content['tags'] if t['name'] == 'Publisher')['timestamp']), int)
        self.assertEqual(content['pageTags'][0]['timestamp'], 456)

    def test_unrepresentable_native_tag_is_retained_without_partial_writes(self):
        self.zot.items['PARENT23']['tags'] = [{'tag': 'Keep\x02original', 'type': 0}]
        parent = copy.deepcopy(self.zot.items['PARENT23'])
        row = copy.deepcopy(self.row)
        old_item, old_bundle = self.current()
        with self.assertRaisesRegex(PilotError, 'Tags must be'):
            self.sync()
        self.assertEqual(self.zot.items['PARENT23'], parent)
        self.assertEqual(self.row, row)
        item, bundle = self.current()
        self.assertEqual(item.hash, old_item.hash)
        self.assertEqual(bundle, old_bundle)

    def test_tag_only_write_never_rewrites_pdf_or_pen_layers_or_render_fingerprint(self):
        old_item, old = self.current()
        before = render_fingerprint(old_item.id, old)
        self.sync()
        item, new = self.current()
        self.assertNotEqual(item.hash, old_item.hash)
        self.assertEqual(before, render_fingerprint(item.id, new))
        for name in old:
            if name.endswith(('.pdf', '.rm')):
                self.assertEqual(new[name], old[name])
        writes = len(self.cloud.puts)
        self.sync()
        self.assertEqual(len(self.cloud.puts), writes)

    def test_add_remove_rename_both_scopes_both_directions(self):
        self.sync()
        self.zot.items['PARENT23']['tags'] = [{'tag': 'Renamed publisher'}]
        self.row['page_tags'] = {self.pid: ['New page tag']}
        self.sync()
        item, bundle = self.current()
        self.assertEqual(tablet_tags(json.loads(bundle[item.id + '.content'])),
                         ({'Renamed publisher'}, {self.pid: {'New page tag'}}))
        self.cloud.update_item(item.id, expected_hash=item.hash, scope_id=self.folder.id,
                               tags=['From tablet'], page_tags={self.pid: ['Tablet page']})
        self.sync()
        self.assertEqual(self.zot.items['PARENT23']['tags'], [{'tag': 'From tablet'}])
        self.assertEqual(self.row['page_tags'], {self.pid: ['Tablet page']})

    def test_partial_write_survives_restart_and_subsequent_user_removal(self):
        with patch.object(self.cloud, 'update_item', side_effect=CloudError('Offline')):
            with self.assertRaises(CloudError):
                self.sync()
        self.assertIn('tag_pending', self.row)
        self.row = json.loads(json.dumps(self.row))  # Reconstruct durable state.
        self.zot.items['PARENT23']['tags'] = [{'tag': 'Publisher', 'type': 1}]
        self.sync()
        self.assertEqual(self.row['tag_state']['paper'], ['Publisher'])
        self.assertNotIn('tag_pending', self.row)

    def test_cloud_rejects_stale_revision_and_new_tags_on_unknown_page(self):
        self.sync()
        item, old = self.current()
        with self.assertRaises(CloudConflict):
            self.cloud.update_item(item.id, expected_hash=self.item.hash, scope_id=self.folder.id, tags=[])
        with self.assertRaises(CloudError):
            self.cloud.update_item(item.id, expected_hash=item.hash, scope_id=self.folder.id,
                                   page_tags={str(uuid.uuid4()): ['Wrong page']})
        self.assertEqual(self.current()[0].hash, item.hash)

    def test_page_editor_and_search_use_uuid_and_reject_stale_pdf_or_context(self):
        self.sync()
        worker = Worker(self.root / 'worker', zotero_factory=lambda: self.zot)
        key = 'VIEW2345'
        data = self.current()[1][self.item.id + '.pdf']
        self.zot.file_path(key).write_bytes(data)
        self.zot.items[key] = {'key': key, 'parentItem': 'PARENT23', 'itemType': 'attachment',
                              'contentType': 'application/pdf', 'linkMode': 'imported_file'}
        link = {**self.row, 'parent': 'PARENT23', 'view_key': key, 'page_map': [self.pid],
                'page_map_digest': hashlib.sha256(data).hexdigest()}
        worker.state.write('documents', {self.item.id: link})
        context = worker.tag_context(key, 0)
        self.assertEqual(context['page_id'], self.pid)
        worker.set_page_tags(key, 0, self.pid, ['Results'], context['token'])
        with self.assertRaises(PilotError):
            worker.set_page_tags(key, 0, self.pid, ['Stale edit'], context['token'])
        result = worker.tag_search('results', 'page')['results']
        self.assertEqual([(r['page'], r['page_id']) for r in result], [(0, self.pid)])
        self.assertEqual(worker.tag_search('results', 'paper')['results'], [])
        vocab = worker.tag_search(vocabulary_only=True)
        self.assertEqual(vocab['results'], [])
        self.assertEqual(vocab['paper_vocabulary'], ['Publisher', 'Tablet'])
        self.assertEqual(vocab['page_vocabulary'], ['Results'])
        self.assertEqual(worker.tag_search(tags=['Publisher', 'Results'])['results'], [])
        either = worker.tag_search(tags=['Publisher', 'Results'], match='any')['results']
        self.assertEqual([(r['scope'], r['tags']) for r in either],
                         [('paper', ['Publisher']), ('page', ['Results'])])
        self.assertEqual(worker.tag_search(tags=['publish'])['results'], [])
        self.assertEqual(len(worker.tag_search(tags=['Publisher', 'Tablet'], scope='paper')['results']), 1)
        with patch.object(self.zot, 'review_action', wraps=self.zot.review_action) as request:
            scoped = worker.tag_search(tags=['Results'], scope='page', results_only=True)
            request.assert_called_once_with('catalog', {'tag_lookup': {
                'papers': False, 'tags': ['Results'], 'match': 'all', 'views': [key]}})
            self.assertEqual(scoped, {'results': result})
        self.zot.file_path(key).write_bytes(data + b'changed')
        with self.assertRaises(PilotError):
            worker.tag_context(key, 0)

    def test_ambiguous_interrupted_edits_wait_for_explicit_combine_and_orphan_tags_are_reviewable(self):
        with patch.object(self.cloud, 'update_item', side_effect=CloudError('Offline')):
            with self.assertRaises(CloudError):
                self.sync()
        self.zot.items['PARENT23']['tags'].append({'tag': 'New local edit'})
        item, _ = self.current()
        self.cloud.update_item(item.id, expected_hash=item.hash, scope_id=self.folder.id, tags=['Concurrent tablet edit'])
        with self.assertRaises(PilotError):
            self.sync()
        worker = Worker(self.root / 'worker', zotero_factory=lambda: self.zot, cloud_factory=lambda: self.cloud)
        key, orphan = 'VIEW2345', str(uuid.uuid4())
        data = self.current()[1][self.item.id + '.pdf']
        self.zot.file_path(key).write_bytes(data)
        self.zot.items[key] = {'key': key, 'parentItem': 'PARENT23', 'itemType': 'attachment', 'contentType': 'application/pdf'}
        self.row['page_tags'][orphan] = ['Pending tag for a removed page']
        worker.state.write('documents', {self.item.id: {**self.row, 'parent': 'PARENT23', 'view_key': key,
                           'page_map': [self.pid], 'page_map_digest': hashlib.sha256(data).hexdigest()}})
        context = worker.tag_context(key, 0)
        self.assertTrue(context['interrupted'])
        self.assertIn(orphan, context['orphaned_pages'])
        context = worker.remove_unavailable_page_tags(key, 0, orphan, context['token'])
        self.assertEqual(context['orphaned_pages'], {})
        context = worker.combine_tag_copies(key, 0, context['token'])
        self.assertFalse(context['interrupted'])
        self.assertEqual(set(context['paper_tags']), {'Publisher', 'Tablet', 'New local edit', 'Concurrent tablet edit'})


if __name__ == '__main__':
    unittest.main()
