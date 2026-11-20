const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

async function main() {
  const context = {};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync('zotero-support/annotation-refresh.js', 'utf8'), context);
  const refresh = context.NotableAnnotationRefresh;

  const left = {
    annotationType: 'highlight',
    annotationColor: '#ffed75',
    annotationPosition: JSON.stringify({pageIndex: 3, rects: [[10, 20, 80, 30]]}),
  };
  const bridge = {
    annotationType: 'highlight',
    annotationColor: '#ffc38c',
    annotationPosition: JSON.stringify({pageIndex: 3, rects: [[70, 20, 120, 30]]}),
  };
  const right = {
    annotationType: 'highlight',
    annotationColor: '#f29eff',
    annotationPosition: JSON.stringify({pageIndex: 3, rects: [[110, 20, 180, 30]]}),
  };
  const incoming = {
    type: 'highlight',
    color: '#ffed75',
    position: {pageIndex: 3, rects: [[10, 20, 180, 30]]},
  };

  assert.equal(refresh.matches(left, incoming), true);
  assert.equal(refresh.matches(bridge, incoming), true);
  assert.equal(refresh.matches(right, incoming), true);
  assert.equal(refresh.matches({
    ...right,
    annotationPosition: JSON.stringify({pageIndex: 4, rects: [[110, 20, 180, 30]]}),
  }, incoming), false);
  console.log('PASS: retroactive refresh matches disjoint and different-color highlight passes in one bridge group.');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
