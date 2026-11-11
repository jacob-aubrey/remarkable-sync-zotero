"""Resume a disposable live-cloud trial after a human adds tablet annotations.

Reads the real cloud but rejects every cloud write. Only the existing disposable
Zotero profile and its explicitly recorded mappings may be updated. Production
Zotero and the Notable library are never opened for writing.
"""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pymupdf
from note_organizer.remarkable_cloud import CloudClient, TokenVault
from note_organizer.remarkable_sources import source_bundle
from note_organizer.zotero_bridge import Bridge, BridgeState
from note_organizer.zotero_pilot import LocalZotero, PilotError, stable_key
from tools.build_zotero_support import build
from tools.test_zotero_pilot_live import BOOTSTRAP


class ReadOnlyCloud(CloudClient):
    def put(self, *args, **kwargs):
        raise AssertionError("Cloud writes are forbidden in physical validation")


class OfflineCloud:
    """Only previously downloaded immutable records; no network client."""
    def __init__(self, records, bundles):
        self.records, self.bundles = records, bundles

    def library(self):
        return self.records

    def pdf_bundle(self, item):
        return self.bundles[item.id]

    def create_folder(self, *args, **kwargs):
        raise AssertionError("Offline replay cannot create folders")

    def upload_pdf(self, *args, **kwargs):
        raise AssertionError("Offline replay cannot upload PDFs")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def summarize(path):
    counts, highlights = Counter(), []
    with pymupdf.open(path) as document:
        for page in document:
            for annotation in page.annots() or ():
                counts[annotation.type[1]] += 1
                if annotation.type[1] == "Highlight":
                    kind, text = document.xref_get_key(annotation.xref, "RMQuote")
                    if kind == "string" and text.strip():
                        highlights.append(text.strip())
        return len(document), dict(counts), highlights


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trial", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--cloud-cache", type=Path, required=True)
    parser.add_argument("--zotero-exe", type=Path, required=True)
    parser.add_argument("--port", type=int, default=23129)
    parser.add_argument("--current-revision", action="store_true",
                        help="Verify the already received physical revision without requiring another pen stroke")
    parser.add_argument("--handwriting-models", type=Path)
    args = parser.parse_args()
    workspace = Path(__file__).resolve().parents[1]
    root = args.trial.resolve(strict=True)
    if not root.is_relative_to((workspace / ".test-tmp").resolve()):
        parser.error("Only a disposable trial inside this workspace's .test-tmp is allowed")
    scope = json.loads((root / "live-cloud-scope.json").read_text(encoding="utf-8"))
    if scope["label"] != "Notable Zotero validation - safe to remove":
        parser.error("The recorded synthetic validation scope is required")
    config = json.loads((root / "extension/config.json").read_text(encoding="utf-8"))
    data, profile = root / "zotero-data", root / "profile"
    assert config == {"data": str(data), "ready": str(root / "ready.json"), "done": str(root / "done")}
    expected_server = json.loads((root / "ready.json").read_text(encoding="utf-8"))["server"]
    with socket.socket() as connection:
        connection.settimeout(1)
        if connection.connect_ex(("127.0.0.1", args.port)) == 0:
            parser.error("The disposable test port is occupied; no process was changed")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    library = args.library.resolve(strict=True)
    original_library = digest(library)
    state = BridgeState(root / "live-bridge-state")
    previous = state.read("documents")[scope["tablet_pdf"]]
    cloud = ReadOnlyCloud(TokenVault(library.parent / "remarkable/device-token.dpapi").load(),
                         cache_dir=args.cloud_cache)
    result = {"scope": "existing synthetic tablet subtree and disposable Zotero profile only",
              "cloud_writes": 0, "checks": []}

    def checked(name, **details):
        result["checks"].append({"name": name, "status": "PASS", **details})
        print(name + ": PASS", flush=True)

    process, client = None, None
    stdout = (output / "stdout.log").open("w")
    stderr = (output / "stderr.log").open("w")
    startup = None
    if os.name == "nt":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE

    def start():
        nonlocal process, client
        for name in ("done", "ready.json", "command.json", "response.json"):
            (root / name).unlink(missing_ok=True)
        process = subprocess.Popen([str(args.zotero_exe.resolve(strict=True)), "-no-remote", "-profile",
            str(profile), "-datadir", str(data), "-headless"], startupinfo=startup, stdout=stdout, stderr=stderr)
        deadline = time.monotonic() + 60
        while not (root / "ready.json").exists():
            if process.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("Disposable profile did not become ready")
            time.sleep(.25)
        ready = json.loads((root / "ready.json").read_text(encoding="utf-8"))
        assert ready["server"] == expected_server
        handwriting = None
        if args.handwriting_models:
            from note_organizer.handwriting_ocr import HandwritingOCR
            handwriting = HandwritingOCR(args.handwriting_models, output / "handwriting-cache")
        client = LocalZotero(args.port, handwriting_ocr=handwriting,
                            grant_store=TokenVault(root / "grant.dpapi"))
        assert client.server_id == expected_server, "Refuse another Zotero instance"
        deadline = time.monotonic() + 15
        while client.http.get(client.endpoint + "/notable/status").status_code != 200:
            if time.monotonic() > deadline:
                raise RuntimeError("Support plugin did not load")
            time.sleep(.2)
        assert client.can_refresh_reader
        result["zotero"] = ready["version"]

    def stop():
        nonlocal client
        if client:
            client.close()
            client = None
        if process:
            (root / "done").touch()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=10)

    try:
        catalog = cloud.library()
        target = next(row for row in catalog if row.id == scope["tablet_pdf"])
        assert target.parent == scope["tablet_child"] and not target.deleted
        if args.current_revision:
            assert target.hash == previous["revision"], "A newer revision arrived; run the fresh-revision test first"
            checked("recorded_physical_tablet_revision_reverified")
        else:
            assert target.hash != previous["revision"], "No new physical tablet revision has reached the cloud"
            checked("fresh_physical_tablet_revision_received")
        (root / "extension/bootstrap.js").write_text(BOOTSTRAP, encoding="utf-8")
        build(profile / "extensions/notable-companion@local.invalid.xpi")
        start()
        engine = Bridge(client, cloud, state, "LIVE2345", scope["root"])
        key = stable_key(engine.namespace, "attachment", target.id)
        assert client.get("item", key), "The existing mapped attachment is required"
        old_pdf = digest(client.file_path(key))
        original_pc_pdf = digest(client.file_path("LIVEPDF2"))
        before_keys = {row["key"] for collection in client.collections()
                       for row in client.collection_items(collection["key"])}
        first = engine.run(apply=True)
        assert not first["review"]
        changed = next(row for row in first["actions"] if row.get("cloud") == target.id)
        assert changed["attachment_key"] == key
        assert changed["publication_action"] == ("unchanged" if args.current_revision else "update")
        path = client.file_path(key)
        current = state.read("documents")[target.id]
        assert (digest(path) == old_pdf) == args.current_revision
        assert digest(path) == current["rendered"]["sha256"]
        assert current["baseline"] == previous["baseline"]
        assert original_pc_pdf == digest(client.file_path("LIVEPDF2"))
        checked("same_durable_Zotero_attachment_verified_original_baselines_unchanged"
                if args.current_revision else "same_durable_Zotero_attachment_updated_original_baselines_unchanged")
        pages, counts, quotes = summarize(path)
        assert counts.get("Ink", 0) > 0 and counts.get("Highlight", 0) > 0 and quotes
        checked("physical_ink_and_highlight_preserved", pages=pages, annotations=counts)
        native = client.reader_annotations(key)
        assert any(row.get("annotationType") == "highlight" and row.get("annotationText", "").strip() in quotes
                   for row in native)
        checked("physical_highlight_available_in_Zotero_native_annotations")
        content = client.zot.fulltext_item(key)["content"]
        assert all(quote in content for quote in quotes)
        assert any(row["key"] in {key, changed["reference_key"]}
                   for row in client.zot.items(q=quotes[0], qmode="everything"))
        checked("highlight_quote_present_in_persistent_library_search")
        if client.handwriting_ocr:
            baseline = source_bundle(engine.sources, target.id, revision=target.hash)[target.id + ".pdf"]
            with pymupdf.open(stream=baseline, filetype="pdf") as clean:
                assert "testing" not in "\n".join(page.get_text() for page in clean).lower()
            assert "testing" in content.lower() and "Machine-read handwriting (unverified)" in content
            assert any(row["key"] in {key, changed["reference_key"]}
                       for row in client.zot.items(q="testing", qmode="everything"))
            checked("actual_machine_recognized_handwriting_only_word_found_in_library_search")
        (output / "physical-tablet.pdf").write_bytes(path.read_bytes())
        with pymupdf.open(path) as document:
            for page in document:
                page.get_pixmap(dpi=110).save(output / f"physical-page-{page.number + 1}.png")
        repeated = engine.run(apply=True)
        assert not repeated["review"]
        assert all(row["publication_action"] == "unchanged" for row in repeated["actions"]
                   if row["action"] == "tablet_to_zotero")
        after_keys = {row["key"] for collection in client.collections()
                      for row in client.collection_items(collection["key"])}
        assert before_keys == after_keys
        checked("repeated_live_sync_has_no_duplicate_items_or_upload_feedback")
        saved = state.read("documents")
        final_catalog = cloud.library()
        assert target.hash == next(row.hash for row in final_catalog if row.id == target.id)
        frozen = OfflineCloud(final_catalog, {identity: source_bundle(engine.sources, identity, revision=row["revision"])
                                             for identity, row in saved.items()})
        expected_pdf = digest(path)
        stop()
        start()
        assert digest(client.file_path(key)) == expected_pdf
        assert quotes[0] in client.zot.fulltext_item(key)["content"]
        assert any(row["key"] in {key, changed["reference_key"]}
                   for row in client.zot.items(q=quotes[0], qmode="everything"))
        checked("physical_PDF_and_highlight_search_survive_Zotero_restart")
        if client.handwriting_ocr:
            assert any(row["key"] in {key, changed["reference_key"]}
                       for row in client.zot.items(q="testing", qmode="everything"))
            checked("actual_handwriting_search_survives_Zotero_restart")
        replay = Bridge(client, frozen, BridgeState(root / "live-bridge-state"), "LIVE2345", scope["root"]).run(apply=True)
        assert not replay["review"]
        assert all(row["publication_action"] == "unchanged" for row in replay["actions"]
                   if row["action"] == "tablet_to_zotero")
        assert digest(client.file_path(key)) == expected_pdf
        checked("offline_saved_source_replay_preserves_same_attachment_without_duplicates")
        assert original_library == digest(library)
        checked("production_Notable_library_unchanged")
        result["handwriting_recognition"] = ("Local RapidOCR PP-OCRv6 transcription and handwriting-only keyword retrieval tested; accuracy remains imperfect"
            if args.handwriting_models else "Ink preserved visually; no recognizer or new transcript tested")
        result["status"] = "PASS"
    except Exception as error:
        result["status"] = "FAIL"
        result["error_type"] = type(error).__name__
        if isinstance(error, PilotError):
            result["detail"] = str(error)
        raise
    finally:
        stop()
        stdout.close()
        stderr.close()
        (output / "results.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
