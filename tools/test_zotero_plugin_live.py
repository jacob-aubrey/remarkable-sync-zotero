"""Validate the graphical plugin in a disposable Zotero profile and library.

The helper extension below is test instrumentation, never shipped to users.
It authorizes only the synthetic profile and reports the actual rendered UI.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.build_zotero_support import build
from note_organizer.zotero_pilot import LocalZotero, PilotError
from note_organizer.zotero_worker import Worker
from tests.test_cloud_outbox import MemoryCloud, pdf_bytes


BOOTSTRAP = """
async function startup({rootURI}) {
  const config = JSON.parse(await (await fetch(rootURI + 'config.json')).text());
  Zotero.uiReadyPromise.then(async () => {
    if (Zotero.DataDirectory.dir !== config.data) throw new Error('Unexpected test library');
    Zotero.Server.LocalAPI._promptForAuthorization = async name => ({allow: name === 'Notable Zotero Pilot', remember: true});
    await IOUtils.writeUTF8(config.ready, JSON.stringify({version: Zotero.version, server: Zotero.Server.LocalAPI.getServerID(), pid: Services.appinfo.processID}));
    while (!(await IOUtils.exists(config.done))) {
      const path = PathUtils.join(PathUtils.parent(config.ready), 'command.json');
      if (await IOUtils.exists(path)) {
        const command = JSON.parse(await IOUtils.readUTF8(path)); await IOUtils.remove(path);
        let response = {id: command.id};
        try {
          const ui = Zotero.Notable;
          if (!ui) throw new Error('Notable UI did not initialize');
          if (command.action === 'open') {
            const window = Zotero.getMainWindow();
            const popup = window.document.getElementById('menu_ToolsPopup');
            popup.dispatchEvent(new window.Event('popupshowing'));
            const entries = [...popup.children].filter(item => item.getAttribute('label') === 'reMarkable Sync');
            if (entries.length !== 1 || entries[0].hidden || entries[0].disabled) {
              throw Error('Tools must contain one usable reMarkable Sync command');
            }
            const menuIcon = 'chrome://notable/content/icons/tablet.svg';
            const [entry] = entries;
            if (!entry.classList.contains('menuitem-iconic')
                || !window.getComputedStyle(entry).listStyleImage.includes(menuIcon)
                || !entry.style.getPropertyValue('--custom-menu-icon-dark').includes(menuIcon)) {
              throw Error('The Tools command is missing its native R icon');
            }
            const manager = Zotero.openInViewer('chrome://mozapps/content/extensions/aboutaddons.html');
            try {
              const getIcon = () => manager.document.querySelector('browser')?.contentDocument
                ?.querySelector(`addon-card[addon-id="${ui.id}"] .addon-icon`);
              for (let i = 0; i < 150 && !getIcon()?.naturalWidth; i++) await Zotero.Promise.delay(100);
              const icon = getIcon();
              if (!icon?.naturalWidth || !icon.src.endsWith('/icons/tablet.svg')) {
                throw Error('The Plugins manager did not load the reMarkable Sync icon');
              }
            } finally { manager.close(); }
            entries[0].doCommand(); const deadline = Date.now() + 120000;
            while ((!ui.process || !Object.keys(ui.latest?.types || {}).length || !ui.view?.document.getElementById('app')?.querySelector('h1')) && Date.now() < deadline) {
              if (ui.view?.document.querySelector('[role="alert"]')) break;
              await Zotero.Promise.delay(100);
            }
          } else if (command.action === 'preferences') {
            const pane = Zotero.PreferencePanes.pluginPanes.find(entry => entry.pluginID === ui.id);
            if (pane?.rawLabel !== ui.shortName) throw Error('reMarkable Sync settings entry is missing');
            const prefs = Zotero.Utilities.Internal.openPreferences(pane.id);
            try {
              const buttonID = 'notable-preferences-open-setup';
              for (let i = 0; i < 150 && !prefs.document.getElementById(buttonID); i++) await Zotero.Promise.delay(100);
              const button = prefs.document.getElementById(buttonID);
              if (!button || button.closest('[hidden]')) throw Error('Sync Setup button is missing or hidden');
              ui.openTab('inbox');
              button.doCommand();
              if (ui.tab !== 'setup') throw Error('Settings did not switch the existing workspace to Setup');
              ui.view.close();
              button.doCommand();
              for (let i = 0; i < 150 && !ui.view?.document.getElementById('app')?.querySelector('h1'); i++) {
                await Zotero.Promise.delay(100);
              }
              if (ui.tab !== 'setup' || !ui.view?.document.getElementById('app')?.querySelector('h1')) {
                throw Error('Settings did not open a new workspace on Setup');
              }
            } finally { prefs.close(); }
          } else if (command.action === 'connect') await ui.action('connect');
          else if (command.action === 'tab') { ui.tab = command.tab; ui.render(); }
          else if (command.action === 'folders') await ui.loadFolders();
          else if (command.action === 'refresh') { await ui.refresh(); ui.render(); }
          else if (command.action === 'render') { ui.latest = command.snapshot; ui.tab = command.tab || 'review'; ui.render(); }
          else if (command.action === 'paper_search_ui') {
            ui.latest = {...ui.latest, links: [{cloud: 'search-fixture', name: 'Tablet fixture', parent: command.parent},
              {cloud: 'search-fixture-unread', name: 'Unread fixture', parent: command.unread}]};
            ui.tab = 'links'; ui.render();
            const doc = ui.view.document;
            const search = () => doc.getElementById('notable-paper-search');
            const contains = text => doc.querySelector('main').textContent.includes(text);
            const waitFor = async (predicate, message) => {
              for (let i = 0; i < 150 && !predicate(); i++) await Zotero.Promise.delay(50);
              if (!predicate()) throw Error(message);
            };
            const enter = value => { search().value = value; search().dispatchEvent(new ui.view.Event('input')); };
            await waitFor(() => contains(command.title), 'Citation metadata did not load');
            enter('gurevich 2025');
            if (!contains(command.title)) throw Error('Creator and year did not match across fields');
            const mode = doc.querySelector('[aria-label="Search fields"]');
            const toolbar = mode.closest('.paper-search-toolbar');
            const reference = mode.cloneNode(true);
            reference.style.cssText = 'position: fixed; visibility: hidden; width: max-content; max-width: none;';
            mode.parentNode.append(reference);
            try {
              for (const width of ['560px', '340px']) {
                toolbar.style.width = width;
                mode.style.fontSize = reference.style.fontSize = '18px';
                const menuWidth = mode.getBoundingClientRect().width;
                const naturalWidth = reference.getBoundingClientRect().width;
                if (menuWidth + 1 < naturalWidth) throw Error(`The search scope clipped at ${width} (${menuWidth} < ${naturalWidth})`);
                if (search().getBoundingClientRect().width < 60) throw Error('The search input became too narrow');
              }
            } finally {
              reference.remove();
              toolbar.style.width = mode.style.fontSize = '';
            }
            mode.value = 'all'; mode.dispatchEvent(new ui.view.Event('change'));
            enter('vascular fixture tag');
            if (!contains(command.title)) throw Error('Reference tags did not match');
            enter('not in the library');
            if (contains(command.title)) throw Error('Unmatched reference was still rendered');
            doc.querySelector('[aria-label="Clear search"]').click();
            if (search().value || !contains(command.title)) throw Error('Clear search did not restore results');
            mode.value = 'files'; mode.dispatchEvent(new ui.view.Event('change'));
            enter('UnknownJournal');
            if (!contains(command.title)) throw Error('Stored attachment filename did not match');
            search().dispatchEvent(new ui.view.KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
            if (search().value) throw Error('Escape did not clear the search');
            const selectStatus = value => {
              const status = doc.querySelector('[aria-label="Reading status"]');
              status.value = value; status.dispatchEvent(new ui.view.Event('change'));
            };
            selectStatus('read');
            if (!contains(command.title) || contains(command.unreadTitle)) throw Error('Read did not match only the exact Read tag');
            selectStatus('unread');
            if (contains(command.title) || !contains(command.unreadTitle)) throw Error('Unread did not find the unmarked paper');
            const unread = await Zotero.Items.getByLibraryAndKeyAsync(Zotero.Libraries.userLibraryID, command.unread);
            unread.addTag('Read');
            await unread.saveTx();
            await waitFor(() => !contains(command.unreadTitle) && contains('0 of 2 linked papers'), 'Reading status did not refresh after a Zotero tag changed');
            unread.removeTag('Read');
            await unread.saveTx();
            await waitFor(() => contains(command.unreadTitle), 'Removing Read did not bring back the unread paper');
            selectStatus('all');
            const review = [...doc.querySelectorAll('main button')].find(button => button.textContent === 'Review paper names');
            if (!review?.dataset.buttonHelp.includes('saved reMarkable name')) throw Error('Review paper names is missing its specific tooltip');
            review.click();
            await waitFor(() => !ui.nameReviewLoading && contains(command.filename), 'Name review did not show the unlinked attachment');
            enter('UnknownJournal');
            if (!contains('Zotero PDF filename · ' + command.filename)) throw Error('The attachment filename was not labeled');
            const reviewMode = doc.querySelector('[aria-label="Search fields"]');
            reviewMode.value = 'citation'; reviewMode.dispatchEvent(new ui.view.Event('change'));
          }
          else if (command.action === 'tag_search_ui') {
            const doc = ui.view.document;
            const waitFor = async (predicate, message) => {
              for (let i = 0; i < 150 && !predicate(); i++) await Zotero.Promise.delay(50);
              if (!predicate()) throw Error(message);
            };
            const input = () => doc.querySelector('[aria-label="Filter tag list"]');
            const choices = () => [...doc.querySelectorAll('.tag-list > button')];
            const choose = tag => choices().find(button => button.textContent === tag);
            const search = () => [...doc.querySelectorAll('main button')].find(button => button.textContent === 'Search');
            const found = title => [...doc.querySelectorAll('.search-result > h3')].some(node => node.textContent === title);
            const filter = value => { input().value = value; input().dispatchEvent(new ui.view.Event('input')); };
            const changeScope = value => {
              const scope = doc.querySelector('[aria-label="Search in"]');
              scope.value = value; scope.dispatchEvent(new ui.view.Event('change'));
            };
            await waitFor(() => ui.paperTagBank?.tags.includes('Reading list'), 'The native paper tag bank did not warm before opening Tags');
            const originalRPC = ui.rpc;
            const originalLinks = ui.latest.links;
            let helperCalls = 0;
            ui.rpc = (...params) => {
              helperCalls++;
              return originalRPC.call(ui, ...params);
            };
            try {
              ui.latest.links = [{parent: command.parent, view_key: 'fixture-view',
                page_map: ['current-page'], page_tags: {'current-page': ['Saved page tag'], orphan: ['Orphan tag']}}];
              ui.busy = true;
              ui.openTab('tags');
              if (!choose('Read') || !choose('Reading list')) throw Error('Opening Tags did not show the warm bank immediately');
              if (choose('Trash-only fixture tag') || choose('Note-only fixture tag')) throw Error('The paper bank included a trashed paper or note');
              changeScope('page');
              if (!choose('Saved page tag') || choose('Orphan tag') || choose('Read')) throw Error('Page tags were not read from current saved links');
              changeScope('paper');
              if (helperCalls) throw Error('Browsing the tag bank called the busy helper');
            } finally {
              ui.busy = false;
              ui.latest.links = originalLinks;
              ui.rpc = originalRPC;
              ui.render();
            }
            const parent = await Zotero.Items.getByLibraryAndKeyAsync(Zotero.Libraries.userLibraryID, command.parent);
            parent.addTag('New native fixture tag');
            await parent.saveTx();
            await waitFor(() => choose('New native fixture tag'), 'A new Zotero tag did not appear in the open picker');
            parent.removeTag('New native fixture tag');
            await parent.saveTx();
            await waitFor(() => !choose('New native fixture tag'), 'The removed Zotero tag stayed in the picker');
            ui.view.dispatchEvent(new ui.view.KeyboardEvent('keydown', {key: 'f', ctrlKey: true}));
            if (doc.activeElement !== input()) throw Error('Ctrl+F did not focus the tag list filter');
            filter('read');
            if (choose('Vascular fixture tag') || !choose('Read') || !choose('Reading list')) throw Error('Typing did not narrow the tag list');
            choose('Read').click();
            filter('vascular');
            choose('Vascular fixture tag').click();
            if (found(command.title)) throw Error('Tag selection applied before Search');
            if (!doc.querySelector('[aria-label="Remove Read from selected tags"]')) throw Error('The picker lost a selected tag after filtering');
            search().click();
            await waitFor(() => found(command.title), 'Match all did not return the paper with both tags');
            if (found(command.unreadTitle)) throw Error('Match all included a paper with only one selected tag');
            const match = doc.querySelector('[aria-label="Match selected tags"]');
            match.value = 'any'; match.dispatchEvent(new ui.view.Event('change'));
            if (found(command.unreadTitle) || !doc.querySelector('main').textContent.includes('Your choices changed')) {
              throw Error('Changing Match did not retain and label the last search');
            }
            search().click();
            await waitFor(() => found(command.unreadTitle), 'Match any did not include the paper with one tag');
            doc.querySelector('[aria-label="Clear search"]').click();
            if (input().value || !doc.querySelector('[aria-label="Remove Read from selected tags"]')) throw Error('Clearing text cleared the tag selection');
            const clear = [...doc.querySelectorAll('main button')].find(button => button.textContent === 'Clear selection');
            clear.click();
            await waitFor(() => !doc.querySelector('[aria-label="Remove Read from selected tags"]'), 'Clear selection did not remove picked tags');
            changeScope('page');
            if (choose('Read')) throw Error('Page scope showed a paper-only tag');
            changeScope('paper');
            choose('Read').click();
            search().click();
            await waitFor(() => found(command.title) && !found(command.unreadTitle), 'Exact Read selection did not update the results');
            const savedBank = ui.paperTagBank;
            const savedRPC = ui.rpc;
            let searchCalls = 0;
            try {
              ui.paperTagBank = {...savedBank, tags: Array.from({length: 1346}, (_, index) =>
                'Fixture tag ' + String(index).padStart(4, '0') + ' with a longer name')};
              ui.rpc = (method, ...params) => {
                if (method === 'tag_search') searchCalls++;
                return savedRPC.call(ui, method, ...params);
              };
              ui.render();
              const bank = doc.querySelector('.tag-list');
              const initial = choices().length;
              if (initial !== 60 || bank.scrollHeight <= bank.clientHeight) throw Error('The tag bank did not start with one scrollable batch');
              const first = choices()[0];
              first.click();
              bank.scrollTop = bank.scrollHeight - bank.clientHeight;
              const previousTop = bank.scrollTop;
              bank.dispatchEvent(new ui.view.Event('scroll'));
              await waitFor(() => choices().length > initial, 'Scrolling did not load the next tag batch');
              if (choices().length !== 120 || choices()[0] !== first || first.getAttribute('aria-pressed') !== 'true'
                  || Math.abs(bank.scrollTop - previousTop) > 1) {
                throw Error('Appending tags lost the scroll position, selection, or first batch');
              }
              const last = choices().at(-1);
              last.dispatchEvent(new ui.view.KeyboardEvent('keydown', {key: 'ArrowRight', bubbles: true}));
              if (doc.activeElement !== choices()[120]) throw Error('Keyboard browsing stopped at the batch boundary');
              filter('1345');
              if (choices().length !== 1 || !choose('Fixture tag 1345 with a longer name') || searchCalls) {
                throw Error('The filter did not reach the full list locally');
              }
              filter('');
            } finally {
              ui.tagSearch.tags.delete('Fixture tag 0000 with a longer name');
              ui.tagSearch.query = '';
              ui.paperTagBank = savedBank;
              ui.rpc = savedRPC;
              ui.render();
            }
          }
          else if (command.action === 'workspace_consistency') {
            const original = ui.latest;
            const doc = ui.view.document;
            const main = () => doc.querySelector('main');
            const control = label => [...main().querySelectorAll('button')].find(button => button.textContent === label);
            const collection = original.collections[0];
            if (!collection) throw Error('Missing disposable import collection');
            ui.latest = {...original, reviews: [], deletions: [], local_deletions: [], status: {},
              bulk: {id: 'fixture', root: 'C:/Fixture', collection: collection.key, lookup: true, failures: [
                {path: 'Unavailable.pdf', reason: 'Could not read file'}], rows: [
                {id: 'complete', path: 'Imported.pdf', status: 'complete', issues: [], copies: ['Imported.pdf']},
                {id: 'pending', path: 'Needs citation.pdf', status: 'pending', issues: ['Review citation'], copies: ['Needs citation.pdf']},
              ]}};
            try {
              ui.openTab('import');
              if (main().querySelector('h2').textContent !== 'Import library') throw Error('Import title differs from its tab');
              const filter = main().querySelector('[aria-label="Filter import exceptions"]');
              filter.value = 'unavailable'; filter.dispatchEvent(new ui.view.Event('input'));
              if (!main().textContent.includes('1 of 2 import exceptions')) throw Error('Import search did not count the matching scan error');
              main().querySelector('[aria-label="Clear search"]').click();
              if (filter.value || !main().textContent.includes('2 of 2 import exceptions')) throw Error('Import search did not clear');
              control('Open Inbox').click();
              for (let i = 0; ui.tab !== 'inbox' && i < 50; i++) await Zotero.Promise.delay(20);
              if (ui.tab !== 'inbox') throw Error('Import did not lead to Inbox');
              for (const tab of ['inbox', 'library', 'sync-review']) {
                ui.openTab(tab);
                const empty = main().querySelector('.review-empty');
                if (!empty?.querySelector('h3') || (tab !== 'sync-review' && !empty.querySelector('.review-recheck'))) {
                  throw Error(tab + ' did not use the shared empty state and its appropriate action');
                }
              }
              ui.openTab('setup');
              const maintenance = [...main().querySelectorAll('summary')].find(node => node.textContent === 'Maintenance and backups');
              if (!maintenance?.parentNode.contains(control('Repair bundled helper')) ||
                  !maintenance.parentNode.contains(control('Verify all files and sync')) ||
                  !(control('Save setup').compareDocumentPosition(maintenance) & 4)) {
                throw Error('Setup maintenance was not grouped after the main setup flow');
              }
            } finally {
              ui.latest = original;
              ui.openTab('tags');
            }
          }
          else if (command.action === 'collection_names') {
            const collection = await Zotero.Collections.getByLibraryAndKeyAsync(Zotero.Libraries.userLibraryID, command.key);
            if (!collection || collection.deleted) throw Error('Missing synthetic collection');
            const originalName = collection.name;
            ui.latest = command.snapshot; ui.folderCatalog = command.folders;
            ui.tab = 'review'; ui.render();
            const doc = ui.view.document;
            const filename = [...doc.querySelectorAll('label')].find(label => label.firstElementChild?.textContent === 'Proposed filename').querySelector('input');
            filename.value = 'My unsaved name.pdf'; filename.dispatchEvent(new ui.view.Event('input'));
            const destination = doc.querySelector('select[data-folder-destination]');
            const connection = ui.filingConnection(), rpc = ui.rpc;
            const ensure = (value, message) => {if (!value) throw Error(message);};
            ui.rpc = () => {throw Error('Renaming unexpectedly contacted the worker');};
            try {
              collection.name = 'Literature'; await collection.saveTx();
              ensure(destination.selectedOptions[0].textContent === 'Zotero: Literature â†’ reMarkable: Literature', 'Open destination kept old name');
              ensure(filename.isConnected && filename.value === 'My unsaved name.pdf', 'Rename replaced the active review form');
              ensure(ui.filingConnection() === connection && destination.value === command.key, 'Rename changed folder identity');
              ui.tab = 'setup'; ui.render();
              ensure(doc.getElementById('notable-connected-zotero').textContent === 'Zotero: Literature', 'Setup kept old collection name');
              response.passed = ['native rename updates open destination', 'review edit survives rename', 'connection and destination keys unchanged', 'setup shows renamed collection'];
              ui.rpc = rpc;
              ui.view.close(); ui.view = null; ui.open();
              const deadline = Date.now() + 15000;
              while (!ui.view?.document.getElementById('app')?.querySelector('h1') && Date.now() < deadline) await Zotero.Promise.delay(50);
              await ui.refresh(); ui.tab = 'setup'; ui.render();
              ensure(ui.latest.config.zotero_path === 'Literature', 'Reopened worker status restored stale name');
              ensure(ui.view.document.getElementById('notable-connected-zotero').textContent === 'Zotero: Literature', 'Reopened Setup kept old name');
              response.passed.push('reopened workspace refreshes stale saved name');
            } finally {
              ui.rpc = rpc; collection.name = originalName; await collection.saveTx();
            }
          }
          else if (command.action === 'startup_navigation') {
            const saved = {latest: ui.latest, tab: ui.tab, selected: ui.selected, rpc: ui.rpc, mount: ui.mount,
              drafts: ui.reviewDrafts, selections: ui.queueSelections, requested: ui.requestedTab};
            const clone = value => JSON.parse(JSON.stringify(value));
            const ensure = (ok, why) => { if (!ok) throw Error(why); };
            const waitFor = async (condition, why) => {
              const until = Date.now() + 15000;
              while (!condition() && Date.now() < until) await Zotero.Promise.delay(25);
              ensure(condition(), why);
            };
            let release, mounting;
            response.cases = [];
            try {
              for (const target of [{tab: 'library', label: 'Library Issues'}, {tab: 'sync-review', label: 'Sync Conflicts'}]) {
                if (ui.view && !ui.view.closed) ui.view.close();
                ui.view = null;
                const state = clone(command.snapshot), requests = [];
                let entered = false;
                const gate = new Promise(resolve => { release = resolve; });
                ui.latest = clone(state); ui.selected = null; ui.queueSelections = {}; ui.requestedTab = null;
                ui.reviewDrafts = new Map([['startup-incoming', {digest: 'fixture', filename: 'Retained startup draft.pdf'}]]);
                const drafts = JSON.stringify([...ui.reviewDrafts]);
                ui.rpc = async (method, params = {}) => {
                  requests.push({method, params: clone(params)});
                  ensure(method === 'status', 'Opening or browsing ran a write or sync action');
                  entered = true; await gate; return clone(state);
                };
                mounting = null;
                ui.mount = function(window) { mounting = Promise.resolve(saved.mount.call(this, window)); return mounting; };
                ui.open();
                await waitFor(() => entered && ui.view?.document.querySelector('nav'), 'Initial status did not wait behind the startup gate');
                const doc = ui.view.document;
                const button = [...doc.querySelectorAll('nav button')].find(node => node.textContent.startsWith(target.label + ' ('));
                ensure(button && !button.disabled, 'Startup tab is not available: ' + target.label);
                button.click();
                await waitFor(() => ui.tab === target.tab, 'The requested tab click did not finish');
                const cases = [...doc.querySelectorAll('.review-layout > aside > button')];
                ensure(cases.length >= 2, 'Startup fixture needs two cases in ' + target.label);
                const previous = ui.selected;
                cases.at(-1).click();
                await waitFor(() => ui.selected !== previous && ui.renderedReviewId === ui.selected, 'The requested case click did not finish');
                const selected = ui.selected;
                ensure(ui.tab === target.tab && selected, 'Early navigation did not select the requested case');
                release(); release = null; await mounting;
                ensure(ui.tab === target.tab, 'Startup status overrode the user-selected ' + target.label + ' tab');
                ensure(ui.selected === selected && ui.renderedReviewId === selected, 'Startup status replaced the selected case');
                ensure([...doc.querySelectorAll('nav button')].some(node => node.textContent.startsWith(target.label + ' (')
                  && node.getAttribute('aria-pressed') === 'true'), 'The visible selected tab does not match user navigation');
                ensure(JSON.stringify([...ui.reviewDrafts]) === drafts, 'Startup status replaced the saved draft');
                ensure(requests.length === 1 && requests[0].method === 'status', 'Startup browsing did more than read status');
                response.cases.push({tab: target.tab, selected, draftRetained: true, requests});
              }
              response.passed = ['early Library Issues selection survives initial status', 'early Sync Conflicts selection survives initial status',
                'startup refresh retains the selected case and draft', 'startup browsing only requests status'];
            } finally {
              if (release) release();
              if (mounting) { try { await mounting; } catch (_) {} }
              if (ui.view && !ui.view.closed) ui.view.close();
              ui.view = null; ui.rpc = saved.rpc; ui.latest = saved.latest; ui.tab = saved.tab; ui.selected = saved.selected;
              ui.reviewDrafts = saved.drafts; ui.queueSelections = saved.selections; ui.requestedTab = saved.requested;
              mounting = null;
              ui.mount = function(window) { mounting = Promise.resolve(saved.mount.call(this, window)); return mounting; };
              try {
                ui.open(); await waitFor(() => !!mounting, 'The ordinary workspace did not reopen after the startup check'); await mounting;
              } finally { ui.mount = saved.mount; }
            }
          }
          else if (command.action === 'queue_workflow') {
            const saved = {latest: ui.latest, tab: ui.tab, selected: ui.selected, rpc: ui.rpc, drafts: ui.reviewDrafts, queue: ui.filingQueue};
            const ensure = (ok, why) => { if (!ok) throw Error(why); };
            const clone = value => JSON.parse(JSON.stringify(value));
            let state = clone(command.snapshot), calls = [];
            const first = state.reviews[0];
            const incoming = clone(first); incoming.id = 'incoming'; incoming.kind = 'inbox'; incoming.payload.findings = []; incoming.payload.title = 'New intake paper';
            incoming.payload.matches = [{key: 'existing-tablet', title: 'Existing tablet copy'}];
            state.reviews.unshift(incoming);
            ui.latest = clone(state); ui.tab = 'inbox'; ui.selected = incoming.id; ui.reviewDrafts = new Map(); ui.filingQueue = [];
            ui.rpc = async (method, params) => {
              calls.push({method, params}); ensure(method === 'recheck_review', 'Unexpected write operation');
              ensure(JSON.stringify(params.identities) === JSON.stringify(state.reviews.slice(1).map(row => row.id)), 'Recheck did not target this exact case');
              for (const row of state.reviews) row.payload.findings = [];
              return {...clone(state), review_recheck: {checked: state.reviews.length - 1, changed_ids: [], unavailable_ids: [], cloud_checked: false}};
            };
            try {
              ui.render(); await Zotero.Promise.delay(100);
              const doc = ui.view.document;
              const buttons = () => [...doc.querySelectorAll('button')];
              const click = label => { const b = buttons().find(b => b.textContent === label || b.textContent.startsWith(label + ' (')); ensure(b, 'Missing ' + label); b.click(); };
              ensure(ui.queueRows('inbox').length === 1 && ui.queueRows('inbox-waiting').length === 35, 'Ready and waiting Inbox papers are mixed');
              ensure(doc.querySelectorAll('.review-layout > aside > button').length === 1, 'Waiting papers appear in the ready list');
              ensure(doc.querySelector('.inbox-waiting')?.open === false, 'Waiting papers should start collapsed');
              ensure(doc.querySelector('[data-queue-summary]').textContent === '1 ready to file · 35 waiting on Library Issues', 'Inbox counts are not scoped');
              ensure(!doc.getElementById('notable-paper-status').textContent.includes('need review'), 'Global review counter still appears');
              const filename = [...doc.querySelectorAll('label')].find(label => label.firstElementChild?.textContent === 'Proposed filename').querySelector('input');
              filename.value = 'My retained filename.pdf'; filename.dispatchEvent(new ui.view.Event('input'));
              const match = doc.querySelector('select option[value="existing-tablet"]').parentElement;
              match.value = ''; match.dispatchEvent(new ui.view.Event('change'));
              ensure(buttons().find(b => b.textContent === 'Approve & file')?.dataset.buttonHelp.includes('filename, tags, and filing'), 'Separate-copy action help is stale');
              match.value = 'existing-tablet'; match.dispatchEvent(new ui.view.Event('change'));
              ensure(buttons().find(b => b.textContent === 'Link to existing reMarkable copy')?.dataset.buttonHelp.includes('Do not upload a second copy'), 'Existing-copy action help is stale');
              const preview = buttons().find(b => b.textContent === 'Suggest filename');
              preview.dispatchEvent(new ui.view.PointerEvent('pointerenter', {pointerType: 'mouse'}));
              ensure(!doc.querySelector('[role="tooltip"]'), 'Hover tooltip appeared immediately');
              preview.dispatchEvent(new ui.view.PointerEvent('pointerleave', {pointerType: 'mouse'}));
              await Zotero.Promise.delay(550);
              ensure(!doc.querySelector('[role="tooltip"]'), 'Tooltip appeared after pointer left');
              preview.dispatchEvent(new ui.view.PointerEvent('pointerenter', {pointerType: 'mouse'}));
              await Zotero.Promise.delay(550);
              ensure(/no file is renamed/i.test(doc.querySelector('[role="tooltip"]')?.textContent || ''), 'Tooltip missing after hovering');
              preview.dispatchEvent(new ui.view.PointerEvent('pointerleave', {pointerType: 'mouse'}));
              preview.focus({preventScroll: true});
              preview.dispatchEvent(new ui.view.KeyboardEvent('keydown', {key: 'F1', bubbles: true, cancelable: true}));
              ensure(/no file is renamed/i.test(doc.querySelector('[role="tooltip"]')?.textContent || ''), 'Keyboard tooltip missing');
              preview.dispatchEvent(new ui.view.KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
              ensure(!doc.querySelector('[role="tooltip"]') && !preview.hasAttribute('aria-describedby'), 'Escape did not dismiss tooltip cleanly');
              ensure(buttons().every(b => b.dataset.buttonHelp && b.getAttribute('aria-description')), 'A button has no accessible help');
              doc.querySelector('.inbox-waiting').open = true; await Zotero.Promise.delay(50);
              const waitingButtons = [...doc.querySelectorAll('.inbox-waiting button')];
              ensure(waitingButtons.length === 35, 'Waiting papers are not accessible');
              waitingButtons.at(-1).click(); await Zotero.Promise.delay(100);
              ensure(ui.tab === 'library' && ui.selected === state.reviews[1].id, 'Waiting navigation did not select the grouped case representative');
              ensure(ui.queueRows('library').length === 1, 'Repeated exact participants were not grouped');
              ensure(!doc.querySelector('input[data-notable-field="Proposed filename"]') && !buttons().some(b => /Approve|Add to Zotero/.test(b.textContent)), 'Library Issues has filing controls');
              ensure(buttons().some(b => b.textContent === 'Compare in Zotero'), 'Library comparison navigation missing');
              click('Inbox'); await Zotero.Promise.delay(100);
              ensure(ui.selected === incoming.id && [...doc.querySelectorAll('input')].some(input => input.value === 'My retained filename.pdf'), 'Switching queues lost the draft or selection');
              click('Library Issues'); await Zotero.Promise.delay(100); click('Check my changes');
              const deadline = Date.now() + 5000;
              while ((ui.busy || !calls.length) && Date.now() < deadline) await Zotero.Promise.delay(20);
              ensure(calls.length === 1 && !ui.queueRows('library').length, 'Resolved library case did not leave the queue');
              ensure(ui.queueRows('inbox').length === 36 && ui.queueRows('inbox-waiting').length === 0, 'Resolved papers did not become ready in Inbox');
              click('Inbox'); await Zotero.Promise.delay(100);
              ensure(!doc.querySelector('.inbox-waiting'), 'Empty waiting group remains after resolution');
              ensure([...doc.querySelectorAll('input')].some(input => input.value === 'My retained filename.pdf'), 'Resolving waiting papers lost the existing draft');
              ensure(ui.latest.reviews.every(row => row.status === 'pending'), 'Library confirmation approved a paper');
              response.passed = ['three queues separate filing from library decisions', 'identical participant sets form one case',
                'keyboard tooltips explain actions and dismiss with Escape', 'all rendered buttons have accessible descriptions',
                'queue switching retains filename draft and selection', 'case confirmation rechecks only its participants',
                'resolved case leaves Library Issues without approving papers', 'blocked papers wait outside the ready list',
                'waiting navigation selects its exact grouped case', 'resolved papers return to Inbox with drafts retained'];
              response.requests = calls;
            } finally { ui.rpc = saved.rpc; ui.latest = saved.latest; ui.tab = saved.tab; ui.selected = saved.selected; ui.reviewDrafts = saved.drafts; ui.filingQueue = saved.queue; ui.render(); }
          }
          else if (command.action === 'review_workflow') {
            const saved = {rpc: ui.rpc, latest: ui.latest, queue: ui.filingQueue, drafts: ui.reviewDrafts,
              drain: ui.drainFilings, selected: ui.selected, stale: ui.reviewStale, recheck: ui.reviewRecheck};
            const ensure = (value, message) => { if (!value) throw Error(message); };
            const clone = value => JSON.parse(JSON.stringify(value));
            let state = clone(command.snapshot), calls = [], drains = 0;
            ui.latest = clone(state); ui.tab = 'review'; ui.selected = state.reviews[0].id;
            ui.reviewDrafts = new Map();
            ui.filingQueue = [{id: 'synthetic-queued', title: 'Unrelated queued paper', connection: ui.filingConnection(), method: 'approve', params: {identity: 'not-in-fixture'}}];
            ui.drainFilings = async () => { drains++; };
            ui.rpc = async (method, params) => {
              calls.push({method, params: clone(params || {})});
              ensure([...ui.view.document.querySelectorAll('button[data-queue]')].every(button => button.disabled), 'Approvals remained enabled during local evidence check');
              if (!['scan_inbox', 'recheck_review', 'acknowledge_review_finding'].includes(method)) throw Error('Unexpected worker operation: ' + method);
              if (method === 'scan_inbox') return clone(state);
              if (method === 'acknowledge_review_finding') {
                for (const row of state.reviews) for (const finding of row.payload.findings) finding.acknowledgement.acknowledged = !!params.keep_separate;
              }
              for (const row of state.reviews) row.payload.review_token = 'fresh-' + calls.length;
              return {...clone(state), review_recheck: {checked: state.reviews.length, changed_ids: [state.reviews[0].id], unavailable_ids: [], cloud_checked: false}};
            };
            try {
              ui.view.resizeTo(640, 850); ui.render(); await Zotero.Promise.delay(150);
              let doc = ui.view.document;
              const filename = [...doc.querySelectorAll('label')].find(label => label.firstElementChild?.textContent === 'Proposed filename').querySelector('input');
              filename.value = 'My unsaved native name.pdf'; filename.dispatchEvent(new ui.view.Event('input'));
              doc.querySelector('details[data-review-detail]').open = true;
              const list = doc.querySelector('.review-layout > aside'); list.scrollTop = 110;
              doc.querySelector('body').scrollTop = 330; await Zotero.Promise.delay(100);
              const before = {outer: doc.querySelector('body').scrollTop, inner: list.scrollTop, selected: ui.selected};
              ui.observeReviewChange('modify', 'item');
              ensure(filename.isConnected && filename.value === 'My unsaved native name.pdf', 'Native edit replaced live draft');
              ensure(doc.getElementById('notable-review-freshness').textContent.includes('Zotero changed'), 'Native edit did not flag stale guidance');
              const click = async label => {
                const button = [...ui.view.document.querySelectorAll('button')].find(value => value.textContent === label);
                ensure(button, 'Missing button: ' + label); const count = calls.length; button.click();
                const deadline = Date.now() + 10000;
                while ((ui.busy || calls.length === count) && Date.now() < deadline) await Zotero.Promise.delay(20);
                ensure(!ui.busy && calls.length > count, 'Local operation did not finish'); await Zotero.Promise.delay(100);
              };
              await click('Check Inbox'); doc = ui.view.document;
              ensure(doc.getElementById('message').textContent === 'Inbox updated', 'Inbox check did not show one outcome');
              ensure(!doc.getElementById('notable-review-freshness').textContent, 'Inbox repeated the review result');
              ensure([...doc.querySelectorAll('input')].some(node => node.value === 'My unsaved native name.pdf'), 'Recheck discarded unsaved filename');
              ensure(ui.selected === before.selected, 'Recheck moved selected paper');
              ensure(doc.querySelector('details[data-review-detail]').open, 'Recheck collapsed evidence');
              ensure(Math.abs(doc.querySelector('body').scrollTop - before.outer) <= 1, 'Recheck moved outer scroll');
              ensure(Math.abs(doc.querySelector('.review-layout > aside').scrollTop - before.inner) <= 1, 'Recheck moved PDF list scroll');
              ensure(doc.querySelector('.review-draft-notice'), 'Changed evidence did not identify retained draft');
              ensure(drains === 0 && ui.filingQueue.length === 1, 'Recheck resumed a queued external filing');
              const keep = [...doc.querySelectorAll('button')].find(button => /^Keep these (references|PDFs) separate$/.test(button.textContent));
              ensure(keep, 'Missing the scoped keep-separate decision'); await click(keep.textContent);
              ensure(drains === 0 && ui.filingQueue.length === 1, 'Acknowledgement resumed a queued external filing');
              ensure(ui.latest.reviews.every(row => row.status === 'pending'), 'Acknowledgement approved a paper');
              ensure(ui.view.document.querySelector('body').textContent.includes('Kept separate'), 'Acknowledgement not shown');
              ensure(calls.map(value => value.method).join(',') === 'recheck_review,scan_inbox,acknowledge_review_finding', 'Unexpected worker request');
              response.passed = ['native edit marks stale without replacing draft', 'local recheck keeps selection and filename draft',
                'local recheck keeps expanded evidence and both scroll positions', 'changed evidence labels retained draft',
                'recheck and acknowledgement never drain queued filings', 'acknowledgement leaves papers pending'];
              response.requests = calls;
            } finally {
              ui.rpc = saved.rpc; ui.latest = saved.latest; ui.filingQueue = saved.queue; ui.reviewDrafts = saved.drafts;
              ui.drainFilings = saved.drain; ui.selected = saved.selected; ui.reviewStale = saved.stale; ui.reviewRecheck = saved.recheck; ui.render();
            }
          }
          else if (command.action === 'layout') {
            ui.busy = !!command.operation; ui.operation = command.operation || null;
            ui.changingFolder = !!command.editFolder; ui.setupDirty = false; ui.setupDraft = null;
            ui.folderPreview = command.folderPreview || null; ui.disconnectPreview = command.disconnectPreview || null;
            if (command.snapshot) ui.latest = command.snapshot;
            if (command.folders) ui.folderCatalog = command.folders;
            if (command.tagEditor) ui.tagEditor = command.tagEditor;
            ui.reviewStale = !!command.reviewStale;
            ui.tab = command.tab || 'setup'; ui.render();
            if (command.expandDetails) for (const detail of ui.view.document.querySelectorAll('details')) detail.open = true;
            ui.view.resizeTo(command.width || 1040, command.height || 760);
            ui.view.document.documentElement.style.colorScheme = command.theme || 'light';
            await Zotero.Promise.delay(300);
            const doc = ui.view.document, rect = node => {
              const r = node.getBoundingClientRect();
              return {x: r.x, y: r.y, width: r.width, height: r.height, right: r.right, bottom: r.bottom};
            };
            response.layout = {
              width: ui.view.innerWidth, scrollWidth: doc.documentElement.scrollWidth,
              root: doc.documentElement.localName,
              linkBadges: [...doc.querySelectorAll('.review-link-badge')].filter(node => node.getClientRects().length).map(node => node.textContent),
              queueSummary: doc.querySelector('[data-queue-summary]')?.textContent,
              waitingOpen: doc.querySelector('.inbox-waiting')?.open || false,
              waitingCount: doc.querySelectorAll('.waiting-papers > li').length,
              readyListCount: doc.querySelectorAll('.review-layout > aside > button').length,
              paperStatus: doc.getElementById('notable-paper-status')?.textContent,
              reviewTitle: doc.querySelector('.review-form > h2')?.textContent,
              fieldLabels: [...doc.querySelectorAll('label.field')].map(label => label.firstElementChild?.textContent),
              filingExpanded: doc.querySelector('details[data-review-detail="filing"]')?.open,
              evidenceExpanded: doc.querySelector('details[data-review-detail="finding-details"]')?.open,
              reviewHeadings: [...doc.querySelectorAll('.review-findings > h3')].map(node => node.textContent),
              whySummaries: [...doc.querySelectorAll('details[data-review-detail="finding-details"] > summary')].map(node => node.textContent),
              nestedEvidence: doc.querySelectorAll('details[data-review-detail="finding-details"] details').length,
              repeatedScenarioCopy: doc.querySelectorAll('.review-why, .review-action').length,
              referenceTypes: [...doc.querySelectorAll('.review-item-type')].map(node => node.textContent),
              mergeHints: [...doc.querySelectorAll('.review-merge-hint')].map(node => node.textContent),
              mergeAdvice: [...doc.querySelectorAll('.review-merge-advice')].map(node => node.textContent).join(' '),
              nativeTypeLabels: {journalArticle: Zotero.ItemTypes.getLocalizedString('journalArticle'), standard: Zotero.ItemTypes.getLocalizedString('standard')},
              buttons: [...doc.querySelectorAll('button')].filter(b => b.getClientRects().length).map(b => {
                const icon = b.querySelector('.button-icon');
                return {text: b.textContent, ...rect(b), icon: !!icon, iconWidth: icon?.getBoundingClientRect().width || 0,
                  iconColor: icon ? ui.view.getComputedStyle(icon).color : '', buttonColor: ui.view.getComputedStyle(b).color,
                  attention: b.classList.contains('sync-conflicts-attention'),
                  insideExplanation: !!b.closest('details[data-review-detail="finding-details"]'),
                  help: b.dataset.buttonHelp || b.getAttribute('aria-description') || b.title || '',
                  iconMask: icon ? ui.view.getComputedStyle(icon).maskImage : 'none'};
              }),
              checkboxDisplays: [...doc.querySelectorAll('.checkbox-field')].map(n => ui.view.getComputedStyle(n).display),
              pairingWidth: doc.querySelector('.pairing-code')?.getBoundingClientRect().width,
              selects: [...doc.querySelectorAll('select')].map(s => ({...rect(s), appearance: ui.view.getComputedStyle(s).appearance, multiple: s.multiple, size: s.size, options: s.options.length,
                optionRects: [...s.options].slice(0, 4).map(o => ({text: o.textContent, ...rect(o), display: ui.view.getComputedStyle(o).display}))}))
            };
          }
          else if (command.action === 'picker') {
            const win = ui.view, doc = win.document;
            const select = doc.querySelectorAll('select')[command.index || 0];
            select.scrollIntoView({block: 'center'}); select.focus();
            const r = select.getBoundingClientRect();
            win.windowUtils.sendMouseEvent('mousedown', r.right - 16, r.y + r.height / 2, 0, 1, 0);
            win.windowUtils.sendMouseEvent('mouseup', r.right - 16, r.y + r.height / 2, 0, 1, 0);
            await Zotero.Promise.delay(300);
            response.popups = [...doc.querySelectorAll('menupopup')].map(p => ({state: p.state, background: win.getComputedStyle(p).backgroundColor,
              appearance: win.getComputedStyle(p).appearance, items: p.children.length, width: p.getBoundingClientRect().width, height: p.getBoundingClientRect().height}));
            response.valueBefore = select.value;
            if (command.choose !== undefined) {
              const popup = [...doc.querySelectorAll('menupopup')].find(p => p.state === 'open');
              const item = popup?.querySelectorAll('menuitem')[command.choose];
              if (!item) throw new Error('No open native option menu');
              item.click();
              await Zotero.Promise.delay(100);
            }
            if (command.close !== false) for (const p of doc.querySelectorAll('menupopup')) p.hidePopup();
            response.valueAfter = select.value;
          }
          else if (command.action === 'upgrade') {
            if (command.packaged && Services.prefs.prefHasUserValue('notable.developmentWorker')) Services.prefs.clearUserPref('notable.developmentWorker');
            const {AddonManager} = ChromeUtils.importESModule('resource://gre/modules/AddonManager.sys.mjs');
            const file = Cc['@mozilla.org/file/local;1'].createInstance(Ci.nsIFile); file.initWithPath(command.path);
            const install = await AddonManager.getInstallForFile(file);
            await install.install();
            const addon = await AddonManager.getAddonByID('notable-companion@local.invalid');
            response.addon = {id: addon.id, name: addon.name, version: addon.version};
          }
          else if (command.action === 'folder_ui') {
            const original = ui.rpc, saved = {latest: ui.latest, folders: ui.folderCatalog};
            const oldRoot = '11111111-1111-4111-8111-111111111111', newRoot = '22222222-2222-4222-8222-222222222222';
            let state = {config: {zotero_root: 'CNCT2345', tablet_root: oldRoot, default_folder: oldRoot,
              zotero_path: 'Tablet papers', tablet_path: 'Previous folder', automatic: true},
              status: {}, types: {}, reviews: [], links: [], mappings: {}, collections: [{key: 'CNCT2345', name: 'Tablet papers', parentCollection: false}],
              connection: {linked_papers: 3, previous: []}};
            response.requests = [];
            ui.rpc = async (method, params = {}) => {
              response.requests.push({method, params});
              if (method === 'pause_sync') { state.config.sync_paused = true; return JSON.parse(JSON.stringify(state)); }
              if (method === 'status') return JSON.parse(JSON.stringify(state));
              if (method === 'preview_connection') return {token: 'preview', proposed: {...params, zotero_path: 'Tablet papers', tablet_path: 'Literature'},
                linked_papers: 3, pending_reviews: 1, restored_papers: 0, zotero_folders: [], tablet_folders: [], excluded_zotero_folders: ['Old topic'], excluded_tablet_folders: []};
              if (method === 'configure') { state.config = {...params, zotero_path: 'Tablet papers', tablet_path: 'Literature', automatic: false, sync_paused: true}; return JSON.parse(JSON.stringify(state)); }
              if (method === 'preview_disconnect') return {token: 'disconnect', linked_papers: 3, pending_reviews: 1};
              if (method === 'disconnect_folder') { state.config.tablet_root = ''; state.config.automatic = false; return JSON.parse(JSON.stringify(state)); }
              throw new Error('Unexpected UI request: ' + method);
            };
            const until = Date.now() + 15000;
            const waitFor = async condition => { while (!condition() && Date.now() < until) await Zotero.Promise.delay(30); if (!condition()) throw new Error('Folder UI did not settle'); };
            const click = label => { const button = [...ui.view.document.querySelectorAll('button')].find(b => b.textContent === label); if (!button || button.disabled) throw new Error('Unavailable button: ' + label); button.click(); };
            try {
              ui.clearFolderEditor(); ui.latest = JSON.parse(JSON.stringify(state)); ui.tab = 'setup';
              ui.folderCatalog = {collections: state.collections, folders: [{id: oldRoot, name: 'Previous folder', parent: ''}, {id: newRoot, name: 'Literature', parent: ''}]}; ui.render();
              click('Change folder'); await waitFor(() => !ui.pausing && ui.view.document.querySelectorAll('main select').length === 3);
              const select = ui.view.document.querySelectorAll('main select')[1]; select.value = newRoot;
              select.dispatchEvent(new ui.view.Event('change', {bubbles: true})); select.dispatchEvent(new ui.view.Event('input', {bubbles: true}));
              if (!ui.view.document.getElementById('notable-sync').disabled) throw new Error('Sync enabled during unsaved change');
              click('Review folder change'); await waitFor(() => !!ui.folderPreview && !ui.busy);
              response.previewShown = ui.view.document.querySelector('main').textContent.includes('Old Zotero folders kept out');
              if (response.requests.some(r => ['configure', 'sync'].includes(r.method))) throw new Error('Preview changed a connection');
              click('Save folder change'); await waitFor(() => !ui.changingFolder && !ui.busy);
              response.savedWithoutSync = !response.requests.some(r => r.method === 'sync') && ui.latest.config.tablet_root === newRoot;
              click('Disconnect folder'); await waitFor(() => !!ui.disconnectPreview && !ui.busy && !ui.pausing);
              click('Cancel'); await waitFor(() => !ui.disconnectPreview);
              if (!ui.latest.config.tablet_root) throw new Error('Cancel disconnected the folder');
              click('Disconnect folder'); await waitFor(() => !!ui.disconnectPreview && !ui.busy && !ui.pausing);
              click('Confirm disconnect'); await waitFor(() => !ui.disconnectPreview && !ui.busy);
              response.disconnected = !ui.latest.config.tablet_root && ui.view.document.getElementById('notable-sync').disabled;
              const entry = Zotero.getMainWindow().document.getElementById('notable-open');
              response.toolbar = {label: entry.getAttribute('label'), tooltip: entry.getAttribute('tooltiptext'), image: entry.style.listStyleImage};
              if (!response.toolbar.image.includes('tablet.svg') || !response.toolbar.tooltip.startsWith('Open reMarkable')) throw new Error('Ambiguous toolbar entry');
            } finally { ui.rpc = original; ui.clearFolderEditor(); ui.latest = saved.latest; ui.folderCatalog = saved.folders; ui.tab = 'setup'; ui.render(); }
          }
          else if (command.action === 'filing_ui') {
            const saved = {latest: ui.latest, tab: ui.tab, rpc: ui.rpc, queue: ui.filingQueue, paused: ui.filingQueuePaused};
            const requests = []; let release, job;
            const gate = new Promise(resolve => { release = resolve; });
            let state = JSON.parse(JSON.stringify(command.snapshot));
            const ensure = (value, reason) => { if (!value) throw Error(reason); };
            const waitFor = async check => { const until = Date.now() + 15000; while (!check() && Date.now() < until) await Zotero.Promise.delay(30); ensure(check(), 'Filing UI did not settle'); };
            const field = label => [...ui.view.document.querySelectorAll('label.field')].find(node => node.firstElementChild.textContent === label)?.querySelector('input, select');
            const edit = (label, value) => { const input = field(label); ensure(input && !input.disabled, 'Field locked: ' + label); input.value = value; input.dispatchEvent(new ui.view.Event('input')); input.dispatchEvent(new ui.view.Event('change')); };
            try {
              ui.filingQueue = []; ui.filingQueuePaused = false; ui.queueError = ''; ui.reviewDrafts.clear(); ui.selected = null;
              ui.latest = state; ui.tab = 'review'; ui.render();
              await waitFor(() => [...(field('Main paper in Zotero (required)')?.options || [])].some(option => option.value === command.mainKey));
              ensure(field('Document type').value === 'supporting_information', 'Supporting information was not selected');
              edit('Main paper in Zotero (required)', command.mainKey);
              edit('Title', 'Reviewed supplement title'); edit('Proposed filename', 'Supplement_SI.pdf');
              ui.rpc = async (method, params) => {
                requests.push({method, params});
                if (method === 'approve_routine') await gate;
                if (method === 'approve') state.reviews = state.reviews.filter(row => row.id !== params.identity);
                return JSON.parse(JSON.stringify(state));
              };
              job = ui.action('approve_routine', {});
              const approve = [...ui.view.document.querySelectorAll('button')].find(button => button.textContent === 'Add to filing queue');
              ensure(approve && !approve.disabled, 'Cannot queue during a batch'); approve.click();
              await waitFor(() => ui.filingQueue.length === 1); await ui.queueSave;
              const queued = JSON.parse(await IOUtils.readUTF8(PathUtils.join(ui.statePath, 'filing-queue.json'))).jobs[0];
              ensure(queued.params.main_reference === command.mainKey && queued.params.filename === 'Supplement_SI.pdf', 'Queued choices were not saved');
              ensure(queued.params.main_reference_metadata.title, 'Main citation snapshot missing');
              ensure(requests.length === 1, 'Approval ran concurrently with batch');
              edit('Title', 'Keep this next-paper draft');
              release(); await job; job = null;
              await waitFor(() => !ui.drainingFilings && !ui.busy && !ui.filingQueue.length);
              ensure(requests.map(row => row.method).join(',') === 'approve_routine,approve', 'Filing must not start a whole-library sync');
              ensure(field('Title').value === 'Keep this next-paper draft', 'Background completion lost the next draft');
              response.passed = ['SI choice and native main-paper search', 'editable review during filing', 'durable queued choices', 'one writer at a time', 'main citation snapshot', 'draft retained through filing', 'filing does not start whole-library sync'];
            } finally {
              release(); if (job) await job;
              ui.rpc = saved.rpc; ui.latest = saved.latest; ui.tab = saved.tab;
              ui.filingQueue = saved.queue; ui.filingQueuePaused = saved.paused; ui.queueError = ''; await ui.saveFilingQueue(); ui.render();
            }
          }
          else if (command.action === 'activity_ui') {
            const saved = {latest: ui.latest, tab: ui.tab, rpc: ui.rpc, launch: Zotero.launchFile,
              selected: ui.selected, all: ui.showAllReviews};
            const requests = [], launches = [];
            let release;
            const gate = new Promise(resolve => { release = resolve; });
            let job;
            const ensure = (condition, text) => { if (!condition) throw new Error(text); };
            const click = text => {
              const button = [...ui.view.document.querySelectorAll('button')].find(b => b.textContent === text || b.textContent.startsWith(text + ' ('));
              ensure(button && !button.disabled, 'Unavailable during work: ' + text); button.click();
            };
            const settle = () => Zotero.Promise.delay(100);
            try {
              ui.latest = command.snapshot; ui.tab = 'import'; ui.showAllReviews = false; ui.render();
              ui.schedule(); ensure(!!ui.timer && ui.automaticAllowed(), 'Open workspace suppressed automatic sync');
              ui.rpc = async method => { requests.push(method); if (method === 'scan_library') return gate; if (method === 'status') return command.snapshot; throw new Error('Unexpected request: ' + method); };
              Zotero.launchFile = file => { launches.push(file.path); };
              job = ui.action('scan_library', {root: command.source}); await settle();
              ui.message('Scanning file 12 of 537: Example paper.pdf'); ui.operation.started -= 65000; ui.updateActivity();
              const doc = ui.view.document;
              ensure(!doc.getElementById('activity').hidden, 'No working indicator');
              ensure(doc.getElementById('activity-progress').value === 12 && doc.getElementById('activity-progress').max === 537, 'Progress count missing');
              ensure(doc.getElementById('activity-time').textContent.includes('1 min'), 'Elapsed time missing');
              ensure([...doc.querySelectorAll('nav button')].every(b => !b.disabled), 'Navigation locked during scan');
              click('Inbox'); await settle();
              ensure(ui.busy && ui.tab === 'inbox', 'Browsing interrupted the job');
              ensure(![...doc.querySelectorAll('button')].find(b => b.textContent === 'Add to filing queue').disabled, 'Cannot queue a review during scan');
              ensure([...doc.querySelectorAll('.review-form input, .review-form select')].every(field => !field.disabled), 'Review fields locked during work');
              click('Open paper'); await settle();
              ensure(launches.length === 1 && launches[0] === command.source, 'Open paper did not use the saved local file');
              ensure(requests.length === 1, 'Opening a paper queued behind the worker or changed a library');
              ensure(doc.querySelectorAll('aside button').length === 2, 'Inbox must show both routine and individual papers');
              release(command.snapshot); await job; job = null;
              ensure(doc.getElementById('activity').hidden && !ui.busy, 'Working indicator stuck after completion');
              const syncing = new Promise(resolve => { release = resolve; });
              ui.rpc = async method => { ensure(method === 'sync', 'Unexpected request: ' + method); return syncing; };
              job = ui.action('sync', {automatic: true}, false); await settle();
              ensure(doc.getElementById('activity').hidden, 'Sync displayed the spinner panel');
              const details = doc.getElementById('notable-sync-details');
              ensure(!details.hidden && !details.open, 'Sync details expanded automatically');
              ensure(doc.getElementById('notable-sync-status').textContent.includes('Syncing with reMarkable'), 'No inline sync status');
              details.open = true; ui.receiveProgress({message: 'Receiving paper 2 of 3'});
              ensure(doc.getElementById('notable-sync-step').textContent === 'Receiving paper 2 of 3', 'Expanded sync details lack current step');
              click('Linked papers'); await settle();
              const filter = [...doc.querySelectorAll('input')].find(input => !input.disabled);
              ensure(ui.busy && ui.tab === 'links' && filter, 'Sync blocked local navigation or filtering');
              release(command.snapshot); await job; job = null;
              ensure(doc.getElementById('notable-sync-details').hidden && !ui.busy, 'Sync status remained active after completion');
              response.passed = ['automatic checks can run with workspace open', 'navigation stays available during work',
                'visible scan progress and elapsed time', 'local paper opens without approval or import',
                'routine papers can be browsed', 'sync details stay collapsed until requested', 'local filtering stays available during sync'];
            } finally {
              release(command.snapshot); if (job) await job;
              ui.rpc = saved.rpc; Zotero.launchFile = saved.launch;
              ui.busy = false; ui.operation = null; ui.latest = saved.latest; ui.tab = saved.tab;
              ui.selected = saved.selected; ui.showAllReviews = saved.all; ui.render(); ui.schedule();
            }
          }
          else if (command.action === 'review_list') {
            const saved = {latest: ui.latest, tab: ui.tab, rpc: ui.rpc};
            const ensure = (value, reason) => { if (!value) throw new Error(reason); };
            const settle = () => Zotero.Promise.delay(200);
            try {
              ui.latest = command.snapshot; ui.tab = 'review'; ui.selected = null; ui.showAllReviews = true; ui.reviewScrollTop = 0;
              ui.render(); ui.view.resizeTo(1040, 900); await settle();
              const doc = ui.view.document;
              const sizes = () => { const list = doc.querySelector('.review-layout > aside'), detail = list.nextElementSibling;
                return {list: list.getBoundingClientRect().height, detail: detail.getBoundingClientRect().height,
                  scroll: list.scrollHeight, visible: list.clientHeight}; };
              response.before = sizes();
              ensure(Math.abs(response.before.list - response.before.detail) < 2, 'List does not match detail height');
              ensure(response.before.scroll > response.before.visible * 2, 'Long list does not scroll independently');
              const pageTop = ui.view.scrollY;
              let button = doc.querySelector('aside button');
              button.dispatchEvent(new ui.view.KeyboardEvent('keydown', {key: 'End', bubbles: true})); await settle();
              ensure(ui.selected === command.snapshot.reviews.at(-1).id, 'End key did not select the last paper');
              let list = doc.querySelector('aside');
              ensure(list.scrollTop > 0 && Math.abs(ui.view.scrollY - pageTop) < 2, 'Selecting a paper scrolled the whole page');
              const selected = list.querySelector('[aria-pressed="true"]'), selectedBox = selected.getBoundingClientRect(), listBox = list.getBoundingClientRect();
              ensure(selectedBox.top >= listBox.top - 1 && selectedBox.bottom <= listBox.bottom + 1, 'Selected paper not visible');
              const position = list.scrollTop;
              ui.render(); await settle();
              ensure(Math.abs(doc.querySelector('aside').scrollTop - position) < 2, 'List position reset during render');
              doc.querySelector('.paper-tag-proposal').open = true; await settle();
              response.expanded = sizes();
              ensure(Math.abs(response.expanded.list - response.expanded.detail) < 2, 'List failed to follow expanded details');
              const requests = [];
              ui.rpc = async (method, params) => {
                requests.push({method, params});
                if (method === 'suggest_paper_tags') return {proposal: command.proposal};
                if (method === 'save_paper_tags') return {saved: true, deferred_until_filing: true};
                if (method === 'status') return command.snapshot;
                throw new Error('Unexpected tag UI request');
              };
              [...doc.querySelectorAll('button')].find(b => b.textContent === 'Find keywords and suggest tags').click(); await settle();
              const checks = [...doc.querySelector('.paper-tag-proposal').querySelectorAll('input[type="checkbox"]')];
              ensure(checks.length && checks.every(input => !input.checked), 'Suggestions applied without selection');
              checks[0].checked = true;
              [...doc.querySelectorAll('button')].find(b => b.textContent === 'Use these tags when filing').click(); await settle();
              ensure(requests.find(row => row.method === 'save_paper_tags')?.params.selected.length === 1, 'Tag selection not respected');
              response.tags = sizes(); ensure(Math.abs(response.tags.list - response.tags.detail) < 2, 'Tag controls broke matching heights');
              ui.view.resizeTo(640, 900); await settle();
              const narrow = sizes();
              ensure(narrow.list <= ui.view.innerHeight * .36 + 2 && narrow.scroll > narrow.visible, 'Narrow list is unbounded');
              response.narrow = narrow;
              const icon = await (await fetch('chrome://notable/content/icons/tablet.svg')).text();
              ensure(icon.includes('M9 16V8') && !icon.includes('<line'), 'Toolbar icon still contains a dot');
              response.passed = ['review list matches details height', 'long list scrolls independently', 'keyboard selection stays visible',
                'list position persists', 'expanded details and tag controls resize list', 'suggested tags require a selection',
                'narrow list remains bounded', 'toolbar uses rectangle and R without dot'];
            } finally { ui.rpc = saved.rpc; ui.latest = saved.latest; ui.tab = saved.tab; ui.selected = null; ui.showAllReviews = false; ui.view.resizeTo(1040, 760); ui.render(); }
          }
          else if (command.action === 'review_scroll') {
            const saved = {latest: ui.latest, tab: ui.tab, selected: ui.selected, showAll: ui.showAllReviews};
            const ensure = (value, reason) => { if (!value) throw new Error(reason); };
            response.cases = [];
            try {
              ui.latest = command.snapshot; ui.tab = 'review'; ui.selected = null; ui.showAllReviews = true;
              ui.render();
              const win = ui.view, doc = win.document, list = doc.querySelector('.review-layout > aside');
              // Zotero's XUL workspace scrolls its HTML body, not window.scrollY.
              const outer = doc.querySelector('body');
              const metrics = () => ({list: list.scrollTop, outer: outer.scrollTop,
                maxList: list.scrollHeight - list.clientHeight,
                maxOuter: outer.scrollHeight - outer.clientHeight});
              for (const width of [1040, 640]) {
                win.resizeTo(width, 760); await Zotero.Promise.delay(300);
                const style = win.getComputedStyle(list), size = list.getBoundingClientRect();
                ensure(style.overflowY === 'auto' && style.overscrollBehaviorY === 'auto', 'List must allow vertical scroll chaining');
                ensure(style.scrollbarGutter === 'stable', 'Scrollbar gutter changed');
                ensure(doc.documentElement.scrollWidth <= win.innerWidth + 1, 'Horizontal overflow at width ' + width);
                if (win.innerWidth > 700) ensure(style.contain.includes('size'), 'Wide list lost layout containment');
                else ensure(size.height <= win.innerHeight * .36 + 2, 'Narrow list exceeds its existing height limit');
                for (const mode of ['line', 'pixel']) {
                  for (const scenario of ['inside-up', 'inside-down', 'top-up', 'bottom-down']) {
                    // End the previous wheel transaction before changing its target's scroll position.
                    await Zotero.Promise.delay(1700);
                    const limits = metrics();
                    ensure(limits.maxList > 600 && (win.innerWidth > 700 || limits.maxOuter > 180), 'Fixture needs a scrollable list and narrow-page fallback: ' + JSON.stringify(limits));
                    list.scrollTop = scenario === 'top-up' ? 0 : scenario === 'bottom-down' ? limits.maxList : limits.maxList / 2;
                    const documentTop = list.getBoundingClientRect().top + outer.scrollTop;
                    outer.scrollTop = Math.min(limits.maxOuter / 2, Math.max(80, documentTop - 100));
                    await Zotero.Promise.delay(100);
                    const before = metrics(), box = list.getBoundingClientRect();
                    const x = box.left + Math.min(80, box.width / 2), y = Math.max(box.top + 15, 30);
                    ensure(y < Math.min(box.bottom, win.innerHeight - 10), 'List target is not visible');
                    const events = [], observe = event => events.push({trusted: event.isTrusted, mode: event.deltaMode,
                      overList: list.contains(event.target)});
                    list.addEventListener('wheel', observe);
                    try {
                      const direction = scenario.endsWith('up') ? -1 : 1;
                      win.windowUtils.sendWheelEvent(x, y, 0, direction * (mode === 'line' ? 3 : 90), 0,
                        mode === 'line' ? 1 : 0, 0, 0, mode === 'line' ? direction * 3 : 0,
                        mode === 'pixel' ? win.windowUtils.WHEEL_EVENT_CAUSED_BY_NO_LINE_OR_PAGE_DELTA_DEVICE : 0);
                      await Zotero.Promise.delay(500);
                    } finally { list.removeEventListener('wheel', observe); }
                    const after = metrics();
                    ensure(events.length && events.every(event => event.trusted && event.overList), 'Input did not reach the real list');
                    const row = {width: win.innerWidth, mode, scenario, before, after, events};
                    response.cases.push(row);
                    if (scenario.startsWith('inside')) {
                      ensure(Math.abs(after.outer - before.outer) < 2, 'Interior list scrolling moved the outer panel: ' + JSON.stringify(row));
                      ensure(scenario.endsWith('up') ? after.list < before.list : after.list > before.list, 'Interior list did not scroll: ' + JSON.stringify(row));
                    } else {
                      ensure(Math.abs(after.list - before.list) < 2, 'Boundary list position changed: ' + JSON.stringify(row));
                      if (limits.maxOuter > 0) ensure(scenario === 'top-up' ? after.outer < before.outer : after.outer > before.outer, 'Boundary did not chain: ' + JSON.stringify(row));
                      else ensure(after.outer === 0, 'Viewport-fitted review unexpectedly scrolled its outer page');
                    }
                  }
                }
              }
              response.passed = ['line and pixel input scroll the list independently in both directions',
                'top and bottom boundaries chain to the outer panel when it has room',
                'wide and narrow layouts preserve their containment, scrollbar gutter and width'];
            } finally {
              ui.latest = saved.latest; ui.tab = saved.tab; ui.selected = saved.selected; ui.showAllReviews = saved.showAll;
              ui.view.resizeTo(1040, 760); ui.render();
            }
          }
          else if (command.action === 'reindex') await ui.action('reindex', {key: command.key});
          else if (command.action === 'rpc') { response.result = await ui.action(command.method, command.params || {}); }
          else if (command.action === 'reader_tags') {
            const item = await Zotero.Items.getByLibraryAndKeyAsync(Zotero.Libraries.userLibraryID, command.context.key);
            const reader = await Zotero.Reader.open(item.id, {pageIndex: command.context.page});
            const buttons = () => [...(reader._iframeWindow?.document.querySelectorAll('.notable-page-tag') || [])];
            const waitFor = async (condition, message) => {
              const until = Date.now() + 30000;
              while (!condition() && Date.now() < until) await Zotero.Promise.delay(100);
              if (!condition()) throw Error(message);
            };
            await waitFor(() => reader._internalReader?._state?.primaryViewStats?.pagesCount && buttons().length,
              'Reader tag control missing');
            const {AddonManager} = ChromeUtils.importESModule('resource://gre/modules/AddonManager.sys.mjs');
            const addon = await AddonManager.getAddonByID(ui.id);
            await addon.disable();
            await waitFor(() => !Zotero.Notable && !buttons().length, 'Plugin shutdown did not remove the tag control');
            await addon.enable();
            await waitFor(() => Zotero.Notable && buttons().length, 'Existing reader lost its tag control after plugin reload');
            if (buttons().length !== 1) throw Error('Plugin reload added duplicate controls');
            const [button] = buttons();
            const doc = button.ownerDocument;
            const style = node => reader._iframeWindow.getComputedStyle(node);
            if (style(button).getPropertyValue('-moz-window-dragging') !== 'no-drag'
                || button.textContent.trim() || !button.querySelector('svg') || button.tabIndex !== -1
                || button.getAttribute('aria-label') !== button.title || button.title !== 'Tag this paper and/or page') {
              throw Error('Reader tag control is missing native toolbar styling, icon, or accessible name');
            }
            const themeRoot = doc.documentElement;
            const initialScheme = themeRoot.getAttribute('data-color-scheme');
            try {
              const colors = ['light', 'dark'].map(scheme => {
                themeRoot.setAttribute('data-color-scheme', scheme);
                const color = style(button).color;
                if (color !== style(doc.getElementById('appearance')).color) {
                  throw Error(`Reader tag control did not match the ${scheme} toolbar color`);
                }
                return color;
              });
              if (colors[0] === colors[1]) throw Error('Reader toolbar theme did not change');
            } finally {
              if (initialScheme === null) themeRoot.removeAttribute('data-color-scheme');
              else themeRoot.setAttribute('data-color-scheme', initialScheme);
            }
            const activeUI = Zotero.Notable;
            const original = activeUI.rpc; response.requests = [];
            activeUI.rpc = async function(method, params) {
              if (method === 'tag_context') { response.requests.push({method, params}); return command.context; }
              if (method === 'set_page_tags') {
                response.requests.push({method, params});
                return {...command.context, page_tags: params.tags, paper_tags: params.paper_tags};
              }
              return original.call(this, method, params);
            };
            try {
              button.click();
              const saveButton = () => [...(activeUI.view?.document.querySelectorAll('button') || [])]
                .find(b => b.textContent === 'Save tags');
              await waitFor(saveButton, 'Reader page editor did not open');
              response.heading = activeUI.view.document.querySelector('main h2')?.textContent;
              const inputs = [...activeUI.view.document.querySelectorAll('main input')];
              inputs[0].value = 'Native paper tag'; inputs[1].value = 'Native page tag';
              saveButton().click();
              await waitFor(() => response.requests.some(r => r.method === 'set_page_tags') && !activeUI.busy,
                'Reader tags did not save');
              response.readerPage = reader._internalReader._state.primaryViewStats.pageIndex;
              response.readerButton = button.title;
            } finally { activeUI.rpc = original; }
          }
          else if (command.action === 'snapshot') {
            const win = command.mainWindow ? Zotero.getMainWindow() : ui.view, canvas = win.document.createElementNS('http://www.w3.org/1999/xhtml', 'canvas');
            canvas.width = win.innerWidth; canvas.height = win.innerHeight;
            if (command.scrollY !== undefined) {
              win.scrollTo(0, command.scrollY);
              win.document.querySelector('body').scrollTop = command.scrollY;
            }
            await Zotero.Promise.delay(100);
            const ctx = canvas.getContext('2d');
            if (ctx.drawWindow) ctx.drawWindow(win, 0, win.scrollY, canvas.width, canvas.height, 'white');
            else {
              const image = await win.browsingContext.currentWindowGlobal.drawSnapshot({x: 0, y: 0, width: canvas.width, height: canvas.height}, 1, 'white');
              ctx.drawImage(image, 0, 0); image.close();
            }
            const raw = atob(canvas.toDataURL('image/png').split(',')[1]);
            await IOUtils.write(PathUtils.join(PathUtils.parent(config.ready), command.filename || 'workspace.png'), Uint8Array.from(raw, c => c.charCodeAt(0)));
            response.width = canvas.width; response.height = canvas.height;
          }
          else if (command.action === 'replay') {
            const record = await Zotero.Items.getByLibraryAndKeyAsync(Zotero.Libraries.userLibraryID, command.key);
            await record.loadAllData(); response.annotations = record.isPDFAttachment() ? record.getAnnotations().length : 0;
          } else throw new Error('Unsupported test command');
          response.message = ui.view?.document.getElementById('message')?.textContent;
          response.buttons = Array.from(ui.view?.document.querySelectorAll('button') || [], b => b.textContent);
          response.headings = Array.from(ui.view?.document.querySelectorAll('h2') || [], node => node.textContent);
          response.title = ui.view?.document.title;
          response.worker = !!ui.process;
        } catch (error) { response.error = error.message; response.stack = error.stack; }
        await IOUtils.writeUTF8(PathUtils.join(PathUtils.parent(config.ready), 'response.json'), JSON.stringify(response));
      }
      await Zotero.Promise.delay(150);
    }
    Services.startup.quit(Ci.nsIAppStartup.eAttemptQuit);
  });
}
function install() {} function shutdown() {} function uninstall() {}
"""


def command(root, action, **values):
    identity = str(uuid.uuid4())
    (root / 'command.tmp').write_text(json.dumps({'id': identity, 'action': action, **values}), encoding='utf-8')
    (root / 'command.tmp').replace(root / 'command.json')
    deadline = time.monotonic() + 135
    while time.monotonic() < deadline:
        path = root / 'response.json'
        if path.exists():
            try:
                result = json.loads(path.read_text(encoding='utf-8'))
            except json.JSONDecodeError:
                # The test extension may still be writing this response.
                # Keep the same command identity and existing overall timeout.
                pass
            else:
                if result.get('id') == identity:
                    if result.get('error'):
                        raise RuntimeError(result['error'] + '\n' + result.get('stack', ''))
                    return result
        time.sleep(.15)
    raise RuntimeError('Plugin UI command timed out')


def connection_checks(client, root, checked):
    """Native Zotero library + unchanged MemoryCloud; no production accounts."""
    client.authorize()
    import copy
    from unittest.mock import patch
    cloud = MemoryCloud()
    old = cloud.create_folder('Previous folder', '', str(uuid.uuid4()))
    new = cloud.create_folder('Literature', '', str(uuid.uuid4()))
    child = cloud.create_folder('Old topic', old.id, str(uuid.uuid4()), old.id)
    client.create('collection', {'key': 'CNCT2345', 'name': 'Connection test papers'})
    client.create('item', {'key': 'CNCTPR23', 'itemType': 'journalArticle', 'title': 'Connection preservation', 'collections': ['CNCT2345']})
    client.create('item', {'key': 'CNCTSR23', 'itemType': 'attachment', 'parentItem': 'CNCTPR23', 'title': 'Main PDF', 'contentType': 'application/pdf',
                           'linkMode': 'imported_file', 'filename': 'Connection.pdf'})
    path = root / 'Connection original.pdf'; path.write_bytes(pdf_bytes())
    client.upload('CNCTSR23', path, '')
    worker = Worker(root / 'connection-worker', zotero_factory=lambda: LocalZotero(int(client.endpoint.split(':')[2].split('/')[0]),
                    grant_store=client.grant_store, interactive=False), cloud_factory=lambda: cloud)
    worker.config = {'server_id': client.server_id, 'zotero_root': 'CNCT2345', 'tablet_root': old.id, 'default_folder': old.id, 'ocr': False, 'automatic': True}
    worker.save()
    with patch.object(worker.review, 'observe_ready', return_value=True): worker.sync()
    row = next(r for r in worker.review.rows() if r['identity'] == 'CNCTSR23')
    worker.approve(row['id'], row['digest'], 'Connection paper.pdf', 'Connection preservation', 'journal_article', 'CNCT2345')
    worker.sync()
    documents = worker.state.read('documents'); identity = next(iter(documents))
    documents[identity]['page_tags'] = {'saved-page-uuid': ['Keep page tag']}; worker.state.write('documents', documents)
    original_state = worker.state.directory
    before_catalog = client.review_action('catalog')
    attachment_bytes = {r['data']['key']: Path(r['path']).read_bytes() for r in before_catalog['attachments']}
    cloud_before = {r.id: r.hash for r in cloud.library()}
    token = worker.preview_connection(zotero_root='CNCT2345', tablet_root=new.id, default_folder=new.id)['token']
    worker.configure(zotero_root='CNCT2345', tablet_root=new.id, default_folder=new.id, confirmation=token)
    assert worker.config['sync_paused'] and not worker.config['automatic']
    assert client.review_action('catalog') == before_catalog
    assert {r.id: r.hash for r in cloud.library()} == cloud_before
    checked('native_folder_change_saves_without_sync_or_changes_to_either_library')
    worker.sync()
    assert not [r for r in cloud.library() if r.parent == new.id]
    assert worker.state.read('documents', {}) == {}
    assert all(client.file_path(key).read_bytes() == data for key, data in attachment_bytes.items())
    checked('native_new_scope_keeps_old_mirrored_folders_and_linked_papers_out_without_deleting_them')
    token = worker.preview_connection(zotero_root='CNCT2345', tablet_root=old.id, default_folder=old.id)['token']
    worker.configure(zotero_root='CNCT2345', tablet_root=old.id, default_folder=old.id, confirmation=token)
    assert worker.state.directory == original_state and worker.state.read('documents') == documents
    checked('native_return_to_old_folders_restores_original_links_page_tags_and_recovery_paths')
    token = worker.preview_disconnect()['token']; worker.disconnect_folder(token)
    assert not worker.config['tablet_root'] and not worker.status()['links']
    assert client.review_action('catalog') == before_catalog
    assert {r.id: r.hash for r in cloud.library()} == cloud_before
    checked('native_disconnect_keeps_papers_notes_collections_and_tablet_bytes_unchanged')
    # The packaged helper itself must also retain and disconnect saved state.
    command(root, 'rpc', method='pause_sync')
    preview = command(root, 'rpc', method='preview_disconnect')['result']
    disconnected = command(root, 'rpc', method='disconnect_folder', params={'confirmation': preview['token']})['result']
    assert not disconnected['config']['tablet_root'] and disconnected['config']['sync_paused']
    checked('packaged_helper_pauses_and_disconnects_without_cloud_access')
    ui = command(root, 'folder_ui')
    assert ui['previewShown'] and ui['savedWithoutSync'] and ui['disconnected'], ui
    checked('native_buttons_change_review_save_cancel_and_disconnect_without_an_implicit_sync')
    checked('native_toolbar_entry_uses_a_tablet_icon_and_an_open_workspace_tooltip')


def native_checks(client, root, checked, *, smoke=False):
    """Real Zotero API, the actual cloud writer against in-memory blob CAS."""
    cloud = MemoryCloud()
    croot = cloud.create_folder('Tablet literature', '', str(uuid.uuid4()))
    client.authorize()
    client.create('collection', {'key': 'SYNC2345', 'name': 'reMarkable'})
    client.create('collection', {'key': 'TPIC2345', 'name': 'Research topic'})
    client.create('item', {'key': 'PARENT23', 'itemType': 'thesis', 'title': 'Synthetic dissertation', 'collections': ['TPIC2345'], 'university': 'Example University'})
    source = root / 'Incoming original.pdf'; source.write_bytes(pdf_bytes())
    client.create('item', {'key': 'SURCE234', 'itemType': 'attachment', 'parentItem': 'PARENT23', 'title': 'Main text', 'contentType': 'application/pdf', 'linkMode': 'imported_file', 'filename': 'Original.pdf'})
    client.upload('SURCE234', source, '')
    catalog = client.review_action('catalog')
    assert catalog['parents']['PARENT23']['university'] == 'Example University'
    assert Path(catalog['attachments'][0]['path']).exists()
    checked('native_catalog_uses_actual_local_paths_and_preserves_thesis_metadata')
    # The same encrypted permission grant can be used by the private worker.
    worker = Worker(root / 'review-worker', zotero_factory=lambda: LocalZotero(int(client.endpoint.split(':')[2].split('/')[0]), grant_store=client.grant_store, interactive=False), cloud_factory=lambda: cloud)
    worker.config = {'server_id': client.server_id, 'zotero_root': 'SYNC2345', 'tablet_root': croot.id, 'default_folder': croot.id, 'ocr': False}
    worker.sync(); time.sleep(2.1); worker.sync()
    row = worker.review.rows()[0]
    assert not worker.state.read('documents', {})
    checked('connector_or_manual_import_waits_for_naming_and_destination_review')
    worker.approve(row['id'], row['digest'], 'Reviewed dissertation.pdf', 'Synthetic dissertation', 'thesis_dissertation', 'SYNC2345', ['TPIC2345'])
    assert client.file_path('SURCE234').name == 'Reviewed dissertation.pdf'
    worker.sync()
    links = worker.state.read('documents'); assert len(links) == 1
    cloud_id, link = next(iter(links.items()))
    assert set(client.get('item', 'PARENT23')['collections']) == {'SYNC2345', 'TPIC2345'}
    assert len(client.children('PARENT23')) == 1
    checked('reviewed_native_rename_and_single_tablet_home_with_multiple_collections')
    worker.sync(); assert len(worker.state.read('documents')) == 1
    checked('repeated_sync_does_not_duplicate_documents_or_reviews')
    if smoke:
        return None
    client.create('collection', {'key': 'MVCL2345', 'name': 'Moved papers', 'parentCollection': 'SYNC2345'})
    worker.sync()
    mapped_folder = worker.state.read('folders')['MVCL2345']['cloud']
    current = next(r for r in cloud.library() if r.id == cloud_id)
    cloud.update_item(cloud_id, expected_hash=current.hash, name='Tablet renamed', parent=mapped_folder, scope_id=croot.id)
    worker.sync()
    assert client.file_path('SURCE234').name == 'Tablet renamed.pdf'
    assert set(client.get('item', 'PARENT23')['collections']) == {'MVCL2345', 'TPIC2345'}
    client.review_action('rename', {'key': 'SURCE234', 'filename': 'PC renamed.pdf', 'md5': client.file_md5('SURCE234')})
    parent = client.get('item', 'PARENT23'); parent['collections'] = ['SYNC2345', 'TPIC2345']; client.update(parent)
    worker.sync()
    current = next(r for r in cloud.library() if r.id == cloud_id)
    assert current.name == 'PC renamed' and current.parent == croot.id
    checked('PDF_rename_and_move_follow_both_directions_without_losing_research_collections')
    current = next(r for r in cloud.library() if r.id == cloud_id)
    cloud.update_item(cloud_id, expected_hash=current.hash, parent='trash', scope_id=croot.id)
    worker.sync(); decision = worker.review.deletions()[0]
    assert not client.get('item', 'PARENT23').get('deleted')
    assert decision['payload']['restore_parent'] == croot.id
    worker.delete_decision(decision['id'], 'restore')
    worker.sync(); assert not worker.review.deletions()
    checked('confirmed_tablet_trash_waits_and_explicit_restore_resumes_same_link')
    # The whole reference has real notes and SI. PDF-only must preserve them.
    client.create('item', {'key': 'NTES2345', 'itemType': 'note', 'parentItem': 'PARENT23', 'note': '<p>Retain this research note</p>'})
    client.create('item', {'key': 'SUPP2345', 'itemType': 'attachment', 'parentItem': 'PARENT23', 'title': 'SI', 'contentType': 'application/pdf', 'linkMode': 'imported_file', 'filename': 'SI.pdf'})
    client.upload('SUPP2345', source, '')
    current = next(r for r in cloud.library() if r.id == cloud_id)
    cloud.update_item(cloud_id, expected_hash=current.hash, parent='trash', scope_id=croot.id)
    worker.sync(); decision = worker.review.deletions()[0]
    # Mutating a note after the displayed inventory invalidates that decision.
    note = client.get('item', 'NTES2345'); note['note'] += '<p>New note</p>'; client.update(note)
    try:
        worker.delete_decision(decision['id'], 'pdf_only')
        raise AssertionError('Stale deletion was accepted')
    except PilotError:
        pass
    assert not client.get('item', 'SURCE234').get('deleted')
    checked('changed_notes_block_stale_deletion_without_trashing_a_PDF')
    # A refused transaction restores the pending decision automatically.
    assert worker.review.deletions()[0]['status'] == 'pending'
    worker.sync(); decision = worker.review.deletions()[0]
    worker.delete_decision(decision['id'], 'pdf_only')
    assert not client.get('item', 'PARENT23').get('deleted')
    assert not client.get('item', 'NTES2345').get('deleted')
    assert not client.get('item', 'SUPP2345').get('deleted')
    inventory = client.review_action('inventory', {'parent': 'PARENT23'})['inventory']
    assert next(r for r in inventory if r['key'] == 'SURCE234')['deleted']
    worker.sync()
    assert len(worker.state.read('documents')) == 1
    checked('delete_PDF_only_trashes_only_matching_source_and_view_keeps_notes_SI_reference_and_never_resurrects')
    second = cloud.upload_pdf(pdf_bytes(), 'Second validation paper', croot.id, str(uuid.uuid4()), croot.id)
    worker.sync()
    row = next(r for r in worker.review.rows() if r['kind'] == 'cloud' and r['identity'] == second.id)
    worker.approve(row['id'], row['digest'], 'Second.pdf', 'Second validation paper', 'journal_article', 'SYNC2345')
    worker.sync()
    link = worker.state.read('documents')[second.id]
    client.create('item', {'key': 'FULLNT23', 'itemType': 'note', 'parentItem': link['parent'], 'note': '<p>Whole-reference note</p>'})
    client.create('item', {'key': 'FULLSI23', 'itemType': 'attachment', 'parentItem': link['parent'], 'title': 'Whole-reference SI', 'contentType': 'application/pdf', 'linkMode': 'imported_file', 'filename': 'Full SI.pdf'})
    client.upload('FULLSI23', source, '')
    current = next(i for i in cloud.library() if i.id == second.id)
    cloud.update_item(second.id, expected_hash=current.hash, parent='trash', scope_id=croot.id)
    worker.sync()
    snapshot = worker.status()
    decision = next(r for r in worker.review.deletions() if r['payload']['cloud'] == second.id)
    worker.delete_decision(decision['id'], 'everything')
    inventory = client.review_action('inventory', {'parent': link['parent']})['inventory']
    assert next(r for r in inventory if r['key'] == link['parent'])['deleted']
    assert client.get('item', 'FULLNT23')['parentItem'] == link['parent']
    assert client.get('item', 'FULLSI23')['parentItem'] == link['parent']
    checked('delete_everything_uses_native_Trash_for_the_reference_notes_and_SI')
    inbox = root / 'ToSort'; inbox.mkdir()
    worker.config['inbox'] = str(inbox)
    incoming = inbox / 'Download.pdf'; incoming.write_bytes(pdf_bytes())
    from docx import Document
    word = Document(); word.add_heading('Synthetic research notebook', 0); word.add_paragraph('A local intake validation document.'); word.save(inbox / 'Notebook.docx')
    # Keep the minimal synthetic OpenXML fixture local to this independent
    # plugin repository, rather than importing a desktop-project test module.
    import zipfile
    parts = {
        '[Content_Types].xml': '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        'docProps/core.xml': '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Local intake slides</dc:title><dc:creator>Example Researcher</dc:creator></cp:coreProperties>',
        'ppt/presentation.xml': '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst></p:presentation>',
        'ppt/_rels/presentation.xml.rels': '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide1.xml"/></Relationships>',
        'ppt/slides/slide1.xml': '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>Local nanoparticle presentation</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>'}
    with zipfile.ZipFile(inbox / 'Slides.pptx', 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)

    (inbox / 'Still downloading.pdf').write_bytes(b'%PDF-1.7\nunfinished')
    worker.sync(); time.sleep(2.1); worker.sync()
    rows = [r for r in worker.review.rows() if r['kind'] == 'inbox']
    assert len(rows) == 3
    for row in rows:
        original = Path(row['payload']['path'])
        digest = hashlib.sha256(original.read_bytes()).hexdigest()
        worker.approve(row['id'], row['digest'], 'Reviewed ' + original.name, row['payload']['title'], row['payload']['document_type'], 'SYNC2345')
        finished = worker.review.row(row['id'])
        stored = client.file_path(finished['approval']['source'])
        assert hashlib.sha256(stored.read_bytes()).hexdigest() == digest
        assert not original.exists()
    assert (inbox / 'Still downloading.pdf').exists()
    checked('PDF_Word_and_PowerPoint_intake_waits_for_completion_and_review_then_verifies_managed_copy_before_cleanup')
    from note_organizer.zotero_pilot import reparent_attachment
    source = finished['approval']['source']
    attachment = client.get('item', source)
    old_parent = attachment['parentItem']
    destination = client.create('item', {'key': 'RPAR2345', 'itemType': 'document', 'title': 'Verified reference move'})
    original_bytes = client.file_path(source).read_bytes()
    reparent_attachment(client, source, destination['key'])
    assert client.get('item', source)['parentItem'] == destination['key']
    assert client.file_path(source).read_bytes() == original_bytes
    reparent_attachment(client, source, destination['key'])
    reparent_attachment(client, source, old_parent)
    assert client.get('item', source)['parentItem'] == old_parent
    checked('native_reparent_is_verified_idempotent_and_keeps_attachment_key_and_bytes')
    return snapshot


def bundled_search_checks(client, root, handwriting_pdf, checked):
    """Run local OCR through the plugin's actual private process transport."""
    import pymupdf
    from note_organizer.zotero_pilot import Ledger, Paper, publish
    from note_organizer.zotero_bridge import BridgeState
    pdfs = []
    if handwriting_pdf:
        pdfs.append(('physical-handwriting', handwriting_pdf.resolve(strict=True), 'testing'))
    scanned = root / 'Bundled scan.pdf'
    with pymupdf.open() as printed, pymupdf.open() as image:
        page = printed.new_page(width=400, height=500)
        page.insert_text((35, 100), 'bundledscanzirconium nanoparticle', fontsize=15)
        png = page.get_pixmap(dpi=200).tobytes('png')
        image.new_page(width=400, height=500).insert_image((0, 0, 400, 500), stream=png)
        image.save(scanned)
    pdfs.append(('image-scan', scanned, 'bundledscanzirconium'))
    from note_organizer.zotero_connections import state_directory
    state_home = root / 'profile/notable'
    state = BridgeState(state_directory(state_home, json.loads((state_home / 'config.json').read_text())) / 'bridge')
    links = {}
    for identity, path, keyword in pdfs:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        result = publish(client, Ledger(root / ('search-' + identity)), 'bundled-search-' + identity,
            [Paper(identity, path, digest, {'title': 'Synthetic search validation: ' + identity})], apply=True)['papers'][0]
        key, parent = result['attachment_key'], result['reference_key']
        links[identity] = {'name': 'Synthetic search validation: ' + identity, 'view_key': key, 'parent': parent, 'revision': 'fixture'}
        state.write('documents', links)
        before = client.file_md5(key)
        command(root, 'reindex', key=key)
        assert keyword in client.zot.fulltext_item(key)['content'].lower()
        assert any(r['key'] in {key, parent} for r in client.zot.items(q=keyword, qmode='everything'))
        assert client.file_md5(key) == before and hashlib.sha256(path.read_bytes()).hexdigest() == digest
        checked('packaged_helper_searches_' + identity + '_in_Zotero_without_modifying_PDF')
    # Native desktop comments join the same attachment-content index, while
    # remaining native Zotero annotations rather than being sent to the tablet.
    client.create('item', {'key': 'NATTXT23', 'itemType': 'annotation', 'parentItem': key,
        'annotationType': 'note', 'annotationComment': 'desktopnativequartzkeyword', 'annotationColor': '#ffd400',
        'annotationPageLabel': '1', 'annotationSortIndex': '00000|000000|00000',
        'annotationPosition': json.dumps({'pageIndex': 0, 'rects': [[10, 10, 30, 30]]})})
    command(root, 'reindex', key=key)
    assert any(r['key'] in {key, parent} for r in client.zot.items(q='desktopnativequartzkeyword', qmode='everything'))
    checked('native_Zotero_comments_are_searchable_alongside_print_highlights_and_ink_transcripts')


def bulk_checks(client, root, checked, count, resume=False):
    """Exercise actual native imports, not a mock of Zotero's storage API."""
    from tests.test_zotero_bulk import pdf
    if not resume:
        client.create('collection', {'key': 'BULK2345', 'name': 'Windows imports'})
    source = root / 'Windows literature'; source.mkdir(exist_ok=resume)
    originals = {}
    if resume:
        originals = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(source.rglob('*.pdf'))
                     if p.name not in {'Broken.pdf', 'Supporting_SI.pdf'}}
        assert len(originals) == count + 1 and len(set(originals.values())) == count
    else:
        for number in range(count):
            path = pdf(source / f'Category {number % 4}' / f'Paper {number:04}.pdf', f'Synthetic bulk scientific study number {number:04}')
            originals[path] = hashlib.sha256(path.read_bytes()).hexdigest()
        duplicate = source / 'Duplicate original.pdf'; duplicate.write_bytes(next(iter(originals)).read_bytes())
        originals[duplicate] = hashlib.sha256(duplicate.read_bytes()).hexdigest()
        (source / 'Broken.pdf').write_bytes(b'not a complete PDF')
        pdf(source / 'Supporting_SI.pdf', 'Supporting information for a synthetic bulk study')
    worker = Worker(root / 'bulk-worker', zotero_factory=lambda: LocalZotero(int(client.endpoint.split(':')[2].split('/')[0]), grant_store=client.grant_store, interactive=False))
    worker.scan_library(str(source), 'BULK2345', lookup=False)
    plan = worker.bulk.read()
    assert len(plan['rows']) == count + 2 and len([r for r in plan['rows'] if r['issues']]) == 2
    if not resume:
        assert not [r for r in client.review_action('catalog')['parents'].values() if 'BULK2345' in r.get('collections', [])]
    checked('native_bulk_scan_is_read_only_groups_exact_duplicates_and_holds_only_broken_and_SI_cases')
    worker.import_library(plan['id'])
    plan = worker.bulk.read()
    assert len([r for r in plan['rows'] if r['status'] == 'complete']) == count, [(r['path'], r['issues']) for r in plan['rows'] if r['issues']]
    def imported_records():
        scope = {'BULK2345'}
        collections = client.collections()
        while True:
            expanded = scope | {r['key'] for r in collections if r.get('parentCollection') in scope}
            if expanded == scope:
                break
            scope = expanded
        return [r for r in client.review_action('catalog')['parents'].values() if set(r.get('collections', [])) & scope]
    records = imported_records()
    categories = [r for r in client.collections() if r.get('parentCollection') == 'BULK2345' and r['name'].startswith('Category ')]
    assert len(categories) == min(4, count)
    assert all(any(r['key'] in parent.get('collections', []) for parent in records) for r in categories)
    checked('native_bulk_import_preserves_original_category_folders')
    if resume:
        catalog = client.review_action('catalog')
        filed = {a['data'].get('parentItem') for a in catalog['attachments'] if a['data'].get('parentItem') in {r['key'] for r in records}}
        assert len(filed) == count and all(r['itemType'] == 'document' for r in records if r['key'] in filed)
        checked('preexisting_reference_only_leftovers_are_retained_without_creating_duplicate_PDFs')
    else:
        assert len(records) == count and all(r['itemType'] == 'document' for r in records)
    for row in plan['rows']:
        if row['status'] == 'complete':
            assert hashlib.sha256(client.file_path(row['attachment']).read_bytes()).hexdigest() == row['digest']
    assert originals == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in originals}
    checked(f'native_bulk_{count}_papers_import_without_complete_citations_or_changes_to_Windows_originals')
    if not resume:
        completed = next(r for r in plan['rows'] if r['status'] == 'complete')
        replayed = client.review_action('bulk_import_status', {'digest': completed['digest'], 'collection': 'BULK2345'})
        assert replayed['attachment'] == completed['attachment']
        replayed_again = client.review_action('bulk_import_status', {'digest': completed['digest'], 'collection': 'BULK2345'})
        assert replayed_again == replayed
        checked('native_import_results_survive_repeated_polling_or_lost_responses')
    worker.scan_library(str(source), 'BULK2345', lookup=False)
    again = worker.bulk.read()
    assert len([r for r in again['rows'] if r['existing']]) == count
    worker.import_library(again['id'])
    assert len(imported_records()) == len(records)
    checked('native_bulk_rescan_reuses_existing_references_and_PDFs_without_creating_duplicates')
    # The normal DOI translator resolves this known preprint without adding a
    # downloaded PDF. The test file contains synthetic text, not a user's paper.
    import pymupdf
    doi_source = root / 'DOI lookup'; doi_source.mkdir()
    known = doi_source / 'Known DOI.pdf'
    paper_title = 'Drug-Eluting, Radiopaque, Tumor-Casting Hydrogels for Endovascular Locoregional Therapy of Hepatocellular Carcinoma'
    with pymupdf.open() as doc:
        page = doc.new_page(); page.insert_text((20, 80), paper_title, fontsize=6)
        page.insert_text((20, 110), 'DOI: 10.1101/2025.11.25.690505', fontsize=10)
        doc.set_metadata({'title': paper_title}); doc.save(known)
    worker.scan_library(str(doi_source), 'BULK2345', lookup=True)
    worker.import_library(worker.bulk.read()['id'])
    row = worker.bulk.read()['rows'][0]
    assert row['status'] == 'complete' and not row['issues'], row
    record = client.get('item', row['parent'])
    assert record['DOI'] == '10.1101/2025.11.25.690505' and record['creators'][0]['lastName'] == 'Dong', record
    assert record['itemType'] == 'preprint' and len(record['creators']) == 9
    assert len(client.children(row['parent'])) == 1
    checked('native_DOI_metadata_keeps_preprint_type_and_author_order_without_downloading_another_PDF')
    # Batch-file routine native imports and match an existing tablet original.
    # Use the same source bytes and in-memory cloud protocol writer as the
    # independently tested bridge; no real tablet account is involved.
    cloud = MemoryCloud()
    croot = cloud.create_folder('Literature', '', str(uuid.uuid4()))
    first = next(iter(originals))
    existing = cloud.upload_pdf(first.read_bytes(), first.stem, croot.id, str(uuid.uuid4()), croot.id)
    worker.cloud_factory = lambda: cloud
    worker.config = {'server_id': client.server_id, 'zotero_root': 'BULK2345', 'tablet_root': croot.id, 'default_folder': croot.id, 'ocr': False}
    worker.sync(); time.sleep(2.1); worker.sync()
    eligible = [r for r in worker.review.rows() if r['kind'] == 'zotero' and r['payload'].get('parent') in {p['key'] for p in records}]
    selected = sorted(eligible, key=lambda r: not any(m['key'] == existing.id for m in r['payload'].get('matches', [])))[:3]
    assert len(selected) == 3
    assert any(any(m['key'] == existing.id for m in r['payload'].get('matches', [])) for r in selected)
    ambiguous = [r for r in selected if not r['payload']['routine']]
    assert len(ambiguous) == 1 and 'several tablet destinations' in str(ambiguous[0]['payload']['issues'])
    held = ambiguous[0]
    worker.approve(held['id'], held['digest'], held['payload']['filename'], held['payload']['title'],
                   held['payload']['document_type'], 'BULK2345', match=existing.id,
                   review_token=held['payload']['review_token'])
    checked('native_import_with_multiple_original_folders_requires_an_explicit_tablet_home')
    worker.approve_routine([{'id': r['id'], 'digest': r['digest'], 'review_token': r['payload']['review_token']}
                            for r in selected if r['payload']['routine']], 'BULK2345')
    assert all(worker.review.row(r['id'])['status'] == 'approved' for r in selected)
    worker.sync()
    assert len(worker.state.read('documents')) == 3 and existing.id in worker.state.read('documents')
    assert len([i for i in cloud.library() if i.is_pdf]) == 3
    checked('native_routine_batch_files_multiple_papers_and_reuses_the_exact_existing_tablet_original')
    tag_id, tag_link = next(iter(worker.state.read('documents').items()))
    key = tag_link['view_key']
    context = worker.tag_context(key, 0)
    ui_tags = command(root, 'reader_tags', context=context)
    command(root, 'snapshot')
    import shutil
    shutil.copyfile(root / 'workspace.png', root / 'tag-workspace.png')
    assert ui_tags['readerButton'] == 'Tag this paper and/or page' and ui_tags['readerPage'] == 0
    assert ui_tags['requests'][0]['params'] == {'key': key, 'page': 0}
    edit = next(r for r in ui_tags['requests'] if r['method'] == 'set_page_tags')['params']
    worker.dispatch('set_page_tags', edit)
    before_pdf = client.file_path(key).read_bytes()
    worker.sync()
    remote = next(i for i in cloud.library() if i.id == tag_id)
    content = json.loads(cloud.pdf_bundle(remote)[remote.id + '.content'])
    assert any(t['name'] == 'Native paper tag' for t in content['tags'])
    assert any(t['name'] == 'Native page tag' and t['pageId'] == context['page_id'] for t in content['pageTags'])
    assert client.file_path(key).read_bytes() == before_pdf
    assert client.native_annotation_count(key) == 0
    assert worker.tag_search('Native page', 'page')['results'][0]['page_id'] == context['page_id']
    worker.sync()
    assert client.file_path(key).read_bytes() == before_pdf
    checked('actual_reader_tag_button_and_editor_roundtrip_both_scopes_without_creating_annotations_or_rewriting_PDF')
    worker.scan_library(str(source), 'BULK2345', lookup=False)
    assert len([r for r in worker.bulk.read()['rows'] if r['existing']]) == count
    worker.import_library(worker.bulk.read()['id'])
    assert all(r['status'] == 'complete' for r in worker.bulk.read()['rows'] if not r['issues'])
    checked('bulk_rescan_recognizes_originals_after_their_managed_PDFs_receive_tablet_annotations')
    # Real native SDK renders and invokes the private transport too, using a
    # subset already imported above to verify reuse through the packaged UI.
    subset = root / 'UI literature'; subset.mkdir()
    linked = {link['baseline'] for link in worker.state.read('documents').values()}
    unlinked = [p for p in originals if hashlib.sha256(p.read_bytes()).hexdigest() not in linked]
    for number, path in enumerate(unlinked[:3]):
        (subset / f'Original {number}.pdf').write_bytes(path.read_bytes())
    result = command(root, 'rpc', method='scan_library', params={'root': str(subset), 'collection': 'BULK2345', 'lookup': False})['result']
    assert len(result['bulk']['rows']) == 3 and all(r['existing'] for r in result['bulk']['rows'])
    result = command(root, 'rpc', method='import_library', params={'plan_id': result['bulk']['id']})['result']
    assert all(r['status'] == 'complete' for r in result['bulk']['rows'])
    rendered = command(root, 'tab', tab='import')
    assert 'Scan folder â€” no library changes' in rendered['buttons']
    checked('actual_graphical_import_tab_and_private_worker_scan_and_reuse_existing_PDFs')
    command(root, 'snapshot')
    import shutil
    shutil.copyfile(root / 'workspace.png', root / 'bulk-workspace.png')


