"""Package the graphical extension and optional managed Windows runtime."""
import argparse
import hashlib
from importlib import metadata
import json
from pathlib import Path
import platform
import zipfile
import subprocess
import tempfile
from urllib.parse import quote


def validate_build_environment():
    """Fail before freezing when the private runtime is incomplete."""
    import importlib
    required = ('pyzotero', 'httpx2', 'httpcore2', 'rapidocr', 'onnxruntime', 'numpy', 'pymupdf', 'rmscene', 'pypdf', 'PIL', 'docx', 'fontTools', 'yake')
    for name in required:
        try:
            importlib.import_module(name)
        except ImportError as error:
            raise RuntimeError('Build requires the complete requirements-zotero-plugin.txt environment; missing ' + name) from error
    return required


def validate_runtime(runtime):
    """Exercise the exact frozen imports without opening Zotero or the cloud."""
    executable = Path(runtime)
    if executable.is_dir():
        executable = executable / 'notable-worker.exe'
    request = {'id': 1, 'method': 'runtime_health', 'params': {'optional': True}}
    with tempfile.TemporaryDirectory(prefix='notable-runtime-health-') as state:
        result = subprocess.run([str(executable.resolve(strict=True)), '--state', state],
                                input=json.dumps(request) + '\n', capture_output=True, text=True, timeout=90)
    try:
        packets = [json.loads(line) for line in result.stdout.splitlines()]
        health = next(row['result'] for row in packets if row.get('id') == 1)
        valid = result.returncode == 0 and health['sync_ready'] and all(row['available'] for row in health['optional_ocr'].values())
        valid = valid and set(health['optional_ocr']) == {'rapidocr', 'onnxruntime', 'numpy'}
    except (ValueError, KeyError, StopIteration, TypeError):
        valid = False
    if not valid:
        raise RuntimeError('The frozen helper failed its required/optional runtime import check; package was not created.')
    return health


def build(output, runtime=None):
    source = Path(__file__).resolve().parents[1] / "zotero-support"
    output = Path(output).resolve()
    if output.is_relative_to(source):
        raise ValueError('Choose an XPI path outside the extension source folder.')
    files = {p.relative_to(source).as_posix(): p for p in source.rglob('*') if p.is_file()}
    runtime_paths = {}
    if runtime:
        runtime = Path(runtime).resolve(strict=True)
        single_file = runtime.is_file()
        executable = runtime if single_file else runtime / 'notable-worker.exe'
        if executable.name != 'notable-worker.exe' or not executable.is_file():
            raise ValueError('The managed worker executable is missing')
        if output.is_relative_to(runtime):
            raise ValueError('Choose an XPI path outside the worker runtime.')
        for path in ([runtime] if single_file else runtime.rglob('*')):
            if path.is_file():
                name = path.name if single_file else path.relative_to(runtime).as_posix()
                member = 'runtime/' + name
                runtime_paths[member] = name
                files[member] = path

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, prefix='.' + output.name + '.',
                                     suffix='.partial', delete=False) as temporary:
        partial = Path(temporary.name)
    try:
        with zipfile.ZipFile(partial, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            entries = []
            for name, path in sorted(files.items()):
                info = zipfile.ZipInfo(name, date_time=(2026, 10, 18, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.file_size = path.stat().st_size
                checksum = hashlib.md5() if name in runtime_paths else None
                with path.open('rb') as source_file, archive.open(info, 'w') as member:
                    while block := source_file.read(1024 * 1024):
                        member.write(block)
                        if checksum is not None:
                            checksum.update(block)
                if checksum is not None:
                    entries.append({'path': runtime_paths[name], 'md5': checksum.hexdigest()})
            if runtime:
                manifest = {'id': hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()[:24], 'files': entries}
                info = zipfile.ZipInfo('runtime-manifest.json', date_time=(2026, 10, 18, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, json.dumps(manifest, sort_keys=True))
        partial.replace(output)
    finally:
        partial.unlink(missing_ok=True)
    return output


def build_record(output, runtime, health):
    from PyInstaller.archive.readers import CArchiveReader

    root = Path(__file__).resolve().parents[1]
    runtime = Path(runtime)
    executable = runtime / 'notable-worker.exe' if runtime.is_dir() else runtime
    worker = CArchiveReader(str(executable))
    models = {}
    for name in worker.toc:
        path = name.replace('\\', '/')
        if path.startswith('models/') and path.endswith(('.onnx', '.traineddata')):
            models[path] = hashlib.sha256(worker.extract(name)).hexdigest()
    with Path(output).open('rb') as package:
        digest = hashlib.file_digest(package, 'sha256').hexdigest()
    record = {'xpi': Path(output).name, 'sha256': digest, 'python': platform.python_version(),
              'architecture': platform.machine(), 'models': models, 'worker_health': health,
              'packages': dict(sorted((item.metadata['Name'], item.version) for item in metadata.distributions())),
              'dependency_lock_sha256': hashlib.sha256((root / 'requirements-windows.lock').read_bytes()).hexdigest(),
              'vendored_manifest_sha256': hashlib.sha256((root / '_vendor/manifest.json').read_bytes()).hexdigest()}
    target = Path(output).with_suffix('.build.json')
    target.write_text(json.dumps(record, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    return target


def write_update_manifest(output, build_receipt):
    """Advertise the exact bundled XPI and its Zotero compatibility."""
    with zipfile.ZipFile(output) as archive:
        manifest = json.loads(archive.read('manifest.json'))
    version = manifest['version']
    zotero = manifest['applications']['zotero']
    release_base, separator, _ = zotero['update_url'].rpartition('/latest/download/')
    if not separator:
        raise ValueError('Expected a GitHub latest release asset for the update manifest.')
    receipt = json.loads(build_receipt.read_text(encoding='utf-8'))
    update = {
        'version': version,
        'update_link': f'{release_base}/download/v{version}/{quote(output.name, safe="")}',
        'update_hash': 'sha256:' + receipt['sha256'],
        'applications': {'zotero': {
            'strict_min_version': zotero['strict_min_version'],
            'strict_max_version': zotero['strict_max_version'],
        }},
    }
    target = output.with_name('updates.json')
    target.write_text(json.dumps({'addons': {zotero['id']: {'updates': [update]}}}, indent=2) + '\n',
                      encoding='utf-8')
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--verify-runtime", action='store_true', help='Required release gate: exercise frozen runtime imports before packaging')
    parser.add_argument("--update-manifest", action='store_true', help='Write the GitHub release update manifest beside the bundled XPI')
    args = parser.parse_args()
    if args.verify_runtime and not args.runtime:
        parser.error('--verify-runtime requires --runtime')
    if args.update_manifest and not args.runtime:
        parser.error('--update-manifest requires --runtime')
    health = validate_runtime(args.runtime) if args.runtime else None
    output = build(args.output, args.runtime)
    print(output)
    if args.runtime:
        build_receipt = build_record(output, args.runtime, health)
        print(build_receipt)
        if args.update_manifest:
            print(write_update_manifest(output, build_receipt))
