// Run the actual label updater without a sync, a re-render, or library writes.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
let rows = [{key: 'ROOT', name: 'Windows imports -- to organize', parentKey: false},
  {key: 'CHILD', name: 'Chemistry', parentKey: 'ROOT'}];
const context = vm.createContext({Zotero: {Libraries: {userLibraryID: 1}, Collections: {
  getByLibrary(library, recursive) { assert.equal(library, 1); assert.equal(recursive, true); return rows; }
}}});
vm.runInContext(fs.readFileSync('zotero-support/ui.js', 'utf8'), context);
const ui = context.NotableUI;
ui.latest = {config: {zotero_root: 'ROOT', tablet_root: 'tablet-root', zotero_path: rows[0].name, tablet_path: 'Literature'},
  collections: rows.map(row => ({key: row.key, name: row.name, parentCollection: row.parentKey})),
  mappings: {ROOT: {cloud: 'tablet-root', root: true}, CHILD: {cloud: 'tablet-child', name: 'Chemistry', zparent: 'ROOT', cparent: 'tablet-root'}}};
ui.folderCatalog = {collections: ui.latest.collections, folders: [
  {id: 'tablet-root', name: 'Literature', parent: ''}, {id: 'tablet-child', name: 'Chemistry', parent: 'tablet-root'}]};
ui.filingQueue = [{connection: ui.filingConnection(), params: {collection: 'CHILD', filename: 'My chosen name.pdf'}}];
const queue = JSON.stringify(ui.filingQueue);
ui.reviewDrafts.set('paper', {filename: 'Unsaved edit.pdf', collection: 'CHILD'});
const draft = ui.reviewDrafts.get('paper');
const node = () => ({dataset: {}, children: [], addEventListener() {}, appendChild(child) {this.children.push(child);},
  replaceChildren() {this.children = [];}});
ui.el = (tag, text, parent) => {const result = node(); result.textContent = text; parent?.appendChild(result); return result;};
ui.select = (label, parent, options, selected) => ({...node(), value: selected, options: options.map(([value, textContent]) => ({value, textContent}))});
// This harness checks rename propagation; the adapter has a dedicated folder UI harness.
ui.folderSelect = (...args) => ui.select(...args);
const destination = ui.destinationSelect('Paired folders', node(), 'CHILD');
const ordinary = {options: [{value: 'ROOT', textContent: rows[0].name}, {value: 'CHILD', textContent: 'Old path'}]};
const label = {};
ui.view = {closed: false, document: {
  querySelectorAll: selector => selector.includes('folder-destination') ? [destination] : [ordinary],
  getElementById: () => label
}};
ui.rpc = ui.render = () => {throw Error('A collection rename must not sync or replace the current form');};
rows[0].name = 'Literature';
ui.refreshCollectionNames();
assert.equal(ordinary.options[0].textContent, 'Literature');
assert.equal(ordinary.options[1].textContent, 'Literature / Chemistry');
assert.equal(destination.options[1].textContent, 'Literature / Chemistry');
assert.equal(destination.title, 'Zotero: Literature / Chemistry → reMarkable: Literature / Chemistry');
assert.equal(destination.value, 'CHILD');
assert.equal(label.textContent, 'Zotero: Literature');
assert.equal(ui.latest.config.zotero_path, 'Literature');
assert.equal(ui.folderCatalog.collections[0].name, 'Literature');
assert.equal(ui.reviewDrafts.get('paper'), draft);
assert.equal(JSON.stringify(ui.filingQueue), queue);
assert.equal(ui.filingConnection(), ui.filingQueue[0].connection);
// A reopened workspace gets current names even when helper status was stale.
ui.view = null;
ui.latest.collections = [];
ui.latest.config.zotero_path = 'Windows imports -- to organize';
ui.refreshCollectionNames();
assert.equal(ui.latest.config.zotero_path, 'Literature');
assert.equal(ui.latest.collections[1].name, 'Chemistry');
console.log('PASS: renamed root and descendant paths update live; selections, drafts, queue, and connection stay intact; reopened labels refresh without sync');
