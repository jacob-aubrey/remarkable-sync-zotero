/* Versioned, authorized native library operations. All deletions use Trash. */
async function notableItem(key) {
  if (!/^[23456789ABCDEFGHIJKLMNPQRSTUVWXYZ]{8}$/.test(key || '')) return null;
  const item = await Zotero.Items.getByLibraryAndKeyAsync(Zotero.Libraries.userLibraryID, key);
  if (item) await item.loadAllData();
  return item;
}

function notableNavigationError(message, status = 409) {
  const error = new Error(message);
  error.navigationStatus = status;
  return error;
}

function notableReviewTargetType(item, target) {
  if (target === 'references') return item.isRegularItem();
  if (target === 'attachment') return item.isFileAttachment();
  return item.isPDFAttachment();
}

function notableReviewItemInTrash(item) {
  return item.deleted || (typeof item.isInTrash === 'function' && item.isInTrash());
}

function notableIsPDFFile(item) {
  return item.isFileAttachment() && (item.isPDFAttachment() || /\.pdf$/i.test(item.attachmentFilename || ''));
}

async function notablePDFFileState(item) {
  try {
    const path = await item.getFilePathAsync();
    if (!path) return {state: 'unavailable'};
    const managed = (item.getNote() || '').includes('Notable pilot:');
    const state = managed ? 'managed' : /\.pdf$/i.test(path) ? 'available' : 'unsupported';
    return {state, path};
  } catch (error) {
    Zotero.logError(error);
    return {state: 'unknown'};
  }
}

// Native merges retain dc:replaces relations. Follow those exact identities for
// inspection only; the sync ledger and its attachment/annotation links stay put.
async function notableReviewNavigationItem(key, target, {allowMissing = false} = {}) {
  const libraryID = Zotero.Libraries.userLibraryID;
  let item = await notableItem(key), currentKey = key;
  const seen = new Set();
  for (let depth = 0; depth <= 8; depth++) {
    if (seen.has(currentKey)) {
      throw notableNavigationError('Zotero merge history contains a cycle. Recheck this review and inspect the items in Zotero.');
    }
    seen.add(currentKey);
    if (item) {
      if (item.libraryID !== libraryID || item.key !== currentKey || !notableReviewTargetType(item, target)) {
        throw notableNavigationError('This review no longer points to the expected kind of Zotero item. Recheck it before navigating.');
      }
      if (!notableReviewItemInTrash(item)) return item;
    }
    if (depth === 8) {
      throw notableNavigationError('Zotero merge history is too long to follow safely. Recheck this review and inspect the items in Zotero.');
    }
    // URI construction for an erased key still uses the native library URI;
    // no title, DOI, file name, cross-library search or saved fallback is used.
    const relations = Zotero.Relations, uri = Zotero.URI;
    if (typeof relations?.getByPredicateAndObject !== 'function' || !relations.replacedItemPredicate
        || typeof uri?.getLibraryURI !== 'function' || (item && typeof uri.getItemURI !== 'function')) {
      throw notableNavigationError(item ? 'This Zotero item is in Trash. Restore it in Zotero, then recheck the review.'
        : 'A Zotero item in this review is no longer available. Recheck the review before navigating.');
    }
    const oldURI = item ? uri.getItemURI(item) : uri.getLibraryURI(libraryID) + '/items/' + currentKey;
    const matches = await relations.getByPredicateAndObject('item', relations.replacedItemPredicate, oldURI);
    if (!Array.isArray(matches) || !matches.length) {
      if (allowMissing && Array.isArray(matches)) return null;
      throw notableNavigationError(item ? 'This Zotero item is in Trash. Restore it in Zotero, then recheck the review.'
        : 'A Zotero item in this review is no longer available. Recheck the review before navigating.');
    }
    // A damaged/ambiguous relation must not be hidden by filtering its subjects.
    const candidates = new Map();
    for (const candidate of matches) {
      if (!candidate || candidate.libraryID !== libraryID
          || !/^[23456789ABCDEFGHIJKLMNPQRSTUVWXYZ]{8}$/.test(candidate.key || '')) {
        throw notableNavigationError('Zotero merge history points outside this library or to an unavailable item. Recheck the review.');
      }
      candidates.set(candidate.key, candidate);
    }
    if (candidates.size !== 1) {
      throw notableNavigationError('Several Zotero items claim this merged item. Recheck the review and inspect the references in Zotero.');
    }
    const next = candidates.values().next().value;
    // Resolve through the ordinary safe lookup, rather than trusting a relation
    // object's cached fields to authorize a selection or Reader open.
    currentKey = next.key;
    item = await notableItem(currentKey);
    if (!item) {
      throw notableNavigationError('The surviving Zotero item is unavailable. Recheck this review before navigating.');
    }
  }
}

