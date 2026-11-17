window.addEventListener('load', () => {
  const draft = window.arguments[0];
  try {
    Services.scriptloader.loadSubScript('chrome://zotero/content/customElements.js', window);
    window.ZoteroPane = Zotero.getMainWindow().ZoteroPane;
    const scroll = document.getElementById('citation-scroll');
    const info = document.createXULElement('info-box');
    scroll.appendChild(info);
    info.mode = 'edit';
    info.saveOnEdit = false;
    info.showTypeMenu = false;
    info.tabType = 'library';
    info.item = draft.item;
    info.open = true;
    info.collapsible = false;
    info.render();
    document.getElementById('citation-type').textContent = Zotero.ItemTypes.getLocalizedString(draft.item.itemTypeID);
    const resize = new ResizeObserver(() => {
      const height = Math.min(window.outerHeight + scroll.scrollHeight - scroll.clientHeight, screen.availHeight - 48);
      if (height <= window.outerHeight + 2) return;
      window.resizeTo(window.outerWidth, height);
      if (window.screenY + height > screen.availTop + screen.availHeight - 16) {
        window.moveTo(window.screenX, screen.availTop + screen.availHeight - height - 16);
      }
    });
    resize.observe(info);
    document.getElementById('citation-cancel').addEventListener('click', () => window.close());
    document.getElementById('citation-apply').addEventListener('click', () => {
      info.blurOpenField();
      draft.accepted = true;
      window.close();
    });
    window.addEventListener('keydown', event => {
      if (event.key === 'Escape' && !event.defaultPrevented) window.close();
    });
  } catch (error) {
    draft.error = 'Couldn’t open Zotero’s citation editor. ' + error.message;
    globalThis.Zotero?.logError(error);
    window.close();
  }
}, {once: true});
