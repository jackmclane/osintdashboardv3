#!/usr/bin/env python3
"""Generate the daily brief. Entry point for the once-a-day GitHub Actions job."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from osint import brief, db  # noqa: E402
from osint.collect import load_config  # noqa: E402

if __name__ == "__main__":
    cfg = load_config()
    conn = db.connect()
    db.init_db(conn)
    brief.generate(conn, cfg)
    conn.close()
