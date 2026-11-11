// Actual shared field helper, synthetic layout metrics, and explicit clipboard calls.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const frames = [], windowListeners = new Map(), copied = [];
const view = {requestAnimationFrame: callback => frames.push(callback), addEventListener: (name, callback) => windowListeners.set(name, callback)};
let document;
class Node {
  constructor(tag) {this.localName = tag; this.children = []; this.dataset = {}; this.style = {}; this.listeners = {}; this.attrs = {}; this.clientWidth = 200; this.scrollWidth = 200; this.scrollHeight = 72; this.selectionStart = 0; this.selectionEnd = 0; this.selectionDirection = 'none'; this.title = ''; this.isConnected = true; this.ownerDocument = document; this.classList = {add: value => {this.attrs.class = value;}}; this.textContent = '';}
  set value(value) {this.currentValue = this.localName === 'input' ? String(value).replace(/[\r\n]/g, '') : String(value);}
  get value() {return this.currentValue || '';}
  addEventListener(name, callback) {(this.listeners[name] ||= []).push(callback);}
  fire(name) {for (const callback of this.listeners[name] || []) callback({});}
  setAttribute(key, value) {this.attrs[key] = String(value);}
  setSelectionRange(start, end, direction) {this.selectionStart = start; this.selectionEnd = end; this.selectionDirection = direction;}
  all() {return this.children.flatMap(child => [child, ...child.all()]);}
  querySelector(selector) {return this.all().find(node => (node.attrs.class || '').split(' ').includes(selector.slice(1)));}
}
const root = new Node('body');
document = {defaultView: view, querySelectorAll: () => root.all().filter(node => node.dataset.notableField)}; view.document = document;
const context = vm.createContext({Zotero: {Utilities: {Internal: {copyTextToClipboard: text => copied.push(text)}}}});
vm.runInContext(fs.readFileSync('zotero-support/ui.js', 'utf8'), context);
const ui = context.NotableUI; ui.view = view;
ui.el = (tag, text, parent, attrs = {}) => {const node = new Node(tag); node.textContent = text; Object.assign(node.attrs, attrs); if (parent) {node.parentElement = parent; parent.children.push(node);} return node;};
ui.button = (text, parent, callback) => {const node = ui.el('button', text, parent); node.click = callback; return node;};
const title = ui.field('Title', root, 'A long title '.repeat(20), 'input', {wrap: true});
assert.equal(title.localName, 'textarea'); assert.equal(title.rows, 2); assert.equal(title.attrs.wrap, 'soft');
assert.equal(title.style.height, '74px');
title.value = 'A\r\n  B\nC'; title.setSelectionRange(5, 8, 'backward');
let drafted;
title.addEventListener('input', () => {drafted = title.value;}); title.fire('input');
assert.equal(title.value, 'A   B C'); assert.equal(drafted, title.value, 'Draft listeners receive a single logical title');
assert.equal(title.selectionStart, 4); assert.equal(title.selectionEnd, 7); assert.equal(title.selectionDirection, 'backward');
assert.equal(title.notableValueDetails, undefined);
const full = 'C:\\Users\\Research folder\\' + 'Long Unicode α folder\\'.repeat(8) + 'paper.pdf';
const path = ui.field('Watched ToSort folder', root, full);
path.scrollWidth = 900; path.notableRefreshValue();
assert.equal(path.title, full);
assert.equal(path.notableValueDetails.hidden, false);
assert.equal(path.notableValueDetails.dataset.reviewDetail, 'full-value:Watched ToSort folder');
const preview = path.notableValueDetails.querySelector('.readable-field-value');
assert.equal(preview.textContent, full);
assert.equal(preview.dataset.notableField, 'Watched ToSort folder full value');
assert.equal(copied.length, 0);
path.notableValueDetails.all().find(node => node.localName === 'button').click(); assert.equal(copied[0], full);
assert.equal(path.notableValueDetails.querySelector('.readable-field-copy-status').textContent, 'Copied.');
path.notableValueDetails.open = true;
path.value = 'C:\\Updated path'; path.notableRefreshValue();
assert.equal(path.notableValueDetails.open, true, 'A still-overflowing value keeps its preview expanded');
assert.equal(preview.textContent, path.value); assert.equal(path.title, path.value);
assert.equal(path.notableValueDetails.querySelector('.readable-field-copy-status').textContent, '');
path.notableValueDetails.open = true; path.scrollWidth = 200; path.fire('input');
assert.equal(path.notableValueDetails.hidden, true); assert.equal(path.notableValueDetails.open, false);
path.scrollWidth = 800; windowListeners.get('resize')(); assert.equal(path.notableValueDetails.hidden, false);
const filename = ui.field('Proposed filename', root, 'one\ntwo\r\nthree.pdf');
assert.equal(filename.value, 'onetwothree.pdf', 'Filename remains a native single-line control');
const code = ui.field('Single-use reMarkable pairing code', root, 'SENSITIVE', 'input', {sensitive: true});
assert.equal(code.title, ''); assert.equal(code.notableValueDetails, undefined); assert.equal(code.notableRefreshValue, undefined);
const select = ui.field('Hidden backing selector', root, '', 'select');
assert.equal(select.notableValueDetails, undefined);
for (const frame of frames) frame();
console.log('PASS: wrapping title normalization/caret/drafts, exact live value preview/copy, overflow visibility, programmatic refresh, resize, single-line filenames, and sensitive-field exclusions');
