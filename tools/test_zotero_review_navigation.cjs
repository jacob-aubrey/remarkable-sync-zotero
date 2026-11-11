// Exercise the real navigation helper/endpoint with synthetic native objects.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function fixture() {
  const calls = [], lookups = [], items = new Map(), collections = new Map(), replacements = new Map(), relationLookups = [];
  const item = (key, id, type = 'journalArticle', extra = {}) => ({key, id, itemType: type, libraryID: 1, version: 1,
    deleted: false, loadAllData: async () => {}, isInTrash() { return this.ancestorTrashed || this.deleted; },
    isRegularItem: () => !['attachment', 'note'].includes(type), isFileAttachment: () => type === 'attachment',
    isPDFAttachment() { return type === 'attachment' && !this.office; }, ...extra});
  items.set('PARENT23', item('PARENT23', 11));
  items.set('PARENT24', item('PARENT24', 12));
  items.set('ATTACH23', item('ATTACH23', 21, 'attachment'));
  items.set('FILE2345', item('FILE2345', 22, 'attachment', {office: true}));
  items.set('NTE23456', item('NTE23456', 31, 'note'));
  collections.set('CLLECT23', {key: 'CLLECT23', id: 41, libraryID: 1, deleted: false});
  const pane = {selectItems: async (ids, options) => {
    calls.push({selectItems: Array.from(ids), inLibraryRoot: options.inLibraryRoot}); return true;
  }, collectionsView: {selectCollection: async id => { calls.push({collection: id}); return true; }}};
  const win = {focus: () => calls.push({focus: true}), Zotero_Tabs: {select: id => calls.push({tab: id})}};
  const Zotero = {Libraries: {userLibraryID: 1},
    Items: {getByLibraryAndKeyAsync: async (library, key) => { lookups.push([library, key]); return items.get(key); }},
    Collections: {getByLibraryAndKeyAsync: async (library, key) => { lookups.push([library, key]); return collections.get(key); }},
    URI: {getLibraryURI: library => 'http://zotero.org/users/' + library,
      getItemURI: item => 'http://zotero.org/users/' + item.libraryID + '/items/' + item.key},
    Relations: {replacedItemPredicate: 'dc:replaces', getByPredicateAndObject: async (type, predicate, uri) => {
      relationLookups.push({type, predicate, uri}); return replacements.get(uri) || [];
    }},
    getMainWindow: () => win, getActiveZoteroPane: () => pane,
    Reader: {open: async id => calls.push({reader: id})}};
  const context = vm.createContext({Zotero, notableEndpoints: {}});
  vm.runInContext(fs.readFileSync('zotero-support/library.js', 'utf8'), context);
  context.notableRegisterLibraryEndpoint(class {});
  const endpoint = new context.notableEndpoints['/api/notable/action']();
  const request = async (data, pinned = true) => {
    const response = await endpoint.run({headers: new Map(pinned ? [['Zotero-Server-ID', 'synthetic']] : []),
      data: {action: 'navigate_review', ...data}});
    return {status: response[0], value: JSON.parse(response[2])};
  };
  const replace = (from, ...to) => replacements.set('http://zotero.org/users/1/items/' + from,
    to.map(value => typeof value === 'string' ? items.get(value) : value));
  return {calls, lookups, items, collections, pane, win, context, request, item, replace, replacements, relationLookups};
}

