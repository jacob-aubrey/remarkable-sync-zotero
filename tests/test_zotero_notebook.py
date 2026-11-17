"""Verify the manual Word export and its source relationships."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from docx import Document

from note_organizer.zotero_notebook import generate_notebook
from note_organizer.zotero_pilot import PilotError


class ZoteroNotebookTests(unittest.TestCase):
    def test_snapshot_links_physical_pages_and_preserves_existing_work(self):
        source = {'library': 'groups/4321', 'paper': 'PAPER234', 'attachment': 'PDFABCDE', 'title': 'A & B &amp; C',
                  'filename': 'Example.pdf', 'annotations': [
                      {'kind': 'highlight', 'page': 3, 'label': 'ii', 'text': 'An exact quotation', 'comment': 'My comment'},
                      {'kind': 'ink', 'page': 5, 'comment': 'Handwriting'}]}
        with TemporaryDirectory() as directory:
            target = Path(directory) / 'Snapshot.docx'
            result = generate_notebook(target, 'Notes on a tag', [source])
            doc = Document(target)
            text = [paragraph.text for paragraph in doc.paragraphs]
            self.assertEqual(result, {'annotations': 2, 'papers': 1, 'backup': ''})
            self.assertIn('A & B &amp; C', text)
            self.assertIn('An exact quotation', text)
            self.assertIn('Note: My comment', text)
            self.assertIn('Note: Handwriting\nImage unavailable. Open this page in Zotero.', text)
            self.assertTrue(any(rel.is_external and rel.target_ref == 'zotero://open-pdf/groups/4321/items/PDFABCDE?page=3'
                                for rel in doc.part.rels.values()))
            doc.add_paragraph('My edits in Word')
            doc.save(target)
            with self.assertRaises(PilotError):
                generate_notebook(target, 'Another snapshot', [source])
            backup = generate_notebook(target, 'Another snapshot', [source], replace=True)['backup']
            self.assertIn('My edits in Word', [paragraph.text for paragraph in Document(backup).paragraphs])
