"""Private JSON transport for the Zotero plugin's managed background process.

The plugin owns setup and review. No terminal or Python installation is part of
the user workflow. New inputs remain gated by durable, content-bound approvals.
"""
from contextlib import contextmanager, nullcontext, redirect_stdout
import hashlib
import json
import logging
import mimetypes
import os
from pathlib import Path
import queue
import re
import shutil
import sys
from tempfile import TemporaryDirectory
import threading
import time
import traceback
import uuid

from .remarkable_cloud import CloudClient, CloudError, TokenVault, atomic_write
from .zotero_bridge import (Bridge, BridgeState, descendants, folder_blocked_papers, sync_collections,
                            sync_folder_scope, zotero_folder_inventory)
from .zotero_pilot import AuthorizationRequired, LocalZotero, PilotError, TransportInterrupted, stable_key
from .zotero_review import (ReviewStore, fingerprint, cloud_change_intent,
                            cloud_change_done)
from . import zotero_connections as connections


REVIEW_GUIDANCE_VERSION = 2
PAPER_REVIEW_KINDS = frozenset({'zotero', 'inbox', 'cloud'})
MANAGED_INBOX_SOURCE = 'This attachment is managed by reMarkable Sync. Check its connection in Linked papers before trying to file it.'


def review_guidance_ready(payload):
    """An empty findings list is current guidance, unlike a legacy issue list."""
    return (payload.get('guidance_version') == REVIEW_GUIDANCE_VERSION
            and payload.get('guidance_state') in {'ready', 'repair'}
            and isinstance(payload.get('findings'), list))


def review_policy_payload(row):
    """Keep legacy saved evidence readable even when its source is missing.

    Older queue records can lack optional path/details/match fields. Normalizing
    their shape must not prevent a different healthy paper from being checked,
    or invent another source/reference when the saved identity is unavailable.
    """
    payload = {**row['payload']}
    if row['kind'] in PAPER_REVIEW_KINDS:
        if not isinstance(payload.get('path'), str):
            payload['path'] = ''
        if not isinstance(payload.get('details'), dict):
            payload['details'] = {}
        for field in ('matches', 'possible_tablet_matches'):
            if not isinstance(payload.get(field), list):
                payload[field] = []
        if row['kind'] == 'zotero':
            payload['attachment'] = row['identity']
    return payload


TYPE_MAP = {"journal_article": "journalArticle", "preprint": "preprint", "conference_paper": "conferencePaper",
            "book_chapter": "bookSection", "book": "book", "thesis_dissertation": "thesis",
            "technical_report": "report", "white_paper": "report", "guideline_standard": "standard",
            "presentation_poster": "presentation", "letter_note": "letter", "patent": "patent",
            "patent_application": "patent", "web_or_other": "document", "unknown": "document"}
CITATION_VENUE = {'journalArticle': 'publicationTitle', 'magazineArticle': 'publicationTitle', 'newspaperArticle': 'publicationTitle',
                  'preprint': 'repository', 'conferencePaper': 'proceedingsTitle', 'bookSection': 'bookTitle',
                  'book': 'publisher', 'thesis': 'university', 'report': 'institution', 'presentation': 'meetingName',
                  'patent': 'issuingAuthority', 'standard': 'organization', 'webpage': 'websiteTitle', 'document': 'publisher'}
NATIVE_DOI_TYPES = {*TYPE_MAP.values(), 'magazineArticle', 'newspaperArticle', 'webpage'}
CITATION_EDITS = {'creators', 'date', 'venue', 'doi', 'patent_number'}


def file_sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def citation_signature(record):
    return fingerprint({key: (record or {}).get(key, "") for key in (
        'title', 'itemType', 'creators', 'date', 'DOI', 'extra', 'issueDate', 'filingDate', 'patentNumber', *set(CITATION_VENUE.values()))})


def creator_record(name, creator_type):
    """Use Zotero's structured personal-name fields; retain corporate names."""
    name = str(name).strip()
    if re.search(r'\b(?:university|institute|institution|consortium|collaboration|organization|organisation|committee|society|group|team|council|academy|foundation|laboratory|centre|center)\b', name, re.I):
        return {'creatorType': creator_type, 'name': name}
    if ',' in name:
        last, first = (part.strip() for part in name.split(',', 1))
    else:
        parts = name.rsplit(None, 1)
        first, last = parts if len(parts) == 2 else ('', name)
    return {'creatorType': creator_type, 'firstName': first, 'lastName': last}


def creator_role(item_type):
    return {'presentation': 'presenter', 'patent': 'inventor'}.get(item_type, 'author')


def creator_name(creator):
    return creator.get('name') or ' '.join(filter(None, [creator.get('firstName'), creator.get('lastName')]))


def clean_citation_edits(values):
    from .sorting.metadata_lookup import normalize_doi
    if values is None:
        return {}
    if not isinstance(values, dict) or any(key not in CITATION_EDITS or not isinstance(value, str) or len(value) > 4000
                                          for key, value in values.items()):
        raise PilotError('Review the citation fields before continuing.')
    cleaned = {key: value.strip() for key, value in values.items()}
    if any('\n' in value or '\r' in value for key, value in cleaned.items() if key != 'creators'):
        raise PilotError('Use a single line for each citation field other than creators.')
    if cleaned.get('doi'):
        doi = normalize_doi(cleaned['doi'])
        if not doi:
            raise PilotError('Enter a valid DOI or clear the DOI field.')
        cleaned['doi'] = doi
    return cleaned


def citation_patch(reference, edits, document_type):
    if not edits:
        return {}
    item_type = reference['itemType']
    fields = {'date': ('filingDate' if document_type == 'patent_application' else 'issueDate') if item_type == 'patent' else 'date',
              'venue': CITATION_VENUE.get(item_type), 'doi': 'DOI' if item_type in NATIVE_DOI_TYPES else None,
              'patent_number': 'patentNumber' if item_type == 'patent' else None}
    patch = {}
    extra = reference.get('extra', '')
    for name, value in edits.items():
        if name == 'creators':
            role = creator_role(item_type)
            primary = {creator_name(row): row for row in reference.get('creators', []) if row.get('creatorType') == role}
            others = [row for row in reference.get('creators', []) if row.get('creatorType') != role]
            creators = [line.strip() for line in value.splitlines() if line.strip()]
            patch['creators'] = [primary.get(name) or creator_record(name, role) for name in creators] + others
        elif field := fields[name]:
            patch[field] = value
            if name == 'doi':
                extra = re.sub(r'^DOI:[^\r\n]*(?:\r?\n|$)', '', extra, flags=re.I | re.M).strip()
        else:
            label = {'venue': 'Publisher', 'doi': 'DOI', 'patent_number': 'Patent Number'}[name]
            extra = re.sub(rf'^{re.escape(label)}:[^\r\n]*(?:\r?\n|$)', '', extra, flags=re.I | re.M).strip()
            if value:
                extra = '\n'.join(filter(None, [extra, f'{label}: {value}']))
    if extra != reference.get('extra', ''):
        patch['extra'] = extra
    return patch


def write_packet(transport, packet):
    # JSON escapes preserve every character without depending on the Windows
    # pipe's locale encoding (often cp1252, even in a frozen executable).
    transport.write(json.dumps(packet, ensure_ascii=True) + '\n')
    transport.flush()


class OperationTrace:
    """Durable, content-free diagnostics for one or more helper operations."""

    def __init__(self, path, *, max_bytes=2 * 1024 * 1024):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.operation = None
        self.max_bytes = max_bytes
        self.started = time.monotonic()
        self.active = None
        self.completed = None
        self.active_since = self.started

    @staticmethod
    def clean(value, key='', depth=0):
        if key.lower() in {'token', 'authorization', 'headers', 'body', 'payload', 'text', 'content', 'params'}:
            return '<redacted>'
        if depth > 4:
            return '<bounded>'
        if isinstance(value, dict):
            return {str(k)[:64]: OperationTrace.clean(v, str(k), depth + 1) for k, v in list(value.items())[:24]}
        if isinstance(value, (list, tuple)):
            return [OperationTrace.clean(v, depth=depth + 1) for v in value[:16]]
        if isinstance(value, str):
            value = re.sub(r'https?://\S+', '<url>', value)
            value = re.sub(r'(?i)(?:bearer\s+\S+|(?:token|password|api[_-]?key|secret)\s*[=:]\s*\S+)', '<redacted>', value)
            value = re.sub(r'[A-Za-z]:[\\/][^\s\"\n]+', '<path>', value)
            return value[:400]
        return value if isinstance(value, (int, float, bool, type(None))) else type(value).__name__

    def event(self, kind, **fields):
        # Diagnostics are best effort: disk/serialization/rotation errors must
        # never change a sync outcome. Never record request/annotation bodies.
        try:
            record = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      "elapsed_ms": round((time.monotonic() - self.started) * 1000),
                      "event": kind, **self.clean(fields)}
            if self.operation:
                record["operation"] = self.operation
            if kind.endswith('_started') or kind == 'stage':
                self.active = {"event": kind, **self.clean(fields)}
                self.active_since = time.monotonic()
            if kind.endswith('_finished'):
                self.completed = {"event": kind, **self.clean(fields)}
                if self.active and fields.get('request_id') == self.active.get('request_id'):
                    self.active = None
            line = (json.dumps(record, ensure_ascii=True) + "\n").encode("utf-8")
            with self.lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size + len(line) > self.max_bytes:
                    previous = self.path.with_name(self.path.name + '.1')
                    older = self.path.with_name(self.path.name + '.2')
                    if previous.exists():
                        previous.replace(older)
                    self.path.replace(previous)
                with self.path.open("ab") as stream:
                    stream.write(line)
                    stream.flush()
                    os.fsync(stream.fileno())
            return True
        except Exception:
            return False

    def start(self, method, correlation=None):
        self.operation = correlation if isinstance(correlation, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,80}', correlation) else uuid.uuid4().hex
        self.started = time.monotonic()
        self.active = self.completed = None
        self.event("operation_started", method=method)

    def finish(self, state="completed"):
        self.event("operation_finished", state=state)
        self.operation = None


def progress_links(worker):
    """Small live connection updates without polling the serialized API."""
    state = getattr(worker, 'state', None)
    if state is None:
        return None
    fields = ('name', 'parent', 'collection', 'revision', 'view_key', 'desktop_annotations_preserved')
    return [{'cloud': key, 'unavailable': bool(row.get('unavailable')), **{field: row[field] for field in fields if field in row}}
            for key, row in state.read('documents', {}).items()]