def activity_checks(root, checked):
    source = root / 'local-review-preview.pdf'
    source.write_bytes(pdf_bytes())
    snapshot = command(root, 'rpc', method='status')['result']
    snapshot.update(config={'zotero_root': 'ROOT2345', 'tablet_root': 'fixture-tablet', 'automatic': True, 'sync_paused': False},
                    collections=[{'key': 'ROOT2345', 'name': 'Tablet papers', 'parentCollection': False}],
                    mappings={'ROOT2345': {'cloud': 'fixture-tablet'}}, links=[], deletions=[], local_deletions=[],
                    connection={'linked_papers': 0, 'previous': []}, bulk=None,
                    status={'state': 'ready', 'message': 'Ready'})
    snapshot['reviews'] = [{'id': str(number), 'kind': 'cloud', 'identity': str(number), 'status': 'pending', 'digest': 'synthetic',
                            'payload': {'path': str(source), 'title': f'Example paper {number}', 'filename': f'Example {number}.pdf',
                                        'document_type': 'journal_article', 'routine': number == 0, 'details': {}, 'issues': [], 'guidance_version': snapshot.get('review_guidance', {}).get('version', 1), 'guidance_state': 'ready', 'findings': []}}
                           for number in range(2)]
    result = command(root, 'activity_ui', snapshot=snapshot, source=str(source))
    for name in result['passed']:
        checked('native_activity_' + name.replace(' ', '_'))


