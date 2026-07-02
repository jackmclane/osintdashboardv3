#!/usr/bin/env python3
"""Run one full collection pass. Entry point for the frequent GitHub Actions job."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from osint.collect import run  # noqa: E402

if __name__ == "__main__":
    run()
