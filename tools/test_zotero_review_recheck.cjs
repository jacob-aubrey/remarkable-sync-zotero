// Local rechecks preserve human decisions and never resume queued transfers.
const assert = require('node:assert/strict');
const {fixture, viewFixture} = require('./test_zotero_ui_recovery.cjs');
const plain = value => JSON.parse(JSON.stringify(value));
function text(node) { return [node.textContent, ...node.children.map(text)].join(' '); }
const finding = (token = 'same-evidence') => ({code: 'possible_duplicate_references', domain: 'library', severity: 'decision',
  summary: 'These references overlap', explanation: 'Compare exact references.', next_step: 'Inspect in Zotero.',
  acknowledgement: {eligible: true, token, acknowledged: false}, references: []});
const paper = (id, token = 'same-evidence') => ({id, kind: 'zotero', status: 'pending', digest: 'bytes',
  payload: {title: 'Paper ' + id, filename: id + '.pdf', guidance_version: 1, guidance_state: 'ready', review_token: 'old-policy', findings: [finding(token)]}});

async function main() {
  {
    const {ui} = fixture();
    ui.latest.config.tablet_root = 'tablet';
    const reference = {...paper('reference', 'old'), kind: 'reference', payload: {parent: 'PARENT1'}};
    const attachment = {...paper('attachment'), payload: {parent: 'PARENT1'}};
    const newPaper = {...paper('new'), payload: {parent: 'PARENT2'}};
    ui.latest.reviews = [reference];
    const messages = [], calls = [];
    let next = [attachment, newPaper], issues = [];
    ui.message = (text, error = false) => messages.push([text, error]);
    ui.filingQueue = [{method: 'approve', params: {identity: 'queued'}}];
    ui.drainFilings = () => assert.fail('Checking Inbox cannot start queued transfers');
    ui.rpc = async (method, params) => {
      calls.push(method);
      if (method === 'recheck_review') return {...ui.latest, review_recheck: {checked: 1}};
      assert.equal(method, 'scan_inbox');
      assert.equal(params.discover, true);
      assert.equal(ui.operation.title, 'Checking Zotero for new papers…');
      ui.receiveProgress({message: 'Checking Zotero file 534 of 537: paper.pdf'});
      return {...ui.latest, reviews: next, inbox_issues: issues};
    };
    await ui.action('check_inbox', {}, false);
    assert.deepEqual(calls, ['recheck_review', 'scan_inbox']);
    assert.deepEqual(messages.splice(0), [['1 new paper in Inbox', false]], 'A PDF arriving for a known citation is not counted as a new paper');
    assert.equal(ui.reviewFreshnessMessage('inbox'), '', 'Inbox does not repeat a saved-review tally');
    await ui.action('check_inbox', {}, false);
    assert.deepEqual(messages.splice(0), [['Inbox is up to date', false]]);
    next = [{...attachment, digest: 'changed'}, newPaper];
    await ui.action('check_inbox', {}, false);
    assert.deepEqual(messages.splice(0), [['Inbox updated', false]]);
    issues = [{reason: 'Cannot read an intake file'}];
    await ui.action('check_inbox', {}, false);
    assert.deepEqual(messages.splice(0), [['Couldn’t finish checking Inbox. Try again.', true]]);
    ui.rpc = async () => {throw Error('Zotero unavailable');};
    await ui.action('check_inbox', {}, false);
    assert.deepEqual(messages, [['Couldn’t check Zotero. Try again.', true]]);
  }
  { const {ui} = fixture(); ui.reviewRecheck = {checked: 1, changed_ids: ['deleted'], resolved_ids: ['deleted']};
    assert.match(ui.reviewFreshnessMessage(), /Closed 1 review for a deleted attachment/); }
  for (const method of ['recheck_review', 'acknowledge_review_finding']) {
    const {ui} = fixture(); let drain = 0;
    ui.filingQueue = [{id: 'queued-decision'}]; ui.reviewStale = true;
    ui.drainFilings = async () => drain++;
    const operations = []; ui.rpc = async (name, params) => {
      operations.push([name, params]);
      return {...ui.latest, review_recheck: {checked: 2, changed_ids: ['a'], unavailable_ids: [], cloud_checked: false}};
    };
    const before = plain(ui.filingQueue);
    await ui.action(method, {}, false);
    assert.deepEqual(operations.map(row => row[0]), [method]);
    assert.equal(drain, 0, 'Local review operations never start queued filings');
    assert.deepEqual(plain(ui.filingQueue), before);
    assert.equal(ui.reviewStale, false);
    assert.match(ui.reviewFreshnessMessage(), /Rechecked 2 local reviews; 1 changed/);
    assert.match(ui.reviewFreshnessMessage(), /Rechecked 2 local reviews/);
  }
  {
    const {ui} = fixture(); ui.reviewStale = true;
    ui.latest.reviews = [paper('a')];
    ui.rpc = async () => { ui.observeReviewChange('modify', 'item'); return {...ui.latest, review_recheck: {checked: 1, changed_ids: []}}; };
    await ui.action('recheck_review', {}, false);
    assert.equal(ui.reviewStale, true, 'A concurrent native edit cannot be cleared by an earlier recheck');
    ui.reviewStale = false; const serial = ui.reviewChangeSerial;
    ui.observeReviewChange('refresh', 'item'); ui.observeReviewChange('modify', 'item-tag');
    assert.equal(ui.reviewStale, false); assert.equal(ui.reviewChangeSerial, serial);
    ui.observeReviewChange('add', 'item'); assert.equal(ui.reviewStale, true);
  }
  {
    const {ui, buttons, node} = viewFixture(); const row = paper('a');
    const other = paper('b'), unrelated = paper('c', 'different-evidence');
    ui.latest.reviews = [row, other, unrelated]; const operations = [];
    ui.action = async (...args) => operations.push(args); ui.render = () => {};
    const root = node(); ui.renderReviewFindings(row, root);
    assert.match(text(root), /1 other PDF review/);
    assert.equal(buttons.has('Review: Paper c'), false, 'Similar prose never joins evidence groups');
    await buttons.get('Keep these references separate')[0].click();
    assert.deepEqual(plain(operations), [['acknowledge_review_finding', {identity: 'a', digest: 'bytes',
      code: 'possible_duplicate_references', token: 'same-evidence', review_token: 'old-policy', keep_separate: true}]]);
    await buttons.get('Review: Paper b')[0].click(); assert.equal(ui.selected, 'b');
    assert.equal(row.status, 'pending'); assert.deepEqual(plain(ui.filingQueue), []);
  }
  {
    const {ui, fields, buttons, node} = viewFixture(); const row = paper('a'); ui.latest.reviews = [row];
    ui.renderReview(node()); fields.get('Proposed filename').value = 'My unsaved name.pdf'; fields.get('Proposed filename').fire('input');
    row.payload.review_token = 'new-policy'; row.payload.title = 'Updated native title'; row.payload.routine = true;
    const root = node(); ui.renderReview(root);
    assert.equal(ui.selected, 'a', 'Recheck keeps the selected paper visible even when it becomes routine');
    assert.equal(fields.get('Proposed filename').value, 'My unsaved name.pdf');
    assert.match(text(root), /Zotero evidence changed/);
    assert.ok(buttons.has('Use current Zotero suggestions'));
    assert.equal(ui.reviewDrafts.get('a').review_token, 'old-policy');
    ui.render = () => {}; await buttons.get('Use current Zotero suggestions')[0].click();
    assert.equal(ui.reviewDrafts.has('a'), false, 'Resetting a draft is explicit');
  }
  {
    const {ui, fields, buttons, node} = viewFixture(); const row = paper('a'); ui.latest.reviews = [row];
    ui.reviewDrafts.set('a', {digest: 'earlier-bytes', filename: 'Earlier name.pdf', title: 'Earlier title'});
    const root = node(); ui.renderReview(root);
    assert.equal(fields.get('Proposed filename').value, 'a.pdf'); assert.match(text(root), /Earlier name.pdf/);
    ui.render = () => {}; await buttons.get('Use earlier filing choices for this file')[0].click();
    assert.equal(ui.reviewDrafts.get('a').digest, 'bytes');
    assert.equal(ui.reviewDrafts.get('a').filename, 'Earlier name.pdf');
  }
  {
    const {ui, context, buttons, node} = viewFixture(); let message;
    ui.message = value => { message = value; };
    context.notableNavigateReview = async () => ({navigated: true, replacements: [{from: 'PARENT23', to: 'PARENT24'}]});
    ui.reviewNavigationButton('Inspect', node(), 'references', ['PARENT23']);
    await buttons.get('Inspect')[0].click();
    assert.equal(ui.reviewStale, true); assert.match(message, /surviving item.*sync links have not been changed/);
    assert.deepEqual(plain(ui.filingQueue), []);
  }
  {
    const {ui} = fixture(); const one = {open: true, dataset: {reviewDetail: 'evidence:duplicate'}}, two = {open: false, dataset: {reviewDetail: 'attachments'}};
    const doc = {body: {scrollTop: 460}, scrollingElement: {scrollTop: 460}, querySelectorAll: () => [one, two]};
    doc.querySelector = () => doc.body;
    ui.view = {document: doc}; ui.selected = 'a'; ui.renderedReviewId = 'a';
    const context = ui.captureReviewContext(); one.open = false; two.open = true; doc.body.scrollTop = 0;
    ui.restoreReviewContext(context); assert.equal(doc.body.scrollTop, 460); assert.equal(one.open, true); assert.equal(two.open, false);
    ui.selected = 'b';
    const beforeRedraw = ui.captureReviewContext();
    assert.equal(beforeRedraw.selected, 'a', 'A jump captures the old rendered paper even though selection already changed');
    ui.renderedReviewId = 'b'; one.open = false; ui.restoreReviewContext(beforeRedraw); assert.equal(one.open, false, 'Expansion does not leak into another paper');
  }
  for (const method of ['recheck_review', 'acknowledge_review_finding', 'sync']) {
    const {ui, writes, nativeDisable} = fixture();
    const button = {dataset: {queue: 'true', approve: 'true'}, querySelector: () => ({})};
    const browse = {dataset: {browse: 'true'}, closest: () => null};
    const mutation = {dataset: {}, closest: () => null};
    ui.view = {document: {querySelectorAll: query => query === 'button' ? [button] : [browse, mutation]}};
    ui.operation = {method}; ui.busy = true;
    nativeDisable.call(ui, true);
    assert.equal(button.disabled, method !== 'sync', 'Approvals wait for local evidence checks; normal sync still allows queued filing');
    assert.equal(browse.disabled, false, 'Local filters and draft fields stay available');
    assert.equal(mutation.disabled, true, 'Connection fields stay guarded during worker writes');
    if (method !== 'sync') {
      await assert.rejects(ui.queueFiling('approve', {identity: 'a', digest: 'bytes'}), /Wait for the local review check/);
      assert.equal(ui.filingQueue.length, 0); assert.equal(writes.length, 0);
    }
  }
  console.log('PASS: local-only recheck UI, queued-write isolation, native-change freshness, scoped overlap groups, retained drafts, merge notice, and scroll/detail context');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
