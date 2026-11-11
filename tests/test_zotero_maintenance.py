from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import pymupdf
from note_organizer.zotero_maintenance import (compare_pdfs, export_annotations, highlight_review,
                                               name_review, repair_keywords, undo_keyword_repair)
from note_organizer.zotero_pilot import PilotError
from note_organizer.zotero_state_backup import backup_state, restore_state
from note_organizer.zotero_worker import Worker


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.worker = Worker(self.root / 'plugin')
        self.client = Mock(server_id='fixture-instance')
        self.worker.zotero = lambda: self.client

    def test_name_queue_finds_zotero_and_tablet_names_without_editing_either(self):
        catalog = {'parents': {'PAPER234': {'title': 'Comparison of liquid embolics'},
                               'PAPER235': {'title': 'An unknown publication year'}},
                   'attachments': [{'path': str(self.root / 'Gurevich_et_al_UnknownJournal_2025.pdf'),
                                    'data': {'key': 'ATTCH234', 'parentItem': 'PAPER234'}}]}
        self.client.review_action.return_value = catalog
        self.worker.state.write('documents', {'tablet': {'parent': 'PAPER234', 'name': 'Paper_UnknownYear'}})
        result = name_review(self.worker)
        self.assertEqual(len(result['matches']), 2)
        entry = next(row for row in result['matches'] if row['key'] == 'PAPER234')
        self.assertEqual({finding['where'] for finding in entry['findings']},
                         {'Zotero filename', 'Saved reMarkable name'})
        self.assertEqual(len(name_review(self.worker, selected=[entry])['queued']), 1)
        self.client.update.assert_not_called()
        catalog['attachments'].clear()
        with self.assertRaisesRegex(PilotError, 'changed'):
            name_review(self.worker, selected=[entry])

    def test_lost_tag_response_can_be_undone_without_dropping_manual_tags(self):
        before = [{'tag': 'Manual', 'type': 0}, {'tag': 'Models and', 'type': 1}]
        current = {'key': 'PAPER234', 'title': 'Fixture paper', 'version': 1, 'tags': deepcopy(before)}
        report = {'keywords': [{'tag': 'Models and', 'source': 'Printed keyword/index-term section',
                                'evidence': 'Keywords: Models and'}]}
        self.worker.state.write('documents', {'tablet': {'parent': 'PAPER234', 'keyword_check': report}})
        self.client.get.side_effect = lambda *_: deepcopy(current)

        def lost_response(value):
            current.update(value)
            raise ConnectionError('lost response')

        preview = self.worker._keyword_cleanup_preview(current, [report])
        self.client.update.side_effect = lost_response
        with self.assertRaises(ConnectionError):
            repair_keywords(self.worker, [{'parent': 'PAPER234', 'token': preview['token'], 'tags': ['Models and']}])
        self.assertEqual(current['tags'], before[:1])
        [journal] = (self.worker.directory / 'tag-repairs').glob('*.json')
        self.client.update.side_effect = lambda value: current.update(value)
        self.assertEqual(undo_keyword_repair(self.worker, journal.stem)['restored'], 1)
        self.assertEqual(current['tags'], before)

    def test_unlinked_auto_tag_repair_can_unquote_and_merge_without_changing_manual_tags(self):
        before = [{'tag': tag, 'type': tag_type} for tag, tag_type in [
            ('"Clinical challenges"', 1), ('"Gold"', 1), ('(Invest Radiol 2004', 1),
            ('()', 0), ('Gold', 0), ('{M6O19}-type POM', 1)]]
        current = {'key': 'PAPER234', 'title': 'Fixture paper', 'version': 1, 'tags': deepcopy(before)}
        self.client.review_action.return_value = {'parents': {'PAPER234': deepcopy(current)}}
        self.client.get.side_effect = lambda *_: deepcopy(current)
        self.client.update.side_effect = lambda value: current.update(value)
        preview, = self.worker.preview_keyword_cleanup()['papers']
        self.client.review_action.assert_called_once_with('catalog', {'parents_only': True})
        self.assertEqual(current['tags'], before)
        selected = [row['tag'] for row in preview['tags']]
        self.assertEqual(set(selected), {'"Clinical challenges"', '"Gold"', '(Invest Radiol 2004'})
        result = repair_keywords(self.worker, [{'parent': 'PAPER234', 'token': preview['token'], 'tags': selected}])
        self.assertEqual((result['removed'], result['renamed']), (1, 2))
        self.assertEqual(current['tags'], [{'tag': '()', 'type': 0}, {'tag': 'Clinical challenges', 'type': 1},
                                          {'tag': 'Gold', 'type': 0}, {'tag': '{M6O19}-type POM', 'type': 1}])
        self.assertEqual(undo_keyword_repair(self.worker, result['id'])['restored'], 3)
        self.assertEqual(current['tags'], sorted(before, key=lambda row: row['tag']))

    def test_backup_restores_saved_state_paused_without_credentials(self):
        self.worker.config.update(automatic=True, sync_paused=False, tablet_root='fixture', api_key='SECRET')
        self.worker.state.write('documents', {'tablet': {'parent': 'PAPER234'}})
        (self.worker.directory / 'tablet-token.dpapi').write_bytes(b'secret')
        archive = self.root / 'backup.zip'
        destination = self.root / 'recovered'
        self.assertGreater(backup_state(self.worker, archive)['files'], 1)
        self.assertTrue(restore_state(self.worker, archive, destination)['paused'])
        config = json.loads((destination / 'config.json').read_text())
        self.assertFalse(config['automatic'])
        self.assertNotIn('api_key', config)
        self.assertFalse((destination / 'tablet-token.dpapi').exists())
        db = sqlite3.connect(destination / 'bridge' / 'pilot.sqlite3')
        try:
            self.assertIn('PAPER234', db.execute("select value from bridge_values where key='documents'").fetchone()[0])
        finally:
            db.close()
        with self.assertRaises(PilotError):
            restore_state(self.worker, archive, destination)

    def test_highlight_review_checks_stale_choice_and_can_undo(self):
        self.worker.state.write('documents', {'tablet': {'view_key': 'FILE2345', 'revision': 'known',
                                                          'group_highlights': True}})
        group = {'members': ['one', 'two'], 'text': 'Fixture', 'page': 1}
        with patch('note_organizer.zotero_maintenance._highlight_preview',
                   return_value={'token': 'current', 'groups': [group]}):
            with self.assertRaises(PilotError):
                highlight_review(self.worker, 'tablet', token='old', selected=[0])
            self.assertTrue(highlight_review(self.worker, 'tablet', token='current', selected=[0])['queued'])
            updated = self.worker.state.read('documents')['tablet']
            self.assertEqual(updated['approved_highlight_groups'], [['one', 'two']])
            highlight_review(self.worker, 'tablet', token='current', undo=True)
        restored = self.worker.state.read('documents')['tablet']
        self.assertTrue(restored['group_highlights'])
        self.assertNotIn('approved_highlight_groups', restored)

    def test_pdf_comparison_and_annotation_links_use_exact_attachments(self):
        paths = {key: self.root / (key + '.pdf') for key in ('FILE2345', 'FILE2346')}
        for key, pages in zip(paths, (('First sample page',), ('Inserted cover', 'First sample page'))):
            with pymupdf.open() as pdf:
                for text in pages:
                    pdf.new_page().insert_text((40, 50), text)
                pdf.save(paths[key])
        self.client.get.return_value = {'itemType': 'attachment', 'contentType': 'application/pdf'}
        self.client.file_path.side_effect = paths.__getitem__
        self.assertEqual(compare_pdfs(self.worker, *paths)['page_map'], [[1, 2]])
        self.client.reader_annotations.return_value = [{'key': 'ANNT2345', 'annotationText': 'Exact quote',
            'annotationComment': 'Exact comment', 'annotationPosition': '{"pageIndex":1}'}]
        target = self.root / 'annotations.md'
        export_annotations(self.worker, 'FILE2346', target)
        saved = target.read_text(encoding='utf-8')
        self.assertIn('zotero://open-pdf/library/items/FILE2346?page=2&annotation=ANNT2345', saved)
        self.assertIn('Exact quote\n\nExact comment', saved)
