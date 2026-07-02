"""OFAC / Commerce / State consolidated screening list — diff-only collector.

Free, no key, no JavaScript required. Uses the Commerce Dept's downloadable
mirror of the consolidated screening list (OFAC SDN, BIS Entity/Denied
Persons/Unverified lists, State Dept debarred list, etc — one file, updated
daily ~5am ET by the source agencies):

  https://www.trade.gov/consolidated-screening-list
  https://data.trade.gov/downloadable_consolidated_screening_list/v1/consolidated.csv

This is deliberately NOT wired into the 30-min collect.py orchestrator: the
file is multi-MB and only updates once a day, so polling it every 30 min
would be pure waste. Run scripts/collect_sanctions.py on its own low-frequency
schedule instead (see .github/workflows/sanctions.yml).

We only care about entities we HAVEN'T seen before — the list has tens of
thousands of rows, and re-flagging all of them every run would bury the
signal in noise. The first run seeds the baseline silently; every run after
that only emits an Event + a signal for genuinely new additions.

Column names in this CSV aren't contractually guaranteed by the source, so
headers are matched case-insensitively with a couple of fallback spellings
rather than hardcoded to one exact casing.
"""
from __future__ import annotations

import csv
import hashlib
import io
import sqlite3
from datetime import datetime, timezone

import requests

from . import db
from .models import Event

CSL_URL = "https://data.trade.gov/downloadable_consolidated_screening_list/v1/consolidated.csv"

_FIELD_CANDIDATES = {
    "uid": ["id", "entity_number", "ent_num", "uid"],
    "name": ["name", "sdn_name"],
    "source_list": ["source", "source_list", "list"],
    "programs": ["programs", "program"],
    "remarks": ["remarks", "remark"],
}


def _pick(row: dict, candidates: list[str]) -> str:
    lower = {k.lower(): v for k, v in row.items() if v is not None}
    for c in candidates:
        if c in lower and str(lower[c]).strip():
            return str(lower[c]).strip()
    return ""


def _row_uid(row: dict) -> str:
    uid = _pick(row, _FIELD_CANDIDATES["uid"])
    if uid:
        return uid
    # No id-like column found — fall back to a stable hash of name+source so
    # this still dedupes sensibly instead of silently dropping the row.
    basis = f"{_pick(row, _FIELD_CANDIDATES['name'])}:{_pick(row, _FIELD_CANDIDATES['source_list'])}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def fetch_rows(timeout: int = 60) -> list[dict]:
    """Pure-ish network call — kept separate from parsing so diff_and_store
    is unit-testable without a live download."""
    resp = requests.get(
        CSL_URL, timeout=timeout, headers={"User-Agent": "personal-osint/1.0"}
    )
    resp.raise_for_status()
    reader = csv.DictReader(io.StringIO(resp.text))
    return list(reader)


def diff_and_store(conn: sqlite3.Connection, rows: list[dict]) -> list[Event]:
    """Compare against sanctions_seen, persist the full current set as seen,
    and return Events only for entities that are genuinely new this run."""
    already_seen = db.seen_sanction_uids(conn)
    is_first_run = len(already_seen) == 0

    parsed = [
        {
            "uid": _row_uid(row),
            "name": _pick(row, _FIELD_CANDIDATES["name"]) or "Unknown entity",
            "source_list": _pick(row, _FIELD_CANDIDATES["source_list"]),
            "programs": _pick(row, _FIELD_CANDIDATES["programs"]),
            "remarks": _pick(row, _FIELD_CANDIDATES["remarks"])[:500],
        }
        for row in rows
    ]

    new_rows = [r for r in parsed if r["uid"] not in already_seen]
    db.upsert_sanctions_seen(conn, parsed)

    if is_first_run:
        # Baseline seed — don't flood with thousands of "new" events on first
        # activation. Real diffs start showing up from the next run onward.
        return []

    return [
        Event(
            source=r["source_list"] or "Consolidated Screening List",
            source_type="sanctions",
            title=f"New listing: {r['name']}",
            url="https://www.trade.gov/consolidated-screening-list",
            summary=(f"Program(s): {r['programs']}" if r["programs"]
                     else r["remarks"] or ""),
            published_at=datetime.now(timezone.utc).isoformat(),
            region="",
            topics="policy, sanctions",
            raw="",
        )
        for r in new_rows
    ]


def run(conn: sqlite3.Connection) -> int:
    """Fetch, diff, store new entities as events, fire one batched signal if
    any are new. Returns the number of new listings found."""
    try:
        rows = fetch_rows()
    except Exception as exc:  # noqa: BLE001 — never let a bad fetch crash the run
        print(f"[sanctions] FAILED to fetch consolidated screening list: {exc}")
        return 0

    events = diff_and_store(conn, rows)
    new_count = db.upsert_events(conn, events) if events else 0

    if events:
        bucket = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H")
        sig = {
            "id": db.signal_id("sanctions_listing", "batch", bucket),
            "kind": "sanctions_listing",
            "title": f"🚫 {len(events)} new sanctions/denied-party listing(s)",
            "detail": "; ".join(e.title for e in events[:5])
                      + (" ..." if len(events) > 5 else ""),
            "magnitude": float(len(events)),
            "url": "https://www.trade.gov/consolidated-screening-list",
        }
        db.add_signals(conn, [sig])

    print(f"[sanctions] {len(rows)} entities checked, {len(events)} new, "
          f"{new_count} stored as events")
    return len(events)
