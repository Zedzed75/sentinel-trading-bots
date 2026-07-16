"""SENTINEL DASHBOARD - Suivi mobile de la flotte (lecture seule).

Serveur web asynchrone (FastAPI) qui centralise les metriques de la flotte
et les expose sur une page responsive (Jinja2 + DaisyUI via CDN, aucune
ecriture : fichiers JSON des bots 4/5, heartbeats, API MT5, psutil).

Securite : Basic Auth obligatoire (dashboard_config.json, gitignore, voir
dashboard_config.example.json). Le serveur refuse de demarrer sans mot de
passe. Pour une exposition hors du reseau local, servir en HTTPS :
  uvicorn sentinel_dashboard:app --host 0.0.0.0 --port 8787 \
      --ssl-keyfile key.pem --ssl-certfile cert.pem

Usage local :  python sentinel_dashboard.py [port]
Tests :        tests/test_sentinel_dashboard.py (JSON corrompus/absents).
"""

import csv
import json
import logging
import os
import secrets
import sys
from datetime import datetime, timezone

import psutil
import MetaTrader5 as mt5
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
_DIR = os.path.dirname(os.path.abspath(__file__))
BOTS_DIR = os.path.join(_DIR, "bots")
LOG_DIR = os.path.join(_DIR, "logs")
TRADES_CSV = os.path.join(LOG_DIR, "trades.csv")
CONFIG_FILE = os.path.join(_DIR, "dashboard_config.json")
TEMPLATES_DIR = os.path.join(_DIR, "templates")

MARGIN_ALERT_LEVEL = 150.0    # alerte rouge sous 150% de niveau de marge
DAILY_DD_LIMIT = 0.04         # seuil critique de la jauge journaliere (bot 1)

# Magic -> strategie (copie volontaire, pas d'imports croises entre modules)
MAGIC_STRATEGY = {
    1001: "breakout", 2001: "breakout", 3001: "breakout",
    1002: "reversion", 2002: "reversion", 3002: "reversion",
    4001: "statarb",
    5001: "trend", 5002: "trend", 5003: "trend", 5004: "trend",
    5005: "trend",
}

# La flotte : heartbeat max aligne sur ops/watchdog.ps1, strategies pour le
# PnL du jour, fichier d'etat pour le statut "verrouille".
FLEET = (
    {"id": 1, "script": "sentinel_bot.py", "nom": "Intraday multi-actifs",
     "hb_max": 300, "strategies": ("breakout", "reversion"),
     "state": "sentinel_state.json"},
    {"id": 2, "script": "sentinel_alpha_compound.py", "nom": "Stat-arb Brent/WTI",
     "hb_max": 300, "strategies": ("statarb",), "state": "alpha_state.json"},
    {"id": 3, "script": "sentinel_trend.py", "nom": "Trend-following H4",
     "hb_max": 300, "strategies": ("trend",), "state": "trend_state.json"},
    {"id": 4, "script": "sentinel_risk_orchestrator.py", "nom": "Orchestrateur",
     "hb_max": 300, "strategies": (), "state": "orchestrator_state.json"},
    {"id": 5, "script": "sentinel_trade_analytics.py", "nom": "Analytics",
     "hb_max": 2700, "strategies": (), "state": None},
    {"id": 6, "script": "sentinel_telegram.py", "nom": "Telegram",
     "hb_max": 300, "strategies": (), "state": None},
    {"id": 7, "script": "sentinel_macro_analyst.py", "nom": "Macro Analyst",
     "hb_max": 300, "strategies": (), "state": None},
)

log = logging.getLogger("dashboard")


