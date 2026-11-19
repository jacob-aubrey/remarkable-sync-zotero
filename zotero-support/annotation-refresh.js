/* Preserve annotation metadata and links when tablet passages/image regions join. */
var NotableAnnotationRefresh = {
  rectangles(annotation) {
    try {
      const value = typeof annotation.annotationPosition === 'string'
        ? JSON.parse(annotation.annotationPosition) : annotation.position;
      return Number.isInteger(value?.pageIndex) && Array.isArray(value.rects) ? value : null;
    } catch (_) { return null; }
  },
  matches(old, incoming) {
    const a = this.rectangles(old), b = this.rectangles(incoming);
    if (!a || !b || a.pageIndex !== b.pageIndex || old.annotationType !== incoming.type
        || old.annotationColor !== incoming.color) return false;
    const equal = (x, y) => x.length === 4 && y.length === 4 && x.every((v, i) => Math.abs(v - y[i]) < .05);
    if (incoming.type === 'highlight') return a.rects.every(x => b.rects.some(y => equal(x, y)));
    if (incoming.type !== 'image' || old.annotationComment !== 'Handwritten note'
        || incoming.comment !== 'Handwritten note' || a.rects.length !== 1 || b.rects.length !== 1) return false;
    const [x0, y0, x1, y1] = a.rects[0], [u0, v0, u1, v1] = b.rects[0];
    return u0 <= x0 + .05 && v0 <= y0 + .05 && u1 >= x1 - .05 && v1 >= y1 - .05;
  },
  async preserve(attachment) {
    const old = attachment.getAnnotations().filter(a => a.annotationIsExternal && !a.deleted
      && ['highlight', 'image'].includes(a.annotationType));
    if (!old.length) return;
    // Reuse the host's parser in preview mode. No item or PDF writes occur here.
    const preview = await Zotero.PDFWorker._enqueue(async () => {
      const bytes = await IOUtils.read(await attachment.getFilePathAsync());
      const buf = new Uint8Array(bytes).buffer;
      return Zotero.PDFWorker._query('pdf.importAnnotations', {
        buf, existingAnnotations: [], password: '', transfer: false,
      }, [buf]);
    }, true);
    const incoming = preview.imported;
    const memberships = new Map(old.map(a => [a, incoming.filter(b => this.matches(a, b))]));
    const plans = [];
    for (const target of incoming) {
      const members = old.filter(a => memberships.get(a).length === 1 && memberships.get(a)[0] === target);
      if (!members.length) continue;
      const exact = members.find(a => JSON.stringify(this.rectangles(a)) === JSON.stringify(target.position));
      if (members.length === 1 && exact) continue;
      if (members.some(a => (a.annotationComment || '') !== (target.comment || ''))) {
        throw Error('An existing annotation comment needs review before regrouping.');
      }
      members.sort((a, b) => a.annotationSortIndex.localeCompare(b.annotationSortIndex));
      plans.push({target, members, keeper: exact || members[0]});
    }
    if (!plans.length) return;
    const directory = PathUtils.join(Zotero.Profile.dir, 'notable', 'annotation-recovery');
    await IOUtils.makeDirectory(directory, {ignoreExisting: true});
    const journal = {schema: 1, attachment: attachment.key,
      before: [...new Set(plans.flatMap(plan => plan.members))].map(a => a.toJSON()),
      replacements: plans.map(plan => ({key: plan.keeper.key, oldKeys: plan.members.map(a => a.key), position: plan.target.position}))};
    const path = PathUtils.join(directory, attachment.key + '-' + crypto.randomUUID() + '.json');
    await IOUtils.writeJSON(path, journal, {tmpPath: path + '.tmp'});
    await Zotero.DB.executeTransaction(async () => {
      for (const {target, members, keeper} of plans) {
        keeper.annotationPosition = JSON.stringify(target.position);
        keeper.annotationSortIndex = target.sortIndex;
        if (target.type === 'highlight') keeper.annotationText = target.text;
        for (const member of members) {
          for (const tag of member.getTags()) {
            if (!keeper.hasTag(tag.tag) || (tag.type || 0) === 0) keeper.addTag(tag.tag, tag.type || 0);
          }
          if (member !== keeper) {
            keeper.addRelation('dc:replaces', Zotero.URI.getItemURI(member));
            for (const [predicate, objects] of Object.entries(member.getRelations())) {
              for (const object of objects) keeper.addRelation(predicate, object);
            }
          }
        }
        await keeper.save({skipSelect: true});
      }
    });
  },
  async resolve(itemID, location) {
    if (!/^[23456789ABCDEFGHIJKLMNPQRSTUVWXYZ]{8}$/.test(location?.annotationID || '')) return location;
    const attachment = await Zotero.Items.getAsync(itemID);
    if (!attachment?.isPDFAttachment() || !attachment.getNote().includes('Notable pilot:')) return location;
    const current = await Zotero.Items.getByLibraryAndKeyAsync(attachment.libraryID, location.annotationID);
    if (current && !current.deleted) return location;
    const uri = Zotero.URI.getLibraryURI(attachment.libraryID) + '/items/' + location.annotationID;
    const matches = (await Zotero.Relations.getByObject('item', uri)).filter(row => row.predicate === 'dc:replaces'
      && row.subject.parentItemID === attachment.id && !row.subject.deleted);
    return matches.length === 1 ? {...location, annotationID: matches[0].subject.key} : location;
  },
  register() {
    this.originalOpen = Zotero.Reader.open;
    const self = this;
    this.open = async function(itemID, location, ...args) {
      return self.originalOpen.call(this, itemID, await self.resolve(itemID, location), ...args);
    };
    Zotero.Reader.open = this.open;
  },
  unregister() {
    if (Zotero.Reader.open === this.open) Zotero.Reader.open = this.originalOpen;
  },
};
