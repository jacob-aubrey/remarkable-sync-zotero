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

Select one tag and choose **Generate Word notebook…** to save a snapshot of its PDF annotations. Paper tags include the matching papers’ annotations, and page tags include the tagged pages. You can also right-click a collection in Zotero and choose **reMarkable Sync → Generate Word notebook…**, with the option to include subcollections. The export uses the Reading layout and links back to the relevant pages in Zotero. The Windows helper already bundles the MIT-licensed [python-docx](https://github.com/python-openxml/python-docx) writer.

### Right-click shortcuts

Right-click a reference, PDF, several selected items, or a collection, then open **reMarkable Sync →**. The submenu offers Word notebook export and commands for the selected links, including opening the tablet PDF, reviewing suggested tags, rechecking keywords, checking a connection, pausing or resuming a link, rebuilding its search index, and exporting source-linked annotations. Selecting a PDF targets that attachment’s link. Select several references or PDFs to export one notebook or show their links together.

Right-click a tag in Zotero’s tag selector, a reference’s Tags pane, the reader’s annotation tag selector, or the plugin’s tag lists, then choose **reMarkable Sync → Generate Word notebook for this tag…**. Native tag exports use the clicked tag in its library. A reference tag includes its PDF annotations, a PDF tag includes that PDF’s annotations, and an annotation tag includes the tagged annotations. Plugin page tags retain their page scope.

Choose **Sync selected papers now** to update the linked PDFs for the selected references. A PDF selection offers **Sync this PDF now** or **Sync selected PDFs now** for those exact attachment links. Collections offer **Sync collection and subcollection papers now**. These actions preserve the global pause setting and leave unrelated filing, deletion, tag-edit, and folder-sync work pending. Papers that need linking or approval still go through Inbox.

In an open linked PDF, right-click and choose **reMarkable Sync → Sync this PDF now**. Selected syncs show progress in Zotero’s bottom-right popup. Every sync refreshes an updated linked PDF in its existing reader, including its annotation sidebar, while retaining the reading page and zoom. New tablet notes appear once the tablet has uploaded them to reMarkable Cloud and the plugin receives them.

The workspace’s **Sync now** button checks the whole connection and changes to **Pause sync** during the check. Once the check pauses safely, choose **Resume sync** to check again. If automatic sync is on, the next scheduled check starts on its own, normally after 30 minutes. Right-click sync keeps the workspace button’s current label.

Existing papers receive the same passage grouping as new papers: consecutive highlighted text of one color becomes one annotation. Handwriting regions include contained words and use more forgiving spacing. Annotation tags, saved note content, and old source links are preserved during regrouping, and stale image thumbnails are rebuilt before the sync is recorded as complete.

Pausing a link, resuming it, and rebuilding its search index require a confirmation that identifies the paper and explains the effect. Export file pickers confirm replacement of existing files. Connection and annotation repair decisions retain their existing review steps. See the [command audit and integration notes](documentation/CONTEXT_MENUS.md).

### Freehand marks in image notes

Freehand strokes from every supported drawing tool, including highlighters, become image notes with the underlying page content. Single marks and large figure outlines are included. Snapped text highlights remain text highlights. After updating, sync to regenerate older linked PDFs from their retained tablet layers; existing source PDFs remain available.

### Merge duplicates in Zotero

Use Zotero’s **Merge Items** for duplicate references. On the next sync, the plugin follows the native merge when it can verify the connected PDFs and the existing tablet copy. It can also follow Zotero Undo when the original PDF and reference return. If Zotero combined both linked references or the PDF changed, **Sync Conflicts** shows the affected connection for review. Use Zotero Undo to recover the original arrangement when you need to keep both links. [Zotero duplicate management](https://www.zotero.org/support/duplicate_detection)

### Attach a download from ToSort

If a PDF in your configured ToSort folder identifies a unique Zotero reference that needs a PDF, Inbox shows the download on that reference. Choose **Attach PDF from ToSort** to store the PDF in Zotero. The plugin clears the original from ToSort after verifying the stored copy, then opens the filename and filing review. Choose **Review ToSort file separately** to use the usual ToSort workflow instead.

Matching runs locally. It uses explicit DOI evidence read by the bundled [PyMuPDF](https://pymupdf.readthedocs.io/en/latest/document.html) and the existing DOI normalizer. A printed DOI also needs a matching paper title. Attaching uses Zotero’s existing [stored-copy workflow](https://www.zotero.org/support/attaching_files).

Open **Citation details…** in Inbox to edit a draft using Zotero’s Info editor, including creator roles, names, and order. Choose **Use citation** to return to Inbox with your edits. Use **Suggest filename** to include citation edits or replace a manually entered name. Approval saves the citation and adds your paper tags. [Zotero item fields](https://www.zotero.org/support/kb/item_types_and_fields) · [Zotero write API](https://www.zotero.org/support/dev/web_api/v3/write_requests)

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
& $python tools/build_zotero_support.py --output dist/reMarkable-Sync-for-Zotero-0.19.42.xpi --runtime build/runtime/notable-worker.exe --verify-runtime --update-manifest
```

The build generates `updates.json` beside the XPI. For a release, tag the matching source, attach both files and the corresponding source to the GitHub release, and verify the downloads before publishing it as the latest release. Keep the version in the filename and the plugin manifest in sync.

Run `python tools/run_test_batches.py` for Python tests. Run the scripts matching `tools/test_*.cjs` with Node.js for JavaScript tests.

See [Sync design](documentation/SYNC_DESIGN.md) for the worker, edit queue, and upstream design references.

## License

The project's source code is licensed under the [GNU Affero General Public License, version 3](LICENSE). Third-party components keep their own licenses. The release includes the [bundled component notices](documentation/ZOTERO_PLUGIN_NOTICES.md) and matching third-party source.
