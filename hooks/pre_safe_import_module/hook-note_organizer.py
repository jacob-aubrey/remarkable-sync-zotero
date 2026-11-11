"""Tell PyInstaller where the plugin's pinned processing modules live."""
from pathlib import Path


def pre_safe_import_module(api):
    api.append_package_path(str(Path(__file__).resolve().parents[2] / '_vendor' / 'note_organizer'))
