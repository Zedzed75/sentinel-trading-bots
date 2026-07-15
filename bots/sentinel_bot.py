"""SENTINEL - Bot de trading algorithmique MetaTrader 5 multi-actifs.

Portefeuille : XAUUSD, EURUSD, GBPUSD (cf. CONFIG_PORTFOLIO).
Strategies (appliquees a chaque actif) :
  A) Breakout de session asiatique (M30), filtre macro VIX.
  B) Mean Reversion Bollinger/RSI (M5) en phase de range.
Risque : 1.5% du solde par trade, SL = 1.5*ATR(14) M30, TP = 2*SL,
partiel 50% + break-even a 1R, coupe-circuit drawdown journalier 4%.
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

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
# Portefeuille : nom canonique -> magics + symboles de repli broker
# (Pepperstone Razor expose le suffixe .p ; le nom canonique est teste d'abord)
# vix_filter : True = bloquer les SELL si VIX > 25 (valeur refuge, or
# uniquement) ; False = shorts autorises en crise (paires forex vs USD)
# breakout : False = strategie A suspendue sur l'actif (la reversion
# continue). EURUSD et GBPUSD suspendus le 2026-07-15 : PF < 1 sur les
# deux moities du backtest, ~650 trades chacun (structurel, pas un
# parametre - docs/AMELIORATION_CONTINUE.md, section 5). Reevaluation
# trimestrielle prevue.
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

RISK_PCT = 0.015              # 1.5% du solde par trade
ATR_PERIOD = 14
ATR_SL_MULT = 1.5             # Distance_SL = 1.5 * ATR(14) M30
RR_RATIO = 2.0                # TP = 2 * Distance_SL
DAILY_DD_LIMIT = 0.04         # coupe-circuit a -4% d'equite vs balance du jour

# Fenetres d'entree par strategie (UTC reel). Le breakout se joue des la
# fin de la plage asiatique (cassure fraiche a l'ouverture occidentale,
# l'edge documente des "opening range breakouts") jusqu'a la fin du
# recouvrement Londres/NY ; la reversion sur le recouvrement et l'apres-midi
# NY (calme propice au range). La gestion des positions n'est jamais bloquee.
BREAKOUT_HOUR_START = 8
BREAKOUT_HOUR_END = 16
REVERSION_HOUR_START = 13
REVERSION_HOUR_END = 18
FORCE_TRADING_HOURS = False    # TEMPORAIRE : bypass horaires pour test direct
                              # >>> remettre a False avant la production <<<
ASIA_HOUR_START = 22          # plage asiatique 22:00 -> 08:00 UTC
ASIA_HOUR_END = 8

BB_PERIOD = 20
BB_DEV = 2.0
RSI_PERIOD = 14
RSI_OVERSOLD = 20
RSI_OVERBOUGHT = 80
RANGE_LOOKBACK = 12           # bougies pour juger l'ecart-type "plat"
RANGE_FLAT_TOL = 0.25         # coef. de variation max de l'ecart-type

VIX_TICKER = "^VIX"
VIX_MAX_FOR_SELL = 25.0

DEVIATION = 20                # slippage max en points
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "sentinel_state.json")
RISK_SCALE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "risk_scale.json")

log = logging.getLogger("sentinel")


# ----------------------------------------------------------------------------
# Indicateurs (fonctions pures, testables)
# ----------------------------------------------------------------------------
def rsi(close: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    """RSI de Wilder."""
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.fillna(100.0).where(loss + gain > 0, 50.0)


def bollinger(close: pd.Series, period: int = BB_PERIOD, ndev: float = BB_DEV):
    """Retourne (bande sup, moyenne, bande inf)."""
    ma = close.rolling(period).mean()
    sd = close.rolling(period).std()
    return ma + ndev * sd, ma, ma - ndev * sd


def atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    """ATR de Wilder sur colonnes high/low/close."""
    prev_close = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - prev_close).abs(),
                    (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def is_flat_range(close: pd.Series, period: int = BB_PERIOD,
                  lookback: int = RANGE_LOOKBACK,
                  tol: float = RANGE_FLAT_TOL) -> bool:
    """Phase de range : ecart-type Bollinger plat ET moyenne mobile plate.

    Une tendance reguliere a aussi un ecart-type constant : on exige en plus
    que la moyenne mobile derive de moins d'un ecart-type sur le lookback.
    """
    sd = close.rolling(period).std().dropna()
    ma = close.rolling(period).mean().dropna()
    if len(sd) < lookback:
        return False
    recent = sd.iloc[-lookback:]
    mean = recent.mean()
    if mean <= 0:
        return False
    sd_flat = bool(recent.std() / mean < tol)
    ma_flat = bool(abs(ma.iloc[-1] - ma.iloc[-lookback]) < mean)
    return sd_flat and ma_flat


# ----------------------------------------------------------------------------
# Logique de signaux (fonctions pures, testables)
# ----------------------------------------------------------------------------
def price_fmt(symbol: str) -> str:
    """Format d'affichage des prix : 2 decimales pour l'or, 5 pour le forex."""
    return "%.2f" if "XAU" in symbol.upper() else "%.5f"


def fp(symbol: str, value: float | None) -> str:
    """Prix formate pour les logs selon la precision de l'actif."""
    return "n/a" if value is None else price_fmt(symbol) % value


