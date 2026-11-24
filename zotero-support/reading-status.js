/* Manual paper tags are shared with reMarkable by ordinary whole-paper sync. */
var NotableReadingStatus = {
  menuID: 'notable-reading-status',

  tags(paper) {
    return paper.getTags().map(row => row.tag).filter(tag => /^(read|unread)$/i.test(tag));
  },

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
      menus: ['read', 'unread'].map(status => ({
        menuType: 'menuitem',
        onShowing: (_, context) => {
          context.menuElem.setAttribute('label', `Mark as ${status}`);
          context.setVisible(this.papers(context.items).some(paper => {
            const tags = this.tags(paper);
            return tags.length !== 1 || tags[0] !== status;
          }));
        },
        onCommand: async (_, context) => {
          const window = context.menuElem.ownerDocument.defaultView;
          try {
            await Zotero.DB.executeTransaction(async () => {
              for (const paper of this.papers(context.items)) {
                const tags = this.tags(paper);
                if (tags.length === 1 && tags[0] === status) continue;
                for (const tag of tags) if (tag !== status) paper.removeTag(tag);
                paper.addTag(status);
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
