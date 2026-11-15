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
    let payload; ui.action = async (_method, params) => { payload = params; return {link_saved: true}; }; ui.render = () => {};
    await buttons.get('Save name & destination').at(-1).click();
    assert.equal(payload.revision, 'original-revision', 'Unsaved moves retain their reviewed revision for backend conflict checks');
    assert.equal(ui.linkDrafts.size, 0);
    assert.match(ui.saveFeedback.value, /saved for the next sync/);
  }
  {
    const {ui, buttons, node} = viewFixture();
    const calls = [];
    ui.tab = 'sync-review';
    ui.latest.links = [
      {cloud: 'unrelated', view_key: 'OTHERPDF', parent: 'OTHERREF'},
      {cloud: 'first', view_key: 'PDF23456', parent: 'PARENT23'}];
    ui.latest.status.result = {review: [
      {kind: 'keywords', cloud: 'first', title: 'Actual Zotero title', reason: 'Keyword warning', parent: 'PARENT23'},
      {kind: 'keywords', cloud: 'second', title: 'Second paper', reason: 'Keyword warning'}]};
    ui.action = async (...args) => calls.push(args);
    ui.reviewNavigationButton = (label, parent, type, keys) => ui.button(label, parent, () => calls.push([type, keys]));
    const root = node();
    ui.renderReview(root);
    assert.equal(buttons.get('Open PDF in Zotero').length, 1);
    assert.equal(buttons.has('Open reference already linked to reMarkable'), false);
    await buttons.get('Open PDF in Zotero')[0].click();
    await buttons.get('Recheck keywords')[0].click();
    await buttons.get('Recheck keyword warnings')[0].click();
    await ui.recheckKeywordsAfterSave('PARENT23');
    assert.deepEqual(JSON.parse(JSON.stringify(calls)), [['pdf', ['PDF23456']],
      ['recheck_keyword_warnings', {cloud: 'first'}], ['recheck_keyword_warnings'],
      ['recheck_keyword_warnings', {parent: 'PARENT23'}, true]]);
  }
  {
    const {ui} = fixture();
    const keyword = {kind: 'keywords', cloud: 'paper', parent: 'PARENT23', reason: 'Unreadable PDF'};
    const duplicate = {attachment: 'ATTACH23', cloud: 'paper', parent: 'PARENT24', reason: 'Exact PDF already linked'};
    const paperConflict = {kind: 'conflict', cloud: 'paper', reason: 'Both names changed'};
    const folderConflict = {kind: 'conflict', collection: 'FOLDER23', reason: 'Both folder names changed'};
    const filing = {cloud: 'pending', reason: 'Review this new tablet document before importing it.'};
    const unrepresented = {cloud: 'missing', reason: filing.reason};
    ui.latest.reviews = [
      {kind: 'conflict', identity: 'paper', payload: {type: 'paper', cloud: 'paper'}},
      {kind: 'conflict', identity: 'FOLDER23', payload: {type: 'folder', zotero: 'FOLDER23'}},
      {kind: 'zotero', identity: 'ATTACH23', payload: {}},
      {kind: 'cloud', identity: 'pending', payload: {}}];
    ui.latest.status.result = {review: [keyword, duplicate, paperConflict, {...keyword}, folderConflict, filing, unrepresented]};
    assert.deepEqual(Array.from(ui.syncWarnings()), [keyword, duplicate, unrepresented],
      'One decision on a link cannot hide unrelated warnings; only represented or repeated notices collapse');
  }
  {
    const {ui, context, buttons, node} = viewFixture();
    const calls = [];
    ui.latest.links = [{cloud: 'paper', view_key: 'ATTACH23', parent: 'PARENT23'},
      {cloud: 'unrelated', source_key: 'ATTACH24', parent: 'PARENT24'}];
    ui.latest.status.result = {review: [
      {kind: 'keywords', cloud: 'paper', parent: 'PARENT23', reason: 'Unreadable PDF'},
      {kind: 'keywords', attachment: 'ATTACH24', reason: 'Missing paper evidence'},
      {kind: 'keywords', cloud: 'stale', attachment: 'ATTACH24', parent: 'PARENT23', reason: 'Stale link'},
      {cloud: 'paper', reason: 'Tablet PDF changed'},
      {cloud: 'paper', attachment: 'ATTACH25', parent: 'PARENT25', reason: 'Exact PDF already linked'}]};
    context.notableNavigateReview = async (...args) => { calls.push(...args); return {}; };
    ui.renderReview(node(), 'sync-review');
    assert.equal(buttons.get('Open PDF in Zotero').length, 1);
    assert.equal(buttons.get('Recheck keywords').length, 2, 'An unscoped row cannot start a bulk keyword recheck');
    assert.equal(buttons.get('Open linked reference in Zotero').length, 1);
    assert.equal(buttons.get('Open reference already linked to reMarkable').length, 1);
    await buttons.get('Open PDF in Zotero')[0].click();
    await buttons.get('Show affected attachment in Zotero').at(-1).click();
    assert.deepEqual(JSON.parse(JSON.stringify(calls)), [
      {target: 'pdf', keys: ['ATTACH23'], expected_parent: 'PARENT23'},
      {target: 'attachment', keys: ['ATTACH25'], expected_parent: 'PARENT25'}]);
  }
  for (const [evidence, label, canCheck] of [
    [{keyword_review: {status: 'found'}}, 'Stated keywords found in the linked PDFs.', false],
    [{keyword_has_zotero_tags: true}, 'This paper already has Zotero tags.', true],
    [{keyword_review: {status: 'tagged_manually'}}, 'This paper already has Zotero tags.', true],
    [{keyword_parent_unavailable: true}, 'The Zotero reference is unavailable for keyword checking.', true],
    [{}, 'Keywords need text recognition or manual checking.', true],
    [{unavailable: true}, 'Keywords need text recognition or manual checking.', false]
  ]) {
    const {ui, buttons, node} = viewFixture();
    const root = node();
    ui.renderLinkedPaper(root, {cloud: 'paper', parent: 'PARENT23', name: 'Paper',
      keyword_check: {status: 'needs_text_recognition'}, ...evidence});
    assert.ok(root.children[0].children.some(child => child.textContent === label));
    assert.equal(buttons.has('Recheck keywords'), canCheck);
    if (canCheck) {
      const calls = [];
      ui.action = async (...args) => calls.push(args);
      await buttons.get('Recheck keywords')[0].click();
      assert.deepEqual(JSON.parse(JSON.stringify(calls)), [['recheck_keyword_warnings', {cloud: 'paper'}]]);
    }
  }
  {
    const {ui, fields, buttons, node} = viewFixture();
    ui.latest.mappings = {Z: {cloud: 'T', root: true}};
    ui.latest.deletions = [{id: 'deletion', payload: {title: 'Paper', inventory: [], restore_parent: 'removed-folder'}}];
    ui.renderReview(node()); fields.get('Restore to reMarkable folder').value = 'T';
    let payload; ui.confirmSyncDecision = async job => { payload = job.params; };
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
    const reference = {key: 'REF', isRegularItem: () => true, loadAllData: async () => {},
      getTags: () => [{tag: 'Existing'}], getField: () => 'Full citation'};
    const item = {key: 'ORIGINAL', parentID: 2, isPDFAttachment: () => true};
    const {ui} = fixture({Zotero: {Items: {getAsync: async id => id === 2 ? reference : item},
      getMainWindow: () => ({crypto: {}})}, notablePDFDigest: async () => 'same-original'});
    ui.latest.links = [{cloud: 'linked', view_key: 'ANNOTATED', parent: 'OTHER', baseline: 'same-original'}];
    ui.busy = true; ui.operation = {method: 'sync'};
    ui.open = () => {};
    ui.rpc = () => { throw Error('Reader must not wait for sync'); };
    await ui.openPageTags({_internalReader: {_state: {primaryViewStats: {pageIndex: 2}}}, itemID: 1});
    assert.equal(ui.tagEditor.loading, false);
    assert.equal(ui.tagEditor.paperOnly, true);
    assert.equal(ui.tagEditor.linkedCopy.view_key, 'ANNOTATED');
    assert.equal(ui.linkedOriginalCases().length, 1, 'Approved papers can appear without an Inbox review');
    ui.latest.links[0].parent = 'REF';
    assert.equal(ui.linkedOriginalCases().length, 0, 'A consolidated reference is no longer a duplicate case');
  }
  {
    const {ui, context} = fixture({Zotero: {Items: {getAsync: async () => ({key: 'PDF', isPDFAttachment: () => true})}}});
    const order = [], pageContext = {key: 'PDF', page: 0};
    ui.open = () => {
      assert.equal(ui.tagEditor.loading, true, 'Open before the worker replies');
      assert.equal(ui.requestedTab, 'tags');
      order.push('open');
    };
    ui.rpc = async (method, params) => {
      assert.equal(method, 'tag_context');
      assert.equal(params.key, pageContext.key); assert.equal(params.page, pageContext.page);
      order.push('context');
      return pageContext;
    };
    await ui.openPageTags({_internalReader: {_state: {primaryViewStats: {pageIndex: 0}}}, itemID: 1});
    assert.deepEqual(order, ['open', 'context']);
    assert.equal(ui.tagEditor.loading, false);
  }
  {
    const {ui, fields, buttons, node, writes} = viewFixture({Services: {uuid: {generateUUID: () => 'edit-1'}},
      Zotero: {Items: {getAsync: async id => id === 1
        ? {key: 'PDF', parentID: 2, isPDFAttachment: () => true}
        : {key: 'REF', getField: () => 'Paper', getTags: () => [{tag: 'Existing'}]}},
      Prefs: {set() {}}}});
    ui.latest.links = [{cloud: 'R', view_key: 'PDF', parent: 'REF', page_map: ['PAGE'], page_map_digest: 'digest',
      page_tags: {PAGE: ['Existing page']}}];
    ui.open = () => {};
    ui.busy = true;
    ui.operation = {method: 'sync'};
    ui.rpc = () => { throw Error('Opening and saving must never wait for the worker'); };
    await ui.openPageTags({_internalReader: {_state: {primaryViewStats: {pageIndex: 0}}}, itemID: 1});
    assert.equal(ui.tagEditor.page_id, 'PAGE');
    ui.renderTagEditor(node());
    fields.get('Add page tag').value = 'New page';
    await buttons.get('Save tags')[0].click();
    assert.deepEqual(writes[0][0].page, {before: ['Existing page'], after: ['Existing page', 'New page']});
    assert.equal(writes[0][0].pdf_digest, 'digest');
    const restarted = fixture().ui;
    restarted.tagEdits = writes[0];
    restarted.action = async (method, params) => {
      assert.equal(method, 'apply_tag_edit');
      assert.equal(params.edit_id, 'edit-1');
      return {tag_edit_applied: 'edit-1'};
    };
    await restarted.drainTagEdits();
    assert.equal(restarted.tagEdits.length, 0);
  }
  {
    const {ui} = fixture();
    ui.busy = true; ui.operation = {method: 'sync'};
    ui.latest.deletions = [{id: 'case', payload: {current: 'first'}}];
    const decision = ui.action('delete_decision', {identity: 'case', choice: 'mirror'});
    const error = assert.rejects(decision, /needs review/);
    const inbox = ui.action('check_inbox');
    assert.equal(ui.afterSync.length, 2);
    const calls = [];
    ui.action = async method => calls.push(method);
    ui.latest.deletions[0].payload.current = 'changed';
    ui.busy = false;
    await ui.drainAfterSync();
    await Promise.all([error, inbox]);
    assert.deepEqual(calls, ['check_inbox']);
  }
  {
    const {ui} = fixture();
    ui.latest.deletions = [{id: 'tablet', payload: {title: 'Tablet paper'}}];
    ui.latest.local_deletions = [{id: 'zotero', payload: {title: 'Zotero paper'}}];
    const jobs = [['delete_decision', 'tablet', 'keep_zotero'], ['local_delete_decision', 'zotero', 'trash_tablet']]
      .map(([method, identity, choice]) => {
        const params = {identity, choice};
        return {key: identity, method, params, connection: ui.filingConnection(),
          evidence: JSON.stringify(ui.syncDecisionEvidence(method, params))};
      });
    ui.busy = true;
    await ui.changeSyncDecisions(() => jobs);
    await ui.drainSyncDecisions();
    assert.equal(ui.syncDecisions.length, 2, 'Both choices wait during the transfer');
    const restarted = fixture().ui;
    restarted.latest = ui.latest; restarted.syncDecisions = ui.syncDecisions;
    const calls = [];
    restarted.action = async (method, params) => { calls.push([method, params.identity]); return {}; };
    restarted.latest.deletions[0].payload.title = 'Changed since review';
    await restarted.drainSyncDecisions();
    assert.deepEqual(calls, [['local_delete_decision', 'zotero']], 'A changed first case must not block the second');
    assert.match(restarted.syncDecisions[0].error, /changed while/);
  }
  {
    const {ui} = fixture();
    let failSync;
    const calls = [];
    ui.rpc = method => {
      calls.push(method);
      if (method === 'sync') return new Promise((_resolve, reject) => { failSync = reject; });
      assert.equal(method, 'recheck_keyword_warnings', 'Only the queued local keyword check survives a sync error');
      return {reviews: [], keyword_recheck: {checked: 1, resolved: 1, unavailable: 0}};
    };
    const sync = assert.rejects(ui.action('sync'), /worker stopped/);
    const pending = assert.rejects(ui.action('dismiss_review', {identity: 'ref', digest: 'original'}), /could be checked/);
    const keywordRecheck = ui.action('recheck_keyword_warnings');
    assert.deepEqual(calls, ['sync'], 'The recheck waits for the sync worker');
    failSync(Error('worker stopped'));
    await Promise.all([sync, pending, keywordRecheck]);
    assert.deepEqual(calls, ['sync', 'recheck_keyword_warnings']);
    assert.equal(ui.afterSync.length, 0);
  }
  {
    const {ui} = fixture();
    let finishSync, finishPause;
    const calls = [];
    ui.rpc = method => {
      calls.push(method);
      if (method === 'sync') return new Promise(resolve => { finishSync = resolve; });
      if (method === 'pause_sync') return new Promise(resolve => { finishPause = resolve; });
      assert.equal(method, 'recheck_keyword_warnings');
      return {...ui.latest, keyword_recheck: {checked: 1, resolved: 1, unavailable: 0}};
    };
    const sync = ui.action('sync');
    const recheck = ui.action('recheck_keyword_warnings');
    const result = recheck.catch(error => error);
    const pending = assert.rejects(ui.action('dismiss_review', {identity: 'ref'}), /Sync was paused/);
    const pause = ui.pauseSync();
    while (!finishPause) await tick();
    finishSync(ui.latest);
    await sync;
    assert.deepEqual(calls, ['sync', 'pause_sync'], 'The recheck waits for the pause to finish');
    finishPause(ui.latest);
    await Promise.all([pause, pending]);
    assert.equal((await result).keyword_recheck?.resolved, 1, 'Pausing transfers retains a requested local recheck');
    assert.deepEqual(calls, ['sync', 'pause_sync', 'recheck_keyword_warnings']);
    assert.equal(ui.latest.config.sync_paused, true);
    assert.equal(ui.afterSync.length, 0);
  }
  {
    const {ui} = fixture();
    const warning = {kind: 'keywords', cloud: 'paper', reason: 'No readable text'};
    const other = {kind: 'conflict', cloud: 'paper', reason: 'Independent warning'};
    ui.latest.links = [{cloud: 'paper', keyword_snapshot: 'same-revision'}];
    ui.latest.status = {result: {review: [warning, other]}};
    const original = JSON.parse(JSON.stringify(ui.latest));
    ui.badge = () => ui.applyKeywordUpdates();
    ui.helperLaunch = {};
    let finishSync;
    ui.rpc = method => {
      assert.equal(method, 'sync');
      return new Promise(resolve => { finishSync = resolve; });
    };
    ui.readKeywordReply = async params => {
      assert.equal(params.cloud, 'paper');
      return {keyword_recheck: {checked: 1, resolved: 1, unavailable: 0}, keyword_updates: [
        {cloud: 'paper', evidence: 'same-revision', checked_at: '10', fields: {keyword_has_zotero_tags: true}, warning: null},
      ]};
    };
    const sync = ui.action('sync');
    const check = await ui.action('recheck_keyword_warnings', {cloud: 'paper'});
    assert.equal(check.keyword_recheck.resolved, 1);
    assert.equal(ui.operation.method, 'sync', 'The local recheck finishes before the transfer');
    assert.equal(ui.afterSync.length, 0);
    assert.deepEqual(JSON.parse(JSON.stringify(ui.latest.status.result.review)), [other]);
    finishSync(original);
    await sync;
    assert.equal(ui.latest.links[0].keyword_has_zotero_tags, true, 'A late status response cannot bring the old warning back');
    ui.latest.links[0] = {cloud: 'paper', keyword_snapshot: 'new-revision'};
    ui.badge();
    assert.equal(ui.latest.links[0].keyword_has_zotero_tags, undefined, 'A new link revision cannot inherit stale keyword evidence');
  }
  {
    const {ui} = fixture();
    const document = {getElementById: () => null};
    const first = {document, closed: true}, second = {document, closed: false};
    ui.windows = new Set([first, second]);
    ui.view = {closed: false, close() { this.closed = true; }};
    ui.detach(first);
    assert.equal(ui.view.closed, false, 'An open library window can still host the workspace');
    ui.detach(second);
    assert.equal(ui.view.closed, true, 'Closing the last library window closes the workspace');
  }
  {
    let registration, refreshes = 0;
    const {context} = fixture({Zotero: {Libraries: {userLibraryID: 1}, ItemTreeManager: {
      registerColumn: option => { registration = option; return 'custom-column'; },
      refreshColumns: () => refreshes++, unregisterColumn: key => assert.equal(key, 'custom-column'),
    }}});
    vm.runInContext(fs.readFileSync('zotero-support/sync-column.js', 'utf8'), context);
    const column = context.NotableSyncColumn;
    column.register('plugin');
    const links = [
      {parent: 'MATCH', cloud: 'received', view_key: 'PDF', revision: '1'},
      {parent: 'WAIT', cloud: 'waiting'},
      {parent: 'PAUSE', cloud: 'paused', paused: true},
      {parent: 'MATCH', cloud: 'conflicted', view_key: 'PDF2', revision: '1'},
    ];
    const status = {links, reviews: [{kind: 'conflict', payload: {type: 'paper', cloud: 'conflicted'}}]};
    column.update(status);
    column.update(status);
    const value = (key, libraryID = 1, isRegularItem = () => true) => registration.dataProvider({key, libraryID, isRegularItem});
    assert.equal(value('MATCH'), 'attention');
    assert.equal(value('WAIT'), 'pending');
    assert.equal(value('PAUSE'), 'paused');
    assert.equal(value('MATCH', 2), '');
    assert.equal(value('MATCH', 1, () => false), '');
    assert.equal(refreshes, 1, 'Unchanged state does not refresh the native tree');
    column.update({links});
    assert.equal(value('MATCH'), 'synced');
    column.unregister();
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
