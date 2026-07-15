"""Tests SENTINEL TRADE ANALYTICS (MT5 mocke).

Executer :  python -m unittest test_sentinel_trade_analytics -v
"""

import csv
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

if not isinstance(sys.modules.get("MetaTrader5"), mock.MagicMock):
    sys.modules["MetaTrader5"] = mock.MagicMock()
fake_mt5 = sys.modules["MetaTrader5"]
fake_mt5.DEAL_ENTRY_IN = 0
fake_mt5.DEAL_ENTRY_OUT = 1
fake_mt5.DEAL_TYPE_BUY = 0
fake_mt5.DEAL_TYPE_SELL = 1

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots"))
import sentinel_trade_analytics as sa  # noqa: E402

UTC = timezone.utc
T0 = int(datetime(2026, 7, 1, 12, 0, tzinfo=UTC).timestamp())
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


def _deal(pos, entry, dtype=0, time=T0, volume=1.0, price=100.0,
          profit=0.0, commission=0.0, swap=0.0, magic=1001,
          symbol="XAUUSD.p"):
    return SimpleNamespace(position_id=pos, entry=entry, type=dtype,
                           time=time, volume=volume, price=price,
                           profit=profit, commission=commission, swap=swap,
                           magic=magic, symbol=symbol)


def _trade(pnl, close_time=NOW, strategy="breakout", symbol="XAUUSD.p"):
    return {"position_id": 1, "symbol": symbol, "magic": 1001,
            "strategy": strategy, "direction": "long", "volume": 1.0,
            "open_time": close_time - timedelta(hours=2),
            "close_time": close_time, "duration_h": 2.0, "pnl": pnl}


class TestMagicMapping(unittest.TestCase):
    def test_all_fleet_magics_are_mapped(self):
        self.assertEqual(sa.MAGIC_STRATEGY[1001], "breakout")
        self.assertEqual(sa.MAGIC_STRATEGY[3002], "reversion")
        self.assertEqual(sa.MAGIC_STRATEGY[4001], "statarb")
        for m in range(5001, 5006):
            self.assertEqual(sa.MAGIC_STRATEGY[m], "trend")
        self.assertEqual(len(sa.MAGIC_STRATEGY), 12)


class TestBuildTrades(unittest.TestCase):
    def test_simple_roundtrip(self):
        deals = [
            _deal(10, entry=0, dtype=0, time=T0, commission=-3.0),
            _deal(10, entry=1, dtype=1, time=T0 + 7200, profit=100.0,
                  commission=-3.0, swap=-1.0),
        ]
        trades = sa.build_trades(deals)
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t["pnl"], 93.0)          # net de frais et swap
        self.assertEqual(t["direction"], "long")
        self.assertEqual(t["strategy"], "breakout")
        self.assertEqual(t["symbol"], "XAUUSD.p")
        self.assertAlmostEqual(t["duration_h"], 2.0)
        self.assertEqual(t["close_time"],
                         datetime.fromtimestamp(T0 + 7200, tz=UTC))

    def test_partial_exits_are_summed(self):
        deals = [
            _deal(11, entry=0, dtype=1, time=T0, volume=1.0),
            _deal(11, entry=1, dtype=0, time=T0 + 3600, volume=0.5,
                  profit=30.0),
            _deal(11, entry=1, dtype=0, time=T0 + 9000, volume=0.5,
                  profit=-10.0),
        ]
        trades = sa.build_trades(deals)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["pnl"], 20.0)
        self.assertEqual(trades[0]["direction"], "short")
        self.assertEqual(trades[0]["close_time"],
                         datetime.fromtimestamp(T0 + 9000, tz=UTC))

    def test_open_position_is_ignored(self):
        self.assertEqual(sa.build_trades([_deal(12, entry=0)]), [])

    def test_partially_closed_position_is_ignored(self):
        deals = [_deal(13, entry=0, volume=1.0),
                 _deal(13, entry=1, time=T0 + 60, volume=0.4, profit=5.0)]
        self.assertEqual(sa.build_trades(deals), [])

    def test_foreign_magic_is_ignored(self):
        deals = [_deal(14, entry=0, magic=9999),
                 _deal(14, entry=1, time=T0 + 60, magic=9999, profit=50.0)]
        self.assertEqual(sa.build_trades(deals), [])

    def test_server_offset_converts_times_to_utc(self):
        # deals estampilles UTC+3 : offset_h=3 rend les heures en UTC reel
        deals = [_deal(15, entry=0, time=T0),
                 _deal(15, entry=1, time=T0 + 7200, profit=10.0)]
        t = sa.build_trades(deals, offset_h=3.0)[0]
        self.assertEqual(t["open_time"],
                         datetime.fromtimestamp(T0, tz=UTC)
                         - timedelta(hours=3))
        self.assertAlmostEqual(t["duration_h"], 2.0)   # duree inchangee

    def test_trades_sorted_by_close_time(self):
        deals = [
            _deal(20, entry=0, time=T0), _deal(20, entry=1, time=T0 + 9999),
            _deal(21, entry=0, time=T0), _deal(21, entry=1, time=T0 + 60),
        ]
        trades = sa.build_trades(deals)
        self.assertEqual([t["position_id"] for t in trades], [21, 20])


