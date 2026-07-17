"""SENTINEL RISK ORCHESTRATOR - Risk supervisor of the bot portfolio.

It does not trade: it monitors the account and coordinates the Sentinel bots.

1. Volatility targeting (Moreira & Muir 2017): measures the realized
   volatility of equity (daily sample) and writes to risk_scale.json a
   [MIN_SCALE, 1] factor = target/realized that the bots apply to their
   position size.
2. Directional concentration: alert if too many Sentinel positions go in
   the same direction (the strategies become a single bet).
3. GLOBAL circuit breaker: if equity loses GLOBAL_MAX_DD from its
   historical peak, close all positions of the Sentinel magics (other
   EAs'/manual ones are untouched) and permanent lock - it keeps purging
   anything the bots might reopen.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone

import numpy as np
import MetaTrader5 as mt5

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
# Fleet magics: bot 1 (1001-3002), alpha (4001), trend (5001-5005)
SENTINEL_MAGICS = ({1001, 1002, 2001, 2002, 3001, 3002, 4001}
                   | set(range(5001, 5006)))

TARGET_VOL = 0.10             # target account volatility, annualized
VOL_WINDOW = 20               # days of returns for realized vol
MIN_SAMPLES = 5               # below that: neutral scale (1.0)
MIN_SCALE = 0.25              # factor floor (never cut to zero)
GLOBAL_MAX_DD = 0.10          # global lock at -10% of the equity peak
MAX_SAME_DIRECTION = 4        # directional concentration alert

DEVIATION = 20
_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(_DIR, "orchestrator_state.json")
RISK_SCALE_FILE = os.path.join(_DIR, "risk_scale.json")

log = logging.getLogger("orchestrator")


def save_json_atomic(path: str, payload: dict):
    """Temp file + os.replace: the previous state survives a crash."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)


HEARTBEAT_FILE = os.path.join(os.path.dirname(_DIR), "logs",
                              "sentinel_risk_orchestrator.hb")


def write_heartbeat(path: str = HEARTBEAT_FILE,
                    now: datetime | None = None):
    """Liveness timestamp after each successful cycle (read by the watchdog)."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write((now or datetime.now(timezone.utc)).isoformat())
    except OSError:
        pass


def vol_scale(realized_vol: float, target: float = TARGET_VOL) -> float:
    """Target/realized reduction factor, clamped to [MIN_SCALE, 1]."""
    if realized_vol <= 0:
        return 1.0
    return float(min(1.0, max(MIN_SCALE, target / realized_vol)))


def write_risk_scale(scale: float, path: str | None = None):
    try:
        save_json_atomic(path or RISK_SCALE_FILE,
                         {"scale": round(scale, 4),
                          "updated": datetime.now(timezone.utc).isoformat()})
    except OSError as exc:
        log.warning("risk_scale write failed: %s", exc)


# ----------------------------------------------------------------------------
# Equity tracking and realized volatility
# ----------------------------------------------------------------------------
class EquityMonitor:
    """One equity sample per UTC day, persisted; annualized vol."""

    def __init__(self, state_file: str = STATE_FILE):
        self.state_file = state_file
        self.history: list[dict] = []      # [{"day", "equity"}]
        self.peak = 0.0
        self.locked = False
        try:
            with open(state_file, encoding="utf-8") as fh:
                st = json.load(fh)
            self.history = st.get("history", [])
            self.peak = st.get("peak", 0.0)
            self.locked = st.get("locked", False)
        except (OSError, ValueError):
            pass

    def _save(self):
        try:
            save_json_atomic(self.state_file,
                             {"history": self.history[-90:],
                              "peak": self.peak, "locked": self.locked})
        except OSError as exc:
            log.warning("State save failed: %s", exc)

    def snapshot(self, now: datetime, equity: float):
        """Record today's equity (first pass of the UTC day)."""
        day = now.date().isoformat()
        if not self.history or self.history[-1]["day"] != day:
            self.history.append({"day": day, "equity": float(equity)})
            self._save()
            log.info("Equity snapshot %s: %.2f", day, equity)

    def realized_vol(self) -> float | None:
        """Annualized volatility of daily returns; None if too short."""
        eq = [h["equity"] for h in self.history[-(VOL_WINDOW + 1):]]
        if len(eq) < MIN_SAMPLES + 1:
            return None
        rets = np.diff(np.log(eq))
        return float(np.std(rets, ddof=1) * np.sqrt(252))

    def check_drawdown(self, equity: float) -> bool:
        """True if the global lock is (or becomes) active."""
        if self.locked:
            return True
        if equity > self.peak:
            self.peak = equity
            self._save()
        elif self.peak > 0 and equity <= self.peak * (1 - GLOBAL_MAX_DD):
            self.locked = True
            self._save()
            log.critical("GLOBAL LOCK: equity %.2f <= -%.0f%% of peak %.2f. "
                         "Closing the whole Sentinel fleet.",
                         equity, GLOBAL_MAX_DD * 100, self.peak)
            return True
        return False


