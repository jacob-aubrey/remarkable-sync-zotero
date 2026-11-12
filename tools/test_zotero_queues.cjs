const assert = require('node:assert/strict');
const {viewFixture} = require('./test_zotero_ui_recovery.cjs');
const {ui, buttons, fields, node} = viewFixture();
const paper = (id, kind, findings = []) => ({id, identity: id, kind, status: 'pending', digest: 'bytes', payload: {
  title: id, filename: id + '.pdf', path: 'fixture.pdf', document_type: 'journal_article',
  guidance_version: 1, guidance_state: 'ready', findings, review_token: 'token'}});
const issue = {code: 'possible_duplicate_references', domain: 'library', severity: 'decision', summary: 'Possible duplicates',
  explanation: 'Compare references.', next_step: 'Inspect in Zotero.', references: [{key: 'PARENT23', title: 'Reference'}]};
const source = paper('local', 'inbox'), zotero = paper('zotero', 'zotero'), tablet = paper('tablet', 'cloud');
const one = paper('one', 'zotero', [issue]), two = paper('two', 'cloud', [issue]);
const conflict = {id: 'conflict', kind: 'conflict', status: 'pending', payload: {type: 'paper', zotero: 'PARENT23', desktop: ['A'], tablet: ['B']}};
ui.latest.reviews = [source, zotero, tablet, one, two, conflict];
assert.equal(ui.queueRows('inbox').length, 3, 'Inbox contains only papers that can proceed to filing');
assert.deepEqual(Array.from(ui.queueRows('inbox-waiting'), row => row.id), ['one', 'two']);
assert.equal(ui.queueSummary('inbox'), '3 ready to file · 2 waiting on Library Issues');
assert.equal(ui.queueSummary('library'), '1 case to inspect');
assert.equal(ui.queueSummary('sync-review'), '1 sync decision');
assert.doesNotMatch(ui.paperStatusMessage(), /need review|ready to file|cases to inspect|sync decision/, 'The shared transfer banner must not repeat a global review total');
assert.equal(ui.queueRows('library').length, 1, 'Exact participant sets form one case');
assert.deepEqual(Array.from(ui.queueRows('library')[0].caseRows), ['one', 'two']);
assert.equal(ui.queueRows('sync-review')[0].id, 'conflict');
ui.selected = 'one'; ui.renderReview(node(), 'library');
assert.ok(buttons.has('Check my changes'));
assert.equal(fields.has('Proposed filename'), false, 'Library Issues never renders filing controls');
assert.equal([...buttons.keys()].some(label => /Approve|Add to Zotero/.test(label)), false);
ui.selected = 'one'; ui.renderReview(node(), 'inbox');
assert.equal(ui.selected, 'local', 'A waiting paper cannot remain selected in the actionable Inbox list');
assert.ok(fields.has('Proposed filename'), 'The Inbox form belongs to the first actionable paper');
assert.equal(ui.inboxActionLabel(zotero, 'existing-tablet'), 'Link to existing reMarkable copy');
assert.equal(ui.inboxActionLabel(tablet, 'existing-zotero'), 'Link to existing Zotero reference');
assert.equal(ui.inboxActionLabel(zotero), 'Approve for reMarkable');
assert.equal(ui.inboxActionLabel(tablet), 'Add to Zotero');
assert.equal(ui.inboxActionLabel(source), 'Approve & file');
assert.match(ui.buttonHelp('Suggest filename'), /no file is renamed/);
assert.match(ui.buttonHelp('Check my changes'), /only the items in this case/);
assert.match(ui.buttonHelp('Inbox (5)'), /ready for filing.*waiting on Library Issues/i);
assert.match(ui.buttonHelp('a paper title', true), /does not approve, merge, or transfer/);


