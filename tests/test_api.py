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
