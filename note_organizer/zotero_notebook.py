"""Write a manual Word snapshot of the annotations Zotero selected."""
from pathlib import Path
import re
from tempfile import NamedTemporaryFile

from docx import Document

from .exporting import build_notebook, export_notebook
from .remarkable_cloud import CloudError, publish_staged
from .zotero_pilot import PilotError


ITEM_KEY = re.compile(r'[23456789A-NP-Z]{8}')
LIBRARY_PATH = re.compile(r'library|groups/[1-9][0-9]*')


def notebook_entries(sources):
    for source in sources:
        library, parent, attachment = (source[key] for key in ('library', 'paper', 'attachment'))
        if not LIBRARY_PATH.fullmatch(library) or not all(ITEM_KEY.fullmatch(key) for key in (parent, attachment)):
            raise PilotError('A Zotero source changed. Select the papers again before exporting.')
        title = source['title']
        filename = Path(source['filename']).name or 'PDF'
        path = f'{title}/{library}/{attachment}/{filename}'
        for annotation in source['annotations']:
            page = annotation['page']
            if type(page) is not int or page < 1:
                raise PilotError('An annotation is missing its PDF page. Reopen the PDF and try again.')
            image = annotation.get('image', '')
            kind = annotation['kind']
            comment = annotation.get('comment', '')
            if kind in {'image', 'ink'} and not image:
                comment = '\n'.join(filter(None, [comment, 'Image unavailable. Open this page in Zotero.']))
            yield {
                'path': path, 'page': page, 'pdf_label': annotation.get('label', ''),
                'rect': (0, 0), 'order': (annotation.get('order', ''),),
                'logical_paper_id': f'{library}/{parent}', 'display_title': title,
                'source_uri': f'zotero://open-pdf/{library}/items/{attachment}',
                'metadata': {'author': source.get('creator', ''), 'doi': source.get('doi', '')},
                'kind': 'drawing' if kind in {'image', 'ink'} else kind,
                'quote': annotation.get('text', ''), 'comment': comment, 'image': image,
            }


def generate_notebook(target, title, sources, replace=False):
    path = Path(target)
    if not path.is_absolute() or path.suffix.lower() != '.docx' or not path.parent.is_dir():
        raise PilotError('Choose a Word document in an existing folder.')
    entries = list(notebook_entries(sources))
    if not entries:
        raise PilotError('The selection has no PDF annotations to export.')
    try:
        if replace and path.is_file():
            _, backup = export_notebook(path, title, entries)
        else:
            with NamedTemporaryFile(suffix='.docx', dir=path.parent, delete=False) as stage:
                temporary = Path(stage.name)
            try:
                build_notebook(title, entries).save(temporary)
                Document(temporary)
                publish_staged(temporary, path, require_absent=True, item_label='notebook')
            finally:
                temporary.unlink(missing_ok=True)
            backup = ''
    except (CloudError, OSError) as error:
        raise PilotError(f'Couldn’t save the notebook to {path}. {error}') from error
    return {'annotations': len(entries), 'papers': len({entry['logical_paper_id'] for entry in entries}), 'backup': backup}
