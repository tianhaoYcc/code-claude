from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    # Support direct execution:
    # python E:\code claude\coding_agent\agent\__main__.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from agent.cli import main
else:
    from .cli import main


if __name__ == "__main__":
    main()
