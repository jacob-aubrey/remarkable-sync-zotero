// Actual controller; synthetic panels and transport. No live papers or accounts.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const timers = new Map(); let serial = 0;
const context = vm.createContext({setTimeout: (callback, delay) => {timers.set(++serial, {callback, delay}); return serial;}, clearTimeout: id => timers.delete(id)});
vm.runInContext(fs.readFileSync('zotero-support/ui.js', 'utf8'), context);
const ui = context.NotableUI;
function node(tag, text = '') {
  return {tag, textContent: text, children: [], listeners: {}, dataset: {}, isConnected: true,
    setAttribute(name, value) {this[name] = value;}, focus() {},
    addEventListener(type, callback) {(this.listeners[type] ||= []).push(callback);},
    fire(type) {for (const callback of this.listeners[type] || []) callback({});},
    replaceChildren() {this.children = [];},
    all() {return this.children.flatMap(child => [child, ...child.all()]);}};
}
ui.el = (tag, text, parent, attrs = {}) => {const item = node(tag, text); Object.assign(item, attrs); parent?.children.push(item); return item;};
ui.button = (text, parent, callback) => {const button = ui.el('button', text, parent, {dataset: {}}); button.click = callback; return button;};
ui.checkbox = (text, parent, checked) => {const field = ui.el('checkbox', text, parent); field.checked = checked; return field;};
ui.filingConnection = () => 'saved-connection';
ui.latest = {config: {}, reviews: [{id: 'review', digest: 'sha1', status: 'pending', payload: {path: 'paper.pdf'}}]};
ui.view = {closed: false};
const scope = {identity: 'review'};
const proposal = {target: 'review:review', token: 'proposal-token', keywords: [{tag: 'Keywords', source: 'Paper'}],
  suggestions: [{tag: 'Topic', source: 'YAKE', evidence: 'Topic'}], selected: [], notice: 'Local only'};
