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
            ui.tagEditor = {key: command.key, page: 0, page_id: 'tag-layout-page', token: 'fixture',
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
              ensure(box.top - label.bottom >= 0 && box.top - label.bottom <= 10,
                kind + ' tag label gap is ' + (box.top - label.bottom));
              ensure(add.left >= box.right && Math.abs(add.top - box.top) <= 2,
                kind + ' Add tag offset is ' + (add.top - box.top));
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
"""

def checks(client, root, checked):
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
    source = root / 'inbox-lifecycle.pdf'
    source.write_bytes(live.pdf_bytes())
    office = root / 'inbox-lifecycle.docx'
    word = Document()
    word.add_paragraph('Synthetic Word attachment for Inbox review.')
    word.save(office)
    lifecycle = live.command(root, 'reference_pdf_lifecycle', source=str(source), office=str(office))
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
    intake = root / 'ToSort'
    intake.mkdir()
    matched_pdf = intake / 'Synthetic ToSort match.pdf'
    write_intake_pdf(matched_pdf, 'Synthetic unique ToSort match', doi='10.1000/tosort-native')
    matched = live.command(root, 'tosort_pdf_match', inbox=str(intake), source=str(matched_pdf))
    checked('native_ToSort_DOI_match_requires_confirmation_and_verifies_cleanup_and_filing_selection')
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
