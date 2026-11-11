/* A manual paper tag is shared with reMarkable by ordinary whole-paper sync. */
var NotableReadingStatus = {
  tag: 'Read',
  menuID: 'notable-reading-status',

  papers(items) {
    const papers = new Map();
    for (const item of items) {
      if (item.deleted) return [];
      const paper = item.isPDFAttachment() ? item.parentItem : item;
      if (!paper?.isRegularItem() || paper.isFeedItem || paper.deleted || !paper.isEditable()) return [];
      papers.set(paper.id, paper);
    }
    return [...papers.values()];
  },

  register(pluginID) {
    Zotero.MenuManager.registerMenu({
      menuID: this.menuID, pluginID, target: 'main/library/item',
      menus: [true, false].map(read => ({
        menuType: 'menuitem',
        onShowing: (_, context) => {
          context.menuElem.setAttribute('label', read ? 'Mark as read' : 'Mark as unread');
          context.setVisible(this.papers(context.items).some(paper => paper.hasTag(this.tag) !== read));
        },
        onCommand: async (_, context) => {
          const window = context.menuElem.ownerDocument.defaultView;
          try {
            await Zotero.DB.executeTransaction(async () => {
              for (const paper of this.papers(context.items)) {
                if (paper.hasTag(this.tag) === read) continue;
                if (read) paper.addTag(this.tag);
                else paper.removeTag(this.tag);
                await paper.save();
              }
            });
          } catch (error) {
            Zotero.logError(error);
            window.alert('Zotero could not save the reading status. Try again.');
          }
        },
      })),
    });
  },

  unregister() {
    Zotero.MenuManager.unregisterMenu(this.menuID);
  },
};
