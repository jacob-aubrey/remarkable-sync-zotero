# Context-menu command audit

Reviewed the workspace actions, native reader controls, worker dispatch methods, notebook collector, and annotation renderer for 0.19.34. The plugin owns a **reMarkable Sync →** submenu in Zotero’s item, collection, and PDF-reader context menus. The Tools command still opens the workspace.

## Commands and scope

| Existing capability | Right-click behavior and reason |
| --- | --- |
| Generate a Word notebook | References, PDFs, multiple selected items, collections, and tags. A selected PDF exports that attachment; a reference exports its PDF annotations. Parent-plus-child selections are deduplicated. Collections retain the subcollection choice. |
| Generate a notebook for a tag | Native library tag selector, item Tags pane, reader annotation tag selector, and plugin tag lists. The clicked tag and its library or page scope determine the export. The current selection or filter does not substitute another tag. |
| Open the tablet’s annotated PDF | Available when the selection resolves to one available linked PDF. Attachment selections match exact source or view keys; other supporting PDFs under the same reference do not inherit the link. |
| Inspect links, history, and recovery | **Show selected links…** narrows Linked papers to the selected references’ or PDFs’ links. Collections include their descendants. **Show all linked papers** clears this scope. |
| Review keywords and topic suggestions | Opens the existing approval panel for one reference’s best PDF or the selected PDF. Suggestions use the existing local reader and require the existing explicit save choice. |
| Recheck keyword warnings | Reuses the selected link’s existing `recheck_keyword_warnings` action, including its local-check path during sync. This checks the plugin’s keyword evidence. Ordinary tag editing remains in Zotero. |
| Check and repair a connection | **Check paper connection…** opens that link and obtains its existing repair preview. Applying a repair still uses the reviewed token and existing confirmation controls. |
| Pause or resume a paper link | Shown for one link according to its current state. A confirmation identifies the paper and explains the effect. Pausing retains both copies. Resuming uses the worker’s existing edition and reference validation. |
| Rebuild a PDF’s search index | Confirms the paper and replacement of its cached search text, then runs the existing local reindex operation for one available linked view. |
| Export source-linked annotations | Exports one available linked PDF through the existing Markdown exporter and file picker. Word notebooks remain a separate command. |
| Rename or move a linked document | Available in the selected link’s existing card, with its saved draft, revision check, and destination chooser. A right-click opens the correct card through **Show selected links…**. |
| Review or combine tablet highlight groups | Available in the selected link’s annotation controls. Geometry and membership require the existing preview and approval. |
| Undo a queued filename repair | Remains on the selected link’s card, alongside the previous name and repair history. |
| Mark a paper as read or unread | Retains the existing independent item-menu commands and shared manual Read tag. |
| Paper and page tag editing | Zotero’s Tags pane and the existing reader tag button retain their roles. A reference right-click has no page position, so it cannot safely choose a page to edit. |
| Sync or retry transfers | **Sync selected papers now** resolves references to their recorded PDF links. PDF selections use exact source or view keys. A collection command names its subcollection scope. The selected IDs pass through worker validation into the existing bridge. Full sync remains in the workspace. |
| Sync while reading | **reMarkable Sync → Sync this PDF now** appears in PDF-view, annotation, and thumbnail context menus. The same branded submenu can also offer tag notebook export. Progress uses Zotero’s native progress window and keeps the reader open. |
| Receive live PDF updates | The common managed-annotation endpoint imports the current annotations and reloads matching open readers. Both automatic and manual syncs use it. The host reader retains its view state, and unrelated readers are left open. |
| Open the workspace or setup | Both are available at the bottom of the submenu. |
| Inbox discovery, citation editing, filing, and attaching ToSort downloads | Remain in Inbox, where review identity, evidence, filename, destination, and approval are available together. |
| Windows library scans, bulk imports, and retrying an import row | Remain in Import library, where the source folder and scan plan are explicit. |
| Paper or folder conflicts, Trash, restoration, and merge decisions | Remain in their existing review cards. A context menu alone does not provide the reviewed deletion inventory or conflict evidence. |
| Library-wide tag cleanup, filename cleanup, maintenance, backups, and restoration | Retain their existing previews and settings. These operations have broader scope than the right-clicked paper. |

