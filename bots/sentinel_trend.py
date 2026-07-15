"""SENTINEL TREND - Bot MT5 de suivi de tendance (Time-Series Momentum).

Strategie type Turtle/CTA (Moskowitz-Ooi-Pedersen 2012, Hurst-Ooi-Pedersen
2017) : entree sur cassure du canal Donchian 55 bougies H4, stop initial a
2xATR(14), sortie sur cassure du canal oppose 20 bougies (trailing lent).
Asymetrie positive : beaucoup de petites pertes, gains rares mais larges.

Risque : 1% de l'equite par trade (dimensionnement normalise par l'ATR,
donc vol-targeting implicite), module par le facteur d'echelle global
ecrit par l'orchestrateur de risque (risk_scale.json).

Securite : verrou permanent si l'equite perd 15% depuis son pic historique.
Pas de fenetre horaire : les tendances se tiennent des semaines.
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
# Portefeuille diversifie multi-classes : nom canonique -> magic + replis.
# risk_mult : facteur applique au risque par trade de l'actif. 0.5 sur
# EURUSD/GBPUSD/XTIUSD depuis le 2026-07-15 : backtest 2 ans negatif sur
# toutes les variantes et les deux moities (docs/AMELIORATION_CONTINUE.md,
# section 5) ; reevaluation prevue a 30 trades reels par actif.
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

ENTRY_CHANNEL = 55            # Donchian d'entree (Turtle System 2)
EXIT_CHANNEL = 20             # Donchian de sortie (trailing lent)
ATR_PERIOD = 14
ATR_STOP_MULT = 2.0           # stop initial = 2 x ATR(14) H4
RISK_PCT = 0.01               # 1% de l'equite par trade
MAX_HISTO_DD = 0.15           # verrou permanent a -15% du pic d'equite

# Pas de fenetre de session : le momentum H4 est insensible a l'heure et
# un filtre horaire serait du sur-ajustement. En revanche, aucune OUVERTURE
# pendant le rollover quotidien (spreads elargis, swaps) : une cassure
# detectee dans cette plage est reevaluee des la sortie du blackout.
# Les sorties (canal oppose, stops) restent permises 24h/24.
ROLLOVER_HOUR_START = 21      # blackout d'ouverture 21:00-23:00 UTC
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
    """Facteur [0,1] ecrit par l'orchestrateur de risque ; 1.0 par defaut."""
    try:
        with open(path, encoding="utf-8") as fh:
            return min(1.0, max(0.0, float(json.load(fh)["scale"])))
    except (OSError, ValueError, KeyError):
        return 1.0


