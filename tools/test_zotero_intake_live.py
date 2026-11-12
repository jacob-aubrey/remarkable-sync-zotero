"""Verify reference intake rendering and native API compatibility in an isolated Zotero profile."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import test_zotero_plugin_live as live

BRANCH = r"""
          else if (command.action === 'phone_intake') {
            const ensure = (value, message) => {if (!value) throw Error(message);};
            clearTimeout(ui.timer); clearTimeout(ui.inboxTimer);
            ensure(typeof Zotero.Attachments.addAvailableFile === 'function', 'Native file resolver missing');
            ensure(typeof Zotero.Attachments.importFromFile === 'function', 'Native import missing');
            ui.zoteroInboxDirty = false;
            const parent = new Zotero.Item('journalArticle');
            parent.setField('title', 'Paper shared from phone'); parent.setField('url', 'https://example.org/paper');
            await parent.saveTx();
            ensure(ui.zoteroInboxDirty, 'Native item save did not flag Inbox discovery');
            parent.setField('DOI', '10.1000/inbox-test'); await parent.saveTx();
            const existing = new Zotero.Item('journalArticle');
            existing.setField('title', 'Existing article record');
            existing.setField('DOI', '10.1000/inbox-test'); await existing.saveTx();
            const row = {id: 'phone-reference', kind: 'reference', identity: parent.key, digest: 'fixture', status: 'pending',
              payload: {title: 'Paper shared from phone', url: 'https://example.org/paper', parent: parent.key}};
            ui.latest = {...ui.latest, reviews: [row]}; ui.tab = 'inbox'; ui.selected = row.id;
            ui.render();
            const doc = ui.view.document;
            for (let i = 0; i < 100 && !doc.querySelector('.reference-pdf-actions'); i++) await Zotero.Promise.delay(50);
            for (const label of ['Open source link', 'Find and attach PDF online', 'Attach downloaded PDF', 'Show reference in Zotero']) {
              ensure([...doc.querySelectorAll('button')].some(b => b.textContent === label), 'Missing intake action: ' + label);
            }
            const lookup = [...doc.querySelectorAll('button')].find(b => b.textContent === 'Check my Zotero library');
            lookup.click();
            for (let i = 0; i < 100 && !doc.querySelector('.reference-comparison'); i++) await Zotero.Promise.delay(50);
            ensure(doc.querySelector('.reference-comparison')?.textContent.includes('Existing article record'), 'Matching DOI comparison missing: ' + doc.getElementById('message').textContent);
            ensure(doc.querySelector('.reference-pdf-actions').textContent.includes('Save the PDF to Downloads'),
              'Inbox did not show where to save the download');
            ensure([...doc.querySelectorAll('.reference-pdf-actions button')].every(button => button.getClientRects().length && !button.disabled),
              'PDF actions should be visible and usable without expanding a section');
            ensure(doc.querySelector('[data-inbox-filter="needs-pdf"]').textContent === 'Needs a PDF (1)', 'Wrong Inbox count');
            ensure(!doc.querySelector('.review-layout > aside button[data-button-help]'), 'Inbox rows still have hover help');
            const dismiss = [...doc.querySelectorAll('button')].find(button => button.textContent === 'Dismiss this reference');
            const originalAction = ui.action, decisions = [];
            const wait = async predicate => {
              for (let i = 0; i < 80 && !predicate(); i++) await Zotero.Promise.delay(25);
              ensure(predicate(), 'Dismiss confirmation did not reach the expected state');
            };
            ui.action = async (method, params) => decisions.push({method, params});
            try {
              dismiss.click(); await wait(() => doc.querySelector('.review-dismiss[open]'));
              let dialog = doc.querySelector('.review-dismiss');
              ensure(doc.activeElement.textContent === 'Cancel', 'Dismiss should default to Cancel');
              dialog.querySelector('button[value="cancel"]').click();
              await wait(() => !doc.querySelector('.review-dismiss'));
              ensure(decisions.length === 0, 'Cancel dismissed a paper');
              dismiss.click(); await wait(() => doc.querySelector('.review-dismiss[open]'));
              doc.querySelector('.review-dismiss button[value="dismiss"]').click();
              await wait(() => decisions.length === 1);
              ensure(decisions[0].method === 'dismiss_review' && decisions[0].params.identity === row.id, 'Confirmed dismissal targeted the wrong review');
            } finally { ui.action = originalAction; }
            const referenceActions = [...doc.querySelectorAll('button')].filter(button =>
              ['Attach downloaded PDF', 'Find and attach PDF online', 'Dismiss this reference'].includes(button.textContent));
            const originalRPC = ui.rpc;
            const originalConfig = ui.latest.config;
            let finishCheck;
            ui.latest = {...ui.latest, config: {...originalConfig, inbox: 'synthetic'}};
            ui.lastInboxFolderCheck = 0;
            ui.inboxRenderPending = false;
            ui.rpc = method => {
              ensure(method === 'scan_inbox', 'Background check called an unexpected action');
              return new Promise(resolve => { finishCheck = resolve; });
            };
            try {
              const poll = ui.checkInbox();
              ensure(ui.inboxChecking && finishCheck, 'Synthetic background check did not start');
              ui.disable(ui.busy);
              ensure(referenceActions.length === 3 && referenceActions.every(button => button.disabled),
                'The regression fixture must start with disabled reference actions');
              finishCheck(ui.latest);
              await poll;
              ensure(referenceActions.every(button => button.isConnected && !button.disabled),
                'Unchanged background check left the reference actions disabled');
            } finally {
              ui.rpc = originalRPC;
              ui.latest = {...ui.latest, config: originalConfig};
            }
            ui.busy = false; ui.operation = null; ui.updateActivity();
            const main = doc.querySelector('main'), top = main.getBoundingClientRect().top;
            for (const text of ['Ready', 'VeryLongFilename'.repeat(150), 'Short again']) {
              ui.message(text);
              ensure(Math.abs(main.getBoundingClientRect().top - top) < 1, 'Filename status moved Inbox');
            }
            const collection = new Zotero.Collection(); collection.name = 'Synthetic tablet root'; await collection.saveTx();
            const savedConfig = JSON.parse(await IOUtils.readUTF8(PathUtils.join(ui.statePath, 'config.json')));
            const cloudRoot = '11111111-1111-4111-8111-111111111111';
            await IOUtils.writeUTF8(PathUtils.join(ui.statePath, 'config.json'), JSON.stringify({...savedConfig,
              zotero_root: collection.key, tablet_root: cloudRoot, default_folder: cloudRoot,
              ocr: false, automatic: false, sync_paused: true}));
            await ui.closeProcess(ui.process);
            const discovered = await ui.rpc('scan_inbox', {discover: true});
            ensure(discovered.reviews.some(row => row.kind === 'reference' && row.identity === parent.key), 'Packaged worker failed local reference discovery');
            ensure(discovered.config.sync_paused, 'Discovery resumed tablet sync');
            response.packagedDiscovery = true;
            response.parentKey = parent.key;
            response.version = Zotero.version;
            response.originalOCR = savedConfig.ocr ?? true;
            response.alertFixture = {...discovered, reviews: discovered.reviews.filter(row => row.identity === parent.key)};
          }
          else if (command.action === 'reference_pdf_lifecycle') {
            const ensure = (value, message) => {if (!value) throw Error(message);};
            clearTimeout(ui.timer); clearTimeout(ui.inboxTimer);
            // Zotero isolates each extension's bootstrap scope. Load the exact
            // packaged helpers for the direct native assertions below.
            const native = {Zotero, IOUtils};
            Services.scriptloader.loadSubScript('chrome://notable/content/library.js', native);
            const waitFor = async (condition, message) => {
              for (let i = 0; i < 100; i++) {
                if (condition()) return;
                await Zotero.Promise.delay(50);
              }
              throw Error(message);
            };
            const makeReference = async (title, type = 'journalArticle') => {
              const item = new Zotero.Item(type);
              item.setField('title', title);
              await item.saveTx();
              return item;
            };
            const importPDF = async parent => Zotero.Attachments.importFromFile({
              file: command.source, parentItemID: parent.id, contentType: 'application/pdf'
            });
            const discover = () => ui.rpc('scan_inbox', {discover: true});
            const referenceRow = (status, parent) => status.reviews.find(
              row => row.kind === 'reference' && row.identity === parent.key);
            const requireReference = (status, parent) => {
              const row = referenceRow(status, parent);
              ensure(row, 'Missing Inbox reference for ' + parent.key);
              return row;
            };
            const expectState = async (key, state, parent, attachment) => {
              const result = await native.notableReferencePDFState(key);
              ensure(result.state === state, 'Expected ' + state + ' for ' + key + ': ' + JSON.stringify(result));
              ensure(result.parent === parent, 'Unexpected parent in ' + JSON.stringify(result));
              if (attachment) ensure(result.attachment === attachment, 'Unexpected attachment in ' + JSON.stringify(result));
              return result;
            };
            const doc = ui.view.document;
            const showRow = (row, status) => {
              ui.latest = {...status, reviews: [row]};
              ui.tab = 'inbox'; ui.selected = row.id; ui.render();
            };
            const expectNoAdd = async (row, status, state) => {
              showRow(row, status);
              await waitFor(() => doc.querySelector('[data-reference-pdf-state="' + state + '"]'),
                'Inbox did not display ' + state + ' for ' + row.identity);
              ensure(!doc.querySelector('.reference-pdf-actions'), 'Inbox offered to add a PDF in state ' + state);
            };

            const article = await makeReference('Synthetic article receiving a PDF');
            const missing = await makeReference('Synthetic article with an unavailable PDF');
            const office = await makeReference('Synthetic reference receiving a Word file', 'document');
            const patent = await makeReference('Synthetic patent with reMarkable notes', 'patent');
            const duplicate = await makeReference('Synthetic patent before merge', 'patent');
            const discarded = await makeReference('Synthetic reference sent to Trash');
            const initial = await discover();
            const articleRow = requireReference(initial, article);
            const officeRow = requireReference(initial, office);
            const patentRow = requireReference(initial, patent);
            const duplicateRow = requireReference(initial, duplicate);
            const discardedRow = requireReference(initial, discarded);
            await expectState(article.key, 'none', article.key);
            showRow(articleRow, initial);
            await waitFor(() => doc.querySelector('.reference-pdf-actions'), 'Inbox hid PDF actions for a reference without a PDF');

            const pdf = await importPDF(article);
            ensure(await native.notablePDFDigest(pdf, Zotero.getMainWindow().crypto) === command.digest,
              'The reader did not compute the same PDF digest as the sync worker');
            await expectState(article.key, 'available', article.key, pdf.key);
            await expectNoAdd(articleRow, initial, 'available');
            const existing = await native.notableAcquirePDF(article.key, command.source, {describe: true});
            ensure(existing.attachment.key === pdf.key && existing.outcome === 'existing' && article.getAttachments().length === 1,
              'A late PDF caused the stale Add action to import a second copy');
            const available = await discover();
            const preparing = requireReference(available, article);
            ensure(preparing.payload.pdf_state === 'preparing' && preparing.payload.preparing_attachment === pdf.key,
              'The original Inbox row did not stay visible while the PDF settled');
            ui.inboxFilter = 'needs-pdf';
            ui.latest = available; ui.render();
            await waitFor(() => doc.querySelector('[data-reference-pdf-state="preparing"]'), 'Preparing filename status missing');
            ensure(ui.selected === articleRow.id && ui.inboxFilter === 'all', 'Preparing PDF lost the selected reference');
            await Zotero.Promise.delay(2200);
            const settled = await discover();
            const filingRow = settled.reviews.find(row => row.kind === 'zotero' && row.identity === pdf.key && row.payload.parent === article.key);
            ensure(filingRow,
              'Settled ordinary Zotero PDF did not enter its own filing review');
            ui.latest = settled; ui.render();
            ensure(ui.selected === filingRow.id && ui.renderedReviewId === filingRow.id, 'Filing pane jumped to a different item');
            ensure(filingRow.inbox_order === articleRow.inbox_order, 'PDF arrival changed the original Inbox date order');
            const chosen = () => doc.querySelector('.review-layout > aside [aria-pressed="true"]');
            ensure(chosen()?.querySelector('.inbox-row-status').textContent === 'Zotero · PDF filing'
              && !doc.querySelector('.reference-pdf-actions'), 'Filing review displayed reference intake controls');
            const filler = Array.from({length: 28}, (_, index) => ({...articleRow, id: `filler-${index}`,
              inbox_order: articleRow.inbox_order + index + 100, payload: {...articleRow.payload, title: `Z test paper ${index}`}}));
            ui.latest = {...settled, reviews: [...settled.reviews, ...filler]}; ui.render();
            const sort = doc.querySelector('select[aria-label="Sort Inbox"]');
            sort.value = 'title-desc'; sort.dispatchEvent(new ui.view.Event('change'));
            ensure(ui.selected === filingRow.id && chosen()?.textContent.includes(filingRow.payload.title), 'Sorting lost the selection');
            const list = doc.querySelector('.review-layout > aside');
            const itemBox = chosen().getBoundingClientRect(), listBox = list.getBoundingClientRect();
            ensure(list.scrollTop > 0 && itemBox.bottom <= listBox.bottom + 1 && itemBox.top >= listBox.top - 1,
              'Sorting moved the selected paper out of view');
            ensure(Zotero.Prefs.get('notable.inboxSort', true) === 'title-desc', 'Sort direction was not saved');
            ui.latest = settled;
            doc.querySelector('[data-inbox-filter="ready"]').click();
            ensure(ui.selected === filingRow.id && doc.querySelectorAll('.review-layout > aside button').length === 1,
              'Ready to file showed references without PDFs');
            doc.querySelector('[data-inbox-filter="all"]').click();

            const missingPDF = await importPDF(missing);
            const missingPath = await missingPDF.getFilePathAsync();
            const movedPath = missingPath + '.fixture-displaced';
            await IOUtils.move(missingPath, movedPath);
            try {
              await expectState(missing.key, 'unavailable', missing.key, missingPDF.key);
              const unavailable = await discover();
              await expectNoAdd(requireReference(unavailable, missing), unavailable, 'unavailable');
              ensure([...doc.querySelectorAll('.reference-intake button')].some(
                button => button.textContent === 'Show PDF attachment in Zotero'),
                'Inbox did not offer to inspect the unavailable attachment in Zotero');
            } finally {
              await IOUtils.move(movedPath, missingPath);
            }
            await expectState(missing.key, 'available', missing.key, missingPDF.key);
            const restored = await discover();
            ensure(requireReference(restored, missing).payload.pdf_state === 'preparing', 'Restored PDF still appeared unavailable');

            const word = await Zotero.Attachments.importFromFile({file: command.office, parentItemID: office.id,
              contentType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'});
            await expectState(office.key, 'file', office.key, word.key);
            await expectNoAdd(officeRow, initial, 'file');
            ensure(!referenceRow(await discover(), office), 'Office attachment left a stale Add PDF reference');

            const original = await importPDF(patent);
            const managed = await importPDF(patent);
            managed.setField('title', 'Main text (reMarkable notes)');
            managed.setNote('<p>Notable pilot: synthetic-connection</p>');
            await managed.saveTx();
            await Zotero.DB.executeTransaction(() => Zotero.Items.trash([original.id]));
            await expectState(patent.key, 'managed', patent.key, managed.key);
            await expectNoAdd(patentRow, initial, 'managed');
            const {mergeItems} = ChromeUtils.importESModule('chrome://zotero/content/mergeItems.mjs');
            await mergeItems(patent, [duplicate]);
            const survivor = await expectState(duplicate.key, 'managed', patent.key, managed.key);
            ensure(survivor.replacement, 'Native merge survivor was not reported');
            await expectNoAdd(duplicateRow, initial, 'replaced');
            const opened = await native.notableNavigateReview({target: 'references', keys: [duplicate.key]});
            ensure(opened.keys[0] === patent.key && opened.replacements?.[0].from === duplicate.key,
              'Show reference in Zotero did not identify the exact survivor');
            await Zotero.DB.executeTransaction(() => Zotero.Items.trash([discarded.id]));
            await expectNoAdd(discardedRow, initial, 'removed');
            const final = await discover();
            ensure(!referenceRow(final, patent) && !referenceRow(final, duplicate), 'Managed patent or merged duplicate returned to Inbox');
            ensure(!referenceRow(final, discarded), 'Trashed reference returned to Inbox');
            ensure(!final.reviews.some(row => row.kind === 'zotero' && [original.key, managed.key].includes(row.identity)),
              'Trashed original or managed notes PDF entered a filing review');
            ensure(final.config.sync_paused, 'Local Inbox audit resumed tablet sync');
            ui.latest = final;
            const methods = [], originalRPC = ui.rpc;
            ui.rpc = function(method, params) {methods.push(method); return originalRPC.call(this, method, params);};
            try { await ui.action('check_inbox'); }
            finally { ui.rpc = originalRPC; }
            ensure(methods.includes('scan_inbox') && methods.every(method => ['recheck_review', 'scan_inbox', 'status'].includes(method)),
              'Check Inbox sent a non-local request: ' + methods.join(', '));
            ensure(ui.latest.config.sync_paused, 'Check Inbox resumed tablet sync');
            response.layoutFixture = {...ui.latest, reviews: [duplicateRow]};
            response.checks = ['native_PDF_arrival_creates_filing_review', 'native_unavailable_PDF_hides_add_and_recovers',
              'native_PDF_preparation_preserves_selection_and_Inbox_order', 'native_Inbox_sort_persists_and_ready_filter_preserves_selection',
              'native_managed_patent_and_merge_survivor_hide_add', 'native_stale_trashed_reference_hides_add',
              'explicit_inbox_check_preserves_pause_and_sends_local_requests', 'native_office_arrival_hides_add'];
          }
          else if (command.action === 'restore_intake_test_ocr') {
            const path = PathUtils.join(ui.statePath, 'config.json');
            const config = JSON.parse(await IOUtils.readUTF8(path));
            await IOUtils.writeUTF8(path, JSON.stringify({...config, ocr: command.enabled}));
            await ui.closeProcess(ui.process);
            await ui.refresh();
          }
          else if (command.action === 'tosort_pdf_match') {
            const ensure = (value, message) => {if (!value) throw Error(message);};
            const waitFor = async (predicate, message) => {
              for (let i = 0; i < 160; i++) {
                if (await predicate()) return;
                await Zotero.Promise.delay(50);
              }
              throw Error(message + ' · ' + ui.view.document.getElementById('message').textContent);
            };
            clearTimeout(ui.inboxTimer);
            const configPath = PathUtils.join(ui.statePath, 'config.json');
            const config = JSON.parse(await IOUtils.readUTF8(configPath));
            await IOUtils.writeUTF8(configPath, JSON.stringify({...config, inbox: command.inbox}));
            await ui.closeProcess(ui.process);
            const parent = new Zotero.Item('journalArticle');
            parent.setField('title', 'Synthetic unique ToSort match');
            parent.setField('DOI', '10.1000/tosort-native');
            await parent.saveTx();
            const expected = await Zotero.Utilities.Internal.md5Async(command.source);
            const scan = () => ui.rpc('scan_inbox', {discover: true});
            await scan();
            await Zotero.Promise.delay(2250);
            ui.latest = await scan();
            const reference = ui.latest.reviews.find(row => row.kind === 'reference' && row.identity === parent.key);
            ensure(reference?.intake_pdf?.doi === '10.1000/tosort-native', 'Native DOI suggestion missing');
            ensure(parent.getAttachments().length === 0 && await IOUtils.exists(command.source), 'Discovery changed Zotero or ToSort');
            ui.tab = 'inbox'; ui.inboxFilter = 'needs-pdf'; ui.selected = reference.id; ui.render();
            const doc = ui.view.document;
            const button = label => [...doc.querySelectorAll('button')].find(node => node.textContent === label);
            await waitFor(() => button('Review ToSort file separately'), 'Separate review button missing');
            ensure(!ui.inboxRows().some(row => row.id === reference.intake_pdf.id), 'Suggested file duplicated the Zotero entry');
            button('Review ToSort file separately').click();
            await waitFor(() => ui.renderedReviewId === reference.intake_pdf.id, 'Separate review did not open the ordinary ToSort entry');
            ui.separateIntakePDFs.clear(); ui.inboxFilter = 'needs-pdf'; ui.selected = reference.id; ui.render();
            await waitFor(() => button('Attach PDF from ToSort'), 'Explicit attachment button missing');
            button('Attach PDF from ToSort').click();
            await waitFor(() => !ui.busy && parent.getAttachments().length === 1 && !ui.inboxRows().some(row => row.id === reference.intake_pdf.id),
              'Confirmed ToSort attachment did not complete');
            const attachment = await Zotero.Items.getAsync(parent.getAttachments()[0]);
            ensure(attachment.parentKey === parent.key && attachment.isStoredFileAttachment(), 'PDF was not stored under the existing reference');
            ensure(await Zotero.Utilities.Internal.md5Async(await attachment.getFilePathAsync()) === expected, 'Stored PDF bytes changed');
            ensure(!await IOUtils.exists(command.source), 'Verified source stayed in ToSort');
            ensure(ui.latest.config.sync_paused, 'Attach PDF resumed tablet sync');
            await Zotero.Promise.delay(2250);
            await ui.checkInbox();
            const filing = ui.latest.reviews.find(row => row.kind === 'zotero' && row.identity === attachment.key);
            ensure(filing?.id === ui.renderedReviewId && ui.inboxFilter === 'all', 'ToSort attachment did not keep its filing form selected');
            ensure(filing.inbox_order === reference.inbox_order, 'ToSort attachment changed Inbox arrival order');
            response.attachment = attachment.key;
          }
          else if (command.action === 'tag_entry_layout') {
            const ensure = (value, message) => {if (!value) throw Error(message);};
            ui.tab = 'tags';
            ui.tagEditor = {key: command.key, page: 0, page_id: `tag-layout-page-${command.width}`, token: 'fixture',
              title: 'Synthetic tag editor', paper_tags: ['Existing paper tag'], page_tags: [], vocabulary: []};
            ui.render();
            ui.view.resizeTo(command.width, 760);
            const doc = ui.view.document;
            doc.documentElement.style.colorScheme = 'dark';
            await Zotero.Promise.delay(180);
            for (const kind of ['paper', 'page']) {
              const input = doc.querySelector(`input[data-notable-field="Add ${kind} tag"]`);
              const entry = input.closest('.tag-editor-entry');
              const box = input.getBoundingClientRect(), label = input.closest('label').firstElementChild.getBoundingClientRect();
              const button = entry.querySelector(`button[aria-label="Add ${kind} tag"]`);
              const add = button.getBoundingClientRect();
              ensure(label.right <= box.left && label.bottom > box.top && label.top < box.bottom,
                kind + ' tag label is not on the left of the field');
              ensure(add.left >= box.right && Math.abs(add.top - box.top) <= 2,
                kind + ' Add tag offset is ' + (add.top - box.top));
              if (kind === 'page') ensure(entry.parentElement.querySelector('.tag-chips').textContent.includes('No page tags yet'), 'Empty page tags lack a placeholder');
              ensure([...entry.parentElement.querySelectorAll('.recent-tag-list button')].some(button => button.textContent === 'Reviewed in Inbox'),
                'A recently filed tag is not available for quick reuse');
              input.value = `A new ${kind} tag`;
              input.dispatchEvent(new ui.view.KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true}));
              ensure(entry.closest('.tag-editor-scope').querySelector('.tag-chips').textContent.includes(`A new ${kind} tag`),
                'Enter did not add a draft ' + kind + ' tag');
            }
            const footer = doc.querySelector('.tag-editor-footer');
            ensure(footer === footer.parentElement.lastElementChild, 'Tag editor actions are not at the bottom');
            ensure([...footer.querySelectorAll('button')].map(node => node.textContent).join('|') === 'Save tags|Close page editor',
              'The two footer actions are not together');
            ensure(doc.documentElement.scrollWidth <= ui.view.innerWidth + 1, 'Tag editor overflowed its viewport');
            ui.tagEditor = null;
          }
          else if (command.action === 'inbox_filing_editor') {
            const ensure = (value, message) => {if (!value) throw Error(message);};
            const doc = ui.view.document;
            await ui.refresh();
            const wait = async (predicate, message) => {
              for (let i = 0; i < 180; i++) {
                if (predicate()) return;
                await Zotero.Promise.delay(50);
              }
              throw Error(message + ' · ' + doc.getElementById('message').textContent);
            };
            const row = ui.latest.reviews.find(row => row.kind === 'zotero' && row.identity === command.key);
            ensure(row, 'Expected a real Zotero review');
            ui.tab = 'inbox'; ui.selected = row.id; ui.inboxFilter = 'all'; ui.render();
            const button = label => [...doc.querySelectorAll('button')].find(node => node.textContent === label);
            const field = label => [...doc.querySelectorAll('[data-notable-field]')].find(node => node.dataset.notableField === label);
            const set = (label, value, event = 'input') => {
              const input = field(label); ensure(input, 'Missing field ' + label); input.value = value;
              input.dispatchEvent(new ui.view.Event(event, {bubbles: true}));
            };
            const filename = () => field('Proposed filename').value;
            const previous = filename();
            set('Document type', 'book', 'change');
            await wait(() => filename() !== previous && /book/i.test(filename()), 'Type correction did not refresh the proposed filename');
            doc.querySelector('[data-review-detail="citation"]').open = true;
            set('Creators (one per line)', 'Reviewer, Jane');
            set('Date or year', '2025-08-01');
            set('Journal, publisher, or institution', 'Verified Journal');
            set('DOI', '10.1000/reviewed');
            button('Suggest filename').click();
            await wait(() => !ui.busy && filename().includes('2025'), 'Citation edits did not inform the filename');
            doc.querySelector('[data-review-detail="paper-tags"]').open = true;
            set('Add paper tag', 'Reviewed in Inbox');
            const control = doc.querySelector('[data-review-detail="paper-tags"] button[aria-label="Add paper tag"]');
            control.click();
            await wait(() => doc.querySelector('[data-review-detail="paper-tags"] .tag-chips').textContent.includes('Reviewed in Inbox'), 'Manual filing tag was not added');
            button('Approve for reMarkable').click();
            await wait(() => !ui.busy && !ui.filingQueue.length && !ui.latest.reviews.some(value => value.id === row.id), 'Citation and tag filing did not finish');
            const parent = await Zotero.Items.getByLibraryAndKeyAsync(Zotero.Libraries.userLibraryID, row.payload.parent);
            ensure(parent.getField('DOI') === '10.1000/reviewed' && parent.getField('publicationTitle') === 'Verified Journal'
              && parent.getField('date') === '2025-08-01' && parent.getCreators()[0].lastName === 'Reviewer', 'The exact Zotero citation was not updated');
            ensure(parent.getTags().some(row => row.tag === 'Reviewed in Inbox'), 'The approved tag was not applied to Zotero');
            ensure(JSON.parse(Zotero.Prefs.get('notable.recentTags', true))[0] === 'Reviewed in Inbox', 'The recent tag was not saved');
            ensure(ui.latest.config.sync_paused, 'Approving the paper started tablet sync');
            const saved = {queue: ui.filingQueue, busy: ui.busy, operation: ui.operation};
            try {
              ui.filingQueue = Array.from({length: 30}, (_, index) => ({id: `job-${index}`, title: 'A queued paper', method: 'approve', params: {identity: `pending-${index}`}}));
              ui.busy = true; ui.operation = {method: 'approve', title: 'Filing this paper', detail: 'Long filename '.repeat(100), started: Date.now(), updated: Date.now()};
              ui.render();
              const queue = doc.querySelector('.filing-queue'), activity = doc.getElementById('activity');
              ensure(!queue.open && queue.getBoundingClientRect().height < 48, 'The filing queue starts out taking over the Inbox');
              ensure(activity.getBoundingClientRect().height < 76, 'The filing progress strip is too tall');
              queue.open = true;
              ensure(queue.querySelector('.filing-queue-jobs').clientHeight <= 190, 'The expanded filing queue does not scroll within its height');
            } finally { ui.filingQueue = saved.queue; ui.busy = saved.busy; ui.operation = saved.operation; ui.filingQueueOpen = false; ui.render(); }
          }
          else if (command.action === 'inbox_alert_layout') {
            ui.latest = command.snapshot;
            ui.tab = 'inbox';
            ui.render();
            ui.view.resizeTo(command.width, 760);
            const doc = ui.view.document;
            doc.documentElement.style.colorScheme = 'dark';
            await Zotero.Promise.delay(150);
            const message = doc.getElementById('message');
            const pdfError = 'No available file found. Open the source link, download the PDF, and use Attach downloaded PDF.';
            for (const text of [pdfError, pdfError + '\n' + 'VeryLongFilename'.repeat(20) + '.pdf']) {
              ui.message(text, true);
              const bounds = message.getBoundingClientRect();
              const range = doc.createRange();
              range.selectNodeContents(message);
              const lines = [...range.getClientRects()];
              if (!lines.length || message.getAttribute('role') !== 'alert'
                  || message.scrollHeight > message.clientHeight + 1
                  || lines.some(line => line.bottom > bounds.bottom + 1 || line.right > bounds.right + 1)) {
                throw Error('Inbox clipped the alert at width ' + ui.view.innerWidth);
              }
            }
            ui.message(pdfError, true);
            response.alertWidth = ui.view.innerWidth;
          }
          else if (command.action === 'folder_deletion_layout') {
            const ensure = (value, message) => {if (!value) throw Error(message);};
            const saved = {rpc: ui.rpc, latest: ui.latest, syncFailure: ui.syncFailure, tab: ui.tab, busy: ui.busy, operation: ui.operation};
            const root = new Zotero.Collection(); root.name = 'Temporary decision root'; await root.saveTx();
            const folder = new Zotero.Collection(); folder.name = 'Test'; folder.parentID = root.id; await folder.saveTx();
            const issue = {zotero: folder.key, tablet: '11111111-1111-4111-8111-111111111112',
              zotero_name: 'Test', tablet_name: 'Test', zotero_state: '', tablet_state: 'trash',
              mirror: {side: 'zotero', evidence: 'fixture', children: 0}};
            const doc = ui.view.document;
            const calls = [];
            const button = label => [...doc.querySelectorAll('button')].find(node => node.textContent === label);
            ui.view.resizeTo(command.width, 760);
            await Zotero.Promise.delay(180);
            try {
              ui.latest = {...command.snapshot, reviews: [], deletions: [], local_deletions: [], folder_issues: [issue],
                status: {}, config: {...command.snapshot.config, zotero_root: root.key, automatic: false, sync_paused: command.paused}};
              const failure = 'A linked collection/folder was deleted or moved outside the selected roots. Review it before retrying.';
              ui.tab = 'inbox'; ui.syncFailure = failure; ui.render();
              ui.message(ui.statusMessage());
              const message = doc.getElementById('message');
              const range = doc.createRange(); range.selectNodeContents(message);
              const lines = [...range.getClientRects()], bounds = message.getBoundingClientRect();
              ensure(message.textContent === failure && message.getAttribute('role') === 'alert' && lines.length
                && lines.every(line => line.right <= bounds.right + 1 && line.bottom <= bounds.bottom + 1),
                'The saved sync failure was clipped after render');
              ui.syncFailure = ''; ui.render();
              const conflict = button('Sync Conflicts (1)');
              ensure(conflict, 'Missing folder decision count');
              conflict.click();
              for (let i = 0; i < 40 && !button('Keep deletion on this device'); i++) await Zotero.Promise.delay(25);
              ensure(doc.querySelector('.folder-sync-decision')?.textContent.includes('reMarkable folder “Test” is in Trash'),
                'The folder decision did not identify the deleted folder and side');
              ensure(!doc.querySelector('main').textContent.includes('No sync conflicts'), 'The empty message hid the folder decision');
              ui.rpc = async (method, params) => {
                calls.push(method);
                if (method === 'confirm_folder_change') {
                  ensure(JSON.stringify(params.issue) === JSON.stringify(issue), 'Wrong folder confirmation payload');
                  ensure(params.choice === (command.paused ? 'mirror' : 'keep'), 'Wrong folder deletion choice');
                  return {...ui.latest, folder_issues: [], folder_change: 'confirmed'};
                }
                ensure(method === 'sync', 'Unexpected RPC: ' + method);
                return {...ui.latest, folder_change: undefined};
              };
              const choice = command.paused ? 'Also trash Zotero collection' : 'Keep deletion on this device';
              ui.busy = true;
              ui.operation = {method: 'sync', started: Date.now(), updated: Date.now(), detail: 'Synthetic sync'};
              ui.disable(true);
              button(choice).click();
              const dialog = () => doc.querySelector('.sync-decision-dialog[open]');
              const confirmation = () => dialog()?.querySelector('button[value="confirm"]:enabled');
              for (let i = 0; i < 80 && !confirmation(); i++) await Zotero.Promise.delay(25);
              ensure(!calls.length, 'The decision ran before confirmation');
              ensure(confirmation(), 'Confirmation was unavailable for ' + choice + ' · '
                + (dialog()?.textContent || doc.getElementById('message')?.textContent || 'No dialog opened'));
              ensure(dialog().scrollWidth <= dialog().clientWidth + 1, 'The confirmation overflowed the window');
              confirmation().click();
              for (let i = 0; i < 80 && !button('Cancel queued decision'); i++) await Zotero.Promise.delay(25);
              ensure(button('Cancel queued decision') && !button(choice) && !calls.length,
                'The card did not visibly queue and lock its choices during sync');
              ui.busy = false; ui.operation = null;
              await ui.drainSyncDecisions();
              ensure(calls.join(',') === 'confirm_folder_change' && !ui.busy, 'Unexpected queued decision requests: ' + calls.join(','));
              ensure(button('Sync Conflicts (0)'), 'Confirmation did not clear the resolved folder decision');
            } finally {
              ui.rpc = saved.rpc; ui.latest = saved.latest; ui.syncFailure = saved.syncFailure; ui.tab = saved.tab;
              ui.busy = saved.busy; ui.operation = saved.operation;
              clearTimeout(ui.timer); ui.render();
            }
          }
          else if (command.action === 'trash_matched_collection') {
            const ensure = (value, message) => {if (!value) throw Error(message);};
            const native = {Zotero};
            Services.scriptloader.loadSubScript('chrome://notable/content/library.js', native);
            const make = async (name, parent) => {
              const row = new Zotero.Collection(); row.name = name;
              if (parent) row.parentID = parent.id;
              await row.saveTx(); return row;
            };
            const root = await make('Disposable connected root');
            const folder = await make('Matching collection', root);
            const child = await make('Subcollection', folder);
            const other = await make('Unaffected collection', root);
            const paper = new Zotero.Item('journalArticle'); paper.setField('title', 'Kept in My Library');
            paper.setCollections([child.id, other.id]); await paper.saveTx();
            const inventory = [folder, child].map(row => [row.key, row.parentKey || '', row.name, row.version || 0])
              .sort(([left], [right]) => left < right ? -1 : left > right ? 1 : 0);
            let staleRejected = false;
            try { await native.notableTrashLinkedCollection({root: root.key, key: folder.key, inventory: inventory.slice(0, 1)}); }
            catch (_) { staleRejected = true; }
            ensure(staleRejected && !folder.deleted && !child.deleted, 'Changed collection inventory caused a deletion');
            const result = await native.notableTrashLinkedCollection({root: root.key, key: folder.key, inventory});
            ensure(result.trashed && folder.deleted && child.deleted, 'Zotero did not move the collection subtree to Trash');
            ensure(!root.deleted && !other.deleted && !paper.deleted && paper.getCollections().includes(other.id),
              'Deleting the collection changed other collections or trashed a paper');
            const restored = await native.notableRestoreLinkedCollection({root: root.key, key: folder.key, parent: root.key, version: folder.version});
            ensure(restored.restored && !folder.deleted && !child.deleted, 'The original Zotero collection and subcollection were not restored');
            await Zotero.DB.executeTransaction(() => Zotero.Items.trash([paper.id]));
            const restoredPaper = await native.notableRestoreLinkedItem({key: paper.key, parent: '', version: paper.version});
            ensure(restoredPaper.restored && !paper.deleted && paper.getCollections().includes(other.id),
              'The original Zotero reference was not restored');
            const sole = new Zotero.Item('book'); sole.setField('title', 'Only in the deleted branch');
            sole.setCollections([child.id]); await sole.saveTx();
            const link = await Zotero.Attachments.linkFromURL({url: 'https://example.invalid/fixture', parentItemID: sole.id, title: 'Saved link'});
            const snapshot = await native.notableCollectionDecision(root.key, folder.key);
            const shared = snapshot.items.find(item => item.key === paper.key);
            ensure(shared.locations.some(row => row.inside && row.path.includes('Matching collection → Subcollection'))
              && shared.locations.some(row => !row.inside && row.key === other.key), 'The shared paper locations were incomplete');
            ensure(snapshot.items.find(row => row.key === sole.key).children.some(row => row.key === link.key), 'Saved links were missing from the preview');
            const plan = {token: snapshot.token, items: snapshot.items.map(item => ({key: item.key, trash: item.key === sole.key, remove: []}))};
            paper.setField('title', 'Changed after confirmation'); await paper.saveTx();
            staleRejected = false;
            try { await native.notableTrashLinkedCollection({root: root.key, key: folder.key, plan}); }
            catch (_) { staleRejected = !folder.deleted && !sole.deleted; }
            ensure(staleRejected && !folder.deleted && !sole.deleted, 'A stale item preview allowed a trash operation');
            plan.token = (await native.notableCollectionDecision(root.key, folder.key)).token;
            await native.notableTrashLinkedCollection({root: root.key, key: folder.key, plan});
            ensure(folder.deleted && child.deleted && sole.deleted && !paper.deleted && paper.getCollections().includes(other.id),
              'The planned folder deletion did not preserve the shared paper and trash the selected item');
            const kept = await make('Another location', root);
            paper.addToCollection(kept.id); await paper.saveTx();
            const view = await Zotero.Attachments.linkFromURL({url: 'https://example.invalid/linked', parentItemID: paper.id, title: 'Linked copy'});
            view.setNote('Notable pilot: test attachment'); await view.saveTx();
            const paperPreview = await native.notablePaperDecision(paper.key);
            response.paperDecision = {parent: paper.key, attachment: view.key, choice: 'remove_collections',
              zotero_plan: {token: paperPreview.token, remove: [other.key]}};
            response.removedLocation = other.key;
            response.keptLocation = kept.key;
            response.trashedCollection = folder.key;
          }
