// Exercise the real chooser's navigation, selection, cancellation, and creation.
// The DOM fixture has no Zotero profile, filesystem access, or network writes.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Event {
  constructor(type, extra = {}) {this.type = type; Object.assign(this, extra);}
  preventDefault() {this.defaultPrevented = true;}
  stopPropagation() {this.stopped = true;}
}
class Node {
  constructor(tag, doc) {this.tag = tag; this.ownerDocument = doc; this.children = []; this.attrs = {}; this.listeners = {}; this.style = {}; this.value = ''; this.textContent = ''; this.disabled = false;}
  appendChild(child) {child.parent = this; this.children.push(child); return child;}
  replaceChildren(...children) {this.children = []; for (const child of children) this.appendChild(child);}
  setAttribute(key, value) {this.attrs[key] = String(value); if (key === 'open') this.open = true;}
  removeAttribute(key) {delete this.attrs[key]; if (key === 'open') this.open = false;}
  addEventListener(type, callback) {(this.listeners[type] ||= []).push(callback);}
  dispatchEvent(event) {event.target ||= this; for (const callback of this.listeners[event.type] || []) callback(event); if (event.bubbles && !event.stopped) this.parent?.dispatchEvent(event); return !event.defaultPrevented;}
  focus() {this.ownerDocument.activeElement = this; this.dispatchEvent(new Event('focus'));}
  showModal() {this.open = true;}
  close() {this.open = false;}
  click() {if (!this.disabled) this.dispatchEvent(new Event('click', {bubbles: true}));}
  all(predicate) {return this.children.flatMap(child => [...(predicate(child) ? [child] : []), ...child.all(predicate)]);}
}
const document = {defaultView: {Event}, createElementNS: (_namespace, tag) => new Node(tag, document)};
const context = vm.createContext({});
vm.runInContext(fs.readFileSync('zotero-support/folder-chooser.js', 'utf8'), context);
const create = args => context.NotableFolderChooser.create({document, parent: new Node('body', document), label: 'Destination folders', ...args});
const options = [
  {value: 'root', label: 'Literature', path: 'Literature'},
  {value: 'cz', label: 'CZ Independent Synthesis', path: 'Literature / CZ Independent Synthesis', parent: 'root'},
  {value: 'silane', label: 'Silanes', path: 'Literature / CZ Independent Synthesis / Silanes', parent: 'cz', detail: 'Zotero: Literature / Silanes → reMarkable: Papers / Silanes'},
  {value: 'nmr', label: '29Si NMR', path: 'Literature / 29Si NMR', parent: 'root'},
];
const find = (host, predicate) => host.all(predicate)[0];
const byText = (host, text) => find(host, node => node.textContent === text);
const treeRows = host => host.all(node => node.attrs.role === 'treeitem');
const key = (node, key) => node.dispatchEvent(new Event('keydown', {key, bubbles: true}));
const flush = () => new Promise(resolve => setImmediate(resolve));
async function main() {
  let changes = 0, remembered;
  const single = create({options, value: 'root', onRemember: value => {remembered = value;}});
  single.addEventListener('change', () => changes++);
  single.open();
  assert.equal(document.activeElement.tag, 'input', 'Opening immediately focuses search');
  assert.equal(treeRows(single).length, 1, 'Hierarchy starts collapsed');
  const expand = find(single, node => node.attrs['aria-label'] === 'Expand Literature');
  expand.click();
  assert.equal(treeRows(single).length, 3);
  treeRows(single)[1].focus(); key(document.activeElement, 'ArrowRight');
  assert.equal(treeRows(single).length, 4);
  key(document.activeElement, 'ArrowRight');
  assert.equal(document.activeElement.attrs['data-value'], 'silane');
  key(document.activeElement, 'Enter');
  assert.equal(single.value, 'silane'); assert.equal(changes, 1);
  assert.equal(remembered[0], 'silane');
  assert.equal(find(single, node => node.tag === 'dialog').open, false);
  assert.equal(document.activeElement.textContent, 'Change…');
  assert.ok(byText(single, options[2].detail), 'Paired paths remain inspectable');
  single.open();
  const search = find(single, node => node.tag === 'input');
  search.value = 'independent silanes'; search.dispatchEvent(new Event('input'));
  assert.equal(treeRows(single).length, 1, 'Search finds a folder across path segments');
  search.value = 'no such folder'; search.dispatchEvent(new Event('input'));
  assert.equal(treeRows(single).length, 0);
  key(search, 'Escape'); assert.equal(single.value, 'silane');
  single.disabled = true; single.open(); assert.equal(find(single, node => node.tag === 'dialog').open, false);
  single.disabled = false;
  const multi = create({options, value: ['root'], multiple: true});
  multi.open(); const multiSearch = find(multi, node => node.tag === 'input');
  multiSearch.value = '29si'; multiSearch.dispatchEvent(new Event('input'));
  treeRows(multi)[0].click();
  assert.equal(multi.selectedOptions.length, 1, 'Multiple choices are staged');
  byText(multi, 'Cancel').click();
  assert.equal(multi.selectedOptions[0].value, 'root');
  multi.open(); multiSearch.value = '29si'; multiSearch.dispatchEvent(new Event('input'));
  key(multiSearch, 'ArrowDown'); key(document.activeElement, ' ');
  byText(multi, 'Use selected collections').click();
  assert.deepEqual(Array.from(multi.selectedOptions, row => row.value), ['root', 'nmr']);
  const remove = find(multi, node => node.attrs['aria-label'] === 'Remove Literature / 29Si NMR'); remove.click();
  assert.equal(multi.selectedOptions.length, 1);
  let createParent;
  const creating = create({options, value: 'cz', onNew: async parent => {createParent = parent; return {value: 'new', label: '<New>', path: 'Literature / CZ Independent Synthesis / <New>', parent};}});
  creating.open(); byText(creating, 'New…').click(); await flush();
  assert.equal(createParent, 'cz'); assert.equal(creating.value, 'new');
  assert.equal(creating.selectedOptions[0].value, 'new');
  assert.ok(byText(creating, 'Literature / CZ Independent Synthesis / <New>'), 'Names are rendered as text');
  creating.setOptions(options); assert.equal(creating.value, 'new', 'Refreshing options does not silently change a draft');
  const multiCreate = create({options, value: ['root'], multiple: true,
    onNew: async () => ({value: 'new', label: 'New collection', path: 'Literature / New collection', parent: 'root'})});
  multiCreate.open(); const newSearch = find(multiCreate, node => node.tag === 'input');
  newSearch.value = '29si'; newSearch.dispatchEvent(new Event('input'));
  treeRows(multiCreate)[0].click();
  byText(multiCreate, 'New…').click(); await flush();
  assert.equal(multiCreate.selectedOptions.length, 1, 'Creation preserves staged selection until Apply');
  byText(multiCreate, 'Use selected collections').click();
  assert.deepEqual(Array.from(multiCreate.selectedOptions, row => row.value), ['root', 'nmr', 'new']);
  const failed = create({options, value: 'root', onNew: async () => {throw Error('Connection unavailable');}});
  failed.open(); byText(failed, 'New…').click(); await flush();
  assert.equal(find(failed, node => node.tag === 'dialog').open, true);
  assert.equal(byText(failed, 'Connection unavailable').hidden, false);
  assert.equal(failed.value, 'root');
  console.log('PASS: shared chooser search, tree navigation, keyboard activation, recent picks, cancellation, multi-select staging, chips, creation, and preserved values');
}
main().catch(error => {console.error(error); process.exitCode = 1;});
