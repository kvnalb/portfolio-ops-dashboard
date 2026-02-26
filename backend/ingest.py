import math
import logging
import time
from datetime import datetime, timezone

import yfinance as yf

logger = logging.getLogger(__name__)


def fetch_single_ticker(ticker: str) -> dict | None:
    """
    Fetch current price data for one ticker via yf.Ticker().
    Returns a dict or None on failure. Never raises.

    Field access order:
    1. yf.Ticker(t).fast_info  — for current price, prev_close, volume
    2. yf.Ticker(t).history(period='2d', interval='1m').iloc[-1]  — fallback

    NaN sanitization: after fetching, replace all float NaN values with None.
    market_time: read from fast_info.get('regularMarketTime') if available;
                 convert to ISO8601 UTC string, or None if unavailable.
    """
    try:
        t = yf.Ticker(ticker)
        fi = t.fast_info

        # Price is required (NOT NULL in schema). NaN price is a hard failure.
        price = fi.last_price
        if price is None or (isinstance(price, float) and math.isnan(price)):
            return None

        def _sanitize(v):
            if isinstance(v, float) and math.isnan(v):
                return None
            return v

        prev_close = _sanitize(getattr(fi, "previous_close", None))
        volume     = _sanitize(getattr(fi, "last_volume",    None))
        day_open   = _sanitize(getattr(fi, "open",           None))
        day_high   = _sanitize(getattr(fi, "day_high",       None))
        day_low    = _sanitize(getattr(fi, "day_low",        None))

        # market_time: capture the data's own timestamp (not server clock).
        market_time = None
        try:
            mt = fi.get("regularMarketTime")
            if mt is not None:
                if hasattr(mt, "astimezone"):
                    market_time = mt.astimezone(timezone.utc).isoformat()
                elif hasattr(mt, "isoformat"):
                    market_time = mt.isoformat()
                else:
                    market_time = str(mt)
        except (AttributeError, KeyError, TypeError):
            pass

        return {
            "price":      price,
            "prev_close": prev_close,
            "volume":     volume,
            "day_open":   day_open,
            "day_high":   day_high,
            "day_low":    day_low,
            "market_time": market_time,
        }

    except Exception:
        return None


def fetch_prices(tickers: list[str]) -> tuple[dict, list[str]]:
    """
    Call fetch_single_ticker() for each ticker sequentially.
    On per-ticker failure: log a warning, continue.
    Returns: (results_dict keyed by ticker, list of failed tickers)
    """
    results: dict = {}
    failed:  list[str] = []

    for ticker in tickers:
        result = fetch_single_ticker(ticker)
        if result is None:
            logger.warning("Failed to fetch ticker: %s", ticker)
            failed.append(ticker)
        else:
            results[ticker] = result

    return results, failed


def run_refresh_cycle() -> None:
    """
    Full refresh cycle — implemented in Step 3.
    Stub here so test_api.py can import and monkeypatch it.
    """
    raise NotImplementedError("run_refresh_cycle() implemented in Step 3")
