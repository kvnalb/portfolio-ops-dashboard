import pytest


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
