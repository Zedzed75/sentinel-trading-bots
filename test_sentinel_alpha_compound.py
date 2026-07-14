"""Tests SENTINEL ALPHA COMPOUND (MT5 mocke, statsmodels reel).

Executer :  python -m unittest test_sentinel_alpha_compound -v
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import numpy as np
import pandas as pd

fake_mt5 = mock.MagicMock()
fake_mt5.POSITION_TYPE_BUY = 0
fake_mt5.POSITION_TYPE_SELL = 1
fake_mt5.ORDER_TYPE_BUY = 0
fake_mt5.ORDER_TYPE_SELL = 1
fake_mt5.TRADE_ACTION_DEAL = 1
fake_mt5.ORDER_TIME_GTC = 0
fake_mt5.ORDER_FILLING_IOC = 1
fake_mt5.TRADE_RETCODE_DONE = 10009
fake_mt5.TIMEFRAME_M15 = 15
sys.modules["MetaTrader5"] = fake_mt5

import sentinel_alpha_compound as sa  # noqa: E402

UTC = timezone.utc
OK_RESULT = SimpleNamespace(retcode=10009)
SYM = SimpleNamespace(trade_tick_size=0.01, trade_tick_value=0.01,
                      volume_min=0.01, volume_max=1000.0, volume_step=0.01,
                      digits=2)


def temp_state():
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.unlink(path)
    return sa.AlphaState(path), path


class TestKelly(unittest.TestCase):
    def setUp(self):
        self.state, self.path = temp_state()
        self.sizer = sa.KellySizer(self.state)
        self.addCleanup(lambda: os.path.exists(self.path)
                        and os.unlink(self.path))

    def test_kelly_fraction_formula(self):
        self.assertAlmostEqual(sa.kelly_fraction(0.6, 2.0), 0.4)   # W-(1-W)/R
        self.assertAlmostEqual(sa.kelly_fraction(0.5, 1.0), 0.0)
        self.assertEqual(sa.kelly_fraction(0.4, 1.0), 0.0)         # borne a 0
        self.assertEqual(sa.kelly_fraction(0.9, 0.0), 0.0)         # R invalide

    def test_default_risk_until_enough_history(self):
        self.state.trades = [100.0] * (sa.MIN_TRADES_FOR_KELLY - 1)
        self.assertEqual(self.sizer.risk_fraction(), sa.DEFAULT_RISK)

    def test_half_kelly_capped(self):
        # 6 gains de 200, 4 pertes de 100 : W=0.6, R=2 -> K=0.4, half=0.2
        self.state.trades = [200.0] * 6 + [-100.0] * 4
        self.assertAlmostEqual(self.sizer.win_rate, 0.6)
        self.assertAlmostEqual(self.sizer.rr_ratio, 2.0)
        self.assertEqual(self.sizer.risk_fraction(), sa.MAX_RISK)  # 0.2 -> cap

    def test_half_kelly_below_cap(self):
        # 5 gains de 120, 5 pertes de 100 : W=0.5, R=1.2
        # K = 0.5 - 0.5/1.2 = 0.0833 ; half = 0.0417 < MAX_RISK
        self.state.trades = [120.0] * 5 + [-100.0] * 5
        self.assertAlmostEqual(self.sizer.risk_fraction(), 0.5 / 12, places=6)

    def test_negative_expectancy_falls_back_to_default(self):
        self.state.trades = [100.0] * 3 + [-100.0] * 7  # W=0.3 R=1 -> K=0
        self.assertEqual(self.sizer.risk_fraction(), sa.DEFAULT_RISK)

    def test_record_persists_history(self):
        self.sizer.record(150.0)
        self.sizer.record(-80.0)
        reloaded = sa.AlphaState(self.path)
        self.assertEqual(reloaded.trades, [150.0, -80.0])


class TestCompounding(unittest.TestCase):
    """Preuve du calcul dynamique des lots sur l'equite (effet compound)."""

    def setUp(self):
        self.state, self.path = temp_state()
        self.sizer = sa.KellySizer(self.state)
        self.analysis = {"beta": 1.0, "sigma": 0.5, "z": -2.4,
                         "pvalue": 0.01, "coint": True}
        self.addCleanup(lambda: os.path.exists(self.path)
                        and os.unlink(self.path))

    def test_lot_proportional_to_equity(self):
        # risque par defaut 1% : SL spread = 4*0.5 = 2.0 -> 2$/lot
        lot_a1, _ = self.sizer.lots_for_spread(10000, self.analysis, SYM, SYM)
        lot_a2, _ = self.sizer.lots_for_spread(20000, self.analysis, SYM, SYM)
        self.assertEqual(lot_a1, 25.0)   # (10000*0.01/2) / 2$ par lot
        self.assertEqual(lot_a2, 50.0)   # equite doublee -> lot double

    def test_lots_grow_with_simulated_gain_streak(self):
        # serie de gains/pertes : l'historique alimente Kelly et l'equite
        # croissante augmente mecaniquement la taille des positions
        for pnl in [120, 120, -100, 120, 120, -100, 120, 120, -100, 120]:
            self.sizer.record(float(pnl))
        self.assertGreaterEqual(len(self.state.trades),
                                sa.MIN_TRADES_FOR_KELLY)
        equities = [10000, 11000, 12500, 14000]
        lots = [self.sizer.lots_for_spread(eq, self.analysis, SYM, SYM)[0]
                for eq in equities]
        self.assertTrue(all(a < b for a, b in zip(lots, lots[1:])),
                        f"lots non croissants : {lots}")

    def test_leg_b_scaled_by_hedge_ratio(self):
        analysis = dict(self.analysis, beta=2.0)
        lot_a, lot_b = self.sizer.lots_for_spread(10000, analysis, SYM, SYM)
        self.assertGreater(lot_a, 0)
        self.assertAlmostEqual(lot_b, lot_a * 2.0, places=2)

    def test_zero_lot_when_too_small(self):
        lot_a, lot_b = self.sizer.lots_for_spread(1.0, self.analysis, SYM, SYM)
        self.assertEqual((lot_a, lot_b), (0.0, 0.0))


