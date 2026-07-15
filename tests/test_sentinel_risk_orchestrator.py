"""Tests SENTINEL RISK ORCHESTRATOR (MT5 mocke).

Executer :  python -m unittest test_sentinel_risk_orchestrator -v
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import numpy as np

if not isinstance(sys.modules.get("MetaTrader5"), mock.MagicMock):
    sys.modules["MetaTrader5"] = mock.MagicMock()
fake_mt5 = sys.modules["MetaTrader5"]
fake_mt5.POSITION_TYPE_BUY = 0
fake_mt5.POSITION_TYPE_SELL = 1
fake_mt5.ORDER_TYPE_BUY = 0
fake_mt5.ORDER_TYPE_SELL = 1
fake_mt5.TRADE_ACTION_DEAL = 1
fake_mt5.ORDER_TIME_GTC = 0
fake_mt5.ORDER_FILLING_IOC = 1
fake_mt5.TRADE_RETCODE_DONE = 10009

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots"))
import sentinel_risk_orchestrator as so  # noqa: E402

UTC = timezone.utc
OK_RESULT = SimpleNamespace(retcode=10009)


def temp_path():
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.unlink(path)
    return path


class TestVolTargeting(unittest.TestCase):
    def test_vol_scale_formula(self):
        self.assertEqual(so.vol_scale(0.20), 0.5)     # cible 10% / realisee 20%
        self.assertEqual(so.vol_scale(0.05), 1.0)     # vol basse : plein risque
        self.assertEqual(so.vol_scale(1.00), so.MIN_SCALE)  # plancher
        self.assertEqual(so.vol_scale(0.0), 1.0)

    def test_realized_vol_from_daily_equity(self):
        mon = so.EquityMonitor(temp_path())
        # equite croissant de 1%/jour : vol des rendements quasi nulle
        base = datetime(2026, 7, 1, tzinfo=UTC)
        for i in range(10):
            mon.snapshot(base + timedelta(days=i), 10000 * 1.01 ** i)
        self.assertAlmostEqual(mon.realized_vol(), 0.0, places=6)

    def test_realized_vol_none_when_too_short(self):
        mon = so.EquityMonitor(temp_path())
        for i in range(so.MIN_SAMPLES):
            mon.snapshot(datetime(2026, 7, 1 + i, tzinfo=UTC), 10000.0)
        self.assertIsNone(mon.realized_vol())

    def test_snapshot_once_per_day(self):
        mon = so.EquityMonitor(temp_path())
        d = datetime(2026, 7, 14, 8, tzinfo=UTC)
        mon.snapshot(d, 10000.0)
        mon.snapshot(d + timedelta(hours=6), 11000.0)   # meme jour : ignore
        self.assertEqual(len(mon.history), 1)
        self.assertEqual(mon.history[0]["equity"], 10000.0)

    def test_risk_scale_file_roundtrip(self):
        path = temp_path()
        try:
            so.write_risk_scale(0.62, path)
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["scale"], 0.62)
        finally:
            os.unlink(path)


class TestGlobalDrawdown(unittest.TestCase):
    def setUp(self):
        self.path = temp_path()
        self.mon = so.EquityMonitor(self.path)
        self.addCleanup(lambda: os.path.exists(self.path)
                        and os.unlink(self.path))

    def test_locks_at_10_pct_and_persists(self):
        self.assertFalse(self.mon.check_drawdown(10000))
        self.assertFalse(self.mon.check_drawdown(9001))   # -9.99%
        self.assertTrue(self.mon.check_drawdown(9000))    # -10%
        self.assertTrue(self.mon.check_drawdown(11000))   # permanent
        self.assertTrue(so.EquityMonitor(self.path).locked)


class TestFleet(unittest.TestCase):
    def setUp(self):
        fake_mt5.reset_mock()
        fake_mt5.order_send.return_value = OK_RESULT
        fake_mt5.symbol_info_tick.return_value = SimpleNamespace(
            ask=100.0, bid=99.98)

    @staticmethod
    def _pos(ticket, magic, ptype=0):
        return SimpleNamespace(ticket=ticket, symbol="XAUUSD.p", magic=magic,
                               type=ptype, volume=1.0, profit=0.0)

    def test_direction_concentration(self):
        pos = [self._pos(1, 1001, 0), self._pos(2, 2001, 0),
               self._pos(3, 4001, 0), self._pos(4, 5001, 1)]
        self.assertEqual(so.direction_concentration(pos), (3, 1))

    def test_kill_fleet_spares_foreign_magics(self):
        fake_mt5.positions_get.return_value = [
            self._pos(1, 1001), self._pos(2, 4001), self._pos(3, 5003),
            self._pos(4, 9999),        # EA externe : intouchable
            self._pos(5, 0)]           # trade manuel : intouchable
        so.kill_fleet()
        closed = [c[0][0]["position"]
                  for c in fake_mt5.order_send.call_args_list]
        self.assertEqual(sorted(closed), [1, 2, 3])


class TestRunCycle(unittest.TestCase):
    def setUp(self):
        fake_mt5.reset_mock()
        fake_mt5.order_send.return_value = OK_RESULT
        fake_mt5.positions_get.return_value = []
        fake_mt5.symbol_info_tick.return_value = SimpleNamespace(
            ask=100.0, bid=99.98)
        self.state_path = temp_path()
        self.scale_path = temp_path()
        self.mon = so.EquityMonitor(self.state_path)
        self.addCleanup(lambda: [os.unlink(p) for p in
                                 (self.state_path, self.scale_path)
                                 if os.path.exists(p)])

    def _run(self, equity, now):
        fake_mt5.account_info.return_value = SimpleNamespace(equity=equity)
        with mock.patch.object(so, "RISK_SCALE_FILE", self.scale_path):
            so.run_cycle(self.mon, now=now)

    def _scale(self):
        with open(self.scale_path, encoding="utf-8") as fh:
            return json.load(fh)["scale"]

    def test_normal_cycle_writes_neutral_scale(self):
        self._run(10000.0, datetime(2026, 7, 14, 14, tzinfo=UTC))
        self.assertEqual(self._scale(), 1.0)   # historique court : neutre
        fake_mt5.order_send.assert_not_called()

    def test_high_realized_vol_reduces_scale(self):
        # equite en dents de scie +/-3%/jour : vol annualisee >> 10%
        base = datetime(2026, 7, 1, tzinfo=UTC)
        eq = 10000.0
        for i in range(12):
            eq *= 1.03 if i % 2 == 0 else 0.97
            self._run(eq, base + timedelta(days=i))
        self.assertLessEqual(self._scale(), 0.5)
        self.assertGreaterEqual(self._scale(), so.MIN_SCALE)

    def test_drawdown_kills_fleet_and_floors_scale(self):
        self.mon.peak = 10000.0
        fake_mt5.positions_get.return_value = [
            SimpleNamespace(ticket=1, symbol="XAUUSD.p", magic=4001,
                            type=0, volume=1.0, profit=-300.0)]
        self._run(8900.0, datetime(2026, 7, 14, 14, tzinfo=UTC))
        self.assertTrue(self.mon.locked)
        self.assertEqual(fake_mt5.order_send.call_args[0][0]["position"], 1)
        self.assertEqual(self._scale(), so.MIN_SCALE)

    def test_concentration_warning_logged(self):
        fake_mt5.positions_get.return_value = [
            SimpleNamespace(ticket=i, symbol="XAUUSD.p", magic=m, type=0,
                            volume=1.0, profit=0.0)
            for i, m in enumerate([1001, 2001, 3001, 5001])]
        with self.assertLogs("orchestrator", level="WARNING") as cm:
            self._run(10000.0, datetime(2026, 7, 14, 14, tzinfo=UTC))
        self.assertIn("CONCENTRATION", cm.output[0])


class TestPersistence(unittest.TestCase):
    def test_save_failure_preserves_previous_state(self):
        path = temp_path()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        mon = so.EquityMonitor(path)
        mon.peak = 10000.0
        mon._save()
        mon.locked = True
        with mock.patch.object(so.os, "replace",
                               side_effect=OSError("disque plein")):
            mon._save()
        again = so.EquityMonitor(path)
        self.assertFalse(again.locked)
        self.assertEqual(again.peak, 10000.0)

    def test_write_heartbeat(self):
        path = os.path.join(tempfile.mkdtemp(), "orchestrator.hb")
        now = datetime(2026, 7, 15, 12, tzinfo=UTC)
        so.write_heartbeat(path, now)
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), now.isoformat())


if __name__ == "__main__":
    unittest.main(verbosity=2)
