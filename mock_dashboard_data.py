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
            {"id": 8, "name": "Arbitrage", "status": "active",
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


def get_arbitrage() -> dict:
    """Fake bot 8 data for the KPI cards and the arbitrage table."""
    return {
        "metrics": {"trades": 21, "win_rate": 61.90, "profit_factor": 1.68,
                    "sharpe": 2.15, "max_drawdown": 740.0,
                    "max_drawdown_pct": -7.40, "total_pnl": 1240.50},
        "rows": [
            {"date": "2026-07-17 21:05", "asset": "XAUUSD.p",
             "direction": "LONG", "mt5_action": "Long execution (breakout)",
             "bot7_view": "STORMY (US CPI at 14:30 UTC)",
             "is_aligned": True, "pnl": 450.00, "winner": "ALIGNED."},
            {"date": "2026-07-17 18:40", "asset": "SpotBrent",
             "direction": "SHORT", "mt5_action": "Short execution (statarb)",
             "bot7_view": "STORMY (US CPI at 14:30 UTC)",
             "is_aligned": False, "pnl": -350.00,
             "winner": "Bot 7 (macro) was right. The semantic filter saw "
                       "it coming."},
            {"date": "2026-07-16 15:12", "asset": "EURUSD.p",
             "direction": "SHORT", "mt5_action": "Short execution (reversion)",
             "bot7_view": "CALM (quiet macro calendar)",
             "is_aligned": True, "pnl": 86.40, "winner": "ALIGNED."},
        ],
        "total": 3, "assets": ["EURUSD.p", "SpotBrent", "XAUUSD.p"],
        "page": 1, "pages": 1, "asset": "", "start": "", "end": "",
    }
