/* Show the last committed link state without querying either device per row. */
var NotableSyncColumn = {
  revealPref: 'notable.syncColumn.firstShown',
  states: {
    synced: ['✓', 'Synced with reMarkable at the last check'],
    pending: ['↻', 'Waiting for the first completed sync'],
    paused: ['Ⅱ', 'reMarkable link paused'],
    attention: ['!', 'reMarkable link needs attention'],
  },
  byPaper: new Map(),
  register(pluginID) {
    this.key = Zotero.ItemTreeManager.registerColumn({
      pluginID, dataKey: 'remarkable-sync-status', label: 'reMarkable', enabledTreeIDs: ['main'],
      width: '100', staticWidth: true, zoteroPersist: ['width', 'hidden', 'sortDirection'],
      dataProvider: item => item.libraryID === Zotero.Libraries.userLibraryID && item.isRegularItem() && !item.deleted
        ? this.byPaper.get(item.key) || '' : '',
      renderCell: (_index, data, column, _first, doc) => {
        const cell = doc.createElement('span');
        cell.className = `cell ${column.className}`;
        const state = this.states[data];
        if (state) {
          cell.textContent = state[0];
          cell.title = state[1];
          cell.setAttribute('role', 'img');
          cell.setAttribute('aria-label', state[1]);
        }
        cell.style.textAlign = 'center';
        return cell;
      },
    });
    if (!this.key) Zotero.debug('reMarkable Sync: Zotero rejected registration of the status column.');
  },
  async reveal(window) {
    const key = this.key;
    if (!key || Zotero.Prefs.get(this.revealPref, true)) return;
    // Zotero applies the library view's column defaults after registration.
    // Wait for the real picker and preserve any visibility choices made later.
    for (let attempt = 0; attempt < 20; attempt++) {
      if (window.closed || this.key !== key || Zotero.Prefs.get(this.revealPref, true)) return;
      const columns = window.ZoteroPane?.itemsView?.tree?._columns;
      const index = columns?.getAsArray().findIndex(column => column.dataKey === key) ?? -1;
      if (index >= 0 && !columns.getAsArray()[index].disabled) {
        if (columns.getAsArray()[index].hidden) columns.toggleHidden(index);
        if (!columns.getAsArray()[index].hidden) Zotero.Prefs.set(this.revealPref, true, true);
        return;
      }
      await Zotero.Promise.delay(250);
    }
  },
  update(snapshot) {
    if (!this.key || !snapshot) return;
    const needsAttention = new Set([
      ...(snapshot.reviews || []).filter(row => row.kind === 'conflict' && row.payload?.type === 'paper'),
      ...(snapshot.deletions || []), ...(snapshot.local_deletions || []),
    ].map(row => row.payload?.cloud).filter(Boolean));
    for (const issue of snapshot.status?.result?.review || []) {
      if (issue.cloud && issue.kind !== 'keywords') needsAttention.add(issue.cloud);
    }
    const blocked = new Set(snapshot.blocked_collections || []);
    const byPaper = new Map();
    const priority = ['synced', 'pending', 'paused', 'attention'];
    for (const link of snapshot.links || []) {
      if (!link.parent) continue;
      const status = link.unavailable || blocked.has(link.collection) || needsAttention.has(link.cloud) ? 'attention'
        : link.paused ? 'paused' : link.revision && link.view_key ? 'synced' : 'pending';
      if (priority.indexOf(status) > priority.indexOf(byPaper.get(link.parent))) byPaper.set(link.parent, status);
    }
    if (byPaper.size === this.byPaper.size && [...byPaper].every(([key, value]) => this.byPaper.get(key) === value)) return;
    this.byPaper = byPaper;
    Zotero.ItemTreeManager.refreshColumns();
  },
  unregister() {
    if (this.key) Zotero.ItemTreeManager.unregisterColumn(this.key);
    this.key = null;
    this.byPaper.clear();
  },
};
