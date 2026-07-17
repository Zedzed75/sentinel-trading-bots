"""Backtest of the Sentinel strategies on the MT5 terminal history.

Replays the bots' EXACT rules (same default parameters) on historical
candles, in R-multiples (initial trade risk = 1R): the results are
independent of sizing and comparable across strategies.

Covered strategies:
- trend    : Donchian ENTRY/EXIT + 2xATR stop (sentinel_trend, H4);
- breakout : Asian range breakout, SL 1.5xATR, TP 2R, 50% partial
             + break-even at 1R, trading window (sentinel_bot, M30);
- statarb  : Brent/WTI cointegration, rolling OLS beta and z-score,
             entry |z|>=2 if ADF p<0.05, exits convergence/z-stop/
             time stop, 07-20h window (sentinel_alpha_compound, M15).
(M5 reversion requires a long M5 history the broker does not provide;
the VIX filter is not replayed - slightly optimistic results on gold
shorts.)

Usage (MT5 terminal open):
  python research/backtest_sentinel.py trend XAUUSD --days 730
  python research/backtest_sentinel.py breakout XAUUSD --days 365
  python research/backtest_sentinel.py trend XAUUSD --grid
  python research/backtest_sentinel.py statarb            # XBRUSD-XTIUSD
  python research/backtest_sentinel.py statarb XBRUSD-XTIUSD --grid

--grid compares parameter variants with an anti-overfitting guard: the
stats are also computed on each half of the sample; a variant is only
robust if both halves are consistent. The engine is pure:
tests/test_backtest_sentinel.py.
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
from statsmodels.tsa.stattools import adfuller

# candles per day
TF_MAP = {"trend": ("H4", 6), "breakout": ("M30", 48), "statarb": ("M15", 96)}

# default legs and broker fallbacks (same as sentinel_alpha_compound)
STATARB_LEGS = ("XBRUSD", "XTIUSD")
STATARB_FALLBACKS = {"XBRUSD": ("SpotBrent", "UKOIL"),
                     "XTIUSD": ("SpotCrude", "USOIL")}


# ----------------------------------------------------------------------------
# Indicators (identical to the bots)
# ----------------------------------------------------------------------------
def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - prev_close).abs(),
                    (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


# ----------------------------------------------------------------------------
# Trend engine: Donchian entry/exit + ATR stop (sentinel_trend rules)
# ----------------------------------------------------------------------------
def backtest_trend(df: pd.DataFrame, entry_ch: int = 55, exit_ch: int = 20,
                   atr_mult: float = 2.0) -> list[dict]:
    a = atr(df).to_numpy()
    hi_e = df["high"].rolling(entry_ch).max().shift(1).to_numpy()
    lo_e = df["low"].rolling(entry_ch).min().shift(1).to_numpy()
    hi_x = df["high"].rolling(exit_ch).max().shift(1).to_numpy()
    lo_x = df["low"].rolling(exit_ch).min().shift(1).to_numpy()
    close = df["close"].to_numpy()
    high, low = df["high"].to_numpy(), df["low"].to_numpy()
    times = list(df["time"])

    trades, pos = [], None
    for i in range(entry_ch + 1, len(df)):
        if pos is not None:
            d, e, r = pos["dir"], pos["entry"], pos["risk"]
            exit_px = None
            if d == 1 and low[i] <= pos["stop"]:
                exit_px = pos["stop"]
            elif d == -1 and high[i] >= pos["stop"]:
                exit_px = pos["stop"]
            elif d == 1 and close[i] < lo_x[i]:
                exit_px = close[i]
            elif d == -1 and close[i] > hi_x[i]:
                exit_px = close[i]
            if exit_px is not None:
                trades.append({"time": times[i], "dir": d,
                               "r": d * (exit_px - e) / r,
                               "bars": i - pos["i"]})
                pos = None
        if pos is None and not np.isnan(a[i]) and a[i] > 0:
            if close[i] > hi_e[i]:
                pos = {"dir": 1, "entry": close[i], "risk": atr_mult * a[i],
                       "stop": close[i] - atr_mult * a[i], "i": i}
            elif close[i] < lo_e[i]:
                pos = {"dir": -1, "entry": close[i], "risk": atr_mult * a[i],
                       "stop": close[i] + atr_mult * a[i], "i": i}
    return trades


# ----------------------------------------------------------------------------
# Breakout engine: Asian range + partial/BE (sentinel_bot rules)
# ----------------------------------------------------------------------------
def _asian_range(df, t, asia_start, asia_end, cache):
    end = t.replace(hour=asia_end, minute=0, second=0, microsecond=0)
    if t < end:
        end -= timedelta(days=1)
    if end not in cache:
        start = end - timedelta(hours=(24 - asia_start) + asia_end)
        win = df[(df["time"] >= start) & (df["time"] < end)]
        cache[end] = (None, None) if win.empty else (
            float(win["high"].max()), float(win["low"].min()))
    return cache[end]


def backtest_breakout(df: pd.DataFrame, sl_mult: float = 1.5,
                      rr: float = 2.0, hour_start: int = 8,
                      hour_end: int = 16, asia_start: int = 22,
                      asia_end: int = 8) -> list[dict]:
    """One R per trade: -1 (full stop), 0.5 (partial then BE),
    0.5 + rr/2 (partial then TP). Exits evaluated at any hour."""
    a = atr(df).to_numpy()
    close = df["close"].to_numpy()
    high, low = df["high"].to_numpy(), df["low"].to_numpy()
    times = list(df["time"])
    cache: dict = {}
    trades, pos = [], None

    for i in range(15, len(df)):
        t = times[i]
        if pos is not None:
            d, e, r = pos["dir"], pos["entry"], pos["risk"]
            hit_stop = (low[i] <= pos["stop"] if d == 1
                        else high[i] >= pos["stop"])
            hit_1r = (high[i] >= e + r if d == 1 else low[i] <= e - r)
            hit_tp = (high[i] >= e + rr * r if d == 1
                      else low[i] <= e - rr * r)
            if hit_stop and not pos["partial"]:
                trades.append({"time": t, "dir": d, "r": -1.0,
                               "bars": i - pos["i"]})
                pos = None
            elif hit_stop and pos["partial"]:      # break-even on the rest
                trades.append({"time": t, "dir": d, "r": 0.5,
                               "bars": i - pos["i"]})
                pos = None
            elif hit_tp:
                if not pos["partial"]:             # 1R then TP within the candle
                    pos["partial"] = True
                trades.append({"time": t, "dir": d, "r": 0.5 + rr / 2,
                               "bars": i - pos["i"]})
                pos = None
            elif hit_1r and not pos["partial"]:    # 50% partial + BE
                pos["partial"] = True
                pos["stop"] = e
            if pos is not None:
                continue

        if hour_start <= t.hour < hour_end and not np.isnan(a[i]) and a[i] > 0:
            hi, lo = _asian_range(df, t, asia_start, asia_end, cache)
            if hi is None:
                continue
            if close[i] > hi:
                pos = {"dir": 1, "entry": close[i], "risk": sl_mult * a[i],
                       "stop": close[i] - sl_mult * a[i], "partial": False,
                       "i": i}
            elif close[i] < lo:
                pos = {"dir": -1, "entry": close[i], "risk": sl_mult * a[i],
                       "stop": close[i] + sl_mult * a[i], "partial": False,
                       "i": i}
    return trades


# ----------------------------------------------------------------------------
# Stat-arb engine: aligned pair, rolling ADF (sentinel_alpha_compound
# rules). df: columns time, close_a, close_b.
# ----------------------------------------------------------------------------
def backtest_statarb(df: pd.DataFrame, entry_z: float = 2.0,
                     exit_z: float = 0.5, stop_z: float = 4.0,
                     max_bars: int = 48, lookback: int = 240,
                     zscore_window: int = 96, adf_pvalue_max: float = 0.05,
                     hour_start: int = 7, hour_end: int = 20) -> list[dict]:
    """1R = spread distance from entry to stop: (stop_z - entry_z) * entry
    sigma. Beta (OLS) and z-score recomputed on each candle over rolling
    windows, like the bot; the ADF is only evaluated on entry candidates
    (CPU cost). PnL freezes the entry beta (lots do not move while in
    position). Not replayed: hard per-leg SLs, orphan-leg purge, time
    stop on wall-clock hours (here in aligned candles).
    """
    a, b = df["close_a"].astype(float), df["close_b"].astype(float)
    # beta = cov/var (OLS slope); spread std dev via composed moments:
    # var(a - beta*b) = var_a + beta^2*var_b - 2*beta*cov_ab (same ddof)
    beta = (b.rolling(lookback).cov(a) / b.rolling(lookback).var()).to_numpy()
    m_a = a.rolling(zscore_window).mean().to_numpy()
    m_b = b.rolling(zscore_window).mean().to_numpy()
    v_a = a.rolling(zscore_window).var().to_numpy()
    v_b = b.rolling(zscore_window).var().to_numpy()
    c_ab = a.rolling(zscore_window).cov(b).to_numpy()
    av, bv = a.to_numpy(), b.to_numpy()
    times = list(df["time"])

    trades, pos = [], None
    for i in range(lookback, len(df)):
        bta = beta[i]
        if np.isnan(bta):
            continue
        var = v_a[i] + bta * bta * v_b[i] - 2 * bta * c_ab[i]
        if not var > 0:
            continue
        sigma = var ** 0.5
        z = ((av[i] - bta * bv[i]) - (m_a[i] - bta * m_b[i])) / sigma
        if pos is not None:
            reason = None
            if abs(z) <= exit_z:
                reason = "convergence"
            elif abs(z) >= stop_z:
                reason = "z_stop"
            elif i - pos["i"] >= max_bars:
                reason = "time_stop"
            if reason:
                pnl = pos["dir"] * ((av[i] - pos["a"])
                                    - pos["beta"] * (bv[i] - pos["b"]))
                trades.append({"time": times[i], "dir": pos["dir"],
                               "r": pnl / pos["risk"], "bars": i - pos["i"],
                               "reason": reason})
                pos = None
            continue                        # no entry on the exit candle
        if abs(z) < entry_z or not (hour_start <= times[i].hour < hour_end):
            continue
        win = slice(i - lookback + 1, i + 1)
        if adfuller(av[win] - bta * bv[win], autolag="AIC")[1] >= adf_pvalue_max:
            continue
        d = 1 if z < 0 else -1              # BUY_SPREAD if z <= -entry_z
        pos = {"dir": d, "a": av[i], "b": bv[i], "beta": bta,
               "risk": (stop_z - entry_z) * sigma, "i": i}
    return trades


# ----------------------------------------------------------------------------
# Statistics (in R)
# ----------------------------------------------------------------------------
def stats(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0, "wr": None, "pf": None, "exp": None,
                "total": 0.0, "max_dd": 0.0}
    rs = [t["r"] for t in trades]
    wins = sum(r for r in rs if r > 0)
    losses = -sum(r for r in rs if r < 0)
    cum = peak = dd = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return {"n": len(rs),
            "wr": round(sum(1 for r in rs if r > 0) / len(rs), 3),
            "pf": round(wins / losses, 2) if losses > 0 else None,
            "exp": round(sum(rs) / len(rs), 3),
            "total": round(sum(rs), 1), "max_dd": round(dd, 1)}


def split_halves(trades: list[dict]) -> tuple[dict, dict]:
    mid = len(trades) // 2
    return stats(trades[:mid]), stats(trades[mid:])


def fmt_stats(s: dict) -> str:
    if s["n"] == 0:
        return "no trade"
    return (f"n={s['n']:<4} WR={s['wr']:.0%} PF={s['pf'] or 'inf'} "
            f"exp={s['exp']:+.2f}R total={s['total']:+.1f}R "
            f"maxDD={s['max_dd']:.1f}R")


# ----------------------------------------------------------------------------
# MT5 data (server time converted to real UTC, like the bots)
# ----------------------------------------------------------------------------
def fetch(symbol: str, tf_name: str, days: int,
          fallbacks: tuple = ()) -> pd.DataFrame | None:
    import MetaTrader5 as mt5
    if not mt5.initialize(
            path="C:/Program Files/Pepperstone MetaTrader 5/terminal64.exe"):
        print("mt5.initialize() KO:", mt5.last_error())
        return None
    found = next((s for s in (symbol, symbol + ".p", *fallbacks)
                  if mt5.symbol_select(s, True)), None)
    if not found:
        print("Symbol not found:", symbol)
        return None
    tf = {"H4": mt5.TIMEFRAME_H4, "M30": mt5.TIMEFRAME_M30,
          "M15": mt5.TIMEFRAME_M15}[tf_name]
    per_day = {"H4": 6, "M30": 48, "M15": 96}[tf_name]
    rates = mt5.copy_rates_from_pos(found, tf, 0,
                                    min(days * per_day + 100, 99000))
    tick = mt5.symbol_info_tick(found)
    offset = 0.0
    if tick and tick.time:
        delta = tick.time - datetime.now(timezone.utc).timestamp()
        if abs(delta) < 13 * 3600:
            offset = round(delta * 2 / 3600) / 2
    mt5.shutdown()
    if rates is None or len(rates) == 0:
        print("No data for", found)
        return None
    df = pd.DataFrame(rates)
    df["time"] = (pd.to_datetime(df["time"], unit="s", utc=True)
                  - pd.Timedelta(hours=offset))
    print(f"{found} {tf_name}: {len(df)} candles "
          f"({df['time'].iloc[0]:%Y-%m-%d} -> {df['time'].iloc[-1]:%Y-%m-%d}),"
          f" server offset {offset:+.1f}h")
    return df


GRIDS = {
    "trend": [{"entry_ch": e, "exit_ch": x, "atr_mult": m}
              for e in (40, 55, 70) for x in (15, 20, 25) for m in (2.0,)],
    "breakout": [{"hour_start": hs, "hour_end": he, "sl_mult": sl}
                 for hs in (8, 10, 13) for he in (16, 18)
                 for sl in (1.0, 1.5, 2.0) if hs < he],
    "statarb": [{"entry_z": z, "max_bars": mb}
                for z in (1.5, 2.0, 2.5) for mb in (32, 48, 64)],
}
ENGINES = {"trend": backtest_trend, "breakout": backtest_breakout,
           "statarb": backtest_statarb}


def fetch_pair(spec: str | None, tf_name: str, days: int) -> pd.DataFrame | None:
    """Two legs aligned by merging on time (columns close_a/close_b)."""
    leg_a, _, leg_b = (spec or "").partition("-")
    if not leg_b:
        leg_a, leg_b = STATARB_LEGS
    frames = []
    for leg in (leg_a, leg_b):
        df = fetch(leg, tf_name, days, STATARB_FALLBACKS.get(leg, ()))
        if df is None:
            return None
        frames.append(df[["time", "close"]])
    merged = frames[0].merge(frames[1], on="time", suffixes=("_a", "_b"))
    print(f"pair {leg_a}/{leg_b}: {len(merged)} common candles")
    return merged


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("strategy", choices=("trend", "breakout", "statarb"))
    p.add_argument("symbol", nargs="?",
                   help="asset, or A-B pair for statarb "
                        "(default: XBRUSD-XTIUSD)")
    p.add_argument("--days", type=int, default=730)
    p.add_argument("--grid", action="store_true")
    args = p.parse_args(argv)
    if args.strategy != "statarb" and not args.symbol:
        p.error("symbol is required for trend and breakout")

    tf_name, _ = TF_MAP[args.strategy]
    if args.strategy == "statarb":
        df = fetch_pair(args.symbol, tf_name, args.days)
        args.symbol = args.symbol or "-".join(STATARB_LEGS)
    else:
        df = fetch(args.symbol, tf_name, args.days)
    if df is None:
        return 1
    engine = ENGINES[args.strategy]

    if not args.grid:
        trades = engine(df)
        h1, h2 = split_halves(trades)
        print(f"\n{args.strategy} {args.symbol} (production parameters)")
        print("  total  :", fmt_stats(stats(trades)))
        print("  half 1 :", fmt_stats(h1))
        print("  half 2 :", fmt_stats(h2))
        return 0

    print(f"\nGrid {args.strategy} {args.symbol} "
          "(robust = consistent halves)")
    for params in GRIDS[args.strategy]:
        trades = engine(df, **params)
        h1, h2 = split_halves(trades)
        label = " ".join(f"{k}={v}" for k, v in params.items())
        print(f"- {label}")
        print("    total  :", fmt_stats(stats(trades)))
        print("    half 1 :", fmt_stats(h1))
        print("    half 2 :", fmt_stats(h2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
