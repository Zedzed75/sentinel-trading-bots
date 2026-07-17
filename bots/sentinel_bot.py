"""SENTINEL - Multi-asset MetaTrader 5 algorithmic trading bot.

Portfolio: XAUUSD, EURUSD, GBPUSD (see CONFIG_PORTFOLIO).
Strategies (applied to each asset):
  A) Asian session breakout (M30), VIX macro filter.
  B) Bollinger/RSI mean reversion (M5) during range phases.
Risk: 1.5% of balance per trade, SL = 1.5*ATR(14) M30, TP = 2*SL,
50% partial + break-even at 1R, 4% daily drawdown circuit breaker.

Pure functions (indicators, signals, trading windows) live in
sentinel_signals.py; this file owns risk, MT5 access and the main loop.
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import MetaTrader5 as mt5
import yfinance as yf

from sentinel_signals import (
    BREAKOUT_HOUR_END, BREAKOUT_HOUR_START, REVERSION_HOUR_END,
    REVERSION_HOUR_START, apply_macro_filter, asian_range, atr,
    breakout_signal, fp, in_trading_hours, reversion_signal,
)

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
# Portfolio: canonical name -> magics + broker fallback symbols
# (Pepperstone Razor exposes the .p suffix; the canonical name is tried first)
# vix_filter: True = block SELLs when VIX > 25 (safe-haven asset, gold
# only); False = shorts allowed during crises (forex pairs vs USD)
# breakout: False = strategy A suspended on the asset (reversion keeps
# running). EURUSD and GBPUSD suspended on 2026-07-15: PF < 1 on both
# halves of the backtest, ~650 trades each (structural, not a
# parameter - docs/AMELIORATION_CONTINUE.md, section 5). Quarterly
# re-evaluation planned.
CONFIG_PORTFOLIO = {
    "XAUUSD": {"magic_breakout": 1001, "magic_reversion": 1002,
               "fallback": ["XAUUSD.p", "GOLD"], "vix_filter": True,
               "breakout": True},
    "EURUSD": {"magic_breakout": 2001, "magic_reversion": 2002,
               "fallback": ["EURUSD.p"], "vix_filter": False,
               "breakout": False},
    "GBPUSD": {"magic_breakout": 3001, "magic_reversion": 3002,
               "fallback": ["GBPUSD.p"], "vix_filter": False,
               "breakout": False},
}

RISK_PCT = 0.015              # 1.5% of balance per trade
ATR_SL_MULT = 1.5             # SL_distance = 1.5 * ATR(14) M30
RR_RATIO = 2.0                # TP = 2 * SL_distance
DAILY_DD_LIMIT = 0.04         # circuit breaker at -4% equity vs day balance

VIX_TICKER = "^VIX"

DEVIATION = 20                # max slippage in points
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "sentinel_state.json")
RISK_SCALE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "risk_scale.json")

log = logging.getLogger("sentinel")


# ----------------------------------------------------------------------------
# Risk management (pure, testable functions)
# ----------------------------------------------------------------------------
def reached_one_r(pos_type: int, price_open: float, sl: float,
                  current: float) -> bool:
    """True if the position reached a 1R profit (initial SL distance)."""
    risk = abs(price_open - sl)
    if risk <= 0:
        return False
    if pos_type == mt5.POSITION_TYPE_BUY:
        return current >= price_open + risk
    return current <= price_open - risk


def save_json_atomic(path: str, payload: dict):
    """Temp file + os.replace: the previous state survives a crash."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)


HEARTBEAT_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "logs", "sentinel_bot.hb")


