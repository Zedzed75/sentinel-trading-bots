"""SENTINEL DASHBOARD v2 - Pilotage mobile de la flotte + meteo du marche.

FastAPI + Jinja2 + DaisyUI/HTMX (CDN) : fragment "live" rafraichi toutes
les 10 s. Fusionne a la volee macro_weather.json (bot 7), trades.csv
(bot 5), heartbeats/verrous, MT5 et psutil ; un fichier absent/corrompu
=> squelette gris, jamais de 500. Actions (Basic Auth + hx-confirm) :
PANIC (positions Sentinel fermees + verrou GLOBAL, deverrouillage humain)
et FORCE RUN bot 7. Basic Auth obligatoire ; HTTPS/VPN hors reseau local.

Usage : python sentinel_dashboard.py [port] [--mock]
(--mock : donnees fictives via mock_dashboard_data.py, sans MT5/VPS)."""

import csv
import json
import logging
import os
import secrets
import subprocess
import sys
from datetime import datetime, timezone

import psutil
import MetaTrader5 as mt5
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

# --- Configuration ---
_DIR = os.path.dirname(os.path.abspath(__file__))
BOTS_DIR = os.path.join(_DIR, "bots")
LOG_DIR = os.path.join(_DIR, "logs")
TRADES_CSV = os.path.join(LOG_DIR, "trades.csv")
CONFIG_FILE = os.path.join(_DIR, "dashboard_config.json")
TEMPLATES_DIR = os.path.join(_DIR, "templates")

MOCK = "--mock" in sys.argv or os.environ.get("SENTINEL_DASHBOARD_MOCK") == "1"
MARGIN_ALERT_LEVEL = 150.0    # alerte rouge sous ce niveau de marge (%)
DAILY_DD_LIMIT, DEVIATION = 0.04, 20  # seuil jauge bot 1 ; slippage points

# Magic -> strategie (copie volontaire, pas d'imports croises entre modules)
MAGIC_STRATEGY = {1001: "breakout", 2001: "breakout", 3001: "breakout",
                  1002: "reversion", 2002: "reversion", 3002: "reversion",
                  4001: "statarb", **{m: "trend" for m in range(5001, 5006)}}

# La flotte (id, script, nom, hb max aligne watchdog, strategies, state)
_F = ("id", "script", "nom", "hb_max", "strategies", "state")
FLEET = tuple(dict(zip(_F, b)) for b in (
    (1, "sentinel_bot.py", "Intraday multi-actifs", 300,
     ("breakout", "reversion"), "sentinel_state.json"),
    (2, "sentinel_alpha_compound.py", "Stat-arb Brent/WTI", 300,
     ("statarb",), "alpha_state.json"),
    (3, "sentinel_trend.py", "Trend-following H4", 300,
     ("trend",), "trend_state.json"),
    (4, "sentinel_risk_orchestrator.py", "Orchestrateur", 300,
     (), "orchestrator_state.json"),
    (5, "sentinel_trade_analytics.py", "Analytics", 2700, (), None),
    (6, "sentinel_telegram.py", "Telegram", 300, (), None),
    (7, "sentinel_macro_analyst.py", "Macro Analyst", 300, (), None),
))

log = logging.getLogger("dashboard")


# --- Lectures robustes : un fichier absent/corrompu ne casse jamais l'interface. ---
def load_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_json_atomic(path: str, payload: dict):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)


def read_trades(path: str = TRADES_CSV) -> list[dict]:
    """Journal du bot 5 ; lignes illisibles ignorees, [] si fichier KO."""
    rows = []
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                try:
                    rows.append({"pnl": float(r["pnl"]),
                                 "strategy": r["strategy"], "close_time":
                                 datetime.fromisoformat(r["close_time"])})
                except (ValueError, KeyError, TypeError):
                    continue
    except OSError:
        pass
    return rows


def day_stats(trades: list[dict], now: datetime) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for t in trades:
        if t["close_time"].date() == now.date():
            st = out.setdefault(t["strategy"], {"pnl": 0.0, "n": 0})
            st["pnl"] = round(st["pnl"] + t["pnl"], 2)
            st["n"] += 1
    return out