def in_trading_hours(now: datetime, start: int, end: int) -> bool:
    """Nouvelles positions uniquement dans [start, end) UTC."""
    if FORCE_TRADING_HOURS:  # bypass temporaire pour test en direct
        return True
    return start <= now.hour < end


def asian_range(df_m30: pd.DataFrame, now: datetime):
    """(high, low) de la plage 22:00 -> 08:00 UTC la plus recente terminee.

    df_m30['time'] doit etre en datetime UTC (ouverture de bougie).
    Retourne (None, None) si aucune bougie dans la fenetre.
    """
    end = now.replace(hour=ASIA_HOUR_END, minute=0, second=0, microsecond=0)
    if now < end:
        end -= timedelta(days=1)
    start = end - timedelta(hours=(24 - ASIA_HOUR_START) + ASIA_HOUR_END)
    win = df_m30[(df_m30["time"] >= start) & (df_m30["time"] < end)]
    if win.empty:
        return None, None
    return float(win["high"].max()), float(win["low"].min())


def breakout_signal(df_m30: pd.DataFrame, asia_high: float,
                    asia_low: float) -> str | None:
    """BUY si cloture M30 > High asiatique, SELL si < Low asiatique."""
    if asia_high is None or asia_low is None or len(df_m30) < 1:
        return None
    close = float(df_m30["close"].iloc[-1])
    if close > asia_high:
        return "BUY"
    if close < asia_low:
        return "SELL"
    return None


def reversion_signal(df_m5: pd.DataFrame) -> str | None:
    """Mean reversion M5 : excursion hors bande + RSI extreme, puis retour."""
    if len(df_m5) < BB_PERIOD + RANGE_LOOKBACK + 2:
        return None
    close = df_m5["close"]
    if not is_flat_range(close):
        return None
    upper, _, lower = bollinger(close)
    r = rsi(close)
    c_prev, c_cur = float(close.iloc[-2]), float(close.iloc[-1])
    if (c_prev < float(lower.iloc[-2]) and float(r.iloc[-2]) < RSI_OVERSOLD
            and c_cur > float(lower.iloc[-1])):
        return "BUY"
    if (c_prev > float(upper.iloc[-2]) and float(r.iloc[-2]) > RSI_OVERBOUGHT
            and c_cur < float(upper.iloc[-1])):
        return "SELL"
    return None


def apply_macro_filter(signal: str | None, vix: float | None,
                       vix_filter: bool = True) -> str | None:
    """Si l'actif a vix_filter : VIX > 25 (ou inconnu) interdit les SELL
    (valeur refuge). Sans vix_filter, le signal passe tel quel."""
    if not vix_filter:
        return signal
    if signal == "SELL" and (vix is None or vix > VIX_MAX_FOR_SELL):
        log.info("Signal SELL bloque par filtre macro (VIX=%s)", vix)
        return None
    return signal


def reached_one_r(pos_type: int, price_open: float, sl: float,
                  current: float) -> bool:
    """True si la position a atteint un profit de 1R (distance du SL initial)."""
    risk = abs(price_open - sl)
    if risk <= 0:
        return False
    if pos_type == mt5.POSITION_TYPE_BUY:
        return current >= price_open + risk
    return current <= price_open - risk


def save_json_atomic(path: str, payload: dict):
    """Temporaire + os.replace : l'etat precedent survit a un crash."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)


HEARTBEAT_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "logs", "sentinel_bot.hb")


def write_heartbeat(path: str = HEARTBEAT_FILE,
                    now: datetime | None = None):
    """Estampille de vie apres chaque cycle reussi (lue par le watchdog) :
    un processus vivant mais gele (reconnexion sans fin) sera relance."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write((now or datetime.now(timezone.utc)).isoformat())
    except OSError:
        pass


def read_risk_scale(path: str | None = None) -> float:
    """Facteur [0,1] ecrit par l'orchestrateur de risque ; 1.0 par defaut."""
    try:
        with open(path or RISK_SCALE_FILE, encoding="utf-8") as fh:
            return min(1.0, max(0.0, float(json.load(fh)["scale"])))
    except (OSError, ValueError, KeyError):
        return 1.0


