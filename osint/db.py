"""SQLite storage. Deliberately small — plain SQL, no ORM.

Phase 1 used a single `events` table. The full build adds three more:
  - market_history : one row per market per run, so we can measure swings
  - signals        : cross-source alerts surfaced at the top of the dashboard
  - ais_positions  : latest vessel position per watched maritime zone

Everything still lives in one committed SQLite file so the free GitHub Actions
writer and the free Streamlit reader can share it. Swap this module for a free
Postgres tier (Supabase/Neon) later without touching the rest of the code.
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import Event

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "osint.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id           TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    source_type  TEXT NOT NULL,
    title        TEXT NOT NULL,
    url          TEXT,
    summary      TEXT,
    published_at TEXT,
    region       TEXT,
    topics       TEXT,
    raw          TEXT,
    collected_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_published ON events(published_at);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(source_type);

-- Time series of market probabilities. Append-only; one row per market per run.
CREATE TABLE IF NOT EXISTS market_history (
    market_id   TEXT NOT NULL,
    platform    TEXT NOT NULL,
    question    TEXT NOT NULL,
    probability REAL NOT NULL,      -- 0..1 for the tracked outcome
    url         TEXT,
    ts          TEXT NOT NULL       -- ISO 8601, when we recorded it
);
CREATE INDEX IF NOT EXISTS idx_mh_market ON market_history(market_id, ts);

-- Cross-source alerts. De-duplicated by id (kind + ref + day bucket).
CREATE TABLE IF NOT EXISTS signals (
    id         TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,       -- "market_swing" | "news_spike"
    title      TEXT NOT NULL,
    detail     TEXT,
    magnitude  REAL,
    url        TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at);

-- Latest AIS position per vessel per watched zone (upserted, not history).
CREATE TABLE IF NOT EXISTS ais_positions (
    mmsi TEXT NOT NULL,
    name TEXT,
    zone TEXT NOT NULL,
    lat  REAL,
    lon  REAL,
    sog  REAL,                       -- speed over ground (knots)
    cog  REAL,                       -- course over ground (degrees)
    ts   TEXT NOT NULL,
    PRIMARY KEY (mmsi, zone)
);
CREATE INDEX IF NOT EXISTS idx_ais_zone ON ais_positions(zone);

-- Latest aircraft position per watched zone (upserted, not history). Mirrors
-- ais_positions but for ADS-B (OpenSky). icao24 is the aircraft's stable
-- transponder address, the aviation equivalent of MMSI.
CREATE TABLE IF NOT EXISTS aircraft_positions (
    icao24    TEXT NOT NULL,
    callsign  TEXT,
    zone      TEXT NOT NULL,
    lat       REAL,
    lon       REAL,
    velocity  REAL,                  -- ground speed, m/s
    heading   REAL,                  -- track angle, degrees
    baro_alt  REAL,                  -- barometric altitude, meters
    ts        TEXT NOT NULL,
    PRIMARY KEY (icao24, zone)
);
CREATE INDEX IF NOT EXISTS idx_aircraft_zone ON aircraft_positions(zone);

-- Entities we've already seen on the OFAC/Commerce/State consolidated
-- screening list. Only genuinely NEW entities (not in this table yet)
-- get turned into an Event + signal — the full list is tens of thousands
-- of rows and re-flagging all of them every run would be pure noise.
CREATE TABLE IF NOT EXISTS sanctions_seen (
    uid         TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    source_list TEXT,                -- which agency list (OFAC SDN, BIS Entity, etc.)
    programs    TEXT,
    remarks     TEXT,
    first_seen  TEXT NOT NULL
);

-- Bookmarked events. Kept as a side table (not a column on events) so
-- starring works regardless of how events.id was generated.
CREATE TABLE IF NOT EXISTS starred (
    event_id   TEXT PRIMARY KEY,
    starred_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


# --------------------------------------------------------------------------- #
# events
# --------------------------------------------------------------------------- #
def upsert_events(conn: sqlite3.Connection, events: list[Event]) -> int:
    """Insert events, ignoring ones already seen (same id). Returns new count."""
    before = conn.total_changes
    conn.executemany(
        """
        INSERT OR IGNORE INTO events
            (id, source, source_type, title, url, summary,
             published_at, region, topics, raw, collected_at)
        VALUES
            (:id, :source, :source_type, :title, :url, :summary,
             :published_at, :region, :topics, :raw, :collected_at)
        """,
        [e.as_row() for e in events],
    )
    conn.commit()
    return conn.total_changes - before


def recent_events(
    conn: sqlite3.Connection,
    limit: int = 200,
    source_type: str | None = None,
) -> list[sqlite3.Row]:
    q = "SELECT * FROM events"
    params: list = []
    if source_type:
        q += " WHERE source_type = ?"
        params.append(source_type)
    q += " ORDER BY COALESCE(published_at, collected_at) DESC LIMIT ?"
    params.append(limit)
    return conn.execute(q, params).fetchall()


def events_since(conn: sqlite3.Connection, since_iso: str) -> list[sqlite3.Row]:
    """All events whose best-known timestamp is >= since_iso. Used by signals."""
    return conn.execute(
        """
        SELECT * FROM events
        WHERE COALESCE(published_at, collected_at) >= ?
        ORDER BY COALESCE(published_at, collected_at) DESC
        """,
        (since_iso,),
    ).fetchall()


# --------------------------------------------------------------------------- #
# market history
# --------------------------------------------------------------------------- #
def record_market_snapshots(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """Append a probability reading for each market. rows: market_id, platform,
    question, probability, url."""
    ts = _now()
    conn.executemany(
        """
        INSERT INTO market_history (market_id, platform, question, probability, url, ts)
        VALUES (:market_id, :platform, :question, :probability, :url, :ts)
        """,
        [{**r, "ts": ts} for r in rows],
    )
    conn.commit()
    return len(rows)


def latest_probability(conn: sqlite3.Connection, market_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM market_history WHERE market_id = ? ORDER BY ts DESC LIMIT 1",
        (market_id,),
    ).fetchone()


def probability_at_or_before(
    conn: sqlite3.Connection, market_id: str, cutoff_iso: str
) -> sqlite3.Row | None:
    """The most recent reading at or before cutoff — i.e. 'where was it ~24h ago'."""
    return conn.execute(
        """
        SELECT * FROM market_history
        WHERE market_id = ? AND ts <= ?
        ORDER BY ts DESC LIMIT 1
        """,
        (market_id, cutoff_iso),
    ).fetchone()


def distinct_market_ids(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT market_id FROM market_history")]


def latest_markets(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """One row per market_id — its most recent snapshot — newest-updated first.
    Powers the Markets tab list."""
    return conn.execute(
        """
        SELECT mh.* FROM market_history mh
        INNER JOIN (
            SELECT market_id, MAX(ts) AS max_ts
            FROM market_history
            GROUP BY market_id
        ) latest ON mh.market_id = latest.market_id AND mh.ts = latest.max_ts
        ORDER BY mh.ts DESC
        """
    ).fetchall()


def market_history_series(conn: sqlite3.Connection, market_id: str) -> list[sqlite3.Row]:
    """Full probability history for one market, oldest first — for trend charts."""
    return conn.execute(
        "SELECT * FROM market_history WHERE market_id = ? ORDER BY ts ASC",
        (market_id,),
    ).fetchall()


# --------------------------------------------------------------------------- #
# signals
# --------------------------------------------------------------------------- #
def signal_id(kind: str, ref: str, bucket: str) -> str:
    return hashlib.sha256(f"{kind}:{ref}:{bucket}".encode()).hexdigest()[:16]


def add_signals(conn: sqlite3.Connection, signals: list[dict]) -> int:
    """Insert signals, ignoring duplicates (same id). Returns new count."""
    before = conn.total_changes
    ts = _now()
    conn.executemany(
        """
        INSERT OR IGNORE INTO signals (id, kind, title, detail, magnitude, url, created_at)
        VALUES (:id, :kind, :title, :detail, :magnitude, :url, :created_at)
        """,
        [{**s, "created_at": s.get("created_at", ts)} for s in signals],
    )
    conn.commit()
    return conn.total_changes - before


def recent_signals(conn: sqlite3.Connection, limit: int = 25) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM signals ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()


# --------------------------------------------------------------------------- #
# AIS
# --------------------------------------------------------------------------- #
def upsert_ais_positions(conn: sqlite3.Connection, rows: list[dict]) -> int:
    ts = _now()
    conn.executemany(
        """
        INSERT INTO ais_positions (mmsi, name, zone, lat, lon, sog, cog, ts)
        VALUES (:mmsi, :name, :zone, :lat, :lon, :sog, :cog, :ts)
        ON CONFLICT(mmsi, zone) DO UPDATE SET
            name=excluded.name, lat=excluded.lat, lon=excluded.lon,
            sog=excluded.sog, cog=excluded.cog, ts=excluded.ts
        """,
        [{**r, "ts": r.get("ts", ts)} for r in rows],
    )
    conn.commit()
    return len(rows)


def ais_zone_summary(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT zone,
               COUNT(*)        AS vessels,
               AVG(sog)        AS avg_speed,
               MAX(ts)         AS last_seen
        FROM ais_positions
        GROUP BY zone
        ORDER BY zone
        """
    ).fetchall()


