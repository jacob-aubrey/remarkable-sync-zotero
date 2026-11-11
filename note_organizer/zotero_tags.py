"""Scoped tag reconciliation. Page identity is the tablet UUID, never a number."""
import hashlib
import json

from .zotero_pilot import PilotError


from .tablet_tags import names as _names, tablet_tags as _tablet_tags


def names(values):
    return _names(values, error_type=PilotError)


def tablet_tags(content):
    return _tablet_tags(content, error_type=PilotError)


def merge(base, desktop, tablet):
    """First contact unions; subsequent observed removals propagate both ways.

    The common baseline advances only after both writes succeed. A partial
    commit therefore remains a visible delta and can be retried after restart.
    """
    desktop, tablet = set(desktop), set(tablet)
    if base is None:
        return sorted(desktop | tablet)
    base = set(base)
    return sorted((base | desktop | tablet) - ((base - desktop) | (base - tablet)))


def render_fingerprint(identity, bundle):
    content = json.loads(bundle[identity + ".content"])
    for key in ("tags", "pageTags"):
        content.pop(key, None)
    values = {key: hashlib.sha256(value).hexdigest() for key, value in bundle.items()
              if key not in {identity + ".content", identity + ".metadata"}}
    values["content"] = content
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def flatten(paper, pages):
    return {('', tag) for tag in paper} | {(pid, tag) for pid, tags in pages.items() for tag in tags}


def packed(values):
    paper, pages = [], {}
    for pid, tag in sorted(values):
        if pid:
            pages.setdefault(pid, []).append(tag)
        else:
            paper.append(tag)
    return {'paper': paper, 'pages': pages}


def reconcile(bridge, parent_key, item, bundle, row, save):
    content = json.loads(bundle[item.id + ".content"])
    remote_paper, remote_pages = tablet_tags(content)
    parent = bridge.zot.get("item", parent_key)
    if not parent or parent.get("deleted"):
        raise PilotError("The reference is unavailable; its tags were retained.")
    local_paper = {v["tag"] for v in parent.get("tags", [])}
    baseline = row.get("tag_state")
    if baseline is None and set(row.get("seen_tags", [])) & (set().union(*remote_pages.values())
            if remote_pages else set()) & local_paper - remote_paper:
        raise PilotError("Legacy page tags also appear as paper tags. Remove unintended paper tags in Zotero before syncing tags.")
    local_pages = row.get("page_tags", {})
    local, remote = flatten(local_paper, local_pages), flatten(remote_paper, remote_pages)
    pending = row.get('tag_pending')
    if pending:
        target = flatten(pending['target']['paper'], pending['target']['pages'])
        observed = []
        for side, current in [('desktop', local), ('tablet', remote)]:
            previous = flatten(pending[side]['paper'], pending[side]['pages'])
            if pending.get(side + '_done') or current == target:
                observed.append(target)
            elif current == previous:
                observed.append(previous)
            else:
                raise PilotError('Tags changed during an interrupted sync. Review the current tags before retrying.')
        left, right = observed
        desired = (target | (local - left) | (remote - right)) - ((left - local) | (right - remote))
    else:
        base = flatten(baseline['paper'], baseline['pages']) if baseline else None
        desired = set(merge(base, local, remote))
        if baseline and local == remote == base:
            return
    target = packed(desired)
    paper, pages = target['paper'], target['pages']
    # Validate the cloud boundary before either side or the retry journal moves.
    names(paper)
    for values in pages.values():
        names(values)
    # Preserve the exact intended edit across a process exit between writes.
    row["tag_pending"] = {'target': target, 'desktop': packed(local), 'tablet': packed(remote)}
    save()
    if set(paper) != local_paper:
        existing = {v["tag"]: v for v in parent.get("tags", [])}
        parent["tags"] = [existing.get(tag, {"tag": tag}) for tag in paper]
        bridge.zot.authorize()
        bridge.zot.update(parent)
    row['page_tags'] = pages
    row['tag_pending']['desktop_done'] = True
    save()
    if set(paper) != remote_paper or {p: set(t) for p, t in pages.items()} != remote_pages:
        updated = bridge.cloud.update_item(item.id, expected_hash=item.hash, scope_id=bridge.croot,
                                          tags=paper, page_tags=pages)
        row["revision"] = updated.hash
    row["page_tags"] = pages
    row["tag_state"] = {"paper": paper, "pages": pages}
    row.pop("tag_pending", None)
    row.pop("seen_tags", None)
    save()
