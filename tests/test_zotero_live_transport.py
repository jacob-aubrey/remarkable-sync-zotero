"""File transport polling uses disposable packets without starting Zotero."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.test_zotero_plugin_live import command


class LiveTransportTests(unittest.TestCase):
    def test_partial_responses_and_an_old_command_wait_for_the_matching_packet(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            response = root / 'response.json'
            response.write_text('', encoding='utf-8')
            expected = {'id': 'current-command', 'result': {'passed': True}}
            packets = iter(['{"id":', json.dumps({'id': 'previous-command', 'result': {}}), json.dumps(expected)])

            def advance(_seconds):
                response.write_text(next(packets), encoding='utf-8')

            with patch('tools.test_zotero_plugin_live.uuid.uuid4', return_value='current-command'), \
                    patch('tools.test_zotero_plugin_live.time.monotonic', return_value=0), \
                    patch('tools.test_zotero_plugin_live.time.sleep', side_effect=advance) as sleep:
                self.assertEqual(command(root, 'layout', tab='setup'), expected)
            self.assertEqual(sleep.call_count, 3)
            sent = json.loads((root / 'command.json').read_text(encoding='utf-8'))
            self.assertEqual(sent, {'id': 'current-command', 'action': 'layout', 'tab': 'setup'})

    def test_persistent_malformed_response_still_reaches_the_existing_deadline(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'response.json').write_text('{', encoding='utf-8')
            with patch('tools.test_zotero_plugin_live.uuid.uuid4', return_value='current-command'), \
                    patch('tools.test_zotero_plugin_live.time.monotonic', side_effect=[0, 1, 136]), \
                    patch('tools.test_zotero_plugin_live.time.sleep') as sleep:
                with self.assertRaisesRegex(RuntimeError, 'Plugin UI command timed out'):
                    command(root, 'layout')
            sleep.assert_called_once_with(.15)

    def test_matching_valid_error_packet_is_reported_without_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'response.json').write_text(json.dumps({
                'id': 'current-command', 'error': 'Layout failed', 'stack': 'Synthetic traceback'
            }), encoding='utf-8')
            with patch('tools.test_zotero_plugin_live.uuid.uuid4', return_value='current-command'), \
                    patch('tools.test_zotero_plugin_live.time.sleep') as sleep:
                with self.assertRaisesRegex(RuntimeError, 'Layout failed\nSynthetic traceback'):
                    command(root, 'layout')
            sleep.assert_not_called()


if __name__ == '__main__':
    unittest.main()
