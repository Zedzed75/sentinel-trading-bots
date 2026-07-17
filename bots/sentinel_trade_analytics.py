"""SENTINEL TRADE ANALYTICS - Journal and analysis of the fleet's trades.

Does not trade: it reads the deal history from the MT5 terminal (Sentinel
magics only), rebuilds the closed trades and publishes:

1. logs/trades.csv       full journal, one closed trade per line;
2. logs/analytics.html   auto-refreshing report: win rate, profit factor,
                         expectancy, net PnL, max drawdown, broken down by
                         strategy and by symbol over 7 days / 30 days /
                         all time, plus the most recent trades.

Goal: measure each strategy continuously to improve it (cut what loses,
reinforce what wins) without digging through the terminal by hand.
All data comes from the terminal (history_deals_get): no persistent
state, the report is rebuilt on every cycle.
"""

import io
import csv
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import MetaTrader5 as mt5

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
# Magic -> strategy (bot 1: 1001-3002, alpha: 4001, trend: 5001-5005).
# Deliberate copy: no cross-imports between bots (see README).
MAGIC_STRATEGY = {
    1001: "breakout", 2001: "breakout", 3001: "breakout",
    1002: "reversion", 2002: "reversion", 3002: "reversion",
    4001: "statarb",
    5001: "trend", 5002: "trend", 5003: "trend", 5004: "trend",
    5005: "trend",
}
HISTORY_DAYS = 365            # history depth requested from the terminal
CYCLE_SECONDS = 900           # one report every 15 minutes
LAST_TRADES_SHOWN = 20        # most recent trades shown in the report
WINDOWS = (("7 days", 7), ("30 days", 30), ("All time", None))

_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(os.path.dirname(_DIR), "logs")
TRADES_CSV = os.path.join(LOG_DIR, "trades.csv")
REPORT_HTML = os.path.join(LOG_DIR, "analytics.html")

CSV_FIELDS = ("close_time", "open_time", "strategy", "symbol", "direction",
              "volume", "pnl", "duration_h", "magic", "position_id")

log = logging.getLogger("analytics")


# ----------------------------------------------------------------------------
# Rebuilding trades from raw deals
# ----------------------------------------------------------------------------
_SERVER_OFFSET = {"hours": 0.0, "at": None}
_OFFSET_SYMBOLS = ("XAUUSD", "XAUUSD.p", "GOLD", "EURUSD", "EURUSD.p")


def server_offset_hours(now: datetime | None = None) -> float:
    """Offset (hours) between the MT5 server clock and real UTC.

    MT5 deals are stamped in server time (UTC+2/+3 at Pepperstone): we
    measure the gap between a recent tick and the local UTC clock,
    rounded to the half hour, cached for 1 h. Without a fresh tick
    (week-end), the last known value is kept.
    """
    now = now or datetime.now(timezone.utc)
    cache = _SERVER_OFFSET
    if cache["at"] is not None and now - cache["at"] < timedelta(hours=1):
        return cache["hours"]
    for name in _OFFSET_SYMBOLS:
        if not mt5.symbol_select(name, True):
            continue
        ts = getattr(mt5.symbol_info_tick(name), "time", None)
        if isinstance(ts, (int, float)) and ts > 0:
            delta_h = (ts - now.timestamp()) / 3600
            if abs(delta_h) <= 13:        # fresh tick, plausible offset
                cache["hours"] = round(delta_h * 2) / 2
                cache["at"] = now
                break
    return cache["hours"]


def build_trades(deals, offset_h: float = 0.0) -> list[dict]:
    """One closed trade per position_id (Sentinel magics only).

    Partial exits are summed; a position whose exit volume does not
    cover the entry (still open) is ignored.
    Net PnL = profit + commission + swap of all the position's deals.
    offset_h (server time - UTC) converts timestamps to real UTC.
    """
    by_pos: dict[int, list] = {}
    for d in deals or []:
        if getattr(d, "magic", None) in MAGIC_STRATEGY:
            by_pos.setdefault(d.position_id, []).append(d)

    trades = []
    for pos_id, group in by_pos.items():
        ins = [d for d in group if d.entry == mt5.DEAL_ENTRY_IN]
        outs = [d for d in group if d.entry == mt5.DEAL_ENTRY_OUT]
        if not ins or not outs:
            continue
        vol_in = sum(d.volume for d in ins)
        if sum(d.volume for d in outs) < vol_in - 1e-8:
            continue
        entry = min(ins, key=lambda d: d.time)
        close_ts = max(d.time for d in outs) - offset_h * 3600
        open_ts = entry.time - offset_h * 3600
        open_dt = datetime.fromtimestamp(open_ts, tz=timezone.utc)
        pnl = sum(d.profit + d.commission + d.swap for d in group)
        trades.append({
            "position_id": pos_id,
            "symbol": entry.symbol,
            "magic": entry.magic,
            "strategy": MAGIC_STRATEGY[entry.magic],
            "direction": ("long" if entry.type == mt5.DEAL_TYPE_BUY
                          else "short"),
            "volume": vol_in,
            "open_time": open_dt,
            "open_hour": f"{open_dt.hour:02d}h",
            "close_time": datetime.fromtimestamp(close_ts, tz=timezone.utc),
            "duration_h": round((close_ts - open_ts) / 3600, 2),
            "pnl": round(pnl, 2),
        })
    trades.sort(key=lambda t: t["close_time"])
    return trades


