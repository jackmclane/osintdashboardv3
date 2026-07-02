"""Cross-source signal detection — the point of the whole tool.

Aggregation is a commodity. The edge is being told *where to look*. This module
scans what we've collected and emits signals that surface at the top of the
dashboard, which is your cue to open X and get the human read on that event.

Three detectors ship (all cheap, no LLM):
  - market_swing : a tracked market's probability moved >= threshold over a window
  - news_spike   : a topic's event volume jumped sharply vs the prior window
  - correlated   : a market_swing and a news_spike land on the same topic in the
                    same run — a market moving AND news volume spiking on the same
                    theme at once is a much stronger tell than either alone.

All deliberately simple heuristics. Tune thresholds in config.yaml.
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone

from . import db
from .normalize import tag as _tag


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _day_bucket(dt: datetime | None = None) -> str:
    return (dt or _now()).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
# market swings
# --------------------------------------------------------------------------- #
def detect_market_swings(
    conn: sqlite3.Connection,
    threshold: float = 0.10,
    window_hours: int = 24,
) -> list[dict]:
    """Flag markets whose probability moved >= threshold (absolute) over the
    window. One signal per market per day (so a persistent swing isn't re-alerted
    every 30 minutes)."""
    cutoff = _iso(_now() - timedelta(hours=window_hours))
    signals: list[dict] = []
    for market_id in db.distinct_market_ids(conn):
        latest = db.latest_probability(conn, market_id)
        past = db.probability_at_or_before(conn, market_id, cutoff)
        if not latest or not past:
            continue
        delta = latest["probability"] - past["probability"]
        if abs(delta) < threshold:
            continue
        direction = "▲" if delta > 0 else "▼"
        now_pct = round(latest["probability"] * 100)
        then_pct = round(past["probability"] * 100)
        signals.append(
            {
                "id": db.signal_id("market_swing", market_id, _day_bucket()),
                "kind": "market_swing",
                "title": f"{direction} {abs(round(delta * 100))}pt swing: "
                         f"{latest['question'][:90]}",
                "detail": f"{latest['platform']}: {then_pct}% → {now_pct}% "
                          f"over ~{window_hours}h",
                "magnitude": round(abs(delta) * 100, 1),
                "url": latest["url"],
            }
        )
    return signals


# --------------------------------------------------------------------------- #
# news spikes
# --------------------------------------------------------------------------- #
def _topics_of(row: sqlite3.Row) -> list[str]:
    return [t.strip() for t in (row["topics"] or "").split(",") if t.strip()]


def detect_news_spikes(
    conn: sqlite3.Connection,
    window_hours: int = 6,
    min_count: int = 4,
    ratio: float = 2.5,
) -> list[dict]:
    """Compare topic volume in the last `window_hours` against the window before
    it. Flag topics that are both busy (>= min_count) and elevated (>= ratio×)."""
    now = _now()
    cur_start = now - timedelta(hours=window_hours)
    prev_start = now - timedelta(hours=2 * window_hours)

    current = db.events_since(conn, _iso(cur_start))
    prev_all = db.events_since(conn, _iso(prev_start))

    def in_prev(row) -> bool:
        ts = row["published_at"] or row["collected_at"]
        return ts is not None and ts < _iso(cur_start)

    cur_counts: Counter = Counter()
    for r in current:
        for t in _topics_of(r):
            if t != "market":  # markets have their own detector
                cur_counts[t] += 1

    prev_counts: Counter = Counter()
    for r in prev_all:
        if in_prev(r):
            for t in _topics_of(r):
                if t != "market":
                    prev_counts[t] += 1

    signals: list[dict] = []
    for topic, cur in cur_counts.items():
        if cur < min_count:
            continue
        prev = prev_counts.get(topic, 0)
        baseline = max(prev, 1)
        if cur / baseline < ratio:
            continue
        signals.append(
            {
                "id": db.signal_id("news_spike", topic, _day_bucket()),
                "kind": "news_spike",
                "title": f"📈 '{topic}' spike: {cur} items in {window_hours}h "
                         f"(was {prev})",
                "detail": f"{cur} events tagged '{topic}' in the last "
                          f"{window_hours}h vs {prev} in the prior {window_hours}h.",
                "magnitude": round(cur / baseline, 1),
                "url": None,
                "_topic": topic,  # internal use only — add_signals ignores extra keys
            }
        )
    return signals


# --------------------------------------------------------------------------- #
# cross-source correlation
# --------------------------------------------------------------------------- #
def _topics_for_text(text: str) -> set[str]:
    _, topics_csv = _tag(text)
    return {t.strip() for t in topics_csv.split(",") if t.strip()}


def detect_correlated(swings: list[dict], spikes: list[dict]) -> list[dict]:
    """Flag when a market_swing and a news_spike share a topic in the same run.
    Both individually are heuristics; the two firing together on the same theme
    is a materially stronger tell than either alone, so this gets surfaced as
    its own higher-priority signal rather than left for the reader to notice."""
    out: list[dict] = []
    for spike in spikes:
        spike_topic = spike.get("_topic")
        if not spike_topic:
            continue
        for swing in swings:
            if spike_topic not in _topics_for_text(swing["title"]):
                continue
            out.append(
                {
                    "id": db.signal_id(
                        "correlated", f"{spike_topic}:{swing['id']}", _day_bucket()
                    ),
                    "kind": "correlated",
                    "title": f"🔗 Market + news both moving on '{spike_topic}'",
                    "detail": f"{swing['title']} — and — {spike['title']}",
                    "magnitude": round(
                        swing.get("magnitude", 0) * spike.get("magnitude", 0), 2
                    ),
                    "url": swing.get("url"),
                }
            )
    return out


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def run(conn: sqlite3.Connection, cfg: dict | None = None) -> int:
    """Run all detectors, store new signals, return how many were new."""
    cfg = cfg or {}
    sc = cfg.get("signals", {})
    swings = detect_market_swings(
        conn,
        threshold=sc.get("market_swing_threshold", 0.10),
        window_hours=sc.get("market_window_hours", 24),
    )
    spikes = detect_news_spikes(
        conn,
        window_hours=sc.get("news_window_hours", 6),
        min_count=sc.get("news_min_count", 4),
        ratio=sc.get("news_ratio", 2.5),
    )
    correlated = detect_correlated(swings, spikes) if sc.get("correlation_enabled", True) else []
    new = db.add_signals(conn, swings + spikes + correlated)
    print(f"[signals] {len(swings)} swing / {len(spikes)} spike / "
          f"{len(correlated)} correlated candidates, +{new} new")
    return new
