/* Zotero owns this interface and the lifetime of the bundled worker. */
var NotableUI = {
  // Display names may change; the extension ID, profile directory, API paths
  // and managed-attachment markers must keep their existing identities.
  name: 'reMarkable Sync for Zotero', shortName: 'reMarkable Sync',
  id: 'notable-companion@local.invalid', process: null, pending: new Map(), serial: 0,
  view: null, latest: null, busy: false, timer: null, windows: new Set(),
  filingQueue: [], reviewDrafts: new Map(), tagDrafts: new Map(), linkDrafts: new Map(),
  inboxSortOptions: [['added-asc', 'Date added · Oldest first'], ['added-desc', 'Date added · Newest first'],
    ['title-asc', 'Title · A–Z'], ['title-desc', 'Title · Z–A']],
  inboxTitleOrder: new Intl.Collator(undefined, {numeric: true, sensitivity: 'base'}),
  inboxSort: 'added-asc', inboxFilter: 'all',
  recentTags: [], tagEdits: [], afterSync: [], syncDecisions: [],
  async start(rootURI) {
    this.stopped = false;
    this.rootURI = rootURI;
    const inboxSort = Zotero.Prefs.get('notable.inboxSort', true);
    if (this.inboxSortOptions.some(([value]) => value === inboxSort)) this.inboxSort = inboxSort;
    try {
      const tags = JSON.parse(Zotero.Prefs.get('notable.recentTags', true) || '[]');
      if (Array.isArray(tags)) this.recentTags = [...new Set(tags.filter(tag => typeof tag === 'string' && tag.trim()))].slice(0, 8);
    } catch (_) {} // A stale preference cannot prevent the tag editor from opening.
    this.scheduleInbox();
    Zotero.Notable = this;
    await this.registerReaderTagButton(rootURI);
    this.collectionObserver = Zotero.Notifier.registerObserver({notify: (event, type) => {
      if (type === 'collection') this.refreshCollectionNames();
      this.observeReviewChange(event, type);
      if (type === 'setting' && event === 'modify' && this.tab === 'tags') {
        this.paperTagEvidenceSerial = (this.paperTagEvidenceSerial || 0) + 1;
        this.scheduleSearchRefresh();
      }
    }}, ['collection', 'item', 'collection-item', 'item-tag', 'tag', 'trash', 'setting'], this.id);
    this.statePath = PathUtils.join(Zotero.Profile.dir, 'notable');
    await IOUtils.makeDirectory(this.statePath, {ignoreExisting: true});
    this.folderRecents = {};
    try {
      const recent = JSON.parse(await IOUtils.readUTF8(PathUtils.join(this.statePath, 'folder-recents.json')));
      for (const kind of ['zotero', 'tablet', 'paired']) {
        if (Array.isArray(recent[kind])) this.folderRecents[kind] = recent[kind].filter(key => typeof key === 'string').slice(0, 6);
      }
    } catch (_) {} // Missing preferences must not prevent startup.
    const queuePath = PathUtils.join(this.statePath, 'filing-queue.json');
    if (await IOUtils.exists(queuePath)) {
      try {
        const saved = JSON.parse(await IOUtils.readUTF8(queuePath));
        if (!Array.isArray(saved.jobs) || saved.jobs.some(job => !this.validFilingJob(job))
          || new Set(saved.jobs.map(job => job.id)).size !== saved.jobs.length) throw Error('Invalid filing queue');
        this.filingQueue = saved.jobs;
        // Restoring approvals never starts transfers without a visible resume.
        this.filingQueuePaused = !!saved.jobs.length;
      } catch (_) { this.queueError = 'The saved filing queue could not be read. Your papers remain pending.'; }
    }
    const tagPath = PathUtils.join(this.statePath, 'tag-edits.json');
    if (await IOUtils.exists(tagPath)) {
      try {
        const saved = JSON.parse(await IOUtils.readUTF8(tagPath));
        if (!Array.isArray(saved) || !saved.every(job => job.edit_id && job.connection && job.page_id
            && Array.isArray(job.paper?.before) && Array.isArray(job.paper?.after)
            && Array.isArray(job.page?.before) && Array.isArray(job.page?.after))) throw Error('Invalid tag edit queue');
        this.tagEdits = saved;
      } catch (_) { this.tagEditError = 'The saved tag edits could not be read.'; this.tagEditsUnreadable = true; }
    }
    const decisionsPath = PathUtils.join(this.statePath, 'sync-decisions.json');
    if (await IOUtils.exists(decisionsPath)) {
      try {
        const saved = JSON.parse(await IOUtils.readUTF8(decisionsPath));
        if (!Array.isArray(saved) || saved.some(job => !job?.key || !job.connection || !job.params
            || !['confirm_folder_change', 'delete_decision', 'local_delete_decision'].includes(job.method))
            || new Set(saved.map(job => job.key)).size !== saved.length) throw Error('Invalid sync decision queue');
        this.syncDecisions = saved;
      } catch (_) { this.syncDecisionReadError = 'Saved sync decisions could not be read. Confirmations are on hold.'; }
    }
    for (const [target, label] of [['main/menubar/tools', this.shortName], ['main/library/item', this.name + '…']]) {
      Zotero.MenuManager.registerMenu({menuID: 'notable-' + target.replaceAll('/', '-'), pluginID: this.id, target,
        menus: [{menuType: 'menuitem', icon: target === 'main/menubar/tools' ? 'chrome://notable/content/icons/tablet.svg' : undefined,
          onShowing: (event, {menuElem}) => menuElem.setAttribute('label', label),
          onCommand: () => this.open()}]});
    }
    // Startup is awaited while Zotero initializes plugins, before its first
    // normal window can finish loading. Do not block that chain on UI ready.
    Zotero.uiReadyPromise.then(async () => {
      if (this.stopped) return;
      this.refreshPaperTagBank();
      for (const window of Zotero.getMainWindows()) this.attach(window);
      const configPath = PathUtils.join(this.statePath, 'config.json');
      if (await IOUtils.exists(configPath)) {
        try { await this.refresh(); }
        catch (_) {
          if (!this.latest) {
            try {
              const config = JSON.parse(await IOUtils.readUTF8(configPath));
              this.latest = {config, status: {}, reviews: [], deletions: [], links: [], types: {}};
            }
            catch (_) { return; }
          }
        }
        this.schedule(5000);
        this.drainTagEdits();
        this.drainSyncDecisions();
      }
    });
  },
  async registerReaderTagButton(rootURI) {
    const icon = await (await fetch(rootURI + 'icons/tag.svg')).text();
    if (this.stopped) return;
    this.readerToolbar = ({reader, doc, append}) => {
      if (reader.type !== 'pdf' || doc.querySelector('.notable-page-tag')) return;
      const button = doc.createElement('button');
      button.type = 'button';
      button.className = 'toolbar-button notable-page-tag';
      button.tabIndex = -1;
      button.title = 'Edit paper and page tags';
      button.setAttribute('aria-label', button.title);
      // Use the bundled SVG inline so its stroke takes the reader's toolbar color.
      button.innerHTML = icon;
      const svg = button.firstElementChild;
      svg.setAttribute('width', '16');
      svg.setAttribute('height', '16');
      svg.setAttribute('aria-hidden', 'true');
      button.addEventListener('click', async () => {
        button.disabled = true;
        try { await this.openPageTags(reader); }
        catch (error) { Zotero.alert(reader._iframeWindow, button.title, error.message); }
        finally { button.disabled = false; }
      });
      append(button);
    };
    Zotero.Reader.registerEventListener('renderToolbar', this.readerToolbar, this.id);
    // A hot update leaves existing readers mounted, so they may not emit renderToolbar again.
    for (const reader of Zotero.Reader._readers) {
      const doc = reader._iframeWindow?.document;
      const sections = doc?.querySelector('.toolbar .end > .custom-sections');
      if (!sections) continue;
      this.readerToolbar({reader, doc, append: button => {
        const section = doc.createElement('div');
        section.className = 'section';
        section.append(button);
        sections.append(section);
      }});
    }
  },
  recordTransport(event, details = {}) {
    try {
      if (!this.statePath) return;
      const path = PathUtils.join(this.statePath, 'sync-transport.jsonl');
      const clean = value => {
        if (typeof value === 'string') return value.replace(/https?:\/\/\S+/g, '<url>')
          .replace(/(?:bearer\s+\S+|(?:token|password|api[_-]?key|secret)\s*[=:]\s*\S+)/gi, '<redacted>')
          .replace(/[A-Za-z]:[\\/][^\s"\n]+/g, '<path>').slice(0, 400);
        if (Array.isArray(value)) return value.slice(0, 12).map(clean);
        if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).slice(0, 24)
          .map(([key, item]) => [key, /^(token|headers|authorization|body|payload|text|content|params)$/i.test(key) ? '<redacted>' : clean(item)]));
        return value;
      };
      const record = JSON.stringify({time: new Date().toISOString(), event, ...clean(details)}) + '\n';
      const recordBytes = typeof TextEncoder === 'function' ? new TextEncoder().encode(record).length : record.length * 3;
      this.transportLog = (this.transportLog || Promise.resolve()).catch(() => {}).then(async () => {
        let size = this.transportBytes || 0;
        try { if (IOUtils.stat) size = (await IOUtils.stat(path)).size; } catch (_) {}
        if (size + recordBytes > (this.transportMaxBytes || 2 * 1024 * 1024)) {
          try { await IOUtils.move(path + '.1', path + '.2'); } catch (error) { if (error.name !== 'NotFoundError') throw error; }
          await IOUtils.move(path, path + '.1'); size = 0;
        }
        await IOUtils.writeUTF8(path, record, {mode: 'appendOrCreate', flush: true});
        this.transportBytes = size + recordBytes;
      }).catch(() => {}); // A failed diagnostic write must not reject an RPC.
      return this.transportLog;
    } catch (_) {}
  },
  processTrace(process) {
    return this.processTraces?.get(process) || {};
  },
  attach(window) {
    if (this.stopped) return;
    if (this.windows.has(window)) return;
    const toolbar = window.document.getElementById('zotero-tabs-toolbar');
    if (!toolbar) return;
    const button = window.document.createXULElement('toolbarbutton');
    button.id = 'notable-open'; button.setAttribute('label', 'reMarkable');
    button.setAttribute('aria-label', 'Open reMarkable Sync');
    button.style.cssText = 'margin-inline: 8px; padding-inline: 8px; list-style-image: url("chrome://notable/content/icons/tablet.svg");';
    button.setAttribute('tooltiptext', 'Open reMarkable Sync — folders, review, and settings');
    button.addEventListener('command', () => {
      if ((!this.view || this.view.closed) && this.syncReviewAvailable()) this.requestedTab = 'sync-review';
      this.open();
    });
    toolbar.appendChild(button); this.windows.add(window);
    if (this.latest) this.badge();
  },
  detach(window) { window.document.getElementById('notable-open')?.remove(); this.windows.delete(window); },
  async stop() {
    this.stopped = true;
    this.cancelAfterSync('The plugin closed before this action could start.');
    this.hideButtonHelp();
    Zotero.Reader.unregisterEventListener('renderToolbar', this.readerToolbar);
    if (this.collectionObserver) Zotero.Notifier.unregisterObserver(this.collectionObserver);
    this.collectionObserver = null;
    for (const reader of Zotero.Reader._readers) {
      reader._iframeWindow?.document.querySelector('.notable-page-tag')?.parentElement.remove();
    }
    clearTimeout(this.timer);
    clearInterval(this.activityTimer);
    clearTimeout(this.inboxTimer);
    clearTimeout(this.paperTagTimer);
    clearTimeout(this.searchRefreshTimer);
    clearTimeout(this.paperTagBankTimer);
    this.reviewObserver?.disconnect();
    this.view?.close(); this.view = null;
    for (const window of this.windows) this.detach(window);
    for (const target of ['main-menubar-tools', 'main-library-item']) Zotero.MenuManager.unregisterMenu('notable-' + target);
    // A launch which is already awaiting Subprocess.call must observe stopped
    // and finish its child before replacement can create another writer.
    if (this.launching) { try { await this.launching; } catch (_) {} }
    if (this.process) await this.closeProcess(this.process, true, 'plugin_shutdown');
    for (const value of this.pending.values()) { clearTimeout(value.timer); value.reject(new Error(this.shortName + ' stopped')); }
    this.pending.clear();
    if (Zotero.Notable === this) delete Zotero.Notable;
  },
  async closeProcess(process, graceful = true, reason = 'parent_requested') {
    let timeout;
    const info = this.processTrace(process);
    if (!info.exited) {
      info.terminationRequested = true; info.reason ||= reason;
      this.recordTransport('parent_termination_requested', {helper_id: info.id, reason: info.reason, graceful});
    }
    try {
      if (graceful) {
        await Promise.race([(async () => {
          await process.stdin.write(JSON.stringify({method: 'shutdown'}) + '\n');
          await process.wait();
        })(), new Promise(resolve => { timeout = setTimeout(resolve, 5000); })]);
      }
      // Mozilla's existing Subprocess API waits for process exit; on Windows
      // kill() terminates immediately. Never release a writer before exit.
      if (!info.exited) {
        this.recordTransport('parent_kill', {helper_id: info.id, reason});
        await process.kill();
      }
      if (this.process === process) { this.process = null; this.processError = ''; }
    } catch (error) {
      if (this.process === process) this.processError = 'The previous helper could not be stopped. Restart Zotero before retrying.';
      throw error;
    } finally {
      clearTimeout(timeout);
    }
  },
  async launch() {
    if (this.stopped) throw new Error(this.shortName + ' is closed.');
    if (this.process) return;
    Zotero.Prefs.set('httpServer.enabled', true);
    Zotero.Prefs.set('httpServer.localAPI.enabled', true);
    await Zotero.Server.init();
    const {Subprocess} = ChromeUtils.importESModule('resource://gre/modules/Subprocess.sys.mjs');
    let command, args, runtimeIdentity = {};
    // A test harness can override the private transport in its disposable
    // profile. This is deliberately absent from all user setup controls.
    const development = Zotero.Prefs.get('notable.developmentWorker', true);
    if (development) {
      const value = JSON.parse(development); command = value.command; args = value.arguments;
    } else {
      let manifest;
      try { manifest = await (await fetch(this.rootURI + 'runtime-manifest.json')).json(); }
      catch (_) { throw new Error('This development build has no bundled helper. Install the complete ' + this.name + ' package.'); }
      const directory = PathUtils.join(this.statePath, 'runtime', manifest.id);
      const marker = PathUtils.join(directory, 'ready.json');
      const executable = PathUtils.join(directory, 'notable-worker.exe');
      const executableManifest = manifest.files.find(file => file.path === 'notable-worker.exe');
      runtimeIdentity = {runtime_id: manifest.id, executable_md5: executableManifest?.md5};
      for (const file of manifest.files) {
        if (file.path.includes('..') || file.path.startsWith('/') || file.path.includes('\\')) throw new Error('Invalid packaged runtime path');
      }
      // A directory runtime also depends on its DLLs and models. A ready
      // marker must not hide a member removed or changed after extraction.
      let healthy = !this.repairRuntime && !!executableManifest && await IOUtils.exists(marker);
      if (healthy) for (const file of manifest.files) {
        const destination = PathUtils.join(directory, ...file.path.split('/'));
        if (!await IOUtils.exists(destination) || await Zotero.Utilities.Internal.md5Async(destination) !== file.md5) {
          healthy = false; break;
        }
      }
      if (!healthy) {
        this.message('Preparing or repairing the bundled helper…');
        for (const file of manifest.files) {
          const destination = PathUtils.join(directory, ...file.path.split('/'));
          await IOUtils.makeDirectory(PathUtils.parent(destination), {ignoreExisting: true});
          const response = await fetch(this.rootURI + 'runtime/' + file.path);
          if (!response.ok) throw new Error('The bundled helper is incomplete. Reinstall ' + this.shortName + '.');
          await IOUtils.write(destination, new Uint8Array(await response.arrayBuffer()));
          if (await Zotero.Utilities.Internal.md5Async(destination) !== file.md5) throw new Error('A helper file failed verification. Reinstall ' + this.shortName + '.');
        }
        await IOUtils.writeUTF8(marker, JSON.stringify({id: manifest.id}));
        this.repairRuntime = false;
      }
      command = PathUtils.join(directory, 'notable-worker.exe'); args = [];
    }
    this.message('Starting the background helper…');
    this.recordTransport('helper_starting', runtimeIdentity);
    if (this.stopped) throw new Error(this.shortName + ' is closed.');
    const diagnostics = Zotero.Prefs.get('notable.testDiagnostics', true) ? ['--debug-stacks'] : [];
    // PyMuPDF caches its message stream at import, before per-request redirects.
    const process = await Subprocess.call({command, arguments: [...args, '--state', this.statePath, '--port', String(Zotero.Server.port), ...diagnostics],
      stderr: 'pipe', environmentAppend: true, environment: {PYTHONUNBUFFERED: '1', PYTHONDONTWRITEBYTECODE: '1', PYMUPDF_MESSAGE: 'fd:2'}});
    this.process = process;
    const info = {id: Date.now().toString(36) + '-' + Math.random().toString(36).slice(2), started: Date.now(), ...runtimeIdentity};
    (this.processTraces ||= new WeakMap()).set(process, info);
    this.recordTransport('helper_started', {helper_id: info.id, command: command.split(/[\\/]/).pop(), pid: process.pid ?? null, ...runtimeIdentity});
    if (typeof process.wait === 'function') info.exitPromise = process.wait().then(result => {
      info.exited = true;
      this.recordTransport('helper_exit', {helper_id: info.id, exit_code: result?.exitCode ?? null,
        duration_ms: Date.now() - info.started, parent_initiated: !!info.terminationRequested, reason: info.reason || 'unexpected_exit'});
      return result;
    }).catch(error => { this.recordTransport('helper_wait_failed', {helper_id: info.id, type: error.name}); });
    if (this.stopped) { await this.closeProcess(process); throw new Error(this.shortName + ' is closed.'); }
    // Drain diagnostics without logging paper contents or credentials.
    (async () => {
      try {
        while (true) {
          const chunk = await process.stderr.readString();
          if (!chunk) break;
          // Raw stderr can contain PDF text or signed URLs. Retain bounded,
          // allowlisted diagnostic structure immediately, not document text.
          const sample = chunk.slice(0, 8192);
          const knownModules = new Set(['pyzotero','httpx2','httpcore2','rapidocr','onnxruntime','numpy','pymupdf','rmscene','pypdf','PIL','cv2','anyio','sniffio','certifi','h11']);
          const knownErrors = new Set(['ImportError','ModuleNotFoundError','ValueError','RuntimeError','TimeoutError','OSError','PermissionError','BrokenPipeError','ConnectionError','ReadTimeout','WriteTimeout','ConnectTimeout']);
          const modules = [...sample.matchAll(/No module named ['"]([A-Za-z0-9_.]+)['"]/g)].map(match => match[1]).filter(name => knownModules.has(name));
          const types = [...sample.matchAll(/\b([A-Za-z_]*(?:Error|Exception))\b/g)].map(match => match[1]).filter(name => knownErrors.has(name));
          this.recordTransport('helper_stderr', {helper_id: info.id, characters: chunk.length,
            missing_modules: modules, error_types: [...new Set(types)], mupdf_error: chunk.includes('MuPDF error:'),
            fatal_python_error: chunk.includes('Fatal Python error:'), windows_fatal_exception: chunk.includes('Windows fatal exception:'),
            access_violation: chunk.includes('access violation'), content_redacted: true});
        }
      } catch (error) {
        this.recordTransport('stderr_read_failed', {helper_id: info.id, type: error.name});
      }
    })();
    (async () => {
      let buffer = '', eof = false, failure = 'The background helper stopped. Retry your action to restart it.';
      try {
        while (true) {
          const chunk = await process.stdout.readString(); if (!chunk) { eof = true; this.recordTransport('stdout_eof', {helper_id: info.id, trailing_characters: buffer.length}); break; }
          buffer += chunk;
          let boundary;
          while ((boundary = buffer.indexOf('\n')) !== -1) {
            const line = buffer.slice(0, boundary); buffer = buffer.slice(boundary + 1);
            const response = JSON.parse(line);
            if (response.event === 'progress') {
              info.lastHeartbeat = Date.now();
              this.recordTransport(response.message === 'Still working...' ? 'heartbeat_received' : 'progress_received', {helper_id: info.id});
              this.touchRequests(); this.receiveProgress(response); continue;
            }
            const pending = this.pending.get(response.id);
            if (!pending) continue;
            this.pending.delete(response.id);
            clearTimeout(pending.timer);
            this.recordTransport(response.error ? 'rpc_failed' : 'rpc_finished', {helper_id: info.id, trace_id: pending.traceId,
              id: response.id, method: pending.method, duration_ms: Date.now() - pending.started,
              error_type: response.error?.type || null,
              user_message: response.error && !['ValueError','FileNotFoundError'].includes(response.error.type) ? response.error.message : null});
            if (response.error) pending.reject(new Error(response.error.message)); else pending.resolve(response.result);
          }
        }
      } catch (error) {
        if (error instanceof SyntaxError) {
          failure = 'The background helper sent an invalid response. Retry your action to restart it.';
          this.recordTransport('invalid_response', {helper_id: info.id, type: error.name, trailing_characters: buffer.length});
        } else {
          this.recordTransport('stdout_read_failed', {helper_id: info.id, type: error.name});
        }
      }
      if (info.userFailure) failure = info.userFailure;
      if (eof && !info.exited && info.exitPromise) await Promise.race([info.exitPromise, new Promise(resolve => setTimeout(resolve, 50))]);
      this.recordTransport('helper_stopped', {helper_id: info.id, reason: failure});
      try { await this.closeProcess(process, false, info.reason || (failure.includes('invalid response') ? 'invalid_protocol' : 'stdout_closed')); } catch (_) {}
      this.rejectProcessRequests(process, this.processError || failure);
    })();
  },
  async rpc(method, params = {}) {
    if (this.stopped) throw new Error(this.shortName + ' is closed.');
    if (this.processError) throw new Error(this.processError);
    if (!this.process) {
      if (!this.launching) this.launching = this.launch().finally(() => { this.launching = null; });
      await this.launching;
    }
    if (this.stopped || !this.process) throw new Error(this.shortName + ' is closed.');
    const id = ++this.serial;
    const info = this.processTrace(this.process), traceId = (info.id || 'helper') + '-' + id;
    const answer = new Promise((resolve, reject) => this.pending.set(id, {resolve, reject, process: this.process, method, traceId, started: Date.now()}));
    this.recordTransport('rpc_started', {helper_id: info.id, trace_id: traceId, id, method});
    this.touchRequests();
    try { await this.process.stdin.write(JSON.stringify({id, method, params, trace_id: traceId}) + '\n'); }
    catch (error) { clearTimeout(this.pending.get(id)?.timer); this.pending.delete(id); throw error; }
    return answer;
  },
  touchRequests() {
    for (const request of this.pending.values()) {
      clearTimeout(request.timer);
      request.timer = setTimeout(async () => {
        const process = request.process;
        const info = this.processTrace(process);
        info.userFailure = 'The helper stopped responding and was stopped safely. Retry your action to restart it.';
        this.recordTransport('rpc_timeout', {helper_id: info.id, trace_id: request.traceId, method: request.method,
          duration_ms: Date.now() - request.started, last_heartbeat_time: info.lastHeartbeat || null});
        if (process) { try { await this.closeProcess(process, false, 'inactivity_timeout'); } catch (_) {} }
        this.rejectProcessRequests(process, this.processError || 'The helper stopped responding and was stopped safely. Retry your action to restart it.');
      }, this.requestTimeout || 600000);
    }
  },
  rejectProcessRequests(process, message) {
    for (const [id, pending] of this.pending) if (pending.process === process) {
      clearTimeout(pending.timer); this.pending.delete(id); pending.reject(new Error(message));
      this.recordTransport('rpc_rejected', {helper_id: this.processTrace(process).id, trace_id: pending.traceId, method: pending.method, user_message: message});
    }
  },
  async refresh() {
    this.latest = await this.rpc('status'); this.refreshCollectionNames(); this.badge(); return this.latest;
  },
  reviewNeedsUpgrade(row) {
    const version = this.latest?.review_guidance?.version || 1;
    return row.status === 'pending' && ['zotero', 'cloud', 'inbox'].includes(row.kind)
      && (row.payload.guidance_version !== version || !['ready', 'repair'].includes(row.payload.guidance_state) || !Array.isArray(row.payload.findings));
  },
  async prepareReviewGuidance({retry = false} = {}) {
    const legacy = (this.latest?.reviews || []).filter(row => this.reviewNeedsUpgrade(row));
    if (!legacy.length || this.stopped) { this.reviewUpgrade = null; return; }
    if (this.reviewUpgradePromise) return this.reviewUpgradePromise;
    if (this.reviewUpgrade?.state === 'failed' && !retry) return;
    if (this.busy) {
      this.reviewUpgrade = {state: 'waiting', count: legacy.length}; this.reviewUpgradeWaiting = true; return;
    }
    this.reviewUpgrade = {state: 'loading', count: legacy.length};
    if (this.view && !this.view.closed) this.render();
    this.reviewUpgradePromise = (async () => {
      try {
        // Upgrade saved evidence through the existing local-only check, never
        // through Sync. Its operation also prevents any queued filing writes.
        await this.action('recheck_review', {}, false);
        const remaining = (this.latest.reviews || []).filter(row => this.reviewNeedsUpgrade(row));
        if (remaining.length) throw Error('Some saved reviews could not be updated. Retry the local update; your papers and filing choices are retained.');
        this.reviewUpgrade = null;
      } catch (error) {
        this.reviewUpgrade = {state: 'failed', count: legacy.length, error: error.message};
      } finally {
        if (this.view && !this.view.closed) this.render();
      }
    })().finally(() => { this.reviewUpgradePromise = null; });
    return this.reviewUpgradePromise;
  },
  observeReviewChange(event, type) {
    if (['item', 'collection-item'].includes(type) && ['add', 'modify', 'delete', 'trash', 'move', 'remove'].includes(event)) {
      this.zoteroInboxDirty = true;
    }
    if (type === 'trash' || (['item', 'item-tag', 'tag'].includes(type) && ['add', 'modify', 'delete', 'trash', 'remove'].includes(event))) {
      this.paperTagEvidenceSerial = (this.paperTagEvidenceSerial || 0) + 1;
      this.scheduleSearchRefresh();
    }
    if (['item-tag', 'tag', 'trash'].includes(type)
        || (type === 'item' && ['add', 'delete', 'trash', 'remove'].includes(event))) {
      this.paperTagBankRevision = (this.paperTagBankRevision || 0) + 1;
      this.schedulePaperTagBankRefresh();
    }
    // Native edits can change duplicate candidates as well as current papers.
    // Signal stale evidence without replacing a form or performing a sync.
    if (!['collection', 'item', 'collection-item'].includes(type)
      || !['add', 'modify', 'delete', 'trash', 'move', 'remove'].includes(event)
      || !(this.latest?.reviews || []).some(row => row.status === 'pending')) return;
    this.reviewChangeSerial = (this.reviewChangeSerial || 0) + 1;
    this.reviewStale = true;
    const message = this.view?.document.getElementById('notable-review-freshness');
    if (message) message.textContent = this.reviewFreshnessMessage();
  },
  scheduleSearchRefresh() {
    if (!this.view?.document || this.view.closed || !['links', 'tags'].includes(this.tab)) return;
    clearTimeout(this.searchRefreshTimer);
    this.searchRefreshTimer = setTimeout(() => {
      if (this.tab === 'tags' && !this.tagEditor && !this.stopped && this.view?.document && !this.view.closed) {
        this.render();
        return;
      }
      this.searchRefreshWaiting = this.busy;
      if (!this.busy && !this.stopped && this.view?.document && !this.view.closed
          && this.tab === 'links') this.render();
    }, 160);
  },
  reviewFreshnessMessage(queue = this.tab === 'review' ? 'inbox' : this.tab) {
    if (this.reviewUpgrade?.state === 'loading') return `Updating ${this.reviewUpgrade.count} saved reviews with current Zotero guidance. This reads local information only; your papers and choices stay in place.`;
    if (this.reviewUpgrade?.state === 'waiting') return 'Saved reviews will update after the current operation finishes. Your filing choices are kept.';
    if (this.reviewUpgrade?.state === 'failed') return 'The saved reviews could not be updated. Use Retry saved review update to try again. ' + this.reviewUpgrade.error;
    if (this.reviewStale) return queue === 'inbox'
      ? 'Zotero changed. Check Inbox to refresh your reviews.'
      : 'Zotero changed. Recheck Zotero to update the guidance. Your unsaved choices are kept.';
    if (queue === 'inbox') return '';
    const check = this.reviewRecheck;
    if (!check) return 'After editing in Zotero, check your changes here.';
    const changed = check.changed_ids?.length || 0, unavailable = check.unavailable_ids?.length || 0, resolved = check.resolved_ids?.length || 0;
    return `Rechecked ${check.checked} local review${check.checked === 1 ? '' : 's'}; ${changed} changed${unavailable ? `; ${unavailable} need repair` : ''}.`
      + (resolved ? ` Closed ${resolved} review${resolved === 1 ? '' : 's'} for ${resolved === 1 ? 'a deleted attachment' : 'deleted attachments'}.` : '');
  },
  collectionCatalog() {
    // Worker status intentionally has no native catalog after a helper restart.
    // Keep Zotero's own cached collection objects authoritative across snapshots.
    if (typeof Zotero !== 'undefined' && Zotero.Collections?.getByLibrary && Zotero.Libraries?.userLibraryID) {
      const rows = Zotero.Collections.getByLibrary(Zotero.Libraries.userLibraryID, true);
      this.nativeCollections = rows.filter(row => !row.deleted)
        .map(row => ({key: row.key, name: row.name, parentCollection: row.parentKey || false}));
      this.collectionsCurrent = true;
    }
    const collections = this.nativeCollections || this.latest?.collections || [];
    if (this.latest) this.latest.collections = collections;
    if (this.folderCatalog) this.folderCatalog.collections = collections;
    return collections;
  },
  refreshCollectionNames() {
    if (!this.latest || this.stopped) return;
    const collections = this.collectionCatalog();
    const paths = new Map(this.paths(collections, 'key', 'parentCollection', 'name'));
    const root = paths.get(this.latest.config?.zotero_root);
    if (root) this.latest.config.zotero_path = root;
    if (!this.view || this.view.closed) return;
    const doc = this.view.document;
    for (const select of doc.querySelectorAll('select[data-zotero-collections]')) {
      const choices = [...paths];
      const empty = [...select.options].filter(option => !option.value).map(option => ['', option.textContent]);
      this.reconcileOptions(select, [...empty, ...choices]);
    }
    for (const select of doc.querySelectorAll('select[data-folder-destination]')) select.notableRefreshNames();
    const label = doc.getElementById('notable-connected-zotero');
    if (label && root) label.textContent = 'Zotero: ' + root;
  },
  reconcileOptions(select, choices) {
    const selected = new Set([...select.options].filter(option => option.selected || option.value === select.value).map(option => option.value));
    if (choices.length === select.options.length && choices.every(([key], index) => select.options[index].value === key)) {
      choices.forEach(([, label], index) => { select.options[index].textContent = label; });
      select.notableRefreshChooser?.(); select.notableRefreshRecords?.(); return;
    }
    select.replaceChildren();
    for (const [key, label] of choices) {
      const option = this.el('option', label, select, {value: key}); option.selected = selected.has(key);
    }
    if (!select.multiple && !choices.some(([key]) => selected.has(key))) select.value = '';
    select.notableRefreshChooser?.(); select.notableRefreshRecords?.();
  },
  receiveProgress(response) {
    if (response.quiet || this.inboxChecking) return;
    if (this.operation?.method === 'check_inbox') {
      this.operation.updated = Date.now();
      return;
    }
    const checkingZoteroFiles = /^Checking Zotero file \d+ of \d+:/.test(response.message);
    const message = checkingZoteroFiles ? 'Checking Zotero for new papers…' : response.message;
    if (this.operation?.method === 'sync') {
      this.operation.updated = Date.now();
      if (message !== 'Still working...') this.operation.detail = message;
      this.updateActivity();
    } else if (checkingZoteroFiles && this.operation?.detail === message) this.operation.updated = Date.now();
    else this.message(message);
    if (!this.latest || !Array.isArray(response.links)) return;
    const previous = new Map((this.latest.links || []).map(row => [row.cloud, row]));
    this.latest.links = response.links.map(row => ({...previous.get(row.cloud), ...row, unavailable: !!row.unavailable}));
    const available = this.latest.links.filter(row => !row.unavailable);
    if (this.latest.connection) this.latest.connection.linked_papers = available.length;
    if (this.latest.filing_summary) {
      this.latest.filing_summary.linked = available.length;
      this.latest.filing_summary.pdfs_received = available.filter(row => row.revision).length;
    }
    if (this.view && !this.view.closed) {
      const summary = this.view.document.getElementById('notable-paper-status');
      if (summary) summary.textContent = this.paperStatusMessage();
    }
    // A link can be committed before a long sync ends. Keep that list live
    // without replacing an in-progress review form or issuing queued RPCs.
    if (this.view && !this.view.closed && this.tab === 'links') {
      const focus = this.captureFocus();
      const content = this.view.document.querySelector('main');
      if (content) { content.replaceChildren(); this.renderLinks(content); this.disable(this.busy); this.restoreFocus(focus); }
    }
  },
  isQueueMessage(issue) { return /^Review (the filename|this new tablet document)/.test(issue.reason || ''); },
  paperStatusMessage() {
    const queued = this.queuedReviewIds();
    const saved = queued.size ? `${queued.size} decisions saved in filing queue · ` : '';
    const linked = (this.latest?.links || []).filter(row => !row.unavailable).length;
    if (this.operation?.method === 'sync') return `${saved}${linked} linked so far`;
    const waiting = this.latest?.filing_summary?.approved_waiting_sync;
    return `${saved}${waiting == null ? '' : `${waiting} approved, waiting for sync · `}${linked} linked`;
  },
  syncStatusMessage() {
    const config = this.latest?.config || {};
    const job = this.operation?.method === 'sync' ? this.operation : null;
    const mode = job?.yielding ? 'Finishing the current paper before filing…'
      : this.pausing || job?.stopping ? 'Stopping sync…'
      : job ? 'Syncing with reMarkable…'
      : config.sync_paused ? 'Sync paused'
      : this.syncFailure && config.automatic ? 'Sync waiting to retry'
      : this.syncFailure || this.latest?.status?.state === 'needs_attention' ? 'Sync needs attention'
      : this.syncReviewAvailable() ? 'Sync needs review'
      : config.automatic ? 'Automatic sync on' : 'Manual sync';
    const last = this.latest?.last_sync_at;
    return last ? `${mode} · Last sync check ${new Date(last * 1000).toLocaleString()}` : mode;
  },
  statusMessage() {
    if (!this.latest?.config?.tablet_root && this.latest?.status?.state === 'setup_required') {
      return 'No folder connected. Choose your folders in Setup; syncing is off.';
    }
    if (this.syncFailure) return this.syncFailure;
    if (this.latest?.folder_issues?.length) return 'Folder sync needs a decision. Open Sync Conflicts.';
    const issues = this.latest?.status?.result?.review || [];
    const pending = this.latest?.reviews?.length || 0;
    if (pending && ((issues.length && issues.every(issue => this.isQueueMessage(issue))) || !this.latest?.status?.message)) {
      return 'Ready';
    }
    return this.latest?.status?.message || 'Ready';
  },
  badge() {
    const count = (this.latest?.reviews || []).filter(row => row.kind === 'conflict' || !row.payload.routine).length
      + (this.latest?.bulk?.rows || []).filter(row => row.issues.length && row.status !== 'dismissed').length
      + (this.latest ? this.syncDecisionCount() : 0);
    const attention = this.syncFailure || this.latest?.status?.state === 'needs_attention' || this.syncWarnings().length;
    for (const window of this.windows) {
      const button = window.document.getElementById('notable-open');
      const suffix = count ? ` (${count})` : attention ? ' (!)' : '';
      button?.setAttribute('label', 'reMarkable' + suffix);
      button?.setAttribute('aria-label', 'Open reMarkable Sync' + suffix);
      if (this.latest?.config?.tablet_root) button?.setAttribute('tooltiptext', this.syncStatusMessage());
    }
  },
  scheduleInbox() {
    clearTimeout(this.inboxTimer);
    if (this.stopped) return;
    this.inboxTimer = setTimeout(async () => {
      try { await this.checkInbox(); }
      finally { this.scheduleInbox(); }
    }, 3000);
  },
  async checkInbox() {
    if (this.stopped || this.busy || this.inboxChecking || this.paperTagChecking || this.pausing || this.changingFolder
      || this.drainingFilings || this.setupDirty || this.acquiringPDF || !(this.latest?.config?.inbox || this.latest?.config?.tablet_root)) return;
    if (this.view?.document?.querySelector?.('dialog[open]')) return;
    const now = Date.now();
    const interval = 300000;
    this.lastInboxRecheck ??= now;
    const full = now - this.lastInboxRecheck >= interval;
    // A file can finish downloading without an item notification. Keep a
    // periodic fallback, but avoid rebuilding the Zotero catalog on idle ticks.
    const discover = !!this.latest.config.tablet_root
      && (this.zoteroInboxDirty !== false || now - (this.lastZoteroInboxCheck || 0) >= 300000);
    const scanFolder = !!this.latest.config.inbox && now - (this.lastInboxFolderCheck || 0) >= 15000;
    if (!full && !discover && !scanFolder) return;
    if (this.latest.config.inbox) this.lastInboxFolderCheck = now;
    this.inboxChecking = true;
    this.busy = true;
    let changed = false;
    try {
      if (full) this.lastInboxRecheck = now;
      if (!full && discover) this.zoteroInboxDirty = false;
      this.inboxRequest = full ? this.refreshInbox() : this.rpc('scan_inbox', {discover});
      const result = await this.inboxRequest;
      if (this.stopped) return;
      if (discover) {
        this.lastZoteroInboxCheck = Date.now();
        if (result.native_files_settling) this.zoteroInboxDirty = true;
      }
      changed = JSON.stringify(this.latest?.reviews) !== JSON.stringify(result.reviews);
      this.latest = result;
      if (result.review_recheck) this.reviewRecheck = result.review_recheck;
      this.refreshCollectionNames();
      this.badge();
    } catch (_) {
      if (discover) this.zoteroInboxDirty = true;
    } finally {
      this.inboxChecking = false;
      this.busy = false;
      this.inboxRenderPending ||= changed;
      if (!this.stopped && this.view && !this.view.closed) {
        if (this.inboxRenderPending && !this.view.document?.querySelector?.('dialog[open]')
          && ['inbox', 'library', 'sync-review'].includes(this.tab)) this.render();
        else this.disable(false);
      }
      this.drainSyncDecisions();
    }
  },
  schedule(sooner) {
    clearTimeout(this.timer); this.timer = null;
    const config = this.latest?.config;
    if (this.stopped || !config?.tablet_root || config.sync_paused || !config.automatic) {
      this.syncDue = null; return;
    }
    const now = Date.now();
    this.syncDue ??= now + Math.min(900000, 300000 * 2 ** Math.min(this.failures || 0, 2));
    if (sooner != null && !this.failures) this.syncDue = Math.min(this.syncDue, now + sooner);
    const delay = Math.max(this.syncDue - now, this.automaticAllowed() ? 0 : 1000);
    const timer = setTimeout(async () => {
      if (this.timer !== timer) return;
      this.timer = null;
      if (this.automaticAllowed()) {
        try { await this.action('sync', {automatic: true}, false); } catch (_) {}
      }
      if (this.timer == null) this.schedule();
    }, delay);
    this.timer = timer;
  },
  automaticAllowed() {
    return !this.stopped && !this.busy && !this.paperTagChecking && !this.acquiringPDF && !this.drainingFilings
      && (!this.filingQueue.length || this.filingQueuePaused) && !this.drainingTagEdits
      && (!this.tagEdits.length || this.tagEditError) && !this.afterSync.length && !this.drainingAfterSync
      && !this.drainingSyncDecisions && !this.syncDecisions.some(job => !job.error) && this.canSync()
      && !this.latest.config.sync_paused && !!this.latest.config.automatic
      && !this.view?.document?.querySelector?.('dialog[open]');
  },
  async yieldSyncForFiling() {
    if (this.launching) { try { await this.launching; } catch (_) { return; } }
    const operation = this.operation;
    if (operation?.method !== 'sync' || operation.stopping || this.pausing || !this.process) return;
    // Approvals have already been saved before asking the single writer to
    // yield. Transient cancel stops at its existing safe boundary; it does
    // not change automatic-sync settings or start another sync afterwards.
    operation.stopping = true; operation.yielding = true;
    this.updateActivity(); this.badge();
    this.recordTransport('cancel_requested', {helper_id: this.processTrace(this.process).id, reason: 'filing_priority'});
    try { await this.process.stdin.write(JSON.stringify({method: 'cancel'}) + '\n'); }
    catch (error) { this.message(error.message, true); }
  },
  canSync() { return !!this.latest?.config?.tablet_root && !this.changingFolder && !this.setupDirty
    && !this.disconnectPreview && !this.pausing && !this.drainingSyncDecisions; },
  async pauseSync() {
    if (this.pausing) return;
    this.pausing = true; clearTimeout(this.timer); this.timer = null;
    this.cancelAfterSync('Sync was paused. Review the item and choose the action again.');
    this.filingQueuePaused = true;
    try { await this.saveFilingQueue(); } catch (_) { this.queueError = 'The queue could not be saved. Sync is being paused.'; }
    if (this.operation) { this.operation.stopping = true; this.operation.yielding = false; }
    if (this.latest?.config) this.latest.config.sync_paused = true;
    this.disable(this.busy); this.badge();
    try {
      this.latest = await this.rpc('pause_sync'); this.badge();
      if (this.view && !this.view.closed) this.render();
    } catch (error) { this.message(error.message, true); throw error; }
    finally { this.pausing = false; this.disable(this.busy); this.schedule(); this.badge(); this.drainSyncDecisions(); this.drainTagEdits(); }
  },
  clearFolderEditor() {
    this.changingFolder = false; this.setupDirty = false; this.setupDraft = null;
    this.folderPreview = null; this.disconnectPreview = null;
    this.tagEditor = null; this.tagSearch = null; this.selected = null;
  },
  open() {
    if (this.view && !this.view.closed) { this.view.focus(); return; }
    this.view = Zotero.getMainWindow().openDialog('chrome://notable/content/workspace.xhtml', 'notable-workspace',
      'chrome,dialog=no,resizable,centerscreen,width=1040,height=760', {controller: this});
  },
  openSetup() {
    if (this.view && !this.view.closed && this.view.document.getElementById('app')?.querySelector('h1')) {
      if (this.tab !== 'setup') this.openTab('setup');
    } else {
      this.requestedTab = 'setup';
    }
    this.open();
  },
  async openPageTags(reader) {
    const state = reader._internalReader?._state;
    const page = (state?.primary !== false ? state?.primaryViewStats : state?.secondaryViewStats)?.pageIndex;
    if (!Number.isInteger(page)) throw new Error('Wait for this PDF page to finish loading.');
    const item = await Zotero.Items.getAsync(reader.itemID);
    if (!item?.isPDFAttachment()) throw new Error('Choose a PDF first.');
    const context = {key: item.key, page, title: 'Page tags', loading: true,
      connection: this.filingConnection()};
    this.tagEditor = context;
    if (this.view && !this.view.closed && this.view.document.getElementById('app')?.querySelector('h1')) {
      this.tabNavigationSerial = (this.tabNavigationSerial || 0) + 1;
      this.tab = 'tags';
      this.render();
    } else {
      this.requestedTab = 'tags';
    }
    this.open();
    try {
      const link = (this.latest?.links || []).find(row => row.view_key === item.key && !row.unavailable);
      const parent = link?.page_map?.[page] && item.parentID ? await Zotero.Items.getAsync(item.parentID) : null;
      if (parent && !parent.deleted && parent.key === link.parent && link.page_map_digest) {
        await parent.loadAllData?.();
        const pageID = link.page_map[page];
        Object.assign(context, {cloud: link.cloud, parent_key: parent.key, pdf_digest: link.page_map_digest,
          title: parent.getField('title') || link.name, page_id: pageID,
          paper_tags: parent.getTags().map(row => row.tag), page_tags: link.page_tags?.[pageID] || [],
          vocabulary: [...new Set([...(this.paperTagBank?.tags || []), ...this.pageTagNames()])].sort(),
          interrupted: !!link.tag_pending, orphaned_pages: Object.fromEntries(Object.entries(link.page_tags || {})
            .filter(([id, values]) => !link.page_map.includes(id) && values.length))});
      } else if (item.parentID) {
        const reference = await Zotero.Items.getAsync(item.parentID);
        if (!reference?.isRegularItem() || reference.deleted) throw Error('The Zotero reference is unavailable.');
        await reference.loadAllData();
        const paperTags = reference.getTags().map(row => row.tag);
        const links = (this.latest?.links || []).filter(row => row.view_key && row.baseline && !row.unavailable);
        Object.assign(context, {paperOnly: true, reference, title: reference.getField('title'), pageMapUnavailable: !!link,
          checkingOriginal: !link && links.length > 0, paperTags, paperDraft: {tags: [...paperTags], input: ''}});
        context.loading = false;
        if (this.tagEditor === context && this.tab === 'tags') this.render();
        if (!link && links.length) {
          this.readerOriginalMatches?.delete(item.key);
          try {
            const digest = await notablePDFDigest(item, Zotero.getMainWindow().crypto);
            if (context.connection !== this.filingConnection()) return;
            const matches = links.filter(row => row.baseline === digest && row.view_key !== item.key);
            if (matches.length === 1) {
              const [match] = matches;
              context.linkedCopy = match;
              this.readerOriginalMatches ||= new Map();
              this.readerOriginalMatches.set(item.key, {attachment: item.key, parent: reference.key,
                cloud: match.cloud, digest, title: context.title, connection: context.connection});
            } else if (matches.length > 1) context.ambiguousCopies = true;
          } catch (_) { context.matchUnavailable = true; }
          finally { context.checkingOriginal = false; }
        }
      } else if (this.busy) {
        context.error = 'This PDF has no Zotero reference or available reMarkable page link.';
      } else Object.assign(context, await this.rpc('tag_context', {key: item.key, page}));
    } catch (error) { context.error = error.message; }
    finally {
      context.loading = false;
      if (this.tagEditor === context && this.tab === 'tags' && this.view?.document && !this.view.closed) this.render();
    }
  },
  async action(method, params = {}, render = true) {
    if (this.paperTagChecking) { try { await this.paperTagRequest; } catch (_) {} }
    if (this.inboxChecking) { try { await this.inboxRequest; } catch (_) {} }
    const syncing = method === 'sync';
    if (syncing && (params.automatic ? !this.automaticAllowed() : !this.canSync())) return;
    if (syncing && !params.automatic && !this.busy && this.syncDecisions.some(job => !job.error)) {
      await this.drainSyncDecisions();
      if (!this.canSync() || this.syncDecisions.some(job => !job.error)) return;
    }
    if (this.busy) {
      if (!syncing && this.operation?.method === 'sync') return this.deferUntilAfterSync(method, params, render);
      return;
    }
    clearTimeout(this.timer);
    this.timer = null;
    if (syncing) this.syncDue = null;
    this.busy = true;
    const reviewChangeSerial = this.reviewChangeSerial || 0;
    const checkingInbox = method === 'check_inbox';
    const localReviewAction = checkingInbox || ['recheck_review', 'attach_intake_pdf', 'confirm_folder_change', 'acknowledge_review_finding', 'preview_link_repair', 'repair_link', 'close_removed_source', 'create_destination', 'preview_keyword_cleanup', 'remove_reviewed_keywords',
      'preview_filename_cleanup', 'queue_filename_cleanup', 'cancel_filename_cleanup', 'maintenance'].includes(method);
    const labels = {sync: 'Syncing with reMarkable', scan_library: 'Scanning your Windows library',
      suggest_paper_tags: 'Finding paper keywords and tag suggestions', save_paper_tags: 'Saving your paper-tag choices',
      preview_keyword_cleanup: 'Checking saved keyword evidence', remove_reviewed_keywords: 'Removing selected automatic tags',
      preview_filename_cleanup: 'Checking linked filenames against Zotero citations', queue_filename_cleanup: 'Saving selected filename changes',
      import_library: 'Importing papers into Zotero', approve_routine: 'Filing the selected papers',
      approve: 'Filing this paper', attach_intake_pdf: 'Attaching the ToSort PDF',
      confirm_folder_change: 'Checking the changed folder',
      recheck_review: 'Rechecking local Zotero evidence', check_inbox: 'Checking Zotero for new papers…',
      acknowledge_review_finding: 'Saving this library decision', reindex: 'Rebuilding the search index', folders: 'Loading folders',
      maintenance: 'Running the selected local library action', apply_tag_edit: 'Applying saved tag changes'};
    const detail = checkingInbox ? '' : 'Starting…';
    this.operation = {method, automatic: !!params.automatic, title: labels[method] || 'Updating your setup',
      started: Date.now(), updated: Date.now(), detail};
    if (syncing) this.badge();
    else if (!checkingInbox) this.message(detail);
    this.disable(true);
    try {
      const previousSyncFailure = this.syncFailure;
      const connection = this.filingConnection();
      const previousInbox = checkingInbox ? this.inboxRows() : null;
      const result = checkingInbox ? await this.refreshInbox() : await this.rpc(method, params);
      this.latest = result.reviews ? result : await this.rpc('status');
      if (result.intake_attachment) {
        const {reference, key} = result.intake_attachment;
        if (this.inboxReferenceSelection?.id === reference) this.inboxReferenceSelection.attachment = key;
        this.zoteroInboxDirty ||= result.native_files_settling;
      }
      if (['sync', 'set_page_tags', 'apply_tag_edit', 'combine_tag_copies', 'remove_unavailable_page_tags'].includes(method)) {
        this.tagSyncRevision = (this.tagSyncRevision || 0) + 1;
      }
      if (syncing && !this.operation.stopping) {
        this.failures = 0;
        this.syncFailure = '';
      }
      this.refreshCollectionNames();
      if (result.review_recheck) {
        this.reviewRecheck = result.review_recheck;
        if (reviewChangeSerial === (this.reviewChangeSerial || 0)) this.reviewStale = false;
      } else if (method === 'sync' && reviewChangeSerial === (this.reviewChangeSerial || 0)) this.reviewStale = false;
      if (connection !== this.filingConnection()) {
        this.reviewDrafts.clear(); this.tagDrafts.clear(); this.linkDrafts.clear(); this.batchCollection = null; this.selected = null;
        this.tagEditor = null;
        this.inboxReferenceSelection = null;
        this.inboxHandoffWaiting = null;
        this.separateIntakePDFs?.clear();
        this.paperSearch = null; this.tagSearch = null; this.syncDue = null; this.failures = 0; this.syncFailure = '';
      }
      this.badge();
      const refreshView = syncing ? ['review', 'inbox', 'library', 'sync-review', 'links'].includes(this.tab) : render;
      if (refreshView && this.view && !this.view.closed) {
        if (syncing && this.view.document?.querySelector?.('dialog[open]')) this.inboxRenderPending = true;
        else this.render();
      }
      if (checkingInbox && this.operation.stopping) this.message('Inbox check stopped.');
      else if (checkingInbox && result.inbox_issues?.length) this.message('Couldn’t finish checking Inbox. Try again.', true);
      else if (result.intake_attachment) this.message(result.intake_attachment.source_retained
        ? 'PDF attached in Zotero. The original is still in ToSort.' : 'PDF attached in Zotero.');
      else if (result.folder_change) {
        const messages = {keep: 'Folder pair disconnected.', mirror: 'Matching folder moved to Trash.', restore: 'Original folder restored.'};
        this.message(result.folder_change === 'confirmed' ? messages[result.folder_choice] || messages.keep
          : 'The folder changed. Review its current status in Sync Conflicts.');
      }
      else if (!syncing || !params.automatic || previousSyncFailure || this.latest.status?.state === 'needs_attention') {
        this.message(checkingInbox ? this.inboxCheckResult(previousInbox) : this.statusMessage());
      }
      return result;
    } catch (error) {
      if (checkingInbox) {
        if (this.operation.stopping) this.message('Inbox check stopped.');
        else this.message('Couldn’t check Zotero. Try again.', true);
        return;
      }
      if (syncing) {
        this.failures = (this.failures || 0) + 1;
        this.syncFailure = error.message;
        this.cancelAfterSync('Sync stopped before the current state could be checked. Review the item and choose the action again.');
      }
      if (this.latest) this.latest.status = {...this.latest.status, state: 'needs_attention', message: error.message};
      this.badge(); this.message(error.message, true); throw error;
    }
    finally {
      this.busy = false; this.operation = null; this.disable(false); this.schedule();
      if (this.searchRefreshWaiting || (syncing && this.tab === 'tags')) {
        this.searchRefreshWaiting = false;
        this.scheduleSearchRefresh();
      }
      if (syncing) this.badge();
      // A local evidence check must not resume queued external writes.
      if (this.reviewUpgradeWaiting && this.view && !this.view.closed) {
        this.reviewUpgradeWaiting = false; this.prepareReviewGuidance();
      } else if (!localReviewAction && this.filingQueue.length && !this.drainingFilings) this.drainFilings().catch(error => this.message(error.message, true));
      if (!this.drainingSyncDecisions) this.drainSyncDecisions();
      if (!this.drainingTagEdits) this.drainTagEdits();
      if (!this.drainingAfterSync) this.drainAfterSync();
    }
  },
  async refreshInbox() {
    const checked = await this.rpc('recheck_review');
    if (this.operation?.stopping || this.stopped) return checked;
    const discover = !!this.latest.config.tablet_root;
    if (discover) this.zoteroInboxDirty = false;
    try {
      const result = await this.rpc('scan_inbox', {discover});
      if (discover) {
        this.lastZoteroInboxCheck = Date.now();
        if (result.native_files_settling) this.zoteroInboxDirty = true;
      }
      if (this.latest.config.inbox) this.lastInboxFolderCheck = Date.now();
      this.lastInboxRecheck = Date.now();
      return {...result, review_recheck: checked.review_recheck};
    } catch (error) {
      if (discover) this.zoteroInboxDirty = true;
      throw error;
    }
  },
  filingConnection() {
    const config = this.latest?.config || {};
    return JSON.stringify([config.connection_id || 'legacy', config.zotero_root, config.tablet_root]);
  },
  actionEvidence(method, params) {
    const identity = params.identity;
    const list = method === 'delete_decision' ? this.latest?.deletions
      : method === 'local_delete_decision' ? this.latest?.local_deletions : null;
    if (list) return JSON.stringify(list.find(row => row.id === identity));
    if (['link', 'cancel_filename_cleanup', 'undo_filename_cleanup'].includes(method)) {
      const link = this.latest?.links?.find(row => row.cloud === identity);
      return JSON.stringify(link && [link.parent, link.view_key, link.revision, link.paused, link.filename_cleanup, link.filename_repair]);
    }
    return null;
  },
  deferUntilAfterSync(method, params, render) {
    if (this.pausing || this.stopped) throw Error('This action could not start while sync is pausing. Try again.');
    const request = JSON.parse(JSON.stringify(params));
    const connection = this.filingConnection(), evidence = this.actionEvidence(method, request);
    const labels = {check_inbox: 'Check Inbox', recheck_review: 'Recheck Zotero', dismiss_review: 'Dismiss Inbox reference',
      attach_intake_pdf: 'Attach PDF from ToSort', confirm_folder_change: 'Apply folder decision',
      delete_decision: 'Apply deletion decision', local_delete_decision: 'Apply deletion decision', resolve_conflict: 'Resolve conflict',
      create_destination: 'Create folder', link: 'Update paper link', move_link: 'Rename or move paper',
      repair_link: 'Repair paper link', reindex: 'Rebuild search index', scan_library: 'Scan library', import_library: 'Import library',
      retry_bulk: 'Update import review', save_paper_tags: 'Save paper tags', paper_tag_candidates: 'Find paper tags'};
    const signature = JSON.stringify([method, request, connection]);
    const existing = this.afterSync.find(job => job.signature === signature);
    if (existing) return existing.result;
    const job = {method, request, render, connection, evidence, signature, label: labels[method] || 'Apply plugin action'};
    job.result = new Promise((resolve, reject) => { job.resolve = resolve; job.reject = reject; });
    this.afterSync.push(job);
    this.refreshAfterSync();
    return job.result;
  },
  async drainAfterSync() {
    if (this.drainingAfterSync || this.busy || this.pausing || this.acquiringPDF || this.drainingTagEdits || this.drainingSyncDecisions || this.stopped) return;
    this.drainingAfterSync = true;
    try {
      while (this.afterSync.length && !this.busy && !this.pausing && !this.acquiringPDF && !this.stopped) {
        const job = this.afterSync[0];
        this.activeAfterSync = job;
        try {
          if (job.connection !== this.filingConnection() || job.evidence !== this.actionEvidence(job.method, job.request)) {
            throw Error(`${job.label} needs review because the item or sync connection changed.`);
          }
          const result = await this.action(job.method, job.request, job.render);
          job.resolve(result);
        } catch (error) { job.reject(error); }
        finally { this.activeAfterSync = null; this.afterSync = this.afterSync.filter(row => row !== job); this.refreshAfterSync(); }
      }
    } finally { this.drainingAfterSync = false; this.schedule(); }
  },
  cancelAfterSync(message) {
    for (const job of this.afterSync) if (job !== this.activeAfterSync) job.reject(Error(message));
    this.afterSync = this.afterSync.filter(job => job === this.activeAfterSync);
    this.refreshAfterSync();
  },
  refreshAfterSync() {
    const host = this.view?.document?.getElementById('notable-after-sync');
    if (!host || this.view.closed) return;
    host.hidden = !this.afterSync.length;
    host.textContent = this.afterSync.length ? 'Waiting for the worker · ' + this.afterSync.map(job => job.label).join(' · ') : '';
  },
  changeSyncDecisions(change) {
    this.syncDecisionWrite = (this.syncDecisionWrite || Promise.resolve()).catch(() => {}).then(async () => {
      if (this.syncDecisionReadError) throw Error(this.syncDecisionReadError);
      const jobs = change(this.syncDecisions);
      const path = PathUtils.join(this.statePath, 'sync-decisions.json');
      await IOUtils.writeUTF8(path, JSON.stringify(jobs), {tmpPath: path + '.tmp', flush: true});
      this.syncDecisions = jobs;
    });
    return this.syncDecisionWrite;
  },
  syncDecisionEvidence(method, params) {
    if (method === 'confirm_folder_change') return (this.latest?.folder_issues || [])
      .find(row => row.zotero === params.issue.zotero && row.tablet === params.issue.tablet);
    const list = method === 'delete_decision' ? this.latest?.deletions : this.latest?.local_deletions;
    return list?.find(row => row.id === params.identity);
  },
  redrawSyncDecisions() {
    if (this.tab !== 'sync-review' || !this.view || this.view.closed) return;
    if (this.view.document.querySelector?.('dialog[open]')) { this.inboxRenderPending = true; return; }
    const top = this.view.scrollY;
    this.render();
    if (Number.isFinite(top)) this.view.scrollTo?.(0, top);
  },
  async drainSyncDecisions() {
    if (this.stopped || this.busy || this.pausing || this.acquiringPDF || this.drainingSyncDecisions
        || !this.latest?.config?.tablet_root || !this.syncDecisions.some(job => !job.error)) return;
    this.drainingSyncDecisions = true;
    let applied = false;
    try {
      while (!this.stopped && !this.busy && !this.pausing && !this.acquiringPDF) {
        await this.syncDecisionWrite;
        const job = this.syncDecisions.find(row => !row.error);
        if (!job || this.stopped || this.busy || this.pausing || this.acquiringPDF) break;
        this.activeSyncDecision = job.key;
        this.redrawSyncDecisions();
        try {
          if (job.connection !== this.filingConnection()) throw Error('This decision belongs to a different sync connection.');
          if (job.evidence !== JSON.stringify(this.syncDecisionEvidence(job.method, job.params))) {
            throw Error('The folder or paper changed while the decision was waiting. Review it again.');
          }
          const result = await this.action(job.method, job.params, false);
          if (!result) break;
          if (result.folder_change === 'changed') throw Error('The folder changed while the decision was waiting. Review it again.');
          await this.changeSyncDecisions(jobs => jobs.filter(row => row.key !== job.key));
          applied = true;
        } catch (error) {
          const changes = await this.changedSyncDecision(job);
          await this.changeSyncDecisions(jobs => jobs.map(row => row.key === job.key ? {...row, error: error.message, changes} : row));
        } finally { this.activeSyncDecision = null; this.redrawSyncDecisions(); }
      }
    } catch (error) {
      this.message('Could not update saved sync decisions. ' + error.message, true);
    } finally {
      this.drainingSyncDecisions = false;
      this.disable(this.busy || this.acquiringPDF);
      this.schedule(applied ? 1000 : undefined);
      if (this.afterSync.length) this.drainAfterSync();
    }
  },
  changeTagEdits(change) {
    this.tagEditWrite = (this.tagEditWrite || Promise.resolve()).catch(() => {}).then(async () => {
      if (this.tagEditsUnreadable) throw Error(this.tagEditError);
      const jobs = change(this.tagEdits);
      const path = PathUtils.join(this.statePath, 'tag-edits.json');
      await IOUtils.writeUTF8(path, JSON.stringify(jobs), {tmpPath: path + '.tmp', flush: true});
      this.tagEdits = jobs;
    });
    return this.tagEditWrite;
  },
  overlayTagEdits(context) {
    const paper = new Set(context.paper_tags), page = new Set(context.page_tags);
    for (const job of this.tagEdits) {
      if (JSON.stringify(job.connection) !== this.filingConnection() || job.parent_key !== context.parent_key) continue;
      const apply = (tags, edit) => {
        for (const tag of edit.before) if (!edit.after.includes(tag)) tags.delete(tag);
        for (const tag of edit.after) if (!edit.before.includes(tag)) tags.add(tag);
      };
      apply(paper, job.paper);
      if (job.cloud === context.cloud && job.key === context.key && job.page_id === context.page_id
          && job.pdf_digest === context.pdf_digest) apply(page, job.page);
    }
    return {paper: [...paper], page: [...page]};
  },
  async drainTagEdits() {
    if (this.stopped || this.busy || this.pausing || this.acquiringPDF || this.drainingTagEdits
        || this.tagEditError || !this.tagEdits.length || !this.latest?.config?.tablet_root) return;
    this.drainingTagEdits = true;
    try {
      while (this.tagEdits.length && !this.stopped && !this.busy && !this.pausing && !this.acquiringPDF) {
        const job = this.tagEdits[0];
        if (JSON.stringify(job.connection) !== this.filingConnection()) throw Error('A saved tag edit belongs to a different sync connection.');
        const {title, ...params} = job;
        const result = await this.action('apply_tag_edit', params, false);
        if (!result) break;
        if (result.tag_edit_applied !== job.edit_id) throw Error('The helper did not confirm this tag edit.');
        await this.changeTagEdits(jobs => jobs.filter(row => row.edit_id !== job.edit_id));
        const context = this.tagEditor;
        if (context?.parent_key === job.parent_key) {
          const apply = (values, edit) => [...new Set(values.filter(tag => !edit.before.includes(tag) || edit.after.includes(tag))
            .concat(edit.after.filter(tag => !edit.before.includes(tag))))];
          context.paper_tags = apply(context.paper_tags, job.paper);
          if (context.cloud === job.cloud && context.page_id === job.page_id && context.pdf_digest === job.pdf_digest) {
            const link = this.latest.links?.find(row => row.cloud === job.cloud);
            context.page_tags = link?.page_tags?.[job.page_id] || [];
          }
        }
      }
    } catch (error) { this.tagEditError = error.message; }
    finally {
      this.drainingTagEdits = false;
      this.refreshTagEditQueue();
      this.schedule();
      if (this.afterSync.length) this.drainAfterSync();
    }
  },
  refreshTagEditQueue() {
    const host = this.view?.document?.getElementById('notable-tag-edits');
    if (!host || this.view.closed) return;
    const open = host.querySelector('details')?.open;
    host.replaceChildren();
    if (!this.tagEdits.length && !this.tagEditError) return;
    const details = this.el('details', '', host, {class: 'filing-queue'});
    details.open = !!open;
    this.el('summary', this.tagEditError ? 'Saved tag changes need attention'
      : `${this.tagEdits.length} saved tag ${this.tagEdits.length === 1 ? 'edit' : 'edits'} waiting to apply`, details);
    if (this.tagEditError) this.el('p', this.tagEditError, details, {class: 'message error', role: 'alert'});
    for (const job of this.tagEdits) this.el('p', job.title || 'Page tags', details);
    if (this.tagEditError && this.tagEdits.length && !this.tagEditsUnreadable) {
      this.button('Retry saved tag changes', details, () => { this.tagEditError = ''; this.drainTagEdits(); }).dataset.browse = 'true';
      this.button('Discard first saved tag edit', details, async () => {
        if (!this.view.confirm('Discard the first saved tag edit? Any changes already applied in Zotero will remain.')) return;
        await this.changeTagEdits(jobs => jobs.slice(1));
        this.tagEditError = ''; this.refreshTagEditQueue(); this.drainTagEdits();
      }).dataset.browse = 'true';
    }
  },
  saveFilingQueue() {
    const path = PathUtils.join(this.statePath, 'filing-queue.json');
    // Snapshot only after earlier writes and their rollback continuations have
    // settled; a queued write must never resurrect a rejected approval.
    this.queueSave = (this.queueSave || Promise.resolve()).catch(() => {}).then(() => IOUtils.writeUTF8(path,
      JSON.stringify({jobs: this.filingQueue, paused: !!this.filingQueuePaused}), {tmpPath: path + '.tmp'}));
    return this.queueSave;
  },
  validFilingJob(job) {
    const text = value => typeof value === 'string' && value.length > 0;
    if (!job || typeof job !== 'object' || !text(job.id) || !text(job.connection)
      || !['approve', 'approve_routine'].includes(job.method) || !job.params || typeof job.params !== 'object' || Array.isArray(job.params)) return false;
    const params = job.params;
    if (job.title != null && typeof job.title !== 'string') return false;
    if (params.research != null && (!Array.isArray(params.research) || params.research.some(key => !text(key)))) return false;
    if (params.tags != null && (!Array.isArray(params.tags) || params.tags.some(tag => !text(tag)))) return false;
    if (params.citation != null && (typeof params.citation !== 'object' || Array.isArray(params.citation)
        || Object.values(params.citation).some(value => typeof value !== 'string'))) return false;
    return job.method === 'approve' ? text(params.identity) && text(params.digest)
      : text(params.collection) && Array.isArray(params.entries) && params.entries.length > 0
        && params.entries.every(row => row && text(row.id) && text(row.digest))
        && new Set(params.entries.map(row => row.id)).size === params.entries.length;
  },
  changeFilingQueue(change) {
    this.queueMutation = (this.queueMutation || Promise.resolve()).catch(() => {}).then(async () => {
      const before = this.filingQueue.slice();
      change();
      try { await this.saveFilingQueue(); }
      catch (error) { this.filingQueue = before; throw error; }
    });
    return this.queueMutation;
  },
  queuedReviewIds() {
    return new Set(this.filingQueue.flatMap(job => job.method === 'approve' ? [job.params.identity] : job.params.entries.map(row => row.id)));
  },
  async queueFiling(method, params, title) {
    if (this.busy && ['check_inbox', 'recheck_review', 'acknowledge_review_finding'].includes(this.operation?.method)) {
      throw Error('Wait for the local review check to finish, then approve the updated review.');
    }
    const ids = method === 'approve' ? [params.identity] : params.entries.map(row => row.id);
    if ((this.latest?.reviews || []).some(row => ids.includes(row.id) && this.reviewNeedsUpgrade(row))) {
      throw Error('This saved review needs its local update before filing. Your choices have been kept.');
    }
    const job = {id: `${Date.now()}-${++this.serial}`, method, params: JSON.parse(JSON.stringify(params)), title: title || params.title || 'Reviewed paper', connection: this.filingConnection()};
    let added = false;
    await this.changeFilingQueue(() => {
      if (!ids.some(id => this.queuedReviewIds().has(id))) { this.filingQueue.push(job); added = true; }
    });
    if (!added) return;
    if (params.tags?.length) this.rememberRecentTags(params.tags);
    if (!this.filingQueuePaused) await this.yieldSyncForFiling();
    if (this.view && !this.view.closed) this.render();
    await this.drainFilings();
  },
  async removeFiling(job) {
    if (job.id === this.activeFiling) throw Error('This paper is already being filed. Use Stop to interrupt it safely.');
    const index = this.filingQueue.indexOf(job);
    if (index < 0) return;
    try { await this.changeFilingQueue(() => { this.filingQueue = this.filingQueue.filter(row => row !== job); }); }
    catch (error) {
      this.filingQueuePaused = true; this.queueError = 'The queue could not be saved. This paper remains queued; retry removing it.';
      throw error;
    }
    if (!this.filingQueue.length) this.queueError = '';
    if (this.view && !this.view.closed) this.render();
  },
  async drainFilings() {
    if (this.busy || this.drainingFilings || this.filingQueuePaused || this.stopped || !this.filingQueue.length) return;
    this.drainingFilings = true;
    let filed = false;
    try {
      await this.queueMutation;
      await this.queueSave;
      while (this.filingQueue.length && !this.filingQueuePaused && !this.stopped) {
        // Queue persistence yields to the event loop. A manual sync may have
        // acquired the writer since our first check; never discard its
        // waiting approval because action() declined a busy operation.
        if (this.busy || this.pausing) break;
        const job = this.filingQueue[0];
        if (job.connection !== this.filingConnection()) throw Error('Queued papers belong to a different folder connection. Return to that connection before resuming.');
        this.activeFiling = job.id;
        if (this.view && !this.view.closed) this.render();
        // An interrupted job may already have finished. A fresh status tells
        // us whether its review still exists before repeating any writes.
        const pending = new Set((this.latest.reviews || []).map(row => row.id));
        if (job.method !== 'approve' || pending.has(job.params.identity)) {
          await this.action(job.method, job.params);
          filed = true;
          const remaining = new Set((this.latest.reviews || []).map(row => row.id));
          if (job.method === 'approve' ? remaining.has(job.params.identity) : job.params.entries.some(row => remaining.has(row.id))) {
            // A stopped batch can have completed some of its papers. Keep
            // only unfinished decisions so resuming does not replay writes.
            if (job.method === 'approve_routine') job.params.entries = job.params.entries.filter(row => remaining.has(row.id));
            if (this.latest.status?.state !== 'paused') this.queueError = 'Some papers still need review or recovery. Resume to retry, or remove the waiting job to edit its choices.';
            this.filingQueuePaused = true; break;
          }
        }
        await this.changeFilingQueue(() => { this.filingQueue = this.filingQueue.filter(row => row.id !== job.id); });
        this.activeFiling = null;
      }
    } catch (error) {
      this.filingQueuePaused = true; this.queueError = error.message;
    } finally {
      this.activeFiling = null; this.drainingFilings = false;
      await this.saveFilingQueue();
      this.schedule(filed ? 1000 : undefined);
      if (this.view && !this.view.closed) this.render();
      if (this.filingQueue.length && !this.filingQueuePaused && !this.stopped) this.drainFilings().catch(error => this.message(error.message, true));
    }
  },
  renderFilingQueue(parent) {
    if (!this.filingQueue.length && !this.queueError) return;
    const queue = this.el('details', '', parent, {class: 'filing-queue'});
    queue.open = !!this.filingQueueOpen;
    queue.addEventListener('toggle', () => { if (queue.isConnected) this.filingQueueOpen = queue.open; });
    const state = this.queueError ? 'need attention' : this.filingQueuePaused ? 'paused' : 'queued';
    this.el('summary', `${this.filingQueue.length} filing ${this.filingQueue.length === 1 ? 'job' : 'jobs'} ${state}`, queue);
    const card = this.el('div', '', queue, {class: 'filing-queue-jobs', tabindex: '0', 'aria-label': 'Saved filing jobs'});
    this.el('p', this.operation?.method === 'sync'
      ? 'Your choices are saved. Sync is finishing its current paper so filing can continue. You can keep reviewing.'
      : 'Your choices are saved and will be filed one at a time. Approved transfers run at the next sync.', card);
    if (this.queueError) {
      this.el('p', this.queueError, card, {class: 'message error'});
      this.el('p', 'Remove a waiting job from the queue to edit it in Inbox, or resume the queue to retry.', card, {class: 'hint'});
    }
    if (this.filingQueuePaused && this.filingQueue.length) {
      const resume = this.button('Resume queued filings', card, async () => {
        this.queueError = ''; this.filingQueuePaused = false; await this.saveFilingQueue(); await this.drainFilings();
      }, true); resume.dataset.browse = 'true';
    }
    for (const job of this.filingQueue) {
      const line = this.el('div', '', card, {class: 'queued-filing'});
      this.el('p', `${job.id === this.activeFiling ? 'Filing: ' : 'Waiting: '}${job.title}`, line);
      if (job.id !== this.activeFiling) {
        const cancel = this.button('Remove from queue', line, async () => {
          await this.removeFiling(job);
        }); cancel.dataset.browse = 'true';
      }
    }
  },
  disable(value) {
    const syncing = value && this.operation?.method === 'sync' && !this.pausing;
    if (this.view && !this.view.closed) for (const button of this.view.document.querySelectorAll('button')) {
      if (button.closest?.('.sync-decision-dialog')) continue;
      if (button.id === 'notable-pause') { button.disabled = !!this.pausing; continue; }
      if (button.dataset.browse === 'true') { button.disabled = false; continue; }
      if (button.dataset.queue === 'true') {
        button.disabled = !!this.pausing || button.dataset.invalid === 'true'
          || (value && ['check_inbox', 'recheck_review', 'acknowledge_review_finding'].includes(this.operation?.method));
        if (button.dataset.approve === 'true') button.querySelector('.button-label').textContent = value ? 'Add to filing queue' : button.dataset.idleLabel || 'Approve & file';
        continue;
      }
      button.disabled = (this.acquiringPDF && (button.dataset.pdfAcquisition === 'true' || button.id === 'notable-sync'))
        || (value && (!syncing || button.dataset.exclusive === 'true' || button.id === 'notable-sync'))
        || !!this.pausing || button.dataset.invalid === 'true' || (button.id === 'notable-sync' && !this.canSync());
    }
    if (this.view && !this.view.closed) for (const field of this.view.document.querySelectorAll('input, select, textarea')) {
      if (field.closest?.('.sync-decision-dialog')) continue;
      field.disabled = field.dataset?.browse !== 'true' && !field.closest('.review-form')
        && ((value && (!syncing || field.dataset?.exclusive === 'true')) || !!this.pausing);
    }
    this.updateActivity();
  },
  message(value, error = false) {
    if (error) this.recordTransport('ui_error', {characters: String(value).length, details_in_rpc_error_when_safe: true});
    if (this.operation) { this.operation.detail = value; this.operation.updated = Date.now(); }
    if (!this.view || this.view.closed) return;
    const node = this.view.document.getElementById('message');
    if (node) {
      const persistent = value === this.statusMessage();
      const failed = error || (persistent && (this.syncFailure || this.latest?.status?.state === 'needs_attention'));
      const folderNotice = persistent && this.latest?.folder_issues?.length;
      node.textContent = value; node.setAttribute('role', failed ? 'alert' : 'status');
      node.className = failed ? 'message error' : folderNotice ? 'message attention' : 'message';
    }
    this.updateActivity();
  },
  updateActivity() {
    if (!this.view || this.view.closed) return;
    const doc = this.view.document, panel = doc.getElementById('activity');
    if (!panel) return;
    const summary = doc.getElementById('notable-paper-status');
    if (summary) summary.textContent = this.paperStatusMessage();
    const syncStatus = doc.getElementById('notable-sync-status');
    const status = this.syncStatusMessage();
    if (syncStatus && syncStatus.textContent !== status) syncStatus.textContent = status;
    const job = this.operation, syncing = this.busy && job?.method === 'sync';
    const syncDetails = doc.getElementById('notable-sync-details');
    if (syncDetails) {
      syncDetails.hidden = !syncing;
      if (!syncing) syncDetails.open = false;
      const detail = doc.getElementById('notable-sync-step');
      const step = syncing ? job.detail : '';
      if (detail.textContent !== step) detail.textContent = step;
    }
    // Intake checks serialize worker writes, but are silent background work.
    const visibleBusy = !!this.busy && !this.inboxChecking && !syncing;
    panel.hidden = !visibleBusy;
    const message = doc.getElementById('message'); if (message) message.hidden = visibleBusy;
    doc.getElementById('app').setAttribute('aria-busy', String(visibleBusy));
    const stop = doc.getElementById('notable-pause');
    if (stop) stop.querySelector('.button-label').textContent = this.pausing ? 'Stopping…' : visibleBusy ? 'Stop' : 'Pause sync';
    if (!job || !visibleBusy) return;
    const seconds = Math.floor((Date.now() - job.started) / 1000);
    const title = job.yielding ? 'Finishing the current paper so filing can continue…' : job.stopping ? 'Stopping the current check…' : job.title;
    for (const [id, text] of [['activity-title', title], ['activity-detail', job.detail]]) {
      const node = doc.getElementById(id); if (node.textContent !== text) node.textContent = text;
    }
    const quiet = Math.floor((Date.now() - job.updated) / 1000);
    doc.getElementById('activity-time').textContent = `Working for ${seconds < 60 ? seconds + ' seconds' : Math.floor(seconds / 60) + ' min ' + seconds % 60 + ' sec'}`
      + (quiet >= 15 ? ` · Last progress update ${quiet} seconds ago` : '');
    const bar = doc.getElementById('activity-progress'), count = job.detail.match(/(\d+) of (\d+)/);
    if (count && Number(count[2]) > 0) { bar.max = Number(count[2]); bar.value = Number(count[1]); }
    else bar.removeAttribute('value');
  },
  async mount(window) {
    this.view = window;
    if (typeof Zotero !== 'undefined') Zotero.UIProperties?.registerRoot(window.document.querySelector('body'));
    window.addEventListener('keydown', event => {
      if (event.defaultPrevented || event.isComposing || !(event.ctrlKey || event.metaKey) || event.altKey) return;
      const key = event.key.toLowerCase();
      if (key !== 'f' && !(key === 'k' && event.shiftKey)) return;
      const dialogs = window.document.querySelectorAll('dialog[open]');
      const scope = dialogs.length ? dialogs[dialogs.length - 1] : window.document;
      const search = [...scope.querySelectorAll('[data-quick-search]:not([disabled])')]
        .find(field => field.getClientRects().length);
      if (search) { event.preventDefault(); search.focus(); search.select(); }
    });
    clearInterval(this.activityTimer); this.activityTimer = setInterval(() => this.updateActivity(), 1000);
    this.latest ||= {config: {}, status: {}, reviews: [], deletions: [], links: [], types: {}};
    if (!this.latest?.config?.tablet_root && this.statePath) {
      try {
        const config = JSON.parse(await IOUtils.readUTF8(PathUtils.join(this.statePath, 'config.json')));
        this.latest = {...this.latest, config};
      } catch (_) {}
    }
    this.reviewUpgrade = null; this.reviewUpgradeWaiting = false;
    const initialTab = this.requestedTab || (this.latest.config.tablet_root ? 'inbox' : 'setup');
    const navigationSerial = this.tabNavigationSerial || 0;
    this.tab = initialTab; this.render();
    window.addEventListener('unload', () => { if (this.view === window) { this.hideButtonHelp(); this.view = null; clearInterval(this.activityTimer); this.reviewObserver?.disconnect(); this.schedule(); } }, {once: true});
    try {
      if (!this.busy) await this.refresh();
      if (this.view !== window || window.closed) return;
      // A late status response must never undo a tab or case the user chose.
      if ((this.tabNavigationSerial || 0) === navigationSerial && this.tab === initialTab) {
        this.tab = this.requestedTab || (this.latest.config.tablet_root ? 'inbox' : 'setup');
      }
      this.requestedTab = null; this.render(); await this.prepareReviewGuidance();
      this.zoteroInboxDirty = true; await this.checkInbox();
    } catch (error) { if (this.view === window && !window.closed) this.message(error.message, true); }
  },
  el(tag, text, parent, attrs = {}) {
    const node = this.view.document.createElementNS('http://www.w3.org/1999/xhtml', tag);
    if (text) node.textContent = text;
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    parent?.appendChild(node); return node;
  },
  buttonIcons: {
    'Sync now': 'refresh-cw', 'Stop': 'x', 'Import library': 'folder-input',
    'Review': 'file-search', 'Inbox': 'folder-input', 'Library Issues': 'file-search', 'Sync Conflicts': 'sync-conflict', 'Tags': 'tag', 'Linked papers': 'link', 'Setup': 'settings',
    'Combine current tags and retry': 'refresh-cw', 'Remove tags from that unavailable page': 'trash-2',
    'Add tag': 'tag', 'Save tags': 'save', 'Close page editor': 'x',
    'Open page': 'book-open', 'Open paper': 'book-open', 'Review linked papers': 'link',
    'Keep reMarkable copy': 'check', 'Move reMarkable copy to Trash': 'trash-2', 'Restore on reMarkable': 'rotate-ccw',
    'Trash linked PDFs in Zotero': 'trash-2', 'Trash reference everywhere in Zotero': 'trash-2', 'Suggest filename': 'refresh-cw',
    'Open reference in Zotero': 'external-link', 'Open Zotero reference': 'external-link', 'Approve & file': 'check', 'Connect Zotero': 'plug',
    'Choose literature folder…': 'folder-open', 'Scan folder — no library changes': 'folder-search',
    'Show matching reference in Zotero': 'external-link', 'Show in Zotero': 'external-link',
    'Resume link': 'link', 'Unlink (keep both copies)': 'unlink', 'Rebuild search index': 'refresh-cw',
    'Save name & destination': 'save', 'Get reMarkable pairing code': 'external-link',
    'Pair reMarkable': 'plug', 'Refresh folders': 'folder-sync', 'Choose ToSort folder…': 'folder-open',
    'Save setup': 'save', 'Save folder change': 'save', 'Review folder change': 'folder-search',
    'Browse all papers': 'file-search', 'Show only papers needing attention': 'file-search',
    'Find papers': 'search', 'Find keywords and suggest tags': 'tag', 'Use these tags when filing': 'save', 'Add keywords and selected tags': 'save',
    'Change folder': 'folder-open', 'Disconnect folder': 'unlink', 'Confirm disconnect': 'unlink',
    'Cancel change': 'x', 'Cancel': 'x', 'Pause sync': 'x'
  },
  buttonHelp(text, itemSelection = false) {
    if (itemSelection) return 'Select this item to inspect its details. Selection does not approve, merge, or transfer it.';
    const label = text.replace(/ \(\d+\)$/, '');
    const help = {
      'Inbox': 'Review files ready for filing and Zotero references that need a PDF. Papers waiting on Library Issues are listed separately.',
      'Library Issues': 'Inspect duplicate references, repeated attachments, and source-file problems in Zotero.',
      'Sync Conflicts': 'Resolve competing device changes and confirmed deletion decisions.',
      'Linked papers': 'Inspect and manage existing paper connections.',
      'Browse links': 'Search linked papers and filter by reading status.',
      'Review paper names': 'Find papers with “Unknown” in a Zotero title, PDF filename, or saved reMarkable name. Preview filename fixes when available.',
      'Refresh name review': 'Check Zotero titles, PDF filenames, and saved reMarkable names for “Unknown” again.',
      'Queue for review': 'Save this paper in the name review list.',
      'Queue rename': 'Schedule this previewed filename change for the next sync.',
      'Search': 'Find papers or pages with the selected tags.',
      'Clear selection': 'Remove all tags from the search selection.',
      'Open Inbox': 'Review the imported papers and choose which to file.',
      'Tags': 'Find and edit paper or page tags.', 'Import library': 'Preview a Windows library before importing selected papers.',
      'Setup': 'Configure the connected folders, local processing, and account connections.',
      'Sync now': 'Check both devices and transfer approved papers and notes. This can change both libraries.',
      'Pause sync': 'Stop after the current safe step and pause automatic syncing. Completed work is retained.',
      'Recheck Zotero': 'Reread local Zotero items and files without syncing or applying queued filings.',
      'Check Inbox': 'Look for new papers and PDFs, and refresh existing Inbox reviews. This check runs on your computer.',
      'Check my Zotero library': 'Search your Zotero library for another reference with the same DOI or title. View its attachments and collections before deciding what to do.',
      'Open source link': 'Open the web address saved on this reference.',
      'Find and attach PDF online': 'Ask Zotero to look for a PDF online and attach it to this reference.',
      'Attach downloaded PDF': 'Choose a PDF from your computer to attach to this Zotero reference.',
      'Dismiss this reference': 'Remove this entry from Inbox while keeping the reference in Zotero.',
      'Check connection': 'Inspect the native merge survivor, connected PDFs, retained original, and tablet document. Preview only; no files or links are changed.',
      'Reconnect surviving reference': 'Recheck the preview and update this private connection to Zotero’s exact merge survivor. Keep the same PDFs, tablet document, notes, and pause state.',
      'Close unavailable review': 'Verify that this unlinked source is no longer live in Zotero, then close its saved review. Files, references, and tablet connections are kept. A restored source can return to Inbox.',
      'Cancel repair': 'Close this preview without changing the connection.',
      'Check my changes': 'Recheck only the items in this case. Resolved findings disappear; remaining issues stay visible.',
      'Open Library Issues': 'Open the cases that need attention before the waiting papers can be filed. Your filing choices are kept.',
      'Resolve in Library Issues': 'Open the related case before filing this incoming paper. Your draft remains saved.',
      'Approve & file': 'Apply this incoming paper’s filename, tags, and filing choices. Approved tablet transfers run at the next sync.',
      'Approve for reMarkable': 'Approve this existing Zotero paper for transfer to its selected tablet destination at the next sync.',
      'Add to Zotero': 'Create or reuse the Zotero record for this tablet paper, retaining its tablet document.',
      'Link to existing reMarkable copy': 'Reuse the selected document already on reMarkable and retain its notes. Do not upload a second copy.',
      'Link to existing Zotero reference': 'Connect this tablet paper to the selected existing Zotero copy instead of importing another reference.',
      'Resume saved filing': 'Continue the interrupted filing using its original saved choices.',
      'Compare in Zotero': 'Select these references together in Zotero to inspect their details. Opening them leaves this review pending.',
      'Keep these references separate': 'Mark this reference comparison as intentional. Changed evidence can bring the question back. This does not merge, delete, or file papers.',
      'Keep these PDFs separate': 'Mark this PDF arrangement as intentional. Changed evidence can bring the question back. This does not merge, delete, or file papers.',
      'Keep these items separate': 'Mark this specific arrangement as intentional. Changed evidence can bring the question back. This does not merge, delete, or file papers.',
      'Review this choice again': 'Undo this saved choice and review this specific arrangement again.',
      'Suggest filename': 'Suggest a filename from the current citation details. This changes the form only; no file is renamed until you approve.',
      'Retry saved review update': 'Retry the local update of older saved reviews without syncing.',
      'Use current Zotero suggestions': 'Discard this form’s unsaved draft and show the current suggestions.',
      'Use earlier filing choices for this file': 'Copy the earlier draft into this changed file’s form. Filing still requires approval.',
      'Remove from queue': 'Remove this saved filing decision from the queue. Keep the paper pending.',
      'Resume queued filings': 'Continue applying the saved filing decisions one at a time.',
      'Dismiss this intake review': 'Ignore this intake entry without deleting its source file.',
      'Dismiss this exception': 'Dismiss this import exception without deleting its original file.',
      'Repair bundled helper': 'Re-extract the verified bundled worker without changing the library.',
      'Refresh folders': 'Read the current folder choices from the connected services.',
      'Rebuild search index': 'Rebuild the local search data from retained documents and notes.',
      'Get reMarkable pairing code': 'Open reMarkable’s pairing page to obtain a connection code.',
      'Pair reMarkable': 'Save the supplied pairing code’s account connection for this plugin.',
      'Connect Zotero': 'Authorize the existing local Zotero API connection for this plugin.',
      'Save tags': 'Apply the displayed paper and page tags to the selected items.',
      'Add tag': 'Add the typed tag to this draft. Save or file the paper to apply it.',
      'Add to filing queue': 'Save this paper’s current choices for filing after the active operation finishes.',
      'Resume link': 'Resume this existing connection without creating another tablet document.',
      'Unlink (keep both copies)': 'Pause this paper’s connection while retaining both copies and their notes.',
      'Review folder change': 'Preview the effects of the displayed folder settings before saving.',
      'Scan folder — no library changes': 'Read the selected Windows folder and prepare an import preview. Do not import files yet.',
      'Review linked papers': 'Open the existing connections to inspect the paper named in this notice.',
      'Close page editor': 'Close the page-tag editor and return to tag search.',
      'Save name & destination': 'Apply this linked paper’s name and folder choice using its reviewed revision.',
      'Disconnect folder': 'Preview disconnecting this folder pair while retaining its papers and connection history.',
      'Confirm disconnect': 'Apply the displayed disconnection. Papers and tablet notes remain in place.',
      'Change folder': 'Preview choosing a different connected folder pair.',
      'Cancel change': 'Discard the proposed connection change.', 'Cancel': 'Leave this decision unapplied.',
      'Save folder change': 'Apply the reviewed folder connection change without deleting its papers.',
      'Save setup': 'Save the displayed processing, intake, and connection preferences.',
      'Combine current tags and retry': 'Combine the displayed tag copies and retry the reviewed tag update.',
      'Remove tags from that unavailable page': 'Remove only the saved tag associations for this unavailable page.',
      'Retry import for this file': 'Retry the saved import step for this file using its verified original.',
      'Apply Zotero name and location': 'Check both devices, then apply Zotero’s name and folder to both copies now. PDF contents and notes are kept.',
      'Apply reMarkable name and location': 'Check both devices, then apply reMarkable’s name and folder to both copies now. PDF contents and notes are kept.',
      'Keep reMarkable copy': 'Retain the tablet document and keep its deleted-Zotero connection paused.',
      'Move reMarkable copy to Trash': 'Send the reviewed tablet document to Trash after confirming its current state.',
      'Restore on reMarkable': 'Restore this reviewed tablet document to the selected connected folder.',
      'Trash linked PDFs in Zotero': 'Recheck this decision, then move the linked notes PDF and any recorded source PDF to Zotero Trash. Keep the reference and its other contents.',
      'Trash reference everywhere in Zotero': 'Review this choice, then move this Zotero reference, all its attachments, and all its notes to Zotero Trash.'
    };
    if (help[label]) return help[label];
    if (/ ×$/.test(label)) return 'Remove this tag from the current draft. Save the tags to apply that removal.';
    if (/^(Show|Open) /.test(label)) return 'Open the specified item in Zotero or its file viewer. Leave the current decision pending.';
    if (/^(Find|Search) /.test(label)) return 'Search the indicated local items and show candidates. No paper is filed or merged.';
    if (/^Choose /.test(label)) return 'Open a folder picker. The chosen path is a draft until the displayed settings are saved.';
    if (/^Review:/.test(label)) return 'Open the related saved review while retaining this paper’s choices.';
    if (/^File \d+ routine/.test(label)) return 'Queue these verified routine papers for filing using the displayed destination.';
    if (/^Import \/ reuse/.test(label)) return 'Import or reuse the selected verified files while retaining their Windows originals.';
    if (/^(Find keywords|Use these tags|Add keywords)/.test(label)) return 'Review and apply the displayed keyword choices; unselected suggestions are not added.';
    if (/^(Browse|Show only)/.test(label)) return 'Change which saved items are displayed without applying decisions.';
    return 'Use this control for the item shown in the current section. Inspect its choices before applying an update.';
  },
  hideButtonHelp() {
    clearTimeout(this.helpHoverTimer);
    this.helpHoverTimer = null;
    this.helpOwner?.removeAttribute('aria-describedby');
    this.helpOwner = null;
    this.helpPopup?.remove();
    this.helpPopup = null;
  },
  setButtonHelp(node, help) { node.dataset.buttonHelp = help; node.setAttribute('aria-description', help); },
  attachButtonHelp(node, help) {
    this.setButtonHelp(node, help);
    const show = () => {
      if (!node.isConnected || !this.view || this.view.closed || node.ownerDocument !== this.view.document) return;
      this.hideButtonHelp();
      const popup = this.el('div', node.dataset.buttonHelp, this.view.document.querySelector('body'), {id: 'notable-button-help', role: 'tooltip', class: 'button-help'});
      this.helpPopup = popup; this.helpOwner = node; node.setAttribute('aria-describedby', popup.id);
      const r = node.getBoundingClientRect();
      popup.style.left = Math.max(8, Math.min(r.left, this.view.innerWidth - Math.min(360, this.view.innerWidth - 16) - 8)) + 'px';
      popup.style.top = Math.max(8, Math.min(r.bottom + 6, this.view.innerHeight - popup.offsetHeight - 8)) + 'px';
    };
    node.addEventListener('pointerenter', event => {
      if (event.pointerType === 'touch') return;
      this.hideButtonHelp();
      this.helpHoverTimer = setTimeout(show, 500);
    });
    node.addEventListener('focus', () => { if (node.matches(':focus-visible')) show(); });
    node.addEventListener('pointerleave', () => this.hideButtonHelp()); node.addEventListener('blur', () => this.hideButtonHelp());
    node.addEventListener('keydown', event => { if (event.key === 'Escape') this.hideButtonHelp(); if (event.key === 'F1') { event.preventDefault(); show(); } });
    let timer;
    node.addEventListener('pointerdown', event => {
      this.hideButtonHelp();
      delete node.dataset.helpPress;
      if (event.pointerType === 'touch') timer = setTimeout(() => { node.dataset.helpPress = 'true'; show(); }, 500);
    });
    for (const event of ['pointerup', 'pointercancel', 'pointerleave']) node.addEventListener(event, () => clearTimeout(timer));
  },
  button(text, parent, click, primary = false, icon = null, {help = true} = {}) {

    // Every action row gets the same wrapping and spacing. Navigation, review
    // lists and tag chips already own their layouts.
    if (!parent.matches('nav, aside, .actions, .tag-chips')) {
      parent = parent.lastElementChild?.classList.contains('actions') ? parent.lastElementChild
        : this.el('div', '', parent, {class: 'actions'});
    }
    const node = this.el('button', '', parent, {type: 'button', class: primary ? 'primary' : ''});
    icon ||= this.buttonIcons[text.replace(/ \(\d+\)$/, '')] || (/^(File \d+ routine|Import \/ reuse \d+ routine)/.test(text) ? 'folder-input' : /^Use (desktop|tablet):/.test(text) ? 'check' : null);
    if (icon) this.el('span', '', node, {class: 'button-icon icon-' + icon, 'aria-hidden': 'true'});
    this.el('span', text, node, {class: 'button-label'});
    if (icon === 'trash-2') node.classList.add('danger');
    if (help) this.attachButtonHelp(node, this.buttonHelp(text, parent.matches('aside')));
    node.addEventListener('click', () => { if (node.dataset.helpPress === 'true') { delete node.dataset.helpPress; return; } this.hideButtonHelp(); Promise.resolve().then(click).catch(error => this.message(error.message, true)); }); return node;
  },
  field(label, parent, value = '', type = 'input', settings = {}) {
    const wrap = settings.wrap === true, sensitive = settings.sensitive === true;
    const enhanced = type === 'input' && !sensitive;
    const wrapper = this.el(enhanced || wrap ? 'div' : 'label', '', parent, {class: enhanced || wrap ? 'field readable-field' : 'field'});
    const caption = enhanced || wrap ? this.el('label', '', wrapper) : wrapper;
    this.el('span', label, caption);
    const input = this.el(wrap ? 'textarea' : type, '', caption);
    input.value = value; input.dataset.notableField = label;
    if (sensitive || (type !== 'input' && !wrap)) return input;
    if (wrap) {input.rows = 2; input.setAttribute('wrap', 'soft'); input.classList.add('notable-wrapped-field');}
    const details = !wrap ? this.el('details', '', wrapper, {class: 'readable-field-full'}) : null;
    let preview, copyStatus;
    if (details) {
      details.hidden = true;
      details.dataset.reviewDetail = 'full-value:' + label;
      this.el('summary', 'Show full value', details);
      preview = this.el('pre', '', details, {class: 'readable-field-value', tabindex: '0', 'aria-label': 'Full ' + label.toLowerCase()});
      preview.dataset.notableField = label + ' full value';
      const copy = this.button('Copy full value', details, () => {
        Zotero.Utilities.Internal.copyTextToClipboard(input.value);
        copyStatus.textContent = 'Copied.';
      });
      copy.dataset.browse = 'true';
      copyStatus = this.el('span', '', details, {class: 'readable-field-copy-status', role: 'status'});
      input.notableValueDetails = details;
    }
    input.notableRefreshValue = () => {
      if (wrap) {
        const previous = input.value, normalized = previous.replace(/\r\n|\r|\n/g, ' ');
        if (previous !== normalized) {
          const start = input.selectionStart, end = input.selectionEnd, direction = input.selectionDirection;
          input.value = normalized;
          if (Number.isInteger(start) && typeof input.setSelectionRange === 'function') {
            input.setSelectionRange(previous.slice(0, start).replace(/\r\n|\r|\n/g, ' ').length,
              previous.slice(0, end).replace(/\r\n|\r|\n/g, ' ').length, direction);
          }
        }
        if (input.style && input.clientWidth > 0) {
          input.style.height = 'auto'; input.style.height = (input.scrollHeight + 2) + 'px';
        }
      } else {
        input.title = input.value;
        preview.textContent = input.value;
        const overflow = input.clientWidth > 0 && input.scrollWidth > input.clientWidth + 1;
        details.hidden = !overflow;
        if (!overflow) details.open = false;
        if (preview.dataset.lastValue !== input.value) copyStatus.textContent = '';
        preview.dataset.lastValue = input.value;
      }
    };
    for (const event of ['input', 'change', 'focus']) input.addEventListener(event, input.notableRefreshValue);
    const view = input.ownerDocument?.defaultView || this.view;
    const afterLayout = () => {
      if (typeof view?.requestAnimationFrame === 'function') view.requestAnimationFrame(() => {if (input.isConnected !== false) input.notableRefreshValue();});
      else input.notableRefreshValue();
    };
    for (let ancestor = wrapper.parentElement; ancestor; ancestor = ancestor.parentElement) {
      if (ancestor.localName === 'details') ancestor.addEventListener('toggle', afterLayout);
    }
    if (view?.addEventListener && !view.notableReadableFieldsResize) {
      view.notableReadableFieldsResize = () => this.refreshReadableFields(view.document);
      view.addEventListener('resize', view.notableReadableFieldsResize);
    }
    input.notableRefreshValue(); afterLayout(); return input;
  },
  refreshReadableFields(doc = this.view?.document) {
    for (const input of doc?.querySelectorAll?.('[data-notable-field]') || []) input.notableRefreshValue?.();
  },
  checkbox(label, parent, checked) {
    const wrapper = this.el('label', '', parent, {class: 'checkbox-field'});
    const input = this.el('input', '', wrapper, {type: 'checkbox'}); input.checked = checked;
    this.el('span', label, wrapper); return input;
  },
  select(label, parent, options, selected = '', multiple = false) {
    const input = this.field(label, parent, '', 'select');
    if (multiple) { input.multiple = true; input.size = 5; }
    for (const [key, label] of options) { const option = this.el('option', label, input, {value: key}); option.selected = multiple ? selected.includes(key) : key === selected; }
    return input;
  },
  paperSelect(label, parent, options, selected = '', settings = {}) {
    const input = this.select(label, parent, options, selected);
    input.parentElement.hidden = true; input.tabIndex = -1;
    const choices = () => Array.from(input.options, option => ({value: option.value,
      label: option.textContent, path: option.textContent, parent: '',
      detail: settings.details?.(option) || option.dataset?.detail || ''}));
    const chooser = NotableFolderChooser.create({document: parent.ownerDocument, parent, label,
      options: choices(), value: input.value, mode: 'record', search: this.chooserSearch.bind(this)});
    input.notableRecordChooser = chooser;
    input.notableRefreshRecords = () => {chooser.setOptions(choices()); chooser.value = input.value;};
    chooser.addEventListener('change', () => {
      input.value = chooser.value;
      const Event = parent.ownerDocument.defaultView.Event;
      input.dispatchEvent(new Event('input', {bubbles: true}));
      input.dispatchEvent(new Event('change', {bubbles: true}));
    });
    input.addEventListener('change', () => {chooser.value = input.value;});
    return input;
  },
  folderOptions(choices, kind) {
    const rows = kind === 'tablet' ? this.folderCatalog?.folders || [] : this.collectionCatalog();
    const by = new Map(rows.map(row => [kind === 'tablet' ? row.id : row.key, row]));
    if (kind === 'tablet') {
      for (const [key, link] of Object.entries(this.latest.mappings || {})) {
        if (!by.has(link.cloud)) by.set(link.cloud, {id: link.cloud,
          name: key === this.latest.config.zotero_root ? undefined : link.tablet_name || link.name,
          parent: link.root ? '' : link.cparent || this.latest.mappings[link.zparent]?.cloud || ''});
      }
    }
    const pairs = kind === 'paired' ? new Map(this.mappedDestinations().map(row => [row.key, row])) : new Map();
    return choices.map(([value, text]) => {
      const row = by.get(value), pair = pairs.get(value);
      const path = pair?.zotero || text;
      return {value, label: row?.name || path.split(' / ').pop(), path,
        parent: kind === 'tablet' ? row?.parent || '' : row?.parentCollection || '',
        detail: pair && pair.tablet !== pair.zotero ? `reMarkable: ${pair.tablet}` : ''};
    });
  },
  rememberFolder(kind, values) {
    if (!Array.isArray(values)) return;
    this.folderRecents ||= {};
    this.folderRecents[kind] = [...new Set(values.filter(value => typeof value === 'string' && value))].slice(0, 6);
    const saved = JSON.stringify(this.folderRecents);
    this.folderRecentWrite = (this.folderRecentWrite || Promise.resolve()).catch(() => {}).then(() =>
      IOUtils.writeUTF8(PathUtils.join(this.statePath, 'folder-recents.json'), saved)).catch(() => {});
  },
  folderSelect(label, parent, options, selected = '', multiple = false, kind = 'zotero', settings = {}) {
    // Keep a native backing control for the existing draft and validation code.
    const input = this.select(label, parent, options, selected, multiple);
    input.parentElement.hidden = true;
    input.tabIndex = -1;
    const chooser = NotableFolderChooser.create({document: parent.ownerDocument, parent, label,
      options: this.folderOptions(options, kind), value: multiple ? Array.from(input.selectedOptions, option => option.value) : input.value,
      multiple, recents: this.folderRecents?.[kind] || [], search: this.chooserSearch.bind(this),
      onRemember: value => this.rememberFolder(kind, value),
      onNew: settings.create === false ? undefined : async parentValue => {
        const creationKind = settings.createKind || kind;
        const creationParent = settings.parentForCreate ? settings.parentForCreate(parentValue) : parentValue;
        const created = await this.newFolderDialog(creationKind, creationParent, settings);
        if (!created) return;
        const choices = settings.choices ? settings.choices() : kind === 'paired'
          ? this.mappedDestinations().map(row => [row.key, row.zotero])
          : this.paths(kind === 'tablet' ? this.folderCatalog?.folders || [] : this.latest.collections || [],
            kind === 'tablet' ? 'id' : 'key', kind === 'tablet' ? 'parent' : 'parentCollection', 'name');
        this.reconcileOptions(input, [...options.filter(([key]) => !key), ...choices]);
        return this.folderOptions(choices, kind).find(option => option.value === (settings.createdKey ? settings.createdKey(created) : created.key));
      }});
    input.notableChooser = chooser;
    input.notableRefreshChooser = () => {
      chooser.setOptions(this.folderOptions(Array.from(input.options, option => [option.value, option.textContent]), kind));
      chooser.value = multiple ? Array.from(input.selectedOptions, option => option.value) : input.value;
    };
    chooser.addEventListener('change', () => {
      if (multiple) {
        const values = new Set(Array.from(chooser.selectedOptions, option => option.value));
        for (const option of input.options) option.selected = values.has(option.value);
      } else input.value = chooser.value;
      const Event = parent.ownerDocument.defaultView.Event;
      input.dispatchEvent(new Event('input', {bubbles: true}));
      input.dispatchEvent(new Event('change', {bubbles: true}));
    });
    input.addEventListener('change', () => { chooser.value = multiple ? Array.from(input.selectedOptions, option => option.value) : input.value; });
    return input;
  },
  newFolderDialog(kind, parentValue, settings = {}) {
    const doc = this.view.document;
    const dialog = this.el('dialog', '', doc.body || doc.querySelector('body'), {class: 'folder-create'});
    const paired = kind === 'paired', tablet = kind === 'tablet';
    const heading = paired ? 'Create linked collection and folder' : tablet ? 'Create reMarkable folder' : 'Create Zotero collection';
    this.el('h2', heading, dialog);
    const choices = settings.parentChoices ? settings.parentChoices() : kind === 'paired' ? this.mappedDestinations().map(row => [row.key, row.zotero])
      : this.paths(tablet ? this.folderCatalog?.folders || [] : this.latest.collections || [],
        tablet ? 'id' : 'key', tablet ? 'parent' : 'parentCollection', 'name');
    const parents = this.folderSelect('Create inside', dialog,
      [...(paired || settings.parentChoices ? [] : [['', tablet ? 'reMarkable top level' : 'My Library']]), ...choices],
      parentValue || settings.newParent || '', false, kind, {create: false});
    const name = this.field(tablet ? 'Folder name' : 'Collection name', dialog);
    const tabletName = paired ? this.field('reMarkable folder name (optional; defaults to collection name)', dialog) : null;
    this.el('p', paired ? 'Creates both destinations and links them. Existing siblings with these names are reused. Your paper will be filed only when you use its filing button.'
      : 'An existing sibling with this name is selected. Your paper and its filing choices stay in place.', dialog, {class: 'hint'});
    if (kind === 'zotero') this.el('p', 'For a collection that should stay only in Zotero, choose My Library or a parent outside the synced tree. Collections inside that tree can be mirrored on the next sync.', dialog, {class: 'hint'});
    const error = this.el('p', '', dialog, {role: 'alert', class: 'message error'});
    return new Promise(resolve => {
      let working = false;
      const finish = result => { dialog.close(); dialog.remove(); resolve(result); };
      dialog.addEventListener('cancel', event => { event.preventDefault(); if (!working) finish(null); });
      const actions = this.el('div', '', dialog, {class: 'actions'});
      const cancel = this.button('Cancel', actions, () => { if (!working) finish(null); });
      const create = this.button('Create', actions, async () => {
        if (working) return;
        if (!name.value.trim()) { error.textContent = 'Enter a name.'; name.focus(); return; }
        working = true; create.disabled = cancel.disabled = true;
        try {
          const result = await this.action('create_destination', {kind, parent: parents.value,
            name: name.value.trim(), tablet_name: tabletName?.value.trim() || ''}, false);
          if (!result) throw Error('Wait for the current operation, then try again.');
          if (result.destination_catalog) {
            if (kind !== 'tablet') this.latest.collections = result.destination_catalog.collections;
            this.folderCatalog = {
              collections: kind === 'tablet' ? this.latest.collections || [] : result.destination_catalog.collections,
              folders: kind === 'zotero' ? this.folderCatalog?.folders || [] : result.destination_catalog.folders};
          }
          const created = result.created_destination;
          if (!created?.complete) {
            const locations = Object.entries(created?.locations || {}).filter(([, state]) => state !== 'not_requested')
              .map(([side, state]) => `${side === 'zotero' ? 'Zotero collection' : 'reMarkable folder'} is ${state === 'ready' ? 'ready' : 'not yet confirmed'}.`).join(' ');
            throw Error([locations, created?.error || 'Creation was interrupted. Retry to reuse any destination already created.'].filter(Boolean).join(' '));
          }
          this.refreshCollectionNames();
          if (settings.choices && !settings.choices().some(([key]) => key === (settings.createdKey ? settings.createdKey(created) : created.key))) {
            throw Error('This destination exists, but is outside the choices allowed here. If it is already a tablet destination, select it under Destination folders. Otherwise, choose another parent or name.');
          }
          finish(created);
        } catch (failure) {
          error.textContent = failure.message;
        } finally {
          working = false;
          if (dialog.isConnected) { create.disabled = cancel.disabled = false; }
        }
      }, true);
      dialog.showModal(); name.focus();
    });
  },
  paths(rows, id, parent, name) {
    const by = new Map(rows.map(row => [row[id], row]));
    const label = row => { const parts = [], seen = new Set(); while (row && !seen.has(row[id])) { seen.add(row[id]); parts.unshift(row[name]); row = by.get(row[parent]); } return parts.join(' / '); };
    return rows.map(row => [row[id], label(row)]).sort((a, b) => a[1].localeCompare(b[1]));
  },
  mappedDestinations() {
    const config = this.latest.config, mappings = this.latest.mappings || {};
    const blocked = new Set(this.latest.blocked_collections || []);
    const unknown = 'Folder name unavailable (refresh folders in Setup)';
    const collections = new Map(this.collectionCatalog().map(row => [row.key, row]));
    const folders = new Map((this.folderCatalog?.folders || []).map(row => [row.id, row]));
    // Saved mappings keep both paths readable before a folder refresh. The
    // roots may have different names; never substitute the Zotero root name
    // for the tablet root. Prefer current catalog names when available.
    for (const [key, link] of Object.entries(mappings)) {
      if (this.nativeCollections && !collections.has(key)) continue;
      const root = key === config.zotero_root;
      if (!collections.has(key)) collections.set(key, {key, name: root ? config.zotero_path || unknown : link.desktop_name || link.name || unknown,
        parentCollection: root ? false : link.zparent});
      if (!folders.has(link.cloud)) folders.set(link.cloud, {id: link.cloud,
        name: root ? config.tablet_path || unknown : link.tablet_name || link.name || unknown,
        parent: root ? '' : link.cparent || mappings[link.zparent]?.cloud});
    }
    const tabletPaths = new Map(this.paths([...folders.values()], 'id', 'parent', 'name'));
    return this.paths([...collections.values()], 'key', 'parentCollection', 'name')
      .filter(([key]) => mappings[key] && !blocked.has(key)).map(([key, zotero]) => {
        const tablet = tabletPaths.get(mappings[key].cloud) || unknown;
        return {key, zotero, tablet, label: `Zotero: ${zotero} → reMarkable: ${tablet}`};
      });
  },
  destinationSelect(label, parent, selected) {
    const destinations = this.mappedDestinations();
    const input = this.folderSelect(label, parent, destinations.map(row => [row.key, row.zotero]), selected, false, 'paired');
    input.dataset.folderDestination = 'true';
    const summary = this.el('details', '', parent, {class: 'folder-pair'});
    const update = () => {
      summary.replaceChildren();
      this.el('summary', 'Linked locations', summary);
      const destination = this.mappedDestinations().find(row => row.key === input.value);
      if (!destination) { this.el('p', 'Refresh folders in Setup to see the linked folders.', summary); return; }
      for (const [label, path] of [['Zotero collection: ', destination.zotero], ['reMarkable folder: ', destination.tablet]]) {
        const line = this.el('p', '', summary); this.el('strong', label, line); this.el('span', path, line);
      }
      input.title = destination.label;
    };
    input.notableRefreshNames = () => {
      this.reconcileOptions(input, this.mappedDestinations().map(row => [row.key, row.zotero]));
      update();
    };
    input.addEventListener('change', update); update();
    return input;
  },
  supportingInformationPicker(parent, initial = {}) {
    const panel = this.el('section', '', parent);
    this.el('p', 'Supporting information is an extra attachment under its main paper. The main paper’s citation stays unchanged.', panel, {class: 'hint'});
    const {toolbar, input: query} = this.quickSearch(panel, {label: 'Search main paper titles',
      value: initial.main_query || '', submit: () => find.click()});
    query.dataset.notableField = 'Main paper title contains';
    const actions = this.el('div', '', toolbar, {class: 'actions'});
    const select = this.paperSelect('Main paper in Zotero (required)', panel, [['', 'Choose the main paper…']], '');
    const status = this.el('p', '', panel, {class: 'hint', role: 'status'});
    let generation = 0;
    const load = async () => {
      const current = ++generation, terms = this.searchTerms(query.value);
      const items = await Zotero.Items.getAll(Zotero.Libraries.userLibraryID, false, false);
      const choices = [];
      for (const item of items) {
        if (item.deleted || !item.isRegularItem()) continue;
        await item.loadAllData();
        if (!select.isConnected || current !== generation) return;
        const record = item.toJSON(), title = record.title || '(Untitled reference)';
        if (!this.matchesSearchTerms(title, terms)) continue;
        const attachments = typeof Zotero.Items.get === 'function' && typeof item.getAttachments === 'function'
          ? item.getAttachments().map(id => Zotero.Items.get(id)).filter(attachment => attachment && !attachment.deleted && attachment.isPDFAttachment?.()) : [];
        const filenames = [];
        for (const attachment of attachments) {
          await attachment.loadAllData();
          if (!select.isConnected || current !== generation) return;
          const filename = attachment.toJSON().filename; if (filename) filenames.push(filename);
        }
        choices.push({key: item.key, title, record, detail: [...filenames, record.publicationTitle, record.date].filter(Boolean).join(' · ')});
      }
      if (!select.isConnected || current !== generation) return;
      const selected = select.value || initial.main_reference || '';
      select.replaceChildren(); this.el('option', 'Choose the main paper…', select, {value: ''});
      for (const choice of choices.sort((a, b) => a.title.localeCompare(b.title))) {
        const option = this.el('option', choice.title, select, {value: choice.key});
        option.dataset.detail = choice.detail;
        option.dataset.metadata = JSON.stringify(Object.fromEntries(['title', 'itemType', 'creators', 'date', 'DOI', 'publicationTitle', 'publisher', 'university', 'extra'].map(key => [key, choice.record[key] ?? ''])));
      }
      select.value = choices.some(choice => choice.key === selected) ? selected : '';
      select.notableRefreshRecords();
      status.textContent = `${choices.length} papers found. Select the main article, not another supporting file.`;
      select.dispatchEvent(new this.view.Event('change'));
    };
    const find = this.button('Find main paper', actions, load); find.dataset.browse = 'true';
    return {panel, query, select, load: () => load().catch(error => { if (status.isConnected) status.textContent = error.message; })};
  },
  openTab(tab) {
    this.tabNavigationSerial = (this.tabNavigationSerial || 0) + 1;
    this.queueSelections ||= {};
    this.queueSelections[this.tab] = this.selected;
    this.tab = tab;
    this.selected = this.queueSelections[tab] || null;
    this.reviewScrollTop = 0;
    this.render();
    this.view?.document.querySelector?.('nav button[aria-pressed="true"]')?.focus?.({preventScroll: true});
  },
  render() {
    if (!this.view || this.view.closed) return;
    this.inboxRenderPending = false;
    this.collectionCatalog();
    this.hideButtonHelp();
    const focus = this.renderedTab === this.tab ? this.captureFocus() : null;
    this.tagSearchFocus = this.tab === 'tags' ? focus?.label : null;
    if (this.tagSearch) this.tagSearch.drawResults = null;
    const reviewContext = ['review', 'inbox', 'library', 'sync-review'].includes(this.tab) && this.renderedTab === this.tab ? this.captureReviewContext() : null;
    const previousList = this.view.document.querySelector('.review-layout > aside');
    if (previousList && this.renderedTab === this.tab) this.reviewScrollTop = previousList.scrollTop;
    this.reviewObserver?.disconnect();
    const syncDetailsOpen = this.view.document.getElementById('notable-sync-details')?.open;
    const root = this.view.document.getElementById('app'); root.replaceChildren();
    const head = this.el('header', '', root); const title = this.el('div', '', head);
    this.el('h1', this.shortName, title); this.el('p', 'Sync papers, handwriting, and tags with Zotero.', title);
    const sync = this.button('Sync now', head, () => this.action('sync'), true);
    sync.id = 'notable-sync'; sync.disabled = this.busy || !this.canSync();
    const pause = this.button('Pause sync', head, () => this.pauseSync()); pause.id = 'notable-pause'; pause.disabled = !!this.pausing;
    const nav = this.el('nav', '', root, {'aria-label': this.shortName + ' sections'});
    for (const [tab, label] of [['inbox', 'Inbox'], ['library', 'Library Issues'], ['sync-review', 'Sync Conflicts'], ['links', 'Linked papers'], ['tags', 'Tags'], ['import', 'Import library'], ['setup', 'Setup']]) {
      const count = ['inbox', 'library', 'sync-review'].includes(tab) ? this.queueRows(tab).length
        + (tab === 'sync-review' ? this.syncDecisionCount() : tab === 'library' ? this.linkedOriginalCases().length : 0) : 0;
      const button = this.button(label + (['inbox', 'library', 'sync-review'].includes(tab) ? ` (${count})` : ''),
        nav, () => this.openTab(tab));
      if (tab === 'sync-review') { button.dataset.syncConflicts = String(count); if (count) button.classList.add('sync-conflicts-attention'); }
      button.dataset.browse = 'true';
      button.dataset.notableField = `Section ${label}`;
      button.setAttribute('aria-pressed', String((this.tab === 'review' ? 'inbox' : this.tab) === tab));
    }
    const status = this.el('div', '', root, {class: 'workspace-status'});
    const failed = this.syncFailure || this.latest.status?.state === 'needs_attention';
    const messageClass = failed ? 'message error' : this.latest.folder_issues?.length ? 'message attention' : 'message';
    this.el('div', this.statusMessage(), status, {id: 'message', role: failed ? 'alert' : 'status', class: messageClass, tabindex: '0'});
    if (this.latest.config.tablet_root) this.el('span', this.syncStatusMessage(), status, {id: 'notable-sync-status', class: 'hint', role: 'status'});
    if (this.latest.config.tablet_root) this.el('span', this.paperStatusMessage(), status, {id: 'notable-paper-status', class: 'hint', role: 'status'});
    const syncDetails = this.el('details', '', status, {id: 'notable-sync-details'});
    syncDetails.open = !!syncDetailsOpen;
    this.el('summary', 'Sync details', syncDetails);
    this.el('p', '', syncDetails, {id: 'notable-sync-step', tabindex: '0'});
    const activity = this.el('section', '', status, {id: 'activity', class: 'activity', 'aria-label': 'Current activity'});
    this.el('span', '', activity, {class: 'activity-spinner', 'aria-hidden': 'true'});
    const info = this.el('div', '', activity, {class: 'activity-content'});
    const announcement = this.el('div', '', info, {role: 'status'});
    this.el('strong', '', announcement, {id: 'activity-title', tabindex: '0'});
    this.el('p', '', announcement, {id: 'activity-detail', tabindex: '0'});
    this.el('progress', '', announcement, {id: 'activity-progress', 'aria-label': 'Current step progress'});
    this.el('small', '', info, {id: 'activity-time', 'aria-live': 'off', tabindex: '0'});
    this.renderFilingQueue(root);
    this.el('p', '', root, {id: 'notable-after-sync', class: 'hint', role: 'status'});
    this.refreshAfterSync();
    this.el('div', '', root, {id: 'notable-tag-edits', 'aria-live': 'polite'});
    this.refreshTagEditQueue();
    const content = this.el('main', '', root);
    if (this.tab === 'setup') this.renderSetup(content);
    else if (this.tab === 'import') this.renderImport(content);
    else if (this.tab === 'links') this.renderLinks(content);
    else if (this.tab === 'tags') this.renderTags(content);
    else this.renderReview(content, this.tab === 'review' ? 'inbox' : this.tab);
    this.disable(this.busy || this.acquiringPDF);
    this.restoreReviewContext(reviewContext);
    this.restoreReviewList();
    this.restoreFocus(focus);
    this.refreshReadableFields();
    this.renderedTab = this.tab;
  },
  captureReviewContext() {
    const doc = this.view.document;
    const outer = doc.querySelector('body') || doc.scrollingElement;
    return {selected: this.renderedReviewId, outer: outer?.scrollTop || 0,
      detail: doc.querySelector('.review-layout > .review-form')?.scrollTop || 0,
      open: [...doc.querySelectorAll('details[data-review-detail]')].filter(node => node.open).map(node => node.dataset.reviewDetail)};
  },
  restoreReviewContext(context) {
    if (!context) return;
    if (context.selected === this.renderedReviewId) {
      const detail = this.view.document.querySelector('.review-layout > .review-form');
      const open = new Set(context.open);
      for (const node of this.view.document.querySelectorAll('details[data-review-detail]')) node.open = open.has(node.dataset.reviewDetail);
      if (detail) detail.scrollTop = context.detail || 0;
    }
    // Zotero's XUL workspace scrolls its HTML body, unlike an ordinary tab.
    const outer = this.view.document.querySelector('body') || this.view.document.scrollingElement;
    if (outer) outer.scrollTop = context.outer;
  },
  captureFocus() {
    const doc = this.view?.document, active = doc?.activeElement;
    const label = active?.dataset?.notableField;
    if (!label) return null;
    const fields = [...doc.querySelectorAll('[data-notable-field]')].filter(node => node.dataset.notableField === label);
    return {label, index: fields.indexOf(active), start: active.selectionStart, end: active.selectionEnd};
  },
  restoreFocus(focus) {
    if (!focus) return;
    const field = [...this.view.document.querySelectorAll('[data-notable-field]')].filter(node => node.dataset.notableField === focus.label)[focus.index];
    if (!field) return;
    field.closest('details')?.setAttribute('open', ''); field.focus({preventScroll: true});
    if (Number.isInteger(focus.start) && typeof field.setSelectionRange === 'function') { try { field.setSelectionRange(focus.start, focus.end); } catch (_) {} }
  },
  restoreReviewList() {
    const list = this.view.document.querySelector('.review-layout > aside');
    if (!list) return;
    list.scrollTop = this.reviewScrollTop || 0;
    const keepVisible = () => {
      const selected = list.querySelector('[aria-pressed="true"]');
      if (!selected || !list.isConnected) return;
      const box = list.getBoundingClientRect(), item = selected.getBoundingClientRect();
      if (item.top < box.top) list.scrollTop -= box.top - item.top;
      else if (item.bottom > box.bottom) list.scrollTop += item.bottom - box.bottom;
    };
    let followSelection = !!(this.reviewFocus || this.revealReviewSelection);
    if (this.reviewFocus) list.querySelector('[aria-pressed="true"]')?.focus({preventScroll: true});
    this.reviewFocus = this.revealReviewSelection = false;
    if (followSelection) keepVisible();
    this.reviewObserver = new this.view.ResizeObserver(() => { if (followSelection) keepVisible(); });
    this.reviewObserver.observe(list.nextElementSibling);
    list.addEventListener('scroll', () => { this.reviewScrollTop = list.scrollTop; followSelection = false; });
  },
  renderTags(content) {
    if (this.tagEditor) { this.renderTagEditor(content); return; }
    this.el('h2', 'Tags', content);
    this.el('p', 'Paper tags are shared with Zotero’s Tags pane. Page tags mark individual pages of linked PDFs.', content, {class: 'hint'});
    this.renderTagSearch(content);
    this.renderTagMaintenance(content);
  },
  renderTagMaintenance(content) {
    this.renderKeywordCleanup(content);
    const finder = this.el('details', '', content, {class: 'card'});
    this.el('summary', 'Paper keywords and tag suggestions', finder);
    this.el('p', 'Find a stored PDF by title. Approve the topics you want to add as paper tags.', finder);
    const {toolbar, input: titleQuery} = this.quickSearch(finder, {label: 'Search PDF titles', submit: () => find.click()});
    delete titleQuery.dataset.quickSearch;
    const actions = this.el('div', '', toolbar, {class: 'actions'});
    const results = this.el('div', '', finder);
    const find = this.button('Find papers', actions, async () => {
      const value = await this.action('paper_tag_candidates', {query: titleQuery.value}, false);
      if (!value || !results.isConnected) return;
      results.replaceChildren();
      this.el('p', `${value.total} matching PDFs${value.total > 50 ? ' · Showing the first 50' : ''}`, results, {role: 'status'});
      for (const paper of value.candidates) {
        const card = this.el('section', '', results, {class: 'card'});
        this.el('strong', paper.title, card); this.el('p', paper.filename, card, {class: 'hint'});
        this.paperTagPanel(card, {key: paper.key});
      }
    });
  },
  rememberRecentTags(tags) {
    if (!tags.length) return;
    this.recentTags = [...new Set([...tags.slice().reverse(), ...this.recentTags])].slice(0, 8);
    try { Zotero.Prefs.set('notable.recentTags', JSON.stringify(this.recentTags), true); }
    catch (error) { if (typeof Zotero !== 'undefined') Zotero.logError?.(error); }
  },
  renderTagEntry(section, {kind, values, inputValue = '', list, empty = `No ${kind} tags yet.`, onChange}) {
    const label = `Add ${kind} tag`, tags = new Set(values);
    const colors = kind === 'paper' && typeof Zotero !== 'undefined' && Zotero.Tags?.getColors(Zotero.Libraries.userLibraryID);
    const chips = this.el('div', '', section, {class: 'tag-chips'});
    const entry = this.el('div', '', section, {class: 'tag-editor-entry'});
    const input = this.field(label, entry, inputValue);
    input.dataset.browse = 'true'; input.maxLength = 255;
    if (list) input.setAttribute('list', list);
    const actions = this.el('div', '', entry, {class: 'actions'});
    const update = () => onChange([...tags], input.value);
    const add = (value = input.value) => {
      value = value.trim();
      if (!value) return;
      tags.add(value);
      if (value === input.value.trim()) { input.value = ''; input.notableRefreshValue(); }
      update(); draw();
    };
    const addButton = this.button('Add tag', actions, () => add());
    addButton.dataset.browse = 'true'; addButton.setAttribute('aria-label', label);
    const recent = this.el('div', '', section, {class: 'recent-tag-list'});
    const draw = () => {
      chips.replaceChildren(); recent.replaceChildren();
      if (!tags.size) this.el('span', empty, chips, {class: 'hint'});
      for (const value of [...tags].sort()) {
        const chip = this.button(`${value} ×`, chips, () => { tags.delete(value); update(); draw(); });
        chip.dataset.browse = 'true'; chip.setAttribute('aria-label', `Remove ${value} from ${kind} tags`);
        if (colors) this.applyTagColor(chip, value, colors);
        chip.addEventListener('click', () => input.focus());
      }
      const choices = this.recentTags.filter(tag => !tags.has(tag));
      recent.hidden = !choices.length;
      if (choices.length) {
        this.el('strong', 'Recent tags', recent);
        const buttons = this.el('div', '', recent, {class: 'tag-chips'});
        for (const value of choices) this.button(value, buttons, () => { add(value); input.focus?.(); }, false, null, {help: false}).dataset.browse = 'true';
      }
    };
    input.addEventListener('input', update);
    input.addEventListener('keydown', event => {
      if (event.key === 'Enter' && !event.isComposing) { event.preventDefault(); add(); }
    });
    draw();
    return {tags, add, input};
  },
  renderTagEditor(content) {
    const context = this.tagEditor;
    const card = this.el('section', '', content, {class: 'card'});
    this.el('h2', `${context.title} — page ${context.page + 1}`, card);
    const close = () => { this.tagEditor = null; this.render(); };
    if (context.paperOnly) {
      this.renderReferenceTagEditor(card, context, close);
      return;
    }
    if (context.loading || context.error) {
      this.el('p', context.error || (this.busy ? 'Waiting for the current operation to load this page’s tags…' : 'Loading tags…'),
        card, {role: context.error ? 'alert' : 'status'});
      this.button('Close page editor', card, close).dataset.browse = 'true';
      return;
    }
    const connection = context.connection || this.filingConnection();
    const draftKey = JSON.stringify([connection, context.key, context.page_id ?? context.page]);
    if (!this.tagDrafts.has(draftKey)) {
      const baseline = this.overlayTagEdits(context);
      this.tagDrafts.set(draftKey, {paper: [...baseline.paper], page: [...baseline.page], baseline, token: context.token});
    }
    const draft = this.tagDrafts.get(draftKey);
    this.el('p', 'Paper tags appear on the Zotero reference. Page tags mark this page. Both sync with reMarkable.', card);
    if (context.interrupted) {
      this.el('p', 'A previous tag sync was interrupted. Sync normally to retry. If tags changed on both copies during that interruption, combine their current tags to recover; labels present on either copy will be retained.', card);
      this.button('Combine current tags and retry', card, async () => {
        const fresh = await this.rpc('tag_context', {key: context.key, page: context.page});
        if (fresh.page_id !== context.page_id || connection !== this.filingConnection()) throw Error('The page changed. Reopen the PDF first.');
        const result = await this.action('combine_tag_copies', {key: context.key, page: context.page, token: fresh.token}, false);
        if (result) {
          draft.paper = [...new Set([...draft.paper, ...result.paper_tags])]; draft.page = [...new Set([...draft.page, ...result.page_tags])];
          draft.baseline = this.overlayTagEdits(result);
          draft.token = result.token; this.tagEditor = result; this.render();
        }
      });
    }
    for (const [pageID, tags] of Object.entries(context.orphaned_pages || {})) {
      this.el('p', 'A removed or unavailable page still has these tags: ' + tags.join(' · '), card);
      this.button('Remove tags from that unavailable page', card, async () => {
        const fresh = await this.rpc('tag_context', {key: context.key, page: context.page});
        if (JSON.stringify(fresh.orphaned_pages?.[pageID]) !== JSON.stringify(tags) || connection !== this.filingConnection()) {
          throw Error('The unavailable page’s tags changed. Reopen the editor to review them.');
        }
        const result = await this.action('remove_unavailable_page_tags', {key: context.key, page: context.page, page_id: pageID, token: fresh.token}, false);
        if (result) { draft.token = result.token; this.tagEditor = result; this.render(); }
      });
    }
    const editor = (title, kind) => {
      const section = this.el('section', '', card, {class: 'tag-editor-scope'});
      this.el('h3', title, section);
      const list = this.tagSuggestions(section, kind, context.vocabulary);
      return this.renderTagEntry(section, {kind, values: draft[kind], inputValue: draft[kind + 'Input'],
        list, onChange: (tags, text) => { draft[kind] = tags; draft[kind + 'Input'] = text; }});
    };
    const paper = editor('Paper tags', 'paper'), page = editor('Page tags', 'page');
    this.paperTagPanel(card, {key: context.key});
    const footer = this.el('div', '', card, {class: 'actions tag-editor-footer'});
    const save = this.button('Save tags', footer, async () => {
      if (draft.saving) return;
      paper.add(); page.add();
      if (context.parent_key && context.pdf_digest) {
        if (connection !== this.filingConnection()) throw Error('The sync connection changed. Reopen the PDF before saving tags.');
        const job = {edit_id: Services.uuid.generateUUID().toString(), connection: JSON.parse(connection),
          cloud: context.cloud, key: context.key, parent_key: context.parent_key,
          page_id: context.page_id, pdf_digest: context.pdf_digest, title: `${context.title} — page ${context.page + 1}`,
          paper: {before: draft.baseline.paper, after: [...paper.tags]}, page: {before: draft.baseline.page, after: [...page.tags]}};
        const differs = ({before, after}) => before.length !== after.length || before.some(tag => !after.includes(tag));
        if (!differs(job.paper) && !differs(job.page)) return;
        draft.saving = true;
        try { await this.changeTagEdits(jobs => [...jobs, job]); }
        finally { draft.saving = false; }
        draft.baseline = {paper: job.paper.after, page: job.page.after};
        this.rememberRecentTags(job.paper.after.filter(tag => !job.paper.before.includes(tag))
          .concat(job.page.after.filter(tag => !job.page.before.includes(tag))));
        this.refreshTagEditQueue();
        this.message('Tag changes saved on this computer. They’ll be applied when the worker is free.');
        this.drainTagEdits();
        return;
      }
      const result = await this.action('set_page_tags', {key: context.key, page: context.page, page_id: context.page_id,
        tags: [...page.tags], paper_tags: [...paper.tags], token: draft.token}, false);
      if (!result) return;
      this.rememberRecentTags([...paper.tags].filter(tag => !context.paper_tags.includes(tag))
        .concat([...page.tags].filter(tag => !context.page_tags.includes(tag))));
      this.tagDrafts.delete(draftKey); this.tagEditor = result; this.render(); this.message('Tags saved. They will reach reMarkable on the next sync.');
    }, true);
    if (context.parent_key && context.pdf_digest) save.dataset.browse = 'true';
    const reference = context.parent_key || context.reference?.key;
    if (reference) this.button('Show Zotero tags', footer, () => this.showZoteroTags(reference), false, 'tag').dataset.browse = 'true';
    this.button('Close page editor', footer, close).dataset.browse = 'true';
  },
  renderReferenceTagEditor(card, context, close) {
    const match = context.linkedCopy;
    if (match) {
      this.el('p', 'This paper syncs through another PDF. Open that copy to edit its page tags.', card);
      this.button('Open linked PDF', card, async () => {
        if (context.connection !== this.filingConnection()) throw Error('The sync connection changed. Open the tag editor again.');
        const current = this.latest.links?.find(row => row.cloud === match.cloud && row.view_key === match.view_key && !row.unavailable);
        const item = current && await notableItem(current.view_key);
        if (!item?.isPDFAttachment() || item.deleted || item.parentKey !== current.parent
            || current.baseline !== match.baseline) throw Error('The linked PDF changed. Open the tag editor again.');
        await Zotero.Reader.open(item.id, {pageIndex: context.page});
        this.tagEditor = null;
        this.view?.close();
      }, true, 'book-open').dataset.browse = 'true';
      this.button('Compare copies in Library Issues', card, () => this.openTab('library')).dataset.browse = 'true';
    } else {
      this.el('p', context.checkingOriginal ? 'Checking this PDF for an existing reMarkable link…'
        : context.pageMapUnavailable ? 'The linked PDF has no available page map for this page. Sync to refresh it.'
        : context.ambiguousCopies ? 'Several reMarkable links match this PDF. Choose the intended copy in Linked papers.'
          : context.matchUnavailable ? 'The PDF could not be checked for an existing link. Paper tags are still available.'
            : 'Page tags require a linked PDF. You can edit this Zotero reference’s paper tags here.', card);
    }
    this.el('h3', 'Paper tags', card);
    const draft = context.paperDraft;
    const editor = this.renderTagEntry(card, {kind: 'paper', values: draft.tags, inputValue: draft.input,
      list: this.tagSuggestions(card, 'paper'),
      onChange: (tags, input) => Object.assign(draft, {tags, input})});
    this.button('Save paper tags', card, async () => {
      if (draft.saving) return;
      editor.add();
      const desired = [...editor.tags];
      const before = context.paperTags;
      const added = desired.filter(tag => !before.includes(tag));
      const removed = before.filter(tag => !desired.includes(tag));
      draft.saving = true;
      try {
        await Zotero.DB.executeTransaction(async () => {
          const reference = await notableItem(context.reference.key);
          if (!reference?.isRegularItem() || reference.deleted) throw Error('The Zotero reference is unavailable.');
          for (const tag of removed) reference.removeTag(tag);
          for (const tag of added) reference.addTag(tag, 0);
          if (added.length || removed.length) await reference.save();
          context.paperTags = reference.getTags().map(row => row.tag);
        });
        draft.tags = [...context.paperTags];
        this.rememberRecentTags(added);
        if (this.tagEditor === context && this.tab === 'tags') this.render();
        this.message('Paper tags saved to this Zotero reference.');
      } finally { draft.saving = false; }
    }, true).dataset.browse = 'true';
    this.button('Show Zotero tags', card, () => this.showZoteroTags(context.reference.key), false, 'tag').dataset.browse = 'true';
    this.button('Close page editor', card, close).dataset.browse = 'true';
  },
  tagSearchState() {
    return this.tagSearch ||= {query: '', scope: 'paper', match: 'all', tags: new Set(), limit: 25,
      showAutomatic: Zotero.Prefs.get('tagSelector.showAutomatic') !== false};
  },
  tagSuggestions(parent, kind, fallback = []) {
    const id = `notable-${kind}-tag-suggestions`;
    const list = this.el('datalist', '', parent, {id});
    const names = kind === 'paper' ? this.paperTagBank?.tags : this.pageTagNames();
    for (const value of names ?? fallback) this.el('option', '', list, {value});
    return id;
  },
  tagEvidence() {
    return `${this.paperTagEvidenceSerial || 0}:${this.tagSyncRevision || 0}`;
  },
  schedulePaperTagBankRefresh() {
    if (this.stopped || typeof Zotero === 'undefined' || !Zotero.Search) return;
    clearTimeout(this.paperTagBankTimer);
    this.paperTagBankTimer = setTimeout(() => this.refreshPaperTagBank(), 180);
  },
  async refreshPaperTagBank() {
    if (this.paperTagBankLoading || this.stopped || typeof Zotero === 'undefined' || !Zotero.Search) return;
    const revision = this.paperTagBankRevision || 0;
    if (this.paperTagBank?.revision === revision) return;
    this.paperTagBankLoading = true;
    this.paperTagBankError = '';
    try {
      const search = new Zotero.Search();
      search.libraryID = Zotero.Libraries.userLibraryID;
      for (const type of ['attachment', 'note', 'annotation']) search.addCondition('itemType', 'isNot', type);
      const tmpTable = await search.search(true);
      let rows = [];
      if (typeof tmpTable === 'string') {
        try {
          rows = await Zotero.Tags.getAllWithin({tmpTable});
        } finally {
          await Zotero.DB.queryAsync(`DROP TABLE IF EXISTS ${tmpTable}`, false, {noCache: true});
        }
      }
      this.paperTagBank = {revision, tags: [...new Set(rows.map(row => row.tag))].sort((a, b) => a.localeCompare(b)),
        manual: new Set(rows.filter(row => !row.type).map(row => row.tag))};
    } catch (error) {
      this.paperTagBankError = 'Couldn’t load Zotero paper tags. ' + error.message;
    } finally {
      this.paperTagBankLoading = false;
      if (revision !== (this.paperTagBankRevision || 0)) this.schedulePaperTagBankRefresh();
      if (this.tagSearch) this.refreshTagSearchView(this.tagSearch);
    }
  },
  pageTagNames() {
    const links = this.latest?.links;
    if (this.pageTagBank?.links === links && this.pageTagBank) return this.pageTagBank.tags;
    const names = new Set();
    for (const row of links || []) {
      if (row.unavailable || !row.parent || !row.view_key || !row.page_tags) continue;
      const pages = new Set(row.page_map || []);
      for (const [page, tags] of Object.entries(row.page_tags)) {
        if (pages.has(page)) for (const tag of tags) names.add(tag);
      }
    }
    const tags = [...names].sort((a, b) => a.localeCompare(b));
    this.pageTagBank = {links, tags};
    return tags;
  },
  refreshTagSearchView(state) {
    if (state === this.tagSearch && this.tab === 'tags' && !this.tagEditor && this.view?.document && !this.view.closed) {
      this.render();
    }
  },
  async runTagSearch(state) {
    const {scope, match, showAutomatic, browseAll} = state;
    const tags = [...state.tags];
    const signature = JSON.stringify([this.tagEvidence(), scope, match, showAutomatic, tags, !!browseAll]);
    if (state.request?.signature === signature) return;
    const request = state.request = {signature};
    state.applied = null;
    state.searchError = '';
    state.pending = !!(tags.length || browseAll);
    state.drawResults?.();
    if (!state.pending) return;
    try {
      const rows = [];
      const items = await Zotero.Items.getAll(Zotero.Libraries.userLibraryID, false, false);
      if (state.request !== request) return;
      const pdfs = new Map(items.filter(item => item.isPDFAttachment() && !notableReviewItemInTrash(item)).map(item => [item.key, item]));
      if (scope !== 'page') {
        const references = items.filter(item => item.isRegularItem() && !item.deleted);
        const withPDF = new Set([...pdfs.values()].map(item => item.parentKey));
        for (const {item, tags: matched} of await notableMatchingPaperTags(references, tags, match, showAutomatic)) {
          rows.push({scope: 'paper', parent: item.key, title: item.getDisplayTitle(), tags: matched, pdf: withPDF.has(item.key)});
        }
      }
      if (scope !== 'paper') {
        const parents = new Map(items.filter(item => item.isRegularItem() && !item.deleted).map(item => [item.key, item]));
        const selected = new Set(tags);
        for (const link of this.latest?.links || []) {
          const parent = parents.get(link.parent);
          if (link.unavailable || !parent || pdfs.get(link.view_key)?.parentKey !== parent.key) continue;
          for (const [page, pageID] of (link.page_map || []).entries()) {
            const names = link.page_tags?.[pageID] || [];
            const matched = selected.size ? names.filter(tag => selected.has(tag)) : names;
            if (matched.length && (match === 'any' || matched.length >= selected.size)) {
              rows.push({scope: 'page', parent: parent.key, key: link.view_key, page, page_id: pageID,
                title: parent.getDisplayTitle(), tags: matched});
            }
          }
        }
      }
      if (state.request !== request) return;
      rows.sort((a, b) => (a.scope === 'page') - (b.scope === 'page')
        || a.title.localeCompare(b.title) || (a.page ?? 0) - (b.page ?? 0));
      state.applied = rows;
    } catch (error) {
      if (state.request === request) state.searchError = 'Couldn’t load tagged items. ' + error.message;
    } finally {
      if (state.request === request) {
        state.pending = false;
        state.drawResults?.();
      }
    }
  },
  renderTagSearch(content) {
    const state = this.tagSearchState();
    const paperTags = this.paperTagBank?.tags;
    const pageTags = this.pageTagNames();
    const colors = Zotero.Tags.getColors(Zotero.Libraries.userLibraryID);
    const choices = () => {
      const papers = (paperTags || []).filter(tag => state.showAutomatic || this.paperTagBank.manual?.has(tag) || colors.has(tag));
      if (state.scope === 'page') return pageTags;
      const tags = state.scope === 'paper' ? papers : [...new Set([...papers, ...pageTags])];
      return tags.sort((a, b) => (colors.get(a)?.position ?? Infinity) - (colors.get(b)?.position ?? Infinity) || a.localeCompare(b));
    };
    const tagBatchSize = 60;
    const panel = this.el('section', '', content, {class: 'tag-search'});
    const {toolbar, input} = this.quickSearch(panel, {label: 'Filter tag names', value: state.query,
      change: value => { state.query = value; state.scrollTop = 0; drawTags(); }});
    const scope = this.searchSelect(toolbar, 'Search in', [['paper', 'Paper tags'], ['page', 'Page tags'], ['both', 'Paper and page tags']], state.scope);
    const match = this.searchSelect(toolbar, 'Match selected tags', [['all', 'Match all'], ['any', 'Match any']], state.match);
    const automatic = this.checkbox('Show automatic tags', toolbar, state.showAutomatic);
    automatic.dataset.browse = 'true';
    automatic.dataset.notableField = 'Show automatic tags';
    const tagList = this.el('div', '', panel, {class: 'tag-list', role: 'group', 'aria-label': 'Available tags'});
    const selected = this.el('div', '', panel, {class: 'tag-selection'});
    const selectedList = this.el('div', '', selected, {class: 'tag-chips', 'aria-label': 'Selected tags'});
    const clear = this.button('Clear selection', selected, () => {
      state.tags.clear(); state.browseAll = false; updateTagButtons(); updateSelection(); apply(); input.focus();
    });
    clear.dataset.browse = 'true';
    const browse = this.button('Show all tagged items', panel, () => { state.browseAll = true; apply(); count.focus(); });
    browse.dataset.browse = 'true';
    const count = this.el('p', '', panel, {class: 'paper-search-count', role: 'status', tabindex: '-1'});
    const results = this.el('div', '', panel);
    let matchingTags = [];
    let tagButtons = [];
    let activeTag = 0;
    let batchScheduled = false;
    const focusTag = (index, focus = true) => {
      if (!tagButtons[index]) return;
      if (tagButtons[activeTag]) tagButtons[activeTag].tabIndex = -1;
      activeTag = index;
      tagButtons[activeTag].tabIndex = 0;
      if (focus) tagButtons[activeTag].focus();
    };
    const drawResults = () => {
      if (!panel.isConnected) return;
      const moreFocused = results.contains(this.view.document.activeElement)
        && this.view.document.activeElement.dataset?.notableField === 'More tagged items';
      results.replaceChildren();
      const scopes = {paper: 'papers', page: 'pages', both: 'papers and pages'};
      browse.closest('.actions').hidden = !!(state.tags.size || state.browseAll);
      const rows = state.applied;
      if (state.pending) count.textContent = 'Finding tagged items…';
      else if (state.searchError) {
        count.textContent = state.searchError;
        this.button('Retry tag search', results, () => { state.request = null; apply(); }).dataset.browse = 'true';
      } else if (!rows) count.textContent = 'Select tags to filter results.';
      else if (!rows.length) count.textContent = `No ${scopes[state.scope]} match the current filters.`;
      else count.textContent = `${rows.length} ${rows.length === 1 ? 'result' : 'results'}`;
      if (!rows) return;
      for (const result of rows.slice(0, state.limit)) this.renderTagResult(results, result);
      if (rows.length > state.limit) {
        const more = this.button('Show more matching results', results, () => { state.limit += 25; drawResults(); });
        more.dataset.browse = 'true';
        more.dataset.notableField = 'More tagged items';
        if (moreFocused) more.focus({preventScroll: true});
      } else if (moreFocused) {
        results.querySelector('.search-result:last-child button')?.focus({preventScroll: true});
      }
    };
    state.drawResults = drawResults;
    const apply = () => { state.limit = 25; this.runTagSearch(state); };
    const updateSelection = () => {
      selected.hidden = !state.tags.size;
      selectedList.replaceChildren();
      for (const value of state.tags) {
        const chip = this.button(`${value} ×`, selectedList, () => {
          state.tags.delete(value); updateTagButtons(); updateSelection(); apply(); input.focus();
        });
        chip.dataset.browse = 'true';
        chip.setAttribute('aria-label', `Remove ${value} from selected tags`);
      }
    };
    const updateTagButtons = () => {
      for (const [index, button] of tagButtons.entries()) {
        button.setAttribute('aria-pressed', String(state.tags.has(matchingTags[index])));
      }
    };
    const appendTagBatch = () => {
      const fragment = tagList.ownerDocument.createDocumentFragment();
      const end = Math.min(tagButtons.length + tagBatchSize, matchingTags.length);
      for (let index = tagButtons.length; index < end; index++) {
        const tag = matchingTags[index];
        const choice = this.el('button', tag, fragment, {type: 'button', 'aria-pressed': String(state.tags.has(tag))});
        tagButtons.push(choice);
        choice.tabIndex = index === 0 ? 0 : -1;
        choice.dataset.browse = 'true';
        choice.dataset.notableField = `Filter by tag ${tag}`;
        if (state.scope !== 'page') this.applyTagColor(choice, tag, colors);
        choice.addEventListener('click', () => {
          focusTag(index, false);
          if (state.tags.has(tag)) state.tags.delete(tag);
          else { state.tags.add(tag); state.browseAll = false; }
          choice.setAttribute('aria-pressed', String(state.tags.has(tag)));
          updateSelection();
          apply();
        });
      }
      tagList.appendChild(fragment);
    };
    const loadNearEnd = () => {
      if (batchScheduled || tagButtons.length >= matchingTags.length) return;
      batchScheduled = true;
      this.view.requestAnimationFrame(() => {
        batchScheduled = false;
        if (!tagList.isConnected || tagButtons.length >= matchingTags.length) return;
        const viewport = tagList.clientHeight;
        const top = tagList.scrollTop;
        if (!viewport || tagList.scrollHeight - top - viewport > viewport / 2) return;
        appendTagBatch();
        tagList.scrollTop = top;
        // A wide window can fit a full batch without a scroll bar.
        while (tagButtons.length < matchingTags.length && tagList.scrollHeight <= viewport) appendTagBatch();
      });
    };
    tagList.addEventListener('scroll', () => { state.scrollTop = tagList.scrollTop; loadNearEnd(); }, {passive: true});
    const drawTags = () => {
      tagList.replaceChildren();
      tagList.scrollTop = 0;
      tagButtons = [];
      activeTag = 0;
      const includesPapers = state.scope !== 'page';
      if (includesPapers && this.paperTagBankError) {
        this.el('p', this.paperTagBankError, tagList, {role: 'status'});
        this.button('Retry loading tags', tagList, () => this.refreshPaperTagBank()).dataset.browse = 'true';
      } else if (includesPapers && !paperTags) {
        this.el('p', 'Loading Zotero paper tags…', tagList, {role: 'status'});
      }
      const query = this.searchTerms(state.query);
      matchingTags = choices().filter(tag => this.matchesSearchTerms(tag, query));
      if (!matchingTags.length && (!includesPapers || paperTags)) {
        this.el('p', query.length ? 'No tags match this filter.' : 'No tags are available for this scope.', tagList);
      }
      if (matchingTags.length) {
        appendTagBatch();
        loadNearEnd();
      }
    };
    input.addEventListener('keydown', event => {
      if (event.key === 'ArrowDown' && tagButtons.length) { event.preventDefault(); focusTag(0); }
    });
    tagList.addEventListener('keydown', event => {
      const index = tagButtons.indexOf(event.target);
      if (index < 0) return;
      const moves = {ArrowUp: index - 1, ArrowLeft: index - 1, ArrowDown: index + 1, ArrowRight: index + 1,
        Home: 0, End: tagButtons.length - 1};
      if (!Object.hasOwn(moves, event.key)) return;
      event.preventDefault();
      const next = moves[event.key];
      if (next === tagButtons.length && next < matchingTags.length) appendTagBatch();
      focusTag(Math.max(0, Math.min(next, tagButtons.length - 1)));
    });
    const changeChoices = () => {
      const available = new Set(choices());
      state.tags = new Set([...state.tags].filter(tag => available.has(tag)));
      state.scrollTop = 0;
      drawTags(); updateSelection(); apply();
    };
    scope.addEventListener('change', () => {
      state.scope = scope.value;
      automatic.closest('label').hidden = state.scope === 'page';
      changeChoices();
    });
    match.addEventListener('change', () => { state.match = match.value; apply(); });
    automatic.addEventListener('change', () => { state.showAutomatic = automatic.checked; changeChoices(); });
    automatic.closest('label').hidden = state.scope === 'page';
    const active = this.tagSearchFocus;
    const previousTop = state.scrollTop || 0;
    // A native edit can delete the last use of a selected tag. Keep the active
    // filter visible so it can be cleared without silently broadening results.
    drawTags();
    const activeIndex = matchingTags.findIndex(tag => active === `Filter by tag ${tag}`);
    while (tagButtons.length <= activeIndex || (tagButtons.length < matchingTags.length
        && tagList.scrollHeight < previousTop + tagList.clientHeight)) appendTagBatch();
    if (activeIndex >= 0) focusTag(activeIndex, false);
    tagList.scrollTop = previousTop;
    updateSelection();
    this.runTagSearch(state);
    drawResults();
    if (!paperTags && !this.paperTagBankError) this.refreshPaperTagBank();
  },
  renderTagResult(parent, result) {
    const page = result.scope === 'page';
    const row = this.el('section', '', parent, {class: 'card search-result'});
    this.el('h3', result.title, row);
    this.el('p', `${page ? 'Page ' + (result.page + 1) : 'Paper tags'} · ${result.tags.join(' · ')}`, row, {class: 'hint'});
    const show = this.button('Show Zotero tags', row, () => this.showZoteroTags(result.parent), false, 'tag');
    show.dataset.browse = 'true';
    if (!page && result.pdf) this.button('Open PDF', row, async () => {
      const paper = await notableItem(result.parent);
      const pdf = paper?.isRegularItem() && !paper.deleted
        && (await paper.getBestAttachments()).find(item => item.isPDFAttachment() && !notableReviewItemInTrash(item));
      if (!pdf) throw Error('This paper’s PDF is unavailable.');
      await Zotero.Reader.open(pdf.id);
    }, false, 'book-open').dataset.browse = 'true';
    if (page) this.button('Open page', row, async () => {
      const fresh = await this.rpc('tag_context', {key: result.key, page: result.page});
      if (fresh.page_id !== result.page_id) throw new Error('The page order changed. Select the page again from refreshed results.');
      const item = await notableItem(result.key);
      if (!item?.isPDFAttachment() || notableReviewItemInTrash(item) || item.parentKey !== result.parent) {
        throw new Error('This PDF is unavailable.');
      }
      await Zotero.Reader.open(item.id, {pageIndex: result.page});
    }, false, 'book-open').dataset.browse = 'true';
  },
  async showZoteroTags(key) {
    await notableNavigateReview({target: 'references', keys: [key]});
    const pane = Zotero.getActiveZoteroPane();
    if (pane.itemPane?.collapsed) pane.toggleItemPane();
    const container = Zotero.getMainWindow().document.getElementById('zotero-item-details');
    const tags = container?.getPane?.('tags');
    if (tags) {
      tags.open = true;
      await container.scrollToPane('tags', 'instant');
    }
  },
  applyTagColor(node, tag, colors) {
    const color = colors.get(tag)?.color;
    if (!color) return;
    node.classList.add('colored-tag');
    node.style.setProperty('--tag-color', color);
  },
  renderKeywordCleanup(content) {
    const panel = this.el('details', '', content, {class: 'card'});
    this.el('summary', 'Review questionable automatic tags', panel);
    this.el('p', 'Check Zotero for automatic tags that look like fragments or have extra quotation marks. Choose which repairs to apply.', panel);
    const scan = this.button('Check automatic tags', panel, async () => {
      const value = await this.action('preview_keyword_cleanup', {}, false);
      if (!value?.papers) return;
      results.replaceChildren();
      if (!value.papers.length) this.el('p', 'No questionable automatic tags were found.', results);
      else this.el('p', `${value.papers.length} ${value.papers.length === 1 ? 'paper' : 'papers'} with possible tag repairs.`, results, {role: 'status'});
      for (const paper of value.papers) {
        const card = this.el('section', '', results, {class: 'card'});
        this.el('h3', paper.title || 'Untitled paper', card);
        const choices = paper.tags.map(row => {
          const check = this.checkbox(row.tag, card, false);
          check.addEventListener('change', () => {
            remove.disabled = !choices.some(([, input]) => input.checked);
            remove.dataset.invalid = String(remove.disabled);
          });
          this.el('p', row.reason, card, {class: 'hint'});
          if (row.replacements) this.el('p', 'Proposed tags · ' + row.replacements.join(' · '), card, {class: 'hint'});
          else if (row.replacement) this.el('p', `Proposed tag · ${row.replacement}`, card, {class: 'hint'});
          if (row.evidence !== row.tag) this.el('p', row.source + ' · ' + row.evidence, card, {class: 'hint'});
          return [row.tag, check];
        });
        const remove = this.button(paper.tags.some(row => row.replacement || row.replacements)
          ? 'Apply selected repairs to this paper' : 'Remove selected tags from this paper', card, async () => {
          const selected = choices.filter(([, input]) => input.checked).map(([tag]) => tag);
          if (!selected.length) return;
          const result = await this.maintenanceCall('repair_keywords',
            {selected: [{parent: paper.parent, token: paper.token, tags: selected}]});
          if (!result) return;
          if (!result.removed && !result.renamed) {
            this.el('p', 'The tags changed. Check automatic tags again before applying repairs.', card, {role: 'status'});
            return;
          }
          card.replaceChildren();
          const changes = [
            result.removed ? `${result.removed} removed` : '',
            result.renamed ? `${result.renamed} corrected` : '',
          ].filter(Boolean).join(' · ');
          this.el('p', `${paper.title || 'Untitled paper'} · ${changes}`, card, {role: 'status'});
          if (selected.length < paper.tags.length) this.el('p', 'Check automatic tags again to review the remaining tags.', card, {class: 'hint'});
        });
        remove.disabled = true;
        remove.dataset.invalid = 'true';
      }
    });
    this.setButtonHelp(scan, 'Preview automatic tag repairs across your Zotero library. Choose which fragments to remove or quoted tags to rename.');
    const results = this.el('div', '', panel);
    const history = this.el('div', '', panel);
    this.button('Review saved tag repairs', panel, async () => {
      const entries = await this.maintenanceCall('tag_history');
      if (!entries) return;
      history.replaceChildren();
      if (!entries.length) this.el('p', 'No saved tag repairs to undo.', history);
      for (const row of entries) {
        const entry = this.el('section', '', history, {class: 'card'});
        this.el('p', `${new Date(row.created_at).toLocaleString()} · ${row.papers} papers`, entry);
        this.button('Undo this tag repair', entry, async () => {
          const result = await this.maintenanceCall('undo_tags', {identity: row.id});
          if (result) this.el('p', `${result.restored} tags restored. ${result.skipped} papers changed after the repair.`, entry, {role: 'status'});
        });
      }
    });
  },
  maintenanceCall(action, params = {}) {
    return this.action('maintenance', {action, ...params}, false);
  },
  renderPDFComparison(content, pdfs) {
    const panel = this.el('details', '', content, {class: 'card'});
    this.el('summary', 'Compare local PDF pages', panel);
    const choose = (label, index) => {
      const caption = this.el('label', label, panel);
      const input = this.el('select', '', caption);
      for (const row of pdfs) this.el('option', row.filename, input, {value: row.key});
      input.value = pdfs[index].key;
      return input;
    };
    const first = choose('First PDF ', 0), second = choose('Second PDF ', 1);
    const output = this.el('div', '', panel);
    this.button('Compare PDF pages', panel, async () => {
      if (first.value === second.value) { this.message('Choose two different PDFs.'); return; }
      const result = await this.maintenanceCall('compare', {left: first.value, right: second.value});
      if (!result) return;
      output.replaceChildren();
      const sides = this.el('div', '', output, {class: 'maintenance-pdf-sides'});
      for (const pdf of result.pdfs) {
        const side = this.el('section', '', sides, {class: 'card'});
        this.el('h4', pdf.filename, side);
        this.el('p', `${pdf.pages} pages · ${pdf.compared} checked`, side);
        this.el('p', pdf.text || 'No extractable page text was found.', side);
        this.reviewNavigationButton('Open PDF in Zotero', side, 'pdf', [pdf.key]);
      }
      this.el('p', `${result.matches} pages have unique matching text.`, output);
      for (const [left, right] of result.page_map) this.el('p', `First PDF page ${left} · Second PDF page ${right}`, output);
    });
  },
  renderAnnotationMaintenance(card, row) {
    const panel = this.el('details', '', card);
    this.el('summary', 'Review or export annotations', panel);
    this.button('Export source-linked annotations…', panel, async () => {
      const target = await this.maintenanceFile('Save Zotero annotations', 'modeSave', '.md', 'Zotero annotations.md');
      if (!target) return;
      const result = await this.maintenanceCall('annotations', {key: row.view_key, target});
      if (result) this.message(`${result.annotations} annotations saved to ${result.path}`);
    });
    const output = this.el('div', '', panel);
    this.button('Preview tablet highlight groups', panel, async () => {
      const preview = await this.maintenanceCall('highlights', {identity: row.cloud});
      if (!preview) return;
      output.replaceChildren();
      if (!preview.groups.length) this.el('p', 'No continuous highlight groups were found in the retained pages.', output);
      const selected = preview.groups.map(group => {
        const known = preview.approved.some(members => JSON.stringify(members) === JSON.stringify(group.members));
        return this.checkbox(`Page ${group.page} · ${group.text}`, output, known);
      });
      const queue = async options => {
        const value = await this.maintenanceCall('highlights', {identity: row.cloud, token: preview.token, ...options});
        if (value?.queued) {
          output.replaceChildren();
          this.el('p', 'Highlight choices saved for the next sync.', output, {role: 'status'});
          this.schedule(1000);
        }
      };
      if (preview.groups.length) this.button('Queue selected highlight groups', output,
        () => queue({selected: selected.flatMap((input, index) => input.checked ? [index] : [])}));
      if (preview.can_undo) this.button('Undo previous highlight choice', output, () => queue({undo: true}));
    });
  },
  async maintenanceFile(title, mode, suffix = '', suggested = '') {
    const {FilePicker} = ChromeUtils.importESModule('chrome://zotero/content/modules/filePicker.mjs');
    const picker = new FilePicker();
    picker.init(this.view, title, picker[mode]);
    if (suffix) picker.appendFilter(suffix === '.zip' ? 'ZIP backups' : 'Markdown files', '*' + suffix);
    if (suggested) picker.defaultString = suggested;
    if (await picker.show() === picker.returnOK) return picker.file;
    return null;
  },
  renderMaintenance(content) {
    const panel = this.el('details', '', content, {class: 'card'});
    this.el('summary', 'Maintenance and backups', panel);
    const verify = this.button('Verify all files and sync', panel, () => this.action('sync', {verify: true}), false, 'refresh-cw');
    verify.dataset.invalid = String(!this.canSync());
    this.button('Repair bundled helper', panel, async () => {
      if (this.process) await this.closeProcess(this.process);
      this.repairRuntime = true; await this.refresh(); this.render();
      this.message('The bundled helper was checked and reinstalled from this plugin package.');
    });
    this.el('p', 'Back up plugin settings, links, and review decisions. Use Zotero’s own backup for your papers and annotations. Restored plugin data starts paused and needs fresh authorization.', panel);
    this.button('Save plugin backup…', panel, async () => {
      const target = await this.maintenanceFile('Save plugin backup', 'modeSave', '.zip', 'reMarkable-Zotero-backup.zip');
      if (!target) return;
      const result = await this.maintenanceCall('backup', {target});
      if (result) this.message('Verified plugin backup saved to ' + result.path);
    });
    this.button('Restore backup into a new folder…', panel, async () => {
      const source = await this.maintenanceFile('Select plugin backup', 'modeOpen', '.zip');
      if (!source) return;
      const folder = await this.maintenanceFile('Choose a parent folder for the recovered plugin data', 'modeGetFolder');
      if (!folder) return;
      const result = await this.maintenanceCall('restore', {source, destination: PathUtils.join(folder, 'reMarkable-Zotero-restored-' + Date.now())});
      if (result) this.message('Verified recovery saved to ' + result.path + '. See the plugin guide to switch to it.');
    });
    const output = this.el('div', '', panel);
    this.button('Show recent sync activity', panel, async () => {
      const value = await this.maintenanceCall('timeline');
      if (!value) return;
      output.replaceChildren();
      if (value.last_sync) this.el('p', 'Last successful sync · ' + new Date(
        typeof value.last_sync === 'number' ? value.last_sync * 1000 : value.last_sync).toLocaleString(), output);
      if (this.syncDue) this.el('p', 'Next planned check · ' + new Date(this.syncDue).toLocaleString(), output);
      if (this.syncFailure) this.el('p', 'The most recent sync needs attention. See the Sync Conflicts tab.', output);
      if (!value.events.length) this.el('p', 'No recent sync activity recorded.', output);
      for (const event of value.events) this.el('p', new Date(event.time).toLocaleString() + ' · ' + event.label, output);
    });
  },
  libraryFindings(row) {
    return (row.payload?.findings || []).filter(finding => !finding.acknowledgement?.acknowledged
      && (finding.domain === 'library' || finding.severity === 'repair'));
  },
  libraryCaseKey(row) {
    const findings = this.libraryFindings(row);
    const refs = [...new Set(findings.flatMap(finding => (finding.references || []).map(item => item.key)))].sort();
    const files = [...new Set(findings.flatMap(finding => (finding.attachments || []).map(item => item.key)))].sort();
    const normalized = value => Array.isArray(value) ? value.map(normalized).sort((a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b)))
      : value && typeof value === 'object' ? Object.fromEntries(Object.keys(value).sort().map(key => [key, normalized(value[key])])) : value;
    const evidence = findings.map(finding => JSON.stringify(normalized(finding))).sort();
    return refs.length || files.length ? JSON.stringify([refs, files, evidence]) : row.id;
  },
  queueRows(queue) {
    const all = this.latest?.reviews || [], queued = this.queuedReviewIds();
    const suggested = new Set(all.filter(row => !queued.has(row.id) && this.inboxIntakeMatch(row, queued)).map(row => row.intake_pdf.id));
    const rows = all.filter(row => !queued.has(row.id) && !suggested.has(row.id));
    if (queue === 'sync-review') return rows.filter(row => row.kind === 'conflict');
    if (queue === 'library') {
      const groups = new Map();
      for (const row of rows) if (row.status !== 'applying' && row.kind !== 'conflict' && this.libraryFindings(row).length) {
        const key = this.libraryCaseKey(row);
        if (!groups.has(key)) groups.set(key, {...row, caseRows: []});
        groups.get(key).caseRows.push(row.id);
      }
      return [...groups.values()];
    }
    const incoming = rows.filter(row => row.kind !== 'conflict');
    if (queue === 'inbox-waiting') return incoming.filter(row => row.status !== 'applying' && this.libraryFindings(row).length);
    if (queue === 'inbox') return incoming.filter(row => row.status === 'applying' || !this.libraryFindings(row).length);
    return incoming;
  },
  inboxIntakeMatch(row, queued = this.queuedReviewIds()) {
    const match = row.intake_pdf;
    return match && !queued.has(match.id) && !this.separateIntakePDFs?.has(match.id)
      && !this.reviewDrafts.has(match.id) ? match : null;
  },
  inboxRows() {
    return [...this.queueRows('inbox'), ...this.queueRows('inbox-waiting')];
  },
  inboxCheckResult(previous) {
    const paperKey = row => ['zotero', 'reference'].includes(row.kind) ? row.payload.parent || row.id : row.id;
    const current = this.inboxRows(), known = new Set(previous.map(paperKey));
    const added = new Set(current.map(paperKey).filter(key => !known.has(key))).size;
    if (added) return `${added} added to Inbox`;
    return JSON.stringify(current) === JSON.stringify(previous) ? 'Inbox is up to date' : 'Inbox updated';
  },
  referencePDFState(row) {
    return row.payload.pdf_state || (row.payload.missing_file ? 'unavailable' : 'none');
  },
  inboxReadyToFile(row) {
    return row.kind !== 'reference' && row.status !== 'applying' && !this.reviewNeedsUpgrade(row);
  },
  sortInboxRows(rows) {
    const direction = this.inboxSort.endsWith('-desc') ? -1 : 1;
    const title = row => row.payload.title || row.payload.filename || '';
    return rows.map((row, index) => ({row, order: row.inbox_order ?? index})).sort((a, b) => {
      const added = a.order - b.order;
      return this.inboxSort.startsWith('title-')
        ? direction * this.inboxTitleOrder.compare(title(a.row), title(b.row)) || added
        : direction * added;
    }).map(entry => entry.row);
  },
  renderInboxControls(parent) {
    const controls = this.el('div', '', parent, {class: 'inbox-controls'});
    const filters = this.el('div', '', controls, {class: 'inbox-filters', role: 'group', 'aria-label': 'Show Inbox entries'});
    const labels = [['all', 'All'], ['ready', 'Ready to file'], ['needs-pdf', 'Needs a PDF'], ['waiting', 'Waiting on Library Issues']];
    const buttons = labels.map(([value, label]) => {
      const button = this.el('button', label, filters, {type: 'button', 'aria-pressed': String(this.inboxFilter === value)});
      button.dataset.browse = 'true';
      button.dataset.inboxFilter = value;
      button.dataset.notableField = `Inbox filter ${value}`;
      button.addEventListener('click', () => {
        if (this.inboxFilter === value) return;
        this.inboxFilter = value;
        this.revealReviewSelection = true;
        this.render();
      });
      return button;
    });
    const label = this.el('label', 'Sort by', controls, {class: 'inbox-sort'});
    const sort = this.el('select', '', label, {'aria-label': 'Sort Inbox', 'data-notable-field': 'Inbox sort'});
    sort.dataset.browse = 'true';
    for (const [value, name] of this.inboxSortOptions) this.el('option', name, sort, {value});
    sort.value = this.inboxSort;
    sort.addEventListener('change', () => {
      this.inboxSort = sort.value;
      Zotero.Prefs.set('notable.inboxSort', sort.value, true);
      this.revealReviewSelection = true;
      this.render();
    });
    const status = this.el('p', '', controls, {class: 'queue-summary', role: 'status', 'data-queue-summary': 'inbox'});
    const update = liveReference => {
      const parts = this.inboxSummary(liveReference);
      const counts = [null, parts.ready, parts.needsPDF, parts.waiting];
      for (const [index, button] of buttons.entries()) {
        const name = labels[index][1];
        button.textContent = counts[index] === null ? name : `${name} (${counts[index]})`;
      }
      status.textContent = parts.other.join(' · ');
      status.hidden = !parts.other.length;
    };
    update();
    return update;
  },
  inboxSummary(liveReference = null) {
    const rows = this.queueRows('inbox');
    const states = rows.filter(row => row.kind === 'reference')
      .map(row => row.id === liveReference?.id ? liveReference.state : this.referencePDFState(row));
    const needsPDF = states.filter(state => state === 'none').length;
    const unavailable = states.filter(state => state === 'unavailable').length;
    const preparing = states.filter(state => state === 'preparing').length;
    const check = states.length - needsPDF - unavailable - preparing;
    const interrupted = rows.filter(row => row.kind !== 'reference' && row.status === 'applying').length;
    const update = rows.filter(row => this.reviewNeedsUpgrade(row)).length;
    return {ready: rows.filter(row => this.inboxReadyToFile(row)).length, needsPDF,
      waiting: this.queueRows('inbox-waiting').length, other: [
        unavailable && `${unavailable} PDF${unavailable === 1 ? '' : 's'} unavailable here`,
        preparing && `${preparing} preparing filename${preparing === 1 ? '' : 's'}`,
        check && `${check} to check in Zotero`,
        interrupted && `${interrupted} filing${interrupted === 1 ? '' : 's'} to resume`,
        update && `${update} awaiting review update`,
      ].filter(Boolean)};
  },
  inboxDisplayRows() {
    const active = this.queueRows('inbox');
    const waiting = this.queueRows('inbox-waiting');
    const selection = this.inboxReferenceSelection;
    if (selection && selection.id === this.selected) {
      const reference = active.find(row => row.id === selection.id);
      if (reference) {
        selection.attachment ||= reference.payload.preparing_attachment;
        if (this.inboxFilter === 'needs-pdf' && this.referencePDFState(reference) === 'preparing') {
          this.inboxFilter = 'all';
        }
      } else {
        const matches = [...active, ...waiting].filter(row => row.kind === 'zotero'
          && row.payload.parent === selection.parent && /\.pdf$/i.test(row.payload.path || '')
          && (!selection.attachment || row.identity === selection.attachment));
        if (matches.length === 1) {
          this.selected = matches[0].id;
          this.inboxHandoffWaiting = waiting.some(row => row.id === this.selected) ? this.selected : null;
          this.revealReviewSelection = true;
          if (this.inboxFilter === 'needs-pdf') this.inboxFilter = 'all';
        }
        this.inboxReferenceSelection = null;
      }
    }
    if (this.inboxFilter === 'waiting') return this.sortInboxRows(waiting);
    if (this.inboxFilter === 'ready') return this.sortInboxRows(active.filter(row => this.inboxReadyToFile(row)));
    if (this.inboxFilter === 'needs-pdf') return this.sortInboxRows(active.filter(row => row.kind === 'reference'
      && this.referencePDFState(row) === 'none'));
    // Keep a paper visible if its new PDF has a library issue to resolve.
    return this.sortInboxRows([...active, ...waiting.filter(row => row.id === this.inboxHandoffWaiting && row.id === this.selected)]);
  },
  queueSummary(queue, liveReference = null) {
    if (queue === 'inbox') {
      const {ready, needsPDF, waiting, other} = this.inboxSummary(liveReference);
      return [
        `${ready} ready to file`,
        needsPDF && `${needsPDF} ${needsPDF === 1 ? 'needs' : 'need'} a PDF`,
        ...other,
        `${waiting} waiting on Library Issues`
      ].filter(Boolean).join(' · ');
    }
    const count = this.queueRows(queue).length;
    if (queue === 'library') {
      const total = count + this.linkedOriginalCases().length;
      return `${total} ${total === 1 ? 'case' : 'cases'} to inspect`;
    }
    const decisions = count + this.syncDecisionCount();
    return `${decisions} sync ${decisions === 1 ? 'decision' : 'decisions'}`;
  },
  syncDecisionCount() {
    return (this.latest.deletions || []).length + (this.latest.local_deletions || []).length + (this.latest.folder_issues || []).length
      + (this.latest.status?.result?.review || []).filter(issue => issue.merge).length
      + this.syncDecisions.filter(job => job.connection !== this.filingConnection() || !this.syncDecisionEvidence(job.method, job.params)).length;
  },
  syncReviewAvailable() {
    return this.latest && (this.queueRows('sync-review').length || this.syncDecisionCount() || this.syncWarnings().length);
  },
  syncWarnings() {
    const queued = new Set((this.latest?.reviews || []).map(row => row.identity));
    const pendingTrash = new Set((this.latest?.deletions || []).map(row => row.payload.cloud));
    return (this.latest?.status?.result?.review || []).filter(issue => issue.merge || (!this.isQueueMessage(issue)
      && !queued.has(issue.attachment) && !queued.has(issue.cloud) && !queued.has(issue.collection)
      && !pendingTrash.has(issue.cloud)));
  },
  decisionLocations(parent, locations, {folder = false} = {}) {
    const boxes = [];
    const list = this.el('fieldset', '', parent, {class: 'decision-locations'});
    this.el('legend', 'Zotero collection locations', list);
    for (const location of [...locations].sort((a, b) => a.path.localeCompare(b.path))) {
      const box = this.checkbox(location.path + (folder && location.inside ? ' (inside the folder going to Trash)' : ''), list, folder && location.inside);
      if (folder && location.inside) box.disabled = true;
      else boxes.push([location.key, box]);
    }
    if (!locations.length) this.el('p', 'This item is in My Library without a collection.', list, {class: 'hint'});
    if (boxes.length > 1) this.button('Select all locations', list, () => {
      const checked = boxes.some(([, box]) => !box.checked);
      for (const [, box] of boxes) box.checked = checked;
      list.dispatchEvent(new this.view.Event('change', {bubbles: true}));
    }, false, null, {help: false});
    return {list, selected: () => boxes.filter(([, box]) => box.checked).map(([key]) => key)};
  },
  async syncDecisionPreview(method, params, parent) {
    if (method === 'confirm_folder_change') return notableCollectionDecision(this.latest.config.zotero_root, params.issue.zotero);
    return notablePaperDecision(parent);
  },
  syncDecisionPreviewRows(snapshot) {
    if (snapshot.items) return [...snapshot.inventory.map(row => ({key: row[0], title: row[2], evidence: row})), ...snapshot.items];
    return [...snapshot.inventory, ...snapshot.locations.map(row => ({...row, title: row.path}))];
  },
  async changedSyncDecision(job) {
    if (!job.preview || job.connection !== this.filingConnection()) return [];
    try {
      const snapshot = await this.syncDecisionPreview(job.method, job.params, job.parent);
      const latest = this.syncDecisionPreviewRows(snapshot);
      const before = new Map(job.preview.map(row => [row.key, row]));
      const after = new Map(latest.map(row => [row.key, row]));
      const changes = [];
      for (const row of latest) {
        const previous = before.get(row.key);
        if (!previous) changes.push('Added · ' + row.title);
        else if (JSON.stringify(previous) !== JSON.stringify(row)) changes.push('Changed · ' + row.title);
      }
      for (const row of job.preview) if (!after.has(row.key)) changes.push('Removed · ' + row.title);
      return changes;
    } catch (_) { return []; }
  },
  async confirmSyncDecision(job, detail) {
    if (this.syncDecisionPrompt || this.syncDecisions.some(row => row.key === job.key)) return;
    const doc = this.view.document;
    const dialog = this.el('dialog', '', doc.body || doc.querySelector('body'),
      {class: 'review-dismiss sync-decision-dialog', 'aria-labelledby': 'confirm-sync-title'});
    this.syncDecisionPrompt = dialog;
    this.el('h2', job.label + '?', dialog, {id: 'confirm-sync-title'});
    this.el('p', job.title, dialog, {class: 'sync-decision-title'});
    this.el('p', detail, dialog);
    const preview = this.el('div', '', dialog, {class: 'sync-decision-preview'});
    const outcome = this.el('p', '', dialog, {class: 'hint', role: 'status'});
    const form = this.el('form', '', dialog, {method: 'dialog', class: 'actions'});
    const cancel = this.el('button', 'Cancel', form, {value: 'cancel', autofocus: ''});
    const confirm = this.el('button', job.label, form, {value: 'confirm'});
    if (job.danger) confirm.classList.add('danger');
    let plan, reviewed, summary = () => job.label;
    const updateOutcome = () => { outcome.textContent = summary(); };
    preview.addEventListener('change', updateOutcome);
    const {method, params} = job;
    const folderTrash = method === 'confirm_folder_change' && params.choice === 'mirror' && params.issue.mirror?.side === 'zotero';
    const zoteroPaper = method === 'delete_decision' && ['pdf_only', 'everything', 'remove_collections'].includes(params.choice);
    const confirmed = new Promise(resolve => {
      dialog.addEventListener('close', () => {
        const accepted = dialog.returnValue === 'confirm' && !confirm.disabled;
        const result = accepted ? {plan: plan?.(), preview: reviewed, summary: summary()} : null;
        dialog.remove(); resolve(result);
      }, {once: true});
      dialog.showModal(); cancel.focus();
    });
    if (folderTrash || zoteroPaper) {
      confirm.disabled = true;
      this.el('p', 'Reading current Zotero items and locations…', preview, {role: 'status'});
      const load = async () => {
        try {
          const snapshot = await this.syncDecisionPreview(method, params, job.parent);
          if (!dialog.open) return;
          reviewed = this.syncDecisionPreviewRows(snapshot);
          preview.replaceChildren();
          if (folderTrash) {
            this.el('p', snapshot.items.length
              ? `${snapshot.inventory.length} Zotero collections and ${snapshot.items.length} items. Review which items will go to Trash.`
              : 'The Zotero collection and its subcollections are empty.', preview);
            if (snapshot.items.some(item => item.locations.some(row => !row.inside))) {
              this.el('p', 'Items with other locations start unchecked for Trash. Check any additional collections you want to remove a kept item from.', preview, {class: 'hint'});
            }
            const choices = snapshot.items.map(item => {
              const section = this.el('section', '', preview, {class: 'sync-decision-item'});
              this.el('h3', item.title, section);
              const trash = this.checkbox('Trash this item everywhere', section, item.locations.every(row => row.inside));
              const locations = this.decisionLocations(section, item.locations, {folder: true});
              const update = () => { locations.list.disabled = trash.checked; };
              trash.addEventListener('change', update); update();
              if (item.children.length) this.el('p', 'Includes ' + item.children.map(child => child.title).join(', '), section, {class: 'hint'});
              return {item, trash, locations, update};
            });
            if (choices.length > 1) this.button('Select all items for Trash', preview, () => {
              const selected = choices.some(({trash}) => !trash.checked);
              for (const choice of choices) { choice.trash.checked = selected; choice.update(); }
              updateOutcome();
            }, false, null, {help: false});
            plan = () => ({token: snapshot.token, items: choices.map(({item, trash, locations}) =>
              ({key: item.key, trash: trash.checked, remove: trash.checked ? [] : locations.selected()}))});
            summary = () => {
              if (!choices.length) return 'Trash empty Zotero collection';
              const count = choices.filter(choice => choice.trash.checked).length;
              const removed = choices.reduce((n, choice) => n + (choice.trash.checked ? 0 : choice.locations.selected().length), 0);
              return `Trash Zotero collection and ${count} ${count === 1 ? 'item' : 'items'} · ${choices.length - count} kept in My Library`
                + (removed ? ` · Remove from ${removed} additional collection locations` : '');
            };
          } else {
            if (params.choice === 'remove_collections') {
              this.el('p', 'Check the collections to remove this reference from. The reference and its attachments stay in My Library.', preview);
              const locations = this.decisionLocations(preview, snapshot.locations);
              const update = () => { confirm.disabled = !locations.selected().length; };
              locations.list.addEventListener('change', update);
              plan = () => ({token: snapshot.token, remove: locations.selected()});
              summary = () => `Remove reference from ${locations.selected().length} Zotero collection locations`;
              update(); updateOutcome();
              return;
            }
            this.el('p', 'Zotero collection locations', preview);
            for (const location of snapshot.locations) this.el('p', location.path, preview, {class: 'hint'});
            if (params.choice === 'everything') {
              const children = snapshot.inventory.filter(row => !row.deleted && row.key !== job.parent);
              if (children.length) this.el('p', 'Attachments and notes going with the reference include ' + children.map(row => row.title).join(', '), preview);
            }
            plan = () => ({token: snapshot.token});
          }
          confirm.disabled = false;
          updateOutcome();
        } catch (error) {
          if (!dialog.open) return;
          preview.replaceChildren();
          this.el('p', error.message, preview, {role: 'alert', class: 'message error'});
        }
      };
      load();
    }
    try {
      const decision = await confirmed;
      if (!decision) return;
      const {label, danger, ...saved} = job;
      const request = decision.plan ? {...params, zotero_plan: decision.plan} : params;
      const evidence = JSON.stringify(this.syncDecisionEvidence(method, params));
      if (!evidence || evidence !== job.evidence || job.connection !== this.filingConnection()) {
        throw Error('The folder or paper changed. Open the decision again to review it.');
      }
      await this.changeSyncDecisions(jobs => jobs.some(row => row.key === job.key)
        ? jobs : [...jobs, {...saved, params: request, preview: decision.preview, summary: decision.summary}]);
    } finally {
      this.syncDecisionPrompt = null;
      this.redrawSyncDecisions();
      this.drainSyncDecisions();
    }
  },
  syncDecisionButton(card, method, id, title, label, params, detail, {primary = false, icon, parent} = {}) {
    const connection = this.filingConnection(), key = JSON.stringify([connection, method, id]);
    const evidence = JSON.stringify(this.syncDecisionEvidence(method, params));
    const button = this.button(label, card, () => {
      const request = typeof params.destination === 'function' ? {...params, destination: params.destination()} : params;
      return this.confirmSyncDecision({key, connection, method, title, label, params: request, parent,
        danger: icon === 'trash-2', evidence}, typeof detail === 'function' ? detail(request) : detail);
    }, primary, icon);
    button.dataset.browse = 'true';
    return button;
  },
  renderSavedSyncDecision(card, method, id, shown) {
    const key = JSON.stringify([this.filingConnection(), method, id]);
    const job = this.syncDecisions.find(row => row.key === key);
    if (!job) return false;
    shown.add(key);
    this.renderQueuedSyncDecision(card, job);
    return true;
  },
  renderQueuedSyncDecision(card, job) {
    const applying = job.key === this.activeSyncDecision;
    this.el('p', (job.error ? 'Needs review · ' : applying ? 'Applying · ' : 'Queued · ') + job.summary, card, {role: 'status'});
    if (job.error) this.el('p', job.error, card, {role: 'alert', class: 'message error'});
    if (job.changes?.length) {
      const list = this.el('ul', '', card);
      for (const change of job.changes) this.el('li', change, list);
    }
    if (!applying) {
      const remove = this.button(job.error ? 'Clear queued choice and review again' : 'Cancel queued decision', card, async () => {
        if (job.key === this.activeSyncDecision) return;
        await this.changeSyncDecisions(jobs => jobs.filter(row => row.key !== job.key || row.key === this.activeSyncDecision));
        this.redrawSyncDecisions();
      });
      remove.dataset.browse = 'true';
    }
  },
  renderFolderIssues(parent, shown = new Set()) {
    const reasons = {trash: 'is in Trash', outside: 'is outside the connected folders', missing: 'is missing'};
    for (const issue of this.latest.folder_issues || []) {
      const card = this.el('section', '', parent, {class: 'card decision folder-sync-decision'});
      const states = [[issue.tablet_state, 'reMarkable folder', issue.tablet_name], [issue.zotero_state, 'Zotero collection', issue.zotero_name]];
      for (const [state, side, name] of states) if (state) this.el('h2', `${side} “${name}” ${reasons[state]}`, card);
      this.el('p', 'Sync for this folder pair and its papers is paused. Other folders can continue syncing.', card);
      const id = [issue.zotero, issue.tablet], title = issue.tablet_name || issue.zotero_name;
      if (this.renderSavedSyncDecision(card, 'confirm_folder_change', id, shown)) continue;
      const decide = (label, choice, detail, options) => this.syncDecisionButton(card, 'confirm_folder_change', id,
        title, label, {issue, choice}, detail, options);
      const deleted = states.some(([state]) => state === 'trash' || state === 'missing');
      this.el('p', 'Keep the change on this device to disconnect the pair and leave the matching folder in place.', card);
      if (issue.mirror?.side === 'zotero') this.el('p', 'Review the Zotero collection, its subcollections, and which items will go to Trash.', card);
      if (issue.mirror?.side === 'tablet') this.el('p', `Trashing the matching reMarkable folder also moves its contents out of the active library (${issue.mirror.children} items and subfolders).`, card);
      decide(deleted ? 'Keep deletion on this device' : 'Keep folder disconnected', 'keep',
        'The folder pair will disconnect. The matching folder on the other device will stay where it is.', {primary: true});
      if (issue.mirror) decide(issue.mirror.side === 'zotero' ? 'Also trash Zotero collection' : 'Also trash reMarkable folder',
        'mirror', issue.mirror.side === 'zotero' ? `Move “${issue.zotero_name}” and its subcollections to Zotero Trash. Choose which items to trash everywhere.`
          : `Move “${issue.tablet_name}” and its contents to reMarkable Trash (${issue.mirror.children} items and subfolders).`, {icon: 'trash-2'});
      if (issue.undo) decide(issue.undo.side === 'zotero' ? 'Restore Zotero collection' : 'Restore reMarkable folder',
        'restore', 'The folder will return to its original location, and the two folders will sync again.', {icon: 'rotate-ccw'});
    }
  },
  openLibraryCase(identity) {
    const target = this.queueRows('library').find(row => row.caseRows.includes(identity));
    if (!target) throw Error('This paper’s library case changed. Recheck Zotero to update its status.');
    this.tabNavigationSerial = (this.tabNavigationSerial || 0) + 1;
    this.queueSelections ||= {}; this.queueSelections[this.tab] = this.selected;
    this.tab = 'library'; this.selected = target.id; this.reviewScrollTop = 0; this.render();
  },
  renderInboxWaiting(parent) {
    const waiting = this.sortInboxRows(this.queueRows('inbox-waiting')
      .filter(row => row.id !== this.inboxHandoffWaiting || row.id !== this.selected));
    if (!waiting.length) return;
    const section = this.el('details', '', parent, {class: 'card inbox-waiting', 'data-inbox-waiting': 'true'});
    section.open = !!this.inboxWaitingOpen;
    this.el('summary', `Waiting on Library Issues (${waiting.length})`, section);
    section.addEventListener('toggle', () => { if (section.isConnected) this.inboxWaitingOpen = section.open; });
    const list = this.el('ul', '', section, {class: 'waiting-papers'});
    for (const row of waiting) {
      const line = this.el('li', '', list);
      this.el('strong', row.payload.title || row.payload.filename || 'Incoming paper', line);
      this.el('span', 'From: ' + (row.kind === 'cloud' ? 'reMarkable' : row.kind === 'inbox' ? 'ToSort' : 'Zotero'), line, {class: 'hint'});
      const jump = this.button('Resolve in Library Issues', line, () => this.openLibraryCase(row.id), false, 'file-search');
      jump.dataset.browse = 'true';
    }
  },
  inboxActionLabel(row, match = '') {
    if (row.status === 'applying') return 'Resume saved filing';
    if (match) return row.kind === 'cloud' ? 'Link to existing Zotero reference' : 'Link to existing reMarkable copy';
    return row.kind === 'cloud' ? 'Add to Zotero' : row.kind === 'zotero' && !this.isInboxOfficeFile(row) ? 'Approve for reMarkable' : 'Approve & file';
  },
  isInboxOfficeFile(row) {
    return /\.(docx|pptx)$/i.test(row.payload.path || '');
  },
  inboxRowCaption(row, state = this.referencePDFState(row)) {
    if (row.kind === 'reference') {
      return state === 'none' ? (this.inboxIntakeMatch(row) ? 'Zotero · Matching PDF in ToSort' : 'Zotero · Needs a PDF')
        : state === 'preparing' ? 'Zotero · Preparing filename'
        : state === 'unavailable' ? 'Zotero · PDF unavailable here' : 'Zotero · Needs a check';
    }
    const source = row.kind === 'cloud' ? 'reMarkable' : row.kind === 'inbox' ? 'ToSort' : 'Zotero';
    if (row.status === 'applying') return source + ' · Resume filing';
    if (this.libraryFindings(row).length) return source + ' · Waiting on Library Issues';
    if (this.reviewNeedsUpgrade(row)) return source + ' · Awaiting review update';
    return source + (this.isInboxOfficeFile(row) ? ' · Office file' : ' · PDF filing');
  },
  reviewProposalValue(row, draft, field) {
    if (draft[field] === undefined) return row.payload[field];
    const baseline = draft.proposal_baseline?.[field] ?? row.payload.previous_proposal?.[field];
    if (!draft.edited_fields?.includes(field) && baseline !== undefined && draft[field] === baseline) return row.payload[field];
    return draft[field];
  },
  async dismissInboxReview(row) {
    const doc = this.view.document;
    const dialog = this.el('dialog', '', doc.body || doc.querySelector('body'), {class: 'review-dismiss', 'aria-labelledby': 'dismiss-review-title'});
    this.el('h2', 'Dismiss from Inbox?', dialog, {id: 'dismiss-review-title'});
    this.el('p', row.payload.title || row.payload.filename || 'Selected Inbox item', dialog);
    this.el('p', 'The Inbox review will be closed. Your Zotero reference and files will be kept.', dialog, {class: 'hint'});
    const choices = this.el('form', '', dialog, {method: 'dialog', class: 'actions'});
    const cancel = this.el('button', 'Cancel', choices, {value: 'cancel', autofocus: ''});
    this.el('button', 'Dismiss from Inbox', choices, {value: 'dismiss', class: 'danger'});
    const confirmed = await new Promise(resolve => {
      dialog.addEventListener('close', () => { const chosen = dialog.returnValue === 'dismiss'; dialog.remove(); resolve(chosen); }, {once: true});
      dialog.showModal(); cancel.focus();
    });
    if (confirmed) await this.action('dismiss_review', {identity: row.id, digest: row.digest});
  },
  renderReferenceIntake(row, detail, onStatus = () => {}) {
    this.el('h2', row.payload.title, detail);
    detail.classList.add('reference-intake');
    const status = this.el('p', '', detail, {role: 'status', 'data-reference-pdf-state': 'checking'});
    const reference = this.reviewNavigationButton('Show reference in Zotero', detail, 'references', [row.identity],
      {inbox: true, onOpened: () => inspectPDF()});
    const url = row.payload.url;
    const hasSourceLink = /^https?:\/\//i.test(url || '');
    if (hasSourceLink) {
      const open = this.button('Open source link', detail, () => Zotero.launchURL(url), false, 'external-link');
      open.dataset.browse = 'true';
    }
    const pdfControls = this.el('div', '', detail);
    const lookup = this.el('div', '', detail);
    lookup.hidden = true;
    const find = this.button('Check my Zotero library', lookup, async () => {
      find.disabled = true;
      try {
        const {source, matches} = await notableReferenceMatches(row.identity);
        if (!results.isConnected) return;
        results.replaceChildren();
        if (!matches.length) {
          this.el('p', 'No matching DOI or title found in your personal Zotero library.', results);
          return;
        }
        this.el('p', 'Compare the attachments and notes. Zotero can merge duplicates with the same item type.', results);
        for (const match of matches) {
          this.el('h3', match.reason, results);
          const comparison = this.el('div', '', results, {class: 'reference-comparison'});
          for (const [label, record] of [['Inbox reference', source], ['Existing Zotero reference', match]]) {
            const pane = this.el('section', '', comparison);
            this.el('strong', label, pane);
            this.el('p', record.title, pane);
            this.el('p', record.doi ? 'DOI ' + record.doi : 'No DOI recorded', pane);
            this.el('p', record.attachments.length ? 'Attachments: ' + record.attachments.join(', ') : 'No attachments', pane);
            this.el('p', record.collections.length ? 'Collections: ' + record.collections.join(', ') : 'No collection', pane);
          }
          this.reviewNavigationButton('Compare both records in Zotero', results, 'references', [row.identity, match.key]).classList.add('primary');
        }
      } finally { find.disabled = false; }
    }, true, 'file-search');
    find.dataset.browse = 'true';
    const results = this.el('div', '', lookup, {role: 'region', 'aria-label': 'Possible duplicate references'});
    const dismiss = this.button('Dismiss this reference', detail, () => this.dismissInboxReview(row));
    let serial = 0;
    const inspectPDF = async () => {
      const request = ++serial;
      status.setAttribute('data-reference-pdf-state', 'checking');
      status.textContent = 'Checking this Zotero reference for a PDF…';
      lookup.hidden = true;
      dismiss.hidden = true;
      pdfControls.replaceChildren();
      let result;
      try { result = await notableReferencePDFState(row.identity); }
      catch (_) { result = {state: 'unknown'}; }
      if (request !== serial || !status.isConnected) return;
      const hasConnection = result.state === 'none' && (this.latest.links || []).some(link => link.parent === row.identity);
      const selection = this.inboxReferenceSelection;
      const preparing = result.state === 'available' && (this.referencePDFState(row) === 'preparing'
        || (selection?.id === row.id && selection.attachment === result.attachment && !!result.attachment));
      const state = result.replacement ? 'replaced' : hasConnection ? 'linked' : preparing ? 'preparing' : result.state;
      status.setAttribute('data-reference-pdf-state', state);
      onStatus(state);
      const text = {
        none: 'This Zotero reference needs a PDF before it can be filed.',
        preparing: 'PDF attached. Preparing the filename for filing…',
        unavailable: 'Zotero lists a PDF for this reference. Open it in Zotero to download or locate the file, then use Check Inbox.',
        available: 'This Zotero reference already has a PDF. Use Check Inbox to refresh its filing status.',
        managed: 'This reference already has a reMarkable notes PDF. Use Check Inbox to refresh its filing status.',
        file: 'This reference already has a Word or PowerPoint file. Use Check Inbox to review it for filing in Zotero.',
        unsupported: 'Zotero has this PDF. Give the attachment filename a .pdf extension in Zotero, then use Check Inbox.',
        linked: 'This Zotero reference already has a reMarkable connection. Use Check Inbox to refresh this entry.',
        removed: 'This reference is unavailable in Zotero. Use Check Inbox to refresh this entry.',
        replaced: 'This reference changed after a Zotero merge. Use Check Inbox to review the surviving reference.',
        unknown: 'The PDF status could not be checked. Use Check Inbox to check Zotero again.'
      };
      status.textContent = state === 'replaced' && result.state === 'managed'
        ? 'The surviving Zotero reference already has a reMarkable notes PDF. Use Check Inbox to refresh its filing status.'
        : state === 'replaced' && result.state === 'available'
          ? 'The surviving Zotero reference already has a PDF. Use Check Inbox to refresh its filing status.'
          : text[state] || text.unknown;
      reference.hidden = state === 'removed';
      lookup.hidden = !['none', 'unavailable'].includes(state);
      dismiss.hidden = lookup.hidden;
      if (state === 'none') this.renderReferencePDFActions(row, pdfControls, inspectPDF, hasSourceLink);
      else if (result.attachment) {
        const showAttachment = ['unavailable', 'unsupported', 'file'].includes(result.state);
        const label = result.state === 'file' ? 'Show attached file in Zotero'
          : showAttachment ? 'Show PDF attachment in Zotero'
            : result.state === 'managed' ? 'Open notes PDF in Zotero' : 'Open PDF in Zotero';
        this.reviewNavigationButton(label, pdfControls, showAttachment ? 'attachment' : 'pdf',
          [result.attachment], {inbox: true, onOpened: inspectPDF});
      }
      if (this.referencePDFState(row) !== state) this.zoteroInboxDirty = true;
      if (this.busy || this.acquiringPDF) this.disable(true);
    };
    return inspectPDF();
  },
  renderReferencePDFActions(row, parent, inspectPDF, hasSourceLink) {
    const match = this.inboxIntakeMatch(row);
    if (match) {
      const suggestion = this.el('section', '', parent, {class: 'intake-pdf-match', 'aria-label': 'Matching PDF in ToSort'});
      this.el('strong', 'Matching PDF in ToSort', suggestion);
      this.el('p', match.filename, suggestion, {class: 'intake-pdf-filename'});
      this.el('p', `Matching DOI ${match.doi}. Zotero will store a copy and clear the verified original from ToSort.`, suggestion, {class: 'hint'});
      this.button('Attach PDF from ToSort', suggestion, () => {
        if (!suggestion.isConnected || this.acquiringPDF || !this.inboxIntakeMatch(row)) return;
        return this.action('attach_intake_pdf', {reference: row.id, identity: match.id, digest: match.digest, doi: match.doi});
      }, true, 'folder-input');
      const separate = this.button('Review ToSort file separately', suggestion, () => {
        this.separateIntakePDFs ||= new Set();
        this.separateIntakePDFs.add(match.id);
        this.inboxFilter = 'all';
        this.selected = match.id;
        this.inboxHandoffWaiting = match.id;
        this.revealReviewSelection = true;
        this.render();
      });
      separate.dataset.browse = 'true';
    }
    const pdfActions = this.el('div', '', parent, {class: 'reference-pdf-actions'});
    if (hasSourceLink) this.el('p', 'Save the PDF to Downloads in your browser, then attach it here.', pdfActions, {class: 'hint'});
    const acquire = async chooseFile => {
      if ((this.busy && this.operation?.method !== 'sync') || this.acquiringPDF || !pdfActions.isConnected) return;
      this.acquiringPDF = true; this.disable(true);
      progress.textContent = chooseFile ? 'Choosing a PDF…' : 'Looking for an available PDF…';
      let acquired, attempted = false;
      try {
        const file = chooseFile ? await chooseFile() : null;
        if (chooseFile && !file) return;
        if (file) progress.textContent = 'Attaching the PDF in Zotero…';
        attempted = true;
        acquired = await notableAcquirePDF(row.identity, file, {describe: true});
        if (this.inboxReferenceSelection?.id === row.id && this.selected === row.id
            && notableIsPDFFile(acquired.attachment)) {
          this.inboxReferenceSelection.attachment = acquired.attachment.key;
        }
        this.zoteroInboxDirty = true;
      } finally {
        this.acquiringPDF = false; this.disable(this.busy); progress.textContent = '';
        if (attempted && parent.isConnected) await inspectPDF();
        this.drainSyncDecisions(); this.drainTagEdits(); this.drainAfterSync();
      }
      await this.checkInbox();
      this.message(acquired.outcome === 'existing' ? 'A PDF was already attached in Zotero.'
        : notableIsPDFFile(acquired.attachment) ? 'PDF attached in Zotero.'
          : 'Zotero attached another file type. This reference still needs a PDF for reMarkable.');
    };
    const progress = this.el('p', '', pdfActions, {class: 'hint', role: 'status'});
    this.button('Attach downloaded PDF', pdfActions, () => acquire(async () => {
      const {FilePicker} = ChromeUtils.importESModule('chrome://zotero/content/modules/filePicker.mjs');
      const picker = new FilePicker(); picker.init(this.view, 'Attach downloaded PDF', picker.modeOpen);
      picker.appendFilter('PDF files', '*.pdf');
      try { picker.displayDirectory = Services.dirsvc.get('DfltDwnld', Ci.nsIFile).path; }
      catch (_) {} // Use the native picker default when Windows has no Downloads directory.
      return await picker.show() === picker.returnOK ? picker.file : null;
    }), true, 'folder-input').dataset.pdfAcquisition = 'true';
    this.button('Find and attach PDF online', pdfActions, () => acquire(null), false, 'file-search').dataset.pdfAcquisition = 'true';
  },
  linkedOriginalCases() {
    const connection = this.filingConnection();
    const cases = new Map();
    for (const issue of [...(this.latest?.linked_original_matches || []), ...(this.readerOriginalMatches?.values() || [])]) {
      if (issue.connection && issue.connection !== connection) continue;
      const link = this.latest?.links?.find(row => row.cloud === issue.cloud && !row.unavailable);
      if (link?.view_key && issue.digest === link.baseline && issue.attachment !== link.view_key
          && issue.parent && issue.parent !== link.parent) {
        cases.set(JSON.stringify([issue.attachment, issue.cloud]), {issue, link});
      }
    }
    return [...cases.values()];
  },
  renderLinkedOriginalCases(content, cases) {
    for (const {issue, link} of cases) {
      const card = this.el('section', '', content, {class: 'card'});
      this.el('h3', issue.title || link.name || 'Two Zotero records for the same original PDF', card);
      this.el('p', 'The first PDF matches the original saved with the reMarkable link. Compare the records before merging.', card);
      const comparison = this.el('div', '', card, {class: 'reference-comparison'});
      const sides = [['Zotero record', issue.parent, issue.attachment],
        ['Record linked to reMarkable', link.parent, link.view_key]];
      const pending = sides.map(async ([label, parentKey, attachmentKey]) => {
        const pane = this.el('section', '', comparison);
        this.el('h4', label, pane);
        const info = this.el('p', 'Reading Zotero details…', pane);
        const [parent, pdf] = await Promise.all([notableItem(parentKey), notableItem(attachmentKey)]);
        if (!pane.isConnected) return null;
        if (!parent?.isRegularItem() || parent.deleted || !pdf?.isPDFAttachment() || pdf.deleted || pdf.parentKey !== parent.key) {
          info.textContent = 'This record or attachment changed. Sync to refresh the comparison.';
          return null;
        }
        if (attachmentKey === issue.attachment && await notablePDFDigest(pdf, Zotero.getMainWindow().crypto) !== issue.digest) {
          info.textContent = 'This PDF changed since the match was found. Sync to refresh the comparison.';
          return null;
        }
        if (!pane.isConnected) return null;
        info.textContent = parent.getDisplayTitle();
        const doi = parent.getField('DOI');
        this.el('p', Zotero.ItemTypes.getLocalizedString(parent.itemTypeID) + (doi ? ' · DOI ' + doi : ''), pane);
        const children = await Zotero.Items.getAsync([...parent.getAttachments(), ...parent.getNotes()]);
        for (const item of children) {
          await item.loadAllData();
          if (!pane.isConnected) return null;
          if (item.deleted) continue;
          const notes = item.isPDFAttachment() ? ` · ${item.getAnnotations().length} annotations` : '';
          this.el('p', item.getDisplayTitle() + notes, pane, {class: 'hint'});
        }
        this.reviewNavigationButton('Open PDF in Zotero', pane, 'pdf', [attachmentKey]);
        return parent;
      });
      Promise.all(pending).then(([left, right]) => {
        if (!card.isConnected || !left || !right) return;
        if (left.itemType !== right.itemType) this.el('p', 'Zotero requires matching item types to merge. Check the correct item type before merging.', card);
        this.reviewNavigationButton('Compare both records in Zotero', card, 'references', [left.key, right.key]);
      }).catch(() => { if (card.isConnected) this.el('p', 'Could not read both Zotero records. Reopen Library Issues to retry.', card); });
    }
  },
  renderReview(content, queue = null) {

    this.renderedReviewId = null;
    const heading = this.el('div', '', content, {class: 'review-heading'});
    const inboxTerms = queue === 'inbox' ? this.searchTerms(this.inboxQuery || '') : [];
    let inboxRows = queue === 'inbox' ? this.inboxDisplayRows() : null;
    if (inboxTerms.length) {
      if (this.inboxFilter === 'all') inboxRows = this.sortInboxRows([...new Map(
        [...inboxRows, ...this.queueRows('inbox-waiting')].map(row => [row.id, row])).values()]);
      inboxRows = inboxRows.filter(({payload}) => this.matchesSearchTerms(
        [payload.title, payload.filename, payload.doi].filter(Boolean).join('\n'), inboxTerms));
    }
    let updateInboxControls;
    if (queue) {
      this.el('h2', queue === 'inbox' ? 'Inbox' : queue === 'library' ? 'Library Issues' : 'Sync Conflicts', heading);
      if (inboxRows) {
        updateInboxControls = this.renderInboxControls(heading);
        this.quickSearch(heading, {label: 'Search Inbox', value: this.inboxQuery || '', change: value => {
          this.inboxQuery = value;
          this.render();
        }});
        if (inboxTerms.length) this.el('p', `${inboxRows.length} matching Inbox entries`, heading,
          {class: 'paper-search-count', role: 'status'});
      }
      else this.el('p', this.queueSummary(queue), heading, {class: 'queue-summary', role: 'status', 'data-queue-summary': queue});
    }
    let recheck;
    if (queue !== 'sync-review') {
      recheck = this.el('section', '', heading, {class: 'review-recheck', 'aria-label': 'Local review check'});
      this.el('p', this.reviewFreshnessMessage(queue), recheck, {id: 'notable-review-freshness', role: 'status'});
      this.button(this.reviewUpgrade?.state === 'failed' ? 'Retry saved review update' : queue === 'inbox' ? 'Check Inbox' : 'Recheck Zotero', recheck,
        () => (this.latest.reviews || []).some(row => this.reviewNeedsUpgrade(row))
          ? this.prepareReviewGuidance({retry: true}) : this.action(queue === 'inbox' ? 'check_inbox' : 'recheck_review'), false, 'refresh-cw');
    }
    const queuedIds = this.queuedReviewIds();
    const routine = (this.latest.reviews || []).filter(row => row.status === 'pending' && row.payload.routine && !this.reviewNeedsUpgrade(row) && !queuedIds.has(row.id));
    if (!queue && routine.length) {
      const card = this.el('section', '', content, {class: 'card review-form'});
      this.el('h2', `${routine.length} routine documents ready`, card);
      this.el('p', 'Missing citation fields do not block filing. Only uncertain identity, versions, unreadable files, or destination conflicts need individual review.', card);
      const destination = this.destinationSelect('Linked folders for this batch (Zotero → reMarkable)', card, this.batchCollection || this.latest.config.zotero_root);
      destination.addEventListener('change', () => { this.batchCollection = destination.value; });
      this.el('p', 'Papers already on the tablet keep their folders. New tablet copies use the destination above. Approved transfers run at the next sync.', card, {class: 'hint'});
      const batch = this.button(`File ${routine.length} routine documents together`, card, async () => {
        await this.queueFiling('approve_routine', {collection: destination.value,
          entries: routine.map(row => ({id: row.id, digest: row.digest, review_token: row.payload.review_token}))}, `${routine.length} routine documents`);
      }, true); batch.dataset.queue = 'true';
      const preview = this.el('details', '', card); this.el('summary', 'Optional filename preview', preview);
      for (const row of routine) this.el('p', row.payload.filename, preview);
      const browse = this.button(this.showAllReviews ? 'Show only papers needing attention' : 'Browse all papers', card,
        () => { this.showAllReviews = !this.showAllReviews; this.selected = null; this.render(); });
      browse.dataset.browse = 'true';
    }
    if (!queue || queue === 'sync-review') {
    const shown = new Set();
    if (this.syncDecisionReadError) this.el('p', this.syncDecisionReadError, content, {role: 'alert', class: 'message error'});
    this.renderFolderIssues(content, shown);
    const mergeWarnings = this.syncWarnings().filter(issue => issue.merge);
    for (const row of this.latest.local_deletions || []) {
      if (mergeWarnings.some(issue => issue.cloud === row.payload.cloud)) continue;
      const card = this.el('section', '', content, {class: 'card decision'});
      this.el('h2', 'Trashed in Zotero: ' + row.payload.title, card);
      if (this.renderSavedSyncDecision(card, 'local_delete_decision', row.id, shown)) continue;
      this.el('p', 'The reMarkable copy remains. This connection stays paused.', card);
      for (const [choice, label, detail] of [
        ['restore_zotero', 'Restore in Zotero', 'Return the Zotero item to the library and resume its reMarkable link.'],
        ['keep_tablet', 'Keep reMarkable copy', 'Keep the reMarkable copy and leave the Zotero item in Trash. The link stays paused.'],
        ['trash_tablet', 'Move reMarkable copy to Trash', 'Move the reMarkable copy and its handwritten notes to Trash.']]) {
        this.syncDecisionButton(card, 'local_delete_decision', row.id, row.payload.title, label,
          {identity: row.id, choice}, detail, {icon: choice === 'trash_tablet' ? 'trash-2' : undefined});
      }
    }
    const deletions = this.latest.deletions || [];
    for (const row of deletions) {
      const card = this.el('section', '', content, {class: 'card decision'});
      this.el('h2', 'Trashed on reMarkable: ' + row.payload.title, card);
      if (this.renderSavedSyncDecision(card, 'delete_decision', row.id, shown)) continue;
      this.el('p', 'Restore the reMarkable copy, keep the Zotero copy, or move the selected Zotero items to Trash.', card);
      const names = (row.payload.inventory || []).filter(i => i.key !== row.payload.parent && !i.deleted).map(i => i.title || i.itemType);
      this.el('p', 'This reference contains: ' + names.join('; '), card);
      const folderChoices = this.mappedDestinations().map(folder => [this.latest.mappings[folder.key].cloud, folder.tablet]);
      const restoreHome = this.folderSelect('Restore to reMarkable folder', card, [['', 'Use its previous reMarkable folder'], ...folderChoices], '', false, 'tablet', {
        createKind: 'paired',
        parentForCreate: value => Object.keys(this.latest.mappings || {}).find(key => this.latest.mappings[key].cloud === value) || this.latest.config.zotero_root,
        createdKey: created => created.tablet,
        choices: () => this.mappedDestinations().map(folder => [this.latest.mappings[folder.key].cloud, folder.tablet])});
      this.el('p', 'If its previous folder was removed, choose another linked folder before restoring.', card, {class: 'hint'});
      const buttons = this.el('div', '', card, {class: 'actions'});
      for (const [choice, label, detail] of [
        ['restore', 'Restore on reMarkable', request => 'Restore the paper and its notes to ' +
          (folderChoices.find(([key]) => key === request.destination)?.[1] || 'its previous reMarkable folder') + '.'],
        ['keep_zotero', 'Keep Zotero copy', 'Keep the Zotero reference and files. The reMarkable copy stays in Trash.'],
        ['pdf_only', 'Trash linked PDFs in Zotero', 'Trash the linked PDFs. The Zotero reference and its other attachments stay in the library.'],
        ['remove_collections', 'Remove from selected collections', 'Keep the Zotero reference in My Library and choose which collection memberships to remove.'],
        ['everything', 'Trash reference everywhere in Zotero', 'Trash the Zotero reference and its attachments, including in every collection where it appears.']]) {
        if (choice === 'remove_collections' && !row.payload.parent) continue;
        this.syncDecisionButton(buttons, 'delete_decision', row.id, row.payload.title, label,
          {identity: row.id, choice, destination: choice === 'restore' ? () => restoreHome.value : ''}, detail,
          {parent: row.payload.parent, primary: choice === 'restore', icon: ['pdf_only', 'everything'].includes(choice) ? 'trash-2' : undefined});
      }
    }
    for (const job of this.syncDecisions) if (!shown.has(job.key)) {
      const card = this.el('section', '', content, {class: 'card decision'});
      this.el('h2', job.title, card);
      this.renderQueuedSyncDecision(card, job);
    }
    const warnings = this.syncWarnings();
    if (warnings.length) {
      const group = this.el('details', '', content, {class: 'card decision sync-warnings'});
      group.open = this.syncWarningsOpen !== false;
      group.addEventListener('toggle', () => { if (group.isConnected) this.syncWarningsOpen = group.open; });
      this.el('summary', `Sync warnings (${warnings.length})`, group);
      for (const issue of warnings) {
        const link = (this.latest.links || []).find(row => row.cloud === issue.cloud || row.source_key === issue.attachment || row.view_key === issue.attachment);
        const card = this.el('section', '', group, {class: 'sync-decision-item'});
        const title = issue.title || link?.name || (issue.attachment ? 'Zotero attachment ' + issue.attachment : 'Sync warning');
        this.el('h3', issue.merge ? 'Zotero merge or attachment change: ' + title : title, card);
        this.el('p', issue.reason, card);
        if (issue.merge) {
          if (issue.parent) this.reviewNavigationButton('Show surviving reference in Zotero', card, 'references', [issue.parent]);
          if (link) this.button('Check connection', card, () => this.previewLinkRepair(link.cloud)).dataset.browse = 'true';
          if (link) this.renderLinkRepair(card, link.cloud);
          continue;
        }
        if (issue.attachment) this.reviewNavigationButton('Show affected attachment in Zotero', card, 'attachment', [issue.attachment]);
        if (issue.collection) this.reviewNavigationButton('Show Zotero collection', card, 'collection', [issue.collection]);
        if (issue.parent) this.reviewNavigationButton('Open affected reference in Zotero', card, 'references', [issue.parent]);
        if (link?.parent && link.parent !== issue.parent) {
          this.el('p', 'Already linked to reMarkable · ' + (link.name || link.parent), card, {class: 'hint'});
          this.reviewNavigationButton('Open reference already linked to reMarkable', card, 'references', [link.parent]);
        }
      }
    }
    }
    if (inboxRows && this.inboxFilter === 'all' && !inboxTerms.length) this.renderInboxWaiting(content);
    const linkedCases = queue === 'library' ? this.linkedOriginalCases() : [];
    if (linkedCases.length) this.renderLinkedOriginalCases(content, linkedCases);
    const rows = inboxRows ?? (queue ? this.queueRows(queue) : (this.latest.reviews || []).filter(row => !queuedIds.has(row.id) && (this.showAllReviews || !row.payload.routine || this.reviewNeedsUpgrade(row) || row.status === 'applying' || row.id === this.selected)));
    if (!rows.length && linkedCases.length) return;
    if (!rows.length && queue === 'sync-review' && content.querySelector?.('.decision')) return;
    if (!rows.length && queue) {
      const blocked = queue === 'inbox' && this.queueRows('inbox-waiting').length;
      const empty = this.el('section', '', content, {class: 'review-empty'});
      const inboxEmpty = {all: blocked ? 'No papers ready to file' : 'Inbox is empty', ready: 'No papers ready to file',
        'needs-pdf': 'No papers need a PDF', waiting: 'No papers waiting on Library Issues'};
      this.el('h3', inboxTerms.length ? 'No Inbox entries match this search.' : queue === 'inbox' ? inboxEmpty[this.inboxFilter]
        : queue === 'library' ? 'No library issues' : 'No sync conflicts', empty);
      if (blocked && this.inboxFilter === 'all' && !inboxTerms.length) {
        const jump = this.button('Open Library Issues', empty, () => this.openLibraryCase(this.queueRows('inbox-waiting')[0]?.id), true, 'file-search');
        jump.dataset.browse = 'true';
      }
      if (recheck) empty.appendChild(recheck);
      return;
    }
    if (!rows.length) { this.el('h2', 'No individual reviews needed', content); this.el('p', 'Routine documents can be filed together above. Approved transfers run at the next sync. Use Import library to bring in an existing Windows collection.', content); return; }
    if (!rows.some(row => row.id === this.selected)) this.selected = rows[0].id;
    const layout = this.el('div', '', content, {class: 'review-layout'}), list = this.el('aside', '', layout, {'aria-label': queue === 'inbox' ? 'Inbox entries' : 'Documents awaiting review'});
    let selectedCaption;
    for (const row of rows) {
      const button = this.button(row.payload.title || row.payload.desktop?.[0] || 'Folder conflict', list, () => { this.selected = row.id; this.reviewFocus = true; this.render(); }, false, 'file-text', {help: queue !== 'inbox'});
      if (queue === 'inbox') {
        const caption = this.el('span', this.inboxRowCaption(row), button.querySelector?.('.button-label') || button, {class: 'inbox-row-status'});
        if (row.id === this.selected) selectedCaption = caption;
      }
      button.dataset.browse = 'true';
      button.setAttribute('aria-pressed', String(row.id === (this.selected || rows[0].id)));
      button.addEventListener('keydown', event => {
        if (!['ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        const index = rows.findIndex(value => value.id === row.id);
        const next = event.key === 'Home' ? 0 : event.key === 'End' ? rows.length - 1 : Math.max(0, Math.min(rows.length - 1, index + (event.key === 'ArrowUp' ? -1 : 1)));
        this.selected = rows[next].id; this.reviewFocus = true; this.render();
      });
    }
    const row = rows.find(r => r.id === this.selected) || rows[0], detail = this.el('section', '', layout, {class: 'card review-form'});
    this.renderedReviewId = row.id;
    if (inboxRows && this.inboxReferenceSelection?.id !== row.id) {
      this.inboxReferenceSelection = row.kind === 'reference' ? {id: row.id, parent: row.identity} : null;
    }
    if (row.kind === 'conflict') {
      this.el('h2', row.payload.type === 'paper' ? 'Name or folder changed on both devices' : 'Folder changed on both devices', detail);
      this.el('p', row.payload.type === 'paper' ? 'Apply one side’s name and folder to both copies. PDF contents stay the same.' : 'Apply one side’s folder name and location. Papers and notes are kept.', detail);
      this.renderConflictComparison(row, detail);
      if (row.payload.type === 'paper') this.reviewOpenButton(row, detail);
      if (row.payload.type === 'paper') {
        this.reviewNavigationButton('Open Zotero reference', detail, 'references', [row.payload.zotero], {conflict: true});
      } else {
        this.reviewNavigationButton('Show Zotero collection', detail, 'collection', [row.payload.zotero], {conflict: true});
      }
      for (const [side, label] of [['desktop', 'Apply Zotero name and location'], ['tablet', 'Apply reMarkable name and location']]) this.button(label, detail,
        () => this.action('resolve_conflict', {identity: row.id, digest: row.digest, side}));
      return;
    }
    if (queue === 'library') {
      this.renderReviewFindings({...row, payload: {...row.payload, findings: this.libraryFindings(row)}}, detail, true);
      for (const id of row.caseRows || [row.id]) {
        const source = this.latest.reviews.find(item => item.id === id);
        if (source?.payload.source_removed && !(this.latest.links || []).some(link => [link.source_key, link.view_key].includes(source.identity))) this.button('Close unavailable review', detail, () => this.action('close_removed_source',
          {identity: source.id, digest: source.digest, review_token: source.payload.review_token}));
      }
      return;
    }
    if (row.kind === 'reference') {
      this.renderReferenceIntake(row, detail, state => {
        if (selectedCaption) selectedCaption.textContent = this.inboxRowCaption(row, state);
        updateInboxControls?.({id: row.id, state});
      });
      return;
    }
    if (queue === 'inbox' && row.status === 'applying') { this.renderApplyingReview(row, detail); return; }
    this.el('h2', queue === 'inbox' ? (row.payload.title || row.payload.filename || 'Incoming file') : 'Review document', detail);
    this.el('p', queue === 'inbox' ? this.inboxRowCaption(row)
      : 'From: ' + (row.kind === 'cloud' ? 'reMarkable' : row.kind === 'inbox' ? 'ToSort' : 'Zotero'), detail, {class: 'review-link-badge'});
    if (queue === 'inbox' && row.status !== 'applying' && this.libraryFindings(row).length) {
      this.el('p', 'Resolve the matching items or file problem before filing this paper.', detail);
      this.reviewOpenButton(row, detail);
      const jump = this.button('Resolve in Library Issues', detail, () => this.openLibraryCase(row.id), true, 'file-search');
      jump.dataset.browse = 'true'; return;
    }
    this.reviewOpenButton(row, detail);
    if (queue === 'inbox' && row.payload.parent) this.reviewNavigationButton('Show reference in Zotero', detail, 'references', [row.payload.parent], {inbox: true});
    if (this.reviewNeedsUpgrade(row)) this.renderSavedReviewUpdate(row, detail);
    else if (row.payload.findings?.length) {
      const active = row.payload.findings.filter(finding => queue !== 'inbox' || !finding.acknowledgement?.acknowledged);
      if (active.length) this.renderReviewFindings({...row, payload: {...row.payload, findings: active}}, detail);
      const kept = queue === 'inbox' ? row.payload.findings.filter(finding => finding.acknowledgement?.acknowledged) : [];
      if (kept.length) {
        const decisions = this.el('details', '', detail); this.el('summary', 'Earlier review choices', decisions);
        for (const finding of kept) this.button('Review this choice again', decisions, () => this.action('acknowledge_review_finding',
          {identity: row.id, digest: row.digest, code: finding.code, token: finding.acknowledgement.token, review_token: row.payload.review_token, keep_separate: false}));
      }
    }
    else for (const issue of row.payload.issues || []) {
      this.el('p', issue, detail, {class: 'message error'});
      this.el('p', this.reviewIssueHelp(issue), detail, {class: 'hint'});
    }
    if (row.kind === 'inbox' && row.status !== 'applying') this.button('Dismiss this intake review', detail,
      () => this.dismissInboxReview(row));
    const filingOptions = this.el('details', '', detail, {'data-review-detail': 'filing'});
    filingOptions.open = queue === 'inbox' || row.status === 'applying' || !row.payload.findings?.length;
    this.el('summary', 'Filing choices', filingOptions);
    const filing = this.el('div', '', filingOptions, {class: 'review-form'});
    const savedDraft = this.reviewDrafts.get(row.id);
    const draft = savedDraft?.digest === row.digest ? savedDraft : {};
    const editedFields = new Set(draft.edited_fields || []);
    for (const field of ['title', 'filename', 'document_type']) {
      const baseline = draft.proposal_baseline?.[field] ?? row.payload.previous_proposal?.[field];
      if (draft[field] !== undefined && baseline !== undefined && draft[field] !== baseline
          && !(field === 'filename' && draft.generated_filename)) editedFields.add(field);
    }
    if (savedDraft && savedDraft.digest !== row.digest) {
      this.el('p', 'The source file changed. Current suggestions are shown. Inspect your earlier unsaved choices before editing this file.', filing, {class: 'hint'});
      const earlier = this.el('details', '', filing, {'data-review-detail': 'earlier-draft'});
      this.el('summary', 'Earlier unsaved choices', earlier);
      for (const [label, value] of [['Title', savedDraft.title], ['Filename', savedDraft.filename], ['Document type', this.latest.types[savedDraft.document_type] || savedDraft.document_type]]) {
        if (value) this.el('p', label + ': ' + value, earlier);
      }
      this.button('Use earlier filing choices for this file', earlier, () => {
        this.reviewDrafts.set(row.id, {...savedDraft, digest: row.digest, review_token: row.payload.review_token}); this.render();
      }).dataset.browse = 'true';
    } else if (draft.review_token && draft.review_token !== row.payload.review_token) {
      this.el('p', 'The Zotero evidence changed. Your unsaved filing choices are still shown. Compare the updated guidance before approving.', filing, {class: 'review-draft-notice', role: 'status'});
      const reset = this.button('Use current Zotero suggestions', filing, () => { this.reviewDrafts.delete(row.id); this.render(); });
      reset.dataset.browse = 'true';
    }
    if (row.payload.local_recheck_error) this.el('p', row.payload.local_recheck_error, filing, {class: 'message error', role: 'alert'});
    const title = this.field('Title', filing, this.reviewProposalValue(row, draft, 'title'), 'input', {wrap: true});
    const isSupplement = row.payload.details?.is_supplementary_material || /(?:^|[_ -])si(?:\.pdf)?$/i.test(row.payload.filename || '');
    const type = this.select('Document type', filing, [...Object.entries(this.latest.types), ['supporting_information', 'Supporting Information']],
      draft.document_type !== undefined ? this.reviewProposalValue(row, draft, 'document_type') : (isSupplement ? 'supporting_information' : row.payload.document_type));
    const main = this.supportingInformationPicker(detail, draft);
    const updateSupplement = () => {
      main.panel.hidden = type.value !== 'supporting_information';
      if (!main.panel.hidden && !main.loaded) { main.loaded = true; main.load(); }
    };
    type.addEventListener('change', updateSupplement); updateSupplement();
    const filename = this.field('Proposed filename', filing, this.reviewProposalValue(row, draft, 'filename'));
    let generatedFilename = !!draft.generated_filename, needsFilenamePreview = !!draft.filename_needs_refresh, previewSerial = 0, pendingPreview;
    const editedCitation = {...draft.citation};
    const citationEdits = () => Object.fromEntries(Object.entries(editedCitation).filter(([key]) =>
      type.value !== 'supporting_information' && (key !== 'patent_number' || type.value.startsWith('patent'))));
    const requestFilename = () => {
      const serial = ++previewSerial, previous = filename.value;
      const params = {identity: row.id, document_type: type.value, title: title.value, citation: citationEdits()};
      pendingPreview = this.rpc('preview', params).then(result => {
        if (serial !== previewSerial || filename.isConnected === false || filename.value !== previous
            || type.value !== params.document_type || title.value !== params.title
            || JSON.stringify(citationEdits()) !== JSON.stringify(params.citation) || !result?.filename) return;
        filename.value = result.filename; filename.notableRefreshValue();
        generatedFilename = true; needsFilenamePreview = false; editedFields.delete('filename'); saveDraft();
      }).finally(() => { if (serial === previewSerial) pendingPreview = null; });
      return pendingPreview;
    };
    if (row.payload.naming_evidence) {
      const evidence = row.payload.naming_evidence;
      this.el('p', `${evidence.source} · ${evidence.method} · Checked ${new Date(evidence.checked_at).toLocaleString()}`, filing, {class: 'hint'});
    }
    this.button('Suggest filename', filing, requestFilename);
    const collections = this.latest.collections || [];
    const paths = this.paths(collections, 'key', 'parentCollection', 'name');
    const mapped = this.latest.mappings || {};
    const cloudHome = row.payload.tablet_parent || this.latest.config.default_folder || this.latest.config.tablet_root;
    const originalHomes = (row.payload.source_collections || []).filter(key => mapped[key]);
    const matchHome = row.kind === 'zotero' && row.payload.matches?.length === 1
      ? Object.entries(mapped).find(([, value]) => value.cloud === row.payload.matches[0].parent)?.[0] : null;
    const defaultHome = matchHome || (row.kind === 'zotero' && originalHomes.length === 1 ? originalHomes[0]
      : Object.entries(mapped).find(([key, value]) => value.cloud === cloudHome)?.[0] || this.latest.config.zotero_root);
    const previousHome = (this.latest.links || []).find(link => link.parent === row.payload.parent)?.collection;
    const home = this.destinationSelect('Destination folders', filing, draft.collection ?? previousHome ?? defaultHome);
    const research = this.folderSelect('Research collections (optional)', filing, paths, draft.research || [], true, 'zotero', {choices: () => this.paths(this.collectionCatalog(), 'key', 'parentCollection', 'name')});
    research.dataset.zoteroCollections = 'research';
    let match;
    if (row.payload.matches?.length) match = this.paperSelect(row.kind === 'cloud' ? 'PDF already found in Zotero' : 'PDF already found on reMarkable', filing,
      [['', row.kind === 'cloud' ? 'Choose an existing Zotero PDF…' : 'Create a separate reMarkable copy'], ...row.payload.matches.map(m => [m.key, 'Use: ' + m.title])],
      draft.match ?? (row.payload.matches.length === 1 ? row.payload.matches[0].key : ''), {details: option => {const match = row.payload.matches.find(value => value.key === option.value); return match?.filename || match?.name || '';}});
    if (match) this.el('p', 'Using the existing copy keeps its notes. Your approval can update its name and folder.', filing, {class: 'hint'});
    const citation = this.el('details', '', filing, {'data-review-detail': 'citation'}); this.el('summary', 'Citation details', citation);
    const details = row.payload.details || {};
    this.el('p', 'Edits are saved to the Zotero reference when you approve. Use Suggest filename to include citation changes in the name.', citation, {class: 'hint'});
    const originals = row.payload.citation || {creators: (details.creators || [details.primary_creator]).filter(Boolean).join('\n'),
      date: details.year, venue: details.venue_or_publisher || details.journal, doi: details.identifier?.doi,
      patent_number: details.patent_number || details.identifier?.patent};
    const patent = this.el('div', '', citation);
    const citationInputs = [['creators', 'Creators (one per line)'], ['date', 'Date or year'],
      ['venue', 'Journal, publisher, or institution'], ['doi', 'DOI'], ['patent_number', 'Patent number']];
    for (const [key, label] of citationInputs) {
      const initial = String(originals[key] || '').replace(/^Unknown(?: (?:author|journal|publisher))?$/i, '');
      const input = this.field(label, key === 'patent_number' ? patent : citation, editedCitation[key] ?? initial,
        key === 'creators' ? 'textarea' : 'input');
      if (key === 'creators') { input.rows = 3; input.classList?.add('notable-wrapped-field'); }
      input.addEventListener('input', () => {
        if (input.value === initial) delete editedCitation[key]; else editedCitation[key] = input.value;
        saveDraft();
      });
    }
    const showCitation = () => { citation.hidden = type.value === 'supporting_information'; patent.hidden = !type.value.startsWith('patent'); };
    showCitation();
    type.addEventListener('change', showCitation);
    if (queue !== 'inbox' && row.payload.parent) this.reviewNavigationButton('Open reference in Zotero', citation, 'references', [row.payload.parent]);
    const tagDetails = this.el('details', '', filing, {'data-review-detail': 'paper-tags'});
    this.el('summary', 'Paper tags', tagDetails);
    this.el('p', 'Tags are added to the Zotero reference when you approve.', tagDetails, {class: 'hint'});
    const paperTags = this.renderTagEntry(tagDetails, {kind: 'paper', values: draft.tags || [], inputValue: draft.tag_input || '',
      list: this.tagSuggestions(tagDetails, 'paper'),
      empty: 'No tags added to this filing.', onChange: () => saveDraft()});
    this.paperTagPanel(tagDetails, {identity: row.id});
    if (this.isInboxOfficeFile(row)) this.el('p', 'This Office file can be filed in Zotero.', filing, {class: 'hint'});
    const approve = this.button(this.busy ? 'Add to filing queue' : queue === 'inbox' ? this.inboxActionLabel(row, match?.value) : 'Approve & file', filing, async () => {
      if (pendingPreview) await pendingPreview;
      if (needsFilenamePreview) await requestFilename();
      if (needsFilenamePreview) throw Error('Wait for the updated filename suggestion, or enter the filename to use.');
      if (!main.panel.hidden && !main.select.value) throw Error('Choose the main paper in Zotero for this supporting information.');
      paperTags.add();
      await this.queueFiling('approve', {identity: row.id, digest: row.digest, filename: filename.value, title: title.value,
        document_type: type.value, collection: home.value, research: Array.from(research.selectedOptions, o => o.value), match: match?.value || '', review_token: row.payload.review_token || '',
        citation: citationEdits(), tags: [...paperTags.tags],
        main_reference: main.panel.hidden ? '' : main.select.value,
        main_reference_metadata: main.panel.hidden ? null : JSON.parse(main.select.selectedOptions[0].dataset.metadata)});
    }, true); approve.dataset.queue = 'true'; approve.dataset.approve = 'true';
    if (queue === 'inbox') {
      approve.dataset.idleLabel = this.inboxActionLabel(row, match?.value);
      this.setButtonHelp(approve, this.buttonHelp(approve.dataset.idleLabel));
      match?.addEventListener('change', () => { approve.dataset.idleLabel = this.inboxActionLabel(row, match.value); this.setButtonHelp(approve, this.buttonHelp(approve.dataset.idleLabel)); const label = approve.querySelector?.('.button-label'); if (label && !this.busy) label.textContent = approve.dataset.idleLabel; });
    }
    approve.dataset.invalid = String(!!row.payload.local_recheck_error || this.reviewNeedsUpgrade(row));
    const saveDraft = () => this.reviewDrafts.set(row.id, {digest: row.digest, review_token: draft.review_token ?? row.payload.review_token, title: title.value, document_type: type.value, filename: filename.value,
      proposal_baseline: {title: row.payload.title, filename: row.payload.filename, document_type: row.payload.document_type}, edited_fields: [...editedFields],
      generated_filename: generatedFilename, filename_needs_refresh: needsFilenamePreview, citation: {...editedCitation},
      tags: [...paperTags.tags], tag_input: paperTags.input.value,
      collection: home.value, research: Array.from(research.selectedOptions, o => o.value), match: match?.value || '',
      main_reference: main.select.value, main_query: main.query.value});
    for (const [field, name] of [[title, 'title'], [filename, 'filename'], [type, 'document_type']]) {
      const edited = () => { editedFields.add(name); if (name === 'filename') { generatedFilename = false; needsFilenamePreview = false; } };
      field.addEventListener('input', edited);
      field.addEventListener('change', edited);
    }
    for (const field of [title, type, filename, home, research, match, main.select, main.query].filter(Boolean)) {
      field.addEventListener('input', saveDraft); field.addEventListener('change', saveDraft);
    }
    type.addEventListener('change', () => {
      if (!editedFields.has('filename')) {
        needsFilenamePreview = true; saveDraft();
        requestFilename().catch(error => this.message(error.message, true));
      }
    });
  },
  renderApplyingReview(row, parent) {
    this.el('h2', 'Finish interrupted filing', parent);
    this.reviewOpenButton(row, parent);
    const saved = row.approval || {};
    const complete = ['title', 'filename', 'document_type', 'collection', 'parent'].every(key => typeof saved[key] === 'string' && saved[key].trim())
      && (saved.research === undefined || (Array.isArray(saved.research) && saved.research.every(key => typeof key === 'string')))
      && (saved.match === undefined || typeof saved.match === 'string');
    if (!complete) {
      this.el('p', 'This older filing has incomplete saved choices and cannot be resumed safely here. Its files and recovery record are retained.', parent, {class: 'message error', role: 'alert'});
      return;
    }
    this.el('p', 'Continue with the choices already saved for this paper.', parent, {class: 'hint'});
    const destination = this.mappedDestinations().find(value => value.key === saved.collection);
    const choices = this.el('dl', '', parent);
    for (const [label, value] of [['Title', saved.title], ['Filename', saved.filename],
      ['Document type', this.latest.types[saved.document_type] || saved.document_type],
      ['Destination', destination?.label || saved.collection]]) {
      this.el('dt', label, choices); this.el('dd', value, choices);
    }
    const resume = this.button('Resume saved filing', parent, async () => {
      let mainMetadata = null;
      const supplement = saved.document_type === 'supporting_information';
      if (supplement) {
        const main = await notableItem(saved.parent);
        if (!main || main.key !== saved.parent || main.deleted || main.isInTrash?.() || !main.isRegularItem()) {
          throw Error('The saved main reference is unavailable. Restore that exact reference in Zotero before resuming this filing.');
        }
        mainMetadata = main.toJSON();
      }
      await this.queueFiling('approve', {identity: row.id, digest: row.digest, filename: saved.filename, title: saved.title,
        document_type: saved.document_type, collection: saved.collection, research: saved.research || [], match: saved.match || '',
        ...(saved.citation_edits ? {citation: saved.citation_edits} : {}), ...(saved.tags?.length ? {tags: saved.tags} : {}),
        review_token: row.payload.review_token || '', main_reference: supplement ? saved.parent : '', main_reference_metadata: mainMetadata}, saved.title);
    }, true, 'refresh-cw');
    resume.dataset.queue = 'true';
  },
  renderSavedReviewUpdate(row, parent) {
    const failed = this.reviewUpgrade?.state === 'failed';
    const panel = this.el('section', '', parent, {class: 'review-saved-update', role: failed ? 'alert' : 'status'});
    this.el('h3', failed ? 'This saved review could not be updated' : 'Updating this saved review', panel);
    this.el('p', failed ? this.reviewUpgrade.error
      : 'Looking up the Zotero references and attachments behind the saved warning. Your filing choices are kept while the local guidance updates.', panel);
    if (failed) this.button('Retry saved review update', panel, () => this.prepareReviewGuidance({retry: true}), false, 'refresh-cw');
    const valid = key => /^[23456789ABCDEFGHIJKLMNPQRSTUVWXYZ]{8}$/.test(key || '');
    if (valid(row.payload.parent)) this.reviewNavigationButton('Open Zotero reference', panel, 'references', [row.payload.parent]);
    const attachment = row.payload.attachment || (row.kind === 'zotero' ? row.identity : '');
    if (valid(attachment)) this.reviewNavigationButton('Show attachment in Zotero', panel, 'attachment', [attachment]);
    if (row.payload.issues?.length) {
      const earlier = this.el('details', '', panel, {'data-review-detail': 'saved-warnings'});
      this.el('summary', 'Previously saved warnings', earlier);
      for (const issue of row.payload.issues) this.el('p', issue, earlier);
    }
  },
  reviewNavigationButton(label, parent, target, keys, context = {}) {
    const button = this.button(label, parent, async () => {
      let result;
      try { result = await notableNavigateReview({target, keys}); }
      catch (error) {
        if (context.conflict) throw Error(error.message + ' Use Sync now to refresh this device conflict before deciding.');
        throw error;
      }
      if (result.replacements?.length) {
        this.reviewStale = true;
        const message = this.view?.document.getElementById('notable-review-freshness');
        if (message) message.textContent = this.reviewFreshnessMessage();
      }
      if (!this.busy) this.message(result.replacements?.length
        ? (context.conflict ? 'Opened the surviving item after a Zotero merge. Use Sync now to refresh this device conflict; its sync links have not been changed.'
          : context.inbox ? 'Opened the surviving Zotero reference. Use Check Inbox to refresh this entry.'
            : 'Opened the surviving item after a Zotero merge. Recheck this review; its sync links have not been changed.')
        : 'Opened in Zotero. Your review decision is still pending.');
      if (context.inbox && result.replacements?.length) this.zoteroInboxDirty = true;
      await context.onOpened?.(result);
    }, false, target === 'pdf' ? 'book-open' : 'external-link');
    button.dataset.browse = 'true';
    return button;
  },
  renderConflictComparison(row, parent) {
    const table = this.el('table', '', parent, {class: 'review-comparison'});
    this.el('caption', 'Current names and folders', table);
    const head = this.el('tr', '', this.el('thead', '', table));
    for (const label of ['Field', 'Zotero', 'reMarkable']) this.el('th', label, head, {scope: 'col'});
    const body = this.el('tbody', '', table), homes = this.mappedDestinations();
    const location = (side, key) => {
      const home = homes.find(value => value.key === key);
      return home?.[side === 'desktop' ? 'zotero' : 'tablet'] || 'Location unavailable — check the connected folders';
    };
    for (const [label, index] of [['Name', 0], [row.payload.type === 'paper' ? 'Folder' : 'Parent folder', 1]]) {
      const line = this.el('tr', '', body); this.el('th', label, line, {scope: 'row'});
      for (const side of ['desktop', 'tablet']) {
        const value = row.payload[side]?.[index];
        this.el('td', index ? location(side, value) : value || 'Name unavailable', line);
      }
    }
  },
  reviewFindingExplanation(finding) {
    // Old queues retain the original explanation inside their consent token.
    // Correct only known authored copy at display time; preserve saved evidence,
    // paper titles, filenames, and approval tokens exactly as they were.
    const corrections = {
      readability_check: ['a reading, encryption or password problem', 'a reading, encryption, or password problem'],
      multiple_identical_counterparts: ['Their references, locations or annotation histories', 'Their references, locations, or annotation histories'],
      repeated_pdf_content: ['the references, notes or Zotero annotations', 'the references, notes, or Zotero annotations'],
      multiple_pdf_attachments: ['a valid main paper, supplement or manuscript arrangement', 'a valid main paper, supplement, or manuscript arrangement']
    };
    const explanation = String(finding.explanation || ''), correction = corrections[finding.code];
    return correction ? explanation.replace(...correction) : explanation;
  },
  reviewProblemDescriptor(row) {
    const findings = (row.payload.findings || []).filter(finding => !finding.acknowledgement?.acknowledged);
    const files = findings.flatMap(finding => finding.attachments || []);
    if (files.some(file => file.availability === 'trashed')) return 'A saved attachment is in Zotero Trash';
    if (files.some(file => file.availability === 'missing')) return 'A saved attachment is unavailable';
    const labels = {repeated_pdf_content: 'Matching PDF copies', possible_duplicate_references: 'Possible duplicate references',
      multiple_pdf_attachments: 'Several PDFs under one reference', local_file_unavailable: 'Local file unavailable',
      local_review_unavailable: 'Saved file could not be checked', readability_check: 'The PDF could not be read',
      external_linked_file: 'PDF stored outside Zotero', repair_required: 'Interrupted filing needs attention'};
    const repair = findings.find(finding => finding.severity === 'repair');
    return labels[repair?.code] || (repair ? 'File needs attention' : labels[findings[0]?.code]) || 'Library choice needed';
  },
  reviewDetailsLabel(row) {
    const findings = (row.payload.findings || []).filter(finding => !finding.acknowledgement?.acknowledged);
    if (findings.some(finding => finding.severity === 'repair')) return 'Why this needs attention';
    if (findings.some(finding => finding.code === 'possible_duplicate_references')) return 'Why these may be duplicates';
    if (findings.some(finding => finding.code === 'repeated_pdf_content')) return 'Why these PDFs match';
    return 'Why this needs attention';
  },
  reviewSeparateLabel(finding) {
    if (finding.code === 'possible_duplicate_references') return 'Keep these references separate';
    return (finding.attachments || []).length || /pdf/.test(finding.code) ? 'Keep these PDFs separate' : 'Keep these items separate';
  },
  reviewItemTypeLabel(type) {
    if (typeof type !== 'string' || !type.trim()) return 'Item type unavailable';
    try {
      const label = typeof Zotero !== 'undefined' && Zotero.ItemTypes?.getLocalizedString(type);
      if (typeof label === 'string' && label.trim() && label !== type) return label;
    } catch (_) {} // Older saved queues can include a type Zotero no longer knows.
    const label = type.replace(/([a-z0-9])([A-Z])/g, '$1 $2').replace(/[_-]+/g, ' ');
    return label.charAt(0).toUpperCase() + label.slice(1);
  },
  renderReviewFindings(row, parent, libraryCase = false) {
    const panel = this.el('section', '', parent, {class: 'review-findings', 'aria-label': 'Review guidance'});
    const references = new Map(), attachments = new Map(), collections = new Map();
    for (const finding of row.payload.findings) {
      for (const reference of finding.references || []) if (reference.key) references.set(reference.key, reference);
    }
    const libraryComparison = libraryCase || (references.size > 1 && row.payload.findings.every(finding => finding.domain === 'library'));
    this.el('h3', this.reviewProblemDescriptor(row), panel);
    const inspection = this.el('div', '', panel);
    const choices = this.el('div', '', panel, {class: 'review-decisions'});
    const participants = this.el('div', '', panel);
    const findingParent = this.el('details', '', panel, {'data-review-detail': 'finding-details'});
    this.el('summary', this.reviewDetailsLabel(row), findingParent);
    const eligible = row.payload.findings.filter(finding => finding.acknowledgement?.eligible);
    for (const finding of row.payload.findings) {
      const card = this.el('article', '', findingParent, {class: 'review-finding'});
      const acknowledgement = finding.acknowledgement;
      if (row.payload.findings.length > 1) this.el('h4', finding.summary, card);
      this.el('p', this.reviewFindingExplanation(finding), card);
      if (finding.evidence?.length) {
        const list = this.el('ul', '', card);
        for (const value of finding.evidence) this.el('li', value, list);
      }
      if (finding.severity === 'repair' && finding.next_step) this.el('p', finding.next_step, card);
      if (acknowledgement?.eligible && row.status === 'pending') {
        const choice = this.el('div', '', choices, {class: 'review-choice'});
        if (eligible.length > 1) this.el('strong', finding.code === 'possible_duplicate_references' ? 'Reference entries' : 'PDF copies', choice);
        this.button(acknowledgement.acknowledged ? 'Review this choice again' : this.reviewSeparateLabel(finding), choice,
          () => this.action('acknowledge_review_finding', {identity: row.id, digest: row.digest,
            code: finding.code, token: acknowledgement.token, review_token: row.payload.review_token,
            keep_separate: !acknowledgement.acknowledged}));
      }
      const related = this.relatedFindingReviews(row, finding).filter(other => !row.caseRows?.includes(other.id));
      if (related.length) {
        const group = this.el('details', '', card, {'data-review-detail': 'related:' + finding.code});
        this.el('summary', `This issue also appears on ${related.length} other PDF review${related.length === 1 ? '' : 's'}`, group);
        this.el('p', 'These reviews share the same saved evidence. Each PDF keeps its own filing choices.', group, {class: 'hint'});
        for (const other of related) {
          const jump = this.button('Review: ' + (other.payload.title || other.payload.filename || 'Related PDF'), group,
            () => { this.selected = this.tab === 'library' ? this.queueRows('library').find(item => item.caseRows.includes(other.id))?.id || other.id : other.id; this.showAllReviews = true; this.reviewFocus = true; this.render(); }, false, 'file-search');
          jump.dataset.browse = 'true';
        }
      }
      for (const reference of finding.references || []) if (reference.key) references.set(reference.key, reference);
      for (const attachment of finding.attachments || []) if (attachment.key) {
        const previous = attachments.get(attachment.key);
        attachments.set(attachment.key, {...attachment,
          availability: ['missing', 'trashed'].includes(previous?.availability) ? previous.availability : attachment.availability || previous?.availability,
          match_basis: previous && previous.match_basis !== 'context' ? previous.match_basis : attachment.match_basis});
      }
      for (const collection of finding.collections || []) if (collection.key) collections.set(collection.key, collection);
    }
    const pdfs = [...attachments.values()].filter(attachment => /\.pdf$/i.test(attachment.filename || '')
      && !['missing', 'trashed'].includes(attachment.availability));
    if (pdfs.length > 1) this.renderPDFComparison(inspection, pdfs);
    if (!libraryComparison && (references.size || attachments.size || collections.size)) this.el('h3', 'Inspect in Zotero', inspection);
    if (libraryCase && !references.size) this.button('Check my changes', inspection, () => this.action('recheck_review', {identities: row.caseRows || [row.id]}), true, 'refresh-cw');
    if (references.size) {
      const keys = [...references.keys()];
      const compare = this.reviewNavigationButton(keys.length > 1 ? 'Compare in Zotero' : 'Open Zotero reference', inspection, 'references', keys);
      if (libraryCase) this.button('Check my changes', inspection, () => this.action('recheck_review', {identities: row.caseRows || [row.id]}), false, 'refresh-cw');
      const types = [...references.values()].map(reference => reference.itemType);
      const knownTypes = types.filter(type => typeof type === 'string' && type.trim());
      const sameType = knownTypes.length === types.length && new Set(knownTypes).size === 1;
      if (libraryComparison && keys.length > 1) {
        compare.setAttribute('class', 'primary');
        let hint, help;
        if (this.reviewStale) {
          hint = 'Zotero changed. Use Check my changes before deciding whether to merge.';
          help = 'Select these references in Zotero to inspect their current details. Use Check my changes to refresh this case.';
        } else if (new Set(knownTypes).size > 1) {
          hint = 'Different item types. If these describe the same publication, correct Item Type in Zotero before merging.';
          help = 'Select these references in Zotero to compare their Item Type fields. Correct any mistaken type there; item types must match before merging.';
        } else if (!sameType) {
          hint = 'Item types are unavailable. Recheck Zotero before merging.';
          help = 'Select these references in Zotero to inspect their Item Type fields. Recheck Zotero to update the saved types.';
        } else {
          hint = 'In Zotero: right-click the selected references → Merge Items…';
          help = 'Select these references together in Zotero. If they describe the same publication, right-click and choose Merge Items.';
        }
        this.el('p', hint, inspection, {class: 'hint review-merge-hint'});
        this.setButtonHelp(compare, help);
      }
      const linked = [...references.values()].filter(reference => (this.latest.links || []).some(link => link.parent === reference.key));
      if (keys.length > 1 && linked.length) {
        const advice = this.el('section', '', participants, {class: 'review-merge-advice', role: 'note'});
        const mergeReady = sameType && !this.reviewStale;
        this.el('strong', mergeReady ? 'Before merging in Zotero' : 'reMarkable connection', advice);
        const ownerLinks = (this.latest.links || []).filter(link => link.parent === linked[0].key);
        if (!mergeReady) this.el('p', 'Keep the linked PDF and its notes while checking these references.', advice);
        else if (linked.length === 1 && ownerLinks.length === 1 && !ownerLinks[0].paused && !ownerLinks[0].unavailable) {
          this.el('p', 'Keep the reference marked “Linked to reMarkable” as the survivor when possible. Keep the linked PDF and its notes. Zotero can combine matching attachments during a merge.', advice);
        } else this.el('p', 'Check the existing tablet connections before merging. If both references are linked, keep them separate to preserve both connections. Keep the linked PDF and its notes.', advice);
      }
      const list = this.el('ul', '', participants, {class: 'review-participants'});
      for (const reference of references.values()) {
        const line = this.el('li', '', list);
        this.el('strong', String(reference.title || 'Reference title unavailable').replace(/<[^>]+>/g, ''), line);
        const metadata = this.el('p', '', line, {class: 'hint'});
        const type = this.reviewItemTypeLabel(reference.itemType);
        this.el('span', type === 'Item type unavailable' ? type : 'Item Type: ' + type, metadata, {class: 'review-item-type'});
        const citation = [reference.date, reference.DOI ? 'DOI: ' + reference.DOI : ''].filter(Boolean).join(' · ');
        if (citation) this.el('span', ' · ' + citation, metadata);
        const links = (this.latest.links || []).filter(link => link.parent === reference.key);
        if (links.length) this.el('p', 'Linked to reMarkable', line, {class: 'review-link-badge'});
        for (const link of links) {
          this.button('Check connection', line, () => this.previewLinkRepair(link.cloud));
          this.renderLinkRepair(line, link.cloud);
          if (link.paused || link.unavailable) this.el('p', link.paused ? 'This link is paused.' : 'This link needs repair.', line, {class: 'hint'});
          this.el('p', 'Tablet document: ' + (link.name || 'Name unavailable'), line, {class: 'hint'});
          const file = attachments.get(link.view_key || link.source_key);
          if (file) this.el('p', 'PDF used by this link: ' + (file.title || file.filename), line, {class: 'hint'});
        }
        if (keys.length > 1) this.reviewNavigationButton('Show this reference', line, 'references', [reference.key]);
      }
    }
    if (attachments.size) {
      const savedCount = [...attachments.values()].filter(file => ['missing', 'trashed'].includes(file.availability)).length;
      const currentCount = attachments.size - savedCount;
      const files = this.el('details', '', panel, {'data-review-detail': 'attachments'});
      this.el('summary', savedCount ? `Attachments: ${currentCount} current · ${savedCount} unavailable saved ${savedCount === 1 ? 'record' : 'records'}`
        : `Inspect ${currentCount} current attachment${currentCount === 1 ? '' : 's'}`, files);
      for (const attachment of attachments.values()) {
        const entry = this.el('section', '', files, {class: 'review-attachment'});
        this.el('strong', attachment.filename || attachment.title || 'Attachment name unavailable', entry);
        if (['missing', 'trashed'].includes(attachment.availability)) {
          this.el('p', attachment.availability === 'trashed' ? 'In Zotero Trash · saved review record' : 'Unavailable in Zotero · saved review record', entry, {class: 'hint'});
          continue;
        }
        if ((this.latest.links || []).some(link => [link.source_key, link.view_key].includes(attachment.key))) this.el('p', 'Used by a reMarkable link', entry, {class: 'review-link-badge'});
        if (attachment.title && attachment.title !== attachment.filename) this.el('p', 'Attachment label: ' + attachment.title, entry, {class: 'hint'});
        if (attachment.match_basis === 'original_baseline') this.el('p', 'The retained original matches; current annotations or PDF bytes may differ.', entry, {class: 'hint'});
        else if (attachment.match_basis === 'current_bytes') this.el('p', 'Current file bytes match. Zotero annotation records still belong to their respective attachments.', entry, {class: 'hint'});
        this.reviewNavigationButton('Show attachment in Zotero', entry, 'attachment', [attachment.key]);
        if (/\.pdf$/i.test(attachment.filename || '')) this.reviewNavigationButton('Open PDF in Zotero', entry, 'pdf', [attachment.key]);
      }
    }
    for (const collection of collections.values()) {
      const paths = new Map(this.paths(this.latest.collections || [], 'key', 'parentCollection', 'name'));
      this.el('p', paths.get(collection.key) || collection.name || 'Linked Zotero collection', panel);
      this.reviewNavigationButton('Show Zotero collection', panel, 'collection', [collection.key]);
    }
  },
  relatedFindingReviews(row, finding) {
    // Only an evidence fingerprint can join reviews. Titles and overlap alone
    // never create an identity, and no approval scope is combined here.
    const token = finding.acknowledgement?.token;
    if (!token) return [];
    return (this.latest.reviews || []).filter(other => other.id !== row.id && other.status === 'pending'
      && (other.payload.findings || []).some(value => value.code === finding.code && value.acknowledgement?.token === token));
  },
  reviewIssueHelp(issue) {
    if (issue.includes('Identical content belongs')) return 'The exact same file bytes occur in more than one Zotero attachment. Open the references in Zotero and compare their parent papers, notes, and versions. Merge true duplicate references using Zotero Duplicate Items, or retain intentional copies. Do not delete annotated copies just to clear this message. Then return and use Sync now; approve only the attachment you intend to link.';
    if (issue.includes('same work') || issue.includes('same DOI')) return 'Another reference matches this paper by DOI or title. This is a possible duplicate, not proof. Compare the references in Zotero: merge true duplicate references, or keep different editions and choose the intended reference before approving.';
    if (issue.includes('same name but different content')) return 'The filenames match but the PDF bytes differ. Open both PDFs and compare editions and annotations. Keep them separate if they are different versions; do not link them based on name alone.';
    if (issue.includes('identical counterpart')) return 'Several byte-identical copies are available. Use the identical PDF selector below to choose the copy that should be linked. Check its location before approving.';
    if (issue.includes('multiple PDFs')) return 'This Zotero reference contains more than one PDF, which may be a main paper, supplement, or another edition. Open this file and set Document type to Supporting Information if appropriate; choose its main reference, or approve the intended main PDF.';
    if (issue.includes('supporting information')) return 'Confirm that this is a supplement. Choose Supporting Information under Document type and select its main Zotero reference before approving.';
    if (issue.includes('several tablet destinations')) return 'The reference belongs to several collections connected to tablet folders. Choose one destination in Linked folders below. Other research collection memberships can remain.';
    if (issue.includes('external Windows file')) return 'This is a linked file outside Zotero storage. In Zotero, use Tools → Manage Attachments → Convert Linked Files to Stored Files, then use Sync now. Keep your original file until the stored copy is verified.';
    if (issue.includes('readable name')) return 'Enter a useful Filename below. You can use the paper title; complete author, journal, and year fields are optional.';
    if (issue.includes('unavailable')) return 'Locate or restore the original file in Zotero or the intake folder, then use Sync now. Approving cannot recover a missing file.';
    if (issue.includes('readability')) return 'Open the PDF to check that it is readable and unlocked. Supply an accessible, unencrypted copy if necessary, then use Sync now. Image-only PDFs can still be filed.';
    return 'Open the paper and check the condition described above. Correct its source or filing choices, then use Sync now to refresh. If the issue persists, retain the file and report the exact message.';
  },
  async openReviewPaper(row) {
    const path = row.payload?.path || '';
    if (!path) {
      const link = (this.latest.links || []).find(link => link.cloud === row.payload?.cloud);
      const item = (link?.view_key || link?.source_key) && await notableItem(link.view_key || link.source_key);
      if (!item?.isPDFAttachment()) throw new Error('No readable paper is available for this review.');
      await Zotero.Reader.open(item.id); return;
    }
    if (!/\.(pdf|docx|pptx)$/i.test(path) || !PathUtils.isAbsolute(path)) throw new Error('No readable paper is available for this review.');
    const file = Zotero.File.pathToFile(path);
    if (!file.exists() || !file.isFile()) throw new Error('This saved paper is unavailable. Refresh its review before opening it.');
    // Use Zotero's existing local-file launcher; opening never approves,
    // imports, renames, or transfers this document.
    Zotero.launchFile(file);
  },
  paperTagSource(scope) {
    const row = scope.identity ? (this.latest?.reviews || []).find(row => row.id === scope.identity) : null;
    const link = scope.key ? (this.latest?.links || []).find(link => [link.source_key, link.view_key].includes(scope.key)) : null;
    return JSON.stringify([this.filingConnection(), scope.identity || scope.key, row?.digest || link?.revision || '', row?.payload?.path || '']);
  },
  paperTagEntry(scope) {
    this.paperTagCache ||= new Map(); this.paperTagDrafts ||= new Map();
    const source = this.paperTagSource(scope), key = source + ':' + (this.paperTagEvidenceSerial || 0);
    if (!this.paperTagCache.has(key)) {
      const draft = this.paperTagDrafts.get(source) || {selected: new Set(), dirty: false, open: false};
      this.paperTagDrafts.set(source, draft);
      this.paperTagCache.set(key, {key, source, scope, state: 'waiting', listeners: new Set(), outputs: new Set(), draft});
      if (this.paperTagCache.size > 128) this.paperTagCache.delete(this.paperTagCache.keys().next().value);
    }
    return this.paperTagCache.get(key);
  },
  schedulePaperTags() {
    clearTimeout(this.paperTagTimer);
    if (this.stopped || !this.view || this.view.closed || ![...(this.paperTagCache?.values() || [])].some(entry => entry.state === 'waiting'
      && [...entry.outputs].some(output => output.isConnected !== false))) return;
    this.paperTagTimer = setTimeout(() => this.preparePaperTags(), 250);
  },
  async preparePaperTags() {
    if (this.stopped || !this.view || this.view.closed) return;
    if (this.busy || this.inboxChecking || this.paperTagChecking || this.drainingFilings || this.pausing || this.changingFolder) {
      this.schedulePaperTags(); return;
    }
    const entry = [...(this.paperTagCache?.values() || [])].find(entry => entry.state === 'waiting'
      && [...entry.outputs].some(output => output.isConnected !== false));
    if (!entry) return;
    const notify = () => {for (const listener of [...entry.listeners]) listener();};
    entry.state = 'loading'; this.paperTagChecking = true; notify();
    try {
      this.paperTagRequest = this.rpc('suggest_paper_tags', {...entry.scope, quiet: true});
      const result = await this.paperTagRequest;
      if (this.stopped) return;
      if (!result?.proposal) throw Error('The paper suggestions were not returned.');
      entry.proposal = result.proposal; entry.state = 'ready';
      if (entry.draft.token && entry.draft.token !== result.proposal.token) entry.draft.saved = false;
      entry.draft.token = result.proposal.token;
      if (!entry.draft.dirty) entry.draft.selected = new Set(result.proposal.selected || []);
    } catch (error) {entry.state = 'failed'; entry.error = error.message;}
    finally {this.paperTagChecking = false; notify(); this.schedulePaperTags();}
  },
  paperTagPanel(parent, scope) {
    const entry = this.paperTagEntry(scope), draft = entry.draft;
    for (const listener of [...entry.listeners]) if (listener.output?.isConnected === false) entry.listeners.delete(listener);
    for (const output of [...entry.outputs]) if (output.isConnected === false) entry.outputs.delete(output);
    const panel = this.el('details', '', parent, {class: 'paper-tag-proposal'});
    panel.open = draft.open;
    panel.addEventListener('toggle', () => {draft.open = panel.open;});
    this.el('summary', 'Paper keywords and suggested tags', panel);
    this.el('p', 'Keywords and topic suggestions are prepared automatically. Review them before adding tags.', panel);
    const output = this.el('div', '', panel);
    entry.outputs.add(output);
    const render = () => {
      if (output.isConnected === false) {entry.listeners.delete(render); entry.outputs.delete(output); return;}
      output.replaceChildren();
      if (entry.state === 'waiting' || entry.state === 'loading') {
        this.el('p', 'Preparing local suggestions… You can keep reviewing this paper.', output, {class: 'hint', role: 'status'}); return;
      }
      if (entry.state === 'failed') {
        this.el('p', 'Could not prepare suggestions: ' + entry.error, output, {class: 'message error', role: 'status'});
        this.button('Retry tag suggestions', output, () => {entry.state = 'waiting'; render(); this.schedulePaperTags();}); return;
      }
      const proposal = entry.proposal;
      this.el('h3', 'Stated keywords', output);
      if (!proposal.keywords.length) this.el('p', 'No reliable keyword list found. Existing tags will be kept.', output);
      for (const row of proposal.keywords) this.el('p', `${row.tag} — ${row.source}`, output);
      if (proposal.checked_at) this.el('p', 'Keyword evidence checked ' + new Date(proposal.checked_at).toLocaleString(), output, {class: 'hint'});
      this.el('h3', 'Suggested tags — choose any you want', output);
      const options = proposal.suggestions.map(row => {
        const input = this.checkbox(row.tag, output, draft.selected.has(row.tag));
        input.addEventListener('change', () => {
          draft.dirty = true; draft.saved = false; save.disabled = false;
          input.checked ? draft.selected.add(row.tag) : draft.selected.delete(row.tag);
        });
        if (row.reason) this.el('p', row.reason, output, {class: 'hint'});
        this.el('p', row.source + ' · ' + row.evidence, output, {class: 'hint'});
        return [row.tag, input];
      });
      this.el('p', proposal.notice, output, {class: 'hint'});
      const save = this.button(scope.identity ? 'Use these tags when filing' : 'Add keywords and selected tags', output, async () => {
        try {
          const result = await this.action('save_paper_tags', {target: proposal.target, token: proposal.token,
            selected: options.filter(([, input]) => input.checked).map(([tag]) => tag)}, false);
          if (result?.saved) {draft.saved = true; save.disabled = true; this.el('p', result.deferred_until_filing ? 'Tag choices saved. They will be added when this paper is filed.' : 'Paper tags saved. Linked tablet copies receive them on the next sync.', output, {role: 'status'});}
        } catch (error) {entry.state = 'failed'; entry.error = error.message; draft.saved = false; render();}
      }, true);
      save.disabled = !!draft.saved;
    };
    render.output = output;
    entry.listeners.add(render); render(); this.schedulePaperTags();
  },
  reviewOpenButton(row, parent) {
    const link = (this.latest.links || []).find(link => link.cloud === row.payload?.cloud);
    if (!row.payload?.path && !link?.view_key && !link?.source_key) return;
    const button = this.button('Open paper', parent, () => this.openReviewPaper(row));
    button.dataset.browse = 'true';
    this.setButtonHelp(button, row.kind === 'cloud'
      ? 'Open the downloaded original PDF. Tablet handwriting is added to Zotero when this paper syncs.'
      : 'Open this saved file or linked Zotero PDF. Your review stays pending.');
    if (row.kind === 'cloud') this.el('p', 'Shows the original PDF; handwriting arrives with sync.', parent, {class: 'hint'});
  },
  renderImport(content) {
    this.el('h2', 'Import library', content);
    this.el('p', 'Scan first, then import routine files together. Exact copies are grouped, existing Zotero files are reused, and your original Windows folders stay in place.', content);
    if (!(this.latest.collections || []).length) {
      this.el('p', 'Connect Zotero, then create a collection for these imports. Tablet pairing can wait.', content);
      this.button('Connect Zotero', content, () => this.action('connect'), true);
      this.button('New collection…', content, async () => {
        const created = await this.newFolderDialog('zotero', '');
        if (created) { this.intakeCollection = created.key; this.render(); }
      }); return;
    }
    const plan = this.latest.bulk;
    const results = content;
    content = this.el('section', '', results, {class: 'card'});
    const path = this.field('Literature folder, including subfolders', content, this.intakeRoot || plan?.root || '');
    this.button('Choose literature folder…', content, async () => {
      const {FilePicker} = ChromeUtils.importESModule('chrome://zotero/content/modules/filePicker.mjs');
      const picker = new FilePicker(); picker.init(this.view, 'Choose your existing literature folder', picker.modeGetFolder);
      if (await picker.show() === picker.returnOK) { path.value = this.intakeRoot = picker.file; path.notableRefreshValue(); invalidate(); }
    });
    const collection = this.folderSelect('Zotero collection for imports', content,
      this.paths(this.latest.collections, 'key', 'parentCollection', 'name'), this.intakeCollection || plan?.collection || this.latest.collections[0].key);
    collection.dataset.zoteroCollections = 'true';
    const lookup = this.checkbox('Retrieve citations using title-verified DOI metadata', content, this.intakeLookup ?? plan?.lookup !== false);
    const scanSettings = () => ({root: path.value, collection: collection.value, lookup: lookup.checked});
    const sameSettings = () => !!plan && path.value.toLowerCase().replace(/[\\/]+$/, '') === plan.root.toLowerCase().replace(/[\\/]+$/, '')
      && collection.value === plan.collection && lookup.checked === (plan.lookup !== false);
    let importButton;
    const planActions = [];
    const notice = this.el('p', '', content, {role: 'status', class: 'hint'});
    const invalidate = () => {
      this.intakeRoot = path.value; this.intakeCollection = collection.value; this.intakeLookup = lookup.checked;
      const stale = !!plan && !sameSettings();
      notice.textContent = stale ? 'These choices changed after scanning. Scan again before importing.' : '';
      for (const button of [importButton, ...planActions].filter(Boolean)) { button.dataset.invalid = String(stale); button.disabled = stale || this.busy; }
    };
    for (const node of [path, collection, lookup]) { node.addEventListener('input', invalidate); node.addEventListener('change', invalidate); }
    this.el('p', 'DOI metadata is used only when its title matches this document’s own title. If no citation can be verified, the file keeps a local document reference and a warning to review its citation in Zotero. This route does not send PDF text to Zotero’s recognition service.', content, {class: 'hint'});
    this.button('Scan folder — no library changes', content, async () => {
      this.intakeRoot = path.value; this.intakeCollection = collection.value;
      this.intakeLookup = lookup.checked;
      await this.action('scan_library', scanSettings(), false);
      this.intakeRoot = this.latest.bulk?.root || path.value; this.render();
    }, true);
    content = results;
    if (!plan) return;
    const rows = plan.rows, ready = rows.filter(row => !row.issues.length && !['complete', 'dismissed'].includes(row.status));
    const complete = rows.filter(row => row.status === 'complete').length;
    const copies = rows.reduce((n, row) => n + row.copies.length, 0);
    const summary = this.el('section', '', content, {class: 'card'});
    this.el('h2', 'Folder scan', summary);
    this.el('p', `${copies} files · ${rows.length} unique contents · ${copies - rows.length} extra exact copies grouped · ${complete} completed`, summary);
    this.el('p', `${ready.length} routine files ready; ${rows.filter(row => row.issues.length && row.status !== 'dismissed').length + plan.failures.length} cases need attention.`, summary);
    this.el('p', `${rows.filter(row => row.existing).length} unique contents already exist in Zotero and will be reused. This import does not rename or move your Windows files.`, summary);
    if (ready.length) importButton = this.button(`Import / reuse ${ready.length} routine files`, summary, () => {
      if (!sameSettings()) throw Error('Scan again to apply the visible folder, collection, and citation-lookup choices.');
      return this.action('import_library', {plan_id: plan.id, ...scanSettings()});
    }, true);
    invalidate();
    if (complete) {
      this.el('p', 'Open Inbox to review the imported papers and choose which to send to reMarkable.', summary);
      this.button('Open Inbox', summary, () => this.openTab('inbox'), false, 'folder-input').dataset.browse = 'true';
    }
    this.el('h2', 'Import exceptions', content);
    const exceptions = [];
    this.quickSearch(content, {label: 'Filter import exceptions', value: this.importFilter || '',
      change: value => { this.importFilter = value; applyFilter(); }});
    const count = this.el('p', '', content, {class: 'paper-search-count', role: 'status'});
    const empty = this.el('p', '', content);
    for (const row of rows.filter(row => row.issues.length && row.status !== 'dismissed')) {
      const card = this.el('section', '', content, {class: 'card decision'});
      exceptions.push([card, [row.path, ...row.issues].join(' ')]);
      this.el('strong', row.path, card);
      for (const issue of row.issues) this.el('p', issue, card);
      const key = row.parent || row.existing || row.candidates?.[0]?.key;
      if (key) this.button('Show matching reference in Zotero', card, async () => {
        const item = await notableItem(key); if (item) await Zotero.getActiveZoteroPane().selectItem(item.id);
      });
      planActions.push(this.button('Retry import for this file', card, () => {
        if (!sameSettings()) throw Error('Scan again to apply the visible folder, collection, and citation-lookup choices.');
        return this.action('retry_bulk', {plan_id: plan.id, row_id: row.id, ...scanSettings()});
      }));
      this.button('Dismiss this exception', card, () => this.action('retry_bulk', {plan_id: plan.id, row_id: row.id, dismiss: true}));
    }
    for (const failure of plan.failures) {
      const message = failure.path + ' · ' + failure.reason;
      const card = this.el('p', message, content, {class: 'message error'});
      exceptions.push([card, message]);
    }
    const applyFilter = () => {
      const query = this.searchTerms(this.importFilter || '');
      let shown = 0;
      for (const [card, text] of exceptions) {
        card.hidden = !this.matchesSearchTerms(text, query);
        if (!card.hidden) shown++;
      }
      count.textContent = `${shown} of ${exceptions.length} import exceptions`;
      empty.textContent = query.length ? 'No import exceptions match this search.' : 'No import exceptions.';
      empty.hidden = shown > 0;
    };
    applyFilter();
    invalidate();
  },
  async previewLinkRepair(identity) {
    const result = await this.action('preview_link_repair', {identity}, false);
    if (result) { this.linkRepair = result.link_repair; this.render(); }
  },
  renderLinkRepair(parent, identity) {
    const preview = this.linkRepair;
    if (!preview || preview.identity !== identity) return;
    const card = this.el('section', '', parent, {class: 'card'});
    this.el('h3', 'Reconnect to the surviving Zotero reference?', card);
    this.el('p', preview.title, card);
    this.el('p', 'The connection will use the verified Zotero PDF and keep the same tablet copy and notes.' + (preview.paused ? ' The connection will stay paused.' : ''), card, {class: 'hint'});
    this.reviewNavigationButton('Open surviving reference', card, 'references', [preview.parent]);
    this.button('Reconnect surviving reference', card, async () => {
      await this.action('repair_link', {identity, token: preview.token}, false);
      this.linkRepair = null; this.render();
    }, true);
    this.button('Cancel repair', card, () => { this.linkRepair = null; this.render(); });
  },
  paperSearchState() {
    return this.paperSearch ||= {view: 'linked', mode: 'citation', query: '', scope: 'all', status: '', reading: 'all', limit: 25};
  },
  searchText(value) {
    return value.normalize('NFD').replace(/\p{M}/gu, '').replace(/[‘’]/g, "'")
      .replace(/[“”]/g, '"').toLocaleLowerCase();
  },
  searchTerms(value) {
    const text = this.searchText(value);
    return (text.match(/"[^"]*"|[^\s"]+/g) || []).map(term => term.replace(/^"|"$/g, '')).filter(Boolean);
  },
  matchesSearchTerms(value, terms) {
    const text = this.searchText(value);
    return terms.every(term => text.includes(term));
  },
  chooserSearch(parent, label, change) {
    const {input} = this.quickSearch(parent, {label, change});
    return {input, terms: value => this.searchTerms(value), matches: (value, terms) => this.matchesSearchTerms(value, terms)};
  },
  async zoteroPaperMatches(query, mode) {
    const search = new Zotero.Search();
    search.libraryID = Zotero.Libraries.userLibraryID;
    search.addCondition('quicksearch-' + {citation: 'titleCreatorYear', all: 'fields', everything: 'everything'}[mode],
      'contains', query);
    const ids = await search.search();
    const keys = new Set();
    for (const item of await Zotero.Items.getAsync(ids || [])) {
      if (notableReviewItemInTrash(item)) continue;
      keys.add(item.key);
      // Quick Search can match a note or annotation under a paper.
      let child = item;
      while (child?.parentID) {
        child = Zotero.Items.get(child.parentID);
        if (child && !notableReviewItemInTrash(child)) keys.add(child.key);
      }
    }
    return keys;
  },
  async loadNameReview() {
    if ((this.busy && this.operation?.method !== 'sync') || this.nameReviewLoading) return;
    const state = this.paperSearchState();
    const connection = this.filingConnection();
    this.nameReviewLoading = true;
    this.nameReviewError = '';
    const render = () => { if (this.tab === 'links' && this.view?.document) this.render(); };
    render();
    try {
      const evidence = this.paperTagEvidenceSerial;
      const names = await this.maintenanceCall('names');
      if (!names || connection !== this.filingConnection()) return;
      state.names = names;
      state.evidence = evidence;
      state.renames = [];
      if (!this.latest.links?.length) return;
      const renames = await this.action('preview_filename_cleanup', {}, false);
      if (renames && connection === this.filingConnection()) state.renames = renames.papers;
    } catch (error) {
      this.nameReviewError = 'Couldn’t refresh name review. ' + error.message;
    } finally {
      this.nameReviewLoading = false;
      render();
    }
  },
  async paperSearchMetadata(keys) {
    const needed = new Set(keys.filter(Boolean));
    if (!needed.size || typeof Zotero === 'undefined' || !Zotero.Items?.getAll) return new Map();
    const signature = JSON.stringify([this.paperTagEvidenceSerial, [...needed].sort()]);
    if (this.paperMetadata?.signature === signature) return this.paperMetadata.promise;
    const promise = (async () => {
      const items = (await Zotero.Items.getAll(Zotero.Libraries.userLibraryID, false, false)).filter(item =>
        !item.deleted && (needed.has(item.key) || (item.isFileAttachment() && needed.has(item.parentKey))));
      await Zotero.Items.loadDataTypes(items);
      const records = new Map([...needed].map(key => [key, {files: []}]));
      for (const item of items) {
        if (notableReviewItemInTrash(item)) continue;
        if (item.isFileAttachment()) {
          const names = [item.attachmentFilename, item.getField('title')].filter(Boolean);
          records.get(item.parentKey)?.files.push(...names);
          records.get(item.key)?.files.push(...names);
        } else if (item.isRegularItem()) {
          const record = records.get(item.key);
          record.title = item.getField('title');
          record.read = item.getTags().some(tag => tag.tag === 'Read');
        }
      }
      return records;
    })();
    this.paperMetadata = {signature, promise};
    try { return await promise; }
    catch (error) {
      if (this.paperMetadata?.promise === promise) this.paperMetadata = null;
      throw error;
    }
  },
  searchSelect(parent, label, choices, value) {
    const select = this.el('select', '', parent, {'aria-label': label, title: label});
    select.dataset.browse = 'true';
    select.dataset.notableField = label;
    for (const [key, name] of choices) this.el('option', name, select, {value: key});
    select.value = value;
    return select;
  },
  quickSearch(content, {label, value = '', change = () => {}, submit, modes, mode, onMode, id}) {
    const toolbar = this.el('div', '', content, {class: 'paper-search-toolbar', role: 'search', 'aria-label': label + ' controls'});
    const box = this.el('div', '', toolbar, {class: 'paper-search-box'});
    const modeBox = this.el(modes ? 'label' : 'span', '', box, {class: modes ? 'paper-search-mode' : 'paper-search-icon'});
    this.el('span', '', modeBox, {class: 'button-icon icon-search', 'aria-hidden': 'true'});
    if (modes) {
      const select = this.searchSelect(modeBox, 'Search fields', modes, mode);
      select.addEventListener('change', () => onMode(select.value));
    }
    const input = this.el('input', '', box, {type: 'text', role: 'searchbox', placeholder: label, 'aria-label': label, autocomplete: 'off'});
    if (id) input.id = id;
    input.value = value;
    input.dataset.quickSearch = 'true';
    input.dataset.notableField = modes ? 'Paper search' : label;
    input.dataset.browse = 'true';
    const clear = this.el('button', '', box, {type: 'button', 'aria-label': 'Clear search', title: 'Clear search'});
    clear.dataset.browse = 'true';
    this.el('span', '', clear, {class: 'button-icon icon-x', 'aria-hidden': 'true'});
    const apply = event => { clear.hidden = !input.value; if (!event?.isComposing) change(input.value); };
    const reset = () => { input.value = ''; input.focus(); apply(); };
    clear.hidden = !input.value;
    clear.addEventListener('click', reset);
    input.addEventListener('input', apply);
    input.addEventListener('compositionend', apply);
    input.addEventListener('keydown', event => {
      if (event.isComposing || event.defaultPrevented) return;
      if (event.key === 'Escape' && input.value) { event.preventDefault(); event.stopPropagation(); reset(); }
      if (event.key === 'Enter' && submit) { event.preventDefault(); submit(); }
    });
    return {toolbar, input};
  },
  renderPaperSearch(content, state, reviewing, update, switchView) {
    const views = this.el('div', '', content, {class: 'actions paper-search-views', role: 'group', 'aria-label': 'Paper view'});
    for (const [value, label] of [['linked', 'Browse links'], ['review', 'Review paper names']]) {
      const button = this.button(label, views, () => switchView(value));
      button.setAttribute('aria-pressed', String(state.view === value));
      button.dataset.browse = 'true';
    }
    const apply = () => { state.limit = 25; update(); };
    const applyQuery = () => { state.appliedQuery = state.query; state.waitingForEnter = false; apply(); };
    const {toolbar, input} = this.quickSearch(content, {
      label: reviewing ? 'Search names to review' : 'Search linked papers', id: 'notable-paper-search',
      value: state.query, change: value => {
        state.query = value;
        state.waitingForEnter = state.mode !== 'files' && value.includes('"');
        if (state.waitingForEnter) apply(); else applyQuery();
      },
      submit: applyQuery,
      modes: [['citation', 'Title, Year, Creator'], ['all', 'All Fields & Tags'], ['everything', 'Everything'], ['files', 'Filenames']],
      mode: state.mode, onMode: value => {
        state.mode = value;
        state.waitingForEnter = false;
        applyQuery();
      },
    });
    input.setAttribute('aria-describedby', 'notable-paper-search-status');
    if (reviewing) {
      const scope = this.searchSelect(toolbar, 'Paper scope', [['all', 'All Zotero papers'], ['linked', 'Linked papers']], state.scope);
      const status = this.searchSelect(toolbar, 'Name review status', [['', 'All name issues'], ['needs', 'Needs review'],
        ['available', 'Rename available'], ['queued', 'Rename queued'], ['saved', 'Saved for review']], state.status);
      scope.addEventListener('change', () => { state.scope = scope.value; apply(); });
      status.addEventListener('change', () => { state.status = status.value; apply(); });
    } else {
      const status = this.searchSelect(toolbar, 'Reading status', [['all', 'All reading statuses'], ['read', 'Read'], ['unread', 'Unread']], state.reading);
      status.addEventListener('change', () => { state.reading = status.value; apply(); });
    }
  },
  renderRenamePreview(card, proposal, state, render) {
    const details = this.el('details', '', card);
    this.el('summary', 'Preview rename', details);
    const table = this.el('table', '', details, {class: 'review-comparison', 'aria-label': 'Proposed filename changes'});
    const header = this.el('tr', '', this.el('thead', '', table));
    for (const title of ['Copy', 'Current name', 'Proposed name']) this.el('th', title, header, {scope: 'col'});
    const body = this.el('tbody', '', table);
    const changes = proposal.files.filter(file => file.before !== file.filename)
      .map(file => ['Zotero PDF', file.before, file.filename]);
    if (proposal.tablet !== proposal.target) changes.push(['reMarkable', proposal.tablet + '.pdf', proposal.target + '.pdf']);
    for (const change of changes) {
      const line = this.el('tr', '', body);
      for (const value of change) this.el('td', value, line);
    }
    this.el('p', proposal.basis, details, {class: 'hint'});
    this.button('Queue rename', details, async () => {
      const {identity, token} = proposal;
      const result = await this.action('queue_filename_cleanup', {selected: [{identity, token}]}, false);
      if (!result) return;
      state.renames = state.renames.filter(row => row.identity !== identity);
      state.notice = result.queued.length ? 'Rename queued for the next sync.' : 'This filename changed. Refresh name review to check it again.';
      if (result.queued.length) this.schedule(1000);
      render();
    });
  },
  renderLinks(content) {
    const state = this.paperSearchState(), reviewing = state.view === 'review';
    const links = this.latest.links || [];
    const activeLinks = links.filter(row => !row.unavailable);
    const received = activeLinks.filter(row => row.revision).length;
    this.el('h2', 'Linked papers', content);
    const render = () => { if (this.tab === 'links' && this.view?.document) this.render(); };
    let update = () => {};
    this.renderPaperSearch(content, state, reviewing, () => update(), value => {
      if (value === state.view) return;
      state.view = value; state.limit = 25;
      if (value === 'review' && (!state.names || state.evidence !== this.paperTagEvidenceSerial)) this.loadNameReview();
      render();
    });
    const renames = new Map((state.renames || []).map(row => [row.identity, row]));
    const byPaper = new Map();
    for (const row of links) {
      const key = row.parent || row.source_key || row.view_key;
      if (!byPaper.has(key)) byPaper.set(key, []);
      byPaper.get(key).push(row);
    }
    const matches = new Map((state.names?.matches || []).map(row => [row.key, row]));
    for (const row of state.names?.queued || []) {
      if (!matches.has(row.key)) matches.set(row.key, {...row, queued: true});
    }
    for (const [key, rows] of byPaper) {
      if (!key || matches.has(key) || !rows.some(row => row.filename_cleanup || renames.has(row.cloud))) continue;
      matches.set(key, {key, title: rows[0].name, findings: [], reference: !!rows[0].parent});
    }
    const rows = reviewing ? [...matches.values()] : links;
    if (reviewing) {
      this.button('Refresh name review', content, () => this.loadNameReview(), false, 'refresh-cw');
      if (this.nameReviewLoading) this.el('p', 'Checking Zotero titles and filenames…', content, {role: 'status'});
      else if (this.nameReviewError) this.el('p', this.nameReviewError, content, {role: 'status'});
      else if (!state.names) this.el('p', 'Refresh name review to find papers with “Unknown” and load saved decisions.', content);
      else if (state.evidence !== this.paperTagEvidenceSerial) this.el('p', 'Zotero changed. Refresh name review for current results.', content);
      if (state.notice) this.el('p', state.notice, content, {role: 'status'});
    }
    const count = this.el('p', '', content, {id: 'notable-paper-search-status', role: 'status', class: 'paper-search-count'});
    if (!reviewing) this.el('p', `${activeLinks.length} connected papers · ${received} tablet copies received · ${links.length - activeLinks.length} unavailable`, content, {class: 'hint'});
    const results = this.el('div', '', content);
    let metadata = new Map();
    let metadataLoading = false;
    let metadataError = false;
    let nativeSearch = null;
    let searchTimer;
    const saveReview = async params => {
      const value = await this.maintenanceCall('names', params);
      if (value) { state.names = value; render(); }
    };
    update = () => {
      const query = state.appliedQuery || '';
      const words = this.searchTerms(query);
      const native = words.length && state.mode !== 'files';
      const signature = JSON.stringify([state.mode, query]);
      if (native && nativeSearch?.signature !== signature) {
        clearTimeout(searchTimer);
        const request = nativeSearch = {signature, loading: true};
        const {mode} = state;
        searchTimer = setTimeout(async () => {
          if (!results.isConnected) return;
          try { request.keys = await this.zoteroPaperMatches(query, mode); }
          catch (error) { request.error = error.message; }
          finally { request.loading = false; if (results.isConnected && request === nativeSearch) update(); }
        }, 140);
      } else if (!native && nativeSearch) {
        clearTimeout(searchTimer);
        nativeSearch = null;
      }
      const visible = rows.filter(row => {
        const key = reviewing ? row.key : row.parent || row.source_key || row.view_key;
        const linked = byPaper.get(key) || [];
        if (reviewing) {
          if (state.scope === 'linked' && !linked.length) return false;
          if (state.status === 'needs' && (!row.findings.length || row.status === 'no_longer_matches')) return false;
          if (state.status === 'available' && !linked.some(link => renames.has(link.cloud))) return false;
          if (state.status === 'queued' && !linked.some(link => link.filename_cleanup)) return false;
          if (state.status === 'saved' && !row.queued) return false;
        }
        const record = metadata.get(key);
        if (!reviewing && state.reading !== 'all' && record?.read !== (state.reading === 'read')) return false;
        if (!words.length) return true;
        const findings = row.findings || [];
        if (state.mode === 'files') {
          const files = [...(record?.files || []), ...(reviewing ? linked.map(link => link.name) : [row.name]),
            ...findings.filter(finding => finding.where !== 'Zotero title').map(finding => finding.name)];
          return this.matchesSearchTerms(files.filter(Boolean).join('\n'), words);
        }
        if (nativeSearch?.keys?.has(key)) return true;
        if (reviewing && state.mode === 'citation') return this.matchesSearchTerms(findings
          .filter(finding => finding.where === 'Zotero title').map(finding => finding.name).join('\n'), words);
        return !record?.title && state.mode === 'citation' && this.matchesSearchTerms(row.title || row.name || '', words);
      });
      const searching = (native && nativeSearch.loading)
        || (metadataLoading && ((words.length && state.mode === 'files') || (!reviewing && state.reading !== 'all')));
      count.textContent = state.waitingForEnter ? 'Press Enter to search for the quoted phrase.'
        : searching ? 'Searching Zotero…' : nativeSearch?.error ? 'Couldn’t search Zotero. ' + nativeSearch.error
        : `${visible.length} of ${rows.length} ${reviewing ? 'papers to review' : 'linked papers'}`
          + (metadataError ? ' · Zotero metadata could not be loaded' : '');
      results.replaceChildren();
      if (nativeSearch?.error) {
        this.button('Retry search', results, () => { nativeSearch = null; update(); }).dataset.browse = 'true';
        return;
      }
      if (searching) return;
      const remaining = reviewing ? visible.filter(row => row.token && !row.queued) : [];
      if (remaining.length > 1) this.button(`Queue ${remaining.length} matching papers for review`, results,
        () => saveReview({selected: remaining.map(({key, token}) => ({key, token}))}));
      if (!visible.length && !metadataLoading && !this.nameReviewLoading && (state.names || !reviewing) && !state.waitingForEnter) {
        this.el('p', query.trim() || (reviewing ? state.status || state.scope === 'linked' : state.reading !== 'all') ? 'No papers match this search.'
          : reviewing ? 'No names need review.' : 'No paper connections yet. Approve papers in Inbox to include them in the next sync.', results);
      }
      for (const row of visible.slice(0, state.limit)) {
        if (!reviewing) { this.renderLinkedPaper(results, row, metadata.get(row.parent)?.title); continue; }
        const card = this.el('section', '', results, {class: 'card paper-name-review'});
        this.el('h3', metadata.get(row.key)?.title || row.title, card);
        if (row.reference) this.reviewNavigationButton('Show reference in Zotero', card, 'references', [row.key]);
        for (const finding of row.findings) {
          const label = finding.where === 'Zotero filename' && /[.]pdf$/i.test(finding.name) ? 'Zotero PDF filename' : finding.where;
          this.el('p', `${label} · ${finding.name}`, card);
          if (finding.attachment) this.reviewNavigationButton('Show attachment in Zotero', card, 'attachment', [finding.attachment]);
        }
        if (row.status === 'no_longer_matches') this.el('p', 'The flagged name no longer appears in Zotero or in the saved reMarkable link.', card);
        if (row.queued) this.button('Remove from name review', card, () => saveReview({dismiss: row.key}));
        else if (row.token) this.button('Queue for review', card, () => saveReview({selected: [{key: row.key, token: row.token}]}));
        for (const link of byPaper.get(row.key) || []) {
          if (link.filename_cleanup) this.renderQueuedRename(card, link);
          else if (renames.has(link.cloud)) this.renderRenamePreview(card, renames.get(link.cloud), state, render);
        }
      }
      if (visible.length > state.limit) {
        const more = this.button('Show more matching papers', results, () => { state.limit += 25; update(); });
        more.dataset.browse = 'true';
      }
      if (this.busy) this.disable(true);
    };
    if (rows.length) {
      metadataLoading = true;
      this.paperSearchMetadata(reviewing ? rows.map(row => row.key) : [...byPaper.keys()]).then(value => {
        metadata = value;
      }).catch(() => { metadataError = true; }).finally(() => {
        metadataLoading = false;
        if (results.isConnected) update();
      });
    }
    update();
  },
  renderQueuedRename(card, row) {
    const pending = row.filename_cleanup;
    this.el('p', `Rename queued · ${pending.target}.pdf`, card);
    const issue = this.latest.status?.result?.review?.find(issue => issue.cloud === row.cloud);
    if (issue?.reason) this.el('p', issue.reason, card, {class: 'hint'});
    if (!pending.started) this.button('Cancel queued rename', card, async () => {
      await this.action('cancel_filename_cleanup', {identity: row.cloud});
      if (this.paperSearch?.view === 'review') await this.loadNameReview();
    });
  },
  renderLinkedPaper(content, row, title) {
    const card = this.el('section', '', content, {class: 'card paper-name-review'});
    this.el('h3', title || row.name, card);
    if (title && title !== row.name) this.el('p', `reMarkable · ${row.name}`, card);
    this.el('p', row.unavailable ? 'The tablet document is unavailable. The link and retained files are kept; sync again when it is available.'
      : row.paused ? 'Link paused. Both copies are kept; changes will not sync until you resume.'
      : row.revision ? 'Tablet PDF and available notes received' : 'Connected; waiting for the first PDF and tablet notes sync', card);
    this.reviewNavigationButton('Show in Zotero', card, 'references', [row.parent]);
    this.button('Check connection', card, () => this.previewLinkRepair(row.cloud));
    this.renderLinkRepair(card, row.cloud);
    this.button(row.paused ? 'Resume link' : 'Unlink (keep both copies)', card, () => this.action('link', {identity: row.cloud, paused: !row.paused}));
    if (row.view_key) this.button('Rebuild search index', card, () => this.action('reindex', {key: row.view_key}));
    if (row.view_key) this.renderAnnotationMaintenance(card, row);
    if (row.filename_cleanup) {
      this.renderQueuedRename(card, row);
    } else if (row.filename_repair) {
      const undo = this.el('details', '', card);
      this.el('summary', 'Review the previous filename repair', undo);
      for (const file of row.filename_repair.files) this.el('p', `${file.filename} → ${file.before}`, undo);
      this.el('p', `reMarkable · ${row.name} → ${row.filename_repair.tablet}`, undo);
      this.button('Queue undo of filename repair', undo, async () => {
        if (await this.action('undo_filename_cleanup', {identity: row.cloud})) this.schedule(1000);
      });
    }
    if (row.keyword_check) {
      const labels = {found: 'Paper keywords checked and added where missing.', no_stated_keywords: 'No stated keyword list found in the readable first pages.',
        needs_text_recognition: 'Keywords need text recognition or manual checking.', extraction_failed: 'The keyword check failed; sync will retry.'};
      this.el('p', labels[row.keyword_check.status] || 'Keyword check pending.', card, {class: 'hint'});
    }
    const edit = this.el('details', '', card); this.el('summary', 'Rename or move', edit);
    const key = JSON.stringify([this.filingConnection(), row.cloud]), draft = this.linkDrafts.get(key) || {};
    edit.open = !!draft.open;
    const name = this.field('PDF name', edit, draft.name ?? row.name);
    const home = this.destinationSelect('Linked folders for this paper (Zotero → reMarkable)', edit, draft.collection ?? row.collection);
    name.dataset.browse = home.dataset.browse = 'true';
    const saveDraft = () => this.linkDrafts.set(key, {name: name.value, collection: home.value, open: edit.open, revision: draft.revision ?? row.revision});
    for (const field of [name, home]) { field.addEventListener('input', saveDraft); field.addEventListener('change', saveDraft); }
    edit.addEventListener('toggle', saveDraft);
    this.button('Save name & destination', edit, async () => {
      await this.action('move_link', {identity: row.cloud, name: name.value, collection: home.value, revision: draft.revision ?? row.revision}, false);
      this.linkDrafts.delete(key); this.render();
      this.schedule(1000);
      this.message('Name and destination saved for the next sync.');
    });
    const history = this.el('details', '', card); this.el('summary', 'Link & recovery history', history);
    this.el('p', 'Clean baseline and downloaded tablet sources are retained privately for recovery.', history);
    for (const event of [...(row.history || []), ...(row.repair_history || [])]) this.el('p', new Date(event.time * 1000).toLocaleString() + ' — ' + event.action, history);
    if (row.desktop_annotations_preserved) this.el('p', 'Independent desktop annotations are preserved in their original attachment.', history);
  },
  renderSetup(content) {
    const config = this.latest.config, draft = this.setupDraft || config, connected = !!config.tablet_root;
    const main = content;
    this.el('h2', 'Setup', main);
    this.el('p', 'Choose where to sync your papers. Enable automatic sync to check for changes while Zotero is open.', main);
    content = this.el(connected ? 'details' : 'section', '', main, {class: 'card'});
    this.el(connected ? 'summary' : 'h2', connected ? 'Zotero and tablet connection settings' : 'Connect Zotero and reMarkable', content);
    const steps = this.el('div', '', content, {class: 'actions'});
    this.button('Connect Zotero', steps, async () => { await this.action('connect'); if (config.tablet_root) await this.loadFolders(); }).dataset.exclusive = 'true';
    this.button('Get reMarkable pairing code', steps, () => Zotero.launchURL('https://my.remarkable.com/device/desktop/connect'));
    const code = this.field('Single-use reMarkable pairing code', content, '', 'input', {sensitive: true}); code.maxLength = 8; code.autocomplete = 'off'; code.className = 'pairing-code';
    code.dataset.exclusive = 'true';
    this.button('Pair reMarkable', content, async () => { const value = code.value; code.value = ''; await this.action('pair', {code: value}); await this.loadFolders(); }).dataset.exclusive = 'true';
    if (!connected) this.button('Refresh folders', content, () => this.loadFolders());
    const scope = this.el('section', '', main, {class: 'card'});
    this.el('h2', connected ? 'Your synced folders' : 'Choose your synced folders', scope);
    const folders = this.folderCatalog;
    const zpaths = folders ? this.paths(folders.collections, 'key', 'parentCollection', 'name') : [];
    const cpaths = folders ? this.paths(folders.folders, 'id', 'parent', 'name') : [];
    const name = (paths, key, fallback) => paths.find(([id]) => id === key)?.[1] || fallback;
    if (connected) {
      this.el('p', 'Zotero: ' + name(zpaths, config.zotero_root, config.zotero_path || 'Saved Zotero collection'), scope, {id: 'notable-connected-zotero'});
      this.el('p', 'reMarkable: ' + name(cpaths, config.tablet_root, config.tablet_path || 'Saved reMarkable folder'), scope);
      this.el('p', 'These two folders are linked. Their names can be different.', scope, {class: 'hint'});
      this.el('p', `${this.latest.connection?.linked_papers ?? this.latest.links?.length ?? 0} linked papers · ${config.sync_paused ? 'Sync paused' : 'Ready to sync'}`, scope, {class: 'hint'});
      if (!this.changingFolder && !this.disconnectPreview) {
        this.button('Change folder', scope, async () => {
          this.changingFolder = true; this.setupDraft = {...this.latest.config}; this.folderPreview = null;
          await this.pauseSync(); if (!this.folderCatalog) await this.loadFolders(); else this.render();
        });
        this.button('Disconnect folder', scope, async () => {
          await this.pauseSync(); this.disconnectPreview = await this.action('preview_disconnect', {}, false); this.render();
        });
      }
      this.button('Refresh folders', scope, () => this.loadFolders());
    }
    if (this.disconnectPreview) {
      const preview = this.disconnectPreview;
      this.el('h3', 'Disconnect this folder?', scope);
      this.el('p', `Your papers and handwritten notes will stay in both libraries. ${preview.linked_papers} paper links and ${preview.pending_reviews} reviews will be kept with this connection. Your tablet will stay paired.`, scope);
      this.button('Confirm disconnect', scope, async () => {
        await this.action('disconnect_folder', {confirmation: preview.token}, false);
        this.clearFolderEditor(); this.render();
      }, true);
      this.button('Cancel', scope, () => { this.disconnectPreview = null; this.render(); });
      return;
    }
    if (!folders) this.el('p', 'Refresh folders to choose synced folders. Local intake, search, and naming settings remain available below.', scope);
    let zroot, croot, destination;
    if ((!connected || this.changingFolder) && folders) {
      this.el('p', 'Choose a dedicated Zotero collection and the reMarkable folder you want to use. Saving keeps papers and notes in place.', scope);
      zroot = this.folderSelect('Zotero collection tree for tablet locations', scope, [['', 'Choose a Zotero collection…'], ...zpaths], draft.zotero_root || '');
      zroot.dataset.zoteroCollections = 'true';
      croot = this.folderSelect('reMarkable folder tree to sync', scope, [['', 'Choose a reMarkable folder…'], ...cpaths], draft.tablet_root || '', false, 'tablet');
      const tabletChoices = () => {
        const rows = this.folderCatalog?.folders || [];
        const by = new Map(rows.map(row => [row.id, row]));
        const inside = id => { const seen = new Set(); while (id && !seen.has(id)) { if (id === croot.value) return true; seen.add(id); id = by.get(id)?.parent; } return false; };
        return this.paths(rows, 'id', 'parent', 'name').filter(([id]) => inside(id));
      };
      destination = this.folderSelect('Default tablet destination for new papers', scope, [], '', false, 'tablet', {choices: tabletChoices, parentChoices: tabletChoices, newParent: draft.tablet_root});
      const updateDestinations = () => {
        const selected = destination.value || draft.default_folder;
        this.reconcileOptions(destination, [['', 'Choose a destination…'], ...tabletChoices()]);
        destination.value = [...destination.options].some(o => o.value === selected) ? selected : croot.value;
        destination.notableRefreshChooser();
      };
      croot.addEventListener('change', updateDestinations); updateDestinations();
      if (this.changingFolder) this.button('Cancel change', scope, () => { this.clearFolderEditor(); this.render(); });
    } else {
      this.el('p', 'Default destination: ' + name(cpaths, config.default_folder, 'Selected folder'), scope, {class: 'hint'});
    }
    const previous = this.latest.connection?.previous || [];
    if (previous.length) {
      const saved = this.el('details', '', scope); this.el('summary', 'Previous folder connections', saved);
      for (const row of previous) this.el('p', `${row.tablet_path || name(cpaths, row.tablet_root, 'Previous reMarkable folder')} · ${row.linked_papers} paper links paused`, saved);
      this.el('p', 'Choose the same Zotero and reMarkable folders again to restore that connection. Its files and page tags are retained.', saved, {class: 'hint'});
    }
    content = this.el('section', '', main, {class: 'card'});
    this.el('h2', 'Intake and search', content);
    const inbox = this.field('Watched ToSort folder (optional)', content, draft.inbox || '');
    this.button('Choose ToSort folder…', content, async () => {
      const {FilePicker} = ChromeUtils.importESModule('chrome://zotero/content/modules/filePicker.mjs');
      const picker = new FilePicker(); picker.init(this.view, 'Choose ToSort folder', picker.modeGetFolder);
      if (await picker.show() === picker.returnOK) { inbox.value = picker.file; inbox.notableRefreshValue(); dirty(); }
    });
    const automatic = this.checkbox('Sync automatically while Zotero is open', content, draft.automatic ?? !connected);
    const ocr = this.checkbox('Search scanned text and handwriting locally', content, draft.ocr !== false);
    this.el('p', 'Handwriting recognition can misread scientific notation. Your original ink stays intact.', content, {class: 'hint'});
    const section = this.el('details', '', main, {class: 'card'}); this.el('summary', 'Naming templates by document type', section);
    this.el('p', 'Leave blank for Smart naming. Tokens include {author_last}, {year}, {title}, {journal}, {document_type}, and {patent_number}.', section);
    const fields = {};
    for (const [key, label] of Object.entries(this.latest.types)) fields[key] = this.field(label, section, draft.templates?.[key] || '');
    const values = () => ({zotero_root: zroot ? zroot.value : config.zotero_root,
      tablet_root: croot ? croot.value : config.tablet_root, default_folder: destination ? destination.value : config.default_folder,
      inbox: inbox.value, automatic: automatic.checked, ocr: ocr.checked,
      templates: Object.fromEntries(Object.entries(fields).map(([key, field]) => [key, field.value]))});
    const dirty = () => { this.setupDirty = true; this.folderPreview = null; this.setupDraft = values(); this.disable(this.busy); };
    for (const node of [...scope.querySelectorAll('select'), ...content.querySelectorAll('input, textarea'), ...section.querySelectorAll('input, textarea')]) node.addEventListener('input', dirty);
    if (this.folderPreview) {
      const preview = this.folderPreview, card = this.el('section', '', main, {class: 'card decision'});
      this.el('h2', 'Check your folder selection', card);
      this.el('p', 'Zotero: ' + preview.proposed.zotero_path, card);
      this.el('p', 'reMarkable: ' + preview.proposed.tablet_path, card);
      this.el('p', `Your papers and notes stay in place. ${preview.linked_papers} current paper links will be kept with the previous connection. Automatic sync will be off.`, card);
      if (preview.restored_papers) this.el('p', `${preview.restored_papers} previous paper links will be restored for these folders.`, card);
      for (const [title, names] of [['Zotero subfolders included', preview.zotero_folders], ['reMarkable subfolders included', preview.tablet_folders],
          ['Old Zotero folders kept out of this sync', preview.excluded_zotero_folders], ['Old reMarkable folders kept out of this sync', preview.excluded_tablet_folders]]) {
        if (!names.length) continue;
        const detail = this.el('details', '', card); this.el('summary', `${title} (${names.length})`, detail);
        for (const value of names) this.el('p', value, detail);
      }
      this.el('p', 'Included subfolders mirror each other only after you click Sync now. Previously linked papers stay paused with their previous connection.', card, {class: 'hint'});
      this.button('Save folder change', card, async () => {
        if (!this.folderPreview) {
          const result = await this.action('preview_connection', this.setupDraft, false);
          this.folderPreview = result?.proposed && result?.token ? result : null; this.render(); return;
        }
        await this.action('configure', {...this.setupDraft, confirmation: preview.token}, false);
        this.clearFolderEditor(); this.render();
      }, true);
    } else {
      this.button(this.changingFolder || (!connected && previous.length) ? 'Review folder change' : 'Save setup', main, async () => {
        this.setupDraft = values();
        if (this.changingFolder || (!connected && previous.length)) {
          const result = await this.action('preview_connection', this.setupDraft, false);
          this.folderPreview = result?.proposed && result?.token ? result : null; this.render();
        } else {
          await this.action('configure', this.setupDraft, false); this.clearFolderEditor(); this.schedule(5000); this.render();
        }
      }, true);
    }
    this.renderMaintenance(main);
  },
  async loadFolders() { this.folderCatalog = await this.action('folders', {}, false); this.refreshCollectionNames(); this.render(); }
};
