"""SENTINEL TREND - MT5 trend-following bot (Time-Series Momentum).

Turtle/CTA-style strategy (Moskowitz-Ooi-Pedersen 2012, Hurst-Ooi-Pedersen
2017): entry on a 55-candle H4 Donchian channel breakout, initial stop at
2xATR(14), exit on the opposite 20-candle channel breakout (slow trailing).
Positive asymmetry: many small losses, rare but large wins.

Risk: 1% of equity per trade (ATR-normalized sizing, hence implicit
vol-targeting), modulated by the global scale factor written by the risk
orchestrator (risk_scale.json).

Safety: permanent lock if equity loses 15% from its historical peak.
No trading window: trends hold for weeks.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import MetaTrader5 as mt5

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
# Diversified multi-class portfolio: canonical name -> magic + fallbacks.
# risk_mult: factor applied to the asset's per-trade risk. 0.5 on
# EURUSD/GBPUSD/XTIUSD since 2026-07-15: 2-year backtest negative on
# all variants and both halves (docs/AMELIORATION_CONTINUE.md,
# section 5); re-evaluation planned at 30 real trades per asset.
TREND_PORTFOLIO = {
    "XAUUSD": {"magic": 5001, "fallback": ["XAUUSD.p", "GOLD"],
               "risk_mult": 1.0},
    "EURUSD": {"magic": 5002, "fallback": ["EURUSD.p"], "risk_mult": 0.5},
    "GBPUSD": {"magic": 5003, "fallback": ["GBPUSD.p"], "risk_mult": 0.5},
    "US500":  {"magic": 5004, "fallback": ["US500.p", "SP500", "SPX500"],
               "risk_mult": 1.0},
    "XTIUSD": {"magic": 5005, "fallback": ["XTIUSD.p", "SpotCrude", "USOIL"],
               "risk_mult": 0.5},
}
TREND_MAGICS = {cfg["magic"] for cfg in TREND_PORTFOLIO.values()}

ENTRY_CHANNEL = 55            # entry Donchian (Turtle System 2)
EXIT_CHANNEL = 20             # exit Donchian (slow trailing)
ATR_PERIOD = 14
ATR_STOP_MULT = 2.0           # initial stop = 2 x ATR(14) H4
RISK_PCT = 0.01               # 1% of equity per trade
MAX_HISTO_DD = 0.15           # permanent lock at -15% of the equity peak

# No session window: H4 momentum is insensitive to the hour and a time
# filter would be overfitting. However, no OPENING during the daily
# rollover (widened spreads, swaps): a breakout detected in that range
# is re-evaluated as soon as the blackout ends.
# Exits (opposite channel, stops) remain allowed 24/7.
ROLLOVER_HOUR_START = 21      # opening blackout 21:00-23:00 UTC
ROLLOVER_HOUR_END = 23

DEVIATION = 20
_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(_DIR, "trend_state.json")
RISK_SCALE_FILE = os.path.join(_DIR, "risk_scale.json")

log = logging.getLogger("trend")

CCY = {"EUR", "GBP", "USD", "JPY", "CHF", "AUD", "NZD", "CAD"}


def price_fmt(symbol: str) -> str:
    s = symbol.upper()
    return "%.5f" if s[:3] in CCY and s[3:6] in CCY else "%.2f"


def fp(symbol: str, value: float | None) -> str:
    return "n/a" if value is None else price_fmt(symbol) % value


def read_risk_scale(path: str = RISK_SCALE_FILE) -> float:
    """[0,1] factor written by the risk orchestrator; defaults to 1.0."""
    try:
        with open(path, encoding="utf-8") as fh:
            return min(1.0, max(0.0, float(json.load(fh)["scale"])))
    except (OSError, ValueError, KeyError):
        return 1.0


# ----------------------------------------------------------------------------
# Indicators and signals (pure, testable functions)
# ----------------------------------------------------------------------------
def atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    prev = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(),
                    (df["low"] - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def donchian(df: pd.DataFrame, n: int) -> tuple[float, float]:
    """(highest high, lowest low) of the n candles BEFORE the signal candle."""
    win = df.iloc[-(n + 1):-1]
    return float(win["high"].max()), float(win["low"].min())


def entry_signal(df: pd.DataFrame, n: int = ENTRY_CHANNEL) -> str | None:
    """BUY if the close breaks the channel high, SELL if it breaks the low."""
    if len(df) < n + 1:
        return None
    hh, ll = donchian(df, n)
    close = float(df["close"].iloc[-1])
    if close > hh:
        return "BUY"
    if close < ll:
        return "SELL"
    return None


def exit_signal(df: pd.DataFrame, direction: int,
                n: int = EXIT_CHANNEL) -> bool:
    """Trend exit: close beyond the n channel opposite to the position."""
    if len(df) < n + 1:
        return False
    hh, ll = donchian(df, n)
    close = float(df["close"].iloc[-1])
    if direction == mt5.POSITION_TYPE_BUY:
        return close < ll
    return close > hh


def compute_lot(equity: float, sl_distance: float, tick_size: float,
                tick_value: float, vol_min: float, vol_max: float,
                vol_step: float, scale: float = 1.0) -> float:
    """Volume risking RISK_PCT of equity (x global scale factor)."""
    if sl_distance <= 0 or tick_size <= 0 or tick_value <= 0:
        return 0.0
    loss_per_lot = (sl_distance / tick_size) * tick_value
    lots = (equity * RISK_PCT * scale) / loss_per_lot
    lots = round(np.floor(lots / vol_step) * vol_step, 8)
    if lots < vol_min:
        return 0.0
    return float(min(lots, vol_max))


# ----------------------------------------------------------------------------
# Historical drawdown lock
# ----------------------------------------------------------------------------
class PeakGuard:
    def __init__(self, state_file: str = STATE_FILE):
        self.state_file = state_file
        self.peak = 0.0
        self.locked = False
        try:
            with open(state_file, encoding="utf-8") as fh:
                st = json.load(fh)
            self.peak = st.get("peak", 0.0)
            self.locked = st.get("locked", False)
        except (OSError, ValueError):
            pass

    def _save(self):
        try:
            save_json_atomic(self.state_file,
                             {"peak": self.peak, "locked": self.locked})
        except OSError as exc:
            log.warning("State save failed: %s", exc)

    def check(self, equity: float) -> bool:
        if self.locked:
            return True
        if equity > self.peak:
            self.peak = equity
            self._save()
        elif self.peak > 0 and equity <= self.peak * (1 - MAX_HISTO_DD):
            self.locked = True
            self._save()
            log.critical("TREND MAX DRAWDOWN: equity %.2f <= -%.0f%% of peak "
                         "%.2f. Permanent lock.", equity,
                         MAX_HISTO_DD * 100, self.peak)
            return True
        return False


# ----------------------------------------------------------------------------
# MT5 access
# ----------------------------------------------------------------------------
def resolve_symbols() -> dict:
    """{name: {"symbol", "magic"}} for assets available at the broker."""
    active = {}
    for name, cfg in TREND_PORTFOLIO.items():
        found = next((s for s in [name] + cfg["fallback"]
                      if mt5.symbol_select(s, True)
                      and mt5.symbol_info(s) is not None), None)
        if found:
            active[name] = {"symbol": found, "magic": cfg["magic"],
                            "risk_mult": cfg.get("risk_mult", 1.0)}
            log.info("Asset %s -> %s (risk x%.1f)", name, found,
                     active[name]["risk_mult"])
        else:
            log.warning("Asset %s unavailable, removed: %s", name,
                        mt5.last_error())
    return active


def get_rates(symbol: str, timeframe: int, count: int) -> pd.DataFrame | None:
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
    if rates is None or len(rates) == 0:
        log.warning("No data for %s: %s", symbol, mt5.last_error())
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df


def send_order(request: dict):
    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        log.error("Order rejected: %s / %s",
                  getattr(result, "retcode", None), mt5.last_error())
        return None
    return result


def open_trend_trade(symbol: str, direction: str, magic: int,
                     df: pd.DataFrame, risk_mult: float = 1.0) -> bool:
    """Market entry, hard SL at 2xATR, no TP (channel exit)."""
    acc = mt5.account_info()
    sym = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if acc is None or sym is None or tick is None:
        return False
    sl_dist = ATR_STOP_MULT * float(atr(df).iloc[-1])
    if sl_dist <= 0:
        return False
    lot = compute_lot(acc.equity, sl_dist, sym.trade_tick_size,
                      sym.trade_tick_value, sym.volume_min, sym.volume_max,
                      sym.volume_step, read_risk_scale() * risk_mult)
    if lot <= 0:
        log.warning("[%s] zero lot, entry skipped.", symbol)
        return False
    buy = direction == "BUY"
    price = tick.ask if buy else tick.bid
    sl = price - sl_dist if buy else price + sl_dist
    result = send_order({
        "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": lot,
        "type": mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL,
        "price": price, "sl": round(sl, sym.digits), "deviation": DEVIATION,
        "magic": magic, "comment": "sentinel_trend",
        "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC})
    if result:
        log.info("[TREND] %s %s lot=%.2f @ %s SL=%s", direction, symbol,
                 lot, fp(symbol, price), fp(symbol, sl))
        return True
    return False


def close_position(pos) -> bool:
    tick = mt5.symbol_info_tick(pos.symbol)
    if tick is None:
        return False
    buy = pos.type == mt5.POSITION_TYPE_SELL
    return send_order({
        "action": mt5.TRADE_ACTION_DEAL, "symbol": pos.symbol,
        "volume": pos.volume,
        "type": mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL,
        "position": pos.ticket, "price": tick.ask if buy else tick.bid,
        "deviation": DEVIATION, "magic": pos.magic,
        "comment": "trend_exit", "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC}) is not None


def positions_for(symbol: str, magic: int) -> list:
    return [p for p in (mt5.positions_get(symbol=symbol) or [])
            if p.magic == magic and p.symbol == symbol]


# ----------------------------------------------------------------------------
# Main loop
# ----------------------------------------------------------------------------
def save_json_atomic(path: str, payload: dict):
    """Temp file + os.replace: the previous state survives a crash."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)