issue.acknowledgement = {acknowledged: true};
assert.equal(ui.queueRows('library').length, 0, 'Intentional arrangements leave Library Issues');
assert.equal(ui.libraryFindings(one).length, 0, 'Intentional arrangements no longer block Inbox');
assert.equal(ui.queueRows('inbox').length, 5, 'An acknowledged arrangement returns its incoming papers to ready');
assert.equal(ui.queueRows('inbox-waiting').length, 0);
assert.equal(ui.queueSummary('inbox'), '5 ready to file · 0 waiting on Library Issues');

const fresh = viewFixture();
fresh.ui.latest.reviews = [one]; fresh.ui.selected = one.id;
fresh.ui.renderReview(fresh.node(), 'inbox');
assert.equal(fresh.buttons.has('Compare in Zotero'), false, 'Intentional arrangements do not recreate a library comparison inside Inbox');
assert.ok(fresh.buttons.has('Review this choice again'));
assert.ok(fresh.fields.has('Proposed filename'));
delete issue.acknowledgement;
two.payload.findings = [issue, {...issue, code: 'local_review_unavailable', severity: 'repair', explanation: 'A second source needs repair'}];
assert.equal(ui.queueRows('library').length, 2, 'Different source-repair evidence must never be hidden by grouping');
two.payload.findings = [issue];
const grouped = ui.queueRows('library')[0];
const separate = viewFixture(); separate.ui.latest = ui.latest;
separate.ui.renderReviewFindings(grouped, separate.node(), true);
assert.equal([...separate.buttons.keys()].some(label => label === 'Review: two'), false, 'Case members must not create no-op related-case links');



