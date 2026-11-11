from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
import uuid

from note_organizer.remarkable_cloud import CloudConflict, CloudError
from note_organizer.zotero_pilot import PilotError
from note_organizer.zotero_review import ReviewStore
from tests.test_cloud_outbox import MemoryCloud, pdf_bytes


class ReviewedCloudChanges(unittest.TestCase):
    def setUp(self):
        self.cloud = MemoryCloud()
        self.root = self.cloud.create_folder("Literature", "", str(uuid.uuid4()))
        self.folder = self.cloud.create_folder("Topic", self.root.id, str(uuid.uuid4()), self.root.id)
        self.paper = self.cloud.upload_pdf(pdf_bytes(), "Paper", self.folder.id, str(uuid.uuid4()), self.root.id)

    def test_rename_move_retains_every_original_source_file(self):
        before = self.cloud.pdf_bundle(self.paper)
        changed = self.cloud.update_item(self.paper.id, expected_hash=self.paper.hash,
            name="Reviewed paper", parent=self.root.id, scope_id=self.root.id)
        after = self.cloud.pdf_bundle(changed)
        self.assertEqual(changed.parent, self.root.id)
        self.assertEqual(changed.name, "Reviewed paper")
        for name, data in before.items():
            if not name.endswith(".metadata"):
                self.assertEqual(after[name], data)

    def test_trash_requires_explicit_restore_and_live_destination(self):
        removed = self.cloud.update_item(self.paper.id, expected_hash=self.paper.hash, parent="trash", scope_id=self.root.id)
        self.assertTrue(removed.deleted)
        with self.assertRaises(CloudError):
            self.cloud.update_item(removed.id, expected_hash=removed.hash, parent=self.folder.id, scope_id=self.root.id)
        restored = self.cloud.update_item(removed.id, expected_hash=removed.hash,
            parent=self.folder.id, scope_id=self.root.id, restore=True)
        self.assertFalse(restored.deleted)
        self.assertEqual(restored.parent, self.folder.id)

    def test_stale_revision_never_overwrites_newer_metadata(self):
        changed = self.cloud.update_item(self.paper.id, expected_hash=self.paper.hash, name="New tablet name", scope_id=self.root.id)
        puts = len(self.cloud.puts)
        with self.assertRaises(CloudConflict):
            self.cloud.update_item(self.paper.id, expected_hash=self.paper.hash, name="Stale desktop name", scope_id=self.root.id)
        self.assertEqual(len(self.cloud.puts), puts)
        self.assertEqual(next(i.name for i in self.cloud.library() if i.id == changed.id), "New tablet name")

    def test_unrelated_root_conflict_retries_without_losing_other_items(self):
        self.cloud.conflict_next = True
        self.cloud.update_item(self.paper.id, expected_hash=self.paper.hash, name="Reviewed", scope_id=self.root.id)
        self.assertEqual(len(self.cloud.library()), 3)

    def test_folder_cycle_and_move_outside_scope_are_rejected(self):
        with self.assertRaises(CloudError):
            self.cloud.update_item(self.folder.id, expected_hash=self.folder.hash, parent=self.folder.id, scope_id=self.root.id)
        outside = self.cloud.create_folder("Outside", "", str(uuid.uuid4()))
        with self.assertRaises(CloudError):
            self.cloud.update_item(self.paper.id, expected_hash=self.paper.hash, parent=outside.id, scope_id=self.root.id)


class ReviewDecisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ReviewStore(Path(self.temp.name))
        self.cloud = MemoryCloud()
        self.root = self.cloud.create_folder("Literature", "", str(uuid.uuid4()))
        self.paper = self.cloud.upload_pdf(pdf_bytes(), "Paper", self.root.id, str(uuid.uuid4()), self.root.id)
        self.link = {"name": "Paper", "tablet_parent": self.root.id, "source_key": "SOURCE23"}
        self.zot = Mock()

    def deleted(self):
        removed = self.cloud.update_item(self.paper.id, expected_hash=self.paper.hash, parent="trash", scope_id=self.root.id)
        key = self.store.confirm_deleted(removed, self.link, attachment="VIEW2345", parent="PARENT23", inventory=[])
        return key, removed

    def test_download_settle_requires_unchanged_signature(self):
        self.assertFalse(self.store.observe_ready("file", "a", now=1))
        self.assertFalse(self.store.observe_ready("file", "b", now=4))
        self.assertFalse(self.store.observe_ready("file", "b", now=5))
        self.assertTrue(self.store.observe_ready("file", "b", now=6))

    def test_changed_document_invalidates_previous_review(self):
        key = self.store.offer("zotero", "SOURCE23", "a", {"title": "Paper"})
        self.store.begin(key, "a", {"parent": "PARENT23", "collection": "ROOT2345"})
        self.store.finish(key)
        self.assertIsNotNone(self.store.approval("zotero", "SOURCE23", "a"))
        self.store.offer("zotero", "SOURCE23", "b", {"title": "Updated"})
        self.assertIsNone(self.store.approval("zotero", "SOURCE23", "b"))
        with self.assertRaises(PilotError):
            self.store.begin(key, "a", {})

    def test_missing_or_live_item_does_not_create_deletion_notification(self):
        for item in (None, self.paper):
            self.assertIsNone(self.store.confirm_deleted(item, self.link, attachment="VIEW2345", parent="PARENT23", inventory=[]))
        self.assertEqual(self.store.deletions(), [])

    def test_confirmed_trash_waits_for_choice_without_writes(self):
        key, removed = self.deleted()
        puts = len(self.cloud.puts)
        self.assertIn(removed.id, self.store.suspended_documents())
        self.assertEqual(self.store.deletions()[0]["status"], "pending")
        self.assertEqual(len(self.cloud.puts), puts)
        self.zot.review_action.assert_not_called()

    def test_restore_resumes_link_and_keeps_zotero(self):
        key, _ = self.deleted()
        self.store.resolve_deletion(key, "restore", cloud=self.cloud, zotero=self.zot, scope=self.root.id)
        self.assertEqual(self.store.deletions(), [])
        self.assertEqual(self.store.suspended_documents(), set())
        self.assertFalse(next(i for i in self.cloud.library() if i.id == self.paper.id).deleted)
        self.zot.review_action.assert_not_called()

    def test_missing_restore_destination_does_not_commit_unrecoverable_action(self):
        self.link['tablet_parent'] = 'missing-folder'
        key, _ = self.deleted()
        with self.assertRaisesRegex(PilotError, 'live destination'):
            self.store.resolve_deletion(key, 'restore', cloud=self.cloud, zotero=self.zot, scope=self.root.id)
        row = self.store.deletions()[0]
        self.assertEqual((row['status'], row['action']), ('pending', ''))
        self.store.resolve_deletion(key, 'pdf_only', cloud=self.cloud, zotero=self.zot, scope=self.root.id)
        self.assertEqual(self.store.deletions(), [])

    def test_legacy_failed_restore_can_change_choice_or_select_new_destination(self):
        self.link['tablet_parent'] = 'missing-folder'
        key, _ = self.deleted()
        with self.store.connect() as db:
            db.execute("UPDATE deletion_decisions SET status='applying',action='restore' WHERE id=?", (key,))
        self.store.resolve_deletion(key, 'restore', cloud=self.cloud, zotero=self.zot,
                                    scope=self.root.id, destination=self.root.id)
        self.assertEqual(self.store.deletions(), [])
        self.assertFalse(next(item for item in self.cloud.library() if item.id == self.paper.id).deleted)

    def test_pdf_only_and_everything_remain_distinct_native_trash_actions(self):
        for action in ("pdf_only", "everything"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as temporary:
                self.store = ReviewStore(Path(temporary))
                if action == "pdf_only":
                    key, removed = self.deleted()
                else:
                    removed = next(i for i in self.cloud.library() if i.id == self.paper.id)
                    key = self.store.confirm_deleted(removed, self.link, attachment="VIEW2345", parent="PARENT23", inventory=[])
                self.store.resolve_deletion(key, action, cloud=self.cloud, zotero=self.zot, scope=self.root.id)
                self.assertEqual(self.zot.review_action.call_args.args[1]["choice"], action)
                self.assertIn(self.paper.id, self.store.suspended_documents())
                calls = self.zot.review_action.call_count
                self.store.resolve_deletion(key, action, cloud=self.cloud, zotero=self.zot, scope=self.root.id)
                self.assertEqual(self.zot.review_action.call_count, calls)

    def test_new_tablet_revision_invalidates_deletion_decision(self):
        key, removed = self.deleted()
        self.cloud.update_item(removed.id, expected_hash=removed.hash, parent=self.root.id, scope_id=self.root.id, restore=True)
        with self.assertRaises(PilotError):
            self.store.resolve_deletion(key, "everything", cloud=self.cloud, zotero=self.zot, scope=self.root.id)
        self.zot.review_action.assert_not_called()