def ais_positions_for_zone(conn: sqlite3.Connection, zone: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM ais_positions WHERE zone = ? ORDER BY ts DESC", (zone,)
    ).fetchall()


def all_ais_positions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every currently-known vessel position across all zones — for the map."""
    return conn.execute("SELECT * FROM ais_positions ORDER BY ts DESC").fetchall()


# --------------------------------------------------------------------------- #
# aircraft (ADS-B / OpenSky) — mirrors the AIS functions above
# --------------------------------------------------------------------------- #
def upsert_aircraft_positions(conn: sqlite3.Connection, rows: list[dict]) -> int:
    ts = _now()
    conn.executemany(
        """
        INSERT INTO aircraft_positions
            (icao24, callsign, zone, lat, lon, velocity, heading, baro_alt, ts)
        VALUES
            (:icao24, :callsign, :zone, :lat, :lon, :velocity, :heading, :baro_alt, :ts)
        ON CONFLICT(icao24, zone) DO UPDATE SET
            callsign=excluded.callsign, lat=excluded.lat, lon=excluded.lon,
            velocity=excluded.velocity, heading=excluded.heading,
            baro_alt=excluded.baro_alt, ts=excluded.ts
        """,
        [{**r, "ts": r.get("ts", ts)} for r in rows],
    )
    conn.commit()
    return len(rows)


def aircraft_zone_summary(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT zone,
               COUNT(*)        AS aircraft,
               AVG(velocity)   AS avg_velocity,
               MAX(ts)         AS last_seen
        FROM aircraft_positions
        GROUP BY zone
        ORDER BY zone
        """
    ).fetchall()