HEARTBEAT_FILE = os.path.join(os.path.dirname(_DIR), "logs",
                              "sentinel_trend.hb")


def write_heartbeat(path: str = HEARTBEAT_FILE,
                    now: datetime | None = None):
    """Liveness timestamp after each successful cycle (read by the watchdog)."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write((now or datetime.now(timezone.utc)).isoformat())
    except OSError:
        pass


def entry_allowed(now: datetime) -> bool:
    """False during the rollover blackout (openings only)."""
    return not (ROLLOVER_HOUR_START <= now.hour < ROLLOVER_HOUR_END)


def scan_symbol(name: str, cfg: dict, timeframe: int, last_bars: dict,
                now: datetime | None = None):
    """On a new closed candle: manage the exit or look for an entry."""
    now = now or datetime.now(timezone.utc)
    symbol, magic = cfg["symbol"], cfg["magic"]
    df = get_rates(symbol, timeframe, ENTRY_CHANNEL + 3)
    if df is None or len(df) < ENTRY_CHANNEL + 2:
        return
    closed = df.iloc[:-1]                 # last row = candle in progress
    bar_time = closed["time"].iloc[-1]
    if last_bars.get(name) == bar_time:
        return
    last_bars[name] = bar_time

    open_pos = positions_for(symbol, magic)
    if open_pos:
        for pos in open_pos:
            if exit_signal(closed, pos.type):
                if close_position(pos):
                    log.info("[TREND] channel exit %s ticket=%s profit=%.2f",
                             name, pos.ticket, pos.profit)
        return
    sig = entry_signal(closed)
    if sig:
        if not entry_allowed(now):
            last_bars.pop(name, None)   # re-evaluate the candle after blackout
            log.info("[TREND] breakout %s deferred (rollover %02d-%02dh UTC)",
                     name, ROLLOVER_HOUR_START, ROLLOVER_HOUR_END)
            return
        log.info("[TREND] Donchian %s breakout %s", ENTRY_CHANNEL, name)
        open_trend_trade(symbol, sig, magic, closed,
                         cfg.get("risk_mult", 1.0))


def close_all_trend():
    for pos in mt5.positions_get() or []:
        if pos.magic in TREND_MAGICS and close_position(pos):
            log.info("Lock: position %s (%s) closed.", pos.ticket,
                     pos.symbol)


def run_cycle(active: dict, guard: PeakGuard, timeframe: int,
              last_bars: dict, now: datetime | None = None):
    now = now or datetime.now(timezone.utc)
    acc = mt5.account_info()
    if acc is None:
        raise ConnectionError(f"account_info() KO: {mt5.last_error()}")
    if guard.check(acc.equity):
        close_all_trend()
        return
    for name, cfg in active.items():
        scan_symbol(name, cfg, timeframe, last_bars, now)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    log.info("Starting SENTINEL TREND (Donchian %s/%s, H4)",
             ENTRY_CHANNEL, EXIT_CHANNEL)
    if not mt5.initialize(
            path="C:/Program Files/Pepperstone MetaTrader 5/terminal64.exe"):
        log.error("mt5.initialize() failed: %s", mt5.last_error())
        return 1
    active = resolve_symbols()
    if not active:
        mt5.shutdown()
        return 1
    guard = PeakGuard()
    last_bars: dict = {}
    timeframe = mt5.TIMEFRAME_H4
    while True:
        try:
            run_cycle(active, guard, timeframe, last_bars)
            write_heartbeat()
        except ConnectionError as exc:
            log.error("Connection lost: %s - reconnecting...", exc)
            mt5.shutdown()
            time.sleep(5)
            mt5.initialize()
        except Exception as exc:
            log.exception("Unexpected error: %s", exc)
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
