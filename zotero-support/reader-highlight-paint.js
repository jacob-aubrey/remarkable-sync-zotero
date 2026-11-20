/* Paint managed external highlights once per color in the Zotero PDF view. */
var NotableReaderHighlightPaint = {
  views: new Map(),
  rendered: new WeakMap(),
  attach(reader) {
    if (!reader?.itemID) return;
    const attachment = reader._item ?? Zotero.Items.get(reader.itemID);
    if (!attachment?.isPDFAttachment() || !attachment.getNote().includes('Notable pilot:')) return;
    const internal = reader._internalReader;
    for (const view of [internal?._primaryView, internal?._secondaryView]) {
      if (view && typeof view._render === 'function' && Array.isArray(view._pages) && !this.views.has(view)) {
        this.wrapView(view);
      }
    }
  },
  wrapView(view) {
    for (const [oldView, state] of this.views) {
      if (oldView._iframeWindow?.closed) this.restore(oldView, state);
    }
    const state = {original: view._render, pages: new Map(), document: null, hints: {}};
    const paint = this;
    state.render = function (...args) {
      paint.updateView(this, state);
      return state.original.apply(this, args);
    };
    view._render = state.render;
    this.views.set(view, state);
    view._render();
  },
  updateView(view, state) {
    const document = view._iframeWindow?.PDFViewerApplication?.pdfDocument;
    if (document && document !== state.document && typeof document.getMetadata === 'function') {
      state.document = document;
      state.hints = {};
      Promise.resolve(document.getMetadata()).then(({info}) => {
        if (state.document !== document || view._render !== state.render) return;
        const hints = info?.Custom?.NotableHighlightPaints;
        if (typeof hints === 'string') {
          state.hints = this.readHints(hints);
          view._render();
        }
      }).catch(error => Zotero.logError(error));
    }
    const paint = this;
    for (const page of view._pages) {
      if (state.pages.has(page) || typeof page._pushHighlight !== 'function') continue;
      const original = page._pushHighlight;
      const wrapped = function (items, annotation) {
        return paint.highlight(this, original, items, annotation, state.hints);
      };
      state.pages.set(page, {original, wrapped});
      page._pushHighlight = wrapped;
    }
    for (const page of state.pages.keys()) {
      if (!view._pages.includes(page)) state.pages.delete(page);
    }
  },
  readHints(json) {
    const regions = value => Array.isArray(value) && value.every(rect =>
      Array.isArray(rect) && rect.length === 4 && rect.every(Number.isFinite));
    return Object.fromEntries(Object.entries(JSON.parse(json) || {}).filter(([, entries]) =>
      Array.isArray(entries) && entries.every(entry => entry && regions(entry.rects)
        && entry.colors && Object.entries(entry.colors).every(([color, rects]) =>
          /^#[0-9a-f]{6}$/i.test(color) && regions(rects)))));
  },
  highlight(page, original, items, annotation, hints) {
    const position = annotation.position;
    const pageIndex = page._pageIndex;
    const rects = position.pageIndex === pageIndex ? position.rects : position.nextPageRects;
    if (!annotation.isExternal || !rects?.length) return original.call(page, items, annotation);
    const cover = NotableAnnotationRefresh.covered.bind(NotableAnnotationRefresh);
    const choices = (hints?.[pageIndex] || []).filter(hint =>
      rects.every(rect => cover(rect, hint.rects)) && hint.rects.every(rect => cover(rect, rects)));
    const colors = choices.length === 1 ? choices[0].colors : {[annotation.color]: rects};
    if (!this.rendered.has(items)) this.rendered.set(items, new Map());
    const covered = this.rendered.get(items);
    for (const [color, regions] of Object.entries(colors)) {
      if (!covered.has(color)) covered.set(color, []);
      const painted = covered.get(color);
      const fresh = [];
      for (const rect of regions) {
        let parts = [rect];
        for (const previous of painted) {
          parts = parts.flatMap(part => this.subtract(part, previous));
          if (!parts.length) break;
        }
        painted.push(...parts);
        fresh.push(...parts);
      }
      if (fresh.length) original.call(page, items, {...annotation, color,
        position: {pageIndex, rects: fresh}});
    }
  },
  subtract(rect, other) {
    const [x0, y0, x1, y1] = rect;
    const left = Math.max(x0, other[0]), top = Math.max(y0, other[1]);
    const right = Math.min(x1, other[2]), bottom = Math.min(y1, other[3]);
    if (left >= right || top >= bottom) return [rect];
    return [[x0, y0, x1, top], [x0, bottom, x1, y1],
      [x0, top, left, bottom], [right, top, x1, bottom]]
      .filter(([a, b, c, d]) => c > a && d > b);
  },
  register(id) {
    this.onToolbar = ({reader}) => queueMicrotask(() => this.attach(reader));
    Zotero.Reader.registerEventListener('renderToolbar', this.onToolbar, id);
  },
  restore(view, state) {
    if (view._render === state.render) view._render = state.original;
    for (const [page, {original, wrapped}] of state.pages) {
      if (page._pushHighlight === wrapped) page._pushHighlight = original;
    }
    this.views.delete(view);
  },
  unregister() {
    Zotero.Reader.unregisterEventListener('renderToolbar', this.onToolbar);
    for (const [view, state] of this.views) this.restore(view, state);
  },
};