class TestComputeStats(unittest.TestCase):
    def test_known_values(self):
        trades = [_trade(p) for p in (100.0, -50.0, 200.0, -50.0)]
        st = sa.compute_stats(trades)
        self.assertEqual(st["trades"], 4)
        self.assertEqual(st["win_rate"], 0.5)
        self.assertEqual(st["profit_factor"], 3.0)   # 300 / 100
        self.assertEqual(st["expectancy"], 50.0)
        self.assertEqual(st["pnl"], 200.0)
        self.assertEqual(st["max_dd"], 50.0)         # pic 100 -> creux 50

    def test_empty(self):
        st = sa.compute_stats([])
        self.assertEqual(st["trades"], 0)
        self.assertEqual(st["pnl"], 0.0)
        self.assertIsNone(st["win_rate"])
        self.assertIsNone(st["profit_factor"])

    def test_profit_factor_none_without_losses(self):
        self.assertIsNone(
            sa.compute_stats([_trade(10.0)])["profit_factor"])


class TestWindows(unittest.TestCase):
    def setUp(self):
        self.trades = [_trade(1.0, NOW - timedelta(days=d))
                       for d in (1, 10, 40)]

    def test_window_filtering(self):
        self.assertEqual(len(sa.in_window(self.trades, NOW, 7)), 1)
        self.assertEqual(len(sa.in_window(self.trades, NOW, 30)), 2)
        self.assertEqual(len(sa.in_window(self.trades, NOW, None)), 3)


class TestOutputs(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.csv_path = os.path.join(self.dir, "trades.csv")
        self.html_path = os.path.join(self.dir, "analytics.html")

    def test_csv_roundtrip(self):
        sa.write_trades_csv([_trade(93.5), _trade(-20.0)], self.csv_path)
        with open(self.csv_path, encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 2)
        self.assertEqual(float(rows[0]["pnl"]), 93.5)
        self.assertEqual(rows[0]["strategy"], "breakout")

    def test_report_contains_key_figures(self):
        html = sa.render_html([_trade(93.5, strategy="statarb")], NOW)
        self.assertIn("statarb", html)
        self.assertIn("93.5", html)
        self.assertIn("Sentinel", html)

    def test_run_cycle_writes_both_files(self):
        fake_mt5.history_deals_get.return_value = [
            _deal(10, entry=0, time=T0),
            _deal(10, entry=1, time=T0 + 7200, profit=100.0),
        ]
        with mock.patch.object(sa, "TRADES_CSV", self.csv_path), \
                mock.patch.object(sa, "REPORT_HTML", self.html_path), \
                mock.patch.object(sa, "LOG_DIR", self.dir):
            sa.run_cycle(now=NOW)
        self.assertTrue(os.path.exists(self.csv_path))
        self.assertTrue(os.path.exists(self.html_path))

    def test_run_cycle_raises_on_lost_connection(self):
        fake_mt5.history_deals_get.return_value = None
        with self.assertRaises(ConnectionError):
            sa.run_cycle(now=NOW)


if __name__ == "__main__":
    unittest.main(verbosity=2)