class TestCointegration(unittest.TestCase):
    def setUp(self):
        self.engine = sa.CointegrationEngine()

    def test_analyze_recovers_beta_on_cointegrated_pair(self):
        rng = np.random.default_rng(42)
        b = pd.Series(100 + np.cumsum(rng.normal(0, 0.5, 240)))
        a = 2.0 * b + 5 + pd.Series(rng.normal(0, 0.3, 240))
        res = self.engine.analyze(a, b)
        self.assertTrue(res["coint"])            # ADF p < 0.05
        self.assertAlmostEqual(res["beta"], 2.0, delta=0.05)
        self.assertGreater(res["sigma"], 0)

    def test_analyze_rejects_independent_walks(self):
        rng = np.random.default_rng(7)
        a = pd.Series(100 + np.cumsum(rng.normal(0, 1, 240)))
        b = pd.Series(50 + np.cumsum(rng.normal(0, 1, 240)))
        res = self.engine.analyze(a, b)
        self.assertFalse(res["coint"])

    def test_entry_signal_thresholds(self):
        base = {"beta": 1.0, "sigma": 1.0, "pvalue": 0.01, "coint": True}
        self.assertEqual(self.engine.entry_signal(dict(base, z=-2.1)),
                         "BUY_SPREAD")
        self.assertEqual(self.engine.entry_signal(dict(base, z=2.1)),
                         "SELL_SPREAD")
        self.assertIsNone(self.engine.entry_signal(dict(base, z=1.9)))
        self.assertIsNone(self.engine.entry_signal(
            dict(base, z=3.0, coint=False)))     # pas de coint -> pas d'entree
        self.assertIsNone(self.engine.entry_signal(None))

    def test_exit_reasons(self):
        self.assertEqual(self.engine.exit_reason(0.3, 5), "convergence")
        self.assertEqual(self.engine.exit_reason(4.2, 5), "z_stop")
        self.assertEqual(self.engine.exit_reason(1.5,
                                                 sa.MAX_BARS_IN_TRADE),
                         "time_stop")
        self.assertIsNone(self.engine.exit_reason(1.5, 5))


class TestDrawdownGuard(unittest.TestCase):
    def setUp(self):
        self.state, self.path = temp_state()
        self.guard = sa.DrawdownGuard(self.state)
        self.addCleanup(lambda: os.path.exists(self.path)
                        and os.unlink(self.path))

    def test_peak_tracking_and_lock(self):
        self.assertFalse(self.guard.check(10000))   # nouveau pic
        self.assertFalse(self.guard.check(12000))   # pic releve
        self.assertFalse(self.guard.check(10300))   # -14.2% : ok
        self.assertTrue(self.guard.check(10200))    # -15% du pic 12000
        self.assertTrue(self.guard.check(13000))    # verrou permanent

    def test_lock_survives_restart(self):
        self.guard.check(10000)
        self.guard.check(8000)
        self.assertTrue(sa.AlphaState(self.path).locked)


class TestPriceFormat(unittest.TestCase):
    def test_forex_vs_commodities(self):
        self.assertEqual(sa.fp("EURUSD.p", 1.234567), "1.23457")
        self.assertEqual(sa.fp("XBRUSD.p", 78.4567), "78.46")
        self.assertEqual(sa.fp("XTIUSD", 74.1), "74.10")
        self.assertEqual(sa.fp("XBRUSD", None), "n/a")


