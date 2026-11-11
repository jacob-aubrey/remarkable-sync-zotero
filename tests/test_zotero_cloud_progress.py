"""Cloud progress remains readable through ordinary, cached, and retry paths."""
import io
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError
import uuid

from note_organizer.remarkable_cloud import CloudClient
from note_organizer.sorting.core_logic import _year_from_text
from tests.test_cloud_outbox import MemoryCloud


class CloudProgressTests(unittest.TestCase):
    def assert_readable(self, messages):
        self.assertTrue(messages)
        for message in messages:
            self.assertIsInstance(message, str)
            for broken in ('\u00e2\u20ac', '\u00c3', '\u00c2', '\ufffd'):
                self.assertNotIn(broken, message)

    def test_listing_and_cached_listing_have_readable_progress_and_counts(self):
        cloud = MemoryCloud()
        cloud.create_folder('Research', '', str(uuid.uuid4()))
        messages = []
        cloud.progress = messages.append
        self.assertEqual(len(cloud.library()), 1)
        self.assertEqual(len(cloud.library()), 1)
        self.assert_readable(messages)
        self.assertTrue(any('1 of 1' in message for message in messages))
        self.assertTrue(any('up to date' in message for message in messages))
        self.assertTrue(any('reMarkable\u2019s file list' in message for message in messages))

    def test_retry_progress_is_readable_without_network_or_sleep(self):
        messages = []
        cloud = CloudClient(progress=messages.append)
        cloud.opener = Mock()
        cloud.opener.open.side_effect = [URLError('synthetic retry'), io.BytesIO(b'complete')]
        with patch.object(cloud.stop, 'wait', return_value=False):
            self.assertEqual(cloud._http('https://fixture.invalid/probe'), b'complete')
        self.assert_readable(messages)
        self.assertEqual(len(messages), 1)
        self.assertIn('2 of 3', messages[0])

    def test_actual_copyright_symbol_outranks_unlabelled_old_year(self):
        self.assertEqual(_year_from_text('Earlier work from 2023. Copyright \u00a9 2026 The Authors.'), '2026')
