#!/usr/bin/env python3
"""AIS vessel position collector — free tier via aisstream.io (WebSocket, needs
a free API key: https://aisstream.io). Optional: dashboard works fine without it.

Set AISSTREAM_API_KEY, set `ais.enabled: true` and list zones (bounding boxes)
under `ais.zones` in config.yaml, then run this on its own schedule
(.github/workflows/ais.yml). Each run opens a short-lived WebSocket, collects
whatever position reports arrive in `ais.listen_seconds`, and upserts the
latest position per vessel per zone.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from osint import db  # noqa: E402
from osint.collect import load_config  # noqa: E402

try:
    import websockets
except ImportError:
    websockets = None

AISSTREAM_URL = "wss://stream.aisstream.io/v0/stream"


async def collect_zone(api_key: str, zone_name: str, bbox: list, listen_seconds: int) -> list[dict]:
    rows: dict[tuple, dict] = {}
    subscribe = {
        "APIKey": api_key,
        "BoundingBoxes": [bbox],
    }
    try:
        async with websockets.connect(AISSTREAM_URL) as ws:
            await ws.send(json.dumps(subscribe))
            try:
                async with asyncio.timeout(listen_seconds):
                    async for raw in ws:
                        msg = json.loads(raw)
                        report = msg.get("Message", {}).get("PositionReport")
                        meta = msg.get("MetaData", {})
                        if not report:
                            continue
                        mmsi = str(meta.get("MMSI", ""))
                        if not mmsi:
                            continue
                        rows[mmsi] = {
                            "mmsi": mmsi,
                            "name": meta.get("ShipName", "").strip() or None,
                            "zone": zone_name,
                            "lat": report.get("Latitude"),
                            "lon": report.get("Longitude"),
                            "sog": report.get("Sog"),
                            "cog": report.get("Cog"),
                        }
            except TimeoutError:
                pass
    except Exception as exc:  # noqa: BLE001
        print(f"[ais] FAILED zone={zone_name}: {exc}")
    return list(rows.values())


async def main_async() -> None:
    cfg = load_config()
    ac = cfg.get("ais", {})
    if not ac.get("enabled", False):
        print("[ais] disabled in config.yaml — skipping")
        return

    api_key = os.environ.get("AISSTREAM_API_KEY", "")
    if not api_key:
        print("[ais] AISSTREAM_API_KEY not set — skipping (this source is optional)")
        return
    if websockets is None:
        print("[ais] websockets package not installed — skipping")
        return

    zones = ac.get("zones", [])
    if not zones:
        print("[ais] no zones configured under ais.zones — skipping")
        return

    listen_seconds = ac.get("listen_seconds", 45)
    conn = db.connect()
    db.init_db(conn)

    total = 0
    for z in zones:
        name, bbox = z.get("name"), z.get("bbox")
        if not name or not bbox:
            continue
        print(f"[ais] listening on zone={name} for {listen_seconds}s ...")
        rows = await collect_zone(api_key, name, bbox, listen_seconds)
        if rows:
            db.upsert_ais_positions(conn, rows)
            total += len(rows)
        print(f"[ais] zone={name}: {len(rows)} vessel(s)")

    conn.close()
    print(f"[ais] DONE — {total} position(s) upserted")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
