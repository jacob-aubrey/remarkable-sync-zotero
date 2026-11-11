"""Safe, DOI-only bibliographic metadata lookup helpers.

This module deliberately does *not* search by title, author, filename, or any
other fuzzy clue.  A caller must provide an exact DOI that was found locally in
a document.  That keeps the network request small, predictable, and much less
likely to attach the wrong citation to a paper.

The public API is intentionally dependency-free so it can be used by the GUI,
the watcher, and tests without requiring an AI client or a third-party HTTP
library.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote
from urllib.request import Request, urlopen


LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 8.0
MAX_TIMEOUT_SECONDS = 30.0
MAX_HTTP_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_DOI_CANDIDATES = 20
MAX_METADATA_FIELD_LENGTH = 1_000
DEFAULT_USER_AGENT = "AI-Paper-Sorter/1.2 (DOI metadata lookup)"

# The DOI Handbook's common DOI pattern: a registrant code followed by a
# slash and a suffix.  It is intentionally permissive about suffix characters;
# ``normalize_doi`` performs the final, whole-string validation.
DOI_PATTERN = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
_DOI_FULL_PATTERN = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
_YEAR_PATTERN = re.compile(r"\b(?:1[5-9]\d{2}|20\d{2}|21\d{2})\b")
_DOI_PREFIX_PATTERN = re.compile(
    r"^\s*(?:doi\s*:\s*|https?://(?:dx\.)?doi\.org/)", re.IGNORECASE
)

JsonFetcher = Callable[..., Mapping[str, Any]]


@dataclass(frozen=True)
class DOIReference:
    """One structured relationship reported by a DOI metadata provider."""

    relation_type: str
    identifier: str
    identifier_type: str = ""


@dataclass(frozen=True)
class DOIResolution:
    """Bibliographic metadata retrieved by an exact DOI lookup.

    ``evidence_label`` is intentionally precise: retrieval by DOI is strong
    evidence, but only the caller can compare this record with the actual
    document and label it a document match.
    """

    doi: str
    provider: str
    evidence_label: str
    title: str = ""
    authors: tuple[str, ...] = ()
    journal: str = ""
    journal_abbreviation: str = ""
    year: str = ""
    volume: str = ""
    issue: str = ""
    document_type: str = "unknown"
    provider_type: str = ""
    publisher: str = ""
    url: str = ""
    relations: tuple[DOIReference, ...] = ()

    @property
    def primary_creator(self) -> str:
        """Return the first listed person or organization, if any."""

        return self.authors[0] if self.authors else ""

    @property
    def is_supplement_to_parent(self) -> bool:
        """Whether provider relations explicitly call this item a supplement."""

        return any(
            reference.relation_type == "is_supplement_to" for reference in self.relations
        )

    @property
    def parent_doi(self) -> str:
        """Return the parent article DOI when this item is explicitly its supplement.

        A relation may point to a URL, ISBN, or another identifier, so this
        property only returns a syntactically valid DOI.  It never performs a
        second network request or guesses at a parent record.
        """

        for reference in self.relations:
            if reference.relation_type == "is_supplement_to":
                parent_doi = normalize_doi(reference.identifier)
                if parent_doi:
                    return parent_doi
        return ""

    def as_document_details(self) -> dict[str, Any]:
        """Return fields in the sorter-friendly generic metadata shape.

        This method does not assign a percentage or decide whether a
        human review is needed; those decisions require comparison with the
        document's locally extracted text.
        """

        return {
            "title": self.title,
            "primary_creator": self.primary_creator,
            "creators": list(self.authors),
            "year": self.year,
            "document_type": self.document_type,
            "venue_or_publisher": self.journal or self.publisher,
            "journal": self.journal,
            "journal_abbreviation": self.journal_abbreviation,
            "volume": self.volume,
            "issue": self.issue,
            "identifier": {"doi": self.doi, **({"url": self.url} if self.url else {})},
            "is_multiple_creators": len(self.authors) > 1,
            "metadata_provider": self.provider,
            "evidence_label": self.evidence_label,
            "provider_type": self.provider_type,
            "relations": [
                {
                    "relation_type": reference.relation_type,
                    "identifier": reference.identifier,
                    "identifier_type": reference.identifier_type,
                }
                for reference in self.relations
            ],
        }


def normalize_doi(value: object) -> str:
    """Return a canonical lowercase DOI, or ``""`` if *value* is not one.

    DOI URLs, a leading ``doi:``, surrounding brackets, percent escaping, and
    ordinary prose punctuation are accepted.  The function rejects a DOI-like
    substring with additional text around it, which prevents accidental fuzzy
    lookup requests.
    """

    if value is None:
        return ""
    candidate = str(value).replace("\u200b", "").strip()
    if not candidate or len(candidate) > 4_096:
        return ""
    candidate = unquote(candidate)
    candidate = _DOI_PREFIX_PATTERN.sub("", candidate).strip()
    candidate = candidate.strip("<[{\"' ")
    candidate = candidate.rstrip(".,;:!?\"'>]}")
    # Parentheses may be legal DOI suffix characters.  Remove only closing
    # brackets that clearly belong to surrounding prose, such as ``(doi)``.
    while candidate.endswith(")") and candidate.count("(") < candidate.count(")"):
        candidate = candidate[:-1].rstrip()
    while candidate.endswith("]") and candidate.count("[") < candidate.count("]"):
        candidate = candidate[:-1].rstrip()
    candidate = re.sub(r"\s+", "", candidate)
    if not _DOI_FULL_PATTERN.fullmatch(candidate):
        return ""
    return candidate.casefold()


def extract_doi_candidates(text: object, *, limit: int = MAX_DOI_CANDIDATES) -> list[str]:
    """Extract distinct DOI candidates in first-appearance order.

    This only identifies printed DOI strings.  It does not decide which DOI is
    the document's DOI; a reference list can legitimately contain many of
    them.  Callers should combine the ordered candidates with local placement
    and document-metadata evidence before resolving one.
    """

    if text is None:
        return []
    try:
        max_results = max(0, min(int(limit), MAX_DOI_CANDIDATES))
    except (TypeError, ValueError):
        max_results = MAX_DOI_CANDIDATES
    if not max_results:
        return []
    sample = str(text)[:250_000]
    candidates: list[str] = []
    seen: set[str] = set()
    for match in DOI_PATTERN.finditer(sample):
        normalized = normalize_doi(match.group(0))
        if normalized and normalized not in seen:
            candidates.append(normalized)
            seen.add(normalized)
            if len(candidates) >= max_results:
                break
    return candidates


def resolve_doi(
    doi: object,
    *,
    fetch_json: JsonFetcher | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
    providers: Sequence[str] = ("crossref", "datacite"),
    deadline: float | None = None,
) -> DOIResolution | None:
    """Look up one exact DOI through Crossref, then DataCite if needed.

    ``fetch_json`` is injectable for tests and offline callers.  It receives a
    provider's exact DOI endpoint plus ``timeout`` and ``user_agent`` keyword
    arguments, and must return decoded JSON.  Any bad response, network
    problem, provider outage, or malformed record safely returns ``None`` (or
    proceeds to the next provider). ``deadline`` is an optional shared monotonic
    deadline that prevents retries from accumulating across document candidates.
    No document text is ever sent here.
    """

    normalized_doi = normalize_doi(doi)
    if not normalized_doi:
        return None
    safe_timeout = _safe_timeout(timeout)
    safe_user_agent = _clean_text(user_agent, limit=300) or DEFAULT_USER_AGENT
    fetcher = fetch_json or _fetch_json

    for provider in _normalized_providers(providers):
        request_timeout = safe_timeout
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            request_timeout = min(request_timeout, remaining)
        url = _provider_url(provider, normalized_doi)
        if not url:
            continue
        try:
            payload = fetcher(url, timeout=request_timeout, user_agent=safe_user_agent)
            if not isinstance(payload, Mapping):
                raise MetadataLookupError("Provider did not return a JSON object.")
            resolution = _parse_provider_response(provider, payload, normalized_doi)
        except Exception as exc:  # A metadata outage must never stop sorting.
            LOGGER.debug("%s DOI lookup failed for %s: %s", provider, normalized_doi, exc)
            continue
        if resolution is not None and _resolution_is_useful(resolution):
            return resolution
    return None


class MetadataLookupError(RuntimeError):
    """Internal safe failure from an exact DOI metadata request."""


def _fetch_json(url: str, *, timeout: float, user_agent: str) -> Mapping[str, Any]:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": user_agent,
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed provider URLs only.
            payload = response.read(MAX_HTTP_RESPONSE_BYTES + 1)
    except (HTTPError, URLError, OSError) as exc:
        raise MetadataLookupError(str(exc)) from exc
    if len(payload) > MAX_HTTP_RESPONSE_BYTES:
        raise MetadataLookupError("Provider response exceeded the safety limit.")
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MetadataLookupError("Provider returned invalid JSON.") from exc
    if not isinstance(decoded, Mapping):
        raise MetadataLookupError("Provider did not return a JSON object.")
    return decoded


def _safe_timeout(value: object) -> float:
    try:
        return max(1.0, min(float(value), MAX_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS


def _normalized_providers(providers: Sequence[str] | None) -> tuple[str, ...]:
    if providers is None:
        return ()
    if isinstance(providers, str):
        providers = (providers,)
    recognized: list[str] = []
    for provider in providers:
        name = str(provider).casefold().strip()
        if name in {"crossref", "datacite"} and name not in recognized:
            recognized.append(name)
    return tuple(recognized)


def _provider_url(provider: str, doi: str) -> str:
    # Preserve the DOI's separator slash as a path separator; encode every
    # other reserved character so a DOI cannot alter the provider endpoint.
    encoded_doi = quote(doi, safe="/")
    if provider == "crossref":
        return f"https://api.crossref.org/works/{encoded_doi}"
    if provider == "datacite":
        return f"https://api.datacite.org/dois/{encoded_doi}"
    return ""


def _parse_provider_response(
    provider: str, payload: Mapping[str, Any], requested_doi: str
) -> DOIResolution | None:
    if provider == "crossref":
        return _parse_crossref(payload, requested_doi)
    if provider == "datacite":
        return _parse_datacite(payload, requested_doi)
    return None


def _parse_crossref(payload: Mapping[str, Any], requested_doi: str) -> DOIResolution | None:
    record = payload.get("message", payload)
    if not isinstance(record, Mapping):
        return None
    returned_doi = normalize_doi(record.get("DOI"))
    if returned_doi != requested_doi:
        return None
    authors = _crossref_authors(record.get("author"))
    title = _first_text(record.get("title"))
    journal = _first_text(record.get("container-title"))
    resolution = DOIResolution(
        doi=requested_doi,
        provider="Crossref",
        evidence_label="DOI metadata retrieved from Crossref",
        title=title,
        authors=authors,
        journal=journal,
        journal_abbreviation=_first_text(record.get("short-container-title")),
        year=_crossref_year(record),
        volume=_clean_text(record.get("volume"), limit=100),
        issue=_clean_text(record.get("issue"), limit=100),
        document_type=_document_type_from_provider(record.get("type")),
        provider_type=_clean_text(record.get("type"), limit=100),
        publisher=_clean_text(record.get("publisher")),
        url=_clean_text(record.get("URL"), limit=2_000),
        relations=_crossref_relations(record.get("relation")),
    )
    return resolution


def _parse_datacite(payload: Mapping[str, Any], requested_doi: str) -> DOIResolution | None:
    data = payload.get("data", payload)
    if not isinstance(data, Mapping):
        return None
    attributes = data.get("attributes", data)
    if not isinstance(attributes, Mapping):
        return None
    returned_doi = normalize_doi(attributes.get("doi") or data.get("id"))
    if returned_doi != requested_doi:
        return None
    container = attributes.get("container")
    container = container if isinstance(container, Mapping) else {}
    types = attributes.get("types")
    types = types if isinstance(types, Mapping) else {}
    provider_type = _first_text(
        types.get("resourceType"),
        types.get("bibtex"),
        types.get("resourceTypeGeneral"),
        attributes.get("type"),
    )
    resolution = DOIResolution(
        doi=requested_doi,
        provider="DataCite",
        evidence_label="DOI metadata retrieved from DataCite",
        title=_datacite_title(attributes.get("titles")) or _clean_text(attributes.get("title")),
        authors=_datacite_authors(attributes.get("creators")),
        journal=_first_text(
            container.get("title"),
            attributes.get("container-title"),
            attributes.get("containerTitle"),
        ),
        journal_abbreviation=_first_text(
            container.get("titleShort"),
            container.get("shortTitle"),
            attributes.get("short-container-title"),
        ),
        year=_datacite_year(attributes),
        volume=_first_text(container.get("volume"), attributes.get("volume")),
        issue=_first_text(container.get("issue"), attributes.get("issue")),
        document_type=_document_type_from_provider(provider_type),
        provider_type=provider_type,
        publisher=_clean_text(attributes.get("publisher")),
        url=_first_text(attributes.get("url"), attributes.get("landingPage")),
        relations=_datacite_relations(attributes.get("relatedIdentifiers")),
    )
    return resolution


def _resolution_is_useful(resolution: DOIResolution) -> bool:
    # A title is the minimum safe basis for a filename.  If one registry has no
    # title, trying the other exact-DOI registry may give a usable record.
    return bool(resolution.title)


def _clean_text(value: object, *, limit: int = MAX_METADATA_FIELD_LENGTH) -> str:
    if value is None:
        return ""
    cleaned = re.sub(r"\s+", " ", str(value)).strip()
    return cleaned[:limit]


def _first_text(*values: object) -> str:
    for value in values:
        if isinstance(value, (list, tuple)):
            for item in value:
                text = _clean_text(item)
                if text:
                    return text
        else:
            text = _clean_text(value)
            if text:
                return text
    return ""


def _person_name(person: object) -> str:
    if not isinstance(person, Mapping):
        return _clean_text(person)
    literal = _first_text(person.get("literal"), person.get("name"))
    if literal:
        return literal
    given = _first_text(person.get("given"), person.get("givenName"))
    family = _first_text(person.get("family"), person.get("familyName"))
    return _first_text(" ".join(part for part in (given, family) if part))


def _deduplicated_people(people: Iterable[object]) -> tuple[str, ...]:
    names: list[str] = []
    seen: set[str] = set()
    for person in people:
        name = _person_name(person)
        fingerprint = name.casefold()
        if name and fingerprint not in seen:
            names.append(name)
            seen.add(fingerprint)
        if len(names) >= 50:
            break
    return tuple(names)


def _crossref_authors(value: object) -> tuple[str, ...]:
    return _deduplicated_people(value if isinstance(value, (list, tuple)) else ())


def _datacite_authors(value: object) -> tuple[str, ...]:
    return _deduplicated_people(value if isinstance(value, (list, tuple)) else ())


def _datacite_title(value: object) -> str:
    """Select the first ordinary DataCite title from its structured list."""

    if not isinstance(value, (list, tuple)):
        return ""
    for item in value:
        if isinstance(item, Mapping):
            title = _clean_text(item.get("title"))
        else:
            title = _clean_text(item)
        if title:
            return title
    return ""


def _year_from_value(value: object) -> str:
    if isinstance(value, Mapping):
        date_parts = value.get("date-parts") or value.get("dateParts")
        if isinstance(date_parts, (list, tuple)) and date_parts:
            first = date_parts[0]
            if isinstance(first, (list, tuple)) and first:
                return _year_from_value(first[0])
        for key in ("date", "value", "published", "issued"):
            year = _year_from_value(value.get(key))
            if year:
                return year
        return ""
    if isinstance(value, (list, tuple)):
        for item in value:
            year = _year_from_value(item)
            if year:
                return year
        return ""
    match = _YEAR_PATTERN.search(_clean_text(value, limit=100))
    return match.group(0) if match else ""


def _crossref_year(record: Mapping[str, Any]) -> str:
    for key in ("published-print", "published-online", "published", "issued", "created"):
        year = _year_from_value(record.get(key))
        if year:
            return year
    return ""


def _datacite_year(attributes: Mapping[str, Any]) -> str:
    year = _year_from_value(attributes.get("publicationYear"))
    if year:
        return year
    dates = attributes.get("dates")
    if isinstance(dates, (list, tuple)):
        for item in dates:
            if isinstance(item, Mapping):
                year = _year_from_value(item.get("date"))
                if year:
                    return year
    return _year_from_value(attributes.get("issued"))


def _document_type_from_provider(value: object) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", _clean_text(value, limit=100).casefold()).strip("_")
    mappings = {
        "patent": "patent",
        "patent_application": "patent_application",
        "journal_article": "journal_article",
        "article": "journal_article",
        "proceedings_article": "conference_paper",
        "conference_paper": "conference_paper",
        "book_chapter": "book_chapter",
        "book": "book",
        "dissertation": "thesis_dissertation",
        "thesis": "thesis_dissertation",
        "report": "technical_report",
        "standard": "guideline_standard",
        "posted_content": "preprint",
        "preprint": "preprint",
        "presentation": "presentation_poster",
        "poster": "presentation_poster",
        "letter": "letter_note",
        "editorial": "letter_note",
        "webpage": "web_or_other",
        "web_page": "web_or_other",
        "other": "web_or_other",
    }
    return mappings.get(normalized, "unknown")


def _normalize_relation_type(value: object) -> str:
    raw = _clean_text(value, limit=100)
    raw = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", raw)
    return re.sub(r"[^a-z0-9]+", "_", raw.casefold()).strip("_")


def _relation_identifier(value: object) -> str:
    raw = _clean_text(value, limit=2_000)
    return normalize_doi(raw) or raw


def _append_relation(
    relations: list[DOIReference], seen: set[tuple[str, str]], relation_type: object,
    identifier: object, identifier_type: object = "",
) -> None:
    normalized_type = _normalize_relation_type(relation_type)
    normalized_identifier = _relation_identifier(identifier)
    key = (normalized_type, normalized_identifier.casefold())
    if normalized_type and normalized_identifier and key not in seen and len(relations) < 30:
        relations.append(
            DOIReference(
                relation_type=normalized_type,
                identifier=normalized_identifier,
                identifier_type=_clean_text(identifier_type, limit=100),
            )
        )
        seen.add(key)


def _crossref_relations(value: object) -> tuple[DOIReference, ...]:
    if not isinstance(value, Mapping):
        return ()
    relations: list[DOIReference] = []
    seen: set[tuple[str, str]] = set()
    for relation_type, targets in value.items():
        target_list = targets if isinstance(targets, (list, tuple)) else (targets,)
        for target in target_list:
            if isinstance(target, Mapping):
                _append_relation(
                    relations,
                    seen,
                    relation_type,
                    target.get("id") or target.get("DOI") or target.get("url"),
                    target.get("id-type") or target.get("identifier-type"),
                )
            else:
                _append_relation(relations, seen, relation_type, target)
    return tuple(relations)


def _datacite_relations(value: object) -> tuple[DOIReference, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    relations: list[DOIReference] = []
    seen: set[tuple[str, str]] = set()
    for target in value:
        if not isinstance(target, Mapping):
            continue
        _append_relation(
            relations,
            seen,
            target.get("relationType"),
            target.get("relatedIdentifier"),
            target.get("relatedIdentifierType"),
        )
    return tuple(relations)
