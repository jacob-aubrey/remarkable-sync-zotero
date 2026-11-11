// Real controller, synthetic views/transports only; no profiles or library writes.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('zotero-support/ui.js', 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
function fixture(extra = {}) {
  const writes = [], context = vm.createContext({setTimeout, clearTimeout, setInterval, clearInterval,
    IOUtils: {writeUTF8: async (_path, text) => writes.push(JSON.parse(text))}, PathUtils: {join: (...parts) => parts.join('/')}, ...extra});
  vm.runInContext(source, context); const ui = context.NotableUI, nativeDisable = ui.disable;
  ui.statePath = 'synthetic'; ui.stopped = false;
  ui.latest = {config: {connection_id: 'C', zotero_root: 'Z', tablet_root: 'T'}, collections: [{key: 'Z', name: 'Library'}],
    mappings: {}, status: {}, types: {journal_article: 'Journal article'}, reviews: [], links: []};
  ui.message = ui.disable = ui.schedule = ui.badge = ui.updateActivity = ui.refreshCollectionNames = () => {};
  return {ui, writes, context, nativeDisable};
}
function viewFixture(extra = {}) {
  const value = fixture(extra), {ui} = value, fields = new Map(), buttons = new Map();
  function node(tag = '', text = '') {
    return {tag, textContent: text, children: [], dataset: {}, value: '', listeners: {}, checked: false,
      addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); },
      fire(name) { for (const callback of this.listeners[name] || []) callback({}); },
      setAttribute(name, text) { this[name] = text; }, appendChild(child) { this.children.push(child); },
      replaceChildren() { this.children = []; }, querySelectorAll() { return []; },
      get options() { return this.children.filter(child => child.tag === 'option'); },
      get selectedOptions() { return this.options.filter(child => child.selected); }};
  }
  ui.el = (tag, text, parent, attrs = {}) => { const element = node(tag, text); Object.assign(element, attrs); parent?.appendChild(element); return element; };
  ui.field = (label, parent, initial = '', tag = 'input') => { const element = ui.el(tag, '', parent); element.value = initial; element.notableRefreshValue = () => {}; fields.set(label, element); return element; };
  ui.checkbox = (label, parent, checked) => { const element = ui.field(label, parent); element.checked = checked; return element; };
  ui.select = (label, parent, choices, selected = '', multiple = false) => {
    const element = ui.field(label, parent, selected, 'select'); element.multiple = multiple;
    for (const [key, name] of choices) { const option = ui.el('option', name, element, {value: key}); option.selected = multiple ? selected.includes(key) : key === selected; }
    return element;
  };
  ui.button = (label, parent, click) => { const element = ui.el('button', label, parent); element.click = click; (buttons.get(label) || buttons.set(label, []).get(label)).push(element); return element; };
  // This harness tests filing/draft behavior; the real adapter has its own folder UI harness.
  ui.folderSelect = (...args) => ui.select(...args);
  // Real wrapping adapters are covered by the paper-picker integration harness.
  ui.paperSelect = (...args) => ui.select(...args);
  ui.destinationSelect = (label, parent, selected) => ui.field(label, parent, selected);
  ui.reviewOpenButton = ui.paperTagPanel = () => {};
  ui.supportingInformationPicker = parent => ({panel: ui.el('section', '', parent), query: node(), select: node(), load() {}});
  return {...value, fields, buttons, node};
}
async function main() {
  for (const condition of ['live', 'missing', 'trashed', 'attachment', 'invalid-key']) {
    const selected = [], lookups = [], actions = []; let focused = 0, opened = 0;
    const item = condition === 'missing' ? null : {id: 73, key: 'PARENT23', libraryID: 1, deleted: condition === 'trashed',
      isRegularItem: () => condition !== 'attachment', loadAllData: async () => {}};
    const value = viewFixture({Zotero: {Libraries: {userLibraryID: 1},
      Items: {getByLibraryAndKeyAsync: async (library, key) => { lookups.push([library, key]); return item; }},
      getMainWindow: () => ({focus: () => focused++}),
      getActiveZoteroPane: () => ({selectItems: async ids => selected.push(...ids)})}});
    const {ui, context, buttons, node} = value;
    vm.runInContext(fs.readFileSync('zotero-support/library.js', 'utf8'), context);
    ui.action = async (...args) => actions.push(args);
    ui.reviewOpenButton = () => opened++;
    const row = {id: 'conflict', kind: 'conflict', status: 'pending', digest: 'original',
      payload: {type: 'paper', zotero: condition === 'invalid-key' ? 'invalid' : 'PARENT23',
        desktop: ['Desktop name', 'Z'], tablet: ['Tablet name', 'Z']}};
    ui.latest.reviews = [row];
    const before = JSON.stringify({row, queue: ui.filingQueue});
    ui.renderReview(node());
    assert.equal(opened, 1, 'The existing Open paper action remains available');
    assert.ok(buttons.has('Apply Zotero name and location') && buttons.has('Apply reMarkable name and location'));
    const button = buttons.get('Open Zotero reference')[0];
    assert.equal(button.dataset.browse, 'true');
    if (condition === 'live') {
      await button.click(); assert.deepEqual(selected, [73]); assert.equal(focused, 1);
    } else {
      const reason = condition === 'trashed' ? /in Trash.*recheck/i
        : condition === 'attachment' ? /expected kind of Zotero item.*Recheck/
        : condition === 'invalid-key' ? /invalid Zotero item keys.*Recheck/ : /no longer available.*Recheck/;
      await assert.rejects(button.click(), error => reason.test(error.message) && /Use Sync now to refresh this device conflict/.test(error.message));
      assert.deepEqual(selected, []); assert.equal(focused, 0);
    }
    assert.deepEqual(lookups, condition === 'invalid-key' ? [] : [[1, 'PARENT23']], 'Use the safe native lookup and parent key');
    assert.deepEqual(actions, [], 'Navigation must not resolve, approve or sync');
    assert.equal(JSON.stringify({row, queue: ui.filingQueue}), before, 'The conflict remains pending with its original choices');
  }
  {
    const {ui, buttons, node} = viewFixture();
    ui.latest.reviews = [{id: 'folder', kind: 'conflict', status: 'pending',
      payload: {type: 'folder', zotero: 'PARENT23', desktop: ['Desktop folder'], tablet: ['Tablet folder']}}];
    ui.renderReview(node());
    assert.equal(buttons.has('Open Zotero reference'), false, 'Folder conflicts do not offer paper navigation');
    assert.ok(buttons.has('Apply Zotero name and location') && buttons.has('Apply reMarkable name and location'));
  }
  for (const failAt of [1, 2]) {
    let attempt = 0; const writes = [];
    const {ui} = fixture({IOUtils: {writeUTF8: async (_path, text) => {
      await tick(); if (++attempt === failAt) throw Error('Disk full'); writes.push(JSON.parse(text));
    }}});
    ui.busy = true;
    const one = ui.queueFiling('approve', {identity: 'one', digest: 'a'}).catch(error => error);
    const two = ui.queueFiling('approve', {identity: 'two', digest: 'b'}).catch(error => error);
    await Promise.all([one, two]);
    const expected = failAt === 1 ? 'two' : 'one';
    assert.deepEqual(Array.from(ui.filingQueue, row => row.params.identity), [expected]);
    assert.deepEqual(writes.at(-1).jobs.map(row => row.params.identity), [expected], 'Failed decisions must not reappear after restart');
  }
  {
    let rejectWrite, attempt = 0; const writes = [];
    const {ui} = fixture({IOUtils: {writeUTF8: async (_path, text) => {
      if (++attempt === 1) await new Promise((_resolve, reject) => { rejectWrite = reject; });
      writes.push(JSON.parse(text));
    }}});
    ui.busy = true;
    const failed = ui.queueFiling('approve', {identity: 'one', digest: 'd'}).catch(error => error);
    while (!rejectWrite) await tick();
    const laterSave = ui.saveFilingQueue(); rejectWrite(Error('Disk full'));
    await failed; await laterSave;
    assert.equal(writes.at(-1).jobs.length, 0, 'A later status/pause save cannot capture a failed approval snapshot');
  }
  {
    const {ui} = fixture();
    assert.equal(ui.validFilingJob({id: 'one', connection: 'C', method: 'approve_routine', params: {collection: 'Z', entries: [null]}}), false);
    assert.equal(ui.validFilingJob({id: 'one', connection: 'C', method: 'approve', params: {identity: 'x', digest: 'd', research: [null]}}), false);
    assert.equal(ui.validFilingJob({id: 'one', connection: 'C', method: 'approve', params: {identity: 'x', digest: 'd'}}), true);
  }
  {
    const {ui} = fixture(); ui.latest.reviews = [{id: 'one', status: 'applying'}];
    ui.rpc = async () => { ui.latest.status = {state: 'needs_review'}; return ui.latest; };
    await ui.queueFiling('approve_routine', {collection: 'Z', entries: [{id: 'one', digest: 'd'}]});
    assert.equal(ui.filingQueue.length, 1); assert.equal(ui.filingQueuePaused, true);
    assert.match(ui.queueError, /recovery/, 'A partial failure must keep its saved choices even without Stop');
  }
  {
    const {ui, fields, buttons, node} = viewFixture();
    ui.tagEditor = {key: 'PDF', page: 0, page_id: 'stable-page', token: 'original-token', title: 'Paper', vocabulary: [], paper_tags: ['old'], page_tags: []};
    ui.renderTags(node());
    fields.get('Add paper tag').value = 'new'; fields.get('Add paper tag').fire('input');
    buttons.get('Add tag')[0].click();
    fields.get('Add page tag').value = 'draft page'; fields.get('Add page tag').fire('input');
    ui.renderTags(node());
    assert.equal(fields.get('Add page tag').value, 'draft page', 'Unsubmitted tag text survives redraw');
    let payload;
    ui.action = async (method, params) => { assert.equal(method, 'set_page_tags'); payload = params; return {...ui.tagEditor, paper_tags: params.paper_tags, page_tags: params.tags}; };
    ui.render = () => {};
    await buttons.get('Save tags').at(-1).click();
    assert.deepEqual(Array.from(payload.paper_tags), ['old', 'new']);
    assert.deepEqual(Array.from(payload.tags), ['draft page']);
    assert.equal(payload.token, 'original-token');
    assert.equal(ui.tagDrafts.size, 0);
  }
  {
    const {ui, fields, node} = viewFixture();
    ui.latest.reviews = [{id: 'r', kind: 'cloud', status: 'pending', digest: 'd', payload: {filename: 'Experiment SI.pdf', title: 'Experiment', document_type: 'journal_article'}}];
    let picker;
    ui.supportingInformationPicker = parent => picker = {panel: ui.el('section', '', parent), query: node(), select: node(), load() {}};
    ui.renderReview(node());
    assert.equal(picker.panel.hidden, false);
    fields.get('Document type').value = 'journal_article'; fields.get('Document type').fire('change');
    assert.equal(picker.panel.hidden, true, 'A heuristic SI classification must allow explicit article override');
  }
  {
    const {ui, fields, buttons, node} = viewFixture();
    ui.latest.bulk = {id: 'plan', root: 'C:/papers', collection: 'Z', lookup: true, failures: [], rows: [
      {id: 'a', path: 'a.pdf', issues: [], copies: ['a.pdf'], status: 'ready'},
      {id: 'b', path: 'b.pdf', issues: ['Check source'], copies: ['b.pdf'], status: 'pending'}]};
    ui.renderImport(node());
    const lookup = fields.get('Retrieve citations using title-verified DOI metadata');
    lookup.checked = false; lookup.fire('change');
    const importButton = buttons.get('Import / reuse 1 routine files')[0];
    assert.equal(importButton.disabled, true);
    assert.throws(() => importButton.click(), /Scan again/);
    assert.throws(() => buttons.get('Retry import for this file')[0].click(), /Scan again/, 'Exception retry must respect visible privacy settings too');
    lookup.checked = true; lookup.fire('change');
    let payload; ui.action = async (_method, params) => { payload = params; };
    await importButton.click(); assert.equal(payload.lookup, true); assert.equal(payload.root, 'C:/papers');
    await buttons.get('Retry import for this file')[0].click(); assert.equal(payload.lookup, true); assert.equal(payload.row_id, 'b');
    fields.get('Literature folder, including subfolders').value = 'C:/other'; fields.get('Literature folder, including subfolders').fire('input');
    assert.throws(() => importButton.click(), /Scan again/);
  }
  {
    const {ui, fields, buttons, node} = viewFixture(); ui.folderCatalog = null;
    ui.renderSetup(node());
    assert.ok(fields.has('Search scanned text and handwriting locally'));
    assert.ok(fields.has('Watched ToSort folder (optional)'));
    assert.ok(buttons.has('Save setup'));
  }
  {
    const {ui, fields, buttons, node} = viewFixture();
    ui.latest.links = [{cloud: 'cloud', name: 'Original.pdf', collection: 'Z', revision: 'original-revision'}];
    ui.renderLinks(node());
    fields.get('PDF name').value = 'My draft.pdf'; fields.get('PDF name').fire('input');
    fields.get('Linked folders for this paper (Zotero → reMarkable)').value = 'new-home';
    fields.get('Linked folders for this paper (Zotero → reMarkable)').fire('change');
    ui.latest.links[0].name = 'Progress.pdf'; ui.latest.links[0].revision = 'new-revision';
    ui.renderLinks(node());
    assert.equal(fields.get('PDF name').value, 'My draft.pdf');
    assert.equal(fields.get('Linked folders for this paper (Zotero → reMarkable)').value, 'new-home');
    let payload; ui.action = async (_method, params) => { payload = params; }; ui.render = () => {};
    await buttons.get('Save name & destination').at(-1).click();
    assert.equal(payload.revision, 'original-revision', 'Unsaved moves retain their reviewed revision for backend conflict checks');
    assert.equal(ui.linkDrafts.size, 0);
  }
  {
    const {ui, fields, buttons, node} = viewFixture();
    ui.latest.mappings = {Z: {cloud: 'T', root: true}};
    ui.latest.deletions = [{id: 'deletion', payload: {title: 'Paper', inventory: [], restore_parent: 'removed-folder'}}];
    ui.renderReview(node()); fields.get('Restore to reMarkable folder').value = 'T';
    let payload; ui.action = async (_method, params) => { payload = params; };
    await buttons.get('Restore on reMarkable')[0].click(); assert.equal(payload.destination, 'T');
  }
  {
    const {ui, node} = viewFixture();
    const select = node('select'); select.value = 'old';
    ui.el('option', 'Old', select, {value: 'old'}).selected = true;
    ui.reconcileOptions(select, [['new', 'New collection']]);
    assert.equal(select.options.length, 1); assert.equal(select.options[0].value, 'new'); assert.equal(select.value, '');
    ui.reconcileOptions(select, [['new', 'New collection'], ['second', 'Added collection']]);
    assert.equal(select.options.length, 2, 'Live collection updates include new options and remove deleted ones');
  }
  {
    const {ui, context} = fixture({Zotero: {Items: {getAsync: async () => ({key: 'PDF', isPDFAttachment: () => true})}}});
    const order = []; ui.open = () => { order.push('open'); };
    ui.rpc = async () => { order.push('context'); return {}; };
    await ui.openPageTags({_internalReader: {_state: {primaryViewStats: {pageIndex: 0}}}, itemID: 1});
    assert.deepEqual(order, ['open', 'context']);
  }
  {
    let releaseLaunch, exited = false, kills = 0; const writes = [];
    const child = {stdin: {write: async text => { writes.push(JSON.parse(text)); exited = true; }},
      stderr: {readString: () => new Promise(() => {})}, stdout: {readString: () => new Promise(() => {})},
      wait: async () => { assert.equal(exited, true); }, kill: async () => { kills++; exited = true; }};
    const Zotero = {Prefs: {set() {}, get: name => name === 'notable.developmentWorker' ? '{"command":"synthetic","arguments":[]}' : false},
      Server: {init: async () => {}, port: 123}, Reader: {unregisterEventListener() {}, _readers: []}, MenuManager: {unregisterMenu() {}}};
    const {ui} = fixture({Zotero, ChromeUtils: {importESModule: () => ({Subprocess: {call: () => new Promise(resolve => { releaseLaunch = resolve; })}})}});
    const request = ui.rpc('sync').catch(error => error);
    while (!releaseLaunch) await tick();
    const stop = ui.stop(); await tick(); releaseLaunch(child); await stop;
    assert.match((await request).message, /closed/);
    assert.equal(kills, 1); assert.deepEqual(writes.map(row => row.method), ['shutdown']);
    assert.equal(ui.process, null, 'No late worker survives plugin shutdown');
  }
  for (const damage of ['missing-exe', 'corrupt-exe', 'missing-dll', 'corrupt-dll', 'healthy']) {
    const extracted = new Set(), fetched = [];
    const child = {stdin: {write: async () => {}}, stderr: {readString: () => new Promise(() => {})},
      stdout: {readString: () => new Promise(() => {})}, wait: async () => {}, kill: async () => {}};
    const manifest = {id: 'bundled', files: [{path: 'notable-worker.exe', md5: 'exe-hash'}, {path: '_internal/library.dll', md5: 'dll-hash'}]};
    const Zotero = {Prefs: {set() {}, get: () => false}, Server: {init: async () => {}, port: 123},
      Utilities: {Internal: {md5Async: async path => {
        const affected = damage === 'corrupt-exe' && path.endsWith('.exe') || damage === 'corrupt-dll' && path.endsWith('.dll');
        return !extracted.has(path) && affected ? 'corrupt-hash' : path.endsWith('.exe') ? 'exe-hash' : 'dll-hash';
      }}}};
    const {ui} = fixture({Zotero, PathUtils: {join: (...parts) => parts.join('/'), parent: path => path.slice(0, path.lastIndexOf('/'))},
      IOUtils: {exists: async path => extracted.has(path) || !(damage === 'missing-exe' && path.endsWith('.exe') || damage === 'missing-dll' && path.endsWith('.dll')), makeDirectory: async () => {},
        write: async path => extracted.add(path), writeUTF8: async () => {}},
      fetch: async url => { fetched.push(url); return url.endsWith('runtime-manifest.json') ? {json: async () => manifest} : {ok: true, arrayBuffer: async () => new ArrayBuffer(1)}; },
      ChromeUtils: {importESModule: () => ({Subprocess: {call: async () => child}})}});
    ui.rootURI = 'extension/'; await ui.launch();
    assert.equal(extracted.size, damage === 'healthy' ? 0 : 2, damage + ': verify every required runtime member before reusing its ready marker');
    assert.equal(fetched.length, damage === 'healthy' ? 1 : 3); await ui.closeProcess(child, false);
  }
  {
    let output, killed = false, options;
    const child = {stdin: {write: async () => {}}, stderr: {readString: () => new Promise(() => {})},
      stdout: {readString: () => new Promise(resolve => { output = resolve; })}, kill: async () => { killed = true; }};
    const Zotero = {Prefs: {set() {}, get: name => name === 'notable.developmentWorker' ? '{"command":"synthetic","arguments":[]}' : false}, Server: {init: async () => {}, port: 123}};
    const {ui} = fixture({Zotero, ChromeUtils: {importESModule: () => ({Subprocess: {call: async value => { options = value; return child; }}})}});
    const request = ui.rpc('status');
    while (!ui.pending.size) await tick();
    assert.equal(options.environment.PYMUPDF_MESSAGE, 'fd:2', 'Cached PyMuPDF diagnostics must use stderr from initial import');
    output('malformed JSON\n');
    await assert.rejects(request, /invalid response/); assert.equal(killed, true); assert.equal(ui.process, null);
  }
  {
    const {ui, context} = fixture(); let killed = false;
    ui.requestTimeout = 5; ui.process = {stdin: {write: async () => {}}, kill: async () => { killed = true; }};
    await assert.rejects(ui.rpc('status'), /stopped responding/);
    assert.equal(killed, true); assert.equal(ui.process, null); assert.equal(ui.pending.size, 0);
  }
  console.log('PASS: conflict reference navigation and stale-item guards; launch/shutdown race, stalled recovery, concurrent save rollback, queue validation, tag drafts, SI override, scan privacy, offline settings, and reader ordering');
}
module.exports = {fixture, viewFixture};
if (require.main === module) main().catch(error => { console.error(error); process.exitCode = 1; });
