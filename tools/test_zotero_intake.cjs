const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {viewFixture} = require('./test_zotero_ui_recovery.cjs');

async function referenceViewChecks() {
  const key = 'PARENT23', attachment = 'FILE2345';
  const row = {id: 'reference-review', identity: key, kind: 'reference', status: 'pending', digest: 'current',
    payload: {title: 'Reference', parent: key, url: 'https://example.org/paper', pdf_state: 'none'}};
  const fixture = state => {
    const f = viewFixture({notableReferencePDFState: state});
    const el = f.ui.el;
    f.ui.el = (...args) => Object.assign(el(...args), {isConnected: true, classList: {add() {}}});
    f.ui.reviewNavigationButton = (label, parent, target, keys, context) => {
      const button = f.ui.button(label, parent, () => context?.onOpened?.());
      button.target = target; button.keys = keys;
      return button;
    };
    f.ui.latest.reviews = [row];
    return f;
  };
  const descendants = root => [root, ...(root.children || []).flatMap(descendants)];
  const states = [
    [{state: 'none'}, 'none', null],
    [{state: 'unavailable', attachment}, 'unavailable', 'Show PDF attachment in Zotero'],
    [{state: 'available', attachment}, 'available', 'Open PDF in Zotero'],
    [{state: 'managed', attachment}, 'managed', 'Open notes PDF in Zotero'],
    [{state: 'file', attachment}, 'file', 'Show attached file in Zotero'],
    [{state: 'unsupported', attachment}, 'unsupported', 'Show PDF attachment in Zotero'],
    [{state: 'managed', attachment, replacement: true}, 'replaced', 'Open notes PDF in Zotero'],
    [{state: 'none', replacement: true}, 'replaced', null],
    [{state: 'removed'}, 'removed', null],
    [{state: 'unknown'}, 'unknown', null]
  ];
  for (const [result, expected, navigation] of states) {
    let resolve;
    const f = fixture(checkKey => {
      assert.equal(checkKey, key);
      return new Promise(done => { resolve = done; });
    });
    const detail = Object.assign(f.node(), {classList: {add() {}}});
    const statuses = [];
    const checking = f.ui.renderReferenceIntake(row, detail, state => statuses.push(state));
    const status = descendants(detail).find(node => node['data-reference-pdf-state'] === 'checking');
    assert.ok(status, 'The current Zotero reference is checked before showing an action');
    assert.equal(f.buttons.has('Attach downloaded PDF'), false);
    resolve(result);
    await checking;
    assert.equal(status['data-reference-pdf-state'], expected);
    assert.deepEqual(statuses, [expected]);
    assert.equal(f.buttons.has('Attach downloaded PDF'), expected === 'none');
    assert.equal(f.buttons.has('Find and attach PDF online'), expected === 'none');
    assert.equal(descendants(detail).some(node => node.textContent.includes('Save the PDF to Downloads')), expected === 'none');
    assert.equal(f.buttons.get('Dismiss this reference')[0].hidden, !['none', 'unavailable'].includes(expected));
    if (navigation) {
      const button = f.buttons.get(navigation)[0];
      assert.equal(button.keys[0], attachment);
      assert.equal(button.target, ['unavailable', 'unsupported', 'file'].includes(expected) ? 'attachment' : 'pdf');
    }
  }
  for (const cancel of [false, true]) {
    let result = {state: 'none'}, resolvePicker;
    const f = fixture(async () => result);
    const downloads = 'C:/Users/Example/Downloads';
    const chosen = downloads + '/Article.pdf';
    let pickerCount = 0, attaches = 0;
    f.context.Services = {dirsvc: {get: name => {
      assert.equal(name, 'DfltDwnld'); return {path: downloads};
    }}};
    f.context.Ci = {nsIFile: {}};
    f.context.ChromeUtils = {importESModule: () => ({FilePicker: class {
      modeOpen = 0;
      returnOK = 0;
      file = chosen;
      init(_window, title, mode) { assert.equal(title, 'Attach downloaded PDF'); assert.equal(mode, 0); }
      appendFilter(label, pattern) { assert.equal(label, 'PDF files'); assert.equal(pattern, '*.pdf'); }
      show() {
        assert.equal(this.displayDirectory, downloads);
        pickerCount++;
        return new Promise(resolve => { resolvePicker = resolve; });
      }
    }})};
    f.context.notableIsPDFFile = () => true;
    f.context.notableAcquirePDF = async (selected, file) => {
      assert.equal(selected, key); assert.equal(file, chosen);
      attaches++;
      result = {state: 'available', attachment};
      return {outcome: 'added', attachment: {}};
    };
    f.ui.checkInbox = async () => {};
    const detail = Object.assign(f.node(), {classList: {add() {}}});
    await f.ui.renderReferenceIntake(row, detail);
    const button = f.buttons.get('Attach downloaded PDF')[0];
    const choose = button.click();
    assert.equal(f.ui.acquiringPDF, true, 'Keep background checks out of the native picker');
    await button.click();
    assert.equal(pickerCount, 1, 'A second click cannot open another picker');
    resolvePicker(cancel ? 1 : 0);
    await choose;
    assert.equal(attaches, cancel ? 0 : 1);
    assert.equal(f.ui.acquiringPDF, false, 'Confirming or canceling restores the controls');
  }
  {
    let result = {state: 'none'};
    const f = fixture(async () => result);
    const page = f.node();
    f.ui.renderReview(page, 'inbox');
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(descendants(page).find(node => node['data-queue-summary'] === 'inbox').textContent,
      '0 ready to file · 1 needs a PDF · 0 waiting on Library Issues');
    result = {state: 'managed', attachment, replacement: true};
    await f.buttons.get('Show reference in Zotero')[0].click();
    assert.equal(descendants(page).some(node => node.class === 'reference-pdf-actions'), false);
    assert.match(descendants(page).find(node => node['data-reference-pdf-state'] === 'replaced').textContent, /surviving Zotero reference already has a reMarkable notes PDF/);
    assert.equal(descendants(page).find(node => node['data-queue-summary'] === 'inbox').textContent,
      '0 ready to file · 1 to check in Zotero · 0 waiting on Library Issues');
    assert.equal(f.ui.zoteroInboxDirty, true);
  }
  for (const outcome of ['existing', 'added', 'other']) {
    let result = {state: 'none'};
    const f = fixture(async () => result);
    const messages = []; let checked = 0;
    f.ui.message = value => messages.push(value);
    f.ui.checkInbox = async () => { checked++; };
    f.context.notableIsPDFFile = item => item.isPDF;
    f.context.notableAcquirePDF = async (selected, file, options) => {
      assert.equal(selected, key); assert.equal(file, null); assert.equal(options.describe, true);
      if (outcome !== 'other') result = {state: 'available', attachment};
      return {outcome: outcome === 'existing' ? 'existing' : 'added', attachment: {isPDF: outcome !== 'other'}};
    };
    const detail = Object.assign(f.node(), {classList: {add() {}}});
    await f.ui.renderReferenceIntake(row, detail);
    await f.buttons.get('Find and attach PDF online')[0].click();
    assert.equal(checked, 1);
    assert.match(messages[0], outcome === 'existing' ? /already attached/ : outcome === 'added' ? /PDF attached/ : /another file type/);
    assert.equal(descendants(detail).some(node => node.class === 'reference-pdf-actions'), outcome === 'other');
  }
}

