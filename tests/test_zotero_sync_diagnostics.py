"""Diagnostic paths use synthetic requests/state; never a real library."""
import importlib
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import httpx2
from note_organizer.zotero_worker import OperationTrace, Worker
from note_organizer.zotero_pilot import LocalZotero
from tools.build_zotero_support import build, validate_build_environment, validate_runtime


class DiagnosticsTests(unittest.TestCase):
    def test_rotation_keeps_complete_records_and_redacts_sensitive_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'trace.jsonl'
            trace = OperationTrace(path, max_bytes=500)
            trace.start('sync', 'safe-run-1')
            for index in range(12):
                self.assertTrue(trace.event('step', ordinal=index, token='private-secret', payload={'pdf': 'private text'},
                                            message='https://secret.invalid/?signed=secret token=private'))
            paths = list(Path(directory).glob('*'))
            self.assertLessEqual(len(paths), 3)
            for item in paths:
                self.assertLessEqual(item.stat().st_size, 500)
                text = item.read_text()
                self.assertNotIn('private-secret', text)
                self.assertNotIn('private text', text)
                self.assertNotIn('secret.invalid', text)
                for line in text.splitlines():
                    self.assertEqual(json.loads(line)['operation'], 'safe-run-1')

    def test_diagnostic_disk_error_cannot_change_operation_outcome(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = OperationTrace(Path(directory) / 'trace.jsonl')
            with patch.object(Path, 'open', side_effect=PermissionError('logging denied')):
                trace.start('sync')
                self.assertFalse(trace.event('stage', name='test'))
                trace.finish()
            self.assertIsNone(trace.operation)

    def test_required_request_timeout_has_started_failed_pair_without_payload(self):
        rows = []
        client = LocalZotero.__new__(LocalZotero)
        client.trace = lambda event, **fields: rows.append({'event': event, **fields})
        def fail(request):
            raise httpx2.ReadTimeout('private response text', request=request)
        with httpx2.Client(transport=httpx2.MockTransport(fail)) as connection:
            client._trace_client(connection)
            with self.assertRaises(httpx2.ReadTimeout):
                connection.post('http://127.0.0.1:23199/api/notable/action?token=private', json={'private': 'PDF text'})
        self.assertEqual([row['event'] for row in rows], ['zotero_request_started', 'zotero_request_failed'])
        self.assertEqual(rows[0]['request_id'], rows[1]['request_id'])
        self.assertNotIn('private', json.dumps(rows))
        self.assertNotIn('PDF text', json.dumps(rows))

    def test_success_request_and_broken_diagnostic_sink_preserve_response(self):
        rows = []
        client = LocalZotero.__new__(LocalZotero)
        client.trace = lambda event, **fields: rows.append(event)
        with httpx2.Client(transport=httpx2.MockTransport(lambda _: httpx2.Response(200, json={'ok': True}))) as connection:
            client._trace_client(connection)
            self.assertTrue(connection.get('http://127.0.0.1:23199/api/').json()['ok'])
            client.trace = lambda *a, **k: (_ for _ in ()).throw(OSError('disk failed'))
            self.assertEqual(connection.get('http://127.0.0.1:23199/api/').status_code, 200)
        self.assertEqual(rows, ['zotero_request_started', 'zotero_request_finished'])

    def test_normal_sync_does_not_load_optional_handwriting_environment(self):
        from tests.test_zotero_worker import WorkerTests
        fixture = WorkerTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.worker.zotero_factory = None
        fixture.worker.config['ocr'] = True
        with patch('note_organizer.zotero_worker.LocalZotero', return_value=fixture.zot), \
                patch.dict(sys.modules, {'rapidocr': None, 'onnxruntime': None}), \
                patch('note_organizer.handwriting_ocr.HandwritingOCR', side_effect=AssertionError('OCR must remain explicit')):
            fixture.worker.sync()

    def test_explicit_ocr_still_requests_ocr_and_reports_unavailable_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = Worker(directory)
            with patch('note_organizer.printed_ocr.PrintedOCR'), \
                    patch('note_organizer.handwriting_ocr.HandwritingOCR', side_effect=ValueError('Optional OCR unavailable')) as ocr:
                with self.assertRaisesRegex(ValueError, 'Optional OCR unavailable'):
                    worker.zotero(ocr=True)
            ocr.assert_called_once()

    def test_runtime_health_separates_required_and_optional_dependencies(self):
        original = importlib.import_module
        def without_ocr(name, *args, **kwargs):
            if name in {'rapidocr', 'onnxruntime', 'numpy'}:
                raise ModuleNotFoundError('not installed', name=name)
            return original(name, *args, **kwargs)
        with tempfile.TemporaryDirectory() as directory:
            worker = Worker(directory)
            with patch('importlib.import_module', side_effect=without_ocr):
                health = worker.runtime_health(optional=True)
            self.assertTrue(health['sync_ready'], health)
            self.assertTrue(all(not value['available'] for value in health['optional_ocr'].values()))

    def test_build_preflight_rejects_missing_required_module(self):
        with patch('importlib.import_module', side_effect=ModuleNotFoundError('missing', name='pyzotero')):
            with self.assertRaisesRegex(RuntimeError, 'missing pyzotero'):
                validate_build_environment()

    def test_page_and_database_boundaries_do_not_capture_document_text(self):
        import pymupdf
        from note_organizer.zotero_pilot import Ledger, searchable_content
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'synthetic.pdf'
            with pymupdf.open() as document:
                document.new_page().insert_text((40, 50), 'PRIVATE_DOCUMENT_CONTENT')
                document.save(path)
            rows = []
            emit = lambda event, **fields: rows.append({'event': event, **fields})
            text, count, _ = searchable_content(path, diagnostic=emit)
            self.assertIn('PRIVATE_DOCUMENT_CONTENT', text)
            self.assertEqual(count, 1)
            self.assertEqual([row['event'] for row in rows], ['pdf_page_started', 'pdf_page_finished'])
            ledger = Ledger(Path(directory) / 'publisher')
            ledger.trace = emit
            ledger.save('synthetic', published=1)
            self.assertIn('database_finished', [row['event'] for row in rows])
            with self.assertRaisesRegex(ValueError, 'synthetic failure'):
                with ledger.connect():
                    raise ValueError('synthetic failure')
            self.assertIn('database_failed', [row['event'] for row in rows])
            self.assertNotIn('PRIVATE_DOCUMENT_CONTENT', json.dumps(rows))

    def test_frozen_release_gate_rejects_a_status_only_or_incomplete_helper(self):
        reply = subprocess.CompletedProcess([], 0, json.dumps({'id': 1, 'result': {'sync_ready': False}}), '')
        with patch('tools.build_zotero_support.subprocess.run', return_value=reply):
            with self.assertRaisesRegex(RuntimeError, 'package was not created'):
                validate_runtime(sys.executable)

    def test_failed_xpi_rebuild_preserves_the_previous_package(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worker = root / 'notable-worker.exe'
            worker.write_bytes(b'synthetic worker')
            output = build(root / 'plugin.xpi', worker)
            original = output.read_bytes()
            with zipfile.ZipFile(output) as archive:
                manifest = json.loads(archive.read('runtime-manifest.json'))
                self.assertEqual(manifest['files'], [{'path': worker.name,
                    'md5': hashlib.md5(archive.read('runtime/' + worker.name)).hexdigest()}])
            original_open = Path.open

            def fail_worker_read(path, *args, **kwargs):
                if path == worker:
                    raise OSError('synthetic read failure')
                return original_open(path, *args, **kwargs)

            with patch.object(Path, 'open', fail_worker_read), self.assertRaisesRegex(OSError, 'synthetic read failure'):
                build(output, worker)
            self.assertEqual(output.read_bytes(), original)
            self.assertEqual(list(root.glob('*.partial')), [])

    def test_exception_trace_and_eof_are_preserved_without_private_error_text(self):
        program = '''
from pathlib import Path
import sys,threading
from note_organizer.zotero_worker import main
class TestWorker:
 def __init__(self,directory,port,resources=None): self.directory=Path(directory);self.stop=threading.Event()
 def dispatch(self,method,params): raise ValueError('private PDF text token=credential')
main(['--state',sys.argv[1]],worker_factory=TestWorker)
'''
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, '-c', program, directory], input=json.dumps(
                {'id': 5, 'method': 'sync', 'trace_id': 'synthetic-run-5'}) + '\n', text=True, capture_output=True, timeout=30)
            rows = [json.loads(line) for line in (Path(directory) / 'sync-trace.jsonl').read_text().splitlines()]
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(all(json.loads(line) for line in result.stdout.splitlines()))
        self.assertNotIn('private PDF text', json.dumps(rows))
        failure = next(row for row in rows if row['event'] == 'error')
        self.assertEqual(failure['operation'], 'synthetic-run-5')
        self.assertTrue(failure['frames'])
        self.assertIn('stdin_closed', [row['event'] for row in rows])
        self.assertEqual(rows[-1]['event'], 'helper_exiting')

    def test_pause_request_is_durable_and_logs_control_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / 'tools/notable_worker.py'), '--state', directory],
                                    input=json.dumps({'id': 1, 'method': 'pause_sync'}) + '\n',
                                    text=True, capture_output=True, timeout=30)
            rows = [json.loads(line) for line in (Path(directory) / 'sync-trace.jsonl').read_text().splitlines()]
            config = json.loads((Path(directory) / 'config.json').read_text())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(config['sync_paused'])
        self.assertTrue(any(row['event'] == 'control_requested' and row['method'] == 'pause_sync' for row in rows))

    def test_cloud_retries_are_timed_without_logging_url_or_request_bytes(self):
        import io
        import urllib.error
        from contextlib import closing
        from note_organizer.remarkable_cloud import CloudClient
        rows = []
        client = CloudClient(trace=lambda event, **fields: rows.append({'event': event, **fields}))
        client.opener.open = unittest.mock.Mock(side_effect=[urllib.error.URLError('private'), closing(io.BytesIO(b'ok'))])
        client.stop.wait = unittest.mock.Mock(return_value=False)
        self.assertEqual(client._http('https://private.invalid/?token=private', body=b'PDF private content'), b'ok')
        self.assertTrue(any(row['event'] == 'cloud_retry' and row['delay_s'] == 2 for row in rows))
        self.assertTrue(any(row['event'] == 'cloud_request_finished' for row in rows))
        self.assertNotIn('private', json.dumps(rows))


if __name__ == '__main__':
    unittest.main()