For several selected items, notebook export, selected sync, and link inspection operate on the whole selection. Other commands that require one unambiguous link are hidden when several links match. Deleted items, deleted parents, feeds, notes, and unsupported selections do not expose the item submenu. Worker mutations are disabled while the single writer is busy; keyword rechecks retain the existing concurrent local-check behavior. Group-library notebooks use their own library, while reMarkable link controls remain scoped to the personal-library connection.

Selected sync validates the connection and the current parent, source, and view keys before starting. Empty, removed, or changed selections fail before cloud writes. It fetches the cloud catalog to verify scope, then processes only the requested document IDs. It skips discovery, intake cleanup, folder creation, folder metadata propagation, and unrelated review-policy refreshes. Paused links, blocked folders, conflicts, baseline checks, and publication guards retain their existing behavior. Saved folder mappings and unselected document rows remain unchanged. The global pause setting and last-full-sync timestamp remain intact, and finishing selected sync does not drain unrelated UI queues or immediately start an overdue full sync.

## Reused integrations

Zotero’s maintained, AGPL-licensed [MenuManager](https://github.com/zotero/zotero/blob/main/chrome/content/zotero/xpcom/pluginAPI/menuManager.js) supports nested item and collection menus, visibility, enablement, and cleanup. Its [developer documentation](https://www.zotero.org/support/dev/zotero_8_for_developers#custom_menu_items) recommends this API. The installed Windows Zotero 10.0.8 source confirms these interfaces; the plugin retains its Zotero 10.0.6–10.0.* compatibility range. Calling the host API adds no runtime dependency or user setup.

MenuManager does not expose a library-tag or item-tag target in the installed build. Small, removable document listeners extend those existing native tag popups, preserving Zotero’s tag commands. The reader uses Zotero’s [custom reader event API](https://www.zotero.org/support/dev/zotero_7_for_developers#custom_reader_event_handlers), with the clicked tag resolved from the reader tag selector. Listeners, popup nodes, and registrations are removed on window close or plugin shutdown. A separate plugin framework or copied menu system would add lifecycle work without supplying these plugin-specific actions.

The existing notebook collector and bundled MIT-licensed python-docx writer already support annotations and source links. This change extends their selection inputs instead of adding a second exporter. The existing worker remains responsible for connection checks, tag evidence, link control, annotation export, and indexing.

For selected sync, the existing AGPL-licensed cloud adapter already supports stable per-document IDs and root-generation checks, as illustrated by [rMAPI’s sync tree](https://github.com/ddvk/rmapi/blob/master/api/sync15/tree.go). Zotero’s [sync documentation](https://www.zotero.org/support/dev/web_api/v3/syncing) distinguishes individual-item and library versions. These primitives are already in the Windows helper; adding a document-ID scope to the bridge reuses them without introducing another sync service or requiring tablet changes. Catalog reads may still be account-wide, while PDF downloads and writes are selection-specific. The reader refresh reuses the host’s native [Reader.reload implementation](https://github.com/zotero/zotero/blob/main/chrome/content/zotero/xpcom/reader.js), whose internal PDF view reload retains view state. These interfaces remain covered by the plugin’s Zotero 10.0 compatibility limit.

The plugin’s timeout, interrupted-connection, installation, and permission messages use reMarkable Sync. The native authorization prompt names reMarkable Sync for Zotero. Legacy machine identifiers and attachment ownership markers remain unchanged so existing connections keep working.

## Freehand image-note correction

The installed MIT-licensed [rmscene](https://github.com/ricklupton/rmscene) parser already distinguishes [GlyphRange text highlights from Line strokes](https://github.com/ricklupton/rmscene/blob/main/src/rmscene/scene_items.py). The upstream release history includes v0.8.0, and the pinned Python implementation already runs in the Windows helper. No parser, device protocol, or drawing dependency was replaced.

The downstream image-card filter excluded highlighter strokes, isolated strokes, and regions above 60% of page width or 35% of page height. These restrictions could omit a highlighter outline around a figure even though the ink was visible on the PDF. Every visible non-erased Line now participates in image-card grouping, including dots and large regions. GlyphRange objects still become text highlights. Image cards cover the page content beneath the strokes and remain invisible on the PDF itself, so the page appearance stays unchanged. Deleted and hidden scene marks retain the existing handling.

Renderer revision 7 invalidates the old render cache, and image cards are enabled when existing links regenerate. The normal publisher retains its annotation-preservation checks. Existing handwritten-note labels and stable card keys are retained for compatibility with Zotero’s thumbnail refresh and saved tags.

Regression checks cover all 16 drawing-tool variants, one-stroke outlines larger than the old limits, isolated dots, unchanged PDF pixels, stable snapped-highlight geometry, and one-time regeneration of older links. The native disposable-profile test exercises reference, PDF, collection, multiple-selection, and tag menus, then imports a highlighter outline as a Zotero image annotation and exports its screenshot to Word.


## Selected-sync and annotation corrections in 0.19.30

The packaged UI added the full-sync-only `monitor_inbox` argument to selected-sync requests. The worker correctly rejected that incompatible request signature, but the earlier transport test did not enable the packaged-helper branch. Only full sync now receives that argument. The regression checks exercise the managed UI branch, and the native smoke test sends an actual selected-sync request through the bundled helper to scope validation.

Renderer revision 10 builds transitive highlight components on existing links as well as new links. Explicit manual repair selections remain intact. Tablet line breaks can occupy three character positions; the grouping accepts up to four whitespace positions between geometrically consecutive lines. It merges overlapping or bridge text passes across a spatial gap even when reMarkable records them in different colors, while preserving one reading-order block and every original quadrilateral. Separate columns and unrelated paragraphs remain separate.

Freehand grouping revisits components after a connecting stroke joins them, includes small components contained within a larger note's bounds, and accepts slightly wider word and line spacing: horizontal gaps up to 14 points, vertical gaps up to 10 points, and a combined gap up to 18 points. It retains separate distant notes and every visible stroke. A retained problem page with 13 text highlights and seven handwriting cards regenerates as three same-color passages and four handwriting regions, with the interior word included. The private paper is not included in the repository; public regression fixtures use synthetic text and geometry.

Zotero skips rendering any image annotation whose PNG already exists. That includes stale, valid PNGs. The adapter now invalidates every managed handwriting card after an updated import, including newly created placeholders, serializes refreshes of the same PDF, and reports completion or failure. The helper waits for actual thumbnail completion before recording its reader receipt. An interrupted or failed render remains retryable on the next sync, without uploading the PDF again.

Before native import replaces grouped annotations, the adapter previews the new annotations with Zotero's existing PDF worker, retains the surviving annotation key, combines manual tags, and records `dc:replaces` aliases for the other members. Old annotation links resolve to the surviving passage through the reader's existing open route. Saved note content is untouched. Original annotation records are saved in the profile's `notable/annotation-recovery` directory before changes. Conflicting comments stop the import for review. Independently created Zotero reader annotations retain the existing protection against replacement.

### Existing work considered

[remarks](https://github.com/lucasrla/remarks) is GPL-3.0 and uses PyMuPDF and Shapely, both available as Windows wheels. Its actual [text conversion code](https://github.com/lucasrla/remarks/blob/master/remarks/conversion/text.py) groups consecutively highlighted words and uses character offsets to join smart highlights. That supplies useful precedent. Its current README explicitly warns that reMarkable software 3.0 and later are unsupported, describes maintenance as uncertain, and reports testing on 2.14 and 2.15. Replacing the current parser with it would therefore regress modern-device compatibility. No code or dependency from remarks is bundled.

The MIT-licensed [rmc](https://github.com/ricklupton/rmc) and [rmscene](https://github.com/ricklupton/rmscene) support the v6 scene format introduced with tablet software 3. The actual rmc Markdown exporter walks text and GlyphRange items; it does not supply semantic grouping of arbitrary handwritten strokes. The plugin already bundles rmscene 0.8 on Windows, so small geometry and passage adaptations retain its existing device support and avoid another parser or setup step.

The native integration reuses the installed AGPL Zotero 10 [PDF worker](https://github.com/zotero/zotero/blob/main/chrome/content/zotero/xpcom/pdfWorker/manager.js), [annotation API](https://github.com/zotero/zotero/blob/main/chrome/content/zotero/xpcom/annotations.js), and [relation API](https://github.com/zotero/zotero/blob/main/chrome/content/zotero/xpcom/data/relations.js). Native tests cover stale PNG replacement on every page, tag preservation, saved note content, and old source links. The internal interfaces remain limited to the plugin's tested Zotero 10 compatibility range.
