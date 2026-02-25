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
