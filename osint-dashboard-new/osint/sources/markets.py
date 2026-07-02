"""Prediction market probabilities — free, no key, Polymarket's public Gamma API.

Docs: https://docs.polymarket.com/  (Gamma markets endpoint is public/unauthenticated)

Each collect() run both (a) emits an Event for markets it hasn't seen before
(so they show up once in the news-style feed) and (b) records a probability
snapshot for every currently-tracked market via `self.snapshots`, which
osint/collect.py picks up and writes to market_history for trend charts and
swing detection.
"""
from __future__ import annotations

import requests

from ..models import Event
from .base import Source

GAMMA_URL = "https://gamma-api.polymarket.com/markets"


class MarketsSource(Source):
    def __init__(self, keywords: list[str], limit: int = 40, min_volume: float = 0.0):
        self.keywords = [k.lower() for k in keywords]
        self.limit = limit
        self.min_volume = min_volume
        self.snapshots: list[dict] = []  # populated by collect()

    def _matches(self, question: str) -> bool:
        if not self.keywords:
            return True
        q = question.lower()
        return any(k in q for k in self.keywords)

    def collect(self) -> list[Event]:
        self.snapshots = []
        events: list[Event] = []
        try:
            resp = requests.get(
                GAMMA_URL,
                params={"active": "true", "closed": "false", "limit": self.limit},
                timeout=30,
            )
            resp.raise_for_status()
            markets = resp.json()
        except Exception as exc:  # noqa: BLE001
            print(f"[markets] FAILED to fetch Polymarket gamma API: {exc}")
            return []

        for m in markets:
            question = (m.get("question") or "").strip()
            if not question or not self._matches(question):
                continue
            try:
                volume = float(m.get("volume") or 0)
            except (TypeError, ValueError):
                volume = 0.0
            if volume < self.min_volume:
                continue

            prob = self._extract_probability(m)
            if prob is None:
                continue

            market_id = str(m.get("id") or m.get("conditionId") or question)
            slug = m.get("slug") or ""
            url = f"https://polymarket.com/event/{slug}" if slug else None

            self.snapshots.append(
                {
                    "market_id": market_id,
                    "platform": "Polymarket",
                    "question": question,
                    "probability": prob,
                    "url": url,
                }
            )

            events.append(
                Event(
                    source="Polymarket",
                    source_type="market",
                    title=f"{question} ({round(prob * 100)}%)",
                    url=url,
                    summary=f"Implied probability: {round(prob * 100)}%",
                    published_at=None,
                    region="",
                    topics="market",
                    raw=market_id,
                )
            )
        return events

    @staticmethod
    def _extract_probability(m: dict) -> float | None:
        """Gamma markets expose outcome prices as a JSON-encoded string list,
        e.g. '["0.73", "0.27"]' aligned with outcomes '["Yes", "No"]'. Take
        the "Yes" price when present, else the first outcome's price."""
        import json

        try:
            prices = json.loads(m.get("outcomePrices") or "[]")
            outcomes = json.loads(m.get("outcomes") or "[]")
        except (ValueError, TypeError):
            return None
        if not prices:
            return None
        if outcomes:
            for outcome, price in zip(outcomes, prices):
                if str(outcome).strip().lower() == "yes":
                    try:
                        return float(price)
                    except (TypeError, ValueError):
                        return None
        try:
            return float(prices[0])
        except (TypeError, ValueError):
            return None
