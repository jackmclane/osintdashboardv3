#!/usr/bin/env python3
"""ADS-B aircraft position collector — free tier via OpenSky Network REST API.

Docs: https://openskynetwork.github.io/opensky-api/rest.html

Anonymous access works but is heavily rate-limited and known to block
requests from cloud/datacenter IPs (including GitHub Actions runners) —
if that happens for you, register a free OpenSky account and set
OPENSKY_CLIENT_ID / OPENSKY_CLIENT_SECRET (OAuth2 client_credentials) to
raise the limit. Either way this source is optional; the dashboard works
fine without it.

Set `adsb.enabled: true` and list zones (bounding boxes) under `adsb.zones`
in config.yaml, then run this on its own schedule (.github/workflows/adsb.yml).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from osint import db  # noqa: E402
from osint.collect import load_config  # noqa: E402

STATES_URL = "https://opensky-network.org/api/states/all"
TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network/"
    "protocol/openid-connect/token"
)

# Index of each field within a state vector, per the OpenSky REST docs.
IDX = {
    "icao24": 0,
    "callsign": 1,
    "longitude": 5,
    "latitude": 6,
    "baro_altitude": 7,
    "velocity": 9,
    "true_track": 10,
}


def _get_token() -> str | None:
    client_id = os.environ.get("OPENSKY_CLIENT_ID", "")
    client_secret = os.environ.get("OPENSKY_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        return None
    try:
        resp = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json().get("access_token")
    except Exception as exc:  # noqa: BLE001
        print(f"[adsb] FAILED to get OAuth token, falling back to anonymous: {exc}")
        return None


def state_to_row(state: list, zone: str) -> dict | None:
    try:
        lat, lon = state[IDX["latitude"]], state[IDX["longitude"]]
    except IndexError:
        return None
    if lat is None or lon is None:
        return None
    callsign = state[IDX["callsign"]]
    return {
        "icao24": state[IDX["icao24"]],
        "callsign": (callsign or "").strip() or None,
        "zone": zone,
        "lat": lat,
        "lon": lon,
        "velocity": state[IDX["velocity"]] if len(state) > IDX["velocity"] else None,
        "heading": state[IDX["true_track"]] if len(state) > IDX["true_track"] else None,
        "baro_alt": state[IDX["baro_altitude"]] if len(state) > IDX["baro_altitude"] else None,
    }


def collect_zone(token: str | None, zone_name: str, bbox: list) -> list[dict]:
    """bbox: [lamin, lomin, lamax, lomax] per the OpenSky REST API."""
    lamin, lomin, lamax, lomax = bbox
    params = {"lamin": lamin, "lomin": lomin, "lamax": lamax, "lomax": lomax}
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        resp = requests.get(STATES_URL, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        print(f"[adsb] FAILED zone={zone_name}: {exc}")
        return []
    states = data.get("states") or []
    rows = [state_to_row(s, zone_name) for s in states]
    return [r for r in rows if r]


def main() -> None:
    cfg = load_config()
    ac = cfg.get("adsb", {})
    if not ac.get("enabled", False):
        print("[adsb] disabled in config.yaml — skipping")
        return

    zones = ac.get("zones", [])
    if not zones:
        print("[adsb] no zones configured under adsb.zones — skipping")
        return

    token = _get_token()
    conn = db.connect()
    db.init_db(conn)

    total = 0
    for z in zones:
        name, bbox = z.get("name"), z.get("bbox")
        if not name or not bbox:
            continue
        rows = collect_zone(token, name, bbox)
        if rows:
            db.upsert_aircraft_positions(conn, rows)
            total += len(rows)
        print(f"[adsb] zone={name}: {len(rows)} aircraft")

    conn.close()
    print(f"[adsb] DONE — {total} position(s) upserted")


if __name__ == "__main__":
    main()
