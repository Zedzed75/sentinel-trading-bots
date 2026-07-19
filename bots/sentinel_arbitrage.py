"""SENTINEL ARBITRAGE (bot 8) - Technical vs semantic daily arbitration.

Does not trade and never touches MT5. Every day at 22:00 UTC:
- takes the day's snapshot of bot 7's 08:30 UTC weather report
  (bots/macro_weather.json);
- takes the day's closed trades from bot 5's journal (logs/trades.csv);
- writes one row per closed trade into the `arbitrage_logs` SQLite table
  (bots/arbitrage.db), deciding who was right when the technical
  execution diverged from the macro view;
- publishes bots/arbitrage_summary.json (the four quant KPIs, read by
  the dashboard) and logs/arbitrage_export.csv (Excel-friendly weekly
  export, UTF-8 BOM).

Alignment semantics: bot 7 does not publish a BUY/SELL bias but a
volatility regime - STORMY favours the directional strategies
(breakout, trend), CALM favours the mean-reverting ones (reversion,
statarb), NEUTRAL favours no one (every trade counts as aligned). A
trade is aligned when its strategy belongs to the day's favoured set.

Clean dataset goal: one row per trade plus a "no signal" row on days
without any trade, so future ML models see the full calendar.

Usage: python bots/sentinel_arbitrage.py [--once]  (--once: run the
daily arbitration immediately, then exit).
"""

import csv
import io
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import date, datetime, timezone

from sentinel_quant_metrics import compute_all

# --- Configuration ---
RUN_HOUR = 22                 # 22:00 UTC: end-of-day arbitration
POLL_SECONDS = 30

_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(os.path.dirname(_DIR), "logs")
DB_FILE = os.path.join(_DIR, "arbitrage.db")
STATE_FILE = os.path.join(_DIR, "arbitrage_state.json")
WEATHER_FILE = os.path.join(_DIR, "macro_weather.json")
HISTORY_FILE = os.path.join(_DIR, "macro_history.json")   # bot 7 archive
SENTINEL_STATE = os.path.join(_DIR, "sentinel_state.json")
SUMMARY_FILE = os.path.join(_DIR, "arbitrage_summary.json")
TRADES_CSV = os.path.join(LOG_DIR, "trades.csv")
EXPORT_CSV = os.path.join(LOG_DIR, "arbitrage_export.csv")
HEARTBEAT_FILE = os.path.join(LOG_DIR, "sentinel_arbitrage.hb")

# Weather regime -> strategies it favours (see module docstring)
WEATHER_FAVOURS = {
    "STORMY": ("breakout", "trend"),
    "CALM": ("reversion", "statarb"),
    "NEUTRAL": ("breakout", "trend", "reversion", "statarb"),
}

WINNER_ALIGNED = "ALIGNED."
WINNER_MT5 = "MT5 bots (technical) were right despite the macro alert."
WINNER_BOT7 = "Bot 7 (macro) was right. The semantic filter saw it coming."
WINNER_FLAT = "Divergence with flat PnL: no winner."
WINNER_NO_VIEW = "No macro view available for the day."

EXPORT_HEADERS = ("Date (UTC)", "Asset", "Direction", "MT5 action",
                  "Bot 7 view", "Aligned", "PnL (EUR)", "Arbitration")

log = logging.getLogger("arbitrage")


# --- SQLite migration and access ---------------------------------------------
def init_db(path: str = DB_FILE) -> sqlite3.Connection:
    """Idempotent migration: creates arbitrage_logs if missing."""
    con = sqlite3.connect(path)
    con.execute("""
        CREATE TABLE IF NOT EXISTS arbitrage_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date_utc TIMESTAMP NOT NULL,
            asset VARCHAR NOT NULL,
            direction VARCHAR NOT NULL,
            mt5_action VARCHAR NOT NULL,
            bot7_view VARCHAR NOT NULL,
            is_aligned BOOLEAN,
            pnl FLOAT NOT NULL,
            winner_arbitrage VARCHAR NOT NULL
        )""")
    con.commit()
    return con


# --- Pure helpers (testable) --------------------------------------------------
def load_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_json_atomic(path: str, payload: dict):
    """Temp file + os.replace: the previous state survives a crash."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    os.replace(tmp, path)


def write_heartbeat(path: str = HEARTBEAT_FILE,
                    now: datetime | None = None):
    """Liveness timestamp after each successful cycle (read by the watchdog)."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write((now or datetime.now(timezone.utc)).isoformat())
    except OSError:
        pass


