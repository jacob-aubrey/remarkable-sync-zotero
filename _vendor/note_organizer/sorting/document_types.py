"""Small, dependency-free helpers for files the sorter can process.

This module is deliberately kept separate from the extraction and AI code so the
background watcher can use it without importing the Gemini or PDF libraries.
"""

from __future__ import annotations

from pathlib import Path
from typing import Union


SUPPORTED_DOCUMENT_EXTENSIONS = frozenset({".pdf", ".docx", ".ppt", ".pptx"})

PathLike = Union[str, Path]


def document_suffix(path: PathLike) -> str:
    """Return a normalized file suffix (including the leading period)."""

    return Path(path).suffix.lower()


def is_office_temporary_file(path: PathLike) -> bool:
    """Return whether *path* is an Office lock/temp file.

    Word commonly creates files such as ``~$Draft.docx`` while a document is
    open.  They are not complete documents and must never enter the watcher or
    processing queue.
    """

    return Path(path).name.startswith("~$")


def is_supported_document(path: PathLike) -> bool:
    """Return whether *path* has one of the application's supported formats."""

    return document_suffix(path) in SUPPORTED_DOCUMENT_EXTENSIONS


def is_processable_document(path: PathLike) -> bool:
    """Return whether *path* is a supported, non-temporary document file."""

    return is_supported_document(path) and not is_office_temporary_file(path)
