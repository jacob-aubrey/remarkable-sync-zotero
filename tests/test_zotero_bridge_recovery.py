"""Failure injection for retained originals, shared homes and source reuse."""
from dataclasses import replace
import hashlib
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from note_organizer import zotero_bridge
from tests.test_remarkable import folder
from tests import test_zotero_bridge as fixtures


class BridgeRecoveryTests(unittest.TestCase):
    setUp = fixtures.BridgeTests.setUp
    add_desktop = fixtures.BridgeTests.add_desktop
    tablet = fixtures.BridgeTests.tablet

    def test_mupdf_format_error_does_not_block_another_paper(self):
        bad, _ = self.tablet()
        good, _ = self.tablet()
        render = zotero_bridge.annotated_pdf
        def fail_one(item, *args, **kwargs):
            if item.id == bad.id:
                raise RuntimeError('code=4: source object number out of range')
            return render(item, *args, **kwargs)
        with patch.object(zotero_bridge, 'annotated_pdf', side_effect=fail_one):
            result = self.engine.run(apply=True)
        self.assertTrue(any(row.get('cloud') == bad.id for row in result['review']))
        self.assertTrue(self.state.read('documents')[good.id]['revision'])

    def test_unexpected_runtime_error_is_not_misreported_as_a_pdf_issue(self):
        self.tablet()
        with patch.object(zotero_bridge, 'annotated_pdf', side_effect=RuntimeError('Unexpected programming error')):
            with self.assertRaisesRegex(RuntimeError, 'Unexpected programming'):
                self.engine.run(apply=True)

    def test_failed_original_staging_is_not_committed_by_another_paper(self):
        self.add_desktop(key="FAIL2345", parent="FAILP234")
        self.add_desktop()
        original_write = zotero_bridge.atomic_write
        failed = False

        def fail_once(path, data, **kwargs):
            nonlocal failed
            if path.parent.name == "originals" and not failed:
                failed = True
                raise OSError("temporary disk error")
            return original_write(path, data, **kwargs)

        with patch.object(zotero_bridge, "atomic_write", side_effect=fail_once):
            first = self.engine.run(apply=True)
        self.assertTrue(any("temporary disk error" in row["reason"] for row in first["review"]))
        self.assertEqual({row["source_key"] for row in self.state.read("documents").values()}, {"SOURCE23"})
        self.assertFalse(self.engine.run(apply=True)["review"])
        self.assertEqual(self.cloud.uploads, 2)

    def test_legacy_incomplete_registration_restages_unchanged_original(self):
        self.add_desktop()
        baseline = hashlib.sha256(self.zot.file_path("SOURCE23").read_bytes()).hexdigest()
        identity = "15332b3d-1876-4608-b987-68e9c835ea28"
        self.state.write("documents", {identity: {"origin": "zotero", "source_key": "SOURCE23",
            "parent": "PARENT23", "collection": "ROOT2345", "baseline": baseline,
            "uploaded": False, "name": "Retained paper"}})
        result = self.engine.run(apply=True)
        self.assertFalse(result["review"])
        self.assertTrue((self.state.directory / "originals" / (baseline + ".pdf")).is_file())
        self.assertEqual(self.cloud.uploads, 1)

    def test_deleted_folder_ancestry_does_not_break_unrelated_selected_root(self):
        deleted = replace(folder("Deleted", "trash"), deleted=True)
        child = folder("Historical child", deleted.id)
        self.cloud.records.update({deleted.id: deleted, child.id: child})
        self.tablet()
        self.assertFalse(self.engine.run(apply=True)["review"])
        self.assertEqual(len(self.state.read("folders")), 1)

    def test_live_child_of_deleted_folder_in_scope_is_not_imported(self):
        deleted = replace(folder("Deleted", self.cloud.root.id), deleted=True)
        child = folder("Historical child", deleted.id)
        self.cloud.records.update({deleted.id: deleted, child.id: child})
        self.assertFalse(self.engine.run(apply=True)["review"])
        self.assertEqual(len(self.state.read("folders")), 1)

    def test_unchanged_retained_revision_never_downloads_pdf_or_strokes_again(self):
        self.tablet()
        with patch.object(self.cloud, "pdf_bundle", wraps=self.cloud.pdf_bundle) as download:
            self.assertFalse(self.engine.run(apply=True)["review"])
            self.assertFalse(self.engine.run(apply=True)["review"])
        self.assertEqual(download.call_count, 1)

    def test_missing_tablet_origin_link_is_unavailable_and_recovers(self):
        item, _ = self.tablet()
        self.engine.run(apply=True)
        self.cloud.records.pop(item.id)
        result = self.engine.run(apply=True)
        self.assertTrue(any(row.get("cloud") == item.id and "unavailable" in row["reason"] for row in result["review"]))
        self.assertTrue(self.state.read("documents")[item.id]["unavailable"])
        self.cloud.records[item.id] = item
        self.assertFalse(self.engine.run(apply=True)["review"])
        self.assertNotIn("unavailable", self.state.read("documents")[item.id])

    def test_moving_one_pdf_keeps_membership_needed_by_sibling(self):
        self.add_desktop()
        row = {"parent": "PARENT23", "collection": "ROOT2345", "source_key": "SOURCE23"}
        self.state.write("documents", {"main": row, "si": {**row, "source_key": "SUPPL234"}})
        self.zot.review_action = lambda *args: {}
        self.engine.set_desktop_document(row, "Moved", "TARGET23", {})
        self.assertEqual(set(self.zot.items["PARENT23"]["collections"]), {"ROOT2345", "TARGET23"})

    def test_long_pdf_renderer_receives_active_cancellation(self):
        self.tablet()
        self.cloud.stop = threading.Event()

        def render(item, bundle, cancelled, **options):
            self.cloud.stop.set()
            self.assertTrue(cancelled())
            raise InterruptedError("cancel inside rendering")

        with patch.object(zotero_bridge, "annotated_pdf", side_effect=render):
            with self.assertRaisesRegex(InterruptedError, "inside rendering"):
                self.engine.run(apply=True)
        self.assertEqual(self.zot.uploads, 0)

    def test_linked_file_uses_managed_view_without_adopting_original(self):
        self.add_desktop()
        original = self.zot.file_path("SOURCE23").read_bytes()
        self.zot.items["SOURCE23"]["linkMode"] = "linked_file"
        self.zot.review_action = lambda *args: self.fail("Linked original must never be adopted")
        self.engine.review_policy = SimpleNamespace(suspended_documents=lambda: set(), approval=lambda *args: {})
        self.assertFalse(self.engine.run(apply=True)["review"])
        row = next(iter(self.state.read("documents").values()))
        self.assertNotEqual(row["view_key"], "SOURCE23")
        self.assertEqual(self.zot.file_path("SOURCE23").read_bytes(), original)

    def test_legacy_linked_file_adoption_recovers_to_managed_view(self):
        self.add_desktop()
        self.zot.items["SOURCE23"]["linkMode"] = "linked_file"
        def fail_first_publication(*args, **kwargs):
            raise OSError("interrupted publication")

        self.engine.review_policy = SimpleNamespace(suspended_documents=lambda: set(), approval=lambda *args: {})
        with patch.object(zotero_bridge, "publish", side_effect=fail_first_publication):
            self.engine.run(apply=True)
        documents = self.state.read("documents")
        identity, row = next(iter(documents.items()))
        row["view_key"] = "SOURCE23"
        self.state.write("documents", documents)
        self.zot.review_action = lambda *args: self.fail("Linked original must never be adopted")
        self.assertFalse(self.engine.run(apply=True)["review"])
        self.assertNotEqual(self.state.read("documents")[identity]["view_key"], "SOURCE23")


if __name__ == "__main__":
    unittest.main()