# ----------------------------------------------------------------------------
# Lectures robustes (fonctions pures, testables) : un fichier absent, vide ou
# corrompu ne doit JAMAIS faire planter l'interface.
# ----------------------------------------------------------------------------
def load_json(path: str) -> dict:
    """Dict du fichier JSON, {} si absent/vide/corrompu/pas un objet."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def read_trades(path: str = TRADES_CSV) -> list[dict]:
    """Journal du bot 5 ; lignes illisibles ignorees, [] si fichier KO."""
    rows = []
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                try:
                    rows.append({
                        "pnl": float(r["pnl"]), "strategy": r["strategy"],
                        "close_time": datetime.fromisoformat(r["close_time"]),
                    })
                except (ValueError, KeyError, TypeError):
                    continue
    except OSError:
        pass
    return rows


def day_stats(trades: list[dict], now: datetime) -> dict[str, dict]:
    """{strategie: {pnl, n}} des trades fermes aujourd'hui (UTC)."""
    out: dict[str, dict] = {}
    for t in trades:
        if t["close_time"].date() == now.date():
            st = out.setdefault(t["strategy"], {"pnl": 0.0, "n": 0})
            st["pnl"] = round(st["pnl"] + t["pnl"], 2)
            st["n"] += 1
    return out


def heartbeat_age(script: str, now: datetime) -> float | None:
    """Age (s) du heartbeat logs/<bot>.hb, None si absent/illisible."""
    path = os.path.join(LOG_DIR, script.replace(".py", ".hb"))
    try:
        with open(path, encoding="utf-8") as fh:
            return (now - datetime.fromisoformat(fh.read().strip())
                    ).total_seconds()
    except (OSError, ValueError):
        return None


def bot_status(hb_age: float | None, hb_max: int, locked: bool) -> str:
    """suspendu (verrou) > actif (hb frais) > fige (hb vieux) > arrete."""
    if locked:
        return "suspendu"
    if hb_age is None:
        return "arrete"
    return "actif" if hb_age <= hb_max else "fige"


def daily_gauge(equity: float | None, day_balance: float | None) -> dict:
    """Jauge de perte journaliere : pct signe et part du seuil consommee."""
    if not equity or not day_balance:
        return {"pct": None, "used": 0.0, "limit_pct": -DAILY_DD_LIMIT * 100}
    pct = (equity - day_balance) / day_balance * 100
    used = min(max(-pct / (DAILY_DD_LIMIT * 100), 0.0), 1.0)  # 0..1 vers -4%
    return {"pct": round(pct, 2), "used": round(used, 3),
            "limit_pct": -DAILY_DD_LIMIT * 100}


def watchdog_alive() -> bool:
    """True si un processus powershell execute ops/watchdog.ps1."""
    try:
        for p in psutil.process_iter(["name", "cmdline"]):
            name = (p.info["name"] or "").lower()
            if "powershell" in name or "pwsh" in name:
                if any("watchdog.ps1" in (a or "")
                       for a in (p.info["cmdline"] or [])):
                    return True
    except Exception:                                  # psutil indisponible
        pass
    return False


def account_snapshot() -> dict:
    """Balance/equite/marge depuis MT5 ; valeurs None si terminal KO."""
    acc = mt5.account_info()
    if acc is None:
        mt5.initialize()                               # tentative de reprise
        acc = mt5.account_info()
    if acc is None:
        return {"ok": False, "balance": None, "equity": None,
                "margin_free": None, "margin_level": None, "currency": ""}
    return {"ok": True, "balance": round(acc.balance, 2),
            "equity": round(acc.equity, 2),
            "margin_free": round(acc.margin_free, 2),
            "margin_level": (round(acc.margin_level, 1)
                             if acc.margin_level else None),
            "currency": acc.currency}


def open_positions() -> list[dict]:
    """Tickets ouverts de la flotte (magics Sentinel uniquement)."""
    out = []
    for p in mt5.positions_get() or []:
        if p.magic not in MAGIC_STRATEGY:
            continue
        out.append({"ticket": p.ticket, "symbol": p.symbol,
                    "sens": "LONG" if p.type == mt5.POSITION_TYPE_BUY
                    else "SHORT", "volume": p.volume,
                    "pnl": round(p.profit, 2),
                    "strategie": MAGIC_STRATEGY[p.magic]})
    return out


