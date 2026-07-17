"""SENTINEL multi-asset functional tests (MT5 and yfinance mocked).

Run:  python -m unittest test_sentinel_bot -v
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

import pandas as pd

# --- Mock external dependencies before importing the bot ---------------------
fake_mt5 = mock.MagicMock()
fake_mt5.TIMEFRAME_M5 = 5
fake_mt5.TIMEFRAME_M30 = 30
fake_mt5.POSITION_TYPE_BUY = 0
fake_mt5.POSITION_TYPE_SELL = 1
fake_mt5.ORDER_TYPE_BUY = 0
fake_mt5.ORDER_TYPE_SELL = 1
fake_mt5.TRADE_ACTION_DEAL = 1
fake_mt5.TRADE_ACTION_SLTP = 6
fake_mt5.TRADE_ACTION_REMOVE = 8
fake_mt5.ORDER_TIME_GTC = 0
fake_mt5.ORDER_FILLING_IOC = 1
fake_mt5.TRADE_RETCODE_DONE = 10009
sys.modules["MetaTrader5"] = fake_mt5
sys.modules["yfinance"] = mock.MagicMock()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots"))
import sentinel_bot as sb  # noqa: E402
import sentinel_signals as ss  # noqa: E402  (trading-window patching)

XAU, XAU_MB, XAU_MR = "XAUUSD.p", 1001, 1002
ACTIVE = {  # resolved portfolio, breakout enabled everywhere (exercises the logic)
    "XAUUSD": {"symbol": "XAUUSD.p", "magic_breakout": 1001,
               "magic_reversion": 1002, "vix_filter": True,
               "breakout": True},
    "EURUSD": {"symbol": "EURUSD.p", "magic_breakout": 2001,
               "magic_reversion": 2002, "vix_filter": False,
               "breakout": True},
    "GBPUSD": {"symbol": "GBPUSD.p", "magic_breakout": 3001,
               "magic_reversion": 3002, "vix_filter": False,
               "breakout": True},
}
UTC = timezone.utc
OK_RESULT = SimpleNamespace(retcode=10009, comment="done")


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


# --- MT5 server clock --------------------------------------------------------
class TestSessions(unittest.TestCase):
    def test_server_offset_detected_from_fresh_tick(self):
        now = datetime(2026, 7, 14, 12, tzinfo=UTC)
        with mock.patch.dict(sb._SERVER_OFFSET,
                             {"hours": 0.0, "at": None}):
            fake_mt5.symbol_info_tick.return_value = SimpleNamespace(
                time=now.timestamp() + 3 * 3600)   # server UTC+3
            self.assertEqual(sb.server_offset_hours("XAUUSD.p", now), 3.0)

    def test_server_offset_ignores_stale_tick(self):
        now = datetime(2026, 7, 14, 12, tzinfo=UTC)
        with mock.patch.dict(sb._SERVER_OFFSET,
                             {"hours": 2.0, "at": None}):
            fake_mt5.symbol_info_tick.return_value = SimpleNamespace(
                time=now.timestamp() - 40 * 3600)  # week-end: stale tick
            self.assertEqual(sb.server_offset_hours("XAUUSD.p", now), 2.0)

    def test_get_rates_converts_server_time_to_utc(self):
        now = datetime(2026, 7, 14, 12, tzinfo=UTC)
        srv = now.timestamp() + 3 * 3600           # candle stamped UTC+3
        fake_mt5.copy_rates_from_pos.return_value = [
            {"time": srv, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0}]
        with mock.patch.dict(sb._SERVER_OFFSET,
                             {"hours": 3.0,       # server offset already measured
                              "at": datetime.now(timezone.utc)}):
            df = sb.get_rates("XAUUSD.p", 1, 1)
        self.assertEqual(df["time"].iloc[0].to_pydatetime(), now)

# --- Risk management -----------------------------------------------------------
class TestRisk(unittest.TestCase):
    def test_lot_risks_exactly_1_5_pct(self):
        # loss/lot = (5.0 / 0.01) * 0.01 = $5; risk = $150 -> 30 lots
        lot = sb.compute_lot(10000, 5.0, 0.01, 0.01, 0.01, 100, 0.01)
        self.assertEqual(lot, 30.0)

    def test_lot_clamped_and_floored(self):
        self.assertEqual(sb.compute_lot(10000, 0.5, 0.01, 0.01, 0.01, 10,
                                        0.01), 10.0)   # max bound
        self.assertEqual(sb.compute_lot(10, 500.0, 0.01, 0.01, 0.01, 100,
                                        0.01), 0.0)    # below min -> 0
        self.assertEqual(sb.compute_lot(10000, 0.0, 0.01, 0.01, 0.01, 100,
                                        0.01), 0.0)    # invalid SL

    def test_lot_scaled_by_orchestrator(self):
        args = (10000, 5.0, 0.01, 0.01, 0.01, 100, 0.01)
        self.assertEqual(sb.compute_lot(*args, scale=0.5), 15.0)  # 30 x 0.5
        self.assertEqual(sb.compute_lot(*args), 30.0)             # default 1.0
        self.assertEqual(sb.read_risk_scale("_absent_.json"), 1.0)

    def test_reached_one_r(self):
        buy = fake_mt5.POSITION_TYPE_BUY
        sell = fake_mt5.POSITION_TYPE_SELL
        self.assertTrue(sb.reached_one_r(buy, 2000, 1997, 2003.0))
        self.assertFalse(sb.reached_one_r(buy, 2000, 1997, 2002.9))
        self.assertTrue(sb.reached_one_r(sell, 2000, 2003, 1997.0))
        self.assertFalse(sb.reached_one_r(sell, 2000, 2003, 1997.1))
        self.assertFalse(sb.reached_one_r(buy, 2000, 2000, 2005))  # zero risk


# --- Daily circuit breaker -------------------------------------------------------
class TestDayGuard(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.path)
        self.guard = sb.DayGuard(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.unlink(self.path)

    def test_triggers_at_4_pct_not_before(self):
        self.guard.roll_day(datetime(2026, 7, 14, 14, tzinfo=UTC), 10000)
        self.assertFalse(self.guard.check(9601))   # -3.99%
        self.assertTrue(self.guard.check(9600))    # -4.00%
        self.assertTrue(self.guard.check(10000))   # stays locked

    def test_unlocks_next_day(self):
        self.guard.roll_day(datetime(2026, 7, 14, 14, tzinfo=UTC), 10000)
        self.assertTrue(self.guard.check(9500))
        self.guard.roll_day(datetime(2026, 7, 15, 0, tzinfo=UTC), 9500)
        self.assertFalse(self.guard.check(9400))   # new reference 9500

    def test_state_persists_across_restart(self):
        self.guard.roll_day(datetime(2026, 7, 14, 14, tzinfo=UTC), 10000)
        self.guard.check(9500)
        reloaded = sb.DayGuard(self.path)
        self.assertTrue(reloaded.locked)
        self.assertEqual(reloaded.day_balance, 10000)


# --- MT5 orders (mocked) ---------------------------------------------------------
class TestOrders(unittest.TestCase):
    def setUp(self):
        fake_mt5.reset_mock()
        fake_mt5.order_send.return_value = OK_RESULT
        fake_mt5.positions_get.return_value = []
        fake_mt5.orders_get.return_value = []
        fake_mt5.account_info.return_value = SimpleNamespace(
            balance=10000.0, equity=10000.0, login=1, currency="USD")
        fake_mt5.symbol_info.return_value = SimpleNamespace(
            trade_tick_size=0.01, trade_tick_value=0.01, volume_min=0.01,
            volume_max=100.0, volume_step=0.01, digits=2)
        fake_mt5.symbol_info_tick.return_value = SimpleNamespace(
            ask=2000.0, bid=1999.8)
        # 30 M30 candles, high-low = 2 -> ATR = 2 -> SL_distance = 3.0
        fake_mt5.copy_rates_from_pos.return_value = [
            {"time": 1752400000 + i * 1800, "open": 2000.0, "high": 2001.0,
             "low": 1999.0, "close": 2000.0} for i in range(30)]

    def test_open_trade_buy_has_sl_tp_and_dynamic_lot(self):
        self.assertTrue(sb.open_trade(XAU, "BUY", XAU_MB,
                                      "test"))
        req = fake_mt5.order_send.call_args[0][0]
        # loss/lot = (3 / 0.01) * 0.01 = $3; 1.5% of 10000 = $150 -> 50 lots
        self.assertEqual(req["volume"], 50.0)
        self.assertEqual(req["sl"], 1997.0)          # 2000 - 1.5*ATR
        self.assertEqual(req["tp"], 2006.0)          # RR 1:2
        self.assertEqual(req["type"], fake_mt5.ORDER_TYPE_BUY)

    def test_open_trade_sell_sl_tp_mirrored(self):
        self.assertTrue(sb.open_trade(XAU, "SELL", XAU_MR,
                                      "test"))
        req = fake_mt5.order_send.call_args[0][0]
        self.assertEqual(req["sl"], 2002.8)          # 1999.8 + 3.0
        self.assertEqual(req["tp"], 1993.8)          # 1999.8 - 6.0

    def test_no_trade_when_lot_is_zero(self):
        fake_mt5.account_info.return_value = SimpleNamespace(
            balance=1.0, equity=1.0, login=1, currency="USD")
        self.assertFalse(sb.open_trade(XAU, "BUY", XAU_MB,
                                       "test"))
        fake_mt5.order_send.assert_not_called()

    def test_partial_close_and_breakeven_at_1r(self):
        pos = SimpleNamespace(ticket=7, symbol=XAU, type=0, volume=1.0,
                              price_open=2000.0, sl=1997.0, tp=2006.0,
                              price_current=2003.0, magic=XAU_MB)
        fake_mt5.positions_get.return_value = [pos]
        sb.manage_positions(XAU, (XAU_MB, XAU_MR))
        reqs = [c[0][0] for c in fake_mt5.order_send.call_args_list]
        close = next(r for r in reqs
                     if r["action"] == fake_mt5.TRADE_ACTION_DEAL)
        be = next(r for r in reqs
                  if r["action"] == fake_mt5.TRADE_ACTION_SLTP)
        self.assertEqual(close["volume"], 0.5)       # 50% of the position
        self.assertEqual(close["position"], 7)
        self.assertEqual(be["sl"], 2000.0)           # break-even

    def test_no_management_before_1r_or_after_be(self):
        early = SimpleNamespace(ticket=8, symbol=XAU, type=0, volume=1.0,
                                price_open=2000.0, sl=1997.0, tp=2006.0,
                                price_current=2001.0, magic=XAU_MB)
        done = SimpleNamespace(ticket=9, symbol=XAU, type=0, volume=0.5,
                               price_open=2000.0, sl=2000.0, tp=2006.0,
                               price_current=2005.0, magic=XAU_MB)
        fake_mt5.positions_get.return_value = [early, done]
        sb.manage_positions(XAU, (XAU_MB, XAU_MR))
        fake_mt5.order_send.assert_not_called()


# --- Main loop (integration) ------------------------------------------------------
class TestRunCycle(unittest.TestCase):
    def setUp(self):
        fake_mt5.reset_mock()
        fake_mt5.order_send.return_value = OK_RESULT
        fake_mt5.positions_get.return_value = []
        fake_mt5.orders_get.return_value = []
        fake_mt5.symbol_info_tick.return_value = SimpleNamespace(
            ask=2000.0, bid=1999.8)
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.path)
        self.guard = sb.DayGuard(self.path)
        self.macro = mock.MagicMock()
        self.macro.vix.return_value = 15.0

    def tearDown(self):
        if os.path.exists(self.path):
            os.unlink(self.path)

    def test_circuit_breaker_closes_everything(self):
        fake_mt5.account_info.return_value = SimpleNamespace(
            balance=10000.0, equity=9500.0)  # -5% intraday
        self.guard.day = "2026-07-14"
        self.guard.day_balance = 10000.0
        pos_xau = SimpleNamespace(ticket=3, symbol=XAU, type=0, volume=1.0,
                                  price_open=2000.0, sl=1997.0, tp=2006.0,
                                  price_current=1995.0, magic=XAU_MB)
        pos_eur = SimpleNamespace(ticket=5, symbol="EURUSD.p", type=1,
                                  volume=0.5, price_open=1.1, sl=1.11,
                                  tp=1.08, price_current=1.105, magic=2002)
        order = SimpleNamespace(ticket=4)
        fake_mt5.positions_get.return_value = [pos_xau, pos_eur]
        fake_mt5.orders_get.return_value = [order]
        sb.run_cycle(ACTIVE, self.guard, self.macro, {},
                     now=datetime(2026, 7, 14, 14, tzinfo=UTC))
        self.assertTrue(self.guard.locked)
        # global close: positions_get() without a symbol filter
        self.assertEqual(fake_mt5.positions_get.call_args, mock.call())
        reqs = [c[0][0] for c in fake_mt5.order_send.call_args_list]
        self.assertTrue(any(r.get("position") == 3 for r in reqs))
        self.assertTrue(any(r.get("position") == 5 for r in reqs))
        self.assertTrue(any(r.get("action") == fake_mt5.TRADE_ACTION_REMOVE
                            and r.get("order") == 4 for r in reqs))
        fake_mt5.copy_rates_from_pos.assert_not_called()  # no more signals

    def test_no_new_positions_outside_trading_hours(self):
        # 19:00 UTC: outside breakout (8-16) AND reversion (13-18) windows
        fake_mt5.account_info.return_value = SimpleNamespace(
            balance=10000.0, equity=10000.0)
        with mock.patch.object(ss, "FORCE_TRADING_HOURS", False):
            sb.run_cycle(ACTIVE, self.guard, self.macro, {},
                         now=datetime(2026, 7, 14, 19, 0, tzinfo=UTC))
        fake_mt5.copy_rates_from_pos.assert_not_called()
        fake_mt5.order_send.assert_not_called()

    def test_production_config_suspends_breakout_on_forex(self):
        # decision of 2026-07-15 (docs/AMELIORATION_CONTINUE.md section 5)
        self.assertTrue(sb.CONFIG_PORTFOLIO["XAUUSD"]["breakout"])
        self.assertFalse(sb.CONFIG_PORTFOLIO["EURUSD"]["breakout"])
        self.assertFalse(sb.CONFIG_PORTFOLIO["GBPUSD"]["breakout"])

    def test_breakout_disabled_symbol_skips_m30_scan(self):
        active = {"EURUSD": {"symbol": "EURUSD.p", "magic_breakout": 2001,
                             "magic_reversion": 2002, "vix_filter": False,
                             "breakout": False}}
        fake_mt5.account_info.return_value = SimpleNamespace(
            balance=10000.0, equity=10000.0)
        with mock.patch.object(ss, "FORCE_TRADING_HOURS", False):
            sb.run_cycle(active, self.guard, self.macro, {},
                         now=datetime(2026, 7, 14, 10, 0, tzinfo=UTC))
        fake_mt5.copy_rates_from_pos.assert_not_called()   # breakout window

    def test_breakout_disabled_keeps_reversion(self):
        active = {"EURUSD": {"symbol": "EURUSD.p", "magic_breakout": 2001,
                             "magic_reversion": 2002, "vix_filter": False,
                             "breakout": False}}
        fake_mt5.account_info.return_value = SimpleNamespace(
            balance=10000.0, equity=10000.0)
        fake_mt5.copy_rates_from_pos.return_value = [
            {"time": 1752400000 + i * 300, "open": 2000.0, "high": 2001.0,
             "low": 1999.0, "close": 2000.0} for i in range(40)]
        with mock.patch.object(ss, "FORCE_TRADING_HOURS", False):
            sb.run_cycle(active, self.guard, self.macro, {},
                         now=datetime(2026, 7, 14, 14, 0, tzinfo=UTC))
        tfs = {c[0][1] for c in fake_mt5.copy_rates_from_pos.call_args_list}
        self.assertEqual(tfs, {fake_mt5.TIMEFRAME_M5})     # M30 never read

    def test_windows_differ_per_strategy(self):
        # 10:00 UTC: breakout active (M30 scanned), reversion closed (no M5)
        fake_mt5.account_info.return_value = SimpleNamespace(
            balance=10000.0, equity=10000.0)
        fake_mt5.copy_rates_from_pos.return_value = [
            {"time": 1752400000 + i * 1800, "open": 2000.0, "high": 2001.0,
             "low": 1999.0, "close": 2000.0} for i in range(30)]
        with mock.patch.object(ss, "FORCE_TRADING_HOURS", False):
            sb.run_cycle(ACTIVE, self.guard, self.macro, {},
                         now=datetime(2026, 7, 14, 10, 0, tzinfo=UTC))
        tfs = {c[0][1] for c in fake_mt5.copy_rates_from_pos.call_args_list}
        self.assertEqual(tfs, {fake_mt5.TIMEFRAME_M30})

    def test_high_vix_blocks_sell_only_on_gold(self):
        # VIX 30 + SELL signal everywhere: XAUUSD blocked, EURUSD/GBPUSD pass
        self.macro.vix.return_value = 30.0
        fake_mt5.account_info.return_value = SimpleNamespace(
            balance=10000.0, equity=10000.0)
        fake_mt5.copy_rates_from_pos.return_value = [
            {"time": 1752400000 + i * 1800, "open": 2000.0, "high": 2001.0,
             "low": 1999.0, "close": 2000.0} for i in range(30)]
        with mock.patch.object(sb, "open_trade") as ot, \
             mock.patch.object(sb, "breakout_signal", return_value="SELL"), \
             mock.patch.object(sb, "reversion_signal", return_value=None):
            sb.run_cycle(ACTIVE, self.guard, self.macro, {},
                         now=datetime(2026, 7, 14, 14, tzinfo=UTC))
            symbols = {c[0][0] for c in ot.call_args_list}
            self.assertEqual(symbols, {"EURUSD.p", "GBPUSD.p"})

    def test_signal_evaluated_once_per_closed_bar(self):
        fake_mt5.account_info.return_value = SimpleNamespace(
            balance=10000.0, equity=10000.0)
        fake_mt5.copy_rates_from_pos.return_value = [
            {"time": 1752400000 + i * 1800, "open": 2000.0, "high": 2001.0,
             "low": 1999.0, "close": 2000.0} for i in range(30)]
        last_bars = {}
        now = datetime(2026, 7, 14, 14, tzinfo=UTC)
        with mock.patch.object(sb, "open_trade") as ot, \
             mock.patch.object(sb, "breakout_signal", return_value="BUY"), \
             mock.patch.object(sb, "reversion_signal", return_value=None):
            sb.run_cycle(ACTIVE, self.guard, self.macro, last_bars, now=now)
            sb.run_cycle(ACTIVE, self.guard, self.macro, last_bars, now=now)
            # 1 trade per asset (3), no re-trade on the same candle
            self.assertEqual(ot.call_count, 3)
            symbols = {c[0][0] for c in ot.call_args_list}
            self.assertEqual(symbols, {"XAUUSD.p", "EURUSD.p", "GBPUSD.p"})


class TestPortfolio(unittest.TestCase):
    def setUp(self):
        fake_mt5.reset_mock()
        fake_mt5.order_send.return_value = OK_RESULT
        fake_mt5.symbol_info_tick.return_value = SimpleNamespace(
            ask=2000.0, bid=1999.8)

    def test_resolve_symbols_skips_missing_pair(self):
        avail = {"XAUUSD.p", "EURUSD.p"}  # GBPUSD missing at the broker
        fake_mt5.symbol_select.side_effect = lambda s, e=True: s in avail
        fake_mt5.symbol_info.side_effect = (
            lambda s: SimpleNamespace(name=s) if s in avail else None)
        self.addCleanup(setattr, fake_mt5.symbol_select, "side_effect", None)
        self.addCleanup(setattr, fake_mt5.symbol_info, "side_effect", None)
        with self.assertLogs("sentinel", level="WARNING") as cm:
            active = sb.resolve_symbols()
        self.assertEqual(set(active), {"XAUUSD", "EURUSD"})
        self.assertEqual(active["XAUUSD"]["symbol"], "XAUUSD.p")  # .p fallback
        self.assertEqual(active["EURUSD"]["magic_breakout"], 2001)
        self.assertTrue(active["XAUUSD"]["vix_filter"])
        self.assertFalse(active["EURUSD"]["vix_filter"])
        self.assertIn("GBPUSD", cm.output[0])

    def test_management_isolated_by_symbol_and_magic(self):
        # a EURUSD position at 1R and a XAU position with a foreign magic
        # must trigger NO management during the XAU scan
        eur = SimpleNamespace(ticket=11, symbol="EURUSD.p", type=0,
                              volume=1.0, price_open=1.10, sl=1.09, tp=1.12,
                              price_current=1.111, magic=2001)
        foreign = SimpleNamespace(ticket=12, symbol=XAU, type=0, volume=1.0,
                                  price_open=2000.0, sl=1997.0, tp=2006.0,
                                  price_current=2003.0, magic=9999)
        fake_mt5.positions_get.return_value = [eur, foreign]
        sb.manage_positions(XAU, (XAU_MB, XAU_MR))
        fake_mt5.order_send.assert_not_called()


class TestMacroFilterFetch(unittest.TestCase):
    def test_vix_fetch_failure_returns_none_and_caches(self):
        mf = sb.MacroFilter()
        now = datetime(2026, 7, 14, 13, tzinfo=UTC)
        with mock.patch.object(sb.yf, "Ticker",
                               side_effect=RuntimeError("net")) as tk:
            self.assertIsNone(mf.vix(now))
            self.assertIsNone(mf.vix(now))          # no re-fetch the same day
            self.assertEqual(tk.call_count, 1)

    def test_vix_fetched_once_per_day(self):
        mf = sb.MacroFilter()
        hist = pd.DataFrame({"Close": [27.5]})
        ticker = mock.MagicMock()
        ticker.history.return_value = hist
        with mock.patch.object(sb.yf, "Ticker", return_value=ticker) as tk:
            d1 = datetime(2026, 7, 14, 13, tzinfo=UTC)
            self.assertEqual(mf.vix(d1), 27.5)
            self.assertEqual(mf.vix(d1), 27.5)
            self.assertEqual(tk.call_count, 1)
            mf.vix(datetime(2026, 7, 15, 13, tzinfo=UTC))
            self.assertEqual(tk.call_count, 2)      # new day -> re-fetch

# --- Atomic persistence & heartbeat ------------------------------------------
class TestPersistence(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.path)
        self.addCleanup(lambda: os.path.exists(self.path)
                        and os.unlink(self.path))

    def test_save_atomic_reload_and_no_tmp(self):
        g = sb.DayGuard(self.path)
        g.day, g.day_balance = "2026-07-15", 10000.0
        g._save()
        self.assertEqual(sb.DayGuard(self.path).day, "2026-07-15")
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_save_failure_preserves_previous_state(self):
        g = sb.DayGuard(self.path)
        g.day, g.locked = "2026-07-15", False
        g._save()
        g.locked = True
        with mock.patch.object(sb.os, "replace",
                               side_effect=OSError("disk full")):
            g._save()                      # swallows the error, state intact
        self.assertFalse(sb.DayGuard(self.path).locked)

    def test_write_heartbeat(self):
        path = os.path.join(tempfile.mkdtemp(), "sentinel_bot.hb")
        now = datetime(2026, 7, 15, 12, tzinfo=UTC)
        sb.write_heartbeat(path, now)
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), now.isoformat())


if __name__ == "__main__":
    unittest.main(verbosity=2)
