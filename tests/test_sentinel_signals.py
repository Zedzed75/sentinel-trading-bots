"""Tests des fonctions pures de strategie du bot 1 (sentinel_signals).

Executer :  python -m unittest test_sentinel_signals -v
Aucun mock MT5/yfinance necessaire : le module est pur (numpy/pandas).
"""

import os
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots"))
import sentinel_signals as ss  # noqa: E402

UTC = timezone.utc


def make_df(closes, highs=None, lows=None, times=None):
    n = len(closes)
    closes = pd.Series(closes, dtype=float)
    return pd.DataFrame({
        "time": times if times is not None
        else pd.date_range("2026-07-14 00:00", periods=n, freq="30min",
                           tz="UTC"),
        "open": closes, "close": closes,
        "high": highs if highs is not None else closes + 1,
        "low": lows if lows is not None else closes - 1,
    })


# --- Indicateurs --------------------------------------------------------------
class TestIndicators(unittest.TestCase):
    def test_rsi_extremes(self):
        up = pd.Series(np.arange(1, 40, dtype=float))
        down = pd.Series(np.arange(40, 1, -1, dtype=float))
        self.assertGreater(ss.rsi(up).iloc[-1], 95)
        self.assertLess(ss.rsi(down).iloc[-1], 5)

    def test_atr_constant_range(self):
        df = make_df([100.0] * 30, highs=[101.0] * 30, lows=[99.0] * 30)
        self.assertAlmostEqual(float(ss.atr(df).iloc[-1]), 2.0, places=6)

    def test_bollinger_ordering(self):
        close = pd.Series(2000 + np.sin(np.arange(60)) * 3)
        upper, mid, lower = ss.bollinger(close)
        self.assertTrue((upper.iloc[25:] > mid.iloc[25:]).all())
        self.assertTrue((mid.iloc[25:] > lower.iloc[25:]).all())

    def test_flat_range_detection(self):
        flat = pd.Series(2000 + np.tile([-1.0, 1.0], 30))
        linear_trend = pd.Series(2000 + np.arange(60) * 2.0)
        accel_trend = pd.Series(2000 + np.arange(60) ** 1.5)
        self.assertTrue(ss.is_flat_range(flat))
        self.assertFalse(ss.is_flat_range(linear_trend))
        self.assertFalse(ss.is_flat_range(accel_trend))


# --- Horaires & plage asiatique -----------------------------------------------
class TestSessions(unittest.TestCase):
    def test_breakout_window_8_to_16(self):
        def d(h, m=0):
            return datetime(2026, 7, 14, h, m, tzinfo=UTC)
        w = (ss.BREAKOUT_HOUR_START, ss.BREAKOUT_HOUR_END)
        with mock.patch.object(ss, "FORCE_TRADING_HOURS", False):
            self.assertFalse(ss.in_trading_hours(d(7, 59), *w))
            self.assertTrue(ss.in_trading_hours(d(8), *w))
            self.assertTrue(ss.in_trading_hours(d(15, 59), *w))
            self.assertFalse(ss.in_trading_hours(d(16), *w))

    def test_reversion_window_13_to_18(self):
        def d(h, m=0):
            return datetime(2026, 7, 14, h, m, tzinfo=UTC)
        w = (ss.REVERSION_HOUR_START, ss.REVERSION_HOUR_END)
        with mock.patch.object(ss, "FORCE_TRADING_HOURS", False):
            self.assertFalse(ss.in_trading_hours(d(12, 59), *w))
            self.assertTrue(ss.in_trading_hours(d(13), *w))
            self.assertTrue(ss.in_trading_hours(d(17, 59), *w))
            self.assertFalse(ss.in_trading_hours(d(18), *w))

    def test_trading_hours_bypass_flag(self):
        # bypass temporaire de test en direct : tout horaire accepte
        with mock.patch.object(ss, "FORCE_TRADING_HOURS", True):
            self.assertTrue(ss.in_trading_hours(
                datetime(2026, 7, 14, 3, tzinfo=UTC),
                ss.BREAKOUT_HOUR_START, ss.BREAKOUT_HOUR_END))

    def test_asian_range_filters_window(self):
        times = pd.to_datetime([
            "2026-07-13 21:30", "2026-07-13 23:00",
            "2026-07-14 07:30", "2026-07-14 09:00"], utc=True)
        df = make_df([50, 45, 45, 10], highs=[100, 50, 55, 200],
                     lows=[90, 40, 35, 1], times=times)
        hi, lo = ss.asian_range(df, datetime(2026, 7, 14, 14, 0, tzinfo=UTC))
        self.assertEqual((hi, lo), (55.0, 35.0))

    def test_asian_range_empty(self):
        df = make_df([50], times=pd.to_datetime(["2026-07-14 12:00"],
                                                utc=True))
        self.assertEqual(ss.asian_range(
            df, datetime(2026, 7, 14, 14, 0, tzinfo=UTC)), (None, None))


