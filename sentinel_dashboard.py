"""SENTINEL DASHBOARD v2 - Mobile fleet control + market weather.

FastAPI + Jinja2 + DaisyUI/HTMX (CDN): "live" fragment refreshed every
10 s. Merges on the fly macro_weather.json (bot 7), trades.csv (bot 5),
heartbeats/locks, MT5 and psutil; a missing/corrupt file => grey
skeleton, never a 500. Actions (Basic Auth + hx-confirm): PANIC
(Sentinel positions closed + GLOBAL lock, human unlock) and FORCE RUN
bot 7. Basic Auth mandatory; HTTPS/VPN outside the local network.

Usage: python sentinel_dashboard.py [port] [--mock]
(--mock: fake data via mock_dashboard_data.py, without MT5/VPS)."""

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
MARGIN_ALERT_LEVEL = 150.0    # red alert below this margin level (%)
DAILY_DD_LIMIT, DEVIATION = 0.04, 20  # bot 1 gauge threshold; slippage points

# Magic -> strategy (deliberate copy, no cross-imports between modules)
MAGIC_STRATEGY = {1001: "breakout", 2001: "breakout", 3001: "breakout",
                  1002: "reversion", 2002: "reversion", 3002: "reversion",
                  4001: "statarb", **{m: "trend" for m in range(5001, 5006)}}

# The fleet (id, script, name, max hb aligned with the watchdog, strategies, state)
_F = ("id", "script", "name", "hb_max", "strategies", "state")
FLEET = tuple(dict(zip(_F, b)) for b in (
    (1, "sentinel_bot.py", "Intraday multi-asset", 300,
     ("breakout", "reversion"), "sentinel_state.json"),
    (2, "sentinel_alpha_compound.py", "Stat-arb Brent/WTI", 300,
     ("statarb",), "alpha_state.json"),
    (3, "sentinel_trend.py", "Trend-following H4", 300,
     ("trend",), "trend_state.json"),
    (4, "sentinel_risk_orchestrator.py", "Orchestrator", 300,
     (), "orchestrator_state.json"),
    (5, "sentinel_trade_analytics.py", "Analytics", 2700, (), None),
    (6, "sentinel_telegram.py", "Telegram", 300, (), None),
    (7, "sentinel_macro_analyst.py", "Macro Analyst", 300, (), None),
))

log = logging.getLogger("dashboard")


# --- Robust reads: a missing/corrupt file never breaks the interface. ---
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
    """Bot 5's journal; unreadable lines ignored, [] if the file is KO."""
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
    """suspended (lock) > active (fresh hb) > frozen (old hb) > stopped."""
    if locked:
        return "suspended"
    if hb_age is None:
        return "stopped"
    return "active" if hb_age <= hb_max else "frozen"


def daily_gauge(equity: float | None, day_balance: float | None) -> dict:
    if not equity or not day_balance:
        return {"pct": None, "used": 0.0, "limit_pct": -DAILY_DD_LIMIT * 100}
    pct = (equity - day_balance) / day_balance * 100
    used = min(max(-pct / (DAILY_DD_LIMIT * 100), 0.0), 1.0)
    return {"pct": round(pct, 2), "used": round(used, 3),
            "limit_pct": -DAILY_DD_LIMIT * 100}


# Legacy French values/keys of macro_weather.json written before the
# English migration (bot 7 rewrites the file in English on its next run).
_LEGACY_WEATHER = {"ORAGEUX": "STORMY", "CALME": "CALM", "NEUTRE": "NEUTRAL"}
_LEGACY_KEYS = {"geo_resume": "geo_summary", "macro_resume": "macro_summary",
                "sentiment_resume": "sentiment_summary",
                "banks_resume": "banks_summary"}


def read_weather(now: datetime | None = None) -> dict | None:
    """Bot 7's weather; None => grey skeleton in the interface."""
    w = load_json(os.path.join(BOTS_DIR, "macro_weather.json"))
    if not w.get("weather"):
        return None
    w["weather"] = _LEGACY_WEATHER.get(w["weather"], w["weather"])
    for old, new in _LEGACY_KEYS.items():
        if old in w and new not in w:
            w[new] = w.pop(old)
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
             "side": "LONG" if p.type == mt5.POSITION_TYPE_BUY else "SHORT",
             "pnl": round(p.profit, 2),
             "strategy": MAGIC_STRATEGY[p.magic]}
            for p in mt5.positions_get() or [] if p.magic in MAGIC_STRATEGY]


