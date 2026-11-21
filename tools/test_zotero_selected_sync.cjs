const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

async function main() {
  const context = {clearTimeout, setTimeout, Date};
  vm.runInNewContext(fs.readFileSync('zotero-support/ui.js', 'utf8'), context);
  const ui = context.NotableUI;
  ui.helperLaunch = {command: 'managed-worker'};
  const drains = [];
  ui.latest = {config: {connection_id: 'C', zotero_root: 'Z', tablet_root: 'T', sync_paused: true},
    status: {}, reviews: [], links: []};
  ui.syncDecisions = [{key: 'unrelated-deletion'}];
  ui.filingQueue = [{id: 'unrelated-filing'}];
  for (const method of ['drainFilings', 'drainSyncDecisions', 'drainTagEdits', 'drainAfterSync']) ui[method] = async () => drains.push(method);
  for (const method of ['disable', 'schedule', 'badge', 'refreshCollectionNames', 'message']) ui[method] = () => {};
  ui.adoptStatus = result => { ui.latest = result; };
  const targets = [{cloud: 'selected', parent: 'PARENT23', source_key: 'SOURCE23', view_key: 'VIEW2345'}];
  let requests = 0, expectedLabel = 'Sync now';
  const progress = [];
  ui.rpc = async (method, params) => {
    requests++;
    assert.equal(method, 'sync_selected');
    assert.equal(ui.operation.method, 'sync');
    assert.equal(ui.operation.scoped, true);
    assert.equal(ui.syncControlLabel(), expectedLabel);
    assert.equal(params.targets[0].cloud, 'selected');
    assert.deepEqual(Object.keys(params).sort(), ['connection', 'targets'],
      'The managed helper rejects full-sync-only parameters on selected sync');
    ui.receiveProgress({message: 'Syncing linked paper 1 of 1: Test paper…'});
    ui.receiveProgress({message: 'Publishing Test paper to Zotero…'});
    ui.receiveProgress({message: 'Still working...'});
    ui.receiveProgress({message: 'Received tablet PDF 1 of 1…'});
    return {...ui.latest, status: {message: 'Selected PDF sync finished.'}};
  };
  ui.syncDue = Date.now() - 1000;
  ui.busy = ui.inboxChecking = true;
  let finishInbox;
  ui.inboxRequest = new Promise(resolve => { finishInbox = resolve; }).then(() => {
    ui.busy = ui.inboxChecking = false;
  });
  const pending = ui.action('sync_selected', {targets, connection: ['C', 'Z', 'T']},
    true, (detail, papers) => progress.push([detail, papers?.checked]));
  assert.equal(requests, 0, 'Wait for the quiet inbox check before using the sync worker');
  assert.deepEqual(progress, [], 'The worker has no live progress until the inbox check has finished');
  finishInbox();
  const result = await pending;
  assert.equal(requests, 1, 'The selected sync starts when the inbox check finishes');
  assert.deepEqual(progress, [['Starting…', undefined], ['Syncing linked paper 1 of 1: Test paper…', 0],
    ['Publishing Test paper to Zotero…', 0], ['Received tablet PDF 1 of 1…', 1]]);
  assert.equal(result.config.sync_paused, true);
  assert.equal(ui.syncControlLabel(), 'Sync now');
  assert.deepEqual(drains, [], 'Refreshing one PDF must not execute pending writes for other papers');
  assert.ok(ui.syncDue > Date.now() + 290000, 'An overdue full sync must not immediately follow a selected refresh');
  assert.equal(ui.busy, false);
  ui.latest.config.sync_paused = false;
  ui.latest.config.automatic = true;
  ui.syncPausedForCycle = true;
  expectedLabel = 'Resume sync';
  await ui.action('sync_selected', {targets, connection: ['C', 'Z', 'T']});
  assert.equal(ui.syncControlLabel(), 'Resume sync', 'A selected refresh keeps the workspace pause visible');
  assert.equal(ui.latest.config.sync_paused, false);
  console.log('PASS: selected sync keeps global pause, saved queues, scope, and full-sync scheduling intact.');
}
main().catch(error => {console.error(error); process.exitCode = 1;});
