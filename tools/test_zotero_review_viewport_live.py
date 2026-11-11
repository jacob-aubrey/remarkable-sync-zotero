"""Native regression for review panes filling the desktop viewport."""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import test_zotero_plugin_live as live

BRANCH = r"""
          else if (command.action === 'review_viewport') {
            clearTimeout(ui.timer); clearTimeout(ui.inboxTimer);
            const ensure = (v, reason) => {if (!v) throw Error(reason);};
            const settle = () => Zotero.Promise.delay(200);
            ui.latest = command.snapshot; ui.tab = 'inbox'; ui.selected = '0'; ui.busy = false;
            ui.view.resizeTo(command.width, command.height); ui.render(); await settle();
            const win = ui.view, doc = win.document;
            const geometry = () => {
              const list = doc.querySelector('.review-layout > aside'), detail = doc.querySelector('.review-form');
              return {height: win.innerHeight, width: win.innerWidth,
                list: {top: list.getBoundingClientRect().top, bottom: list.getBoundingClientRect().bottom, height: list.clientHeight},
                detail: {top: detail.getBoundingClientRect().top, bottom: detail.getBoundingClientRect().bottom, height: detail.clientHeight}};
            };
            if (win.innerWidth > 700) {
              const check = () => {
                const g = geometry();
                ensure(Math.abs(g.list.bottom - (win.innerHeight - 26)) < 2, 'List leaves unused viewport: ' + JSON.stringify(g));
                ensure(Math.abs(g.detail.bottom - g.list.bottom) < 2, 'Detail and list bottoms differ');
                ensure(doc.documentElement.scrollWidth <= win.innerWidth + 1, 'Horizontal overflow');
                return g;
              };
              response.short = check();
              const list = doc.querySelector('aside'); list.scrollTop = 120;
              ui.latest.reviews[0].payload.url = 'https://example.org/' + 'long-filename-'.repeat(500);
              ui.render(); await settle();
              response.long = check();
              let detail = doc.querySelector('.review-form');
              ensure(detail.scrollHeight > detail.clientHeight + 100, 'Long details must scroll inside their pane');
              detail.scrollTop = 140; const top = detail.scrollTop;
              ui.render(); await settle();
              ensure(Math.abs(doc.querySelector('.review-form').scrollTop - top) < 2, 'Refresh lost detail reading position');
              const body = doc.querySelector('body'); const outer = body.scrollTop;
              const button = doc.querySelector('aside button');
              button.dispatchEvent(new win.KeyboardEvent('keydown', {key: 'End', bubbles: true})); await settle();
              ensure(ui.selected === command.snapshot.reviews.at(-1).id, 'Keyboard selection failed');
              const selected = doc.querySelector('aside [aria-pressed="true"]'), box = selected.getBoundingClientRect();
              const listBox = doc.querySelector('aside').getBoundingClientRect();
              ensure(box.top >= listBox.top - 1 && box.bottom <= listBox.bottom + 1, 'Last selected row is clipped');
              ensure(Math.abs(body.scrollTop - outer) < 2, 'Selecting a paper moved the whole page');
              ensure(doc.querySelector('.review-form').scrollTop === 0, 'New paper inherited prior reading position');
              response.final = check();
            } else {
              ensure(doc.documentElement.scrollWidth <= win.innerWidth + 1, 'Narrow horizontal overflow');
              ensure(doc.querySelector('aside').clientHeight <= win.innerHeight * .36 + 2, 'Narrow list is unbounded');
              response.narrow = geometry();
            }
            ui.hideButtonHelp();
          }
"""

def checks(client, root, checked):
    client.authorize()
    snapshot = live.command(root, 'rpc', method='status')['result']
    snapshot['config'] = {**snapshot['config'], 'tablet_root': 'fixture-tablet', 'zotero_root': 'ROOT2345', 'automatic': True, 'sync_paused': True}
    snapshot['reviews'] = [{'id': str(i), 'kind': 'reference', 'identity': 'PARENT23', 'digest': 'fixture', 'status': 'pending',
        'payload': {'title': f'Example paper {i:03}', 'url': 'https://example.org/paper'}} for i in range(240)]
    evidence = []
    for width, height in [(1600, 1000), (1040, 900), (1040, 760), (640, 900)]:
        result = live.command(root, 'review_viewport', snapshot=snapshot, width=width, height=height)
        evidence.append(result)
        live.command(root, 'snapshot', filename=f'review-{width}-{height}.png')
        checked(f'review_viewport_and_independent_scrolling_{width}_{height}')
    (root / 'review-viewport-results.json').write_text(json.dumps(evidence, indent=2), encoding='utf-8')
    return None

if __name__ == '__main__':
    marker = "          else if (command.action === 'reindex')"
    live.BOOTSTRAP = live.BOOTSTRAP.replace(marker, BRANCH + marker, 1)
    live.native_checks = checks
    live.main()
