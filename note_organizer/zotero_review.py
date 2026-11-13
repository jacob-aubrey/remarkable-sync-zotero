"""Durable user review and confirmed-deletion decisions for the Zotero UI.

No cloud calls, UI framework, account secrets or library writes occur merely
by importing this module. Actions use existing adapters and stable identities.
"""
import hashlib
import json
from pathlib import Path
import time

from .zotero_pilot import Ledger, PilotError, ReviewChanged


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


ACKNOWLEDGE_LIBRARY_FINDINGS = frozenset({'possible_duplicate_references', 'repeated_pdf_content'})


def finding_acknowledgement_token(code, evidence):
    """Identify an inspected library arrangement, without storing blanket consent."""
    if code not in ACKNOWLEDGE_LIBRARY_FINDINGS:
        raise PilotError('This condition cannot be marked as intentionally separate.')
    return fingerprint(['review-library-arrangement-v1', code, evidence])


def cloud_change_content(cloud, item):
    """Identify bytes/metadata a rename or move must leave unchanged."""
    metadata_file = next((row for row in item.files if row['id'] == item.id + '.metadata'), None)
    if metadata_file is None:
        raise PilotError('The tablet metadata is unavailable. Sync before retrying this change.')
    metadata = json.loads(cloud.blob(metadata_file['hash'], metadata_file['id']))
    # update_item changes these fields, and always normalizes deleted to bool.
    metadata = {**metadata, 'deleted': bool(metadata.get('deleted'))}
    for key in ('visibleName', 'parent', 'lastModified'):
        metadata.pop(key, None)
    return fingerprint({'kind': item.kind, 'metadata': metadata,
                        'files': sorted((row for row in item.files if row['id'] != metadata_file['id']),
                                        key=lambda row: row['id'])})


def cloud_change_intent(cloud, item, name, parent):
    return {'cloud': item.id, 'before_hash': item.hash, 'name': name, 'parent': parent,
            'content': cloud_change_content(cloud, item)}


def cloud_change_done(cloud, item, intent):
    """Recover our exact target without replaying onto an independent edit."""
    if not item or item.deleted or item.id != intent.get('cloud'):
        raise PilotError('The tablet document is unavailable. Its saved filing was retained.')
    if intent.get('after_hash'):
        if item.hash == intent['after_hash']:
            return True
        raise PilotError('The tablet document changed after filing. Its newer changes were retained; review its current state before retrying.')
    target = item.name == intent['name'] and item.parent == intent['parent']
    if item.hash == intent['before_hash']:
        return target
    if target and cloud_change_content(cloud, item) == intent['content']:
        return True  # A successful response was lost; no write is needed.
    raise PilotError('The tablet document changed while filing was interrupted. Its newer changes were retained; review its current state before retrying.')


