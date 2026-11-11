"""Cloud notes have stable identities and folder membership, independent of PDFs.

The documents/<UUID>.pdf locator is a database identity, not a permanent file.
Only the primary paper is published in the user's folder. Tablet scene sources
remain immutable in remarkable_sources; temporary extraction files are removed.
"""
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace

import pymupdf as fitz

from .extraction import extract, scan_fingerprint
from .paper_registry import Registry
from .remarkable_cloud import CloudError, atomic_write
from .remarkable_pdf import annotated_pdf
from .storage import canonical, digest_file


STORAGE_VERSION = 2


def document_path(store, cloud_id):
    if not cloud_id or any(c in cloud_id for c in '/\\:\x00') or cloud_id in {'.', '..'}:
        raise CloudError('Invalid cloud document identity.')
    return store.path.parent / 'remarkable' / 'documents' / (cloud_id + '.pdf')


def primary_candidate(store, item, folder, old, original_digest):
    """Use an explicit relationship or an exact name; never guess by a title."""
    registry = Registry(store)
    sources = registry.sources()
    managed = {source['locator'] for source in sources if source['kind'] == 'local'
               and source['metadata'].get('role') == 'tablet_render'}
    managed.update(row.get('path') for row in store.remarkable_items().values()
                   if row.get('storage_version') != STORAGE_VERSION)
    candidates = set()
    if old.get('storage_version') == STORAGE_VERSION and old.get('main_path'):
        candidates.add(canonical(old['main_path']))
    for source in registry.local_for_cloud(item.id):
        if source['locator'] not in managed:
            candidates.add(source['locator'])
    name = item.name.casefold().removesuffix('.pdf')
    for path in Path(folder).iterdir():
        if path.is_file() and path.suffix.lower() == '.pdf' and canonical(path) not in managed:
            if path.stem.casefold() == name:
                candidates.add(canonical(path))
    # An exact registered baseline can also establish identity across a rename.
    candidates.update(source['locator'] for source in sources if source['kind'] == 'local'
                      and source['locator'] not in managed and source['digest'] == original_digest
                      and Path(source['locator']).parent == Path(canonical(folder)))
    candidates = sorted(path for path in candidates if Path(path).is_file())
    if len(candidates) > 1:
        raise CloudError('More than one PC original could match this tablet paper. Review the copies before combining notes.')
    return Path(candidates[0]) if candidates else None


def tablet_only_pdf(item, bundle, cancelled=lambda: False):
    """Exclude annotations inherited in an uploaded PC PDF from tablet notes.

    Original PDF bytes are preserved elsewhere. Removing native annotations
    happens only in this disposable extraction input, never in either original.
    Flattened page artwork is retained, as it belongs to the original page.
    """
    with fitz.open(stream=bundle[item.id + '.pdf'], filetype='pdf') as pdf:
        for page in pdf:
            if cancelled():
                raise InterruptedError('Tablet note extraction stopped')
            for annotation in list(page.annots() or []):
                page.delete_annot(annotation)
        baseline = pdf.tobytes(garbage=4, deflate=True)
    return annotated_pdf(item, {**bundle, item.id + '.pdf': baseline}, cancelled)


def save_cloud_notes(store, item, bundle, main_path, *, old=None, extractor=None, tablet_renderer=None,
                     cancelled=lambda: False):
    """Save a complete tablet-only scan; retain legacy document IDs/history."""
    old = old or {}
    anchor = canonical(document_path(store, item.id))
    main_path = canonical(main_path)
    projects = [project for project in store.projects()
                if Path(main_path).is_relative_to(Path(project['root']))]
    if not projects:
        raise CloudError('The PC destination must belong to a watched folder before saving tablet notes.')
    rendered = tablet_renderer(item, bundle) if tablet_renderer else tablet_only_pdf(item, bundle, cancelled)
    staged = []
    private = store.path.parent / 'remarkable'
    private.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='extract-', dir=private) as temp:
        path = Path(temp) / 'tablet.pdf'
        path.write_bytes(rendered)
        for project in projects:
            if cancelled():
                raise InterruptedError('Tablet note extraction stopped')
            settings = json.loads(project['settings'])
            digest, _, metadata, entries = (extractor(path, settings) if extractor else
                                            extract(path, settings, cancelled))
            metadata.update(managed_cloud_id=item.id, primary_path=main_path,
                            logical_folder=str(Path(main_path).parent), tablet_layers_only=True,
                            cloud_hash=item.hash)
            staged.append((project, digest, scan_fingerprint(settings), metadata, entries))
    Registry(store)
    legacy_path = old.get('legacy_path') or (old.get('path') if old.get('storage_version') != STORAGE_VERSION else '')
    legacy_ids = old.get('legacy_document_ids')
    if legacy_ids is None:
        with store.connect() as db:
            # The notes transaction can succeed just before publication of the
            # sync record is interrupted. Recover the same IDs from the anchor
            # as well, so a retry retains complete migration evidence.
            legacy_ids = [row[0] for row in db.execute(
                'SELECT id FROM documents WHERE path IN (?,?) ORDER BY id', (legacy_path or '', anchor))]
    # No notes change until every extraction succeeded. Document moves and
    # scans share one transaction so their identities and history commit together.
    from contextlib import contextmanager
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        @contextmanager
        def transaction():
            yield db
        transactional_registry = object.__new__(Registry)
        transactional_registry.store = SimpleNamespace(path=store.path, connect=transaction)
        staged_projects = {project['id'] for project, *_ in staged}
        saved_projects = {row[0] for row in db.execute(
            'SELECT project_id FROM documents WHERE path IN (?,?)',
            (canonical(legacy_path) if legacy_path else anchor, anchor))}
        if saved_projects - staged_projects:
            raise CloudError('A saved tablet note record belongs to a different watched folder. Reconcile its folder membership before updating notes.')
        if legacy_path and canonical(legacy_path) != anchor:
            for row in db.execute('SELECT id,project_id FROM documents WHERE path=?', (canonical(legacy_path),)).fetchall():
                occupied = db.execute('SELECT id FROM documents WHERE project_id=? AND path=?', (row['project_id'], anchor)).fetchone()
                if occupied and occupied['id'] != row['id']:
                    raise CloudError('Two saved tablet sources need review before their histories can be moved.')
                db.execute('UPDATE documents SET path=?,hidden=0,missing=0 WHERE id=?', (anchor, row['id']))
            source = db.execute("SELECT id FROM paper_sources WHERE kind='local' AND locator=?", (canonical(legacy_path),)).fetchone()
            if source:
                transactional_registry.relocate_local(source['id'], anchor)
        for project, digest, fingerprint, metadata, entries in staged:
            store.save_scan(project['id'], anchor, digest, item.hash, fingerprint, metadata, entries, connection=db)
        cloud = next(source for source in transactional_registry.sources() if source['kind'] == 'cloud' and source['locator'] == item.id)
        transactional_registry.register_local(anchor, digest=hashlib.sha256(rendered).hexdigest(), title=item.name,
            metadata={'cloud_id': item.id, 'role': 'tablet_render', 'page_mapping_identity': True,
                      'original_pdf_digest': cloud['digest'], 'tablet_layers_only': True}, paper_id=cloud['paper_id'])
    result = {'notes_migrated': True, 'legacy_document_ids': legacy_ids}
    if legacy_path:
        result.update(legacy_path=canonical(legacy_path), legacy_digest=old.get('legacy_digest', old.get('digest', '')),
                      legacy_cloud_hash=old.get('legacy_cloud_hash', old.get('hash', '')))
    return result


