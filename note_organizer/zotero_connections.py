"""Keep folder connections separate without moving library or recovery files.

The config file is the atomic commit point. Legacy state stays in its original
directory so absolute baseline/source paths and interrupted jobs remain valid.
"""
import json
import re
import sqlite3
import time
import uuid

from .zotero_pilot import PilotError


def state_directory(directory, config):
    identity = config.get('connection_id', 'legacy')
    if identity == 'legacy':
        return directory
    if not isinstance(identity, str) or not re.fullmatch(r'[a-f0-9]{32}', identity):
        raise PilotError('The saved folder connection is invalid. Your files were retained.')
    return directory / 'connections' / identity


def values(directory, config):
    path = state_directory(directory, config) / 'bridge' / 'pilot.sqlite3'
    if not path.exists():
        return {'folders': {}, 'documents': {}}
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        return {'folders': {}, 'documents': {}, **{
            key: json.loads(value) for key, value in db.execute('SELECT key,value FROM bridge_values')}}
    except (sqlite3.Error, ValueError) as error:
        raise PilotError('A previous folder connection needs recovery. Its files were retained.') from error
    finally:
        db.close()


def same_roots(left, right):
    return all(left.get(key) == right.get(key) for key in ('server_id', 'zotero_root', 'tablet_root'))


def exclusions(directory, config):
    result = {'zotero': set(), 'tablet': set(), 'documents': set(), 'sources': set()}
    for entry in config.get('connection_history', []):
        if entry['connection_id'] == config.get('connection_id', 'legacy'):
            continue
        if not (state_directory(directory, entry) / 'bridge' / 'pilot.sqlite3').exists():
            raise PilotError('A previous connection record is missing. Restore its recovery files before syncing.')
        saved = values(directory, entry)
        same_server = entry.get('server_id') == config.get('server_id')
        if same_server:
            result['zotero'].update(saved['folders'])
        result['tablet'].update(row['cloud'] for row in saved['folders'].values())
        result['documents'].update(saved['documents'])
        if same_server:
            for row in saved['documents'].values():
                result['sources'].update(row.get(key) for key in ('source_key', 'view_key') if row.get(key))
    result['zotero'].discard(config.get('zotero_root'))
    result['tablet'].discard(config.get('tablet_root'))
    return result


def switch(directory, current, proposed):
    history = list(current.get('connection_history', []))
    identity = current.get('connection_id', 'legacy')
    if current.get('tablet_root'):
        entry = {key: current[key] for key in ('server_id', 'zotero_root', 'tablet_root',
                 'default_folder', 'zotero_path', 'tablet_path') if key in current}
        entry.update(connection_id=identity, archived_at=time.time())
        history = [row for row in history if row['connection_id'] != identity] + [entry]
    previous = next((row for row in reversed(history) if proposed.get('tablet_root') and same_roots(row, proposed)), None)
    identity = previous['connection_id'] if previous else uuid.uuid4().hex
    history = [row for row in history if row['connection_id'] != identity]
    return {**proposed, 'connection_id': identity, 'connection_history': history,
            'automatic': False, 'sync_paused': True}


def filtered_scope(scope, excluded, key, parent, root):
    """Exclude an old mapped subtree, including children added since disconnect."""
    result = {}
    for identity, (row, depth) in sorted(scope.items(), key=lambda pair: pair[1][1]):
        if identity == root or (identity not in excluded and parent(row) in result):
            result[identity] = (row, depth)
    return result
