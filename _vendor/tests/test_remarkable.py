"""Synthetic cloud/Paper Pro regressions; never use a real account."""
from dataclasses import replace
from concurrent.futures.process import BrokenProcessPool
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import uuid

import fitz
from rmscene import simple_text_document, write_blocks
from rmscene.crdt_sequence import CrdtSequenceItem
from rmscene.scene_items import Line, Point, Pen, PenColor, GlyphRange, Rectangle
from rmscene.scene_stream import SceneLineItemBlock, SceneGlyphItemBlock, SceneInfo
from rmscene.tagged_block_common import CrdtId, LwwValue

from note_organizer.extraction import extract
from note_organizer.paper_registry import Registry
from note_organizer.remarkable_cloud import CloudClient, CloudItem, CloudError, TokenVault, parse_index, atomic_write, _SafeRedirect
from note_organizer.remarkable_pdf import annotated_pdf, page_map, UnsupportedAnnotations
from note_organizer.remarkable_sync import (RemarkableSync, default_config, destination, folder_chain,
                                          safe_name, scoped_folders, folder_destination)
from note_organizer.service import Service
from note_organizer.storage import Store, DEFAULT_SETTINGS


def folder(name, parent=""):
    return CloudItem(str(uuid.uuid4()), "a" * 64, name, parent, "CollectionType", False, [])


def fixture(parent="", paper_size=(1404, 1872), deleted_ink=False):
    doc_id, page_id = str(uuid.uuid4()), str(uuid.uuid4())
    with fitz.open() as doc:
        page = doc.new_page(width=600, height=800)
        page.insert_text((60, 100), "A selected scientific result")
        pdf = doc.tobytes()
    blocks = list(simple_text_document("Typed margin note"))
    blocks.append(SceneInfo(LwwValue(CrdtId(1, 1), CrdtId(0, 11)), LwwValue(CrdtId(1, 2), True),
                            LwwValue(CrdtId(1, 3), True), paper_size))
    selected = GlyphRange(None, len("selected scientific"), "selected scientific", PenColor.GREEN,
                          [Rectangle(-700, 265, 315, 50)], color_rgba=(0, 255, 0, 255))
    blocks.append(SceneGlyphItemBlock(CrdtId(0, 11), CrdtSequenceItem(CrdtId(1, 100), CrdtId(0, 0), CrdtId(0, 0), 0, selected)))
    line = Line(PenColor.BLUE, Pen.FINELINER_2,
                [Point(-600, 440, 0, 0, 16, 90), Point(-550, 460, 0, 0, 16, 90)], 2, 0)
    blocks.append(SceneLineItemBlock(CrdtId(0, 11), CrdtSequenceItem(CrdtId(1, 101), CrdtId(1, 100), CrdtId(0, 0), int(deleted_ink), line)))
    stream = BytesIO()
    write_blocks(stream, blocks, options={"version": "3.1"})
    content = {"fileType": "pdf", "cPages": {"pages": [{"id": page_id, "redir": {"value": 0}, "idx": {"value": "ba"}}]}}
    bundle = {doc_id + ".pdf": pdf, doc_id + ".content": json.dumps(content).encode(),
              doc_id + "/" + page_id + ".rm": stream.getvalue()}
    item = CloudItem(doc_id, hashlib.sha256(b"".join(bundle.values())).hexdigest(), "Journal article", parent,
                     "DocumentType", False, [{"id": name} for name in bundle])
    return item, bundle


class RemarkableTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.store = Store(self.data / "library.sqlite3")
        self.pc = self.root / "PC"
        self.pc.mkdir()
        self.cloud_source = folder("Literature")
        self.cloud_folder = folder("Research", self.cloud_source.id)
        self.item, self.bundle = fixture(self.cloud_folder.id)
        self.engine = RemarkableSync(self.store, threading.Event(), Mock())
        self.engine.client = Mock()
        self.engine.client.library.return_value = [self.cloud_source, self.cloud_folder, self.item]
        self.engine.client.pdf_bundle.return_value = self.bundle
        self.config = {**default_config(self.data), "integration_enabled": True,
                       "enabled": True, "pc_root": str(self.pc)}
        self.store.set_preference("remarkable", self.config)

    def tearDown(self):
        self.temp.cleanup()

    def matched(self):
        target = self.pc / "Research"
        target.mkdir(exist_ok=True)
        self.engine.sync()
        return self.store.remarkable_items()[self.item.id]

    def test_pdf_keeps_source_unchanged_and_extracts_tablet_ink_highlight_and_typed_note(self):
        before = dict(self.bundle)
        output = self.root / "annotated.pdf"
        output.write_bytes(annotated_pdf(self.item, self.bundle))
        _, _, metadata, entries = extract(output, DEFAULT_SETTINGS)
        self.assertEqual(self.bundle, before)
        highlight = next(e for e in entries if e["kind"] == "highlight")
        self.assertEqual(highlight["quote"], "selected scientific")
        self.assertEqual(highlight["comment"], "")
        self.assertTrue(any(e["kind"] == "drawing" and e["image"] for e in entries))
        self.assertTrue(any(e["comment"] == "Typed margin note" for e in entries))
        self.assertTrue(metadata["warnings"])

    def test_paper_pro_grid_supported(self):
        item, bundle = fixture(paper_size=(1620, 2160))
        with fitz.open(stream=annotated_pdf(item, bundle), filetype="pdf") as pdf:
            self.assertEqual(len(list(pdf[0].annots())), 3)

    def test_deleted_stroke_not_resurrected(self):
        item, bundle = fixture(deleted_ink=True)
        with fitz.open(stream=annotated_pdf(item, bundle), filetype="pdf") as pdf:
            self.assertNotIn("Ink", [a.type[1] for a in pdf[0].annots()])

    def test_cpages_reordered_pages_and_added_page_override_stale_legacy_map(self):
        content = {"cPages": {"pages": [{"id": "b", "idx": {"value": "bb"}},
                   {"id": "a", "idx": {"value": "ba"}, "redir": {"value": 1}}]},
                   "redirectionPageMap": [0, 1]}
        self.assertEqual(page_map(content, 2), [("a", 1), ("b", -1)])
        self.assertEqual(page_map({"pages": ["a", "b"], "redirectionPageMap": [1, 0]}, 2), [("a", 1), ("b", 0)])

    def test_invalid_map_and_unsupported_layer_fail_closed(self):
        with self.assertRaises(UnsupportedAnnotations):
            page_map({"pages": ["a"], "redirectionPageMap": [99]}, 1)
        name = next(n for n in self.bundle if n.endswith(".rm"))
        self.bundle[name] = b"unsupported scene"
        with self.assertRaises(UnsupportedAnnotations):
            annotated_pdf(self.item, self.bundle)

    def test_exact_folder_match_and_incremental_sync(self):
        saved = self.matched()
        self.assertEqual(saved["state"], "synced")
        self.assertEqual(Path(saved["path"]).parent, self.pc / "Research")
        self.engine.sync()
        self.engine.client.pdf_bundle.assert_called_once()
        self.assertEqual(len(self.store.projects()), 1)

    def test_renderer_upgrade_refreshes_synced_pdf_with_unchanged_cloud_hash(self):
        saved = self.matched()
        next_revision = saved["render_revision"] + 1
        with patch("note_organizer.remarkable_sync.RENDER_REVISION", next_revision):
            self.engine.sync()
            refreshed = self.store.remarkable_items()[self.item.id]
            self.assertEqual(refreshed["hash"], saved["hash"])
            self.assertEqual(refreshed["render_revision"], next_revision)
            self.assertEqual(self.engine.client.pdf_bundle.call_count, 2)
            self.engine.sync()
            self.assertEqual(self.engine.client.pdf_bundle.call_count, 2)

    def test_renderer_upgrade_retries_conversion_review_with_unchanged_cloud_hash(self):
        (self.pc / "Research").mkdir()
        self.bundle[next(n for n in self.bundle if n.endswith(".rm"))] = b"unsupported"
        self.engine.sync()
        saved = self.store.remarkable_items()[self.item.id]
        next_revision = saved["render_revision"] + 1
        with patch("note_organizer.remarkable_sync.RENDER_REVISION", next_revision):
            self.engine.sync()
            refreshed = self.store.remarkable_items()[self.item.id]
            self.assertEqual(refreshed["hash"], saved["hash"])
            self.assertEqual(refreshed["render_revision"], next_revision)
            self.assertEqual(self.engine.client.pdf_bundle.call_count, 2)
            self.engine.sync()
            self.assertEqual(self.engine.client.pdf_bundle.call_count, 2)

    def test_missing_folder_goes_to_review_without_creating_matching_pc_folder(self):
        self.engine.sync()
        saved = self.store.remarkable_items()[self.item.id]
        self.assertEqual(saved["state"], "review")
        self.assertTrue(Path(saved["review_pdf"]).exists())
        self.assertFalse((self.pc / "Research").exists())
        self.assertFalse(self.store.projects())

    def test_review_excluded_even_under_watched_parent_and_direct_events(self):
        self.engine.sync()
        saved = self.store.remarkable_items()[self.item.id]
        self.store.add_project(self.root)
        service = Service(self.store)
        service.sync()
        self.assertFalse(self.store.documents())
        service.enqueue(saved["review_pdf"])
        self.assertFalse(service.pending)
        self.assertFalse(service.projects_for(saved["review_pdf"]))

    def test_mapping_promotes_review_and_skips_native_notebooks(self):
        notebook = replace(self.item, id=str(uuid.uuid4()), name="Notebook", files=[])
        self.engine.client.library.return_value.append(notebook)
        self.engine.sync()
        self.config["mappings"] = {self.cloud_folder.id: str(self.pc)}
        self.store.set_preference("remarkable", self.config)
        self.engine.sync()
        saved = self.store.remarkable_items()[self.item.id]
        self.assertEqual(saved["state"], "synced")
        self.assertEqual(Path(saved["path"]).parent, self.pc)
        self.assertNotIn(notebook.id, self.store.remarkable_items())

    def test_bad_annotation_format_retains_raw_download_in_review(self):
        self.config["mappings"] = {self.cloud_folder.id: str(self.pc)}
        self.store.set_preference("remarkable", self.config)
        self.bundle[next(n for n in self.bundle if n.endswith(".rm"))] = b"bad"
        self.engine.sync()
        saved = self.store.remarkable_items()[self.item.id]
        self.assertEqual(saved["state"], "review")
        self.assertIn("conversion", saved["reason"])
        self.assertTrue((Path(saved["review_pdf"]).parent / "source.json").exists())
        self.assertFalse(self.store.projects())

    def test_local_changes_never_overwritten_and_cloud_copy_stays_in_review(self):
        saved = self.matched()
        path = Path(saved["path"])
        path.write_bytes(b"Local edited version")
        self.item.hash = "b" * 64
        self.engine.sync()
        reviewed = self.store.remarkable_items()[self.item.id]
        self.assertEqual(path.read_bytes(), b"Local edited version")
        self.assertEqual(reviewed["state"], "review")
        self.engine.sync()
        self.assertEqual(self.store.remarkable_items()[self.item.id]["state"], "review")
        self.assertEqual(path.read_bytes(), b"Local edited version")

    def test_failure_retains_last_successful_manifest_and_pdf(self):
        saved = self.matched()
        self.item.hash = "b" * 64
        self.engine.client.pdf_bundle.side_effect = CloudError("Network unavailable")
        self.engine.sync()
        self.assertEqual(self.store.remarkable_items()[self.item.id], saved)
        self.assertTrue(Path(saved["path"]).exists())
        self.assertEqual(self.store.preference("remarkable_last_sync")["failed"], 1)

    def test_render_process_crash_preserves_successful_pdf_and_retries_automatically(self):
        saved = self.matched()
        path = Path(saved["path"])
        previous_bytes = path.read_bytes()
        self.item.hash = "b" * 64
        self.engine.renderer = Mock(side_effect=[BrokenProcessPool("Synthetic PDF process crash"),
                                                 annotated_pdf(self.item, self.bundle)])
        self.assertFalse(self.engine.sync())
        self.assertEqual(self.store.remarkable_items()[self.item.id], saved)
        self.assertEqual(path.read_bytes(), previous_bytes)
        self.assertEqual(self.store.preference("remarkable_last_sync")["failed"], 1)
        self.assertEqual(self.store.preference("remarkable_last_sync")["reviewed"], 0)
        self.assertFalse((Path(self.config["review_dir"]) / self.item.id).exists())
        self.assertTrue(self.engine.sync())
        self.assertEqual(self.engine.renderer.call_count, 2)
        updated = self.store.remarkable_items()[self.item.id]
        self.assertEqual(updated["state"], "synced")
        self.assertEqual(updated["hash"], self.item.hash)
        self.assertEqual(self.store.preference("remarkable_last_sync")["failed"], 0)

    def test_first_download_process_crash_remains_retryable_without_a_review_manifest(self):
        (self.pc / "Research").mkdir()
        self.engine.renderer = Mock(side_effect=BrokenProcessPool("Synthetic PDF process crash"))
        self.assertFalse(self.engine.sync())
        self.assertNotIn(self.item.id, self.store.remarkable_items())
        self.assertEqual(self.store.preference("remarkable_last_sync")["failed"], 1)
        self.engine.renderer = Mock(wraps=annotated_pdf)
        self.assertTrue(self.engine.sync())
        self.assertEqual(self.store.remarkable_items()[self.item.id]["state"], "synced")

    def test_interrupted_adoption_repairs_from_retained_source_without_download(self):
        (self.pc / "Research").mkdir()
        with patch("note_organizer.cloud_notes.save_cloud_notes", side_effect=RuntimeError("Synthetic registration failure")):
            self.assertFalse(self.engine.sync())
        self.assertNotIn(self.item.id, self.store.remarkable_items())
        self.assertFalse(self.store.entries())
        self.assertTrue(self.engine.sync(force=True))
        self.engine.client.pdf_bundle.assert_called_once()
        self.assertTrue(self.store.remarkable_items()[self.item.id]["identity_registered"])
        self.assertEqual(len(Registry(self.store).local_for_cloud(self.item.id)), 2)
        self.assertEqual(len(self.store.entries()), 3)

    def test_worker_queues_repaired_download_even_when_rendered_bytes_are_unchanged(self):
        (self.pc / "Research").mkdir()
        service = Service(self.store)
        service.remarkable = self.engine
        service.watch = Mock()
        with patch("note_organizer.cloud_notes.save_cloud_notes", side_effect=RuntimeError("Synthetic registration failure")):
            service.check_cloud()
        self.assertFalse(service.cloud_paths)
        self.assertTrue(service.check_cloud())
        saved = self.store.remarkable_items()[self.item.id]
        self.assertEqual(service.cloud_paths, {saved["path"]})
        self.assertTrue(saved["identity_registered"])
        self.engine.client.pdf_bundle.assert_called_once()

    def test_remote_deletion_retains_local_pdf(self):
        saved = self.matched()
        self.item.deleted = True
        self.engine.sync()
        self.assertTrue(Path(saved["path"]).exists())
        self.assertEqual(self.store.remarkable_items()[self.item.id]["state"], "cloud unavailable")

    def test_cloud_rename_retains_main_original_and_stable_note_ids(self):
        saved = self.matched()
        service = Service(self.store)
        service.sync()
        count = len(self.store.entries())
        doc_id = self.store.documents()[0]["id"]
        self.item.name = "Renamed paper"
        self.item.hash = "c" * 64
        self.engine.sync()
        service.sync()
        self.assertEqual(len(self.store.entries()), count)
        self.assertEqual(self.store.documents()[0]["id"], doc_id)
        self.assertTrue(Path(saved["path"]).exists())
        self.assertEqual(self.store.remarkable_items()[self.item.id]["path"], saved["path"])

    def test_duplicate_cloud_names_need_review_before_merging_independent_tablet_notes(self):
        other, bundle = fixture(self.cloud_folder.id)
        self.engine.client.library.return_value.append(other)
        self.engine.client.pdf_bundle.side_effect = lambda i: bundle if i.id == other.id else self.bundle
        self.matched()
        items = list(self.store.remarkable_items().values())
        self.assertEqual(sorted(item["state"] for item in items), ["review", "synced"])
        self.assertEqual(len(list((self.pc / "Research").glob("*.pdf"))), 1)
        self.assertTrue(Path(next(item for item in items if item["state"] == "review")["review_pdf"]).exists())

    def test_hierarchy_cycle_or_missing_parent_stops_before_downloads(self):
        self.cloud_folder.parent = self.cloud_folder.id
        with self.assertRaises(CloudError):
            self.engine.sync()
        self.engine.client.pdf_bundle.assert_not_called()

    def test_folder_matching_uses_full_path_and_rejects_traversal(self):
        (self.pc / "Research").mkdir()
        child = folder("Subfolder", self.cloud_folder.id)
        (self.pc / "Subfolder").mkdir()
        self.assertIsNone(destination([self.cloud_folder, child], self.config))
        child.name = ".."
        self.assertIsNone(destination([child], self.config))

    def test_explicit_mapping_outside_sorted_is_reviewed_without_writing_to_it(self):
        outside = self.root / "Outside Sorted"
        outside.mkdir()
        self.config["mappings"] = {self.cloud_folder.id: str(outside)}
        self.store.set_preference("remarkable", self.config)
        self.engine.sync()
        self.assertEqual(self.store.remarkable_items()[self.item.id]["state"], "review")
        self.assertFalse(list(outside.iterdir()))
        catalog = self.store.preference("remarkable_folders", [])
        record = next(folder for folder in catalog if folder["id"] == self.cloud_folder.id)
        self.assertIsNone(folder_destination(record, catalog, self.engine.config()))

    def test_mapping_requires_explicit_sorted_root_before_downloading(self):
        self.config["pc_root"] = ""
        self.config["mappings"] = {self.cloud_folder.id: str(self.pc)}
        self.store.set_preference("remarkable", self.config)
        self.engine.sync()
        self.assertEqual(self.store.remarkable_items()[self.item.id]["state"], "review")
        self.assertFalse(list(self.pc.iterdir()))

    def test_pause_and_cancellation_do_not_download(self):
        self.config["enabled"] = False
        self.store.set_preference("remarkable", self.config)
        self.assertFalse(self.engine.sync())
        self.engine.client.library.assert_not_called()
        self.engine.stop.set()
        with self.assertRaises(InterruptedError):
            self.engine.sync(force=True)

    def test_cloud_blob_integrity_and_invalid_index(self):
        client = CloudClient(cache_dir=self.data / "cache")
        client.get = Mock(return_value=b"corrupt download")
        with self.assertRaises(CloudError):
            client.blob("a" * 64, "document.pdf")
        with self.assertRaises(CloudError):
            parse_index(b"5\n")
        with self.assertRaises(CloudError):
            parse_index(b"4\nbad\n")

    def test_cloud_client_fetches_v4_root_and_v3_files_without_library_writes(self):
        doc_id = self.item.id
        metadata = json.dumps({"visibleName": "Paper", "type": "DocumentType", "parent": ""}).encode()
        meta_hash = hashlib.sha256(metadata).hexdigest()
        index = f"3\n{meta_hash}:0:{doc_id}.metadata:0:{len(metadata)}\n"
        index_hash = hashlib.sha256(bytes.fromhex(meta_hash)).hexdigest()
        root = f"4\n{index_hash}:80000000:{doc_id}:1:1\n"
        root_hash = hashlib.sha256(root.encode()).hexdigest()
        client = CloudClient("secret")
        client.user_token = "session"
        def get(url, filename=None):
            if url.endswith("/v4/root"):
                return json.dumps({"hash": root_hash}).encode()
            if filename == "root.docSchema":
                return root.encode()
            if filename == doc_id + ".docSchema":
                return index.encode()
            return metadata
        client.get = Mock(side_effect=get)
        self.assertEqual(client.library()[0].name, "Paper")
        self.assertTrue(all("/v4/root" in call.args[0] or "/v3/files/" in call.args[0] for call in client.get.call_args_list))

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_pairing_token_encrypted_on_disk(self):
        vault = TokenVault(self.data / "credential.dpapi")
        vault.save("synthetic-device-token")
        self.assertNotIn(b"synthetic-device-token", vault.path.read_bytes())
        self.assertEqual(vault.load(), "synthetic-device-token")
        vault.forget()
        self.assertEqual(vault.load(), "")

    def test_untrusted_names_safe_for_windows(self):
        self.assertEqual(safe_name("CON.pdf"), "_CON")
        self.assertNotIn("/", safe_name("../../paper.pdf"))

    def test_literature_scope_maps_directly_to_sorted_and_ignores_other_folders(self):
        self.item.parent = self.cloud_source.id
        other_folder = folder("Personal")
        other_pdf, other_bundle = fixture(other_folder.id)
        self.engine.client.library.return_value.extend([other_folder, other_pdf])
        self.config["source_folder_name"] = "Literature"
        self.store.set_preference("remarkable", self.config)
        self.engine.sync()
        saved = self.store.remarkable_items()[self.item.id]
        self.assertEqual(Path(saved["path"]).parent, self.pc)
        self.assertNotIn(other_pdf.id, self.store.remarkable_items())
        self.assertEqual(self.store.preference("remarkable")["source_folder_id"], self.cloud_source.id)
        self.engine.client.pdf_bundle.assert_called_once_with(self.item)

    def test_ambiguous_literature_requires_explicit_selection_and_keeps_folder_choices(self):
        self.engine.client.library.return_value.append(folder("Literature"))
        self.config["source_folder_name"] = "Literature"
        self.store.set_preference("remarkable", self.config)
        with self.assertRaises(CloudError):
            self.engine.sync()
        self.engine.client.pdf_bundle.assert_not_called()
        self.assertEqual(len(self.store.preference("remarkable_folders")), 3)

    def test_blank_source_never_downloads_the_whole_library(self):
        self.config.update(source_folder_id="", source_folder_name="")
        self.store.set_preference("remarkable", self.config)
        with self.assertRaisesRegex(CloudError, "Choose a cloud source"):
            self.engine.sync(force=True)
        self.engine.client.library.assert_not_called()
        self.engine.client.pdf_bundle.assert_not_called()

    def test_selected_source_under_trashed_or_deleted_ancestor_is_rejected(self):
        ancestor = folder("Archive")
        self.cloud_source.parent = ancestor.id
        self.engine.client.library.return_value.append(ancestor)
        self.config["source_folder_id"] = self.cloud_source.id
        self.store.set_preference("remarkable", self.config)
        for deleted, parent in ((True, ""), (False, "trash")):
            with self.subTest(deleted=deleted, parent=parent):
                ancestor.deleted, ancestor.parent = deleted, parent
                with self.assertRaisesRegex(CloudError, "source folder is unavailable"):
                    self.engine.sync()
                self.engine.client.pdf_bundle.assert_not_called()

    def test_folder_catalog_scope_and_destinations_match_download_routing(self):
        nested = folder("Nested", self.cloud_folder.id)
        outside = folder("Research")
        self.engine.client.library.return_value.extend([nested, outside])
        self.engine.sync(listing_only=True)
        catalog = self.store.preference("remarkable_folders")
        config = self.store.preference("remarkable")
        scoped = scoped_folders(catalog, self.cloud_source.id)
        self.assertEqual({f["id"] for f in scoped}, {self.cloud_source.id, self.cloud_folder.id, nested.id})
        self.assertFalse(scoped_folders(catalog, ""))
        nested_record = next(f for f in scoped if f["id"] == nested.id)
        self.assertEqual(nested_record["parent"], self.cloud_folder.id)
        self.assertEqual(nested_record["ancestor_ids"], [self.cloud_source.id, self.cloud_folder.id])
        self.assertIsNone(folder_destination(nested_record, catalog, config))
        research = self.pc / "Research"
        (research / "Nested").mkdir(parents=True)
        self.assertEqual(folder_destination(nested_record, catalog, config), research / "Nested")
        config["mappings"] = {self.cloud_folder.id: str(self.pc)}
        self.assertIsNone(folder_destination(nested_record, catalog, config))
        (self.pc / "Nested").mkdir()
        self.assertEqual(folder_destination(nested_record, catalog, config), self.pc / "Nested")
        source_record = next(f for f in scoped if f["id"] == self.cloud_source.id)
        self.assertEqual(folder_destination(source_record, catalog, config), self.pc)
        outside_record = next(f for f in catalog if f["id"] == outside.id)
        self.assertIsNone(folder_destination(outside_record, catalog, config))

    def test_unchanged_conversion_review_retries_only_on_manual_sync_or_route_change(self):
        (self.pc / "Research").mkdir()
        layer = next(n for n in self.bundle if n.endswith(".rm"))
        self.bundle[layer] = b"unsupported"
        self.engine.sync()
        saved = self.store.remarkable_items()[self.item.id]
        self.assertEqual(saved["reason_kind"], "conversion")
        self.engine.sync()
        self.engine.client.pdf_bundle.assert_called_once()
        self.engine.sync(force=True)
        self.assertEqual(self.engine.client.pdf_bundle.call_count, 2)
        self.config["mappings"] = {self.cloud_folder.id: str(self.pc)}
        self.store.set_preference("remarkable", self.config)
        self.engine.sync()
        self.assertEqual(self.engine.client.pdf_bundle.call_count, 3)
        self.engine.sync()
        self.assertEqual(self.engine.client.pdf_bundle.call_count, 3)

    def test_unchanged_local_conflict_review_is_not_downloaded_each_cycle(self):
        saved = self.matched()
        Path(saved["path"]).write_bytes(b"Local edited version")
        self.engine.sync()
        review = self.store.remarkable_items()[self.item.id]
        self.assertEqual(review["reason_kind"], "local_conflict")
        self.engine.sync()
        self.assertEqual(self.engine.client.pdf_bundle.call_count, 2)
        self.engine.sync(force=True)
        self.assertEqual(self.engine.client.pdf_bundle.call_count, 3)
        self.assertEqual(Path(saved["path"]).read_bytes(), b"Local edited version")

    def test_removing_a_local_conflict_allows_automatic_retry(self):
        saved = self.matched()
        path = Path(saved["path"])
        path.write_bytes(b"Local edited version")
        self.engine.sync()
        path.unlink()
        self.engine.sync()
        self.assertEqual(self.engine.client.pdf_bundle.call_count, 3)
        self.assertEqual(self.store.remarkable_items()[self.item.id]["state"], "synced")
        self.assertTrue(path.exists())

    def test_legacy_conversion_review_migrates_without_redownloading(self):
        (self.pc / "Research").mkdir()
        self.bundle[next(n for n in self.bundle if n.endswith(".rm"))] = b"unsupported"
        self.engine.sync()
        saved = self.store.remarkable_items()[self.item.id]
        saved.pop("reason_kind")
        saved.pop("route_fingerprint")
        self.store.save_remarkable_item(self.item.id, saved)
        self.engine.sync()
        self.engine.client.pdf_bundle.assert_called_once()
        self.assertEqual(self.store.remarkable_items()[self.item.id]["reason_kind"], "conversion")

    def test_ancestor_rename_refreshes_review_path_without_redownloading(self):
        self.engine.sync()
        self.cloud_folder.name = "New research name"
        self.engine.sync()
        saved = self.store.remarkable_items()[self.item.id]
        self.assertEqual(saved["cloud_path"], "/Literature/New research name/Journal article")
        self.engine.client.pdf_bundle.assert_called_once()

    def test_ancestor_rename_refreshes_synced_path_with_explicit_mapping(self):
        self.config["mappings"] = {self.cloud_folder.id: str(self.pc)}
        self.store.set_preference("remarkable", self.config)
        self.engine.sync()
        self.cloud_folder.name = "New research name"
        self.engine.sync()
        saved = self.store.remarkable_items()[self.item.id]
        self.assertEqual(saved["cloud_path"], "/Literature/New research name/Journal article")
        self.engine.client.pdf_bundle.assert_called_once()

    def test_watched_parent_is_reused_without_creating_duplicate_projects(self):
        pid = self.store.add_project(self.pc)
        self.matched()
        self.assertEqual([p["id"] for p in self.store.projects()], [pid])

    def test_atomic_cloud_write_refuses_changed_or_unowned_file(self):
        path = self.pc / "managed.pdf"
        path.write_bytes(b"Local changes")
        with self.assertRaises(CloudError):
            atomic_write(path, b"Cloud", expected_digest="a" * 64)
        with self.assertRaises(CloudError):
            atomic_write(path, b"Cloud", require_absent=True)
        self.assertEqual(path.read_bytes(), b"Local changes")

    def test_redirect_strips_credentials_for_different_host_and_refuses_http(self):
        from urllib.request import Request
        request = Request("https://internal.cloud.remarkable.com/files", headers={"Authorization": "Bearer synthetic"})
        redirected = _SafeRedirect().redirect_request(request, None, 302, "", {}, "https://example.com/file")
        self.assertFalse(redirected.has_header("Authorization"))
        with self.assertRaises(CloudError):
            _SafeRedirect().redirect_request(request, None, 302, "", {}, "http://example.com/file")

    def test_cloud_service_runs_automatically_after_restart(self):
        service = Service(self.store, interval=.02)
        service.remarkable = Mock()
        reached = threading.Event()
        service.remarkable.sync.side_effect = lambda **kwargs: reached.set() or False
        service.start()
        try:
            self.assertTrue(reached.wait(3))
        finally:
            service.stop()
            service.thread.join(5)
        self.assertFalse(service.thread.is_alive())


if __name__ == "__main__":
    unittest.main()