def compute_lot(balance: float, sl_distance: float, tick_size: float,
                tick_value: float, vol_min: float, vol_max: float,
                vol_step: float, scale: float = 1.0) -> float:
    """Volume risquant RISK_PCT du solde (x echelle globale de risque)."""
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
# Filtre macro VIX (yfinance, un fetch par jour)
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
            log.info("VIX du jour : %.2f", self._vix)
        except Exception as exc:
            log.warning("Echec recuperation VIX (%s) - SELL bloques.", exc)
            self._vix = None
            self._date = today
        return self._vix


# ----------------------------------------------------------------------------
# Coupe-circuit drawdown journalier
# ----------------------------------------------------------------------------
class DayGuard:
    """Balance de reference a 00:00 UTC, verrou si equite <= -4%."""

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
            log.warning("Echec sauvegarde etat : %s", exc)

    def roll_day(self, now: datetime, balance: float):
        """A appeler a chaque tick : reinitialise a chaque nouveau jour UTC."""
        today = now.date().isoformat()
        if self.day != today:
            self.day = today
            self.day_balance = balance
            self.locked = False
            self._save()
            log.info("Nouveau jour UTC %s - balance de reference %.2f",
                     today, balance)

    def check(self, equity: float) -> bool:
        """True si le coupe-circuit doit se declencher (ou est deja actif)."""
        if self.locked:
            return True
        if self.day_balance and equity <= self.day_balance * (1 - DAILY_DD_LIMIT):
            self.locked = True
            self._save()
            log.error("COUPE-CIRCUIT : equite %.2f <= -4%% de %.2f. "
                      "Bot verrouille jusqu'a 00:00 UTC.",
                      equity, self.day_balance)
            return True
        return False


# ----------------------------------------------------------------------------
# Acces MT5
# ----------------------------------------------------------------------------
def connect() -> bool:
    if not mt5.initialize(path="C:/Program Files/Pepperstone MetaTrader 5/terminal64.exe"):
        log.error("mt5.initialize() a echoue : %s", mt5.last_error())
        return False
    info = mt5.account_info()
    if info is None:
        log.error("account_info() a echoue : %s", mt5.last_error())
        return False
    log.info("Connecte MT5 - compte %s, balance %.2f %s",
             info.login, info.balance, info.currency)
    return True


def resolve_symbols() -> dict:
    """Valide chaque actif du portefeuille (nom canonique puis replis).

    Retourne {nom: {"symbol": symbole broker, "magic_breakout", "magic_reversion"}}.
    Un actif absent chez le broker est retire avec un WARNING, sans bloquer.
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
            log.info("Actif %s -> symbole broker %s%s", name, found,
                     "" if active[name]["breakout"]
                     else " (breakout suspendu)")
        else:
            log.warning("Actif %s indisponible chez le broker, retire du "
                        "portefeuille : %s", name, mt5.last_error())
    return active


_SERVER_OFFSET = {"hours": 0.0, "at": None}


def server_offset_hours(symbol: str, now: datetime | None = None) -> float:
    """Decalage (heures) entre l'horloge du serveur MT5 et l'UTC reel.

    Les bougies MT5 sont estampillees en heure serveur (UTC+2/+3 chez
    Pepperstone) : sans conversion, toutes les fenetres horaires (plage
    asiatique en tete) seraient decalees. On mesure l'ecart entre un tick
    recent et l'horloge locale UTC, arrondi a la demi-heure, memorise 1 h.
    Sans tick frais (week-end), la derniere valeur connue est conservee.
    """
    now = now or datetime.now(timezone.utc)
    cache = _SERVER_OFFSET
    if cache["at"] is not None and now - cache["at"] < timedelta(hours=1):
        return cache["hours"]
    ts = getattr(mt5.symbol_info_tick(symbol), "time", None)
    if isinstance(ts, (int, float)) and ts > 0:
        delta_h = (ts - now.timestamp()) / 3600
        if abs(delta_h) <= 13:            # tick frais, offset plausible
            cache["hours"] = round(delta_h * 2) / 2
            cache["at"] = now
    return cache["hours"]


def get_rates(symbol: str, timeframe: int, count: int) -> pd.DataFrame | None:
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
    if rates is None or len(rates) == 0:
        log.warning("copy_rates_from_pos vide (%s tf=%s) : %s",
                    symbol, timeframe, mt5.last_error())
        return None
    df = pd.DataFrame(rates)
    df["time"] = (pd.to_datetime(df["time"], unit="s", utc=True)
                  - pd.Timedelta(hours=server_offset_hours(symbol)))
    return df


def send_order(request: dict):
    result = mt5.order_send(request)
    if result is None:
        log.error("order_send None : %s", mt5.last_error())
        return None
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        log.error("Ordre refuse retcode=%s comment=%s",
                  result.retcode, getattr(result, "comment", ""))
        return None
    return result


def open_trade(symbol: str, direction: str, magic: int, tag: str) -> bool:
    """Ouvre un trade au marche avec SL/TP obligatoires et lot dynamique."""
    acc = mt5.account_info()
    sym = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    df_m30 = get_rates(symbol, mt5.TIMEFRAME_M30, 100)
    if acc is None or sym is None or tick is None or df_m30 is None:
        log.error("Donnees indisponibles pour ouvrir un trade : %s",
                  mt5.last_error())
        return False
    sl_dist = ATR_SL_MULT * float(atr(df_m30).iloc[-1])
    if sl_dist <= 0:
        log.warning("ATR nul, trade ignore.")
        return False
    lot = compute_lot(acc.balance, sl_dist, sym.trade_tick_size,
                      sym.trade_tick_value, sym.volume_min,
                      sym.volume_max, sym.volume_step, read_risk_scale())
    if lot <= 0:
        log.warning("Lot calcule nul (solde %.2f, SL %.2f), trade ignore.",
                    acc.balance, sl_dist)
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
    """Cloture totale ou partielle d'une position par ordre inverse."""
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
    """A 1R : cloture 50% + break-even, strictement par symbole ET magic."""
    for pos in mt5.positions_get(symbol=symbol) or []:
        if pos.symbol != symbol or pos.magic not in magics:
            continue
        if pos.sl == pos.price_open:      # deja passe en break-even
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
                    log.info("Position %s : 50%% cloture a 1R (%.2f lots).",
                             pos.ticket, half)
            if move_sl_to_breakeven(pos):
                log.info("Position %s : SL deplace au break-even %s.",
                         pos.ticket, fp(symbol, pos.price_open))


