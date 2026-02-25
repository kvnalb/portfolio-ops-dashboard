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
NAV_RECON_TOLERANCE = 0.01        # Flag reconciliation breaks > 1%
ANOMALY_ZSCORE_THRESHOLD = 2.0
ANOMALY_LOOKBACK_PERIODS = 20