async function notableReferencePDFState(key) {
  try {
    const parent = await notableReviewNavigationItem(key, 'references', {allowMissing: true});
    if (!parent) return {state: 'removed', parent: key, replacement: false};
    const attachments = (await Zotero.Items.getAsync(parent.getAttachments()))
      .filter(attachment => attachment && attachment.parentKey === parent.key
        && !notableReviewItemInTrash(attachment) && attachment.isFileAttachment());
    const pdfs = attachments.filter(notableIsPDFFile);
    await Zotero.Items.loadDataTypes(pdfs, ['note']);
    const best = {state: 'none', parent: parent.key, replacement: parent.key !== key};
    const priority = ['none', 'unknown', 'unavailable', 'unsupported', 'managed', 'available'];
    for (const attachment of pdfs) {
      const {state} = await notablePDFFileState(attachment);
      if (priority.indexOf(state) > priority.indexOf(best.state)) Object.assign(best, {state, attachment: attachment.key});
      if (state === 'available') break;
    }
    if (best.state === 'none') {
      for (const attachment of attachments) {
        if (/\.(docx|pptx)$/i.test(attachment.attachmentFilename || '')
            && /\.(docx|pptx)$/i.test(await attachment.getFilePathAsync() || '')) {
          return {...best, state: 'file', attachment: attachment.key};
        }
      }
    }
    return best;
  } catch (error) {
    if (!error.navigationStatus) Zotero.logError(error);
    return {state: 'unknown', parent: key, replacement: false};
  }
}

