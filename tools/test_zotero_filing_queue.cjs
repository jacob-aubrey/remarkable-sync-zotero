// Run the real controller with delayed transport responses, no library writes.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('zotero-support/ui.js', 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
function fixture() {
  const writes = [];
  const context = vm.createContext({setTimeout, clearTimeout, IOUtils: {writeUTF8: async (path, text) => writes.push(JSON.parse(text))},
    PathUtils: {join: (...parts) => parts.join('/')}});
  vm.runInContext(source, context);
  const ui = context.NotableUI;
  ui.statePath = 'synthetic'; ui.stopped = false;
  ui.refreshCollectionNames = () => {};
  ui.latest = {config: {zotero_root: 'Z', tablet_root: 'C'}, status: {}, reviews: [{id: 'one'}, {id: 'two'}]};
  ui.disable = ui.updateActivity = ui.schedule = ui.badge = ui.message = () => {};
  return {ui, writes};
}
function reviewFixture() {
  const {ui} = fixture(); const fields = new Map(), buttons = new Map();
  const node = (value = '') => ({value, dataset: {}, selectedOptions: [], listeners: {},
    addEventListener(name, callback) { this.listeners[name] = callback; }, setAttribute() {}, replaceChildren() {}});
  ui.el = () => node();
  ui.field = (label, parent, value) => { const input = node(value); input.notableRefreshValue = () => {}; fields.set(label, input); return input; };
  ui.select = (label, parent, options, selected = '') => {
    const input = ui.field(label, parent, selected);
    input.selectedOptions = options.filter(([key]) => Array.isArray(selected) ? selected.includes(key) : key === selected).map(([value]) => ({value}));
    return input;
  };
  // This harness tests filing/draft behavior; the real adapter has its own folder UI harness.
  ui.folderSelect = (...args) => ui.select(...args);
  // Real wrapping adapters are covered by the paper-picker integration harness.
  ui.paperSelect = (...args) => ui.select(...args);
  ui.destinationSelect = (label, parent, selected) => ui.field(label, parent, selected);
  ui.button = (label, parent, click) => { const input = node(); input.click = click; buttons.set(label, input); return input; };
  ui.supportingInformationPicker = () => ({panel: node(), query: node(), select: node()});
  ui.reviewOpenButton = ui.paperTagPanel = () => {};
  ui.latest = {...ui.latest, types: {journal_article: 'Journal article'}, reviews: [{id: 'one', digest: 'same', kind: 'cloud', status: 'pending',
    payload: {guidance_version: 1, guidance_state: 'ready', findings: [], path: 'paper.pdf', title: 'Paper', filename: 'Original.pdf', document_type: 'journal_article', matches: [{key: 'existing', title: 'Existing paper'}]}}]};
  return {ui, fields, buttons};
}
async function waitFor(check) {
  for (let n = 0; n < 100; n++) { if (check()) return; await tick(); }
  throw Error('Queue did not settle');
}
async function main() {
  {
    const {ui, writes} = fixture(); const calls = []; let release; let active = 0, maxActive = 0;
    ui.rpc = async (method, params) => {
      calls.push({method, params}); maxActive = Math.max(maxActive, ++active);
      if (method === 'approve_routine') await new Promise(resolve => { release = resolve; });
      if (method === 'approve') ui.latest.reviews = ui.latest.reviews.filter(r => r.id !== params.identity);
      active--; return ui.latest;
    };
    const batch = ui.action('approve_routine', {});
    const params = {identity: 'two', filename: 'Chosen.pdf', digest: 'original'};
    await ui.queueFiling('approve', params, 'Paper two');
    params.filename = 'Changed after clicking.pdf';
    assert.equal(calls.length, 1, 'Queued approval must wait for the current batch');
    assert.equal(writes.at(-1).jobs[0].params.filename, 'Chosen.pdf');
    await ui.queueFiling('approve', {identity: 'two'}, 'Duplicate click');
    assert.equal(ui.filingQueue.length, 1);
    release(); await batch;
    await waitFor(() => !ui.drainingFilings && !ui.filingQueue.length);
    assert.equal(maxActive, 1);
    assert.deepEqual(calls.map(c => c.method), ['approve_routine', 'approve'], 'Filing must never start a whole-library sync');
    assert.equal(calls[1].params.filename, 'Chosen.pdf');
    assert.equal(writes.at(-1).jobs.length, 0);
  }
  {
    const {ui} = fixture(); const calls = [];
    ui.rpc = async method => { calls.push(method); throw Error('The file changed'); };
    await ui.queueFiling('approve', {identity: 'one'}, 'Paper one');
    assert.equal(ui.filingQueue.length, 1); assert.equal(ui.filingQueuePaused, true);
    assert.match(ui.queueError, /file changed/);
    await ui.drainFilings(); assert.equal(calls.length, 1, 'Failure must not retry automatically');
  }
  {
    const {ui} = fixture(); ui.busy = true;
    await ui.queueFiling('approve', {identity: 'one'}, 'Paper one');
    ui.busy = false; ui.latest.config.tablet_root = 'different';
    ui.rpc = async () => { throw Error('Wrong connection reached the worker'); };
    await ui.drainFilings(); assert.match(ui.queueError, /different folder connection/);
    assert.equal(ui.filingQueue.length, 1);
  }
  {
    const {ui} = fixture(); let releaseSync;
    const calls = [], cancellations = [];
    ui.process = {stdin: {write: async line => cancellations.push(JSON.parse(line))}};
    ui.rpc = async (method, params) => {
      calls.push(method);
      if (method === 'approve') ui.latest.reviews = ui.latest.reviews.filter(r => r.id !== params.identity);
      if (method === 'sync' && !releaseSync) {
        await new Promise(resolve => { releaseSync = resolve; });
        ui.latest.status = {state: 'paused'};
      }
      return ui.latest;
    };
    const sync = ui.action('sync');
    await waitFor(() => !!releaseSync);
    await ui.queueFiling('approve', {identity: 'one'}, 'One');
    await ui.queueFiling('approve', {identity: 'two'}, 'Two');
    assert.equal(ui.filingQueue.length, 2, 'Both decisions save while the single writer is syncing');
    assert.equal(cancellations.length, 1, 'Several approvals request only one safe transient cancellation');
    assert.equal(cancellations[0].method, 'cancel');
    assert.notEqual(ui.latest.config.sync_paused, true, 'Yielding must preserve the user\'s sync preference');
    releaseSync(); await sync;
    await waitFor(() => !ui.drainingFilings && !ui.filingQueue.length);
    assert.deepEqual(calls, ['sync', 'approve', 'approve'], 'Queued filings must not launch more syncs');
  }
  {
    const {ui, writes} = fixture(); const calls = [];
    ui.rpc = async (method, params) => {
      calls.push(params.entries.map(row => row.id));
      ui.latest.reviews = [{id: 'two'}];
      ui.latest.status = {state: calls.length === 1 ? 'paused' : 'ready'};
      if (calls.length > 1) ui.latest.reviews = [];
      return ui.latest;
    };
    await ui.queueFiling('approve_routine', {entries: [{id: 'one'}, {id: 'two'}]}, 'Two papers');
    assert.equal(ui.filingQueuePaused, true);
    assert.deepEqual(writes.at(-1).jobs[0].params.entries.map(row => row.id), ['two'], 'Keep unfinished approvals after Stop');
    ui.filingQueuePaused = false;
    await ui.drainFilings();
    assert.deepEqual(calls.map(ids => Array.from(ids)), [['one', 'two'], ['two']], 'Resume must not replay completed writes');
  }
  {
    const {ui} = fixture(); let calls = 0;
    ui.saveFilingQueue = async () => { throw Error('Disk is full'); };
    ui.rpc = async () => { calls++; };
    await assert.rejects(ui.queueFiling('approve', {identity: 'one'}, 'One'), /Disk is full/);
    assert.equal(calls, 0, 'Unsaved decisions must never reach the writer');
    assert.equal(ui.filingQueue.length, 0, 'A failed save must leave the paper editable in Review');
  }
  {
    const {ui} = fixture(); let releaseSave, releaseSync;
    const calls = [];
    ui.queueSave = new Promise(resolve => { releaseSave = resolve; });
    ui.filingQueue = [{id: 'saved', method: 'approve', params: {identity: 'one'}, connection: ui.filingConnection()}];
    ui.rpc = async (method, params) => {
      calls.push(method);
      if (method === 'sync') await new Promise(resolve => { releaseSync = resolve; });
      if (method === 'approve') ui.latest.reviews = ui.latest.reviews.filter(row => row.id !== params.identity);
      return ui.latest;
    };
    const drain = ui.drainFilings();
    const sync = ui.action('sync');
    await waitFor(() => !!releaseSync);
    releaseSave(); await drain;
    assert.equal(ui.filingQueue.length, 1, 'A sync racing queue persistence must not silently drop a saved decision');
    releaseSync(); await sync;
    await waitFor(() => !ui.drainingFilings && !ui.filingQueue.length);
    assert.deepEqual(calls, ['sync', 'approve']);
  }
  {
    const {ui} = fixture();
    ui.reviewDrafts.set('one', {title: 'Edited title'}); ui.batchCollection = 'old';
    ui.rpc = async () => ({...ui.latest, config: {...ui.latest.config, ocr: false}});
    await ui.action('configure');
    assert.equal(ui.reviewDrafts.size, 1, 'Ordinary settings changes retain review edits');
    ui.rpc = async () => ({...ui.latest, config: {...ui.latest.config, tablet_root: 'new'}});
    await ui.action('configure');
    assert.equal(ui.reviewDrafts.size, 0, 'A new connection must not inherit old review choices');
    assert.equal(ui.batchCollection, null);
  }
  {
    const {ui} = fixture();
    const reviews = ui.latest.reviews;
    ui.latest.filing_summary = {approved_waiting_sync: 10, linked: 0, pdfs_received: 0};
    ui.latest.links = [{cloud: 'one', name: 'Old', paused: true, history: [{action: 'linked'}]}];
    ui.receiveProgress({message: 'Received PDF', links: [{cloud: 'one', name: 'New', revision: 'received'}, {cloud: 'two', name: 'Second'}]});
    assert.equal(ui.latest.links.length, 2);
    assert.equal(ui.latest.links[0].paused, true, 'Progress snapshots retain link controls and history');
    assert.equal(ui.latest.links[0].history[0].action, 'linked');
    assert.equal(ui.latest.links[0].revision, 'received');
    assert.equal(ui.latest.reviews, reviews, 'Live link updates must not replace review edits');
    assert.equal(ui.latest.filing_summary.linked, 2);
    assert.equal(ui.latest.filing_summary.pdfs_received, 1);
    assert.match(ui.paperStatusMessage(), /10 approved, waiting for sync.*2 linked/);
    ui.operation = {method: 'sync'};
    assert.match(ui.paperStatusMessage(), /2 linked so far/);
    assert.doesNotMatch(ui.paperStatusMessage(), /10 approved/, 'A running sync must not present an outdated awaiting-sync count');
  }
  {
    const {ui} = fixture(); ui.busy = true;
    await ui.queueFiling('approve', {identity: 'one'}, 'One');
    const job = ui.filingQueue[0]; ui.activeFiling = job.id;
    await assert.rejects(ui.removeFiling(job), /already being filed/);
    ui.activeFiling = null;
    ui.saveFilingQueue = async () => { throw Error('Disk full'); };
    await assert.rejects(ui.removeFiling(job), /Disk full/);
    assert.equal(ui.filingQueue[0], job, 'A failed removal must retain the saved approval');
    assert.equal(ui.filingQueuePaused, true, 'A failed queue write must pause further writes');
  }
  {
    const {ui, fields, buttons} = reviewFixture();
    ui.renderReview({});
    assert.equal(fields.get('PDF already found in Zotero').value, 'existing', 'Reuse the single verified existing PDF by default');
    const match = fields.get('PDF already found in Zotero');
    match.value = ''; match.listeners.change();
    ui.rpc = async () => ({filename: 'Preview.pdf'});
    await buttons.get('Suggest filename').click();
    ui.renderReview({});
    assert.equal(fields.get('Proposed filename').value, 'Preview.pdf', 'A generated filename survives tab switching and queue rerenders');
    assert.equal(fields.get('PDF already found in Zotero').value, '', 'An explicit Keep separate choice survives rerenders');
    ui.rpc = async (method, params) => {
      assert.equal(method, 'preview'); assert.equal(params.document_type, 'book');
      return {filename: 'Paper_Book.pdf'};
    };
    const type = fields.get('Document type'); type.value = 'book'; type.listeners.change();
    await tick();
    assert.equal(fields.get('Proposed filename').value, 'Paper_Book.pdf', 'Changing the document type refreshes a generated filename');
    const venue = fields.get('Journal, publisher, or institution'); venue.value = 'Reviewed venue'; venue.listeners.input();
    const tag = fields.get('Add paper tag'); tag.value = 'Reviewed tag'; tag.listeners.input();
    let approved;
    ui.queueFiling = async (_method, params) => {approved = params;};
    await buttons.get('Approve & file').click();
    assert.deepEqual(JSON.parse(JSON.stringify(approved.citation)), {venue: 'Reviewed venue'});
    assert.deepEqual(Array.from(approved.tags), ['Reviewed tag']);
    ui.latest.reviews[0].payload.routine = true;
    ui.renderReview({});
    const destination = fields.get('Linked folders for this batch (Zotero → reMarkable)');
    destination.value = 'chosen-subfolder'; destination.listeners.change();
    ui.renderReview({});
    assert.equal(fields.get('Linked folders for this batch (Zotero → reMarkable)').value, 'chosen-subfolder', 'Batch destination survives rerenders');
  }
  console.log('PASS: saved choices, no implicit sync, serialized jobs, interrupted batch resume, save failures, connection isolation, and live link snapshots');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
