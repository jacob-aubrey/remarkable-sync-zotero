"""A scoped, additive Zotero/reMarkable bridge built on existing adapters.

Collection/PDF additions travel both ways. Only tablet revisions update managed
annotated PDFs. Moves, deletions, and changed desktop originals require review.
The bridge never guesses paper identity from titles, authors or DOI.
"""
from collections import Counter
from contextlib import contextmanager
import hashlib
import json
import re
from types import SimpleNamespace
import uuid
from zipfile import BadZipFile

import pymupdf

from .remarkable_cloud import CloudItem, CloudError, atomic_write
from .remarkable_pdf import annotated_pdf, RENDER_REVISION, UnsupportedAnnotations
from .remarkable_sources import save_source_bundle, source_bundle, source_info
from .zotero_pilot import (AuthorizationRequired, Ledger, Paper, PilotError,
                           TransportInterrupted, publish, stable_key, marker)


class BridgeState(Ledger):
    def __init__(self, directory):
        super().__init__(directory)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS bridge_values (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    def read(self, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM bridge_values WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def write(self, key, value):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO bridge_values VALUES(?,?)", (key, json.dumps(value)))


def descendants(records, root, key, parent, unavailable=()):
    """Validate ancestry before any write; no recursive loops or truncated trees."""
    by_id = {key(row): row for row in records}
    if len(by_id) != len(records) or root not in by_id or root in unavailable:
        raise PilotError("The selected sync root is missing or its catalog is ambiguous.")
    result = {}
    for identity, row in by_id.items():
        seen, cursor, depth = set(), identity, 0
        while cursor:
            if cursor in unavailable:
                break  # Retain deleted ancestry, but never include its subtree.
            if cursor in seen:
                raise PilotError("A folder catalog contains a cycle. No synchronization was attempted.")
            seen.add(cursor)
            if cursor == root:
                result[identity] = (row, depth)
                break
            ancestor = by_id.get(cursor)
            if ancestor is None:
                raise PilotError("A folder catalog is incomplete. Retry after synchronization finishes.")
            cursor, depth = parent(ancestor) or "", depth + 1
            if cursor == "trash":
                break  # Tablet trash is a virtual container outside every root.
    return result


def sync_collections(client, mappings, collections=None):
    collections = list(client.collections() if collections is None else collections)
    active = {row['key'] for row in collections}
    for key in set(mappings) - active:
        if not mappings[key].get('pending'):
            row = client.get('collection', key)
            if row and row.get('deleted'):
                collections.append(row)
    return collections


def sync_folder_scope(collections, catalog, zroot, croot, mappings, exclusions, confirmed=None):
    from .zotero_connections import filtered_scope
    from .zotero_review import fingerprint

    folders = [item for item in catalog if item.kind == 'CollectionType']
    zkey, zparent = lambda row: row['key'], lambda row: row.get('parentCollection')
    ckey, cparent = lambda row: row.id, lambda row: row.parent
    deleted_z = {zkey(row) for row in collections if row.get('deleted')}
    deleted_c = {ckey(row) for row in folders if row.deleted or row.parent == 'trash'}
    zscope = filtered_scope(descendants(collections, zroot, zkey, zparent, deleted_z), exclusions['zotero'], zkey, zparent, zroot)
    cscope = filtered_scope(descendants(folders, croot, ckey, cparent, deleted_c), exclusions['tablet'], ckey, cparent, croot)
    if mappings.get(zroot, {}).get('cloud', croot) != croot:
        raise PilotError('The saved root folder mapping does not match Setup. Check the connected folders.')
    reverse = {link['cloud']: key for key, link in mappings.items()}
    if len(reverse) != len(mappings):
        raise PilotError('More than one collection maps to the same tablet folder.')
    unavailable = {key for key, link in mappings.items() if key != zroot and (
        (key not in zscope and link.get('pending') != 'zotero')
        or (link['cloud'] not in cscope and link.get('pending') != 'cloud'))}
    names_z = {zkey(row): row['name'] for row in collections}
    names_c = {ckey(row): row.name for row in folders}
    deleted_collection = {zkey(row): row for row in collections if zkey(row) in deleted_z}
    deleted_folder = {ckey(row): row for row in folders if ckey(row) in deleted_c}

    def exclude_branches(blocked):
        while True:
            tablet_blocked = {mappings[key]['cloud'] for key in blocked}
            reserved_z = {(mappings[key].get('zparent'), names_z.get(key, mappings[key].get('name'))) for key in blocked}
            reserved_c = {(mappings[key].get('cparent'), names_c.get(mappings[key]['cloud'], mappings[key].get('name')))
                          for key in blocked}
            # A new folder with the same name is not the deleted folder. Keep
            # it out of automatic pairing while the saved names are reserved.
            exclude_z = blocked | {key for key, (row, _) in zscope.items() if key not in mappings
                                   and (zparent(row), row['name']) in reserved_z}
            exclude_c = tablet_blocked | {key for key, (row, _) in cscope.items() if key not in reverse
                                          and (cparent(row), row.name) in reserved_c}
            desktop = filtered_scope(zscope, exclude_z, zkey, zparent, zroot)
            tablet = filtered_scope(cscope, exclude_c, ckey, cparent, croot)
            more = {key for key, link in mappings.items() if key != zroot and (
                link.get('zparent') in blocked or link.get('cparent') in tablet_blocked
                or (key in zscope and key not in desktop) or (link['cloud'] in cscope and link['cloud'] not in tablet))}
            if more <= blocked:
                return blocked, tablet_blocked, desktop, tablet
            blocked.update(more)

    confirmed = confirmed or {}
    retired, _, _, _ = exclude_branches({key for key, ck in confirmed.items() if key != zroot and reverse.get(ck) == key})
    blocked, tablet_blocked, desktop, tablet = exclude_branches(unavailable | retired)
    issues = []
    for key, link in mappings.items():
        ck = link['cloud']
        if key not in unavailable or key in retired or link.get('zparent') in blocked or link.get('cparent') in tablet_blocked:
            continue
        issue = {'zotero': key, 'tablet': ck,
            'zotero_name': names_z.get(key, link.get('desktop_name', link.get('name', 'Zotero collection'))),
            'tablet_name': names_c.get(ck, link.get('tablet_name', link.get('name', 'reMarkable folder'))),
            'zotero_state': ('trash' if key in deleted_z else 'outside' if key in names_z else 'missing') if key not in zscope else '',
            'tablet_state': ('trash' if ck in deleted_c else 'outside' if ck in names_c else 'missing') if ck not in cscope else ''}
        if issue['tablet_state'] in {'trash', 'missing'} and not issue['zotero_state']:
            inventory = zotero_folder_inventory(collections, key)
            if not any(row[0] in exclusions['zotero'] for row in inventory):
                issue['mirror'] = {'side': 'zotero', 'evidence': fingerprint(inventory), 'children': len(inventory) - 1}
        if issue['zotero_state'] in {'trash', 'missing'} and not issue['tablet_state']:
            inventory = tablet_folder_inventory(catalog, ck)
            if not any(row[0] in exclusions['tablet'] or row[0] in exclusions['documents'] for row in inventory):
                issue['mirror'] = {'side': 'tablet', 'evidence': fingerprint(inventory), 'children': len(inventory) - 1}
        if issue['tablet_state'] == 'trash' and not issue['zotero_state'] and link.get('cparent') in tablet:
            issue['undo'] = {'side': 'tablet', 'parent': link['cparent'], 'version': deleted_folder[ck].hash}
        if issue['zotero_state'] == 'trash' and not issue['tablet_state']:
            row = deleted_collection[key]
            if row.get('parentCollection') in desktop:
                issue['undo'] = {'side': 'zotero', 'parent': row['parentCollection'], 'version': row.get('version', 0)}
        issues.append(issue)
    return desktop, tablet, {'issues': issues, 'confirmed': confirmed, 'zotero': sorted(blocked | (set(zscope) - set(desktop))),
                             'tablet': sorted(tablet_blocked | (set(cscope) - set(tablet)))}


def folder_blocked_papers(documents, catalog, blocked):
    zotero, tablet = set(blocked['zotero']), set(blocked['tablet'])
    return ({key for key, row in documents.items() if row.get('collection') in zotero or row.get('tablet_parent') in tablet}
            | {item.id for item in catalog if item.id in documents and item.parent in tablet})


def _folder_inventory(rows, root):
    from collections import defaultdict
    by_parent, by_key = defaultdict(list), {}
    for row in rows:
        by_key[row[0]] = row
        by_parent[row[1]].append(row[0])
    seen, pending = set(), [root] if root in by_key else []
    while pending:
        key = pending.pop()
        if key not in seen:
            seen.add(key)
            pending.extend(by_parent[key])
    return [by_key[key] for key in sorted(seen)]


def zotero_folder_inventory(collections, root):
    return _folder_inventory(([row['key'], row.get('parentCollection') or '', row['name'], row.get('version', 0)]
                              for row in collections if not row.get('deleted')), root)


def tablet_folder_inventory(catalog, root):
    return _folder_inventory(([item.id, item.parent, item.name, item.hash]
                              for item in catalog if not item.deleted and item.parent != 'trash'), root)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def annotation_render_id(fingerprint, *, group_highlights=False, handwriting_cards=False, approved_highlight_groups=()):
    identity = [fingerprint, RENDER_REVISION]
    if approved_highlight_groups:
        identity.append("approved-highlights-1:" + _sha(json.dumps(approved_highlight_groups).encode()))
    elif group_highlights:
        identity.append("grouped-highlights-1")
    if handwriting_cards:
        identity.append("handwriting-cards-1")
    return identity


class Bridge:
    def __init__(self, zotero, cloud, state, zotero_root, cloud_root, *, saved_annotations=None, review_policy=None, follow_changes=False, progress=None, exclusions=None, file_digests=None):
        self.file_digests = file_digests if file_digests is not None else {}
        self.zot, self.cloud, self.state = zotero, cloud, state
        self.zroot, self.croot = zotero_root, str(uuid.UUID(cloud_root))
        # An optional selected-paper transcript provider can reuse saved Notable
        # text. It must resolve by tablet UUID, never title or similar keywords.
        self.saved_annotations = saved_annotations or (lambda item_id: ())
        self.review_policy = review_policy
        self.follow_changes = follow_changes
        self.progress = progress or (lambda message: None)
        self.exclusions = exclusions or {'zotero': set(), 'tablet': set(), 'documents': set(), 'sources': set()}
        self.namespace = "notable-bridge-" + _sha(json.dumps([zotero.server_id, zotero_root, self.croot]).encode())[:24]
        self.publisher = Ledger(state.directory / "publisher")
        self.publisher.trace = getattr(state, 'trace', None)
        self.sources = SimpleNamespace(path=state.directory / "sources" / "library.sqlite3")
        # Reuse the cloud adapter's existing content-addressed baseline support.
        if hasattr(self.cloud, "baseline_dir"):
            self.cloud.baseline_dir = self.sources.path.parent / "remarkable" / "baselines"

    def _cancelled(self):
        stop = getattr(self.cloud, "stop", None)
        return bool(stop and stop.is_set())

    def run(self, *, apply=False):
        with self.state.lock():
            self.state.bind(self.zot.server_id, self.namespace)
            return self._run(apply)

    @contextmanager
    def _document_errors(self, reviews, *, cloud=None, attachment=None, suspended=None):
        """Keep a damaged or unavailable paper from blocking unrelated work."""
        try:
            yield
        except (InterruptedError, AuthorizationRequired, TransportInterrupted):
            # Cancellation and lost permission belong to the whole operation.
            raise
        except (PilotError, CloudError, OSError, ValueError, KeyError, BadZipFile,
                pymupdf.FileDataError, pymupdf.EmptyFileError, RuntimeError) as error:
            if type(error) is RuntimeError and not str(error).startswith('code=4:'):
                raise  # Isolate MuPDF format errors without hiding programming failures.
            if isinstance(error, CloudError) and "authorization expired" in str(error).lower():
                raise
            identity = {"cloud": cloud} if cloud else {"attachment": attachment}
            reviews.append({**identity, "reason": str(error) or "This paper could not be processed. Its source was retained."})
            if cloud and suspended is not None:
                suspended.add(cloud)

    def _run(self, apply):
        self.progress('Checking connected folders…')
        saved_maps = self.state.read('folders', {self.zroot: {'cloud': self.croot, 'root': True}})
        collections = sync_collections(self.zot, saved_maps)
        self.progress('Checking reMarkable’s current file list…')
        catalog = self.cloud.library()
        confirmed = self.state.read('folder_blocks', {}).get('confirmed', {})
        zscope, cscope, blocked = sync_folder_scope(collections, catalog, self.zroot, self.croot, saved_maps, self.exclusions, confirmed)
        blocked_keys = set(blocked['zotero'])
        maps = {key: link for key, link in saved_maps.items() if key not in blocked_keys}
        if apply:
            self.state.write('folder_blocks', blocked)
        zby = {key: row for key, (row, _) in zscope.items()}
        cby = {key: row for key, (row, _) in cscope.items()}
        documents = self.state.read("documents", {})
        reports, reviews = [], []

        def save():
            if apply:
                self.state.write("folders", {**saved_maps, **maps})
                self.state.write("documents", documents)

        def zwrite():
            if apply:
                self.zot.authorize()

        save()  # Persist the root-only mapping even when both subtrees are empty.

        # Recover uncertain folder creation using the identities committed before
        # the request. A confirmed deletion is never treated as an incomplete job.
        for zk, link in list(maps.items()):
            ck = link["cloud"]
            if link.get("pending") == "cloud" and ck not in cby:
                if zk not in zby:
                    raise PilotError("A pending collection left the selected Zotero subtree.")
                if apply:
                    row = self.cloud.create_folder(link["name"], link["cparent"], ck, self.croot)
                    cby[ck] = row
                    link.pop("pending")
                    save()
            elif link.get("pending") == "zotero" and zk not in zby:
                if ck not in cby:
                    raise PilotError("A pending tablet folder left the selected subtree.")
                if apply:
                    zwrite()
                    zby[zk] = self.zot.create("collection", {"key": zk, "name": link["name"],
                                                          "parentCollection": link["zparent"]})
                    link.pop("pending")
                    save()
            if zk in zby and ck in cby and link.get("pending"):
                link.pop("pending")
                save()
            if zk in zby and ck in cby and not link.get("root"):
                if not self.follow_changes and (zby[zk].get("parentCollection") != link["zparent"] or cby[ck].parent != link["cparent"]
                        or zby[zk]["name"] != link.get("desktop_name", link["name"]) or cby[ck].name != link.get("tablet_name", link["name"])):
                    raise PilotError("A linked folder was renamed or moved. Existing files were retained; review its mapping.")

        # Discover parent before child in either direction. A unique sibling name
        # can link folders; paper identity always requires a recorded source ID.
        pending_z, pending_c = set(zby) - set(maps), set(cby) - {v["cloud"] for v in maps.values()}
        while pending_z or pending_c:
            progressed = False
            for zp, parent_link in list(maps.items()):
                cp = parent_link["cloud"]
                zs = [k for k in pending_z if zby[k].get("parentCollection") == zp]
                cs = [k for k in pending_c if cby[k].parent == cp]
                zcounts = Counter(zby[k]["name"] for k in zs)
                ccounts = Counter(cby[k].name for k in cs)
                if any(n > 1 for n in zcounts.values()) or any(n > 1 for n in ccounts.values()):
                    raise PilotError("Duplicate sibling folder names need an explicit mapping before sync.")
                for zk in zs:
                    name = zby[zk]["name"]
                    match = next((k for k in cs if cby[k].name == name), None)
                    ck = match or str(uuid.uuid5(uuid.NAMESPACE_URL, self.namespace + "/collection/" + zk))
                    maps[zk] = {"cloud": ck, "name": name, "zparent": zp, "cparent": cp,
                                **({} if match else {"pending": "cloud"})}
                    save()
                    reports.append({"action": "link_folders" if match else "folder_to_tablet", "zotero": zk, "cloud": ck})
                    if match:
                        pending_c.remove(match)
                    elif apply:
                        cby[ck] = self.cloud.create_folder(name, cp, ck, self.croot)
                        maps[zk].pop("pending")
                        save()
                    else:
                        cby[ck] = CloudItem(ck, "", name, cp, "CollectionType", False, [])
                    pending_z.remove(zk)
                    progressed = True
                for ck in cs:
                    if ck not in pending_c:
                        continue
                    zk = stable_key(self.namespace, "collection", ck)
                    if zk in zby:
                        raise PilotError("A generated collection ID is already in use. No existing collection was adopted.")
                    name = cby[ck].name
                    maps[zk] = {"cloud": ck, "name": name, "zparent": zp, "cparent": cp, "pending": "zotero"}
                    save()
                    reports.append({"action": "folder_to_zotero", "zotero": zk, "cloud": ck})
                    if apply:
                        zwrite()
                        zby[zk] = self.zot.create("collection", {"key": zk, "name": name, "parentCollection": zp})
                        maps[zk].pop("pending")
                        save()
                    else:
                        zby[zk] = {"key": zk, "name": name, "parentCollection": zp}
                    pending_c.remove(ck)
                    progressed = True
            if not progressed:
                raise PilotError("Some folders cannot be mapped inside the selected roots.")

        reverse = {v["cloud"]: k for k, v in maps.items()}
        if len(reverse) != len(maps):
            raise PilotError("More than one collection maps to the same tablet folder.")
        for zk, link in maps.items():
            if link.get("root"):
                continue
            if self.follow_changes:
                tablet = cby[link["cloud"]]
                before_desktop = [link.get("desktop_name", link["name"]), link["zparent"]]
                before_tablet = [link.get("tablet_name", link["name"]), link["zparent"]]
                desktop = [zby[zk]["name"], zby[zk].get("parentCollection")]
                remote = [tablet.name, reverse.get(tablet.parent)]
                if not remote[1] or desktop[1] not in maps:
                    raise PilotError("A mapped folder moved outside the selected tree. Review its scope before syncing.")
                zname, cname = desktop[0] != before_desktop[0], remote[0] != before_tablet[0]
                zhome, chome = desktop[1] != before_desktop[1], remote[1] != before_tablet[1]
                if ((zname and cname and desktop[0] != remote[0]) or (zhome and chome and desktop[1] != remote[1])):
                    conflict = {"type": "folder", "zotero": zk, "cloud": tablet.id, "desktop": desktop, "tablet": remote,
                                "cloud_hash": tablet.hash, "version": zby[zk].get("version", 0)}
                    if self.review_policy:
                        from .zotero_review import fingerprint
                        self.review_policy.offer("conflict", zk, fingerprint(conflict), conflict)
                    reviews.append({"collection": zk, "reason": "This folder changed on both devices. Choose a version in Notable review."})
                    return {"mode": "apply" if apply else "plan", "actions": reports, "review": reviews, "folders": len(maps), "documents": len(documents)}
                chosen_name = desktop[0] if zname else remote[0] if cname else None
                chosen_parent = desktop[1] if zhome else remote[1] if chome else before_desktop[1]
                desired_desktop = [chosen_name if chosen_name is not None else desktop[0], chosen_parent]
                desired_tablet = [chosen_name if chosen_name is not None else remote[0], chosen_parent]
                if apply:
                    if desired_tablet != remote:
                        cby[tablet.id] = self.cloud.update_item(tablet.id, expected_hash=tablet.hash, name=desired_tablet[0], parent=maps[chosen_parent]["cloud"], scope_id=self.croot)
                    if desired_desktop != desktop:
                        zwrite()
                        zby[zk]["name"], zby[zk]["parentCollection"] = desired_desktop
                        self.zot.update_collection(zby[zk])
                    link.update(name=desired_desktop[0], desktop_name=desired_desktop[0], tablet_name=desired_tablet[0],
                                zparent=chosen_parent, cparent=maps[chosen_parent]["cloud"])
                    save()
                continue
            if (zby[zk].get("parentCollection") != link["zparent"] or cby[link["cloud"]].parent != link["cparent"]
                    or zby[zk]["name"] != link.get("desktop_name", link["name"]) or cby[link["cloud"]].name != link.get("tablet_name", link["name"])):
                raise PilotError("A linked folder was renamed or moved. Existing files were retained; review its mapping.")

        # Each Zotero child PDF is a distinct document (including SI), with its
        # own immutable original. Returned marked attachments never re-enter this
        # upload path. Multiple collections need an explicit primary location.
        references = {}
        suspended = (self.review_policy.suspended_documents() if self.review_policy else set()) | self.exclusions['documents']
        suspended.update(folder_blocked_papers(documents, catalog, blocked))
        if self.follow_changes:
            from pathlib import Path
            from .zotero_review import fingerprint
            by_id = {item.id: item for item in catalog}
            for identity, row in documents.items():
                if identity in suspended or not row.get("parent"):
                    continue
                with self._document_errors(reviews, cloud=identity, suspended=suspended):
                    item = by_id.get(identity)
                    parent = self.zot.get("item", row["parent"])
                    if not item or item.deleted or item.parent not in reverse or not parent or parent.get("deleted"):
                        continue
                    homes = [k for k in parent.get("collections", []) if k in maps]
                    home = row["collection"] if row["collection"] in homes else homes[0] if len(homes) == 1 else None
                    if not home:
                        reviews.append({"cloud": identity, "reason": "Choose one tablet home; removing a collection membership does not delete the paper."})
                        continue
                    key = row.get("view_key") or row.get("source_key")
                    if not key:
                        continue
                    path = self.zot.file_path(key)
                    if row.get('filename_cleanup'):
                        if not apply:
                            suspended.add(identity)
                            continue
                        changed = self._apply_filename_cleanup(row, parent, item, home, catalog, save)
                        catalog[catalog.index(item)] = changed
                        item = changed
                        path = self.zot.file_path(key)
                        reports.append({'action': 'placeholder_filename_repaired', 'cloud': identity})
                    if apply and re.fullmatch(r'[a-f0-9]{64}', item.name) and path.stem == item.name and row['name'] == item.name:
                        from .sorting.core_logic import sanitize_filename_part
                        title = re.sub(r'<[^>]+>', '', parent.get('title', '')).strip()
                        title = re.sub(r'\.pdf$', '', title, flags=re.I)
                        if title and not re.fullmatch(r'[a-f0-9]{64}', title, re.I):
                            name = sanitize_filename_part(title)[:220]
                            if name:
                                self.set_desktop_document(row, name, home, maps)
                                changed = self.cloud.update_item(identity, expected_hash=item.hash, name=name,
                                                                 parent=item.parent, scope_id=self.croot)
                                catalog[catalog.index(item)] = changed
                                item = changed
                                path = self.zot.file_path(key)
                                reports.append({'action': 'legacy_name_repaired', 'cloud': identity})
                    # Preserve any independent names established during adoption.
                    row.setdefault("desktop_name", path.stem)
                    desktop = [path.stem, home]
                    remote = [item.name, reverse[item.parent]]
                    old_desktop = [row["desktop_name"], row["collection"]]
                    old_remote = [row["name"], row["collection"]]
                    zdiff, cdiff = desktop != old_desktop, remote != old_remote
                    if zdiff and cdiff and desktop != remote:
                        conflict = {"type": "paper", "cloud": identity, "zotero": parent["key"], "desktop": desktop, "tablet": remote,
                                    "cloud_hash": item.hash, "version": parent.get("version", 0), "file_md5": hashlib.md5(path.read_bytes()).hexdigest()}
                        self.review_policy.offer("conflict", identity, fingerprint(conflict), conflict)
                        suspended.add(identity)
                        reviews.append({"cloud": identity, "reason": "This paper was renamed or moved on both devices. Choose a version in review."})
                        continue
                    if (zdiff or cdiff) and apply:
                        chosen = desktop if zdiff else remote
                        if zdiff and desktop != remote:
                            changed = self.cloud.update_item(identity, expected_hash=item.hash, name=chosen[0], parent=maps[chosen[1]]["cloud"], scope_id=self.croot)
                            catalog[catalog.index(item)] = changed
                        elif cdiff and desktop != remote:
                            self.set_desktop_document(row, chosen[0], chosen[1], maps)
                        row.update(name=chosen[0], desktop_name=chosen[0], collection=chosen[1], tablet_parent=maps[chosen[1]]["cloud"])
                        reports.append({"action": "paper_location_updated", "cloud": identity})
                    save()
        self.progress('Checking Zotero papers in connected folders…')
        for zk in zby:
            if not apply and zk not in zscope:
                continue
            for row in self.zot.collection_items(zk):
                if row.get("itemType") not in {"attachment", "note", "annotation"} and not row.get("deleted"):
                    references[row["key"]] = row
        for number, parent in enumerate(references.values(), 1):
            self.progress(f'Checking Zotero paper {number} of {len(references)}…')
            homes = [k for k in parent.get("collections", []) if k in maps]
            for attachment in self.zot.children(parent["key"]):
                if (attachment.get("itemType") != "attachment" or attachment.get("deleted")
                        or attachment.get("contentType") != "application/pdf"
                        or "Notable pilot:" in attachment.get("note", "")):
                    continue
                source_key = attachment["key"]
                if source_key in self.exclusions['sources']:
                    continue
                with self._document_errors(reviews, attachment=source_key):
                    existing = next(((ck, d) for ck, d in documents.items() if d.get("source_key") == source_key), None)
                    from .zotero_bulk import observed_digest
                    source_path = self.zot.file_path(source_key)
                    baseline = observed_digest(source_path, self.file_digests)
                    approval = self.review_policy.approval("zotero", source_key, baseline) if self.review_policy else {}
                    counterpart = next((ck for ck, linked in documents.items()
                                        if not existing and linked.get('baseline') == baseline and not linked.get('unavailable')), None)
                    if counterpart:
                        reviews.append({'attachment': source_key, 'cloud': counterpart,
                                        'reason': 'This exact PDF already has a connected tablet copy. Reconcile its Zotero references before sending another copy.'})
                        continue
                    # A reference can have several PDFs, each with its own tablet
                    # home. Research memberships do not override that choice.
                    chosen = existing[1]["collection"] if existing else (approval or {}).get("collection")
                    zk = chosen if chosen in homes else homes[0] if len(homes) == 1 else None
                    if not zk:
                        reviews.append({"attachment": source_key, "reason": "Choose one tablet destination for this PDF inside the sync subtree."})
                        continue
                    if existing:
                        ck, old = existing
                        if ck in suspended:
                            continue
                        # Repair a pre-existing interrupted registration from the
                        # unchanged desktop source before attempting its upload.
                        original = self.state.directory / "originals" / (baseline + ".pdf")
                        if apply and old["baseline"] == baseline and not original.exists():
                            data = source_path.read_bytes()
                            if _sha(data) != baseline:
                                raise PilotError("Desktop PDF changed while being staged. Retry sync.")
                            atomic_write(original, data, require_absent=True)
                        if old["baseline"] != baseline or old["collection"] != zk or old["parent"] != parent["key"]:
                            reviews.append({"attachment": source_key, "reason": "The desktop original or its collection changed; it was not sent back to the tablet."})
                        continue
                    if approval is None:
                        reviews.append({"attachment": source_key, "reason": "Review the filename and tablet destination before sending this new document."})
                        continue
                    ck = str(uuid.uuid5(uuid.NAMESPACE_URL, self.namespace + "/pdf/" + source_key))
                    proposed = {"origin": "zotero", "source_key": source_key, "parent": parent["key"],
                                     "collection": zk, "baseline": baseline, "uploaded": False,
                                     "role": "si" if approval.get("document_type") == "supporting_information" else "main",
                                     "tablet_parent": maps[zk]["cloud"],
                                     "name": (parent.get("title") or attachment.get("title") or source_key)[:180]}
                    # Include a child label when a reference has several PDF files.
                    label = attachment.get("title", "")
                    if label and label != parent.get("title"):
                        proposed["name"] += " - " + label[:60]
                    if approval.get("filename"):
                        from pathlib import Path
                        proposed["name"] = Path(approval["filename"]).stem
                    original = self.state.directory / "originals" / (baseline + ".pdf")
                    if apply:
                        data = source_path.read_bytes()
                        if _sha(data) != baseline:
                            raise PilotError("Desktop PDF changed while being staged. Retry sync.")
                        if not original.exists():
                            atomic_write(original, data, require_absent=True)
                        elif _sha(original.read_bytes()) != baseline:
                            raise PilotError("A retained desktop original changed. It was not uploaded.")
                    # Another successful paper must never persist this mapping
                    # until its immutable original has been staged completely.
                    documents[ck] = proposed
                    save()
                    reports.append({"action": "pdf_to_tablet", "attachment": source_key, "cloud": ck})

        cloud_pdfs = {r.id: r for r in catalog if not r.deleted and r.is_pdf and r.parent in reverse}
        for identity in suspended:
            cloud_pdfs.pop(identity, None)
        for number, (ck, row) in enumerate(list(documents.items()), 1):
            self.progress(f'Checking approved paper {number} of {len(documents)}…')
            if ck in suspended:
                continue
            if ck in cloud_pdfs:
                row.pop("unavailable", None)
            elif row.get("uploaded"):
                row["unavailable"] = True
                if row["origin"] != "zotero":
                    reviews.append({"cloud": ck, "reason": "Linked tablet PDF is unavailable or outside the selected folders. Its absence does not confirm deletion; saved files were retained."})
            save()
            if row["origin"] != "zotero":
                continue
            with self._document_errors(reviews, cloud=ck, suspended=suspended):
                parent, source = self.zot.get("item", row["parent"]), self.zot.get("item", row["source_key"])
                if (not parent or not source or parent.get("deleted") or source.get("deleted")
                        or source.get("parentItem") != row["parent"]
                        or row["collection"] not in parent.get("collections", [])):
                    reviews.append({"cloud": ck, "reason": "The linked Zotero source was deleted or moved. No PDF was recreated."})
                    # Do not publish or upload this stale mapping later in the run.
                    cloud_pdfs.pop(ck, None)
                    continue
                if ck not in cloud_pdfs and row["uploaded"]:
                    reviews.append({"cloud": ck, "reason": "Linked tablet PDF is missing or moved; it was not recreated."})
                    continue
                if ck not in cloud_pdfs and apply:
                    self.progress(f'Sending approved paper {number} of {len(documents)} to reMarkable…')
                    original = self.state.directory / "originals" / (row["baseline"] + ".pdf")
                    data = original.read_bytes()
                    if _sha(data) != row["baseline"]:
                        raise PilotError("A staged original changed. Upload stopped.")
                    item = self.cloud.upload_pdf(data, row["name"], maps[row["collection"]]["cloud"], ck, self.croot)
                    cloud_pdfs[ck] = item
                    # A lost response leaves uploaded=False and the persisted ID;
                    # next run validates its baseline instead of creating a new PDF.
                if ck in cloud_pdfs:
                    row["uploaded"] = True
                    save()

        for number, (ck, item) in enumerate(cloud_pdfs.items(), 1):
            if ck in suspended:
                continue
            with self._document_errors(reviews, cloud=ck):
                row = documents.get(ck)
                self.progress(f'{"Syncing linked" if row else "Checking new tablet"} paper {number} of {len(cloud_pdfs)}: {item.name} [tablet item {ck}]…')
                zk = reverse[item.parent]
                if row and row["collection"] != zk:
                    reviews.append({"cloud": ck, "reason": "Linked tablet PDF moved; review the primary collection."})
                    continue
                cached_source = source_info(self.sources, ck, self._cancelled)
                retained = bool(cached_source and cached_source.get("cloud_hash") == item.hash)
                bundle = (source_bundle(self.sources, ck, self._cancelled, revision=item.hash)
                          if retained else self.cloud.pdf_bundle(item))
                baseline = _sha(bundle[ck + ".pdf"])
                if row and row["baseline"] != baseline:
                    reviews.append({"cloud": ck, "reason": "The tablet PDF baseline changed; annotations were not applied to a different edition."})
                    continue
                if not row:
                    approval = self.review_policy.approval("cloud", ck, baseline) if self.review_policy else {}
                    if approval is None:
                        reviews.append({"cloud": ck, "reason": "Review this new tablet document before importing it."})
                        continue
                    row = {"origin": "cloud", "parent": "", "collection": zk, "baseline": baseline,
                           "role": "si" if approval.get("document_type") == "supporting_information" else "main",
                           "uploaded": True, "name": item.name, "tablet_parent": item.parent}
                    if approval.get("parent"):
                        row["parent"] = approval["parent"]
                    documents[ck] = row
                    save()
                if self.review_policy and row["origin"] == "cloud" and not row.get("parent"):
                    raise PilotError("The approved Zotero reference is unavailable. Review this paper before importing it; no replacement reference was created.")
                if self.review_policy and "role" not in row:
                    approval = self.review_policy.approval("cloud", ck, baseline)
                    if approval is None and row.get("source_key"):
                        approval = self.review_policy.approval("zotero", row["source_key"], baseline)
                    row["role"] = "si" if (approval or {}).get("document_type") == "supporting_information" else "main"
                if not apply:
                    reports.append({"action": "tablet_to_zotero", "cloud": ck, "collection": zk})
                    continue
                if not retained:
                    save_source_bundle(self.sources, item, bundle)
                # Rendering only retained, integrity-checked source bytes also makes
                # later retries independent of mutable in-memory download objects.
                retained_bundle = source_bundle(self.sources, ck, revision=item.hash)
                from .zotero_tags import render_fingerprint
                cached = row.get("rendered", {})
                fingerprint = render_fingerprint(ck, retained_bundle)
                cached_input = cached.get("input") or []
                first_publication = not (row.get("view_key") or row.get("revision") or cached)
                # Grouping existing marks requires the separate manual repair.
                # New links opt in and keep the choice on every later sync.
                group_highlights = row.get("group_highlights", first_publication)
                approved = row.get("approved_highlight_groups") or []
                handwriting_cards = (row.get("handwriting_cards") or first_publication
                                     or bool(cached_input and fingerprint != cached_input[0]))
                render_id = annotation_render_id(fingerprint, group_highlights=group_highlights,
                                                 handwriting_cards=handwriting_cards, approved_highlight_groups=approved)
                output = self.state.directory / "rendered" / (cached.get("sha256", "missing") + ".pdf")
                if cached.get("input") == render_id and output.exists():
                    digest = cached["sha256"]
                    from .zotero_bulk import observed_digest
                    if observed_digest(output, self.file_digests) != digest:
                        raise PilotError("A retained annotated PDF changed. It was preserved for review.")
                else:
                    try:
                        rendered = annotated_pdf(item, retained_bundle, self._cancelled,
                                                 diagnostic=getattr(self.state, 'trace', None),
                                                 group_highlights=group_highlights, handwriting_cards=handwriting_cards,
                                                 approved_highlight_groups=approved)
                    except UnsupportedAnnotations as error:
                        reviews.append({"cloud": ck, "reason": str(error)})
                        continue
                    digest = _sha(rendered)
                    output = self.state.directory / "rendered" / (digest + ".pdf")
                    if not output.exists():
                        atomic_write(output, rendered, require_absent=True)
                    if group_highlights:
                        row["group_highlights"] = True
                    if handwriting_cards:
                        row["handwriting_cards"] = True
                    row["rendered"] = {"input": render_id, "sha256": digest}
                    save()
                annotations = tuple(self.saved_annotations(ck))
                if (self.review_policy and row.get("source_key") == row.get("view_key")
                        and row.get("source_key") and not self.publisher.get(ck)["published"]):
                    source_attachment = self.zot.get("item", row["source_key"])
                    if source_attachment and source_attachment.get("linkMode") == "linked_file":
                        # Recover an adoption recorded by an earlier version;
                        # publication never supported linked-file attachments.
                        row.pop("view_key")
                if self.review_policy and row.get("source_key") and not row.get("view_key"):
                    source = row["source_key"]
                    self.progress(f'Preparing Zotero publication for {item.name} [tablet item {ck}]…')
                    if self.publisher.get(ck)["published"]:
                        row["view_key"] = stable_key(self.namespace, "attachment", ck)
                    else:
                        original = self.state.directory / "originals" / (baseline + ".pdf")
                        original_md5 = hashlib.md5(original.read_bytes()).hexdigest()
                        attachment = self.zot.get("item", source)
                        adopted = ({"adopted": False, "native": 0} if attachment and attachment.get("linkMode") == "linked_file"
                                   else self.zot.review_action("adopt", {"key": source, "parent": row["parent"], "md5": original_md5,
                                                                 "marker": marker(self.namespace, ck)}))
                        row["view_key"] = source if adopted["adopted"] else stable_key(self.namespace, "attachment", ck)
                        row["desktop_annotations_preserved"] = adopted["native"]
                        if adopted["adopted"]:
                            self.publisher.save(ck, md5=original_md5)
                    save()
                paper = Paper(ck, output, digest, {"title": row["name"]}, role=row.get("role", "main"), annotations=annotations,
                              source_attachment_key=row.get("source_key", "") if row.get("source_key") != row.get("view_key") else "",
                              filename=row["name"] + ".pdf" if self.review_policy else "")
                parents = {ck: row["parent"]} if row.get("parent") else None
                self.progress(f'Publishing {item.name} to Zotero [tablet item {ck}]…')
                result = publish(self.zot, self.publisher, self.namespace, [paper], apply=True,
                                 collection_key=zk, reference_keys=parents,
                                 attachment_keys={ck: row["view_key"]} if row.get("view_key") else None)["papers"][0]
                if row["origin"] == "cloud":
                    row["parent"] = result["reference_key"]
                row["revision"] = item.hash
                row["view_key"] = result["attachment_key"]
                if self.follow_changes and "desktop_name" not in row:
                    row["desktop_name"] = self.zot.file_path(row["view_key"]).stem
                save()
                reports.append({**result, "action": "tablet_to_zotero", "publication_action": result["action"], "cloud": ck})
                from .remarkable_pdf import page_map
                with pymupdf.open(stream=bundle[ck + '.pdf'], filetype='pdf') as original_pdf:
                    row['page_map'] = [pid for pid, _ in page_map(json.loads(bundle[ck + '.content']), len(original_pdf))]
                row['page_map_digest'] = digest
                save()
                # Existing links need keyword extraction too. Read the retained
                # original, never the rendered view's synthetic export metadata.
                from .zotero_tagging import backfill_keywords
                original = self.sources.path.parent / 'remarkable' / 'baselines' / (baseline + '.pdf')
                check = backfill_keywords(self.zot, row['parent'], original, row, save)
                if check['status'] == 'extraction_failed':
                    reviews.append({'cloud': ck, 'reason': 'Keyword extraction failed: ' + check.get('detail', '')})
                elif check['status'] == 'needs_text_recognition':
                    reviews.append({'cloud': ck, 'reason': 'No readable keyword text was found. This PDF needs text recognition or manual tags.'})
                self._tags(row['parent'], bundle, ck, row, save)
                self.progress(f'Received tablet PDF {number} of {len(cloud_pdfs)}…')
        return {"mode": "apply" if apply else "plan", "actions": reports, "review": reviews,
                "folder_issues": blocked['issues'], "folders": len({**saved_maps, **maps}), "documents": len(documents)}

    def _apply_filename_cleanup(self, row, parent, item, home, catalog, save):
        from .zotero_filenames import citation_snapshot

        plan = row['filename_cleanup']
        target = plan['target']
        old_names = {plan['tablet'], target} if plan.get('started') else {plan['tablet']}
        if (parent['key'] != plan['parent'] or home != plan['home'] or item.parent != plan['tablet_parent']
                or item.name not in old_names or citation_snapshot(parent) != plan['citation']
                or any(row.get(key) != plan[key] for key in ('source_key', 'view_key'))):
            raise PilotError('The citation or linked location changed after the filename preview. Check the queued rename in Linked papers.')
        if (item.name != target and any(other.id != item.id and not other.deleted and other.parent == item.parent
                                       and other.name.casefold() == target.casefold() for other in catalog)):
            raise PilotError('The proposed name is already used in this tablet folder. Review the queued rename in Linked papers.')
        if item.hash != row.get('revision'):
            if _sha(self.cloud.pdf_bundle(item)[item.id + '.pdf']) != row['baseline']:
                raise PilotError('The tablet PDF changed editions. Review the queued rename in Linked papers.')
        for entry in plan['files']:
            attachment = self.zot.get('item', entry['key'])
            path = self.zot.file_path(entry['key'])
            allowed = {entry['before'], entry['filename']} if plan.get('started') else {entry['before']}
            with path.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'md5').hexdigest()
            if (not attachment or attachment.get('deleted') or attachment.get('parentItem') != parent['key']
                    or path.name not in allowed or digest != entry['md5']):
                raise PilotError('A linked PDF changed after the filename preview. Check the queued rename in Linked papers.')
            if path.name.casefold() != entry['filename'].casefold() and path.with_name(entry['filename']).exists():
                raise PilotError('The proposed filename already exists in Zotero storage. Check the queued rename in Linked papers.')
        plan['started'] = True
        save()
        for entry in plan['files']:
            if self._cancelled():
                raise InterruptedError('Filename cleanup paused.')
            if entry['before'] != entry['filename']:
                self.zot.review_action('rename', {'key': entry['key'], 'filename': entry['filename'], 'md5': entry['md5'],
                                                 'expected_parent': parent['key'], 'before_filename': entry['before']})
        if item.name != target:
            if self._cancelled():
                raise InterruptedError('Filename cleanup paused.')
            item = self.cloud.update_item(item.id, expected_hash=item.hash, name=target, scope_id=self.croot)
        if plan.get('undo'):
            row.pop('filename_repair', None)
        else:
            row['filename_repair'] = {'tablet': plan['tablet'], 'desktop': plan['desktop'], 'target': target,
                'home': home, 'tablet_parent': plan['tablet_parent'], 'source_key': plan['source_key'],
                'view_key': plan['view_key'], 'files': plan['files']}
        row.update(name=target, desktop_name=plan.get('desktop_target', target), tablet_parent=item.parent)
        row.pop('filename_cleanup')
        save()
        return item

    def desktop_document_plan(self, row, name, home):
        """Validate every filename and source before changing a membership."""
        from .sorting.core_logic import validate_document_filename
        parent = self.zot.get("item", row["parent"])
        if not parent or parent.get("deleted"):
            raise PilotError("The Zotero reference was trashed. Its tablet copy was retained.")
        siblings = self.state.read("documents", {}).values()
        keep_previous = any(other != row and other.get("parent") == row["parent"]
                            and other.get("collection") == row["collection"] for other in siblings)
        removed = set() if keep_previous else {row["collection"]}
        parent["collections"] = sorted((set(parent.get("collections", [])) - removed) | {home})
        files = []
        for key in sorted({row.get('source_key'), row.get('view_key')} - {None, ''}):
            path = self.zot.file_path(key)
            filename = validate_document_filename(name + path.suffix, path.suffix)
            attachment = self.zot.get('item', key)
            if not attachment or attachment.get('deleted') or attachment.get('parentItem') != row['parent']:
                raise PilotError('A connected attachment changed. Its filename and collections were retained.')
            files.append({'key': key, 'filename': filename,
                          'before_filename': attachment.get('filename') or path.name,
                          'md5': hashlib.md5(path.read_bytes()).hexdigest()})
        return {'parent': parent, 'files': files}

    def set_desktop_document(self, row, name, home, maps, *, plan=None):
        """Move only the tablet-home membership; keep all research categories."""
        plan = self.desktop_document_plan(row, name, home) if plan is None else plan
        self.zot.authorize()
        self.zot.update(plan['parent'])
        for entry in plan['files']:
            attachment = self.zot.get('item', entry['key'])
            path = self.zot.file_path(entry['key'])
            if not attachment or attachment.get('deleted') or attachment.get('parentItem') != row['parent']:
                raise PilotError('A connected attachment changed while moving its paper. Its file was retained.')
            if (attachment.get('filename') or path.name) == entry['filename']:
                with path.open('rb') as stream:
                    if hashlib.file_digest(stream, 'md5').hexdigest() != entry['md5']:
                        raise PilotError('A connected PDF changed while moving its paper. Its file was retained.')
                title = attachment.get('title', '')
                if not title.lower().endswith(('.pdf', '.docx', '.pptx')) or title == entry['filename']:
                    continue  # A prior interrupted step already renamed this file.
                # Finish updating a filename-like display label after a lost
                # rename response, without replacing a custom attachment title.
            self.zot.review_action('rename', {key: entry[key] for key in ('key', 'filename', 'md5')})

    def _tags(self, parent_key, bundle, cloud_id, row, save):
        from .zotero_tags import reconcile
        # The bundle revision is the only revision whose tags were observed.
        from .remarkable_cloud import CloudItem
        item = CloudItem(cloud_id, row['revision'], row['name'], row.get('tablet_parent', ''), 'DocumentType', False, [])
        reconcile(self, parent_key, item, bundle, row, save)
