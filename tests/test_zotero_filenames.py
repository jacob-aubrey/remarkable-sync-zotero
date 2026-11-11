import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid

from note_organizer.zotero_bridge import Bridge
from note_organizer.zotero_filenames import citation_filename
from note_organizer.zotero_pilot import PilotError
from tests import test_zotero_worker as fixtures


class FilenameCleanupTests(unittest.TestCase):
    setUp = fixtures.WorkerTests.setUp
    add_pdf = fixtures.WorkerTests.add_pdf

    def setup_link(self):
        original = self.add_pdf()
        filename = 'Taylor_UnknownJournal_UnknownYear.pdf'
        storage = self.root / 'managed'
        storage.mkdir()
        path = original.rename(storage / filename)
        source = self.zot.items['SOURCE23']
        source['filename'] = filename
        self.zot.items['PARENT23'].update({
            'itemType': 'journalArticle', 'title': 'Photon-counting CT',
            'publicationTitle': 'Radiology', 'date': '2024-01',
            'creators': [{'creatorType': 'author', 'firstName': 'Ellen', 'lastName': 'Taylor'}],
        })
        self.zot.file_path = lambda key: storage / self.zot.items[key]['filename']
        native = self.zot.review_action

        def review_action(action, data=None):
            if action == 'rename':
                current = self.zot.file_path(data['key'])
                assert hashlib.md5(current.read_bytes()).hexdigest() == data['md5']
                assert source['parentItem'] == data['expected_parent']
                assert current.name in {data['before_filename'], data['filename']}
                current.rename(current.with_name(data['filename']))
            return native(action, data)

        self.zot.review_action = review_action
        item = self.cloud.upload_pdf(path.read_bytes(), path.stem, self.croot.id, str(uuid.uuid4()), self.croot.id)
        row = {'parent': 'PARENT23', 'name': path.stem, 'desktop_name': path.stem, 'source_key': 'SOURCE23',
               'collection': 'ROOT2345', 'revision': item.hash, 'baseline': hashlib.sha256(path.read_bytes()).hexdigest()}
        self.worker.state.write('folders', {'ROOT2345': {'cloud': self.croot.id}})
        self.worker.state.write('documents', {item.id: row})
        return item, path

    def preview(self):
        row, = self.worker.preview_filename_cleanup()['papers']
        return row, {key: row[key] for key in ('identity', 'token')}

    def test_preview_queue_and_resume_after_lost_tablet_response(self):
        item, path = self.setup_link()
        preview, selection = self.preview()
        self.assertEqual(preview['target'], 'Taylor_Radiology_2024')
        self.assertEqual(preview['files'], [{'before': path.name, 'filename': 'Taylor_Radiology_2024.pdf'}])
        self.assertEqual(self.worker.queue_filename_cleanup([selection]), {'queued': [item.id], 'skipped': []})
        self.assertTrue(path.is_file(), 'Queueing does not rename the Zotero file')
        self.assertEqual(self.cloud.pdf_bundle(item)[item.id + '.pdf'], path.read_bytes())
        original = path.read_bytes()
        cloud_update = self.cloud.update_item

        def lost_response(*args, **kwargs):
            cloud_update(*args, **kwargs)
            raise OSError('Tablet response lost')

        bridge = Bridge(self.zot, self.cloud, self.worker.state, 'ROOT2345', self.croot.id)
        links = self.worker.state.read('documents')
        row = links[item.id]
        save = lambda: self.worker.state.write('documents', links)
        with patch.object(self.cloud, 'update_item', side_effect=lost_response):
            with self.assertRaises(OSError):
                bridge._apply_filename_cleanup(row, self.zot.get('item', 'PARENT23'), item,
                                               'ROOT2345', self.cloud.library(), save)
        self.assertTrue(self.worker.state.read('documents')[item.id]['filename_cleanup']['started'])
        current = next(entry for entry in self.cloud.library() if entry.id == item.id)
        bridge._apply_filename_cleanup(row, self.zot.get('item', 'PARENT23'), current,
                                       'ROOT2345', self.cloud.library(), save)
        self.assertEqual(current.name, 'Taylor_Radiology_2024')
        self.assertEqual(self.zot.file_path('SOURCE23').read_bytes(), original)
        self.assertEqual(self.zot.items['PARENT23']['title'], 'Photon-counting CT')
        self.assertNotIn('filename_cleanup', self.worker.state.read('documents')[item.id])
        self.worker.undo_filename_cleanup(item.id)
        links = self.worker.state.read('documents')
        row = links[item.id]
        restored = bridge._apply_filename_cleanup(row, self.zot.get('item', 'PARENT23'), current,
                                                   'ROOT2345', self.cloud.library(), save)
        self.assertEqual(restored.name, path.stem)
        self.assertEqual(self.zot.file_path('SOURCE23'), path)
        self.assertEqual(path.read_bytes(), original)
        self.assertNotIn('filename_repair', self.worker.state.read('documents')[item.id])

    def test_changed_evidence_does_not_queue_or_apply(self):
        item, path = self.setup_link()
        _, selection = self.preview()
        parent = self.zot.items['PARENT23']
        parent['date'] = '2025'
        self.assertEqual(self.worker.queue_filename_cleanup([selection]), {'queued': [], 'skipped': [item.id]})
        _, selection = self.preview()
        self.worker.queue_filename_cleanup([selection])
        parent['date'] = '2026'
        bridge = Bridge(self.zot, self.cloud, self.worker.state, 'ROOT2345', self.croot.id)
        row = self.worker.state.read('documents')[item.id]
        with self.assertRaisesRegex(PilotError, 'citation'):
            bridge._apply_filename_cleanup(row, parent, item, 'ROOT2345', self.cloud.library(), lambda: None)
        self.assertTrue(path.is_file())
        self.worker.cancel_filename_cleanup(item.id)
        self.assertNotIn('filename_cleanup', self.worker.state.read('documents')[item.id])

    def test_missing_citation_uses_title_and_keeps_si_suffix(self):
        record = {'itemType': 'journalArticle', 'title': 'A <i>readable</i> title', 'publicationTitle': 'Radiology'}
        self.assertEqual(citation_filename(record, {}, supplement=True), ('A_readable_title_SI.pdf', 'Zotero title'))
        self.assertEqual(citation_filename({'title': 'Taylor_UnknownYear'}, {}), ('', 'Zotero title'))
