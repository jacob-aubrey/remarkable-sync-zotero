"""Verified settings and decision snapshots, with credentials excluded."""
from contextlib import closing
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import tempfile
import zipfile

from .remarkable_cloud import atomic_write
from .zotero_pilot import PilotError


STATE_FILE = re.compile(
    r'(?:connections/[0-9a-f]{32}/)?(?:bridge|review)/[a-zA-Z0-9_-]+\.sqlite3'
    r'|config\.json|unknown-name-review\.json|folder-recents\.json|tag-repairs/[0-9a-f]{32}\.json'
)
PRIVATE = re.compile(r'password|secret|(?:api|device|access|refresh)[_-]?key|credential|grant|token', re.I)


def _paused(value):
    def scrub(item):
        if isinstance(item, dict):
            return {key: scrub(data) for key, data in item.items() if not PRIVATE.search(key)}
        if isinstance(item, list):
            return [scrub(data) for data in item]
        return item

    return {**scrub(value), 'automatic': False, 'sync_paused': True}


def _database(path):
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    db.execute('PRAGMA query_only=ON')
    return closing(db)


def backup_state(worker, target):
    target = Path(target).resolve()
    if target.exists() or target.is_relative_to(worker.directory):
        raise PilotError('Choose a new backup file outside the active plugin folder.')
    with tempfile.TemporaryDirectory(dir=target.parent, prefix='.zotero-backup-') as folder:
        scratch = Path(folder)
        archive_path = scratch / 'backup.zip'
        files = {'config.json': json.dumps(_paused(worker.config), ensure_ascii=False).encode('utf-8')}
        for pattern in ('bridge/*.sqlite3', 'review/*.sqlite3', 'connections/*/bridge/*.sqlite3',
                        'connections/*/review/*.sqlite3', 'tag-repairs/*.json', 'unknown-name-review.json',
                        'folder-recents.json'):
            for path in worker.directory.glob(pattern):
                name = path.relative_to(worker.directory).as_posix()
                if path.is_symlink() or not path.is_file() or not STATE_FILE.fullmatch(name):
                    continue
                if path.suffix == '.sqlite3':
                    copy = scratch / 'database.sqlite3'
                    with _database(path) as source, closing(sqlite3.connect(copy)) as snapshot:
                        source.backup(snapshot)
                    files[name] = copy.read_bytes()
                    copy.unlink()
                else:
                    files[name] = path.read_bytes()
        manifest = {'format': 1, 'files': {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
        with zipfile.ZipFile(archive_path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in sorted(files.items()):
                archive.writestr(name, data)
            archive.writestr('manifest.json', json.dumps(manifest, sort_keys=True))
        with zipfile.ZipFile(archive_path) as archive:
            _verify(archive)
        atomic_write(target, archive_path.read_bytes(), require_absent=True)
    return {'path': str(target), 'files': len(files)}


def _verify(archive):
    names = archive.namelist()
    manifest = json.loads(archive.read('manifest.json'))
    files = manifest.get('files', {})
    if (manifest.get('format') != 1 or not isinstance(files, dict) or 'config.json' not in files
            or len(names) != len(set(names)) or set(names) != {'manifest.json', *files}
            or any(not STATE_FILE.fullmatch(name) for name in files)):
        raise PilotError('The backup contains an unexpected file or format.')
    for name, expected in files.items():
        with archive.open(name) as source:
            if hashlib.file_digest(source, 'sha256').hexdigest() != expected:
                raise PilotError('A file in the backup failed verification.')
    return files


def restore_state(worker, source, destination):
    destination = Path(destination).resolve()
    if destination.exists() or destination.is_relative_to(worker.directory):
        raise PilotError('Choose a new folder outside the active plugin folder.')
    with zipfile.ZipFile(source) as archive, tempfile.TemporaryDirectory(
            dir=destination.parent, prefix='.zotero-restore-') as folder:
        files = _verify(archive)
        staging = Path(folder) / 'restored'
        staging.mkdir()
        for name in files:
            target = staging.joinpath(*PurePosixPath(name).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(name) as source_file, target.open('xb') as output:
                shutil.copyfileobj(source_file, output)
            if target.suffix == '.sqlite3':
                with _database(target) as db:
                    if db.execute('PRAGMA quick_check').fetchone() != ('ok',):
                        raise PilotError('A saved database failed its integrity check.')
        config = staging / 'config.json'
        config.write_text(json.dumps(_paused(json.loads(config.read_text(encoding='utf-8')))), encoding='utf-8')
        staging.rename(destination)
    return {'path': str(destination), 'files': len(files), 'paused': True}
