"""Scoped two-way additions and tablet-only annotation return contracts."""
import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

import pymupdf

from note_organizer.remarkable_cloud import CloudError, CloudItem
from note_organizer.zotero_bridge import Bridge, BridgeState
from note_organizer.zotero_pilot import PilotError
from tests.test_zotero_pilot import FakeZotero
from tests.test_remarkable import fixture, folder


class BridgeZotero(FakeZotero):
    def __init__(self, root):
        super().__init__()
        self.root = root
        self.collections["ROOT2345"] = {"key": "ROOT2345", "name": "Literature", "parentCollection": False}

    def collections(self):
        return list(copy.deepcopy(self.collection_records).values())

    # FakeZotero uses .collections as a dictionary. Keep both adapter interfaces.
    def get(self, kind, key):
        return copy.deepcopy((self.collection_records if kind == "collection" else self.items).get(key))

    def create(self, kind, record):
        self.writes += 1
        (self.collection_records if kind == "collection" else self.items)[record["key"]] = copy.deepcopy(record)
        return self.get(kind, record["key"])

    def collection_items(self, key):
        return [copy.deepcopy(r) for r in self.items.values() if key in r.get("collections", [])]

    def children(self, key):
        return [copy.deepcopy(r) for r in self.items.values() if r.get("parentItem") == key]

    def file_path(self, key):
        return self.root / (key + ".pdf")

    def update(self, record):
        self.writes += 1
        self.items[record["key"]] = copy.deepcopy(record)


