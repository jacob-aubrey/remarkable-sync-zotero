/* Collect current Zotero annotations for the bundled Notable Word exporter. */
var NotableNotebook = {
  label: 'Generate Word notebook…',

  menuCollection({collectionTreeRows}) {
    const row = collectionTreeRows?.length === 1 ? collectionTreeRows[0] : null;
    return row?.isCollection() && !row.ref.deleted ? row.ref : null;
  },

  async exportCollection(ui, window, collection) {
    const includeChildren = {value: false};
    if (collection.hasChildCollections() && !Services.prompt.confirmCheck(window, this.label,
      `Export PDF annotations for ${collection.name}.`, 'Include subcollections', includeChildren)) return;
    return this.generate(ui, window, {collection, includeChildren: includeChildren.value});
  },

  selectionTargets(items) {
    const papers = new Set(items.filter(item => item.isRegularItem()).map(item => item.id));
    return [...new Map(items.filter(item => !item.isPDFAttachment() || !papers.has(item.parentID)).map(item =>
      [item.id, item.isPDFAttachment() && item.parentItem ? {item: item.parentItem, pdf: item} : {item}])).values()];
  },

  collectionItems(collection, includeChildren) {
    const pending = [collection], papers = new Map();
    for (const folder of pending) {
      for (const item of folder.getChildItems()) {
        if ((item.isRegularItem() || (item.isPDFAttachment() && !item.parentID)) && !notableReviewItemInTrash(item)) {
          papers.set(item.id, item);
        }
      }
      if (includeChildren) pending.push(...folder.getChildCollections());
    }
    return [...papers.values()];
  },

  async tagItems(ui, {tag, scope, showAutomatic, libraryID = Zotero.Libraries.userLibraryID, annotationTags = false}) {
    const items = await Zotero.Items.getAll(libraryID, false, false);
    const references = items.filter(item => item.isRegularItem() && !item.deleted);
    const matches = scope === 'page' ? [] : await notableMatchingPaperTags(references, [tag], 'all', showAutomatic);
    const targets = matches.map(({item}) => ({item}));
    if (annotationTags) {
      // Native tags can belong to a PDF or an individual reader annotation.
      // A reference tag includes its PDFs; an annotation tag includes only that annotation.
      const wholePapers = new Set(matches.map(({item}) => item.id));
      for (const pdf of items.filter(item => item.isPDFAttachment() && !notableReviewItemInTrash(item))) {
        if (wholePapers.has(pdf.parentID)) continue;
        await pdf.loadAllData();
        const item = pdf.parentItem || pdf;
        if (notableReviewItemInTrash(item)) continue;
        if (pdf.hasTag(tag)) targets.push({item, pdf});
        else {
          const annotations = pdf.getAnnotations().filter(note => !note.deleted);
          if (annotations.length) await Zotero.Items.loadDataTypes(annotations, ['tags']);
          if (annotations.some(note => note.hasTag(tag))) targets.push({item, pdf, annotationTag: tag});
        }
      }
    }
    if (scope === 'paper') return targets;
    if (libraryID !== Zotero.Libraries.userLibraryID) return targets;
    const allPages = new Set(matches.map(({item}) => item.key));
    const papers = new Map(references.map(item => [item.key, item]));
    const pdfs = new Map(items.filter(item => item.isPDFAttachment() && !notableReviewItemInTrash(item)).map(item => [item.key, item]));
    for (const link of ui.latest?.links || []) {
      const item = papers.get(link.parent), pdf = pdfs.get(link.view_key);
      if (!item || !pdf || link.unavailable || pdf.parentID !== item.id || allPages.has(item.key)) continue;
      if (!(link.page_map || []).some(page => link.page_tags?.[page]?.includes(tag))) continue;
      const context = await ui.readLocalReply('--local-check', {method: 'tag_context', params: {key: pdf.key, notebook_tag: tag}});
      if (context.parent_key !== item.key) {
        throw Error('A tagged PDF changed. Refresh Tags and try again.');
      }
      // The worker verifies the PDF digest and resolves the current tags.
      const pages = new Set(context.notebook_pages);
      if (pages.size) targets.push({item, pdf, pages});
    }
    return targets;
  },

  async annotations(pdf, pages, annotationTag) {
    const annotations = pdf.getAnnotations().filter(note => !note.deleted).flatMap(note => {
      if (annotationTag && !note.hasTag(annotationTag)) return [];
      const position = JSON.parse(note.annotationPosition || '{}');
      if (!Number.isInteger(position.pageIndex) || position.pageIndex < 0 || (pages && !pages.has(position.pageIndex))) return [];
      return [{note, page: position.pageIndex + 1}];
    });
    const hasImage = note => ['image', 'ink'].includes(note.annotationType);
    if (annotations.some(({note}) => hasImage(note))) {
      try { await Zotero.PDFWorker.renderAttachmentAnnotations(pdf.id, false); }
      catch (error) { Zotero.logError(error); }
    }
    const rows = [];
    for (const {note, page} of annotations) {
      const kind = note.annotationType, text = note.annotationText || '', comment = note.annotationComment || '';
      if (!text && !comment && !['highlight', 'underline', 'image', 'ink', 'strikeout', 'squiggly'].includes(kind)) continue;
      let image = '';
      if (hasImage(note)) {
        try {
          if (await Zotero.Annotations.hasCacheImage(note)) {
            const path = Zotero.Annotations.getCacheImagePath(note);
            image = (await Zotero.File.generateDataURI(path, 'image/png')).split(',')[1] || '';
          }
        } catch (error) { Zotero.logError(error); }
      }
      rows.push({page, kind, text, comment, image, label: note.annotationPageLabel || '', order: note.annotationSortIndex || ''});
    }
    return rows;
  },

  async collect(targets) {
    const sources = [], seen = new Set();
    for (const {item, pdf: selectedPDF, pages, annotationTag} of targets) {
      await item.loadAllData();
      if (notableReviewItemInTrash(item)) continue;
      const attachments = selectedPDF || item.isPDFAttachment() ? [selectedPDF || item] : await Zotero.Items.getAsync(item.getAttachments());
      const group = item.libraryID === Zotero.Libraries.userLibraryID ? null : Zotero.Groups.getByLibraryID(item.libraryID);
      for (const pdf of attachments) {
        if (!pdf.isPDFAttachment() || seen.has(pdf.id) || notableReviewItemInTrash(pdf) || (pdf !== item && pdf.parentID !== item.id)) continue;
        seen.add(pdf.id);
        await pdf.loadAllData();
        const annotations = await this.annotations(pdf, pages, annotationTag);
        if (!annotations.length) continue;
        sources.push({library: group ? `groups/${group.id}` : 'library', paper: item.key, attachment: pdf.key,
          title: item.getDisplayTitle(), creator: item.getField('firstCreator'), doi: item.getField('DOI'),
          filename: pdf.attachmentFilename || pdf.getDisplayTitle(), annotations});
      }
    }
    return sources;
  },

  async generate(ui, window, options) {
    if (this.running) throw Error('A Word notebook export is already running.');
    this.running = true;
    try {
      const {collection, includeChildren, tag, items} = options;
      const name = collection?.name || tag || (items.length === 1 ? items[0].getDisplayTitle() : 'selected papers');
      const title = `Notes on ${name}`;
      if (window === ui.view) ui.message('Collecting PDF annotations…');
      const targets = collection ? this.collectionItems(collection, includeChildren).map(item => ({item}))
        : items ? this.selectionTargets(items) : await this.tagItems(ui, options);
      const sources = await this.collect(targets);
      if (!sources.length) throw Error('The selection has no PDF annotations to export.');
      if (window.closed || ui.stopped) return;
      const suggested = title.replace(/[<>:"/\\|?*\x00-\x1f]/g, '_').slice(0, 160).trim().replace(/\.+$/, '') + '.docx';
      const file = await ui.pickLocalFile(window, {title: 'Save Word notebook', mode: 'modeSave', filter: 'Word document',
        suffix: '.docx', suggested, defaultExtension: 'docx'});
      if (!file || ui.stopped) return;
      if (!ui.helperLaunch) await ui.rpc('status');
      const result = await ui.readLocalReply('--local-check', {method: 'generate_notebook', params: {
        target: file.path, title, sources, replace: file.replace,
      }});
      const summary = `Saved ${result.annotations} ${result.annotations === 1 ? 'annotation' : 'annotations'} from ${result.papers} ${result.papers === 1 ? 'paper' : 'papers'} to ${file.path}.`;
      return result.backup ? `${summary}\nThe previous copy is at ${result.backup}.` : summary;
    } finally {
      this.running = false;
    }
  },
};
