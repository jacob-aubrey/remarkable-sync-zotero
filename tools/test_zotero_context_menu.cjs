const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const vm = require('node:vm');

async function test() {
  const registrations = [], removed = [], calls = [];
  const scope = {Zotero: {
    Libraries: {userLibraryID: 1},
    MenuManager: {registerMenu: data => registrations.push(data), unregisterMenu: id => removed.push(id)},
    Reader: {registerEventListener() {}, unregisterEventListener() {}},
    logError: error => { throw error; },
  }, Services: {prompt: {alert: assert.fail}}, notableReviewItemInTrash: item => !!(item.deleted || item.parentItem?.deleted)};
  for (const file of ['notebook.js', 'context-menu.js']) {
    vm.runInNewContext(readFileSync(join(__dirname, '../zotero-support', file), 'utf8'), scope);
  }
  const menu = scope.NotableContextMenu, notebook = scope.NotableNotebook;
  const ui = {id: 'plugin', shortName: 'reMarkable Sync', latest: {config: {tablet_root: 'root'}, links: []},
    open: () => calls.push(['open']), openSetup() {}, openContextTab: tab => calls.push(['tab', tab]),
    paperSearchState() { return this.search ||= {}; }, canSync: () => true, canCheckDuringSync: () => false,
    action: async (method, params) => calls.push([method, params]),
  };
  const paper = (id, key, options = {}) => ({id, key, libraryID: 1, isRegularItem: () => true,
    isPDFAttachment: () => false, isEditable: () => true, getAttachments: () => [id + 1], ...options});
  const a = paper(1, 'PARENT23'), b = paper(3, 'PARENT45');
  const pdf = (id, key, parent) => ({...paper(id, key), isRegularItem: () => false, isPDFAttachment: () => true,
    parentID: parent?.id, parentItem: parent});
  const aPDF = pdf(2, 'SOURCE23', a), unrelated = pdf(5, 'OTHERPDF', a);
  ui.latest.links = [{cloud: 'cloud-a', parent: a.key, source_key: aPDF.key, view_key: 'VIEW2345'},
    {cloud: 'cloud-b', parent: b.key, source_key: 'SOURCE45', view_key: 'VIEW4567', paused: true}];
  menu.register(ui);
  assert.equal(registrations.length, 2);
  const [itemMenu, collectionMenu] = registrations.map(row => row.menus[0]);
  assert.equal(itemMenu.menuType, 'submenu');
  assert.equal(collectionMenu.menuType, 'submenu');
  function context(items = []) {
    return {items, menuElem: {ownerGlobal: {}, setAttribute(key, value) { this[key] = value; }},
      setVisible(value) { this.visible = value; }, setEnabled(value) { this.enabled = value; }};
  }
  const visible = items => itemMenu.menus.filter(command => {
    if (!command.onShowing) return false;
    const ctx = context(items); command.onShowing(null, ctx);
    return ctx.visible;
  }).map(command => { const ctx = context(items); command.onShowing(null, ctx); return ctx.menuElem.label; });
  const find = label => itemMenu.menus.find(command => {
    const ctx = context([a]); command.onShowing?.(null, ctx); return ctx.menuElem.label === label;
  });
  assert.ok(visible([a]).includes('Pause paper link (keep both copies)'));
  assert.ok(!visible([a]).includes('Resume paper link'));
  assert.ok(visible([b]).includes('Resume paper link'));
  assert.ok(visible([aPDF]).includes('Open reMarkable PDF'));
  assert.ok(!visible([unrelated]).includes('Open reMarkable PDF'), 'An unrelated supplementary PDF must not inherit its parent’s link');
  assert.ok(visible([a, b]).includes(notebook.label));
  assert.ok(visible([a, b]).includes('Show selected links…'));
  assert.ok(!visible([a, b]).includes('Pause paper link (keep both copies)'));
  const notebookTargets = notebook.selectionTargets([a, aPDF, aPDF, pdf(9, 'STAND234')]);
  assert.equal(notebookTargets.length, 2);
  assert.equal(notebookTargets[0].item, a);
  assert.equal(notebook.selectionTargets([aPDF])[0].pdf, aPDF);
  assert.equal(notebook.selectionTargets([aPDF])[0].item, a);
  for (const item of [paper(8, 'TRASH234', {deleted: true}), paper(8, 'FEED2345', {isFeedItem: true}),
    {...aPDF, parentItem: {...a, deleted: true}}]) {
    const ctx = context([item]); itemMenu.onShowing(null, ctx); assert.equal(ctx.visible, false);
  }
  assert.ok(!visible([paper(7, a.key, {libraryID: 2})]).includes('Open reMarkable PDF'));
  assert.equal(calls.length, 0, 'Building a menu must not run worker operations');
  await find('Show selected links…').onCommand(null, context([a, b]));
  assert.deepEqual([...ui.search.contextClouds], ['cloud-a', 'cloud-b']);
  await find('Pause paper link (keep both copies)').onCommand(null, context([aPDF]));
  assert.equal(calls.at(-1)[0], 'link');
  assert.equal(calls.at(-1)[1].identity, 'cloud-a');
  const count = calls.length;
  ui.busy = true;
  await find('Pause paper link (keep both copies)').onCommand(null, context([a]));
  assert.equal(calls.length, count, 'A busy worker must not receive another mutation');
  ui.busy = false; ui.latest.links = [];
  await find('Pause paper link (keep both copies)').onCommand(null, context([a]));
  assert.equal(calls.length, count, 'A removed link must not run a stale menu command');
  menu.unregister();
  assert.deepEqual(removed, [...menu.menuIDs]);
  console.log('Context menus: selection scope, exact PDF links, batches, Trash, groups, busy state, and cleanup passed.');
}
test().catch(error => { console.error(error); process.exitCode = 1; });
