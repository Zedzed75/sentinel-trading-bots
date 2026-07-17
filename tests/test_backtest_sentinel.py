"""Backtest engine tests (synthetic data, no MT5).

Run:  python -m unittest test_backtest_sentinel -v
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "research"))
import backtest_sentinel as bt  # noqa: E402

UTC = timezone.utc


def make_df(closes, start=None, step_h=4, spread=0.5):
    start = start or datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
    rows = [{"time": start + timedelta(hours=step_h * i), "open": c,
             "high": c + spread, "low": c - spread, "close": c}
            for i, c in enumerate(closes)]
    return pd.DataFrame(rows)


class TestTrendEngine(unittest.TestCase):
    def test_long_entry_and_channel_exit_r(self):
        # flat, bullish breakout, climb, then drop below the exit channel
        closes = [100.0] * 60 + [103.0] + [104.0 + i for i in range(10)] \
            + [95.0]
        trades = bt.backtest_trend(make_df(closes), entry_ch=55, exit_ch=5,
                                   atr_mult=2.0)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["dir"], 1)
        self.assertLess(trades[0]["r"], 0)     # exit 95 below the 103 entry

    def test_stop_intrabar_gives_minus_one_r(self):
        # breakout then a candle that touches the stop: loss of exactly -1R
        closes = [100.0] * 60 + [103.0] + [80.0]
        trades = bt.backtest_trend(make_df(closes), entry_ch=55, exit_ch=5)
        self.assertEqual(len(trades), 1)
        self.assertAlmostEqual(trades[0]["r"], -1.0, places=6)

    def test_no_trade_without_breakout(self):
        self.assertEqual(bt.backtest_trend(make_df([100.0] * 80),
                                           entry_ch=55, exit_ch=20), [])


class TestBreakoutEngine(unittest.TestCase):
    @staticmethod
    def _day(prices_by_hour, day=6):
        """M30 candles of a flat Asian night 22h->08h at 100 then the
        2026-01-<day+1> day at the given prices {hour: close}."""
        rows = []
        t = datetime(2026, 1, day, 22, 0, tzinfo=UTC)
        while t.hour != 8 or t.minute != 0:
            rows.append({"time": t, "open": 100.0, "high": 100.5,
                         "low": 99.5, "close": 100.0})
            t += timedelta(minutes=30)
        end = t.replace(hour=21, minute=30)
        while t <= end:
            c = prices_by_hour.get(t.hour, 100.0)
            rows.append({"time": t, "open": c, "high": c + 0.6,
                         "low": c - 0.6, "close": c})
            t += timedelta(minutes=30)
        return pd.DataFrame(rows)

    def test_breakout_in_window_takes_trade(self):
        df = self._day({9: 102.0, 10: 102.0})     # breakout at 09h (window 8-16)
        trades = bt.backtest_breakout(df, hour_start=8, hour_end=16)
        self.assertGreaterEqual(len(trades), 1)
        self.assertEqual(trades[0]["dir"], 1)

    def test_breakout_outside_window_ignored(self):
        df = self._day({19: 102.0, 20: 102.0})    # breakout at 19h only
        trades = bt.backtest_breakout(df, hour_start=8, hour_end=16)
        self.assertEqual(trades, [])

    def test_full_stop_is_minus_one_r(self):
        # breakout at 9h then collapse: full stop at -1R
        df = self._day({9: 102.0, 10: 80.0, 11: 80.0})
        trades = bt.backtest_breakout(df, hour_start=8, hour_end=16)
        self.assertEqual(trades[0]["r"], -1.0)

    def test_partial_then_breakeven_gives_half_r(self):
        # 9h: breakout; 10h: > 1R (partial+BE); 11h: back to the entry
        df = self._day({9: 102.0, 10: 104.0, 11: 101.0, 12: 101.0})
        trades = bt.backtest_breakout(df, hour_start=8, hour_end=16,
                                      sl_mult=1.5)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["r"], 0.5)


class TestStatarbEngine(unittest.TestCase):
    """Synthetic cointegrated pair: a = 5 + 1.2*b + e (e stationary)."""

    @staticmethod
    def make_pair(e, seed=42):
        """Aligned M15 DataFrame; e = stationary spread noise series."""
        n = len(e)
        rng = np.random.default_rng(seed)
        b = 60 + np.cumsum(rng.normal(0, 0.2, n))
        a = 5 + 1.2 * b + np.asarray(e, dtype=float)
        times = pd.date_range("2026-01-05 00:00", periods=n, freq="15min",
                              tz="UTC")
        return pd.DataFrame({"time": times, "close_a": a, "close_b": b})

    @staticmethod
    def base_noise(n):
        """Uniform noise bounded +/-0.07 (sigma ~ 0.04): |z| of the noise
        alone stays < 2 (no spurious entry) and the ADF clearly rejects H0."""
        return np.random.default_rng(3).uniform(-0.07, 0.07, n)

    def test_buy_spread_converges_positive_r(self):
        # widening to ~ -3 sigma over 2 candles then mean reversion
        e = self.base_noise(360)
        e[300:302] = -0.12
        trades = bt.backtest_statarb(self.make_pair(e), hour_start=0,
                                     hour_end=24)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["dir"], 1)          # BUY_SPREAD (z < 0)
        self.assertEqual(trades[0]["reason"], "convergence")
        self.assertGreater(trades[0]["r"], 0)

    def test_z_stop_gives_negative_r(self):
        # entry at ~ -3 sigma then widening beyond 4 sigma
        # (a single widened candle: |z| falls back under 2 after the exit,
        # otherwise the engine re-enters, like the bot)
        e = self.base_noise(360)
        e[300] = -0.12
        e[301] = -0.30
        trades = bt.backtest_statarb(self.make_pair(e), hour_start=0,
                                     hour_end=24)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["reason"], "z_stop")
        self.assertLess(trades[0]["r"], 0)

    def test_time_stop_after_max_bars(self):
        # the spread stays wide without converging or exploding, then
        # reverts to the mean just after the time stop (no re-entry)
        e = self.base_noise(360)
        e[300:309] = -0.12
        trades = bt.backtest_statarb(self.make_pair(e), max_bars=8,
                                     hour_start=0, hour_end=24)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["reason"], "time_stop")
        self.assertEqual(trades[0]["bars"], 8)

    def test_no_entry_outside_hours(self):
        # same widening, but at 03:00 UTC: 07-20h window closed
        e = self.base_noise(360)
        e[300:302] = -0.12                  # bar 300 = +75h -> 03:00 UTC
        trades = bt.backtest_statarb(self.make_pair(e),
                                     hour_start=7, hour_end=20)
        self.assertEqual(trades, [])

    def test_no_entry_without_cointegration(self):
        # independent random walks: the ADF never validates the entry
        rng = np.random.default_rng(7)
        n = 360
        times = pd.date_range("2026-01-05 00:00", periods=n, freq="15min",
                              tz="UTC")
        df = pd.DataFrame({
            "time": times,
            "close_a": 100 + np.cumsum(rng.normal(0, 1, n)),
            "close_b": 60 + np.cumsum(rng.normal(0, 1, n))})
        trades = bt.backtest_statarb(df, hour_start=0, hour_end=24)
        self.assertEqual(trades, [])


class TestStats(unittest.TestCase):
    def test_known_values(self):
        trades = [{"r": r} for r in (1.5, -1.0, 1.5, -1.0)]
        s = bt.stats(trades)
        self.assertEqual(s["n"], 4)
        self.assertEqual(s["wr"], 0.5)
        self.assertEqual(s["pf"], 1.5)     # 3.0 / 2.0
        self.assertEqual(s["total"], 1.0)

    def test_empty(self):
        self.assertEqual(bt.stats([])["n"], 0)

    def test_split_halves(self):
        trades = [{"r": 1.0}] * 4 + [{"r": -1.0}] * 4
        h1, h2 = bt.split_halves(trades)
        self.assertEqual((h1["total"], h2["total"]), (4.0, -4.0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
