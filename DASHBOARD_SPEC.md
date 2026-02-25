# Portfolio Operations Dashboard — Build Spec v2
*Hand this file to Claude Code. Work top to bottom. Do not skip sections.*
*All changes from v1 are marked with ⚠️ so you can see what was hardened and why.*

---

## Project Summary

Build a real-time portfolio operations dashboard that demonstrates:
- Live market data ingestion with persistent time-series storage
- SQL-powered KPI computation (NAV, P&L attribution, reconciliation)
- System observability: ingestion latency, pipeline health, cycle status
- FastAPI REST backend
- React + Recharts frontend (single page, no router)
- Operational exception detection: both financial anomalies and platform anomalies

Framing: this is not a trading app. It is an **operations monitoring tool** — the kind of
internal dashboard a middle-office team at a hedge fund or asset manager would use to verify
data integrity, track portfolio health, and catch breaks before they become problems.
This is exactly what Arcesium's platform does at enterprise scale. The dashboard monitors
two classes of anomalies simultaneously: financial anomalies (abnormal asset price moves)
and platform anomalies (ingestion latency spikes, pipeline failures, stale data).

---

## Stack

| Layer | Choice | Reason |
|---|---|---|
| Data | `yfinance` | Free, no API key, reliable, real equities data |
| Database | SQLite (via `sqlite3` stdlib) | Zero setup, schema still demonstrates SQL fluency |
| Backend | FastAPI + uvicorn | Thin, clean, industry standard |
| Frontend | React 18 via CDN + Recharts via CDN | No build toolchain, ships as single HTML file |
| Styling | Tailwind via CDN | Looks professional without CSS work |
| Scheduling | APScheduler `BackgroundScheduler` | ⚠️ Must use BackgroundScheduler specifically — see note in main.py section |

---

## Repo Structure

```
portfolio-ops-dashboard/
├── backend/
│   ├── main.py              # FastAPI app + APScheduler
│   ├── db.py                # Schema creation + all queries
│   ├── ingest.py            # yfinance fetcher + DB writer
│   ├── kpis.py              # NAV, P&L, attribution, reconciliation, anomaly logic
│   └── config.py            # Portfolio definition + constants
├── tests/
│   ├── conftest.py          # Shared fixtures: tmp DB, mock prices, test portfolio
│   ├── test_db.py           # Schema, FK enforcement, all query functions
│   ├── test_ingest.py       # NaN sanitization, per-ticker failure isolation
│   ├── test_kpis.py         # NAV math, recon logic, anomaly detection edge cases
│   ├── test_api.py          # All 9 endpoints: status codes, schema, empty-DB 503
│   └── test_cycle.py        # Full refresh cycle: atomicity, rollback, system_metrics
├── frontend/
│   └── index.html           # Single-file React app (CDN imports)
├── pytest.ini
├── requirements.txt
└── README.md
```

---

## Portfolio Definition (`config.py`)

Define a static, fixed-weight paper portfolio. Do not make this configurable at runtime —
hardcode it. The point is to have a realistic multi-asset book, not a portfolio builder.

```python
# config.py

PORTFOLIO = [
    # US Equities
    {"ticker": "AAPL",  "shares": 50,  "cost_basis": 165.00, "asset_class": "equity"},
    {"ticker": "MSFT",  "shares": 30,  "cost_basis": 375.00, "asset_class": "equity"},
    {"ticker": "JPM",   "shares": 40,  "cost_basis": 185.00, "asset_class": "equity"},
    {"ticker": "GS",    "shares": 15,  "cost_basis": 420.00, "asset_class": "equity"},
    # Fixed Income (ETF proxies)
    {"ticker": "AGG",   "shares": 100, "cost_basis": 95.00,  "asset_class": "fixed_income"},
    {"ticker": "TLT",   "shares": 60,  "cost_basis": 88.00,  "asset_class": "fixed_income"},
    # Commodities (ETF proxies)
    {"ticker": "GLD",   "shares": 25,  "cost_basis": 175.00, "asset_class": "commodity"},
    {"ticker": "USO",   "shares": 80,  "cost_basis": 72.00,  "asset_class": "commodity"},
    # International
    {"ticker": "EEM",   "shares": 120, "cost_basis": 38.00,  "asset_class": "international"},
    {"ticker": "EFA",   "shares": 90,  "cost_basis": 72.00,  "asset_class": "international"},
    # Cash equivalent
    {"ticker": "SHV",   "shares": 200, "cost_basis": 110.00, "asset_class": "cash_equiv"},
]

REFRESH_INTERVAL_SECONDS = 60
DB_PATH = "portfolio_ops.db"
NAV_RECON_TOLERANCE = 0.01       # Flag reconciliation breaks > 1%
ANOMALY_ZSCORE_THRESHOLD = 2.0
ANOMALY_LOOKBACK_PERIODS = 20
```

---

## Database Schema (`db.py`)

### ⚠️ Connection helper — enforce foreign keys on every connection

```python
# db.py — use this helper everywhere. Never call sqlite3.connect() directly.

import sqlite3
from config import DB_PATH

def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")  # ⚠️ SQLite ignores FKs by default
    conn.execute("PRAGMA journal_mode = WAL;") # ⚠️ Allows concurrent reads during writes
    return conn
```

### Create all tables on startup

```sql
-- Stores every price fetch. Append-only, never update.
CREATE TABLE IF NOT EXISTS price_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    fetched_at      TEXT NOT NULL,          -- ISO8601 UTC (server time of fetch)
    market_time     TEXT,                   -- ⚠️ Timestamp from yfinance data itself
    price           REAL NOT NULL,
    volume          INTEGER,
    day_open        REAL,
    day_high        REAL,
    day_low         REAL,
    prev_close      REAL,
    data_source     TEXT DEFAULT 'yfinance'
);

-- One row per refresh cycle. Computed from price_snapshots.
CREATE TABLE IF NOT EXISTS nav_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    computed_at     TEXT NOT NULL,
    total_nav       REAL NOT NULL,
    total_cost      REAL NOT NULL,
    total_pnl       REAL NOT NULL,
    total_pnl_pct   REAL NOT NULL
);

-- Per-position detail for each NAV snapshot.
CREATE TABLE IF NOT EXISTS position_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    nav_snapshot_id INTEGER NOT NULL REFERENCES nav_snapshots(id),
    ticker          TEXT NOT NULL,
    asset_class     TEXT NOT NULL,
    shares          REAL NOT NULL,
    price           REAL NOT NULL,
    cost_basis      REAL NOT NULL,
    market_value    REAL NOT NULL,
    unrealized_pnl  REAL NOT NULL,
    pnl_pct         REAL NOT NULL,
    weight          REAL NOT NULL
);

-- Reconciliation log.
CREATE TABLE IF NOT EXISTS recon_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    checked_at      TEXT NOT NULL,
    check_type      TEXT NOT NULL,          -- 'nav_sum', 'position_count', 'price_staleness'
    expected_value  REAL,
    actual_value    REAL,
    delta_pct       REAL,
    status          TEXT NOT NULL,          -- 'PASS' or 'BREAK'
    detail          TEXT
);

-- Anomaly log.
CREATE TABLE IF NOT EXISTS anomaly_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at     TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    asset_class     TEXT NOT NULL,
    current_price   REAL NOT NULL,
    prev_close      REAL NOT NULL,
    move_pct        REAL NOT NULL,
    zscore          REAL NOT NULL,
    severity        TEXT NOT NULL           -- 'WARNING' or 'CRITICAL' (>3 sigma)
);

-- ⚠️ System metrics: one row per refresh cycle.
CREATE TABLE IF NOT EXISTS system_metrics (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_at                TEXT NOT NULL,
    status                  TEXT NOT NULL,  -- 'SUCCESS', 'PARTIAL', 'FAILED'
    error_detail            TEXT,           -- NULL on success
    ingestion_latency_ms    REAL,
    db_write_latency_ms     REAL,
    total_rows_processed    INTEGER,
    tickers_succeeded       INTEGER,
    tickers_failed          INTEGER
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_price_ticker_time
    ON price_snapshots(ticker, fetched_at DESC);

CREATE INDEX IF NOT EXISTS idx_nav_time
    ON nav_snapshots(computed_at DESC);

CREATE INDEX IF NOT EXISTS idx_position_nav_id
    ON position_snapshots(nav_snapshot_id);

CREATE INDEX IF NOT EXISTS idx_system_metrics_time
    ON system_metrics(cycle_at DESC);
```

