import unittest
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import Mock, MagicMock, patch

import httpx2

from note_organizer.zotero_pilot import LocalZotero, PilotError, ReviewChanged, TransportInterrupted
from note_organizer.zotero_worker import write_packet, progress_links


class TransportTests(unittest.TestCase):
    def test_live_link_snapshot_includes_receipt_state_without_paths_or_credentials(self):
        worker = SimpleNamespace(state=SimpleNamespace(read=lambda *args: {'cloud-paper': {
            'name': 'Paper', 'parent': 'PARENT23', 'collection': 'ROOT2345', 'view_key': 'VIEW2345',
            'revision': 'received-revision', 'baseline': 'private digest', 'rendered': {'input': ['private source']}}}))
        self.assertEqual(progress_links(worker), [{'cloud': 'cloud-paper', 'unavailable': False, 'name': 'Paper', 'parent': 'PARENT23',
            'collection': 'ROOT2345', 'view_key': 'VIEW2345', 'revision': 'received-revision'}])
        self.assertIsNone(progress_links(SimpleNamespace()))

    def test_live_availability_snapshot_explicitly_clears_a_previous_missing_state(self):
        row = {'name': 'Paper', 'unavailable': True}
        worker = SimpleNamespace(state=SimpleNamespace(read=lambda *args: {'paper': row}))
        self.assertTrue(progress_links(worker)[0]['unavailable'])
        row.pop('unavailable')
        self.assertFalse(progress_links(worker)[0]['unavailable'])

    def test_unicode_review_queue_survives_a_windows_cp1252_pipe(self):
        raw = io.BytesIO()
        pipe = io.TextIOWrapper(raw, encoding='cp1252')
        title = 'Nanoparticle\u2010enhanced CT / \u03b1\u2011particles / \u4e2d\u6587 / \U0001f4d6'
        packet = {'id': 1, 'result': {'reviews': [{'title': title}] * 140}}
        write_packet(pipe, packet)
        wire = raw.getvalue()
        self.assertTrue(wire.isascii())
        self.assertEqual(json.loads(wire), packet)
        write_packet(pipe, {'event': 'progress', 'message': title})
        self.assertEqual(json.loads(raw.getvalue().splitlines()[1])['message'], title)

    def test_worker_reads_utf8_requests_and_returns_unicode_under_a_legacy_locale(self):
        program = '''
from pathlib import Path
import sys, threading
from note_organizer.zotero_worker import main
class EchoWorker:
    def __init__(self, directory, port, resources=None):
        self.directory = Path(directory)
        self.stop = threading.Event()
    def dispatch(self, method, params):
        self.progress(params['title'])
        return params
main(['--state', sys.argv[1]], worker_factory=EchoWorker)
'''
        title = 'A\u2010B \u03b1 \u4e2d\u6587 \U0001f4d6'
        for method, quiet in [('echo', False), ('suggest_paper_tags', True)]:
            request = {'id': 1, 'method': method, 'params': {'title': title, 'tag': '\u03bcCT', 'quiet': quiet}}
            with tempfile.TemporaryDirectory() as directory:
                result = subprocess.run([sys.executable, '-c', program, directory],
                                        cwd=Path(__file__).resolve().parents[1],
                                        env={**os.environ, 'PYTHONIOENCODING': 'cp1252', 'PYTHONUTF8': '0'},
                                        input=(json.dumps(request, ensure_ascii=False) + '\n').encode('utf-8'),
                                        capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr.decode('ascii', errors='replace'))
            packets = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual(packets[0], {'event': 'progress', 'message': title, **({'quiet': True} if quiet else {})})
            self.assertEqual(packets[1], {'id': 1, 'result': request['params']})

    def test_quiet_tag_errors_stay_local_and_malformed_params_do_not_stop_transport(self):
        program = """
from pathlib import Path
import sys, threading
from note_organizer.zotero_worker import main
from note_organizer.zotero_pilot import PilotError
class LocalWorker:
    def __init__(self, directory, port, resources=None):
        self.directory = Path(directory)
        self.stop = threading.Event()
        self.last = {'state': 'ready', 'message': 'Existing status'}
        self.writes = 0
    def dispatch(self, method, params):
        if not isinstance(params, dict):
            raise ValueError('Invalid parameters')
        if method == 'suggest_paper_tags':
            raise PilotError('Tag extraction unavailable')
        if method == 'save_paper_tags':
            self.writes += 1
        return {'status': self.last, 'writes': self.writes}
main(['--state', sys.argv[1]], worker_factory=LocalWorker)
"""
        for params, expected in [({'quiet': True}, 'ready'), ({'quiet': False}, 'needs_attention'), ([], 'needs_attention')]:
            requests = [{'id': 1, 'method': 'suggest_paper_tags', 'params': params},
                        {'id': 2, 'method': 'status', 'params': {}}]
            with tempfile.TemporaryDirectory() as directory:
                result = subprocess.run([sys.executable, '-c', program, directory],
                                        cwd=Path(__file__).resolve().parents[1],
                                        input=''.join(json.dumps(request) + '\n' for request in requests).encode(),
                                        capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr.decode('ascii', errors='replace'))
            packets = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertIn('error', packets[0])
            self.assertEqual(packets[-1]['result']['status']['state'], expected)
            self.assertEqual(packets[-1]['result']['writes'], 0)
            if expected == 'ready':
                self.assertEqual(packets[-1]['result']['status']['message'], 'Existing status')

    def test_long_request_emits_transport_heartbeat(self):
        program = '''
from pathlib import Path
import sys, threading, time
from note_organizer.zotero_worker import main
class SlowWorker:
    def __init__(self, directory, port, resources=None):
        self.directory = Path(directory)
        self.stop = threading.Event()
    def dispatch(self, method, params):
        time.sleep(16)
        return {'complete': True}
main(['--state', sys.argv[1]], worker_factory=SlowWorker)
'''
        request = {'id': 1, 'method': 'sync', 'params': {}}
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, '-c', program, directory],
                                    cwd=Path(__file__).resolve().parents[1],
                                    input=(json.dumps(request) + '\n').encode('utf-8'),
                                    capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode('ascii', errors='replace'))
        packets = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertIn({'event': 'progress', 'message': 'Still working...'}, packets)
        self.assertEqual(packets[-1], {'id': 1, 'result': {'complete': True}})

    def client(self):
        client = LocalZotero.__new__(LocalZotero)
        client.endpoint = 'http://127.0.0.1:23199/api'
        client.server_id = 'synthetic-instance'
        client.zot = SimpleNamespace(local_api_key='synthetic-test-key')
        client.authorize = Mock()
        return client

    def connection(self, response=None, error=None):
        connection = MagicMock()
        connection.__enter__.return_value = connection
        if error:
            connection.post.side_effect = error
        else:
            connection.post.return_value = response
        return connection

    def test_poll_recovers_with_fresh_bounded_connections_after_reset_or_empty_response(self):
        first = self.connection(error=httpx2.ReadError('private diagnostic must not reach UI'))
        empty = Mock(status_code=200)
        empty.json.side_effect = ValueError('Empty body')
        second = self.connection(empty)
        last = Mock(status_code=200)
        last.json.return_value = {'pending': False, 'attachment': 'TEST2345'}
        third = self.connection(last)
        with patch('httpx2.Client', side_effect=[first, second, third]) as factory, patch('time.sleep'):
            self.assertEqual(self.client().review_action('bulk_import_status', {'digest': 'a' * 64}), last.json.return_value)
        self.assertEqual(factory.call_count, 3)
        self.assertTrue(all(c.__exit__.called for c in [first, second, third]))
        self.assertEqual(third.post.call_args.kwargs['headers']['Connection'], 'close')

    def test_persistent_transport_failure_pauses_after_three_attempts(self):
        connections = [self.connection(error=httpx2.ReadError('private token')) for _ in range(3)]
        with patch('httpx2.Client', side_effect=connections) as factory, patch('time.sleep'):
            with self.assertRaises(TransportInterrupted) as caught:
                self.client().review_action('bulk_import', {})
        self.assertEqual(factory.call_count, 3)
        self.assertNotIn('private token', str(caught.exception))

    def test_collection_conflict_reports_destination_without_paper_advice(self):
        response = Mock(status_code=409)
        with patch('httpx2.Client', return_value=self.connection(response)) as factory:
            with self.assertRaisesRegex(PilotError, 'The collection could not be created. Refresh folders and retry.') as caught:
                self.client().review_action('create_collection', {})
        self.assertNotIsInstance(caught.exception, ReviewChanged)
        self.assertEqual(factory.call_count, 1)

    def test_conflicts_and_non_idempotent_actions_are_never_replayed(self):
        response = Mock(status_code=409)
        with patch('httpx2.Client', return_value=self.connection(response)) as factory:
            with self.assertRaises(ReviewChanged):
                self.client().review_action('bulk_import', {})
        self.assertEqual(factory.call_count, 1)
        with patch('httpx2.Client', return_value=self.connection(error=httpx2.ReadError('Lost response'))) as factory:
            with self.assertRaises(TransportInterrupted):
                self.client().review_action('trash', {})
        self.assertEqual(factory.call_count, 1)