const root = node('root');
const buttons = () => root.all().filter(item => item.tag === 'button');
async function main() {
  ui.message = () => {throw Error('Quiet progress must not replace the user status');};
  ui.receiveProgress({event: 'progress', quiet: true, message: 'Still working...'});
  const calls = [];
  ui.action = async (...args) => {calls.push(args); return {saved: true, deferred_until_filing: true};};
  ui.rpc = async (method, params) => {calls.push([method, params]); assert.equal(ui.busy, false); return {proposal};};
  ui.paperTagPanel(root, scope);
  assert.equal(buttons().some(item => /Find keywords/.test(item.textContent)), false);
  assert.equal(root.children[0].open, false, 'Suggestions start before the panel is expanded');
  assert.ok(timers.size);
  await ui.preparePaperTags();
  assert.equal(calls.length, 1); assert.equal(calls[0][0], 'suggest_paper_tags'); assert.equal(calls[0][1].quiet, true);
  assert.equal(ui.busy, false, 'Automatic suggestions never activate global progress');
  assert.equal(timers.size, 0, 'A completed analysis does not poll or rerender repeatedly');
  const checkbox = root.all().find(item => item.tag === 'checkbox');
  checkbox.checked = true; checkbox.fire('change');
  ui.paperTagPanel(root, scope); await ui.preparePaperTags();
  assert.equal(calls.length, 1, 'The same paper analysis is reused');
  assert.equal(root.all().filter(item => item.tag === 'checkbox').at(-1).checked, true, 'Draft choices survive rerender');
  await buttons().find(item => item.textContent === 'Use these tags when filing').click();
  assert.equal(calls[1][0], 'save_paper_tags', 'Only explicit approval saves choices');
  assert.deepEqual(Array.from(calls[1][1].selected), ['Topic']);
  const savedButton = buttons().find(item => item.textContent === 'Use these tags when filing');
  assert.equal(savedButton.disabled, true);
  checkbox.checked = false; checkbox.fire('change');
  assert.equal(savedButton.disabled, false, 'Changing an approved selection enables saving it again immediately');
  checkbox.checked = true; checkbox.fire('change');
  const priorEntry = ui.paperTagEntry(scope);
  for (const output of priorEntry.outputs) output.isConnected = false;
  ui.paperTagPanel(root, scope);
  assert.equal(priorEntry.outputs.size, 1, 'Detached output nodes are pruned from the cache');
  assert.equal(priorEntry.listeners.size, 1, 'Detached panel callbacks do not retain old DOM trees');
  ui.paperTagEvidenceSerial = 1;
  ui.paperTagPanel(root, scope);
  ui.rpc = async () => {throw Error('File unavailable');};
  await ui.preparePaperTags();
  assert.equal(timers.size, 0, 'Failed analysis waits for an explicit retry');
  const retry = buttons().find(item => item.textContent === 'Retry tag suggestions'); assert.ok(retry);
  retry.click();
  let finish;
  ui.rpc = (_method, params) => {assert.equal(params.quiet, true); return new Promise(resolve => {finish = resolve;});};
  const pending = ui.preparePaperTags();
  assert.equal(ui.paperTagChecking, true);
  assert.equal(ui.busy, false);
  finish({proposal}); await pending;
  assert.equal(ui.paperTagChecking, false);
  assert.equal(root.all().filter(item => item.tag === 'checkbox').at(-1).checked, true, 'Retry preserves approved/manual draft choices');
  ui.latest.reviews[0].digest = 'new-content'; ui.paperTagPanel(root, scope);
  const changedEntry = ui.paperTagEntry(scope); assert.equal(changedEntry.draft.selected.size, 0, 'New document bytes get a fresh draft');
  ui.busy = true; const before = changedEntry.state; await ui.preparePaperTags(); assert.equal(changedEntry.state, before, 'User operations have priority');
  ui.stopped = true; ui.schedulePaperTags(); assert.equal(timers.size, 0);
  const cleanup = node('root');
  ui.setButtonHelp = () => {};
  const cleanupCalls = [];
  ui.action = async (method, params) => {
    cleanupCalls.push([method, params]);
    if (method === 'preview_keyword_cleanup') return {papers: [{parent: 'PAPER', title: 'Paper', token: 'current',
      tags: [{tag: 'Models and', source: 'Printed keyword section', evidence: 'Keywords: Models and', reason: 'Clipped phrase.'}]}]};
    return {removed: 1};
  };
  ui.renderKeywordCleanup(cleanup);
  await cleanup.all().find(item => item.textContent === 'Check automatic tags').click();
  assert.deepEqual(cleanupCalls.map(([method]) => method), ['preview_keyword_cleanup']);
  assert.ok(cleanup.all().some(item => item.textContent.includes('Keywords: Models and')));
  const remove = cleanup.all().find(item => item.textContent === 'Remove selected tags from this paper');
  const suspect = cleanup.all().find(item => item.tag === 'checkbox');
  assert.equal(suspect.checked, false);
  assert.equal(remove.disabled, true);
  await remove.click();
  assert.equal(cleanupCalls.length, 1);
  suspect.checked = true;
  suspect.fire('change');
  assert.equal(remove.disabled, false);
  await remove.click();
  assert.equal(cleanupCalls[1][0], 'maintenance');
  assert.equal(cleanupCalls[1][1].action, 'repair_keywords');
  assert.equal(cleanupCalls[1][1].selected[0].token, 'current');
  assert.deepEqual(Array.from(cleanupCalls[1][1].selected[0].tags), ['Models and']);
  const names = node('root'), callsForNames = [];
  ui.busy = false; ui.stopped = false;
  const match = {key: 'PAPER234', title: 'Comparison of liquid embolics', token: 'current-name',
    reference: true, findings: [{where: 'Zotero filename', name: 'Gurevich_et_al_UnknownJournal_2025.pdf', attachment: 'FILE2345'}]};
  const unlinked = {key: 'PAPER567', title: 'An unknown effect', token: 'other',
    reference: true, findings: [{where: 'Zotero title', name: 'An unknown effect'}]};
  const rename = {identity: 'LINK', token: 'fresh', title: match.title, tablet: 'Gurevich_UnknownJournal_2025',
    target: 'Gurevich_Radiology_2025', files: [{before: match.findings[0].name, filename: 'Gurevich_Radiology_2025.pdf'}]};
  ui.latest.links = [{cloud: 'LINK', name: rename.tablet, parent: match.key}];
  const metadata = new Map([[match.key, {title: match.title, citation: [match.title, 'Gurevich', '2025'],
    files: [match.findings[0].name], fields: ['Vascular Imaging']}]]);
  ui.paperSearchMetadata = async () => metadata;
  ui.reviewNavigationButton = (label, parent) => ui.button(label, parent, async () => {});
  ui.renderLinkedPaper = (parent, row, title) => ui.el('h3', title || row.name, parent);
  ui.action = async (method, params) => {
    callsForNames.push([method, params]);
    if (method === 'preview_filename_cleanup') return {papers: [rename]};
    if (method === 'queue_filename_cleanup') {
      ui.latest.links[0].filename_cleanup = {target: rename.target}; return {queued: ['LINK'], skipped: []};
    }
    return {matches: [{...match, queued: !!params.selected}, unlinked], queued: params.selected ? [match] : []};
  };
  const find = text => names.all().find(item => item.textContent === text);
  const control = label => names.all().find(item => item['aria-label'] === label);
  const rerender = () => {names.replaceChildren(); ui.renderLinks(names);};
  const tick = () => new Promise(resolve => setImmediate(resolve));
  ui.tab = 'links'; ui.view.document = {};
  ui.render = rerender;
  ui.schedule = () => {};
  rerender(); await tick();
  let search = control('Search linked papers');
  search.value = 'gurevich 2025'; search.fire('input');
  assert.ok(find(match.title), 'Terms can match across citation fields');
  let mode = control('Search fields');
  mode.value = 'all'; mode.fire('change');
  search.value = 'vascular'; search.fire('input');
  assert.ok(find(match.title), 'All Fields & Tags searches tags');
  search.value = 'no-match'; search.fire('input');
  assert.ok(find('No papers match this search.'));
  control('Clear search').fire('click');
  assert.equal(search.value, '');
  assert.ok(find(match.title));
  await find('Review paper names').click();
  await tick();
  assert.ok(find(unlinked.title), 'All Zotero papers includes unlinked records');
  const scopeField = control('Paper scope');
  scopeField.value = 'linked'; scopeField.fire('change');
  assert.equal(find(unlinked.title), undefined);
  search = control('Search names to review');
  search.value = 'UnknownJournal'; search.fire('input');
  assert.ok(find('Zotero PDF filename · ' + match.findings[0].name));
  assert.ok(find('Preview rename'));
  assert.ok(find(rename.files[0].filename));
  assert.deepEqual(callsForNames.map(([method]) => method), ['maintenance', 'preview_filename_cleanup']);
  await find('Queue for review').click();
  assert.equal(callsForNames[2][1].selected[0].token, match.token);
  await find('Queue rename').click();
  assert.equal(callsForNames[3][1].selected[0].identity, rename.identity);
  assert.equal(callsForNames[3][1].selected[0].token, rename.token);
  assert.ok(find('Rename queued · ' + rename.target + '.pdf'));
  console.log('PASS: automatic quiet tag preparation, explicit-only approval, per-source caching, preserved draft choices, source changes, failures/retry, idle guards, and shutdown');
}
main().catch(error => {console.error(error); process.exitCode = 1;});
