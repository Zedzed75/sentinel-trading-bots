"""Fake data to test the dashboard display without a VPS or MT5.

Usage:  python sentinel_dashboard.py --mock  (or SENTINEL_DASHBOARD_MOCK=1)
The server then serves this static state: no MT5, psutil or fleet-file
access; the action buttons answer "mock mode".
"""

from datetime import datetime, timezone


def get_state() -> dict:
    now = datetime.now(timezone.utc)
    return {
        "time": now.strftime("%Y-%m-%d %H:%M:%S UTC") + " (MOCK)",
        "account": {"ok": True, "balance": 10250.00, "equity": 10187.42,
                    "margin_free": 9414.10, "margin_level": 512.3,
                    "currency": "EUR"},
        "margin_alert": False,
        "weather": {
            "weather": "STORMY", "confidence": 0.76,
            "focus": "US CPI at 14:30 UTC - dominant intraday catalyst",
            "geo_summary": "Risk premium on Brent: escalation in the Red "
                           "Sea and threats on Hormuz.",
            "macro_summary": "The CPI will decide the Fed's trajectory; "
                             "the dollar is vulnerable to a low surprise.",
            "sentiment_summary": "Aggressive tariff statements: gap risk "
                                 "at the US open.",
            "banks_summary": "JPMorgan keeps a bullish bias XAUUSD targets "
                             "4200; Goldman sees Brent at 95 by year-end.",
            "conflict": "Geopolitics fears a bullish oil shock but the GS "
                        "desk sees buyer capitulation: the judge settles "
                        "STORMY, favouring volatility over direction.",
            "date": now.date().isoformat(),
        },
        "bots": [
            {"id": 1, "name": "Intraday multi-asset", "status": "active",
             "day_pnl": -401.02, "day_trades": 5, "trade": True},
            {"id": 2, "name": "Stat-arb Brent/WTI", "status": "active",
             "day_pnl": 86.40, "day_trades": 1, "trade": True},
            {"id": 3, "name": "Trend-following H4", "status": "active",
             "day_pnl": 0.0, "day_trades": 0, "trade": True},
            {"id": 4, "name": "Orchestrator", "status": "active",
             "day_pnl": 0, "day_trades": 0, "trade": False},
            {"id": 5, "name": "Analytics", "status": "frozen",
             "day_pnl": 0, "day_trades": 0, "trade": False},
            {"id": 6, "name": "Telegram", "status": "stopped",
             "day_pnl": 0, "day_trades": 0, "trade": False},
            {"id": 7, "name": "Macro Analyst", "status": "active",
             "day_pnl": 0, "day_trades": 0, "trade": False},
        ],
        "daily_gauge": {"pct": -1.92, "used": 0.48, "limit_pct": -4.0},
        "global_lock": False,
        "risk_scale": 1.0,
        "positions": [
            {"ticket": 79512345, "symbol": "SpotBrent", "side": "LONG",
             "volume": 0.5, "pnl": 42.17, "strategy": "statarb"},
            {"ticket": 79512346, "symbol": "SpotCrude", "side": "SHORT",
             "volume": 0.55, "pnl": 44.23, "strategy": "statarb"},
            {"ticket": 79512399, "symbol": "XAUUSD", "side": "LONG",
             "volume": 0.11, "pnl": -12.50, "strategy": "breakout"},
        ],
        "system": {"cpu": 17.0, "ram": 47.0, "watchdog": True},
    }