// Navigate using Zotero's own panes and Reader. No sync/review decision is made.
async function notableReferenceMatches(key) {
  const source = await notableItem(key);
  if (!source?.isRegularItem() || notableReviewItemInTrash(source)) {
    throw Error('This reference is unavailable. Refresh Inbox.');
  }
  const doi = item => ['DOI', 'url', 'title']
    .map(field => String(item.getField(field) || '').match(/10\.\d{4,9}\/[^\s?#]+/i)?.[0])
    .find(Boolean)?.replace(/[.,;]+$/, '').toLowerCase();
  const title = item => String(item.getField('title') || '').toLowerCase().replace(/[^\p{L}\p{N}]/gu, '');
  const sourceDOI = doi(source), sourceTitle = title(source);
  const candidates = (await Zotero.Items.getAll(Zotero.Libraries.userLibraryID, false, false))
    .filter(item => item.key !== key && item.isRegularItem() && !notableReviewItemInTrash(item));
  await Zotero.Items.loadDataTypes(candidates);
  const describe = async item => {
    const attachments = await Zotero.Items.getAsync(item.getAttachments());
    return {
      title: item.getField('title'),
      doi: doi(item),
      attachments: attachments.filter(attachment => !attachment.deleted).map(attachment => attachment.getField('title')),
      collections: item.getCollections().map(id => Zotero.Collections.get(id)?.name).filter(Boolean),
    };
  };
  const matches = [];
  for (const item of candidates) {
    const candidateDOI = doi(item);
    const sameDOI = sourceDOI && sourceDOI === candidateDOI;
    if (!sameDOI && (!sourceTitle || sourceTitle !== title(item) || (sourceDOI && candidateDOI))) continue;
    matches.push({key: item.key, ...await describe(item), reason: sameDOI ? 'Matching DOI' : 'Matching title'});
  }
  return {source: matches.length ? await describe(source) : null, matches};
}

async function notableNavigateReview(data) {
  const target = data?.target;
  if (!['references', 'attachment', 'pdf', 'collection'].includes(target)) {
    throw notableNavigationError('Choose a supported Zotero navigation target.', 400);
  }
  const supplied = data.keys === undefined ? [data.key] : data.keys;
  if (!Array.isArray(supplied) || !supplied.length || supplied.length > 100
      || supplied.some(key => typeof key !== 'string' || !/^[23456789ABCDEFGHIJKLMNPQRSTUVWXYZ]{8}$/.test(key))) {
    throw notableNavigationError('The review has invalid Zotero item keys. Recheck it before navigating.', 400);
  }
  const keys = [...new Set(supplied)];
  if (target !== 'references' && keys.length !== 1) {
    throw notableNavigationError('Choose one Zotero target for this action.', 400);
  }
  const libraryID = Zotero.Libraries.userLibraryID;
  const items = [], replacements = [];
  let collection;
  // Validate the complete selection before changing focus, tabs or selection.
  if (target === 'collection') {
    collection = await Zotero.Collections.getByLibraryAndKeyAsync(libraryID, keys[0]);
    if (!collection || collection.deleted || collection.libraryID !== libraryID) {
      throw notableNavigationError('This Zotero collection is unavailable or in Trash. Recheck the review before navigating.');
    }
  } else {
    for (const key of keys) {
      const item = await notableReviewNavigationItem(key, target);
      if (item.key !== key) replacements.push({from: key, to: item.key});
      if (!items.some(selected => selected.key === item.key)) items.push(item);
    }
  }
  const win = Zotero.getMainWindow(), pane = Zotero.getActiveZoteroPane();
  if (items.some(item => item.libraryID !== libraryID || notableReviewItemInTrash(item)
      || !notableReviewTargetType(item, target))) {
    throw notableNavigationError('A Zotero review item changed while navigation was being checked. Recheck the review before retrying.');
  }
  if (!win || !pane || typeof win.focus !== 'function') {
    throw notableNavigationError('Open the Zotero library window, then retry this navigation.');
  }
  if (target === 'collection') {
    if (typeof pane.collectionsView?.selectCollection !== 'function' || typeof win.Zotero_Tabs?.select !== 'function') {
      throw notableNavigationError('Zotero collection navigation is unavailable. Open the collection in Zotero directly.');
    }
  } else if (target === 'pdf') {
    if (typeof Zotero.Reader?.open !== 'function') {
      throw notableNavigationError('Zotero Reader is unavailable. Open the PDF from its Zotero attachment.');
    }
  } else if (typeof pane.selectItems !== 'function') {
    throw notableNavigationError('Zotero item navigation is unavailable. Open the references in Zotero directly.');
  }
  win.focus();
  if (target === 'collection') {
    win.Zotero_Tabs.select('zotero-pane');
    if (await pane.collectionsView.selectCollection(collection.id) === false) {
      throw notableNavigationError('Zotero could not select this collection. Recheck the review before retrying.');
    }
  } else if (target === 'pdf') {
    await Zotero.Reader.open(items[0].id);
  } else if (await pane.selectItems(items.map(item => item.id), {inLibraryRoot: true}) === false) {
    throw notableNavigationError('Zotero could not select the review items. Recheck the review before retrying.');
  }
  return {navigated: true, target, keys: target === 'collection' ? keys : items.map(item => item.key),
    ...(replacements.length ? {requestedKeys: keys, replacements} : {})};
}

async function notableInventory(parent) {
  const items = [parent];
  for (const id of [...parent.getAttachments(true), ...parent.getNotes(true)]) {
    const item = await Zotero.Items.getAsync(id);
    await item.loadAllData();
    items.push(item);
    if (item.isPDFAttachment()) items.push(...item.getAnnotations());
  }
  const inventory = [];
  for (const item of items) {
    await item.loadAllData();
    const row = {key: item.key, version: item.version, itemType: item.itemType,
      title: item.getDisplayTitle(), deleted: !!item.deleted,
      signature: Zotero.Utilities.Internal.md5(JSON.stringify(item.toJSON()))};
    if (item.isFileAttachment()) {
      const path = await item.getFilePathAsync();
      row.fileMD5 = path ? await Zotero.Utilities.Internal.md5Async(path) : '';
    }
    inventory.push(row);
  }
  return inventory.sort((a, b) => a.key.localeCompare(b.key));
}

// One native import per digest may run at a time. Polling keeps the private
// transport cancellable while Zotero completes its ordinary metadata lookup.
var notableBulkJobs = new Map();
var notableIntakePDFJobs = new Map();

function notableDOI(value) {
  return String(value || '').replace(/^https?:\/\/(?:dx\.)?doi\.org\//i, '').replace(/^doi:\s*/i, '').trim().toLowerCase();
}

function notableTitleMatches(candidate, title) {
  // Bibliography text is not evidence of this document's identity. Compare
  // only its independently extracted front title, retaining word boundaries.
  const words = value => new Set(String(value || '').toLowerCase().match(/[\p{L}\p{N}]+/gu) || []);
  const left = words(candidate), right = words(title);
  if (Math.min(left.size, right.size) < 3) return false;
  return [...left].filter(word => right.has(word)).length / Math.max(left.size, right.size) >= .72;
}

async function notableBulkImport(data) {
  const collection = await Zotero.Collections.getByLibraryAndKeyAsync(Zotero.Libraries.userLibraryID, data.collection);
  if (!collection || collection.deleted) throw new Error('The destination collection changed. Scan again.');
  const source = Zotero.File.pathToFile(data.source), root = Zotero.File.pathToFile(data.root);
  if (!root.isDirectory() || !root.contains(source, true) || !/\.(pdf|docx|pptx)$/i.test(source.leafName)
      || await Zotero.Utilities.Internal.md5Async(source.path) !== data.md5) throw new Error('The source file changed. Scan again.');
  let attachment = data.existing ? await notableItem(data.existing) : null;
  if (data.existing && (!attachment || attachment.deleted || !attachment.isFileAttachment()
      || await Zotero.Utilities.Internal.md5Async(await attachment.getFilePathAsync()) !== data.existing_md5)) {
    throw new Error('The matching Zotero PDF changed. Scan again.');
  }
  const marker = 'Notable bulk import: ' + data.digest;
  if (!attachment) {
    // Recover a commit whose response was lost. No filename or DOI guessing.
    const matches = [];
    for (const item of await Zotero.Items.getAll(Zotero.Libraries.userLibraryID, false, false)) {
      if (!item.deleted && item.isFileAttachment() && item.getNote().includes(marker)) matches.push(item);
    }
    if (matches.length > 1) throw new Error('Several stored copies claim this import. Review them in Zotero.');
    if (matches.length) {
      attachment = matches[0];
      if (await Zotero.Utilities.Internal.md5Async(await attachment.getFilePathAsync()) !== data.md5) {
        throw new Error('The previously imported PDF changed. Both copies were retained.');
      }
    }
  }
  let parent = attachment?.parentKey ? await notableItem(attachment.parentKey) : null;
  let newMetadata = null;
  if (!attachment) {
    // DOI translators return metadata without saving another reference or PDF.
    // Accept a candidate only when its title matches the source's own title.
    let metadata = null;
    if (data.lookup && !Zotero.HTTP.browserIsOffline()) {
      for (const doi of (data.dois || []).slice(0, 3)) {
        try {
          const translate = new Zotero.Translate.Search(); translate.setIdentifier({DOI: doi});
          const translators = await translate.getTranslators();
          if (!translators.length) continue;
          translate.setTranslator(translators);
          const records = await translate.translate({libraryID: false, saveAttachments: false});
          const candidate = records[0];
          if (candidate && notableTitleMatches(candidate.title, data.identity_title || data.title)
              && notableDOI(candidate.DOI) === notableDOI(doi)) { metadata = candidate; break; }
        } catch (_) { /* A failed citation lookup does not reject a readable PDF. */ }
      }
    }
    if (metadata) {
      const candidates = [];
      for (const item of await Zotero.Items.getAll(Zotero.Libraries.userLibraryID, false, false)) {
        if (!item.deleted && item.isRegularItem() && notableDOI(item.getField('DOI')) === notableDOI(metadata.DOI)) candidates.push(item);
      }
      if (candidates.length > 1 || (candidates.length && candidates[0].getAttachments().some(id => Zotero.Items.get(id).isPDFAttachment()))) {
        throw new Error('A reference with this DOI already has a PDF, or several references match. Review the versions.');
      }
      if (candidates.length) parent = candidates[0];
      else {
        // Store the file first. A failed copy must not leave an empty citation.
        newMetadata = metadata;
      }
    }
    attachment = await Zotero.Attachments.importFromFile({file: source.path, libraryID: Zotero.Libraries.userLibraryID,
      ...(parent ? {parentItemID: parent.id} : {collections: [collection.id]}), title: source.leafName});
    attachment.setNote('<p>' + marker + '</p>'); await attachment.saveTx();
    if (await Zotero.Utilities.Internal.md5Async(await attachment.getFilePathAsync()) !== data.md5) {
      throw new Error('The stored copy did not verify. The original Windows file was retained.');
    }
  }
  // RecognizeDocument creates a parent before its result can be checked.
  // Reuse the metadata-only DOI translators above; otherwise retain a local
  // document reference for manual review rather than risking a cited identity.
  const citationUnavailable = !parent && !newMetadata && !!data.lookup;
  if (!parent) {
    // A shutdown between these two saves must not leave an empty duplicate
    // reference. File storage has finished; only metadata is transactional.
    await Zotero.DB.executeTransaction(async () => {
      parent = new Zotero.Item(newMetadata?.itemType || 'document'); parent.libraryID = Zotero.Libraries.userLibraryID;
      if (newMetadata) parent.fromJSON(newMetadata);
      else parent.setField('title', String(data.title || source.leafName).slice(0, 1000));
      parent.setCollections([collection.id]); await parent.save();
      attachment.parentID = parent.id; attachment.setCollections([]); await attachment.save();
    });
  }
  parent.addToCollection(collection.id); await parent.saveTx();
  const issues = [];
  if (citationUnavailable) issues.push('Citation metadata could not be verified against this document\'s own title. The local document was imported; review its citation metadata in Zotero.');
  const doi = notableDOI(parent.getField('DOI'));
  if (doi) {
    for (const item of await Zotero.Items.getAll(Zotero.Libraries.userLibraryID, false, false)) {
      if (item.id !== parent.id && !item.deleted && item.isRegularItem() && notableDOI(item.getField('DOI')) === doi) {
        issues.push('Another Zotero reference has the same DOI; check the references or versions.'); break;
      }
    }
  }
  return {attachment: attachment.key, parent: parent.key, data: attachment.toJSON(), reference: parent.toJSON(),
    path: await attachment.getFilePathAsync(), issues};
}

// Create a personal-library collection through Zotero's native transaction API.
async function notableCreateCollection(data) {
  const libraryID = Zotero.Libraries.userLibraryID;
  const library = Zotero.Libraries.get(libraryID);
  if (!library?.editable) throw notableNavigationError('The personal library is not editable.', 403);
  if (typeof data.name !== 'string' || !data.name.trim() || data.name.trim().length > 200
      || /[\\/\x00-\x1f]/.test(data.name)
      || !/^[23456789ABCDEFGHIJKLMNPQRSTUVWXYZ]{8}$/.test(data.key || '')
      || (data.parent && !/^[23456789ABCDEFGHIJKLMNPQRSTUVWXYZ]{8}$/.test(data.parent))) {
    throw notableNavigationError('Choose a valid name and parent collection.', 400);
  }
  let result;
  const summary = collection => ({key: collection.key, name: collection.name, parentCollection: collection.parentKey || false, version: collection.version || 0});
  await Zotero.DB.executeTransaction(async () => {
    const parent = data.parent ? await Zotero.Collections.getByLibraryAndKeyAsync(libraryID, data.parent) : null;
    if (data.parent && (!parent || parent.deleted || (typeof parent.isInTrash === 'function' && parent.isInTrash()) || parent.libraryID !== libraryID)) {
      throw notableNavigationError('The parent collection is unavailable. Refresh the chooser.');
    }
    const existing = await Zotero.Collections.getByLibraryAndKeyAsync(libraryID, data.key);
    if (existing && (existing.deleted || existing.name !== data.name.trim() || (existing.parentKey || '') !== (data.parent || ''))) {
      throw notableNavigationError('The saved destination changed. Refresh the chooser before retrying.');
    }
    const siblings = Zotero.Collections.getByLibrary(libraryID, true)
      .filter(row => !row.deleted && (row.parentKey || '') === (data.parent || '') && row.name === data.name.trim());
    if (siblings.length > 1) throw notableNavigationError('Several siblings have that name. Resolve them in Zotero first.');
    if (existing || siblings.length) { result = summary(existing || siblings[0]); return; }
    const collection = new Zotero.Collection();
    collection.libraryID = libraryID; collection.key = data.key;
    // An explicit key identifies the object before its fields are loaded. The
    // native missing-record path marks new custom-key objects ready for writes.
    await collection.loadPrimaryData();
    collection.name = data.name.trim(); collection.parentKey = data.parent || false;
    await collection.save();
    result = summary(collection);
  });
  return {collection: result};
}

async function notableTagCatalogItems(items, {papers, tags, match, views}) {
  const references = papers ? items.filter(item => item.isRegularItem()) : [];
  const paperKeys = new Set();
  if (references.length) {
    await Zotero.Items.loadDataTypes(references, ['tags']);
    for (const item of references) {
      const itemTags = new Set(item.getTags().map(value => value.tag));
      if (!itemTags.size) continue;
      const matches = !tags.length || (match === 'any' ? tags.some(tag => itemTags.has(tag)) : tags.every(tag => itemTags.has(tag)));
      if (matches) paperKeys.add(item.key);
    }
  }
  const pageViews = new Set(views);
  const pdfs = items.filter(item => item.isFileAttachment() && (pageViews.has(item.key)
    || (paperKeys.has(item.parentKey) && (item.isPDFAttachment() || /\.pdf$/i.test(item.attachmentFilename || '')))));
  const wanted = new Set([...paperKeys, ...pdfs.map(pdf => pdf.parentKey)]);
  const attachments = new Set(pdfs);
  return items.filter(item => wanted.has(item.key) || attachments.has(item));
}

function notableRegisterLibraryEndpoint(Base) {
  notableEndpoints['/api/notable/action'] = class extends Base {
    supportedMethods = ['POST'];
    supportedDataTypes = ['application/json'];
    async run(request) {
      const reply = (status, value) => [status, 'application/json', JSON.stringify(value)];
      if (!request.headers.get('Zotero-Server-ID')) return reply(428, {error: 'Select a Zotero instance'});
      const data = typeof request.data === 'string' ? JSON.parse(request.data) : request.data;
      if (data.action === 'create_collection') {
        try { return reply(200, await notableCreateCollection(data)); }
        catch (error) { return reply(error.navigationStatus || 409, {error: error.navigationStatus ? error.message : 'The collection could not be created. Retry safely.'}); }
      }
      if (data.action === 'navigate_review') {
        try {
          return reply(200, await notableNavigateReview(data));
        } catch (error) {
          return reply(error.navigationStatus || 409, {error: error.navigationStatus ? error.message
            : 'Zotero could not open this review target. Recheck the review and try again.'});
        }
      }
      if (data.action === 'inspect_review_reference') {
        if (!/^[23456789ABCDEFGHIJKLMNPQRSTUVWXYZ]{8}$/.test(data.key || '')) return reply(400, {error: 'Choose a saved Zotero reference'});
        try {
          const parent = await notableReviewNavigationItem(data.key, 'references');
          return reply(200, {requested: data.key, parent: parent.toJSON(), inventory: await notableInventory(parent)});
        } catch (error) {
          return reply(error.navigationStatus || 409, {error: error.navigationStatus ? error.message : 'The surviving reference could not be verified.'});
        }
      }
      if (data.action === 'attach_intake_pdf') {
        const signature = JSON.stringify([data.source, data.doi_value, data.md5]);
        const active = notableIntakePDFJobs.get(data.parent);
        if (active && active.signature !== signature) return reply(200, {error: 'Another attachment is being stored for this reference.'});
        const job = active || {signature, promise: notableAttachIntakePDF(data)};
        notableIntakePDFJobs.set(data.parent, job);
        try { return reply(200, await job.promise); }
        catch (error) { return reply(200, {error: error.intakeMessage || 'Zotero could not store the PDF. The ToSort file was kept.'}); }
        finally { if (notableIntakePDFJobs.get(data.parent) === job) notableIntakePDFJobs.delete(data.parent); }
      }
      if (data.action === 'bulk_import' || data.action === 'bulk_import_status') {
        if (!/^[a-f0-9]{64}$/.test(data.digest || '')) return reply(400, {error: 'Invalid import identity'});
        if (!/^[23456789ABCDEFGHIJKLMNPQRSTUVWXYZ]{8}$/.test(data.collection || '')) return reply(400, {error: 'Choose a collection'});
        // Different destination choices share a digest lock. An import which
        // outlives Stop may finish before the next destination is applied.
        const key = data.digest;
        let group = notableBulkJobs.get(key);
        if (!group && data.action === 'bulk_import') {
          group = {tail: Promise.resolve(), jobs: new Map()}; notableBulkJobs.set(key, group);
        }
        let job = group?.jobs.get(data.collection);
        const signature = data.action === 'bulk_import' ? JSON.stringify(data) : null;
        if (job && !job.pending && signature && job.signature !== signature) {
          group.jobs.delete(data.collection); job = null;
        }
        if (!job && data.action === 'bulk_import') {
          job = {pending: true, signature}; group.jobs.set(data.collection, job);
          group.tail = group.tail.catch(() => {}).then(() => notableBulkImport(data)).then(result => { job.result = result; job.pending = false; }, error => {
            job.error = error.message; job.pending = false;
          });
        }
        if (!job) return reply(409, {error: 'This import was interrupted. Scan the folder again.'});
        if (job.pending) return reply(200, {pending: true});
        if (job.error) { group.jobs.delete(data.collection); return reply(200, {error: job.error}); }
        // Keep completed results for a lost HTTP response or repeated poll.
        // Different collection choices have separate, revalidated operations.
        if (notableBulkJobs.size > 1000) {
          for (const [oldKey, old] of notableBulkJobs) {
            if (oldKey !== key && [...old.jobs.values()].every(value => !value.pending)) { notableBulkJobs.delete(oldKey); break; }
          }
        }
        return reply(200, job.result);
      }
      if (data.action === 'catalog') {
        const attachments = [], parents = {}, missingPDFParents = new Set(), pdfInventory = {};
        const full = !data.parents_only && !data.names_only && !data.tag_lookup;
        let items = (await Zotero.Items.getAll(Zotero.Libraries.userLibraryID, false, false))
          .filter(item => !item.deleted && (item.isRegularItem() || (!data.parents_only && item.isFileAttachment())));
        if (data.tag_lookup) {
          const {papers, tags, match, views} = data.tag_lookup;
          if (typeof papers !== 'boolean' || !Array.isArray(tags) || tags.some(tag => typeof tag !== 'string')
            || !['all', 'any'].includes(match) || !Array.isArray(views) || views.some(key => typeof key !== 'string')) {
            return reply(400, {error: 'Invalid tag search'});
          }
          items = await notableTagCatalogItems(items, {papers, tags, match, views});
        }
        // Zotero's existing batch loader avoids thousands of sequential database
        // reads on a cold library. Annotation bodies are not catalog entries.
        await Zotero.Items.loadDataTypes(items);
        for (const item of items) {
          if (notableReviewItemInTrash(item)) continue;
          if (item.isRegularItem()) { parents[item.key] = item.toJSON(); continue; }
          if (!item.isFileAttachment()) continue;
          if (data.names_only) {
            // Zotero remembers the filename even when the PDF is cloud-only
            // or missing on this computer.
            attachments.push({data: item.toJSON(), path: item.attachmentFilename || ''});
            continue;
          }
          let path;
          if (notableIsPDFFile(item)) {
            const pdf = await notablePDFFileState(item);
            path = pdf.path;
            const key = item.parentKey;
            if (key) {
              if (full) (pdfInventory[key] ??= {available: [], managed: [], unavailable: [], unknown: [], unsupported: []})[pdf.state].push(item.key);
              if (pdf.state === 'unavailable') missingPDFParents.add(key);
            }
            if (pdf.state === 'unsupported') continue;
          } else path = await item.getFilePathAsync();
          if (!path || !/\.(pdf|docx|pptx)$/i.test(path)) continue;
          attachments.push({data: item.toJSON(), path});
          if (item.parentKey && !parents[item.parentKey]) {
            const parent = Zotero.Items.getByLibraryAndKey(Zotero.Libraries.userLibraryID, item.parentKey);
            if (parent && !parent.deleted) parents[item.parentKey] = parent.toJSON();
          }
        }
        return reply(200, {attachments, parents, missing_pdf_parents: [...missingPDFParents],
          ...(full ? {pdf_inventory: pdfInventory} : {}), data_directory: Zotero.DataDirectory.dir});
      }
      if (data.action === 'reparent') {
        const item = await notableItem(data.key), parent = await notableItem(data.parent);
        if (!item || item.deleted || !item.isFileAttachment() || !parent || parent.deleted || !parent.isRegularItem()
            || item.libraryID !== parent.libraryID) return reply(409, {error: 'Attachment or destination unavailable'});
        const previous = item.parentKey || '';
        const path = await item.getFilePathAsync();
        if (!path || await Zotero.Utilities.Internal.md5Async(path) !== data.md5
            || ![data.previous_parent || '', parent.key].includes(previous)) return reply(409, {error: 'Attachment changed'});
        if (previous !== parent.key) { item.parentKey = parent.key; await item.saveTx(); }
        if (item.parentKey !== parent.key || await Zotero.Utilities.Internal.md5Async(await item.getFilePathAsync()) !== data.md5) {
          return reply(409, {error: 'Reparent verification failed'});
        }
        return reply(200, {key: item.key, parent: parent.key});
      }
      if (data.action === 'inventory') {
        const parent = await notableItem(data.parent);
        if (!parent || !parent.isRegularItem()) return reply(404, {error: 'Reference unavailable'});
        return reply(200, {inventory: await notableInventory(parent)});
      }
      if (data.action === 'prepare_upload') {
        const item = await notableItem(data.key);
        if (!item || item.deleted || !item.isFileAttachment() || !item.isStoredFileAttachment()) {
          return reply(409, {error: 'Stored attachment unavailable'});
        }
        if (typeof data.md5 !== 'string' || !/^(?:[a-f0-9]{32})?$/.test(data.md5)) return reply(400, {error: 'Invalid PDF hash'});
        const path = await item.getFilePathAsync();
        const actual = path && await IOUtils.exists(path) ? await Zotero.Utilities.Internal.md5Async(path) : '';
        if (actual !== data.md5) return reply(409, {error: 'PDF changed'});
        const remote_md5 = item.attachmentSyncedHash || '';
        const remote_mtime = item.attachmentSyncedModificationTime || 0;
        // The local API authorizes against attachmentSyncedHash, including
        // If-None-Match:* for unsynced imports which already have local bytes.
        // Keep preparation read-only so a lost response cannot turn a local
        // seed into a fictitious account receipt before Python persists it.
        const verified = path && await IOUtils.exists(path) ? await Zotero.Utilities.Internal.md5Async(path) : '';
        if (verified !== data.md5) return reply(409, {error: 'PDF changed'});
        return reply(200, {md5: data.md5, authorization_md5: item.attachmentSyncedHash || '', remote_md5, remote_mtime});
      }
      if (data.action === 'finish_upload') {
        const item = await notableItem(data.key);
        if (!item || item.deleted || !item.isFileAttachment() || !item.isStoredFileAttachment()) {
          return reply(409, {error: 'Stored attachment unavailable'});
        }
        if (typeof data.md5 !== 'string' || !/^[a-f0-9]{32}$/.test(data.md5)
            || typeof data.remote_md5 !== 'string' || !/^(?:[a-f0-9]{32})?$/.test(data.remote_md5)
            || !Number.isSafeInteger(data.remote_mtime) || data.remote_mtime < 0) {
          return reply(400, {error: 'Invalid upload receipt'});
        }
        const path = await item.getFilePathAsync();
        if (!path || !await IOUtils.exists(path) || await Zotero.Utilities.Internal.md5Async(path) !== data.md5) {
          return reply(409, {error: 'PDF changed'});
        }
        // Account upload uses this previous remote hash for If-Match. The
        // local API's hash describes local bytes and must not replace it.
        item.attachmentSyncedHash = data.remote_md5 || null;
        item.attachmentSyncedModificationTime = data.remote_mtime;
        item.attachmentSyncState = 'to_upload';
        await item.saveTx();
        if (await Zotero.Utilities.Internal.md5Async(path) !== data.md5) return reply(409, {error: 'PDF changed'});
        return reply(200, {md5: data.md5, sync_state: 'to_upload'});
      }
      if (data.action === 'rename') {
        const item = await notableItem(data.key);
        if (!item || item.deleted || !item.isFileAttachment()) return reply(404, {error: 'Attachment unavailable'});
        const name = data.filename;
        if (typeof name !== 'string' || name.length > 240 || /[\\/\x00-\x1f]/.test(name)
            || !/\.(pdf|docx|pptx)$/i.test(name)) return reply(400, {error: 'Invalid filename'});
        const path = await item.getFilePathAsync();
        if (!path || await Zotero.Utilities.Internal.md5Async(path) !== data.md5) return reply(409, {error: 'File changed'});
        const previousName = Zotero.File.pathToFile(path).leafName;
        if (data.expected_parent && (item.parentKey !== data.expected_parent
            || ![data.before_filename, name].includes(previousName))) return reply(409, {error: 'Linked filename changed'});
        const title = item.getField('title');
        const filenameTitle = !title || /\.(pdf|docx|pptx)$/i.test(title)
          || title === previousName || title === previousName.replace(/\.[^.]+$/, '');
        if (previousName !== name && await item.renameAttachmentFile(name, {overwrite: false, unique: false, updateTitle: false}) !== true) {
          return reply(409, {error: 'Filename unavailable'});
        }
        // Filename-like labels are what users see in Zotero's item list.
        // Keep meaningful custom labels and the parent citation unchanged.
        if (filenameTitle && title !== name && item.getField('title') === title) {
          item.setField('title', name);
          await item.saveTx();
        }
        return reply(200, {key: item.key, filename: name});
      }
      if (data.action === 'adopt') {
        const item = await notableItem(data.key);
        if (!item || item.deleted || !item.isPDFAttachment() || item.parentKey !== data.parent
            || !/^Notable pilot: [a-f0-9]{64}$/.test(data.marker || '')) return reply(409, {error: 'Source link changed'});
        const path = await item.getFilePathAsync();
        if (!path || await Zotero.Utilities.Internal.md5Async(path) !== data.md5) return reply(409, {error: 'Source bytes changed'});
        const independent = item.getAnnotations().filter(a => !a.deleted && !a.annotationIsExternal).length;
        if (independent || item.attachmentLinkMode === Zotero.Attachments.LINK_MODE_LINKED_FILE) return reply(200, {adopted: false, native: independent});
        if (item.getNote().includes('Notable pilot:') && !item.getNote().includes(data.marker)) return reply(409, {error: 'Another managed link owns this PDF'});
        if (!item.getNote().includes(data.marker)) {
          item.setNote(item.getNote() + '<p>' + data.marker + '</p>');
          await item.saveTx();
        }
        return reply(200, {adopted: true, native: 0});
      }
      if (data.action === 'trash') {
        const parent = await notableItem(data.parent), view = await notableItem(data.attachment);
        if (!parent || !view || !parent.isRegularItem() || view.parentKey !== parent.key
            || !view.getNote().includes('Notable pilot:')) return reply(409, {error: 'Link changed'});
        if (!['pdf_only', 'everything'].includes(data.choice)) return reply(400, {error: 'Choose a deletion scope'});
        const targets = data.choice === 'everything' ? [parent] : [view];
        if (data.choice === 'pdf_only' && data.source_attachment) {
            const source = await notableItem(data.source_attachment);
          if (!source || source.parentKey !== parent.key
              || !view.getNote().includes('Notable source attachment: ' + source.key)) return reply(409, {error: 'Source link changed'});
          targets.push(source);
        }
        if (targets.every(item => item.deleted)) return reply(200, {status: 'trashed', keys: targets.map(i => i.key)});
        let stale = false;
        await Zotero.DB.executeTransaction(async () => {
          if (JSON.stringify(await notableInventory(parent)) !== JSON.stringify(data.inventory)) { stale = true; return; }
          await Zotero.Items.trash(targets.map(item => item.id));
        });
        return reply(stale ? 409 : 200, stale ? {error: 'Reference or notes changed'} : {status: 'trashed', keys: targets.map(i => i.key)});
      }
      return reply(400, {error: 'Unknown library action'});
    }
  };
}

// Reuse Zotero's maintained retrieval and storage; never create another reference.
async function notableAcquirePDF(key, file = null, {describe = false} = {}) {
  // Recheck after selection: Zotero may have delivered a PDF or merged the item
  // while the file picker was open.
  const status = await notableReferencePDFState(key);
  if (status.replacement || status.state === 'removed') throw Error('This reference is unavailable. Check Inbox for its current Zotero entry.');
  if (status.state === 'unknown') throw Error('Zotero could not verify the PDF attachments. Check Inbox and try again.');
  if (status.state === 'unavailable') throw Error('Zotero already lists a PDF for this reference. Open it in Zotero to download or locate the file.');
  if (status.state === 'unsupported') throw Error('The attached PDF needs a .pdf filename. Rename it in Zotero, then check Inbox again.');
  if (status.state === 'file') throw Error('Zotero already has a file for this reference. Check Inbox for its filing review.');
  if (status.attachment) {
    const attachment = await notableItem(status.attachment);
    if (!attachment || notableReviewItemInTrash(attachment) || attachment.parentKey !== key) throw Error('The PDF attachment changed. Check Inbox again.');
    return describe ? {attachment, outcome: 'existing'} : attachment;
  }
  const item = await notableItem(key);
  if (!item?.isRegularItem() || notableReviewItemInTrash(item)) throw Error('This reference is unavailable. Check Inbox again.');
  if (!file && typeof Zotero.Attachments.addAvailableFile !== 'function') throw Error('This Zotero version cannot retrieve files here. Open the source link, download the PDF, and use Attach downloaded PDF.');
  const attachment = file ? await Zotero.Attachments.importFromFile({file, parentItemID: item.id, contentType: 'application/pdf'})
    : await Zotero.Attachments.addAvailableFile(item);
  if (!attachment) throw Error('No available file found. Open the source link, download the PDF, and use Attach downloaded PDF.');
  return describe ? {attachment, outcome: 'added'} : attachment;
}

async function notableAttachIntakePDF(data) {
  const reject = message => { throw Object.assign(Error(message), {intakeMessage: message}); };
  if (typeof data.source !== 'string' || !/\.pdf$/i.test(data.source) || !/^[a-f0-9]{32}$/.test(data.md5 || '')
      || typeof data.doi_value !== 'string' || !data.doi_value) {
    reject('The ToSort attachment request is invalid. Check Inbox before attaching.');
  }
  const state = Zotero.File.pathToFile(PathUtils.join(Zotero.Profile.dir, 'notable'));
  const source = Zotero.File.pathToFile(data.source);
  if (!source.isFile() || !source.parent?.parent || !state.equals(source.parent.parent)
      || !source.parent.leafName.startsWith('intake-pdf-')) {
    reject('The ToSort PDF could not be verified. Check Inbox before attaching.');
  }
  const parent = await notableItem(data.parent);
  if (!parent?.isRegularItem() || notableReviewItemInTrash(parent) || parent.getField('DOI') !== data.doi_value) {
    reject('The Zotero reference changed. Check Inbox before attaching.');
  }
  const status = await notableReferencePDFState(data.parent);
  if (status.replacement || !['none', 'available'].includes(status.state)) {
    reject('Zotero already has a file for this reference, or the reference changed. Check Inbox before attaching.');
  }
  if (await Zotero.Utilities.Internal.md5Async(data.source) !== data.md5) {
    reject('The ToSort PDF changed. Check Inbox before attaching.');
  }
  // The ordinary Add PDF workflow rechecks the target after file selection.
  const attachment = await notableAcquirePDF(data.parent, data.source);
  const stored = await attachment.getFilePathAsync();
  if (attachment.parentKey !== data.parent || notableReviewItemInTrash(attachment) || !attachment.isStoredFileAttachment()
      || !stored || await Zotero.Utilities.Internal.md5Async(stored) !== data.md5) {
    reject('Zotero has a different PDF or the stored copy could not be verified. The ToSort file was kept.');
  }
  return {key: attachment.key};
}
