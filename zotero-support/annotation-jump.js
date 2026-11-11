/* Use Zotero's PDF location navigator so zoom, rotation, and page labels stay native. */
var NotableAnnotationJump = {
  buttonClass: 'notable-annotation-jump',
  windows: new Map(),
  readerDocuments: new Map(),

  position(value) {
    let position;
    try { position = typeof value === 'string' ? JSON.parse(value) : value; }
    catch (_) { return null; }
    if (!Number.isInteger(position?.pageIndex) || position.pageIndex < 0) return null;
    const coordinates = (values, validLength) => Array.isArray(values) && values.length > 0
      && values.every(value => Array.isArray(value) && validLength(value.length) && value.every(Number.isFinite));
    if (coordinates(position.rects, length => length === 4)
      || (Number.isFinite(position.width) && position.width > 0
        && coordinates(position.paths, length => length >= 2 && length % 2 === 0))) return position;
    return null;
  },

  target(row) {
    const annotation = row.annotation;
    if (!annotation?.isAnnotation() || annotation.deleted || !annotation.parentItemID) return null;
    const attachment = Zotero.Items.get(annotation.parentItemID);
    const position = this.position(annotation.annotationPosition);
    return attachment?.isPDFAttachment() && !attachment.deleted && position ? {attachment, position} : null;
  },

  button(doc, command, window) {
    const html = 'http://www.w3.org/1999/xhtml';
    const button = doc.createElementNS(html, 'button');
    button.type = 'button';
    button.className = this.buttonClass;
    button.title = 'Show on page';
    button.setAttribute('aria-label', button.title);
    button.style.cssText = 'display: inline-grid; place-items: center; min-width: 22px; min-height: 22px; padding: 2px;';
    const icon = doc.createElementNS(html, 'span');
    icon.setAttribute('aria-hidden', 'true');
    icon.style.cssText = 'display: block; width: 14px; height: 14px; background: currentColor; mask: url("chrome://notable/content/icons/external-link.svg") center / contain no-repeat;';
    button.append(icon);
    button.addEventListener('focusin', event => event.stopPropagation());
    button.addEventListener('click', async event => {
      event.stopPropagation();
      button.disabled = true;
      try { await command(); }
      catch (error) {
        Zotero.logError(error);
        Services.prompt.alert(window, 'Show on page', 'Zotero could not open this PDF at the annotation.');
      }
      finally { button.disabled = false; }
    });
    return button;
  },

  attach(window) {
    const doc = window.document;
    const box = doc.getElementById('zotero-editpane-attachment-annotations');
    if (!box || this.windows.has(window)) return;
    const update = () => {
      for (const row of box.querySelectorAll('annotation-row')) {
        const action = row.querySelector('.action');
        if (!action || action.querySelector('.' + this.buttonClass) || !this.target(row)) continue;
        action.append(this.button(doc, async () => {
          const target = this.target(row);
          if (!target) return;
          const {attachment, position} = target;
          // An annotation ID selects the item and may skip the jump when it is
          // already visible. The native position route always scrolls and spotlights.
          const location = {position};
          if (!Zotero.Prefs.get('fileHandler.pdf') && window.ZoteroPane?.viewAttachment) {
            await window.ZoteroPane.viewAttachment(attachment.id, null, false, {location});
          } else {
            await Zotero.Reader.open(attachment.id, location, {openInWindow: Zotero.Prefs.get('openReaderInNewWindow')});
          }
        }, window));
      }
    };
    // Zotero 10's native attachment section has no per-row rendering hook.
    // Observe this section only and leave its elements and event handlers intact.
    const observer = new window.MutationObserver(update);
    observer.observe(box, {childList: true, subtree: true});
    this.windows.set(window, {box, observer});
    update();
  },

  detach(window) {
    const entry = this.windows.get(window);
    if (!entry) return;
    entry.observer.disconnect();
    for (const button of entry.box.querySelectorAll('.' + this.buttonClass)) button.remove();
    this.windows.delete(window);
  },

  register(pluginID) {
    this.renderSidebar = ({reader, doc, params, append}) => {
      const position = reader.type === 'pdf' && this.position(params.annotation?.position);
      if (!position) return;
      if (!this.readerDocuments.has(doc)) {
        const forget = () => this.readerDocuments.delete(doc);
        doc.defaultView.addEventListener('unload', forget, {once: true});
        this.readerDocuments.set(doc, forget);
      }
      append(this.button(doc, () => reader.navigate({position}), Zotero.getMainWindow()));
    };
    Zotero.Reader.registerEventListener('renderSidebarAnnotationHeader', this.renderSidebar, pluginID);
    Zotero.uiReadyPromise.then(() => {
      if (this.renderSidebar) for (const window of Zotero.getMainWindows()) this.attach(window);
    });
  },

  unregister() {
    if (this.renderSidebar) Zotero.Reader.unregisterEventListener('renderSidebarAnnotationHeader', this.renderSidebar);
    this.renderSidebar = null;
    for (const window of this.windows.keys()) this.detach(window);
    for (const [doc, forget] of this.readerDocuments) {
      doc.defaultView?.removeEventListener('unload', forget);
      for (const button of doc.querySelectorAll('.' + this.buttonClass)) button.remove();
    }
    this.readerDocuments.clear();
  },
};