async function main() {
  {
    const f = fixture(), before = JSON.stringify([...f.items.values()]);
    const result = await f.request({target: 'references', keys: ['PARENT24', 'PARENT23', 'PARENT24']});
    assert.equal(result.status, 200);
    assert.deepEqual(result.value, {navigated: true, target: 'references', keys: ['PARENT24', 'PARENT23']});
    assert.deepEqual(f.calls, [{focus: true}, {selectItems: [12, 11], inLibraryRoot: true}]);
    assert.equal(JSON.stringify([...f.items.values()]), before, 'Navigation cannot edit items');
  }
  for (const [target, key, expected] of [
    ['attachment', 'ATTACH23', [{focus: true}, {selectItems: [21], inLibraryRoot: true}]],
    ['attachment', 'FILE2345', [{focus: true}, {selectItems: [22], inLibraryRoot: true}]],
    ['pdf', 'ATTACH23', [{focus: true}, {reader: 21}]],
    ['collection', 'CLLECT23', [{focus: true}, {tab: 'zotero-pane'}, {collection: 41}]]
  ]) {
    const f = fixture(), result = await f.request({target, key});
    assert.equal(result.status, 200); assert.deepEqual(result.value.keys, [key]);
    assert.deepEqual(f.calls, expected);
  }
  for (const [data, expectedStatus] of [
    [{target: 'references', keys: ['PARENT23', 'MISSING2']}, 409],
    [{target: 'references', keys: ['PARENT23', 'ATTACH23']}, 409],
    [{target: 'references', keys: ['NTE23456']}, 409],
    [{target: 'references', keys: ['PARENT23', 'invalid']}, 400],
    [{target: 'references', keys: []}, 400],
    [{target: 'references', keys: 'PARENT23'}, 400],
    [{target: 'references', keys: Array(101).fill('PARENT23')}, 400],
    [{target: 'references'}, 400],
    [{target: 'attachment', key: 'PARENT23'}, 409],
    [{target: 'pdf', key: 'FILE2345'}, 409],
    [{target: 'pdf', key: 'PARENT23'}, 409],
    [{target: 'collection', key: 'MISSING2'}, 409],
    [{target: 'collection', keys: ['CLLECT23', 'CLLECT24']}, 400],
    [{target: 'merge', keys: ['PARENT23', 'PARENT24']}, 400]
  ]) {
    const f = fixture(), result = await f.request(data);
    assert.equal(result.status, expectedStatus, JSON.stringify(data));
    assert.match(result.value.error, /Zotero|review|target/);
    assert.deepEqual(f.calls, [], 'A stale/invalid participant cannot cause partial focus or selection');
  }
  for (const extra of [{deleted: true}, {ancestorTrashed: true}, {libraryID: 2}]) {
    const f = fixture(); Object.assign(f.items.get('PARENT24'), extra);
    assert.equal((await f.request({target: 'references', keys: ['PARENT23', 'PARENT24']})).status, 409);
    assert.deepEqual(f.calls, []);
  }
  for (const extra of [{deleted: true}, {libraryID: 2}]) {
    const f = fixture(); Object.assign(f.collections.get('CLLECT23'), extra);
    assert.equal((await f.request({target: 'collection', key: 'CLLECT23'})).status, 409);
    assert.deepEqual(f.calls, []);
  }
  {
    const f = fixture(); delete f.pane.selectItems;
    assert.equal((await f.request({target: 'references', key: 'PARENT23'})).status, 409);
    assert.deepEqual(f.calls, [], 'Unsupported APIs fail before focus');
  }
  {
    const f = fixture(); delete f.win.Zotero_Tabs;
    assert.equal((await f.request({target: 'collection', key: 'CLLECT23'})).status, 409);
    assert.deepEqual(f.calls, []);
  }
  {
    const f = fixture();
    assert.equal((await f.request({target: 'references', key: 'PARENT23'}, false)).status, 428);
    assert.deepEqual(f.calls, []); assert.deepEqual(f.lookups, []);
  }
  {
    const f = fixture();
    const result = await f.context.notableNavigateReview({target: 'references', keys: ['PARENT23']});
    assert.equal(result.navigated, true);
    await assert.rejects(f.context.notableNavigateReview({target: 'references', key: 'MISSING2'}), /no longer available/);
  }
  {
    const f = fixture(); f.items.get('PARENT23').deleted = true; f.replace('PARENT23', 'PARENT24');
    const before = JSON.stringify([...f.items.values()]);
    const result = await f.request({target: 'references', keys: ['PARENT23', 'PARENT24']});
    assert.equal(result.status, 200);
    assert.deepEqual(result.value, {navigated: true, target: 'references', keys: ['PARENT24'],
      requestedKeys: ['PARENT23', 'PARENT24'], replacements: [{from: 'PARENT23', to: 'PARENT24'}]});
    assert.deepEqual(f.calls, [{focus: true}, {selectItems: [12], inLibraryRoot: true}]);
    assert.deepEqual(f.relationLookups, [{type: 'item', predicate: 'dc:replaces', uri: 'http://zotero.org/users/1/items/PARENT23'}]);
    assert.equal(JSON.stringify([...f.items.values()]), before, 'Following a native merge cannot edit or rebind any item');
  }
  {
    const f = fixture(); f.replace('MISSING2', 'PARENT24');
    const result = await f.request({target: 'references', key: 'MISSING2'});
    assert.equal(result.status, 200); assert.deepEqual(result.value.keys, ['PARENT24']);
    assert.deepEqual(result.value.replacements, [{from: 'MISSING2', to: 'PARENT24'}]);
  }
  {
    const f = fixture(); f.replace('PARENT23', 'PARENT24');
    const result = await f.request({target: 'references', key: 'PARENT23'});
    assert.deepEqual(result.value, {navigated: true, target: 'references', keys: ['PARENT23']});
    assert.deepEqual(f.relationLookups, [], 'A restored/live exact item takes precedence over historical replacements');
  }
  {
    const f = fixture(), lookup = f.context.Zotero.Items.getByLibraryAndKeyAsync;
    f.context.Zotero.Items.getByLibraryAndKeyAsync = async (library, key) => {
      if (key === 'PARENT24') f.items.get('PARENT23').deleted = true;
      return lookup(library, key);
    };
    assert.equal((await f.request({target: 'references', keys: ['PARENT23', 'PARENT24']})).status, 409);
    assert.deepEqual(f.calls, [], 'An earlier item becoming trashed during later lookups prevents selection');
  }
  {
    const f = fixture(); f.items.get('PARENT23').deleted = true;
    f.items.set('INTERM23', f.item('INTERM23', 13, 'journalArticle', {deleted: true}));
    f.replace('PARENT23', 'INTERM23'); f.replace('INTERM23', 'PARENT24');
    const result = await f.request({target: 'references', key: 'PARENT23'});
    assert.equal(result.status, 200); assert.deepEqual(result.value.keys, ['PARENT24']);
    assert.equal(f.relationLookups.length, 2);
  }
  for (const target of ['pdf', 'attachment']) {
    const f = fixture(); f.items.get('ATTACH23').deleted = true;
    f.items.set('ATTACH24', f.item('ATTACH24', 23, 'attachment'));
    f.replace('ATTACH23', 'ATTACH24');
    const result = await f.request({target, key: 'ATTACH23'});
    assert.equal(result.status, 200); assert.deepEqual(result.value.keys, ['ATTACH24']);
    assert.deepEqual(f.calls, target === 'pdf' ? [{focus: true}, {reader: 23}]
      : [{focus: true}, {selectItems: [23], inLibraryRoot: true}]);
  }
  for (const setup of [
    f => { f.replace('PARENT23', 'PARENT24', 'ATTACH23'); },
    f => { f.replace('PARENT23', 'ATTACH23'); },
    f => { f.replace('PARENT23', {...f.items.get('PARENT24'), libraryID: 2}); },
    f => { f.replace('PARENT23', {...f.items.get('PARENT24'), key: 'invalid'}); },
    f => { f.replace('PARENT23', {...f.items.get('PARENT24'), key: 'MISSING2'}); },
    f => { f.items.get('PARENT24').deleted = true; f.replace('PARENT23', 'PARENT24'); },
    f => { f.replace('PARENT23', 'PARENT23'); },
    f => { f.items.get('PARENT24').deleted = true; f.replace('PARENT23', 'PARENT24'); f.replace('PARENT24', 'PARENT23'); },
    f => { delete f.context.Zotero.Relations.getByPredicateAndObject; },
    f => { delete f.context.Zotero.URI.getLibraryURI; },
    f => { f.replacements.set('http://zotero.org/users/1/items/PARENT23', {}); }
  ]) {
    const f = fixture(); f.items.get('PARENT23').deleted = true; setup(f);
    const result = await f.request({target: 'references', keys: ['PARENT24', 'PARENT23']});
    assert.equal(result.status, 409, 'Ambiguous, invalid or unavailable merge history must fail');
    assert.deepEqual(f.calls, [], 'A valid original participant cannot cause partial focus/selection before a merge error');
  }
  {
    const f = fixture(); f.items.get('ATTACH23').deleted = true; f.replace('ATTACH23', 'FILE2345');
    assert.equal((await f.request({target: 'pdf', key: 'ATTACH23'})).status, 409);
    assert.deepEqual(f.calls, [], 'A PDF cannot resolve to an office attachment');
  }
  {
    const f = fixture(); f.items.get('PARENT23').deleted = true;
    let previous = 'PARENT23';
    for (let n = 2; n <= 9; n++) {
      const key = 'ATMAX23' + n;
      f.items.set(key, f.item(key, 200 + n, 'journalArticle', {deleted: n !== 9}));
      f.replace(previous, key); previous = key;
    }
    const result = await f.request({target: 'references', key: 'PARENT23'});
    assert.equal(result.status, 200); assert.deepEqual(result.value.keys, ['ATMAX239']);
    assert.equal(f.relationLookups.length, 8, 'Exactly eight native replacement steps are supported');
  }
  {
    const f = fixture(); f.items.get('PARENT23').deleted = true;
    let previous = 'PARENT23';
    for (let n = 2; n <= 9; n++) {
      const key = 'CHAIN23' + n;
      f.items.set(key, f.item(key, 100 + n, 'journalArticle', {deleted: true}));
      f.replace(previous, key); previous = key;
    }
    f.replace(previous, 'PARENT24');
    const result = await f.request({target: 'references', key: 'PARENT23'});
    assert.equal(result.status, 409); assert.match(result.value.error, /too long/);
    assert.equal(f.relationLookups.length, 8); assert.deepEqual(f.calls, []);
  }
  {
    const f = fixture();
    f.items.get('PARENT23').deleted = true; f.replace('PARENT23', 'PARENT24');
    const survivor = f.items.get('PARENT24');
    survivor.toJSON = () => ({key: survivor.key, itemType: 'journalArticle'});
    survivor.getAttachments = survivor.getNotes = () => [];
    survivor.getDisplayTitle = () => 'Survivor';
    f.context.Zotero.Utilities = {Internal: {md5: value => 'signature'}};
    const result = await f.request({action: 'inspect_review_reference', key: 'PARENT23'});
    assert.equal(result.status, 200); assert.equal(result.value.parent.key, 'PARENT24');
    assert.equal(result.value.inventory.length, 1); assert.deepEqual(f.calls, [], 'Inspection must not focus or modify Zotero');
    assert.equal((await f.request({action: 'inspect_review_reference', key: 'invalid'})).status, 400);
  }
  console.log('PASS: exact native navigation, bounded native merge survivors, identity/type/library/Trash guards and no partial selection or item writes');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
