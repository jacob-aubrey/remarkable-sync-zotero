// Execute the native endpoint with synthetic items/files, never a real profile.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function fixture() {
  const items = new Map(), files = new Map([['/intake/paper.pdf', 'a'.repeat(32)]]);
  const state = {copies: 0, recognitions: 0, translations: [], candidate: null, copyError: false, copyBarrier: null, nextID: 2};
  const byID = id => [...items.values()].find(item => item.id === id);
  class Item {
    constructor(type) {
      this.id = state.nextID++; this.key = 'AAAAAA' + String.fromCharCode(65 + Math.floor(this.id / 20)) + String.fromCharCode(65 + this.id % 20);
      this.itemType = type; this.deleted = false; this.fields = {}; this.collections = [];
      this.attachmentSyncedHash = null; this.attachmentSyncedModificationTime = 0; this.attachmentSyncState = 'in_sync';
      this.attachmentLinkMode = 0; this.note = ''; this.saves = 0;
    }
    async loadAllData() {}
    get attachmentSyncedHash() { return this.syncedHash; }
    set attachmentSyncedHash(value) {
      if (value !== null && (typeof value !== 'string' || value.length !== 32)) throw Error('Invalid attachment synced hash');
      this.syncedHash = value;
    }
    isRegularItem() { return this.itemType !== 'attachment'; }
    isFileAttachment() { return this.itemType === 'attachment'; }
    isPDFAttachment() { return this.isFileAttachment() && /\.pdf$/.test(this.path || ''); }
    isStoredFileAttachment() { return this.isFileAttachment() && this.attachmentLinkMode !== 2; }
    get parentKey() { return byID(this.parentID)?.key; }
    getFilePathAsync() { return Promise.resolve(this.path || false); }
    getAttachments() { return [...items.values()].filter(item => item.parentID === this.id).map(item => item.id); }
    getNote() { return this.note; }
    setNote(note) { this.note = note; }
    getField(name) { return this.fields[name] || ''; }
    setField(name, value) { this.fields[name] = value; }
    setCollections(collections) { this.collections = [...collections]; }
    addToCollection(id) { if (!this.collections.includes(id)) this.collections.push(id); }
    fromJSON(value) { Object.assign(this.fields, value); }
    toJSON() { return {key: this.key, itemType: this.itemType, ...this.fields, parentItem: this.parentKey}; }
    async save() { items.set(this.key, this); this.saves++; }
    async saveTx() { return this.save(); }
    async renameAttachmentFile(name, options) {
      state.renameOptions = options;
      if (state.renameError) return false;
      const target = '/storage/' + name;
      files.set(target, files.get(this.path)); files.delete(this.path); this.path = target;
      if (state.renameNewTitle) this.setField('title', state.renameNewTitle);
      return true;
    }
  }
  const zotero = {Libraries: {userLibraryID: 1}, DataDirectory: {dir: '/storage'}, Item,
    Items: {getAll: async () => [...items.values()], getByLibraryAndKeyAsync: async (library, key) => items.get(key),
      getByLibraryAndKey: (library, key) => items.get(key),
      loadDataTypes: async objects => { state.batches = (state.batches || 0) + 1; state.batchSize = objects.length; }, get: byID},
    Collections: {getByLibraryAndKeyAsync: async (library, key) => ({id: key === 'DEST2345' ? 10 : 11})},
    File: {pathToFile: path => ({path, leafName: path.split('/').at(-1), isDirectory: () => path === '/intake',
      contains: other => other.path.startsWith(path + '/')})},
    Utilities: {Internal: {md5Async: async path => {
      if (!files.has(path)) throw Error('Missing file'); return files.get(path);
    }}},
    HTTP: {browserIsOffline: () => false},
    Translate: {Search: class {
      setIdentifier(identifier) { state.translations.push(identifier); }
      async getTranslators() { return [{}]; }
      setTranslator() {}
      async translate() { return state.candidate ? [state.candidate] : []; }
    }},
    Attachments: {LINK_MODE_LINKED_FILE: 2, importFromFile: async options => {
      state.copies++;
      if (state.copyBarrier) await state.copyBarrier;
      if (state.copyError) throw Error('Storage unavailable');
      const item = new Item('attachment'); item.path = '/storage/' + item.key + '.pdf';
      item.parentID = options.parentItemID; item.collections = options.collections || [];
      files.set(item.path, files.get(options.file)); await item.save(); return item;
    }},
    RecognizeDocument: {recognizeItems: async () => { state.recognitions++; }},
    DB: {executeTransaction: async callback => callback()}};
  const context = vm.createContext({Zotero: zotero, notableEndpoints: {}, IOUtils: {exists: async path => files.has(path)}});
  vm.runInContext(fs.readFileSync('zotero-support/library.js', 'utf8'), context);
  context.notableRegisterLibraryEndpoint(class {});
  const endpoint = new context.notableEndpoints['/api/notable/action']();
  const request = async data => {
    const response = await endpoint.run({headers: new Map([['Zotero-Server-ID', 'test-instance']]), data});
    return {status: response[0], value: JSON.parse(response[2])};
  };
  const importData = {action: 'bulk_import', collection: 'DEST2345', source: '/intake/paper.pdf', root: '/intake',
    md5: 'a'.repeat(32), digest: 'd'.repeat(64), lookup: true, title: 'Original nanoparticle delivery investigation',
    identity_title: 'Original nanoparticle delivery investigation', dois: ['10.1000/cited'], evidence: 'References: Unrelated protein folding and cellular dynamics'};
  return {context, state, items, files, request, importData, Item};
}

