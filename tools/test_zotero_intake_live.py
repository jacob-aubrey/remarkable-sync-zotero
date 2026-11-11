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
            for (const label of ['Open source link', 'Find and attach PDF online', 'Attach downloaded PDF', 'Show reference in Zotero']) {
              ensure([...doc.querySelectorAll('button')].some(b => b.textContent === label), 'Missing intake action: ' + label);
            }
            const lookup = [...doc.querySelectorAll('button')].find(b => b.textContent === 'Check my Zotero library');
            lookup.click();
            for (let i = 0; i < 100 && !doc.querySelector('.reference-comparison'); i++) await Zotero.Promise.delay(50);
            ensure(doc.querySelector('.reference-comparison')?.textContent.includes('Existing article record'), 'Matching DOI comparison missing: ' + doc.getElementById('message').textContent);
            ensure(doc.querySelector('.reference-pdf-actions').open === false, 'PDF actions should start collapsed');
            ensure(doc.querySelector('[data-queue-summary]').textContent.includes('1 need a PDF'), 'Wrong Inbox summary');
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
          }
"""

def checks(client, root, checked):
    client.authorize()
    result = live.command(root, 'phone_intake')
    catalog = client.review_action('catalog')
    assert result['packagedDiscovery']
    assert result['parentKey'] in catalog['parents']
    assert 'missing_pdf_parents' in catalog
    checked('native_item_events_reference_actions_and_bounded_filename_status_' + result['version'])
    return None

if __name__ == '__main__':
    marker = "          else if (command.action === 'reindex')"
    live.BOOTSTRAP = live.BOOTSTRAP.replace(marker, BRANCH + marker, 1)
    live.native_checks = checks
    live.main()