def heartbeat_age(script: str, now: datetime) -> float | None:
    path = os.path.join(LOG_DIR, script.replace(".py", ".hb"))
    try:
        with open(path, encoding="utf-8") as fh:
            hb = datetime.fromisoformat(fh.read().strip())
        return (now - hb).total_seconds()
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
    if not equity or not day_balance:
        return {"pct": None, "used": 0.0, "limit_pct": -DAILY_DD_LIMIT * 100}
    pct = (equity - day_balance) / day_balance * 100
    used = min(max(-pct / (DAILY_DD_LIMIT * 100), 0.0), 1.0)
    return {"pct": round(pct, 2), "used": round(used, 3),
            "limit_pct": -DAILY_DD_LIMIT * 100}


def read_weather(now: datetime | None = None) -> dict | None:
    """Meteo du bot 7 ; None => squelette gris dans l'interface."""
    w = load_json(os.path.join(BOTS_DIR, "macro_weather.json"))
    if not w.get("weather"):
        return None
    now = now or datetime.now(timezone.utc)
    w["stale"] = w.get("date") != now.date().isoformat()
    return w


def watchdog_alive() -> bool:
    try:
        return any("watchdog.ps1" in (a or "")
                   for p in psutil.process_iter(["name", "cmdline"])
                   if "powershell" in (p.info["name"] or "").lower()
                   for a in p.info["cmdline"] or [])
    except Exception:
        return False


def account_snapshot() -> dict:
    acc = mt5.account_info()
    if acc is None:
        mt5.initialize()
        acc = mt5.account_info()
    if acc is None:
        return dict.fromkeys(("balance", "equity", "margin_free",
                              "margin_level"), None) | {"ok": False,
                                                        "currency": ""}
    return {"ok": True, "balance": round(acc.balance, 2),
            "equity": round(acc.equity, 2),
            "margin_free": round(acc.margin_free, 2),
            "margin_level": (round(acc.margin_level, 1)
                             if acc.margin_level else None),
            "currency": acc.currency}


def open_positions() -> list[dict]:
    return [{"ticket": p.ticket, "symbol": p.symbol, "volume": p.volume,
             "sens": "LONG" if p.type == mt5.POSITION_TYPE_BUY else "SHORT",
             "pnl": round(p.profit, 2),
             "strategie": MAGIC_STRATEGY[p.magic]}
            for p in mt5.positions_get() or [] if p.magic in MAGIC_STRATEGY]


def build_state(now: datetime | None = None) -> dict:
    """Instantane complet servi a l'interface (ne leve jamais)."""
    if MOCK:
        import mock_dashboard_data
        return mock_dashboard_data.get_state()
    now = now or datetime.now(timezone.utc)
    per = day_stats(read_trades(), now)
    bots = [{"id": b["id"], "nom": b["nom"], "trade": bool(b["strategies"]),
             "statut": bot_status(
                 heartbeat_age(b["script"], now), b["hb_max"],
                 bool(b["state"] and load_json(os.path.join(
                     BOTS_DIR, b["state"])).get("locked"))),
             "pnl_jour": round(sum(per.get(s, {}).get("pnl", 0.0)
                                   for s in b["strategies"]), 2),
             "trades_jour": sum(per.get(s, {}).get("n", 0)
                                for s in b["strategies"])}
            for b in FLEET]
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
        "meteo": read_weather(now),
        "bots": bots,
        "jauge_jour": daily_gauge(acc["equity"], day_ref),
        "verrou_global": bool(load_json(os.path.join(
            BOTS_DIR, "orchestrator_state.json")).get("locked")),
        "risk_scale": load_json(os.path.join(
            BOTS_DIR, "risk_scale.json")).get("scale"),
        "positions": open_positions(),
        "systeme": {"cpu": cpu, "ram": ram, "watchdog": watchdog_alive()},
    }


# --- Actions d'urgence (Basic Auth + hx-confirm cote interface) ---
def close_all_positions() -> int:
    """Ferme toutes les positions des magics Sentinel par ordre inverse."""
    closed = 0
    for p in mt5.positions_get() or []:
        if p.magic not in MAGIC_STRATEGY:
            continue
        tick = mt5.symbol_info_tick(p.symbol)
        if tick is None:
            continue
        buy = p.type == mt5.POSITION_TYPE_SELL
        r = mt5.order_send({
            "action": mt5.TRADE_ACTION_DEAL, "symbol": p.symbol,
            "volume": p.volume, "position": p.ticket,
            "type": mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL,
            "price": tick.ask if buy else tick.bid, "deviation": DEVIATION,
            "magic": p.magic, "comment": "panic_dashboard",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC})
        closed += bool(r is not None
                       and r.retcode == mt5.TRADE_RETCODE_DONE)
    return closed


