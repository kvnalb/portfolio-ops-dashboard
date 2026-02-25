import pytest
import sqlite3
import db


class TestSchema:
    def test_all_six_tables_created(self, tmp_db):
        conn = sqlite3.connect(tmp_db)
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()}
        conn.close()
        assert tables == {
            "price_snapshots", "nav_snapshots", "position_snapshots",
            "recon_log", "anomaly_log", "system_metrics",
        }

    def test_four_indexes_created(self, tmp_db):
        conn = sqlite3.connect(tmp_db)
        indexes = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()}
        conn.close()
        assert indexes == {
            "idx_price_ticker_time",
            "idx_nav_time",
            "idx_position_nav_id",
            "idx_system_metrics_time",
        }

    def test_foreign_keys_enforced(self, db_conn):
        with pytest.raises(sqlite3.IntegrityError):
            db_conn.execute(
                "INSERT INTO position_snapshots "
                "(nav_snapshot_id, ticker, asset_class, shares, price, cost_basis, "
                "market_value, unrealized_pnl, pnl_pct, weight) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (9999, "AAPL", "equity", 10, 110.0, 100.0, 1100.0, 100.0, 0.1, 0.333),
            )

    def test_wal_mode_active(self, db_conn):
        result = db_conn.execute("PRAGMA journal_mode;").fetchone()[0]
        assert result == "wal"

    def test_create_tables_idempotent(self, tmp_db):
        db.create_tables()  # second call — IF NOT EXISTS must not raise


class TestGetConnection:
    def test_returns_row_factory_connection(self, db_conn):
        db_conn.execute(
            "INSERT INTO price_snapshots (ticker, fetched_at, price) VALUES (?, ?, ?)",
            ("AAPL", "2024-01-15T15:00:00+00:00", 110.0),
        )
        db_conn.commit()
        row = db_conn.execute(
            "SELECT ticker, price FROM price_snapshots WHERE ticker='AAPL'"
        ).fetchone()
        # Column-by-name access confirms row_factory = sqlite3.Row
        assert row["ticker"] == "AAPL"
        assert row["price"] == 110.0

    def test_direct_sqlite_connect_would_miss_pragma(self, tmp_db):
        conn = sqlite3.connect(tmp_db)
        result = conn.execute("PRAGMA foreign_keys;").fetchone()[0]
        conn.close()
        # Raw connection has FK enforcement OFF — documents why get_connection() is required
        assert result == 0


class TestQueryFunctions:
    def test_get_latest_prices_returns_most_recent_only(self, populated_db):
        # Insert an older price for AAPL — get_latest_prices() must ignore it
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO price_snapshots (ticker, fetched_at, price) VALUES (?, ?, ?)",
            ("AAPL", "2024-01-01T10:00:00+00:00", 90.0),
        )
        conn.commit()
        conn.close()

        result = db.get_latest_prices()
        # populated_db has AAPL at 2024-01-15 with price=110.0 (newer wins)
        assert result["AAPL"]["price"] == 110.0

    def test_get_nav_history_returns_ascending(self, populated_db):
        # populated_db already has 1 row at 2024-01-15; add 2 more at later timestamps
        conn = db.get_connection()
        for ts in ("2024-01-16T10:00:00+00:00", "2024-01-17T10:00:00+00:00"):
            conn.execute(
                "INSERT INTO nav_snapshots "
                "(computed_at, total_nav, total_cost, total_pnl, total_pnl_pct) "
                "VALUES (?, ?, ?, ?, ?)",
                (ts, 3300.0, 3000.0, 300.0, 0.10),
            )
        conn.commit()
        conn.close()

        result = db.get_nav_history(n=3)
        assert len(result) == 3
        assert result[0]["computed_at"] < result[1]["computed_at"] < result[2]["computed_at"]

    def test_get_nav_history_respects_n_limit(self, populated_db):
        # Insert 9 more nav_snapshots (total 10 in DB)
        conn = db.get_connection()
        for i in range(9):
            ts = f"2024-01-{16 + i:02d}T10:00:00+00:00"
            conn.execute(
                "INSERT INTO nav_snapshots "
                "(computed_at, total_nav, total_cost, total_pnl, total_pnl_pct) "
                "VALUES (?, ?, ?, ?, ?)",
                (ts, 3300.0, 3000.0, 300.0, 0.10),
            )
        conn.commit()
        conn.close()

        result = db.get_nav_history(n=3)
        assert len(result) == 3

    def test_get_asset_class_attribution_groups_correctly(self, populated_db):
        result = db.get_asset_class_attribution()
        # populated_db has 3 positions: equity, fixed_income, commodity
        assert len(result) == 3
        for row in result:
            assert "asset_class" in row
            assert "total_market_value" in row
            assert "total_pnl" in row
            assert "total_weight" in row
            assert "avg_pnl_pct" in row
        total_weight = sum(row["total_weight"] for row in result)
        assert total_weight == pytest.approx(1.0, abs=0.001)

    def test_get_position_detail_ordered_by_market_value(self, populated_db):
        result = db.get_position_detail()
        assert result[0]["market_value"] >= result[1]["market_value"] >= result[2]["market_value"]

    def test_get_recon_status_returns_latest_per_check_type(self, populated_db):
        # populated_db has nav_sum=PASS at 2024-01-15; insert a newer BREAK
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO recon_log "
            "(checked_at, check_type, expected_value, actual_value, delta_pct, status, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "2024-01-16T15:00:00+00:00", "nav_sum",
                3300.0, 3135.0, 0.05, "BREAK",
                "DB sum $3135.00 vs in-memory $3300.00",
            ),
        )
        conn.commit()
        conn.close()

        result = db.get_recon_status()
        nav_sum_row = next(r for r in result if r["check_type"] == "nav_sum")
        assert nav_sum_row["status"] == "BREAK"

    def test_get_system_health_returns_ascending(self, populated_db):
        # populated_db has 1 system_metrics row; add 4 more at later timestamps
        conn = db.get_connection()
        for i in range(4):
            ts = f"2024-01-{16 + i:02d}T10:00:00+00:00"
            conn.execute(
                "INSERT INTO system_metrics "
                "(cycle_at, status, ingestion_latency_ms, db_write_latency_ms, "
                "total_rows_processed, tickers_succeeded, tickers_failed) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ts, "SUCCESS", 200.0, 20.0, 3, 3, 0),
            )
        conn.commit()
        conn.close()

        result = db.get_system_health(n=5)
        assert len(result) == 5
        for i in range(len(result) - 1):
            assert result[i]["cycle_at"] < result[i + 1]["cycle_at"]

    def test_get_system_health_respects_n_limit(self, populated_db):
        # Insert 9 more rows (total 10 in DB)
        conn = db.get_connection()
        for i in range(9):
            ts = f"2024-01-{16 + i:02d}T10:00:00+00:00"
            conn.execute(
                "INSERT INTO system_metrics "
                "(cycle_at, status, ingestion_latency_ms, db_write_latency_ms, "
                "total_rows_processed, tickers_succeeded, tickers_failed) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ts, "SUCCESS", 200.0, 20.0, 3, 3, 0),
            )
        conn.commit()
        conn.close()

        result = db.get_system_health(n=4)
        assert len(result) == 4
