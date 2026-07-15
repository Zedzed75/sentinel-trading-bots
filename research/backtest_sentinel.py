"""Backtest des strategies Sentinel sur l'historique du terminal MT5.

Rejoue les regles EXACTES des bots (memes parametres par defaut) sur les
bougies historiques, en R-multiples (risque initial du trade = 1R) : les
resultats sont independants du sizing et comparables entre strategies.

Strategies couvertes :
- trend    : Donchian ENTRY/EXIT + stop 2xATR (sentinel_trend, H4) ;
- breakout : cassure de plage asiatique, SL 1.5xATR, TP 2R, partiel 50%
             + break-even a 1R, fenetre horaire (sentinel_bot, M30).
(La reversion M5 exige un historique M5 long que le broker ne fournit
pas ; le filtre VIX n'est pas rejoue - resultats legerement optimistes
sur les shorts or.)

Usage (terminal MT5 ouvert) :
  python research/backtest_sentinel.py trend XAUUSD --days 730
  python research/backtest_sentinel.py breakout XAUUSD --days 365
  python research/backtest_sentinel.py trend XAUUSD --grid

--grid compare des variantes de parametres avec un garde-fou anti
sur-ajustement : les stats sont aussi calculees sur chaque moitie de
l'echantillon ; une variante n'est robuste que si les deux moitiees
sont coherentes. Le moteur est pur : tests/test_backtest_sentinel.py.
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

TF_MAP = {"trend": ("H4", 6), "breakout": ("M30", 48)}   # bougies par jour


# ----------------------------------------------------------------------------
# Indicateurs (identiques aux bots)
# ----------------------------------------------------------------------------
def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - prev_close).abs(),
                    (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


# ----------------------------------------------------------------------------
# Moteur trend : Donchian entry/exit + stop ATR (regles de sentinel_trend)
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
# Moteur breakout : plage asiatique + partiel/BE (regles de sentinel_bot)
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
    """Un R par trade : -1 (stop plein), 0.5 (partiel puis BE),
    0.5 + rr/2 (partiel puis TP). Sorties evaluees a toute heure."""
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
            elif hit_stop and pos["partial"]:      # break-even sur le solde
                trades.append({"time": t, "dir": d, "r": 0.5,
                               "bars": i - pos["i"]})
                pos = None
            elif hit_tp:
                if not pos["partial"]:             # 1R puis TP dans la bougie
                    pos["partial"] = True
                trades.append({"time": t, "dir": d, "r": 0.5 + rr / 2,
                               "bars": i - pos["i"]})
                pos = None
            elif hit_1r and not pos["partial"]:    # partiel 50% + BE
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
# Statistiques (en R)
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
        return "aucun trade"
    return (f"n={s['n']:<4} WR={s['wr']:.0%} PF={s['pf'] or 'inf'} "
            f"exp={s['exp']:+.2f}R total={s['total']:+.1f}R "
            f"maxDD={s['max_dd']:.1f}R")


# ----------------------------------------------------------------------------
# Donnees MT5 (heure serveur convertie en UTC reel, comme les bots)
# ----------------------------------------------------------------------------
def fetch(symbol: str, tf_name: str, days: int) -> pd.DataFrame | None:
    import MetaTrader5 as mt5
    if not mt5.initialize(
            path="C:/Program Files/Pepperstone MetaTrader 5/terminal64.exe"):
        print("mt5.initialize() KO :", mt5.last_error())
        return None
    found = next((s for s in (symbol, symbol + ".p")
                  if mt5.symbol_select(s, True)), None)
    if not found:
        print("Symbole introuvable :", symbol)
        return None
    tf = {"H4": mt5.TIMEFRAME_H4, "M30": mt5.TIMEFRAME_M30}[tf_name]
    per_day = {"H4": 6, "M30": 48}[tf_name]
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
        print("Pas de donnees pour", found)
        return None
    df = pd.DataFrame(rates)
    df["time"] = (pd.to_datetime(df["time"], unit="s", utc=True)
                  - pd.Timedelta(hours=offset))
    print(f"{found} {tf_name} : {len(df)} bougies "
          f"({df['time'].iloc[0]:%Y-%m-%d} -> {df['time'].iloc[-1]:%Y-%m-%d}),"
          f" offset serveur {offset:+.1f}h")
    return df


GRIDS = {
    "trend": [{"entry_ch": e, "exit_ch": x, "atr_mult": m}
              for e in (40, 55, 70) for x in (15, 20, 25) for m in (2.0,)],
    "breakout": [{"hour_start": hs, "hour_end": he, "sl_mult": sl}
                 for hs in (8, 10, 13) for he in (16, 18)
                 for sl in (1.0, 1.5, 2.0) if hs < he],
}
ENGINES = {"trend": backtest_trend, "breakout": backtest_breakout}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("strategy", choices=("trend", "breakout"))
    p.add_argument("symbol")
    p.add_argument("--days", type=int, default=730)
    p.add_argument("--grid", action="store_true")
    args = p.parse_args(argv)

    tf_name, _ = TF_MAP[args.strategy]
    df = fetch(args.symbol, tf_name, args.days)
    if df is None:
        return 1
    engine = ENGINES[args.strategy]

    if not args.grid:
        trades = engine(df)
        h1, h2 = split_halves(trades)
        print(f"\n{args.strategy} {args.symbol} (parametres production)")
        print("  total    :", fmt_stats(stats(trades)))
        print("  moitie 1 :", fmt_stats(h1))
        print("  moitie 2 :", fmt_stats(h2))
        return 0

    print(f"\nGrille {args.strategy} {args.symbol} "
          "(robuste = moities coherentes)")
    for params in GRIDS[args.strategy]:
        trades = engine(df, **params)
        h1, h2 = split_halves(trades)
        label = " ".join(f"{k}={v}" for k, v in params.items())
        print(f"- {label}")
        print("    total    :", fmt_stats(stats(trades)))
        print("    moitie 1 :", fmt_stats(h1))
        print("    moitie 2 :", fmt_stats(h2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