def write_heartbeat(path: str = HEARTBEAT_FILE,
                    now: datetime | None = None):
    """Liveness timestamp after each successful cycle (read by the
    watchdog): a process that is alive but frozen (endless reconnect)
    will be restarted."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write((now or datetime.now(timezone.utc)).isoformat())
    except OSError:
        pass


def read_risk_scale(path: str | None = None) -> float:
    """[0,1] factor written by the risk orchestrator; defaults to 1.0."""
    try:
        with open(path or RISK_SCALE_FILE, encoding="utf-8") as fh:
            return min(1.0, max(0.0, float(json.load(fh)["scale"])))
    except (OSError, ValueError, KeyError):
        return 1.0


def compute_lot(balance: float, sl_distance: float, tick_size: float,
                tick_value: float, vol_min: float, vol_max: float,
                vol_step: float, scale: float = 1.0) -> float:
    """Volume risking RISK_PCT of the balance (x global risk scale)."""
    if sl_distance <= 0 or tick_size <= 0 or tick_value <= 0:
        return 0.0
    loss_per_lot = (sl_distance / tick_size) * tick_value
    lots = (balance * RISK_PCT * scale) / loss_per_lot
    lots = np.floor(lots / vol_step) * vol_step
    lots = round(lots, 8)
    if lots < vol_min:
        return 0.0
    return float(min(lots, vol_max))


# ----------------------------------------------------------------------------
# VIX macro filter (yfinance, one fetch per day)
# ----------------------------------------------------------------------------
class MacroFilter:
    def __init__(self):
        self._date = None
        self._vix = None

    def vix(self, now: datetime) -> float | None:
        today = now.date()
        if self._date == today:
            return self._vix
        try:
            hist = yf.Ticker(VIX_TICKER).history(period="5d")
            self._vix = float(hist["Close"].iloc[-1])
            self._date = today
            log.info("Today's VIX: %.2f", self._vix)
        except Exception as exc:
            log.warning("VIX fetch failed (%s) - SELLs blocked.", exc)
            self._vix = None
            self._date = today
        return self._vix


# ----------------------------------------------------------------------------
# Daily drawdown circuit breaker
# ----------------------------------------------------------------------------
class DayGuard:
    """Reference balance at 00:00 UTC, lock if equity <= -4%."""

    def __init__(self, state_file: str = STATE_FILE):
        self.state_file = state_file
        self.day = None
        self.day_balance = None
        self.locked = False
        self._load()

    def _load(self):
        try:
            with open(self.state_file, encoding="utf-8") as fh:
                st = json.load(fh)
            self.day = st.get("day")
            self.day_balance = st.get("day_balance")
            self.locked = st.get("locked", False)
        except (OSError, ValueError):
            pass

    def _save(self):
        try:
            save_json_atomic(self.state_file,
                             {"day": self.day,
                              "day_balance": self.day_balance,
                              "locked": self.locked})
        except OSError as exc:
            log.warning("State save failed: %s", exc)

    def roll_day(self, now: datetime, balance: float):
        """Call on every tick: resets on each new UTC day."""
        today = now.date().isoformat()
        if self.day != today:
            self.day = today
            self.day_balance = balance
            self.locked = False
            self._save()
            log.info("New UTC day %s - reference balance %.2f",
                     today, balance)

    def check(self, equity: float) -> bool:
        """True if the circuit breaker must trigger (or is already active)."""
        if self.locked:
            return True
        if self.day_balance and equity <= self.day_balance * (1 - DAILY_DD_LIMIT):
            self.locked = True
            self._save()
            log.error("CIRCUIT BREAKER: equity %.2f <= -4%% of %.2f. "
                      "Bot locked until 00:00 UTC.",
                      equity, self.day_balance)
            return True
        return False


# ----------------------------------------------------------------------------
# MT5 access
# ----------------------------------------------------------------------------
def connect() -> bool:
    if not mt5.initialize(path="C:/Program Files/Pepperstone MetaTrader 5/terminal64.exe"):
        log.error("mt5.initialize() failed: %s", mt5.last_error())
        return False
    info = mt5.account_info()
    if info is None:
        log.error("account_info() failed: %s", mt5.last_error())
        return False
    log.info("Connected to MT5 - account %s, balance %.2f %s",
             info.login, info.balance, info.currency)
    return True


def resolve_symbols() -> dict:
    """Validate each portfolio asset (canonical name then fallbacks).

    Returns {name: {"symbol": broker symbol, "magic_breakout", "magic_reversion"}}.
    An asset missing at the broker is dropped with a WARNING, without blocking.
    """
    active = {}
    for name, cfg in CONFIG_PORTFOLIO.items():
        found = next((s for s in [name] + cfg["fallback"]
                      if mt5.symbol_select(s, True)
                      and mt5.symbol_info(s) is not None), None)
        if found:
            active[name] = {"symbol": found,
                            "magic_breakout": cfg["magic_breakout"],
                            "magic_reversion": cfg["magic_reversion"],
                            "vix_filter": cfg.get("vix_filter", True),
                            "breakout": cfg.get("breakout", True)}
            log.info("Asset %s -> broker symbol %s%s", name, found,
                     "" if active[name]["breakout"]
                     else " (breakout suspended)")
        else:
            log.warning("Asset %s unavailable at the broker, removed from "
                        "the portfolio: %s", name, mt5.last_error())
    return active


_SERVER_OFFSET = {"hours": 0.0, "at": None}


def server_offset_hours(symbol: str, now: datetime | None = None) -> float:
    """Offset (hours) between the MT5 server clock and real UTC.

    MT5 candles are stamped in server time (UTC+2/+3 at Pepperstone):
    without conversion, every trading window (the Asian range first)
    would be shifted. We measure the gap between a recent tick and the
    local UTC clock, rounded to the half hour, cached for 1 h. Without
    a fresh tick (week-end), the last known value is kept.
    """
    now = now or datetime.now(timezone.utc)
    cache = _SERVER_OFFSET
    if cache["at"] is not None and now - cache["at"] < timedelta(hours=1):
        return cache["hours"]
    ts = getattr(mt5.symbol_info_tick(symbol), "time", None)
    if isinstance(ts, (int, float)) and ts > 0:
        delta_h = (ts - now.timestamp()) / 3600
        if abs(delta_h) <= 13:            # fresh tick, plausible offset
            cache["hours"] = round(delta_h * 2) / 2
            cache["at"] = now
    return cache["hours"]


def get_rates(symbol: str, timeframe: int, count: int) -> pd.DataFrame | None:
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
    if rates is None or len(rates) == 0:
        log.warning("copy_rates_from_pos empty (%s tf=%s): %s",
                    symbol, timeframe, mt5.last_error())
        return None
    df = pd.DataFrame(rates)
    df["time"] = (pd.to_datetime(df["time"], unit="s", utc=True)
                  - pd.Timedelta(hours=server_offset_hours(symbol)))
    return df


def send_order(request: dict):
    result = mt5.order_send(request)
    if result is None:
        log.error("order_send None: %s", mt5.last_error())
        return None
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        log.error("Order rejected retcode=%s comment=%s",
                  result.retcode, getattr(result, "comment", ""))
        return None
    return result


def open_trade(symbol: str, direction: str, magic: int, tag: str) -> bool:
    """Open a market trade with mandatory SL/TP and dynamic lot size."""
    acc = mt5.account_info()
    sym = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    df_m30 = get_rates(symbol, mt5.TIMEFRAME_M30, 100)
    if acc is None or sym is None or tick is None or df_m30 is None:
        log.error("Data unavailable to open a trade: %s",
                  mt5.last_error())
        return False
    sl_dist = ATR_SL_MULT * float(atr(df_m30).iloc[-1])
    if sl_dist <= 0:
        log.warning("Zero ATR, trade skipped.")
        return False
    lot = compute_lot(acc.balance, sl_dist, sym.trade_tick_size,
                      sym.trade_tick_value, sym.volume_min,
                      sym.volume_max, sym.volume_step, read_risk_scale())
    if lot <= 0:
        log.warning("Computed lot is zero (balance %.2f, SL %.2f), "
                    "trade skipped.", acc.balance, sl_dist)
        return False
    if direction == "BUY":
        order_type, price = mt5.ORDER_TYPE_BUY, tick.ask
        sl, tp = price - sl_dist, price + RR_RATIO * sl_dist
    else:
        order_type, price = mt5.ORDER_TYPE_SELL, tick.bid
        sl, tp = price + sl_dist, price - RR_RATIO * sl_dist
    digits = sym.digits
    request = {
        "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": lot,
        "type": order_type, "price": price,
        "sl": round(sl, digits), "tp": round(tp, digits),
        "deviation": DEVIATION, "magic": magic, "comment": tag,
        "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = send_order(request)
    if result:
        log.info("[%s] %s %s lot=%.2f @ %s SL=%s TP=%s",
                 tag, direction, symbol, lot, fp(symbol, price),
                 fp(symbol, sl), fp(symbol, tp))
        return True
    return False


def has_open_position(symbol: str, magic: int) -> bool:
    positions = mt5.positions_get(symbol=symbol) or []
    return any(p.magic == magic for p in positions)


def close_position(pos, volume: float | None = None) -> bool:
    """Full or partial close of a position via an opposite order."""
    tick = mt5.symbol_info_tick(pos.symbol)
    if tick is None:
        return False
    if pos.type == mt5.POSITION_TYPE_BUY:
        order_type, price = mt5.ORDER_TYPE_SELL, tick.bid
    else:
        order_type, price = mt5.ORDER_TYPE_BUY, tick.ask
    request = {
        "action": mt5.TRADE_ACTION_DEAL, "symbol": pos.symbol,
        "volume": volume if volume else pos.volume, "type": order_type,
        "position": pos.ticket, "price": price, "deviation": DEVIATION,
        "magic": pos.magic, "comment": "sentinel_close",
        "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
    }
    return send_order(request) is not None


def move_sl_to_breakeven(pos) -> bool:
    request = {
        "action": mt5.TRADE_ACTION_SLTP, "symbol": pos.symbol,
        "position": pos.ticket, "sl": pos.price_open, "tp": pos.tp,
    }
    return send_order(request) is not None


def manage_positions(symbol: str, magics: tuple):
    """At 1R: close 50% + break-even, strictly per symbol AND magic."""
    for pos in mt5.positions_get(symbol=symbol) or []:
        if pos.symbol != symbol or pos.magic not in magics:
            continue
        if pos.sl == pos.price_open:      # already moved to break-even
            continue
        if not pos.sl:
            continue
        if reached_one_r(pos.type, pos.price_open, pos.sl, pos.price_current):
            sym = mt5.symbol_info(symbol)
            step = sym.volume_step if sym else 0.01
            half = np.floor((pos.volume / 2) / step) * step
            half = round(half, 8)
            if half >= (sym.volume_min if sym else 0.01):
                if close_position(pos, half):
                    log.info("Position %s: 50%% closed at 1R (%.2f lots).",
                             pos.ticket, half)
            if move_sl_to_breakeven(pos):
                log.info("Position %s: SL moved to break-even %s.",
                         pos.ticket, fp(symbol, pos.price_open))


def close_everything():
    """GLOBAL circuit breaker: close everything, all symbols, cancel all orders."""
    for pos in mt5.positions_get() or []:
        if close_position(pos):
            log.info("Circuit breaker: position %s (%s) closed.",
                     pos.ticket, pos.symbol)
    for order in mt5.orders_get() or []:
        send_order({"action": mt5.TRADE_ACTION_REMOVE, "order": order.ticket})
        log.info("Circuit breaker: pending order %s cancelled.", order.ticket)


# ----------------------------------------------------------------------------
# Main loop
# ----------------------------------------------------------------------------
def scan_symbol(name: str, cfg: dict, macro: MacroFilter,
                last_bars: dict, now: datetime):
    """Active management + signal detection for one portfolio asset."""
    symbol, mb, mr = cfg["symbol"], cfg["magic_breakout"], cfg["magic_reversion"]
    vf = cfg.get("vix_filter", True)
    manage_positions(symbol, (mb, mr))

    # --- Strategy A: M30 breakout (on a new closed candle) ---
    if (cfg.get("breakout", True)
            and in_trading_hours(now, BREAKOUT_HOUR_START,
                                 BREAKOUT_HOUR_END)):
        df_m30 = get_rates(symbol, mt5.TIMEFRAME_M30, 96)
        if df_m30 is not None and len(df_m30) > 2:
            closed = df_m30.iloc[:-1]  # last row = candle in progress
            bar_time = closed["time"].iloc[-1]
            if last_bars.get((name, "m30")) != bar_time:
                last_bars[(name, "m30")] = bar_time
                hi, lo = asian_range(closed, now)
                sig = apply_macro_filter(breakout_signal(closed, hi, lo),
                                         macro.vix(now), vf)
                if sig and not has_open_position(symbol, mb):
                    log.info("[%s] BREAKOUT signal %s (Asia H=%s L=%s)",
                             name, sig, fp(symbol, hi), fp(symbol, lo))
                    open_trade(symbol, sig, mb, "sentinel_breakout")

    # --- Strategy B: M5 mean reversion (on a new closed candle) ---
    if in_trading_hours(now, REVERSION_HOUR_START, REVERSION_HOUR_END):
        df_m5 = get_rates(symbol, mt5.TIMEFRAME_M5, 120)
        if df_m5 is not None and len(df_m5) > 2:
            closed = df_m5.iloc[:-1]
            bar_time = closed["time"].iloc[-1]
            if last_bars.get((name, "m5")) != bar_time:
                last_bars[(name, "m5")] = bar_time
                sig = apply_macro_filter(reversion_signal(closed),
                                         macro.vix(now), vf)
                if sig and not has_open_position(symbol, mr):
                    log.info("[%s] REVERSION signal %s", name, sig)
                    open_trade(symbol, sig, mr, "sentinel_reversion")


def run_cycle(active: dict, guard: DayGuard, macro: MacroFilter,
              last_bars: dict, now: datetime | None = None):
    """One loop pass: global circuit breaker then portfolio scan."""
    now = now or datetime.now(timezone.utc)
    acc = mt5.account_info()
    if acc is None:
        raise ConnectionError(f"account_info() KO: {mt5.last_error()}")

    guard.roll_day(now, acc.balance)
    if guard.check(acc.equity):
        close_everything()
        return

    for name, cfg in active.items():
        scan_symbol(name, cfg, macro, last_bars, now)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    log.info("Starting SENTINEL multi-asset %s",
             list(CONFIG_PORTFOLIO))
    if not connect():
        return 1
    active = resolve_symbols()
    if not active:
        log.error("No portfolio asset available.")
        mt5.shutdown()
        return 1
    guard = DayGuard()
    macro = MacroFilter()
    last_bars: dict = {}
    while True:
        try:
            run_cycle(active, guard, macro, last_bars)
            write_heartbeat()
        except ConnectionError as exc:
            log.error("Connection lost: %s - reconnecting...", exc)
            mt5.shutdown()
            time.sleep(5)
            if not connect():
                time.sleep(10)
        except Exception as exc:
            log.exception("Unexpected error: %s", exc)
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
