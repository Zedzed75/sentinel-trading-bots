"""Donnees fictives pour tester l'affichage du dashboard sans VPS ni MT5.

Usage :  python sentinel_dashboard.py --mock  (ou SENTINEL_DASHBOARD_MOCK=1)
Le serveur sert alors cet etat statique : aucun acces MT5, psutil ni
fichiers de la flotte ; les boutons d'action repondent "mode mock".
"""

from datetime import datetime, timezone


def get_state() -> dict:
    now = datetime.now(timezone.utc)
    return {
        "heure": now.strftime("%Y-%m-%d %H:%M:%S UTC") + " (MOCK)",
        "compte": {"ok": True, "balance": 10250.00, "equity": 10187.42,
                   "margin_free": 9414.10, "margin_level": 512.3,
                   "currency": "EUR"},
        "marge_alerte": False,
        "meteo": {
            "weather": "ORAGEUX", "confidence": 0.76,
            "focus": "CPI US a 14:30 UTC - catalyseur intraday dominant",
            "geo_resume": "Prime de risque sur le Brent : escalade en mer "
                          "Rouge et menaces sur Ormuz.",
            "macro_resume": "Le CPI decidera de la trajectoire de la Fed ; "
                            "le dollar est vulnerable a une surprise basse.",
            "sentiment_resume": "Declarations tarifaires agressives : "
                                "risque de gap a l'ouverture US.",
            "banks_resume": "JPMorgan maintient un biais acheteur XAUUSD "
                            "vise 4200 ; Goldman voit un Brent a 95 fin "
                            "d'annee.",
            "conflict": "Le geopolitique craint un choc petrolier haussier "
                        "mais le desk GS voit une capitulation des "
                        "acheteurs : le juge tranche ORAGEUX en privilegiant "
                        "la volatilite plutot que la direction.",
            "date": now.date().isoformat(),
        },
        "bots": [
            {"id": 1, "nom": "Intraday multi-actifs", "statut": "actif",
             "pnl_jour": -401.02, "trades_jour": 5, "trade": True},
            {"id": 2, "nom": "Stat-arb Brent/WTI", "statut": "actif",
             "pnl_jour": 86.40, "trades_jour": 1, "trade": True},
            {"id": 3, "nom": "Trend-following H4", "statut": "actif",
             "pnl_jour": 0.0, "trades_jour": 0, "trade": True},
            {"id": 4, "nom": "Orchestrateur", "statut": "actif",
             "pnl_jour": 0, "trades_jour": 0, "trade": False},
            {"id": 5, "nom": "Analytics", "statut": "fige",
             "pnl_jour": 0, "trades_jour": 0, "trade": False},
            {"id": 6, "nom": "Telegram", "statut": "arrete",
             "pnl_jour": 0, "trades_jour": 0, "trade": False},
            {"id": 7, "nom": "Macro Analyst", "statut": "actif",
             "pnl_jour": 0, "trades_jour": 0, "trade": False},
        ],
        "jauge_jour": {"pct": -1.92, "used": 0.48, "limit_pct": -4.0},
        "verrou_global": False,
        "risk_scale": 1.0,
        "positions": [
            {"ticket": 79512345, "symbol": "SpotBrent", "sens": "LONG",
             "volume": 0.5, "pnl": 42.17, "strategie": "statarb"},
            {"ticket": 79512346, "symbol": "SpotCrude", "sens": "SHORT",
             "volume": 0.55, "pnl": 44.23, "strategie": "statarb"},
            {"ticket": 79512399, "symbol": "XAUUSD", "sens": "LONG",
             "volume": 0.11, "pnl": -12.50, "strategie": "breakout"},
        ],
        "systeme": {"cpu": 17.0, "ram": 47.0, "watchdog": True},
    }
