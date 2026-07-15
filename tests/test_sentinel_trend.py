"""Tests SENTINEL TREND (MT5 mocke).

Executer :  python -m unittest test_sentinel_trend -v
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

import numpy as np
import pandas as pd

if "MetaTrader5" not in sys.modules or not isinstance(
        sys.modules.get("MetaTrader5"), mock.MagicMock):
    fake_mt5 = mock.MagicMock()
    sys.modules["MetaTrader5"] = fake_mt5
else:
    fake_mt5 = sys.modules["MetaTrader5"]
fake_mt5.POSITION_TYPE_BUY = 0
fake_mt5.POSITION_TYPE_SELL = 1
fake_mt5.ORDER_TYPE_BUY = 0
fake_mt5.ORDER_TYPE_SELL = 1
fake_mt5.TRADE_ACTION_DEAL = 1
fake_mt5.ORDER_TIME_GTC = 0
fake_mt5.ORDER_FILLING_IOC = 1
fake_mt5.TRADE_RETCODE_DONE = 10009
fake_mt5.TIMEFRAME_H4 = 16388

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots"))
import sentinel_trend as st  # noqa: E402

UTC = timezone.utc
OK_RESULT = SimpleNamespace(retcode=10009)


def make_df(closes, highs=None, lows=None):
    closes = pd.Series(closes, dtype=float)
    return pd.DataFrame({
        "time": pd.date_range("2026-06-01", periods=len(closes), freq="4h",
                              tz="UTC"),
        "close": closes,
        "high": highs if highs is not None else closes + 1,
        "low": lows if lows is not None else closes - 1,
    })


class TestSignals(unittest.TestCase):
    def test_donchian_excludes_signal_bar(self):
        # canal calcule sur les n bougies AVANT la derniere
        df = make_df([100] * 10 + [200])       # la cassure ne se compte pas
        hh, ll = st.donchian(df, 10)
        self.assertEqual((hh, ll), (101.0, 99.0))

    def test_entry_breakout_buy_sell_none(self):
        flat = [100.0] * st.ENTRY_CHANNEL
        self.assertEqual(st.entry_signal(make_df(flat + [102.0])), "BUY")
        self.assertEqual(st.entry_signal(make_df(flat + [98.0])), "SELL")
        self.assertIsNone(st.entry_signal(make_df(flat + [100.5])))
        self.assertIsNone(st.entry_signal(make_df([100.0, 102.0])))  # court

    def test_exit_on_opposite_channel(self):
        flat = [100.0] * st.EXIT_CHANNEL
        buy, sell = fake_mt5.POSITION_TYPE_BUY, fake_mt5.POSITION_TYPE_SELL
        self.assertTrue(st.exit_signal(make_df(flat + [98.0]), buy))
        self.assertFalse(st.exit_signal(make_df(flat + [100.5]), buy))
        self.assertTrue(st.exit_signal(make_df(flat + [102.0]), sell))
        self.assertFalse(st.exit_signal(make_df(flat + [100.5]), sell))


class TestRiskScale(unittest.TestCase):
    def test_lot_scaled_by_orchestrator_factor(self):
        args = (10000, 2.0, 0.01, 0.01, 0.01, 1000.0, 0.01)
        self.assertEqual(st.compute_lot(*args, scale=1.0), 50.0)  # 1% / 2$
        self.assertEqual(st.compute_lot(*args, scale=0.5), 25.0)
        self.assertEqual(st.compute_lot(*args, scale=0.0), 0.0)

    def test_read_risk_scale_file_and_defaults(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"scale": 0.42}, fh)
            self.assertEqual(st.read_risk_scale(path), 0.42)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"scale": 7.0}, fh)      # borne a [0,1]
            self.assertEqual(st.read_risk_scale(path), 1.0)
        finally:
            os.unlink(path)
        self.assertEqual(st.read_risk_scale(path), 1.0)  # absent -> 1.0


class TestPeakGuard(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.path)
        self.guard = st.PeakGuard(self.path)
        self.addCleanup(lambda: os.path.exists(self.path)
                        and os.unlink(self.path))

    def test_locks_at_15_pct_from_peak(self):
        self.assertFalse(self.guard.check(10000))
        self.assertFalse(self.guard.check(8501))    # -14.99%
        self.assertTrue(self.guard.check(8500))     # -15%
        self.assertTrue(self.guard.check(12000))    # permanent
        self.assertTrue(st.PeakGuard(self.path).locked)


class TestExecution(unittest.TestCase):
    def setUp(self):
        fake_mt5.reset_mock()
        fake_mt5.order_send.return_value = OK_RESULT
        fake_mt5.positions_get.return_value = []
        fake_mt5.account_info.return_value = SimpleNamespace(
            balance=10000.0, equity=10000.0)
        fake_mt5.symbol_info.return_value = SimpleNamespace(
            trade_tick_size=0.01, trade_tick_value=0.01, volume_min=0.01,
            volume_max=1000.0, volume_step=0.01, digits=2)
        fake_mt5.symbol_info_tick.return_value = SimpleNamespace(
            ask=102.0, bid=101.98)
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.path)
        self.guard = st.PeakGuard(self.path)
        self.active = {"XAUUSD": {"symbol": "XAUUSD.p", "magic": 5001}}
        self.addCleanup(lambda: os.path.exists(self.path)
                        and os.unlink(self.path))

    @staticmethod
    def _rates(closes):
        return [{"time": 1750000000 + i * 14400, "open": c, "high": c + 1,
                 "low": c - 1, "close": c} for i, c in enumerate(closes)]

    NOON = datetime(2026, 7, 14, 14, tzinfo=UTC)   # hors blackout rollover

    def test_breakout_opens_trade_with_sl_no_tp(self):
        # 56 bougies plates puis cassure haussiere + bougie en cours
        closes = [100.0] * (st.ENTRY_CHANNEL + 1) + [103.0, 103.0]
        fake_mt5.copy_rates_from_pos.return_value = self._rates(closes)
        st.run_cycle(self.active, self.guard, 16388, {}, now=self.NOON)
        req = fake_mt5.order_send.call_args[0][0]
        self.assertEqual(req["type"], fake_mt5.ORDER_TYPE_BUY)
        self.assertEqual(req["magic"], 5001)
        self.assertIn("sl", req)
        self.assertNotIn("tp", req)          # sortie par canal, pas de TP
        self.assertLess(req["sl"], 102.0)    # SL sous le prix d'entree

    def test_no_reentry_same_bar_and_no_entry_if_position(self):
        closes = [100.0] * (st.ENTRY_CHANNEL + 1) + [103.0, 103.0]
        fake_mt5.copy_rates_from_pos.return_value = self._rates(closes)
        last_bars = {}
        st.run_cycle(self.active, self.guard, 16388, last_bars, now=self.NOON)
        st.run_cycle(self.active, self.guard, 16388, last_bars,
                     now=self.NOON)   # meme bougie
        self.assertEqual(fake_mt5.order_send.call_count, 1)
        # position ouverte + nouvelle bougie -> pas de nouvelle entree
        pos = SimpleNamespace(ticket=1, symbol="XAUUSD.p", magic=5001,
                              type=0, volume=1.0, profit=5.0)
        fake_mt5.positions_get.return_value = [pos]
        fake_mt5.copy_rates_from_pos.return_value = self._rates(
            closes + [103.5])
        st.run_cycle(self.active, self.guard, 16388, last_bars, now=self.NOON)
        self.assertEqual(fake_mt5.order_send.call_count, 1)

    def test_rollover_blackout_defers_entry(self):
        closes = [100.0] * (st.ENTRY_CHANNEL + 1) + [103.0, 103.0]
        fake_mt5.copy_rates_from_pos.return_value = self._rates(closes)
        last_bars = {}
        blackout = datetime(2026, 7, 14, 21, 30, tzinfo=UTC)
        st.run_cycle(self.active, self.guard, 16388, last_bars, now=blackout)
        fake_mt5.order_send.assert_not_called()
        self.assertNotIn("XAUUSD", last_bars)   # bougie non consommee
        # sortie du blackout : la meme cassure est reprise
        after = datetime(2026, 7, 14, 23, 5, tzinfo=UTC)
        st.run_cycle(self.active, self.guard, 16388, last_bars, now=after)
        self.assertEqual(fake_mt5.order_send.call_count, 1)

    def test_exit_allowed_during_blackout(self):
        pos = SimpleNamespace(ticket=7, symbol="XAUUSD.p", magic=5001,
                              type=0, volume=1.0, profit=42.0)
        fake_mt5.positions_get.return_value = [pos]
        closes = [100.0] * (st.ENTRY_CHANNEL + 1) + [97.0, 97.0]
        fake_mt5.copy_rates_from_pos.return_value = self._rates(closes)
        blackout = datetime(2026, 7, 14, 22, 0, tzinfo=UTC)
        st.run_cycle(self.active, self.guard, 16388, {}, now=blackout)
        self.assertEqual(fake_mt5.order_send.call_args[0][0]["position"], 7)

    def test_exit_channel_closes_position(self):
        pos = SimpleNamespace(ticket=7, symbol="XAUUSD.p", magic=5001,
                              type=0, volume=1.0, profit=42.0)
        fake_mt5.positions_get.return_value = [pos]
        closes = [100.0] * (st.ENTRY_CHANNEL + 1) + [97.0, 97.0]  # sous canal
        fake_mt5.copy_rates_from_pos.return_value = self._rates(closes)
        st.run_cycle(self.active, self.guard, 16388, {}, now=self.NOON)
        req = fake_mt5.order_send.call_args[0][0]
        self.assertEqual(req["position"], 7)
        self.assertEqual(req["type"], fake_mt5.ORDER_TYPE_SELL)

    def test_drawdown_lock_closes_only_trend_magics(self):
        self.guard.peak = 10000.0
        fake_mt5.account_info.return_value = SimpleNamespace(
            balance=8000.0, equity=8000.0)
        mine = SimpleNamespace(ticket=1, symbol="XAUUSD.p", magic=5001,
                               type=0, volume=1.0, profit=-100.0)
        other = SimpleNamespace(ticket=2, symbol="XAUUSD.p", magic=1001,
                                type=0, volume=1.0, profit=-100.0)
        fake_mt5.positions_get.return_value = [mine, other]
        st.run_cycle(self.active, self.guard, 16388, {})
        self.assertTrue(self.guard.locked)
        reqs = [c[0][0] for c in fake_mt5.order_send.call_args_list]
        self.assertEqual([r["position"] for r in reqs], [1])  # 1001 intact
        fake_mt5.copy_rates_from_pos.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
