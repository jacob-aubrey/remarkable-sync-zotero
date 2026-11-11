"""Verify the exact pinned snapshot without requiring the upstream checkout."""
import hashlib
import json
from pathlib import Path


def verify(root):
    manifest = json.loads((root / '_vendor/manifest.json').read_text(encoding='utf-8'))
    for name, expected in manifest['files'].items():
        path = (root / name).resolve(strict=True)
        if not path.is_relative_to((root / '_vendor').resolve()):
            raise ValueError('Snapshot entry is outside _vendor')
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('Pinned core differs: ' + name)
    actual = {p.relative_to(root).as_posix() for p in (root / '_vendor').rglob('*.py')}
    if actual != set(manifest['files']):
        raise ValueError('Snapshot contains unrecorded or missing Python modules')
    print(f"Verified {len(actual)} pinned core and fixture files.")


if __name__ == '__main__':
    verify(Path(__file__).resolve().parents[1])