def close_everything():
    """Coupe-circuit GLOBAL : ferme tout, tous symboles, annule tout ordre."""
    for pos in mt5.positions_get() or []:
        if close_position(pos):
            log.info("Coupe-circuit : position %s (%s) fermee.",
                     pos.ticket, pos.symbol)
    for order in mt5.orders_get() or []:
        send_order({"action": mt5.TRADE_ACTION_REMOVE, "order": order.ticket})
        log.info("Coupe-circuit : ordre en attente %s annule.", order.ticket)


# ----------------------------------------------------------------------------
# Boucle principale
# ----------------------------------------------------------------------------
def scan_symbol(name: str, cfg: dict, macro: MacroFilter,
                last_bars: dict, now: datetime):
    """Gestion active + detection de signaux pour un actif du portefeuille."""
    symbol, mb, mr = cfg["symbol"], cfg["magic_breakout"], cfg["magic_reversion"]
    vf = cfg.get("vix_filter", True)
    manage_positions(symbol, (mb, mr))

    # --- Strategie A : breakout M30 (sur nouvelle bougie cloturee) ---
    if (cfg.get("breakout", True)
            and in_trading_hours(now, BREAKOUT_HOUR_START,
                                 BREAKOUT_HOUR_END)):
        df_m30 = get_rates(symbol, mt5.TIMEFRAME_M30, 96)
        if df_m30 is not None and len(df_m30) > 2:
            closed = df_m30.iloc[:-1]  # derniere ligne = bougie en cours
            bar_time = closed["time"].iloc[-1]
            if last_bars.get((name, "m30")) != bar_time:
                last_bars[(name, "m30")] = bar_time
                hi, lo = asian_range(closed, now)
                sig = apply_macro_filter(breakout_signal(closed, hi, lo),
                                         macro.vix(now), vf)
                if sig and not has_open_position(symbol, mb):
                    log.info("[%s] Signal BREAKOUT %s (Asie H=%s L=%s)",
                             name, sig, fp(symbol, hi), fp(symbol, lo))
                    open_trade(symbol, sig, mb, "sentinel_breakout")

    # --- Strategie B : mean reversion M5 (sur nouvelle bougie cloturee) ---
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
                    log.info("[%s] Signal REVERSION %s", name, sig)
                    open_trade(symbol, sig, mr, "sentinel_reversion")


def run_cycle(active: dict, guard: DayGuard, macro: MacroFilter,
              last_bars: dict, now: datetime | None = None):
    """Un passage de boucle : coupe-circuit global puis scan du portefeuille."""
    now = now or datetime.now(timezone.utc)
    acc = mt5.account_info()
    if acc is None:
        raise ConnectionError(f"account_info() KO : {mt5.last_error()}")

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
    log.info("Demarrage SENTINEL multi-actifs %s",
             list(CONFIG_PORTFOLIO))
    if not connect():
        return 1
    active = resolve_symbols()
    if not active:
        log.error("Aucun actif du portefeuille disponible.")
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
            log.error("Connexion perdue : %s - reconnexion...", exc)
            mt5.shutdown()
            time.sleep(5)
            if not connect():
                time.sleep(10)
        except Exception as exc:
            log.exception("Erreur inattendue : %s", exc)
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
