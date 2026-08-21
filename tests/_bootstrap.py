"""Shared import-path setup for standalone test scripts moved below the repository root."""
from pathlib import Path
import sys

ROOT = str(Path(__file__).resolve().parent.parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
