// Upgrade the saved Review data on opening; never require a hidden manual step.
const assert = require('node:assert/strict');
const {fixture, viewFixture} = require('./test_zotero_ui_recovery.cjs');
const plain = value => JSON.parse(JSON.stringify(value));
const oldPaper = (id = 'legacy') => ({id, kind: 'zotero', status: 'pending', identity: 'SURCE234', digest: 'original-bytes',
  payload: {title: 'Existing paper', filename: 'Author_UnknownJournal_UnknownYear.pdf', parent: 'PARENT23', attachment: 'SURCE234',
    issues: ['Identical content belongs to several Zotero attachments.'], review_token: 'old-evidence', routine: false}});
const upgraded = row => ({...plain(row), payload: {...plain(row.payload), guidance_version: 1, guidance_state: 'ready',
  findings: [{code: 'repeated_pdf_content', domain: 'library', summary: 'Identical PDFs occur in several attachments',
    explanation: 'Inspect the exact copies.', next_step: 'Compare the references.', references: [{key: 'PARENT23', title: 'Existing paper'}, {key: 'THER2345', title: 'Other reference'}],
    acknowledgement: {eligible: true, acknowledged: false, token: 'exact-evidence'}}], review_token: 'current-evidence'}});
function text(node) { return [node.textContent, ...node.children.map(text)].join(' '); }
// Hold the real startup status request while exercising the real tab buttons.
// No worker, Zotero profile, or native library is involved.
function startupFixture({connected = true, requestedTab = null} = {}) {
  const value = viewFixture(), {ui, node} = value, requests = [], renders = [];
  ui.latest.config.tablet_root = connected ? 'T' : null;
  ui.requestedTab = requestedTab;
  ui.yieldBackground = async () => {};
  for (const name of ['hideButtonHelp', 'restoreReviewContext', 'restoreReviewList', 'restoreFocus', 'renderFilingQueue',
    'renderSetup', 'renderImport', 'renderLinks', 'renderTags', 'renderReview']) ui[name] = () => {};
  ui.captureFocus = ui.captureReviewContext = () => null;
  const nativeRender = ui.render.bind(ui);
  ui.render = () => { renders.push({window: ui.view, tab: ui.tab, selected: ui.selected}); nativeRender(); };
  ui.rpc = async method => {
    assert.equal(method, 'status', 'This navigation check must not sync, approve, or write queued filings');
    return new Promise(resolve => requests.push(resolve));
  };
  const window = () => {
    const app = node('div'), handlers = {};
    const result = {closed: false, document: {querySelector: () => null, getElementById: id => id === 'app' ? app : null},
      addEventListener(event, callback) { handlers[event] = callback; },
      close() { result.closed = true; handlers.unload?.(); }};
    return result;
  };
  const clickTab = label => {
    const button = value.buttons.get(label)?.at(-1);
    assert.ok(button, 'The rendered tab button must exist: ' + label);
    button.click();
  };
  const release = (index = 0, connectedAfter = connected) => requests[index]({...plain(ui.latest),
    config: {...plain(ui.latest.config), tablet_root: connectedAfter ? 'T' : null}});
  return {...value, requests, renders, window, clickTab, release};
}
async function startupNavigationChecks() {
  for (const [tab, label] of [['inbox', 'Inbox (0)'], ['library', 'Library Issues (0)'], ['sync-review', 'Sync Conflicts (0)'],
    ['links', 'Linked papers'], ['tags', 'Tags'], ['import', 'Import library'], ['setup', 'Setup']]) {
    const f = startupFixture(), {ui} = f, view = f.window();
    ui.selected = 'initial-paper'; ui.queueSelections = {[tab]: 'chosen-paper'};
    ui.reviewDrafts.set('chosen-paper', {filename: 'Keep this unsaved name.pdf'});
    const drafts = plain([...ui.reviewDrafts]), mounting = ui.mount(view);
    try {
      assert.equal(f.requests.length, 1, 'The initial status request is still pending');
      f.clickTab(label);
      assert.equal(ui.tab, tab);
      const selected = ui.selected, navigation = ui.tabNavigationSerial;
      f.release(); await mounting;
      assert.equal(ui.tab, tab, 'Finishing startup must preserve the clicked ' + label + ' tab');
      assert.equal(ui.selected, selected, 'Finishing startup must preserve the selected paper');
      assert.equal(ui.tabNavigationSerial, navigation, 'Refreshing information must not count as user navigation');
      assert.deepEqual(plain([...ui.reviewDrafts]), drafts, 'Finishing startup must keep unsaved filing choices');
    } finally { clearInterval(ui.activityTimer); }
  }
  for (const [before, after, expected] of [[false, true, 'inbox'], [true, false, 'setup'], [false, false, 'setup']]) {
    const f = startupFixture({connected: before}), {ui} = f, mounting = ui.mount(f.window());
    try {
      assert.equal(ui.tab, before ? 'inbox' : 'setup');
      f.release(0, after); await mounting;
      assert.equal(ui.tab, expected, 'Without user navigation, fresh connection information chooses the correct initial section');
    } finally { clearInterval(ui.activityTimer); }
  }
  {
    const f = startupFixture({connected: false}), {ui} = f, mounting = ui.mount(f.window());
    try {
      assert.equal(ui.tab, 'setup'); f.clickTab('Setup');
      f.release(0, true); await mounting;
      assert.equal(ui.tab, 'setup', 'Explicitly clicking the already-selected Setup tab must also beat the delayed Inbox default');
    } finally { clearInterval(ui.activityTimer); }
  }
  for (const timing of ['before-open', 'while-loading']) {
    const f = startupFixture({requestedTab: timing === 'before-open' ? 'tags' : null}), {ui} = f;
    const mounting = ui.mount(f.window());
    try {
      if (timing === 'while-loading') ui.requestedTab = 'tags';
      f.release(); await mounting;
      assert.equal(ui.tab, 'tags', 'An explicit page-tag request must open Tags when no user navigation supersedes it');
      assert.equal(ui.requestedTab, null, 'The one-time tab request must be consumed');
    } finally { clearInterval(ui.activityTimer); }
  }
  {
    const f = startupFixture({requestedTab: 'tags'}), {ui} = f, mounting = ui.mount(f.window());
    try {
      f.clickTab('Library Issues (0)'); f.release(); await mounting;
      assert.equal(ui.tab, 'library', 'A later user click must supersede the initial requested Tags tab');
      assert.equal(ui.requestedTab, null, 'The old requested tab must not redirect a future opening');
    } finally { clearInterval(ui.activityTimer); }
  }
  {
    const f = startupFixture(), {ui} = f, oldView = f.window(), newView = f.window();
    const oldMount = ui.mount(oldView); oldView.close();
    const newMount = ui.mount(newView);
    try {
      f.clickTab('Sync Conflicts (0)'); f.release(1); await newMount;
      const completedRenders = f.renders.length;
      f.release(0); await oldMount;
      assert.equal(ui.view, newView);
      assert.equal(ui.tab, 'sync-review', 'An older closed window must not redirect the newly opened window');
      assert.equal(f.renders.length, completedRenders, 'The old mount must not render into the newly opened window');
    } finally { clearInterval(ui.activityTimer); }
  }
}
async function main() {
  await startupNavigationChecks();
  {
    const {ui} = fixture(); const old = oldPaper(); ui.latest.reviews = [old];
    ui.filingQueue = [{id: 'already-queued', method: 'approve', params: {identity: 'another'}}];
    ui.reviewDrafts.set(old.id, {digest: old.digest, filename: 'My unsaved name.pdf'}); ui.selected = old.id;
    let drains = 0; ui.drainFilings = async () => drains++;
    const calls = []; ui.rpc = async (method) => { calls.push(method); return {...ui.latest,
      reviews: [upgraded(old)], review_guidance: {version: 1, needs_upgrade: [], ready: 1, repair: 0}}; };
    const queue = plain(ui.filingQueue), drafts = plain([...ui.reviewDrafts]);
    await ui.prepareReviewGuidance(); await ui.prepareReviewGuidance();
    assert.deepEqual(calls, ['recheck_review'], 'Old reviews upgrade once; current saved reviews do not recheck on every open');
    assert.equal(ui.reviewNeedsUpgrade(ui.latest.reviews[0]), false);
    assert.deepEqual(plain(ui.filingQueue), queue); assert.deepEqual(plain([...ui.reviewDrafts]), drafts);
    assert.equal(ui.selected, old.id); assert.equal(drains, 0);
  }
  {
    const {ui} = fixture(); const old = oldPaper(); const calls = [];
    ui.render = () => {}; ui.yieldBackground = async () => {};
    const current = {...ui.latest, reviews: [old], review_guidance: {version: 1, needs_upgrade: [old.id]}};
    ui.rpc = async method => { calls.push(method); return method === 'status' ? current : {...current, reviews: [upgraded(old)],
      review_guidance: {version: 1, needs_upgrade: []}}; };
    const window = {closed: false, addEventListener() {}, document: {}};
    await ui.mount(window); clearInterval(ui.activityTimer);
    assert.deepEqual(calls, ['status', 'recheck_review', 'scan_inbox'], 'Opening a workspace performs the missing local migration automatically');
    assert.equal(ui.reviewNeedsUpgrade(ui.latest.reviews[0]), false);
  }
  for (const state of ['ready', 'repair']) {
    const {ui} = fixture(); ui.latest.reviews = [{...oldPaper(), payload: {guidance_version: 1, guidance_state: state, findings: []}}];
    ui.rpc = async () => assert.fail('A current zero-finding/repair row was upgraded again');
    await ui.prepareReviewGuidance(); assert.equal(ui.reviewUpgrade, null);
  }
  {
    const {ui} = fixture(); const row = oldPaper(); row.payload.guidance_version = 1; row.payload.findings = [];
    assert.equal(ui.reviewNeedsUpgrade(row), true, 'A partial marker without ready/repair is still incomplete');
  }
  for (const kind of ['applying', 'approved', 'ignored', 'conflict']) {
    const {ui} = fixture(); const row = oldPaper(); if (kind === 'conflict') row.kind = kind; else row.status = kind;
    ui.latest.reviews = [row]; ui.rpc = async () => assert.fail('Recovery/history/conflict must not be migrated');
    await ui.prepareReviewGuidance(); assert.equal(ui.reviewNeedsUpgrade(row), false);
  }
  {
    const {ui} = fixture(); const row = oldPaper(); ui.latest.reviews = [row]; let calls = 0;
    ui.rpc = async () => { calls++; throw Error('Zotero is temporarily unavailable'); };
    await ui.prepareReviewGuidance(); assert.equal(ui.reviewUpgrade.state, 'failed');
    assert.equal(ui.reviewNeedsUpgrade(row), true); assert.match(ui.reviewFreshnessMessage(), /Retry saved review update/);
    await ui.prepareReviewGuidance(); assert.equal(calls, 1, 'A failed migration does not loop');
    ui.rpc = async method => { calls++; assert.equal(method, 'recheck_review'); return {...ui.latest, reviews: [upgraded(row)]}; };
    await ui.prepareReviewGuidance({retry: true}); assert.equal(calls, 2); assert.equal(ui.reviewUpgrade, null);
  }
  {
    const {ui} = fixture(); const row = oldPaper(); ui.latest.reviews = [row]; let release;
    ui.rpc = async () => { await new Promise(resolve => { release = resolve; }); return {...ui.latest, reviews: [upgraded(row)]}; };
    const one = ui.prepareReviewGuidance(), two = ui.prepareReviewGuidance();
    release(); await Promise.all([one, two]); assert.equal(ui.reviewUpgrade, null, 'Concurrent opens share one migration');
  }
  {
    const {ui} = fixture(); const row = oldPaper(); ui.latest.reviews = [row]; ui.busy = true;
    await ui.prepareReviewGuidance(); assert.equal(ui.reviewUpgrade.state, 'waiting'); assert.equal(ui.reviewUpgradeWaiting, true);
    ui.busy = false; ui.view = {closed: false}; ui.render = () => {}; let drains = 0; ui.drainFilings = async () => drains++;
    const methods = []; ui.rpc = async method => { methods.push(method); return {...ui.latest, reviews: method === 'recheck_review' ? [upgraded(row)] : [row]}; };
    await ui.action('folders', {}, false); if (ui.reviewUpgradePromise) await ui.reviewUpgradePromise;
    assert.deepEqual(methods, ['folders', 'recheck_review']); assert.equal(drains, 0);
  }
  {
    const {ui, writes} = fixture(); const row = oldPaper(); ui.latest.reviews = [row];
    await assert.rejects(ui.queueFiling('approve', {identity: row.id, digest: row.digest}), /saved review needs its local update/);
    assert.equal(writes.length, 0); assert.equal(ui.filingQueue.length, 0);
  }
  {
    const {ui, node, buttons} = viewFixture(); const row = oldPaper(); ui.latest.reviews = [row];
    ui.reviewUpgrade = {state: 'failed', error: 'A local Zotero lookup failed'};
    const panel = node(); ui.renderReview(panel);
    assert.match(text(panel), /This saved review could not be updated/);
    assert.ok(buttons.has('Retry saved review update'));
    assert.ok(buttons.has('Open Zotero reference') && buttons.has('Show attachment in Zotero'));
    assert.equal(buttons.get('Approve & file')[0].dataset.invalid, 'true');
    assert.match(text(panel), /Previously saved warnings/);
    assert.equal(buttons.has('File 1 routine documents together'), false);
  }
  {
    const {ui, node, buttons} = viewFixture(); const row = upgraded(oldPaper()); ui.latest.reviews = [row];
    ui.renderReview(node()); assert.ok(buttons.has('Compare in Zotero'));
    assert.ok(buttons.has('Keep these PDFs separate')); assert.equal(buttons.get('Approve & file')[0].dataset.invalid, 'false');
  }
  console.log('PASS: delayed startup preserves user navigation, selections, and drafts; correct initial and requested tabs; stale-window isolation; automatic saved-review upgrades, recovery, and no queued filings');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
