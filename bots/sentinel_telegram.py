"""SENTINEL TELEGRAM - Fleet and trade monitoring on mobile.

Does not trade. Every POLL_SECONDS:

1. automatic push: position opened, closed (with the deal's PnL),
   circuit-breaker lock activation, daily report at DAILY_REPORT_HOUR
   UTC (profit/loss day/7d/30d/total + equity);
2. incoming commands: /status (equity, positions, locks, processes),
   /pnl (total profit/loss per window and per strategy).

Configuration: bots/telegram_config.json -> {"token": "..."} (gitignored,
see telegram_config.example.json; token created via @BotFather).
The chat_id is captured on the first message received by the bot (/start)
and persisted in telegram_state.json: sending the bot a message is enough.
Only that chat is listened to afterwards. API https://api.telegram.org
(HTTPS).
"""

import csv
import json
import logging
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone

import requests
import MetaTrader5 as mt5

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
# Magic -> strategy (deliberate copy: no cross-imports between bots)
MAGIC_STRATEGY = {
    1001: "breakout", 2001: "breakout", 3001: "breakout",
    1002: "reversion", 2002: "reversion", 3002: "reversion",
    4001: "statarb",
    5001: "trend", 5002: "trend", 5003: "trend", 5004: "trend",
    5005: "trend",
}
POLL_SECONDS = 30
DAILY_REPORT_HOUR = 18        # daily report after bot 1's window

# Strategy/symbol pairs suspended or at reduced risk (research decisions
# applied in the configs of bots 1 and 3, see
# docs/AMELIORATION_CONTINUE.md section 5). Deliberate copy, like
# MAGIC_STRATEGY: keep up to date with each decision. The daily report
# recalls the real trades accumulated since then and the quarterly
# review - the review loop no longer relies on memory.
# aliases: possible broker names of the symbol in trades.csv.
SUSPENSIONS = (
    {"strategy": "breakout", "symbol": "EURUSD", "action": "suspended",
     "since": "2026-07-15", "aliases": ("EURUSD",)},
    {"strategy": "breakout", "symbol": "GBPUSD", "action": "suspended",
     "since": "2026-07-15", "aliases": ("GBPUSD",)},
    {"strategy": "trend", "symbol": "EURUSD", "action": "risk /2",
     "since": "2026-07-15", "aliases": ("EURUSD",)},
    {"strategy": "trend", "symbol": "GBPUSD", "action": "risk /2",
     "since": "2026-07-15", "aliases": ("GBPUSD",)},
    {"strategy": "trend", "symbol": "XTIUSD", "action": "risk /2",
     "since": "2026-07-15", "aliases": ("XTIUSD", "SpotCrude", "USOIL")},
)
REVIEW_AFTER_DAYS = 91        # quarterly review
REVIEW_MIN_TRADES = 30        # real-journal threshold (AMELIORATION section 4)

# OPENING windows per strategy (real UTC) - deliberate copy of the
# configs of bots 1-3, like MAGIC_STRATEGY. start > end = window that
# wraps around midnight (trend: only the 21-23h rollover blackout closes
# openings). Exits and circuit breakers are never blocked.
ENTRY_WINDOWS = (
    {"strategy": "breakout (bot 1)", "start": 8, "end": 16,
     "note": "XAUUSD only, EURUSD/GBPUSD suspended"},
    {"strategy": "reversion (bot 1)", "start": 13, "end": 18},
    {"strategy": "statarb (bot 2)", "start": 7, "end": 20},
    {"strategy": "trend (bot 3)", "start": 23, "end": 21},
)

_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(os.path.dirname(_DIR), "logs")
TRADES_CSV = os.path.join(LOG_DIR, "trades.csv")
CONFIG_FILE = os.path.join(_DIR, "telegram_config.json")
STATE_FILE = os.path.join(_DIR, "telegram_state.json")
RISK_SCALE_FILE = os.path.join(_DIR, "risk_scale.json")

# state file -> label of the monitored lock (with release schedule)
LOCK_SOURCES = {
    "sentinel_state.json": "bot 1 (daily lock -4%, lifted at 00:00 UTC)",
    "alpha_state.json": "bot 2 (permanent lock -15% from peak, "
                        "human review required)",
    "trend_state.json": "bot 3 (permanent lock -15% from peak, "
                        "human review required)",
    "orchestrator_state.json": "GLOBAL permanent lock -10% "
                               "(whole fleet, human review required)",
}
FLEET_BOTS = ("sentinel_risk_orchestrator.py", "sentinel_bot.py",
              "sentinel_alpha_compound.py", "sentinel_trend.py",
              "sentinel_trade_analytics.py", "sentinel_macro_analyst.py")