const nodeText = root => [root.textContent || '', ...(root.children || []).map(nodeText)].join(' ');
const descendants = root => [root, ...(root.children || []).flatMap(descendants)];
const reference = (id, payload = {}) => ({...paper(id, 'reference'), payload: {title: id, parent: id, ...payload}});
{
  const f = viewFixture();
  f.ui.latest.reviews = [source, zotero, tablet, reference('needs-pdf'), reference('missing-file', {missing_file: true}),
    reference('unknown', {pdf_state: 'unknown'}), {...paper('resume', 'zotero'), status: 'applying'}];
  assert.equal(f.ui.queueSummary('inbox'), '3 ready to file · 1 needs a PDF · 1 PDF unavailable here · 1 to check in Zotero · 1 filing to resume · 0 waiting on Library Issues');
  assert.match(f.ui.inboxRowCaption(reference('missing-file', {missing_file: true})), /PDF unavailable here/);
  assert.match(f.ui.inboxRowCaption(tablet), /reMarkable.*PDF filing/);
}
{
  const f = viewFixture();
  const office = {...zotero, payload: {...zotero.payload, path: 'slides.pptx'}};
  f.ui.latest.reviews = [office];
  const view = f.node(); f.ui.renderReview(view, 'inbox');
  assert.match(nodeText(view), /Zotero · Office file/);
  assert.match(nodeText(view), /This Office file can be filed in Zotero/);
  assert.doesNotMatch(nodeText(view), /Attach a PDF/);
  assert.ok(f.buttons.has('Approve & file'));
}
{
  const waiting = viewFixture(); waiting.ui.latest = ui.latest;
  waiting.ui.render = () => {}; waiting.ui.tab = 'inbox'; waiting.ui.selected = 'local';
  waiting.ui.reviewDrafts.set('two', {digest: two.digest, filename: 'My retained draft.pdf'});
  const view = waiting.node(); waiting.ui.renderInboxWaiting(view);
  const details = descendants(view).find(item => item.tag === 'details');
  assert.ok(details, 'Blocked papers remain available in an Inbox waiting section');
  assert.notEqual(details.open, true, 'Waiting papers are collapsed by default');
  assert.match(nodeText(view), /one/); assert.match(nodeText(view), /two/);
  assert.equal(waiting.fields.size, 0, 'Waiting papers expose no filing fields');
  assert.equal([...waiting.buttons.keys()].some(label => /Approve|Add to Zotero|Resume saved filing/.test(label)), false, 'Waiting papers expose navigation, not approval');
  waiting.ui.openLibraryCase('two');
  assert.equal(waiting.ui.tab, 'library');
  assert.equal(waiting.ui.selected, 'one', 'Waiting navigation opens the exact grouped case representative');
  assert.equal(waiting.ui.reviewDrafts.get('two').filename, 'My retained draft.pdf', 'Waiting navigation retains the existing filing draft');
}
{
  const f = viewFixture(); f.ui.latest.reviews = [source, zotero, tablet, one, two, conflict];
  f.ui.filingQueue = [{method: 'approve', params: {identity: source.id}}, {method: 'approve', params: {identity: one.id}}];
  assert.deepEqual(Array.from(f.ui.queueRows('inbox'), row => row.id), ['zotero', 'tablet']);
  assert.deepEqual(Array.from(f.ui.queueRows('inbox-waiting'), row => row.id), ['two'], 'Durably queued decisions are not offered again in waiting');
  assert.deepEqual(Array.from(f.ui.queueRows('library')[0].caseRows), ['two']);
}
{
  const f = viewFixture(), applying = {...paper('recovery', 'zotero', [issue]), status: 'applying'};
  f.ui.latest.reviews = [applying, one];
  assert.deepEqual(Array.from(f.ui.queueRows('inbox'), row => row.id), ['recovery'], 'Interrupted filings remain actionable with their saved choices');
  assert.deepEqual(Array.from(f.ui.queueRows('inbox-waiting'), row => row.id), ['one']);
  assert.equal(f.ui.queueRows('library').length, 1, 'Old applying findings do not create another library case');
}
{
  const f = viewFixture();
  assert.equal(f.ui.queueSummary('inbox'), '0 ready to file · 0 waiting on Library Issues');
  assert.equal(f.ui.queueSummary('library'), '0 cases to inspect');
  assert.equal(f.ui.queueSummary('sync-review'), '0 sync decisions');
  f.ui.latest.reviews = [source, zotero];
  f.ui.latest.deletions = [{id: 'tablet-trash'}]; f.ui.latest.local_deletions = [{id: 'zotero-trash'}];
  assert.equal(f.ui.queueSummary('inbox'), '2 ready to file · 0 waiting on Library Issues');
  assert.equal(f.ui.queueSummary('sync-review'), '2 sync decisions', 'Deletion decisions belong to the Sync Conflicts count');
  f.ui.latest.filing_summary = {approved_waiting_sync: 35}; f.ui.latest.links = [{cloud: 'linked'}];
  assert.match(f.ui.paperStatusMessage(), /35 approved, waiting for sync.*1 linked/);
  assert.doesNotMatch(f.ui.paperStatusMessage(), /need review/);
}

{
  const f = viewFixture(); f.ui.latest.reviews = JSON.parse(JSON.stringify([one, two]));
  f.ui.selected = one.id; f.ui.render = () => {};
  f.ui.reviewDrafts.set(one.id, {digest: one.digest, review_token: one.payload.review_token, filename: 'Retained before waiting.pdf'});
  const blockedView = f.node(); f.ui.renderReview(blockedView, 'inbox');
  assert.match(nodeText(blockedView), /Ready to file \(0\).*Waiting on Library Issues \(2\)/);
  assert.match(nodeText(blockedView), /No papers ready to file/);
  assert.equal(f.fields.size, 0, 'An all-blocked Inbox must not render a filing form');
  assert.equal([...f.buttons.keys()].some(label => /Approve|Add to Zotero|Resume saved filing/.test(label)), false);
  assert.ok(f.buttons.has('Open Library Issues'), 'The empty ready queue gives the next useful action');
  f.buttons.get('Open Library Issues')[0].click();
  assert.equal(f.ui.tab, 'library'); assert.equal(f.ui.selected, one.id);
  f.ui.latest.reviews[0].payload.findings = [];
  f.ui.tab = 'inbox'; const readyView = f.node(); f.ui.renderReview(readyView, 'inbox');
  assert.match(nodeText(readyView), /Ready to file \(1\).*Waiting on Library Issues \(1\)/);
  assert.equal(f.fields.get('Proposed filename').value, 'Retained before waiting.pdf', 'A resolved paper returns to ready with its earlier draft');
}