def tagging_checks(client, root, checked):
    import copy
    import pymupdf
    from note_organizer.zotero_worker import file_sha
    source = root / 'Keyword papers'; source.mkdir()
    path = source / 'Keyword fixture.pdf'
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((40, 80), 'Spectral CT nanoparticle research\n\nIntroduction\nSpectral CT measures iodine concentration. Nanoparticles improve contrast.\nIodine concentration is measured with spectral imaging.')
        document.set_metadata({'keywords': 'Spectral CT\x02nanoparticles'})
        document.save(path)
    digest = file_sha(path)
    collection = client.collections()[0]['key']
    native = client.review_action('bulk_import', {'root': str(source), 'source': str(path), 'digest': digest,
        'md5': hashlib.md5(path.read_bytes()).hexdigest(), 'collection': collection, 'lookup': False,
        'title': 'Keyword fixture paper', 'dois': [], 'evidence': ''})
    while native.get('pending'):
        time.sleep(.2)
        native = client.review_action('bulk_import_status', {'digest': digest, 'collection': collection})
    parent = client.get('item', native['parent']); parent['tags'] = [{'tag': 'Curated label', 'type': 0}]; client.update(parent)
    proposal = command(root, 'rpc', method='suggest_paper_tags', params={'key': native['attachment']})['result']['proposal']
    assert {row['tag'] for row in proposal['keywords']} == {'Spectral CT', 'nanoparticles'}, proposal
    assert [(tag['tag'], tag.get('type', 0)) for tag in client.get('item', native['parent'])['tags']] == [('Curated label', 0)]
    checked('packaged_keyword_analysis_retains_PDF_bytes_and_native_tags_without_writes')
    command(root, 'rpc', method='save_paper_tags', params={'target': proposal['target'], 'token': proposal['token'], 'selected': []})
    tags = client.get('item', native['parent'])['tags']
    assert ('Spectral CT', 1) in [(tag['tag'], tag.get('type', 0)) for tag in tags] and ('Curated label', 0) in [(tag['tag'], tag.get('type', 0)) for tag in tags], tags
    assert not {row['tag'] for row in proposal['suggestions']} & {row['tag'] for row in tags}
    checked('native_keywords_keep_manual_types_and_unselected_suggestions_are_not_added')
    proposal = command(root, 'rpc', method='suggest_paper_tags', params={'key': native['attachment']})['result']['proposal']
    selected = proposal['suggestions'][0]['tag']
    command(root, 'rpc', method='save_paper_tags', params={'target': proposal['target'], 'token': proposal['token'], 'selected': [selected]})
    assert (selected, 0) in [(tag['tag'], tag.get('type', 0)) for tag in client.get('item', native['parent'])['tags']]
    assert file_sha(path) == digest and file_sha(native['path']) == digest
    checked('native_selected_suggestion_is_explicit_and_original_PDF_bytes_are_unchanged')
    snapshot = command(root, 'rpc', method='status')['result']
    snapshot.update(config={'zotero_root': collection, 'tablet_root': 'fixture', 'automatic': False},
        collections=[{'key': collection, 'name': 'Literature', 'parentCollection': False}], mappings={collection: {'cloud': 'fixture'}},
        links=[], deletions=[], local_deletions=[], bulk=None)
    snapshot['reviews'] = [{'id': str(index), 'kind': 'cloud', 'identity': str(index), 'status': 'pending', 'digest': digest,
        'payload': {'path': str(path), 'title': f'Example paper {index:03}', 'filename': f'Example {index:03}.pdf',
                    'document_type': 'journal_article', 'details': {}, 'issues': [], 'guidance_version': snapshot.get('review_guidance', {}).get('version', 1), 'guidance_state': 'ready'}} for index in range(160)]
    proposal = copy.deepcopy(proposal); proposal['selected'] = []
    result = command(root, 'review_list', snapshot=snapshot, proposal=proposal)
    for name in result['passed']:
        checked('native_' + name.replace(' ', '_'))


