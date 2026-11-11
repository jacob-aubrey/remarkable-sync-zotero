"""Keep physical PDF positions distinct from labels and verified journal pages."""


def _positive_integer(value):
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number > 0 and str(value).strip() == str(number) else None


def pdf_label(entry):
    # Older scans stored the embedded label, or the physical page as a fallback.
    return str(entry.get("pdf_label", entry.get("label", "")) or "").strip()


def journal_page(entry):
    """Only explicit verification makes a label a journal page number."""
    return str(entry.get("journal_page", "") or "").strip() if entry.get("journal_page_verified") is True else ""


def page_description(entry):
    page = _positive_integer(entry.get("page"))
    total = _positive_integer(entry.get("page_count") or entry.get("metadata", {}).get("pages"))
    description = f"PDF page {page or '?'}"
    if page and total and total >= page:
        description += f" of {total}"
    journal = journal_page(entry)
    label = pdf_label(entry)
    if journal:
        description += f" · Journal p. {journal}"
    elif label and label != str(page):
        description += f" · PDF label {label}"
    return description


def page_tooltip(entry):
    description = page_description(entry) + "\nOpen PDF opens at the physical PDF page."
    if not journal_page(entry) and pdf_label(entry) and pdf_label(entry) != str(entry.get("page")):
        description += "\nAn embedded PDF label is not a verified journal page number."
    return description


def page_search_text(entry):
    return " ".join((str(entry.get("page", "")), pdf_label(entry), journal_page(entry)))
