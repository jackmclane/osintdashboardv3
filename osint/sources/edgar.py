"""SEC EDGAR full-text search — free, no key, official SEC endpoint.

Docs: https://www.sec.gov/cgi-bin/srqsb?text=... (legacy) — current API:
      https://efts.sec.gov/LATEST/search-index?q=...

SEC's fair-access policy requires a descriptive User-Agent identifying who's
making the request (name + contact). Without one, requests get rate-limited
or blocked, so this source silently no-ops if `edgar.user_agent` in
config.yaml doesn't look like it contains a real contact ("@").

This is phrase search over recent filings, not a firehose — set
`edgar.keywords` in config.yaml to the specific phrases you care about
(company names, deal terms, sanctions language, etc).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests

from ..models import Event
from .base import Source

EFTS_URL = "https://efts.sec.gov/LATEST/search-index"


def _filing_url(hit: dict) -> str | None:
    """Build the public Archives URL for a full-text-search hit.

    Verified shape: hit["_id"] is like "0001628280-26-046349:x.htm" and
    hit["_source"]["cik"] gives the filer's CIK (list, first entry used).
    """
    src = hit.get("_source", {})
    ciks = src.get("cik") or []
    cik = str(ciks[0]) if ciks else None
    raw_id = hit.get("_id", "")
    if not cik or ":" not in raw_id:
        return None
    accession, doc = raw_id.split(":", 1)
    accession_nodash = accession.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_nodash}/{doc}"


def hit_to_event(hit: dict) -> Event | None:
    src = hit.get("_source", {})
    title = (src.get("display_names") or [src.get("form", "SEC filing")])[0]
    form = src.get("form", "")
    filed = src.get("file_date")
    url = _filing_url(hit)
    if not url:
        return None
    return Event(
        source="SEC EDGAR",
        source_type="filing",
        title=f"{title} — {form} filing",
        url=url,
        summary=", ".join(src.get("display_names", [])[:5]),
        published_at=filed,
        region="",
        topics="policy, filing",
        raw=hit.get("_id", ""),
    )


class EDGARSource(Source):
    def __init__(
        self,
        keywords: list[str],
        forms: list[str],
        user_agent: str,
        lookback_hours: int = 24,
        max_records: int = 10,
    ):
        self.keywords = keywords
        self.forms = forms
        self.user_agent = user_agent
        self.lookback_hours = lookback_hours
        self.max_records = max_records

    def _search(self, keyword: str) -> list[dict]:
        since = (datetime.now(timezone.utc) - timedelta(hours=self.lookback_hours)).date()
        params = {
            "q": keyword,
            "dateRange": "custom",
            "startdt": since.isoformat(),
            "enddt": datetime.now(timezone.utc).date().isoformat(),
            "forms": ",".join(self.forms) if self.forms else None,
        }
        params = {k: v for k, v in params.items() if v}
        resp = requests.get(
            EFTS_URL, params=params, timeout=30,
            headers={"User-Agent": self.user_agent},
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("hits", {}).get("hits", [])[: self.max_records]

    def collect(self) -> list[Event]:
        if "@" not in (self.user_agent or ""):
            print("[edgar] skipped — set edgar.user_agent to 'Your Name you@email.com' "
                  "in config.yaml (SEC requires a real contact)")
            return []
        if not self.keywords:
            return []

        events: list[Event] = []
        for kw in self.keywords:
            try:
                hits = self._search(kw)
            except Exception as exc:  # noqa: BLE001
                print(f"[edgar] FAILED keyword={kw!r}: {exc}")
                continue
            for hit in hits:
                ev = hit_to_event(hit)
                if ev:
                    events.append(ev)
        return events
