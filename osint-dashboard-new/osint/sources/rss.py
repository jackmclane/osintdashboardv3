"""Free RSS/Atom ingestion via feedparser. No key required."""
from __future__ import annotations

import feedparser

from ..models import Event
from ..normalize import tag
from .base import Source


class RSSSource(Source):
    def __init__(self, feeds: list[dict]):
        """feeds: list of {"name": ..., "url": ...} dicts from config.yaml."""
        self.feeds = feeds

    def collect(self) -> list[Event]:
        events: list[Event] = []
        for feed in self.feeds:
            name, url = feed.get("name", "RSS"), feed.get("url")
            if not url:
                continue
            try:
                parsed = feedparser.parse(url)
            except Exception as exc:  # noqa: BLE001
                print(f"[rss] FAILED {name} ({url}): {exc}")
                continue
            for entry in parsed.entries:
                title = entry.get("title", "").strip()
                if not title:
                    continue
                summary = (entry.get("summary") or "")[:500]
                published = entry.get("published") or entry.get("updated")
                region, topics = tag(title, summary)
                events.append(
                    Event(
                        source=name,
                        source_type="rss",
                        title=title,
                        url=entry.get("link"),
                        summary=summary,
                        published_at=published,
                        region=region,
                        topics=topics,
                        raw=str(entry.get("id", "")),
                    )
                )
        return events
