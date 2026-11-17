/* Adapt Zotero's Info box to an unsaved Inbox citation. */
var NotableCitationDraft = {
  item(type, fields, title) {
    const item = new Zotero.Item(type);
    item.libraryID = Zotero.Libraries.userLibraryID;
    const names = this.names(item);
    const nativeFields = Object.fromEntries(Object.entries(fields).filter(([key]) => names.has(key) || key === 'creators'));
    item.fromJSON({itemType: type, ...nativeFields});
    const titleField = Zotero.ItemFields.getFieldIDFromTypeAndBase(item.itemTypeID, 'title') || 'title';
    item.setField(titleField, title);
    // Info box actions that commit immediately in the library may update this
    // transient item, but only Inbox approval may write a reference to Zotero.
    item.save = item.saveTx = async () => {};
    return item;
  },

  names(item) {
    return new Set(Zotero.ItemFields.getItemTypeFields(item.itemTypeID).map(id => Zotero.ItemFields.getName(id)));
  },

  fields(item, complete = false) {
    const data = item.toJSON();
    const titleField = Zotero.ItemFields.getFieldIDFromTypeAndBase(item.itemTypeID, 'title');
    const excluded = new Set(['title', Zotero.ItemFields.getName(titleField || Zotero.ItemFields.getID('title'))]);
    const fields = {};
    for (const key of this.names(item)) {
      if (!excluded.has(key) && (complete || key in data)) fields[key] = data[key] ?? '';
    }
    fields.creators = (data.creators || []).map(creator => 'name' in creator
      ? {creatorType: creator.creatorType, name: creator.name}
      : {creatorType: creator.creatorType, firstName: creator.firstName || '', lastName: creator.lastName || ''});
    return fields;
  },

  retarget(patch, type) {
    if (!patch || patch.item_type === type || Object.keys(patch.before).length) return patch;
    const item = this.item(patch.item_type, patch.fields, '');
    item.setType(type);
    return {item_type: type, fields: this.fields(item, true), before: {}};
  },

  async edit(ui, row, documentType, title, previous) {
    const parent = row.payload.parent ? await notableItem(row.payload.parent) : null;
    if (row.payload.parent && (!parent?.isRegularItem() || notableReviewItemInTrash(parent))) {
      throw Error('This Zotero reference is unavailable. Recheck Inbox before editing its citation.');
    }
    const seed = parent ? {item_type: Zotero.ItemTypes.getName(parent.itemTypeID), fields: this.fields(parent)}
      : await ui.readLocalReply('--local-check', {method: 'citation_draft', params: {identity: row.id, digest: row.digest, document_type: documentType}});
    const prior = this.retarget(previous, seed.item_type);
    const previouslyEdited = prior?.item_type === seed.item_type ? prior.fields : {};
    const item = this.item(seed.item_type, {...seed.fields, ...previouslyEdited}, title);
    const window = ui.view;
    if (!window || window.closed || ui.stopped) return;
    const args = {item, accepted: false};
    ui.citationDialogOpen = true;
    try {
      window.openDialog('chrome://notable/content/citation-details.xhtml', 'notable-citation-details',
        'chrome,dialog,modal,resizable,centerscreen', args);
    } finally {
      ui.citationDialogOpen = false;
    }
    if (args.error) throw Error(args.error);
    if (!args.accepted || window.closed) return;
    const edited = this.fields(item, true);
    const fields = {}, before = {};
    for (const [key, value] of Object.entries(edited)) {
      if (parent) {
        const original = seed.fields[key] ?? '';
        if (JSON.stringify(original) === JSON.stringify(value)) continue;
        before[key] = original;
      }
      fields[key] = value;
    }
    const patch = Object.keys(fields).length ? {item_type: seed.item_type, fields, before} : null;
    return {title: item.getField('title', false, true), patch};
  },
};