API_URL = "https://api.telegram.org/bot{token}/{method}"

log = logging.getLogger("telegram")


# ----------------------------------------------------------------------------
# Data reading (pure, testable functions)
# ----------------------------------------------------------------------------
HEARTBEAT_FILE = os.path.join(LOG_DIR, "sentinel_telegram.hb")


def write_heartbeat(path: str = HEARTBEAT_FILE,
                    now: datetime | None = None):
    """Liveness timestamp after each successful cycle (read by the watchdog)."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write((now or datetime.now(timezone.utc)).isoformat())
    except OSError:
        pass


def load_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def read_trades(path: str = TRADES_CSV) -> list[dict]:
    """Journal written by sentinel_trade_analytics (file-based coordination,
    like risk_scale.json). List of {pnl, strategy, close_time}."""
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            return [{"pnl": float(r["pnl"]), "strategy": r["strategy"],
                     "symbol": r.get("symbol", ""),
                     "close_time": datetime.fromisoformat(r["close_time"])}
                    for r in csv.DictReader(fh)]
    except (OSError, ValueError, KeyError):
        return []


def pnl_summary(rows: list[dict], now: datetime) -> dict:
    """Net totals day / 7d / 30d / all-time + breakdown per strategy."""
    def total(days=None):
        if days is None:
            sel = rows
        else:
            lim = now - timedelta(days=days)
            sel = [r for r in rows if r["close_time"] >= lim]
        return round(sum(r["pnl"] for r in sel), 2)

    today = [r for r in rows if r["close_time"].date() == now.date()]
    by_strategy: dict[str, dict] = {}
    for r in rows:
        st = by_strategy.setdefault(r["strategy"], {"pnl": 0.0, "count": 0})
        st["pnl"] = round(st["pnl"] + r["pnl"], 2)
        st["count"] += 1
    return {"day": round(sum(r["pnl"] for r in today), 2),
            "d7": total(7), "d30": total(30), "total": total(),
            "count": len(rows), "by_strategy": by_strategy}


def fmt_eur(v: float) -> str:
    return f"{v:+.2f} EUR"


def _badge(v: float) -> str:
    return "✅" if v >= 0 else "\U0001f53b"   # green check / red triangle


def format_pnl_message(s: dict) -> str:
    lines = ["\U0001f4b0 Profit/Loss (net of fees)",
             f"Today: {_badge(s['day'])} {fmt_eur(s['day'])}",
             f"7 days: {_badge(s['d7'])} {fmt_eur(s['d7'])}",
             f"30 days: {_badge(s['d30'])} {fmt_eur(s['d30'])}",
             f"Total ({s['count']} trades): "
             f"{_badge(s['total'])} {fmt_eur(s['total'])}"]
    if s["by_strategy"]:
        lines.append("")
        lines.append("By strategy:")
        for name, st in sorted(s["by_strategy"].items()):
            lines.append(f"- {name}: {fmt_eur(st['pnl'])} "
                         f"({st['count']} trades)")
    return "\n".join(lines)


def suspension_lines(rows: list[dict], now: datetime) -> list[str]:
    """Reminder of suspended/reduced pairs: real trades accumulated since
    the decision and quarterly review date (⚠️ if overdue)."""
    lines = []
    for s in SUSPENSIONS:
        since = datetime.fromisoformat(s["since"]).replace(tzinfo=timezone.utc)
        due = since + timedelta(days=REVIEW_AFTER_DAYS)
        aliases = tuple(a.upper() for a in s["aliases"])
        n = sum(1 for r in rows
                if r["strategy"] == s["strategy"]
                and r["close_time"] >= since
                and r.get("symbol", "").upper().startswith(aliases))
        due_txt = (f"review OVERDUE ({due:%Y-%m-%d}) ⚠️" if now >= due
                   else f"review on {due:%Y-%m-%d}")
        lines.append(f"- {s['strategy']} {s['symbol']}: {s['action']} "
                     f"since {s['since']}, {n} trades since "
                     f"(threshold {REVIEW_MIN_TRADES}), {due_txt}")
    return lines


def entry_status_lines(now: datetime) -> list[str]:
    """For /status: can each strategy OPEN a trade right now?
    Window open -> closing hour; closed -> next opening.
    (Exits and circuit breakers do not depend on any window.)"""
    lines = []
    for w in ENTRY_WINDOWS:
        s, e, h = w["start"], w["end"], now.hour
        is_open = (s <= h < e) if s < e else (h >= s or h < e)
        state = (f"can trade until {e:02d}:00" if is_open
                 else f"⏳ window closed, opens at {s:02d}:00")
        note = f" [{w['note']}]" if w.get("note") else ""
        lines.append(f"- {w['strategy']}: {state}{note}")
    return lines


def active_locks(dir_path: str = _DIR) -> list[str]:
    """Labels of the circuit breakers currently locked."""
    return [label for name, label in LOCK_SOURCES.items()
            if load_json(os.path.join(dir_path, name)).get("locked")]


def new_closing_deals(deals, since: float) -> list:
    """Exit deals of the Sentinel magics later than `since`."""
    return [d for d in deals or []
            if d.entry == mt5.DEAL_ENTRY_OUT
            and getattr(d, "magic", None) in MAGIC_STRATEGY
            and d.time > since]


def should_send_daily(last_report_day: str | None, now: datetime) -> bool:
    return (now.hour >= DAILY_REPORT_HOUR
            and last_report_day != now.date().isoformat())


def bots_processes() -> dict[str, bool]:
    """{script: process alive} via a CIM query (Windows)."""
    cmd = ("Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" "
           "| Select-Object -ExpandProperty CommandLine")
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True, text=True, timeout=25).stdout or ""
    except Exception as exc:                      # pragma: no cover
        log.warning("Process check impossible: %s", exc)
        out = ""
    return {b: (b in out) for b in FLEET_BOTS}


def sentinel_positions() -> list:
    return [p for p in (mt5.positions_get() or [])
            if p.magic in MAGIC_STRATEGY]


def status_text(now: datetime) -> str:
    acc = mt5.account_info()
    lines = ["\U0001f916 Sentinel fleet - "
             + now.strftime("%Y-%m-%d %H:%M UTC")]
    if acc is not None:
        lines.append(f"Equity: {acc.equity:.2f} {acc.currency} "
                     f"(balance {acc.balance:.2f})")
    scale = load_json(RISK_SCALE_FILE).get("scale")
    if scale is not None:
        lines.append(f"Risk scale: {scale}")
    pos = sentinel_positions()
    lines.append(f"Open positions ({len(pos)}):")
    for p in pos:
        side = "LONG" if p.type == mt5.POSITION_TYPE_BUY else "SHORT"
        strat = MAGIC_STRATEGY.get(p.magic, "?")
        lines.append(f"- {side} {p.symbol} {p.volume} lot ({strat}) "
                     f"PnL {fmt_eur(p.profit)}")
    locks = active_locks()
    lines.append("Locks: " + ("none"
                 if not locks else "\U0001f512 " + " ; ".join(locks)))
    lines.append("Entry windows (UTC):")
    lines += entry_status_lines(now)
    lines.append("Processes:")
    for name, alive in bots_processes().items():
        lines.append(f"- {name}: {'OK' if alive else 'STOPPED!'}")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Notifier: persistent state + Telegram API
# ----------------------------------------------------------------------------
class TelegramNotifier:
    def __init__(self, token: str, state_file: str = STATE_FILE):
        self.token = token
        self.state_file = state_file
        st = load_json(state_file)
        self.chat_id = st.get("chat_id")
        self.last_update_id = st.get("last_update_id", 0)
        self.last_deal_ts = st.get("last_deal_ts", 0)
        self.last_report_day = st.get("last_report_day")
        self.open_tickets = st.get("open_tickets", [])
        self.known_locks = st.get("known_locks", [])

    def _save(self):
        try:
            tmp = self.state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"chat_id": self.chat_id,
                           "last_update_id": self.last_update_id,
                           "last_deal_ts": self.last_deal_ts,
                           "last_report_day": self.last_report_day,
                           "open_tickets": self.open_tickets,
                           "known_locks": self.known_locks}, fh)
            os.replace(tmp, self.state_file)
        except OSError as exc:
            log.warning("State save failed: %s", exc)

    def api(self, method: str, **params) -> dict:
        try:
            resp = requests.post(API_URL.format(token=self.token,
                                                method=method),
                                 json=params, timeout=15)
            return resp.json()
        except Exception as exc:
            log.warning("Telegram API %s KO: %s", method, exc)
            return {}

    def send(self, text: str):
        if self.chat_id:
            self.api("sendMessage", chat_id=self.chat_id, text=text)

    # --- incoming commands ----------------------------------------------------
    def poll_commands(self, now: datetime | None = None):
        now = now or datetime.now(timezone.utc)
        resp = self.api("getUpdates", offset=self.last_update_id + 1,
                        timeout=0)
        for upd in (resp or {}).get("result", []):
            self.last_update_id = max(self.last_update_id,
                                      upd.get("update_id", 0))
            msg = upd.get("message") or {}
            chat = (msg.get("chat") or {}).get("id")
            text = (msg.get("text") or "").strip().lower()
            if chat and self.chat_id is None:      # first contact = master
                self.chat_id = chat
                self._save()
                log.info("Master chat registered: %s", chat)
                self.send("Sentinel connected ✅\n"
                          "Commands: /status /pnl")
                continue
            if chat != self.chat_id:               # only one chat listened to
                continue
            self._handle(text, now)
        self._save()

    def _handle(self, text: str, now: datetime):
        if text.startswith("/status"):
            self.send(status_text(now))
        elif text.startswith("/pnl"):
            self.send(format_pnl_message(pnl_summary(read_trades(), now)))
        elif text.startswith("/start") or text.startswith("/help"):
            self.send("Commands: /status (fleet, positions, locks) "
                      "and /pnl (profit/loss)")


# ----------------------------------------------------------------------------
# Automatically pushed events
# ----------------------------------------------------------------------------
def check_closed_deals(notif: TelegramNotifier, now: datetime):
    """Notify each exit deal (net deal PnL) since the last one seen."""
    if not notif.last_deal_ts:                    # first cycle: reference
        notif.last_deal_ts = int(now.timestamp())
        return
    deals = mt5.history_deals_get(
        datetime.fromtimestamp(notif.last_deal_ts, tz=timezone.utc)
        - timedelta(days=1), now + timedelta(days=1))
    for d in new_closing_deals(deals, notif.last_deal_ts):
        pnl = d.profit + d.commission + d.swap
        strat = MAGIC_STRATEGY[d.magic]
        notif.send(f"{_badge(pnl)} Closed {d.symbol} ({strat}): "
                   f"{fmt_eur(pnl)}")
        notif.last_deal_ts = max(notif.last_deal_ts, int(d.time))
    notif._save()


def check_position_events(notif: TelegramNotifier):
    """Notify newly opened positions (Sentinel magics)."""
    current = {p.ticket: p for p in sentinel_positions()}
    for ticket, p in current.items():
        if ticket not in notif.open_tickets:
            side = "LONG" if p.type == mt5.POSITION_TYPE_BUY else "SHORT"
            strat = MAGIC_STRATEGY.get(p.magic, "?")
            notif.send(f"\U0001f4c8 Opened {side} {p.symbol} "
                       f"{p.volume} lot ({strat})")
    notif.open_tickets = list(current)
    notif._save()


def check_locks(notif: TelegramNotifier):
    """Alert when a circuit breaker activates (only once)."""
    locks = active_locks()
    for label in locks:
        if label not in notif.known_locks:
            notif.send(f"\U0001f6a8 CIRCUIT BREAKER ACTIVE: {label}")
    notif.known_locks = locks
    notif._save()


def maybe_daily_report(notif: TelegramNotifier, now: datetime):
    if not should_send_daily(notif.last_report_day, now):
        return
    acc = mt5.account_info()
    rows = read_trades()
    msg = format_pnl_message(pnl_summary(rows, now))
    if acc is not None:
        msg += f"\n\nEquity: {acc.equity:.2f} {acc.currency}"
    susp = suspension_lines(rows, now)
    if susp:
        msg += ("\n\n⏳ Pairs under watch:\n"
                + "\n".join(susp))
    notif.send("\U0001f4c5 Daily report\n" + msg)
    notif.last_report_day = now.date().isoformat()
    notif._save()


def run_cycle(notif: TelegramNotifier, now: datetime | None = None):
    now = now or datetime.now(timezone.utc)
    if mt5.account_info() is None:
        raise ConnectionError(f"account_info() KO: {mt5.last_error()}")
    notif.poll_commands(now)
    check_position_events(notif)
    check_closed_deals(notif, now)
    check_locks(notif)
    maybe_daily_report(notif, now)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    def _token():
        return (load_json(CONFIG_FILE).get("token")
                or os.environ.get("TELEGRAM_BOT_TOKEN"))

    token = _token()
    if not token:
        log.warning("No token: create bots/telegram_config.json (see "
                    "telegram_config.example.json, token via @BotFather). "
                    "Waiting...")
    while not token:                  # passive wait, watchdog-friendly
        time.sleep(60)
        token = _token()
    if not mt5.initialize(
            path="C:/Program Files/Pepperstone MetaTrader 5/terminal64.exe"):
        log.error("mt5.initialize() failed: %s", mt5.last_error())
        return 1
    notif = TelegramNotifier(token)
    log.info("Starting SENTINEL TELEGRAM (chat_id %s)",
             notif.chat_id or "waiting for the first message")
    while True:
        try:
            run_cycle(notif)
            write_heartbeat()
        except ConnectionError as exc:
            log.error("Connection lost: %s - reconnecting...", exc)
            mt5.shutdown()
            time.sleep(5)
            mt5.initialize()
        except Exception as exc:
            log.exception("Unexpected error: %s", exc)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
