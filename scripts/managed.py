"""Use the script's explicit checkout even with -I and an obsolete editable install."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from repowatch.release_cli import main

if __name__ == "__main__":
    main()
