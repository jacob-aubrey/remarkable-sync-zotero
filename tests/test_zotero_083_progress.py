import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
import uuid

from note_organizer.remarkable_cloud import CloudClient
from tests.test_cloud_outbox import MemoryCloud


class CatalogProgressTests(unittest.TestCase):
    def test_catalog_reports_actual_phase_and_reuses_only_unchanged_root(self):
        cloud = MemoryCloud()
        events = []
        cloud.progress = events.append
        folder = cloud.create_folder('Literature', '', str(uuid.uuid4()))
        cloud.get = Mock(wraps=cloud.get)
        first = cloud.library()
        self.assertTrue(any('file list: 1 of 1' in e for e in events))
        self.assertTrue(events[-1].startswith('Finished reading'))
        # Callers may edit their list/objects; cached evidence must stay intact.
        first[0].name = 'Caller edit'
        first.clear()
        cloud.get.reset_mock()
        self.assertEqual(cloud.library()[0].name, 'Literature')
        self.assertEqual(cloud.get.call_count, 1)  # A fresh root check is required.
        cloud.update_item(folder.id, expected_hash=folder.hash, name='Papers')
        self.assertEqual(cloud.library()[0].name, 'Papers')
        cloud.stop.set()
        with self.assertRaises(InterruptedError):
            cloud.library()

    def test_cache_budget_is_enforced_without_rescanning_after_every_blob(self):
        with tempfile.TemporaryDirectory() as directory:
            cloud = CloudClient(cache_dir=Path(directory), cache_limit_bytes=30)
            cloud.prune_cache = Mock(wraps=cloud.prune_cache)
            for index in range(4):
                data = bytes([index]) * 10
                cloud.get = Mock(return_value=data)
                self.assertEqual(cloud.blob(hashlib.sha256(data).hexdigest(), 'synthetic.metadata'), data)
                self.assertLessEqual(sum(p.stat().st_size for p in Path(directory).iterdir()), 30)
            self.assertEqual(cloud.prune_cache.call_count, 2)


if __name__ == '__main__':
    unittest.main()
