# OSINT Monitor

A personal, zero-cost open-source-intelligence aggregator. It watches news
(RSS + GDELT), SEC filings, sanctions list updates, prediction markets, and
optionally AIS vessel/ADS-B aircraft positions — and surfaces cross-source
**signals** (a prediction market swinging, a topic's news volume spiking,
both happening at once, or a new sanctions listing) so you know where to
point a human read (X, primary sources, etc.) instead of doom-scrolling
everything yourself.

**Cost: $0/month.** Every source used is free with no paid tier required.
See [Cost control](#cost-control) below for the one thing to watch.

## Architecture

```
scripts/run_once.py  ──┐
                        ├─> osint/collect.py ─> sources (RSS/GDELT/Markets/EDGAR)
GitHub Actions          │                        │
(every 30 min)          │                        v
                        │                   osint/db.py (SQLite, committed to git)
                        │                        ^
scripts/collect_ais.py ─┤                        │
scripts/collect_adsb.py ┤                        │
scripts/collect_       ─┘                        │
  sanctions.py                                    │
                                                    │
scripts/make_brief.py  ─> osint/brief.py ──────────┘
(once/day)                 (optional 1 LLM call)

dashboard/app.py  <── reads data/osint.db ── Streamlit Community Cloud (free)
```

SQLite (`data/osint.db`) is committed to the repo by GitHub Actions after
each collection run, and read by the Streamlit app on every page load. No
external database, no paid hosting.

## Sources

| Source | What | Key required | Notes |
|---|---|---|---|
| RSS | News feeds you configure | No | `config.yaml` → `rss.feeds` |
| GDELT | Global news monitoring | No | `config.yaml` → `gdelt.queries` |
| Polymarket (Gamma API) | Prediction market probabilities | No | `config.yaml` → `markets.keywords` |
| SEC EDGAR full-text search | Filing phrase matches | No (but needs a real contact string) | `config.yaml` → `edgar.keywords`, `edgar.user_agent` |
| Commerce Dept Consolidated Screening List | New OFAC/BIS/State sanctions listings (diff-only) | No | `config.yaml` → `sanctions.enabled` |
| aisstream.io | Vessel (AIS) positions | Yes, free tier | `config.yaml` → `ais.zones`, secret `AISSTREAM_API_KEY` |
| OpenSky Network | Aircraft (ADS-B) positions | No (optional free account raises rate limit) | `config.yaml` → `adsb.zones` |

## Signals

`osint/signals.py` runs three cheap, no-LLM detectors after every collection
pass:

- **market_swing** — a tracked market's probability moved ≥ threshold over a window
- **news_spike** — a topic's event volume jumped sharply vs. the prior window
- **correlated** 🔗 — a market_swing and a news_spike land on the same topic in
  the same run (a much stronger tell than either alone)

Plus a batched **sanctions_listing** 🚫 alert whenever the sanctions collector
finds new entities. All thresholds are tunable in `config.yaml` under `signals:`.

## Dashboard

Run locally:

```bash
pip install -r requirements.txt
streamlit run dashboard/app.py
```

Tabs: **Maritime** (news + AIS/ADS-B map) / **Conflict** / **Geopolitics** /
**Policy** / **Other** (untagged) / **Markets** (with probability trend
charts) / **Filings** (EDGAR) / **Starred** (bookmarks) / **Daily Brief**.

Within any news tab, near-duplicate stories from different sources are
clustered into a single card (`osint/normalize.py::cluster_titles`), and
every item has a ⭐ toggle to bookmark it — bookmarks show up in the Starred
tab regardless of which tab you starred them from.

## Deploying for free (Streamlit Community Cloud)

1. Push this folder to a new GitHub repo.
2. On [share.streamlit.io](https://share.streamlit.io), create a new app
   pointed at that repo.
3. Set **main file path** to `dashboard/app.py` (not `app.py` — it lives in
   the `dashboard/` subfolder).
4. Add any secrets you're using (`AISSTREAM_API_KEY`, `OPENSKY_CLIENT_ID`,
   `OPENSKY_CLIENT_SECRET`, `ANTHROPIC_API_KEY`) under the app's Settings → Secrets.
5. In the GitHub repo, add the same secrets under Settings → Secrets and
   variables → Actions, so the scheduled workflows can use them too.

## Daily brief

`osint/brief.py` runs once a day (`.github/workflows/brief.yml`). Without an
`ANTHROPIC_API_KEY` secret it produces a free grouped digest (regions +
signals, no LLM). With the key set, it makes **exactly one** Claude Haiku
call per day for a synthesized brief — a fraction of a cent, well inside a
sub-$10/month budget even if you ran it hourly, which you don't need to.

## Cost control

Everything here is free-tier by design, with one thing to actually watch:
**GitHub Actions minutes.** Public repos get unlimited Actions minutes;
private repos get 2,000 free minutes/month. This project ships 5 scheduled
workflows (`collect` every 30 min, `ais`/`adsb` hourly, `sanctions` every
6h, `brief` daily) — comfortably free on a public repo, but if you make the
repo private, keep an eye on Settings → Billing → Actions usage, and reduce
the `collect.yml` cron frequency if you're getting close.

Everything else (GDELT, RSS, Polymarket, SEC EDGAR, the sanctions CSV,
OpenSky anonymous access, Streamlit Community Cloud hosting) has no paid
tier to accidentally cross into.

## Configuration

All source toggles, keywords, zones, and signal thresholds live in
`config.yaml`. Copy `.env.example` to `.env` for local runs if you're using
any of the optional keyed sources.

## Project layout

```
osint/                  # core package
  models.py             # the Event schema every source normalizes into
  db.py                 # SQLite schema + all queries
  normalize.py          # coarse free tagging + story clustering
  signals.py            # cross-source signal detectors
  sanctions.py          # OFAC/BIS/State diff collector
  collect.py            # orchestrator for the frequent sources
  brief.py              # daily brief (free digest or 1 LLM call)
  sources/
    rss.py  gdelt.py  markets.py  edgar.py

dashboard/app.py        # Streamlit UI

scripts/                # entry points called by GitHub Actions
  run_once.py  make_brief.py
  collect_ais.py  collect_adsb.py  collect_sanctions.py

.github/workflows/      # collect.yml  ais.yml  adsb.yml  sanctions.yml  brief.yml
```