def adopt_paper(sync, item, bundle, folder, old):
    """Publish only a missing primary original, then attach verified tablet notes."""
    from .combined_pdf import render_combined_pdf
    from .remarkable_outbox import Outbox
    from .remarkable_sync import safe_name
    raw = bundle[item.id + '.pdf']
    raw_digest = hashlib.sha256(raw).hexdigest()
    if old.get('storage_version') != STORAGE_VERSION and old.get('path') and Path(old['path']).is_file():
        if digest_file(old['path']) != old.get('digest'):
            raise CloudError('The previous reading PDF has PC edits. Review and preserve those notes before combining this paper.')
    main = primary_candidate(sync.store, item, folder, old, raw_digest)
    checked_digest = digest_file(main) if main else raw_digest
    baseline = sync.private / 'baselines' / (raw_digest + '.pdf')
    validator = getattr(sync, 'combined_renderer', None)
    if validator:
        validator(main or baseline, item, bundle)
    else:
        render_combined_pdf(main or baseline, item, bundle, cancelled=sync.stop.is_set)
    if main is None:
        main = Path(folder) / (safe_name(item.name) + '.pdf')
        # Case-insensitive collisions must be reviewed on every platform.
        if any(path.name.casefold() == main.name.casefold() for path in Path(folder).iterdir()):
            raise CloudError('A different PC file occupies this paper name. Review it before importing.')
        atomic_write(main, raw, require_absent=True)
    main_digest = digest_file(main)
    if main_digest != checked_digest:
        raise CloudError('The PC paper changed during version verification. Retry when the file is stable.')
    # Keep the verified local version in the routed folder. Note extraction
    # below preserves independently identifiable tablet records.
    if Path(main).parent != Path(folder):
        raise CloudError('The linked PC paper is in a different folder. Reconcile the folder locations before combining notes.')
    if digest_file(main) != main_digest:
        raise CloudError('The PC paper changed while checking its tablet notes. Retry when the file is stable.')
    if not any(main.is_relative_to(Path(project['root'])) for project in sync.store.projects()):
        sync.store.add_project(main.parent)
    registry = Registry(sync.store)
    existing_local = next((source for source in registry.sources() if source['kind'] == 'local'
                           and source['locator'] == canonical(main)), None)
    if existing_local and any(source['kind'] == 'cloud' and source['locator'] != item.id
                              for source in registry.sources(existing_local['paper_id'])):
        raise CloudError('Another tablet document is already linked to this paper. Review those copies and their independent notes first.')
    cloud = Outbox(sync).register_committed(item, raw_digest,
        metadata={'cloud_hash': item.hash, 'parent': item.parent, 'path': old.get('cloud_path', '')})
    local = registry.register_local(main, digest=main_digest)
    if local['paper_id'] != cloud['paper_id']:
        registry.link_sources([local['id'], cloud['id']], paper_id=local['paper_id'])
    notes = save_cloud_notes(sync.store, item, bundle, main, old=old,
                             extractor=getattr(sync, 'extractor', None),
                             tablet_renderer=getattr(sync, 'tablet_renderer', None), cancelled=sync.stop.is_set)
    return {'storage_version': STORAGE_VERSION, 'path': canonical(main), 'digest': main_digest,
            'main_path': canonical(main), 'main_digest': main_digest, **notes}