"""

def checks(client, root, checked):
    import json
    import sqlite3
    from docx import Document
    from tests.test_zotero_intake import write_intake_pdf
    client.authorize()
    result = live.command(root, 'phone_intake')
    catalog = client.review_action('catalog')
    assert result['packagedDiscovery']
    assert result['parentKey'] in catalog['parents']
    assert 'missing_pdf_parents' in catalog
    checked('native_item_events_reference_actions_and_bounded_filename_status_' + result['version'])
    checked('native_unchanged_background_check_unlocks_pdf_and_dismiss_buttons')
    checked('native_dismiss_requires_explicit_confirmation')
    source = root / 'inbox-lifecycle.pdf'
    source.write_bytes(live.pdf_bytes())
    office = root / 'inbox-lifecycle.docx'
    word = Document()
    word.add_paragraph('Synthetic Word attachment for Inbox review.')
    word.save(office)
    from note_organizer.zotero_worker import file_sha
    lifecycle = live.command(root, 'reference_pdf_lifecycle', source=str(source), office=str(office), digest=file_sha(source))
    for name in lifecycle['checks']:
        checked(name)
    for width in [1040, 640]:
        layout = live.command(root, 'layout', snapshot=lifecycle['layoutFixture'], tab='inbox', width=width)['layout']
        assert layout['scrollWidth'] <= layout['width'] + 1, layout
        assert '1 to check in Zotero' in layout['queueSummary'], layout
        live.command(root, 'snapshot', filename=f'inbox-merge-{width}.png')
    checked('native_stale_merge_detail_fits_wide_and_narrow_Inbox_layouts')
    for width in [1040, 640]:
        live.command(root, 'inbox_alert_layout', snapshot=result['alertFixture'], width=width)
        live.command(root, 'snapshot', filename=f'inbox-alert-{width}.png')
    checked('native_Inbox_alerts_wrap_without_clipping_at_wide_and_narrow_widths')
    for width, paused in [(1040, False), (640, True)]:
        live.command(root, 'folder_deletion_layout', snapshot=result['alertFixture'], width=width, paused=paused)
    checked('native_folder_deletion_confirms_and_visibly_queues_during_sync_at_both_widths')
    trashed = live.command(root, 'trash_matched_collection')
    assert client.get('collection', trashed['trashedCollection'])['deleted'], 'The worker could not read a saved collection in Zotero Trash'
    checked('native_mirror_deletion_checks_contents_and_trashes_selected_items_while_preserving_shared_papers')
    request = trashed['paperDecision']
    client.review_action('trash', request)
    remaining = client.get('item', request['parent'])
    assert not remaining.get('deleted') and trashed['keptLocation'] in remaining['collections']
    assert trashed['removedLocation'] not in remaining['collections'] and not client.get('item', request['attachment']).get('deleted')
    checked('native_removing_a_selected_location_keeps_the_other_location_and_its_attachment')
    intake = root / 'ToSort'
    intake.mkdir()
    matched_pdf = intake / 'Synthetic ToSort match.pdf'
    write_intake_pdf(matched_pdf, 'Synthetic unique ToSort match', doi='10.1000/tosort-native')
    matched = live.command(root, 'tosort_pdf_match', inbox=str(intake), source=str(matched_pdf))
    checked('native_ToSort_DOI_match_requires_confirmation_and_verifies_cleanup_and_filing_selection')
    state = root / 'profile' / 'notable'
    config = json.loads((state / 'config.json').read_text(encoding='utf-8'))
    with sqlite3.connect(state / 'bridge' / 'pilot.sqlite3') as db:
        db.execute('INSERT OR IGNORE INTO bridge_values VALUES (?, ?)', ('folders', json.dumps({
            config['zotero_root']: {'cloud': config['tablet_root'], 'root': True}})))
    live.command(root, 'inbox_filing_editor', key=matched['attachment'])
    checked('native_filing_updates_filename_citation_and_tags_and_keeps_progress_and_queue_compact')
    for width in [1040, 640]:
        live.command(root, 'tag_entry_layout', key=matched['attachment'], width=width)
        live.command(root, 'snapshot', filename=f'tag-entry-{width}.png')
    checked('native_tag_editor_aligns_both_fields_and_buttons_and_keeps_footer_at_bottom')
    # The shared packaged search check runs after this fixture. Restore its OCR
    # setting before it asks the private worker to reindex a scanned PDF.
    live.command(root, 'restore_intake_test_ocr', enabled=result['originalOCR'])
    return None

if __name__ == '__main__':
    marker = "          else if (command.action === 'reindex')"
    live.BOOTSTRAP = live.BOOTSTRAP.replace(marker, BRANCH + marker, 1)
    live.native_checks = checks
    live.main()
