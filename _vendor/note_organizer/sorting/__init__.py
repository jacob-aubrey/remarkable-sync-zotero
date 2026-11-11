"""Local document naming and optional DOI/Gemini lookup for the Inbox.

Extraction is lazy: importing the combined app does not load PDF, Office, or
Gemini libraries and does not contact online services.
"""

FILENAME_FORMAT_LABELS = {
    "smart": "Smart (choose by document type)",
    "journal_compact": "Author, journal abbreviation, year",
    "journal_detailed": "Author, journal, volume, issue, year",
    "author_year_title": "Author, year, title",
    "title_year_type": "Title, year, document type",
    "custom": "Custom template",
}

__all__ = ["FILENAME_FORMAT_LABELS"]