# ----------------------------------------------------------------------------
# Fleet position monitoring
# ----------------------------------------------------------------------------
def sentinel_positions() -> list:
    return [p for p in (mt5.positions_get() or [])
            if p.magic in SENTINEL_MAGICS]


def direction_concentration(positions: list) -> tuple[int, int]:
    """(buy count, sell count) among the fleet's positions."""
    buys = sum(1 for p in positions if p.type == mt5.POSITION_TYPE_BUY)
    return buys, len(positions) - buys


def close_position(pos) -> bool:
    tick = mt5.symbol_info_tick(pos.symbol)
    if tick is None:
        return False
    buy = pos.type == mt5.POSITION_TYPE_SELL
    result = mt5.order_send({
        "action": mt5.TRADE_ACTION_DEAL, "symbol": pos.symbol,
        "volume": pos.volume,
        "type": mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL,
        "position": pos.ticket, "price": tick.ask if buy else tick.bid,
        "deviation": DEVIATION, "magic": pos.magic,
        "comment": "orchestrator_kill", "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC})
    return result is not None and result.retcode == mt5.TRADE_RETCODE_DONE


def kill_fleet():
    """Close all positions of the Sentinel magics (others are kept)."""
    for pos in sentinel_positions():
        if close_position(pos):
            log.warning("Position %s (%s, magic=%s) closed by the lock.",
                        pos.ticket, pos.symbol, pos.magic)


# ----------------------------------------------------------------------------
# Main loop
# ----------------------------------------------------------------------------
def run_cycle(monitor: EquityMonitor, now: datetime | None = None):
    now = now or datetime.now(timezone.utc)
    acc = mt5.account_info()
    if acc is None:
        raise ConnectionError(f"account_info() KO: {mt5.last_error()}")

    # 1. global lock: purge the fleet while it is active
    if monitor.check_drawdown(acc.equity):
        kill_fleet()
        write_risk_scale(MIN_SCALE)       # belt and braces
        return

    monitor.snapshot(now, acc.equity)

    # 2. volatility targeting -> shared scale factor
    rvol = monitor.realized_vol()
    scale = 1.0 if rvol is None else vol_scale(rvol)
    write_risk_scale(scale)
    if rvol is not None and scale < 1.0:
        log.info("Realized vol %.1f%% > target %.0f%% -> scale=%.2f",
                 rvol * 100, TARGET_VOL * 100, scale)

    # 3. directional concentration of the fleet
    buys, sells = direction_concentration(sentinel_positions())
    if max(buys, sells) >= MAX_SAME_DIRECTION:
        log.warning("CONCENTRATION: %s buys / %s sells Sentinel in the "
                    "same direction - the strategies are correlated.",
                    buys, sells)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    log.info("Starting SENTINEL RISK ORCHESTRATOR (vol target %.0f%%, "
             "global DD %.0f%%)", TARGET_VOL * 100, GLOBAL_MAX_DD * 100)
    if not mt5.initialize(
            path="C:/Program Files/Pepperstone MetaTrader 5/terminal64.exe"):
        log.error("mt5.initialize() failed: %s", mt5.last_error())
        return 1
    monitor = EquityMonitor()
    while True:
        try:
            run_cycle(monitor)
            write_heartbeat()
        except ConnectionError as exc:
            log.error("Connection lost: %s - reconnecting...", exc)
            mt5.shutdown()
            time.sleep(5)
            mt5.initialize()
        except Exception as exc:
            log.exception("Unexpected error: %s", exc)
        time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