class Worker:
    def __init__(self, directory, port=23119, *, resources=None, zotero_factory=None, cloud_factory=None):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.resources = Path(resources or Path(__file__).resolve().parents[1])
        self.port, self.stop = int(port), threading.Event()
        self.pause_pending = threading.Event()
        path = self.directory / "config.json"
        self.config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        pause_marker = self.directory / 'pause-requested.json'
        if pause_marker.exists():
            self.config['sync_paused'] = True
            self.save()
            pause_marker.unlink()
        self._open_connection()
        self.zotero_factory, self.cloud_factory = zotero_factory, cloud_factory
        self.last = {'state': 'setup_required' if not self.config.get('tablet_root') else 'paused' if self.config.get('sync_paused') else 'ready',
                     'message': 'Sync paused. Click Sync now when ready.' if self.config.get('sync_paused') else ''}
        self.collections = []
        self.progress = lambda message: None
        self.trace = OperationTrace(self.directory / "sync-trace.jsonl")
        self.review.trace = self.state.trace = self.trace.event
        self.file_digests = {}
        from .zotero_bulk import BulkLibrary
        self.bulk = BulkLibrary(self)

    def _open_connection(self):
        self.connection_dir = connections.state_directory(self.directory, self.config)
        self.review = ReviewStore(self.connection_dir / 'review')
        self.state = BridgeState(self.connection_dir / 'bridge')
        if hasattr(self, 'trace'):
            self.review.trace = self.state.trace = self.trace.event

    def _connection_snapshot(self):
        return fingerprint({'config': self.config, 'values': connections.values(self.directory, self.config),
                            'review': file_sha(self.review.path)})

    def _begin_operation(self):
        if self.pause_pending.is_set():
            raise InterruptedError('Sync paused')
        self.stop.clear()
        if self.pause_pending.is_set():
            self.stop.set()
            raise InterruptedError('Sync paused')

    def pause_sync(self):
        self.stop.set()
        proposed = {**self.config, 'sync_paused': True}
        atomic_write(self.directory / 'config.json', json.dumps(proposed, ensure_ascii=False).encode())
        self.config = proposed
        (self.directory / 'pause-requested.json').unlink(missing_ok=True)
        self.pause_pending.clear()
        self.last = {'state': 'paused', 'message': 'Sync paused. Your papers and notes stay in place.'}
        return self.status()

    def save(self):
        atomic_write(self.directory / "config.json", json.dumps(self.config, ensure_ascii=False).encode())

    def zotero(self, *, interactive=False, ocr=False, reconnect=False):
        if self.zotero_factory:
            return self.zotero_factory()
        printed, handwriting = None, None
        if ocr and self.config.get("ocr", True):
            from .printed_ocr import PrintedOCR
            from .handwriting_ocr import HandwritingOCR
            printed = PrintedOCR(self.resources / "models/printed", self.directory / "printed-cache", cancelled=self.stop.is_set)
            handwriting = HandwritingOCR(self.resources / "models/handwriting", self.directory / "handwriting-cache", cancelled=self.stop.is_set)
        client = LocalZotero(self.port, printed_ocr=printed, handwriting_ocr=handwriting, trace=self.trace.event,
                             grant_store=TokenVault(self.directory / "zotero-grant.dpapi"), interactive=interactive)
        if not reconnect and self.config.get("server_id") and client.server_id != self.config["server_id"]:
            client.close()
            raise PilotError("A different Zotero library is open. Reconnect in Setup before using saved mappings.")
        return client

    def cloud(self):
        if self.cloud_factory:
            return self.cloud_factory()
        token = TokenVault(self.directory / "tablet-token.dpapi").load()
        if not token:
            raise PilotError("Pair reMarkable from Setup first.")
        return CloudClient(token, cache_dir=self.directory / "blobs", stop=self.stop, progress=self.progress, trace=self.trace.event)

    def runtime_health(self, optional=False):
        """Read-only packaged-runtime check; never connects or starts OCR."""
        import importlib
        required, extras = {}, {}
        for name in ('pyzotero', 'httpx2', 'httpcore2', 'pymupdf', 'rmscene', 'pypdf'):
            try:
                importlib.import_module(name)
                required[name] = {'available': True}
            except Exception as error:
                required[name] = {'available': False, 'missing_module': getattr(error, 'name', None), 'error_type': type(error).__name__}
        if optional:
            for name in ('rapidocr', 'onnxruntime', 'numpy'):
                try:
                    importlib.import_module(name)
                    extras[name] = {'available': True}
                except Exception as error:
                    extras[name] = {'available': False, 'missing_module': getattr(error, 'name', None), 'error_type': type(error).__name__}
        return {'required': required, 'optional_ocr': extras, 'sync_ready': all(row['available'] for row in required.values())}

    def status(self):
        from .sorting.core_logic import DOCUMENT_TYPE_LABELS
        from .zotero_bulk import intake_pdf_suggestions
        config = {k: v for k, v in self.config.items() if k != "server_id"}
        pending_reviews = self.review.rows(inbox_order=True)
        folder_blocks = self.state.read('folder_blocks', {})
        blocked_tablet = set(folder_blocks.get('tablet', []))
        pending_reviews = [row for row in pending_reviews if row['kind'] != 'cloud'
                           or row['payload'].get('tablet_parent') not in blocked_tablet]
        suggestions = intake_pdf_suggestions(pending_reviews) if config.get('inbox') else {}
        for row in pending_reviews:
            if row['id'] in suggestions:
                row['intake_pdf'] = suggestions[row['id']]
        paper_reviews = [row for row in pending_reviews if row['status'] == 'pending' and row['kind'] in PAPER_REVIEW_KINDS]
        review_guidance = {'version': REVIEW_GUIDANCE_VERSION,
            'needs_upgrade': [row['id'] for row in paper_reviews if not review_guidance_ready(row['payload'])],
            'ready': sum(review_guidance_ready(row['payload']) and row['payload']['guidance_state'] == 'ready' for row in paper_reviews),
            'repair': sum(review_guidance_ready(row['payload']) and row['payload']['guidance_state'] == 'repair' for row in paper_reviews)}
        documents = self.state.read('documents', {})
        available = {key: row for key, row in documents.items() if not row.get('unavailable')}
        linked_sources = {row.get('source_key') for row in documents.values()}
        waiting = {row['approval'].get('source') or row['kind'] + ':' + row['identity']
                   for row in self.review.rows(pending_only=False)
                   if row['status'] == 'approved' and row['kind'] in {'cloud', 'zotero', 'inbox'}
                   and (row['kind'] == 'cloud' or Path(row['payload'].get('path') or row['approval'].get('filename')
                                                     or row['payload'].get('filename', '')).suffix.lower() == '.pdf')
                   and row['identity'] not in documents and row['approval'].get('source') not in linked_sources}
        staged = {identity: row['source_key'] for identity, row in available.items()
                  if row.get('origin') == 'zotero' and row.get('uploaded') is False and row.get('source_key')}
        if staged:
            suspended = self.review.suspended_documents()
            waiting.update(source for identity, source in staged.items() if identity not in suspended)
        return {"config": config, "status": self.last, "last_sync_at": self.state.read("last_sync_at"),
                "reviews": pending_reviews, "review_guidance": review_guidance,
                "deletions": self.review.deletions(), "local_deletions": self.review.local_deletions(), "types": DOCUMENT_TYPE_LABELS,
                "collections": self.collections, "mappings": self.state.read("folders", {}),
                'folder_issues': folder_blocks.get('issues', []), 'blocked_collections': folder_blocks.get('zotero', []),
                "cleanup_pending": len(self.review.cleanups()), "bulk": self.bulk.read(),
                'filing_summary': {'approved_waiting_sync': len(waiting), 'linked': len(available),
                                   'pdfs_received': sum(bool(row.get('revision')) for row in available.values())},
                "links": [{"cloud": key, **row, **self.review.link_control(key)} for key, row in self.state.read("documents", {}).items()],
                'connection': {'linked_papers': len(available),
                  'previous': [{**{key: row.get(key, '') for key in ('zotero_root', 'tablet_root', 'zotero_path', 'tablet_path')},
                                'linked_papers': len(connections.values(self.directory, row)['documents'])}
                               for row in self.config.get('connection_history', [])]}}

    def connect(self):
        client = self.zotero(interactive=True, reconnect=True)
        try:
            client.authorize()
            collections = client.collections()
            changed = bool(self.config.get('server_id') and self.config['server_id'] != client.server_id)
            proposed = {**self.config, 'server_id': client.server_id}
            if changed:
                # Explicit Connect authorizes a new binding, never reuse old
                # item keys or approvals in another Zotero instance.
                proposed = connections.switch(self.directory, self.config, {**proposed,
                    'zotero_root': '', 'tablet_root': '', 'default_folder': '',
                    'zotero_path': '', 'tablet_path': ''})
                plan = self.bulk.read()
                if plan:
                    # Invalidate before committing the binding. A failed save
                    # cannot leave an old import authorization active elsewhere.
                    self.bulk.save({**plan, 'invalidated': 'The Zotero instance changed. Scan the folder again before importing.'})
            atomic_write(self.directory / 'config.json', json.dumps(proposed, ensure_ascii=False).encode())
            self.config, self.collections = proposed, collections
            if changed:
                self._open_connection()
                self.last = {'state': 'setup_required', 'message': 'Connected to the new Zotero instance. Previous mappings were retained separately. Choose folders in Setup.'}
            return {**self.status(), 'connected': True, 'server_changed': changed}
        finally:
            client.close()

    def _tag_plans(self):
        path = self.directory / 'paper-tag-proposals.json'
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}

    def _save_tag_plans(self, plans):
        atomic_write(self.directory / 'paper-tag-proposals.json', json.dumps(plans, ensure_ascii=False).encode('utf-8'))

    def _paper_target(self, client, identity='', key=''):
        if bool(identity) == bool(key):
            raise PilotError('Choose one paper for tag suggestions.')
        if identity:
            row = self.review.row(identity)
            if not row or row['kind'] not in {'cloud', 'zotero', 'inbox'}:
                raise PilotError('Choose a current paper review.')
            path = client.file_path(row['identity']) if row['kind'] == 'zotero' else Path(row['payload']['path'])
            parent = row['payload'].get('parent', '')
            digest = file_sha(path)
            if digest != row['digest']:
                raise PilotError('The reviewed paper changed. Refresh it before checking tags.')
            target = 'review:' + identity
        else:
            item = client.get('item', key)
            if not item or item.get('deleted') or item.get('itemType') != 'attachment' or not item.get('parentItem'):
                raise PilotError('Choose a stored PDF with a parent reference.')
            path, parent = client.file_path(key), item['parentItem']
            digest, target = file_sha(path), 'item:' + key
        record = client.get('item', parent) if parent else None
        if parent and (not record or record.get('deleted')):
            raise PilotError('The paper reference is unavailable.')
        return {'target': target, 'identity': identity, 'key': key, 'path': str(path), 'digest': digest,
                'parent': parent, 'tags': (record or {}).get('tags', [])}

    def paper_tag_candidates(self, query=''):
        client = self.zotero()
        try:
            catalog = client.review_action('catalog')
            result = []
            for attachment in catalog['attachments']:
                parent = catalog['parents'].get(attachment['data'].get('parentItem'))
                if parent and Path(attachment['path']).suffix.lower() == '.pdf' and str(query).casefold() in parent.get('title', '').casefold():
                    result.append({'key': attachment['data']['key'], 'title': parent.get('title', ''), 'filename': Path(attachment['path']).name})
            return {'candidates': sorted(result, key=lambda row: row['title'].casefold())[:50], 'total': len(result)}
        finally:
            client.close()

    def suggest_paper_tags(self, identity='', key='', quiet=False):
        from .zotero_tagging import analyse, VERSION
        client = self.zotero()
        try:
            source = self._paper_target(client, identity, key)
            plans = self._tag_plans()
            previous = plans.get(source['target'], {})
            if (previous.get('analysis_version') == VERSION
                    and all(previous.get(field) == source[field] for field in ('path', 'digest', 'parent', 'tags'))
                    and isinstance(previous.get('keywords'), list) and isinstance(previous.get('suggestions'), list)):
                return {'proposal': previous, 'cached': True}
            catalog = client.review_action('catalog')
            vocabulary = {tag['tag'] for parent in catalog['parents'].values() for tag in parent.get('tags', [])}
            if not quiet:
                self.progress('Reading stated keywords and preparing local tag suggestions…')
            from .zotero_maintenance import now
            proposal = {**source, **analyse(source['path'], vocabulary, [tag['tag'] for tag in source['tags']]),
                        'analysis_version': VERSION, 'checked_at': now()}
            offered = {row['tag'] for row in proposal['suggestions']}
            proposal['selected'] = [tag for tag in previous.get('selected', []) if tag in offered
                                    and previous.get('digest') == source['digest']]
            proposal['token'] = fingerprint(proposal)
            plans[source['target']] = proposal
            self._save_tag_plans(plans)
            return {'proposal': proposal}
        finally:
            client.close()

    def save_paper_tags(self, target, token, selected):
        from .zotero_tags import names
        from .zotero_tagging import append_tags
        selected = names(selected)
        plans = self._tag_plans()
        proposal = plans.get(target)
        if not proposal or proposal['token'] != token or not set(selected) <= {row['tag'] for row in proposal['suggestions']}:
            raise PilotError('The tag suggestions changed. Check this paper again.')
        client = self.zotero()
        try:
            source = self._paper_target(client, proposal['identity'], proposal['key'])
            if any(source[field] != proposal[field] for field in ('path', 'digest', 'parent', 'tags')):
                raise PilotError('The paper or its tags changed. Check its suggestions again before saving.')
            if not proposal['identity']:
                record = client.get('item', source['parent'])
                updated = append_tags(record.get('tags', []), [row['tag'] for row in proposal['keywords']], selected)
                if updated != record.get('tags', []):
                    record['tags'] = updated
                    client.authorize(); client.update(record)
            proposal['selected'] = selected
            proposal['saved'] = True
            plans[target] = proposal
            self._save_tag_plans(plans)
            return {'saved': True, 'deferred_until_filing': bool(proposal['identity'])}
        finally:
            client.close()

    def _keyword_cleanup_rows(self, record, reports):
        from .zotero_tagging import cleanup_candidates, fragment_reason, keyword_without_markup, normal
        candidates = {}
        reliable = set()
        for report in reports:
            flagged = cleanup_candidates(report)
            candidates.update(flagged)
            reliable.update(normal(row['tag']) for row in report.get('keywords', [])
                            if normal(row['tag']) not in flagged)
        automatic = [tag['tag'] for tag in record.get('tags', []) if tag.get('type') == 1]
        numbered_fragments = any(re.match(r'^\d+\s', tag) and fragment_reason(tag) for tag in automatic)
        rows = []
        for tag in automatic:
            key = normal(tag)
            candidate = candidates.get(key)
            if candidate and key not in reliable and candidate['tag'].casefold() == tag.casefold():
                rows.append({**candidate, 'tag': tag})
                continue
            replacement = keyword_without_markup(tag)
            if replacement:
                rows.append({'tag': tag, 'replacement': replacement, 'reason': 'Remove the quotation marks or bullet from the keyword.',
                             'source': 'Existing automatic Zotero tag', 'evidence': tag})
                continue
            if key in reliable:
                continue
            reason = fragment_reason(tag)
            if not reason and numbered_fragments and re.match(r'^\d{2,3}\s+\w+\s+\w+', tag):
                reason = 'This label starts with a possible reference number. Other numbered fragments appear on this paper.'
            if reason:
                rows.append({'tag': tag, 'reason': reason, 'source': 'Existing automatic Zotero tag', 'evidence': tag})
        return sorted(rows, key=lambda row: row['tag'].casefold())

    def _keyword_cleanup_preview(self, record, reports):
        rows = self._keyword_cleanup_rows(record, reports)
        tags = sorted(record.get('tags', []), key=lambda tag: tag['tag'])
        return {'parent': record['key'], 'title': record.get('title', ''), 'tags': rows,
                'token': fingerprint([self.config.get('tablet_root'), record['key'], record.get('version'),
                                      tags, rows])}

    def preview_keyword_cleanup(self):
        reports = {}
        for link in self.state.read('documents', {}).values():
            if link.get('parent') and link.get('keyword_check'):
                reports.setdefault(link['parent'], []).append(link['keyword_check'])
        client = self.zotero()
        try:
            parents = client.review_action('catalog', {'parents_only': True})['parents']
            rows = []
            for parent, snapshot in parents.items():
                checks = reports.get(parent, ())
                if not self._keyword_cleanup_rows(snapshot, checks):
                    continue
                # The native catalog and item API serialize versions and tag
                # defaults differently. Sign the same item view used to save.
                record = client.get('item', parent)
                if record and not record.get('deleted'):
                    preview = self._keyword_cleanup_preview(record, checks)
                    if preview['tags']:
                        rows.append(preview)
            return {'papers': sorted(rows, key=lambda row: row['title'].casefold())}
        finally:
            client.close()

    def remove_reviewed_keywords(self, parent, token, selected):
        from .zotero_tags import names
        selected = set(names(selected))
        if not selected:
            raise PilotError('Choose at least one tag to remove.')
        reports = [link['keyword_check'] for link in self.state.read('documents', {}).values()
                   if link.get('parent') == parent and link.get('keyword_check')]
        client = self.zotero()
        try:
            record = client.get('item', parent)
            if not record or record.get('deleted'):
                raise PilotError('This paper is unavailable. Check the automatic tags again.')
            preview = self._keyword_cleanup_preview(record, reports)
            if preview['token'] != token or not selected <= {
                    row['tag'] for row in preview['tags'] if not (row.get('replacement') or row.get('replacements'))}:
                raise PilotError('The paper or its tags changed. Check the automatic tags again before removing any.')
            record['tags'] = [tag for tag in record['tags'] if tag['tag'] not in selected or tag.get('type') != 1]
            client.authorize()
            client.update(record)
            return {'removed': len(selected)}
        finally:
            client.close()

    def apply_document_keywords(self, client, parent, path, identity='', digest=''):
        from .zotero_tagging import document_keywords, append_tags
        record = client.get('item', parent)
        if not record or record.get('deleted'):
            raise PilotError('The paper reference changed before its keyword tags could be saved.')
        existing = record.get('tags', [])
        keyword_path = path
        for link in self.state.read('documents', {}).values():
            if link.get('parent') != parent:
                continue
            if not any(key and client.file_path(key) == path for key in (link.get('view_key'), link.get('source_key'))):
                continue
            baseline = link.get('baseline', '')
            original = self.state.directory / 'sources' / 'remarkable' / 'baselines' / (baseline + '.pdf')
            if original.is_file() and file_sha(original) == baseline:
                keyword_path = original
                break
        keywords = document_keywords(keyword_path, [row['tag'] for row in existing])
        if digest and file_sha(path) != digest:
            raise PilotError('The paper changed while reading its keywords. Its existing tags were retained.')
        plan = self._tag_plans().get('review:' + identity, {}) if identity else {}
        accepted = plan.get('selected', []) if plan.get('saved') and plan.get('digest') == file_sha(path) else []
        updated = append_tags(existing, [row['tag'] for row in keywords], accepted)
        if updated != existing:
            record['tags'] = updated
            client.authorize(); client.update(record)
        return record

    def pair(self, code):
        self._begin_operation()
        client = CloudClient(stop=self.stop)
        token = client.pair(code)
        TokenVault(self.directory / "tablet-token.dpapi").save(token)
        return {"paired": True}

    def folders(self):
        self._begin_operation()
        client = self.zotero()
        try:
            self.collections = client.collections()
            return {"collections": self.collections, "folders": [
                {"id": item.id, "name": item.name, "parent": item.parent}
                for item in self.cloud().library() if not item.deleted and item.kind == "CollectionType"]}
        finally:
            client.close()

    def create_destination(self, kind, parent='', name='', tablet_name=''):
        """Create only the explicitly requested destination, with stable retry IDs."""
        if kind not in {'zotero', 'tablet', 'paired'}:
            raise PilotError('Choose a Zotero collection, a tablet folder, or a linked pair.')
        def checked(value):
            if not isinstance(value, str) or not value.strip() or len(value.strip()) > 200 or re.search(r'[\x00-\x1f/\\]', value):
                raise PilotError('Enter a name of 1–200 characters without slashes or control characters.')
            return value.strip()
        name = checked(name)
        tablet_name = checked(tablet_name or name) if kind == 'paired' else name
        if not isinstance(parent, str):
            raise PilotError('Choose a parent folder or collection.')
        client = self.zotero() if kind != 'tablet' else None
        cloud = None
        items, collections = [], list(self.collections)
        try:
            if kind != 'zotero':
                cloud = self.cloud()
            if client:
                collections = client.collections()
                by_key = {row['key']: row for row in collections if not row.get('deleted')}
                if parent and parent not in by_key:
                    raise PilotError('The parent Zotero collection is unavailable. Refresh the chooser.')
            if cloud:
                items = [item for item in cloud.library() if item.kind == 'CollectionType' and not item.deleted]
                by_id = {item.id: item for item in items}
            mappings = self.state.read('folders', {})
            if kind == 'paired':
                zroot, croot = self.config.get('zotero_root'), self.config.get('tablet_root')
                if not zroot or not croot:
                    raise PilotError('Save a Zotero/reMarkable connection before creating linked folders.')
                mappings.setdefault(zroot, {'cloud': croot, 'root': True})
                excluded = connections.exclusions(self.directory, self.config)
                zscope = descendants(collections, zroot, lambda row: row['key'], lambda row: row.get('parentCollection'))
                cscope = descendants(items, croot, lambda row: row.id, lambda row: row.parent)
                zscope = connections.filtered_scope(zscope, excluded['zotero'], lambda row: row['key'], lambda row: row.get('parentCollection'), zroot)
                cscope = connections.filtered_scope(cscope, excluded['tablet'], lambda row: row.id, lambda row: row.parent, croot)
                if parent not in mappings or parent not in zscope or mappings[parent]['cloud'] not in cscope:
                    raise PilotError('Choose an available linked parent inside the current connection.')
                cparent = mappings[parent]['cloud']
            else:
                cparent = parent
            if cloud and cparent and cparent not in by_id:
                raise PilotError('The parent tablet folder is unavailable. Refresh the chooser.')
            zmatch = [row for row in collections if not row.get('deleted') and (row.get('parentCollection') or '') == parent and row['name'] == name] if client else []
            cmatch = [item for item in items if (item.parent or '') == cparent and item.name == tablet_name] if cloud else []
            if len(zmatch) > 1 or len(cmatch) > 1:
                raise PilotError('Several siblings already have that name. Resolve the duplicate names before creating a destination.')
            if kind == 'paired':
                if zmatch and zmatch[0]['key'] in mappings and (not cmatch or mappings[zmatch[0]['key']]['cloud'] != cmatch[0].id):
                    raise PilotError('This collection already links to another tablet folder. Choose a different name or inspect its connection.')
                if cmatch and any(link['cloud'] == cmatch[0].id and key != (zmatch[0]['key'] if zmatch else None) for key, link in mappings.items()):
                    raise PilotError('This tablet folder already links to another Zotero collection. Choose a different name.')
            journal_path = self.connection_dir / 'destination-creations.json'
            journal = json.loads(journal_path.read_text(encoding='utf-8')) if journal_path.exists() else {}
            operation = fingerprint([self.config.get('server_id', ''), kind, parent, name, tablet_name])
            saved = journal.setdefault(operation, {'zotero': (zmatch[0]['key'] if zmatch else stable_key('destination:' + operation, 'collection', parent)),
                                                  'tablet': cmatch[0].id if cmatch else str(uuid.uuid4())})
            def persist():
                atomic_write(journal_path, json.dumps(journal, ensure_ascii=False).encode('utf-8'))
            persist()  # Identities survive a lost response on either service.
            created = {'kind': kind, 'key': saved['tablet'] if kind == 'tablet' else saved['zotero'],
                       'name': name, 'tablet_name': tablet_name, 'reused': bool((not client or zmatch) and (not cloud or cmatch)), 'complete': False,
                       'zotero': saved['zotero'] if client else '', 'tablet': saved['tablet'] if cloud else '',
                       'locations': {'zotero': 'ready' if zmatch else 'pending' if client else 'not_requested',
                                     'tablet': 'ready' if cmatch else 'pending' if cloud else 'not_requested'}}
            try:
                if client:
                    if zmatch:
                        record = zmatch[0]
                    else:
                        record = client.review_action('create_collection', {'key': saved['zotero'], 'parent': parent, 'name': name})['collection']
                    saved['zotero'] = record['key']; created.update(key=record['key'], zotero=record['key'])
                    created['locations']['zotero'] = 'ready'
                    collections = [row for row in collections if row['key'] != record['key']] + [record]
                    self.collections = collections
                    persist()
                if cloud:
                    folder = cmatch[0] if cmatch else cloud.create_folder(tablet_name, cparent, saved['tablet'], self.config.get('tablet_root', '') if kind == 'paired' else '')
                    saved['tablet'] = folder.id; created['tablet'] = folder.id
                    created['locations']['tablet'] = 'ready'
                    if kind == 'tablet':
                        created['key'] = folder.id
                    items = [item for item in items if item.id != folder.id] + [folder]
                    persist()
                if kind == 'paired':
                    mappings[saved['zotero']] = {'cloud': saved['tablet'], 'name': name, 'desktop_name': name, 'tablet_name': tablet_name,
                                                 'zparent': parent, 'cparent': cparent}
                    self.state.write('folders', mappings)
                created['complete'] = True
            except (OSError, ValueError, PilotError, CloudError) as error:
                created['error'] = 'Creation is incomplete. Existing destinations were kept. Retry to finish without duplicates. ' + str(error)
            return {**self.status(), 'created_destination': created, 'destination_catalog': {'collections': collections,
                    'folders': [{'id': item.id, 'name': item.name, 'parent': item.parent} for item in items]}}
        finally:
            if client:
                client.close()

    def _setup_plan(self, values):
        self._begin_operation()
        from .sorting.core_logic import DOCUMENT_TYPES, validate_filename_template
        allowed = {"zotero_root", "tablet_root", "default_folder", "inbox", "templates", "automatic", "ocr"}
        if set(values) - allowed:
            raise PilotError("Unsupported setup field.")
        for kind, template in values.get("templates", {}).items():
            if kind not in DOCUMENT_TYPES:
                raise PilotError("Choose a supported document type.")
            if template:
                validate_filename_template(template)
        client = self.zotero()
        try:
            proposed = {**self.config, **values, 'sync_paused': True}
            collections = client.collections()
            zscope = descendants(collections, proposed["zotero_root"], lambda r: r["key"], lambda r: r.get("parentCollection"))
            folders = [r for r in self.cloud().library() if r.kind == 'CollectionType']
            cscope = descendants(folders, proposed['tablet_root'], lambda r: r.id, lambda r: r.parent,
                                 unavailable={r.id for r in folders if r.deleted})
            if proposed.get("default_folder", proposed["tablet_root"]) not in cscope:
                raise PilotError("Choose a default tablet folder inside the selected scope.")
            inbox = str(proposed.get("inbox", "")).strip()
            if inbox:
                inbox_path = Path(inbox).resolve(strict=True)
                if not inbox_path.is_dir():
                    raise PilotError("Choose an existing ToSort folder.")
                catalog = client.review_action("catalog")
                # Do not scan Zotero's own storage as an intake folder.
                data = Path(catalog['data_directory']).resolve() if catalog.get('data_directory') else None
                if (data and (inbox_path.is_relative_to(data) or data.is_relative_to(inbox_path))) or any(Path(a["path"]).resolve().is_relative_to(inbox_path) for a in catalog["attachments"]):
                    raise PilotError("Choose a separate download folder, outside Zotero's managed storage.")
                proposed["inbox"] = str(inbox_path)
            def path(rows, identity, key, parent):
                by = {key(row): row for row in rows}
                names = []
                seen = set()
                while identity:
                    if identity in seen or identity not in by:
                        raise PilotError('The folder list changed. Refresh folders before saving.')
                    seen.add(identity)
                    row = by[identity]
                    names.insert(0, row['name'] if isinstance(row, dict) else row.name)
                    identity = parent(row)
                return ' / '.join(names)
            proposed.update(zotero_path=path(collections, proposed['zotero_root'], lambda r: r['key'], lambda r: r.get('parentCollection')),
                            tablet_path=path(folders, proposed['tablet_root'], lambda r: r.id, lambda r: r.parent), sync_paused=True)
            changed = not connections.same_roots(self.config, proposed)
            if changed:
                proposed = connections.switch(self.directory, self.config, proposed)
            excluded = connections.exclusions(self.directory, proposed)
            zscope = connections.filtered_scope(zscope, excluded['zotero'], lambda r: r['key'], lambda r: r.get('parentCollection'), proposed['zotero_root'])
            cscope = connections.filtered_scope(cscope, excluded['tablet'], lambda r: r.id, lambda r: r.parent, proposed['tablet_root'])
            if proposed.get('default_folder', proposed['tablet_root']) not in cscope:
                raise PilotError('That destination belongs to a paused folder connection. Choose another folder or use it as the new root.')
            retained = len(excluded['documents'])
            previous = next((row for row in self.config.get('connection_history', []) if connections.same_roots(row, proposed)), None)
            plan = {'proposed': proposed, 'changed': changed, 'old_tablet_path': self.config.get('tablet_path', 'Current reMarkable folder'),
                    'linked_papers': len(self.state.read('documents', {})), 'pending_reviews': len(self.review.rows()),
                    'paused_papers': retained, 'restored_papers': len(connections.values(self.directory, previous)['documents']) if previous else 0,
                    'zotero_folders': [row['name'] for row, depth in zscope.values() if depth],
                    'tablet_folders': [row.name for row, depth in cscope.values() if depth],
                    'excluded_zotero_folders': [r['name'] for r in collections if r['key'] in excluded['zotero']],
                    'excluded_tablet_folders': [r.name for r in folders if r.id in excluded['tablet']]}
            # UUIDs/timestamps used to stage a new connection are not part of the
            # confirmation. A fresh preview revalidates current metadata and state.
            stable = {k: v for k, v in proposed.items() if k not in {'connection_id', 'connection_history'}}
            plan['token'] = fingerprint({'state': self._connection_snapshot(), 'setup': stable, 'collections': collections,
                                         'folders': [(r.id, r.hash, r.name, r.parent) for r in folders],
                                         'exclusions': {k: sorted(v) for k, v in excluded.items()}})
            self.collections = collections
            return plan
        finally:
            client.close()

    def preview_connection(self, **values):
        self.pause_sync()
        return self._setup_plan(values)

    def configure(self, confirmation=None, **values):
        local_keys = {'inbox', 'templates', 'automatic', 'ocr'}
        roots = {'zotero_root', 'tablet_root', 'default_folder'}
        if (set(values) <= local_keys | roots
                and all(values.get(key, self.config.get(key, '')) == self.config.get(key, '') for key in roots)):
            # Naming, OCR and intake preferences are local. An unavailable cloud
            # catalog must not prevent saving them for the next connection.
            from .sorting.core_logic import DOCUMENT_TYPES, validate_filename_template
            for kind, template in values.get('templates', {}).items():
                if kind not in DOCUMENT_TYPES:
                    raise PilotError('Choose a supported document type.')
                if template:
                    validate_filename_template(template)
            proposed = {**self.config, **values}
            inbox = str(proposed.get('inbox', '')).strip()
            if inbox:
                path = Path(inbox).resolve(strict=True)
                if not path.is_dir():
                    raise PilotError('Choose an existing ToSort folder.')
                if inbox != self.config.get('inbox', ''):
                    client = self.zotero()
                    try:
                        catalog = client.review_action('catalog')
                        data = Path(catalog['data_directory']).resolve() if catalog.get('data_directory') else None
                        if (data and (path.is_relative_to(data) or data.is_relative_to(path))) or any(Path(a['path']).resolve().is_relative_to(path) for a in catalog['attachments']):
                            raise PilotError("Choose a separate download folder, outside Zotero's managed storage.")
                    finally:
                        client.close()
                proposed['inbox'] = str(path)
            atomic_write(self.directory / 'config.json', json.dumps(proposed, ensure_ascii=False).encode())
            self.config = proposed
            self.last = {'state': 'paused' if proposed.get('sync_paused') else 'ready', 'message': 'Preferences saved.'}
            return self.status()
        plan = self._setup_plan(values)
        if plan['changed'] and (self.config.get('tablet_root') or self.config.get('connection_history')):
            if confirmation != plan['token']:
                raise PilotError('Review the folder change again before saving. No folders or papers were changed.')
        proposed = plan['proposed']
        if not self.config.get('tablet_root') and not self.config.get('connection_history') and values.get('automatic'):
            proposed = {**proposed, 'automatic': True, 'sync_paused': False}
        directory = connections.state_directory(self.directory, proposed)
        review, state = ReviewStore(directory / 'review'), BridgeState(directory / 'bridge')
        atomic_write(self.directory / 'config.json', json.dumps(proposed, ensure_ascii=False).encode())
        self.config, self.connection_dir, self.review, self.state = proposed, directory, review, state
        self.last = ({'state': 'paused', 'message': 'Setup saved. Check your folders, then click Sync now when ready.'}
                     if proposed.get('sync_paused') else {'state': 'ready', 'message': 'Setup saved. Automatic sync is on.'})
        return self.status()

    def preview_disconnect(self):
        self.pause_sync()
        return {'token': self._connection_snapshot(), 'linked_papers': len(self.state.read('documents', {})),
                'pending_reviews': len(self.review.rows())}

    def disconnect_folder(self, confirmation):
        if confirmation != self._connection_snapshot():
            raise PilotError('The connection changed. Review the disconnect again. Your files were retained.')
        proposed = connections.switch(self.directory, self.config, {**self.config, 'tablet_root': '', 'default_folder': '', 'tablet_path': ''})
        directory = connections.state_directory(self.directory, proposed)
        review, state = ReviewStore(directory / 'review'), BridgeState(directory / 'bridge')
        atomic_write(self.directory / 'config.json', json.dumps(proposed, ensure_ascii=False).encode())
        self.config, self.connection_dir, self.review, self.state = proposed, directory, review, state
        self.last = {'state': 'setup_required', 'message': 'Folder disconnected. Your papers and notes stay in Zotero and reMarkable.'}
        return self.status()

    def _naming_signature(self):
        return fingerprint({'revision': 4, 'templates': self.config.get('templates', {})})

    def _proposal(self, path, metadata=None):
        from .sorting.core_logic import get_basic_document_details, build_proposed_filename, sanitize_filename_part, validate_document_filename
        from .zotero_filenames import PLACEHOLDER, title_filename
        details = get_basic_document_details(path)
        extracted_title = details.get('title', '')
        metadata = metadata or {}
        if metadata.get("title"):
            details["title"] = metadata["title"]
        reverse = {v: k for k, v in TYPE_MAP.items()}
        if metadata.get("itemType") in reverse:
            details["document_type"] = reverse[metadata["itemType"]]
        creators = metadata.get("creators", [])
        if creators:
            details["creators"] = [creator_name(c) for c in creators]
            details["primary_creator"] = details["creators"][0]
        item_type = metadata.get('itemType', '')
        date = metadata.get('date') or metadata.get('issueDate') or metadata.get('filingDate') or ''
        if date:
            from .sorting.core_logic import _normalize_year
            year = _normalize_year(date)
            if year != 'Unknown':
                details['year'] = year
        venue = metadata.get(CITATION_VENUE.get(item_type, ''), '') or metadata.get('publicationTitle', '')
        if venue:
            details['venue_or_publisher'] = venue
            if item_type in {'journalArticle', 'preprint'} or metadata.get('publicationTitle'):
                details['journal'] = venue
        if metadata.get('patentNumber'):
            details['patent_number'] = metadata['patentNumber']
        doi_extra = re.search(r'^DOI:\s*([^\r\n]+)', metadata.get('extra', ''), flags=re.I | re.M)
        if doi := metadata.get('DOI') or (doi_extra[1].strip() if doi_extra else ''):
            details['identifier'] = {**(details.get('identifier') or {}), 'doi': doi}
        # Extraction warnings about missing fields stop being true when the
        # authoritative Zotero citation supplies them.
        from .sorting.core_logic import _missing_citation_fields, _normalize_year
        warnings = []
        missing = _missing_citation_fields(details)
        for warning in details.get('review_reasons', []):
            if warning == 'No reliable author or responsible organization was found.' and creators:
                continue
            if warning == 'No reliable publication or release year was found.' and _normalize_year(details.get('year')) != 'Unknown':
                continue
            if warning.startswith('Missing citation fields:'):
                if missing:
                    warnings.append('Missing citation fields: ' + ', '.join(missing) + '. Review and complete the proposed name.')
                continue
            warnings.append(warning)
        details.update(review_reasons=warnings, needs_review=bool(warnings), missing_citation_fields=missing)
        template = self.config.get("templates", {}).get(details["document_type"], "")
        name = build_proposed_filename(details, path.suffix, filename_format="custom" if template else "smart", custom_template=template)
        if not template and (any(part in name for part in ('UnknownJournal', 'UnknownYear', 'UnknownAuthor'))
                             or details.get("primary_creator", "").casefold() in {"", "unknown", "unknown author"}):
            name = path.name  # A readable paper does not need a perfect citation to keep a useful filename.
            if re.fullmatch(r'[0-9a-f]{64}', path.stem, re.I):
                # Cloud downloads use content hashes privately, never as the
                # visible tablet name when a readable name is available.
                readable = ''
                for candidate in (metadata.get('title'), extracted_title):
                    candidate = str(candidate or '').strip()
                    if candidate.lower().endswith(path.suffix.lower()):
                        candidate = candidate[:-len(path.suffix)].strip()
                    if candidate and not re.fullmatch(r'[0-9a-f]{64}', candidate, re.I):
                        readable = candidate
                        break
                if not readable:
                    readable = 'Paper'
                    details['filename_needs_review'] = True
                if re.fullmatch(r'[0-9a-f]{64}(?:\.pdf)?', str(details.get('title', '')), re.I):
                    details['title'] = readable
                stem = sanitize_filename_part(readable)[:220] or 'Paper'
                try:
                    name = validate_document_filename(stem + path.suffix, path.suffix)
                except ValueError:
                    name = validate_document_filename('Paper_' + stem + path.suffix, path.suffix)
        if not template and PLACEHOLDER.search(name):
            name = (title_filename(details.get('title', ''), path.suffix, supplement=details.get('is_supplementary_material'))
                    or title_filename(extracted_title, path.suffix, supplement=details.get('is_supplementary_material'))
                    or validate_document_filename('Paper' + path.suffix, path.suffix))
            details['filename_needs_review'] = True
        from .zotero_maintenance import now
        basis = ('Existing filename' if name == path.name else
                 'Zotero citation and local document details' if metadata.get('key') else
                 'Saved source title and local document details' if metadata.get('title') else 'Local document details')
        citation = {'creators': '\n'.join(creator_name(c) for c in creators if c.get('creatorType') == creator_role(item_type))
                    if metadata.get('key') else '\n'.join(details.get('creators') or [details.get('primary_creator', '')]),
                    'date': date or str(details.get('year') or ''), 'venue': venue or details.get('venue_or_publisher') or details.get('journal', ''),
                    'doi': details.get('identifier', {}).get('doi', ''), 'patent_number': details.get('patent_number') or details.get('identifier', {}).get('patent', '')}
        return {"details": details, "filename": name, "title": details.get("title", path.stem), 'citation': citation,
                "document_type": details["document_type"], "parent": metadata.get("key", ""),
                'naming_evidence': {'source': basis, 'method': 'Naming template' if template else 'Smart naming',
                                    'checked_at': now()},
                'naming_signature': self._naming_signature()}

    def _offer(self, kind, identity, path, *, metadata=None, **extra):
        """Queue settled content, or signal a later check for a changing file."""
        from .inbox import _complete_document, file_signature
        if not self.review.observe_ready(kind + ":" + identity, file_signature(path)):
            return 'settling'
        if not _complete_document(path):
            if kind == 'zotero':
                raise PilotError('Open the attachment in Zotero to finish the download or check whether the file is readable.')
            return
        from .zotero_bulk import intake_pdf_doi, observed_digest
        digest = observed_digest(path, self.file_digests)
        if kind == 'inbox':
            self.review.supersede_inbox(path, identity)
        old = self.review.row(fingerprint([kind, identity])[:24])
        intake_pdf = kind == 'inbox' and path.suffix.lower() == '.pdf'
        signature = citation_signature(metadata)
        same_pending_source = (old and old['status'] == 'pending'
            and old['payload'].get('proposal_revision') == 2 and review_guidance_ready(old['payload'])
            and old['payload'].get('metadata_signature') == signature
            and old['payload'].get('naming_signature') == self._naming_signature()
            and old['payload'].get('parent', '') == (metadata or {}).get('key', '')
            and old['payload'].get('path') == str(path)
            and (not intake_pdf or 'intake_doi' in old['payload'])
            and not any(old['payload'].get(key) for key in
                        ('unavailable', 'local_recheck_error', 'tablet_evidence_stale')))
        if old and old['digest'] == digest and (old['status'] in {'approved', 'applying', 'ignored', 'attached'} or same_pending_source):
            return  # Do not re-extract every paper on every timer tick.
        payload = {**self._proposal(path, metadata), "path": str(path), "source": kind, "metadata_signature": signature,
                   'proposal_revision': 2,
                   'source_collections': (metadata or {}).get('collections', []) if kind == 'zotero' else [], **extra}
        if intake_pdf:
            payload['intake_doi'] = {}
            root = self.config.get('inbox')
            if (root and not payload.get('details', {}).get('is_supplementary_material')
                    and not re.search(r'(?:^|[_ -])si$', path.stem, re.I)
                    and path.resolve().parent == Path(root).resolve()):
                payload['intake_doi'] = intake_pdf_doi(path)
        if old and old['status'] == 'pending' and old['digest'] == digest:
            previous = {key: old['payload'].get(key, '') for key in ('title', 'filename', 'document_type')}
            if any(payload.get(key, '') != previous[key] for key in previous):
                payload['previous_proposal'] = previous
            elif old['payload'].get('previous_proposal'):
                payload['previous_proposal'] = old['payload']['previous_proposal']
        payload["review_token"] = fingerprint(payload)
        self.review.offer(kind, identity, digest, payload)

    def scan_inbox(self, *, catalog=False, discover=False):
        """Discover settled downloads without opening remote clients."""
        issues = []
        native_files_settling = False
        inbox = self.config.get("inbox")
        if inbox:
            from .inbox import file_signature
            try:
                paths = [path for path in Path(inbox).iterdir() if path.is_file() and path.suffix.lower() in {'.pdf', '.docx', '.pptx'} and not path.name.startswith('~$')]
            except OSError as error:
                # A temporarily disconnected intake directory is not proof its
                # individual files were removed.
                issues.append({'cloud': '', 'title': 'Intake folder', 'reason': str(error)})
            else:
                self.review.retire_missing_inbox(paths)
                for path in paths:
                    try:
                        identity = str(path.resolve()) + '|' + fingerprint(file_signature(path))[:16]
                        self.review.supersede_inbox(path, identity)
                        self._offer('inbox', identity, path)
                    except (OSError, ValueError, PilotError) as error:
                        issues.append({'cloud': '', 'title': path.name, 'reason': f'Could not check this intake paper: {error}'})
        if discover:
            client = self.zotero()
            try:
                self.catalog_issues = []
                native = self._catalog_local(client, local=True)
                native_files_settling = native['files_settling']
                fresh = [row for row in self.review.rows() if row['kind'] in {'zotero', 'inbox'}
                         and row['status'] == 'pending' and not review_guidance_ready(row['payload'])]
                if fresh:
                    self.refresh_review_policy(client, native, rows=fresh, cancelled=lambda: False)
                issues.extend(self.catalog_issues)
            finally:
                client.close()
        if catalog:
            self.catalog_issues.extend(issues)
        return {**self.status(), 'inbox_issues': issues, 'native_files_settling': native_files_settling}

    def _native_pdf_matches(self, catalog):
        """Current, unmanaged originals only; cached review matches are not evidence."""
        from .zotero_bulk import observed_digest
        excluded = connections.exclusions(self.directory, self.config)
        managed = {row.get(field) for row in self.state.read('documents', {}).values()
                   for field in ('source_key', 'view_key')} | excluded['sources']
        indexed = {}
        for attachment in catalog['attachments']:
            if self.stop.is_set():
                raise InterruptedError('Sync stopped')
            record = attachment['data']
            if (Path(attachment['path']).suffix.lower() != '.pdf' or record['key'] in managed
                    or 'Notable pilot:' in record.get('note', '')):
                continue
            try:
                digest = observed_digest(attachment['path'], self.file_digests)
            except OSError:
                continue
            indexed.setdefault(digest, []).append({'key': record['key'], 'parent': record.get('parentItem', ''),
                'title': catalog['parents'].get(record.get('parentItem'), {}).get('title', Path(attachment['path']).stem)})
        return indexed

    @contextmanager
    def _catalog_paper(self, item):
        try:
            yield
        except (InterruptedError, AuthorizationRequired, TransportInterrupted):
            raise
        except (CloudError, PilotError, OSError, KeyError, ValueError) as error:
            if isinstance(error, CloudError) and 'authorization expired' in str(error).lower():
                raise
            if self.stop.is_set():
                raise InterruptedError('Sync stopped') from error
            self.catalog_issues.append({'cloud': item.id, 'title': item.name,
                                        'reason': f'Could not check this tablet paper: {error}'})

    def _catalog_local(self, client, *, local=False):
        from .zotero_bulk import doi_key, references_by_doi
        native = client.review_action("catalog")
        documents = self.state.read("documents", {})
        excluded = connections.exclusions(self.directory, self.config)
        collections = client.collections()
        old_collections = set(excluded['zotero'])
        for _ in range(len(collections)):
            more = {row['key'] for row in collections if row.get('parentCollection') in old_collections}
            if more <= old_collections:
                break
            old_collections.update(more)
        active = descendants(collections, self.config['zotero_root'], lambda r: r['key'], lambda r: r.get('parentCollection'))
        active = set(connections.filtered_scope(active, excluded['zotero'], lambda r: r['key'],
                                                 lambda r: r.get('parentCollection'), self.config['zotero_root']))
        managed_sources = {row.get(field) for row in documents.values()
                           for field in ('source_key', 'view_key')} | excluded['sources']
        files_settling = False
        source_checks = {}
        preparing = {}
        for number, attachment in enumerate(native["attachments"], 1):
            if not local and self.stop.is_set():
                raise InterruptedError('Sync stopped')
            record, path = attachment["data"], Path(attachment["path"])
            parent = native['parents'].get(record.get('parentItem'), {})
            homes = set(parent.get('collections', []))
            if homes & old_collections and not homes & active:
                continue
            if record["key"] in managed_sources or "Notable pilot:" in record.get("note", ""):
                continue
            self.progress(f'Checking Zotero file {number} of {len(native["attachments"])}: {path.name}')
            try:
                result = self._offer("zotero", record["key"], path, metadata=parent, attachment=record["key"])
                if result == 'settling':
                    files_settling = True
                    source_checks[record['key']] = ('settling', 'The Zotero file is changing. Inbox will check it again shortly.')
                    if path.suffix.lower() == '.pdf':
                        preparing.setdefault(record.get('parentItem'), []).append(record['key'])
            except (OSError, ValueError, PilotError) as error:
                source_checks[record['key']] = ('unreadable', str(error))
                self.catalog_issues.append({'cloud': '', 'attachment': record['key'], 'title': path.name,
                                            'reason': f'Could not check this local paper: {error}'})
        self._reconcile_zotero_sources(client, native, managed_sources,
                                       self._owned_zotero_sources(documents, excluded), source_checks, local=local)

        # These references already have a supported file, a pending file
        # review, or a tablet connection. A second Inbox entry would suggest
        # adding another copy while the first file still owns its workflow.
        file_parents = {entry['data'].get('parentItem') for entry in native['attachments']
                        if not entry['data'].get('deleted')
                        and Path(entry['path']).suffix.lower() in {'.pdf', '.docx', '.pptx'}}
        pending = self.review.rows()
        filing_parents = {row['payload'].get('parent') for row in pending if row['kind'] == 'zotero'}
        pending_references = {row['identity'] for row in pending if row['kind'] == 'reference'}
        linked_parents = {row.get('parent') for row in documents.values()} | excluded['parents']
        inventory = native.get('pdf_inventory')
        missing_parents = set(native.get('missing_pdf_parents', [])) if inventory is None else set()
        doi_references = references_by_doi(native['parents']) if self.config.get('inbox') else {}
        references = {}
        for key, parent in native['parents'].items():
            homes = set(parent.get('collections', []))
            pdfs = (inventory or {}).get(key, {})
            if (parent.get('deleted') or key in filing_parents or key in linked_parents or pdfs.get('managed')
                    or (homes & old_collections and not homes & active)):
                continue
            pdf_keys = preparing.get(key, [])
            if key in pending_references and pdf_keys:
                pdf_state = 'preparing'
            elif key in file_parents or pdfs.get('available'):
                continue
            else:
                legacy_state = 'unavailable' if key in missing_parents else 'none'
                pdf_state = next((state for state in ('unsupported', 'unknown', 'unavailable') if pdfs.get(state)), legacy_state)
            references[key] = {'title': parent.get('title') or 'Untitled reference', 'parent': key,
                               'url': parent.get('url') or ('https://doi.org/' + parent['DOI'] if parent.get('DOI') else ''),
                               'pdf_state': pdf_state, 'missing_file': pdf_state == 'unavailable', 'routine': False}
            if pdf_state == 'none' and doi_references:
                doi = doi_key(parent.get('DOI'))
                if len(doi_references.get(doi, [])) == 1:
                    references[key]['intake_doi'] = doi
            if pdf_state == 'preparing' and len(pdf_keys) == 1:
                references[key]['preparing_attachment'] = pdf_keys[0]
        self.review.reconcile_references(references)
        return {**native, 'files_settling': files_settling, 'collections': collections}

    def _catalog(self, client, cloud):
        self.catalog_issues = []
        native = self._catalog_local(client)
        documents = self.state.read("documents", {})
        excluded = connections.exclusions(self.directory, self.config)
        self.scan_inbox(catalog=True)
        self.progress('Reading reMarkable’s file list…')
        catalog = cloud.library()
        confirmed = self.state.read('folder_blocks', {}).get('confirmed', {})
        mappings = self.state.read('folders', {})
        _, scope, blocked = sync_folder_scope(sync_collections(client, mappings, native['collections']), catalog, self.config['zotero_root'],
                                               self.config['tablet_root'], mappings, excluded, confirmed)
        self.state.write('folder_blocks', blocked)
        held_papers = folder_blocked_papers(documents, catalog, blocked)
        candidates = []
        native_by_digest = None
        prior = {(row['kind'], row['identity']): row for row in self.review.rows(pending_only=False)}
        pending = [item for item in catalog if item.is_pdf and not item.deleted and item.parent in scope
                   and item.id not in documents and item.id not in excluded['documents']]
        for number, item in enumerate(pending, 1):
            if self.stop.is_set():
                raise InterruptedError('Sync stopped')
            self.progress(f'Checking tablet paper {number} of {len(pending)}: {item.name}')
            with self._catalog_paper(item):
                previous = prior.get(('cloud', item.id))
                path = Path(previous['payload'].get('path', '')) if previous else None
                from .zotero_bulk import observed_digest
                unchanged = (previous and previous['payload'].get('cloud_hash') == item.hash and path.is_file()
                             and observed_digest(path, self.file_digests) == previous['digest'])
                if unchanged:
                    digest = previous['digest']
                else:
                    bundle = cloud.pdf_bundle(item)
                    data = bundle[item.id + ".pdf"]
                    digest = hashlib.sha256(data).hexdigest()
                    path = self.connection_dir / "incoming" / (digest + ".pdf")
                    if not path.exists():
                        atomic_write(path, data, require_absent=True)
                    elif observed_digest(path, self.file_digests) != digest:
                        raise PilotError('A saved review PDF changed. It was retained; restore its original before retrying.')
                candidates.append({"key": item.id, "title": item.name, "digest": digest, "parent": item.parent, "side": "cloud"})
                old = self.review.approval("cloud", item.id, digest)
                repair = previous['payload'].get('repair_required', '') if previous and previous['status'] == 'pending' else ''
                interrupted_changed = False
                if previous and previous['status'] == 'applying' and previous['approval'].get('cloud_change'):
                    try:
                        cloud_change_done(cloud, item, previous['approval']['cloud_change'])
                    except PilotError:
                        interrupted_changed = True
                        repair = 'This tablet paper changed while filing was interrupted. Review its current name and destination before filing again.'
                if old is not None:
                    if native_by_digest is None:
                        native_by_digest = self._native_pdf_matches(native)
                    matches = native_by_digest.get(digest, [])
                    if any(match.get('parent') != old.get('parent') for match in matches):
                        repair = 'An identical PDF now exists in Zotero. Choose it before syncing to avoid a duplicate reference.'
                    elif Path(old.get('filename', '')).stem == digest:
                        repair = 'An earlier filing used a temporary download name. Check the readable name before syncing.'
                    if repair:
                        old = None
                if old is None:
                    if native_by_digest is None:
                        native_by_digest = self._native_pdf_matches(native)
                    readable_name = item.name
                    if item.name == digest and previous and previous['payload'].get('title') != digest:
                        readable_name = previous['payload'].get('title') or item.name
                    signature = fingerprint({'name': readable_name, 'templates': self.config.get('templates', {}), 'filename_policy': 2})
                    if previous and previous['digest'] == digest and previous['payload'].get('proposal_signature') == signature:
                        proposal = {key: previous['payload'][key] for key in ('details', 'filename', 'title', 'document_type', 'parent')}
                    else:
                        proposal = self._proposal(path, {"title": readable_name})
                    payload = {**proposal, "path": str(path), "source": "cloud", 'proposal_signature': signature, 'proposal_revision': 2,
                               "cloud": item.id, "cloud_hash": item.hash, "tablet_parent": item.parent}
                    # Byte-identical originals are strong match candidates. Titles
                    # and DOI alone never trigger annotation overlay or adoption.
                    payload["matches"] = native_by_digest.get(digest, [])
                    if repair:
                        payload.update(repair_required=repair, previous_approval=previous['payload'].get('previous_approval') or previous['approval'])
                    payload["review_token"] = fingerprint(payload)
                    if repair:
                        self.review.reopen(previous['id'], digest, payload, applying=interrupted_changed)
                    self.review.offer("cloud", item.id, digest, payload)
        for row in self.review.rows():
            if row["kind"] in {"zotero", "inbox"} and row["payload"]["path"].lower().endswith(".pdf"):
                from .zotero_bulk import title_key
                name = title_key(Path(row["payload"]["path"]).stem)
                payload = {**row["payload"], "matches": [r for r in candidates if r["digest"] == row["digest"]],
                           "possible_tablet_matches": [r["title"] for r in candidates if r["digest"] != row["digest"]
                                                       and name == title_key(Path(r["title"]).stem)]}
                self.review.offer(row["kind"], row["identity"], row["digest"], payload)
        for identity, link in documents.items():
            if identity in held_papers:
                continue  # A folder change is not evidence of an individual paper deletion.
            item = next((r for r in catalog if r.id == identity), None)
            interrupted = prior.get(('cloud', identity))
            if interrupted and interrupted['status'] == 'applying':
                approved = interrupted['approval']
                original = self.state.directory / 'originals' / (interrupted['digest'] + '.pdf')
                source = client.get('item', link.get('source_key')) if link.get('source_key') else None
                if (approved.get('match') and link.get('source_key') == approved.get('source')
                        and link.get('parent') == approved.get('parent') and link.get('collection') == approved.get('collection')
                        and link.get('baseline') == interrupted['digest'] and original.is_file()
                        and file_sha(original) == interrupted['digest'] and source and not source.get('deleted')
                        and source.get('parentItem') == approved['parent']):
                    # Registration is the final side-effecting filing step.
                    # Finish only its bookkeeping; later tablet edits belong
                    # to ordinary sync and must never replay this old rename.
                    self.review.complete_counterpart('zotero', approved['source'], interrupted['digest'],
                                                     interrupted['payload'], approved)
                    self.review.finish(interrupted['id'], approval=approved)
            if self.review.link_control(identity)["paused"]:
                continue
            self.review.observe_live(item)
            if item and not item.deleted and link.get("parent"):
                parent = client.get("item", link["parent"])
                key = link.get("view_key") or link.get("source_key")
                view = client.get("item", key) if key else None
                if (not parent or parent.get('deleted')) and view and not view.get('deleted') and view.get('parentItem') != link['parent']:
                    if self._reconcile_merged_parent(identity, client):
                        link = self.state.read('documents', {}).get(identity, link)
                        parent = client.get('item', link['parent'])
                self.review.observe_zotero_live(identity, parent)
                self.review.observe_zotero_live(identity, view)
                self.review.confirm_zotero_deleted(item, link, parent if parent and parent.get("deleted") else view)
            if item and (item.deleted or item.parent == "trash") and link.get("parent"):
                published = self.state.directory / "publisher"
                from .zotero_pilot import Ledger
                namespace = Bridge(client, cloud, self.state, self.config["zotero_root"], self.config["tablet_root"]).namespace
                key = link.get("view_key") or stable_key(namespace, "attachment", identity)
                inventory = client.review_action("inventory", {"parent": link["parent"]})["inventory"]
                self.review.confirm_deleted(item, link, attachment=key, parent=link["parent"], inventory=inventory)
        return native

    def refresh_review_policy(self, client, catalog=None, *, rows=None, cancelled=None):
        from .zotero_bulk import review_policy
        rows = self.review.rows() if rows is None else rows
        rows = [row for row in rows if row['kind'] != 'reference']
        rows = [{**row, 'payload': review_policy_payload(row)} if row['status'] != 'applying' else row for row in rows]
        catalog = client.review_action("catalog") if catalog is None else catalog
        native_matches = self._native_pdf_matches(catalog) if any(row['kind'] == 'cloud' and row['status'] == 'pending' for row in rows) else {}
        for row in rows:
            if row['kind'] == 'cloud' and row['status'] == 'pending':
                row['payload'] = {**row['payload'], 'matches': native_matches.get(row['digest'], [])}
        native_keys = {entry['data']['key'] for entry in catalog['attachments'] if not entry['data'].get('deleted')}
        findings = {}
        policy = review_policy(rows, catalog, self.state.read("documents", {}),
                               self.state.read("folders", {}), cancelled=cancelled or self.stop.is_set, cache=self.file_digests,
                               findings=findings, acknowledgements=self.review.acknowledgement_tokens())
        for row in rows:
            if row["status"] == "applying":
                continue  # Preserve the existing recovery journal.
            issues, guidance = policy[row['id']], findings[row['id']]
            local_error = row['payload'].get('local_recheck_error')
            if local_error:
                issues = [*issues, local_error]
                guidance = [*guidance, {'code': 'local_review_unavailable', 'domain': 'file',
                    'severity': 'repair', 'summary': ('The saved attachment is in Zotero Trash' if row['payload'].get('source_availability') == 'trashed'
                        else 'A saved attachment is no longer available' if row['payload'].get('source_availability') == 'missing'
                        else 'This attachment belongs to reMarkable Sync' if row['payload'].get('source_availability') == 'managed'
                        else 'Recheck could not verify this source'),
                    'explanation': local_error,
                    'next_step': 'Inspect the source in Zotero. Restore or locate the file, then recheck; use Sync now when a current tablet comparison is needed.',
                    'evidence': [], 'references': self._review_repair_references(row, catalog),
                    'attachments': self._review_repair_attachments(row, catalog), 'collections': []}]
            # A background policy refresh has no native deletion proof. Leave
            # an absent source needing the local migration until recheck has
            # classified it; otherwise Sync could suppress the UI's upgrade.
            classified = (row['kind'] != 'zotero' or row['identity'] in native_keys
                          or row['payload'].get('source_availability') in {'missing', 'trashed', 'unknown', 'unsupported'})
            guidance_version = REVIEW_GUIDANCE_VERSION if classified else REVIEW_GUIDANCE_VERSION - 1
            payload = {**row["payload"], "issues": issues, "findings": guidance,
                       "routine": not issues, 'guidance_version': guidance_version,
                       'guidance_state': 'repair' if local_error else 'ready'}
            if row['kind'] == 'zotero':
                parent = catalog.get('parents', {}).get(payload.get('parent'), {})
                payload['source_collections'] = parent.get('collections', [])
            payload["review_token"] = fingerprint({k: v for k, v in payload.items() if k != "review_token"})
            self.review.offer(row["kind"], row["identity"], row["digest"], payload)

    @staticmethod
    def _review_repair_references(row, catalog):
        parent = row['payload'].get('parent')
        # A native pending source has a saved exact parent even after a merge.
        # Navigation may follow a native replacement relation; no link is rebound.
        if not parent or (row['kind'] != 'zotero' and parent not in catalog['parents']):
            return []
        record = catalog['parents'].get(parent, {})
        return [{'key': parent, 'title': record.get('title') or row['payload'].get('title') or 'Saved Zotero reference',
                 **{field: record.get(field, '') for field in ('itemType', 'date', 'DOI')}}]

    @staticmethod
    def _review_repair_attachments(row, catalog):
        if row['kind'] != 'zotero':
            return []
        key = row['identity']
        entry = next((entry for entry in catalog['attachments'] if entry['data']['key'] == key), {})
        record = entry.get('data', {})
        saved = row['payload']
        return [{'key': key, 'parent': record.get('parentItem') or saved.get('parent', ''),
                 'title': record.get('title') or 'Saved source attachment',
                 'filename': record.get('filename') or saved.get('filename') or Path(saved.get('path') or '').name,
                 'match_basis': 'context',
                 'availability': 'current' if entry and not record.get('deleted') else saved.get('source_availability', 'missing')}]

    def _source_has_recorded_link(self, key):
        if any(row['status'] == 'applying' and row['approval'].get('source') == key
               for row in self.review.rows()):
            return True
        if any(key in {link.get('source_key'), link.get('view_key')}
               for link in self.state.read('documents', {}).values()):
            return True
        # Archived same-library connections still own their exact attachments.
        return key in connections.exclusions(self.directory, self.config)['sources']

    def _resolve_source_review(self, row):
        with self.review.connect() as db:
            result = db.execute("UPDATE review_queue SET status='resolved' WHERE id=? AND status='pending' AND digest=? AND payload=?",
                                (row['id'], row['digest'], json.dumps(row['payload'])))
            if result.rowcount != 1:
                raise PilotError('This review changed while its removed source was checked. Recheck it again.')

    @staticmethod
    def _unchecked_pdf_states(catalog):
        return {key: state for inventory in (catalog.get('pdf_inventory') or {}).values()
                for state in ('unavailable', 'unknown', 'unsupported') for key in inventory.get(state, [])}

    @staticmethod
    def _trashed_zotero_source(client, key):
        record = client.get('item', key)
        return bool(record and record.get('key') == key and record.get('itemType') == 'attachment'
                    and record.get('deleted') is True)

    def _missing_zotero_source(self, client, key, pdf_state):
        """Return repair evidence, or None for a Trash entry safe to recheck for retirement."""
        if pdf_state == 'unknown':
            return {'source_removed': False, 'source_availability': 'unknown',
                    'local_recheck_error': 'Zotero could not check whether the saved PDF is available. Recheck Zotero before continuing.'}
        if pdf_state == 'unavailable':
            return {'source_removed': False, 'source_availability': 'missing',
                    'local_recheck_error': 'Zotero lists this PDF, but the file is unavailable on this computer. Open it in Zotero to download or locate it, then recheck.'}
        if pdf_state == 'unsupported':
            return {'source_removed': False, 'source_availability': 'unsupported',
                    'local_recheck_error': 'This PDF’s filename needs to end in .pdf before Inbox can file it. Open its attachment in Zotero to check its filename.'}
        if self._trashed_zotero_source(client, key):
            if not self._source_has_recorded_link(key):
                return None
            return {'source_removed': True, 'source_availability': 'trashed',
                    'local_recheck_error': 'The saved attachment is in Zotero Trash and still owns a tablet connection. Restore that exact attachment before resuming its connection.'}
        return {'source_removed': True, 'source_availability': 'missing',
                'local_recheck_error': 'The saved Zotero attachment is no longer available. It may have been merged or removed; inspect its reference or Trash in Zotero before continuing.'}

    def _resolve_trashed_source_review(self, client, row):
        # A concurrent sync must not acquire the source between verification and retirement.
        with self.state.lock():
            key = row['identity']
            if not self._trashed_zotero_source(client, key) or self._source_has_recorded_link(key):
                raise PilotError('The removed attachment or its connection changed. Recheck this case again.')
            self._resolve_source_review(row)

    @staticmethod
    def _owned_zotero_sources(documents, excluded):
        return {(link[field], link['parent']) for link in documents.values() if link.get('parent') and not link.get('unavailable')
                for field in ('source_key', 'view_key') if link.get(field)} | excluded['source_parents']

    def _resolve_owned_source_review(self, client, row, parent):
        with self.state.lock():
            key = row['identity']
            record = client.get('item', key)
            owned = self._owned_zotero_sources(self.state.read('documents', {}),
                                               connections.exclusions(self.directory, self.config))
            if (not record or record.get('key') != key or record.get('itemType') != 'attachment'
                    or record.get('deleted') or record.get('parentItem') != parent or (key, parent) not in owned):
                raise PilotError('This attachment’s reMarkable connection changed. Recheck Zotero before continuing.')
            self._resolve_source_review(row)

    def _reconcile_zotero_sources(self, client, catalog, managed, owned, source_checks, *, local):
        records = {entry['data']['key']: entry['data'] for entry in catalog['attachments'] if not entry['data'].get('deleted')}
        pdf_states = self._unchecked_pdf_states(catalog)
        changed = []
        for row in self.review.rows():
            if row['kind'] != 'zotero' or row['status'] != 'pending':
                continue
            if not local and self.stop.is_set():
                raise InterruptedError('Sync stopped')
            key = row['identity']
            record = records.get(key)
            if record:
                parent = record.get('parentItem')
                if (key, parent) in owned and parent in catalog['parents']:
                    try:
                        self._resolve_owned_source_review(client, row, parent)
                    except (AuthorizationRequired, TransportInterrupted):
                        raise
                    except (OSError, PilotError) as error:
                        self.catalog_issues.append({'cloud': '', 'attachment': key,
                            'title': row['payload'].get('title', 'Saved Zotero attachment'), 'reason': str(error)})
                    continue
                if key in managed or 'Notable pilot:' in record.get('note', ''):
                    source_checks[key] = ('managed', MANAGED_INBOX_SOURCE)
                if key in source_checks:
                    availability, error = source_checks[key]
                    evidence = {'source_removed': False, 'source_availability': availability, 'local_recheck_error': error}
                    if any(row['payload'].get(field) != value for field, value in evidence.items()):
                        changed.append({**row, 'payload': {**row['payload'], **evidence}})
                continue
            try:
                evidence = self._missing_zotero_source(client, key, pdf_states.get(key))
            except (AuthorizationRequired, TransportInterrupted):
                raise
            except (OSError, ValueError, PilotError):
                evidence = self._missing_zotero_source(client, row['identity'], 'unknown')
            if evidence is None:
                try:
                    self._resolve_trashed_source_review(client, row)
                except (AuthorizationRequired, TransportInterrupted):
                    raise
                except (OSError, PilotError) as error:
                    # Keep the saved row when ownership or native evidence changed mid-check.
                    self.catalog_issues.append({'cloud': '', 'attachment': row['identity'],
                        'title': row['payload'].get('title', 'Saved Zotero attachment'), 'reason': str(error)})
                continue
            if any(row['payload'].get(field) != value for field, value in evidence.items()):
                changed.append({**row, 'payload': {**row['payload'], **evidence}})
        if changed:
            self.refresh_review_policy(client, catalog, rows=changed, cancelled=(lambda: False) if local else None)

    def recheck_review(self, identity='', identities=None):
        """Recheck saved reviews against local Zotero/files, without a sync.

        This reuses the existing native catalog and policy adapters. It never
        constructs the cloud client, discovers new sources, applies approvals,
        cleans intake, updates a bridge link or resolves a mapping conflict.
        Pending evidence is refreshed, or an unlinked source positively confirmed
        in Zotero Trash is retired while retaining its history. Applying journals
        are opaque.
        """
        if not isinstance(identity, str):
            raise PilotError('Choose a current review to recheck.')
        rows = self.review.rows()
        if identity:
            rows = [row for row in rows if row['id'] == identity]
            if not rows:
                raise PilotError('This review is no longer pending. Open its current review before rechecking.')
        if identities is not None:
            if (identity or not isinstance(identities, list) or not identities or len(identities) > 1000
                    or any(not isinstance(value, str) or not value for value in identities)
                    or len(set(identities)) != len(identities)):
                raise PilotError('Choose a current set of items to recheck.')
            selected = set(identities)
            rows = [row for row in rows if row['id'] in selected]
            if {row['id'] for row in rows} != selected:
                raise PilotError('This case changed. Open its current items before rechecking.')
        before = {row['id']: (row['digest'], row['payload'], row['status']) for row in rows}
        pending = [row for row in rows if row['status'] == 'pending' and row['kind'] in PAPER_REVIEW_KINDS]
        summary = {'checked_at': time.time(), 'checked': 0, 'checked_ids': [], 'changed_ids': [],
                   'unavailable_ids': [], 'resolved_ids': [], 'preserved_applying': sum(row['status'] == 'applying' for row in rows),
                   'skipped_conflicts': sum(row['status'] == 'pending' and row['kind'] == 'conflict' for row in rows),
                   'cloud_checked': False}
        if not pending:
            self.review_recheck = summary
            return {**self.status(), 'review_recheck': summary}
        # Explicit rechecks establish fresh content evidence, rather than
        # trusting the process-long stat cache (Windows mtime can be restored).
        self.file_digests.clear()
        client = self.zotero()
        try:
            self.progress('Rechecking saved reviews against local Zotero and files…')
            catalog = client.review_action('catalog')
            collections = client.collections()
            attachments = {entry['data']['key']: entry for entry in catalog['attachments']}
            pdf_states = self._unchecked_pdf_states(catalog)
            from .inbox import _complete_document
            from .zotero_bulk import observed_digest
            retire_candidates = []
            for row in pending:
                if self.stop.is_set():
                    raise InterruptedError('Recheck stopped. No library changes were made.')
                upgrading = not review_guidance_ready(row['payload'])
                retrying_source = bool(row['payload'].get('local_recheck_error')) and not row['payload'].get('tablet_evidence_stale')
                payload = review_policy_payload(row)
                payload.pop('local_recheck_error', None)
                payload.pop('source_removed', None)
                payload.pop('source_availability', None)
                metadata = None
                try:
                    if row['kind'] == 'zotero':
                        native = attachments.get(row['identity'])
                        if not native or native['data'].get('deleted') or native['data'].get('itemType') != 'attachment':
                            evidence = self._missing_zotero_source(client, row['identity'], pdf_states.get(row['identity']))
                            if evidence is None:
                                retire_candidates.append(row)
                                continue
                            payload.update(evidence)
                            raise PilotError(evidence['local_recheck_error'])
                        data = native['data']
                        if 'Notable pilot:' in data.get('note', '') or self._source_has_recorded_link(row['identity']):
                            payload.update(source_removed=False, source_availability='managed')
                            raise PilotError(MANAGED_INBOX_SOURCE)
                        parent_key = data.get('parentItem', '')
                        if parent_key and parent_key not in catalog['parents']:
                            raise PilotError('The attachment’s parent reference is unavailable. Inspect its current reference or Trash in Zotero before continuing.')
                        metadata = catalog['parents'].get(parent_key, {})
                        path = Path(native['path'])
                        payload['path'] = str(path)
                    else:
                        if not payload['path']:
                            raise PilotError('This older review has no saved source path. Locate the original file before retrying; the saved review was retained.')
                        path = Path(payload['path'])
                    if not path.is_file():
                        raise PilotError('The saved source file is unavailable locally. Locate or restore its file in Zotero before rechecking.')
                    digest = observed_digest(path, self.file_digests)
                    if row['kind'] == 'cloud' and digest != row['digest']:
                        raise PilotError('The saved tablet review PDF changed locally. Its original digest was retained; restore the original review file before syncing or filing.')
                    if not _complete_document(path):
                        raise PilotError('The source file is incomplete or could not be read. Open it and finish any file download before rechecking.')
                    metadata_changed = (row['kind'] == 'zotero' and (
                        payload.get('metadata_signature') != citation_signature(metadata)
                        or payload.get('parent', '') != metadata.get('key', '')))
                    if upgrading or retrying_source or digest != row['digest'] or metadata_changed or str(path) != row['payload'].get('path') or payload.get('proposal_revision') != 2 or payload.get('naming_signature') != self._naming_signature():
                        # Re-extract only affected proposals. Current native
                        # metadata is authoritative, never parsed from a name.
                        proposal_metadata = metadata if row['kind'] == 'zotero' else ({'title': payload.get('title', '')} if row['kind'] == 'cloud' else None)
                        proposal = self._proposal(path, proposal_metadata)
                        if file_sha(path) != digest:
                            raise PilotError('The file changed while it was being rechecked. Recheck again after it finishes saving.')
                        if digest == row['digest']:
                            previous = {key: payload.get(key, '') for key in ('title', 'filename', 'document_type')}
                            if any(proposal.get(key, '') != previous[key] for key in previous):
                                payload['previous_proposal'] = previous
                        payload.update(proposal, path=str(path), proposal_revision=2)
                        if row['kind'] == 'zotero':
                            payload.update(metadata_signature=citation_signature(metadata),
                                           source_collections=metadata.get('collections', []))
                        if digest != row['digest']:
                            payload['matches'] = []
                            payload['possible_tablet_matches'] = []
                            payload['tablet_evidence_stale'] = True
                            row['digest'] = digest
                    if row['kind'] == 'zotero':
                        payload['source_collections'] = metadata.get('collections', [])
                    if payload.get('tablet_evidence_stale'):
                        payload['local_recheck_error'] = 'The source content changed. Sync now to compare it with current tablet copies before filing.'
                except (OSError, ValueError, PilotError) as error:
                    payload['local_recheck_error'] = str(error)
                row['payload'] = payload
            for row in retire_candidates:
                self._resolve_trashed_source_review(client, row)
                summary['resolved_ids'].append(row['id'])
            active = [row for row in pending if row['id'] not in summary['resolved_ids']]
            if active:
                self.refresh_review_policy(client, catalog, rows=active)
            self.collections = collections
            for row in pending:
                saved = self.review.row(row['id'])
                summary['checked_ids'].append(row['id'])
                if (saved['digest'], saved['payload'], saved['status']) != before[row['id']]:
                    summary['changed_ids'].append(row['id'])
                if saved['status'] == 'pending' and saved['payload'].get('local_recheck_error'):
                    summary['unavailable_ids'].append(row['id'])
            summary['checked'] = len(pending)
            self.review_recheck = summary
            return {**self.status(), 'review_recheck': summary}
        finally:
            client.close()

    def acknowledge_review_finding(self, identity, digest, code, token, review_token, keep_separate=True):
        """Record an explicit, evidence-bound library arrangement decision."""
        row = self.review.row(identity)
        if (not row or row['status'] != 'pending' or row['digest'] != digest
                or row['payload'].get('review_token') != review_token):
            raise PilotError('This review changed. Recheck it before recording this decision.')
        self.recheck_review(identity)
        row = self.review.row(identity)
        if row['payload'].get('local_recheck_error'):
            raise PilotError(row['payload']['local_recheck_error'])
        self.review.acknowledge_finding(identity, digest, code, token,
            expected_review_token=review_token, keep_separate=keep_separate)
        # One arrangement can be shared by several pending papers. Refresh
        # their independent rows without changing any filing approvals.
        return self.recheck_review()

    def scan_library(self, root, collection, lookup=True):
        return self.bulk.scan(root, collection, lookup)

    def import_library(self, plan_id, root=None, collection=None, lookup=None):
        return self.bulk.apply(plan_id, root=root, collection=collection, lookup=lookup)

    def retry_bulk(self, plan_id, row_id, dismiss=False, root=None, collection=None, lookup=None):
        return self.bulk.retry(plan_id, row_id, dismiss=dismiss, root=root, collection=collection, lookup=lookup)

    def close_removed_source(self, identity, digest, review_token):
        """Retire a deleted intake source, without approving it or ignoring restorations."""
        row = self.review.row(identity)
        if (not row or row['kind'] != 'zotero' or row['status'] != 'pending' or row['digest'] != digest
                or row['payload'].get('review_token') != review_token or not row['payload'].get('source_removed')):
            raise PilotError('This intake entry changed. Recheck it before removing it from Inbox.')
        if self._source_has_recorded_link(row['identity']):
            raise PilotError('This attachment owns a tablet connection. Repair that connection before removing its intake entry.')
        client = self.zotero()
        try:
            record = client.get('item', row['identity'])
            if record and not record.get('deleted'):
                raise PilotError('This attachment is present in Zotero. Recheck its current review instead of removing it.')
            self._resolve_source_review(row)
            return self.status()
        finally:
            client.close()

    def dismiss_review(self, identity, digest):
        self.review.dismiss(identity, digest)
        return self.status()

    def confirm_folder_change(self, issue, choice='keep'):
        """Resolve the exact live folder pair the user reviewed."""
        if not self.config.get('tablet_root') or not isinstance(issue, dict) or choice not in {'keep', 'mirror', 'restore'}:
            raise PilotError('Choose a current folder issue in Sync Conflicts.')
        client = self.zotero()
        try:
            with self.state.lock():
                cloud = self.cloud()
                mappings = self.state.read('folders', {})
                collections, catalog = sync_collections(client, mappings), cloud.library()
                confirmed = self.state.read('folder_blocks', {}).get('confirmed', {})
                scope_args = (collections, catalog, self.config['zotero_root'], self.config['tablet_root'],
                              mappings, connections.exclusions(self.directory, self.config))
                _, _, current = sync_folder_scope(*scope_args, confirmed)
                if issue not in current['issues']:
                    self.state.write('folder_blocks', current)
                    return {**self.status(), 'folder_change': 'changed'}
                if choice == 'restore':
                    undo = issue.get('undo', {})
                    if undo.get('side') == 'tablet':
                        restored = cloud.update_item(issue['tablet'], expected_hash=undo['version'], parent=undo['parent'],
                                                     scope_id=self.config['tablet_root'], restore=True)
                        if restored.deleted or restored.parent != undo['parent']:
                            raise PilotError('The restored reMarkable folder could not be verified.')
                    elif undo.get('side') == 'zotero':
                        result = client.review_action('restore_linked_collection', {'key': issue['zotero'],
                            'root': self.config['zotero_root'], 'parent': undo['parent'], 'version': undo['version']})
                        if not result.get('restored'):
                            raise PilotError('The restored Zotero collection could not be verified.')
                    else:
                        raise PilotError('The deleted folder is no longer available to restore to its previous parent.')
                    scope_args = (sync_collections(client, mappings), cloud.library(), *scope_args[2:])
                elif choice == 'mirror':
                    mirror = issue.get('mirror', {})
                    if mirror.get('side') == 'zotero':
                        result = client.review_action('trash_linked_collection', {'root': self.config['zotero_root'],
                            'key': issue['zotero'], 'inventory': zotero_folder_inventory(collections, issue['zotero'])})
                        if not result.get('trashed'):
                            raise PilotError('Zotero could not verify the matching collection. Sync to review it again.')
                    elif mirror.get('side') == 'tablet':
                        folder = next(item for item in catalog if item.id == issue['tablet'])
                        moved = cloud.update_item(folder.id, expected_hash=folder.hash, parent='trash', scope_id=self.config['tablet_root'])
                        if not moved.deleted or moved.parent != 'trash':
                            raise PilotError('The matching reMarkable folder could not be verified in Trash.')
                    else:
                        raise PilotError('The matching folder cannot be moved to Trash from this review.')
                if choice != 'restore':
                    confirmed = {**confirmed, issue['zotero']: issue['tablet']}
                _, _, current = sync_folder_scope(*scope_args, confirmed)
                self.state.write('folder_blocks', current)
        finally:
            client.close()
        return {**self.status(), 'folder_change': 'confirmed', 'folder_choice': choice}

    def attach_intake_pdf(self, reference, identity, digest, doi):
        """Attach exactly the ToSort file and citation the user reviewed."""
        from .inbox import file_signature
        from .zotero_bulk import digest_file, intake_pdf_suggestions, references_by_doi

        pending = self.review.rows()
        match = intake_pdf_suggestions(pending).get(reference)
        if not match or (match['id'], match['digest'], match['doi']) != (identity, digest, doi):
            raise PilotError('The ToSort match changed. Check Inbox before attaching.')
        row = next(row for row in pending if row['id'] == identity)
        parent = next(row['identity'] for row in pending if row['id'] == reference)
        root = self.config.get('inbox')
        path = Path(row['payload']['path'])
        if not root or path.suffix.lower() != '.pdf' or path.resolve().parent != Path(root).resolve():
            raise PilotError('The PDF is no longer in ToSort. Check Inbox before attaching.')
        try:
            signature = file_signature(path)
            if file_sha(path) != digest:
                raise PilotError('The ToSort PDF changed. Check Inbox before attaching.')
        except OSError as error:
            raise PilotError('The ToSort PDF is unavailable. Check Inbox before attaching.') from error

        client = self.zotero()
        try:
            native = client.review_action('catalog', {'parents_only': True})
            candidates = references_by_doi(native['parents']).get(doi, [])
            excluded = connections.exclusions(self.directory, self.config)
            linked = self.state.read('documents', {})
            if (len(candidates) != 1 or candidates[0]['key'] != parent or parent in excluded['parents']
                    or any(link.get('parent') == parent for link in linked.values())):
                raise PilotError('The Zotero reference or its DOI match changed. Check Inbox before attaching.')
            # Native Zotero imports from a private snapshot. An in-progress
            # download or replacement can never change the bytes being attached.
            with TemporaryDirectory(prefix='intake-pdf-', dir=self.directory) as scratch:
                staged = Path(scratch) / path.name
                shutil.copyfile(path, staged)
                if file_sha(staged) != digest or file_signature(path) != signature:
                    raise PilotError('The ToSort PDF changed. Check Inbox before attaching.')
                result = client.review_action('attach_intake_pdf', {'parent': parent, 'doi_value': candidates[0]['DOI'],
                    'source': str(staged), 'md5': digest_file(staged, 'md5')})
            if result.get('error'):
                raise PilotError(result['error'])
            key = result['key']
            attachment = client.get('item', key)
            stored = client.file_path(key)
            if (not attachment or attachment.get('parentItem') != parent or attachment.get('deleted')
                    or stored.resolve() == path.resolve() or file_sha(stored) != digest):
                raise PilotError('The stored Zotero copy could not be verified. The ToSort file was kept.')
            self.review.finish_intake_attachment(identity, digest, {'source': str(path), 'signature': signature,
                                                                    'sha256': digest, 'target': key})
            self.cleanup_intake(client, identity=identity)
        finally:
            client.close()
        result = self.scan_inbox(discover=True)
        return {**result, 'intake_attachment': {'reference': reference, 'key': key, 'source_retained': path.exists()}}

    def approve_routine(self, entries, collection):
        """One explicit batch decision, revalidated file by file, never blind approval."""
        if collection not in self.state.read("folders", {}):
            raise PilotError("Choose a mapped tablet destination in Setup first.")
        if not entries or len(entries) > 10000 or len({e["id"] for e in entries}) != len(entries):
            raise PilotError("Choose a current routine batch.")
        self.stop.clear()
        client = self.zotero()
        try:
            self.refresh_review_policy(client)
        except (InterruptedError, PilotError):
            if not self.stop.is_set():
                raise
            self.last = {'state': 'paused', 'message': f'Filing paused: 0 filed; {len(entries)} remaining.'}
            return self.status()
        finally:
            client.close()
        completed, held = 0, 0
        for number, expected in enumerate(entries, 1):
            if self.stop.is_set():
                break
            self.progress(f'Filing paper {number} of {len(entries)}…')
            row = self.review.row(expected["id"])
            if row and row["status"] == "approved":
                continue  # Linking one side can also resolve its identical counterpart's review.
            if (not row or row["status"] not in {'pending', 'applying'} or row["digest"] != expected["digest"]
                    or not row["payload"].get("routine") or row["payload"].get("review_token") != expected.get("review_token")):
                held += 1
                continue
            payload = row["payload"]
            matches = payload.get("matches", [])
            home = collection
            if row['kind'] == 'zotero' and not matches:
                original_homes = [k for k in payload.get('source_collections', []) if k in self.state.read('folders', {})]
                if len(original_homes) == 1:
                    home = original_homes[0]
            if row['kind'] == 'cloud' or len(matches) == 1:
                match_parent = payload.get('tablet_parent') if row['kind'] == 'cloud' else matches[0].get('parent')
                home = next((key for key, value in self.state.read("folders", {}).items() if value["cloud"] == match_parent), home)
            match = matches[0]['key'] if matches else ''
            if row['status'] == 'applying' and (any(row['approval'].get(key) != value for key, value in {
                    'filename': payload['filename'], 'title': payload['title'], 'document_type': payload['document_type'],
                    'collection': home, 'match': match}.items()) or row['approval'].get('research')):
                held += 1
                continue
            try:
                self.approve(row["id"], row["digest"], payload["filename"], payload["title"], payload["document_type"],
                             home, match=match, review_token=payload["review_token"], _batch=True)
                completed += 1
            except InterruptedError:
                self.stop.set()
                break
            except Exception as error:
                if self.stop.is_set():
                    break
                if (self.review.row(row['id']) or {}).get('status') == 'applying':
                    # Preserve the original approved snapshot and choices for
                    # Resume after a partly committed operation.
                    held += 1
                    continue
                payload = {**payload, "routine": False, "issues": [str(error)]}
                self.review.offer(row["kind"], row["identity"], row["digest"], payload)
                held += 1
        remaining = sum(1 for expected in entries if (self.review.row(expected['id']) or {}).get('status') != 'approved')
        self.last = {"state": "paused" if self.stop.is_set() else "needs_review" if held else "ready",
                     "message": f"Filing paused: {completed} filed; {remaining} remaining." if self.stop.is_set() else f"Filed {completed} papers together; {held} held for review. Sync transfers approved papers."}
        return self.status()

    def sync(self, automatic=False, verify=False):
        if not self.config.get("tablet_root"):
            return self.status()
        if self.pause_pending.is_set():
            raise InterruptedError('Sync paused')
        if automatic and (self.config.get('sync_paused') or not self.config.get('automatic')):
            return self.status()
        if self.config.get('sync_paused'):
            self.config['sync_paused'] = False
            self.save()
        self._begin_operation()
        # Periodically establish fresh byte evidence even when file stats agree.
        if verify or time.time() - getattr(self, '_last_digest_verification', 0) > 86400:
            self.file_digests.clear()
            self._last_digest_verification = time.time()
        started_trace = self.trace.operation is None
        if started_trace:
            self.trace.start("sync")
        self.trace.event("stage", name="opening_connections")
        client = None
        try:
            # Handwriting recognition is optional and must not prevent the
            # normal sync from starting when its separate environment is absent.
            client = self.zotero()
            cloud = self.cloud()
            self.trace.event("stage", name="cleaning_intake")
            self.cleanup_intake(client)
            self.trace.event("stage", name="cataloging_sources")
            self.progress('Checking new papers and existing links…')
            self._catalog(client, cloud)
            self.trace.event("stage", name="refreshing_review_policy")
            self.progress('Checking Zotero matches and saved review decisions…')
            self.refresh_review_policy(client)
            self.trace.event("stage", name="syncing_bridge")
            excluded = connections.exclusions(self.directory, self.config)
            excluded['documents'].update(issue['cloud'] for issue in self.catalog_issues if issue.get('cloud'))
            result = Bridge(client, cloud, self.state, self.config["zotero_root"], self.config["tablet_root"], review_policy=self.review, follow_changes=True,
                            progress=self.progress, exclusions=excluded, file_digests=self.file_digests).run(apply=True)
            catalog_failures = {row['cloud'] for row in self.catalog_issues}
            result['review'] = self.catalog_issues + [row for row in result['review'] if row.get('cloud') not in catalog_failures]
            links = self.state.read('documents', {})
            partial_indexes = 0
            for action in result.get('actions', []):
                diagnostics = action.get('index_warnings') or {}
                if not diagnostics:
                    continue
                pages = {}
                for kind, label in (('printed_ocr', 'printed text'), ('handwriting', 'handwriting')):
                    values = diagnostics.get(kind, [])
                    page_numbers = sorted({value.get('page') for value in values if isinstance(value.get('page'), int)})
                    if page_numbers:
                        pages[label] = ', '.join(str(page) for page in page_numbers)
                if not diagnostics.get('incomplete') and not pages:
                    continue
                partial_indexes += 1
                identity = action.get('cloud', '')
                title = links.get(identity, {}).get('name') or 'Linked paper'
                detail = '; '.join(f'{label} on page(s) {numbers}' for label, numbers in pages.items())
                outcome = 'Search indexing is incomplete' if diagnostics.get('incomplete') else 'OCR reported a search-quality warning'
                recovery = 'OCR will retry on the next sync.' if diagnostics.get('incomplete') else 'Review the OCR result before relying on full-text search.'
                result['review'].append({'cloud': identity, 'title': title,
                    'reason': outcome + (f': {detail}' if detail else '') + '. ' + recovery})
            result['partial_indexes'] = partial_indexes
            self.collections = client.collections()
            completed_at = time.time()
            self.state.write("last_sync_at", completed_at)
            folder_issues = self.state.read('folder_blocks', {}).get('issues', [])
            folder_count = len(folder_issues)
            message = ('Sync finished. Review the paused folders in Sync Conflicts.' if folder_count else
                       'Sync finished — some links need attention' if result['review'] else 'Sync finished')
            self.last = {"state": "needs_review" if self.review.rows() or self.review.deletions() or self.review.local_deletions()
                         or result['review'] or folder_count else "ready", "message": message, "updated": completed_at, "result": result}
            self.trace.event("sync_result", actions=len(result.get("actions", [])),
                             reviews=len(result.get("review", [])), partial_indexes=partial_indexes)
            return self.status()
        finally:
            if client is not None:
                client.close()
            if started_trace and self.trace.operation:
                self.trace.finish("completed" if self.last.get("state") in {"ready", "needs_review"} else "stopped")

    def preview(self, identity, document_type, title, template=None, citation=None):
        from .sorting.core_logic import _normalize_year, build_proposed_filename, validate_filename_template
        from .zotero_filenames import PLACEHOLDER, title_filename
        row = self.review.row(identity)
        if not row:
            raise PilotError("Select a document in the review queue.")
        supplement = document_type == 'supporting_information'
        document_type = 'journal_article' if supplement else document_type
        details = {**row["payload"]["details"], "document_type": document_type, "title": title}
        details['is_supplementary_material'] = supplement
        edits = clean_citation_edits(citation)
        if 'creators' in edits:
            details['creators'] = [line.strip() for line in edits['creators'].splitlines() if line.strip()]
            details['primary_creator'] = next(iter(details['creators']), '')
        if 'date' in edits:
            details['year'] = _normalize_year(edits['date'])
        if 'venue' in edits:
            details.update(journal=edits['venue'], venue_or_publisher=edits['venue'], journal_abbreviation='')
        if 'patent_number' in edits:
            details['patent_number'] = edits['patent_number']
        details['identifier'] = {**details.get('identifier', {}), **{key: edits[field] for key, field in (
            ('doi', 'doi'), ('patent', 'patent_number')) if field in edits}}
        template = self.config.get("templates", {}).get(document_type, "") if template is None else template
        if template:
            validate_filename_template(template)
        name = build_proposed_filename(details, Path(row["payload"]["path"]).suffix,
                 filename_format="custom" if template else "smart", custom_template=template)
        if not template and PLACEHOLDER.search(name):
            original_type = row['payload'].get('document_type') or row['payload']['details'].get('document_type')
            original_title = row['payload'].get('title') or row['payload']['details'].get('title')
            if not edits and document_type == original_type and title == original_title and not supplement:
                name = row['payload']['filename']
            if PLACEHOLDER.search(name):
                suffix = Path(row['payload']['path']).suffix
                name = title_filename(title, suffix, supplement=supplement) or ('Paper_SI' if supplement else 'Paper') + suffix
        return {"filename": name}

    def approve(self, identity, digest, filename, title, document_type, collection, research=None, match="", review_token="", _batch=False,
                main_reference='', main_reference_metadata=None, citation=None, tags=None):
        from .sorting.core_logic import validate_document_filename
        from .zotero_tags import names
        edits = clean_citation_edits(citation)
        requested_tags = names([] if tags is None else tags)
        row = self.review.row(identity)
        if not row or row["digest"] != digest:
            raise PilotError("The document changed. Refresh before approving.")
        if review_token and row["payload"].get("review_token") != review_token:
            raise PilotError("The reference details changed. Refresh the review before approving.")
        if row['status'] == 'pending' and row['payload'].get('local_recheck_error'):
            raise PilotError(row['payload']['local_recheck_error'])
        path = Path(row["payload"]["path"])
        client = self.zotero()
        try:
            if row['kind'] == 'zotero' and row['status'] == 'pending':
                # Dragging an attachment between references does not change its
                # bytes. A digest alone cannot authorize filing the old parent
                # or recreating a reference removed by a native merge.
                source_record = client.get('item', row['identity'])
                expected_parent = row['payload'].get('parent', '')
                if (not source_record or source_record.get('deleted')
                        or source_record.get('itemType') != 'attachment'
                        or source_record.get('parentItem', '') != expected_parent):
                    raise PilotError('The source attachment or its reference changed. Recheck Zotero before filing.')
                if 'Notable pilot:' in source_record.get('note', '') or self._source_has_recorded_link(row['identity']):
                    raise PilotError(MANAGED_INBOX_SOURCE)
                if expected_parent:
                    current_parent = client.get('item', expected_parent)
                    if (not current_parent or current_parent.get('deleted')
                            or current_parent.get('itemType') in {'attachment', 'annotation', 'note'}):
                        raise PilotError('The source reference is unavailable. Recheck Zotero before filing; the saved reference will not be recreated.')
            if row['kind'] == 'cloud' and row['status'] == 'pending' and not _batch:
                # A PC import can add an identical original after this review
                # was displayed. Refresh locally and ask for a fresh choice.
                current_matches = self._native_pdf_matches(client.review_action('catalog')).get(digest, [])
                if current_matches != row['payload'].get('matches', []):
                    self.refresh_review_policy(client)
                    raise PilotError('The matching Zotero papers changed. Refresh the review and choose the current match before filing.')
            if row["kind"] == "zotero":
                path = client.file_path(row["identity"])
                if row['status'] == 'pending' and str(path) != row['payload'].get('path'):
                    raise PilotError('The source file name or location changed. Recheck Zotero before filing.')
            filename = validate_document_filename(filename, path.suffix)
            if file_sha(path) != digest:
                raise PilotError("The file changed during review. Refresh it before filing.")
            if row['status'] == 'pending' and review_token and not _batch:
                self.recheck_review(identity)
                current_review = self.review.row(identity)
                if current_review['digest'] != digest or current_review['payload'].get('review_token') != review_token:
                    raise PilotError('The paper or its Zotero evidence changed. Recheck Zotero and review its current choices before confirming.')
            collections = client.collections()
            mapped = descendants(collections, self.config["zotero_root"], lambda r: r["key"], lambda r: r.get("parentCollection"))
            blocked = self.state.read('folder_blocks', {})
            if collection in blocked.get('zotero', []) or (row['kind'] == 'cloud' and row['payload'].get('tablet_parent') in blocked.get('tablet', [])):
                raise PilotError('This folder’s sync is paused. Review the folder in Sync Conflicts before filing here.')
            if collection not in mapped or any(k not in {r["key"] for r in collections} for k in (research or [])):
                raise PilotError("Choose a current tablet home and current research collections.")
            parent = row["payload"].get("parent") or stable_key("notable-review", "reference", row["kind"] + row["identity"])
            source = row["identity"] if row["kind"] == "zotero" else stable_key("notable-review", "source", row["identity"])
            chosen_match = next((m for m in row["payload"].get("matches", []) if m["key"] == match), None)
            if match and not chosen_match and row['kind'] == 'zotero' and row['status'] == 'applying':
                # A partially committed link is omitted from the next discovery
                # pass. Recover its exact saved decision, never a new match:
                # the native PDF and live tablet bytes are checked below.
                saved = row['approval']
                link = self.state.read('documents', {}).get(match, {})
                expected = {'filename': filename, 'title': title, 'document_type': document_type,
                            'parent': parent, 'collection': collection, 'source': source,
                            'research': research or [], 'match': match}
                if (all(saved.get(key) == value for key, value in expected.items())
                        and link.get('source_key') == source and link.get('parent') == parent
                        and link.get('collection') == collection and link.get('baseline') == digest):
                    chosen_match = {'key': match}
            if match and not chosen_match:
                raise PilotError("Choose a displayed, byte-verified match or keep this document separate.")
            if row['kind'] == 'cloud':
                current = next((r for r in self.cloud().library() if r.id == row['identity']), None)
                # Recovery may have already renamed/moved this item itself.
                if (not current or current.deleted or (row['status'] != 'applying' and current.hash != row['payload'].get('cloud_hash'))):
                    raise PilotError('The tablet document changed since review. Sync to refresh it before filing.')
            if row['kind'] == 'cloud' and not match:
                # Recheck even batch approvals and old cached decisions before
                # creating a reference. A late PC import must not become a clone.
                candidates = self._native_pdf_matches(client.review_action('catalog')).get(digest, [])
                if candidates:
                    raise PilotError('This tablet PDF already exists in Zotero. Recheck the paper and select its existing PDF before filing; no duplicate reference was created.')
            if chosen_match and row["kind"] == "cloud":
                if file_sha(client.file_path(match)) != digest:
                    raise PilotError("The matching PDF changed. Its annotations were retained separately.")
                parent, source = chosen_match["parent"], match
                if not parent:
                    raise PilotError("Create a Zotero parent reference for this attachment before linking it.")
            elif chosen_match:
                matched_cloud = next((r for r in self.cloud().library() if r.id == match), None)
                if not matched_cloud or matched_cloud.deleted or hashlib.sha256(self.cloud().pdf_bundle(matched_cloud)[match + ".pdf"]).hexdigest() != digest:
                    raise PilotError("The tablet original changed. Refresh the match before linking.")
            supplement = document_type == 'supporting_information'
            source_parent = row['payload'].get('parent')
            source_reference = client.get('item', source_parent) if source_parent else None
            if supplement:
                if edits:
                    raise PilotError('Edit the main citation separately before filing supporting information.')
                if not main_reference:
                    raise PilotError('Choose the main paper in Zotero for this supporting information.')
                main = client.get('item', main_reference)
                if not main or main.get('deleted') or main.get('itemType') in {'attachment', 'annotation', 'note'}:
                    raise PilotError('The selected main paper is unavailable. Choose a current Zotero reference.')
                if not isinstance(main_reference_metadata, dict) or citation_signature(main) != citation_signature(main_reference_metadata):
                    raise PilotError('The main paper changed while this filing waited. Find and select it again.')
                if chosen_match and row['kind'] == 'cloud' and chosen_match.get('parent') != main_reference:
                    raise PilotError('The matching PDF belongs to a different reference. Check the main paper before linking.')
                parent = main_reference
            approval = {"filename": filename, "title": title, "document_type": document_type,
                        "parent": parent, "collection": collection, "source": source, "research": research or [], "match": match}
            if row['kind'] == 'cloud' or chosen_match:
                # Persist the observed revision and exact target before any
                # citation/attachment mutation. Resume must recognize our own
                # committed move, never authorize overwriting a newer edit.
                cloud = self.cloud()
                cloud_identity = row['identity'] if row['kind'] == 'cloud' else match
                current = next((r for r in cloud.library() if r.id == cloud_identity), None)
                folders = self.state.read('folders', {})
                if not current or current.deleted or collection not in folders:
                    raise PilotError('The tablet document or its selected home is unavailable. Sync before filing.')
                destination = folders[collection]['cloud']
                intent = row['approval'].get('cloud_change') if row['status'] == 'applying' else None
                if intent is None:
                    if row['kind'] == 'cloud' and current.hash != row['payload'].get('cloud_hash'):
                        # Older journals have no post-write receipt. They can
                        # finish an already-achieved target without any cloud
                        # write, only after rechecking the original PDF bytes.
                        completed = (row['status'] == 'applying' and current.name == Path(filename).stem
                                     and current.parent == destination and
                                     hashlib.sha256(cloud.pdf_bundle(current)[current.id + '.pdf']).hexdigest() == digest)
                        if not completed:
                            raise PilotError('The tablet document changed while filing was interrupted. Its newer changes were retained; review its current state before retrying.')
                    intent = cloud_change_intent(cloud, current, Path(filename).stem, destination)
                cloud_change_done(cloud, current, intent)
                approval['cloud_change'] = intent
            reference = client.get("item", parent)
            fields = citation_patch(reference or {'itemType': TYPE_MAP.get(document_type, 'document')}, edits, document_type)
            if edits:
                original = row['approval'].get('citation_original') if row['status'] == 'applying' else None
                if original is not None:
                    if any(((reference or {}).get(field) or None) not in (value or None, fields.get(field) or None)
                           for field, value in original.items()):
                        raise PilotError('The Zotero citation changed after filing started. Review the saved filing before retrying.')
                else:
                    original = {field: (reference or {}).get(field) for field in fields}
                approval.update(citation_edits=edits, citation_original=original)
            if requested_tags:
                approval['tags'] = requested_tags
            if row["kind"] == "zotero" and source_parent and source_reference:
                if row["status"] != "applying" and row["payload"].get("metadata_signature") != citation_signature(source_reference):
                    raise PilotError("The Zotero citation changed during review. Sync to refresh it before filing.")
                if row["status"] == "applying" and source_reference.get("title") not in {row["payload"].get("title"), title}:
                    raise PilotError("The reference title changed while filing was interrupted. Review it before retrying.")
            self.review.begin(identity, digest, approval)
            if not reference:
                details = row["payload"]["details"]
                metadata = {"key": parent, "itemType": TYPE_MAP.get(document_type, "document"),
                            "title": title, "collections": sorted(set([collection, *(research or [])]))}
                creators = details.get("creators") or [details.get("primary_creator", "")]
                creator_type = "presenter" if document_type == "presentation_poster" else "inventor" if document_type in {"patent", "patent_application"} else "author"
                metadata["creators"] = [creator_record(name, creator_type) for name in creators if name and str(name).lower() not in {"unknown", "unknown author"}]
                year = str(details.get("year", ""))
                if len(year) == 4 and year.isdigit() and document_type not in {"patent", "patent_application"}:
                    metadata["date"] = year
                if document_type in {"journal_article", "preprint", "conference_paper", "book_chapter", "book", "thesis_dissertation", "technical_report"} and details.get("identifier", {}).get("doi"):
                    metadata["DOI"] = details["identifier"]["doi"]
                metadata.update(fields)
                if requested_tags:
                    metadata['tags'] = [{'tag': tag} for tag in requested_tags]
                reference = client.create("item", metadata)
            else:
                reference["collections"] = sorted(set(reference.get("collections", []) + [collection] + (research or [])))
                if not supplement and not (chosen_match and row['kind'] == 'cloud'):
                    reference["title"] = title
                reference.update(fields)
                existing_tags = reference.get('tags', [])
                existing_names = {tag['tag'] for tag in existing_tags}
                if requested_tags:
                    reference['tags'] = existing_tags + [{'tag': tag} for tag in requested_tags if tag not in existing_names]
                client.update(reference)
            # Zotero can automatically rename an attachment when its citation
            # changes. Resolve by key again rather than using the pre-save path.
            if row['kind'] == 'zotero':
                path = client.file_path(source)
                if file_sha(path) != digest:
                    raise PilotError('The PDF changed while saving its citation. Its files were retained.')
            self.apply_document_keywords(client, parent, path, identity, digest)
            if row['kind'] == 'zotero':
                path = client.file_path(source)
            if row["kind"] == "inbox":
                from .inbox import file_signature
                self.review.cleanup(identity, {"source": str(path), "signature": file_signature(path), "sha256": digest, "target": source})
                attachment = client.get("item", source)
                if not attachment:
                    client.create("item", {"key": source, "itemType": "attachment", "parentItem": parent,
                        "linkMode": "imported_file", "contentType": mimetypes.guess_type(filename)[0] or "application/octet-stream",
                        "title": filename, "filename": filename})
                current = client.file_md5(source)
                wanted = hashlib.md5(path.read_bytes()).hexdigest()
                if current not in {"", wanted}:
                    raise PilotError("A previously imported attachment changed. Both files were retained.")
                from .zotero_pilot import Ledger, upload_attachment
                source_ledger = Ledger(self.connection_dir / 'source-imports')
                with source_ledger.lock():
                    source_ledger.bind(client.server_id, 'intake-source-files')
                    # Even matching bytes may still need their account-upload
                    # receipt completed after a lost response or interruption.
                    upload_attachment(client, source_ledger, source, source, path, current, wanted)
                if client.file_md5(source) != wanted:
                    raise PilotError("Import verification failed. The download was retained.")
                # Gate the newly stored attachment under its Zotero identity too.
                self.review.complete_counterpart('zotero', source, digest, row['payload'], approval)
            elif row["kind"] == "zotero":
                attachment = client.get("item", source)
                if not attachment.get("parentItem") or (supplement and attachment.get('parentItem') != parent):
                    from .zotero_pilot import reparent_attachment
                    reparent_attachment(client, source, parent)
                client.review_action("rename", {"key": source, "filename": filename, "md5": hashlib.md5(path.read_bytes()).hexdigest()})
                path = client.file_path(source)
                if file_sha(path) != digest:
                    raise PilotError('The renamed PDF changed. Its files were retained.')
            if row["kind"] == "cloud" or chosen_match:
                cloud = self.cloud()
                cloud_identity = row["identity"] if row["kind"] == "cloud" else match
                item = next((r for r in cloud.library() if r.id == cloud_identity), None)
                if not item or item.deleted:
                    raise PilotError("The tablet document moved or was trashed. Refresh the review.")
                folders = self.state.read("folders", {})
                if collection not in folders:
                    raise PilotError("Sync the selected tablet home before filing.")
                destination = folders[collection]["cloud"]
                previous = self.state.read('documents', {}).get(item.id, {}) if chosen_match else {}
                if previous and previous.get('baseline') != digest:
                    raise PilotError('The existing tablet connection belongs to a different PDF. Its files and location were retained.')
                intent = approval['cloud_change']
                if not cloud_change_done(cloud, item, intent):
                    item = cloud.update_item(item.id, expected_hash=intent['before_hash'],
                        name=intent['name'], parent=intent['parent'], scope_id=self.config['tablet_root'])
                intent['after_hash'] = item.hash
                self.review.checkpoint(identity, cloud_change=intent)
                if chosen_match:
                    links = self.state.read("documents", {})
                    previous = links.get(item.id, {})
                    if previous and previous.get('baseline') != digest:
                        raise PilotError('The existing tablet connection belongs to a different PDF. Its notes were retained.')
                    if previous.get('view_key'):
                        view = client.get('item', previous['view_key'])
                        if not view or view.get('deleted') or view.get('parentItem') != previous.get('parent'):
                            raise PilotError('The connected tablet PDF changed. Review its current reference before relinking.')
                        from .zotero_pilot import reparent_attachment
                        reparent_attachment(client, view['key'], parent)
                        view = client.get('item', view['key'])
                        source_note = 'Notable source attachment: ' + source
                        if source_note not in view.get('note', ''):
                            view['note'] = view.get('note', '') + '<p>' + source_note + '</p>'
                        client.update(view)
                    links[item.id] = {**previous, "origin": "zotero", "source_key": source, "parent": parent, "collection": collection,
                        "baseline": digest, "uploaded": True, "name": Path(filename).stem, "tablet_parent": destination}
                    if previous.get('parent') and previous['parent'] != parent:
                        for field in ('tag_state', 'tag_pending', 'keyword_check'):
                            links[item.id].pop(field, None)
                    original = self.state.directory / "originals" / (digest + ".pdf")
                    if not original.exists():
                        atomic_write(original, path.read_bytes(), require_absent=True)
                    elif file_sha(original) != digest:
                        raise PilotError('The retained original changed. It was preserved; repair it before linking this paper.')
                    self.state.write("documents", links)
                    self.review.complete_counterpart('zotero', source, digest, row['payload'], approval)
                    pending_cloud = next((r for r in self.review.rows() if r["kind"] == "cloud" and r["identity"] == item.id), None)
                    if pending_cloud:
                        self.review.begin(pending_cloud["id"], pending_cloud["digest"], approval)
                        self.review.finish(pending_cloud["id"])
            self.review.finish(identity, approval=approval)
            if row["kind"] == "cloud" or chosen_match:
                self.review.link_control(item.id, event={"action": "linked" if chosen_match else "approved", "filename": filename})
            if row["kind"] == "inbox":
                self.cleanup_intake(client)
            return {} if _batch else self.status()
        finally:
            client.close()

    def cleanup_intake(self, client, *, identity=None):
        from .inbox import _finish_verified_copy
        for job in self.review.cleanups():
            if identity and job['identity'] != identity:
                continue
            row = self.review.row(job["identity"])
            if not row or row["status"] not in {"approved", "attached"}:
                continue
            data = job["payload"]
            source = Path(data["source"])
            if not source.exists():
                self.review.cleanup(job["identity"], complete=True)
                continue
            try:
                _finish_verified_copy(source, client.file_path(data["target"]), data["signature"], data["sha256"])
                self.review.cleanup(job["identity"], complete=True)
            except (OSError, ValueError):
                # A locked or changed download is retained; the verified import
                # and its recorded cleanup can be retried on a later cycle.
                continue

    def delete_decision(self, identity, choice, destination=''):
        client = self.zotero()
        try:
            result = self.review.resolve_deletion(identity, choice, cloud=self.cloud(), zotero=client, scope=self.config["tablet_root"], destination=destination)
            return {"decision": result, **self.status()}
        finally:
            client.close()

    def _link_repair_snapshot(self, identity, client, *, merge_recovery=False):
        """Verify a native parent merge without replacing PDFs or publication state."""
        from .zotero_pilot import marker
        links = self.state.read('documents', {})
        link = links.get(identity)
        if not link or not link.get('parent') or not link.get('baseline'):
            raise PilotError('This saved connection is unavailable. Recheck Zotero before repairing it.')
        if any(row['payload'].get('cloud') == identity for row in [*self.review.deletions(), *([] if merge_recovery else self.review.local_deletions())]):
            raise PilotError('Finish this paper’s deletion decision before repairing its connection.')
        keys = {link.get('source_key'), link.get('view_key')} - {None, ''}
        if any(row['status'] == 'applying' and (row['identity'] in keys | {identity}
               or row['approval'].get('source') in keys or row['approval'].get('match') == identity)
               for row in self.review.rows()):
            raise PilotError('Finish this paper’s interrupted filing before repairing its connection.')
        cloud = self.cloud()
        bridge = Bridge(client, cloud, self.state, self.config['zotero_root'], self.config['tablet_root'])
        publisher = bridge.publisher
        publication = publisher.get(identity)
        if publication.get('pending_md5') or publisher.upload_intent(identity):
            raise PilotError('This PDF has an unfinished publication. Recover that operation before repairing its connection.')
        resolved = client.review_action('inspect_review_reference', {'key': link['parent']})
        parent = resolved.get('parent', {})
        if (resolved.get('requested') != link['parent'] or not parent.get('key') or parent.get('deleted')
                or parent.get('itemType') in {'attachment', 'annotation', 'note'}):
            raise PilotError('The surviving Zotero reference could not be verified.')
        if parent['key'] == link['parent']:
            raise PilotError('This connection already uses the current Zotero reference. No merge repair is needed.')
        if link.get('collection') not in parent.get('collections', []):
            raise PilotError('The surviving reference is outside its linked collection. Restore that membership in Zotero, then retry.')
        if any(cloud != identity and (other.get('parent') == parent['key']
               or keys & ({other.get('source_key'), other.get('view_key')} - {None, ''}))
               for cloud, other in links.items()):
            raise PilotError('Another tablet connection uses this reference or PDF. Keep the connections separate until their ownership is resolved.')
        desktop_original = self.state.directory / 'originals' / (link['baseline'] + '.pdf')
        tablet_original = self.state.directory / 'sources' / 'remarkable' / 'baselines' / (link['baseline'] + '.pdf')
        # Cloud-origin papers keep their clean baseline in the cloud adapter's
        # retained source store. Desktop uploads use originals/. Accept the
        # other existing store for older/relinked records, without copying it.
        originals = ([tablet_original, desktop_original] if link.get('origin') == 'cloud'
                     else [desktop_original, tablet_original])
        original = next((path for path in originals if path.is_file()), None)
        if original is None or file_sha(original) != link['baseline']:
            raise PilotError('The retained original could not be verified. Its files and connection were kept.')
        files = []
        if not keys:
            raise PilotError('This connection has no recorded Zotero PDF. Restore its PDF before repairing it.')
        for key in sorted(keys):
            record = client.get('item', key)
            if (not record or record.get('deleted') or record.get('itemType') != 'attachment'
                    or record.get('parentItem') != parent['key'] or record.get('contentType') != 'application/pdf'):
                raise PilotError('A connected PDF was removed or replaced. Restore that exact attachment in Zotero before repairing; no replacement PDF was guessed.')
            path = client.file_path(key)
            content = path.read_bytes()
            sha = hashlib.sha256(content).hexdigest()
            md5 = hashlib.md5(content).hexdigest()
            if key == link.get('view_key'):
                # This is the same ownership contract used by publish(). A
                # parent repair cannot adopt an independently reassigned PDF.
                if (record.get('linkMode') != 'imported_file'
                        or marker(bridge.namespace, identity) not in record.get('note', '')):
                    raise PilotError('The connected tablet PDF changed ownership or storage mode. Restore its managed attachment before repairing.')
                source = link.get('source_key')
                if source and source != key and ('Notable source attachment: ' + source) not in record.get('note', ''):
                    raise PilotError('The tablet view’s original source ownership changed. Restore its source attachment link before repairing.')
                expected = publication.get('md5')
                if (expected and expected != md5) or (not expected and sha != link['baseline']):
                    raise PilotError('The connected PDF changed since its last publication. Retain its annotations and recover that change before repairing.')
            elif sha != link['baseline']:
                raise PilotError('The original source PDF changed. Compare its edition before repairing this connection.')
            files.append({'key': key, 'record': record, 'sha256': sha, 'md5': md5})
        library = cloud.library()
        scope = descendants(library, self.config['tablet_root'], lambda item: item.id, lambda item: item.parent,
                            unavailable={item.id for item in library if item.deleted})
        tablet = next((item for item in library if item.id == identity), None)
        if not tablet or tablet.deleted or tablet.parent not in scope:
            raise PilotError('The connected tablet document is unavailable or outside its linked folders. No copy was uploaded.')
        bundle = cloud.pdf_bundle(tablet)
        if hashlib.sha256(bundle[identity + '.pdf']).hexdigest() != link['baseline']:
            raise PilotError('The tablet original changed. Its edition must be reviewed before reconnecting.')
        return {'identity': identity, 'connection': [client.server_id, self.config['zotero_root'], self.config['tablet_root']],
                'link': link, 'parent': parent, 'inventory': resolved['inventory'], 'files': files,
                'tablet_hash': tablet.hash, 'publication': publication,
                'paused': self.review.link_control(identity)['paused']}

    def _reconcile_merged_parent(self, identity, client):
        """Follow native merge evidence only when every owned PDF survives intact."""
        links = self.state.read('documents', {})
        old = links.get(identity, {})
        if not old.get('parent'):
            return False
        parent = client.get('item', old['parent'])
        view = client.get('item', old.get('view_key') or old.get('source_key'))
        if ((parent and not parent.get('deleted')) or not view or view.get('deleted')
                or not view.get('parentItem') or view['parentItem'] == old['parent']):
            return False
        try:
            snapshot = self._link_repair_snapshot(identity, client, merge_recovery=True)
            if snapshot['parent']['key'] != view['parentItem']:
                return False
            with self.state.lock():
                current = self._link_repair_snapshot(identity, client, merge_recovery=True)
                if fingerprint(current) != fingerprint(snapshot):
                    return False
                with self.state.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    stored = db.execute("SELECT value FROM bridge_values WHERE key='documents'").fetchone()
                    links = json.loads(stored[0]) if stored else {}
                    if links.get(identity) != snapshot['link']:
                        return False
                    links[identity] = {**links[identity], 'parent': snapshot['parent']['key'],
                        'repair_history': [*links[identity].get('repair_history', []), {'time': time.time(),
                            'action': 'Followed verified native merge', 'from': old['parent'], 'to': snapshot['parent']['key']}]}
                    db.execute("UPDATE bridge_values SET value=? WHERE key='documents'", (json.dumps(links),))
            with self.review.connect() as db:
                for row in db.execute("SELECT id,payload FROM local_deletions WHERE status='pending'").fetchall():
                    payload = json.loads(row['payload'])
                    if payload.get('cloud') == identity and payload.get('key') == old['parent']:
                        db.execute("UPDATE local_deletions SET status='resolved',action='verified_native_merge' WHERE id=?", (row['id'],))
            return True
        except (PilotError, OSError, ValueError):
            return False  # Ambiguous or changed evidence keeps the existing review.

    def preview_link_repair(self, identity):
        client = self.zotero()
        try:
            snapshot = self._link_repair_snapshot(identity, client)
            return {**self.status(), 'link_repair': {'identity': identity, 'token': fingerprint(snapshot),
                    'previous_parent': snapshot['link']['parent'], 'parent': snapshot['parent']['key'],
                    'title': snapshot['parent'].get('title') or 'Surviving Zotero reference',
                    'pdfs': [row['key'] for row in snapshot['files']], 'paused': snapshot['paused']}}
        finally:
            client.close()

    def repair_link(self, identity, token):
        client = self.zotero()
        try:
            with self.state.lock():
                snapshot = self._link_repair_snapshot(identity, client)
                if not token or fingerprint(snapshot) != token:
                    raise PilotError('The reference, PDF, notes, or tablet document changed. Review the connection repair again.')
                # Update one private ledger entry atomically. Native records, tablet
                # identity, publication receipts, baselines, and pause state stay put.
                with self.state.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    stored = db.execute("SELECT value FROM bridge_values WHERE key='documents'").fetchone()
                    links = json.loads(stored[0]) if stored else {}
                    if links.get(identity) != snapshot['link']:
                        raise PilotError('This connection changed. Review its repair again.')
                    link = {**links[identity], 'parent': snapshot['parent']['key']}
                    link['repair_history'] = [*link.get('repair_history', []), {'time': time.time(),
                        'action': 'Reconnected native merge survivor', 'from': snapshot['link']['parent'], 'to': link['parent']}]
                    links[identity] = link
                    db.execute("UPDATE bridge_values SET value=? WHERE key='documents'", (json.dumps(links),))
                return self.status()
        finally:
            client.close()

    def link(self, identity, paused):
        links = self.state.read("documents", {})
        row = links.get(identity)
        if not row:
            raise PilotError("This recorded paper link is unavailable.")
        if not paused:
            item = next((r for r in self.cloud().library() if r.id == identity), None)
            if not item or item.deleted or hashlib.sha256(self.cloud().pdf_bundle(item)[identity + ".pdf"]).hexdigest() != row["baseline"]:
                raise PilotError("The tablet original changed. Match its edition again before resuming.")
            client = self.zotero()
            try:
                parent = client.get("item", row["parent"])
                view = client.get("item", row.get("view_key") or row.get("source_key"))
                if not parent or parent.get("deleted") or not view or view.get("deleted"):
                    raise PilotError("Restore the Zotero paper before resuming this link.")
                self.review.observe_zotero_live(identity, parent)
                self.review.observe_zotero_live(identity, view)
            finally:
                client.close()
        self.review.link_control(identity, paused=paused, event={"action": "unlinked" if paused else "resumed"})
        return self.status()

    def local_delete_decision(self, identity, choice):
        client = self.zotero()
        try:
            self.review.resolve_local_deletion(identity, choice, cloud=self.cloud(), zotero=client, scope=self.config["tablet_root"])
            return self.status()
        finally:
            client.close()

    def _filename_cleanup_plans(self, client, links, identities):
        from .zotero_filenames import PLACEHOLDER, citation_filename, citation_snapshot
        from .sorting.core_logic import validate_document_filename

        maps = self.state.read('folders', {})
        suspended = self.review.suspended_documents()
        conflicts = {r['identity'] for r in self.review.rows() if r['kind'] == 'conflict'}
        catalog = client.review_action('catalog')
        attachments = {entry['data']['key']: entry for entry in catalog['attachments']}
        plans = {}
        for identity in identities:
            row = links.get(identity, {})
            tablet = row.get('name', '')
            desktop = row.get('desktop_name', '')
            parent = catalog['parents'].get(row.get('parent'))
            primary = row.get('view_key') or row.get('source_key')
            home = row.get('collection')
            if (not (PLACEHOLDER.search(tablet) or PLACEHOLDER.search(desktop))
                    or identity in suspended or identity in conflicts or row.get('filename_cleanup')
                    or row.get('unavailable') or not row.get('revision') or home not in maps
                    or not parent or parent.get('deleted') or home not in parent.get('collections', [])
                    or primary not in attachments):
                continue
            primary_entry = attachments[primary]
            if Path(primary_entry['path']).stem != desktop:
                continue
            if not PLACEHOLDER.search(tablet):
                filename, basis = tablet + '.pdf', 'Existing tablet name'
            elif not PLACEHOLDER.search(desktop):
                filename, basis = desktop + '.pdf', 'Existing Zotero filename'
            else:
                filename, basis = citation_filename(parent, self.config.get('templates', {}),
                                                     supplement=row.get('role') == 'si')
            if not filename:
                continue
            try:
                filename = validate_document_filename(filename, '.pdf')
                files = []
                for key in sorted({row.get('source_key'), row.get('view_key')} - {None, ''}):
                    entry = attachments.get(key)
                    if not entry or entry['data'].get('parentItem') != parent['key']:
                        raise ValueError('Linked PDF unavailable')
                    path = Path(entry['path'])
                    if path.suffix.lower() != '.pdf':
                        raise ValueError('Linked file is not a PDF')
                    if key == primary or PLACEHOLDER.search(path.stem):
                        if entry['data'].get('linkMode') == 'linked_file' and path.name != filename:
                            raise ValueError('External linked file')
                        stat = path.stat()
                        files.append({'key': key, 'before': path.name, 'filename': filename, 'path': str(path),
                                      'size': stat.st_size, 'mtime': stat.st_mtime_ns})
            except (OSError, ValueError):
                continue
            plan = {'identity': identity, 'parent': parent['key'], 'title': parent.get('title', ''),
                    'desktop': desktop, 'tablet': tablet, 'target': Path(filename).stem, 'basis': basis,
                    'home': home, 'tablet_parent': maps[home]['cloud'], 'citation': citation_snapshot(parent),
                    'source_key': row.get('source_key'), 'view_key': row.get('view_key'),
                    'files': files}
            plan['token'] = fingerprint([self.config.get('connection_id'), self.config.get('tablet_root'), plan])
            plans[identity] = plan
        return plans

    def preview_filename_cleanup(self):
        links = self.state.read('documents', {})
        client = self.zotero()
        try:
            plans = self._filename_cleanup_plans(client, links, links)
            return {'papers': [{**plan, 'files': [{k: entry[k] for k in ('before', 'filename')} for entry in plan['files']]}
                               for plan in sorted(plans.values(), key=lambda row: row['title'].casefold())]}
        finally:
            client.close()

    def queue_filename_cleanup(self, selected):
        if (not isinstance(selected, list) or not selected
                or any(not isinstance(row, dict) or not isinstance(row.get('identity'), str)
                       or not isinstance(row.get('token'), str) for row in selected)
                or len({row['identity'] for row in selected}) != len(selected)):
            raise PilotError('Choose one or more papers from the filename preview.')
        client = self.zotero()
        try:
            with self.state.lock():
                links = self.state.read('documents', {})
                plans = self._filename_cleanup_plans(client, links, [row['identity'] for row in selected])
                queued, skipped = [], []
                for selection in selected:
                    if self.stop.is_set():
                        raise InterruptedError('Filename cleanup paused.')
                    identity = selection['identity']
                    plan = plans.get(identity)
                    if not plan or plan['token'] != selection['token']:
                        skipped.append(identity)
                        continue
                    try:
                        for entry in plan['files']:
                            path = Path(entry.pop('path'))
                            with path.open('rb') as stream:
                                entry['md5'] = hashlib.file_digest(stream, 'md5').hexdigest()
                            stat = path.stat()
                            if stat.st_size != entry.pop('size') or stat.st_mtime_ns != entry.pop('mtime'):
                                raise ValueError('File changed')
                    except (OSError, ValueError):
                        skipped.append(identity)
                        continue
                    links[identity]['filename_cleanup'] = plan
                    queued.append(identity)
                if queued:
                    self.state.write('documents', links)
            return {'queued': queued, 'skipped': skipped}
        finally:
            client.close()

    def cancel_filename_cleanup(self, identity):
        with self.state.lock():
            links = self.state.read('documents', {})
            row = links.get(identity, {})
            pending = row.get('filename_cleanup')
            if not pending or pending.get('started'):
                raise PilotError('This rename has already started. Sync again to finish it.')
            row.pop('filename_cleanup')
            self.state.write('documents', links)
        return self.status()

    def undo_filename_cleanup(self, identity):
        from .zotero_filenames import citation_snapshot

        client = self.zotero()
        try:
            with self.state.lock():
                links = self.state.read('documents', {})
                row = links.get(identity, {})
                previous = row.get('filename_repair')
                parent = client.get('item', row['parent']) if previous else None
                if (not previous or row.get('filename_cleanup') or not parent or parent.get('deleted')
                        or any(row.get(field) != previous['target'] for field in ('name', 'desktop_name'))
                        or row.get('collection') != previous['home']
                        or any(row.get(field) != previous[field] for field in ('tablet_parent', 'source_key', 'view_key'))):
                    raise PilotError('The linked names or locations changed after this repair. Review them before renaming.')
                row['filename_cleanup'] = {**previous, 'tablet': previous['target'], 'desktop': previous['target'],
                    'target': previous['tablet'], 'desktop_target': previous['desktop'], 'undo': True,
                    'parent': parent['key'], 'citation': citation_snapshot(parent),
                    'files': [{**entry, 'before': entry['filename'], 'filename': entry['before']} for entry in previous['files']]}
                self.state.write('documents', links)
            return self.status()
        finally:
            client.close()

    def move_link(self, identity, name, collection, revision):
        from .sorting.core_logic import validate_document_filename
        name = Path(validate_document_filename(name, ".pdf")).stem
        links = self.state.read("documents", {})
        row = links.get(identity)
        maps = self.state.read("folders", {})
        if not row or collection not in maps or row.get("revision") != revision:
            raise PilotError("The selected link changed. Sync before renaming or moving it.")
        if row.get('filename_cleanup'):
            raise PilotError('Finish or cancel the queued filename change before editing this link.')
        item = next((r for r in self.cloud().library() if r.id == identity), None)
        if not item or item.deleted or item.hash != revision:
            raise PilotError("The tablet copy changed. Sync before renaming or moving it.")
        client = self.zotero()
        try:
            path = client.file_path(row["view_key"])
            if path.stem != row.get("desktop_name"):
                raise PilotError("The desktop filename changed. Sync before editing this link.")
            Bridge(client, self.cloud(), self.state, self.config["zotero_root"], self.config["tablet_root"]).set_desktop_document(row, name, collection, maps)
            self.review.link_control(identity, event={"action": "rename or move requested", "name": name})
            return self.status()
        finally:
            client.close()

    def reindex(self, key):
        if key not in {r.get("view_key") for r in self.state.read("documents", {}).values()}:
            raise PilotError("Select a linked PDF to rebuild its search index.")
        # Local search remains usable while tablet transfers are paused. A new
        # pause/cancel request can still interrupt this independent operation.
        self._begin_operation()
        client = self.zotero(ocr=True)
        try:
            path = client.file_path(key)
            before = file_sha(path)
            missing = client.index(key, path)
            if file_sha(path) != before:
                raise PilotError("The PDF changed while indexing. Refresh its link.")
            return {"pages_without_text": missing, **self.status()}
        finally:
            client.close()

    def tag_context(self, key, page=None):
        from .zotero_tags import names
        links = self.state.read('documents', {})
        matches = [(identity, row) for identity, row in links.items() if row.get('view_key') == key]
        if len(matches) != 1:
            raise PilotError('Sync and link this PDF before adding page tags.')
        identity, row = matches[0]
        if not row.get('page_map') or not row.get('page_map_digest'):
            raise PilotError('Sync this PDF to load its stable page identities.')
        client = self.zotero()
        try:
            if file_sha(client.file_path(key)) != row['page_map_digest']:
                raise PilotError('This PDF changed. Sync before editing its page tags.')
            parent = client.get('item', row['parent'])
            if not parent or parent.get('deleted'):
                raise PilotError('This reference is unavailable.')
            if page is not None and (type(page) is not int or not 0 <= page < len(row['page_map'])):
                raise PilotError('Choose an available PDF page.')
            pid = row['page_map'][page] if page is not None else None
            vocabulary = {v['tag'] for v in parent.get('tags', [])}
            for link in links.values():
                vocabulary.update(link.get('tag_state', {}).get('paper', []))
                for values in link.get('page_tags', {}).values():
                    vocabulary.update(values)
            context = {'key': key, 'cloud': identity, 'title': parent.get('title', row['name']),
                       'paper_tags': [v['tag'] for v in parent.get('tags', [])],
                       'page': page, 'page_id': pid, 'page_tags': row.get('page_tags', {}).get(pid, []),
                       'vocabulary': sorted(vocabulary), 'version': parent.get('version', 0),
                       'interrupted': bool(row.get('tag_pending')),
                       'orphaned_pages': {pid: values for pid, values in row.get('page_tags', {}).items()
                                          if pid not in row['page_map'] and values}}
            context['token'] = fingerprint(context)
            return context
        finally:
            client.close()

    def combine_tag_copies(self, key, page, token):
        """Explicit recovery: union the two current copies, keeping all labels."""
        from .zotero_tags import tablet_tags
        context = self.tag_context(key, page)
        if context['token'] != token:
            raise PilotError('The current tags changed. Reopen the page editor first.')
        links = self.state.read('documents', {})
        row = links[context['cloud']]
        cloud = self.cloud()
        item = next((r for r in cloud.library() if r.id == context['cloud']), None)
        if not item or item.deleted:
            raise PilotError('This tablet PDF is unavailable. Its tags were retained.')
        paper, remote_pages = tablet_tags(json.loads(cloud.pdf_bundle(item)[item.id + '.content']))
        local = row.get('page_tags', {})
        row['page_tags'] = {pid: sorted(set(local.get(pid, [])) | set(remote_pages.get(pid, [])))
                            for pid in set(local) | set(remote_pages)}
        client = self.zotero()
        try:
            parent = client.get('item', row['parent'])
            existing = {t['tag']: t for t in parent.get('tags', [])}
            parent['tags'] = [existing.get(t, {'tag': t}) for t in sorted(set(existing) | paper)]
            client.authorize()
            client.update(parent)
        finally:
            client.close()
        row.pop('tag_state', None)
        row.pop('tag_pending', None)
        row.pop('seen_tags', None)
        self.state.write('documents', links)
        return self.tag_context(key, page)

    def remove_unavailable_page_tags(self, key, page, page_id, token):
        context = self.tag_context(key, page)
        if context['token'] != token or page_id not in context['orphaned_pages']:
            raise PilotError('This page-tag review changed. Reopen the page editor first.')
        links = self.state.read('documents', {})
        links[context['cloud']]['page_tags'].pop(page_id)
        self.state.write('documents', links)
        return self.tag_context(key, page)

    def set_page_tags(self, key, page, page_id, tags, token, paper_tags=None):
        from .zotero_tags import names
        tags = names(tags)
        context = self.tag_context(key, page)
        if context['token'] != token or context['page_id'] != page_id:
            raise PilotError('These page tags changed. Reopen Tag this page before saving.')
        if paper_tags is not None:
            paper_tags = names(paper_tags)
            client = self.zotero()
            try:
                parent = client.get('item', self.state.read('documents', {})[context['cloud']]['parent'])
                if parent.get('version', 0) != context['version']:
                    raise PilotError('The paper tags changed. Reopen Tag this page before saving.')
                old = {v['tag']: v for v in parent.get('tags', [])}
                if set(paper_tags) != set(old):
                    parent['tags'] = [old.get(t, {'tag': t}) for t in paper_tags]
                    client.authorize()
                    client.update(parent)
            finally:
                client.close()
        links = self.state.read('documents', {})
        row = links[context['cloud']]
        row.setdefault('page_tags', {})[page_id] = tags
        self.state.write('documents', links)
        return self.tag_context(key, page)

    def tag_search(self, query='', scope='both', *, tags=None, match='all', vocabulary_only=False, results_only=False):
        if scope not in {'both', 'paper', 'page'} or not isinstance(query, str):
            raise PilotError('Choose paper tags, page tags, or both.')
        if (match not in ('all', 'any') or not isinstance(vocabulary_only, bool) or not isinstance(results_only, bool)
                or (tags is not None and (not isinstance(tags, list)
                    or any(not isinstance(tag, str) or not tag for tag in tags)))):
            raise PilotError('Choose tags and whether to match all or any of them.')
        query = query.strip().casefold()
        selected = set(tags) if tags is not None else None

        def matching(values):
            if selected is None:
                return [tag for tag in values if not query or query in tag.casefold()]
            if not selected:
                return values
            found = [tag for tag in values if tag in selected]
            if match == 'all' and not selected.issubset(found):
                return []
            return found

        client = self.zotero()
        results, paper_tags, page_tags = [], set(), set()
        try:
            links = self.state.read('documents', {})
            lookup = None
            if results_only and selected is not None and not vocabulary_only:
                views = set()
                if scope != 'paper':
                    for row in links.values():
                        page_tags_by_id = row.get('page_tags', {})
                        if row.get('view_key') and any(matching(page_tags_by_id.get(page, [])) for page in row.get('page_map', [])):
                            views.add(row['view_key'])
                lookup = {'tag_lookup': {'papers': scope != 'page', 'tags': tags, 'match': match, 'views': sorted(views)}}
            catalog = client.review_action('catalog', lookup) if lookup else client.review_action('catalog')
            parents = catalog['parents']
            attachments = {}
            for a in catalog['attachments']:
                if Path(a['path']).suffix.lower() == '.pdf':
                    attachments.setdefault(a['data'].get('parentItem'), []).append(a['data']['key'])
            for key, parent in parents.items():
                tags = [v['tag'] for v in parent.get('tags', [])]
                paper_tags.update(tags)
                if not vocabulary_only and scope != 'page' and (matched := matching(tags)):
                    results.append({'scope': 'paper', 'parent': key, 'key': next(iter(attachments.get(key, [])), ''),
                                    'title': parent.get('title', ''), 'tags': matched})
            for identity, row in links.items():
                parent = parents.get(row.get('parent'))
                if not parent or row.get('view_key') not in attachments.get(row.get('parent'), []):
                    continue
                for number, pid in enumerate(row.get('page_map', [])):
                    tags = row.get('page_tags', {}).get(pid, [])
                    page_tags.update(tags)
                    if not vocabulary_only and scope != 'paper' and (matched := matching(tags)):
                        results.append({'scope': 'page', 'parent': row['parent'], 'key': row['view_key'],
                                        'cloud': identity, 'page_id': pid, 'page': number,
                                        'title': parent.get('title', row['name']), 'tags': matched})
            if results_only:
                return {'results': results}
            return {'results': results, 'vocabulary': sorted(paper_tags | page_tags),
                    'paper_vocabulary': sorted(paper_tags), 'page_vocabulary': sorted(page_tags)}
        finally:
            client.close()

    def resolve_conflict(self, identity, digest, side):
        row = self.review.row(identity)
        if not row or row["kind"] != "conflict" or row["digest"] != digest or side not in {"desktop", "tablet"}:
            raise PilotError("This mapping review changed. Refresh before deciding.")
        if row['status'] == 'approved' and row['approval'].get('side') == side:
            return self.status()
        if row['status'] == 'applying' and row['approval'].get('side') != side:
            raise PilotError('Finish recovering the saved conflict choice before choosing another version.')
        client = self.zotero()
        try:
            payload = row["payload"]
            paper = payload["type"] == "paper"
            record = client.get("item" if paper else "collection", payload["zotero"])
            cloud = self.cloud()
            item = next((r for r in cloud.library() if r.id == payload['cloud']), None)
            if not record or record.get('deleted') or not item or item.deleted:
                raise PilotError('The selected item is unavailable. Its saved conflict choice was retained.')
            links = self.state.read("documents", {})
            link = links.get(payload["cloud"]) if paper else None
            mappings = self.state.read('folders', {})
            chosen = payload[side]
            if chosen[1] not in mappings:
                raise PilotError('The chosen destination is no longer connected. Sync before resolving this conflict.')
            if paper:
                if not link:
                    raise PilotError('The paper connection changed. Sync before resolving its conflict.')
                path = client.file_path(link.get("view_key") or link.get("source_key"))
                if hashlib.md5(path.read_bytes()).hexdigest() != payload["file_md5"]:
                    raise PilotError("The PDF changed during review. Sync before choosing.")
            intent = row['approval'].get('conflict_change') if row['status'] == 'applying' else None
            bridge = Bridge(client, cloud, self.state, self.config['zotero_root'], self.config['tablet_root'])
            if intent is None:
                # Legacy applying reviews can close an already-achieved choice
                # without replaying a mutation onto a newer revision.
                position = ([path.stem, chosen[1]] if paper and chosen[1] in record.get('collections', [])
                            else [record.get('name'), record.get('parentCollection')])
                legacy_done = (row['status'] == 'applying' and position == chosen
                               and item.name == chosen[0] and item.parent == mappings[chosen[1]]['cloud'])
                if not legacy_done and (item.hash != payload['cloud_hash'] or record.get('version', 0) != payload['version']
                                        or not paper and position != payload['desktop']):
                    raise PilotError('The folder changed since review. Sync again before choosing.')
                native = bridge.desktop_document_plan(link, chosen[0], chosen[1]) if paper else {
                    'parent': {**record, 'name': chosen[0], 'parentCollection': chosen[1]}, 'files': []}
                if side == 'desktop':
                    native['parent'] = record
                    for entry in native['files']:
                        entry['filename'] = entry['before_filename']
                intent = {'record_before': record, 'native': native,
                          'cloud': cloud_change_intent(cloud, item, chosen[0], mappings[chosen[1]]['cloud']),
                          'cloud_peer_hash': item.hash}
                if legacy_done:
                    intent['cloud']['after_hash'] = item.hash
                self.review.begin(identity, digest, {'side': side, 'conflict_change': intent})

            def native_value(value):
                return fingerprint({key: item for key, item in value.items() if key != 'version'})

            before, target = intent['record_before'], intent['native']['parent']
            if side == 'desktop':
                if record != before:
                    raise PilotError('The desktop changed while resolving this conflict. Its newer changes were retained.')
            elif native_value(record) not in {native_value(before), native_value(target)}:
                raise PilotError('The desktop changed while resolving this conflict. Its newer changes were retained.')
            native_done = native_value(record) == native_value(target)
            for entry in intent['native']['files']:
                attachment = client.get('item', entry['key'])
                path = client.file_path(entry['key'])
                name = (attachment or {}).get('filename') or path.name
                if (not attachment or attachment.get('deleted') or attachment.get('parentItem') != payload['zotero']
                        or hashlib.md5(path.read_bytes()).hexdigest() != entry['md5']
                        or name not in {entry['before_filename'], entry['filename']}):
                    raise PilotError('A desktop PDF changed while resolving this conflict. Its files were retained.')
                native_done = native_done and name == entry['filename']
            if side == "desktop":
                change = intent['cloud']
                if not cloud_change_done(cloud, item, change):
                    item = cloud.update_item(item.id, expected_hash=change['before_hash'],
                        name=change['name'], parent=change['parent'], scope_id=self.config['tablet_root'])
                change['after_hash'] = item.hash
                self.review.checkpoint(identity, conflict_change=intent)
            else:
                if item.hash != intent['cloud_peer_hash']:
                    raise PilotError('The tablet changed while resolving this conflict. Its newer changes were retained.')
                if not native_done:
                    target = {**target, 'version': record.get('version', 0)}
                    if paper:
                        bridge.set_desktop_document(link, chosen[0], chosen[1], mappings,
                            plan={**intent['native'], 'parent': target})
                    else:
                        client.update_collection(target)
            if paper:
                link.update(name=chosen[0], desktop_name=chosen[0], collection=chosen[1], tablet_parent=mappings[chosen[1]]["cloud"])
                self.state.write("documents", links)
            else:
                mappings[payload['zotero']].update(name=chosen[0], desktop_name=chosen[0], tablet_name=chosen[0], zparent=chosen[1], cparent=mappings[chosen[1]]['cloud'])
                self.state.write('folders', mappings)
            self.review.finish(identity)
            return self.status()
        finally:
            client.close()

    def dispatch(self, method, params):
        allowed = {"status", "connect", "pair", "folders", "configure", "preview_connection", "preview_disconnect", "disconnect_folder", "pause_sync", "sync", "preview", "approve", "delete_decision", "resolve_conflict", "link", "reindex", "local_delete_decision", "move_link", "scan_library", "import_library", "approve_routine", "tag_context", "set_page_tags", "tag_search", "combine_tag_copies", "remove_unavailable_page_tags"}
        allowed.update({'create_destination', 'scan_inbox', 'paper_tag_candidates', 'suggest_paper_tags', 'save_paper_tags', 'preview_keyword_cleanup', 'remove_reviewed_keywords', 'preview_filename_cleanup', 'queue_filename_cleanup', 'cancel_filename_cleanup', 'retry_bulk', 'dismiss_review', 'runtime_health', 'recheck_review', 'acknowledge_review_finding', 'preview_link_repair', 'repair_link', 'close_removed_source'})
        allowed.update({'maintenance', 'undo_filename_cleanup', 'attach_intake_pdf', 'confirm_folder_change'})
        if method not in allowed:
            raise PilotError("Unsupported plugin request.")
        if method not in {'status', 'pause_sync', 'preview_disconnect', 'runtime_health', 'scan_inbox'}:
            self._begin_operation()
        return getattr(self, method)(**params)

    def maintenance(self, action, **params):
        from . import zotero_maintenance as maintenance
        from . import zotero_state_backup as backup

        actions = {'names': maintenance.name_review, 'repair_keywords': maintenance.repair_keywords,
                   'undo_tags': maintenance.undo_keyword_repair, 'tag_history': maintenance.keyword_repairs,
                   'highlights': maintenance.highlight_review, 'compare': maintenance.compare_pdfs,
                   'annotations': maintenance.export_annotations, 'timeline': maintenance.sync_timeline,
                   'backup': backup.backup_state, 'restore': backup.restore_state}
        if action not in actions:
            raise PilotError('Choose a supported maintenance action.')
        return actions[action](self, **params)


