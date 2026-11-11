"""Run the publisher against a NEW disposable Zotero profile, never a real library.

Requires the optional pilot dependencies and an extracted Zotero 10 executable.
The test extension simulates consent only inside the generated test profile.
"""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from note_organizer.paper_registry import Registry
from note_organizer.remarkable_sources import save_source_bundle
from note_organizer.storage import Store
from note_organizer.zotero_pilot import Ledger, LocalZotero, Paper, PilotError, load_manifest, publish
from tools.zotero_pilot import prepare


def import_native_annotations(root, key):
    """Ask only the disposable test extension to run Zotero's own importer."""
    import uuid
    command_id = uuid.uuid4().hex
    temporary = root / "command.tmp"
    temporary.write_text(json.dumps({"id": command_id, "action": "importPDF", "key": key}), encoding="utf-8")
    temporary.replace(root / "command.json")
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        response = root / "response.json"
        if response.exists():
            value = json.loads(response.read_text(encoding="utf-8"))
            if value.get("id") == command_id:
                if value.get("error"):
                    raise RuntimeError("Disposable native PDF importer failed: " + value["error"])
                return value
        time.sleep(.1)
    raise RuntimeError("Disposable native PDF importer timed out")


def live_cloud_checks(client, root, library, cache, checked):
    """Add only synthetic documents below a newly recorded validation root."""
    import pymupdf
    import uuid
    from note_organizer.remarkable_cloud import CloudClient, TokenVault
    from note_organizer.zotero_bridge import Bridge, BridgeState
    token = TokenVault(library.resolve(strict=True).parent / "remarkable/device-token.dpapi").load()
    cloud = CloudClient(token, cache_dir=cache)
    initial = {r.id: r.hash for r in cloud.library()}
    root_id, child_id, pdf_id = [str(uuid.uuid4()) for _ in range(3)]
    # Record identities durably before the first external write, for recovery.
    (root / "live-cloud-scope.json").write_text(json.dumps({"root": root_id,
        "tablet_child": child_id, "tablet_pdf": pdf_id,
        "label": "Notable Zotero validation - safe to remove"}, indent=2), encoding="utf-8")
    cloud.create_folder("Notable Zotero validation - safe to remove", "", root_id)
    client.authorize()
    client.create("collection", {"key": "LIVE2345", "name": "Disposable live cloud validation"})
    client.create("collection", {"key": "LIVECH23", "name": "PC folder", "parentCollection": "LIVE2345"})
    client.create("item", {"key": "LIVEREF2", "itemType": "document", "title": "Synthetic PC paper",
                          "collections": ["LIVECH23"]})
    source = root / "live-cloud-fixture.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((60, 100), "syntheticcloudroundtrip nanoparticle")
        doc.save(source)
    client.create("item", {"key": "LIVEPDF2", "itemType": "attachment", "parentItem": "LIVEREF2",
        "linkMode": "imported_file", "contentType": "application/pdf", "title": "Synthetic original", "filename": "Synthetic.pdf"})
    client.upload("LIVEPDF2", source, "")
    bridge = Bridge(client, cloud, BridgeState(root / "live-bridge-state"), "LIVE2345", root_id)
    first = bridge.run(apply=True)
    assert not first["review"]
    documents = bridge.state.read("documents")
    assert len(documents) == 1
    sent_id = next(iter(documents))
    tablet_catalog = {r.id: r for r in cloud.library()}
    assert tablet_catalog[sent_id].parent == bridge.state.read("folders")["LIVECH23"]["cloud"]
    assert cloud.pdf_bundle(tablet_catalog[sent_id])[sent_id + ".pdf"] == source.read_bytes()
    checked("LIVE_cloud_accepts_PC_collection_and_PDF_and_returns_exact_baseline")
    cloud.create_folder("Tablet folder", root_id, child_id, root_id)
    cloud.upload_pdf(source.read_bytes(), "Synthetic tablet-side paper", child_id, pdf_id, root_id)
    second = bridge.run(apply=True)
    assert not second["review"]
    returned = next(r for r in second["actions"] if r.get("cloud") == pdf_id and "attachment_key" in r)
    assert "syntheticcloudroundtrip" in client.zot.fulltext_item(returned["attachment_key"])["content"]
    checked("LIVE_cloud_folder_and_PDF_addition_reach_disposable_Zotero_library")
    repeated = bridge.run(apply=True)
    assert not repeated["review"]
    assert all(r["publication_action"] == "unchanged" for r in repeated["actions"] if r["action"] == "tablet_to_zotero")
    final = {r.id: r.hash for r in cloud.library()}
    assert all(final.get(identity) == digest for identity, digest in initial.items()), "An unrelated cloud item changed during validation"
    assert len(final) - len(initial) == 5  # validation root, two folders, two PDFs
    checked("LIVE_repeat_has_no_duplicates_and_all_preexisting_cloud_item_hashes_are_unchanged",
            new_test_folders=3, new_test_pdfs=2)


