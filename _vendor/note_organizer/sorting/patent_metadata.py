"""Conservative patent front-page extraction; never classify from a citation alone."""
from __future__ import annotations

import re
from datetime import datetime

PATENT_TYPES = frozenset({"patent", "patent_application"})


def normalize_patent_number(value):
    text = re.sub(r"[\s,./-]+", "", str(value or "")).upper()
    # Preserve the printed country, number and kind code; never invent a suffix.
    if re.fullmatch(r"US(?:(?:19|20)\d{9}|\d{5,8}|(?:D|PP|RE)\d{4,8})(?:[A-Z]\d?)?", text):
        return text
    if re.fullmatch(r"(?:EP|WO|GB|DE|FR|CA|AU|JP|CN|KR)\d{5,14}(?:[A-Z]\d?)?", text):
        return text
    return ""


def patent_date(value):
    text = re.sub(r"\s+", " ", str(value or "")).strip(" *:;,")
    text = re.sub(r"\bSept\.?", "Sep", text, flags=re.I).replace(".", "")
    for fmt in ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def _field(text, code, label=""):
    match = re.search(r"\(" + code + r"\)\s*" + label + r"\s*[:.]?\s*(.*?)(?=\(\s*(?:\d{2}|\*)\s*\)|\Z)", text, re.I | re.S)
    return re.sub(r"\s+", " ", match.group(1)).strip() if match else ""


def extract_patent_fields(text):
    # Front-page masthead plus independent labelled bibliographic fields. A
    # bare US number, a filename, or a patent mentioned in prose is insufficient.
    front = str(text or "")[:8000]
    masthead = re.search(r"(?:\(12\)|\(19\))?\s*United States\s+(?:\(12\)\s*)?(?:Patent Application Publication|(?:Design |Plant |Reissued )?Patent)\b", front[:1600], re.I)
    if not masthead:
        return {}
    front = front[masthead.start():]
    number_match = re.search(r"(?:\(10\)\s*)?(?:Patent|Pub(?:lication)?\.?)[ \t]+No\.?\s*:\s*(US\s*(?:D|PP|RE)?\s*\d[\d, /]*\s*(?:[A-Z]\s*\d?)?)", front[:2000], re.I)
    if not number_match:
        return {}
    number = normalize_patent_number(number_match.group(1))
    if not number:
        return {}
    application = "application" in masthead.group(0).lower()
    # Never read the filing/priority date or a cited patent's date as this date.
    date_label = r"(?:Pub(?:lication)?\.?\s*Date)" if application else r"(?:Date\s+of\s+(?:Patent|Reissued Patent))"
    date_match = re.search(date_label + r"\s*:\s*\*?\s*([A-Za-z]+\.?\s+\d{1,2},?\s+\d{4}|\d{4}-\d{2}-\d{2})", front[:3000], re.I)
    if not re.search(r"\((?:54|72|75)\)", front):
        return {}
    published = patent_date(date_match.group(1)) if date_match else ""
    kind = re.search(r"([A-Z]\d?)$", number[2:])
    kind = kind.group(1) if kind else ""
    # Modern US application numbers and kind codes should agree with the header.
    if (application and kind.startswith("B")) or (not application and re.match(r"US(?:19|20)\d{9}A", number)):
        return {}
    inventor_text = _field(front, "72", r"Inventors?") or _field(front, "75", r"Inventors?")
    inventors = []
    for item in inventor_text.split(";"):
        # US front pages separate names from their city/address with a comma.
        name = item.split(",", 1)[0].strip()
        name = re.sub(r"\s*\([^)]*\)", "", name).strip()
        if name and len(name) <= 120 and not re.search(r"\d|\b(?:Assignee|Applicant|Patent)\b", name, re.I):
            inventors.append(name)
    assignee = _field(front, "73", r"Assignees?")
    title = _field(front, "54")
    result = {
        "document_type": "patent_application" if application else "patent",
        "patent_number": number,
        "patent_country": number[:2],
        "patent_kind_code": kind,
        "identifier": {"patent": number},
        "inventors": inventors,
        "creators": inventors,
        "primary_creator": inventors[0] if inventors else "",
        "is_multiple_creators": len(inventors) > 1,
        "assignee": assignee[:300],
        "title": title[:500],
        "issue_date": "" if application else published,
        "publication_date": published if application else "",
        "year": published[:4] if published else "Unknown",
        "is_supplementary_material": False,
    }
    return result
