const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

async function main() {
  const context = {clearTimeout, setTimeout, Date};
  vm.runInNewContext(fs.readFileSync('zotero-support/ui.js', 'utf8'), context);
  const ui = context.NotableUI;
  const drains = [];
  ui.latest = {config: {connection_id: 'C', zotero_root: 'Z', tablet_root: 'T', sync_paused: true},
    status: {}, reviews: [], links: []};
  ui.syncDecisions = [{key: 'unrelated-deletion'}];
  ui.filingQueue = [{id: 'unrelated-filing'}];
  for (const method of ['drainFilings', 'drainSyncDecisions', 'drainTagEdits', 'drainAfterSync']) ui[method] = async () => drains.push(method);
  for (const method of ['disable', 'schedule', 'badge', 'refreshCollectionNames', 'message']) ui[method] = () => {};
  ui.adoptStatus = result => { ui.latest = result; };
  const targets = [{cloud: 'selected', parent: 'PARENT23', source_key: 'SOURCE23', view_key: 'VIEW2345'}];
  ui.rpc = async (method, params) => {
    assert.equal(method, 'sync_selected');
    assert.equal(ui.operation.method, 'sync');
    assert.equal(ui.operation.scoped, true);
    assert.equal(params.targets[0].cloud, 'selected');
    return {...ui.latest, status: {message: 'Selected PDF sync finished.'}};
  };
  ui.syncDue = Date.now() - 1000;
  const result = await ui.action('sync_selected', {targets, connection: ['C', 'Z', 'T']});
  assert.equal(result.config.sync_paused, true);
  assert.deepEqual(drains, [], 'Refreshing one PDF must not execute pending writes for other papers');
  assert.ok(ui.syncDue > Date.now() + 290000, 'An overdue full sync must not immediately follow a selected refresh');
  assert.equal(ui.busy, false);
  console.log('PASS: selected sync keeps global pause, saved queues, scope, and full-sync scheduling intact.');
}
main().catch(error => {console.error(error); process.exitCode = 1;});
