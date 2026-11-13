# reMarkable Sync for Zotero

Sync PDFs, tablet annotations, and tags between Zotero and reMarkable. Review incoming papers, search your library, and resolve sync conflicts in Zotero.

## Install on Windows

The plugin supports Zotero 10.0.6 and later in the 10.0 series.

1. Download the XPI from the [latest release](https://github.com/jacob-aubrey/remarkable-sync-zotero/releases/latest).
2. In Zotero, open **Tools → Plugins**. From the gear menu, choose **Install Plugin From File** and select the XPI. You can install it over an earlier local copy.
3. Open the plugin's details and set **Allow automatic updates** to **On**.
4. Open **Zotero Settings → reMarkable Sync → Open Sync Setup** to connect your folders and manage automatic sync.

The XPI includes the Windows helper and OCR models. Zotero handles future updates through its plugin manager.

## Find papers and tags

In **Linked papers**, the citation, fields and tags, and full-text modes use Zotero Quick Search within the selected plugin view. Choose **Filenames** to find a Zotero attachment or reMarkable copy by name. Use **Ctrl+F** or **Ctrl+Shift+K** to focus search, and **Esc** to clear the query.

In **Tags**, type to narrow the list of tag names, then select tags to filter results as you go. Paper tags are shared with the Zotero reference. **Show Zotero tags** opens the reference’s native Tags section. Page tags are saved for individual pages of linked PDFs. Use the tag button in the PDF reader to edit paper and page tags together.

### Merge duplicates in Zotero

Use Zotero’s **Merge Items** for duplicate references. On the next sync, the plugin follows the native merge when it can verify the connected PDFs and the existing tablet copy. It can also follow Zotero Undo when the original PDF and reference return. If Zotero combined both linked references or the PDF changed, **Sync Conflicts** shows the affected connection for review. Use Zotero Undo to recover the original arrangement when you need to keep both links. [Zotero duplicate management](https://www.zotero.org/support/duplicate_detection)

### Attach a download from ToSort

If a PDF in your configured ToSort folder identifies a unique Zotero reference that needs a PDF, Inbox shows the download on that reference. Choose **Attach PDF from ToSort** to store the PDF in Zotero. The plugin clears the original from ToSort after verifying the stored copy, then opens the filename and filing review. Choose **Review ToSort file separately** to use the usual ToSort workflow instead.

Matching runs locally. It uses explicit DOI evidence read by the bundled [PyMuPDF](https://pymupdf.readthedocs.io/en/latest/document.html) and the existing DOI normalizer. A printed DOI also needs a matching paper title. Attaching uses Zotero’s existing [stored-copy workflow](https://www.zotero.org/support/attaching_files).

Inbox lets you edit citation details and add paper tags before approval. Changing the document type updates the suggested filename. Use **Suggest filename** to include citation edits or replace a manually entered name. Approval saves the edited fields using the Zotero reference’s existing item type and adds your tags to its current tags. [Zotero item fields](https://www.zotero.org/support/kb/item_types_and_fields) · [Zotero write API](https://www.zotero.org/support/dev/web_api/v3/write_requests)

### Confirm a linked folder deletion

If a linked subfolder is deleted or moved out of the connection, its folder pair pauses while the other folders continue syncing. Open **Sync Conflicts** to review the folder by name. You can keep the deletion on one device, also trash the matching folder, or restore the original folder when it is still in Trash and its previous parent is available.

Before trashing a Zotero collection, review its contents and each item’s collection locations. Items with other locations start unchecked for Trash. Trashing a reference everywhere also takes its attachments and notes out of the active library. Removing a reference from checked collections leaves it and its attachments in My Library. For an individual paper, you can also trash just the linked PDFs. Trashing a reMarkable folder moves its contents out of the active library. [Zotero collections and Trash](https://www.zotero.org/support/collections_and_tags)

Confirmed choices show on their cards and are saved while sync runs. Cancel a choice before it starts, or make decisions on other cards while it waits. If the reviewed items or locations change, the affected decision asks for another review.

## Build from source

Use Windows x64 and Python 3.13. Download **third-party-source.zip** from the matching GitHub release and extract it. Its `ocr-build-resources` directory contains the pinned models and their notices.

```powershell
py -3.13 -m venv .venv
$python = '.\.venv\Scripts\python.exe'
& $python -m pip install --require-hashes -r requirements-windows.lock
& $python tools/verify_vendored_core.py
$env:NOTABLE_BUILD_RESOURCES = (Resolve-Path './ocr-build-resources').Path
& $python -m PyInstaller --noconfirm --distpath build/runtime 'Notable Zotero Worker.spec'
& $python tools/build_zotero_support.py --output dist/reMarkable-Sync-for-Zotero-0.19.20.xpi --runtime build/runtime/notable-worker.exe --verify-runtime --update-manifest
```

The build generates `updates.json` beside the XPI. For a release, tag the matching source, attach both files and the corresponding source to the GitHub release, and verify the downloads before publishing it as the latest release. Keep the version in the filename and the plugin manifest in sync.

Run `python tools/run_test_batches.py` for Python tests. Run the scripts matching `tools/test_*.cjs` with Node.js for JavaScript tests.

See [Sync design](documentation/SYNC_DESIGN.md) for the worker, edit queue, and upstream design references.

## License

The project's source code is licensed under the [GNU Affero General Public License, version 3](LICENSE). Third-party components keep their own licenses. The release includes the [bundled component notices](documentation/ZOTERO_PLUGIN_NOTICES.md) and matching third-party source.