def engage_global_lock():
    """Verrou GLOBAL de l'orchestrateur + redemarrage (watchdog <30 s) :
    il recharge locked=true et purge en continu. Deverrouillage humain."""
    path = os.path.join(BOTS_DIR, "orchestrator_state.json")
    save_json_atomic(path, load_json(path) | {"locked": True})
    try:
        for p in psutil.process_iter(["name", "cmdline"]):
            if "python" in (p.info["name"] or "").lower() and any(
                    "sentinel_risk_orchestrator.py" in (a or "")
                    for a in (p.info["cmdline"] or [])):
                p.kill()
    except Exception as exc:
        log.warning("Orchestrateur non redemarre (%s) : le verrou fichier "
                    "prendra effet a son prochain (re)demarrage.", exc)


# --- Application FastAPI (Basic Auth sur tout) ---
app = FastAPI(title="Sentinel Dashboard", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=TEMPLATES_DIR)
_basic = HTTPBasic()


def _credentials() -> tuple[str, str]:
    cfg = load_json(CONFIG_FILE)
    return (cfg.get("user") or os.environ.get("DASHBOARD_USER") or "sentinel",
            cfg.get("password") or os.environ.get("DASHBOARD_PASSWORD") or "")


def require_auth(creds: HTTPBasicCredentials = Depends(_basic)) -> str:
    user, password = _credentials()
    if not password and not MOCK:      # jamais d'acces sans mot de passe
        raise HTTPException(503, "Mot de passe non configure.")
    if not MOCK and not (secrets.compare_digest(creds.username, user)
                         and secrets.compare_digest(creds.password, password)):
        raise HTTPException(401, "Identifiants invalides.",
                            headers={"WWW-Authenticate": "Basic"})
    return creds.username


@app.get("/")
def index(request: Request, _: str = Depends(require_auth)):
    return templates.TemplateResponse(request, "dashboard.html",
                                      {"state": build_state(), "mock": MOCK})


@app.get("/partial/live")
def partial_live(request: Request, _: str = Depends(require_auth)):
    return templates.TemplateResponse(request, "_live.html",
                                      {"state": build_state()})


@app.get("/api/state")
def api_state(_: str = Depends(require_auth)) -> dict:
    return build_state()


_MOCK_MSG = "<span class='text-warning'>mode mock : aucune action</span>"


@app.post("/api/panic", response_class=HTMLResponse)
def api_panic(_: str = Depends(require_auth)) -> str:
    if MOCK:
        return _MOCK_MSG
    n = close_all_positions()
    engage_global_lock()
    log.critical("PANIC dashboard : %d position(s) fermee(s), verrou pose.", n)
    return (f"<span class='text-error font-bold'>\U0001f6a8 PANIC : {n} "
            f"position(s) fermee(s), verrou GLOBAL pose — deverrouillage "
            f"manuel requis (orchestrator_state.json)</span>")


@app.post("/api/forcerun", response_class=HTMLResponse)
def api_forcerun(_: str = Depends(require_auth)) -> str:
    if MOCK:
        return _MOCK_MSG
    subprocess.Popen(
        [sys.executable, "-u", "sentinel_macro_analyst.py", "--once"],
        cwd=BOTS_DIR, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return ("<span class='text-info'>\U0001f504 Bot 7 lance : rapport "
            "meteo sur Telegram dans ~1 minute.</span>")


def main() -> int:
    import uvicorn
    logging.basicConfig(level=logging.INFO, datefmt="%H:%M:%S",
                        format="%(asctime)s [%(levelname)s] %(message)s")
    if MOCK:
        log.info("MODE MOCK : donnees fictives, actions desactivees.")
    elif not _credentials()[1]:
        log.error("Aucun mot de passe : copier dashboard_config.example.json "
                  "vers dashboard_config.json et definir 'password'.")
        return 1
    elif not mt5.initialize(
            path="C:/Program Files/Pepperstone MetaTrader 5/terminal64.exe"):
        log.warning("MT5 indisponible au demarrage (%s).", mt5.last_error())
    port = next((int(a) for a in sys.argv[1:] if a.isdigit()), 8787)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
