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
const calls = [];
ui.latest = {config: {tablet_root: 'example', automatic: true, sync_paused: false}};
ui.stopped = false;
ui.action = async (...args) => { calls.push(args); ui.syncDue = null; };
async function fire() {
  const timer = timers.get(ui.timer);
  timers.delete(ui.timer);
  now += timer.delay;
  await timer.callback();
}
async function main() {
  ui.schedule(5000);
  assert.equal(timers.get(ui.timer).delay, 5000);
  ui.view = {closed: false};
  await fire();
  assert.equal(calls.length, 1);
  assert.equal(calls[0][1].automatic, true);
  assert.equal(timers.get(ui.timer).delay, 300000);
  now += 200000; ui.schedule();
  assert.equal(timers.get(ui.timer).delay, 100000, 'An unrelated action keeps the original deadline');
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
  ui.schedule(1000); assert.equal(timers.get(ui.timer).delay, 900000);
  ui.stopped = true; ui.schedule(); assert.equal(timers.size, 0);
  console.log('PASS: open-workspace sync, persistent deadlines, busy retry, filing priority, pause races, and backoff');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