def build_state(now: datetime | None = None) -> dict:
    """Full snapshot served to the interface (never raises)."""
    if MOCK:
        import mock_dashboard_data
        return mock_dashboard_data.get_state()
    now = now or datetime.now(timezone.utc)
    per = day_stats(read_trades(), now)
    bots = [{"id": b["id"], "name": b["name"], "trade": bool(b["strategies"]),
             "status": bot_status(
                 heartbeat_age(b["script"], now), b["hb_max"],
                 bool(b["state"] and load_json(os.path.join(
                     BOTS_DIR, b["state"])).get("locked"))),
             "day_pnl": round(sum(per.get(s, {}).get("pnl", 0.0)
                                  for s in b["strategies"]), 2),
             "day_trades": sum(per.get(s, {}).get("n", 0)
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
        "time": now.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "account": acc,
        "margin_alert": bool(acc["margin_level"] is not None
                             and acc["margin_level"] < MARGIN_ALERT_LEVEL),
        "weather": read_weather(now),
        "bots": bots,
        "daily_gauge": daily_gauge(acc["equity"], day_ref),
        "global_lock": bool(load_json(os.path.join(
            BOTS_DIR, "orchestrator_state.json")).get("locked")),
        "risk_scale": load_json(os.path.join(
            BOTS_DIR, "risk_scale.json")).get("scale"),
        "positions": open_positions(),
        "system": {"cpu": cpu, "ram": ram, "watchdog": watchdog_alive()},
    }


# --- Emergency actions (Basic Auth + hx-confirm on the interface side) ---
def close_all_positions() -> int:
    """Close all positions of the Sentinel magics via opposite orders."""
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
    """Orchestrator GLOBAL lock + restart (watchdog <30 s): it reloads
    locked=true and keeps purging. Human unlock."""
    path = os.path.join(BOTS_DIR, "orchestrator_state.json")
    save_json_atomic(path, load_json(path) | {"locked": True})
    try:
        for p in psutil.process_iter(["name", "cmdline"]):
            if "python" in (p.info["name"] or "").lower() and any(
                    "sentinel_risk_orchestrator.py" in (a or "")
                    for a in (p.info["cmdline"] or [])):
                p.kill()
    except Exception as exc:
        log.warning("Orchestrator not restarted (%s): the file lock will "
                    "take effect on its next (re)start.", exc)


# --- FastAPI application (Basic Auth on everything) ---
app = FastAPI(title="Sentinel Dashboard", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=TEMPLATES_DIR)
_basic = HTTPBasic()


def _credentials() -> tuple[str, str]:
    cfg = load_json(CONFIG_FILE)
    return (cfg.get("user") or os.environ.get("DASHBOARD_USER") or "sentinel",
            cfg.get("password") or os.environ.get("DASHBOARD_PASSWORD") or "")


def require_auth(creds: HTTPBasicCredentials = Depends(_basic)) -> str:
    user, password = _credentials()
    if not password and not MOCK:      # never any access without a password
        raise HTTPException(503, "Password not configured.")
    if not MOCK and not (secrets.compare_digest(creds.username, user)
                         and secrets.compare_digest(creds.password, password)):
        raise HTTPException(401, "Invalid credentials.",
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


_MOCK_MSG = "<span class='text-warning'>mock mode: no action</span>"


@app.post("/api/panic", response_class=HTMLResponse)
def api_panic(_: str = Depends(require_auth)) -> str:
    if MOCK:
        return _MOCK_MSG
    n = close_all_positions()
    engage_global_lock()
    log.critical("Dashboard PANIC: %d position(s) closed, lock engaged.", n)
    return (f"<span class='text-error font-bold'>\U0001f6a8 PANIC: {n} "
            f"position(s) closed, GLOBAL lock engaged — manual unlock "
            f"required (orchestrator_state.json)</span>")


@app.post("/api/forcerun", response_class=HTMLResponse)
def api_forcerun(_: str = Depends(require_auth)) -> str:
    if MOCK:
        return _MOCK_MSG
    subprocess.Popen(
        [sys.executable, "-u", "sentinel_macro_analyst.py", "--once"],
        cwd=BOTS_DIR, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return ("<span class='text-info'>\U0001f504 Bot 7 launched: weather "
            "report on Telegram in ~1 minute.</span>")


def main() -> int:
    import uvicorn
    logging.basicConfig(level=logging.INFO, datefmt="%H:%M:%S",
                        format="%(asctime)s [%(levelname)s] %(message)s")
    if MOCK:
        log.info("MOCK MODE: fake data, actions disabled.")
    elif not _credentials()[1]:
        log.error("No password: copy dashboard_config.example.json to "
                  "dashboard_config.json and set 'password'.")
        return 1
    elif not mt5.initialize(
            path="C:/Program Files/Pepperstone MetaTrader 5/terminal64.exe"):
        log.warning("MT5 unavailable at startup (%s).", mt5.last_error())
    port = next((int(a) for a in sys.argv[1:] if a.isdigit()), 8787)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
