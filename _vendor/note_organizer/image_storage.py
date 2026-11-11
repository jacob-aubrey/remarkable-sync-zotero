"""Lossless, content-addressed note images shared by current notes and history."""
import base64
import hashlib
import json


def intern(db, entry):
    entry = dict(entry)
    value = entry.pop("image", None)
    if value is not None:
        data, encoding = value.encode("utf-8"), "utf8"
        try:
            decoded = base64.b64decode(value, validate=True)
            if base64.b64encode(decoded).decode("ascii") == value:
                data, encoding = decoded, "base64"
        except (ValueError, UnicodeError):
            pass
        key = hashlib.sha256(encoding.encode() + b":" + data).hexdigest()
        db.execute("INSERT OR IGNORE INTO note_images VALUES(?,?,?)", (key, encoding, data))
        entry["image_ref"] = key
    return entry


def image(db, key):
    row = db.execute("SELECT encoding,data FROM note_images WHERE digest=?", (key,)).fetchone()
    if row is None:
        raise ValueError("A saved note image is missing. Restore a verified library backup.")
    return base64.b64encode(row[1]).decode("ascii") if row[0] == "base64" else row[1].decode("utf-8")


def expand(db, entry, include_images=True):
    key = entry.pop("image_ref", None)
    if key and include_images:
        entry["image"] = image(db, key)
    if not include_images:
        entry.pop("image", None)
    return entry


def migrate(db, cancelled=lambda: False):
    """Called only by explicit maintenance, after creating a verified backup."""
    changed = 0
    for table in ("entries", "history"):
        # Bound memory even for libraries with years of drawing revisions.
        cursor = db.execute(f"SELECT rowid,payload FROM {table} WHERE json_type(payload,'$.image')='text'")
        while rows := cursor.fetchmany(32):
            for rowid, payload in rows:
                if cancelled():
                    raise InterruptedError("Storage maintenance cancelled")
                value = json.dumps(intern(db, json.loads(payload)), sort_keys=True, ensure_ascii=False)
                db.execute(f"UPDATE {table} SET payload=? WHERE rowid=?", (value, rowid))
                changed += 1
    db.execute("""DELETE FROM note_images WHERE digest NOT IN (
        SELECT json_extract(payload,'$.image_ref') FROM entries WHERE json_type(payload,'$.image_ref')='text'
        UNION SELECT json_extract(payload,'$.image_ref') FROM history WHERE json_type(payload,'$.image_ref')='text')""")
    return changed