async function main() {
  const items = new Map(); let imports = 0, retrieves = 0;
  const parent = {key: 'PARENT23', id: 1, libraryID: 1, loadAllData: async () => {}, isRegularItem: () => true,
    deleted: false, getAttachments: () => []};
  items.set(parent.key, parent);
  const errors = [];
  const Zotero = {Libraries: {userLibraryID: 1}, Items: {getByLibraryAndKeyAsync: async (_, key) => items.get(key),
    getAsync: async ids => ids.map(id => items.get(id)), loadDataTypes: async () => {}},
    URI: {getLibraryURI: () => 'zotero:library', getItemURI: item => `zotero:library/items/${item.key}`},
    Relations: {replacedItemPredicate: 'dc:replaces', getByPredicateAndObject: async () => []}, logError: error => errors.push(error),
    Attachments: {importFromFile: async options => {imports++; assert.equal(options.parentItemID, 1); return {key: 'PDF23456'};},
      addAvailableFile: async item => {retrieves++; assert.equal(item, parent); return false;}}};
  const context = vm.createContext({Zotero}); vm.runInContext(fs.readFileSync('zotero-support/library.js', 'utf8'), context);
  await assert.rejects(context.notableAcquirePDF(parent.key), /No available file/);
  assert.equal(retrieves, 1);
  assert.equal((await context.notableAcquirePDF(parent.key, 'download.pdf')).key, 'PDF23456');
  assert.equal(imports, 1);
  parent.getAttachments = () => [2];
  let path = 'existing.pdf';
  const pdf = {key: 'EXIST234', parentKey: parent.key, loadAllData: async () => {}, isFileAttachment: () => true,
    isPDFAttachment: () => true, getFilePathAsync: async () => path, getNote: () => ''};
  items.set(2, pdf);
  items.set(pdf.key, pdf);
  assert.equal((await context.notableReferencePDFState(parent.key)).state, 'available');
  const reused = await context.notableAcquirePDF(parent.key, 'download.pdf', {describe: true});
  assert.equal(reused.attachment.key, 'EXIST234');
  assert.equal(reused.outcome, 'existing');
  assert.equal(imports, 1, 'A PDF arriving during the file picker must be reused');
  pdf.getNote = () => 'Notable pilot: test';
  assert.equal((await context.notableReferencePDFState(parent.key)).state, 'managed');
  path = false;
  assert.equal((await context.notableReferencePDFState(parent.key)).state, 'unavailable');
  await assert.rejects(context.notableAcquirePDF(parent.key, 'download.pdf'), /already lists a PDF/);
  assert.equal(imports, 1, 'A listed PDF should be located or downloaded through Zotero before attaching another');
  pdf.getFilePathAsync = async () => {throw Error('Cannot check path');};
  assert.equal((await context.notableReferencePDFState(parent.key)).state, 'unknown');
  assert.equal(errors.length, 1);
  pdf.deleted = true;
  assert.equal((await context.notableReferencePDFState(parent.key)).state, 'none');
  parent.deleted = true;
  await assert.rejects(context.notableAcquirePDF(parent.key, 'download.pdf'), /unavailable/);
  const survivor = {...parent, key: 'SURVIV23', deleted: false, getAttachments: () => []};
  items.set(survivor.key, survivor);
  Zotero.Relations.getByPredicateAndObject = async () => [survivor];
  const replacement = await context.notableReferencePDFState(parent.key);
  assert.equal(replacement.parent, survivor.key);
  assert.equal(replacement.replacement, true);
  await assert.rejects(context.notableAcquirePDF(parent.key, 'download.pdf'), /unavailable/);
  const uiContext = vm.createContext({setTimeout, clearTimeout, setInterval, clearInterval,
    IOUtils: {readUTF8: async () => JSON.stringify({tablet_root: 'configured'})}, PathUtils: {join: (...v) => v.join('/')}});
  vm.runInContext(fs.readFileSync('zotero-support/ui.js', 'utf8'), uiContext);
  const ui = uiContext.NotableUI; const painted = [];
  Object.assign(ui, {statePath: '/profile/notable', yieldBackground() {}, render() {painted.push(this.tab);},
    refresh: async () => {}, prepareReviewGuidance: async () => {}, checkInbox: async () => {}});
  await ui.mount({closed: false, addEventListener() {}}); clearInterval(ui.activityTimer);
  assert.equal(painted[0], 'inbox', 'The first paint uses saved configuration');
  assert.equal(painted.includes('setup'), false, 'Configured startup never flashes Setup');
  await referenceViewChecks();
  console.log('PASS: reference PDF actions, live attachment and merge guards, retrieval feedback, and configured first paint');
}
main().catch(error => {console.error(error); process.exitCode = 1;});
