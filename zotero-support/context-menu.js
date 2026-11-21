/* Selection-aware commands use Zotero's menus and the existing worker actions. */
var NotableContextMenu = {
  menuIDs: ['notable-context-items', 'notable-context-collection'],
  windows: new Map(),

  items(context) {
    const items = context.items || [];
    return items.length && items.every(item => !item.isFeedItem && !notableReviewItemInTrash(item)
      && (item.isRegularItem() || item.isPDFAttachment())) ? items : [];
  },
  links(ui, items) {
    return (ui.latest?.links || []).filter(link => items.some(item => item.libraryID === Zotero.Libraries.userLibraryID
      && (item.isRegularItem() ? link.parent === item.key : [link.source_key, link.view_key].includes(item.key))));
  },
  singleLink(ui, context) {
    const links = this.links(ui, this.items(context));
    return links.length === 1 ? links[0] : null;
  },
  editable(ui, context) {
    const items = this.items(context);
    return !!items.length && items.every(item => item.libraryID === Zotero.Libraries.userLibraryID && item.isEditable());
  },
  idle(ui) { return !ui.busy && !ui.pausing && !ui.changingFolder && !ui.stopped; },
  canSyncSelection(ui) { return (!ui.busy || ui.inboxChecking) && !ui.stopped && ui.canSync(); },
  window(context) { return context.menuElem.ownerGlobal; },
  async report(window, label, action, ui) {
    try {
      const result = await action();
      if (typeof result === 'string' && result) {
        if (ui?.view && !ui.view.closed) { ui.view.focus(); ui.message(result); }
        else Services.prompt.alert(window, label, result);
      }
    } catch (error) {
      Zotero.logError(error);
      if (ui?.view && !ui.view.closed) { ui.view.focus(); ui.message(error.message, true); }
      else Services.prompt.alert(window, label, error.message);
    }
  },
  command(ui, label, visible, action, enabled = () => true, confirmation) {
    return {menuType: 'menuitem', onShowing: (_event, context) => {
      context.menuElem.setAttribute('label', typeof label === 'function' ? label(context) : label);
      context.setVisible(!!visible(context));
      context.setEnabled(!ui.stopped && !!enabled(context));
    }, onCommand: (_event, context) => {
      // Resolve the current selection and link again; never retain menu snapshots.
      if (ui.stopped || !visible(context) || !enabled(context)) return;
      const title = typeof label === 'function' ? label(context) : label;
      return this.report(this.window(context), title, () => {
        if (confirmation) {
          const before = JSON.stringify([ui.filingConnection(), this.singleLink(ui, context)]);
          if (!Services.prompt.confirm(this.window(context), title, confirmation(context))) return;
          if (!visible(context) || !enabled(context) || before !== JSON.stringify([ui.filingConnection(), this.singleLink(ui, context)])) {
            throw Error('The paper or connection changed. Choose the action again to review its current state.');
          }
        }
        return action(context);
      }, ui);
    }};
  },
  async syncTargets(ui, window, links) {
    // Let Zotero dismiss the native menu before opening another window.
    await new Promise(resolve => setTimeout(resolve, 0));
    if (ui.stopped) return;
    if (!this.canSyncSelection(ui)) throw Error('Wait for the current operation to finish before syncing these PDFs.');
    const targets = [...new Map(links.map(link => [link.cloud, Object.fromEntries(
      ['cloud', 'parent', 'source_key', 'view_key'].map(key => [key, link[key] || '']))])).values()];
    if (!targets.length) throw Error('Choose a linked paper or PDF to sync.');
    const owner = window?.closed ? Zotero.getMainWindow() : window;
    const progress = new Zotero.ProgressWindow({window: owner, closeOnClick: false});
    progress.changeHeadline('reMarkable Sync');
    const selection = `${targets.length} selected ${targets.length === 1 ? 'PDF' : 'PDFs'}`;
    const line = new progress.ItemProgress('attachmentPDF',
      ui.inboxChecking ? `Waiting for the Inbox check before syncing ${selection}…` : `Syncing ${selection}…`);
    progress.show();
    try {
      const result = await ui.action('sync_selected', {targets, connection: JSON.parse(ui.filingConnection())},
        true, (detail, papers) => {
          line.setText(detail === 'Starting…' ? `Syncing ${selection}…` : detail.replace(/ \[tablet item [^\]]+\]/, ''));
          if (papers) line.setProgress(Math.min(99, Math.floor(100 * papers.checked / papers.total)));
        });
      if (!result) throw Error('Another operation started. Choose Sync again when it finishes.');
      line.setText(result.status?.message || 'Selected PDF sync finished.');
      line.setProgress(100);
      progress.startCloseTimer(6000);
      return result;
    } catch (error) {
      Zotero.logError(error);
      line.setText(error.message);
      line.setError();
      progress.startCloseTimer(12000);
    }
  },
  showLinks(ui, links) {
    Object.assign(ui.paperSearchState(), {view: 'linked', query: '', appliedQuery: '', waitingForEnter: false,
      reading: 'all', limit: 25, contextClouds: links.map(link => link.cloud)});
    ui.openContextTab('links');
  },
  async suggestionPDF(context) {
    const items = this.items(context);
    if (items.length !== 1) return null;
    const [item] = items;
    if (item.isPDFAttachment()) return item.parentID ? item : null;
    // Keep the native best-PDF choice, including the plugin's linked-copy choice.
    return (await item.getBestAttachments()).find(pdf => pdf.isPDFAttachment() && !notableReviewItemInTrash(pdf));
  },
  register(ui) {
    const selected = context => this.items(context);
    const one = context => this.singleLink(ui, context);
    const writable = context => this.editable(ui, context) && this.idle(ui);
    const linked = context => this.links(ui, selected(context));
    const availablePDF = context => one(context)?.view_key && !one(context).unavailable;
    const itemMenus = [
      this.command(ui, context => selected(context).every(item => item.isPDFAttachment())
        ? selected(context).length === 1 ? 'Sync this PDF now' : 'Sync selected PDFs now' : 'Sync selected papers now', context => {
        const items = selected(context);
        return items.length && items.every(item => this.links(ui, [item]).length);
      }, context => this.syncTargets(ui, this.window(context), linked(context)),
      context => this.editable(ui, context) && this.canSyncSelection(ui)),
      this.command(ui, NotableNotebook.label, context => selected(context).length, context =>
        NotableNotebook.generate(ui, this.window(context), {items: selected(context)}), () => !NotableNotebook.running),
      this.command(ui, 'Open reMarkable PDF', availablePDF, async context => {
        const link = one(context);
        const pdf = await Zotero.Items.getByLibraryAndKeyAsync(Zotero.Libraries.userLibraryID, link.view_key);
        if (!pdf?.isPDFAttachment() || notableReviewItemInTrash(pdf) || pdf.parentItem?.key !== link.parent) {
          throw Error('The linked PDF changed. Check the paper connection before opening it.');
        }
        await Zotero.Reader.open(pdf.id);
      }),
      this.command(ui, 'Show selected links…', context => linked(context).length, context => this.showLinks(ui, linked(context))),
      this.command(ui, 'Review paper keywords and suggested tags…', context => {
        const items = selected(context);
        return this.editable(ui, context) && items.length === 1 && (items[0].isPDFAttachment()
          ? items[0].parentID : items[0].getAttachments().length);
      }, async context => {
        const pdf = await this.suggestionPDF(context);
        if (!pdf) throw Error('This reference has no available PDF for keyword suggestions.');
        ui.tagEditor = null;
        ui.contextPaperTags = {key: pdf.key, title: pdf.parentItem?.getDisplayTitle() || pdf.getDisplayTitle()};
        ui.openContextTab('tags');
      }),
      this.command(ui, 'Recheck paper keywords', context => one(context)?.parent && !one(context).unavailable,
        async context => { ui.open(); await ui.action('recheck_keyword_warnings', {cloud: one(context).cloud}); },
        context => this.editable(ui, context) && !ui.pausing && !ui.changingFolder && (!ui.busy || ui.canCheckDuringSync())),
      this.command(ui, 'Check paper connection…', context => !!one(context), async context => {
        this.showLinks(ui, [one(context)]);
        await ui.previewLinkRepair(one(context).cloud);
      }, writable),
      this.command(ui, 'Pause paper link (keep both copies)', context => one(context) && !one(context).paused,
        async context => { ui.open(); await ui.action('link', {identity: one(context).cloud, paused: true}); }, writable,
        context => `Pause the link for “${one(context).name || 'this paper'}”?\n\nBoth copies will stay in place. Changes to this paper will stop syncing until you resume its link.`),
      this.command(ui, 'Resume paper link', context => one(context)?.paused && !one(context).unavailable,
        async context => { ui.open(); await ui.action('link', {identity: one(context).cloud, paused: false}); }, writable,
        context => `Resume the link for “${one(context).name || 'this paper'}”?\n\nFuture syncs can update its PDFs, tags, filenames, and locations on Zotero and reMarkable. The current edition will be checked before resuming.`),
      this.command(ui, 'Rebuild reMarkable PDF search index', availablePDF, async context => {
        ui.open(); await ui.action('reindex', {key: one(context).view_key});
      }, writable, context => `Rebuild the local search index for “${one(context).name || 'this PDF'}”?\n\nThis replaces its cached search text and may run OCR. The PDF and its annotations will stay in place.`),
      this.command(ui, 'Export source-linked annotations…', availablePDF, async context => {
        const link = one(context), connection = ui.filingConnection();
        const file = await ui.pickLocalFile(this.window(context), {title: 'Save source-linked annotations', mode: 'modeSave',
          filter: 'Markdown document', suffix: '.md', suggested: 'Zotero annotations.md', defaultExtension: 'md'});
        if (!file || ui.stopped) return;
        if (connection !== ui.filingConnection()) throw Error('The connection changed. Choose the PDF again.');
        ui.open();
        const result = await ui.maintenanceCall('annotations', {key: link.view_key, target: file.path});
        if (result) return `${result.annotations} annotations saved to ${result.path}.`;
      }, () => this.idle(ui)),
    ];
    const collection = context => NotableNotebook.menuCollection(context);
    const collectionLinks = context => {
      const rows = context.collectionTreeRows || [];
      if (!rows.every(row => row.isCollection() && !row.ref.deleted)) return [];
      return this.links(ui, rows.flatMap(row => NotableNotebook.collectionItems(row.ref, true)));
    };
    const collectionMenus = [
      this.command(ui, context => context.collectionTreeRows?.length > 1
        ? 'Sync selected collections and subcollections now' : 'Sync collection and subcollection papers now',
        context => collectionLinks(context).length,
        context => this.syncTargets(ui, this.window(context), collectionLinks(context)), () => this.canSyncSelection(ui)),
      this.command(ui, NotableNotebook.label, collection, context =>
        NotableNotebook.exportCollection(ui, this.window(context), collection(context)), () => !NotableNotebook.running),
      this.command(ui, 'Show collection links (including subcollections)…', context => collectionLinks(context).length,
        context => this.showLinks(ui, collectionLinks(context))),
    ];
    for (const [index, target, menus, visible] of [
      [0, 'main/library/item', itemMenus, context => selected(context).length],
      [1, 'main/library/collection', collectionMenus, context => collection(context) || collectionLinks(context).length],
    ]) {
      Zotero.MenuManager.registerMenu({menuID: this.menuIDs[index], pluginID: ui.id, target, menus: [{
        menuType: 'submenu', icon: 'chrome://notable/content/icons/tablet.svg',
        onShowing: (_event, context) => {
          context.menuElem.setAttribute('label', ui.shortName);
          context.setVisible(!!visible(context));
        }, menus: [...menus, {menuType: 'separator'},
          this.command(ui, 'Open reMarkable Sync…', () => true, () => ui.open()),
          this.command(ui, 'Sync setup…', () => true, () => ui.openSetup()),
        ],
      }]});
    }
    this.readerTagMenu = ({reader, params, append}) => {
      const node = reader._iframeWindow?.document.elementFromPoint(params.x, params.y)?.closest('#selector .tag, .annotation .tags .tag');
      const tag = node?.textContent;
      const pdf = Zotero.Items.get(reader.itemID);
      if (!pdf?.isPDFAttachment() || notableReviewItemInTrash(pdf)) return;
      const links = this.links(ui, [pdf]), commands = [];
      if (links.length) commands.push({label: 'Sync this PDF now', disabled: !this.canSyncSelection(ui),
        onCommand: () => this.report(reader._window, ui.shortName, () => this.syncTargets(ui, reader._window, this.links(ui, [pdf])), ui)});
      if (tag) commands.push({label: 'Generate Word notebook for this tag…',
        disabled: !!NotableNotebook.running, onCommand: () => this.exportTag(ui, reader._window, {
          tag, libraryID: pdf.libraryID, annotationTags: true,
        })});
      if (commands.length) append({label: ui.shortName, groups: [commands]});
    };
    for (const type of ['createSelectorContextMenu', 'createViewContextMenu', 'createAnnotationContextMenu', 'createThumbnailContextMenu']) {
      Zotero.Reader.registerEventListener(type, this.readerTagMenu, ui.id);
    }
  },
  exportTag(ui, window, {tag, libraryID, scope = 'paper', annotationTags = true}) {
    return this.report(window, NotableNotebook.label, () => NotableNotebook.generate(ui, window,
      {tag, libraryID, scope, showAutomatic: true, annotationTags}), ui);
  },
  attach(ui, window) {
    if (this.windows.has(window)) return;
    const doc = window.document;
    const nodes = new Set();
    let tagContext = null;
    const appendTagMenu = (popup, context) => {
      popup.querySelectorAll('.notable-tag-menu').forEach(node => node.remove());
      if (!context) return;
      const separator = doc.createXULElement('menuseparator');
      const menu = doc.createXULElement('menu');
      separator.className = menu.className = 'notable-tag-menu';
      menu.setAttribute('label', ui.shortName);
      const children = doc.createXULElement('menupopup');
      const contexts = context.tags ? context.tags.map(tag => ({tag, libraryID: context.libraryID})) : [context];
      for (const target of contexts) {
        const entry = doc.createXULElement('menuitem');
        entry.setAttribute('label', contexts.length === 1 ? 'Generate Word notebook for this tag…' : `Generate Word notebook for “${target.tag}”…`);
        entry.disabled = !!NotableNotebook.running;
        entry.addEventListener('command', () => this.exportTag(ui, window, target));
        children.append(entry);
      }
      menu.append(children); popup.append(separator, menu);
      nodes.add(separator); nodes.add(menu);
      for (const node of nodes) if (!node.isConnected) nodes.delete(node);
    };
    const openTagPopup = (event, context) => {
      event.preventDefault();
      event.stopPropagation();
      const popup = doc.createXULElement('menupopup');
      (doc.querySelector('popupset') || doc.documentElement).append(popup);
      nodes.add(popup);
      appendTagMenu(popup, context);
      popup.addEventListener('popuphidden', () => { popup.remove(); nodes.delete(popup); }, {once: true});
      popup.openPopupAtScreen(event.screenX, event.screenY, true);
    };
    const contextmenu = event => {
      tagContext = null;
      const target = event.target?.closest ? event.target : event.target?.parentElement;
      const nativeTag = target?.closest('.tag-selector-item');
      const box = target?.closest('tags-box');
      const tagRow = box && target.closest('[tagName]');
      const chip = target?.closest('[data-notable-tag]');
      const annotation = target?.closest('annotation-row .tags')?.closest('annotation-row')?.annotation;
      if (nativeTag) {
        // Capture the clicked tag before Zotero opens its existing tag popup.
        const libraryID = window.ZoteroPane?.getSelectedLibraryID();
        if (libraryID) tagContext = {tag: nativeTag.textContent, libraryID};
      } else if (tagRow && box.item) {
        const tag = tagRow.getAttribute('tagName');
        if (tag) tagContext = {tag, libraryID: box.item.libraryID};
      } else if (chip) {
        tagContext = {tag: chip.dataset.notableTag, libraryID: Zotero.Libraries.userLibraryID,
          scope: chip.dataset.notableTagScope || 'both', annotationTags: false};
        openTagPopup(event, tagContext);
      } else if (annotation && !notableReviewItemInTrash(annotation)) {
        const tags = annotation.getTags().map(row => row.tag);
        if (tags.length) openTagPopup(event, {tags, libraryID: annotation.libraryID});
      }
    };
    const showing = event => {
      const popup = event.target;
      if (popup.id === 'tag-menu' || (popup.localName === 'menupopup' && popup.id.endsWith('tags-context-menu'))) {
        appendTagMenu(popup, tagContext);
      }
    };
    doc.addEventListener('contextmenu', contextmenu, true);
    doc.addEventListener('popupshowing', showing, true);
    this.windows.set(window, {nodes, contextmenu, showing});
  },
  detach(window) {
    const state = this.windows.get(window);
    if (!state) return;
    window.document.removeEventListener('contextmenu', state.contextmenu, true);
    window.document.removeEventListener('popupshowing', state.showing, true);
    for (const node of state.nodes) node.remove();
    this.windows.delete(window);
  },
  unregister() {
    for (const id of this.menuIDs) Zotero.MenuManager.unregisterMenu(id);
    if (this.readerTagMenu) for (const type of ['createSelectorContextMenu', 'createViewContextMenu', 'createAnnotationContextMenu', 'createThumbnailContextMenu']) {
      Zotero.Reader.unregisterEventListener(type, this.readerTagMenu);
    }
    this.readerTagMenu = null;
    for (const window of this.windows.keys()) this.detach(window);
  },
};