def bridge_checks(client, root, checked):
    """Real local Zotero API with a synthetic tablet transport, no real account."""
    import pymupdf
    from note_organizer.printed_ocr import PrintedOCR
    from note_organizer.remarkable_cloud import TokenVault
    from note_organizer.zotero_bridge import Bridge, BridgeState
    from tests.test_zotero_bridge import FakeCloud
    from tests.test_remarkable import folder, fixture
    client.authorize()
    client.create("collection", {"key": "BRIDGE23", "name": "Isolated bridge"})
    client.create("collection", {"key": "DESK2345", "name": "Desktop subcollection", "parentCollection": "BRIDGE23"})
    client.create("item", {"key": "THESIS23", "itemType": "thesis", "title": "Desktop thesis",
                          "university": "Example University", "collections": ["DESK2345"]})
    source = root / "bridge-source.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((60, 100), "desktopbridgeoriginal")
        doc.save(source)
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    for key, title in (("ATTACH23", "Main text"), ("SUPP2345", "Supporting information")):
        client.create("item", {"key": key, "itemType": "attachment", "parentItem": "THESIS23",
            "linkMode": "imported_file", "contentType": "application/pdf", "title": title, "filename": title + ".pdf"})
        client.upload(key, source, "")
    cloud = FakeCloud()
    sub = folder("Tablet subcollection", cloud.root.id)
    cloud.records[sub.id] = sub
    item, bundle = fixture(sub.id, deleted_ink=True)
    content = json.loads(bundle[item.id + ".content"])
    content["tags"] = [{"name": "bridge-tablet-tag"}]
    bundle[item.id + ".content"] = json.dumps(content).encode()
    cloud.add(item, bundle)
    engine = Bridge(client, cloud, BridgeState(root / "bridge-state"), "BRIDGE23", cloud.root.id,
                    saved_annotations=lambda identity: ({"ocr": "bridgesavedtranscript"},) if identity == item.id else ())
    preview = engine.run()
    assert not preview["review"] and cloud.uploads == 0
    first = engine.run(apply=True)
    assert not first["review"]
    assert cloud.uploads == 2
    assert len(client.children("THESIS23")) == 4  # two retained originals + two tablet views
    assert client.get("item", "THESIS23")["university"] == "Example University"
    assert hashlib.sha256(client.file_path("ATTACH23").read_bytes()).hexdigest() == source_sha
    checked("bridge_desktop_PDF_and_SI_upload_to_tablet_and_return_under_existing_thesis")
    preferred_url = client.zot.item("THESIS23")["links"]["attachment"]["href"]
    preferred = preferred_url.rstrip("/").split("/")[-1]
    assert "Notable source attachment: ATTACH23" in client.get("item", preferred).get("note", "")
    checked("normal_Zotero_parent_open_prefers_tablet_view_of_original_main_PDF")
    links = engine.state.read("folders")
    assert len(links) == 3 and len(client.collections()) >= 3
    tablet = next(r for r in first["actions"] if r.get("cloud") == item.id and "attachment_key" in r)
    assert "bridgesavedtranscript" in client.zot.fulltext_item(tablet["attachment_key"])["content"]
    assert {t["tag"] for t in client.get("item", tablet["reference_key"])["tags"]} == {"bridge-tablet-tag"}
    checked("bridge_tablet_PDF_folders_tags_and_transcript_reach_real_Zotero")
    native = import_native_annotations(root, tablet["attachment_key"])
    assert native["types"].get("highlight") == 1 and native["types"].get("note") == 1
    checked("Zotero_native_PDF_importer_reads_returned_highlight_and_typed_note",
            external=native["external"], api_children=len(client.children(tablet["attachment_key"])),
            explicit_annotation_items=len(client.reader_annotations(tablet["attachment_key"])))
    repeated = engine.run(apply=True)
    assert not repeated["review"] and cloud.uploads == 2
    returned = [r for r in repeated["actions"] if r["action"] == "tablet_to_zotero"]
    assert all(r["publication_action"] == "unchanged" for r in returned)
    assert len(client.children("THESIS23")) == 4
    checked("bridge_repeat_has_no_duplicate_PDFs_or_feedback_uploads")
    newer, changed = fixture(sub.id)
    changed = {k.replace(newer.id, item.id): value for k, value in changed.items()}
    changed[item.id + ".pdf"] = bundle[item.id + ".pdf"]
    cloud.add(replace(item, hash=hashlib.sha256(b"".join(changed.values())).hexdigest(), files=[{"id": k} for k in changed]), changed)
    refreshed = engine.run(apply=True)
    assert not refreshed["review"] and cloud.uploads == 2
    assert any(r.get("attachment_key") == tablet["attachment_key"] and r.get("publication_action") == "update" for r in refreshed["actions"])
    with pymupdf.open(client.file_path(tablet["attachment_key"])) as doc:
        assert len(list(doc[0].annots())) == 3
    native = import_native_annotations(root, tablet["attachment_key"])
    assert native["types"] == {"highlight": 1, "note": 1}
    checked("Zotero_native_highlight_and_note_refresh_while_ink_remains_embedded_in_PDF",
            external=native["external"], api_children=len(client.children(tablet["attachment_key"])))
    checked("bridge_new_tablet_ink_refreshes_same_durable_Zotero_attachment")
    # Full application indexing of image-only and rotated scans, not just OCR.
    model = Path(__file__).resolve().parents[1] / ".test-tmp/ocr-route-preflight/lang"
    scan = root / "bridge-scanned.pdf"
    with pymupdf.open() as original, pymupdf.open() as raster:
        page = original.new_page(width=400, height=500)
        page.insert_text((40, 100), "scannedzirconium nanoparticle silver", fontsize=16)
        png = page.get_pixmap(dpi=200).tobytes("png")
        for rotation in (0, 90):
            target = raster.new_page(width=400, height=500)
            target.insert_image(target.rect, stream=png)
            target.set_rotation(rotation)
        raster.save(scan)
    sha = hashlib.sha256(scan.read_bytes()).hexdigest()
    client.printed_ocr = PrintedOCR(model, root / "printed-cache")
    scanned = Paper("bridge-scan", scan, sha, {"title": "Scanned literature fixture"})
    published = publish(client, Ledger(root / "ocr-publisher"), "ocr-pilot", [scanned], apply=True)["papers"][0]
    indexed = client.zot.fulltext_item(published["attachment_key"])["content"]
    assert indexed.count("scannedzirconium") >= 2
    assert not published["pages_without_text"]
    assert any(row["key"] in {published["attachment_key"], published["reference_key"]}
               for row in client.zot.items(q="scannedzirconium", qmode="everything"))
    assert hashlib.sha256(scan.read_bytes()).hexdigest() == sha
    checked("printed_OCR_finds_upright_and_rotated_scan_in_real_Zotero_without_rewriting_PDF")
    client.printed_ocr = None
    # DPAPI encrypts only remembered grants, tied to the selected server ID.
    vault = TokenVault(root / "grant.dpapi")
    client.grant_store = vault
    client.zot.local_api_key = None
    client.authorize()
    second = LocalZotero(int(client.endpoint.split(":")[2].split("/")[0]), grant_store=vault, interactive=False)
    try:
        assert second.zot.local_api_key and second.server_id == client.server_id
        second.zot.set_fulltext(published["attachment_key"], {"content": indexed, "indexedPages": 2, "totalPages": 2})
        assert second.zot.local_api_key.encode() not in vault.path.read_bytes()
    finally:
        second.close()
    checked("remembered_local_write_grant_reconnects_from_DPAPI_without_new_prompt")