def review_scroll_checks(root, checked):
    """Real Gecko scroll handling with injected line and high-resolution pixel input."""
    source = root / 'scroll-fixture.pdf'
    source.write_bytes(pdf_bytes())
    snapshot = command(root, 'rpc', method='status')['result']
    snapshot.update(config={'zotero_root': 'ROOT2345', 'tablet_root': 'fixture-tablet', 'sync_paused': True},
                    collections=[{'key': 'ROOT2345', 'name': 'Fixture papers', 'parentCollection': False}],
                    mappings={'ROOT2345': {'cloud': 'fixture-tablet'}}, links=[], deletions=[], local_deletions=[],
                    connection={'linked_papers': 0, 'previous': []}, bulk=None,
                    status={'state': 'paused', 'message': 'Synthetic review scroll test'})
    snapshot['reviews'] = [{'id': str(index), 'kind': 'cloud', 'identity': str(index), 'status': 'pending', 'digest': 'fixture',
        'payload': {'path': str(source), 'title': f'Example paper {index:03}', 'filename': f'Example {index:03}.pdf',
                    'document_type': 'journal_article', 'details': {}, 'issues': [], 'guidance_version': snapshot.get('review_guidance', {}).get('version', 1), 'guidance_state': 'ready'}} for index in range(160)]
    result = command(root, 'review_scroll', snapshot=snapshot)
    (root / 'review-scroll-results.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    for name in result['passed']:
        checked('native_scroll_' + name.replace(' ', '_'))


def review_guidance_checks(root, checked):
    """Render structured library findings and mapping comparisons in real Zotero."""
    import copy
    source = root / 'guidance-fixture.pdf'
    source.write_bytes(pdf_bytes())
    snapshot = command(root, 'rpc', method='status')['result']
    snapshot.update(config={'zotero_root': 'ROOT2345', 'tablet_root': 'fixture-tablet', 'sync_paused': True,
                            'zotero_path': 'Research library', 'tablet_path': 'Tablet library'},
                    collections=[{'key': 'ROOT2345', 'name': 'Research library', 'parentCollection': False}],
                    mappings={'ROOT2345': {'cloud': 'fixture-tablet'}}, links=[], deletions=[], local_deletions=[],
                    connection={'linked_papers': 0, 'previous': []}, bulk=None,
                    status={'state': 'paused', 'message': 'Synthetic Review guidance check'})
    row = {'id': 'review', 'kind': 'cloud', 'identity': 'fixture', 'status': 'pending', 'digest': 'fixture',
           'payload': {'path': str(source), 'title': 'A reference with a long research title ' * 6,
                       'filename': 'A_very_long_filename_' * 8 + '.pdf', 'document_type': 'journal_article',
                       'details': {}, 'issues': [], 'guidance_version': snapshot.get('review_guidance', {}).get('version', 1), 'guidance_state': 'ready'}}
    records = [{'key': key, 'title': f'Reference {number}: ' + 'Long article title ' * 8, 'date': '2024', 'DOI': '10.1000/fixture', 'itemType': 'journalArticle'}
               for number, key in enumerate(['PARENT23', 'OTHER234'], 1)]
    row['payload']['findings'] = [{'code': 'possible_duplicate_references', 'domain': 'library', 'severity': 'decision',
        'summary': 'Several references may describe the same publication',
        'explanation': 'Matching citation fields suggest candidates, not proof that distinct editions should be merged.',
        'next_step': 'Compare these references using Zotero. Keep intentional versions separate.',
        'evidence': ['Matching DOI: 10.1000/fixture'], 'references': records,
        'attachments': [{'key': 'SOURCE23', 'parent': 'PARENT23', 'filename': 'Article.pdf', 'title': 'Full Text', 'match_basis': 'original_baseline'}],
        'acknowledgement': {'eligible': True, 'token': 'synthetic-scope', 'acknowledged': False}}]
    startup = copy.deepcopy(snapshot)
    first = copy.deepcopy(row); first['id'] = 'startup-library-one'
    second = copy.deepcopy(row); second['id'] = 'startup-library-two'
    second['payload']['findings'][0]['references'] = [{'key': 'THIRD234', 'title': 'A separate startup case'}]
    second['payload']['findings'][0]['evidence'] = ['Separate startup evidence']
    startup['reviews'] = [first, second, *[{'id': 'startup-sync-' + str(index), 'kind': 'conflict',
        'identity': 'startup-' + str(index), 'status': 'pending', 'digest': 'fixture',
        'payload': {'type': 'paper', 'zotero': 'PARENT23', 'desktop': ['Desktop ' + str(index), 'ROOT2345'],
                    'tablet': ['Tablet ' + str(index), 'ROOT2345']}} for index in range(2)]]
    opened = command(root, 'startup_navigation', snapshot=startup)
    (root / 'startup-navigation-results.json').write_text(json.dumps(opened, indent=2), encoding='utf-8')
    for name in opened['passed']:
        checked('native_startup_' + name.replace(' ', '_'))
    layouts = []
    for kind in ['paper', 'folder', 'library', 'library-mixed', 'library-unknown', 'library-stale', 'inbox', 'waiting', 'missing', 'no-conflicts']:
        data = copy.deepcopy(snapshot)
        if kind == 'missing':
            missing = copy.deepcopy(row)
            missing['payload']['findings'] = [{'code': 'local_review_unavailable', 'domain': 'file', 'severity': 'repair', 'summary': 'Saved attachment unavailable',
                'explanation': 'This saved attachment is not available in Zotero.', 'references': records[:1], 'attachments': [
                    {'key': 'SOURCE23', 'title': 'Current annotated PDF', 'filename': 'Current.pdf', 'availability': 'current'},
                    {'key': 'REMOVED2', 'title': 'Saved source attachment', 'filename': 'Removed.pdf', 'availability': 'missing'}]}]
            data['reviews'] = [missing]
        elif kind == 'no-conflicts':
            data['reviews'] = []
        elif kind == 'waiting':
            data['reviews'] = [copy.deepcopy(row)]
        elif kind == 'inbox':
            incoming = copy.deepcopy(row); incoming['kind'] = 'inbox'; incoming['payload']['findings'] = []
            data['reviews'] = [incoming]
        elif kind in {'library', 'library-mixed', 'library-unknown', 'library-stale'}:
            library = copy.deepcopy(row)
            if kind != 'library':
                finding = library['payload']['findings'][0]
                finding.update(code='repeated_pdf_content', summary='Matching original PDFs under two references',
                    explanation='The original PDFs match, but current annotations may differ.',
                    evidence=['Matched retained original PDF content.'])
                finding['references'][0]['title'] = 'Synthetic journal article'
                finding['references'][1]['title'] = 'Synthetic_article.pdf'
                finding['attachments'][0]['parent'] = 'OTHER234'
                if kind == 'library-mixed':
                    finding['references'][1]['itemType'] = 'standard'
                elif kind == 'library-unknown':
                    finding['references'][1].pop('itemType')
            data['reviews'] = [library]
            data['links'] = [{'parent': 'PARENT23' if kind == 'library' else 'OTHER234',
                'view_key': 'SOURCE23', 'name': 'Linked tablet article', 'cloud': 'fixture-document'}]
        else:
            data['reviews'] = [{'id': 'review', 'kind': 'conflict', 'identity': 'fixture', 'status': 'pending', 'digest': 'fixture',
                'payload': {'type': kind, 'zotero': 'PARENT23',
                    'desktop': ['Zotero naming ' * 12, 'ROOT2345'], 'tablet': ['Tablet naming ' * 12, 'ROOT2345']}}]
        sizes = [(width, 'light') for width in [1040, 640, 400]]
        if kind in {'paper', 'missing', 'no-conflicts', 'library-mixed', 'library-unknown', 'library-stale'}:
            sizes += [(1040, 'dark'), (400, 'dark')]
        for width, theme in sizes:
            result = command(root, 'layout', snapshot=data, tab='inbox' if kind in {'inbox', 'waiting'} else 'library' if kind in {'library', 'library-mixed', 'library-unknown', 'library-stale', 'missing'} else 'sync-review', width=width, height=900, theme=theme, reviewStale=kind == 'library-stale',
                             expandDetails=kind == 'missing' and theme == 'dark')
            layout = result['layout']
            assert layout['scrollWidth'] <= layout['width'] + 1, (kind, width, layout)
            buttons = [value['text'] for value in layout['buttons']]
            sync_button = next(value for value in layout['buttons'] if value['text'] == 'Sync now')
            conflict_button = next(value for value in layout['buttons'] if value['text'].startswith('Sync Conflicts'))
            assert 'refresh-cw.svg' in sync_button['iconMask'] and 'sync-conflict.svg' in conflict_button['iconMask'], layout
            assert conflict_button['attention'] is (kind in {'paper', 'folder'}), layout
            if kind in {'paper', 'folder'}:
                assert conflict_button['iconColor'] != conflict_button['buttonColor'], layout
            else:
                assert conflict_button['iconColor'] == conflict_button['buttonColor'], layout
            if kind == 'missing':
                assert buttons.count('Show attachment in Zotero') == 1 and buttons.count('Open PDF in Zotero') == 1, layout
                assert layout['reviewHeadings'] == ['A saved attachment is unavailable'], layout
                assert layout['whySummaries'] == ['Why this needs attention'], layout
                assert layout['nestedEvidence'] == 0 and layout['repeatedScenarioCopy'] == 0, layout
            elif kind == 'no-conflicts':
                assert 'Apply Zotero name and location' not in buttons, layout
            elif kind == 'library':
                assert 'Compare in Zotero' in buttons and not any('Apply reMarkable' in text for text in buttons)
                assert 'Linked to reMarkable' in layout['linkBadges'], layout
                assert layout.get('filingExpanded') is None and layout['evidenceExpanded'] is False, layout
                assert layout['reviewHeadings'] == ['Possible duplicate references'], layout
                assert layout['whySummaries'] == ['Why these may be duplicates'], layout
                assert layout['nestedEvidence'] == 0 and layout['repeatedScenarioCopy'] == 0, layout
                keep = next(value for value in layout['buttons'] if value['text'] == 'Keep these references separate')
                assert not keep['insideExplanation'], 'The library decision must stay outside the explanation dropdown'
                assert layout['referenceTypes'] == ['Item Type: ' + layout['nativeTypeLabels']['journalArticle']] * 2, layout
                assert any('Merge Items' in text for text in layout['mergeHints']), layout
                compare = next(value for value in layout['buttons'] if value['text'] == 'Compare in Zotero')
                assert 'Merge Items' in compare['help'], layout
            elif kind in {'library-mixed', 'library-unknown', 'library-stale'}:
                assert 'Compare in Zotero' in buttons and 'Check my changes' in buttons, layout
                assert 'Linked to reMarkable' in layout['linkBadges'], layout
                assert layout['reviewHeadings'] == ['Matching PDF copies'], layout
                assert layout['whySummaries'] == ['Why these PDFs match'], layout
                assert layout['nestedEvidence'] == 0 and layout['repeatedScenarioCopy'] == 0, layout
                assert not any('Merge Items' in text for text in layout['mergeHints']), layout
                assert 'as the survivor' not in layout['mergeAdvice'], layout
                assert 'Keep the linked PDF and its notes' in layout['mergeAdvice'], layout
                compare = next(value for value in layout['buttons'] if value['text'] == 'Compare in Zotero')
                assert 'Merge Items' not in compare['help'], layout
                assert layout.get('filingExpanded') is None and layout['evidenceExpanded'] is False, layout
                keep = next(value for value in layout['buttons'] if value['text'] == 'Keep these PDFs separate')
                assert not keep['insideExplanation'], layout
                if kind == 'library-mixed':
                    assert layout['referenceTypes'] == ['Item Type: ' + layout['nativeTypeLabels']['journalArticle'], 'Item Type: ' + layout['nativeTypeLabels']['standard']], layout
                    assert layout['mergeHints'] == ['Different item types. If these describe the same publication, correct Item Type in Zotero before merging.'], layout
                    assert 'Item Type' in compare['help'], layout
                elif kind == 'library-unknown':
                    assert layout['referenceTypes'] == ['Item Type: ' + layout['nativeTypeLabels']['journalArticle'], 'Item type unavailable'], layout
                    assert layout['mergeHints'] == ['Item types are unavailable. Recheck Zotero before merging.'], layout
                    assert 'Recheck Zotero' in compare['help'], layout
                else:
                    assert layout['referenceTypes'] == ['Item Type: ' + layout['nativeTypeLabels']['journalArticle']] * 2, layout
                    assert layout['mergeHints'] == ['Zotero changed. Use Check my changes before deciding whether to merge.'], layout
                    assert 'Check my changes' in compare['help'], layout
            elif kind == 'inbox':
                assert 'Approve & file' in buttons and 'From: ToSort' in layout['linkBadges'], layout
                assert 'Proposed filename' in layout['fieldLabels'] and 'Destination folders' in layout['fieldLabels'], layout
                assert 'Suggest filename' in buttons and 'Update filename preview' not in buttons, layout
            elif kind == 'waiting':
                assert layout['queueSummary'] == '0 ready to file · 1 waiting on Library Issues', layout
                assert layout['waitingOpen'] is False and layout['waitingCount'] == 1 and layout['readyListCount'] == 0, layout
                assert 'Open Library Issues' in buttons and not any('Approve' in label for label in buttons), layout
                assert 'need review' not in layout['paperStatus'], layout
            else:
                assert 'Apply Zotero name and location' in buttons and 'Apply reMarkable name and location' in buttons
                assert ('Show Zotero collection' if kind == 'folder' else 'Open Zotero reference') in buttons
                assert 'Recheck Zotero' not in buttons, 'Local recheck must not look like a refresh of both conflict sources'
                assert layout['reviewTitle'] == ('Folder changed on both devices' if kind == 'folder' else 'Name or folder changed on both devices'), layout
            if width == 1040:
                command(root, 'snapshot', filename='queue-' + kind + ('-dark' if theme == 'dark' else '') + '.png')
            layouts.append({'kind': kind, 'theme': theme, 'requested_width': width, 'actual_width': layout['width'], 'scroll_width': layout['scrollWidth']})
    checked('native_item_type_guidance_uses_localized_labels_and_guards_mismatched_missing_and_stale_types')
    workflow = copy.deepcopy(snapshot)
    workflow['reviews'] = []
    for index in range(35):
        entry = copy.deepcopy(row); entry.update(id='review-' + str(index), digest='fixture', identity='fixture-' + str(index))
        entry['payload'].update(title='Synthetic paper ' + str(index), review_token='original-policy')
        workflow['reviews'].append(entry)
    result = command(root, 'queue_workflow', snapshot=workflow)
    (root / 'review-workflow-results.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    for name in result['passed']:
        checked('native_workflow_' + name.replace(' ', '_'))
    (root / 'review-guidance-results.json').write_text(json.dumps(layouts, indent=2), encoding='utf-8')
    checked('native_structured_guidance_and_conflict_icons_in_light_and_dark_without_horizontal_overflow')


def maintenance_checks(client, root, checked):
    native_checks(client, root, checked, smoke=True)

    def rpc(action, **params):
        return command(root, 'rpc', method='maintenance', params={'action': action, **params})['result']

    key = 'UNKW2345'
    client.create('item', {'key': key, 'itemType': 'document', 'title': 'Fixture_UnknownJournal'})
    [candidate] = [row for row in rpc('names')['matches'] if row['key'] == key]
    result = rpc('names', selected=[{'key': key, 'token': candidate['token']}])
    assert any(row['key'] == key for row in result['queued'])
    assert client.get('item', key)['title'] == 'Fixture_UnknownJournal'
    checked('packaged_unknown_name_review_scans_and_queues_without_renaming')
    parent = 'NMAT2345'
    title = 'Comparison of liquid embolics'
    unread = 'NMRD2345'
    unread_title = 'Read this methods paper'
    filename = 'Gurevich_et_al_UnknownJournal_2025.pdf'
    client.create('item', {'key': parent, 'itemType': 'journalArticle', 'title': title, 'date': '2025',
                           'creators': [{'creatorType': 'author', 'lastName': 'Gurevich'}],
                           'tags': [{'tag': 'Vascular fixture tag'}, {'tag': 'Read'}]})
    client.create('item', {'key': unread, 'itemType': 'journalArticle', 'title': unread_title,
                           'tags': [{'tag': 'Vascular fixture tag'}, {'tag': 'Reading list'}]})
    client.create('item', {'key': 'NMTG2345', 'itemType': 'note', 'parentItem': parent,
                           'note': '<p>Tag bank fixture</p>', 'tags': [{'tag': 'Note-only fixture tag'}]})
    client.create('item', {'key': 'NMTR2345', 'itemType': 'document', 'title': 'Trashed tag fixture',
                           'deleted': 1, 'tags': [{'tag': 'Trash-only fixture tag'}]})
    client.create('item', {'key': 'NMFL2345', 'itemType': 'attachment', 'parentItem': parent,
                           'title': 'Main PDF', 'contentType': 'application/pdf',
                           'linkMode': 'imported_file', 'filename': filename})
    result = rpc('names')
    [missing] = [row for row in result['matches'] if row['key'] == parent]
    assert missing['findings'] == [{'where': 'Zotero filename', 'name': filename, 'attachment': 'NMFL2345'}], missing
    checked('packaged_name_review_finds_zotero_attachment_even_without_local_pdf')
    command(root, 'paper_search_ui', parent=parent, title=title, filename=filename, unread=unread, unreadTitle=unread_title)
    checked('native_paper_search_matches_citations_tags_and_filenames_and_clears_without_leaving_view')
    checked('native_read_and_unread_filter_use_the_exact_zotero_tag_and_refresh_after_edits')
    command(root, 'tag_search_ui', parent=parent, title=title, unreadTitle=unread_title)
    checked('native_tag_picker_stages_exact_tags_and_runs_all_or_any_on_demand')
    checked('native_tag_bank_is_ready_without_the_worker_and_refreshes_after_tag_edits')
    checked('native_large_tag_bank_loads_on_scroll_and_keyboard_without_searching_or_jumping')
    repair_key = 'TAGR2345'
    before = {'"Gold"': 1, '(Invest Radiol 2004': 1, '►small bowel': 1, 'Gold': 0, '()': 0,
              '{M6O19}-type POM': 1}
    client.create('item', {'key': repair_key, 'itemType': 'journalArticle', 'title': 'Automatic tag repair fixture',
                           'tags': [{'tag': tag, 'type': kind} for tag, kind in before.items()]})
    preview = command(root, 'rpc', method='preview_keyword_cleanup')['result']
    [paper] = [row for row in preview['papers'] if row['parent'] == repair_key]
    assert {row['tag']: row.get('replacement') for row in paper['tags']} == {
        '"Gold"': 'Gold', '(Invest Radiol 2004': None, '►small bowel': 'small bowel'}, paper
    result = rpc('repair_keywords', selected=[{'parent': repair_key, 'token': paper['token'],
                                                'tags': [row['tag'] for row in paper['tags']]}])
    assert (result['removed'], result['renamed']) == (1, 2), result
    assert {row['tag']: row.get('type', 0) for row in client.get('item', repair_key)['tags']} == {
        'Gold': 0, '()': 0, 'small bowel': 1, '{M6O19}-type POM': 1}
    assert rpc('undo_tags', identity=result['id'])['restored'] == 3
    assert {row['tag']: row.get('type', 0) for row in client.get('item', repair_key)['tags']} == before
    checked('native_automatic_tag_repair_reaches_unlinked_papers_preserves_manual_tags_and_can_be_undone')
    command(root, 'workspace_consistency')
    checked('native_import_search_and_inbox_handoff_shared_empty_states_and_setup_order')
    backup = root / 'maintenance-backup.zip'
    restored = root / 'maintenance-restored'
    assert rpc('backup', target=str(backup))['files'] >= 2
    assert rpc('restore', source=str(backup), destination=str(restored))['paused']
    config = json.loads((restored / 'config.json').read_text())
    assert not config['automatic'] and config['sync_paused']
    assert not list(restored.rglob('*.dpapi'))
    checked('packaged_plugin_backup_verifies_and_restores_paused_without_credentials')
    assert isinstance(rpc('timeline')['events'], list)
    checked('packaged_sync_timeline_uses_safe_event_labels')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir', type=Path, required=True)
    parser.add_argument('--zotero-exe', type=Path, required=True)
    parser.add_argument('--runtime', type=Path)
    parser.add_argument('--xpi', type=Path, help='Test the exact release package instead of rebuilding it')
    parser.add_argument('--handwriting-pdf', type=Path)
    parser.add_argument('--visible', action='store_true')
    parser.add_argument('--keep-open', action='store_true')
    parser.add_argument('--port', type=int, default=23149)
    parser.add_argument('--bulk-count', type=int, default=0)
    parser.add_argument('--skip-ocr', action='store_true', help='Only for development-worker checks without bundled models')
    parser.add_argument('--connection-checks', action='store_true')
    parser.add_argument('--activity-checks', action='store_true')
    parser.add_argument('--maintenance-checks', action='store_true', help='Run the packaged local maintenance smoke checks')
    parser.add_argument('--preferences-checks', action='store_true', help='Open Setup from Zotero’s native settings pane')
    parser.add_argument('--tagging-checks', action='store_true')
    parser.add_argument('--review-scroll-checks', action='store_true', help='Only synthetic review input/layout checks; no cloud or library import')
    parser.add_argument('--review-guidance-checks', action='store_true', help='Render structured library and paper/folder guidance at wide/narrow widths')
    parser.add_argument('--existing-fixture', type=Path, help='Copy only a previous synthetic fixture into this new disposable profile')
    args = parser.parse_args()
    root = args.work_dir.resolve(); root.mkdir(parents=True, exist_ok=False)
    profile, data, extension = (root / name for name in ['profile', 'zotero-data', 'test-extension'])
    for path in [profile / 'extensions', data, extension]: path.mkdir(parents=True, exist_ok=True)
    if args.existing_fixture:
        import shutil
        prior = args.existing_fixture.resolve(strict=True)
        record = json.loads((prior / 'test-extension/config.json').read_text())
        if Path(record['data']).resolve() != prior / 'zotero-data' or not (prior / 'done').exists():
            raise ValueError('A closed synthetic fixture is required')
        shutil.copytree(prior / 'zotero-data', data, dirs_exist_ok=True)
        shutil.copytree(prior / 'Windows literature', root / 'Windows literature')
    config = {'data': str(data), 'ready': str(root / 'ready.json'), 'done': str(root / 'done')}
    (extension / 'config.json').write_text(json.dumps(config), encoding='utf-8')
    (extension / 'bootstrap.js').write_text(BOOTSTRAP, encoding='utf-8')
    test_id = 'notable-graphical-test@example.invalid'
    (extension / 'manifest.json').write_text(json.dumps({'manifest_version': 2, 'name': 'Disposable graphical test', 'version': '0.0.1',
        'applications': {'zotero': {'id': test_id, 'strict_min_version': '10.0', 'strict_max_version': '10.*', 'update_url': 'https://example.invalid/updates.json'}}}), encoding='utf-8')
    (profile / 'extensions' / test_id).write_text(str(extension), encoding='utf-8')
    if args.xpi:
        import shutil
        shutil.copyfile(args.xpi.resolve(strict=True), profile / 'extensions/notable-companion@local.invalid.xpi')
    else:
        build(profile / 'extensions/notable-companion@local.invalid.xpi', args.runtime)
    prefs = {'extensions.zotero.firstRun2': False, 'extensions.zotero.firstRunGuidance': False,
        'extensions.zotero.useDataDir': True, 'extensions.zotero.dataDir': str(data), 'extensions.zotero.warnOnUnsafeDataDir': False,
        'extensions.zotero.sync.autoSync': False, 'extensions.zotero.automaticScraperUpdates': False,
        'extensions.zotero.httpServer.enabled': True, 'extensions.zotero.httpServer.port': args.port,
        'extensions.zotero.httpServer.localAPI.enabled': True, 'extensions.zoteroWinWordIntegration.skipInstallation': True,
        'extensions.zoteroOpenOfficeIntegration.skipInstallation': True, 'extensions.update.enabled': False,
        'extensions.enabledScopes': 15, 'extensions.autoDisableScopes': 0, 'extensions.startupScanScopes': 15, 'app.update.enabled': False, 'app.update.auto': False}
    prefs['notable.testDiagnostics'] = True
    if not args.runtime and not args.xpi:
        prefs['notable.developmentWorker'] = json.dumps({'command': sys.executable, 'arguments': [str(Path(__file__).resolve().parent / 'notable_worker.py')]})
    (profile / 'user.js').write_text('\n'.join(f'user_pref({json.dumps(k)}, {json.dumps(v)});' for k, v in prefs.items()), encoding='utf-8')
    startup = subprocess.STARTUPINFO(); startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW; startup.wShowWindow = 1 if args.visible else subprocess.SW_HIDE
    result = {'scope': 'Disposable Zotero profile; no real library or cloud account', 'checks': []}
    def checked(name):
        result['checks'].append({'name': name, 'status': 'PASS'})
        (root / 'results.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    client = None
    with (root / 'stdout.log').open('w') as stdout, (root / 'stderr.log').open('w') as stderr:
        process = subprocess.Popen([str(args.zotero_exe.resolve(strict=True)), '-no-remote', '-profile', str(profile), '-datadir', str(data), *([] if args.visible else ['-headless'])], startupinfo=startup, stdout=stdout, stderr=stderr)
        try:
            deadline = time.monotonic() + 180
            while not Path(config['ready']).exists():
                # A normal first launch may restart after discovering an XPI.
                # The scoped ready file, rather than the launcher PID, is the
                # authority for the instance that actually owns this profile.
                if time.monotonic() > deadline: raise RuntimeError('Disposable Zotero failed to start')
                time.sleep(.2)
            from note_organizer.remarkable_cloud import TokenVault
            client = LocalZotero(args.port, grant_store=TokenVault(profile / 'notable/zotero-grant.dpapi'))
            assert client.server_id == json.loads(Path(config['ready']).read_text())['server']
            ui = command(root, 'open')
            assert ui['worker'] and any(name in ui['title'] for name in ['Notable', 'reMarkable Sync']) and 'Connect Zotero' in ui['buttons'], ui
            checked('Zotero_Tools_and_Plugins_display_the_R_icon_and_Tools_opens_setup')
            if args.preferences_checks:
                command(root, 'preferences')
                checked('Zotero_settings_opens_setup_in_new_and_existing_workspaces')
            command(root, 'connect')
            command(root, 'tab', tab='review')
            checked('graphical_connect_persists_permission_without_terminal')
            snapshot = native_checks(client, root, checked) if not (args.existing_fixture or args.review_scroll_checks
                         or args.review_guidance_checks or args.maintenance_checks) else None
            if args.maintenance_checks:
                maintenance_checks(client, root, checked)
            if args.review_scroll_checks:
                review_scroll_checks(root, checked)
            if args.review_guidance_checks:
                review_guidance_checks(root, checked)
            if args.bulk_count:
                bulk_checks(client, root, checked, args.bulk_count, resume=bool(args.existing_fixture))
            if args.connection_checks:
                connection_checks(client, root, checked)
            if args.activity_checks:
                activity_checks(root, checked)
            if args.tagging_checks:
                tagging_checks(client, root, checked)
            if not (args.skip_ocr or args.review_scroll_checks or args.review_guidance_checks or args.maintenance_checks):
                bundled_search_checks(client, root, args.handwriting_pdf, checked)
            if snapshot:
                rendered = command(root, 'render', snapshot=snapshot, tab='sync-review')
                assert all(label in rendered['buttons'] for label in
                           ['Restore on reMarkable', 'Trash linked PDFs in Zotero', 'Trash reference in Zotero']), rendered['buttons']
                assert 'No sync conflicts' not in rendered['headings'], 'Pending deletion decisions must not show an empty-queue heading'
                checked('actual_sync_conflicts_window_renders_all_three_deletion_choices_without_an_empty_state')
            command(root, 'snapshot')
            (root / 'results.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
            if args.keep_open:
                print(json.dumps({'state': 'ready_for_visual_inspection', 'root': str(root)}), flush=True)
                while not (root / 'close-test').exists(): time.sleep(1)
        except Exception as error:
            result['error'] = str(error)
            raise
        finally:
            if client: client.close()
            Path(config['done']).touch()
            try: process.wait(timeout=15)
            except subprocess.TimeoutExpired: process.terminate(); process.wait(timeout=10)
            (root / 'results.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
            print(json.dumps(result, indent=2))


if __name__ == '__main__': main()
