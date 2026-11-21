// Exercise the actual controller against timer/window races without waiting
// five minutes or touching a profile, network connection, or library.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const timers = new Map();
let next = 1;
let now = 1000000;
const context = {Date: {now: () => now}, setTimeout: (callback, delay) => { const id = next++; timers.set(id, {callback, delay}); return id; },
  clearTimeout: id => timers.delete(id)};
vm.createContext(context);
vm.runInContext(fs.readFileSync('zotero-support/ui.js', 'utf8'), context);
const ui = context.NotableUI;
const action = ui.action;
const calls = [];
ui.latest = {config: {tablet_root: 'example', automatic: true, sync_paused: false}};
ui.stopped = false;
ui.action = async (...args) => { calls.push(args); ui.syncDue = null; };
async function fire() {
  const timer = timers.get(ui.timer);
  timers.delete(ui.timer);
  now += timer.delay;
  return timer.callback();
}
async function main() {
  ui.schedule(5000);
  assert.equal(timers.get(ui.timer).delay, 5000);
  ui.view = {closed: false};
  await fire();
  assert.equal(calls.length, 1);
  assert.equal(calls[0][1].automatic, true);
  assert.equal(timers.get(ui.timer).delay, 1800000);
  now += 200000; ui.schedule();
  assert.equal(timers.get(ui.timer).delay, 1600000, 'An unrelated action keeps the original 30-minute deadline');
  ui.busy = true;
  await fire();
  assert.equal(calls.length, 1, 'A local check keeps the single writer');
  assert.equal(timers.get(ui.timer).delay, 1000, 'A busy tick retries rather than losing the timer');
  ui.busy = false;
  await fire();
  assert.equal(calls.length, 2);
  ui.schedule(1000);
  assert.equal(timers.get(ui.timer).delay, 1000, 'A completed filing brings the next check forward');
  const oldCallback = timers.get(ui.timer).callback;
  ui.latest.config.sync_paused = true;
  ui.schedule(); assert.equal(ui.timer, null);
  await oldCallback();
  assert.equal(calls.length, 2, 'Pause invalidates even a stale timer callback');
  ui.latest.config.sync_paused = false;
  ui.latest.config.automatic = false;
  ui.schedule(); assert.equal(ui.timer, null);
  ui.latest.config.automatic = true;
  ui.failures = 2;
  ui.schedule(1000); assert.equal(timers.get(ui.timer).delay, 600000);
  ui.stopped = true; ui.schedule(); assert.equal(timers.size, 0);

  ui.action = action; ui.view = null; ui.stopped = false; ui.failures = 0;
  ui.latest = {config: {tablet_root: 'example', automatic: true, sync_paused: false}, reviews: []};
  for (const method of ['message', 'refreshCollectionNames', 'recordTransport']) ui[method] = () => {};
  const controls = [], checks = [];
  ui.process = {stdin: {write: async line => controls.push(JSON.parse(line).method)}};
  let complete;
  ui.rpc = (method, params) => {
    assert.equal(method, 'sync');
    checks.push(!!params.automatic);
    return new Promise(resolve => {
      complete = state => resolve({...ui.latest, status: {state}});
    });
  };

  assert.equal(ui.syncControlLabel(), 'Sync now');
  const manualCheck = ui.toggleSync();
  assert.equal(ui.syncControlLabel(), 'Pause sync');
  await ui.toggleSync();
  assert.equal(ui.syncControlLabel(), 'Stopping…');
  ui.receiveProgress({message: 'Still working...'});
  assert.deepEqual(controls, [], 'Wait until sync has started before sending cancel');
  ui.receiveProgress({message: 'Checking new papers and existing links…'});
  ui.receiveProgress({message: 'Checking Zotero matches and saved review decisions…'});
  assert.deepEqual(controls, ['cancel']);
  complete('paused'); await manualCheck;
  assert.equal(ui.latest.config.sync_paused, false, 'A workspace pause keeps automatic sync enabled');
  assert.equal(ui.syncControlLabel(), 'Resume sync');
  assert.equal(timers.get(ui.timer).delay, 1800000);

  const automaticCheck = fire();
  assert.equal(ui.syncControlLabel(), 'Pause sync', 'The next automatic check exits the temporary pause');
  complete('ready'); await automaticCheck;
  assert.equal(ui.syncControlLabel(), 'Sync now');

  const secondManualCheck = ui.toggleSync();
  ui.receiveProgress({message: 'Checking linked PDFs…'});
  await ui.toggleSync();
  assert.deepEqual(controls, ['cancel', 'cancel']);
  complete('paused'); await secondManualCheck;
  const resumedCheck = ui.toggleSync();
  assert.equal(ui.syncControlLabel(), 'Pause sync');
  complete('ready'); await resumedCheck;
  assert.equal(ui.syncControlLabel(), 'Sync now');
  assert.deepEqual(checks, [false, true, false, false]);
  ui.stopped = true; ui.schedule(); assert.equal(timers.size, 0);
  console.log('PASS: automatic deadline, busy retry, backoff, and full-sync button start, pause, and resume');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