BOOTSTRAP = """
async function startup({rootURI}) {
  const config = JSON.parse(await (await fetch(rootURI + 'config.json')).text());
  Zotero.uiReadyPromise.then(async () => {
    if (Zotero.DataDirectory.dir !== config.data) throw new Error('Unexpected data directory');
    Zotero.Server.LocalAPI._promptForAuthorization = async appName => ({
      allow: appName === 'Notable Zotero Pilot', remember: true
    });
    await IOUtils.writeUTF8(config.ready, JSON.stringify({version: Zotero.version,
      server: Zotero.Server.LocalAPI.getServerID()}));
    const deadline = Date.now() + 600000;
    while (Date.now() < deadline && !(await IOUtils.exists(config.done))) {
      const commandPath = PathUtils.join(PathUtils.parent(config.ready), 'command.json');
      if (await IOUtils.exists(commandPath)) {
        const command = JSON.parse(await IOUtils.readUTF8(commandPath));
        await IOUtils.remove(commandPath);
        let response = {id: command.id};
        try {
          if (command.action !== 'importPDF' || !/^[A-Z2-9]{8}$/.test(command.key)) throw new Error('Invalid command');
          let item = await Zotero.Items.getByLibraryAndKeyAsync(Zotero.Libraries.userLibraryID, command.key);
          await item.loadAllData();
          await Zotero.PDFWorker.import(item.id, true);
          await item.loadAllData();
          let annotations = item.getAnnotations();
          response.types = {};
          for (const annotation of annotations) {
            response.types[annotation.annotationType] = (response.types[annotation.annotationType] || 0) + 1;
          }
          response.external = annotations.filter(a => a.annotationIsExternal).length;
        } catch (error) { response.error = error.name; }
        await IOUtils.writeUTF8(PathUtils.join(PathUtils.parent(config.ready), 'response.json'), JSON.stringify(response));
      }
      await new Promise(resolve => setTimeout(resolve, 500));
    }
    Services.startup.quit(Ci.nsIAppStartup.eAttemptQuit);
  });
}
function install() {} function shutdown() {} function uninstall() {}
"""


