"""Verify the same packaged XPI in separate oldest/current Zotero profiles."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from zipfile import ZipFile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--xpi', type=Path, required=True)
    parser.add_argument('--oldest', type=Path, required=True)
    parser.add_argument('--current', type=Path, required=True)
    parser.add_argument('--work-dir', type=Path, required=True)
    parser.add_argument('--base-port', type=int, default=23203)
    args = parser.parse_args()
    xpi = args.xpi.resolve(strict=True)
    with ZipFile(xpi) as archive:
        minimum = json.loads(archive.read('manifest.json'))['applications']['zotero']['strict_min_version']
    root = args.work_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    script = Path(__file__).with_name('test_zotero_plugin_live.py').resolve()

    def test(role, executable, port):
        directory = root / role
        result = subprocess.run([sys.executable, str(script), '--zotero-exe', str(executable.resolve(strict=True)),
            '--work-dir', str(directory), '--xpi', str(xpi), '--port', str(port), '--maintenance-checks'],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=360)
        (root / (role + '.log')).write_text(result.stdout + result.stderr, encoding='utf-8')
        ready = directory / 'ready.json'
        results = directory / 'results.json'
        return {'role': role, 'version': json.loads(ready.read_text())['version'] if ready.is_file() else None,
                'passed': result.returncode == 0, 'results': json.loads(results.read_text()) if results.is_file() else {}}

    with ThreadPoolExecutor(max_workers=2) as pool:
        oldest = pool.submit(test, 'oldest', args.oldest, args.base_port)
        current = pool.submit(test, 'current', args.current, args.base_port + 1)
        checks = [oldest.result(), current.result()]
    with xpi.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    receipt = {'sha256': digest, 'checks': checks}
    (root / 'verification.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(receipt, indent=2))
    versions = [row['version'] for row in checks]
    return int(not (all(row['passed'] for row in checks) and versions[0] == minimum
                    and versions[1] and versions[0] != versions[1]))


if __name__ == '__main__':
    sys.exit(main())
