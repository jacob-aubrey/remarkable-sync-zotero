/* Thin Zotero 10.0 adapter. Sync, OCR, and paper processing stay in Python.
 * Reuses Zotero's local API authorization and native external-annotation import.
 * No database access, account token handling, remote server, or separate UI.
 */
var notableEndpoints = {};
var notableOriginalBestAttachments;
var notablePreferredAttachments;
var notableChrome;
var NotableUI;
var NotableReadingStatus;
var NotableSyncColumn;
var notableImageJobs = new Map();

async function startup({id, rootURI}) {
  await Zotero.initializationPromise;
  notableChrome = Cc['@mozilla.org/addons/addon-manager-startup;1'].getService(Ci.amIAddonManagerStartup)
    .registerChrome(Services.io.newURI(rootURI + 'manifest.json'), [['content', 'notable', rootURI]]);
  Services.scriptloader.loadSubScript(rootURI + 'library.js', globalThis);
  Services.scriptloader.loadSubScript(rootURI + 'annotation-refresh.js', globalThis);
  Services.scriptloader.loadSubScript(rootURI + 'reader-highlight-paint.js', globalThis);
  NotableReaderHighlightPaint.register(id);
  NotableAnnotationRefresh.register();
  // Root inherits Zotero's local API enablement, Host/browser restrictions,
  // server-ID validation and normal local-write authorization. The inherited
  // endpoint interface is version-sensitive; validate before widening manifest.
  const Base = Zotero.Server.LocalAPI.Root;
  const pluginVersion = (await (await fetch(rootURI + 'manifest.json')).json()).version;
  const status = class extends Base {
    supportedMethods = ['GET'];
    run() {
      return [200, 'application/json', JSON.stringify({schema: 1, version: pluginVersion})];
    }
  };
  const annotations = class extends Base {
    supportedMethods = ['GET', 'POST'];
    supportedDataTypes = ['application/json'];
    async run(request) {
      // Read calls also require explicit instance pinning for this adapter.
      if (!request.headers.get('Zotero-Server-ID')) return [428, 'text/plain', 'Select a Zotero instance'];
      const key = request.pathParams.itemKey;
      if (!/^[23456789ABCDEFGHIJKLMNPQRSTUVWXYZ]{8}$/.test(key)) return [400, 'text/plain', 'Invalid attachment key'];
      let item = await Zotero.Items.getByLibraryAndKeyAsync(Zotero.Libraries.userLibraryID, key);
      if (!item || !item.isPDFAttachment() || item.deleted) return [404, 'text/plain', 'Attachment unavailable'];
      await item.loadAllData();
      if (!item.getNote().includes('Notable pilot:')) return [403, 'text/plain', 'Attachment is not managed by reMarkable Sync'];
      let all = item.getAnnotations();
      let readersRefreshed = 0;
      let native = all.filter(annotation => !annotation.annotationIsExternal && !annotation.deleted).length;
      if (request.method === 'POST') {
        if (native) return [409, 'text/plain', 'Independent reader annotations require review'];
        const isHandwritingCard = annotation => annotation.annotationIsExternal && !annotation.deleted
          && annotation.annotationType === 'image' && annotation.annotationComment === 'Handwritten note';
        try {
          const data = typeof request.data === 'string' ? JSON.parse(request.data) : request.data;
          await NotableAnnotationRefresh.preserve(item, data?.image_replacements);
        }
        catch (error) {
          Zotero.logError(error);
          return [409, 'text/plain', 'Existing annotation metadata could not be preserved. Review before regrouping.'];
        }
        // The native importer compares whole-second mtimes. A verified upload
        // can happen within the same second; explicitly invalidate its stamp.
        item.attachmentLastProcessedModificationTime = 0;
        await item.saveTx({skipAll: true});
        await Zotero.PDFWorker.import(item.id, true);
        await item.loadAllData();
        all = item.getAnnotations();
        // Import notifications update annotation cards, but Zotero's local file
        // upload does not reload PDF bytes in an already-open reader.
        for (const reader of Zotero.Reader._readers.filter(reader => reader.itemID === item.id)) {
          await reader._initPromise;
          await reader.reload();
          NotableReaderHighlightPaint.attach(reader);
          readersRefreshed++;
        }
        const handwriting = all.filter(isHandwritingCard);
        if (handwriting.length) {
          // The attachment pane can draw a missing-image placeholder during
          // import. Retained cards can also have new ink inside the same crop.
          // Zotero's renderer skips cached images, so regenerate those cards.
          const previous = notableImageJobs.get(key);
          const job = {state: 'rendering'};
          notableImageJobs.set(key, job);
          job.promise = (async () => {
            // Serialize refreshes of one PDF. A previous render must finish
            // before its replacement clears and rebuilds the same cache files.
            if (previous) await previous.promise;
            for (const annotation of handwriting) {
              await Zotero.Annotations.removeCacheImage(annotation);
            }
            await Zotero.PDFWorker.renderAttachmentAnnotations(item.id);
            const ready = [];
            for (const annotation of handwriting) {
              if (await Zotero.Annotations.hasCacheImage(annotation)) ready.push(annotation.id);
            }
            if (ready.length !== handwriting.length) throw Error('Some image notes were not rendered');
            if (ready.length) await Zotero.Notifier.trigger('modify', 'item', ready);
            job.state = 'ready';
          })().catch(error => { job.state = 'failed'; Zotero.logError(error); });
        } else notableImageJobs.delete(key);
      }
      const imageJob = notableImageJobs.get(key);
      return [200, 'application/json', JSON.stringify({schema: 1, native,
        readersRefreshed,
        imagesReady: !imageJob || imageJob.state === 'ready',
        imagesFailed: imageJob?.state === 'failed',
        external: all.filter(annotation => annotation.annotationIsExternal && !annotation.deleted).length,
        nativeText: all.filter(annotation => !annotation.annotationIsExternal && !annotation.deleted)
          .map(annotation => ({key: annotation.key, text: annotation.annotationText || '', comment: annotation.annotationComment || ''}))
          .sort((a, b) => a.key.localeCompare(b.key))})];
    }
  };
  notableEndpoints = {'/api/notable/status': status, '/api/notable/annotations/:itemKey': annotations};
  notableRegisterLibraryEndpoint(Base);
  for (const [path, endpoint] of Object.entries(notableEndpoints)) Zotero.Server.Endpoints[path] = endpoint;
  // Zotero normally opens its oldest matching PDF. Preserve that choice of
  // source (including main vs SI), but open its explicitly linked tablet view
  // when one exists. All originals remain available under the same reference.
  notableOriginalBestAttachments = Zotero.Item.prototype.getBestAttachments;
  notablePreferredAttachments = async function (...args) {
    const attachments = await notableOriginalBestAttachments.apply(this, args);
    for (const attachment of attachments) await attachment.loadAllData();
    const owned = attachment => attachment.getNote().includes('Notable pilot:');
    const original = attachments.find(attachment => !owned(attachment));
    if (!original) return attachments;
    const replacement = attachments.find(attachment => owned(attachment)
      && attachment.getNote().includes('Notable source attachment: ' + original.key));
    return replacement ? [replacement, ...attachments.filter(attachment => attachment !== replacement)] : attachments;
  };
  Zotero.Item.prototype.getBestAttachments = notablePreferredAttachments;
  Services.scriptloader.loadSubScript(rootURI + 'folder-chooser.js', globalThis);
  Services.scriptloader.loadSubScript(rootURI + 'ui.js', globalThis);
  Services.scriptloader.loadSubScript(rootURI + 'reading-status.js', globalThis);
  Services.scriptloader.loadSubScript(rootURI + 'sync-column.js', globalThis);
  Services.scriptloader.loadSubScript(rootURI + 'notebook.js', globalThis);
  Services.scriptloader.loadSubScript(rootURI + 'context-menu.js', globalThis);
  Services.scriptloader.loadSubScript(rootURI + 'citation-draft.js', globalThis);
  Services.scriptloader.loadSubScript(rootURI + 'annotation-jump.js', globalThis);
  NotableSyncColumn.register(id);
  await NotableUI.start(rootURI);
  await Zotero.PreferencePanes.register({
    pluginID: id,
    src: 'preferences.xhtml',
    label: NotableUI.shortName,
    image: 'icons/tablet.svg',
  });
  NotableReadingStatus.register(NotableUI.id);
  NotableAnnotationJump.register(NotableUI.id);
}

async function shutdown() {
  globalThis.NotableReaderHighlightPaint?.unregister();
  globalThis.NotableAnnotationRefresh?.unregister();
  globalThis.NotableAnnotationJump?.unregister();
  NotableReadingStatus?.unregister();
  NotableSyncColumn?.unregister();
  if (NotableUI) await NotableUI.stop();
  for (const [path, endpoint] of Object.entries(notableEndpoints)) {
    if (Zotero.Server.Endpoints[path] === endpoint) delete Zotero.Server.Endpoints[path];
  }
  notableEndpoints = {};
  if (Zotero.Item.prototype.getBestAttachments === notablePreferredAttachments) {
    Zotero.Item.prototype.getBestAttachments = notableOriginalBestAttachments;
  }
  notableChrome?.destruct();
}
function onMainWindowLoad({window}) {
  NotableUI?.attach(window);
  globalThis.NotableAnnotationJump?.attach(window);
}
function onMainWindowUnload({window}) {
  globalThis.NotableAnnotationJump?.detach(window);
  NotableUI?.detach(window);
}
function install() {}
function uninstall() {}