def aircraft_positions_for_zone(conn: sqlite3.Connection, zone: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM aircraft_positions WHERE zone = ? ORDER BY ts DESC", (zone,)
    ).fetchall()


def all_aircraft_positions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every currently-known aircraft position across all zones — for the map."""
    return conn.execute("SELECT * FROM aircraft_positions ORDER BY ts DESC").fetchall()


# --------------------------------------------------------------------------- #
# sanctions (OFAC / Commerce / State consolidated screening list) — diff-only
# --------------------------------------------------------------------------- #
def seen_sanction_uids(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT uid FROM sanctions_seen")}


def upsert_sanctions_seen(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """Record entities as seen. Insert-or-ignore — first_seen never changes
    for an entity we already know about."""
    ts = _now()
    before = conn.total_changes
    conn.executemany(
        """
        INSERT OR IGNORE INTO sanctions_seen
            (uid, name, source_list, programs, remarks, first_seen)
        VALUES (:uid, :name, :source_list, :programs, :remarks, :first_seen)
        """,
        [{**r, "first_seen": r.get("first_seen", ts)} for r in rows],
    )
    conn.commit()
    return conn.total_changes - before


# --------------------------------------------------------------------------- #
# starred / bookmarked items
# --------------------------------------------------------------------------- #
def star_event(conn: sqlite3.Connection, event_id: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO starred (event_id, starred_at) VALUES (?, ?)",
        (event_id, _now()),
    )
    conn.commit()


def unstar_event(conn: sqlite3.Connection, event_id: str) -> None:
    conn.execute("DELETE FROM starred WHERE event_id = ?", (event_id,))
    conn.commit()


def starred_ids(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT event_id FROM starred")}