# ----------------------------------------------------------------------------
# Statistics
# ----------------------------------------------------------------------------
def compute_stats(trades: list[dict]) -> dict:
    """Win rate, profit factor, expectancy, net PnL, max drawdown of the
    cumulative curve.

    profit_factor is None when there is no loss (undefined).
    Drawdown is measured on the cumulative PnL in close order.
    """
    if not trades:
        return {"trades": 0, "win_rate": None, "profit_factor": None,
                "expectancy": None, "pnl": 0.0, "max_dd": 0.0,
                "avg_duration_h": None}
    pnls = [t["pnl"] for t in trades]
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)
    cum = peak = max_dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    return {
        "trades": len(pnls),
        "win_rate": sum(1 for p in pnls if p > 0) / len(pnls),
        "profit_factor": (round(gross_win / gross_loss, 2)
                          if gross_loss > 0 else None),
        "expectancy": round(sum(pnls) / len(pnls), 2),
        "pnl": round(sum(pnls), 2),
        "max_dd": round(max_dd, 2),
        "avg_duration_h": round(sum(t["duration_h"] for t in trades)
                                / len(trades), 1),
    }


def in_window(trades: list[dict], now: datetime,
              days: int | None) -> list[dict]:
    if days is None:
        return trades
    limit = now - timedelta(days=days)
    return [t for t in trades if t["close_time"] >= limit]


