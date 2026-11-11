"""Inspect populated screens in an already-running disposable plugin fixture.

Start test_zotero_plugin_live.py with --keep-open first. This tool never opens
the production Zotero profile or connects to a reMarkable account.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.test_zotero_plugin_live import command


def audit(root):
    config = json.loads((root / 'test-extension/config.json').read_text())
    if Path(config['data']).resolve() != root / 'zotero-data' or (root / 'done').exists():
        raise ValueError('An active disposable Zotero fixture is required')
    snapshot = command(root, 'rpc', method='status')['result']
    folders = {
        'collections': [{'key': f'z{i}', 'name': f'Research / Spectral CT / Long folder {i}', 'parentCollection': False} for i in range(120)],
        'folders': [{'id': f'c{i}', 'name': f'Research / Spectral CT / Long folder {i}', 'parent': ''} for i in range(120)]
    }
    # The destination picker follows the selected root. Keep an eligible child
    # so its interaction test can select a different value inside that root.
    folders['folders'][2]['parent'] = 'c1'
    snapshot['collections'] = folders['collections']
    snapshot['mappings'] = {'z0': {'cloud': 'c0'}}
    snapshot['config'] = {'zotero_root': 'z0', 'tablet_root': 'c0', 'automatic': False}
    snapshot['links'] = [{'cloud': 'fixture', 'parent': 'PARENT23', 'name': 'A long paper title with research context ' * 3,
                          'collection': 'z0', 'view_key': 'SURCE234', 'paused': False, 'revision': 'fixture', 'history': []}]
    snapshot['local_deletions'] = [{'id': 'fixture', 'payload': {'title': 'Example trashed reference'}}]
    snapshot['deletions'] = [{'id': 'fixture', 'payload': {'title': 'Example tablet paper', 'inventory': []}}]
    snapshot['reviews'] = [{'id': 'fixture', 'kind': 'inbox', 'identity': 'fixture', 'status': 'pending', 'digest': 'fixture',
                            'payload': {'title': 'Long document title for individual review ' * 3, 'document_type': 'journal_article',
                                        'path': 'Example.pdf', 'filename': 'Example.pdf'}}]
    snapshot['bulk'] = {'id': 'fixture', 'root': 'Example literature', 'collection': 'z0', 'lookup': False, 'failures': [],
                        'rows': [{'path': 'An example file needing review.pdf', 'copies': ['Example.pdf'], 'issues': ['Check this edition'], 'status': 'pending'}]}
    editor = {'title': 'Example paper', 'page': 0, 'page_id': 'fixture', 'key': 'SURCE234', 'token': 'fixture',
              'paper_tags': ['Spectral CT', 'A long research label ' * 6], 'page_tags': ['Methods'], 'vocabulary': ['Methods'], 'orphaned_pages': {}}
    checks = []
    for tab in ['setup', 'review', 'links', 'tags', 'import']:
        for theme, width in [('light', 1040), ('dark', 1040), ('dark', 640)]:
            label = f'{tab}-{theme}-{width}'
            result = command(root, 'layout', tab=tab, snapshot=snapshot, folders=folders, tagEditor=editor, theme=theme, width=width, expandDetails=True, editFolder=tab == 'setup')
            layout = result['layout']
            assert layout['scrollWidth'] <= layout['width'] + 1, (label, layout)
            buttons = layout['buttons']
            assert all(b['icon'] for b in buttons if b['text'] in ['Sync now', 'Review', 'Linked papers', 'Tags', 'Import library', 'Setup']), (label, buttons)
            for index, button in enumerate(buttons):
                assert button['width'] > 0 and button['right'] <= layout['width'] + 1, (label, button)
                if button['icon']:
                    assert button['iconWidth'] == 18 and button['iconMask'] != 'none', (label, button)
                for other in buttons[index + 1:]:
                    overlap_x = min(button['right'], other['right']) - max(button['x'], other['x'])
                    overlap_y = min(button['bottom'], other['bottom']) - max(button['y'], other['y'])
                    assert overlap_x <= 0 or overlap_y <= 0, (label, button, other)
            for select in layout['selects']:
                assert select['right'] <= layout['width'] + 1, (label, select)
                if not select['multiple']:
                    assert select['height'] < 60 and all(r['height'] == 0 for r in select['optionRects']), (label, select)
            if tab == 'setup' and 'pairingWidth' in layout:
                assert layout['pairingWidth'] <= 240, (label, layout)
                assert all(display == 'flex' for display in layout['checkboxDisplays']), (label, layout)
            command(root, 'snapshot', filename=label + '.png', scrollY=0)
            checks.append({'name': label, 'status': 'PASS', 'buttons': len(buttons), 'selects': len(layout['selects'])})
    preview = {'token': 'visual-fixture', 'proposed': {'zotero_path': 'Tablet papers', 'tablet_path': 'Literature'},
               'linked_papers': 12, 'pending_reviews': 3, 'restored_papers': 0,
               'zotero_folders': ['New research topic'], 'tablet_folders': ['Methods'],
               'excluded_zotero_folders': ['Genesis', 'Isaiah'], 'excluded_tablet_folders': ['Previous topic']}
    for mode in ['folderPreview', 'disconnectPreview']:
        for theme, width in [('light', 1040), ('dark', 1040), ('dark', 640)]:
            label = f'{mode}-{theme}-{width}'
            result = command(root, 'layout', tab='setup', snapshot=snapshot, folders=folders, theme=theme, width=width,
                             expandDetails=True, editFolder=mode == 'folderPreview', **{mode: preview})
            layout = result['layout']
            assert layout['scrollWidth'] <= layout['width'] + 1, (label, layout)
            for index, button in enumerate(layout['buttons']):
                assert button['right'] <= layout['width'] + 1, (label, button)
                for other in layout['buttons'][index+1:]:
                    assert min(button['right'], other['right']) <= max(button['x'], other['x']) or min(button['bottom'], other['bottom']) <= max(button['y'], other['y']), (label, button, other)
            command(root, 'snapshot', filename=label + '.png', scrollY=10000)
            checks.append({'name': label, 'status': 'PASS'})
    command(root, 'layout', tab='setup', snapshot=snapshot, folders=folders, theme='dark', width=1040, editFolder=True)
    for index in range(3):
        result = command(root, 'picker', index=index, choose=2)
        assert result['popups'] and any(p['state'] == 'open' and p['height'] > 0 for p in result['popups']), result
        assert all(p['appearance'] != 'none' for p in result['popups'] if p['state'] == 'open'), result
        assert result['valueAfter'] != result['valueBefore'], result
        checks.append({'name': f'native-folder-picker-{index}-opens-selects-closes', 'status': 'PASS', 'popups': result['popups']})
    command(root, 'layout', tab='setup', snapshot=snapshot, folders=folders, theme='dark', width=1040, editFolder=True)
    command(root, 'snapshot', filename='setup-after-pickers.png', scrollY=0)
    command(root, 'snapshot', filename='setup-dark-bottom.png', scrollY=10000)
    for tab in ['review', 'import']:
        for theme, width in [('light', 1040), ('dark', 1040), ('dark', 640)]:
            label = f'working-{tab}-{theme}-{width}'
            result = command(root, 'layout', tab=tab, snapshot=snapshot, folders=folders, theme=theme, width=width,
                             operation={'title': 'Scanning your Windows library', 'method': 'scan_library',
                                        'detail': 'Scanning file 12 of 537: Example research paper.pdf',
                                        'started': int(__import__('time').time() * 1000) - 65000,
                                        'updated': int(__import__('time').time() * 1000)})
            layout = result['layout']
            assert layout['scrollWidth'] <= layout['width'] + 1, (label, layout)
            for index, button in enumerate(layout['buttons']):
                for other in layout['buttons'][index+1:]:
                    assert min(button['right'], other['right']) <= max(button['x'], other['x']) or min(button['bottom'], other['bottom']) <= max(button['y'], other['y']), (label, button, other)
            command(root, 'snapshot', filename=label + '.png', scrollY=0)
            checks.append({'name': label, 'status': 'PASS'})
    command(root, 'layout', tab='review', snapshot=snapshot, folders=folders, theme='dark', width=1040)
    (root / 'ui-results.json').write_text(json.dumps({'checks': checks}, indent=2))
    print(json.dumps({'passed': len(checks), 'results': str(root / 'ui-results.json')}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir', type=Path, required=True)
    audit(parser.parse_args().work_dir.resolve())