### Query functions to implement in `db.py`

All query functions must use `get_connection()`. Never open a raw `sqlite3.connect()`.

**`get_latest_prices() -> dict[str, dict]`**
```sql
SELECT ticker, price, market_time, fetched_at
FROM price_snapshots
WHERE (ticker, fetched_at) IN (
    SELECT ticker, MAX(fetched_at)
    FROM price_snapshots
    GROUP BY ticker
)
```
Return dict keyed by ticker: `{ticker: {price, market_time, fetched_at}}`.

**`get_nav_history(n=50) -> list[dict]`**
```sql
SELECT computed_at, total_nav, total_pnl, total_pnl_pct
FROM nav_snapshots
ORDER BY computed_at DESC
LIMIT ?
```
Return in ascending time order (reverse the list after fetching).

**`get_asset_class_attribution() -> list[dict]`**
```sql
SELECT
    ps.asset_class,
    SUM(ps.market_value)   AS total_market_value,
    SUM(ps.unrealized_pnl) AS total_pnl,
    SUM(ps.weight)         AS total_weight,
    AVG(ps.pnl_pct)        AS avg_pnl_pct
FROM position_snapshots ps
WHERE ps.nav_snapshot_id = (SELECT MAX(id) FROM nav_snapshots)
GROUP BY ps.asset_class
ORDER BY total_market_value DESC
```

**`get_position_detail() -> list[dict]`**
```sql
SELECT ticker, asset_class, shares, price, cost_basis,
       market_value, unrealized_pnl, pnl_pct, weight
FROM position_snapshots
WHERE nav_snapshot_id = (SELECT MAX(id) FROM nav_snapshots)
ORDER BY market_value DESC
```

**`get_price_history(ticker, n=50) -> list[dict]`**
```sql
SELECT fetched_at, price
FROM price_snapshots
WHERE ticker = ?
ORDER BY fetched_at DESC
LIMIT ?
```

**`get_recent_anomalies(n=20) -> list[dict]`**
```sql
SELECT detected_at, ticker, asset_class, move_pct, zscore, severity
FROM anomaly_log
ORDER BY detected_at DESC
LIMIT ?
```

**`get_recon_status() -> list[dict]`**
```sql
SELECT check_type, checked_at, status, delta_pct, detail
FROM recon_log
WHERE (check_type, checked_at) IN (
    SELECT check_type, MAX(checked_at)
    FROM recon_log
    GROUP BY check_type
)
```

**`get_system_health(n=30) -> list[dict]`** ⚠️ new
```sql
SELECT cycle_at, status, error_detail,
       ingestion_latency_ms, db_write_latency_ms,
       total_rows_processed, tickers_succeeded, tickers_failed
FROM system_metrics
ORDER BY cycle_at DESC
LIMIT ?
```
Return in ascending time order (reverse after fetching).

---

## Ingestion Logic (`ingest.py`)

### ⚠️ Fetch prices individually, not in batch

Do NOT use `yfinance.download([list of tickers])`. Version 0.2.40 returns a pandas
MultiIndex DataFrame for multi-ticker downloads that is fragile and frequently breaks
automated parsing. Instead, loop through tickers and fetch each one individually:

```python
import yfinance as yf
import math

def fetch_single_ticker(ticker: str) -> dict | None:
    """
    Fetch current price data for one ticker via yf.Ticker().
    Returns a dict or None on failure. Never raises.

    Field access order:
    1. yf.Ticker(t).fast_info  — for current price, prev_close, volume
    2. yf.Ticker(t).history(period='2d', interval='1m').iloc[-1]  — fallback

    ⚠️ NaN sanitization: after fetching, replace all float NaN values with None.
    Check every numeric field explicitly with: math.isnan(v) if isinstance(v, float)
    SQLite stores None as NULL. SUM() over NULLs returns NULL safely.
    SUM() over NaN returns NaN, which crashes the frontend silently.

    ⚠️ market_time: read from fast_info.get('regularMarketTime') if available.
    For history() fallback, use the DataFrame index timestamp of the last row.
    Convert to ISO8601 UTC string. If unavailable, store None.
    """

def fetch_prices(tickers: list[str]) -> tuple[dict, list[str]]:
    """
    Call fetch_single_ticker() for each ticker sequentially.
    On per-ticker failure: log a warning, continue.
    Returns: (results_dict keyed by ticker, list of failed tickers)
    """
```

### ⚠️ run_refresh_cycle() — atomic transaction + system metrics

```python
import time
from datetime import datetime, timezone

def run_refresh_cycle():
    """
    ⚠️ CRITICAL: all DB writes wrapped in a single atomic transaction.
    If any step fails, ROLLBACK so the DB never contains partial records.

    ⚠️ system_metrics is written on a SEPARATE connection AFTER the main
    transaction, so it is always recorded even on ROLLBACK.

    Cycle status rules:
      SUCCESS  — all tickers fetched, all writes committed
      PARTIAL  — tickers_failed > 0 but tickers_succeeded > 0, writes committed
      FAILED   — exception raised, transaction rolled back

    Order of operations:
    1.  cycle_start = datetime.now(timezone.utc).isoformat()
    2.  t0 = time.time()
    3.  prices, failed = fetch_prices(all tickers)   ← pure fetch, outside transaction
    4.  ingestion_latency_ms = (time.time() - t0) * 1000
    5.  conn = get_connection()
    6.  conn.execute("BEGIN")
    7.  t1 = time.time()
    8.  INSERT price_snapshots rows (one per succeeded ticker)
    9.  nav_result = kpis.compute_nav(prices)
    10. INSERT nav_snapshots row → capture nav_snapshot_id via cursor.lastrowid
    11. INSERT position_snapshots rows using nav_snapshot_id
    12. recon_results = kpis.run_reconciliation_checks(nav_result, conn, nav_snapshot_id)
    13. INSERT recon_log rows
    14. anomalies = kpis.detect_anomalies(prices, conn)
    15. INSERT anomaly_log rows (only if non-empty)
    16. conn.execute("COMMIT")
    17. db_write_latency_ms = (time.time() - t1) * 1000
    18. status = 'PARTIAL' if failed else 'SUCCESS'

    On exception:
    19. conn.execute("ROLLBACK")
    20. status = 'FAILED', error_detail = str(e)

    Always (separate connection):
    21. INSERT system_metrics row with all timing fields and status
    """
```

---

## KPI Logic (`kpis.py`)

### NAV Computation

```python
def compute_nav(prices: dict, portfolio: list) -> dict:
    """
    For each position: if ticker not in prices, skip with warning.
    market_value = shares * price
    unrealized_pnl = market_value - (shares * cost_basis)
    pnl_pct = unrealized_pnl / (shares * cost_basis)
    total_nav = sum(market_value)
    total_cost = sum(shares * cost_basis)
    total_pnl = total_nav - total_cost
    total_pnl_pct = total_pnl / total_cost
    weight = market_value / total_nav   ← compute after total_nav is known

    Returns: {total_nav, total_cost, total_pnl, total_pnl_pct,
              positions: [{ticker, asset_class, shares, price, cost_basis,
                           market_value, unrealized_pnl, pnl_pct, weight}]}
    """
```

### Reconciliation Checks

