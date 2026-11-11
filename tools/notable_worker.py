"""Build entry point for the plugin's private, managed worker."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from note_organizer.zotero_worker import main
if __name__ == "__main__":
    main()
