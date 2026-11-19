const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

async function main() {
  const events = [];
  const handwriting = {id: 11, annotationIsExternal: true, annotationType: 'image',
    annotationComment: 'Handwritten note'};
  const other = {...handwriting, id: 12, annotationComment: 'Unrelated image'};
  const added = {...handwriting, id: 13};
  let annotations = [handwriting, other];
  let finishRendering;
  const rendered = new Promise(resolve => { finishRendering = resolve; });
  const attachment = {id: 1, isPDFAttachment: () => true, loadAllData: async () => {},
    getNote: () => 'Notable pilot: managed', getAnnotations: () => annotations,
    saveTx: async () => {}};
  const Zotero = {
    Server: {LocalAPI: {Root: class {}}, Endpoints: {}},
    Libraries: {userLibraryID: 1},
    Items: {getByLibraryAndKeyAsync: async () => attachment},
    Item: class {},
    Reader: {_readers: [{itemID: 1, _initPromise: Promise.resolve(), reload: async () => events.push('reload')},
      {itemID: 99, reload: async () => assert.fail('An unrelated PDF was reloaded')} ]},
    PreferencePanes: {register: async () => {}},
    PDFWorker: {
      import: async () => { annotations = [handwriting, other, added]; },
      renderAttachmentAnnotations: () => { events.push('render'); return rendered; }
    },
    Annotations: {
      removeCacheImage: async annotation => { events.push(`invalidate:${annotation.id}`); },
      hasCacheImage: async annotation => [handwriting.id, added.id].includes(annotation.id)
    },
    Notifier: {trigger: async (event, type, ids) => {
      events.push([event, type, Array.from(ids)]);
    }},
    logError: error => events.push(error)
  };
  const context = vm.createContext({
    Zotero,
    Cc: {'@mozilla.org/addons/addon-manager-startup;1': {
      getService: () => ({registerChrome: () => ({})})
    }},
    Ci: {},
    Services: {io: {newURI: uri => uri}, scriptloader: {loadSubScript() {}}},
    fetch: async () => ({json: async () => ({version: '0.16.2'})}),
    notableRegisterLibraryEndpoint() {},
    NotableUI: {start: async () => {}},
    NotableReadingStatus: {register() {}},
    NotableSyncColumn: {register() {}},
    NotableAnnotationJump: {register() {}},
    NotableAnnotationRefresh: {register() {}, preserve: async () => {}}
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../zotero-support/bootstrap.js'), 'utf8'), context);
  await context.startup({id: 'notable-companion@local.invalid', rootURI: 'resource://test/'});
  const Endpoint = Zotero.Server.Endpoints['/api/notable/annotations/:itemKey'];
  const [status, , body] = await new Endpoint().run({
    method: 'POST', headers: new Map([['Zotero-Server-ID', 'fixture']]), pathParams: {itemKey: 'PDF23456'}
  });
  assert.equal(status, 200);
  assert.equal(JSON.parse(body).readersRefreshed, 1);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(JSON.parse(body).imagesReady, false);
  assert.deepEqual(events, ['reload', 'invalidate:11', 'invalidate:13', 'render']);
  finishRendering();
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(events, ['reload', 'invalidate:11', 'invalidate:13', 'render', ['modify', 'item', [11, 13]]]);
  const [, , ready] = await new Endpoint().run({method: 'GET',
    headers: new Map([['Zotero-Server-ID', 'fixture']]), pathParams: {itemKey: 'PDF23456'}});
  assert.equal(JSON.parse(ready).imagesReady, true);
  Zotero.PDFWorker.renderAttachmentAnnotations = async () => { throw Error('Synthetic image failure'); };
  await new Endpoint().run({method: 'POST', headers: new Map([['Zotero-Server-ID', 'fixture']]), pathParams: {itemKey: 'PDF23456'}});
  await new Promise(resolve => setImmediate(resolve));
  const [, , failed] = await new Endpoint().run({method: 'GET',
    headers: new Map([['Zotero-Server-ID', 'fixture']]), pathParams: {itemKey: 'PDF23456'}});
  assert.equal(JSON.parse(failed).imagesFailed, true);
  Zotero.PDFWorker.renderAttachmentAnnotations = async () => {};
  await new Endpoint().run({method: 'POST', headers: new Map([['Zotero-Server-ID', 'fixture']]), pathParams: {itemKey: 'PDF23456'}});
  await new Promise(resolve => setImmediate(resolve));
  const [, , retried] = await new Endpoint().run({method: 'GET',
    headers: new Map([['Zotero-Server-ID', 'fixture']]), pathParams: {itemKey: 'PDF23456'}});
  assert.equal(JSON.parse(retried).imagesReady, true);
}

main().then(() => console.log('Handwriting thumbnail refresh passed.'), error => {
  console.error(error);
  process.exitCode = 1;
});
