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
  assert.equal(refresh.matches({...right,
    annotationPosition: JSON.stringify({pageIndex: 3, rects: [[175, 20, 195, 30]]}),
  }, incoming), false);
  assert.equal(refresh.matches({
    ...right,
    annotationPosition: JSON.stringify({pageIndex: 4, rects: [[110, 20, 180, 30]]}),
  }, incoming), false);
  const originalImage = {
    annotationType: 'image',
    annotationComment: 'Handwritten note',
    annotationPosition: JSON.stringify({pageIndex: 3, rects: [[10, 20, 180, 130]]}),
  };
  const croppedImage = {type: 'image', comment: 'Handwritten note',
    position: {pageIndex: 3, rects: [[10, 20, 100, 65]]}};
  const otherImage = {type: 'image', comment: 'Handwritten note',
    position: {pageIndex: 3, rects: [[110, 80, 180, 130]]}};
  assert.equal(refresh.matches(originalImage, croppedImage), false);
  assert.equal(refresh.imageAnchor(originalImage, [otherImage, croppedImage], [{
    before: JSON.parse(originalImage.annotationPosition), after: croppedImage.position,
  }])[0], croppedImage);
  assert.equal(refresh.covered([10, 20, 80, 30], [[10, 20, 80, 24], [10, 24, 80, 30]]), true);
  vm.runInContext(fs.readFileSync('zotero-support/reader-highlight-paint.js', 'utf8'), context);
  const paint = context.NotableReaderHighlightPaint;
  const page = {_pageIndex: 3};
  const collect = (items, annotation) => items.push(annotation);
  const base = {isExternal: true, color: '#ffd600', position: {pageIndex: 3, rects: [[10, 20, 80, 30]]}};
  const items = [];
  paint.highlight(page, collect, items, base, {});
  paint.highlight(page, collect, items, {...base, position: {...base.position, rects: [[50, 20, 100, 30]]}}, {});
  assert.equal(JSON.stringify(items[1].position.rects), JSON.stringify([[80, 20, 100, 30]]));
  const mixed = [];
  paint.highlight(page, collect, mixed, base, {3: [{rects: base.position.rects,
    colors: {'#ffd600': base.position.rects, '#ff80bc': [[50, 20, 70, 30]]}}]});
  assert.equal(mixed.length, 2);
  assert.equal(mixed[1].color, '#ff80bc');
  console.log('PASS: retroactive refresh matches disjoint and different-color highlight passes in one bridge group.');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
