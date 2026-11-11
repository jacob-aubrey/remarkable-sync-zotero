"""Temporary libraries and an in-memory cloud; never contact a user account."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import uuid

import fitz

from note_organizer.paper_registry import Registry
from note_organizer.remarkable_cloud import (CloudClient, CloudConflict, CloudError, CloudItem,
                                           _SafeRedirect, atomic_write, crc32c_header, parse_index, serialize_index)
from note_organizer import remarkable_cloud
from note_organizer.remarkable_outbox import Outbox
from note_organizer.remarkable_sync import RemarkableSync, default_config, effective_config, is_integration_enabled
from note_organizer.service import Service
from note_organizer.storage import Store, canonical


class MemoryCloud(CloudClient):
    """Exercise the real protocol writer against immutable blobs and root CAS."""
    def __init__(self):
        super().__init__("synthetic")
        self.user_token = "synthetic"
        body = serialize_index([], root=True)
        self.root_hash, self.generation = hashlib.sha256(body).hexdigest(), 0
        self.files, self.puts = {self.root_hash: body}, []
        self.conflict_next = self.ambiguous_next = False

    def get(self, url, filename=None):
        if url.endswith("/v4/root"):
            return json.dumps({"hash": self.root_hash, "generation": self.generation}).encode()
        return self.files[url.rsplit("/", 1)[-1]]

    def put(self, url, data, filename, content_type="application/octet-stream", integrity=True):
        self.puts.append((url, data, filename))
        if url.endswith("/v3/root"):
            payload = json.loads(data)
            if self.conflict_next:
                self.conflict_next = False
                self.generation += 1
                raise CloudConflict("Synthetic concurrent writer")
            if payload["generation"] != self.generation:
                raise CloudConflict("Synthetic generation conflict")
            self.root_hash, self.generation = payload["hash"], self.generation + 1
            if self.ambiguous_next:
                self.ambiguous_next = False
                raise CloudError("Synthetic lost confirmation")
            return json.dumps({"hash": self.root_hash, "generation": self.generation}).encode()
        self.files[url.rsplit("/", 1)[-1]] = data
        return b""


def pdf_bytes():
    with fitz.open() as pdf:
        page = pdf.new_page()
        page.insert_text((72, 72), "Synthetic paper")
        return pdf.tobytes()


class AtomicCloudWriteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "space \u03bb \U0001f680.pdf"
        self.original = b"Managed original"
        self.digest = hashlib.sha256(self.original).hexdigest()

    def tearDown(self):
        self.temp.cleanup()

    def test_absent_publication_preserves_file_created_at_publication_boundary(self):
        real = remarkable_cloud._publish_absent
        def race(temporary, path):
            path.write_bytes(b"Concurrent local file")
            return real(temporary, path)
        with patch.object(remarkable_cloud, "_publish_absent", side_effect=race):
            with self.assertRaises(CloudError):
                atomic_write(self.path, b"Cloud", require_absent=True)
        self.assertEqual(self.path.read_bytes(), b"Concurrent local file")
        self.assertEqual(list(self.root.iterdir()), [self.path])

    @unittest.skipUnless(os.name == "nt", "Mandatory Windows sharing guards")
    def test_guarded_replacement_supports_spaces_unicode_and_removes_previous_output(self):
        self.path.write_bytes(self.original)
        atomic_write(self.path, b"Cloud", expected_digest=self.digest)
        self.assertEqual(self.path.read_bytes(), b"Cloud")
        self.assertEqual(list(self.root.iterdir()), [self.path])

    @unittest.skipUnless(os.name == "nt", "Mandatory Windows sharing guards")
    def test_guard_prevents_edit_and_replacement_between_digest_check_and_move(self):
        self.path.write_bytes(self.original)
        other = self.root / "editor replacement.pdf"
        other.write_bytes(b"Editor save")
        real = remarkable_cloud._rename_guarded
        def try_editor_save(kernel, handle, destination):
            with self.assertRaises(PermissionError):
                self.path.write_bytes(b"Editor write")
            with self.assertRaises(PermissionError):
                os.replace(other, self.path)
            return real(kernel, handle, destination)
        with patch.object(remarkable_cloud, "_rename_guarded", side_effect=try_editor_save):
            atomic_write(self.path, b"Cloud", expected_digest=self.digest)
        self.assertEqual(self.path.read_bytes(), b"Cloud")
        self.assertEqual(other.read_bytes(), b"Editor save")

    @unittest.skipUnless(os.name == "nt", "Mandatory Windows sharing guards")
    def test_existing_writable_handle_prevents_replacement(self):
        self.path.write_bytes(self.original)
        with self.path.open("r+b"):
            with self.assertRaises(CloudError):
                atomic_write(self.path, b"Cloud", expected_digest=self.digest)
        self.assertEqual(self.path.read_bytes(), self.original)

    @unittest.skipUnless(os.name == "nt", "Mandatory Windows sharing guards")
    def test_publication_collision_retains_both_files_and_reports_preserved_path(self):
        self.path.write_bytes(self.original)
        real = remarkable_cloud._publish_absent
        def race(temporary, path):
            path.write_bytes(b"New editor file")
            return real(temporary, path)
        with patch.object(remarkable_cloud, "_publish_absent", side_effect=race):
            with self.assertRaises(CloudError) as failure:
                atomic_write(self.path, b"Cloud", expected_digest=self.digest)
        preserved = [path for path in self.root.iterdir() if path.name.endswith(".preserved")]
        self.assertEqual(self.path.read_bytes(), b"New editor file")
        self.assertEqual(len(preserved), 1)
        self.assertEqual(preserved[0].read_bytes(), self.original)
        self.assertIn(str(preserved[0]), str(failure.exception))

    @unittest.skipUnless(os.name == "nt", "Mandatory Windows sharing guards")
    def test_publication_failure_restores_verified_original(self):
        self.path.write_bytes(self.original)
        with patch.object(remarkable_cloud, "_publish_absent", side_effect=OSError("Synthetic publication failure")):
            with self.assertRaises(OSError):
                atomic_write(self.path, b"Cloud", expected_digest=self.digest)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_interrupted_replacement_exposes_retained_original_before_new_publication(self):
        preserved = self.path.with_name(self.path.name + ".synthetic.preserved")
        preserved.write_bytes(self.original)
        with self.assertRaisesRegex(CloudError, "interrupted replacement") as failure:
            atomic_write(self.path, b"Cloud", require_absent=True)
        self.assertIn(str(preserved), str(failure.exception))
        self.assertFalse(self.path.exists())
        self.assertEqual(preserved.read_bytes(), self.original)


class CloudWriteTests(unittest.TestCase):
    def setUp(self):
        self.cloud = MemoryCloud()
        self.source_id = str(uuid.uuid4())
        self.cloud.create_folder("Literature", "", self.source_id)

    def test_crc32c_uses_castagnoli_and_v4_root_preserves_totals(self):
        self.assertEqual(crc32c_header(b"123456789"), "crc32c=4waSgw==")
        entries = parse_index(self.cloud.files[self.cloud.root_hash])
        self.assertEqual(entries[0]["id"], self.source_id)
        self.assertEqual(entries[0]["subfiles"], 1)
        self.assertGreater(entries[0]["size"], 0)

    def test_signed_upload_redirect_preserves_pdf_and_strips_credentials(self):
        from urllib.request import Request
        request = Request("https://internal.cloud.remarkable.com/files", data=b"PDF bytes",
                          headers={"Authorization": "Bearer synthetic", "X-Goog-Hash": "crc32c=4waSgw=="}, method="PUT")
        redirected = _SafeRedirect().redirect_request(request, None, 307, "", {}, "https://storage.googleapis.com/upload")
        self.assertEqual(redirected.get_method(), "PUT")
        self.assertEqual(redirected.data, b"PDF bytes")
        self.assertIsNone(redirected.get_header("Authorization"))
        self.assertEqual(redirected.get_header("X-goog-hash"), "crc32c=4waSgw==")

    def test_upload_prepares_pdf_pages_and_preserves_other_root_entries(self):
        document_id = str(uuid.uuid4())
        item = self.cloud.upload_pdf(pdf_bytes(), "Paper", self.source_id, document_id, self.source_id)
        self.assertEqual({entry["id"] for entry in parse_index(self.cloud.files[self.cloud.root_hash])},
                         {self.source_id, document_id})
        files = {file["id"]: file for file in item.files}
        content = json.loads(self.cloud.files[files[document_id + ".content"]["hash"]])
        self.assertEqual(content["pageCount"], 1)
        self.assertEqual(len(content["pages"]), 1)
        self.assertTrue(any(url.endswith("/v3/root") for url, _, _ in self.cloud.puts))

    def test_ambiguous_commit_retry_does_not_upload_or_duplicate(self):
        document_id, data = str(uuid.uuid4()), pdf_bytes()
        self.cloud.ambiguous_next = True
        with self.assertRaises(CloudError):
            self.cloud.upload_pdf(data, "Paper", self.source_id, document_id, self.source_id)
        calls = len(self.cloud.puts)
        self.cloud.upload_pdf(data, "Paper", self.source_id, document_id, self.source_id)
        self.assertEqual(len(self.cloud.puts), calls)
        self.assertEqual(sum(entry["id"] == document_id for entry in parse_index(self.cloud.files[self.cloud.root_hash])), 1)

    def test_generation_conflict_rereads_and_retries_root(self):
        self.cloud.conflict_next = True
        with patch.object(self.cloud.stop, "wait", return_value=False):
            item = self.cloud.upload_pdf(pdf_bytes(), "Paper", self.source_id, str(uuid.uuid4()), self.source_id)
        self.assertTrue(item.is_pdf)
        self.assertEqual(len(parse_index(self.cloud.files[self.cloud.root_hash])), 2)

    def test_id_collision_and_out_of_scope_parent_never_replace(self):
        data, document_id = pdf_bytes(), str(uuid.uuid4())
        self.cloud.upload_pdf(data, "Paper", self.source_id, document_id, self.source_id)
        before = self.cloud.root_hash
        with fitz.open(stream=data, filetype="pdf") as doc:
            doc.new_page()
            different = doc.tobytes()
        with self.assertRaises(CloudError):
            self.cloud.upload_pdf(different, "Paper", self.source_id, document_id, self.source_id)
        with self.assertRaises(CloudError):
            self.cloud.create_folder("Wrong scope", "", str(uuid.uuid4()), self.source_id)
        self.assertEqual(self.cloud.root_hash, before)

    def test_retry_does_not_claim_a_document_moved_outside_literature(self):
        data, document_id = pdf_bytes(), str(uuid.uuid4())
        item = self.cloud.upload_pdf(data, "Paper", self.source_id, document_id, self.source_id)
        entries = parse_index(self.cloud.files[self.cloud.root_hash])
        files = list(item.files)
        old_meta = next(file for file in files if file["id"] == document_id + ".metadata")
        metadata = json.loads(self.cloud.files[old_meta["hash"]])
        metadata["parent"] = ""
        new_meta = self.cloud._file_entry(old_meta["id"], json.dumps(metadata).encode())
        files = [new_meta if file["id"] == old_meta["id"] else file for file in files]
        doc_hash = hashlib.sha256(b"".join(bytes.fromhex(file["hash"])
                                         for file in sorted(files, key=lambda file: file["id"]))).hexdigest()
        self.cloud.files[doc_hash] = serialize_index(files)
        for entry in entries:
            if entry["id"] == document_id:
                entry["hash"] = doc_hash
        root = serialize_index(entries, root=True)
        self.cloud.root_hash = hashlib.sha256(root).hexdigest()
        self.cloud.files[self.cloud.root_hash] = root
        self.cloud.generation += 1
        puts = len(self.cloud.puts)
        with self.assertRaises(CloudError):
            self.cloud.upload_pdf(data, "Paper", self.source_id, document_id, self.source_id)
        self.assertEqual(len(self.cloud.puts), puts)


class CloudOutboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.pc = self.root / "Sorted"
        self.pc.mkdir()
        self.store = Store(self.root / "data" / "library.sqlite3")
        self.engine = RemarkableSync(self.store, threading.Event(), Mock())
        self.outbox = Outbox(self.engine)
        self.cloud = MemoryCloud()
        self.source_id = str(uuid.uuid4())
        self.cloud.create_folder("Literature", "", self.source_id)
        self.engine.client = self.cloud
        self.config = {**default_config(self.store.path.parent), "integration_enabled": True,
                       "pc_root": str(self.pc), "source_folder_id": self.source_id,
                       "send_new_pdfs": True}
        self.store.set_preference("remarkable", self.config)
        self.catalog = [{"id": self.source_id, "name": "Literature", "parent": "", "ancestor_ids": [], "path": "/Literature"}]
        self.store.set_preference("remarkable_folders", self.catalog)
        self.pdf = self.pc / "Paper.pdf"
        self.pdf.write_bytes(pdf_bytes())

    def tearDown(self):
        self.temp.cleanup()

    def test_fresh_off_explicit_off_overrides_legacy_and_manual_force(self):
        self.assertFalse(is_integration_enabled({}))
        self.assertTrue(is_integration_enabled({"pc_root": "Sorted"}))
        self.assertFalse(is_integration_enabled({"pc_root": "Sorted", "integration_enabled": False}))
        self.store.set_preference("remarkable", {})
        self.assertFalse(effective_config(self.store)["integration_enabled"])
        client = self.engine.client = Mock()
        self.assertFalse(self.engine.sync(force=True, listing_only=True))
        with self.assertRaises(CloudError):
            self.engine.connect("abcdefgh")
        client.library.assert_not_called()
        client.pair.assert_not_called()

    def test_confirmed_filing_queue_sends_once_and_links_original(self):
        job = self.outbox.queue_pdf(self.pdf)
        self.assertEqual(self.outbox.queue_pdf(self.pdf)["cloud_id"], job["cloud_id"])
        self.assertTrue(self.outbox.process())
        self.assertEqual(self.outbox.jobs()[0]["state"], "sent")
        registry = Registry(self.store)
        self.assertEqual(registry.paper_for_cloud(job["cloud_id"]), job["paper_id"])
        self.assertEqual(registry.local_for_cloud(job["cloud_id"])[0]["path"], canonical(self.pdf))
        puts = len(self.cloud.puts)
        self.assertFalse(self.outbox.process())
        self.assertEqual(len(self.cloud.puts), puts)
        self.assertTrue(self.pdf.is_file())

    def test_off_pauses_saved_job_and_root_boundary_blocks_new_jobs(self):
        self.outbox.queue_pdf(self.pdf)
        self.config["integration_enabled"] = False
        self.store.set_preference("remarkable", self.config)
        self.assertFalse(self.outbox.process())
        self.assertEqual(self.outbox.jobs()[0]["state"], "queued")
        self.config["integration_enabled"] = True
        self.store.set_preference("remarkable", self.config)
        outside = self.root / "Outside.pdf"
        outside.write_bytes(pdf_bytes())
        self.assertIsNone(self.outbox.queue_pdf(outside))
        self.assertEqual(len(self.outbox.jobs()), 1)

    def test_missing_route_becomes_sendable_after_mapping(self):
        child = self.pc / "Different PC name"
        child.mkdir()
        pdf = child / "Paper.pdf"
        pdf.write_bytes(pdf_bytes())
        job = self.outbox.queue_pdf(pdf)
        self.assertEqual(job["state"], "needs_mapping")
        cloud_id = str(uuid.uuid4())
        self.cloud.create_folder("Tablet name", self.source_id, cloud_id, self.source_id)
        self.config["mappings"] = {cloud_id: canonical(child)}
        self.store.set_preference("remarkable", self.config)
        self.assertTrue(self.outbox.process())
        self.assertEqual(self.outbox.jobs()[0]["folder_id"], cloud_id)

    def test_changed_local_file_and_existing_cloud_pdf_need_review(self):
        self.outbox.queue_pdf(self.pdf)
        self.pdf.write_bytes(self.pdf.read_bytes() + b"\n% local change")
        self.assertFalse(self.outbox.process())
        self.assertEqual(self.outbox.jobs()[0]["state"], "review")
        second = self.pc / "Second.pdf"
        second.write_bytes(pdf_bytes())
        self.cloud.upload_pdf(second.read_bytes(), "Existing", self.source_id, str(uuid.uuid4()), self.source_id)
        self.outbox.queue_pdf(second)
        self.assertFalse(self.outbox.process())
        self.assertEqual(self.outbox.jobs()[-1]["state"], "review")
        self.assertIn("already on reMarkable", self.outbox.jobs()[-1]["reason"])

    def test_ambiguous_commit_outbox_retry_reuses_preallocated_id(self):
        job = self.outbox.queue_pdf(self.pdf)
        self.cloud.ambiguous_next = True
        self.assertFalse(self.outbox.process())
        self.assertEqual(self.outbox.jobs()[0]["state"], "waiting")
        self.assertTrue(self.outbox.process())
        self.assertEqual(self.outbox.jobs()[0]["cloud_id"], job["cloud_id"])
        self.assertEqual(len(parse_index(self.cloud.files[self.cloud.root_hash])), 2)

    def test_lost_confirmation_with_intervening_download_retains_original_identity(self):
        self.config["enabled"] = True
        self.store.set_preference("remarkable", self.config)
        job = self.outbox.queue_pdf(self.pdf)
        self.cloud.ambiguous_next = True
        self.assertFalse(self.outbox.process())
        puts = len(self.cloud.puts)
        self.assertTrue(self.engine.sync())
        registry = Registry(self.store)
        self.assertEqual(registry.paper_for_cloud(job["cloud_id"]), job["paper_id"])
        self.assertFalse(self.outbox.process())
        self.assertEqual(self.outbox.jobs()[0]["state"], "sent")
        self.assertEqual(self.outbox.jobs()[0]["cloud_id"], job["cloud_id"])
        self.assertEqual(len(self.cloud.puts), puts)
        self.assertEqual(len(registry.local_for_cloud(job["cloud_id"])), 2)

    def test_retry_repairs_identity_created_by_older_lost_confirmation_flow(self):
        job = self.outbox.queue_pdf(self.pdf)
        self.cloud.ambiguous_next = True
        self.assertFalse(self.outbox.process())
        registry = Registry(self.store)
        cloud_source = registry.register_cloud(job["cloud_id"], digest=job["digest"])
        self.assertNotEqual(cloud_source["paper_id"], job["paper_id"])
        self.assertTrue(self.outbox.process())
        self.assertEqual(registry.paper_for_cloud(job["cloud_id"]), job["paper_id"])
        self.assertEqual(self.outbox.jobs()[0]["state"], "sent")

    def test_retry_restored_pdf_requeues_review_without_changing_saved_cloud_id(self):
        original = self.pdf.read_bytes()
        job = self.outbox.queue_pdf(self.pdf)
        self.pdf.write_bytes(original + b"\n% change")
        self.assertFalse(self.outbox.process())
        self.pdf.write_bytes(original)
        self.assertTrue(self.outbox.process(retry_review=True))
        self.assertEqual(self.outbox.jobs()[0]["state"], "sent")
        self.assertEqual(self.outbox.jobs()[0]["cloud_id"], job["cloud_id"])

    def test_retry_changed_pdf_requires_confirmation_and_refreshes_digest(self):
        job = self.outbox.queue_pdf(self.pdf)
        self.pdf.write_bytes(self.pdf.read_bytes() + b"\n% change")
        self.assertFalse(self.outbox.process())
        with self.assertRaisesRegex(CloudError, "Confirm sending"):
            self.outbox.retry(job["id"])
        self.assertFalse(self.outbox.process(retry_review=True))
        refreshed = self.outbox.retry(job["id"], accept_changes=True)
        self.assertEqual(refreshed["digest"], hashlib.sha256(self.pdf.read_bytes()).hexdigest())
        self.assertTrue(self.outbox.process())

    def test_changed_pdf_after_ambiguous_commit_preserves_old_intent_and_allocates_new_id(self):
        job = self.outbox.queue_pdf(self.pdf)
        self.cloud.ambiguous_next = True
        self.assertFalse(self.outbox.process())
        self.pdf.write_bytes(self.pdf.read_bytes() + b"\n% reviewed new version")
        replacement = self.outbox.retry(job["id"], accept_changes=True)
        self.assertNotEqual(replacement["cloud_id"], job["cloud_id"])
        self.assertEqual(self.outbox.jobs()[0]["cloud_id"], job["cloud_id"])
        self.assertEqual(self.outbox.jobs()[0]["state"], "cancelled")
        self.assertTrue(self.outbox.process())
        self.assertEqual(len(parse_index(self.cloud.files[self.cloud.root_hash])), 3)

    def test_source_change_pauses_transfer_until_destination_is_confirmed(self):
        job = self.outbox.queue_pdf(self.pdf)
        replacement = str(uuid.uuid4())
        self.cloud.create_folder("New source", "", replacement)
        self.config["source_folder_id"] = replacement
        self.store.set_preference("remarkable", self.config)
        puts = len(self.cloud.puts)
        self.assertFalse(self.outbox.process())
        self.assertEqual(len(self.cloud.puts), puts)
        self.assertEqual(self.outbox.jobs()[0]["reason_kind"], "destination_change")
        self.assertFalse(self.outbox.process(retry_review=True))
        with self.assertRaisesRegex(CloudError, "Confirm the current source"):
            self.outbox.retry(job["id"])
        self.outbox.retry(job["id"], accept_destination=True)
        self.assertTrue(self.outbox.process())
        remote = next(item for item in self.cloud.library() if item.id == job["cloud_id"])
        self.assertEqual(remote.parent, replacement)

    def test_mapping_change_pauses_transfer_before_uploading_to_another_folder(self):
        old_folder = str(uuid.uuid4())
        new_folder = str(uuid.uuid4())
        child = self.pc / "PC folder"
        child.mkdir()
        pdf = child / "Paper.pdf"
        pdf.write_bytes(self.pdf.read_bytes())
        self.cloud.create_folder("Old destination", self.source_id, old_folder, self.source_id)
        self.cloud.create_folder("New destination", self.source_id, new_folder, self.source_id)
        self.config["mappings"] = {old_folder: canonical(child)}
        self.store.set_preference("remarkable", self.config)
        self.engine.sync(listing_only=True, force=True)
        job = self.outbox.queue_pdf(pdf)
        self.assertEqual(job["folder_id"], old_folder)
        self.config["mappings"] = {new_folder: canonical(child)}
        self.store.set_preference("remarkable", self.config)
        puts = len(self.cloud.puts)
        self.assertFalse(self.outbox.process())
        self.assertEqual(len(self.cloud.puts), puts)
        self.assertEqual(self.outbox.jobs()[0]["reason_kind"], "destination_change")
        self.outbox.retry(job["id"], accept_destination=True)
        self.assertTrue(self.outbox.process())
        remote = next(item for item in self.cloud.library() if item.id == job["cloud_id"])
        self.assertEqual(remote.parent, new_folder)

    def test_cancelled_transfer_keeps_original_and_never_uploads(self):
        job = self.outbox.queue_pdf(self.pdf)
        puts = len(self.cloud.puts)
        self.outbox.cancel(job["id"])
        self.assertFalse(self.outbox.process(retry_review=True))
        self.assertEqual(self.outbox.jobs()[0]["state"], "cancelled")
        self.assertEqual(len(self.cloud.puts), puts)
        self.assertTrue(self.pdf.is_file())

    def test_confirmed_duplicate_link_resolves_review_without_upload(self):
        data = self.pdf.read_bytes()
        cloud_id = str(uuid.uuid4())
        self.cloud.upload_pdf(data, "Existing", self.source_id, cloud_id, self.source_id)
        job = self.outbox.queue_pdf(self.pdf)
        self.assertFalse(self.outbox.process())
        registry = Registry(self.store)
        cloud_source = registry.register_cloud(cloud_id, digest=job["digest"])
        local = next(source for source in registry.sources() if source["kind"] == "local")
        registry.link_sources([cloud_source["id"], local["id"]])
        puts = len(self.cloud.puts)
        self.assertFalse(self.outbox.process())
        self.assertEqual(self.outbox.jobs()[0]["state"], "sent")
        self.assertEqual(self.outbox.jobs()[0]["cloud_id"], cloud_id)
        self.assertEqual(len(self.cloud.puts), puts)

    def test_folder_baseline_does_not_bulk_create_and_new_pc_folder_does(self):
        (self.pc / "Existing unmatched folder").mkdir()
        self.config["mirror_pc_folders"] = True
        self.store.set_preference("remarkable", self.config)
        self.outbox.observe_folders(self.catalog)
        self.assertFalse(self.outbox.jobs())
        new = self.pc / "New folder"
        new.mkdir()
        self.outbox.observe_folders(self.catalog)
        self.assertEqual(self.outbox.jobs()[0]["kind"], "folder")
        self.assertTrue(self.outbox.process())
        self.assertEqual(self.engine.config()["mappings"][self.outbox.jobs()[0]["cloud_id"]], canonical(new))

    def test_new_nested_pc_folders_create_parent_before_child(self):
        self.config["mirror_pc_folders"] = True
        self.store.set_preference("remarkable", self.config)
        self.outbox.observe_folders(self.catalog)
        nested = self.pc / "Parent" / "Child"
        nested.mkdir(parents=True)
        self.outbox.observe_folders(self.catalog)
        self.assertEqual(len(self.outbox.jobs()), 2)
        self.assertTrue(self.outbox.process())
        jobs = {job["name"]: job for job in self.outbox.jobs()}
        self.assertEqual(jobs["Child"]["folder_id"], jobs["Parent"]["cloud_id"])
        self.assertTrue(all(job["state"] == "sent" for job in jobs.values()))

    def test_new_local_counterpart_saves_matching_cloud_folder_link(self):
        item = self.cloud.create_folder("Counterpart", self.source_id, str(uuid.uuid4()), self.source_id)
        catalog = self.catalog + [{"id": item.id, "name": item.name, "parent": self.source_id,
                                  "ancestor_ids": [self.source_id], "path": "/Literature/" + item.name}]
        self.config["mirror_pc_folders"] = True
        self.store.set_preference("remarkable", self.config)
        self.outbox.observe_folders(catalog)
        target = self.pc / item.name
        target.mkdir()
        self.outbox.observe_folders(catalog)
        self.assertEqual(self.engine.config()["mappings"][item.id], canonical(target))
        self.assertFalse(self.outbox.jobs())

    def test_new_folders_created_while_master_off_wait_until_reenabled(self):
        self.config["mirror_pc_folders"] = True
        self.store.set_preference("remarkable", self.config)
        self.outbox.observe_folders(self.catalog)
        self.config["integration_enabled"] = False
        self.store.set_preference("remarkable", self.config)
        (self.pc / "While disabled").mkdir()
        self.outbox.observe_folders(self.catalog)
        self.assertFalse(self.outbox.jobs())
        self.config["integration_enabled"] = True
        self.store.set_preference("remarkable", self.config)
        self.outbox.observe_folders(self.catalog)
        self.assertEqual(self.outbox.jobs()[0]["name"], "While disabled")

    def test_new_cloud_folder_creates_local_counterpart_with_stable_link(self):
        self.config["mirror_cloud_folders"] = True
        self.store.set_preference("remarkable", self.config)
        self.outbox.observe_folders(self.catalog)
        item = self.cloud.create_folder("New tablet folder", self.source_id, str(uuid.uuid4()), self.source_id)
        catalog = self.catalog + [{"id": item.id, "name": item.name, "parent": self.source_id,
                                  "ancestor_ids": [self.source_id], "path": "/Literature/" + item.name}]
        self.outbox.observe_folders(catalog)
        self.assertTrue((self.pc / item.name).is_dir())
        self.assertEqual(self.engine.config()["mappings"][item.id], canonical(self.pc / item.name))

    def test_folder_creation_runs_independently_of_automatic_note_downloads(self):
        self.config["mirror_cloud_folders"] = True
        self.store.set_preference("remarkable", self.config)
        self.assertFalse(self.engine.sync())
        item = self.cloud.create_folder("Independent folder", self.source_id, str(uuid.uuid4()), self.source_id)
        self.assertFalse(self.engine.sync())
        self.assertTrue((self.pc / item.name).is_dir())
        self.assertFalse(self.store.remarkable_items())

    def test_filed_pdf_registers_local_notes_independently_of_cloud(self):
        service = Service(self.store)
        self.config["integration_enabled"] = False
        self.store.set_preference("remarkable", self.config)
        with patch.object(service, "watch"), patch.object(service, "enqueue") as enqueue:
            service.filed_pdf({"path": str(self.pdf), "job_id": "filed-test", "details": {"title": "Paper"}})
            service.filed_pdf({"path": str(self.pdf), "job_id": "filed-test", "details": {"title": "Paper"}})
        self.assertEqual(len(self.store.projects()), 1)
        self.assertEqual(len(Registry(self.store).sources()), 1)
        self.assertEqual(self.store.preference("inbox_handoffs"), ["filed-test"])
        self.assertFalse(self.outbox.jobs())
        self.assertEqual(enqueue.call_count, 2)

    def test_worker_rejects_out_of_root_mapping_without_replacing_saved_routes(self):
        service = Service(self.store)
        service.remarkable.sync = Mock(side_effect=lambda **_: service.stop() or False)
        service.command("remarkable_map", "outside-folder", str(self.root / "Outside"))
        with self.assertLogs(level="ERROR"):
            service.run()
        self.assertNotIn("outside-folder", service.remarkable.config()["mappings"])
        self.assertIn("linked PC library root", self.store.preference("remarkable_error"))


if __name__ == "__main__":
    unittest.main()
