"""Plugin modules and its self-contained, pinned processing core."""
from pathlib import Path
_core = Path(__file__).resolve().parents[1] / '_vendor' / 'note_organizer'
if _core.is_dir():
    __path__.append(str(_core))
from ._version import APP_NAME, APP_DISPLAY_NAME, __version__
