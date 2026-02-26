import pytest
import math
import logging
import ingest


# ─── Shared mock helpers ─────────────────────────────────────────────────────

def _make_fast_info(
    price=110.0, prev_close=109.0, volume=1_000_000,
    day_open=108.0, day_high=111.0, day_low=107.0, market_time=None,
):
    """Return a minimal fast_info-like object matching yfinance's FastInfo API."""
    class _FI:
        pass

    fi = _FI()
    fi.last_price = price
    fi.previous_close = prev_close
    fi.last_volume = volume
    fi.open = day_open
    fi.day_high = day_high
    fi.day_low = day_low
    # .get() simulates dict-like access used for regularMarketTime
    fi.get = lambda key, default=None: (market_time if key == "regularMarketTime" else default)
    return fi


def _ticker_for(fi):
    """Return a yf.Ticker replacement class that always yields the given fast_info."""
    class _T:
        def __init__(self, ticker):
            self.fast_info = fi
    return _T


# ─── Tests ───────────────────────────────────────────────────────────────────

class TestNaNSanitization:
    def test_nan_price_returns_none(self, monkeypatch):
        # price is NOT NULL in schema — NaN price must make fetch fail
        fi = _make_fast_info(price=float("nan"))
        monkeypatch.setattr(ingest.yf, "Ticker", _ticker_for(fi))
        result = ingest.fetch_single_ticker("AAPL")
        assert result is None

    def test_nan_volume_becomes_none(self, monkeypatch):
        # volume is nullable — NaN should be sanitized to None, not blow up
        fi = _make_fast_info(volume=float("nan"))
        monkeypatch.setattr(ingest.yf, "Ticker", _ticker_for(fi))
        result = ingest.fetch_single_ticker("AAPL")
        assert result is not None
        assert result["volume"] is None

    def test_nan_prev_close_becomes_none(self, monkeypatch):
        fi = _make_fast_info(prev_close=float("nan"))
        monkeypatch.setattr(ingest.yf, "Ticker", _ticker_for(fi))
        result = ingest.fetch_single_ticker("AAPL")
        assert result is not None
        assert result["prev_close"] is None

    def test_no_nan_values_survive_in_output(self, monkeypatch):
        # All optional fields are NaN; price is valid — no NaN must reach the dict
        fi = _make_fast_info(
            prev_close=float("nan"),
            volume=float("nan"),
            day_open=float("nan"),
            day_high=float("nan"),
            day_low=float("nan"),
        )
        monkeypatch.setattr(ingest.yf, "Ticker", _ticker_for(fi))
        result = ingest.fetch_single_ticker("AAPL")
        assert result is not None
        assert not any(isinstance(v, float) and math.isnan(v) for v in result.values())


class TestPerTickerFailureIsolation:
    def test_one_bad_ticker_does_not_stop_others(self, monkeypatch):
        good_fi = _make_fast_info()

        def mock_ticker(ticker):
            if ticker == "BADTICKER":
                raise RuntimeError("fetch failed")
            return _ticker_for(good_fi)(ticker)

        monkeypatch.setattr(ingest.yf, "Ticker", mock_ticker)
        results, failed = ingest.fetch_prices(["AAPL", "BADTICKER", "GLD"])
        assert "AAPL" in results
        assert "GLD" in results
        assert "BADTICKER" in failed

    def test_all_tickers_fail_returns_empty_dict(self, monkeypatch):
        monkeypatch.setattr(ingest.yf, "Ticker", lambda t: (_ for _ in ()).throw(RuntimeError("always fails")))
        results, failed = ingest.fetch_prices(["AAPL", "GLD"])
        assert results == {}
        assert set(failed) == {"AAPL", "GLD"}

    def test_failed_tickers_logged_as_warning(self, monkeypatch, caplog):
        monkeypatch.setattr(ingest.yf, "Ticker", lambda t: (_ for _ in ()).throw(RuntimeError("fetch failed")))
        with caplog.at_level(logging.WARNING):
            ingest.fetch_prices(["AAPL"])
        assert "AAPL" in caplog.text
        assert any(r.levelno >= logging.WARNING for r in caplog.records)


class TestMarketTime:
    def test_market_time_captured_when_available(self, monkeypatch):
        from datetime import datetime, timezone
        market_dt = datetime(2024, 1, 15, 15, 0, 0, tzinfo=timezone.utc)
        fi = _make_fast_info(market_time=market_dt)
        monkeypatch.setattr(ingest.yf, "Ticker", _ticker_for(fi))
        result = ingest.fetch_single_ticker("AAPL")
        assert result is not None
        assert result["market_time"] is not None
        datetime.fromisoformat(result["market_time"])  # must be valid ISO8601

    def test_market_time_is_none_when_unavailable(self, monkeypatch):
        fi = _make_fast_info(market_time=None)
        monkeypatch.setattr(ingest.yf, "Ticker", _ticker_for(fi))
        result = ingest.fetch_single_ticker("AAPL")
        assert result is not None
        assert result["market_time"] is None


class TestFetchNeverCallsBatchDownload:
    def test_download_is_never_called(self, monkeypatch):
        def bad_download(*args, **kwargs):
            raise AssertionError("batch download called")

        fi = _make_fast_info()
        monkeypatch.setattr(ingest.yf, "Ticker", _ticker_for(fi))
        monkeypatch.setattr(ingest.yf, "download", bad_download)
        # Must not raise — confirms only per-ticker yf.Ticker() path is used
        ingest.fetch_prices(["AAPL", "GLD"])