async function clearActionScopeChecks() {
  const f = viewFixture(), calls = [];
  f.ui.latest.reviews = [];
  f.ui.latest.local_deletions = [{id: 'local-trash', payload: {title: 'Kept tablet paper'}}];
  f.ui.latest.deletions = [{id: 'tablet-trash', payload: {title: 'Trashed tablet paper', parent: 'PARENT23',
    inventory: [{key: 'PDF12345', title: 'Annotated PDF'}, {key: 'NOTE2345', title: 'Research note'}]}}];
  f.ui.confirmSyncDecision = async job => calls.push([job.method, job.params]);
  const view = f.node(); f.ui.renderReview(view, 'sync-review');
  assert.equal(f.buttons.has('Recheck Zotero'), false, 'A local-only check must not look like a tablet-conflict refresh');
  assert.doesNotMatch(nodeText(view), /Delete everything|Delete PDF only/);
  for (const label of ['Restore on reMarkable', 'Trash linked PDFs in Zotero', 'Trash reference everywhere in Zotero',
    'Keep reMarkable copy', 'Move reMarkable copy to Trash', 'Remove from selected collections']) {
    assert.ok(f.buttons.has(label), 'Each decision names its target: ' + label);
    await f.buttons.get(label)[0].click();
  }
  assert.deepEqual(JSON.parse(JSON.stringify(calls)), [
    ['delete_decision', {identity: 'tablet-trash', choice: 'restore', destination: ''}],
    ['delete_decision', {identity: 'tablet-trash', choice: 'pdf_only', destination: ''}],
    ['delete_decision', {identity: 'tablet-trash', choice: 'everything', destination: ''}],
    ['local_delete_decision', {identity: 'local-trash', choice: 'keep_tablet'}],
    ['local_delete_decision', {identity: 'local-trash', choice: 'trash_tablet'}],
    ['delete_decision', {identity: 'tablet-trash', choice: 'remove_collections', destination: ''}]
  ]);
  assert.match(f.ui.buttonHelp('Trash linked PDFs in Zotero'), /notes PDF and any recorded source PDF.*Zotero Trash/);
  assert.match(f.ui.buttonHelp('Trash reference everywhere in Zotero'), /reference, all its attachments, and all its notes/);
  assert.equal(f.ui.filingQueue.length, 0, 'Sync decisions do not become queued filing approvals');
  for (const kind of ['cloud', 'zotero']) {
    const incoming = viewFixture(), row = paper('matched', kind);
    row.payload.matches = [{key: 'EXISTS23', title: 'Existing annotated copy'}];
    incoming.ui.latest.reviews = [row]; const content = incoming.node(); incoming.ui.renderReview(content, 'inbox');
    const match = incoming.fields.get(kind === 'cloud' ? 'PDF already found in Zotero' : 'PDF already found on reMarkable');
    assert.ok(match); assert.equal(match.value, 'EXISTS23');
    assert.match(match.options[0].textContent, kind === 'cloud' ? /Choose an existing Zotero PDF/ : /Create a separate reMarkable copy/);
    assert.equal(match.options[1].textContent, 'Use: Existing annotated copy');
    assert.ok(incoming.fields.has('Proposed filename') && incoming.fields.has('Destination folders'));
    assert.ok(incoming.buttons.has('Suggest filename'));
    assert.match(nodeText(content), /Your approval can update its name and folder/);
    assert.ok(incoming.buttons.has(kind === 'cloud' ? 'Link to existing Zotero reference' : 'Link to existing reMarkable copy'));
  }
}