def read_trades(path: str = TRADES_CSV) -> list[dict]:
    """Bot 5's journal; unreadable lines ignored, [] if the file is KO."""
    rows = []
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                try:
                    rows.append({
                        "pnl": float(r["pnl"]), "strategy": r["strategy"],
                        "symbol": r.get("symbol", "?"),
                        "direction": (r.get("direction") or "?").upper(),
                        "close_time":
                            datetime.fromisoformat(r["close_time"])})
                except (ValueError, KeyError, TypeError):
                    continue
    except OSError:
        pass
    return rows


def day_weather(weather: dict, day: date) -> dict | None:
    """Bot 7's snapshot only if it is the requested day's report."""
    if weather.get("weather") and weather.get("date") == day.isoformat():
        return weather
    return None


def bot7_view_text(weather: dict | None) -> str:
    if weather is None:
        return "unavailable"
    return f"{weather['weather']} ({weather.get('focus', '')})"[:200]


def arbitrate(trade: dict, weather: dict | None) -> dict:
    """One arbitrage_logs row for one closed trade (pure logic).

    is_aligned: strategy favoured by the day's regime; None without a
    macro view (BOOLEAN column stays NULL, the dataset keeps the row).
    """
    if weather is None:
        aligned = None
        winner = WINNER_NO_VIEW
    else:
        aligned = trade["strategy"] in WEATHER_FAVOURS[weather["weather"]]
        if aligned:
            winner = WINNER_ALIGNED
        elif trade["pnl"] > 0:
            winner = WINNER_MT5
        elif trade["pnl"] < 0:
            winner = WINNER_BOT7
        else:
            winner = WINNER_FLAT
    side = "Long" if trade["direction"] == "LONG" else \
        "Short" if trade["direction"] == "SHORT" else trade["direction"]
    return {
        "date_utc": trade["close_time"].isoformat(),
        "asset": trade["symbol"],
        "direction": trade["direction"],
        "mt5_action": f"{side} execution ({trade['strategy']})",
        "bot7_view": bot7_view_text(weather),
        "is_aligned": aligned,
        "pnl": round(trade["pnl"], 2),
        "winner_arbitrage": winner,
    }


def no_signal_row(day: date, weather: dict | None) -> dict:
    """Calendar continuity for the ML dataset on days without a trade."""
    return {"date_utc": datetime(day.year, day.month, day.day, RUN_HOUR,
                                 tzinfo=timezone.utc).isoformat(),
            "asset": "-", "direction": "-", "mt5_action": "No signal",
            "bot7_view": bot7_view_text(weather), "is_aligned": None,
            "pnl": 0.0, "winner_arbitrage": WINNER_NO_VIEW if weather is None
            else "No trade to arbitrate."}


