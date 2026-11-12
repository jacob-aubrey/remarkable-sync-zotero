/* Shared local chooser. Collection and cloud writes belong to the controller. */
var NotableFolderChooser = {
  serial: 0,
  create({document: doc, parent, label, options = [], value = '', multiple = false,
    recents = [], onRemember, onNew, mode = 'folder', search: buildSearch}) {
    const records = mode === 'record';
    const make = (tag, text, target, attrs = {}) => {
      const node = doc.createElementNS('http://www.w3.org/1999/xhtml', tag);
      if (text) node.textContent = text;
      for (const [key, item] of Object.entries(attrs)) node.setAttribute(key, String(item));
      target?.appendChild(node); return node;
    };
    const normalize = rows => rows.map(row => ({...row, value: String(row.value),
      label: String(row.label || row.path || (records ? 'Choose a paper' : 'Choose a folder')), path: String(row.path || row.label || ''),
      parent: row.parent == null ? '' : String(row.parent)}));
    let choices = normalize(options), selected = multiple ? [...value].map(String) : [String(value ?? '')];
    let staged = [...selected], focused = '', rows = [], expanded = new Set(), disabled = false;
    const host = make('div', '', parent, {class: 'folder-chooser'});
    const id = 'folder-chooser-' + ++this.serial;
    make('span', label, host, {id: id + '-label', class: 'folder-chooser-label'});
    const compact = make('div', '', host, {class: 'folder-chooser-compact'});
    const summary = make('div', '', compact, {class: 'folder-chooser-summary', 'aria-live': 'polite'});
    const change = make('button', 'Change…', compact, {type: 'button', 'aria-haspopup': 'dialog', 'aria-labelledby': id + '-label ' + id + '-change', id: id + '-change'});
    const dialog = make('dialog', '', host, {class: 'folder-chooser-dialog', 'aria-labelledby': id + '-title'});
    make('h2', label, dialog, {id: id + '-title'});
    const toolbar = make('div', '', dialog, {class: 'folder-chooser-toolbar'});
    const searchLabel = records ? 'Search papers' : 'Search folders';
    const control = buildSearch?.(toolbar, searchLabel, () => renderTree());
    const search = control?.input || make('input', '', toolbar, {type: 'search', placeholder: searchLabel, 'aria-label': searchLabel});
    const newButton = onNew ? make('button', 'New…', toolbar, {type: 'button'}) : null;
    const newParent = onNew ? make('p', '', dialog, {class: 'folder-chooser-hint'}) : null;
    const recentList = make('div', '', dialog, {class: 'folder-chooser-recents'});
    const count = make('p', '', dialog, {class: 'folder-chooser-hint', 'aria-live': 'polite'});
    const error = make('p', '', dialog, {class: 'folder-chooser-error', role: 'alert'});
    error.hidden = true;
    const tree = make('div', '', dialog, {class: 'folder-chooser-tree', role: records ? 'listbox' : 'tree', 'aria-label': label,
      'aria-multiselectable': String(multiple)});
    const footer = make('div', '', dialog, {class: 'folder-chooser-footer'});
    const cancel = make('button', 'Cancel', footer, {type: 'button'});
    const apply = multiple ? make('button', 'Use selected collections', footer, {type: 'button', class: 'primary'}) : null;
    const byValue = key => choices.find(row => row.value === key);
    const emit = () => {
      const EventType = doc.defaultView.Event;
      host.dispatchEvent(new EventType('input', {bubbles: true}));
      host.dispatchEvent(new EventType('change', {bubbles: true}));
    };
    const remember = keys => {
      for (const key of [...keys].reverse()) if (key) recents = [key, ...recents.filter(item => item !== key)].slice(0, 8);
      onRemember?.([...recents]);
    };
    const close = () => {
      if (dialog.open && typeof dialog.close === 'function') dialog.close();
      else dialog.removeAttribute('open');
      change.focus();
    };
    const renderSummary = () => {
      summary.replaceChildren();
      const picked = selected.map(key => byValue(key) || {value: key, label: key, path: key}).filter(row => row.value);
      if (!picked.length) make('span', multiple ? 'No collections selected' : byValue('')?.label || (records ? 'Choose a paper' : 'Choose a folder'), summary, {class: 'folder-chooser-hint'});
      for (const row of picked) {
        const chip = make('span', '', summary, {class: multiple ? 'folder-chooser-chip' : 'folder-chooser-selected', title: row.detail || row.path});
        make('span', row.path, chip);
        if (multiple) {
          const remove = make('button', '×', chip, {type: 'button', 'aria-label': 'Remove ' + row.path});
          remove.disabled = disabled;
          remove.addEventListener('click', () => {selected = selected.filter(key => key !== row.value); renderSummary(); emit();});
        }
        if (!multiple && row.detail && row.detail !== row.path) make('small', row.detail, chip, {class: 'folder-chooser-detail'});
      }
    };
    const commit = keys => {selected = [...keys]; remember(selected); renderSummary(); close(); emit();};
    const choose = key => {
      if (!multiple) {commit([key]); return;}
      staged = staged.includes(key) ? staged.filter(item => item !== key) : [...staged, key];
      focused = key; renderTree(); rows.find(item => item.option.value === key)?.node.focus();
    };
    const parentHint = () => {if (newParent) newParent.textContent = focused ? 'New inside: ' + (byValue(focused)?.path || focused) : 'Choose a parent folder, or create at the top level.';};
    const children = key => choices.filter(row => row.value && row.parent === key);
    const expandParents = key => {
      const seen = new Set();
      while (key && !seen.has(key)) {seen.add(key); const row = byValue(key); key = row?.parent; if (key) expanded.add(key);}
    };
    const focusAt = index => {const item = rows[Math.max(0, Math.min(rows.length - 1, index))]; item?.node.focus();};
    const renderTree = () => {
      tree.replaceChildren(); recentList.replaceChildren(); rows = [];
      const query = search.value.trim();
      const tokens = control?.terms(query) || query.toLocaleLowerCase().split(/\s+/).filter(Boolean);
      const matches = row => {
        const text = row.path + ' ' + (row.detail || '');
        return control ? control.matches(text, tokens) : tokens.every(token => text.toLocaleLowerCase().includes(token));
      };
      const visible = [], visited = new Set();
      const walk = (row, depth) => {
        if (visited.has(row.value)) return;
        visited.add(row.value); visible.push({row, depth});
        if (expanded.has(row.value)) for (const child of children(row.value)) walk(child, depth + 1);
      };
      if (query) {
        for (const row of choices.filter(matches)) visible.push({row, depth: 1});
      } else {
        const values = new Set(choices.map(row => row.value).filter(Boolean));
        for (const row of choices.filter(row => !row.parent || !values.has(row.parent))) walk(row, 1);
      }
      for (const {row, depth} of visible) {
        const hasChildren = !records && !!row.value && !query && children(row.value).length > 0;
        const selectedHere = staged.includes(row.value);
        const siblings = visible.filter(entry => query || entry.row.parent === row.parent);
        const item = make('div', '', tree, {role: records ? 'option' : 'treeitem', tabindex: rows.length ? '-1' : '0',
          'aria-level': depth, 'aria-selected': String(selectedHere), class: 'folder-chooser-option',
          'aria-posinset': siblings.findIndex(entry => entry.row.value === row.value) + 1, 'aria-setsize': siblings.length,
          'aria-label': row.path || row.label, 'data-value': row.value});
        if (records) item.removeAttribute('aria-level');
        item.style.paddingInlineStart = (8 + (depth - 1) * 18) + 'px';
        if (hasChildren) {
          item.setAttribute('aria-expanded', String(expanded.has(row.value)));
          const expand = make('button', expanded.has(row.value) ? '▾' : '▸', item,
            {type: 'button', tabindex: '-1', class: 'folder-chooser-expand', 'aria-label': (expanded.has(row.value) ? 'Collapse ' : 'Expand ') + row.label});
          expand.addEventListener('click', event => {
            event.stopPropagation(); focused = row.value;
            expanded.has(row.value) ? expanded.delete(row.value) : expanded.add(row.value);
            renderTree(); rows.find(entry => entry.option.value === row.value)?.node.focus();
          });
        } else if (!records) make('span', '', item, {class: 'folder-chooser-spacer', 'aria-hidden': 'true'});
        if (multiple) make('span', selectedHere ? '☑' : '☐', item, {'aria-hidden': 'true', class: 'folder-chooser-check'});
        const text = make('span', '', item, {class: 'folder-chooser-option-text'});
        make('strong', row.label, text);
        const parentPath = row.path.split(' / ').slice(0, -1).join(' / ');
        if (parentPath && !records) make('small', parentPath, text, {class: 'folder-chooser-detail'});
        if (row.detail && row.detail !== row.path) make('small', row.detail, text, {class: 'folder-chooser-detail'});
        item.addEventListener('focus', () => {
          focused = row.value;
          for (const entry of rows) entry.node.tabIndex = entry.node === item ? 0 : -1;
          parentHint();
        });
        item.addEventListener('click', () => choose(row.value));
        item.addEventListener('keydown', event => {
          const index = rows.findIndex(entry => entry.node === item);
          if (['ArrowDown', 'ArrowUp', 'Home', 'End', 'ArrowRight', 'ArrowLeft', 'Enter', ' '].includes(event.key)) event.preventDefault();
          if (event.key === 'ArrowDown') focusAt(index + 1);
          else if (event.key === 'ArrowUp') focusAt(index - 1);
          else if (event.key === 'Home') focusAt(0);
          else if (event.key === 'End') focusAt(rows.length - 1);
          else if (event.key === 'Enter' || event.key === ' ') choose(row.value);
          else if (event.key === 'ArrowRight' && hasChildren) {
            if (!expanded.has(row.value)) {expanded.add(row.value); renderTree(); rows.find(entry => entry.option.value === row.value)?.node.focus();}
            else focusAt(index + 1);
          } else if (event.key === 'ArrowLeft' && !query) {
            if (expanded.has(row.value)) {expanded.delete(row.value); renderTree(); rows.find(entry => entry.option.value === row.value)?.node.focus();}
            else rows.find(entry => entry.option.value === row.parent)?.node.focus();
          } else if (event.key.length === 1 && event.key !== ' ' && !event.ctrlKey && !event.metaKey && !event.altKey) {
            event.preventDefault(); search.value += event.key; search.focus(); updateSearch();
          }
        });
        rows.push({node: item, option: row});
      }
      if (rows.some(entry => entry.option.value === focused)) {
        for (const entry of rows) entry.node.tabIndex = entry.option.value === focused ? 0 : -1;
      }
      count.textContent = query ? visible.length + (records ? ' matching papers' : ' matching folders') : records ? 'Browse papers' : 'Browse folders';
      if (!visible.length) make('p', records ? 'No matching papers. Try a different title.' : 'No matching folders. Try a different name, or create one.', tree, {class: 'folder-chooser-hint'});
      if (!query) {
        const recent = recents.map(byValue).filter(Boolean).slice(0, 5);
        if (recent.length) make('strong', 'Recent destinations', recentList);
        for (const row of recent) {
          const button = make('button', row.label, recentList, {type: 'button', title: row.path});
          button.addEventListener('click', () => choose(row.value));
        }
      }
      parentHint();
    };
    const open = () => {
      if (disabled || dialog.open) return;
      staged = [...selected]; focused = selected.find(key => byValue(key)) || '';
      error.hidden = true;
      for (const key of selected) expandParents(key);
      search.value = ''; updateSearch();
      if (typeof dialog.showModal === 'function') dialog.showModal(); else dialog.setAttribute('open', '');
      search.focus();
    };
    change.addEventListener('click', open);
    cancel.addEventListener('click', close);
    apply?.addEventListener('click', () => commit(staged));
    dialog.addEventListener('cancel', event => {event.preventDefault(); close();});
    dialog.addEventListener('keydown', event => {if (event.key === 'Escape') {event.preventDefault(); close();}});
    if (!control) search.addEventListener('input', renderTree);
    const updateSearch = () => search.dispatchEvent(new doc.defaultView.Event('input'));
    search.addEventListener('keydown', event => {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {event.preventDefault(); focusAt(event.key === 'ArrowUp' ? rows.length - 1 : 0);}
      else if (event.key === 'Enter' && rows.length === 1) {event.preventDefault(); choose(rows[0].option.value);}
    });
    newButton?.addEventListener('click', async () => {
      const parentValue = focused, previousDraft = [...staged]; close();
      const restoreDraft = () => {open(); staged = previousDraft; renderTree();};
      try {
        const result = await onNew(parentValue);
        if (!result) {if (multiple) restoreDraft(); return;}
        choices = normalize([...choices.filter(row => row.value !== String(result.value)), result]);
        if (multiple) {
          restoreDraft(); staged = [...new Set([...previousDraft, String(result.value)])];
          focused = String(result.value); expandParents(focused); renderTree();
        }
        else commit([String(result.value)]);
      } catch (reason) {
        restoreDraft(); error.textContent = reason.message || String(reason); error.hidden = false;
      }
    });
    Object.defineProperties(host, {
      value: {get: () => multiple ? selected[0] || '' : selected[0] || '', set: next => {selected = multiple && Array.isArray(next) ? next.map(String) : [String(next ?? '')]; renderSummary();}},
      selectedOptions: {get: () => selected.map(key => byValue(key)).filter(Boolean).map(row => ({value: row.value, textContent: row.path, selected: true}))},
      options: {get: () => choices.map(row => ({value: row.value, textContent: row.path, selected: selected.includes(row.value)}))},
      disabled: {get: () => disabled, set: next => {disabled = !!next; change.disabled = disabled; if (newButton) newButton.disabled = disabled; renderSummary();}},
      multiple: {value: multiple},
    });
    host.setOptions = next => {choices = normalize(next); renderSummary(); if (dialog.open) renderTree();};
    host.open = open; host.close = close;
    renderSummary(); return host;
  },
};
