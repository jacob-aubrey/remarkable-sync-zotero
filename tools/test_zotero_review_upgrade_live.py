"""Exercise an actual saved legacy Review queue across a Zotero extension update.

This Windows-only integration test creates a disposable profile. It installs the
specified old complete XPI, seeds real synthetic references/PDFs and an issue-only
SQLite review queue, then upgrades to source or a specified complete new XPI.
The UI talks to its real worker; no findings or RPC results are mocked. A gate
only delays the automatic local recheck so an unsaved form draft can be entered.
The existing graphical test harness supplies profile isolation and shutdown.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import test_zotero_plugin_live as live
from tools.build_zotero_support import build
from note_organizer.zotero_worker import REVIEW_GUIDANCE_VERSION, Worker, file_sha
from note_organizer.zotero_review import fingerprint


UPGRADE_COMMANDS = """
          else if (command.action === 'upgrade_stop_helper') {
            clearTimeout(ui.timer);
            if (ui.process) await ui.closeProcess(ui.process, true, 'synthetic_upgrade_seed');
            ui.view?.close(); ui.view = null;
          }
          else if (command.action === 'upgrade_select_legacy') {
            await ui.refresh(); ui.selected = command.identity; ui.showAllReviews = true; ui.tab = 'review'; ui.render();
            response.body = ui.view.document.querySelector('body').textContent;
            response.review = ui.latest.reviews.find(row => row.id === command.identity);
          }
          else if (command.action === 'upgrade_install_open') {
            const oldUI = ui;
            if (command.worker) Zotero.Prefs.set('notable.developmentWorker', JSON.stringify(command.worker), true);
            else if (Services.prefs.prefHasUserValue('notable.developmentWorker')) Services.prefs.clearUserPref('notable.developmentWorker');
            const {AddonManager} = ChromeUtils.importESModule('resource://gre/modules/AddonManager.sys.mjs');
            const file = Cc['@mozilla.org/file/local;1'].createInstance(Ci.nsIFile); file.initWithPath(command.path);
            const install = await AddonManager.getInstallForFile(file); await install.install();
            const deadline = Date.now() + 30000;
            while ((!Zotero.Notable || Zotero.Notable === oldUI) && Date.now() < deadline) await Zotero.Promise.delay(50);
            const updated = Zotero.Notable;
            if (!updated || updated === oldUI) throw Error('New extension controller did not initialize');
            const rpc = updated.rpc.bind(updated);
            updated.syntheticUpgradeRequests = [];
            updated.syntheticUpgradeGate = new Promise(resolve => {updated.syntheticUpgradeRelease = resolve;});
            updated.rpc = async (method, params = {}) => {
              updated.syntheticUpgradeRequests.push({method, params: JSON.parse(JSON.stringify(params))});
              if (method === 'recheck_review' && updated.syntheticUpgradeGate) await updated.syntheticUpgradeGate;
              return rpc(method, params);
            };
            updated.open();
            while ((!updated.view?.document.getElementById('app')?.querySelector('h1')
                    || !updated.latest?.reviews?.some(row => row.id === command.identity)
                    || !updated.syntheticUpgradeRequests.some(value => value.method === 'recheck_review'))
                    && Date.now() < deadline) await Zotero.Promise.delay(50);
            if (!updated.syntheticUpgradeRequests.some(value => value.method === 'recheck_review')) throw Error('Opening a saved old queue did not schedule automatic local guidance');
            updated.selected = command.identity; updated.showAllReviews = true; updated.tab = 'review'; updated.render();
            const doc = updated.view.document;
            const label = [...doc.querySelectorAll('label')].find(node => node.firstElementChild?.textContent === 'Proposed filename');
            const filename = label?.querySelector('input');
            if (!filename) throw Error('Pending migration did not retain a renderable filename field');
            filename.value = 'My unsaved upgrade draft.pdf'; filename.dispatchEvent(new updated.view.Event('input'));
            const section = doc.querySelector('details[data-review-detail="citation"]'); if (section) section.open = true;
            doc.querySelector('body').scrollTop = 180;
            updated.syntheticUpgradeContext = {selected: updated.selected, outer: doc.querySelector('body').scrollTop,
              expanded: section?.dataset.reviewDetail || null};
            response.beforeContext = updated.syntheticUpgradeContext;
            response.body = doc.querySelector('body').textContent;
            response.requests = updated.syntheticUpgradeRequests;
            response.approvalDisabled = [...doc.querySelectorAll('button')].filter(button => button.dataset.approve === 'true').every(button => button.disabled);
          }
          else if (command.action === 'upgrade_release') {
            ui.syntheticUpgradeRelease(); ui.syntheticUpgradeGate = null;
            const deadline = Date.now() + 60000;
            while ((ui.busy || ui.reviewUpgradePromise || !ui.latest?.reviews?.find(row => row.id === command.identity)?.payload?.guidance_version)
                    && Date.now() < deadline) await Zotero.Promise.delay(50);
            if (ui.busy) throw Error('Automatic upgrade recheck did not finish');
            const doc = ui.view.document;
            response.snapshot = ui.latest;
            response.requests = ui.syntheticUpgradeRequests;
            response.selected = ui.selected;
            response.filenameDraftPreserved = ui.reviewDrafts.get(command.identity)?.filename === 'My unsaved upgrade draft.pdf';
            response.outer = doc.querySelector('body').scrollTop;
            response.maxOuter = doc.querySelector('body').scrollHeight - doc.querySelector('body').clientHeight;
            response.expandedPreserved = !ui.syntheticUpgradeContext.expanded || [...doc.querySelectorAll('details[data-review-detail]')]
              .some(section => section.dataset.reviewDetail === ui.syntheticUpgradeContext.expanded && section.open);
            ui.openLibraryCase(command.identity); response.librarySelected = ui.selected;
            const conflictIcon = doc.querySelector('nav .icon-sync-conflict');
            if (!conflictIcon) throw Error('The upgraded Sync Conflicts icon is missing');
            const iconStyle = ui.view.getComputedStyle(conflictIcon);
            response.upgradedIcon = {mask: iconStyle.maskImage, mapping: iconStyle.getPropertyValue('--button-icon'),
              stylesheets: [...doc.styleSheets].map(sheet => sheet.href)};
            response.body = doc.querySelector('body').textContent;
          }
          else if (command.action === 'upgrade_click_references') {
            const button = [...ui.view.document.querySelectorAll('button')].find(node => node.textContent === 'Compare in Zotero');
            if (!button) throw Error('Missing exact reference-set navigation after automatic migration');
            button.scrollIntoView({block: 'center'});
            button.click(); await Zotero.Promise.delay(200);
            response.selectedKeys = Zotero.getActiveZoteroPane().getSelectedItems().map(item => item.key);
          }
          else if (command.action === 'upgrade_watch') {
            if (command.start) {
              const watched = new Set();
              for (const key of command.keys) {
                const item = await Zotero.Items.getByLibraryAndKeyAsync(Zotero.Libraries.userLibraryID, key);
                if (item) watched.add(item.id);
              }
              const events = [], observer = {notify(event, type, ids) {
                const relevant = ids.filter(id => watched.has(id)); if (relevant.length) events.push({event, type, ids: relevant});
              }};
              const id = Zotero.Notifier.registerObserver(observer, ['item'], 'saved-review-upgrade-test', 1);
              globalThis.savedReviewUpgradeObservation = {id, events}; response.started = true;
            } else {
              const observation = globalThis.savedReviewUpgradeObservation;
              Zotero.Notifier.unregisterObserver(observation.id); response.events = observation.events;
              globalThis.savedReviewUpgradeObservation = null;
            }
          }
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir', type=Path, required=True)
    parser.add_argument('--zotero-exe', type=Path, required=True)
    parser.add_argument('--legacy-xpi', type=Path, required=True, help='Previous complete release, e.g. 0.11.0')
    parser.add_argument('--new-xpi', type=Path, help='New complete release to test with its actual bundled worker; omission uses source')
    parser.add_argument('--port', type=int, default=23219)
    args = parser.parse_args()
    root = args.work_dir.resolve()
    if root.exists():
        raise ValueError('Choose a new empty disposable work directory')
    current_xpi = args.new_xpi.resolve(strict=True) if args.new_xpi else build(root.with_name(root.name + '-source-upgrade.xpi'))
    worker_override = None if args.new_xpi else {'command': sys.executable,
        'arguments': [str(Path(__file__).with_name('notable_worker.py').resolve())]}
    with zipfile.ZipFile(args.legacy_xpi.resolve(strict=True)) as archive:
        legacy_version = json.loads(archive.read('manifest.json'))['version']
    with zipfile.ZipFile(current_xpi) as archive:
        source_version = json.loads(archive.read('manifest.json'))['version']
    marker = "          else if (command.action === 'reindex')"
    if marker not in live.BOOTSTRAP:
        raise RuntimeError('Graphical test instrumentation entry point changed')
    live.BOOTSTRAP = live.BOOTSTRAP.replace(marker, UPGRADE_COMMANDS + marker, 1)

    def upgrade_checks(client, work, checked):
        client.authorize()
        collection = 'UPGCL234'
        parents, attachments = ['UPGPA234', 'UPGPB234', 'UPGPC234'], ['UPGATA23', 'UPGATB23', 'UPGATC23']
        client.create('collection', {'key': collection, 'name': 'Synthetic saved Review upgrade'})
        for index, key in enumerate(parents):
            client.create('item', {'key': key, 'itemType': 'journalArticle',
                'title': 'Synthetic overlapping publication about nanoparticle imaging' if index < 2 else 'Synthetic interrupted recovery publication',
                'date': '2024', 'DOI': '10.9999/upgrade.shared' if index < 2 else '10.9999/upgrade.recovery',
                'publicationTitle': 'Synthetic Journal', 'collections': [collection]})
        source = work / 'synthetic-upgrade.pdf'; source.write_bytes(live.pdf_bytes())
        for parent, key in zip(parents, attachments):
            client.create('item', {'key': key, 'itemType': 'attachment', 'parentItem': parent,
                'title': 'Synthetic original PDF', 'contentType': 'application/pdf', 'linkMode': 'imported_file',
                'filename': 'Synthetic upgrade ' + key + '.pdf'})
            client.upload(key, source, '')
        live.command(work, 'upgrade_stop_helper')
        state = Worker(work / 'profile/notable')
        state.config.update(server_id=client.server_id, zotero_root=collection,
            tablet_root='synthetic-unpaired-tablet', automatic=False, sync_paused=True)
        state.save()
        state.state.write('folders', {collection: {'cloud': 'synthetic-unpaired-tablet', 'name': 'Synthetic saved Review upgrade'}})
        state.state.write('documents', {'untouched-ledger': {'source_key': 'KEEPSRC2', 'parent': 'KEEPPA23', 'baseline': 'unchanged-sentinel'}})
        identities = []
        for parent, key in zip(parents, attachments):
            path = client.file_path(key)
            # Deliberately no findings, guidance marker or metadata signature:
            # this is the persisted issue-only shape from before structured UI.
            payload = {'source': 'zotero', 'path': str(path), 'attachment': key, 'parent': parent,
                'title': 'Saved legacy paper ' + key, 'filename': 'Saved legacy ' + key + '.pdf',
                'document_type': 'journal_article', 'details': {}, 'routine': False,
                'issues': ['Identical content belongs to several Zotero attachments.',
                           'Another Zotero reference may describe the same work; check the references or versions.']}
            payload['review_token'] = fingerprint(payload)
            identities.append(state.review.offer('zotero', key, file_sha(path), payload))
        state.review.begin(identities[2], file_sha(client.file_path(attachments[2])), {
            'parent': parents[2], 'source': attachments[2], 'filename': 'Interrupted saved choice.pdf',
            'collection': collection, 'step': 'synthetic-saved-recovery-journal'})
        applying_before = state.review.row(identities[2])
        legacy_rows = state.review.rows()
        assert all('findings' not in row['payload'] and 'guidance_version' not in row['payload'] for row in legacy_rows)
        live.command(work, 'open')
        legacy = live.command(work, 'upgrade_select_legacy', identity=identities[0])
        assert 'Identical content belongs to several Zotero attachments.' in legacy['body']
        assert 'Compare in Zotero' not in legacy['buttons']
        live.command(work, 'snapshot', filename='legacy-saved-review.png')
        checked('actual_previous_XPI_reopens_persisted_issue_only_queue_without_current_navigation')

        def capture():
            return {'items': {key: client.get('item', key) for key in [*parents, *attachments]},
                'files': {key: file_sha(client.file_path(key)) for key in attachments},
                'bridge': hashlib.sha256(state.state.path.read_bytes()).hexdigest()}
        before = capture()
        live.command(work, 'upgrade_watch', start=True, keys=[*parents, *attachments])
        prepared = live.command(work, 'upgrade_install_open', identity=identities[0], path=str(current_xpi),
            worker=worker_override)
        assert prepared['approvalDisabled'], 'Old saved review could be approved before its guidance update'
        assert 'Updating this saved review' in prepared['body']
        checked('new_extension_automatically_starts_local_update_without_manual_Recheck_click')
        upgraded = live.command(work, 'upgrade_release', identity=identities[0])
        snapshot = upgraded['snapshot']
        current = next(row for row in snapshot['reviews'] if row['id'] == identities[0])
        assert current['payload']['guidance_version'] == REVIEW_GUIDANCE_VERSION
        assert state.review.row(identities[0])['payload']['guidance_version'] == REVIEW_GUIDANCE_VERSION, 'Guidance marker was not persisted in SQLite'
        assert isinstance(current['payload']['findings'], list) and current['payload']['findings']
        assert 'Compare in Zotero' in upgraded['buttons']
        assert 'Keep these PDFs separate' in upgraded['buttons']
        assert 'Show attachment in Zotero' in upgraded['buttons']
        assert upgraded['filenameDraftPreserved']
        assert 'Approve & file' not in upgraded['buttons'], 'Library Issues must not expose filing actions'
        assert upgraded['librarySelected'] == identities[0], 'The waiting paper did not open its exact library case'
        assert 'sync-conflict.svg' in upgraded['upgradedIcon']['mask'], ('The upgraded window retained stale icon CSS', upgraded['upgradedIcon'])
        assert abs(upgraded['outer'] - min(prepared['beforeContext']['outer'], upgraded['maxOuter'])) <= 1, 'Automatic guidance update changed outer scroll'
        live.command(work, 'snapshot', filename='upgraded-saved-review.png')
        checked('actual_saved_queue_gains_structured_guidance_and_native_buttons_without_mocked_findings')
        checked('automatic_guidance_update_retains_draft_and_exact_waiting_case_navigation')
        checked('actual_in_place_upgrade_loads_the_new_sync_conflict_icon_mask')
        assert state.review.row(identities[2]) == applying_before
        checked('old_applying_journal_is_byte_equivalent_after_automatic_pending_only_upgrade')
        observed = live.command(work, 'upgrade_click_references')
        assert set(observed['selectedKeys']) == set(parents), observed
        live.command(work, 'snapshot', filename='upgraded-native-navigation.png')
        checked('upgraded_reference_button_selects_exact_native_participant_set')
        events = live.command(work, 'upgrade_watch', start=False)['events']
        after = capture()
        assert before == after and events == [], 'Automatic migration or native navigation changed library/bridge/files'
        assert not (work / 'profile/notable/tablet-token.dpapi').exists(), 'Synthetic profile unexpectedly has tablet credentials'
        methods = [value['method'] for value in upgraded['requests']]
        assert methods.count('recheck_review') == 1, methods
        assert not set(methods) & {'sync', 'approve', 'approve_routine', 'resolve_conflict', 'pair', 'import_library'}, methods
        checked('automatic_upgrade_preserves_native_records_PDFs_bridge_and_recovery_without_cloud_or_filing_actions')
        report = {'version': json.loads((work / 'ready.json').read_text())['version'],
            'legacy_extension': legacy_version, 'new_extension': source_version,
            'new_xpi_sha256': hashlib.sha256(current_xpi.read_bytes()).hexdigest(),
            'new_worker': 'Bundled frozen worker' if args.new_xpi else 'Source worker',
            'scope': 'Disposable profile, persistent legacy SQLite, actual worker RPC, native reference selection',
            'old_rows_had_no_findings_or_guidance_marker': True, 'manual_recheck_clicks': 0,
            'requests': upgraded['requests'], 'native_notifications': events,
            'native_records_files_bridge_unchanged': before == after,
            'applying_journal_unchanged': True, 'form_draft_preserved': upgraded['filenameDraftPreserved'],
            'upgraded_sync_conflict_icon': upgraded['upgradedIcon'],
            'selected_reference_keys': observed['selectedKeys'], 'legacy_review_ids': identities,
            'screenshots': ['legacy-saved-review.png', 'upgraded-saved-review.png', 'upgraded-native-navigation.png']}
        (work / 'saved-review-upgrade-evidence.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        return None

    live.native_checks = upgrade_checks
    sys.argv = [sys.argv[0], '--work-dir', str(root), '--zotero-exe', str(args.zotero_exe),
                '--xpi', str(args.legacy_xpi), '--port', str(args.port), '--skip-ocr']
    live.main()


if __name__ == '__main__':
    main()