@contextmanager
def _periodic_stack_diagnostics(path, *, interval=20, max_bytes=2 * 1024 * 1024):
    """Optional stack snapshots on an ordinary Python thread holding the GIL."""
    import traceback
    try:
        stream = Path(path).open('w', encoding='utf-8')
    except OSError:
        yield None
        return
    stopped = threading.Event()
    def emit():
        nonlocal stream
        try:
            while not stopped.wait(interval):
                # CPython gh-140815 / gh-140985: the native faulthandler
                # watchdog can traverse freed frames on the bundled 3.13.1.
                # Retained Python frames and traceback.print_stack avoid that
                # unsynchronized native stack walk; never inspect locals.
                frames = sys._current_frames()
                try:
                    if stream.tell() >= max_bytes:
                        stream.close()
                        current = Path(path)
                        previous = current.with_name(current.name + '.1')
                        if previous.exists():
                            previous.replace(current.with_name(current.name + '.2'))
                        current.replace(previous)
                        stream = current.open('w', encoding='utf-8')
                    stream.write(f'Periodic stack snapshot ({interval:g} seconds)\n')
                    for identity, frame in sorted(frames.items()):
                        stream.write(f'\nThread 0x{identity:x}:\n')
                        traceback.print_stack(frame, file=stream)
                    stream.flush()
                finally:
                    frames.clear()
                    frame = None
        except Exception:
            pass  # Optional diagnostic I/O must not interrupt sync.
        finally:
            stream.close()
    thread = threading.Thread(target=emit, name='Notable stack diagnostics', daemon=True)
    try:
        thread.start()
    except BaseException:
        stream.close()
        raise
    try:
        yield thread
    finally:
        stopped.set()
        thread.join(timeout=2)
        # A stalled filesystem may outlast this bound. The thread owns its
        # stream and closes it on exit, so shutdown never closes beneath it.


