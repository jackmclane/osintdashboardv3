"""GDELT 2.0 DOC API — free, no key, global news monitoring.

Docs: https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/
"""
from __future__ import annotations

import requests

from ..models import Event
from ..normalize import tag
from .base import Source

GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"


class GDELTSource(Source):
    def __init__(self, queries: list[str], timespan: str = "1h", max_records: int = 75):
        self.queries = queries
        self.timespan = timespan
        self.max_records = max_records

    def _fetch_one(self, query: str) -> list[dict]:
        params = {
            "query": query,
            "mode": "artlist",
            "format": "json",
            "timespan": self.timespan,
            "maxrecords": self.max_records,
            "sort": "datedesc",
        }
        resp = requests.get(GDELT_URL, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return data.get("articles", [])

    def collect(self) -> list[Event]:
        events: list[Event] = []
        for query in self.queries:
            try:
                articles = self._fetch_one(query)
            except Exception as exc:  # noqa: BLE001
                print(f"[gdelt] FAILED query={query!r}: {exc}")
                continue
            for art in articles:
                title = (art.get("title") or "").strip()
                if not title:
                    continue
                region, topics = tag(title, "")
                events.append(
                    Event(
                        source=art.get("sourcecountry", "GDELT") or "GDELT",
                        source_type="gdelt",
                        title=title,
                        url=art.get("url"),
                        summary="",
                        published_at=art.get("seendate"),
                        region=region,
                        topics=topics,
                        raw=str(art.get("domain", "")),
                    )
                )
        return events