async function main() {
  for (const title of ['Gunaseelan et al_Advanced Science_2024.pdf', 'Full Text', 'Supporting Information']) {
    const f = fixture(), item = new f.Item('attachment');
    item.path = '/storage/old.pdf'; item.setField('title', title);
    f.files.set(item.path, 'a'.repeat(32)); await item.save();
    const request = {action: 'rename', key: item.key, filename: 'Gunaseelan_et_al_Adv. Sci._2024.pdf', md5: 'a'.repeat(32)};
    assert.equal((await f.request(request)).status, 200);
    assert.equal(item.getField('title'), title.endsWith('.pdf') ? request.filename : title);
    assert.equal((await f.request(request)).status, 200, 'Retrying a completed rename is harmless');
  }
  {
    const f = fixture(), item = new f.Item('attachment');
    item.path = '/storage/old.pdf'; item.setField('title', 'Old title.pdf');
    f.files.set(item.path, 'a'.repeat(32)); await item.save(); f.state.renameNewTitle = 'Custom title edited while renaming';
    assert.equal((await f.request({action: 'rename', key: item.key, filename: 'New.pdf', md5: 'a'.repeat(32)})).status, 200);
    assert.equal(item.getField('title'), f.state.renameNewTitle, 'A concurrent title edit must remain intact');
  }
  {
    const f = fixture(), item = new f.Item('attachment');
    item.path = '/storage/old.pdf'; item.setField('title', 'Old title.pdf');
    f.files.set(item.path, 'a'.repeat(32)); await item.save(); f.state.renameError = true;
    assert.equal((await f.request({action: 'rename', key: item.key, filename: 'New.pdf', md5: 'a'.repeat(32)})).status, 409);
    assert.equal(item.getField('title'), 'Old title.pdf', 'Failed renames must retain the original display label');
  }
  {
    const f = fixture();
    for (let i = 0; i < 500; i++) {
      const parent = new f.Item('journalArticle'); await parent.save();
      const pdf = new f.Item('attachment'); pdf.parentID = parent.id; pdf.path = '/storage/' + i + '.pdf'; f.files.set(pdf.path, 'a'.repeat(32)); await pdf.save();
      parent.loadAllData = pdf.loadAllData = async () => { throw Error('Catalog must use the native batch loader'); };
    }
    const annotation = new f.Item('annotation'); annotation.isRegularItem = () => false; await annotation.save();
    const result = await f.request({action: 'catalog'});
    assert.equal(result.status, 200);
    assert.equal(result.value.attachments.length, 500);
    assert.equal(Object.keys(result.value.parents).length, 500);
    assert.equal(f.state.batches, 1);
    assert.equal(f.state.batchSize, 1000);
  }
  {
    const f = fixture();
    f.state.candidate = {itemType: 'journalArticle', title: 'Unrelated protein folding and cellular dynamics', DOI: '10.1000/cited'};
    const result = await f.context.notableBulkImport(f.importData);
    assert.equal(result.reference.title, f.importData.identity_title, 'A cited bibliography title must not replace the document identity');
    assert.equal(result.reference.DOI, undefined);
    assert.equal(f.state.recognitions, 0, 'Unverified metadata must not invoke a recognizer that saves citations before validation');
    assert.ok(result.issues.some(issue => issue.includes('could not be verified')));
    assert.equal(f.state.copies, 1);
  }
  {
    const f = fixture();
    f.state.candidate = {itemType: 'journalArticle', title: f.importData.identity_title, DOI: '10.1000/cited'};
    f.state.copyError = true;
    await assert.rejects(() => f.context.notableBulkImport(f.importData), /Storage unavailable/);
    assert.equal(f.items.size, 0, 'A failed storage copy must not save a DOI parent');
  }
  {
    const f = fixture();
    let release;
    f.state.copyBarrier = new Promise(resolve => { release = resolve; });
    assert.equal((await f.request(f.importData)).value.pending, true);
    const second = {...f.importData, collection: 'NEXT2345'};
    assert.equal((await f.request(second)).value.pending, true);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(f.state.copies, 1, 'Different destinations must share the content lock');
    release();
    await f.context.notableBulkJobs.get(f.importData.digest).tail;
    const firstResult = (await f.request({...f.importData, action: 'bulk_import_status'})).value;
    const secondResult = (await f.request({...second, action: 'bulk_import_status'})).value;
    assert.equal(firstResult.attachment, secondResult.attachment);
    assert.equal(firstResult.parent, secondResult.parent);
    assert.equal(f.state.copies, 1);
    assert.equal([...f.items.values()].filter(item => item.isRegularItem()).length, 1);
    assert.deepEqual(f.items.get(firstResult.parent).collections, [10, 11]);
  }
  {
    const f = fixture(), item = new f.Item('attachment');
    item.path = '/storage/existing.pdf'; item.note = 'Notable pilot: ' + 'c'.repeat(64);
    item.attachmentSyncedHash = 'b'.repeat(32); item.attachmentSyncedModificationTime = 123456789;
    f.files.set(item.path, 'a'.repeat(32)); await item.save();
    const prepare = await f.request({action: 'prepare_upload', key: item.key, md5: 'a'.repeat(32)});
    assert.equal(prepare.status, 200);
    assert.deepEqual(prepare.value, {md5: 'a'.repeat(32), authorization_md5: 'b'.repeat(32), remote_md5: 'b'.repeat(32), remote_mtime: 123456789});
    assert.equal(item.attachmentSyncedHash, 'b'.repeat(32), 'Preparing a local upload must preserve an existing account receipt');
    // Zotero localAPI mutates these fields after replacing its stored file.
    f.files.set(item.path, 'e'.repeat(32)); item.attachmentSyncedHash = 'e'.repeat(32); item.attachmentSyncedModificationTime = 999999999;
    const finish = await f.request({action: 'finish_upload', key: item.key, md5: 'e'.repeat(32), remote_md5: prepare.value.remote_md5, remote_mtime: prepare.value.remote_mtime});
    assert.equal(finish.status, 200);
    assert.deepEqual(finish.value, {md5: 'e'.repeat(32), sync_state: 'to_upload'});
    assert.equal(item.attachmentSyncedHash, 'b'.repeat(32));
    assert.equal(item.attachmentSyncedModificationTime, 123456789);
    assert.equal(item.attachmentSyncState, 'to_upload');
    assert.equal((await f.request({action: 'finish_upload', key: item.key, md5: 'e'.repeat(32), remote_md5: prepare.value.remote_md5, remote_mtime: prepare.value.remote_mtime})).status, 200,
      'A lost finish response must be safely retryable with the same receipt');
    const invalid = await f.request({action: 'finish_upload', key: item.key, md5: 'e'.repeat(32), remote_md5: '', remote_mtime: -1});
    assert.equal(invalid.status, 400);
    const changed = await f.request({action: 'finish_upload', key: item.key, md5: 'f'.repeat(32), remote_md5: '', remote_mtime: 0});
    assert.equal(changed.status, 409);
    const unchangedSaves = item.saves;
    assert.equal((await f.request({action: 'prepare_upload', key: item.key, md5: 'f'.repeat(32)})).status, 409);
    assert.equal(item.saves, unchangedSaves);
    assert.equal((await f.request({action: 'finish_upload', key: item.key, md5: 'e'.repeat(32), remote_md5: 'bad-hash', remote_mtime: 0})).status, 400);
  }
  {
    const f = fixture(), item = new f.Item('attachment');
    item.path = '/storage/new.pdf'; item.note = 'Notable pilot: ' + 'c'.repeat(64); await item.save();
    const empty = await f.request({action: 'prepare_upload', key: item.key, md5: ''});
    assert.equal(empty.status, 200);
    assert.deepEqual(empty.value, {md5: '', authorization_md5: '', remote_md5: '', remote_mtime: 0});
    f.files.set(item.path, 'a'.repeat(32));
    const beforePrepare = item.saves;
    const seed = await f.request({action: 'prepare_upload', key: item.key, md5: 'a'.repeat(32)});
    assert.equal(seed.value.authorization_md5, '');
    assert.equal(seed.value.remote_md5, '');
    assert.equal(item.attachmentSyncedHash, null, 'Preparation must not invent an account receipt for local bytes');
    assert.equal(item.saves, beforePrepare, 'Preparation must stay read-only until Python durably stores its receipt');
    assert.deepEqual((await f.request({action: 'prepare_upload', key: item.key, md5: 'a'.repeat(32)})).value, seed.value,
      'A lost prepare response must return the same original account receipt');
    f.files.set(item.path, 'e'.repeat(32)); item.attachmentSyncedHash = 'e'.repeat(32);
    assert.equal((await f.request({action: 'finish_upload', key: item.key, md5: 'e'.repeat(32), remote_md5: '', remote_mtime: 0})).status, 200);
    assert.equal(item.attachmentSyncedHash, null);
    assert.equal(item.attachmentSyncState, 'to_upload');
    item.attachmentLinkMode = 2;
    assert.equal((await f.request({action: 'prepare_upload', key: item.key, md5: 'e'.repeat(32)})).status, 409);
    assert.equal((await f.request({action: 'finish_upload', key: item.key, md5: 'e'.repeat(32), remote_md5: '', remote_mtime: 0})).status, 409);
  }
  {
    const f = fixture(), item = new f.Item('attachment');
    item.path = '/storage/approved-intake.docx'; await item.save();
    assert.equal(item.note, '');
    assert.equal(item.isPDFAttachment(), false);
    const receipt = await f.request({action: 'prepare_upload', key: item.key, md5: ''});
    assert.equal(receipt.status, 200, 'Approved ordinary non-PDF intake shares the authorized stored-file receipt protocol');
    f.files.set(item.path, 'e'.repeat(32)); item.attachmentSyncedHash = 'e'.repeat(32);
    assert.equal((await f.request({action: 'finish_upload', key: item.key, md5: 'e'.repeat(32),
      remote_md5: receipt.value.remote_md5, remote_mtime: receipt.value.remote_mtime})).status, 200);
    assert.equal(item.attachmentSyncedHash, null);
    assert.equal(item.attachmentSyncState, 'to_upload');
  }
  console.log('PASS: citation identity, storage failure rollback, digest serialization, and account-upload receipts');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