def main(argv=None, *, worker_factory=Worker):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--port", type=int, default=23119)
    parser.add_argument("--debug-stacks", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    # Gecko sends UTF-8 JSON. The console code page must not reinterpret titles,
    # user-entered tags or filenames arriving through the private stdin pipe.
    if hasattr(sys.stdin, 'reconfigure'):
        sys.stdin.reconfigure(encoding='utf-8')
    if args.debug_stacks:
        args.state.mkdir(parents=True, exist_ok=True)
    diagnostics = (_periodic_stack_diagnostics(args.state / 'test-worker-stack.log')
                   if args.debug_stacks else nullcontext())
    with diagnostics:
        return _serve(args, worker_factory)


@contextmanager
def _transport_logging():
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(previous)


@_transport_logging()
def _serve(args, worker_factory):
    import pymupdf
    # PyMuPDF caches its message/log streams at import, so redirect_stdout
    # around dispatch cannot keep its PDF diagnostics off the JSON pipe.
    pymupdf.set_messages(stream=sys.stderr)
    pymupdf.set_log(stream=sys.stderr)
    resources = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    trace = OperationTrace(Path(args.state) / "sync-trace.jsonl")
    try:
        executable_sha = file_sha(sys.executable)
    except OSError:
        executable_sha = None
    trace.event('helper_started', pid=os.getpid(), executable=Path(sys.executable).name,
                executable_sha256=executable_sha)
    try:
        worker = worker_factory(args.state, args.port, resources=resources)
    except Exception as error:
        trace.event('helper_start_failed', error_type=type(error).__name__)
        raise
    worker.trace = trace
    for ledger in (getattr(worker, 'state', None), getattr(worker, 'review', None)):
        if ledger is not None:
            ledger.trace = trace.event
    # NumPy/OpenBLAS's Windows DLL initialization inspects piped stdin. A
    # concurrent blocking stdin reader can deadlock that native import. Load
    # NumPy on the main thread before starting the transport reader, including
    # when handwriting recognition will only be requested later in the session.
    # Upstream: numpy/numpy#24290; modelcontextprotocol/python-sdk#2832.
    if sys.platform == "win32":
        try:
            import numpy  # noqa: F401
        except ModuleNotFoundError:
            pass
    pending = queue.Queue()
    shutdown_requested = threading.Event()
    def read():
        for line in sys.stdin:
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise ValueError('Request must be an object')
                if request.get("method") in {"cancel", "shutdown", "pause_sync"}:
                    worker.trace.event('control_requested', method=request['method'], request_id=request.get('id'))
                    worker.stop.set()
                    if request["method"] == "shutdown":
                        shutdown_requested.set()
                        break
                    if request['method'] == 'pause_sync':
                        try:
                            atomic_write(worker.directory / 'pause-requested.json', b'{}')
                        except OSError:
                            pass  # The queued pause reports a failed config save to the UI.
                        worker.pause_pending.set()
                        pending.put(request)
                else:
                    pending.put(request)
            except (ValueError, TypeError) as error:
                worker.trace.event('invalid_request', error_type=type(error).__name__, characters=len(line))
        worker.trace.event('stdin_closed', shutdown_requested=shutdown_requested.is_set())
        worker.stop.set()
        pending.put(None)
    threading.Thread(target=read, daemon=True).start()
    transport = sys.stdout
    packet_lock = threading.Lock()
    def send(packet):
        with packet_lock:
            write_packet(transport, packet)
    last_links = None
    quiet_request = False
    def progress(message):
        # Existing messages can contain paper titles. Log the ordinal/stage
        # and safe tablet identity instead of document metadata or text.
        tablet = re.search(r'tablet item ([a-fA-F0-9-]{36})', str(message))
        worker.trace.event("progress", message=str(message).split(':', 1)[0],
                           tablet_key=tablet.group(1) if tablet else None)
        nonlocal last_links
        packet = {'event': 'progress', 'message': message}
        if quiet_request:
            packet['quiet'] = True
        links = progress_links(worker)
        if links is not None:
            signature = json.dumps(links, sort_keys=True)
            if signature != last_links:
                packet['links'] = links
                last_links = signature
        send(packet)
    worker.progress = progress
    while True:
        request = pending.get()
        if request is None:
            break
        response = {"id": request.get("id")}
        method = str(request.get("method", ""))
        quiet_request = (method == 'suggest_paper_tags' and isinstance(request.get('params'), dict)
                         and request['params'].get('quiet') is True)
        worker.trace.start(method, request.get('trace_id'))
        worker.trace.event("request_received", method=method, request_id=request.get('id'))
        heartbeat_stop = threading.Event()
        heartbeat = None
        if request.get("method") not in {"status", "shutdown"}:
            def keep_alive():
                while not heartbeat_stop.wait(15):
                    worker.trace.event('heartbeat_sent', request_id=request.get('id'), active=worker.trace.active,
                                       active_ms=round((time.monotonic() - worker.trace.active_since) * 1000) if worker.trace.active else 0,
                                       last_completed=worker.trace.completed)
                    send({'event': 'progress', 'message': 'Still working...', **({'quiet': True} if quiet_request else {})})
            heartbeat = threading.Thread(target=keep_alive, name='Notable transport heartbeat', daemon=True)
            heartbeat.start()
        try:
            # Third-party diagnostic output must never corrupt the JSON stream.
            with redirect_stdout(sys.stderr):
                response["result"] = worker.dispatch(request.get("method"), request.get("params", {}))
        except Exception as error:
            worker.trace.event("error", type=type(error).__name__,
                               missing_module=getattr(error.__cause__ or error.__context__, 'name', None),
                               frames=[{'file': Path(frame.filename).name, 'line': frame.lineno, 'function': frame.name}
                                       for frame in traceback.extract_tb(error.__traceback__)[-16:]])
            if isinstance(error, InterruptedError):
                worker.trace.event('operation_cancelled', request_id=request.get('id'), pause=worker.pause_pending.is_set())
                worker.last = {"state": "paused", "message": "Sync stopped. Your files and pending reviews were retained."}
                response["result"] = worker.status()
                send(response)
                continue
            error_type = type(error).__name__.lower()
            if "timeout" in error_type:
                safe = "Zotero did not answer within 30 seconds. Your files were retained; retry from Notable."
            elif "transport" in error_type or "connect" in error_type:
                safe = "The connection to Zotero was interrupted. Your files were retained; retry from Notable."
            else:
                safe = str(error) if isinstance(error, (PilotError, CloudError, ValueError, FileNotFoundError)) else "The operation could not finish. Your files were retained; retry from Notable."
            if not quiet_request:
                worker.last = {"state": "needs_attention", "message": safe, "error": type(error).__name__}
            # Validation/file errors may embed document text or a private title.
            # Keep the UI response intact, but only retain their structural context.
            worker.trace.event('user_error', error_type=type(error).__name__,
                               message=None if isinstance(error, (ValueError, FileNotFoundError)) else safe,
                               message_characters=len(safe))
            response["error"] = {"message": safe, "type": type(error).__name__}
        finally:
            heartbeat_stop.set()
            if heartbeat:
                heartbeat.join(timeout=2)
            worker.trace.finish("error" if "error" in response else "completed")
        send(response)
    worker.trace.event('helper_exiting', reason='shutdown_requested' if shutdown_requested.is_set() else 'stdin_eof')


if __name__ == "__main__":
    main()
