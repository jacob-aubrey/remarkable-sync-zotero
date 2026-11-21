const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const vm = require('node:vm');

async function test() {
  const registrations = [], removed = [], calls = [], prompts = [], updates = [], readerHooks = new Map(), timers = new Map();
  let confirm = true;
  const scope = {setTimeout: (callback, delay) => { const token = Symbol(); timers.set(token, {callback, delay}); return token; },
    clearTimeout: token => timers.delete(token), Zotero: {
    Libraries: {userLibraryID: 1},
    MenuManager: {registerMenu: data => registrations.push(data), unregisterMenu: id => removed.push(id)},
    Reader: {registerEventListener(type, listener) { readerHooks.set(type, listener); }, unregisterEventListener(type) { readerHooks.delete(type); }},
    logError: error => { throw error; },
  }, Services: {prompt: {alert: assert.fail, confirm(window, title, message) { prompts.push({title, message}); return confirm; }}},
  notableReviewItemInTrash: item => !!(item.deleted || item.parentItem?.deleted)};
  for (const file of ['notebook.js', 'context-menu.js']) {
    vm.runInNewContext(readFileSync(join(__dirname, '../zotero-support', file), 'utf8'), scope);
  }
  const menu = scope.NotableContextMenu, notebook = scope.NotableNotebook;
  function windowFixture() {
    function element(tag) {
      return {children: [], style: {}, attributes: {},
        setAttribute(key, value) { this.attributes[key] = value; },
        set textContent(value) { this.text = value; if (tag === 'div') updates.push(value); },
        set value(value) { this.progress = value; updates.push(value); },
        append(...children) {
          for (const child of children) { child.parent = this; child.isConnected = true; }
          this.children.push(...children);
          if (tag === 'window') updates.push('shown');
        },
        remove() { this.parent.children = this.parent.children.filter(child => child !== this); this.isConnected = false; },
      };
    }
    return {document: {documentElement: element('window'), createElementNS: (_namespace, tag) => element(tag)},
      addEventListener() {}, removeEventListener() {}};
  }
  const ui = {id: 'plugin', shortName: 'reMarkable Sync', latest: {config: {tablet_root: 'root'}, links: []},
    open: () => calls.push(['open']), openSetup() {}, openContextTab: tab => calls.push(['tab', tab]),
    paperSearchState() { return this.search ||= {}; }, canSync: () => true, canCheckDuringSync: () => false,
    filingConnection: () => JSON.stringify(['connection', 'zotero', 'root']),
    async action(method, params, render, onProgress) {
      calls.push([method, params]);
      if (onProgress) {
        onProgress('Starting…');
        onProgress('Publishing a paper to Zotero [tablet item fixture]…', {checked: 1, total: 2});
      }
      return {status: {message: 'Selected sync complete.'}};
    },
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
    return {items, menuElem: {ownerGlobal: windowFixture(), setAttribute(key, value) { this[key] = value; }},
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
  assert.ok(visible([aPDF]).includes('Sync this PDF now'));
  assert.ok(visible([a, b]).includes('Sync selected papers now'));
  assert.ok(!visible([a]).includes('Sync all linked folders now'));
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
  const beforeCancel = calls.length;
  confirm = false;
  await find('Pause paper link (keep both copies)').onCommand(null, context([aPDF]));
  assert.equal(calls.length, beforeCancel, 'Canceling the confirmation must not change the link');
  assert.ok(prompts.at(-1).message.includes('stop syncing'));
  confirm = true;
  await find('Pause paper link (keep both copies)').onCommand(null, context([aPDF]));
  assert.equal(calls.at(-1)[0], 'link');
  assert.equal(calls.at(-1)[1].identity, 'cloud-a');
  const beforeSyncPrompts = prompts.length;
  const selectedContext = context([a, b, aPDF]);
  const pending = find('Sync selected papers now').onCommand(null, selectedContext);
  const [panel] = selectedContext.menuElem.ownerGlobal.document.documentElement.children;
  assert.equal(panel.id, 'notable-selected-sync-progress', 'Show selected progress inside the library immediately');
  assert.equal(panel.style.pointerEvents, 'none');
  assert.equal(menu.canSyncSelection(ui), false, 'A duplicate command cannot replace an active notice');
  await pending;
  assert.equal(panel.children[2].progress, 100);
  const [completion] = timers.values();
  assert.equal(completion.delay, 6000, 'Keep the result visible when the worker finishes');
  completion.callback();
  assert.equal(panel.isConnected, false);
  assert.equal(calls.at(-1)[0], 'sync_selected');
  assert.deepEqual(Array.from(calls.at(-1)[1].targets, target => target.cloud), ['cloud-a', 'cloud-b']);
  assert.equal(prompts.length, beforeSyncPrompts, 'Ordinary selected sync does not need a destructive-action confirmation');
  scope.Zotero.Items = {get: () => aPDF};
  const readerMenus = [];
  const readerWindow = windowFixture();
  readerHooks.get('createViewContextMenu')({reader: {itemID: aPDF.id, _window: readerWindow}, params: {}, append: menu => readerMenus.push(menu)});
  assert.equal(readerMenus[0].label, 'reMarkable Sync');
  assert.equal(readerMenus[0].groups[0][0].label, 'Sync this PDF now');
  await readerMenus[0].groups[0][0].onCommand();
  const [readerPanel] = readerWindow.document.documentElement.children;
  assert.equal(readerPanel.id, 'notable-selected-sync-progress', 'Place progress inside the window that owns the PDF reader');
  assert.deepEqual(Array.from(calls.at(-1)[1].targets, target => target.cloud), ['cloud-a']);
  ui.busy = true;
  ui.inboxChecking = true;
  const selectedPapers = context([aPDF, b]);
  const papersCommand = find('Sync selected papers now');
  papersCommand.onShowing(null, selectedPapers);
  assert.equal(selectedPapers.enabled, true, 'A quiet Inbox check leaves the selected-paper command available');
  await papersCommand.onCommand(null, selectedPapers);
  assert.deepEqual(Array.from(calls.at(-1)[1].targets, target => target.cloud), ['cloud-a', 'cloud-b']);
  const folderRow = items => ({isCollection: () => true, ref: {getChildItems: () => items, getChildCollections: () => []}});
  const selectedFolders = {...context(), collectionTreeRows: [folderRow([a]), folderRow([a, b])]};
  const folderCommand = collectionMenu.menus[0];
  collectionMenu.onShowing(null, selectedFolders);
  assert.equal(selectedFolders.visible, true);
  folderCommand.onShowing(null, selectedFolders);
  assert.equal(selectedFolders.enabled, true);
  await folderCommand.onCommand(null, selectedFolders);
  assert.deepEqual(Array.from(calls.at(-1)[1].targets, target => target.cloud), ['cloud-a', 'cloud-b']);
  readerMenus.length = 0;
  readerHooks.get('createViewContextMenu')({reader: {itemID: aPDF.id, _window: readerWindow}, params: {}, append: menu => readerMenus.push(menu)});
  assert.equal(readerMenus[0].groups[0][0].disabled, false);
  updates.length = 0;
  await readerMenus[0].groups[0][0].onCommand();
  assert.deepEqual(Array.from(calls.at(-1)[1].targets, target => target.cloud), ['cloud-a']);
  assert.deepEqual(updates, ['Waiting for the Inbox check before syncing 1 selected PDF…', 'shown',
    'Syncing 1 selected PDF…', 'Publishing a paper to Zotero…', 50, 100, 'Selected sync complete.']);
  assert.equal(readerPanel.isConnected, false, 'A second sync replaces the previous result');
  assert.equal(readerWindow.document.documentElement.children.length, 1);
  ui.inboxChecking = false;
  const count = calls.length;
  await find('Pause paper link (keep both copies)').onCommand(null, context([a]));
  assert.equal(calls.length, count, 'A busy worker must not receive another mutation');
  ui.busy = false; ui.latest.links = [];
  await find('Pause paper link (keep both copies)').onCommand(null, context([a]));
  assert.equal(calls.length, count, 'A removed link must not run a stale menu command');
  menu.unregister();
  assert.equal(timers.size, 0, 'Unloading removes scheduled notices from all windows');
  assert.equal(readerHooks.size, 0);
  assert.deepEqual(removed, [...menu.menuIDs]);
  console.log('Context menus: selection scope, exact PDF links, batches, Trash, groups, busy state, and cleanup passed.');
}
test().catch(error => { console.error(error); process.exitCode = 1; });