def split_by(trades: list[dict], key: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for t in trades:
        out.setdefault(t[key], []).append(t)
    return out


# ----------------------------------------------------------------------------
# Outputs: CSV journal and HTML report
# ----------------------------------------------------------------------------
HEARTBEAT_FILE = os.path.join(LOG_DIR, "sentinel_trade_analytics.hb")


def write_heartbeat(path: str = HEARTBEAT_FILE,
                    now: datetime | None = None):
    """Liveness timestamp after each successful cycle (read by the watchdog)."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write((now or datetime.now(timezone.utc)).isoformat())
    except OSError:
        pass


def _write_atomic(path: str, text: str):
    """Write via a temp file + rename: never a corrupted file."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    os.replace(tmp, path)


def write_trades_csv(trades: list[dict], path: str):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for t in trades:
        row = dict(t)
        row["open_time"] = t["open_time"].isoformat()
        row["close_time"] = t["close_time"].isoformat()
        writer.writerow(row)
    _write_atomic(path, buf.getvalue())


def _pct(v) -> str:
    return "-" if v is None else f"{v * 100:.0f}%"


def _num(v, unit: str = "") -> str:
    return "-" if v is None else f"{v:,.2f}{unit}"


def _stats_cells(st: dict) -> str:
    pf = "-" if st["profit_factor"] is None else f"{st['profit_factor']:.2f}"
    return (f"<td>{st['trades']}</td><td>{_pct(st['win_rate'])}</td>"
            f"<td>{pf}</td><td>{_num(st['expectancy'])}</td>"
            f"<td class='{_sign(st['pnl'])}'>{_num(st['pnl'])}</td>"
            f"<td>{_num(st['max_dd'])}</td>"
            f"<td>{_num(st['avg_duration_h'], ' h')}</td>")


def _sign(pnl) -> str:
    return "pos" if (pnl or 0) >= 0 else "neg"


def _stats_head(first_col: str) -> str:
    return (f"<tr><th>{first_col}</th><th>Trades</th><th>Win rate</th>"
            "<th>Profit factor</th><th>Expectancy</th><th>Net PnL</th>"
            "<th>Max DD</th><th>Avg duration</th></tr>")


def _stats_table(trades: list[dict], group_key: str,
                 total_label: str = "ALL",
                 first_col: str = "Strategy") -> str:
    rows = [f"<tr class='total'><td>{total_label}</td>"
            f"{_stats_cells(compute_stats(trades))}</tr>"]
    for name, sub in sorted(split_by(trades, group_key).items()):
        rows.append(f"<tr><td>{name}</td>{_stats_cells(compute_stats(sub))}"
                    "</tr>")
    return "<table>" + _stats_head(first_col) + "\n".join(rows) + "</table>"


def render_html(trades: list[dict], now: datetime) -> str:
    sections = []
    for label, days in WINDOWS:
        sub = in_window(trades, now, days)
        sections.append(f"<h2>{label} ({len(sub)} trades)</h2>"
                        + _stats_table(sub, "strategy"))
    sections.append("<h2>By symbol (all time)</h2>"
                    + _stats_table(trades, "symbol", first_col="Symbol"))

    # Breakdown by UTC open hour: informs the entry windows with real
    # trades (AMELIORATION_CONTINUE.md, roadmap 2). Conclusions remain
    # subject to the sample thresholds of section 3.
    sections.append("<h2>By UTC open hour (all time)</h2>")
    for strat, sub in sorted(split_by(trades, "strategy").items()):
        sections.append(f"<h3>{strat}</h3>"
                        + _stats_table(sub, "open_hour", "ALL HOURS",
                                       first_col="Hour (UTC)"))

    last = [(f"<tr><td>{t['close_time']:%Y-%m-%d %H:%M}</td>"
             f"<td>{t['strategy']}</td><td>{t['symbol']}</td>"
             f"<td>{t['direction']}</td><td>{t['volume']}</td>"
             f"<td class='{_sign(t['pnl'])}'>{t['pnl']}</td>"
             f"<td>{t['duration_h']} h</td></tr>")
            for t in reversed(trades[-LAST_TRADES_SHOWN:])]
    sections.append(
        f"<h2>{LAST_TRADES_SHOWN} most recent trades</h2><table>"
        "<tr><th>Close (UTC)</th><th>Strategy</th><th>Symbol</th>"
        "<th>Side</th><th>Volume</th><th>PnL</th><th>Duration</th></tr>"
        + "\n".join(last) + "</table>")

    body = "\n".join(sections)
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="300">
<title>Sentinel - trade analytics</title>
<style>
 body {{ font-family: Segoe UI, sans-serif; margin: 2em;
        background: #1b1e24; color: #d8dde6; }}
 h1 {{ font-size: 1.3em; }} h2 {{ font-size: 1.1em; margin-top: 1.6em; }}
 h3 {{ font-size: 1em; margin: 1em 0 0; color: #aab3c2; }}
 small {{ color: #8a93a3; }}
 table {{ border-collapse: collapse; margin-top: .6em; }}
 td, th {{ padding: .4em .8em; border-bottom: 1px solid #333a45;
          text-align: left; }}
 tr.total td {{ font-weight: bold; }}
 td.pos {{ color: #4cc36a; }} td.neg {{ color: #e05555; }}
</style>
</head><body>
<h1>Sentinel trade analytics <small>updated
{now:%Y-%m-%d %H:%M} UTC - PnL net of fees and swap, times converted
to real UTC, Sentinel magics only</small></h1>
{body}
</body></html>
"""


# ----------------------------------------------------------------------------
# Main loop
# ----------------------------------------------------------------------------
def run_cycle(now: datetime | None = None):
    now = now or datetime.now(timezone.utc)
    # one-day margin: deals are stamped in server time (UTC+2/3)
    deals = mt5.history_deals_get(now - timedelta(days=HISTORY_DAYS),
                                  now + timedelta(days=1))
    if deals is None:
        raise ConnectionError(f"history_deals_get() KO: {mt5.last_error()}")
    trades = build_trades(deals, server_offset_hours(now))
    os.makedirs(LOG_DIR, exist_ok=True)
    write_trades_csv(trades, TRADES_CSV)
    _write_atomic(REPORT_HTML, render_html(trades, now))
    log.info("%d closed trades analyzed -> analytics.html + trades.csv",
             len(trades))


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    log.info("Starting SENTINEL TRADE ANALYTICS (cycle %ds, history "
             "%d days)", CYCLE_SECONDS, HISTORY_DAYS)
    if not mt5.initialize(
            path="C:/Program Files/Pepperstone MetaTrader 5/terminal64.exe"):
        log.error("mt5.initialize() failed: %s", mt5.last_error())
        return 1
    while True:
        try:
            run_cycle()
            write_heartbeat()
        except ConnectionError as exc:
            log.error("Connection lost: %s - reconnecting...", exc)
            mt5.shutdown()
            time.sleep(5)
            mt5.initialize()
        except Exception as exc:
            log.exception("Unexpected error: %s", exc)
        time.sleep(CYCLE_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