def main():
    import argparse
    import pymupdf
    from tests.test_remarkable import fixture
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zotero-exe", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True, help="New directory; refuses an existing one")
    parser.add_argument("--port", type=int, default=23129)
    parser.add_argument("--live-cloud-library", type=Path,
                        help="Opt-in live account trial: adds only a new labeled validation subtree")
    parser.add_argument("--cloud-cache", type=Path,
                        help="Optional existing metadata blob cache to avoid downloading it twice")
    args = parser.parse_args()
    root = args.work_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    profile, data, plugin = (root / name for name in ("profile", "zotero-data", "extension"))
    for directory in (profile / "extensions", data, plugin):
        directory.mkdir(parents=True, exist_ok=True)
    config = {"data": str(data), "ready": str(root / "ready.json"), "done": str(root / "done")}
    (plugin / "config.json").write_text(json.dumps(config), encoding="utf-8")
    (plugin / "bootstrap.js").write_text(BOOTSTRAP, encoding="utf-8")
    extension_id = "notable-pilot-test@example.invalid"
    (plugin / "manifest.json").write_text(json.dumps({"manifest_version": 2, "name": "Disposable publisher test",
        "version": "0.0.1", "applications": {"zotero": {"id": extension_id,
        "strict_min_version": "10.0", "strict_max_version": "10.*", "update_url": "https://example.invalid/updates.json"}}}), encoding="utf-8")
    (profile / "extensions" / extension_id).write_text(str(plugin), encoding="utf-8")
    from tools.build_zotero_support import build
    build(profile / "extensions" / "notable-companion@local.invalid.xpi")
    prefs = {"extensions.zotero.firstRun2": False, "extensions.zotero.firstRunGuidance": False,
        "extensions.zotero.useDataDir": True, "extensions.zotero.dataDir": str(data),
        "extensions.zotero.warnOnUnsafeDataDir": False, "extensions.zotero.sync.autoSync": False,
        "extensions.zotero.automaticScraperUpdates": False, "extensions.zotero.httpServer.enabled": True,
        "extensions.zotero.httpServer.port": args.port, "extensions.zotero.httpServer.localAPI.enabled": True,
        "extensions.zoteroWinWordIntegration.skipInstallation": True,
        "extensions.zoteroOpenOfficeIntegration.skipInstallation": True,
        "extensions.update.enabled": False, "extensions.enabledScopes": 15,
        "extensions.autoDisableScopes": 0, "extensions.startupScanScopes": 15,
        "app.update.enabled": False, "app.update.auto": False}
    (profile / "user.js").write_text("\n".join(f"user_pref({json.dumps(k)}, {json.dumps(v)});" for k, v in prefs.items()), encoding="utf-8")
    startup = None
    if os.name == "nt":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
    result, client = {"checks": [], "scope": "new synthetic profile only"}, None
    def checked(name, **details):
        result["checks"].append({"name": name, "status": "PASS", **details})
    with (root / "stdout.log").open("w") as stdout, (root / "stderr.log").open("w") as stderr:
        process = subprocess.Popen([str(args.zotero_exe.resolve(strict=True)), "-no-remote", "-profile", str(profile),
            "-datadir", str(data), "-headless"], startupinfo=startup, stdout=stdout, stderr=stderr)
        try:
            deadline = time.monotonic() + 60
            while not Path(config["ready"]).exists():
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("Disposable Zotero did not become ready")
                time.sleep(.25)
            result["zotero"] = json.loads(Path(config["ready"]).read_text(encoding="utf-8"))["version"]
            client = LocalZotero(args.port)
            assert client.server_id == json.loads(Path(config["ready"]).read_text(encoding="utf-8"))["server"], "Refuse a different Zotero instance on the test port"
            deadline = time.monotonic() + 15
            while client.http.get(client.endpoint + "/notable/status").status_code != 200:
                if time.monotonic() > deadline:
                    raise RuntimeError("Disposable support extension did not become ready")
                time.sleep(.2)
            assert client.can_refresh_reader
            checked("thin_support_extension_loads_in_Zotero_10")
            store = Store(root / "notable" / "library.sqlite3")
            item, bundle = fixture(deleted_ink=True)
            original = bundle[item.id + ".pdf"]
            save_source_bundle(store, item, bundle)
            paper_id = Registry(store).register_cloud(item.id, hashlib.sha256(original).hexdigest(),
                                                      title="Synthetic dissertation")["paper_id"]
            metadata = {"itemType": "thesis", "title": "Synthetic dissertation", "university": "Example University", "thesisType": "Ph.D."}
            database_before = store.path.read_bytes()
            manifest = prepare(store.path, paper_id, root / "prepared", metadata=metadata)
            assert store.path.read_bytes() == database_before
            namespace, papers = load_manifest(manifest)
            # A previously saved transcript is handed off, not recognized here.
            papers = [replace(papers[0], annotations=({"ocr": "savedhandwritingtranscript"},))]
            ledger = Ledger(root / "helper")
            plan = publish(client, ledger, namespace, papers)
            assert plan["papers"][0]["action"] == "create"
            assert not client.zot.top()
            checked("plan_leaves_Zotero_empty_and_Notable_database_unchanged")
            first = publish(client, ledger, namespace, papers, apply=True)["papers"][0]
            key, reference = first["attachment_key"], first["reference_key"]
            assert client.get("item", reference)["university"] == "Example University"
            checked("publish_annotated_PDF_and_thesis_metadata")
            indexed = client.zot.fulltext_item(key)["content"]
            for term in ("selected scientific", "Typed margin note", "savedhandwritingtranscript"):
                assert term in indexed
            # Verify library retrieval, not merely a locally constructed string.
            for query in ("selected", "Typed", "savedhandwritingtranscript"):
                matches = client.zot.items(q=query, qmode="everything")
                assert any(row["key"] in {key, reference} for row in matches)
            checked("highlight_quote_typed_comment_and_saved_transcript_are_searchable")
            client.zot.authorize_local("Notable Zotero Pilot")
            record = client.zot.item(reference)["data"]
            record["title"] = "Reviewed title in Zotero"
            client.zot.update_items([record])
            assert publish(client, ledger, namespace, papers, apply=True)["papers"][0]["action"] == "unchanged"
            assert len(client.zot.children(reference)) == 1
            checked("repeat_keeps_one_attachment_and_user_metadata")
            # Same cached tablet identity/baseline, newer synthetic scene with added ink.
            revised, changed = fixture(deleted_ink=False)
            changed = {name.replace(revised.id, item.id): value for name, value in changed.items()}
            changed[item.id + ".pdf"] = original
            revised = replace(item, hash=hashlib.sha256(b"".join(changed.values())).hexdigest())
            save_source_bundle(store, revised, changed)
            second_manifest = prepare(store.path, paper_id, root / "prepared", metadata=metadata)
            namespace, updated = load_manifest(second_manifest)
            updated = [replace(updated[0], annotations=papers[0].annotations)]
            assert updated[0].sha256 != papers[0].sha256
            second = publish(client, ledger, namespace, updated, apply=True)["papers"][0]
            assert second["attachment_key"] == key and second["action"] == "update"
            assert len(client.zot.children(reference)) == 1
            assert client.get("item", reference)["title"] == "Reviewed title in Zotero"
            assert "scientific" in client.zot.fulltext_item(key)["content"]
            current = ledger.directory / "versions" / updated[0].sha256 / "Current.pdf"
            with pymupdf.open(current) as pdf:
                assert len(list(pdf[0].annots())) == 3
            assert client.file_md5(key) == hashlib.md5(current.read_bytes()).hexdigest()
            checked("tablet_scene_revision_refreshes_same_attachment_with_three_marks")
            old = ledger.directory / "versions" / papers[0].sha256 / "Current.pdf"
            assert old.exists() and original == bundle[item.id + ".pdf"]
            checked("previous_prepared_version_and_original_retained")
            long_path = root / "long-thesis.pdf"
            with pymupdf.open() as document:
                for number in range(125):
                    page = document.new_page()
                    remaining = page.insert_textbox(pymupdf.Rect(50, 50, 550, 740),
                        ("Synthetic zirconium nanoparticle gold silver spectroscopy scientific reference.\n" * 70), fontsize=6)
                    assert remaining >= 0
                document[-1].insert_text((50, 770), "pilotlongtailmarker")
                character_count = sum(len(page.get_text()) for page in document)
                assert character_count > 500000
                document.save(long_path)
            long_paper = Paper("long-thesis-fixture", long_path, hashlib.sha256(long_path.read_bytes()).hexdigest(),
                               {"itemType": "thesis", "title": "Long synthetic thesis"})
            long_result = publish(client, ledger, namespace, [long_paper], apply=True)["papers"][0]
            matches = client.zot.items(q="pilotlongtailmarker", qmode="everything")
            assert any(row["key"] in {long_result["attachment_key"], long_result["reference_key"]} for row in matches)
            checked("long_thesis_search_finds_marker_beyond_100_pages_and_500000_characters",
                    pages=125, characters=character_count)
            # Mimic a native index rebuild that contains only printed PDF text.
            client.zot.authorize_local("Notable Zotero Pilot")
            client.zot.set_fulltext(key, {"content": "A selected scientific result", "indexedPages": 1, "totalPages": 1})
            repaired = publish(client, ledger, namespace, updated, apply=True)["papers"][0]
            assert repaired["action"] == "unchanged"
            assert "savedhandwritingtranscript" in client.zot.fulltext_item(key)["content"]
            checked("native_text_index_rebuild_is_repaired_without_PDF_reupload")
            bridge_checks(client, root, checked)
            client.create("item", {"key": "NATIVE23", "itemType": "annotation", "parentItem": key,
                "annotationType": "note", "annotationComment": "Independent desktop note",
                "annotationColor": "#ffd400", "annotationPageLabel": "1",
                "annotationSortIndex": "00000|000000|00000",
                "annotationPosition": json.dumps({"pageIndex": 0, "rects": [[10, 10, 30, 30]]})})
            previous_md5 = client.file_md5(key)
            blocked = replace(updated[0], path=long_path, sha256=long_paper.sha256)
            try:
                publish(client, ledger, namespace, [blocked], apply=True)
            except PilotError as error:
                assert "Zotero reader annotations" in str(error)
            else:
                raise AssertionError("Independent native reader annotations did not block replacement")
            assert client.file_md5(key) == previous_md5
            checked("native_Zotero_reader_notes_block_PDF_replacement_and_are_preserved")
            url = client.endpoint + "/notable/annotations/" + key
            assert client.http.post(url, json={}, headers={"Zotero-Server-ID": client.server_id}).status_code == 401
            assert client.http.get(url).status_code == 428
            assert client.http.get(url, headers={"Zotero-Server-ID": "wrong-instance"}).status_code == 412
            checked("support_extension_reuses_local_API_write_authorization_and_requires_instance_pin")
            if args.live_cloud_library:
                live_cloud_checks(client, root, args.live_cloud_library,
                                  args.cloud_cache or root / "cloud-blobs", checked)
            client.close()
            client = None
            Path(config["done"]).touch()
            process.wait(timeout=15)
            Path(config["done"]).unlink()
            Path(config["ready"]).unlink()
            process = subprocess.Popen([str(args.zotero_exe.resolve()), "-no-remote", "-profile", str(profile),
                "-datadir", str(data), "-headless"], startupinfo=startup, stdout=stdout, stderr=stderr)
            deadline = time.monotonic() + 60
            while not Path(config["ready"]).exists():
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("Disposable Zotero restart did not become ready")
                time.sleep(.25)
            client = LocalZotero(args.port)
            assert client.server_id == json.loads(Path(config["ready"]).read_text(encoding="utf-8"))["server"]
            for query, expected in (("savedhandwritingtranscript", {key, reference}),
                    ("pilotlongtailmarker", {long_result["attachment_key"], long_result["reference_key"]})):
                assert any(row["key"] in expected for row in client.zot.items(q=query, qmode="everything"))
            checked("transcript_and_long_thesis_search_survive_Zotero_restart")
        except Exception as error:
            result["error"] = type(error).__name__
            # Keep raw API errors/credentials out of artifacts and terminal output.
            if isinstance(error, PilotError):
                result["detail"] = str(error)
            raise
        finally:
            if client:
                client.close()
            Path(config["done"]).touch()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=10)
            (root / "results.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
            print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
