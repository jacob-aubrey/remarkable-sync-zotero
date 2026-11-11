"""Explicit destination creation, retained identities, and independent folder names."""
from dataclasses import replace
import tempfile
from pathlib import Path
from unittest.mock import patch
import unittest

from note_organizer.zotero_worker import Worker
from note_organizer.zotero_bridge import Bridge
from note_organizer.zotero_pilot import PilotError
from tests.test_zotero_worker import ReviewZotero
from tests.test_zotero_bridge import FakeCloud


class CreationCloud(FakeCloud):
    def create_folder(self, name, parent, identity, scope=''):
        return super().create_folder(name, parent, identity, self.root.id)

    def update_item(self, identity, *, expected_hash, name=None, parent=None, scope_id=''):
        old = self.records[identity]
        if old.hash != expected_hash:
            raise AssertionError('Unexpected revision')
        self.records[identity] = replace(old, name=old.name if name is None else name,
                                         parent=old.parent if parent is None else parent, hash='c' * 64)
        return self.records[identity]


class DestinationCreationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.zot = ReviewZotero(self.root)
        self.cloud = CreationCloud()
        self.worker = Worker(self.root / 'helper', zotero_factory=lambda: self.zot, cloud_factory=lambda: self.cloud)
        self.worker.config = {'zotero_root': 'ROOT2345', 'tablet_root': self.cloud.root.id, 'default_folder': self.cloud.root.id, 'ocr': False}
        self.worker.state.write('folders', {'ROOT2345': {'cloud': self.cloud.root.id, 'root': True}})
        self.native = self.zot.review_action
        self.zot.review_action = self.review_action

    def review_action(self, action, payload=None):
        if action == 'create_collection':
            self.zot.authorize()
            return {'collection': self.zot.create('collection', {'key': payload['key'], 'name': payload['name'], 'parentCollection': payload['parent'] or False})}
        return self.native(action, payload)

    def test_zotero_only_without_cloud_and_existing_sibling_reuse(self):
        with patch.object(self.worker, 'cloud', side_effect=AssertionError('No cloud call')):
            first = self.worker.create_destination('zotero', 'ROOT2345', 'Research')
            second = self.worker.create_destination('zotero', 'ROOT2345', 'Research')
        self.assertTrue(first['created_destination']['complete'])
        self.assertEqual(first['created_destination']['key'], second['created_destination']['key'])
        self.assertTrue(second['created_destination']['reused'])
        self.assertEqual(second['created_destination']['locations'], {'zotero': 'ready', 'tablet': 'not_requested'})
        self.assertEqual(self.zot.writes, 1)
        self.assertEqual(self.cloud.folder_writes, 0)
        self.assertEqual(len(first['destination_catalog']['collections']), 2)

    def test_tablet_only_without_zotero(self):
        with patch.object(self.worker, 'zotero', side_effect=AssertionError('No Zotero call')):
            result = self.worker.create_destination('tablet', self.cloud.root.id, 'Tablet folder')
        self.assertTrue(result['created_destination']['complete'])
        self.assertEqual(result['created_destination']['locations'], {'zotero': 'not_requested', 'tablet': 'ready'})
        self.assertEqual(self.cloud.folder_writes, 1)
        self.assertEqual(self.zot.writes, 0)

    def test_lost_commit_response_reuses_persisted_ids(self):
        self.cloud.lose_folder = True
        first = self.worker.create_destination('paired', 'ROOT2345', 'Collection', 'Tablet name')
        self.assertFalse(first['created_destination']['complete'])
        self.assertEqual(first['created_destination']['locations'], {'zotero': 'ready', 'tablet': 'pending'})
        self.assertIn('Retry', first['created_destination']['error'])
        second = self.worker.create_destination('paired', 'ROOT2345', 'Collection', 'Tablet name')
        self.assertTrue(second['created_destination']['complete'])
        self.assertEqual(second['created_destination']['locations'], {'zotero': 'ready', 'tablet': 'ready'})
        self.assertEqual(first['created_destination']['zotero'], second['created_destination']['zotero'])
        self.assertEqual(first['created_destination']['tablet'], second['created_destination']['tablet'])
        self.assertEqual(self.zot.writes, 1)
        self.assertEqual(self.cloud.folder_writes, 1)
        mapping = self.worker.state.read('folders')[second['created_destination']['key']]
        self.assertEqual(mapping['desktop_name'], 'Collection')
        self.assertEqual(mapping['tablet_name'], 'Tablet name')
        # Different initial names remain stable on a normal sync and a dry run.
        bridge = Bridge(self.zot, self.cloud, self.worker.state, 'ROOT2345', self.cloud.root.id, follow_changes=True)
        self.assertFalse(bridge.run(apply=True)['review'])
        self.assertFalse(Bridge(self.zot, self.cloud, self.worker.state, 'ROOT2345', self.cloud.root.id).run(apply=False)['review'])
        self.assertEqual(self.cloud.records[mapping['cloud']].name, 'Tablet name')
        self.assertEqual(self.zot.collection_records[second['created_destination']['key']]['name'], 'Collection')

    def test_invalid_parent_and_names_write_nothing(self):
        for args in [('paired', '', 'Name'), ('zotero', 'MISSING2', 'Name'), ('tablet', self.cloud.root.id, ' '), ('zotero', '', 'Bad/Name')]:
            with self.assertRaises(PilotError):
                self.worker.create_destination(*args)
        self.assertEqual(self.zot.writes, 0)
        self.assertEqual(self.cloud.folder_writes, 0)

    def test_independent_names_survive_parent_move_but_explicit_rename_propagates(self):
        child = self.worker.create_destination('paired', 'ROOT2345', 'Collection', 'Tablet name')['created_destination']
        parent = self.worker.create_destination('paired', 'ROOT2345', 'Parent')['created_destination']
        self.zot.collection_records[child['key']]['parentCollection'] = parent['key']
        result = Bridge(self.zot, self.cloud, self.worker.state, 'ROOT2345', self.cloud.root.id, follow_changes=True).run(apply=True)
        self.assertFalse(result['review'])
        self.assertEqual(self.cloud.records[child['tablet']].name, 'Tablet name')
        self.assertEqual(self.cloud.records[child['tablet']].parent, parent['tablet'])
        self.zot.collection_records[child['key']]['name'] = 'New explicit name'
        Bridge(self.zot, self.cloud, self.worker.state, 'ROOT2345', self.cloud.root.id, follow_changes=True).run(apply=True)
        self.assertEqual(self.cloud.records[child['tablet']].name, 'New explicit name')