def build_state(now: datetime | None = None) -> dict:
    """Instantane complet servi a l'interface (ne leve jamais)."""
    now = now or datetime.now(timezone.utc)
    trades = read_trades()
    per_strategy = day_stats(trades, now)
    bots = []
    for b in FLEET:
        locked = bool(b["state"]
                      and load_json(os.path.join(BOTS_DIR,
                                                 b["state"])).get("locked"))
        pnl = round(sum(per_strategy.get(s, {}).get("pnl", 0.0)
                        for s in b["strategies"]), 2)
        n = sum(per_strategy.get(s, {}).get("n", 0) for s in b["strategies"])
        bots.append({"id": b["id"], "nom": b["nom"], "script": b["script"],
                     "statut": bot_status(heartbeat_age(b["script"], now),
                                          b["hb_max"], locked),
                     "pnl_jour": pnl, "trades_jour": n,
                     "trade": bool(b["strategies"])})
    acc = account_snapshot()
    day_ref = load_json(os.path.join(BOTS_DIR,
                                     "sentinel_state.json")).get("day_balance")
    try:
        cpu, ram = psutil.cpu_percent(), psutil.virtual_memory().percent
    except Exception:
        cpu = ram = None
    return {
        "heure": now.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "compte": acc,
        "marge_alerte": bool(acc["margin_level"] is not None
                             and acc["margin_level"] < MARGIN_ALERT_LEVEL),
        "bots": bots,
        "jauge_jour": daily_gauge(acc["equity"], day_ref),
        "verrou_global": bool(load_json(os.path.join(
            BOTS_DIR, "orchestrator_state.json")).get("locked")),
        "risk_scale": load_json(os.path.join(
            BOTS_DIR, "risk_scale.json")).get("scale"),
        "positions": open_positions(),
        "systeme": {"cpu": cpu, "ram": ram, "watchdog": watchdog_alive()},
    }


# ----------------------------------------------------------------------------
# Application FastAPI (Basic Auth sur tout)
# ----------------------------------------------------------------------------
app = FastAPI(title="Sentinel Dashboard", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=TEMPLATES_DIR)
_basic = HTTPBasic()


def _credentials() -> tuple[str, str]:
    cfg = load_json(CONFIG_FILE)
    return (cfg.get("user") or os.environ.get("DASHBOARD_USER") or "sentinel",
            cfg.get("password") or os.environ.get("DASHBOARD_PASSWORD") or "")


def require_auth(creds: HTTPBasicCredentials = Depends(_basic)) -> str:
    user, password = _credentials()
    if not password:                       # jamais d'acces sans mot de passe
        raise HTTPException(503, "Mot de passe non configure "
                                 "(dashboard_config.json).")
    if not (secrets.compare_digest(creds.username, user)
            and secrets.compare_digest(creds.password, password)):
        raise HTTPException(401, "Identifiants invalides.",
                            headers={"WWW-Authenticate": "Basic"})
    return creds.username


@app.get("/")
def index(request: Request, _: str = Depends(require_auth)):
    return templates.TemplateResponse(request, "dashboard.html",
                                      {"seuil_marge": MARGIN_ALERT_LEVEL})


@app.get("/api/state")
def api_state(_: str = Depends(require_auth)) -> dict:
    return build_state()


def main() -> int:
    import uvicorn
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")
    if not _credentials()[1]:
        log.error("Aucun mot de passe : copier dashboard_config.example.json "
                  "vers dashboard_config.json et definir 'password'.")
        return 1
    if not mt5.initialize(
            path="C:/Program Files/Pepperstone MetaTrader 5/terminal64.exe"):
        log.warning("MT5 indisponible au demarrage (%s) : les donnees compte "
                    "seront vides jusqu'a la reprise.", mt5.last_error())
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8787
    log.info("Dashboard sur http://0.0.0.0:%d (Basic Auth requis)", port)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