def load_history(path: str | None = None) -> list[dict]:
    """Bot 7's daily archive (list of dated reports); [] if KO."""
    try:
        with open(path or HISTORY_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def weather_from_history(history: list[dict], day: date) -> dict | None:
    """The archived bot 7 report of that exact day, if any."""
    for entry in history:
        if isinstance(entry, dict) and day_weather(entry, day):
            return entry
    return None


def should_run(state: dict, now: datetime) -> bool:
    """One arbitration per day, from 22:00 UTC."""
    return (now.hour >= RUN_HOUR
            and state.get("last_run_day") != now.date().isoformat())


# --- Persistence of the daily batch -------------------------------------------
def insert_rows(con: sqlite3.Connection, rows: list[dict]):
    con.executemany(
        "INSERT INTO arbitrage_logs (date_utc, asset, direction, mt5_action,"
        " bot7_view, is_aligned, pnl, winner_arbitrage)"
        " VALUES (:date_utc, :asset, :direction, :mt5_action, :bot7_view,"
        " :is_aligned, :pnl, :winner_arbitrage)", rows)
    con.commit()


def delete_day(con: sqlite3.Connection, day: date):
    """Upsert of the day (a forced rerun replaces, never duplicates)."""
    con.execute("DELETE FROM arbitrage_logs WHERE date_utc LIKE ?",
                (day.isoformat() + "%",))
    con.commit()


def all_pnl_rows(con: sqlite3.Connection) -> list[tuple[date, float]]:
    """(day, pnl) of real trades only (no-signal rows excluded)."""
    cur = con.execute("SELECT date_utc, pnl FROM arbitrage_logs"
                      " WHERE asset != '-' ORDER BY date_utc")
    return [(datetime.fromisoformat(d).date(), p) for d, p in cur.fetchall()]


def write_summary(con: sqlite3.Connection, now: datetime,
                  capital_base: float | None = None,
                  path: str | None = None):
    """The dashboard KPIs (whole history of the table). capital_base
    (bot 1's day reference balance) turns max drawdown into a percentage."""
    metrics = compute_all(all_pnl_rows(con), capital_base)
    save_json_atomic(path or SUMMARY_FILE,
                     metrics | {"generated_at": now.isoformat()})
    return metrics


def export_csv(con: sqlite3.Connection, path: str | None = None):
    """Excel-friendly weekly export (UTF-8 BOM, readable headers)."""
    cur = con.execute(
        "SELECT date_utc, asset, direction, mt5_action, bot7_view,"
        " is_aligned, pnl, winner_arbitrage FROM arbitrage_logs"
        " ORDER BY date_utc")
    path = path or EXPORT_CSV
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(EXPORT_HEADERS)
    for d, asset, direction, action, view, aligned, pnl, winner in cur:
        writer.writerow([d, asset, direction, action, view,
                         "" if aligned is None else ("YES" if aligned
                                                     else "NO"),
                         f"{pnl:+.2f}", winner])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write(buf.getvalue())
    os.replace(tmp, path)


def days_with_real_rows(con: sqlite3.Connection) -> set[str]:
    """ISO days already holding at least one real trade row."""
    cur = con.execute("SELECT DISTINCT substr(date_utc, 1, 10)"
                      " FROM arbitrage_logs WHERE asset != '-'")
    return {r[0] for r in cur.fetchall()}


def backfill_missing_days(con: sqlite3.Connection, now: datetime,
                          trades: list[dict] | None = None,
                          history: list[dict] | None = None) -> int:
    """(Re)arbitrate past journal days without any real arbitrage row.

    Journal corrections (e.g. the close_time shift of issue #32) repair
    the dataset instead of leaving holes: each such day is rebuilt with
    its ARCHIVED weather (macro_history.json). Days already holding
    real rows are never touched; a stale no-signal row is replaced.
    Returns the number of days backfilled.
    """
    by_day: dict[date, list[dict]] = {}
    for t in (read_trades() if trades is None else trades):
        by_day.setdefault(t["close_time"].date(), []).append(t)
    done = days_with_real_rows(con)
    history = load_history() if history is None else history
    filled = 0
    for day in sorted(by_day):
        if day >= now.date() or day.isoformat() in done:
            continue                  # today belongs to the daily run
        weather = weather_from_history(history, day)
        delete_day(con, day)          # drops a stale no-signal row
        insert_rows(con, [arbitrate(t, weather) for t in by_day[day]])
        filled += 1
        log.info("Backfill %s: %d row(s), archived weather %s", day,
                 len(by_day[day]), "found" if weather else "unavailable")
    return filled


# --- Daily cycle ---------------------------------------------------------------
def run_arbitration(con: sqlite3.Connection, state: dict, now: datetime):
    """22:00 UTC: weather snapshot vs the day's closed trades (after a
    backfill pass repairing any past-day hole, issue #32)."""
    day = now.date()
    trades = read_trades()
    backfill_missing_days(con, now, trades)
    weather = day_weather(load_json(WEATHER_FILE), day)
    todays = [t for t in trades if t["close_time"].date() == day]
    rows = ([arbitrate(t, weather) for t in todays]
            or [no_signal_row(day, weather)])
    delete_day(con, day)
    insert_rows(con, rows)
    metrics = write_summary(con, now,
                            load_json(SENTINEL_STATE).get("day_balance"))
    export_csv(con)
    state["last_run_day"] = day.isoformat()
    save_json_atomic(STATE_FILE, state)
    log.info("Arbitration %s: %d row(s), win rate %s%%, PF %s -> "
             "arbitrage.db + summary + export", day, len(rows),
             metrics["win_rate"], metrics["profit_factor"])


def run_cycle(con: sqlite3.Connection, state: dict,
              now: datetime | None = None):
    now = now or datetime.now(timezone.utc)
    if should_run(state, now):
        run_arbitration(con, state, now)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    con = init_db()
    state = load_json(STATE_FILE)
    if "--once" in sys.argv:          # immediate arbitration (manual test)
        run_arbitration(con, state, datetime.now(timezone.utc))
        return 0
    log.info("Starting SENTINEL ARBITRAGE (daily arbitration %02d:00 UTC)",
             RUN_HOUR)
    while True:
        try:
            run_cycle(con, state)
            write_heartbeat()
        except Exception as exc:      # never crash: log and continue
            log.exception("Unexpected error: %s", exc)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
