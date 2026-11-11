"""Refresh the plugin's pinned Notable core snapshot; no network or sibling runtime required.

Usage: python tools/vendor_notable_core.py --source ../Notable
Only statically referenced Python modules and test fixtures are copied. The
manifest records source revision and SHA-256 for reviewable updates.
"""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import subprocess


# These files contain intentional plugin-specific behavior or exist only here.
# Port upstream changes to them explicitly before updating the snapshot.
DOWNSTREAM = {
    'note_organizer/remarkable_cloud.py',
    'note_organizer/remarkable_marks.py',
    'note_organizer/remarkable_pdf.py',
    'note_organizer/sorting/core_logic.py',
}


def dependencies(path, module, package):
    result = set()
    for node in ast.walk(ast.parse(path.read_text(encoding='utf-8-sig'))):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level and package == 'note_organizer':
            prefix = module.split('.')[:-node.level]
            if path.name == '__init__.py':
                prefix = module.split('.')[:len(module.split('.')) - node.level + 1]
            result.add('.'.join(prefix + [node.module]) if node.module else '.'.join(prefix))
        elif node.module == package:
            result.update(alias.name for alias in node.names)
        elif node.module and node.module.startswith(package + '.'):
            result.add(node.module[len(package) + 1:])
    return result - {''}


def refresh(source):
    root = Path(__file__).resolve().parents[1]
    output = root / '_vendor'
    files = {}
    core_seeds = set()
    for package in ('tests', 'note_organizer'):
        local, upstream, pinned = root / package, source / package, output / package
        pending = [(path.stem, path) for path in local.glob('*.py') if path.stem != '__init__']
        seen = set()
        if package == 'note_organizer':
            pending.append(('_version', upstream / '_version.py'))
            pending.extend((name, Path()) for name in core_seeds)
        while pending:
            name, path = pending.pop()
            if name in seen:
                continue
            seen.add(name)
            relative = Path(name.replace('.', '/'))
            own = local / relative.with_suffix('.py')
            candidate = upstream / relative.with_suffix('.py')
            downstream = pinned / relative.with_suffix('.py')
            if own.is_file():
                path = own
            elif downstream.relative_to(output).as_posix() in DOWNSTREAM:
                path = downstream
            elif candidate.is_file():
                path = candidate
            elif (upstream / relative / '__init__.py').is_file():
                path = upstream / relative / '__init__.py'
            if not path.is_file():
                raise RuntimeError(f'Missing dependency {package}.{name}')
            if path.is_relative_to(upstream) or path.is_relative_to(pinned):
                base = upstream if path.is_relative_to(upstream) else pinned
                paths = [path]
                cursor = path.parent
                while cursor != base:
                    init = cursor / '__init__.py'
                    if init.is_file(): paths.append(init)
                    cursor = cursor.parent
                for item in paths:
                    relative = item.relative_to(base)
                    upstream_init = upstream / relative
                    # Only the selected module is a downstream override.
                    # Shared package initializers still follow upstream.
                    data_source = upstream_init if item != path and upstream_init.is_file() else item
                    files[pinned / relative] = data_source.read_bytes()
            pending.extend((dependency, Path()) for dependency in dependencies(path, name, package))
            if package == 'tests':
                for node in ast.walk(ast.parse(path.read_text(encoding='utf-8-sig'))):
                    if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith('note_organizer.'):
                        core_seeds.add(node.module[len('note_organizer.'):])
    revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    # Discover the entire graph before modifying the current snapshot.
    entries = {}
    for target, data in files.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        entries[target.relative_to(root).as_posix()] = hashlib.sha256(data).hexdigest()
    manifest = {'source': 'https://github.com/jacob-aubrey/pdf-note-organizer', 'revision': revision,
                'note': 'Content hashes identify the exact snapshot, including retained plugin-specific adaptations.',
                'files': dict(sorted(entries.items()))}
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    print(f'Pinned {len(entries)} core and fixture files.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    refresh(parser.parse_args().source.resolve(strict=True))
