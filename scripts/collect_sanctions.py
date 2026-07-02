#!/usr/bin/env python3
"""Consolidated screening list (OFAC/BIS/State) diff collector.

Deliberately its own low-frequency schedule (.github/workflows/sanctions.yml)
— see osint/sanctions.py docstring for why this isn't in the main collect.py loop.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from osint import db, sanctions  # noqa: E402
from osint.collect import load_config  # noqa: E402

if __name__ == "__main__":
    cfg = load_config()
    if not cfg.get("sanctions", {}).get("enabled", False):
        print("[sanctions] disabled in config.yaml — skipping")
        sys.exit(0)
    conn = db.connect()
    db.init_db(conn)
    sanctions.run(conn)
    conn.close()
