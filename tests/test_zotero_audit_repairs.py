"""Audit repair regressions: disposable libraries and the real cloud CAS writer."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from note_organizer.zotero_pilot import PilotError
from note_organizer.zotero_worker import OperationTrace, Worker, _periodic_stack_diagnostics, main
from note_organizer.zotero_review import fingerprint
from tests import test_zotero_worker as worker_fixtures
from tests import test_zotero_bridge as bridge_fixtures


class ConflictRecoveryTests(unittest.TestCase):
    setUp = worker_fixtures.WorkerTests.setUp
    add_pdf = worker_fixtures.WorkerTests.add_pdf

    def conflict(self):
        child = self.cloud.create_folder('Topic', self.croot.id, str(uuid.uuid4()), self.croot.id)
        self.worker.sync()
        key = next(k for k, row in self.worker.state.read('folders').items() if row['cloud'] == child.id)
        self.zot.collection_records[key]['name'] = 'Desktop choice'
        self.cloud.update_item(child.id, expected_hash=child.hash, name='Tablet choice', scope_id=self.croot.id)
        self.worker.sync()
        return child, next(row for row in self.worker.review.rows() if row['kind'] == 'conflict')

    def test_lost_cloud_conflict_response_recovers_after_restart_without_rewriting(self):
        child, row = self.conflict()
        update = self.cloud.update_item
        def commit_then_lose(*args, **kwargs):
            update(*args, **kwargs)
            raise OSError('Synthetic lost response after successful commit')
        with patch.object(self.cloud, 'update_item', side_effect=commit_then_lose):
            with self.assertRaises(OSError):
                self.worker.resolve_conflict(row['id'], row['digest'], 'desktop')
        puts = len(self.cloud.puts)
        self.worker.save()
        self.worker = Worker(self.worker.directory, zotero_factory=lambda: self.zot, cloud_factory=lambda: self.cloud)
        self.worker.resolve_conflict(row['id'], row['digest'], 'desktop')
        self.assertEqual(len(self.cloud.puts), puts)
        self.assertEqual(self.worker.review.row(row['id'])['status'], 'approved')
        self.worker.sync()
        self.assertFalse(self.worker.review.rows())

    def test_lost_native_conflict_response_recovers_without_a_second_native_update(self):
        child, row = self.conflict()
        update = self.zot.update_collection
        def commit_then_lose(record):
            update(record)
            raise OSError('Synthetic lost native response')
        with patch.object(self.zot, 'update_collection', side_effect=commit_then_lose):
            with self.assertRaises(OSError):
                self.worker.resolve_conflict(row['id'], row['digest'], 'tablet')
        with patch.object(self.zot, 'update_collection', side_effect=AssertionError('Already complete')):
            self.worker.resolve_conflict(row['id'], row['digest'], 'tablet')
        self.worker.sync()
        self.assertFalse(self.worker.review.rows())

    def test_interrupted_choice_cannot_change_sides_or_overwrite_a_later_tablet_edit(self):
        child, row = self.conflict()
        update = self.cloud.update_item
        def commit_then_lose(*args, **kwargs):
            update(*args, **kwargs)
            raise OSError('Synthetic lost cloud response')
        with patch.object(self.cloud, 'update_item', side_effect=commit_then_lose):
            with self.assertRaises(OSError):
                self.worker.resolve_conflict(row['id'], row['digest'], 'desktop')
        current = next(item for item in self.cloud.library() if item.id == child.id)
        changed = update(current.id, expected_hash=current.hash, name='Later independent name', scope_id=self.croot.id)
        puts = len(self.cloud.puts)
        for side in ('desktop', 'tablet'):
            with self.assertRaises(PilotError):
                self.worker.resolve_conflict(row['id'], row['digest'], side)
        self.assertEqual(len(self.cloud.puts), puts)
        self.assertEqual(next(item.hash for item in self.cloud.library() if item.id == child.id), changed.hash)

    def test_partial_paper_rename_resumes_only_the_remaining_attachment(self):
        source = self.add_pdf()
        self.zot.items['VIEWKEY2'] = {**self.zot.items['SOURCE23'], 'key': 'VIEWKEY2'}
        self.zot.file_path('VIEWKEY2').write_bytes(source.read_bytes())
        item = self.cloud.upload_pdf(source.read_bytes(), 'Tablet choice', self.croot.id,
                                     str(uuid.uuid4()), self.croot.id)
        self.worker.state.write('folders', {'ROOT2345': {'cloud': self.croot.id, 'root': True}})
        self.worker.state.write('documents', {item.id: {'parent': 'PARENT23', 'collection': 'ROOT2345',
            'source_key': 'SOURCE23', 'view_key': 'VIEWKEY2', 'name': 'Original', 'desktop_name': 'Original'}})
        payload = {'type': 'paper', 'zotero': 'PARENT23', 'cloud': item.id,
                   'desktop': ['VIEWKEY2', 'ROOT2345'], 'tablet': ['Tablet choice', 'ROOT2345'],
                   'cloud_hash': item.hash, 'version': 0, 'file_md5': hashlib.md5(source.read_bytes()).hexdigest()}
        identity = self.worker.review.offer('conflict', item.id, fingerprint(payload), payload)
        rename = self.zot.review_action
        renamed = []
        def commit_first_then_lose(action, data):
            result = rename(action, data)
            renamed.append(data['key'])
            if len(renamed) == 1:
                raise OSError('Synthetic lost first attachment rename response')
            return result
        with patch.object(self.zot, 'review_action', side_effect=commit_first_then_lose):
            with self.assertRaises(OSError):
                self.worker.resolve_conflict(identity, fingerprint(payload), 'tablet')
            self.worker.resolve_conflict(identity, fingerprint(payload), 'tablet')
        self.assertEqual(renamed, ['SOURCE23', 'VIEWKEY2'])
        self.assertEqual(self.worker.review.row(identity)['status'], 'approved')


class FilingRecoveryTests(unittest.TestCase):
    setUp = worker_fixtures.WorkerTests.setUp
    add_pdf = worker_fixtures.WorkerTests.add_pdf

    def paper(self):
        item = self.cloud.upload_pdf(worker_fixtures.pdf_bytes(), 'Tablet paper', self.croot.id,
                                     str(uuid.uuid4()), self.croot.id)
        self.worker.sync()
        row = next(row for row in self.worker.review.rows() if row['kind'] == 'cloud')
        args = (row['id'], row['digest'], 'Reviewed.pdf', 'Reviewed paper', 'journal_article', 'ROOT2345')
        return item, row, args

    def test_resume_preserves_newer_tablet_name_before_any_more_native_writes(self):
        item, row, args = self.paper()
        with patch.object(self.worker.review, 'finish', side_effect=OSError('Synthetic interrupted completion')):
            with self.assertRaises(OSError):
                self.worker.approve(*args)
        current = next(value for value in self.cloud.library() if value.id == item.id)
        self.cloud.update_item(item.id, expected_hash=current.hash, name='Later user rename', scope_id=self.croot.id)
        writes = self.zot.writes
        puts = len(self.cloud.puts)
        with self.assertRaisesRegex(PilotError, 'newer changes were retained'):
            self.worker.approve(*args)
        self.assertEqual(self.zot.writes, writes)
        self.assertEqual(len(self.cloud.puts), puts)
        self.assertEqual(next(value.name for value in self.cloud.library() if value.id == item.id), 'Later user rename')

    def test_lost_filing_move_response_recovers_without_another_cloud_mutation(self):
        item, row, args = self.paper()
        update = self.cloud.update_item
        def commit_then_lose(*values, **kwargs):
            update(*values, **kwargs)
            raise OSError('Synthetic lost move response')
        with patch.object(self.cloud, 'update_item', side_effect=commit_then_lose):
            with self.assertRaises(OSError):
                self.worker.approve(*args)
        puts = len(self.cloud.puts)
        self.worker.approve(*args)
        self.assertEqual(len(self.cloud.puts), puts)
        self.assertEqual(self.worker.review.row(row['id'])['status'], 'approved')

    def test_lost_response_with_new_tags_does_not_claim_the_old_move_is_complete(self):
        item, row, args = self.paper()
        update = self.cloud.update_item
        def commit_then_change(*values, **kwargs):
            changed = update(*values, **kwargs)
            update(changed.id, expected_hash=changed.hash, tags=['Independent tablet edit'], scope_id=self.croot.id)
            raise OSError('Synthetic loss with an independent subsequent edit')
        with patch.object(self.cloud, 'update_item', side_effect=commit_then_change):
            with self.assertRaises(OSError):
                self.worker.approve(*args)
        puts = len(self.cloud.puts)
        with self.assertRaisesRegex(PilotError, 'newer changes were retained'):
            self.worker.approve(*args)
        self.assertEqual(len(self.cloud.puts), puts)

    def test_resume_keeps_the_original_filing_choices(self):
        item, row, args = self.paper()
        with patch.object(self.worker.review, 'finish', side_effect=OSError('Synthetic interrupted completion')):
            with self.assertRaises(OSError):
                self.worker.approve(*args)
        writes = self.zot.writes
        with self.assertRaisesRegex(PilotError, 'saved choices'):
            self.worker.approve(*args[:2], 'Different.pdf', *args[3:])
        self.assertEqual(self.zot.writes, writes)

    def test_sync_reopens_changed_unlinked_filing_with_fresh_consent_and_no_duplicate_reference(self):
        item, row, args = self.paper()
        with patch.object(self.worker.review, 'finish', side_effect=OSError('Synthetic interrupted completion')):
            with self.assertRaises(OSError):
                self.worker.approve(*args)
        current = next(value for value in self.cloud.library() if value.id == item.id)
        self.cloud.update_item(item.id, expected_hash=current.hash, name='Later user rename', scope_id=self.croot.id)
        references = set(self.zot.items)
        self.worker.sync()
        refreshed = self.worker.review.row(row['id'])
        self.assertEqual(refreshed['status'], 'pending')
        self.assertEqual(refreshed['payload']['title'], 'Later user rename')
        self.assertEqual(refreshed['approval'], {})
        self.worker.approve(row['id'], row['digest'], 'Later user rename.pdf', 'Later user rename',
                            'journal_article', 'ROOT2345', review_token=refreshed['payload']['review_token'])
        self.assertEqual(set(self.zot.items), references)
        self.assertEqual(self.worker.review.row(row['id'])['status'], 'approved')

    def test_registered_matched_filing_finishes_bookkeeping_without_replaying_old_name(self):
        path = self.add_pdf()
        item = self.cloud.upload_pdf(path.read_bytes(), 'Tablet paper', self.croot.id, str(uuid.uuid4()), self.croot.id)
        with patch.object(self.worker.review, 'observe_ready', return_value=True):
            self.worker.sync()
        row = next(value for value in self.worker.review.rows() if value['kind'] == 'cloud')
        args = (row['id'], row['digest'], 'Reviewed.pdf', 'Reviewed paper', 'journal_article', 'ROOT2345')
        with patch.object(self.worker.review, 'finish', side_effect=OSError('Synthetic interruption after registration')):
            with self.assertRaises(OSError):
                self.worker.approve(*args, match='SOURCE23')
        current = next(value for value in self.cloud.library() if value.id == item.id)
        self.cloud.update_item(item.id, expected_hash=current.hash, name='Later user rename', scope_id=self.croot.id)
        with patch('note_organizer.zotero_worker.Bridge') as bridge:
            bridge.return_value.run.return_value = {'review': [], 'actions': []}
            self.worker.sync()
        self.assertEqual(next(value.name for value in self.cloud.library() if value.id == item.id), 'Later user rename')
        self.assertEqual(self.worker.review.row(row['id'])['status'], 'approved')


class DesktopValidationTests(unittest.TestCase):
    setUp = bridge_fixtures.BridgeTests.setUp
    add_desktop = bridge_fixtures.BridgeTests.add_desktop

    def test_invalid_windows_name_retains_all_collection_membership(self):
        self.add_desktop()
        parent = copy.deepcopy(self.zot.items['PARENT23'])
        row = {'parent': 'PARENT23', 'collection': 'ROOT2345', 'source_key': 'SOURCE23'}
        with self.assertRaises(ValueError):
            self.engine.set_desktop_document(row, 'Invalid:WindowsName', 'TARGET23', {})
        self.assertEqual(self.zot.items['PARENT23'], parent)
        self.assertEqual(self.zot.writes, 0)

    def test_unavailable_second_attachment_retains_membership_and_first_filename(self):
        self.add_desktop()
        parent = copy.deepcopy(self.zot.items['PARENT23'])
        row = {'parent': 'PARENT23', 'collection': 'ROOT2345', 'source_key': 'SOURCE23', 'view_key': 'MISSING2'}
        with self.assertRaises(PilotError):
            self.engine.set_desktop_document(row, 'Valid name', 'TARGET23', {})
        self.assertEqual(self.zot.items['PARENT23'], parent)
        self.assertEqual(self.zot.writes, 0)


class WaitingTransferStatusTests(unittest.TestCase):
    setUp = worker_fixtures.WorkerTests.setUp

    def approved(self, kind, identity, payload, approval):
        digest = fingerprint(identity)
        key = self.worker.review.offer(kind, identity, digest, payload)
        self.worker.review.begin(key, digest, approval)
        self.worker.review.finish(key)

    def test_waiting_count_includes_only_pdf_eligible_approvals_and_deduplicates_sources(self):
        self.approved('zotero', 'PDFKEY23', {'path': 'Original.PDF'}, {'source': 'PDFKEY23', 'filename': 'Reviewed.pdf'})
        self.approved('zotero', 'WORDKEY2', {'path': 'Original.docx'}, {'source': 'WORDKEY2', 'filename': 'Reviewed.docx'})
        self.approved('zotero', 'SLIDEKEY', {'path': 'Original.pptx'}, {'source': 'SLIDEKEY', 'filename': 'Reviewed.pptx'})
        self.approved('zotero', 'LEGACY23', {}, {'source': 'LEGACY23', 'filename': 'Legacy.PDF'})
        self.approved('cloud', 'tablet-paper', {'path': 'cached-without-extension'}, {'source': 'TABLET23'})
        self.approved('inbox', 'download', {'path': 'Download.pdf'}, {'source': 'INBOXKEY'})
        self.approved('zotero', 'INBOXKEY', {'path': 'Stored.pdf'}, {'source': 'INBOXKEY'})
        self.worker.review.offer('zotero', 'PENDING2', fingerprint('pending'), {'path': 'Pending.pdf'})
        self.assertEqual(self.worker.status()['filing_summary'],
                         {'approved_waiting_sync': 4, 'linked': 0, 'pdfs_received': 0})

    def test_linked_and_paused_pdfs_do_not_return_to_waiting_while_global_pause_retains_untransferred_pdfs(self):
        self.approved('zotero', 'LINKED23', {'path': 'Linked.pdf'}, {'source': 'LINKED23'})
        self.approved('zotero', 'PAUSED23', {'path': 'Paused.pdf'}, {'source': 'PAUSED23'})
        self.approved('cloud', 'linked-cloud', {'path': 'Cached.pdf'}, {'source': 'CLOUDKEY'})
        self.approved('zotero', 'WAITING2', {'path': 'Waiting.pdf'}, {'source': 'WAITING2'})
        self.approved('zotero', 'OFFICE23', {'path': 'Filed.docx'}, {'source': 'OFFICE23'})
        self.worker.state.write('documents', {
            'linked-pdf': {'source_key': 'LINKED23', 'revision': 'received'},
            'paused-pdf': {'source_key': 'PAUSED23'},
            'linked-cloud': {'source_key': 'CLOUDKEY', 'revision': 'received'}})
        self.worker.review.link_control('paused-pdf', paused=True)
        self.worker.config['sync_paused'] = True
        self.assertEqual(self.worker.status()['filing_summary'],
                         {'approved_waiting_sync': 1, 'linked': 3, 'pdfs_received': 2})

    def test_staged_uploads_count_once_per_source_and_leave_waiting_after_upload(self):
        self.approved('zotero', 'STAGED23', {'path': 'Staged.pdf'}, {'source': 'STAGED23'})
        self.approved('zotero', 'WAITING2', {'path': 'Waiting.pdf'}, {'source': 'WAITING2'})
        self.approved('zotero', 'OFFICE23', {'path': 'Filed.docx'}, {'source': 'OFFICE23'})
        documents = {'staged-one': {'origin': 'zotero', 'source_key': 'STAGED23', 'uploaded': False},
                     'same-source': {'origin': 'zotero', 'source_key': 'STAGED23', 'uploaded': False},
                     'staged-two': {'origin': 'zotero', 'source_key': 'STAGED24', 'uploaded': False}}
        self.worker.state.write('documents', documents)
        self.worker.config['sync_paused'] = True
        self.assertEqual(self.worker.status()['filing_summary'],
                         {'approved_waiting_sync': 3, 'linked': 3, 'pdfs_received': 0})
        for row in documents.values():
            row['uploaded'] = True
        self.worker.state.write('documents', documents)
        self.assertEqual(self.worker.status()['filing_summary'],
                         {'approved_waiting_sync': 1, 'linked': 3, 'pdfs_received': 0})

    def test_staged_count_excludes_individually_paused_deleted_unavailable_and_non_upload_rows(self):
        documents = {identity: {'origin': 'zotero', 'source_key': source, 'uploaded': False}
                     for identity, source in [('active', 'ACTIVE23'), ('paused', 'PAUSED23'),
                                              ('deleted', 'DELETED2'), ('unavailable', 'MISSING2')]}
        documents['unavailable']['unavailable'] = True
        documents['cloud-origin'] = {'origin': 'cloud', 'source_key': 'CLOUDKEY', 'uploaded': False}
        documents['no-source'] = {'origin': 'zotero', 'uploaded': False}
        self.worker.state.write('documents', documents)
        self.worker.review.link_control('paused', paused=True)
        self.worker.review.confirm_deleted(SimpleNamespace(id='deleted', hash='a' * 64, deleted=True, parent='trash'),
            {'name': 'Disposable', 'tablet_parent': 'home'}, attachment='VIEWKEY2', parent='PARENT23', inventory=[])
        self.assertEqual(self.worker.status()['filing_summary']['approved_waiting_sync'], 1)


class DiagnosticSafetyTests(unittest.TestCase):
    setUp = worker_fixtures.WorkerTests.setUp

    def test_periodic_diagnostics_emit_python_stacks_and_stop_without_a_native_watchdog(self):
        path = self.root / 'stacks.log'
        with patch('faulthandler.dump_traceback_later', side_effect=AssertionError('Unsafe native watchdog')):
            with _periodic_stack_diagnostics(path, interval=.01) as thread:
                deadline = time.monotonic() + 3
                while (not path.exists() or path.stat().st_size == 0) and time.monotonic() < deadline:
                    time.sleep(.01)
            self.assertFalse(thread.is_alive())
        snapshot = path.read_text(encoding='utf-8')
        self.assertIn('Periodic stack snapshot', snapshot)
        self.assertIn('test_periodic_diagnostics_emit_python_stacks', snapshot)
        size = path.stat().st_size
        time.sleep(.03)
        self.assertEqual(path.stat().st_size, size)

    def test_diagnostic_context_closes_when_worker_startup_fails(self):
        with patch('sys.stdin', io.StringIO('')), patch('threading.Thread', wraps=threading.Thread) as factory:
            with self.assertRaisesRegex(RuntimeError, 'Synthetic startup failure'):
                main(['--state', str(self.root), '--debug-stacks'],
                     worker_factory=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('Synthetic startup failure')))
        diagnostics = factory.return_value
        # The actual thread returned by the wrapped constructor has exited;
        # opening for replacement also proves the stream was released on Windows.
        self.assertFalse(any(t.name == 'Notable stack diagnostics' for t in threading.enumerate()))
        (self.root / 'test-worker-stack.log').write_text('closed', encoding='utf-8')

    def test_debug_diagnostics_leave_json_transport_clean_and_stop_at_eof(self):
        class EchoWorker:
            def __init__(self, directory, port, resources=None):
                self.directory = Path(directory)
                self.stop = threading.Event()
            def dispatch(self, method, params):
                return params
        request = json.dumps({'id': 7, 'method': 'echo', 'params': {'title': 'Disposable'}}) + '\n'
        output = io.StringIO()
        with patch('sys.stdin', io.StringIO(request)), patch('sys.stdout', output):
            main(['--state', str(self.root), '--debug-stacks'], worker_factory=EchoWorker)
        self.assertEqual(json.loads(output.getvalue()), {'id': 7, 'result': {'title': 'Disposable'}})
        self.assertFalse(any(t.name == 'Notable stack diagnostics' for t in threading.enumerate()))

    def test_operation_trace_persists_safe_stage_and_error_records(self):
        trace = OperationTrace(self.root / 'sync-trace.jsonl')
        trace.start('sync')
        trace.event('stage', name='syncing_bridge')
        trace.event('error', type='ValueError', message='The source changed.')
        trace.finish('error')
        records = [json.loads(line) for line in (self.root / 'sync-trace.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual([record['event'] for record in records],
                         ['operation_started', 'stage', 'error', 'operation_finished'])
        self.assertEqual(records[1]['name'], 'syncing_bridge')
        self.assertEqual(records[2]['message'], 'The source changed.')
        self.assertNotIn('token', json.dumps(records))


class PdfDiagnosticTransportTests(unittest.TestCase):
    setUp = worker_fixtures.WorkerTests.setUp

    def test_real_pdf_diagnostics_and_cached_development_log_stay_out_of_json_stdout(self):
        program = '''
from pathlib import Path
import sys, threading
import pymupdf
from note_organizer.zotero_worker import main
class PdfWorker:
    def __init__(self, directory, port, resources=None):
        self.directory = Path(directory)
        self.stop = threading.Event()
    def dispatch(self, method, params):
        with pymupdf.open() as document:
            page = document.new_page()
            xref = document.get_new_xref()
            document.update_object(xref, '<<>>')
            document.update_stream(xref, b'q garbage Q')
            page.set_contents(xref)
            data = document.tobytes()
        with pymupdf.open(stream=data, filetype='pdf') as document:
            document[0].get_text()
            document[0].get_pixmap()
        pymupdf.log('Synthetic cached development diagnostic')
        return {'processed': True}
main(['--state', sys.argv[1]], worker_factory=PdfWorker)
'''
        request = {'id': 71, 'method': 'process', 'params': {}}
        result = subprocess.run([sys.executable, '-c', program, str(self.root)],
                                cwd=Path(__file__).resolve().parents[1],
                                env={key: value for key, value in os.environ.items()
                                     if key not in {'PYMUPDF_MESSAGE', 'PYMUPDF_LOG'}},
                                input=(json.dumps(request) + '\n').encode('utf-8'), capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        self.assertEqual([json.loads(line) for line in result.stdout.splitlines()],
                         [{'id': 71, 'result': {'processed': True}}])
        self.assertIn(b'MuPDF error:', result.stderr)
        self.assertIn(b'Synthetic cached development diagnostic', result.stderr)


if __name__ == '__main__':
    unittest.main()