# ----------------------------------------------------------------------------
# Indicateurs et signaux (fonctions pures, testables)
# ----------------------------------------------------------------------------
def atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    prev = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(),
                    (df["low"] - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def donchian(df: pd.DataFrame, n: int) -> tuple[float, float]:
    """(plus haut, plus bas) des n bougies PRECEDANT la bougie de signal."""
    win = df.iloc[-(n + 1):-1]
    return float(win["high"].max()), float(win["low"].min())


def entry_signal(df: pd.DataFrame, n: int = ENTRY_CHANNEL) -> str | None:
    """BUY si la cloture casse le haut du canal n, SELL si elle casse le bas."""
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
    """Sortie de tendance : cloture au-dela du canal n oppose a la position."""
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
    """Volume risquant RISK_PCT de l'equite (x facteur d'echelle global)."""
    if sl_distance <= 0 or tick_size <= 0 or tick_value <= 0:
        return 0.0
    loss_per_lot = (sl_distance / tick_size) * tick_value
    lots = (equity * RISK_PCT * scale) / loss_per_lot
    lots = round(np.floor(lots / vol_step) * vol_step, 8)
    if lots < vol_min:
        return 0.0
    return float(min(lots, vol_max))


# ----------------------------------------------------------------------------
# Verrou de drawdown historique
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
            log.warning("Echec sauvegarde etat : %s", exc)

    def check(self, equity: float) -> bool:
        if self.locked:
            return True
        if equity > self.peak:
            self.peak = equity
            self._save()
        elif self.peak > 0 and equity <= self.peak * (1 - MAX_HISTO_DD):
            self.locked = True
            self._save()
            log.critical("DRAWDOWN MAX TREND : equite %.2f <= -%.0f%% du pic "
                         "%.2f. Verrou permanent.", equity,
                         MAX_HISTO_DD * 100, self.peak)
            return True
        return False


# ----------------------------------------------------------------------------
# Acces MT5
# ----------------------------------------------------------------------------
def resolve_symbols() -> dict:
    """{nom: {"symbol", "magic"}} pour les actifs disponibles chez le broker."""
    active = {}
    for name, cfg in TREND_PORTFOLIO.items():
        found = next((s for s in [name] + cfg["fallback"]
                      if mt5.symbol_select(s, True)
                      and mt5.symbol_info(s) is not None), None)
        if found:
            active[name] = {"symbol": found, "magic": cfg["magic"],
                            "risk_mult": cfg.get("risk_mult", 1.0)}
            log.info("Actif %s -> %s (risque x%.1f)", name, found,
                     active[name]["risk_mult"])
        else:
            log.warning("Actif %s indisponible, retire : %s", name,
                        mt5.last_error())
    return active


def get_rates(symbol: str, timeframe: int, count: int) -> pd.DataFrame | None:
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
    if rates is None or len(rates) == 0:
        log.warning("Pas de donnees %s : %s", symbol, mt5.last_error())
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df


def send_order(request: dict):
    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        log.error("Ordre refuse : %s / %s",
                  getattr(result, "retcode", None), mt5.last_error())
        return None
    return result


def open_trend_trade(symbol: str, direction: str, magic: int,
                     df: pd.DataFrame, risk_mult: float = 1.0) -> bool:
    """Entree au marche, SL dur a 2xATR, sans TP (sortie par canal)."""
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
        log.warning("[%s] lot nul, entree ignoree.", symbol)
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
# Boucle principale
# ----------------------------------------------------------------------------
def save_json_atomic(path: str, payload: dict):
    """Temporaire + os.replace : l'etat precedent survit a un crash."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)


HEARTBEAT_FILE = os.path.join(os.path.dirname(_DIR), "logs",
                              "sentinel_trend.hb")


def write_heartbeat(path: str = HEARTBEAT_FILE,
                    now: datetime | None = None):
    """Estampille de vie apres chaque cycle reussi (lue par le watchdog)."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write((now or datetime.now(timezone.utc)).isoformat())
    except OSError:
        pass


def entry_allowed(now: datetime) -> bool:
    """False pendant le blackout de rollover (ouvertures uniquement)."""
    return not (ROLLOVER_HOUR_START <= now.hour < ROLLOVER_HOUR_END)


def scan_symbol(name: str, cfg: dict, timeframe: int, last_bars: dict,
                now: datetime | None = None):
    """Sur nouvelle bougie cloturee : gere la sortie ou cherche une entree."""
    now = now or datetime.now(timezone.utc)
    symbol, magic = cfg["symbol"], cfg["magic"]
    df = get_rates(symbol, timeframe, ENTRY_CHANNEL + 3)
    if df is None or len(df) < ENTRY_CHANNEL + 2:
        return
    closed = df.iloc[:-1]                 # derniere ligne = bougie en cours
    bar_time = closed["time"].iloc[-1]
    if last_bars.get(name) == bar_time:
        return
    last_bars[name] = bar_time

    open_pos = positions_for(symbol, magic)
    if open_pos:
        for pos in open_pos:
            if exit_signal(closed, pos.type):
                if close_position(pos):
                    log.info("[TREND] sortie canal %s ticket=%s profit=%.2f",
                             name, pos.ticket, pos.profit)
        return
    sig = entry_signal(closed)
    if sig:
        if not entry_allowed(now):
            last_bars.pop(name, None)   # reevaluer la bougie apres le blackout
            log.info("[TREND] cassure %s differee (rollover %02d-%02dh UTC)",
                     name, ROLLOVER_HOUR_START, ROLLOVER_HOUR_END)
            return
        log.info("[TREND] cassure Donchian %s %s", ENTRY_CHANNEL, name)
        open_trend_trade(symbol, sig, magic, closed,
                         cfg.get("risk_mult", 1.0))


def close_all_trend():
    for pos in mt5.positions_get() or []:
        if pos.magic in TREND_MAGICS and close_position(pos):
            log.info("Verrou : position %s (%s) fermee.", pos.ticket,
                     pos.symbol)


def run_cycle(active: dict, guard: PeakGuard, timeframe: int,
              last_bars: dict, now: datetime | None = None):
    now = now or datetime.now(timezone.utc)
    acc = mt5.account_info()
    if acc is None:
        raise ConnectionError(f"account_info() KO : {mt5.last_error()}")
    if guard.check(acc.equity):
        close_all_trend()
        return
    for name, cfg in active.items():
        scan_symbol(name, cfg, timeframe, last_bars, now)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    log.info("Demarrage SENTINEL TREND (Donchian %s/%s, H4)",
             ENTRY_CHANNEL, EXIT_CHANNEL)
    if not mt5.initialize(
            path="C:/Program Files/Pepperstone MetaTrader 5/terminal64.exe"):
        log.error("mt5.initialize() a echoue : %s", mt5.last_error())
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
            log.error("Connexion perdue : %s - reconnexion...", exc)
            mt5.shutdown()
            time.sleep(5)
            mt5.initialize()
        except Exception as exc:
            log.exception("Erreur inattendue : %s", exc)
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