class ReviewStore(Ledger):
    def __init__(self, directory):
        super().__init__(directory)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS review_queue (id TEXT PRIMARY KEY, kind TEXT NOT NULL, identity TEXT NOT NULL, digest TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL, approval TEXT NOT NULL DEFAULT '{}', UNIQUE(kind,identity))")
            db.execute("CREATE TABLE IF NOT EXISTS deletion_decisions (id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL, action TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '{}')")
            db.execute("CREATE TABLE IF NOT EXISTS observations (identity TEXT PRIMARY KEY, signature TEXT NOT NULL, first_seen REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS link_controls (identity TEXT PRIMARY KEY, paused INTEGER NOT NULL, history TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS local_deletions (id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL, action TEXT NOT NULL DEFAULT '')")
            db.execute("CREATE TABLE IF NOT EXISTS intake_cleanup (identity TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS review_finding_acknowledgements (fingerprint TEXT PRIMARY KEY, code TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL)")

    def acknowledgement_tokens(self):
        """Read only scoped arrangements; a different fingerprint reopens review."""
        with self.connect() as db:
            return {row[0] for row in db.execute('SELECT fingerprint FROM review_finding_acknowledgements')}

    def acknowledge_finding(self, identity, expected_digest, code, expected_token, *,
                            expected_review_token, keep_separate=True):
        """Record/revoke one current library finding, without approving its paper.

        The caller first refreshes local evidence. Row and finding tokens protect
        against a changed selection, bytes, participants or relationships between
        the displayed decision and this transaction. Applying journals are never
        eligible for this independent action.
        """
        if code not in ACKNOWLEDGE_LIBRARY_FINDINGS or not isinstance(keep_separate, bool):
            raise PilotError('Only an inspected library overlap can be marked as intentionally separate.')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT digest,status,payload FROM review_queue WHERE id=?', (identity,)).fetchone()
            if not row or row['status'] != 'pending' or row['digest'] != expected_digest:
                raise PilotError('This review changed. Recheck it before recording this arrangement.')
            payload = json.loads(row['payload'])
            finding = next((value for value in payload.get('findings', []) if value.get('code') == code), None)
            decision = finding.get('acknowledgement', {}) if finding else {}
            if (not expected_review_token or payload.get('review_token') != expected_review_token
                    or not decision.get('eligible') or not expected_token
                    or decision.get('token') != expected_token or finding.get('domain') != 'library'):
                raise PilotError('The library evidence changed. Recheck this review before recording its arrangement.')
            if keep_separate:
                # Retain a small audit record, never the whole citation/row or a
                # permanent rule that ignores every later issue for this PDF.
                record = {'references': sorted(value['key'] for value in finding.get('references', [])),
                          'attachments': sorted(value['key'] for value in finding.get('attachments', []))}
                db.execute('INSERT OR IGNORE INTO review_finding_acknowledgements VALUES(?,?,?,?)',
                           (expected_token, code, json.dumps(record), time.time()))
            else:
                db.execute('DELETE FROM review_finding_acknowledgements WHERE fingerprint=? AND code=?',
                           (expected_token, code))

    def cleanup(self, identity, payload=None, *, complete=False):
        with self.connect() as db:
            if payload is not None:
                db.execute("INSERT OR IGNORE INTO intake_cleanup VALUES(?,?,'pending')", (identity, json.dumps(payload)))
            if complete:
                db.execute("UPDATE intake_cleanup SET status='complete' WHERE identity=?", (identity,))

    def cleanups(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM intake_cleanup WHERE status='pending'").fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def confirm_zotero_deleted(self, item, link, record):
        if not item or item.deleted or not record:
            return
        if not record.get('deleted'):
            self.observe_zotero_live(item.id, record)
            return
        key = fingerprint(["zotero-trash", item.id, item.hash, record["key"]])[:24]
        payload = {"cloud": item.id, "cloud_hash": item.hash, "title": link["name"], "key": record["key"]}
        with self.connect() as db:
            for old in db.execute("SELECT id,payload FROM local_deletions WHERE status!='resolved' AND id!=?", (key,)).fetchall():
                if json.loads(old["payload"])["cloud"] == item.id:
                    db.execute("UPDATE local_deletions SET status='resolved',action='superseded' WHERE id=?", (old["id"],))
            db.execute("INSERT OR IGNORE INTO local_deletions VALUES(?,?,'pending','')", (key, json.dumps(payload)))

    def observe_zotero_live(self, cloud_id, record):
        """A native Undo/restore resolves only the notice for that exact item."""
        if not record or record.get('deleted'):
            return
        with self.connect() as db:
            for row in db.execute("SELECT id,payload FROM local_deletions WHERE status!='resolved'").fetchall():
                payload = json.loads(row['payload'])
                if payload['cloud'] == cloud_id and payload['key'] == record.get('key'):
                    db.execute("UPDATE local_deletions SET status='resolved',action='restored_in_zotero' WHERE id=?", (row['id'],))

    def observe_live(self, item):
        if not item or item.deleted or item.parent == "trash":
            return
        with self.connect() as db:
            for row in db.execute("SELECT id,payload,status,action FROM deletion_decisions WHERE status!='resolved'").fetchall():
                if json.loads(row["payload"])["cloud"] == item.id and (row["status"] == "pending" or row["action"] == "restore"):
                    db.execute("UPDATE deletion_decisions SET status='resolved',action='restore',result=? WHERE id=?",
                               (json.dumps({"action": "restore", "external": True, "cloud": item.id}), row["id"]))

    def local_deletions(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM local_deletions WHERE status!='resolved' ORDER BY rowid").fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def retire_merge_deletions(self, cloud_id, keys):
        with self.connect() as db:
            for row in db.execute("SELECT id,payload FROM local_deletions WHERE status='pending'").fetchall():
                payload = json.loads(row['payload'])
                if payload['cloud'] == cloud_id and payload['key'] in keys:
                    db.execute("UPDATE local_deletions SET status='resolved',action='native_merge' WHERE id=?", (row['id'],))

    def resolve_local_deletion(self, identity, choice, *, cloud, zotero, scope):
        if choice not in {'trash_tablet', 'keep_tablet', 'restore_zotero'}:
            raise PilotError('Choose how to handle the Zotero deletion.')
        row = next((r for r in self.local_deletions() if r["id"] == identity), None)
        if not row:
            raise PilotError("This deletion decision is no longer pending.")
        data = row["payload"]
        record = zotero.get("item", data["key"])
        item = next((r for r in cloud.library() if r.id == data["cloud"]), None)
        if not record or not record.get("deleted") or not item:
            raise PilotError("The linked item changed. Sync again before deciding.")
        if not item.deleted and item.hash != data["cloud_hash"]:
            raise PilotError("The tablet copy changed after this notification. Sync before deciding.")
        if choice == 'restore_zotero':
            if item.deleted or item.parent == 'trash':
                raise PilotError('The reMarkable copy is also in Trash. Sync again before restoring the connection.')
            result = zotero.review_action('restore_linked_item', {'key': data['key'], 'version': record.get('version', 0),
                                                                 'parent': record.get('parentItem') or ''})
            if not result.get('restored'):
                raise PilotError('The Zotero item could not be verified after restoring it.')
        elif choice == "trash_tablet" and not item.deleted:
            cloud.update_item(item.id, expected_hash=item.hash, parent="trash", scope_id=scope)
        self.link_control(item.id, paused=choice != 'restore_zotero', event={'action': choice})
        with self.connect() as db:
            db.execute("UPDATE local_deletions SET status='resolved',action=? WHERE id=?", (choice, identity))

    def link_control(self, identity, *, paused=None, event=None):
        with self.connect() as db:
            row = db.execute("SELECT paused,history FROM link_controls WHERE identity=?", (identity,)).fetchone()
            value = {"paused": bool(row[0]) if row else False, "history": json.loads(row[1]) if row else []}
            if paused is not None:
                value["paused"] = bool(paused)
            if event:
                value["history"].append({"time": time.time(), **event})
            if paused is not None or event:
                db.execute("INSERT OR REPLACE INTO link_controls VALUES(?,?,?)", (identity, int(value["paused"]), json.dumps(value["history"])))
        return value

    def observe_ready(self, identity, signature, *, now=None, settle=2):
        now = time.time() if now is None else now
        with self.connect() as db:
            row = db.execute("SELECT signature,first_seen FROM observations WHERE identity=?", (identity,)).fetchone()
            if row and row[0] == signature:
                return now - row[1] >= settle
            db.execute("INSERT OR REPLACE INTO observations VALUES(?,?,?)", (identity, signature, now))
        return False

    def offer(self, kind, identity, digest, payload):
        with self.connect() as db:
            return self._offer(db, kind, identity, digest, payload)

    def retire_conflict(self, identity):
        with self.connect() as db:
            db.execute("UPDATE review_queue SET status='superseded' WHERE kind='conflict' AND identity=? AND status='pending'",
                       (identity,))

    @staticmethod
    def _offer(db, kind, identity, digest, payload):
        key = fingerprint([kind, identity])[:24]
        encoded = json.dumps(payload)
        old = db.execute("SELECT digest,status,payload FROM review_queue WHERE id=?", (key,)).fetchone()
        if old and old[0] == digest:
            if old[1] in {"approved", "ignored", "attached"} or (old[1] in {"pending", "applying"} and old[2] == encoded):
                return key
        # Keep an explicitly approved action journal during recovery. A
        # changed input gets a new pending review instead of stale consent.
        db.execute("INSERT INTO review_queue(id,kind,identity,digest,payload,status) VALUES(?,?,?,?,?,'pending') ON CONFLICT(id) DO UPDATE SET digest=excluded.digest,payload=excluded.payload,status=CASE WHEN review_queue.digest=excluded.digest AND review_queue.status='applying' THEN 'applying' ELSE 'pending' END,approval=CASE WHEN review_queue.digest=excluded.digest THEN review_queue.approval ELSE '{}' END",
                   (key, kind, identity, digest, encoded))
        return key

    def rows(self, *, pending_only=True, inbox_order=False):
        columns = 'r.*'
        source = 'review_queue AS r'
        if inbox_order:
            # Keep the paper's arrival order when its reference gains a PDF.
            columns += ', MIN(r.rowid, COALESCE(ref.rowid, r.rowid)) AS inbox_order'
            source += (" LEFT JOIN review_queue AS ref ON r.kind='zotero' AND ref.kind='reference'"
                       " AND ref.identity=json_extract(r.payload, '$.parent')")
        pending = " WHERE r.status IN ('pending','applying')" if pending_only else ''
        with self.connect() as db:
            rows = db.execute(f'SELECT {columns} FROM {source}{pending} ORDER BY r.rowid').fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"]), "approval": json.loads(row["approval"])} for row in rows]

    def reopen(self, identity, expected_digest, payload, *, applying=False):
        """Require a fresh decision when new evidence invalidates old consent."""
        with self.connect() as db:
            db.execute("UPDATE review_queue SET status='pending',payload=?,approval='{}' WHERE id=? AND digest=? AND status=?",
                       (json.dumps(payload), identity, expected_digest, 'applying' if applying else 'approved'))

    def dismiss(self, identity, digest):
        with self.connect() as db:
            row = db.execute('SELECT digest,status FROM review_queue WHERE id=?', (identity,)).fetchone()
            if not row or row['digest'] != digest or row['status'] != 'pending':
                raise PilotError('Only a current pending review can be dismissed. Finish or recover an active filing first.')
            db.execute("UPDATE review_queue SET status='ignored' WHERE id=?", (identity,))

    def finish_intake_attachment(self, identity, digest, cleanup):
        """Retire the ToSort review and record its verified cleanup together."""
        with self.connect() as db:
            changed = db.execute("UPDATE review_queue SET status='attached' WHERE id=? AND kind='inbox' AND digest=? AND status='pending'",
                                 (identity, digest)).rowcount
            if not changed:
                raise PilotError('The ToSort review changed. The files were retained.')
            db.execute("INSERT OR REPLACE INTO intake_cleanup VALUES(?,?,'pending')", (identity, json.dumps(cleanup)))

    def mark_unavailable(self, identity, reason):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM review_queue WHERE id=? AND status='pending'", (identity,)).fetchone()
            if row:
                payload = {**json.loads(row['payload']), 'unavailable': reason, 'routine': False}
                db.execute('UPDATE review_queue SET payload=? WHERE id=?', (json.dumps(payload), identity))

    def supersede_inbox(self, path, current_identity):
        """Keep history, but offer only the current instance of an intake path."""
        with self.connect() as db:
            rows = db.execute("SELECT id,identity,payload FROM review_queue WHERE kind='inbox' AND status='pending'").fetchall()
            for row in rows:
                if row['identity'] != current_identity and Path(json.loads(row['payload']).get('path', '')).resolve() == Path(path).resolve():
                    db.execute("UPDATE review_queue SET status='superseded' WHERE id=?", (row['id'],))

    def reconcile_references(self, references):
        """Publish one complete local pass while retaining dismissed history."""
        with self.connect() as db:
            for key, payload in references.items():
                self._offer(db, 'reference', key, fingerprint(['reference', key]), payload)
            rows = db.execute("SELECT id,identity FROM review_queue WHERE kind='reference' AND status='pending'").fetchall()
            db.executemany("UPDATE review_queue SET status='superseded' WHERE id=? AND status='pending'",
                           [(row['id'],) for row in rows if row['identity'] not in references])

    def retire_missing_inbox(self, current_paths):
        """Retain vanished inputs as history instead of presenting dead approvals."""
        present = {str(Path(path).resolve()).casefold() for path in current_paths}
        with self.connect() as db:
            rows = db.execute("SELECT id,payload FROM review_queue WHERE kind='inbox' AND status='pending'").fetchall()
            for row in rows:
                if str(Path(json.loads(row['payload'])['path']).resolve()).casefold() not in present:
                    db.execute("UPDATE review_queue SET status='unavailable' WHERE id=?", (row['id'],))

    def complete_counterpart(self, kind, identity, digest, payload, approval):
        """Commit a verified substep atomically, including an already-finished retry.

        The original action owns consent and has rechecked the bytes. A second
        review journal is bookkeeping, not another user approval gate.
        """
        key = fingerprint([kind, identity])[:24]
        with self.connect() as db:
            old = db.execute('SELECT digest FROM review_queue WHERE id=?', (key,)).fetchone()
            if old and old['digest'] != digest:
                raise PilotError('The matching document changed. Refresh before completing its filing.')
            db.execute("INSERT INTO review_queue(id,kind,identity,digest,payload,status,approval) VALUES(?,?,?,?,?,'approved',?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,status='approved',approval=excluded.approval",
                       (key, kind, identity, digest, json.dumps(payload), json.dumps(approval)))
        return key

    def row(self, identity):
        with self.connect() as db:
            row = db.execute("SELECT * FROM review_queue WHERE id=?", (identity,)).fetchone()
        return {**dict(row), "payload": json.loads(row["payload"]), "approval": json.loads(row["approval"])} if row else None

    def begin(self, identity, expected_digest, approval):
        with self.connect() as db:
            row = db.execute("SELECT digest,status,approval FROM review_queue WHERE id=?", (identity,)).fetchone()
            if not row or row[0] != expected_digest or row[1] not in {"pending", "applying"}:
                raise PilotError("This review changed. Refresh it before approving.")
            if row['status'] == 'applying':
                previous = json.loads(row['approval'])
                if any(key in previous and previous[key] != value for key, value in approval.items()):
                    raise PilotError('This filing is being recovered with its saved choices. Finish it before changing those choices.')
                approval = {**previous, **approval}
            db.execute("UPDATE review_queue SET status='applying',approval=? WHERE id=?", (json.dumps(approval), identity))

    def checkpoint(self, identity, **changes):
        """Advance an existing action journal without replacing its consent."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT approval FROM review_queue WHERE id=? AND status='applying'", (identity,)).fetchone()
            if not row:
                raise PilotError('This filing changed before its recovery step could be saved.')
            approval = {**json.loads(row['approval']), **changes}
            db.execute('UPDATE review_queue SET approval=? WHERE id=?', (json.dumps(approval), identity))
        return approval

    def finish(self, identity, *, approval=None):
        with self.connect() as db:
            if approval is not None:
                row = db.execute('SELECT approval FROM review_queue WHERE id=?', (identity,)).fetchone()
                approval = {**(json.loads(row['approval']) if row else {}), **approval}
                db.execute("UPDATE review_queue SET approval=? WHERE id=?", (json.dumps(approval), identity))
            db.execute("UPDATE review_queue SET status='approved' WHERE id=? AND status='applying'", (identity,))

    def approval(self, kind, identity, digest):
        with self.connect() as db:
            row = db.execute("SELECT digest,status,approval FROM review_queue WHERE kind=? AND identity=?", (kind, identity)).fetchone()
        if row and row[0] == digest and row[1] == "approved":
            return json.loads(row[2])
        return None

    def primary_collection(self, parent):
        for row in self.rows(pending_only=False):
            if row["status"] == "approved" and row["approval"].get("parent") == parent:
                return row["approval"].get("collection")
        return None

    def confirm_deleted(self, cloud_item, link, *, attachment, parent, inventory):
        if not cloud_item or not (cloud_item.deleted or cloud_item.parent == "trash"):
            return None  # Missing IDs and unreachable cloud are not deletion proof.
        key = fingerprint(["tablet-trash", cloud_item.id, cloud_item.hash])[:24]
        payload = {"cloud": cloud_item.id, "cloud_hash": cloud_item.hash, "title": link["name"],
                   "restore_parent": link.get("tablet_parent"), "attachment": attachment,
                   "source_attachment": link.get("source_key", "") if link.get("source_key") != attachment else "", "parent": parent,
                   "inventory": inventory}
        with self.connect() as db:
            for old in db.execute("SELECT id,payload FROM deletion_decisions WHERE status!='resolved' AND id!=?", (key,)).fetchall():
                if json.loads(old["payload"])["cloud"] == cloud_item.id:
                    db.execute("UPDATE deletion_decisions SET status='resolved',action='superseded',result=? WHERE id=?", (json.dumps({"reason": "Newer tablet trash revision"}), old["id"]))
            db.execute("INSERT OR IGNORE INTO deletion_decisions VALUES(?,?,'pending','','{}')", (key, json.dumps(payload)))
            db.execute("UPDATE deletion_decisions SET payload=? WHERE id=? AND status='pending'", (json.dumps(payload), key))
        return key

    def deletions(self, *, pending_only=True):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM deletion_decisions" + (" WHERE status!='resolved'" if pending_only else "") + " ORDER BY rowid").fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"]), "result": json.loads(row["result"])} for row in rows]

    def resolve_deletion(self, identity, action, *, cloud, zotero, scope, destination='', zotero_plan=None):
        if action not in {'restore', 'pdf_only', 'everything', 'keep_zotero', 'remove_collections'}:
            raise PilotError('Choose how to handle the reMarkable deletion.')
        with self.lock():
            row = next((row for row in self.deletions(pending_only=False) if row["id"] == identity), None)
            if not row:
                raise PilotError("This deletion notification is unavailable.")
            if row["status"] == "resolved":
                if row["action"] != action:
                    raise PilotError("This deletion already has a different recorded decision.")
                return row["result"]
            data = row["payload"]
            catalog = cloud.library()
            item = next((r for r in catalog if r.id == data["cloud"]), None)
            # A prior restore that demonstrably did not commit can be changed.
            if row['action'] and row['action'] != action:
                if not (row['action'] == 'restore' and item and item.hash == data['cloud_hash'] and (item.deleted or item.parent == 'trash')):
                    raise PilotError('A different deletion action is being recovered. Refresh its status.')
            if action == 'restore' and destination:
                data = {**data, 'restore_parent': destination}
            # Recognize an interrupted, already-completed restore by its recorded
            # destination. Other newer cloud edits require a fresh decision.
            restored = action == "restore" and row["status"] == "applying" and item and not item.deleted and item.parent == data["restore_parent"]
            if not restored and (not item or item.hash != data["cloud_hash"] or not (item.deleted or item.parent == "trash")):
                raise PilotError("The tablet item changed since this notification. Refresh before deciding.")
            if action == 'restore' and not restored:
                from .zotero_bridge import descendants
                folders = [r for r in catalog if r.kind == 'CollectionType']
                live = descendants(folders, scope, lambda r: r.id, lambda r: r.parent,
                                   unavailable={r.id for r in folders if r.deleted})
                if not data.get('restore_parent') or data['restore_parent'] not in live:
                    raise PilotError('Choose a live destination inside the connected tablet folder before restoring this paper.')
            with self.connect() as db:
                db.execute("UPDATE deletion_decisions SET status='applying',action=?,payload=? WHERE id=?", (action, json.dumps(data), identity))
            if action == "restore":
                if not restored:
                    cloud.update_item(item.id, expected_hash=item.hash, parent=data["restore_parent"], scope_id=scope, restore=True)
            elif action != 'keep_zotero':
                # The native Zotero endpoint validates the whole displayed
                # attachment/note inventory again inside its transaction. It
                # uses Zotero Trash and never permanently erases records.
                try:
                    zotero.review_action("trash", {"choice": action, **data,
                                                   **({'zotero_plan': zotero_plan} if zotero_plan is not None else {})})
                except ReviewChanged:
                    with self.connect() as db:
                        db.execute("UPDATE deletion_decisions SET status='pending',action='' WHERE id=?", (identity,))
                    raise
            result = {"action": action, "cloud": data["cloud"], "parent": data["parent"],
                      "attachment": data["attachment"], "status": "resolved"}
            with self.connect() as db:
                db.execute("UPDATE deletion_decisions SET status='resolved',result=? WHERE id=?", (json.dumps(result), identity))
            return result

    def suspended_documents(self):
        # A confirmed deletion remains suspended even after a local-trash
        # choice; only an explicit restore resumes its link.
        result = set()
        for row in self.deletions(pending_only=False):
            if row["status"] != "resolved" or row["action"] != "restore":
                result.add(row["payload"]["cloud"])
            elif row["action"] == "restore":
                result.discard(row["payload"]["cloud"])
        result.update(row["payload"]["cloud"] for row in self.local_deletions())
        with self.connect() as db:
            result.update(row[0] for row in db.execute("SELECT identity FROM link_controls WHERE paused=1"))
        return result