class FakeCloud:
    def __init__(self):
        self.root = folder("Tablet literature")
        self.records, self.bundles = {self.root.id: self.root}, {}
        self.folder_writes = self.uploads = 0
        self.lose_upload = self.lose_folder = False

    def library(self):
        return list(copy.deepcopy(self.records).values())

    def create_folder(self, name, parent, identity, scope):
        assert scope == self.root.id
        self.folder_writes += 1
        row = CloudItem(identity, "b" * 64, name, parent, "CollectionType", False, [])
        self.records[identity] = row
        if self.lose_folder:
            self.lose_folder = False
            raise OSError("Connection lost after commit")
        return copy.deepcopy(row)

    def upload_pdf(self, data, name, parent, identity, scope):
        assert scope == self.root.id and identity not in self.records
        self.uploads += 1
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            content = {"fileType": "pdf", "pages": [str(uuid.uuid4()) for _ in doc]}
        bundle = {identity + ".pdf": data, identity + ".content": json.dumps(content).encode()}
        self.add(CloudItem(identity, "c" * 64, name, parent, "DocumentType", False,
                           [{"id": k} for k in bundle]), bundle)
        if self.lose_upload:
            self.lose_upload = False
            raise OSError("Connection lost after commit")
        return copy.deepcopy(self.records[identity])

    def add(self, item, bundle):
        self.records[item.id], self.bundles[item.id] = item, bundle

    def pdf_bundle(self, item):
        return copy.deepcopy(self.bundles[item.id])

    def update_item(self, identity, *, expected_hash, scope_id, tags=None, page_tags=None):
        assert self.records[identity].hash == expected_hash and scope_id == self.root.id
        content = json.loads(self.bundles[identity][identity + '.content'])
        if tags is not None:
            content['tags'] = [{'name': tag} for tag in tags]
        if page_tags is not None:
            content['pageTags'] = [{'name': tag, 'pageId': pid} for pid, values in page_tags.items() for tag in values]
        self.bundles[identity][identity + '.content'] = json.dumps(content).encode()
        self.records[identity] = replace(self.records[identity], hash=hashlib.sha256(json.dumps(content).encode()).hexdigest())
        return copy.deepcopy(self.records[identity])


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.zot = BridgeZotero(self.root)
        self.zot.collection_records = self.zot.collections
        del self.zot.collections  # Expose the adapter method.
        self.cloud = FakeCloud()
        self.state = BridgeState(self.root / "state")
        self.engine = Bridge(self.zot, self.cloud, self.state, "ROOT2345", self.cloud.root.id)

    def add_desktop(self, key="SOURCE23", parent="PARENT23", collection="ROOT2345", text="desktop original"):
        self.zot.items[parent] = {"key": parent, "itemType": "thesis", "title": "Reviewed dissertation",
            "university": "Example University", "collections": [collection], "tags": [{"tag": "Reviewed"}]}
        self.zot.items[key] = {"key": key, "parentItem": parent, "itemType": "attachment",
            "title": "Main PDF", "contentType": "application/pdf", "linkMode": "imported_file"}
        with pymupdf.open() as doc:
            page = doc.new_page()
            page.insert_text((60, 100), text)
            doc.save(self.zot.file_path(key))

    def tablet(self, **kwargs):
        item, bundle = fixture(self.cloud.root.id, **kwargs)
        self.cloud.add(item, bundle)
        return item, bundle

    def test_preview_has_no_external_writes(self):
        self.add_desktop()
        self.tablet()
        self.engine.run()
        self.assertEqual((self.cloud.uploads, self.cloud.folder_writes, self.zot.writes), (0, 0, 0))
        self.assertIsNone(self.state.read("documents"))

    def test_tablet_paper_returns_annotations_and_repeats_without_duplicate_or_reupload(self):
        item, _ = self.tablet()
        self.engine.saved_annotations = lambda ck: ({"ocr": "savedscientifictranscript"},)
        result = self.engine.run(apply=True)
        key = result["actions"][-1]["attachment_key"]
        self.assertIn("savedscientifictranscript", self.zot.index_contents[key])
        self.assertIn("Typed margin note", self.zot.index_contents[key])
        self.engine.run(apply=True)
        self.assertEqual((len(self.zot.items), self.zot.uploads, self.cloud.uploads), (2, 1, 0))
        row = self.state.read("documents")[item.id]
        self.assertEqual(row["revision"], item.hash)
        self.assertTrue(row["group_highlights"])
        self.assertTrue(row["handwriting_cards"])

    def test_existing_unchanged_view_does_not_republish_for_annotation_features(self):
        item, _ = self.tablet()
        self.engine.run(apply=True)
        links = self.state.read("documents")
        row = links[item.id]
        row.pop("group_highlights")
        row.pop("handwriting_cards")
        row["rendered"]["input"] = row["rendered"]["input"][:2]
        self.state.write("documents", links)

        with patch("note_organizer.zotero_bridge.annotated_pdf") as render:
            self.engine.run(apply=True)
            render.assert_not_called()
        self.assertEqual(self.zot.uploads, 1)

    def test_desktop_pdf_goes_to_tablet_and_notes_return_under_existing_thesis(self):
        self.add_desktop()
        original = self.zot.file_path("SOURCE23").read_bytes()
        result = self.engine.run(apply=True)
        returned = next(a for a in result["actions"] if a["action"] == "tablet_to_zotero")
        self.assertEqual(returned["reference_key"], "PARENT23")
        self.assertEqual(self.zot.items["PARENT23"]["university"], "Example University")
        self.engine.run(apply=True)
        self.assertEqual(self.cloud.uploads, 1)
        self.assertEqual(len(self.zot.items), 3)  # original + current tablet PDF, one bibliography
        self.assertEqual(self.zot.file_path("SOURCE23").read_bytes(), original)

    def test_folders_added_both_ways_and_nested_pdf_keeps_location(self):
        self.zot.collection_records["CHILD234"] = {"key": "CHILD234", "name": "Desktop child", "parentCollection": "ROOT2345"}
        child = folder("Tablet child", self.cloud.root.id)
        grandchild = folder("Grandchild", child.id)
        self.cloud.records.update({child.id: child, grandchild.id: grandchild})
        self.add_desktop(collection="CHILD234")
        self.engine.run(apply=True)
        links = self.state.read("folders")
        self.assertEqual(len(links), 4)
        self.assertTrue(any(r["name"] == "Grandchild" for r in self.zot.collection_records.values()))
        uploaded = next(r for r in self.cloud.records.values() if r.is_pdf)
        self.assertEqual(uploaded.parent, links["CHILD234"]["cloud"])
        self.engine.run(apply=True)
        self.assertEqual((self.cloud.folder_writes, self.cloud.uploads), (1, 1))

    def test_unique_existing_folder_names_link_without_creating_duplicates(self):
        self.zot.collection_records["CHILD234"] = {"key": "CHILD234", "name": "Same", "parentCollection": "ROOT2345"}
        child = folder("Same", self.cloud.root.id)
        self.cloud.records[child.id] = child
        self.engine.run(apply=True)
        self.assertEqual(self.state.read("folders")["CHILD234"]["cloud"], child.id)
        self.assertEqual((self.cloud.folder_writes, self.zot.writes), (0, 0))

    def test_lost_folder_commit_confirmation_reuses_identity(self):
        self.zot.collection_records["CHILD234"] = {"key": "CHILD234", "name": "New", "parentCollection": "ROOT2345"}
        self.cloud.lose_folder = True
        with self.assertRaises(OSError):
            self.engine.run(apply=True)
        self.engine.run(apply=True)
        self.assertEqual(self.cloud.folder_writes, 1)

    def test_lost_pdf_commit_confirmation_reuses_identity(self):
        self.add_desktop()
        self.cloud.lose_upload = True
        interrupted = self.engine.run(apply=True)
        self.assertIn("Connection lost", interrupted["review"][0]["reason"])
        self.engine.run(apply=True)
        self.assertEqual(self.cloud.uploads, 1)
        self.assertEqual(len(self.zot.items), 3)

    def test_deleted_tablet_pdf_is_not_recreated(self):
        self.add_desktop()
        self.engine.run(apply=True)
        ck = next(iter(self.state.read("documents")))
        self.cloud.records.pop(ck)
        result = self.engine.run(apply=True)
        self.assertTrue(result["review"])
        self.assertEqual(self.cloud.uploads, 1)

    def test_deleted_desktop_source_is_not_recreated_or_uploaded_after_interruption(self):
        self.add_desktop()
        self.cloud.lose_upload = True
        self.assertTrue(self.engine.run(apply=True)["review"])
        self.zot.items.pop("SOURCE23")
        result = self.engine.run(apply=True)
        self.assertTrue(result["review"])
        self.assertEqual(self.zot.uploads, 0)

    def test_changed_desktop_original_is_never_sent_again(self):
        self.add_desktop()
        self.engine.run(apply=True)
        self.zot.file_path("SOURCE23").write_bytes(b"desktop annotations changed this file")
        result = self.engine.run(apply=True)
        self.assertTrue(result["review"])
        self.assertEqual(self.cloud.uploads, 1)

    def test_changed_desktop_original_blocks_an_interrupted_upload_that_did_not_commit(self):
        self.add_desktop()
        upload = self.cloud.upload_pdf
        with patch.object(self.cloud, 'upload_pdf', side_effect=OSError('Disconnected before commit')):
            self.assertTrue(self.engine.run(apply=True)['review'])
        identity, link = next(iter(self.state.read('documents').items()))
        original = self.zot.file_path('SOURCE23')
        before = original.read_bytes()
        original.write_bytes(b'new local revision')
        with patch.object(self.cloud, 'upload_pdf', wraps=upload) as retry:
            result = self.engine.run(apply=True)
            retry.assert_not_called()
        self.assertTrue(any('changed after approval' in row['reason'] for row in result['review']))
        self.assertEqual((self.state.directory / 'originals' / (link['baseline'] + '.pdf')).read_bytes(), before)
        original.write_bytes(before)
        self.engine.run(apply=True)
        self.assertIn(identity, self.cloud.records)
        self.assertEqual(self.cloud.uploads, 1)

    def test_different_tablet_edition_cannot_replace_current_annotated_pdf(self):
        item, bundle = self.tablet()
        self.engine.run(apply=True)
        bundle[item.id + ".pdf"] = b"different edition"
        self.cloud.records[item.id] = replace(item, hash="d" * 64)
        self.cloud.bundles[item.id] = bundle
        self.assertTrue(self.engine.run(apply=True)["review"])
        self.assertEqual(self.zot.uploads, 1)

    def test_saved_transcript_correction_reindexes_without_upload(self):
        self.tablet()
        self.engine.run(apply=True)
        self.engine.saved_annotations = lambda ck: ({"ocr": "corrected scientific transcription"},)
        self.engine.run(apply=True)
        self.assertEqual((self.zot.uploads, self.zot.indexes), (1, 2))

    def test_paper_tags_share_zotero_selector_page_tags_stay_scoped_and_removals_propagate(self):
        item, bundle = self.tablet()
        content = json.loads(bundle[item.id + ".content"])
        pid = content['cPages']['pages'][0]['id']
        content.update(tags=[{"name": "nanoparticle"}], pageTags=[{"name": "method", "pageId": pid}])
        bundle[item.id + ".content"] = json.dumps(content).encode()
        self.cloud.bundles[item.id] = bundle
        result = self.engine.run(apply=True)
        parent = result["actions"][-1]["reference_key"]
        self.assertEqual({t["tag"] for t in self.zot.items[parent]["tags"]}, {"nanoparticle"})
        self.assertEqual(self.state.read('documents')[item.id]['page_tags'], {pid: ['method']})
        self.zot.items[parent]["tags"] = [{"tag": "My own label"}]
        self.engine.run(apply=True)
        self.assertEqual(self.zot.items[parent]["tags"], [{"tag": "My own label"}])

    def test_inserted_page_preserves_tag_identity_and_moves_search_page_mapping(self):
        item, bundle = self.tablet()
        content = json.loads(bundle[item.id + '.content'])
        pid = content['cPages']['pages'][0]['id']
        content['pageTags'] = [{'name': 'Important result', 'pageId': pid}]
        bundle[item.id + '.content'] = json.dumps(content).encode()
        self.cloud.bundles[item.id] = bundle
        self.engine.run(apply=True)
        added = str(uuid.uuid4())
        content['cPages']['pages'].append({'id': added, 'idx': {'value': 'aa'}, 'redir': {'value': -1}})
        bundle[item.id + '.content'] = json.dumps(content).encode()
        self.cloud.bundles[item.id] = bundle
        self.cloud.records[item.id] = replace(item, hash=hashlib.sha256(bundle[item.id + '.content']).hexdigest())
        result = self.engine.run(apply=True)
        self.assertEqual(result['review'], [])
        row = self.state.read('documents')[item.id]
        self.assertEqual(row['page_map'], [added, pid])
        self.assertEqual(row['page_tags'], {pid: ['Important result']})
        with pymupdf.open(stream=self.zot.files[row['view_key']], filetype='pdf') as pdf:
            self.assertEqual(len(pdf), 2)

    def test_multiple_collection_membership_requires_primary_location(self):
        self.add_desktop()
        self.zot.collection_records["CHILD234"] = {"key": "CHILD234", "name": "Child", "parentCollection": "ROOT2345"}
        self.zot.items["PARENT23"]["collections"].append("CHILD234")
        self.assertTrue(self.engine.run(apply=True)["review"])
        self.assertEqual(self.cloud.uploads, 0)

    def test_unselected_tablet_documents_and_notebooks_are_excluded(self):
        outside = folder("Unselected")
        item, bundle = fixture(outside.id)
        self.cloud.records[outside.id] = outside
        self.cloud.add(item, bundle)
        notebook = CloudItem(str(uuid.uuid4()), "f" * 64, "Notebook", self.cloud.root.id,
                             "DocumentType", False, [{"id": "notebook.content"}])
        self.cloud.records[notebook.id] = notebook
        self.engine.run(apply=True)
        self.assertFalse(self.zot.items)

    def test_deleted_linked_collection_holds_its_pair_and_preserves_instance_check(self):
        self.zot.collection_records["CHILD234"] = {"key": "CHILD234", "name": "Child", "parentCollection": "ROOT2345"}
        self.engine.run(apply=True)
        original = self.state.read('folders')['CHILD234']
        self.zot.collection_records.pop("CHILD234")
        result = self.engine.run(apply=True)
        self.assertEqual(result['folder_issues'][0]['zotero_state'], 'missing')
        self.assertEqual(result['folder_issues'][0]['tablet'], original['cloud'])
        self.assertEqual(self.state.read('folders')['CHILD234'], original)
        self.assertNotIn('CHILD234', self.zot.collection_records)
        self.assertEqual(self.cloud.folder_writes, 1)
        self.zot.server_id = "different-instance"
        with self.assertRaises(PilotError):
            Bridge(self.zot, self.cloud, self.state, "ROOT2345", self.cloud.root.id).run(apply=True)

    def test_concurrent_cycle_is_rejected(self):
        with self.state.lock(), self.assertRaises(PilotError):
            self.engine.run(apply=True)

    def test_trashed_folders_are_outside_the_selected_tree(self):
        deleted = folder("In trash", "trash")
        self.cloud.records[deleted.id] = deleted
        self.engine.run(apply=True)
        self.assertEqual(len(self.state.read("folders")), 1)

    def test_missing_render_is_rebuilt_from_retained_sources(self):
        self.tablet()
        self.engine.run(apply=True)
        for path in (self.state.directory / "rendered").glob("*.pdf"):
            path.unlink()
        result = self.engine.run(apply=True)
        self.assertFalse(result["review"])
        self.assertEqual(len(self.zot.items), 2)

    def test_unsupported_scene_is_reviewed_without_losing_other_papers(self):
        item, bundle = self.tablet()
        bad, bad_bundle = fixture(self.cloud.root.id)
        name = next(k for k in bad_bundle if k.endswith(".rm"))
        bad_bundle[name] = b"unknown future firmware"
        self.cloud.add(bad, bad_bundle)
        result = self.engine.run(apply=True)
        self.assertEqual(len(result["review"]), 1)
        self.assertEqual(result["review"][0]["cloud"], bad.id)
        self.assertEqual(len(self.zot.items), 2)
        self.assertTrue((self.state.directory / "sources/remarkable/sources" / (bad.id + ".json")).exists())

    def test_missing_desktop_pdf_does_not_block_another_paper(self):
        self.add_desktop(key="MISSING2", parent="MISSING3")
        self.zot.file_path("MISSING2").unlink()
        self.add_desktop()
        result = self.engine.run(apply=True)
        self.assertEqual(result["review"][0]["attachment"], "MISSING2")
        self.assertEqual(self.cloud.uploads, 1)
        self.assertEqual(self.zot.uploads, 1)

    def test_changed_staged_original_is_retained_without_blocking_other_papers(self):
        self.add_desktop(key="CHANGED2", parent="CHANGED3")
        baseline = hashlib.sha256(self.zot.file_path("CHANGED2").read_bytes()).hexdigest()
        identity = str(uuid.uuid4())
        original = self.state.directory / "originals" / (baseline + ".pdf")
        original.parent.mkdir(parents=True)
        original.write_bytes(b"independent local edit")
        self.state.write("documents", {identity: {"origin": "zotero", "source_key": "CHANGED2", "parent": "CHANGED3",
            "collection": "ROOT2345", "baseline": baseline, "uploaded": False, "name": "Changed original"}})
        self.add_desktop()
        result = self.engine.run(apply=True)
        self.assertTrue(any(row.get("cloud") == identity and "changed" in row["reason"] for row in result["review"]))
        self.assertEqual(original.read_bytes(), b"independent local edit")
        self.assertNotIn(identity, self.cloud.records)
        self.assertEqual((self.cloud.uploads, self.zot.uploads), (1, 1))

    def test_corrupt_tablet_pdf_is_retained_and_other_papers_still_publish(self):
        bad, bundle = self.tablet()
        bundle[bad.id + ".pdf"] = b"unreadable PDF"
        self.cloud.bundles[bad.id] = bundle
        good, _ = self.tablet()
        result = self.engine.run(apply=True)
        self.assertEqual(result["review"][0]["cloud"], bad.id)
        self.assertEqual(self.state.read("documents")[good.id]["revision"], good.hash)
        self.assertTrue((self.state.directory / "sources/remarkable/sources" / (bad.id + ".json")).exists())
        self.assertEqual(self.zot.uploads, 1)

    def test_failed_publication_resumes_and_does_not_block_other_papers(self):
        bad, _ = self.tablet()
        good, _ = self.tablet()
        updates = []
        self.engine.progress = lambda message: updates.append((message, self.state.read("documents")))
        self.zot.fail_index = True
        result = self.engine.run(apply=True)
        self.assertEqual(result["review"][0]["cloud"], bad.id)
        self.assertEqual(self.state.read("documents")[good.id]["revision"], good.hash)
        received = next(rows for message, rows in updates if message.startswith("Received tablet PDF"))
        self.assertEqual(received[good.id]["revision"], good.hash)
        self.assertNotIn("revision", received[bad.id])
        retried = self.engine.run(apply=True)
        self.assertFalse(retried["review"])
        self.assertEqual(self.state.read("documents")[bad.id]["revision"], bad.hash)
        self.assertEqual((len(self.zot.items), self.zot.uploads), (4, 2))

    def test_cloud_approval_never_recreates_unavailable_parent(self):
        item, _ = self.tablet()
        self.engine.review_policy = SimpleNamespace(
            suspended_documents=lambda: set(), primary_collection=lambda key: None,
            approval=lambda *args: {"parent": "PARENT23"})
        for parent in (None, {"deleted": True, "collections": ["ROOT2345"]}, {"collections": []}):
            with self.subTest(parent=parent):
                if parent is not None:
                    self.zot.items["PARENT23"] = {"key": "PARENT23", "itemType": "document", **parent}
                result = self.engine.run(apply=True)
                self.assertTrue(any(row.get("cloud") == item.id for row in result["review"]))
                self.assertEqual((self.zot.writes, self.zot.uploads), (0, 0))

    def test_approved_supporting_information_keeps_its_attachment_role(self):
        item, _ = self.tablet()
        self.zot.items["PARENT23"] = {"key": "PARENT23", "itemType": "journalArticle", "collections": ["ROOT2345"]}
        self.engine.review_policy = SimpleNamespace(
            suspended_documents=lambda: set(), primary_collection=lambda key: None,
            approval=lambda *args: {"parent": "PARENT23", "document_type": "supporting_information"})
        result = self.engine.run(apply=True)
        self.assertFalse(result["review"])
        view = self.state.read("documents")[item.id]["view_key"]
        self.assertEqual(self.zot.items[view]["title"], "Supporting information")
        self.assertEqual(self.zot.items[view]["parentItem"], "PARENT23")

    def test_main_and_supporting_pdf_keep_their_separate_tablet_homes(self):
        self.add_desktop()
        self.engine.run(apply=True)
        self.zot.collection_records["CHILD234"] = {"key": "CHILD234", "name": "Supporting", "parentCollection": "ROOT2345"}
        self.add_desktop(key="SUPPLE23", collection="CHILD234", text="Supporting data")
        self.zot.items["PARENT23"]["collections"] = ["ROOT2345", "CHILD234"]
        self.zot.review_action = lambda action, payload: {"adopted": False, "native": 0}
        self.engine.review_policy = SimpleNamespace(
            suspended_documents=lambda: set(), primary_collection=lambda key: "ROOT2345",
            approval=lambda kind, identity, baseline: {"collection": "CHILD234", "document_type": "supporting_information"}
                if identity == "SUPPLE23" else {})
        self.assertFalse(self.engine.run(apply=True)["review"])
        self.assertFalse(self.engine.run(apply=True)["review"])
        homes = {row["source_key"]: row["collection"] for row in self.state.read("documents").values()}
        self.assertEqual(homes, {"SOURCE23": "ROOT2345", "SUPPLE23": "CHILD234"})

    def test_cancel_is_not_downgraded_to_a_paper_error(self):
        self.tablet()
        def cancel(message):
            raise InterruptedError("Stopped by user")
        self.engine.progress = cancel
        with self.assertRaisesRegex(InterruptedError, "Stopped by user"):
            self.engine.run(apply=True)

    def test_expired_tablet_permission_stops_without_retrying_every_paper(self):
        self.tablet()
        self.tablet()
        attempted = []
        def expired(item):
            attempted.append(item.id)
            raise CloudError("reMarkable authorization expired. Reconnect using a new pairing code.")
        self.cloud.pdf_bundle = expired
        with self.assertRaisesRegex(CloudError, "authorization expired"):
            self.engine.run(apply=True)
        self.assertEqual(len(attempted), 1)

    def test_revised_tablet_scene_updates_same_zotero_attachment(self):
        item, old_bundle = self.tablet(deleted_ink=True)
        first = self.engine.run(apply=True)["actions"][-1]
        newer, bundle = fixture(self.cloud.root.id)
        bundle = {k.replace(newer.id, item.id): v for k, v in bundle.items()}
        bundle[item.id + ".pdf"] = old_bundle[item.id + ".pdf"]
        updated = replace(item, hash="e" * 64, files=[{"id": k} for k in bundle])
        self.cloud.add(updated, bundle)
        second = self.engine.run(apply=True)["actions"][-1]
        self.assertEqual(first["attachment_key"], second["attachment_key"])
        self.assertEqual(self.zot.uploads, 2)
        with pymupdf.open(stream=self.zot.files[second["attachment_key"]], filetype="pdf") as doc:
            self.assertEqual(len(list(doc[0].annots())), 3)
