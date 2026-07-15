"""SENTINEL - Fonctions pures de strategie du bot 1 (sentinel_bot.py).

Indicateurs, fenetres horaires et logique de signaux : aucun acces MT5 ni
reseau, tout est testable directement (tests/test_sentinel_signals.py) et
reutilisable par la recherche (research/backtest_sentinel.py).
"""

import logging
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

# ----------------------------------------------------------------------------
# Parametres de strategie
# ----------------------------------------------------------------------------
# Fenetres d'entree par strategie (UTC reel). Le breakout se joue des la
# fin de la plage asiatique (cassure fraiche a l'ouverture occidentale,
# l'edge documente des "opening range breakouts") jusqu'a la fin du
# recouvrement Londres/NY ; la reversion sur le recouvrement et l'apres-midi
# NY (calme propice au range). La gestion des positions n'est jamais bloquee.
BREAKOUT_HOUR_START = 8
BREAKOUT_HOUR_END = 16
REVERSION_HOUR_START = 13
REVERSION_HOUR_END = 18
FORCE_TRADING_HOURS = False   # bypass horaires pour test en direct uniquement
ASIA_HOUR_START = 22          # plage asiatique 22:00 -> 08:00 UTC
ASIA_HOUR_END = 8

ATR_PERIOD = 14
BB_PERIOD = 20
BB_DEV = 2.0
RSI_PERIOD = 14
RSI_OVERSOLD = 20
RSI_OVERBOUGHT = 80
RANGE_LOOKBACK = 12           # bougies pour juger l'ecart-type "plat"
RANGE_FLAT_TOL = 0.25         # coef. de variation max de l'ecart-type

VIX_MAX_FOR_SELL = 25.0

log = logging.getLogger("sentinel")


# ----------------------------------------------------------------------------
# Indicateurs
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
# Fenetres horaires et logique de signaux
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