class TestExecution(unittest.TestCase):
    def setUp(self):
        fake_mt5.reset_mock()
        fake_mt5.order_send.return_value = OK_RESULT
        fake_mt5.positions_get.return_value = []
        fake_mt5.symbol_info.return_value = SYM
        fake_mt5.symbol_info_tick.return_value = SimpleNamespace(
            ask=80.00, bid=79.98)
        self.state, self.path = temp_state()
        self.trader = sa.PairTrader(self.state)
        self.trader.sym_a, self.trader.sym_b = "XBRUSD.p", "XTIUSD.p"
        self.analysis = {"beta": 1.0, "sigma": 0.5, "z": -2.4,
                         "pvalue": 0.01, "coint": True}
        self.now = datetime(2026, 7, 14, 14, tzinfo=UTC)
        self.addCleanup(lambda: os.path.exists(self.path)
                        and os.unlink(self.path))

    def test_open_spread_sends_two_legs_with_sl(self):
        ok = self.trader.open_spread("BUY_SPREAD", self.analysis, 10000,
                                     self.now)
        self.assertTrue(ok)
        reqs = [c[0][0] for c in fake_mt5.order_send.call_args_list]
        self.assertEqual(len(reqs), 2)
        leg_a = next(r for r in reqs if r["symbol"] == "XBRUSD.p")
        leg_b = next(r for r in reqs if r["symbol"] == "XTIUSD.p")
        self.assertEqual(leg_a["type"], fake_mt5.ORDER_TYPE_BUY)   # achat A
        self.assertEqual(leg_b["type"], fake_mt5.ORDER_TYPE_SELL)  # vente B
        self.assertEqual(leg_a["sl"], 78.0)    # ask 80 - 4*sigma(0.5)
        self.assertEqual(leg_b["sl"], 81.98)   # bid 79.98 + 2.0
        self.assertTrue(all(r["magic"] == sa.MAGIC_ALPHA for r in reqs))
        self.assertEqual(self.state.open["direction"], "BUY_SPREAD")

    def test_close_spread_records_pnl_and_clears_state(self):
        self.state.open = {"direction": "BUY_SPREAD",
                           "entry_time": self.now.isoformat(),
                           "beta": 1.0, "sigma": 0.5}
        pos_a = SimpleNamespace(ticket=1, symbol="XBRUSD.p", type=0,
                                volume=1.0, profit=150.0, magic=sa.MAGIC_ALPHA)
        pos_b = SimpleNamespace(ticket=2, symbol="XTIUSD.p", type=1,
                                volume=1.0, profit=-50.0, magic=sa.MAGIC_ALPHA)
        fake_mt5.positions_get.return_value = [pos_a, pos_b]
        self.trader.close_spread("convergence")
        self.assertEqual(fake_mt5.order_send.call_count, 2)
        self.assertEqual(self.state.trades, [100.0])   # PnL net enregistre
        self.assertIsNone(self.state.open)

    def test_orphan_leg_triggers_full_close(self):
        # une jambe a saute sur son SL : la jambe restante doit etre purgee
        self.state.open = {"direction": "BUY_SPREAD",
                           "entry_time": self.now.isoformat(),
                           "beta": 1.0, "sigma": 0.5}
        lone = SimpleNamespace(ticket=3, symbol="XBRUSD.p", type=0,
                               volume=1.0, profit=-40.0, magic=sa.MAGIC_ALPHA)
        fake_mt5.positions_get.side_effect = (
            lambda symbol=None: [lone] if symbol == "XBRUSD.p" else [])
        self.addCleanup(setattr, fake_mt5.positions_get, "side_effect", None)
        self.trader.manage(self.analysis, 10000, self.now)
        self.assertIsNone(self.state.open)
        self.assertEqual(self.state.trades, [-40.0])

    def test_time_stop_via_manage(self):
        entry = self.now - timedelta(minutes=sa.TF_MINUTES
                                     * sa.MAX_BARS_IN_TRADE)
        self.state.open = {"direction": "BUY_SPREAD",
                           "entry_time": entry.isoformat(),
                           "beta": 1.0, "sigma": 0.5}
        pos_a = SimpleNamespace(ticket=1, symbol="XBRUSD.p", type=0,
                                volume=1.0, profit=10.0, magic=sa.MAGIC_ALPHA)
        pos_b = SimpleNamespace(ticket=2, symbol="XTIUSD.p", type=1,
                                volume=1.0, profit=-25.0, magic=sa.MAGIC_ALPHA)
        fake_mt5.positions_get.return_value = [pos_a, pos_b]
        # z encore ecarte (1.5) mais N bougies atteintes -> stop temporel
        self.trader.manage(dict(self.analysis, z=1.5), 10000, self.now)
        self.assertIsNone(self.state.open)
        self.assertEqual(self.state.trades, [-15.0])

    def test_run_cycle_drawdown_lock_closes_spread(self):
        self.state.peak_equity = 10000.0
        fake_mt5.account_info.return_value = SimpleNamespace(equity=8400.0)
        guard = sa.DrawdownGuard(self.state)
        pos = SimpleNamespace(ticket=9, symbol="XBRUSD.p", type=0,
                              volume=1.0, profit=-500.0, magic=sa.MAGIC_ALPHA)
        fake_mt5.positions_get.return_value = [pos]
        sa.run_cycle(self.trader, guard, fake_mt5.TIMEFRAME_M15, now=self.now)
        self.assertTrue(self.state.locked)
        fake_mt5.copy_rates_from_pos.assert_not_called()
        self.assertTrue(fake_mt5.order_send.called)    # jambe fermee


if __name__ == "__main__":
    unittest.main(verbosity=2)