```python
def run_reconciliation_checks(
    nav_result: dict,
    conn: sqlite3.Connection,
    nav_snapshot_id: int
) -> list[dict]:
    """
    Uses the passed-in connection (inside active transaction).
    nav_snapshot_id is the id of the nav_snapshots row just inserted.

    Check 1 — 'nav_sum' (independent recompute):
        SELECT SUM(market_value) FROM position_snapshots
        WHERE nav_snapshot_id = ?
        Compare to nav_result['total_nav'].
        delta_pct = abs(db_sum - expected) / expected
        BREAK if delta_pct > NAV_RECON_TOLERANCE
        Detail: "DB sum ${actual:.2f} vs in-memory ${expected:.2f}"

        ⚠️ This is only independent if position_snapshots were written before
        this check is called. The write order in run_refresh_cycle() guarantees
        this — do not change that order.

    Check 2 — 'position_count':
        SELECT COUNT(DISTINCT ticker) FROM position_snapshots
        WHERE nav_snapshot_id = ?
        BREAK if count != len(nav_result['positions'])
        Detail: "Expected {expected} positions, found {actual} in DB"

    Check 3 — 'price_staleness':
        ⚠️ Query market_time (data timestamp), NOT fetched_at (server clock).
        SELECT ticker, market_time, fetched_at FROM price_snapshots
        WHERE (ticker, fetched_at) IN (SELECT ticker, MAX(fetched_at) ...)
        For each ticker: use market_time if not NULL, else fall back to fetched_at.
        BREAK if any ticker's effective timestamp is older than
        3 * REFRESH_INTERVAL_SECONDS from now.
        Detail: "Stale tickers: {ticker}: {age_seconds}s old"
    """
```

### Anomaly Detection

```python
def detect_anomalies(prices: dict, conn: sqlite3.Connection) -> list[dict]:
    """
    For each ticker in prices:
        1. Pull last ANOMALY_LOOKBACK_PERIODS prices from price_snapshots via conn
        2. Skip ticker silently if fewer than ANOMALY_LOOKBACK_PERIODS + 1 rows
        3. Compute period returns: [p[t]/p[t-1] - 1 for t in range(1, len)]
        4. mean_return = statistics.mean(returns)
        5. std_return = statistics.stdev(returns)

        ⚠️ ZeroDivisionError guard — apply before computing z-score:
            std_return = max(std_return, 1e-6)
            Handles frozen quotes and illiquid instruments where all prices
            in the lookback window are identical.

        6. If prev_close is None for this ticker, skip it.
           current_return = (current_price / prev_close) - 1
        7. zscore = (current_return - mean_return) / std_return
        8. If abs(zscore) > ANOMALY_ZSCORE_THRESHOLD:
               severity = 'CRITICAL' if abs(zscore) > 3.0 else 'WARNING'
               append to results

    Return list of anomaly_log-schema dicts. Empty list = no anomalies.
    Use statistics stdlib — do not import numpy or pandas here.
    """
```

---

## API Endpoints (`main.py`)

### ⚠️ APScheduler — BackgroundScheduler required

```python
# ⚠️ CRITICAL: BackgroundScheduler only. Never BlockingScheduler.
# BlockingScheduler.start() blocks the calling thread and freezes uvicorn.
# BackgroundScheduler runs in a daemon thread alongside the server process.

from apscheduler.schedulers.background import BackgroundScheduler
```

### Startup sequence

```python
@app.on_event("startup")
async def startup():
    db.create_tables()
    ingest.run_refresh_cycle()          # run once immediately on startup
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        ingest.run_refresh_cycle,
        'interval',
        seconds=REFRESH_INTERVAL_SECONDS,
        max_instances=1                 # ⚠️ prevent overlapping cycles
    )
    scheduler.start()
```

### Endpoints

```
GET /api/health
    {"status": "ok", "last_refresh": "<ISO8601>", "next_refresh": "<ISO8601>"}
    Pull last_refresh from MAX(cycle_at) in system_metrics.

GET /api/nav/current
    Latest nav_snapshots row + all position_snapshots for that snapshot.

GET /api/nav/history?n=50
    Last n nav_snapshots rows, ascending by time.

GET /api/attribution
    Asset class P&L attribution from latest snapshot.

GET /api/positions
    Full position detail from latest snapshot.

GET /api/reconciliation
    Latest recon status per check_type.

GET /api/anomalies?n=20
    Recent anomaly log entries.

GET /api/prices/{ticker}/history?n=50
    Price time series for a single ticker.

GET /api/system/metrics?n=30          ⚠️ new
    system_metrics history ascending by time.
    Used by SystemHealthBar frontend component.
```

All endpoints: CORS open for all origins. Pydantic response models for each.

**⚠️ Empty-DB 503 handling — implement exactly this pattern, no COUNT(*) queries:**

Each `db.py` query function must return `None` when its primary table has no rows
(e.g. `get_nav_history()` returns `None` if `nav_snapshots` is empty). Endpoints
check for a `None` return and raise `HTTPException(status_code=503)`:

```python
from fastapi import HTTPException

@app.get("/api/nav/current")
def get_nav_current():
    result = db.get_nav_current()
    if result is None:
        raise HTTPException(status_code=503, detail="No data yet — first cycle in progress")
    return result
```

Apply this pattern to these endpoints (return 503 on None):
  `/api/nav/current`, `/api/nav/history`, `/api/attribution`, `/api/positions`

Do NOT return 503 for these endpoints — empty data is a valid state for them:
  `/api/health` → always 200
  `/api/reconciliation` → return empty list `[]` if no rows
  `/api/anomalies` → return empty list `[]` if no rows
  `/api/system/metrics` → return empty list `[]` if no rows
  `/api/prices/{ticker}/history` → return empty list `[]` if no rows

The distinction: a missing NAV means the system hasn't completed its first cycle
and is genuinely not ready to serve. An empty anomaly feed or recon log is normal
operational state and should not block the frontend from rendering.

---

## Frontend (`frontend/index.html`)

Single HTML file. All imports via CDN. No build step.

```html
<script src="https://unpkg.com/react@18/umd/react.development.js"></script>
<script src="https://unpkg.com/react-dom@18/umd/react-dom.development.js"></script>
<script src="https://unpkg.com/recharts@2.8.0/umd/Recharts.js"></script>
<script src="https://unpkg.com/@babel/standalone/babel.min.js"></script>
<link href="https://cdn.jsdelivr.net/npm/tailwindcss@2.2.19/dist/tailwind.min.css" rel="stylesheet">
```

### Layout

```
┌──────────────────────────────────────────────────────────────────┐
│  PORTFOLIO OPS DASHBOARD      Last refresh: 14:23:01  [● live]  │
│  Ingest: 847ms  │  DB Write: 23ms  │  Tickers: 11/11  │ SUCCESS │  ← ⚠️ SystemHealthBar
├──────────────┬──────────────┬──────────────┬────────────────────┤
│  Total NAV   │  Total P&L   │   P&L %      │  Recon: 3/3 PASS   │
├──────────────────────────────┬───────────────────────────────────┤
│  NAV Over Time (line chart)  │  P&L by Asset Class (bar chart)   │
├──────────────────────────────┼───────────────────────────────────┤
│  Position Table              │  Anomaly Feed                     │
│  (color-coded P&L)           │  (WARNING/CRITICAL badges)        │
└──────────────────────────────┴───────────────────────────────────┘
```

### Components — build in this order

**`SystemHealthBar`** ⚠️ new
Renders below the title row in the header. Shows from latest system_metrics:
ingestion_latency_ms, db_write_latency_ms, tickers_succeeded/total, status badge.
Status badge: green SUCCESS / yellow PARTIAL / red FAILED.
On FAILED: show error_detail as a collapsed `<details>` element below the bar.
Pull from `/api/system/metrics?n=1`.

**`KPICard`**: `{label, value, subvalue, color}`. Large value on dark card.
P&L card: green if positive, red if negative.

**`NavChart`**: Recharts LineChart. x=computed_at as HH:MM, y=total_nav as $xxx,xxx.
Single line, dot on most recent point.

**`AttributionChart`**: Recharts BarChart horizontal. y=asset_class, x=total_pnl.
Bars green/red by sign.

**`PositionTable`**: HTML table. Columns: Ticker, Class, Price, Shares, Market Value,
P&L, P&L%, Weight. Color P&L cells. Default sort: market_value DESC.

**`ReconPanel`**: Three rows, one per check_type. PASS/BREAK badge, delta_pct, detail.

**`AnomalyFeed`**: Scrollable. Each item: ticker, move_pct, z-score, severity badge, timestamp.

**`App`**: Root component.

### ⚠️ Data fetching — useEffect with cleanup required