async function applyingRecoveryChecks() {
  await clearActionScopeChecks();
  const interrupted = () => ({...paper('interrupted', 'zotero'), status: 'applying',
    approval: {title: 'Saved title', filename: 'Chosen filename.pdf', document_type: 'journal_article',
      parent: 'PARENT23', collection: 'Z', research: ['RESEARCH'], match: 'existing-tablet'}});
  const text = root => [root.textContent || '', ...(root.children || []).map(text)].join(' ');
  {
    const f = viewFixture(), row = interrupted(), calls = [];
    let tagPanels = 0;
    f.ui.latest.reviews = [row]; f.ui.selected = row.id;
    f.ui.reviewDrafts.set(row.id, {...row.payload, title: 'Uncommitted draft title', filename: 'Uncommitted draft.pdf'});
    f.ui.paperTagPanel = () => { tagPanels++; };
    f.ui.queueFiling = async (...args) => calls.push(args);
    const view = f.node(); f.ui.renderReview(view, 'inbox');
    assert.equal(f.fields.size, 0, 'Interrupted filings have no editable filing controls');
    assert.equal(tagPanels, 0, 'Interrupted filings have no tag controls');
    assert.match(text(view), /Saved title/); assert.match(text(view), /Chosen filename.pdf/);
    assert.equal(text(view).includes('Uncommitted draft.pdf'), false, 'Resume displays the saved approval, not an in-memory draft');
    await f.buttons.get('Resume saved filing')[0].click();
    assert.deepEqual(JSON.parse(JSON.stringify(calls)), [['approve', {identity: row.id, digest: row.digest,
      filename: 'Chosen filename.pdf', title: 'Saved title', document_type: 'journal_article', collection: 'Z',
      research: ['RESEARCH'], match: 'existing-tablet', review_token: 'token', main_reference: '', main_reference_metadata: null}, 'Saved title']]);
  }
  for (const missing of ['title', 'filename', 'document_type', 'collection', 'parent']) {
    const f = viewFixture(), row = interrupted(); delete row.approval[missing];
    f.ui.latest.reviews = [row]; const view = f.node(); f.ui.renderReview(view, 'inbox');
    assert.equal(f.buttons.has('Resume saved filing'), false, 'Incomplete journals must not fabricate ' + missing);
    assert.match(text(view), /files and recovery record are retained/);
    assert.equal(f.fields.size, 0);
  }
  {
    const lookups = [], calls = [], record = {key: 'MAIN2345', itemType: 'journalArticle', title: 'Current saved main article', date: '2024'};
    const f = viewFixture({notableItem: async key => { lookups.push(key); return {key, deleted: false,
      isRegularItem: () => true, toJSON: () => record}; }});
    const row = interrupted(); row.approval.document_type = 'supporting_information'; row.approval.parent = record.key;
    f.ui.latest.reviews = [row]; f.ui.queueFiling = async (...args) => calls.push(args);
    f.ui.renderReview(f.node(), 'inbox'); await f.buttons.get('Resume saved filing')[0].click();
    assert.deepEqual(lookups, [record.key], 'Supporting-information recovery looks up the exact saved parent');
    assert.equal(calls[0][1].main_reference, record.key);
    assert.equal(calls[0][1].main_reference_metadata, record);
    assert.equal(calls[0][1].filename, row.approval.filename);
  }
  {
    const f = viewFixture({notableItem: async () => null}), row = interrupted(); let queued = false;
    row.approval.document_type = 'supporting_information'; f.ui.latest.reviews = [row];
    f.ui.queueFiling = async () => { queued = true; }; f.ui.renderReview(f.node(), 'inbox');
    await assert.rejects(f.buttons.get('Resume saved filing')[0].click(), /saved main reference is unavailable/);
    assert.equal(queued, false, 'An unavailable saved parent never falls back to a different reference');
  }
  console.log('PASS: source-specific Inbox actions, exact library cases, queue separation, button descriptions, and saved-choice filing recovery');
}
applyingRecoveryChecks().catch(error => { console.error(error); process.exitCode = 1; });
