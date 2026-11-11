"""Content-based library intake and exception-only review policy.

The source tree is read-only. Zotero owns stored copies and citation lookup;
the existing bridge remains the only code that applies tablet annotations.
"""
from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import re
import time

from .remarkable_cloud import atomic_write
from .zotero_pilot import PilotError, TransportInterrupted, stable_key
from .zotero_review import fingerprint, finding_acknowledgement_token


EXTENSIONS = {".pdf", ".docx", ".pptx"}


def digest_file(path, algorithm="sha256"):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, algorithm).hexdigest()


def observed_digest(path, cache):
    """Memoize observations only; actions still rehash before changing a file."""
    path = Path(path)
    stat = path.stat()
    signature = (str(path.resolve()), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    if signature not in cache:
        cache[signature] = digest_file(path)
    return cache[signature]


def title_key(value):
    return " ".join(re.findall(r"\w+", str(value).casefold()))


def doi_key(value):
    from .sorting.metadata_lookup import normalize_doi
    return normalize_doi(value).casefold()


def inspect_document(path, *, evidence=False):
    from .sorting.core_logic import extract_document, _basic_details_from_extraction, _document_doi_candidates
    extraction = extract_document(path)
    if path.suffix.lower() == ".pdf":
        if not extraction.page_or_section_count:
            raise PilotError("The PDF has no readable pages or needs a password.")
    details = _basic_details_from_extraction(path, extraction)
    result = {"title": details["title"], "document_type": details["document_type"],
              "dois": _document_doi_candidates(extraction),
              "supplement": details.get("is_supplementary_material", False)}
    if evidence:
        result["evidence"] = extraction.text[:24000]
    return result


def library_index(catalog, documents=None, *, cancelled=lambda: False, cache=None):
    """Hash each local attachment once per operation, including managed baselines."""
    by_digest, by_key = defaultdict(list), {}
    cache = {} if cache is None else cache
    for row in catalog["attachments"]:
        if row['data'].get('deleted'):
            continue
        if cancelled():
            raise InterruptedError("Stopped. Your files were retained.")
        path = Path(row["path"])
        try:
            digest = observed_digest(path, cache)
        except OSError:
            continue  # Unavailable Zotero files cannot establish an exact match.
        value = {**row, "digest": digest}
        by_digest[digest].append(value)
        by_key[row["data"]["key"]] = value
    for link in (documents or {}).values():
        key = link.get("view_key") or link.get("source_key")
        if key in by_key and link.get("baseline"):
            values = by_digest[link["baseline"]]
            if not any(r["data"]["key"] == key for r in values):
                values.append(by_key[key])
    return by_digest, by_key


def candidate_references(info, catalog):
    """Suggest a possible version/reference collision, never use it as PDF identity."""
    title = title_key(info.get("title", ""))
    dois = {doi_key(v) for v in info.get("dois", []) if doi_key(v)}
    return [p for p in catalog["parents"].values() if
            (doi_key(p.get("DOI", "")) in dois if doi_key(p.get("DOI", "")) else False)
            or (len(title) >= 18 and title_key(p.get("title", "")) == title)]


def ensure_collections(client, root, paths, records):
    """Reuse native collections, including case variants; persist stable IDs."""
    result = []
    for parts in sorted(set(tuple(value) for value in paths)):
        parent = root
        for part in parts:
            if not part or part in {'.', '..'} or any(c in part for c in '\r\n\x00'):
                raise PilotError('The source folder path is invalid.')
            siblings = [r for r in records.values() if r.get('parentCollection') == parent and r['name'].casefold() == part.casefold() and not r.get('deleted')]
            if len(siblings) > 1:
                raise PilotError('Several sibling collections have the same name. Resolve their folder mapping first.')
            if siblings:
                parent = siblings[0]['key']
                continue
            key = stable_key('notable-source-folders', 'collection', parent + ':' + part.casefold())
            existing = client.get('collection', key)
            if existing and (existing.get('deleted') or existing.get('parentCollection') != parent or existing['name'].casefold() != part.casefold()):
                raise PilotError('A saved folder identity belongs to a different collection.')
            record = existing or client.create('collection', {'key': key, 'name': part, 'parentCollection': parent})
            records[key] = record
            parent = key
        result.append(parent)
    return sorted(set(result))


class BulkLibrary:
    def __init__(self, worker):
        self.worker = worker
        self.path = worker.directory / "bulk-library.json"

    def read(self):
        return json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else None

    def save(self, plan):
        atomic_write(self.path, json.dumps(plan, ensure_ascii=False).encode())

    def scan(self, root, collection, lookup=True):
        worker = self.worker
        worker.stop.clear()
        root = Path(root).resolve(strict=True)
        if not root.is_dir():
            raise PilotError("Choose a literature folder.")
        client = worker.zotero()
        try:
            collections = client.collections()
            if collection not in {r["key"] for r in collections}:
                raise PilotError("Choose a current personal Zotero collection.")
            catalog = client.review_action("catalog")
            data_directory = catalog.get("data_directory")
            if data_directory:
                data = Path(data_directory).resolve()
                if root.is_relative_to(data) or data.is_relative_to(root):
                    raise PilotError("Choose your literature folder, outside Zotero's data directory.")
            indexed, _ = library_index(catalog, worker.state.read("documents", {}), cancelled=worker.stop.is_set, cache=worker.file_digests)
            grouped, failures = {}, []
            paths = []
            worker.progress('Finding documents in your Windows folder…')
            for directory, folders, names in os.walk(root, followlinks=False):
                folders[:] = [name for name in sorted(folders) if not Path(directory, name).is_symlink()
                              and not Path(directory, name).is_junction()]
                for name in sorted(names):
                    if worker.stop.is_set():
                        raise PilotError("Scan stopped. No library changes were made.")
                    path = Path(directory, name)
                    if path.suffix.lower() not in EXTENSIONS or name.startswith("~$"):
                        continue
                    if path.is_symlink() or not path.resolve().is_relative_to(root):
                        continue
                    paths.append(path)
            for number, path in enumerate(paths, 1):
                if worker.stop.is_set():
                    raise PilotError('Scan stopped. No library changes were made.')
                worker.progress(f'Scanning file {number} of {len(paths)}: {path.name}')
                try:
                    before = path.stat()
                    digest = digest_file(path)
                    if (before.st_size, before.st_mtime_ns) != (path.stat().st_size, path.stat().st_mtime_ns):
                        raise PilotError("The file changed during the scan.")
                    if digest in grouped:
                        grouped[digest]["copies"].append(str(path))
                        continue
                    row = {"id": digest[:24], "digest": digest, "path": str(path), "copies": [str(path)],
                           "issues": [], "status": "pending", "existing": "", "parent": ""}
                    grouped[digest] = row
                    try:
                        row["info"] = inspect_document(path)
                    except Exception:
                        row["info"] = {"title": path.stem, "dois": []}
                        row["issues"].append("The file could not be read completely; inspect it before import.")
                    matches = indexed.get(digest, [])
                    if len(matches) == 1:
                        row["existing"] = matches[0]["data"]["key"]
                        row["parent"] = matches[0]["data"].get("parentItem", "")
                    elif matches:
                        row["issues"].append("Identical content belongs to multiple Zotero attachments; choose which reference to keep.")
                    else:
                        candidates = candidate_references(row["info"], catalog)
                        if candidates:
                            row["issues"].append("A possibly matching reference already exists; its PDF or edition needs checking.")
                            row["candidates"] = [{"key": p["key"], "title": p.get("title", "")} for p in candidates]
                    if not row["existing"] and (row["info"].get("supplement") or re.search(r"(?:^|[_ -])(?:si|supporting[ _-]?information|supplement(?:ary)?)(?:$|[_ -])", path.stem, re.I)):
                        row["issues"].append("Supporting information needs its main reference identified.")
                except (OSError, PilotError) as error:
                    failures.append({"path": str(path), "reason": str(error)})
            # Different bytes with the same filename or document title can be
            # editions or annotated copies. Preserve both and ask, not overwrite.
            names = defaultdict(set)
            for row in grouped.values():
                for key in ("name:" + title_key(Path(row["path"]).stem), "title:" + title_key(row["info"].get("title", ""))):
                    if len(key) >= 18:
                        names[key].add(row["digest"])
            collisions = set().union(*(values for values in names.values() if len(values) > 1)) if names else set()
            for digest in collisions:
                grouped[digest]["issues"].append("Another source file has the same name or title but different content; check the versions.")
            plan = {"id": fingerprint([str(root), collection, time.time_ns()]), "root": str(root),
                    'server_id': worker.config.get('server_id', ''),
                    "collection": collection, "lookup": bool(lookup), "rows": list(grouped.values()), "failures": failures}
            self.save(plan)
            worker.collections = collections
            return worker.status()
        finally:
            client.close()

    def retry(self, plan_id, row_id, *, dismiss=False, root=None, collection=None, lookup=None):
        plan = self.read()
        if not plan or plan['id'] != plan_id:
            raise PilotError('The folder scan changed. Review its current summary first.')
        row = next((row for row in plan['rows'] if row['id'] == row_id), None)
        if not row:
            raise PilotError('This import exception is no longer in the current scan.')
        if dismiss:
            row['status'] = 'dismissed'
            self.save(plan)
            return self.worker.status()
        if ((root is not None and Path(root).resolve() != Path(plan['root']).resolve())
                or (collection is not None and collection != plan['collection'])
                or (lookup is not None and bool(lookup) != plan['lookup'])):
            raise PilotError('The import settings changed. Scan again before retrying with these settings.')
        if plan.get('invalidated') or plan.get('server_id', '') != self.worker.config.get('server_id', ''):
            raise PilotError('The Zotero instance changed. Scan the folder again before importing.')
        # Reuse the existing scanner's current content/collision policy; an
        # exception cannot bypass identity checks just because Retry was clicked.
        self.scan(plan['root'], plan['collection'], plan['lookup'])
        refreshed = self.read()
        selected = next((row for row in refreshed['rows'] if row['id'] == row_id), None)
        if selected and not selected['issues']:
            return self.apply(refreshed['id'], row_id=row_id)
        self.worker.last = {'state': 'needs_review', 'message': 'Rechecked this file. Resolve its current exceptions before importing.'}
        return self.worker.status()

    def apply(self, plan_id, *, root=None, collection=None, lookup=None, row_id=None):
        worker = self.worker
        plan = self.read()
        if not plan or plan["id"] != plan_id:
            raise PilotError("The folder scan changed. Review its current summary first.")
        if plan.get('invalidated') or plan.get('server_id', '') != worker.config.get('server_id', ''):
            raise PilotError('The Zotero instance changed. Scan the folder again before importing.')
        if ((root is not None and Path(root).resolve() != Path(plan['root']).resolve())
                or (collection is not None and collection != plan['collection'])
                or (lookup is not None and bool(lookup) != plan['lookup'])):
            raise PilotError('The import settings changed. Scan again before importing with these settings.')
        worker.stop.clear()
        client = worker.zotero()
        try:
            catalog = client.review_action("catalog")
            indexed, _ = library_index(catalog, worker.state.read("documents", {}), cancelled=worker.stop.is_set)
            collection_records = {r['key']: r for r in client.collections()}
            eligible = [row for row in plan['rows'] if not row['issues'] and row['status'] not in {'complete', 'dismissed'}
                        and (row_id is None or row['id'] == row_id)]
            for number, row in enumerate(eligible, 1):
                if worker.stop.is_set():
                    break
                worker.progress(f'Importing / reusing document {number} of {len(eligible)}…')
                path = Path(row["path"])
                try:
                    if not path.resolve(strict=True).is_relative_to(Path(plan["root"])) or digest_file(path) != row["digest"]:
                        raise PilotError("The source changed after scanning. Scan again before importing it.")
                    source_root = Path(plan['root']).resolve()
                    relatives = []
                    for copy in row['copies']:
                        copied = Path(copy).resolve(strict=True)
                        if not copied.is_relative_to(source_root) or digest_file(copied) != row['digest']:
                            raise PilotError('A source copy changed after scanning. Scan again before preserving its folder.')
                        relatives.append(copied.relative_to(source_root).parent.parts)
                    homes = ensure_collections(client, plan['collection'], relatives, collection_records)
                    home = homes[0]
                    matches = indexed.get(row["digest"], [])
                    if len(matches) > 1:
                        raise PilotError("An identical PDF now belongs to several Zotero attachments.")
                    existing = matches[0] if matches else None
                    if row["existing"] and (not existing or existing["data"]["key"] != row["existing"]):
                        raise PilotError("The matching Zotero attachment changed after scanning.")
                    if existing:
                        parent_key = existing['data'].get('parentItem')
                        parent = catalog['parents'].get(parent_key, {})
                        if parent_key:
                            # Already stored and filed: no native import or
                            # citation lookup. Retry idempotent keywords, whose
                            # earlier write may have been interrupted. Reuse a managed view
                            # whose private original matches this source digest.
                            worker.apply_document_keywords(client, parent_key, Path(existing['path']), digest=existing['digest'])
                            parent = client.get('item', parent_key)
                            wanted = sorted(set(parent.get('collections', [])) | set(homes))
                            if sorted(parent.get('collections', [])) != wanted:
                                parent['collections'] = wanted
                                client.authorize(); client.update(parent)
                            row.update(status='complete', attachment=existing['data']['key'], parent=parent_key)
                            if number % 20 == 0:
                                self.save(plan)
                            continue
                    if not existing and candidate_references(row["info"], catalog):
                        raise PilotError("A matching citation was added after scanning; check its existing attachment.")
                    info = inspect_document(path, evidence=True)
                    row["status"] = "applying"
                    self.save(plan)
                    result = client.review_action("bulk_import", {"source": str(path), "root": plan["root"],
                        "digest": row["digest"], "md5": digest_file(path, "md5"), "collection": home,
                        "existing": existing["data"]["key"] if existing else "",
                        "existing_md5": digest_file(existing["path"], "md5") if existing else "",
                        "lookup": plan["lookup"], "dois": info.get("dois", []), "evidence": info.get("evidence", ""),
                        "title": info["title"] or path.stem})
                    deadline = time.monotonic() + 120
                    while result.get("pending"):
                        if worker.stop.wait(.2):
                            raise InterruptedError("Stopped. Zotero may finish the current file; retry to resume without duplicates.")
                        if time.monotonic() > deadline:
                            raise TransportInterrupted("Zotero is still processing this file. The batch is saved; retry to resume without duplicates.")
                        result = client.review_action("bulk_import_status", {"digest": row["digest"], "collection": home})
                    if result.get("error"):
                        raise PilotError(result["error"])
                    if result.get("issues"):
                        row["issues"].extend(result["issues"])
                    row.update({'attachment': result['attachment'], 'parent': result['parent']})
                    result['reference'] = worker.apply_document_keywords(client, result['parent'], Path(result['path']), digest=row['digest'])
                    reference = client.get('item', result['parent'])
                    wanted = sorted(set(reference.get('collections', [])) | set(homes))
                    if sorted(reference.get('collections', [])) != wanted:
                        reference['collections'] = wanted
                        client.authorize(); client.update(reference)
                    result['reference'] = reference
                    value = {"data": result["data"], "path": result["path"], "digest": row["digest"]}
                    indexed[row["digest"]] = [value]
                    catalog["attachments"].append(value)
                    catalog["parents"][result["parent"]] = result["reference"]
                    row['status'] = 'complete'
                except InterruptedError:
                    row['status'] = 'pending'
                    self.save(plan)
                    worker.last = {'state': 'paused', 'message': f'Import paused. {sum(r["status"] == "complete" for r in eligible)} of {len(eligible)} selected documents completed; retry to resume.'}
                    return worker.status()
                except TransportInterrupted as error:
                    self.save(plan)
                    worker.last = {'state': 'waiting', 'message': str(error)}
                    return worker.status()
                except Exception as error:
                    # The native import may have completed before a transport
                    # interruption. A rescan detects its actual stored bytes.
                    row["status"] = "pending"
                    row["issues"].append(str(error))
                self.save(plan)
            self.save(plan)
            if any(row['kind'] == 'cloud' for row in worker.review.rows()):
                # Import changes which tablet documents already have a Zotero
                # counterpart; do not leave the visible review snapshot stale.
                worker.refresh_review_policy(client, catalog)
            completed = sum(row['status'] == 'complete' for row in eligible)
            held = sum(bool(row['issues']) for row in plan['rows'] if row['status'] != 'dismissed')
            paused = worker.stop.is_set()
            worker.last = {'state': 'paused' if paused else 'needs_review' if held else 'ready',
                           'message': (f'Import paused. {completed} of {len(eligible)} selected documents completed; retry to resume.' if paused else
                                       f'Bulk import saved: {completed} selected documents completed; {held} held for review. Original Windows files remain in place.')}
            return worker.status()
        except InterruptedError:
            worker.last = {'state': 'paused', 'message': 'Import paused. The batch and original files were retained; retry to resume.'}
            return worker.status()
        finally:
            client.close()


def review_policy(rows, catalog, documents, mappings, *, cancelled=lambda: False, cache=None, findings=None,
                  acknowledgements=()):
    """Only material identity/file problems hold up a routine batch.

    No missing author, year, DOI, journal, or low OCR confidence rule appears
    here. Citation quality remains visible in Zotero and is not a reading gate.

    ``findings`` optionally receives navigation/evidence records for the same
    decisions. Only explicitly acknowledged, unchanged library overlap findings
    are removed from the blocking issues; unrelated filing and repair checks
    remain in force. The acknowledged guidance remains visible.
    """
    indexed, by_key = library_index(catalog, documents, cancelled=cancelled, cache=cache)
    attachment_catalog = {row['data']['key']: row for row in catalog['attachments'] if not row['data'].get('deleted')}
    acknowledged = set(acknowledgements)

    def arrangement_evidence(reference_keys, values, *, matched_digest=None):
        """Keep identity/content/ownership changes separate from cosmetic state.

        Reuse the operation's existing content index and native catalog. Tags,
        read state, collection membership, display filenames and UI prose do
        not establish whether two references or PDF copies are distinct.
        """
        references = []
        for key in sorted(set(filter(None, reference_keys))):
            record = catalog['parents'].get(key, {})
            relations = record.get('relations', {})
            references.append({'key': key, 'title': title_key(record.get('title', '')),
                'DOI': doi_key(record.get('DOI', '')), 'itemType': record.get('itemType', ''),
                'date': record.get('date', ''), 'edition': record.get('edition', ''),
                'versionNumber': record.get('versionNumber', ''),
                'relations': {relation: sorted([targets] if isinstance(targets, str) else targets)
                              for relation, targets in sorted(relations.items())}})
        files = {}
        for value in values:
            data = value['data']
            observed = by_key.get(data['key'], {}).get('digest')
            files[data['key']] = {'key': data['key'], 'parent': data.get('parentItem', ''),
                                 'digest': observed, 'linkMode': data.get('linkMode', '')}
            if matched_digest is not None:
                files[data['key']]['match_basis'] = 'current_bytes' if observed == matched_digest else 'original_baseline'
        return {'references': references, 'attachments': [files[key] for key in sorted(files)],
                **({'matched_digest': matched_digest} if matched_digest is not None else {})}

    def references(keys):
        return [{'key': key, **{field: catalog['parents'][key].get(field, '')
                               for field in ('title', 'itemType', 'date', 'DOI')}}
                for key in sorted(set(filter(None, keys))) if key in catalog['parents']]

    def attachments(values, digest=None):
        result = []
        for value in values:
            record = value['data']
            result.append({'key': record['key'], 'parent': record.get('parentItem', ''),
                           'title': record.get('title', ''),
                           'filename': record.get('filename') or Path(value['path']).name,
                           'match_basis': ('current_bytes' if value.get('digest') == digest else 'original_baseline')
                           if digest is not None else 'context'})
        return sorted({row['key']: row for row in result}.values(), key=lambda row: row['key'])

    outcomes = {}
    for row in rows:
        payload, issues = row["payload"], []
        structured = {}
        parent = payload.get('parent', '')
        current = attachment_catalog.get(payload.get('attachment') or (row['identity'] if row['kind'] == 'zotero' else ''))
        current_references = references([parent])
        current_attachments = attachments([current]) if current else []

        def report(message, code, domain, severity, summary, explanation, next_step, *,
                   evidence=(), refs=None, files=None, collections=(), arrangement=None):
            decision = None
            if arrangement is not None:
                token = finding_acknowledgement_token(code, arrangement)
                decision = {'eligible': True, 'token': token, 'acknowledged': token in acknowledged}
            if not decision or not decision['acknowledged']:
                issues.append(message)
            finding = {'code': code, 'domain': domain, 'severity': severity,
                'summary': summary, 'explanation': explanation, 'next_step': next_step,
                'evidence': list(evidence), 'references': current_references if refs is None else refs,
                'attachments': current_attachments if files is None else files, 'collections': list(collections)}
            if decision is not None:
                finding['acknowledgement'] = decision
            structured.setdefault(code, finding)

        if payload.get('repair_required'):
            report(payload['repair_required'], 'repair_required', 'file', 'repair',
                   'This filing needs recovery before it can continue', payload['repair_required'],
                   'Review the reported recovery condition before approving. Keep the existing files and notes.')
        if row["kind"] not in {"zotero", "inbox", "cloud"}:
            # The historical policy reports only the mapping conflict here.
            issues, structured = [], {}
            report('A linked document or folder has conflicting changes.', 'mapping_conflict', 'sync', 'decision',
                   'Names or locations changed independently',
                   'Both sides changed since the saved connection state. The plugin needs an explicit choice.',
                   'Compare the current names and locations before applying the Zotero or tablet choice.',
                   refs=references([payload.get('zotero')]) if payload.get('type') == 'paper' else [])
            outcomes[row['id']] = issues
            if findings is not None:
                findings[row['id']] = list(structured.values())
            continue
        path = Path(payload["path"])
        if not path.is_file():
            report('The local file is unavailable.', 'local_file_unavailable', 'file', 'repair',
                   'The saved file is not available locally',
                   'The plugin cannot read the saved source path. This does not prove that the paper was deleted.',
                   'Show the attachment in Zotero and locate or restore its file. Then return and use Sync now to recheck.')
        # A failed extraction is different from a readable image-only PDF.
        for warning in payload.get("details", {}).get("review_reasons", []):
            if any(term in warning.casefold() for term in ("could not read", "could not extract metadata", "encrypted", "password")):
                report('The file needs a readability check.', 'readability_check', 'file', 'repair',
                       'The file could not be read reliably',
                       'Extraction reported a reading, encryption or password problem. An image-only PDF alone is not a conflict.',
                       'Open the file and verify that it is readable and unlocked before retrying.')
        matches = payload.get("matches", [])
        if not matches and payload.get("possible_tablet_matches"):
            report('A tablet PDF has the same name but different content; check the editions before uploading another copy.',
                   'tablet_name_content_mismatch', 'sync', 'decision',
                   'A similarly named tablet PDF has different content',
                   'A matching name does not establish that these are the same edition or attachment.',
                   'Compare the PDFs before linking them or creating another tablet copy.',
                   evidence=['The names match, but the PDF content comparison did not.'])
        if len(matches) > 1:
            native_matches = [attachment_catalog[match['key']] for match in matches if match.get('key') in attachment_catalog]
            report('More than one identical counterpart exists; choose the intended copy.',
                   'multiple_identical_counterparts', 'filing', 'decision',
                   'Several matching counterparts are available',
                   'The existing content comparison found several candidates. Their references, locations or annotation histories may differ.',
                   'Inspect the candidates and choose the existing copy that should be linked.',
                   evidence=[f'{len(matches)} matching counterparts were found.'],
                   refs=references([parent] + [value['data'].get('parentItem') for value in native_matches]),
                   files=attachments(native_matches) if native_matches else current_attachments)
        if len(indexed.get(row["digest"], [])) > 1:
            repeated = indexed[row['digest']]
            baseline_match = any(value['digest'] != row['digest'] for value in repeated)
            report('Identical content belongs to several Zotero attachments.',
                   'repeated_pdf_content', 'library', 'decision',
                   'The same original PDF is associated with several attachments' if baseline_match
                   else 'The same file bytes occur in several Zotero attachments',
                   ('At least one match uses a retained original PDF baseline; current annotated files may differ. '
                    if baseline_match else 'The current local files have matching content hashes. ')
                   + 'Matching PDF content does not establish that the references, notes or Zotero annotations are interchangeable.',
                   'Inspect the exact references and attachments in Zotero. Merge true duplicate references there, or retain intentional copies.',
                   evidence=['Matched retained original PDF content; current annotations may differ.' if baseline_match
                             else 'Matched the current local file bytes.'],
                   refs=references([value['data'].get('parentItem') for value in repeated]),
                   files=attachments(repeated, row['digest']),
                   arrangement=arrangement_evidence([value['data'].get('parentItem') for value in repeated],
                                                     repeated, matched_digest=row['digest']))
        details = payload.get("details", {})
        if details.get('filename_needs_review'):
            report('Choose a readable name for this PDF before filing it.', 'readable_filename_needed', 'filing', 'decision',
                   'Choose a readable filename',
                   'The source has no useful name for filing. Complete citation fields are not required.',
                   'Enter a readable filename below, then check its destination.')
        if details.get("is_supplementary_material") or re.search(r"(?:^|[_ -])si$", path.stem, re.I):
            report("Confirm the supporting information's main reference before sending it.",
                   'supporting_information_parent', 'filing', 'decision',
                   'Confirm the main reference for this supplement',
                   'Supporting information is a valid attachment arrangement. The plugin needs to know which paper it belongs to.',
                   'Inspect the attachment in Zotero, then select Supporting Information and its main reference below.')
        if parent:
            record = catalog["parents"].get(parent, {})
            duplicate_parents = [p for p in candidate_references({"title": record.get("title", ""), "dois": [record.get("DOI", "")]}, catalog) if p["key"] != parent]
            if duplicate_parents:
                evidence = []
                doi = doi_key(record.get('DOI', ''))
                if doi and any(doi_key(other.get('DOI', '')) == doi for other in duplicate_parents):
                    evidence.append('Matching DOI: ' + doi)
                title = title_key(record.get('title', ''))
                if len(title) >= 18 and any(title_key(other.get('title', '')) == title for other in duplicate_parents):
                    evidence.append('Matching normalized reference titles.')
                report('Another Zotero reference may describe the same work; check the references or versions.',
                       'possible_duplicate_references', 'library', 'decision',
                       'Several references may describe the same publication',
                       'Matching citation fields suggest candidates, not proof that distinct editions or related works should be merged.',
                       'Show these references in Zotero. Use its native merge workflow for true duplicates, or keep distinct works separate.',
                       evidence=evidence, refs=references([parent] + [other['key'] for other in duplicate_parents]),
                       arrangement=arrangement_evidence([parent] + [other['key'] for other in duplicate_parents],
                           [value for value in catalog['attachments']
                            if value['data'].get('parentItem') in {parent, *(other['key'] for other in duplicate_parents)}]))
            homes = set(record.get("collections", [])) & set(mappings)
            if len(homes) > 1:
                report('This reference has several tablet destinations; choose one home.',
                       'multiple_tablet_destinations', 'sync', 'decision',
                       'Choose one tablet home for this paper',
                       'Multiple Zotero collection memberships are valid. More than one of these collections is connected to a tablet destination.',
                       'Choose one linked destination below. Other research collection memberships can remain.',
                       evidence=[f'{len(homes)} collection memberships lead to connected tablet destinations.'],
                       collections=[{'key': key, 'name': mappings[key].get('name', '')} for key in sorted(homes)])
            existing = [value for value in catalog['attachments']
                        if value['data'].get('parentItem') == parent and not value['data'].get('deleted')
                        and Path(value['path']).suffix.lower() == '.pdf']
            # A deleted queued source is history, not an extra current PDF.
            # A cloud/intake source may still need a role choice before adding
            # its new PDF alongside an existing native attachment.
            incoming = row['kind'] in {'cloud', 'inbox'}
            if path.suffix.lower() == '.pdf' and (len(existing) > 1 or (incoming and existing)):
                existing_pdfs = attachments(existing)
                report('This reference has multiple PDFs; choose its original or supporting file before linking.',
                       'multiple_pdf_attachments', 'library', 'decision',
                       'Identify the role of this PDF among the reference attachments',
                       'Several PDFs under one reference may be a valid main paper, supplement, or manuscript arrangement. Multiple PDFs alone do not prove a duplicate.',
                       'Inspect the attachments in Zotero and confirm which PDF and role should be linked.',
                       evidence=[f"{len(existing_pdfs)} existing PDF attachment{'s' if len(existing_pdfs) != 1 else ''} under this reference."],
                       files=existing_pdfs)
        if row["kind"] == "zotero" and row["identity"] in by_key and by_key[row["identity"]]["data"].get("linkMode") == "linked_file":
            report('This PDF is linked to an external Windows file; store a Zotero copy before tablet updates.',
                   'external_linked_file', 'file', 'repair',
                   'This attachment points to an external file',
                   'Zotero stores a link to a file outside its managed storage. Tablet updates require a supported stored attachment.',
                   'Show this attachment in Zotero and use its native conversion to a stored file before retrying.')
        outcomes[row["id"]] = list(dict.fromkeys(issues))
        if findings is not None:
            findings[row['id']] = list(structured.values())
    return outcomes
