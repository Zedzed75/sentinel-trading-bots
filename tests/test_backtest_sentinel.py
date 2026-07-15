"""Tests du moteur de backtest (donnees synthetiques, sans MT5).

Executer :  python -m unittest test_backtest_sentinel -v
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

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
        # plat, cassure haussiere, montee, puis chute sous le canal de sortie
        closes = [100.0] * 60 + [103.0] + [104.0 + i for i in range(10)] \
            + [95.0]
        trades = bt.backtest_trend(make_df(closes), entry_ch=55, exit_ch=5,
                                   atr_mult=2.0)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["dir"], 1)
        self.assertLess(trades[0]["r"], 0)     # sortie 95 sous l'entree 103

    def test_stop_intrabar_gives_minus_one_r(self):
        # cassure puis bougie qui touche le stop : perte exactement -1R
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
        """Bougies M30 d'une nuit asiatique 22h->08h plate a 100 puis
        la journee du 2026-01-<day+1> aux prix donnes {heure: close}."""
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
        df = self._day({9: 102.0, 10: 102.0})     # cassure a 09h (fenetre 8-16)
        trades = bt.backtest_breakout(df, hour_start=8, hour_end=16)
        self.assertGreaterEqual(len(trades), 1)
        self.assertEqual(trades[0]["dir"], 1)

    def test_breakout_outside_window_ignored(self):
        df = self._day({19: 102.0, 20: 102.0})    # cassure a 19h seulement
        trades = bt.backtest_breakout(df, hour_start=8, hour_end=16)
        self.assertEqual(trades, [])

    def test_full_stop_is_minus_one_r(self):
        # cassure a 9h puis effondrement : stop plein a -1R
        df = self._day({9: 102.0, 10: 80.0, 11: 80.0})
        trades = bt.backtest_breakout(df, hour_start=8, hour_end=16)
        self.assertEqual(trades[0]["r"], -1.0)

    def test_partial_then_breakeven_gives_half_r(self):
        # 9h : cassure ; 10h : > 1R (partiel+BE) ; 11h : retour a l'entree
        df = self._day({9: 102.0, 10: 104.0, 11: 101.0, 12: 101.0})
        trades = bt.backtest_breakout(df, hour_start=8, hour_end=16,
                                      sl_mult=1.5)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["r"], 0.5)


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
