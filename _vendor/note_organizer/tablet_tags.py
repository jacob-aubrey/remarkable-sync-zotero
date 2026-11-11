"""Validate tablet tag payloads independently of Zotero."""

def names(values, *, error_type=ValueError):
    if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip()
            or len(v) > 255 or any(ord(c) < 32 for c in v) for v in values):
        raise error_type("Tags must be nonempty names of at most 255 characters.")
    return sorted(set(values))


def tablet_tags(content, *, error_type=ValueError):
    paper, pages = set(), {}
    for field in ("tags", "pageTags"):
        values = content.get(field) or []
        if not isinstance(values, list):
            raise error_type("Unrecognized tablet tags require review.")
        for value in values:
            if not isinstance(value, dict):
                raise error_type("Unrecognized tablet tag requires review.")
            tag = names([value.get("name")], error_type=error_type)[0]
            if field == "tags":
                paper.add(tag)
            else:
                page = value.get("pageId")
                if not isinstance(page, str) or not page:
                    raise error_type("A tablet page tag has no page identity.")
                pages.setdefault(page, set()).add(tag)
    return paper, pages
