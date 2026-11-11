const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
async function main() {
  const items = new Map(); let imports = 0, retrieves = 0;
  const parent = {key: 'PARENT23', id: 1, libraryID: 1, loadAllData: async () => {}, isRegularItem: () => true,
    deleted: false, getAttachments: () => []};
  items.set(parent.key, parent);
  const Zotero = {Libraries: {userLibraryID: 1}, Items: {getByLibraryAndKeyAsync: async (_, key) => items.get(key), getAsync: async id => items.get(id)},
    Attachments: {importFromFile: async options => {imports++; assert.equal(options.parentItemID, 1); return {key: 'PDF23456'};},
      addAvailableFile: async item => {retrieves++; assert.equal(item, parent); return false;}}};
  const context = vm.createContext({Zotero}); vm.runInContext(fs.readFileSync('zotero-support/library.js', 'utf8'), context);
  await assert.rejects(context.notableAcquirePDF(parent.key), /No available file/);
  assert.equal(retrieves, 1);
  assert.equal((await context.notableAcquirePDF(parent.key, 'download.pdf')).key, 'PDF23456');
  assert.equal(imports, 1);
  parent.getAttachments = () => [2];
  items.set(2, {key: 'EXIST234', isPDFAttachment: () => true, fileExists: async () => true});
  assert.equal((await context.notableAcquirePDF(parent.key, 'download.pdf')).key, 'EXIST234');
  assert.equal(imports, 1, 'A PDF arriving during the file picker must be reused');
  parent.deleted = true;
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
  console.log('PASS: phone PDF retrieval, unavailable/Trash guards, duplicate avoidance, and configured first paint');
}
main().catch(error => {console.error(error); process.exitCode = 1;});
