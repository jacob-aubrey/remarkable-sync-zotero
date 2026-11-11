const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

// The record component's rendering/keyboard behavior has its own harness. Here
// the real controller keeps native option identity, metadata, and draft events.
function fixture() {
  const configs = [];
  class Event {constructor(type, details = {}) {this.type = type; Object.assign(this, details);}}
  const doc = {defaultView: {Event}};
  const node = (tag, text = '') => ({tag, textContent: text, children: [], dataset: {}, listeners: {}, ownerDocument: doc, isConnected: true,
    appendChild(child) {child.parentElement = this; this.children.push(child); return child;},
    replaceChildren() {this.children = [];},
    addEventListener(type, listener) {(this.listeners[type] ||= []).push(listener);},
    dispatchEvent(event) {for (const listener of this.listeners[event.type] || []) listener(event);},
    get options() {return this.children.filter(child => child.tag === 'option');},
    get selectedOptions() {const chosen = this.options.filter(option => option.selected); return chosen.length ? chosen : this.tag === 'select' && !this.multiple ? this.options.slice(0, 1) : [];},
    get value() {return this.tag === 'select' ? this.selectedOptions[0]?.value || '' : this._value || '';},
    set value(value) {if (this.tag === 'select') for (const option of this.options) option.selected = option.value === value; else this._value = value;},
  });
  doc.body = node('body');
  const context = vm.createContext({Zotero: {Libraries: {userLibraryID: 1}, Items: {}}, NotableFolderChooser: {create(config) {
    configs.push(config); const chooser = node('div'); chooser.value = config.value; chooser.choices = config.options;
    chooser.setOptions = options => {chooser.choices = options;}; config.parent.appendChild(chooser); return chooser;
  }}});
  vm.runInContext(fs.readFileSync('zotero-support/ui.js', 'utf8'), context);
  const ui = context.NotableUI;
  ui.el = (tag, text, parent, attrs = {}) => {const result = node(tag, text); Object.assign(result, attrs); parent?.appendChild(result); return result;};
  ui.field = (label, parent, value = '', type = 'input') => {const wrapper = ui.el('label', '', parent), input = ui.el(type, '', wrapper); input.value = value; input.dataset.notableField = label; return input;};
  ui.button = (label, parent, handler) => {const button = ui.el('button', label, parent); button.click = handler; return button;};
  ui.view = {document: doc, Event, closed: false};
  ui.render = () => {throw Error('Paper selection must preserve the current filing form');};
  return {ui, doc, configs, context};
}

async function main() {
  const longTitle = 'An exceptionally long scientific paper title with punctuation / and searchable descriptive content '.repeat(5);
  {
    const {ui, doc, configs} = fixture();
    const input = ui.paperSelect('Existing PDF', doc.body, [['', 'Create separate copy'], ['EXACT123', longTitle], ['OTHER234', longTitle]], 'EXACT123',
      {details: option => option.value ? option.value + '_full_attachment_filename.pdf' : ''});
    input.options[1].dataset.metadata = JSON.stringify({title: longTitle, creators: [{lastName: 'Researcher'}]});
    const originalOption = input.options[1], metadata = originalOption.dataset.metadata;
    assert.equal(input.parentElement.hidden, true);
    assert.equal(configs[0].mode, 'record');
    assert.equal(configs[0].options[1].label, longTitle, 'Display model keeps the entire title, including slash text');
    assert.equal(configs[0].options[1].parent, '', 'Paper titles do not become a folder hierarchy');
    assert.equal(configs[0].options[1].detail, 'EXACT123_full_attachment_filename.pdf');
    let inputs = 0, changes = 0;
    input.addEventListener('input', () => inputs++); input.addEventListener('change', () => changes++);
    input.notableRecordChooser.value = 'OTHER234'; input.notableRecordChooser.dispatchEvent({type: 'change'});
    assert.equal(input.value, 'OTHER234', 'Identical titles retain distinct stable record keys');
    assert.equal(inputs, 1); assert.equal(changes, 1, 'Selection reaches the existing draft/approve-label listeners once');
    input.value = 'EXACT123'; input.dispatchEvent({type: 'change'});
    assert.equal(input.notableRecordChooser.value, 'EXACT123');
    ui.el('option', 'A newly loaded paper', input, {value: 'LATER234'});
    input.notableRefreshRecords();
    assert.equal(input.options[1], originalOption, 'Refreshing display choices cannot replace metadata-bearing native options');
    assert.equal(input.selectedOptions[0].dataset.metadata, metadata);
    assert.equal(input.notableRecordChooser.choices.length, 4);
    assert.equal(input.notableRecordChooser.value, 'EXACT123');
  }
  {
    const {ui, doc, context} = fixture();
    const metadata = {key: 'PARENT23', title: longTitle, itemType: 'journalArticle', date: '2026', DOI: '10.1234/synthetic',
      creators: [{firstName: 'Ada', lastName: 'Researcher'}], publicationTitle: 'A Journal', extra: 'Retained citation data'};
    context.Zotero.Items.getAll = async () => [{key: metadata.key, deleted: false, isRegularItem: () => true,
      loadAllData: async () => {}, toJSON: () => metadata, getAttachments: () => []}];
    const picker = ui.supportingInformationPicker(doc.body, {main_reference: metadata.key});
    await picker.load();
    assert.equal(picker.select.value, metadata.key);
    assert.equal(picker.select.notableRecordChooser.value, metadata.key);
    assert.equal(picker.select.notableRecordChooser.choices[1].label, longTitle);
    const selected = picker.select.selectedOptions[0];
    const approvedMetadata = JSON.parse(selected.dataset.metadata);
    assert.equal(approvedMetadata.title, longTitle);
    assert.deepEqual(approvedMetadata.creators, metadata.creators);
    assert.equal(approvedMetadata.DOI, metadata.DOI, 'Supporting-information approval retains the selected paper citation');
    picker.select.notableRecordChooser.value = ''; picker.select.notableRecordChooser.dispatchEvent({type: 'change'});
    assert.equal(picker.select.value, '', 'The explicit create/separate/choose option remains available');
    assert.equal(selected.dataset.metadata, JSON.stringify(approvedMetadata));
  }
  console.log('PASS: full paper titles, stable exact-match keys, metadata-preserving main-paper loading, hidden native selection, and draft events');
}
main().catch(error => {console.error(error); process.exitCode = 1;});