```javascript
// ⚠️ Do NOT use a bare setInterval outside useEffect.
// It creates a new interval on every render, never cleans up, and captures
// stale state via closure. Always use the pattern below.

const { useState, useEffect, useCallback } = React;
const API = "http://localhost:8000/api";

function App() {
  const [data, setData] = useState(null);
  const [refreshing, setRefreshing] = useState(false);

  const fetchAllData = useCallback(async () => {
    setRefreshing(true);
    try {
      const [nav, history, attribution, positions, recon, anomalies, sysMetrics] =
        await Promise.all([
          fetch(`${API}/nav/current`).then(r => r.json()),
          fetch(`${API}/nav/history?n=50`).then(r => r.json()),
          fetch(`${API}/attribution`).then(r => r.json()),
          fetch(`${API}/positions`).then(r => r.json()),
          fetch(`${API}/reconciliation`).then(r => r.json()),
          fetch(`${API}/anomalies?n=20`).then(r => r.json()),
          fetch(`${API}/system/metrics?n=30`).then(r => r.json()),
        ]);
      // ⚠️ Functional update form prevents stale closure on setData
      setData(_ => ({ nav, history, attribution, positions, recon, anomalies, sysMetrics }));
    } catch (err) {
      console.error("Fetch failed:", err);
    } finally {
      setRefreshing(false);
    }
  }, []); // empty deps: fetchAllData reference is stable

  useEffect(() => {
    fetchAllData();                          // fetch immediately on mount
    const id = setInterval(fetchAllData, 60_000);
    return () => clearInterval(id);         // ⚠️ cleanup on unmount
  }, [fetchAllData]);

  if (!data) return <div className="text-white p-8">Initializing...</div>;
  // render components...
}
```

### Colors
- Background: `#0f172a`
- Card: `#1e293b`
- Positive: `#22c55e`
- Negative: `#ef4444`
- WARNING: `#eab308`
- CRITICAL/FAILED: `#ef4444`
- PARTIAL: `#eab308`
- Text: `#f8fafc`

---

## `requirements.txt`

```
fastapi==0.111.0
uvicorn==0.29.0
yfinance==0.2.40
apscheduler==3.10.4
pydantic==2.7.1
pytest==8.2.0
pytest-asyncio==0.23.6
httpx==0.27.0
```

---

## README Structure (write this last, ~30 min)

````markdown
# Portfolio Operations Dashboard

A real-time portfolio operations dashboard demonstrating live market data ingestion,
time-series persistence, NAV computation, reconciliation, and dual-class anomaly
detection (financial and platform) across a multi-asset book.

## What it does

Ingests live equity and ETF prices across 11 instruments, computes portfolio NAV and
P&L attribution by asset class every 60 seconds, runs three independent reconciliation
checks to verify data integrity, and surfaces both financial anomalies (z-score-based
price moves) and platform anomalies (ingestion latency, pipeline failures, stale data)
in a single-page React dashboard.

## Architecture

```
yfinance API
    │  (per-ticker fetch, NaN sanitization, market_time capture)
    ▼
ingest.py ──► kpis.py (compute_nav, recon_checks, anomaly_detection)
    │
    ▼  (atomic transaction per cycle)
SQLite
 ├── price_snapshots      (append-only time series)
 ├── nav_snapshots        (portfolio-level KPIs)
 ├── position_snapshots   (per-position detail)
 ├── recon_log            (3 integrity checks per cycle)
 ├── anomaly_log          (financial exception events)
 └── system_metrics       (pipeline health per cycle)
    │
    ▼
FastAPI (9 REST endpoints)
    │
    ▼
React + Recharts (single HTML, 60s polling)
```

## Design Decisions

| Decision | Rationale |
|---|---|
| Append-only `price_snapshots` | Immutable audit trail; never overwrite historical data — matches how production market data systems work |
| Atomic transaction per cycle | Prevents orphaned records if a cycle fails mid-write; mirrors transactional guarantees expected in production data pipelines |
| `PRAGMA foreign_keys = ON` | SQLite ignores FK constraints by default; enabling it catches referential integrity violations that would otherwise silently corrupt query results |
| Reconciliation as independent DB recompute | The `nav_sum` check recomputes total NAV from `position_snapshots` after writing and compares to the in-memory result — two independent code paths must agree, which is how real middle-office recon engines work |
| `market_time` for staleness check | The data's own timestamp catches frozen market data that would falsely pass a server-clock staleness check |
| Z-score over fixed threshold for anomalies | Adapts to each asset's volatility regime; a 2% move in USO is normal, the same move in SHV indicates a problem |
| `std = max(std, 1e-6)` guard | Prevents ZeroDivisionError when all lookback prices are identical (after-hours frozen quotes, illiquid instruments) |
| Per-ticker fetch over batch download | `yfinance` 0.2.40 returns a complex MultiIndex DataFrame for multi-ticker batch calls that breaks automated parsing; per-ticker `fast_info` is explicit and reliable |
| `BackgroundScheduler` over `BlockingScheduler` | `BlockingScheduler.start()` blocks the calling thread and freezes the uvicorn event loop; `BackgroundScheduler` runs in a daemon thread |
| `system_metrics` written on separate connection | Ensures pipeline health is always recorded, even when the main transaction is rolled back — you cannot observe a failed cycle if the failure log is inside the failed transaction |
| Dual anomaly classes | Monitors financial anomalies (price z-scores) and platform anomalies (latency, failures) simultaneously; the pipeline itself is observable, not just the portfolio |

## KPIs Tracked

**Financial:** NAV over time, unrealized P&L by position and asset class,
portfolio weight by asset class, 3-check reconciliation status, intraday z-score anomalies

**Platform:** Ingestion latency (ms), DB write latency (ms), tickers succeeded/failed
per cycle, cycle status (SUCCESS / PARTIAL / FAILED)

