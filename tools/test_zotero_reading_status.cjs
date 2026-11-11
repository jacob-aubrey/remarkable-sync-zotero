const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const vm = require('node:vm');

async function test() {
  let registration, unregistered, transaction = false;
  const Zotero = {
    MenuManager: {
      registerMenu(options) { registration = options; },
      unregisterMenu(id) { unregistered = id; },
    },
    DB: {async executeTransaction(callback) {
      transaction = true;
      try { await callback(); }
      finally { transaction = false; }
    }},
    logError(error) { throw error; },
  };
  const scope = {Zotero};
  vm.runInNewContext(readFileSync(join(__dirname, '../zotero-support/reading-status.js'), 'utf8'), scope);
  scope.NotableReadingStatus.register('test-plugin');
  assert.equal(registration.target, 'main/library/item');
  assert.equal(registration.pluginID, 'test-plugin');

  function paper(id, tags = [], options = {}) {
    return {
      id, tags: new Set(tags), saves: 0,
      isPDFAttachment: () => false, isRegularItem: () => true, isEditable: () => true,
      hasTag(tag) { return this.tags.has(tag); },
      addTag(tag) { this.tags.add(tag); },
      removeTag(tag) { this.tags.delete(tag); },
      async save() { assert.ok(transaction); this.saves++; },
      ...options,
    };
  }
  function menuContext(items) {
    return {
      items,
      menuElem: {
        ownerDocument: {defaultView: {alert: assert.fail}},
        setAttribute(name, value) { this[name] = value; },
      },
      setVisible(visible) { this.visible = visible; },
    };
  }
  const unread = paper(1, ['Spectral CT']);
  const read = paper(2, ['Read', 'Methods']);
  const attachment = {isPDFAttachment: () => true, parentItem: unread};
  const [markRead, markUnread] = registration.menus;
  const selection = menuContext([unread, read, attachment]);
  for (const command of [markRead, markUnread]) {
    command.onShowing(null, selection);
    assert.equal(selection.visible, true);
  }
  assert.equal(selection.menuElem.label, 'Mark as unread');
  await markRead.onCommand(null, selection);
  assert.deepEqual([...unread.tags], ['Spectral CT', 'Read']);
  assert.equal(unread.saves, 1);
  assert.equal(read.saves, 0);
  markRead.onShowing(null, selection);
  assert.equal(selection.visible, false);
  await markUnread.onCommand(null, selection);
  assert.deepEqual([...unread.tags], ['Spectral CT']);
  assert.deepEqual([...read.tags], ['Methods']);
  assert.equal(unread.saves, 2);
  assert.equal(read.saves, 1);

  const pdfOnly = menuContext([attachment]);
  markRead.onShowing(null, pdfOnly);
  assert.equal(pdfOnly.visible, true);
  await markRead.onCommand(null, pdfOnly);
  assert.ok(unread.hasTag('Read'));

  for (const unsupported of [paper(3, [], {deleted: true}), paper(3, [], {isFeedItem: true}),
    paper(3, [], {isEditable: () => false}), {...attachment, parentItem: undefined}]) {
    const mixed = menuContext([read, unsupported]);
    markRead.onShowing(null, mixed);
    assert.equal(mixed.visible, false);
    await markRead.onCommand(null, mixed);
    assert.equal(read.saves, 1);
  }

  scope.NotableReadingStatus.unregister();
  assert.equal(unregistered, registration.menuID);
}

test().catch(error => { console.error(error); process.exitCode = 1; });
