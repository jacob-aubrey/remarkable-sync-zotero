"""Safe, dependency-free local extraction for PowerPoint presentations.

The modern .pptx format is an Office Open XML ZIP package, so its visible slide
text and core properties can be read without starting PowerPoint or sending a
file anywhere. The older binary .ppt format is intentionally not parsed here:
it remains an opaque file that can be renamed or moved, but the caller is
clearly told that a person should review its proposed name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import posixpath
import re
from typing import Iterable
import xml.etree.ElementTree as ElementTree
import zipfile


PathLike = str | Path

# Keep these in step with the app's other local document readers. The limits
# apply before the package's XML is parsed, preventing accidental work on an
# unexpectedly large or malformed Office package.
MAX_PRESENTATION_BYTES = 100 * 1024 * 1024
MAX_PPTX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_PPTX_MEMBERS = 10_000
MAX_PPTX_XML_MEMBER_BYTES = 20 * 1024 * 1024
MAX_PPTX_SLIDES = 500
MAX_PRESENTATION_TEXT_CHARS = 24_000

_PRESENTATION_PART = "ppt/presentation.xml"
_PRESENTATION_RELS_PART = "ppt/_rels/presentation.xml.rels"
_CONTENT_TYPES_PART = "[Content_Types].xml"
_CORE_PROPERTIES_PART = "docProps/core.xml"


class PresentationExtractionError(ValueError):
    """A PowerPoint presentation could not be read safely."""


@dataclass
class PresentationExtraction:
    """Text and core properties read locally from one PowerPoint file."""

    text: str
    metadata: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    document_format: str = "PPTX"
    slide_count: int = 0
    requires_manual_review: bool = False


def extract_presentation(
    presentation_path: PathLike,
    *,
    max_chars: int = MAX_PRESENTATION_TEXT_CHARS,
    max_slides: int = MAX_PPTX_SLIDES,
) -> PresentationExtraction:
    """Read a PowerPoint file without launching PowerPoint or using the cloud.

    .pptx files yield bounded visible slide text and any useful core
    properties. Legacy .ppt files deliberately yield no text and a plain
    warning because their binary format cannot be safely parsed with the
    standard library alone.
    """

    _validate_limit(max_chars, "max_chars")
    _validate_limit(max_slides, "max_slides")

    path = Path(presentation_path)
    suffix = path.suffix.casefold()
    if suffix not in {".ppt", ".pptx"}:
        raise PresentationExtractionError("Unsupported presentation type. Supported formats: .ppt and .pptx.")
    if not path.exists() or not path.is_file():
        raise PresentationExtractionError("Presentation file was not found.")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise PresentationExtractionError(f"Could not inspect presentation: {exc}") from exc
    if size <= 0:
        raise PresentationExtractionError("Presentation is empty.")
    if size > MAX_PRESENTATION_BYTES:
        raise PresentationExtractionError(
            f"Presentation is larger than the {MAX_PRESENTATION_BYTES // (1024 * 1024)} MB safety limit."
        )

    if suffix == ".ppt":
        return PresentationExtraction(
            text="",
            warnings=[
                "Legacy .ppt text extraction is unsupported. The presentation can be moved or renamed, "
                "but review its proposed name manually."
            ],
            document_format="PPT",
            requires_manual_review=True,
        )
    return _extract_pptx(path, max_chars=max_chars, max_slides=max_slides)


def _validate_limit(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive whole number.")


def _extract_pptx(path: Path, *, max_chars: int, max_slides: int) -> PresentationExtraction:
    try:
        with zipfile.ZipFile(path) as archive:
            member_names = _validate_pptx_archive(archive)
            _validate_content_types(archive)
            presentation = _read_xml_member(archive, _PRESENTATION_PART)
            if _local_name(presentation.tag) != "presentation":
                raise PresentationExtractionError("PPTX does not contain a valid presentation part.")

            warnings: list[str] = []
            metadata = _read_core_properties(archive, member_names, warnings)
            slide_members, declared_slide_count = _ordered_slide_members(
                archive, member_names, presentation, warnings
            )
            incomplete_slides = len(slide_members) != declared_slide_count

            parts: list[str] = []
            used = 0
            text_truncated = False
            slide_limit_reached = len(slide_members) > max_slides
            if slide_limit_reached:
                warnings.append(f"Only the first {max_slides} PowerPoint slides were read.")

            for slide_member in slide_members[:max_slides]:
                try:
                    slide = _read_xml_member(archive, slide_member)
                except PresentationExtractionError as exc:
                    warnings.append(f"Could not read a PowerPoint slide: {exc}")
                    incomplete_slides = True
                    continue
                slide_text = _slide_text(slide)
                used, did_truncate = _append_bounded_text(parts, slide_text, used, max_chars)
                text_truncated = text_truncated or did_truncate
                if text_truncated:
                    break

            if text_truncated:
                warnings.append("Extracted PowerPoint text was shortened to the safety limit.")
            text = "".join(parts)
            if not text:
                warnings.append("No extractable PowerPoint slide text was found.")
            return PresentationExtraction(
                text=text,
                metadata=metadata,
                warnings=warnings,
                document_format="PPTX",
                slide_count=declared_slide_count,
                requires_manual_review=not bool(text) or incomplete_slides or slide_limit_reached or text_truncated,
            )
    except PresentationExtractionError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise PresentationExtractionError("File does not contain a valid PPTX package.") from exc
    except OSError as exc:
        raise PresentationExtractionError(f"Could not read PPTX: {exc}") from exc


def _validate_pptx_archive(archive: zipfile.ZipFile) -> set[str]:
    """Validate basic ZIP package bounds before opening XML members."""

    infos = archive.infolist()
    if len(infos) > MAX_PPTX_MEMBERS:
        raise PresentationExtractionError("PPTX contains too many package members to inspect safely.")

    names: set[str] = set()
    total_uncompressed = 0
    for info in infos:
        name = info.filename
        if name in names:
            raise PresentationExtractionError("PPTX contains duplicate package members and cannot be read safely.")
        names.add(name)
        total_uncompressed += info.file_size
        if total_uncompressed > MAX_PPTX_UNCOMPRESSED_BYTES:
            raise PresentationExtractionError("PPTX expands beyond the safety limit and was not opened.")

    required = {_CONTENT_TYPES_PART, _PRESENTATION_PART}
    if not required.issubset(names):
        raise PresentationExtractionError("File is not a complete PPTX presentation package.")
    return names


def _validate_content_types(archive: zipfile.ZipFile) -> None:
    content_types = _read_xml_member(archive, _CONTENT_TYPES_PART)
    if _local_name(content_types.tag) != "Types":
        raise PresentationExtractionError("PPTX does not contain valid package content types.")


def _read_core_properties(
    archive: zipfile.ZipFile, member_names: set[str], warnings: list[str]
) -> dict[str, str]:
    if _CORE_PROPERTIES_PART not in member_names:
        return {}
    try:
        root = _read_xml_member(archive, _CORE_PROPERTIES_PART)
    except PresentationExtractionError as exc:
        warnings.append(f"Could not read PowerPoint core properties: {exc}")
        return {}
    if _local_name(root.tag) != "coreProperties":
        warnings.append("PowerPoint core properties were not in the expected format.")
        return {}

    values = {
        "title": _first_named_text(root, "title"),
        "author": _first_named_text(root, "creator"),
        "subject": _first_named_text(root, "subject"),
        "keywords": _first_named_text(root, "keywords"),
        "created": _first_named_text(root, "created"),
        "modified": _first_named_text(root, "modified"),
        "last_modified_by": _first_named_text(root, "lastModifiedBy"),
    }
    return {key: value for key, value in values.items() if value}


def _ordered_slide_members(
    archive: zipfile.ZipFile,
    member_names: set[str],
    presentation: ElementTree.Element,
    warnings: list[str],
) -> tuple[list[str], int]:
    """Resolve slides in the order recorded by ppt/presentation.xml."""

    slide_ids = [element for element in presentation.iter() if _local_name(element.tag) == "sldId"]
    if not slide_ids:
        return [], 0
    if _PRESENTATION_RELS_PART not in member_names:
        raise PresentationExtractionError("PPTX is missing the relationship map for its slides.")

    relationships = _presentation_relationships(archive)
    members: list[str] = []
    for slide_id in slide_ids:
        relationship_id = _attribute_by_local_name(slide_id, "id")
        if not relationship_id:
            warnings.append("A PowerPoint slide reference was missing its relationship identifier.")
            continue
        relationship = relationships.get(relationship_id)
        if relationship is None:
            warnings.append("A PowerPoint slide reference was missing from the relationship map.")
            continue
        target, target_mode, relationship_type = relationship
        if target_mode.casefold() == "external":
            warnings.append("An external PowerPoint slide reference was skipped.")
            continue
        if relationship_type and not relationship_type.casefold().endswith("/slide"):
            warnings.append("A non-slide PowerPoint relationship was skipped.")
            continue
        member = _resolve_relationship_target(_PRESENTATION_PART, target)
        if member is None or member not in member_names:
            warnings.append("A referenced PowerPoint slide was missing or unsafe and was skipped.")
            continue
        members.append(member)
    return members, len(slide_ids)


def _presentation_relationships(archive: zipfile.ZipFile) -> dict[str, tuple[str, str, str]]:
    root = _read_xml_member(archive, _PRESENTATION_RELS_PART)
    relationships: dict[str, tuple[str, str, str]] = {}
    for element in root.iter():
        if _local_name(element.tag) != "Relationship":
            continue
        relationship_id = element.attrib.get("Id", "").strip()
        if not relationship_id:
            continue
        if relationship_id in relationships:
            raise PresentationExtractionError("PPTX contains duplicate slide relationship identifiers.")
        target = element.attrib.get("Target", "").strip()
        target_mode = element.attrib.get("TargetMode", "").strip()
        relationship_type = element.attrib.get("Type", "").strip()
        relationships[relationship_id] = (target, target_mode, relationship_type)
    return relationships


def _resolve_relationship_target(source_member: str, target: str) -> str | None:
    """Return a safe, package-relative relationship target, if one exists."""

    clean_target = target.strip().replace("\\", "/")
    if not clean_target or "://" in clean_target or clean_target.startswith("//"):
        return None
    if clean_target.startswith("/"):
        resolved = posixpath.normpath(clean_target.lstrip("/"))
    else:
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source_member), clean_target))
    if resolved in {"", ".", ".."} or resolved.startswith("../"):
        return None
    return resolved


def _read_xml_member(archive: zipfile.ZipFile, member_name: str) -> ElementTree.Element:
    try:
        info = archive.getinfo(member_name)
    except KeyError as exc:
        raise PresentationExtractionError(f"PPTX is missing required part: {member_name}.") from exc
    if info.file_size > MAX_PPTX_XML_MEMBER_BYTES:
        raise PresentationExtractionError(
            f"PPTX part {member_name} exceeds the {MAX_PPTX_XML_MEMBER_BYTES // (1024 * 1024)} MB XML safety limit."
        )
    try:
        contents = archive.read(info)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise PresentationExtractionError(f"Could not read PPTX part {member_name}.") from exc
    try:
        return ElementTree.fromstring(contents)
    except (ElementTree.ParseError, UnicodeError) as exc:
        raise PresentationExtractionError(f"PPTX part {member_name} contains invalid XML.") from exc


def _slide_text(slide: ElementTree.Element) -> str:
    paragraphs: list[str] = []
    for paragraph in slide.iter():
        if _local_name(paragraph.tag) != "p":
            continue
        runs = [
            element.text or ""
            for element in paragraph.iter()
            if _local_name(element.tag) == "t" and element.text
        ]
        text = _clean_text("".join(runs))
        if text:
            paragraphs.append(text)

    # A malformed but recoverable slide may contain text runs without paragraph
    # nodes. Preserve that text rather than silently returning an empty slide.
    if not paragraphs:
        runs = [element.text or "" for element in slide.iter() if _local_name(element.tag) == "t" and element.text]
        fallback = _clean_text("".join(runs))
        if fallback:
            paragraphs.append(fallback)
    return "\n".join(paragraphs)


def _append_bounded_text(parts: list[str], value: str, used: int, limit: int) -> tuple[int, bool]:
    clean = _clean_text(value)
    if not clean:
        return used, False
    separator = "\n\n" if parts else ""
    remaining = limit - used
    if remaining <= len(separator):
        return used, True
    available = remaining - len(separator)
    fragment = clean[:available].rstrip()
    if not fragment:
        return used, True
    parts.append(f"{separator}{fragment}")
    return used + len(separator) + len(fragment), len(fragment) < len(clean)


def _first_named_text(root: ElementTree.Element, name: str) -> str:
    for element in root.iter():
        if _local_name(element.tag) == name:
            return _clean_text("".join(element.itertext()))
    return ""


def _attribute_by_local_name(element: ElementTree.Element, name: str) -> str:
    # p:sldId has both a numeric unqualified id and a namespaced relationship
    # identifier. Prefer the namespaced attribute.
    for key, value in element.attrib.items():
        if key.startswith("{") and _local_name(key) == name:
            return value.strip()
    for key, value in element.attrib.items():
        if _local_name(key) == name:
            return value.strip()
    return ""


def _local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1]


def _clean_text(value: str | Iterable[str]) -> str:
    if not isinstance(value, str):
        value = "".join(value)
    return re.sub(r"\s+", " ", value.replace("\x00", " ")).strip()


__all__ = [
    "MAX_PRESENTATION_TEXT_CHARS",
    "MAX_PPTX_SLIDES",
    "PresentationExtraction",
    "PresentationExtractionError",
    "extract_presentation",
]