## What I'd add with more time
- Migrate backend from Python/FastAPI to Kotlin or TypeScript for alignment with enterprise fintech stacks (Arcesium's core platform is Java/Kotlin)
- Migrate from SQLite to AWS RDS Postgres; schema requires no changes — swap the connection string
- Deploy FastAPI container via AWS ECS Fargate behind an Application Load Balancer; serve frontend from S3 + CloudFront
- Alerting: POST to Slack webhook on recon BREAK, CRITICAL anomaly, or consecutive FAILED cycles
- Realized P&L tracking via a `trades` table with FIFO cost basis lot matching
- Multi-portfolio support: add `portfolio_id` FK to all snapshot tables
````

---

## Testing Infrastructure

### `pytest.ini`

```ini
[pytest]
testpaths = tests
pythonpath = backend
asyncio_mode = auto
filterwarnings =
    ignore::DeprecationWarning:yfinance
    ignore::DeprecationWarning:httpx
    ignore::DeprecationWarning:apscheduler
    ignore::ResourceWarning
```

`pythonpath = backend` makes all backend modules importable without `sys.path` hacks.

`filterwarnings` suppresses noisy deprecation walls from yfinance, httpx, and
apscheduler — libraries that produce 30-50 lines of warnings per test run. These
warnings do not indicate test failures. Filtering them at the library level (not
globally with `ignore::DeprecationWarning`) preserves visibility into any
deprecation warnings in your own backend code.

### `tests/conftest.py` — shared fixtures

Claude Code must implement these fixtures exactly. Every test file imports from here.
Do not duplicate fixture logic across test files.

```python
import pytest
import sqlite3
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

import db as db_module

# ── Minimal test portfolio (3 positions, clean round numbers for easy mental math) ──
TEST_PORTFOLIO = [
    {"ticker": "AAPL", "shares": 10,  "cost_basis": 100.00, "asset_class": "equity"},
    {"ticker": "AGG",  "shares": 20,  "cost_basis": 50.00,  "asset_class": "fixed_income"},
    {"ticker": "GLD",  "shares": 5,   "cost_basis": 200.00, "asset_class": "commodity"},
]
# Total cost: (10*100) + (20*50) + (5*200) = 1000 + 1000 + 1000 = 3000.00

# ── Mock prices: all up 10% from cost basis for predictable assertions ──
MOCK_PRICES_UP10 = {
    "AAPL": {"price": 110.00, "prev_close": 109.00, "market_time": "2024-01-15T15:00:00+00:00",
             "volume": 1000000, "day_open": 108.0, "day_high": 111.0, "day_low": 107.0},
    "AGG":  {"price": 55.00,  "prev_close": 54.50,  "market_time": "2024-01-15T15:00:00+00:00",
             "volume": 500000,  "day_open": 54.0,  "day_high": 55.5,  "day_low": 53.5},
    "GLD":  {"price": 220.00, "prev_close": 218.00, "market_time": "2024-01-15T15:00:00+00:00",
             "volume": 200000,  "day_open": 217.0, "day_high": 221.0, "day_low": 216.0},
}
# Expected NAV:  (10*110) + (20*55) + (5*220) = 1100 + 1100 + 1100 = 3300.00
# Expected P&L:  3300 - 3000 = 300.00
# Expected P&L%: 300 / 3000 = 0.10 (10%)
# Expected weights: each class = 1100/3300 = 0.3333...

# ── Mock prices: AAPL missing (simulates a fetch failure) ──
MOCK_PRICES_MISSING_AAPL = {k: v for k, v in MOCK_PRICES_UP10.items() if k != "AAPL"}

@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """
    Creates an isolated SQLite DB in a temp directory.
    Monkeypatches db_module.DB_PATH so all db.py calls use this temp file.
    Runs create_tables(). Yields the db path. Cleaned up by tmp_path fixture.
    """
    db_path = str(tmp_path / "test_portfolio.db")
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    db_module.create_tables()
    yield db_path

@pytest.fixture
def db_conn(tmp_db):
    """
    Opens and yields a get_connection() connection to the tmp DB.
    Closes after test. Use for tests that need direct DB access.
    """
    conn = db_module.get_connection()
    yield conn
    conn.close()

@pytest.fixture
def populated_db(tmp_db):
    """
    Inserts one complete cycle of data using TEST_PORTFOLIO and MOCK_PRICES_UP10:
      - 3 price_snapshot rows (one per ticker)
      - 1 nav_snapshot row
      - 3 position_snapshot rows (one per ticker)
      - 3 recon_log rows (one per check_type, all PASS)
      - 1 system_metrics row (status=SUCCESS)

    Returns nav_snapshot_id (always 1 for the first insert).
    All writes use direct SQL via get_connection() — never calls run_refresh_cycle().
    This keeps populated_db independent of ingest.py and kpis.py correctness.
    """
    from datetime import datetime, timezone
    now = "2024-01-15T15:00:00+00:00"

    conn = db_module.get_connection()
    conn.execute("BEGIN")

    # price_snapshots — column order must exactly match schema definition
    for ticker, p in MOCK_PRICES_UP10.items():
        conn.execute(
            "INSERT INTO price_snapshots "
            "(ticker, fetched_at, market_time, price, volume, day_open, day_high, day_low, prev_close) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ticker, now, p["market_time"], p["price"], p["volume"],
             p["day_open"], p["day_high"], p["day_low"], p["prev_close"])
        )

    # nav_snapshot — expected values from MOCK_PRICES_UP10 + TEST_PORTFOLIO math:
    # total_nav=3300, total_cost=3000, total_pnl=300, total_pnl_pct=0.10
    conn.execute(
        "INSERT INTO nav_snapshots (computed_at, total_nav, total_cost, total_pnl, total_pnl_pct) "
        "VALUES (?, ?, ?, ?, ?)",
        (now, 3300.00, 3000.00, 300.00, 0.10)
    )
    nav_snapshot_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # position_snapshots — one row per TEST_PORTFOLIO position
    # market_value = shares * price, unrealized_pnl = market_value - (shares * cost_basis)
    # weight = market_value / total_nav = 1100 / 3300 = 0.3333...
    positions = [
        ("AAPL", "equity",        10, 110.00, 100.00, 1100.00, 100.00, 0.10, 1100/3300),
        ("AGG",  "fixed_income",  20,  55.00,  50.00, 1100.00, 100.00, 0.10, 1100/3300),
        ("GLD",  "commodity",      5, 220.00, 200.00, 1100.00, 100.00, 0.10, 1100/3300),
    ]
    for ticker, asset_class, shares, price, cost_basis, market_value, pnl, pnl_pct, weight in positions:
        conn.execute(
            "INSERT INTO position_snapshots "
            "(nav_snapshot_id, ticker, asset_class, shares, price, cost_basis, "
            "market_value, unrealized_pnl, pnl_pct, weight) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (nav_snapshot_id, ticker, asset_class, shares, price, cost_basis,
             market_value, pnl, pnl_pct, weight)
        )

    # recon_log — one PASS row per check_type
    for check_type in ("nav_sum", "position_count", "price_staleness"):
        conn.execute(
            "INSERT INTO recon_log (checked_at, check_type, expected_value, actual_value, "
            "delta_pct, status, detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now, check_type, 3300.00, 3300.00, 0.0, "PASS", "All checks passed")
        )

    # system_metrics — one SUCCESS row
    conn.execute(
        "INSERT INTO system_metrics "
        "(cycle_at, status, error_detail, ingestion_latency_ms, db_write_latency_ms, "
        "total_rows_processed, tickers_succeeded, tickers_failed) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (now, "SUCCESS", None, 245.3, 18.7, 3, 3, 0)
    )

    conn.execute("COMMIT")
    conn.close()
    return nav_snapshot_id
```

---

## Build Order for Claude Code (TDD)

**The rule: for every step, write all tests first, verify they fail for the right reason,
implement the code, then verify all tests pass before moving to the next step.**

"Fail for the right reason" means: the test fails with `ImportError`, `AttributeError`,
or an assertion about wrong output — not with a syntax error in the test itself.
If a test file has a syntax error, fix the test before proceeding.

Run the full test suite at the end of every step: `pytest tests/ -v -W ignore::DeprecationWarning`.
Do not proceed to the next step if any test that was passing before is now failing.

```
Step 0: Testing infrastructure only (no implementation files yet)
        → Write conftest.py, pytest.ini
        → Write all 5 test files with all test functions as stubs (pass or xfail)
        → Run: pytest tests/ -v
        → Verify: all tests collected, most fail with ImportError (expected)
        → Gate: pytest collects tests without syntax errors

Step 1: config.py + db.py
        → Tests to write first: tests/test_db.py (full file)
        → Run pytest tests/test_db.py — all should fail with ImportError
        → Implement config.py and db.py
        → Run pytest tests/test_db.py — all should pass
        → Gate: pytest tests/test_db.py passes 100%

Step 2: ingest.py fetch layer
        → Tests to write first: tests/test_ingest.py (full file)
        → Run pytest tests/test_ingest.py — fails with ImportError
        → Implement fetch_prices() and fetch_single_ticker()
        → Run pytest tests/test_ingest.py — all should pass
        → Gate: pytest tests/test_ingest.py passes 100%

Step 3: kpis.py + ingest.py write path
        → Tests to write first: tests/test_kpis.py + tests/test_cycle.py (full files)
        → Run — fails with ImportError
        → Implement kpis.py and complete ingest.py run_refresh_cycle()
        → Run pytest tests/test_kpis.py tests/test_cycle.py — all pass
        → Gate: both files pass 100%, AND pytest tests/ -v still passes everything

Step 4: main.py
        → Tests to write first: tests/test_api.py (full file)
        → Run — fails with ImportError
        → Implement main.py
        → Run pytest tests/test_api.py — all pass
        → Gate: pytest tests/ -v passes 100%

Step 5: frontend/index.html
        → No pytest tests (browser rendering not pytest-testable)
        → Manual verification only — see checklist below
        → Gate: pytest tests/ -v still passes 100% after any backend changes
           made to support the frontend

Step 6: README.md
        → Gate: pytest tests/ -v passes 100%
```

---

## Test Specifications

Claude Code must write these tests before implementing each module.
Each test description is precise enough to write the assertion without seeing the implementation.

---

### `tests/test_db.py` — written before Step 1

```python
# Write these tests. They must FAIL before db.py exists, PASS after.

class TestSchema:
    def test_all_six_tables_created(self, tmp_db):
        # Connect directly with sqlite3.connect (not get_connection) and query
        # sqlite_master for table names. Assert all 6 table names are present:
        # price_snapshots, nav_snapshots, position_snapshots,
        # recon_log, anomaly_log, system_metrics
        pass

    def test_four_indexes_created(self, tmp_db):
        # Query sqlite_master for type='index' where name NOT LIKE 'sqlite_%'
        # Assert exactly 4 indexes exist with the names specified in the schema
        pass

    def test_foreign_keys_enforced(self, db_conn):
        # Attempt to INSERT a position_snapshots row with nav_snapshot_id=9999
        # (which does not exist in nav_snapshots)
        # Assert sqlite3.IntegrityError is raised
        # This test ONLY passes if PRAGMA foreign_keys = ON is active
        pass

    def test_wal_mode_active(self, db_conn):
        # Execute "PRAGMA journal_mode;" and assert result == "wal"
        pass

    def test_create_tables_idempotent(self, tmp_db):
        # Call db.create_tables() a second time
        # Assert no exception is raised (IF NOT EXISTS must be respected)
        pass


class TestGetConnection:
    def test_returns_row_factory_connection(self, db_conn):
        # Insert a price_snapshot row, fetch it, access a column by name (not index)
        # Assert column-by-name access works (confirms row_factory = sqlite3.Row)
        pass

    def test_direct_sqlite_connect_would_miss_pragma(self, tmp_db):
        # Open a raw sqlite3.connect() connection (bypassing get_connection)
        # Execute "PRAGMA foreign_keys;"
        # Assert result == 0 (pragma is OFF on raw connections)
        # This documents WHY get_connection() is required — raw connect loses the pragma
        pass


class TestQueryFunctions:
    def test_get_latest_prices_returns_most_recent_only(self, populated_db):
        # populated_db has prices for AAPL, AGG, GLD
        # Insert a second, older price_snapshot for AAPL at an earlier fetched_at
        # Call db.get_latest_prices()
        # Assert result["AAPL"]["price"] == the NEWER price, not the older one
        pass

    def test_get_nav_history_returns_ascending(self, populated_db):
        # Insert 3 nav_snapshot rows at t1, t2, t3 (t1 < t2 < t3)
        # Call db.get_nav_history(n=3)
        # Assert result[0]["computed_at"] < result[1]["computed_at"] < result[2]["computed_at"]
        # (ascending — the query fetches DESC and the function reverses)
        pass

    def test_get_nav_history_respects_n_limit(self, populated_db):
        # Insert 10 nav_snapshot rows
        # Call db.get_nav_history(n=3)
        # Assert len(result) == 3
        pass

    def test_get_asset_class_attribution_groups_correctly(self, populated_db):
        # populated_db has 3 positions: equity, fixed_income, commodity
        # Call db.get_asset_class_attribution()
        # Assert len(result) == 3
        # Assert each row has keys: asset_class, total_market_value, total_pnl, total_weight, avg_pnl_pct
        # Assert SUM of total_weight across all rows ≈ 1.0 (within 0.001)
        pass

    def test_get_position_detail_ordered_by_market_value(self, populated_db):
        # Call db.get_position_detail()
        # Assert result[0]["market_value"] >= result[1]["market_value"] >= result[2]["market_value"]
        pass

    def test_get_recon_status_returns_latest_per_check_type(self, populated_db):
        # Insert 2 recon_log rows for check_type='nav_sum': one old PASS, one recent BREAK
        # Call db.get_recon_status()
        # Assert the nav_sum row in the result has status='BREAK' (most recent wins)
        pass

    def test_get_system_health_returns_ascending(self, populated_db):
        # Insert 5 system_metrics rows at different timestamps
        # Call db.get_system_health(n=5)
        # Assert timestamps are in ascending order
        pass

    def test_get_system_health_respects_n_limit(self, populated_db):
        # Insert 10 system_metrics rows
        # Call db.get_system_health(n=4)
        # Assert len(result) == 4
        pass
```

---

### `tests/test_ingest.py` — written before Step 2

```python
# All tests mock yfinance. Never call live yfinance in tests.
# Use pytest monkeypatch or unittest.mock.patch to replace yf.Ticker.

class TestNaNSanitization:
    def test_nan_price_returns_none(self, monkeypatch):
        # Mock yf.Ticker("AAPL").fast_info to return an object where
        # regularMarketPrice = float('nan')
        # Call fetch_single_ticker("AAPL")
        # Assert result is None (fetch fails on NaN price — price is NOT NULL in schema)
        pass

    def test_nan_volume_becomes_none(self, monkeypatch):
        # Mock yf.Ticker("AAPL").fast_info to return a valid price but
        # regularMarketVolume = float('nan')
        # Call fetch_single_ticker("AAPL")
        # Assert result is not None (volume is nullable)
        # Assert result["volume"] is None (NaN was sanitized to None)
        pass

    def test_nan_prev_close_becomes_none(self, monkeypatch):
        # Same pattern: prev_close = float('nan') → result["prev_close"] is None
        pass

    def test_no_nan_values_survive_in_output(self, monkeypatch):
        # Mock a fast_info that returns NaN for all optional fields
        # Call fetch_single_ticker
        # Assert no value in the result dict is float('nan')
        # Use: assert not any(isinstance(v, float) and math.isnan(v) for v in result.values())
        pass


class TestPerTickerFailureIsolation:
    def test_one_bad_ticker_does_not_stop_others(self, monkeypatch):
        # Mock yf.Ticker so "BADTICKER" raises an exception, others succeed
        # Call fetch_prices(["AAPL", "BADTICKER", "GLD"])
        # Assert results dict has "AAPL" and "GLD" keys
        # Assert "BADTICKER" is in the failed list
        # Assert no exception is raised
        pass

    def test_all_tickers_fail_returns_empty_dict(self, monkeypatch):
        # Mock yf.Ticker to always raise an exception
        # Call fetch_prices(["AAPL", "GLD"])
        # Assert results == {}
        # Assert failed == ["AAPL", "GLD"] (or equivalent)
        # Assert no exception is raised
        pass

    def test_failed_tickers_logged_as_warning(self, monkeypatch, caplog):
        # Mock one ticker to fail
        # Call fetch_prices with that ticker
        # Assert caplog contains a WARNING-level message mentioning the failed ticker
        pass


class TestMarketTime:
    def test_market_time_captured_when_available(self, monkeypatch):
        # Mock fast_info.regularMarketTime to return a datetime object
        # Call fetch_single_ticker
        # Assert result["market_time"] is a non-None ISO8601 string
        pass

    def test_market_time_is_none_when_unavailable(self, monkeypatch):
        # Mock fast_info with no regularMarketTime attribute (or raises AttributeError)
        # Call fetch_single_ticker
        # Assert result["market_time"] is None (not an exception)
        pass


class TestFetchNeverCallsBatchDownload:
    def test_download_is_never_called(self, monkeypatch):
        # Monkeypatch yfinance.download to raise AssertionError("batch download called")
        # Call fetch_prices(["AAPL", "GLD"])
        # Assert no AssertionError is raised
        # (confirms per-ticker path is used, not batch)
        pass
```

---

### `tests/test_kpis.py` — written before Step 3

```python
# All tests use TEST_PORTFOLIO and MOCK_PRICES_UP10 from conftest.
# No DB writes in this file — kpis.compute_nav() is pure, takes no DB conn.
# recon and anomaly tests do need a db_conn (they query the DB).

class TestComputeNav:
    def test_nav_total_correct(self):
        # Call compute_nav(MOCK_PRICES_UP10, TEST_PORTFOLIO)
        # Assert result["total_nav"] == pytest.approx(3300.00)
        pass

    def test_cost_total_correct(self):
        # Assert result["total_cost"] == pytest.approx(3000.00)
        pass

    def test_pnl_correct(self):
        # Assert result["total_pnl"] == pytest.approx(300.00)
        pass

    def test_pnl_pct_correct(self):
        # Assert result["total_pnl_pct"] == pytest.approx(0.10)
        pass

    def test_weights_sum_to_one(self):
        # Assert sum(p["weight"] for p in result["positions"]) == pytest.approx(1.0)
        pass

    def test_each_weight_correct(self):
        # Each position has market_value 1100, total_nav 3300
        # Each weight should be pytest.approx(1100/3300) ≈ 0.3333
        pass

    def test_missing_ticker_skipped(self):
        # Call compute_nav(MOCK_PRICES_MISSING_AAPL, TEST_PORTFOLIO)
        # Assert len(result["positions"]) == 2 (AAPL skipped)
        # Assert result["total_nav"] == pytest.approx(2200.00)  # only AGG + GLD
        pass

    def test_position_pnl_per_row(self):
        # For AAPL: unrealized_pnl = (10 * 110) - (10 * 100) = 100.00
        # For AGG:  unrealized_pnl = (20 * 55)  - (20 * 50)  = 100.00
        # For GLD:  unrealized_pnl = (5 * 220)  - (5 * 200)  = 100.00
        pass

    def test_all_required_keys_in_positions(self):
        # Each position dict must have all required keys:
        # ticker, asset_class, shares, price, cost_basis,
        # market_value, unrealized_pnl, pnl_pct, weight
        pass


class TestReconChecks:
    def test_nav_sum_passes_when_correct(self, tmp_db):
        # Insert a nav_snapshot and matching position_snapshots via direct SQL
        # so DB values exactly match the in-memory nav_result.
        # Call run_reconciliation_checks(nav_result, conn, nav_snapshot_id)
        # Assert the nav_sum check has status='PASS'
        pass

    def test_nav_sum_breaks_on_tampered_db(self, tmp_db):
        # Insert a nav_snapshot with total_nav=3300 and position_snapshots
        # summing to 3300. Then UPDATE one position's market_value to introduce
        # a 5% discrepancy (well above 1% tolerance).
        # Call run_reconciliation_checks with original nav_result (total_nav=3300)
        # Assert nav_sum check has status='BREAK'
        pass

    def test_position_count_passes_when_all_present(self, tmp_db):
        # Insert nav_snapshot + 3 position_snapshots (matching TEST_PORTFOLIO)
        # Assert position_count check status='PASS'
        pass

    def test_position_count_breaks_on_missing_position(self, tmp_db):
        # Insert nav_snapshot + only 2 position_snapshots (should be 3)
        # Pass nav_result with 3 positions
        # Assert position_count check status='BREAK'
        pass

    def test_staleness_passes_on_fresh_data(self, tmp_db):
        # Insert price_snapshots with market_time = now - 30 seconds
        # (well within 3 * REFRESH_INTERVAL_SECONDS = 180s threshold)
        # Assert price_staleness check status='PASS'
        pass

    def test_staleness_breaks_on_old_market_time(self, tmp_db):
        # Insert price_snapshots with market_time = now - 600 seconds (10 minutes ago)
        # Assert price_staleness check status='BREAK'
        # Assert detail mentions the stale ticker name
        pass

    def test_staleness_falls_back_to_fetched_at_when_market_time_null(self, tmp_db):
        # Insert price_snapshot with market_time=NULL and fetched_at = now - 30s
        # Assert price_staleness check status='PASS' (uses fetched_at as fallback)
        pass

    def test_returns_exactly_three_check_types(self, tmp_db):
        # Call run_reconciliation_checks
        # Assert len(result) == 3
        # Assert {r["check_type"] for r in result} == {"nav_sum", "position_count", "price_staleness"}
        pass


class TestAnomalyDetection:
    def test_no_anomaly_on_normal_move(self, tmp_db):
        # Insert 25 price_snapshots for AAPL with prices incrementing by 0.01 each
        # (tiny moves, z-score will be near 0)
        # Set current price to a value within 1 sigma of the mean return
        # Call detect_anomalies
        # Assert AAPL not in results
        pass

    def test_warning_on_two_sigma_move(self, tmp_db):
        # Insert 25 price_snapshots for AAPL with stable prices (std very low)
        # Set current price to produce a return that is exactly 2.5 sigma above mean
        # Call detect_anomalies
        # Assert result contains AAPL with severity='WARNING'
        pass

    def test_critical_on_three_sigma_move(self, tmp_db):
        # Same setup but return is 3.5 sigma above mean
        # Assert severity='CRITICAL'
        pass

    def test_no_crash_on_identical_prices(self, tmp_db):
        # Insert 25 price_snapshots for AAPL all with price=100.00 (std=0 scenario)
        # Call detect_anomalies
        # Assert no ZeroDivisionError is raised
        # Assert AAPL not in results (std clamped to 1e-6, move will be ~0 sigma)
        pass

    def test_skips_ticker_with_insufficient_history(self, tmp_db):
        # Insert only 5 price_snapshots for AAPL (fewer than ANOMALY_LOOKBACK_PERIODS+1=21)
        # Call detect_anomalies
        # Assert AAPL not in results (silently skipped, not an error)
        pass

    def test_skips_ticker_with_no_prev_close(self, tmp_db):
        # Insert sufficient history for AAPL
        # Pass prices dict where AAPL["prev_close"] = None
        # Assert AAPL not in results (skipped, not an error)
        pass
```

---

### `tests/test_api.py` — written before Step 4

```python
# Uses httpx.AsyncClient with FastAPI's test app.
# Monkeypatches APScheduler so it never actually starts a background thread.
# Monkeypatches run_refresh_cycle so tests don't call live yfinance.

import pytest
from httpx import AsyncClient, ASGITransport

@pytest.fixture
async def client(tmp_db, monkeypatch):
    """
    Yields an AsyncClient pointed at the FastAPI app.
    Monkeypatches:
    - ingest.run_refresh_cycle → no-op (don't call yfinance in tests)
    - BackgroundScheduler.start → no-op (don't start background threads)
    The tmp_db fixture ensures tests use an isolated DB.
    """
    import ingest
    import main
    from apscheduler.schedulers.background import BackgroundScheduler

    monkeypatch.setattr(ingest, "run_refresh_cycle", lambda: None)
    monkeypatch.setattr(BackgroundScheduler, "start", lambda self: None)

    async with AsyncClient(
        transport=ASGITransport(app=main.app), base_url="http://test"
    ) as ac:
        yield ac


@pytest.fixture
async def populated_client(populated_db, monkeypatch):
    """
    Same as client fixture but uses populated_db so endpoints return real data.
    """
    import ingest
    import main
    from apscheduler.schedulers.background import BackgroundScheduler

    monkeypatch.setattr(ingest, "run_refresh_cycle", lambda: None)
    monkeypatch.setattr(BackgroundScheduler, "start", lambda self: None)

    async with AsyncClient(
        transport=ASGITransport(app=main.app), base_url="http://test"
    ) as ac:
        yield ac


class TestEmptyDB:
    """All endpoints must return 503 when no data exists yet."""

    async def test_nav_current_503_when_empty(self, client):
        r = await client.get("/api/nav/current")
        assert r.status_code == 503

    async def test_nav_history_503_when_empty(self, client):
        r = await client.get("/api/nav/history")
        assert r.status_code == 503

    async def test_attribution_503_when_empty(self, client):
        r = await client.get("/api/attribution")
        assert r.status_code == 503

    async def test_positions_503_when_empty(self, client):
        r = await client.get("/api/positions")
        assert r.status_code == 503

    async def test_health_200_even_when_empty(self, client):
        # /api/health should always return 200 — it's the liveness check
        r = await client.get("/api/health")
        assert r.status_code == 200

    async def test_system_metrics_200_even_when_empty(self, client):
        # Returns empty list, not 503 — no data is a valid system state
        r = await client.get("/api/system/metrics")
        assert r.status_code == 200
        assert r.json() == []


class TestNavEndpoints:
    async def test_nav_current_schema(self, populated_client):
        r = await populated_client.get("/api/nav/current")
        assert r.status_code == 200
        data = r.json()
        assert "total_nav" in data
        assert "total_pnl" in data
        assert "positions" in data
        assert isinstance(data["positions"], list)
        assert len(data["positions"]) == 3

    async def test_nav_history_returns_list(self, populated_client):
        r = await populated_client.get("/api/nav/history?n=10")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    async def test_nav_history_n_param_respected(self, populated_client):
        # populated_db has 1 nav_snapshot; requesting n=10 should return 1
        r = await populated_client.get("/api/nav/history?n=10")
        assert len(r.json()) == 1


class TestAttributionEndpoint:
    async def test_attribution_schema(self, populated_client):
        r = await populated_client.get("/api/attribution")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        for row in data:
            assert "asset_class" in row
            assert "total_market_value" in row
            assert "total_pnl" in row

    async def test_attribution_has_three_classes(self, populated_client):
        r = await populated_client.get("/api/attribution")
        assert len(r.json()) == 3


class TestReconEndpoint:
    async def test_reconciliation_returns_three_checks(self, populated_client):
        r = await populated_client.get("/api/reconciliation")
        assert r.status_code == 200
        data = r.json()
        check_types = {row["check_type"] for row in data}
        assert check_types == {"nav_sum", "position_count", "price_staleness"}

    async def test_reconciliation_status_values_valid(self, populated_client):
        r = await populated_client.get("/api/reconciliation")
        for row in r.json():
            assert row["status"] in ("PASS", "BREAK")


class TestSystemMetricsEndpoint:
    async def test_system_metrics_schema(self, populated_client):
        # Insert one system_metrics row into populated_db first
        # Then assert the endpoint returns it with all expected fields
        r = await populated_client.get("/api/system/metrics?n=5")
        assert r.status_code == 200
        # If populated_db includes a system_metrics row, check its schema
        data = r.json()
        if data:
            row = data[0]
            assert "cycle_at" in row
            assert "status" in row
            assert "ingestion_latency_ms" in row


class TestCORSHeaders:
    async def test_cors_header_present(self, client):
        r = await client.get(
            "/api/health",
            headers={"Origin": "http://localhost:3000"}
        )
        assert "access-control-allow-origin" in r.headers
```

---

### `tests/test_cycle.py` — written before Step 3 (alongside test_kpis.py)

```python
# Tests for run_refresh_cycle() atomicity and system_metrics recording.
# Mocks yfinance so tests are fast and deterministic.

class TestAtomicity:
    def test_successful_cycle_writes_all_six_tables(self, tmp_db, monkeypatch):
        # Mock fetch_prices to return MOCK_PRICES_UP10
        # Call run_refresh_cycle()
        # Assert each of the 6 tables has at least 1 row
        pass

    def test_failed_cycle_leaves_no_partial_records(self, tmp_db, monkeypatch):
        # Mock fetch_prices to return MOCK_PRICES_UP10 (fetch succeeds)
        # Mock kpis.compute_nav to raise RuntimeError mid-cycle
        # Call run_refresh_cycle()
        # Assert price_snapshots is EMPTY (transaction rolled back)
        # Assert nav_snapshots is EMPTY
        # Assert position_snapshots is EMPTY
        pass

    def test_failed_cycle_still_writes_system_metrics(self, tmp_db, monkeypatch):
        # Same setup as above (compute_nav raises RuntimeError)
        # Call run_refresh_cycle()
        # Assert system_metrics has 1 row with status='FAILED'
        # Assert system_metrics error_detail contains "RuntimeError"
        # This proves system_metrics uses a separate connection from the main txn
        pass

    def test_partial_cycle_status_on_some_failed_tickers(self, tmp_db, monkeypatch):
        # Mock fetch_prices to return (MOCK_PRICES_MISSING_AAPL, ["AAPL"])
        # (2 succeeded, 1 failed)
        # Call run_refresh_cycle()
        # Assert system_metrics has status='PARTIAL'
        # Assert system_metrics tickers_succeeded=2, tickers_failed=1
        pass


class TestSystemMetricsFields:
    def test_latency_fields_are_positive(self, tmp_db, monkeypatch):
        # Mock fetch_prices to return MOCK_PRICES_UP10 with a small sleep
        # Call run_refresh_cycle()
        # Assert system_metrics ingestion_latency_ms > 0
        # Assert system_metrics db_write_latency_ms > 0
        pass

    def test_rows_processed_equals_ticker_count(self, tmp_db, monkeypatch):
        # Mock fetch_prices to return MOCK_PRICES_UP10 (3 tickers)
        # Call run_refresh_cycle()
        # Assert system_metrics total_rows_processed == 3
        pass

    def test_cycle_at_is_valid_iso8601(self, tmp_db, monkeypatch):
        # Call run_refresh_cycle()
        # Fetch system_metrics row
        # Assert datetime.fromisoformat(row["cycle_at"]) does not raise
        pass


class TestIdempotency:
    def test_two_cycles_write_two_nav_snapshots(self, tmp_db, monkeypatch):
        # Mock fetch_prices to return MOCK_PRICES_UP10
        # Call run_refresh_cycle() twice
        # Assert nav_snapshots has exactly 2 rows
        # Assert system_metrics has exactly 2 rows
        pass
```

---

## Prompts to hand Claude Code at each step

**Step 0 — Testing infrastructure:**
> "Set up the testing infrastructure for this project as specified in DASHBOARD_SPEC.md.
> Create pytest.ini, tests/conftest.py with all fixtures, and all 5 test files
> (test_db.py, test_ingest.py, test_kpis.py, test_api.py, test_cycle.py) with
> all test functions stubbed as `pass`. Do not implement any backend modules yet.
> Run `pytest tests/ -v -W ignore::DeprecationWarning` and confirm all tests are collected without syntax errors.
> Most will show as PASSED (stubs) or FAILED with ImportError — both are expected.
> Fix any syntax errors in test files before proceeding."

**Step 1 — config.py + db.py:**
> "Write the full test bodies for tests/test_db.py as specified in DASHBOARD_SPEC.md.
> Run `pytest tests/test_db.py -v -W ignore::DeprecationWarning` — all tests should fail (ImportError or assertion errors).
> Then implement config.py and db.py exactly as specified.
> Run `pytest tests/test_db.py -v -W ignore::DeprecationWarning` — all tests must pass before proceeding.
> Do not modify test files to make tests pass — fix the implementation instead."

**Step 2 — ingest.py fetch layer:**
> "Write the full test bodies for tests/test_ingest.py as specified in DASHBOARD_SPEC.md.
> Run `pytest tests/test_ingest.py -v -W ignore::DeprecationWarning` — all should fail with ImportError.
> Implement fetch_single_ticker() and fetch_prices() in ingest.py.
> All tests must use monkeypatched yfinance — never call live yfinance in tests.
> Run `pytest tests/test_ingest.py -v -W ignore::DeprecationWarning` — all must pass.
> Then run `pytest tests/ -v -W ignore::DeprecationWarning` to confirm no regressions."

**Step 3 — kpis.py + ingest.py write path:**
> "Write the full test bodies for tests/test_kpis.py and tests/test_cycle.py
> as specified in DASHBOARD_SPEC.md.
> Run both test files — should fail with ImportError.
> Implement kpis.py (compute_nav, run_reconciliation_checks, detect_anomalies)
> and complete run_refresh_cycle() in ingest.py.
> Key requirements: atomic transaction with ROLLBACK on failure, system_metrics
> written on a separate connection, std = max(std, 1e-6) in detect_anomalies.
> Run `pytest tests/test_kpis.py tests/test_cycle.py -v -W ignore::DeprecationWarning` — all must pass.
> Then run `pytest tests/ -v -W ignore::DeprecationWarning` — no regressions."

**Step 4 — main.py:**
> "Write the full test bodies for tests/test_api.py as specified in DASHBOARD_SPEC.md.
> Run `pytest tests/test_api.py -v -W ignore::DeprecationWarning` — should fail with ImportError.
> Implement main.py: FastAPI app, all 9 endpoints, Pydantic response models,
> CORS middleware, BackgroundScheduler (never BlockingScheduler), max_instances=1.
> The client fixture monkeypatches run_refresh_cycle and BackgroundScheduler.start
> to no-ops — the test app must start without calling yfinance or spawning threads.
> Empty DB endpoints must return 503 except /api/health (200) and
> /api/system/metrics (200, empty list).
> Run `pytest tests/test_api.py -v -W ignore::DeprecationWarning` — all must pass.
> Then run `pytest tests/ -v -W ignore::DeprecationWarning` — no regressions."

**Step 5 — frontend/index.html:**
> "Implement frontend/index.html as a single-file React app using CDN imports.
> Implement all 7 components including SystemHealthBar.
> Use useEffect with clearInterval cleanup — no bare setInterval.
> Use functional update form (setData(_ => newData)) in the fetch callback.
> Colors: background #0f172a, cards #1e293b, positive #22c55e, negative #ef4444,
> warning/partial #eab308.
> After implementing, run `pytest tests/ -v -W ignore::DeprecationWarning` to confirm no regressions from
> any backend changes made to support the frontend."

**Step 6 — README.md:**
> "Write README.md following the structure in DASHBOARD_SPEC.md.
> Run `pytest tests/ -v -W ignore::DeprecationWarning` one final time and confirm 100% pass rate.
> The README should include a 'Tests' section listing the test modules and
> what each covers."
