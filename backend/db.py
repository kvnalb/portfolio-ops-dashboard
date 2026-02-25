import sqlite3
from config import DB_PATH


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn


def create_tables() -> None:
    conn = get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS price_snapshots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker          TEXT NOT NULL,
            fetched_at      TEXT NOT NULL,
            market_time     TEXT,
            price           REAL NOT NULL,
            volume          INTEGER,
            day_open        REAL,
            day_high        REAL,
            day_low         REAL,
            prev_close      REAL,
            data_source     TEXT DEFAULT 'yfinance'
        );

        CREATE TABLE IF NOT EXISTS nav_snapshots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            computed_at     TEXT NOT NULL,
            total_nav       REAL NOT NULL,
            total_cost      REAL NOT NULL,
            total_pnl       REAL NOT NULL,
            total_pnl_pct   REAL NOT NULL
        );

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

        CREATE TABLE IF NOT EXISTS recon_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            checked_at      TEXT NOT NULL,
            check_type      TEXT NOT NULL,
            expected_value  REAL,
            actual_value    REAL,
            delta_pct       REAL,
            status          TEXT NOT NULL,
            detail          TEXT
        );

        CREATE TABLE IF NOT EXISTS anomaly_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            detected_at     TEXT NOT NULL,
            ticker          TEXT NOT NULL,
            asset_class     TEXT NOT NULL,
            current_price   REAL NOT NULL,
            prev_close      REAL NOT NULL,
            move_pct        REAL NOT NULL,
            zscore          REAL NOT NULL,
            severity        TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS system_metrics (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle_at                TEXT NOT NULL,
            status                  TEXT NOT NULL,
            error_detail            TEXT,
            ingestion_latency_ms    REAL,
            db_write_latency_ms     REAL,
            total_rows_processed    INTEGER,
            tickers_succeeded       INTEGER,
            tickers_failed          INTEGER
        );

        CREATE INDEX IF NOT EXISTS idx_price_ticker_time
            ON price_snapshots(ticker, fetched_at DESC);

        CREATE INDEX IF NOT EXISTS idx_nav_time
            ON nav_snapshots(computed_at DESC);

        CREATE INDEX IF NOT EXISTS idx_position_nav_id
            ON position_snapshots(nav_snapshot_id);

        CREATE INDEX IF NOT EXISTS idx_system_metrics_time
            ON system_metrics(cycle_at DESC);
    """)
    conn.close()


def get_latest_prices() -> dict:
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT ticker, price, market_time, fetched_at
            FROM price_snapshots
            WHERE (ticker, fetched_at) IN (
                SELECT ticker, MAX(fetched_at)
                FROM price_snapshots
                GROUP BY ticker
            )
        """).fetchall()
        return {row["ticker"]: dict(row) for row in rows}
    finally:
        conn.close()


def get_nav_history(n: int = 50) -> list | None:
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT computed_at, total_nav, total_pnl, total_pnl_pct
            FROM nav_snapshots
            ORDER BY computed_at DESC
            LIMIT ?
        """, (n,)).fetchall()
        if not rows:
            return None
        return [dict(row) for row in reversed(rows)]
    finally:
        conn.close()


def get_nav_current() -> dict | None:
    conn = get_connection()
    try:
        nav_row = conn.execute("""
            SELECT * FROM nav_snapshots ORDER BY computed_at DESC LIMIT 1
        """).fetchone()
        if nav_row is None:
            return None
        nav_id = nav_row["id"]
        positions = conn.execute("""
            SELECT * FROM position_snapshots WHERE nav_snapshot_id = ?
        """, (nav_id,)).fetchall()
        result = dict(nav_row)
        result["positions"] = [dict(p) for p in positions]
        return result
    finally:
        conn.close()


def get_asset_class_attribution() -> list | None:
    conn = get_connection()
    try:
        rows = conn.execute("""
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
        """).fetchall()
        if not rows:
            return None
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_position_detail() -> list | None:
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT ticker, asset_class, shares, price, cost_basis,
                   market_value, unrealized_pnl, pnl_pct, weight
            FROM position_snapshots
            WHERE nav_snapshot_id = (SELECT MAX(id) FROM nav_snapshots)
            ORDER BY market_value DESC
        """).fetchall()
        if not rows:
            return None
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_price_history(ticker: str, n: int = 50) -> list:
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT fetched_at, price
            FROM price_snapshots
            WHERE ticker = ?
            ORDER BY fetched_at DESC
            LIMIT ?
        """, (ticker, n)).fetchall()
        return [dict(row) for row in reversed(rows)]
    finally:
        conn.close()


def get_recent_anomalies(n: int = 20) -> list:
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT detected_at, ticker, asset_class, move_pct, zscore, severity
            FROM anomaly_log
            ORDER BY detected_at DESC
            LIMIT ?
        """, (n,)).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_recon_status() -> list:
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT check_type, checked_at, status, delta_pct, detail
            FROM recon_log
            WHERE (check_type, checked_at) IN (
                SELECT check_type, MAX(checked_at)
                FROM recon_log
                GROUP BY check_type
            )
        """).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_system_health(n: int = 30) -> list:
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT cycle_at, status, error_detail,
                   ingestion_latency_ms, db_write_latency_ms,
                   total_rows_processed, tickers_succeeded, tickers_failed
            FROM system_metrics
            ORDER BY cycle_at DESC
            LIMIT ?
        """, (n,)).fetchall()
        return [dict(row) for row in reversed(rows)]
    finally:
        conn.close()