# --- Signaux ------------------------------------------------------------------
class TestSignals(unittest.TestCase):
    def test_breakout_buy_sell_none(self):
        self.assertEqual(ss.breakout_signal(make_df([56]), 55, 35), "BUY")
        self.assertEqual(ss.breakout_signal(make_df([34]), 55, 35), "SELL")
        self.assertIsNone(ss.breakout_signal(make_df([50]), 55, 35))
        self.assertIsNone(ss.breakout_signal(make_df([56]), None, None))

    def _reversion_df(self, tail):
        base = list(2000 + np.tile([-1.0, 1.0], 25))[:50 - len(tail)]
        return make_df(base + tail)

    def test_reversion_buy(self):
        df = self._reversion_df([1990.0, 2000.0])  # sous la bande, puis retour
        fake_rsi = pd.Series([50.0] * 48 + [10.0, 50.0])
        with mock.patch.object(ss, "is_flat_range", return_value=True), \
             mock.patch.object(ss, "rsi", return_value=fake_rsi):
            self.assertEqual(ss.reversion_signal(df), "BUY")

    def test_reversion_sell(self):
        df = self._reversion_df([2010.0, 2000.0])
        fake_rsi = pd.Series([50.0] * 48 + [90.0, 50.0])
        with mock.patch.object(ss, "is_flat_range", return_value=True), \
             mock.patch.object(ss, "rsi", return_value=fake_rsi):
            self.assertEqual(ss.reversion_signal(df), "SELL")

    def test_reversion_requires_extreme_rsi(self):
        df = self._reversion_df([1990.0, 2000.0])
        fake_rsi = pd.Series([50.0] * 50)  # RSI jamais < 20
        with mock.patch.object(ss, "is_flat_range", return_value=True), \
             mock.patch.object(ss, "rsi", return_value=fake_rsi):
            self.assertIsNone(ss.reversion_signal(df))

    def test_reversion_requires_flat_range(self):
        df = self._reversion_df([1990.0, 2000.0])
        with mock.patch.object(ss, "is_flat_range", return_value=False):
            self.assertIsNone(ss.reversion_signal(df))

    def test_macro_filter_blocks_sell(self):
        self.assertIsNone(ss.apply_macro_filter("SELL", 30.0))
        self.assertIsNone(ss.apply_macro_filter("SELL", None))  # VIX inconnu
        self.assertEqual(ss.apply_macro_filter("SELL", 20.0), "SELL")
        self.assertEqual(ss.apply_macro_filter("BUY", 30.0), "BUY")
        self.assertIsNone(ss.apply_macro_filter(None, 20.0))

    def test_macro_filter_asymmetric_by_asset(self):
        # VIX 30 : SELL bloque si vix_filter (or), autorise sinon (forex)
        self.assertIsNone(ss.apply_macro_filter("SELL", 30.0,
                                                vix_filter=True))
        self.assertEqual(ss.apply_macro_filter("SELL", 30.0,
                                               vix_filter=False), "SELL")
        self.assertEqual(ss.apply_macro_filter("SELL", None,
                                               vix_filter=False), "SELL")
        self.assertEqual(ss.apply_macro_filter("BUY", 30.0,
                                               vix_filter=False), "BUY")

    def test_price_format_by_asset(self):
        self.assertEqual(ss.fp("XAUUSD.p", 2001.2345), "2001.23")
        self.assertEqual(ss.fp("EURUSD.p", 1.234567), "1.23457")
        self.assertEqual(ss.fp("GBPUSD.p", 1.34), "1.34000")


if __name__ == "__main__":
    unittest.main()
