"""SENTINEL - Pure strategy functions of bot 1 (sentinel_bot.py).

Indicators, trading windows and signal logic: no MT5 or network access,
everything is directly testable (tests/test_sentinel_signals.py) and
reusable by research (research/backtest_sentinel.py).
"""

import logging
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

# ----------------------------------------------------------------------------
# Strategy parameters
# ----------------------------------------------------------------------------
# Entry windows per strategy (real UTC). The breakout plays from the end
# of the Asian range (fresh break at the Western open, the documented
# "opening range breakouts" edge) until the end of the London/NY overlap;
# reversion during the overlap and the NY afternoon (calm favourable to
# ranges). Position management is never blocked.
BREAKOUT_HOUR_START = 8
BREAKOUT_HOUR_END = 16
REVERSION_HOUR_START = 13
REVERSION_HOUR_END = 18
FORCE_TRADING_HOURS = False   # hours bypass for live testing only
ASIA_HOUR_START = 22          # Asian range 22:00 -> 08:00 UTC
ASIA_HOUR_END = 8

ATR_PERIOD = 14
BB_PERIOD = 20
BB_DEV = 2.0
RSI_PERIOD = 14
RSI_OVERSOLD = 20
RSI_OVERBOUGHT = 80
RANGE_LOOKBACK = 12           # candles to judge a "flat" std dev
RANGE_FLAT_TOL = 0.25         # max coefficient of variation of the std dev

VIX_MAX_FOR_SELL = 25.0

log = logging.getLogger("sentinel")


# ----------------------------------------------------------------------------
# Indicators
# ----------------------------------------------------------------------------
def rsi(close: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    """Wilder's RSI."""
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.fillna(100.0).where(loss + gain > 0, 50.0)


def bollinger(close: pd.Series, period: int = BB_PERIOD, ndev: float = BB_DEV):
    """Returns (upper band, mean, lower band)."""
    ma = close.rolling(period).mean()
    sd = close.rolling(period).std()
    return ma + ndev * sd, ma, ma - ndev * sd


def atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    """Wilder's ATR on high/low/close columns."""
    prev_close = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - prev_close).abs(),
                    (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def is_flat_range(close: pd.Series, period: int = BB_PERIOD,
                  lookback: int = RANGE_LOOKBACK,
                  tol: float = RANGE_FLAT_TOL) -> bool:
    """Range phase: flat Bollinger std dev AND flat moving average.

    A steady trend also has a constant std dev: we additionally require
    the moving average to drift less than one std dev over the lookback.
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
# Trading windows and signal logic
# ----------------------------------------------------------------------------
def price_fmt(symbol: str) -> str:
    """Price display format: 2 decimals for gold, 5 for forex."""
    return "%.2f" if "XAU" in symbol.upper() else "%.5f"


def fp(symbol: str, value: float | None) -> str:
    """Price formatted for logs according to the asset's precision."""
    return "n/a" if value is None else price_fmt(symbol) % value


def in_trading_hours(now: datetime, start: int, end: int) -> bool:
    """New positions only within [start, end) UTC."""
    if FORCE_TRADING_HOURS:  # temporary bypass for live testing
        return True
    return start <= now.hour < end


def asian_range(df_m30: pd.DataFrame, now: datetime):
    """(high, low) of the most recent completed 22:00 -> 08:00 UTC range.

    df_m30['time'] must be UTC datetime (candle open time).
    Returns (None, None) if no candle falls within the window.
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
    """BUY if M30 close > Asian High, SELL if < Asian Low."""
    if asia_high is None or asia_low is None or len(df_m30) < 1:
        return None
    close = float(df_m30["close"].iloc[-1])
    if close > asia_high:
        return "BUY"
    if close < asia_low:
        return "SELL"
    return None


def reversion_signal(df_m5: pd.DataFrame) -> str | None:
    """M5 mean reversion: excursion beyond the band + extreme RSI, then return."""
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
    """If the asset has vix_filter: VIX > 25 (or unknown) forbids SELLs
    (safe-haven asset). Without vix_filter, the signal passes as is."""
    if not vix_filter:
        return signal
    if signal == "SELL" and (vix is None or vix > VIX_MAX_FOR_SELL):
        log.info("SELL signal blocked by macro filter (VIX=%s)", vix)
        return None
    return signal
