const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const attachment = {id: 17, isPDFAttachment: () => true};
const highlight = {pageIndex: 2, rects: [[10, 40, 32, 49], [10, 29, 29, 38]]};
const ink = {pageIndex: 2, width: 2, paths: [[1, 3, 5, 9]]};
let renderSidebar, button, destination;
const doc = {
  createElementNS() {
    return {style: {}, setAttribute() {}, append() {}, addEventListener(type, listener) { this[type] = listener; }};
  },
  defaultView: {addEventListener() {}, removeEventListener() {}},
  querySelectorAll: () => [],
};
const context = vm.createContext({Zotero: {
  Items: {get: id => id === attachment.id ? attachment : null},
  Reader: {registerEventListener(type, handler) {
    assert.equal(type, 'renderSidebarAnnotationHeader');
    renderSidebar = handler;
  }, unregisterEventListener(type, handler) { assert.equal(handler, renderSidebar); }},
  getMainWindow: () => null,
  getMainWindows: () => [],
  uiReadyPromise: Promise.resolve(),
}});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../zotero-support/annotation-jump.js'), 'utf8'), context);
const jump = context.NotableAnnotationJump;

(async () => {
  const target = jump.target({annotation: {isAnnotation: () => true, parentItemID: 17,
    annotationPosition: JSON.stringify(highlight)}});
  assert.equal(target.attachment, attachment);
  assert.equal(JSON.stringify(target.position), JSON.stringify(highlight));
  assert.equal(jump.target({annotation: {isAnnotation: () => false, parentItemID: 17}}), null);
  for (const invalid of ['broken JSON', {pageIndex: -1, rects: highlight.rects}, {pageIndex: 0, rects: []},
    {pageIndex: 0, width: 0, paths: ink.paths}, {pageIndex: 1, rects: [[1, 2, 3, Infinity]]}]) {
    assert.equal(jump.position(invalid), null);
  }

  jump.register('notable-companion@local.invalid');
  const reader = {type: 'pdf', navigate: location => { destination = location; }};
  renderSidebar({reader, doc, params: {annotation: {position: ink}}, append: value => { button = value; }});
  assert.equal(button.title, 'Show on page');
  await button.click({stopPropagation() {}});
  assert.equal(destination.position, ink);
  assert.equal(Object.hasOwn(destination, 'annotationID'), false, 'Native position must take precedence over selection');
  renderSidebar({reader: {type: 'epub'}, doc, params: {annotation: {position: ink}}, append: () => assert.fail('Unexpected EPUB button')});
  jump.unregister();
  assert.equal(jump.readerDocuments.size, 0);
  console.log('PDF annotation jump tests passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
