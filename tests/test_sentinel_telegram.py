"""Tests SENTINEL TELEGRAM (MT5 et requests mockes).

Executer :  python -m unittest test_sentinel_telegram -v
"""

import json
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
fake_mt5.POSITION_TYPE_BUY = 0
fake_mt5.POSITION_TYPE_SELL = 1
if not isinstance(sys.modules.get("requests"), mock.MagicMock):
    sys.modules["requests"] = mock.MagicMock()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots"))
import sentinel_telegram as tg  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 7, 15, 19, 0, tzinfo=UTC)


def _row(pnl, days_ago=0, strategy="breakout", symbol="XAUUSD.p"):
    return {"pnl": pnl, "strategy": strategy, "symbol": symbol,
            "close_time": NOW - timedelta(days=days_ago)}


class TestPnlSummary(unittest.TestCase):
    def test_known_windows_and_strategies(self):
        rows = [_row(100.0), _row(-30.0, days_ago=3, strategy="trend"),
                _row(50.0, days_ago=20), _row(-40.0, days_ago=45)]
        s = tg.pnl_summary(rows, NOW)
        self.assertEqual(s["day"], 100.0)
        self.assertEqual(s["d7"], 70.0)
        self.assertEqual(s["d30"], 120.0)
        self.assertEqual(s["total"], 80.0)
        self.assertEqual(s["count"], 4)
        self.assertEqual(s["by_strategy"]["breakout"]["pnl"], 110.0)
        self.assertEqual(s["by_strategy"]["trend"]["count"], 1)

    def test_empty(self):
        s = tg.pnl_summary([], NOW)
        self.assertEqual((s["total"], s["count"]), (0.0, 0))

    def test_message_contains_key_figures(self):
        msg = tg.format_pnl_message(tg.pnl_summary([_row(80.0)], NOW))
        self.assertIn("+80.00", msg)
        self.assertIn("breakout", msg)


class TestTradesCsv(unittest.TestCase):
    def test_read_trades_csv(self):
        path = os.path.join(tempfile.mkdtemp(), "trades.csv")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("close_time,open_time,strategy,symbol,direction,"
                     "volume,pnl,duration_h,magic,position_id\n"
                     "2026-07-14T12:30:01+00:00,2026-07-14T11:35:10+00:00,"
                     "breakout,GBPUSD,long,1.35,292.19,0.91,3001,79\n")
        rows = tg.read_trades(path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["pnl"], 292.19)
        self.assertEqual(rows[0]["symbol"], "GBPUSD")
        self.assertEqual(rows[0]["close_time"].hour, 12)

    def test_read_trades_missing_file(self):
        self.assertEqual(tg.read_trades("nulle_part.csv"), [])


class TestDealsAndLocks(unittest.TestCase):
    def test_new_closing_deals_filters(self):
        t0 = int(NOW.timestamp())
        deals = [
            SimpleNamespace(entry=1, magic=1001, time=t0, profit=5.0,
                            commission=0.0, swap=0.0, symbol="XAUUSD"),
            SimpleNamespace(entry=0, magic=1001, time=t0),        # entree
            SimpleNamespace(entry=1, magic=9999, time=t0),        # etranger
            SimpleNamespace(entry=1, magic=4001, time=t0 - 999),  # deja vu
        ]
        out = tg.new_closing_deals(deals, since=t0 - 1)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].symbol, "XAUUSD")

    def test_active_locks_reads_state_files(self):
        d = tempfile.mkdtemp()
        for name, locked in (("alpha_state.json", True),
                             ("trend_state.json", False)):
            with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
                json.dump({"locked": locked}, fh)
        locks = tg.active_locks(d)
        self.assertEqual(len(locks), 1)
        self.assertIn("bot 2", locks[0])

    def test_should_send_daily(self):
        self.assertTrue(tg.should_send_daily("2026-07-14", NOW))   # 19h > 18h
        self.assertFalse(tg.should_send_daily("2026-07-15", NOW))  # deja fait
        early = NOW.replace(hour=int(tg.DAILY_REPORT_HOUR) - 1)
        self.assertFalse(tg.should_send_daily("2026-07-14", early))


class TestEntryWindows(unittest.TestCase):
    """/status : chaque strategie peut-elle ouvrir un trade maintenant ?"""

    @staticmethod
    def _at(hour):
        return "\n".join(tg.entry_status_lines(
            NOW.replace(hour=hour, minute=30)))

    def test_registry_matches_bot_configs(self):
        # breakout 8-16, reversion 13-18 (bot 1), statarb 7-20 (bot 2),
        # trend : blackout rollover 21-23 (bot 3)
        by_strat = {w["strategy"]: (w["start"], w["end"])
                    for w in tg.ENTRY_WINDOWS}
        self.assertEqual(by_strat["breakout (bot 1)"], (8, 16))
        self.assertEqual(by_strat["reversion (bot 1)"], (13, 18))
        self.assertEqual(by_strat["statarb (bot 2)"], (7, 20))
        self.assertEqual(by_strat["trend (bot 3)"], (23, 21))

    def test_morning_10h(self):
        txt = self._at(10)
        self.assertIn("breakout (bot 1) : peut trader jusqu'a 16:00", txt)
        self.assertIn("reversion (bot 1) : ⏳ fenetre fermee, "
                      "ouvre a 13:00", txt)
        self.assertIn("statarb (bot 2) : peut trader jusqu'a 20:00", txt)
        self.assertIn("trend (bot 3) : peut trader", txt)

    def test_rollover_22h_only_trend_blackout_applies(self):
        txt = self._at(22)
        self.assertIn("trend (bot 3) : ⏳ fenetre fermee, ouvre a 23:00", txt)
        self.assertIn("breakout (bot 1) : ⏳ fenetre fermee, "
                      "ouvre a 08:00", txt)

    def test_wrap_around_trend_open_after_23h(self):
        self.assertIn("trend (bot 3) : peut trader jusqu'a 21:00",
                      self._at(23))
        self.assertIn("trend (bot 3) : peut trader jusqu'a 21:00",
                      self._at(3))

    def test_breakout_note_mentions_suspensions(self):
        self.assertIn("XAUUSD uniquement, EURUSD/GBPUSD suspendus",
                      self._at(10))

    def test_status_text_includes_windows_section(self):
        fake_mt5.account_info.return_value = SimpleNamespace(
            equity=10000.0, currency="EUR", balance=10000.0)
        fake_mt5.positions_get.return_value = []
        with mock.patch.object(tg, "bots_processes", return_value={}), \
             mock.patch.object(tg, "active_locks", return_value=[]):
            txt = tg.status_text(NOW)
        self.assertIn("Fenetres d'entree (UTC) :", txt)
        self.assertIn("statarb (bot 2)", txt)


class TestSuspensions(unittest.TestCase):
    """Suivi des couples suspendus/reduits dans le rapport quotidien."""

    def test_registry_matches_research_decisions(self):
        # decisions du 2026-07-15 (AMELIORATION_CONTINUE.md section 5)
        couples = {(s["strategy"], s["symbol"], s["action"])
                   for s in tg.SUSPENSIONS}
        self.assertIn(("breakout", "EURUSD", "suspendu"), couples)
        self.assertIn(("breakout", "GBPUSD", "suspendu"), couples)
        self.assertIn(("trend", "XTIUSD", "risque /2"), couples)
        self.assertEqual(len(tg.SUSPENSIONS), 5)

    def test_counts_trades_since_decision_with_aliases(self):
        rows = [
            _row(10.0, days_ago=0, strategy="trend", symbol="EURUSD.p"),
            _row(-5.0, days_ago=0, strategy="trend", symbol="SpotCrude"),
            _row(10.0, days_ago=40, strategy="trend",
                 symbol="EURUSD.p"),                    # avant la decision
            _row(10.0, days_ago=0, strategy="breakout",
                 symbol="EURUSD.p"),                    # autre strategie
        ]
        lines = "\n".join(tg.suspension_lines(rows, NOW))
        self.assertIn("trend EURUSD : risque /2 depuis le 2026-07-15, "
                      "1 trades depuis", lines)
        self.assertIn("trend XTIUSD : risque /2 depuis le 2026-07-15, "
                      "1 trades depuis", lines)
        self.assertIn("breakout GBPUSD : suspendu depuis le 2026-07-15, "
                      "0 trades depuis", lines)

    def test_review_date_and_overdue_flag(self):
        lines = "\n".join(tg.suspension_lines([], NOW))
        self.assertIn("reevaluation le 2026-10-14", lines)   # +91 jours
        self.assertNotIn("ECHUE", lines)
        later = NOW + timedelta(days=120)
        self.assertIn("ECHUE", "\n".join(tg.suspension_lines([], later)))

    def test_daily_report_includes_surveillance_section(self):
        fd, state = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(state)
        n = tg.TelegramNotifier("TOKEN", state_file=state)
        n.chat_id = 4242
        n.last_report_day = "2026-07-14"
        n.api = mock.MagicMock(return_value={})
        fake_mt5.account_info.return_value = SimpleNamespace(
            equity=10000.0, currency="EUR", balance=10000.0)
        with mock.patch.object(tg, "read_trades", return_value=[]):
            tg.maybe_daily_report(n, NOW)                # 19h : rapport du
        sent = [c for c in n.api.call_args_list
                if c[0][0] == "sendMessage"][0]
        self.assertIn("Couples sous surveillance", sent[1]["text"])
        self.assertIn("breakout EURUSD", sent[1]["text"])
        os.path.exists(state) and os.unlink(state)


class TestNotifier(unittest.TestCase):
    def setUp(self):
        fd, self.state = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.state)
        self.n = tg.TelegramNotifier("TOKEN", state_file=self.state)
        self.n.api = mock.MagicMock(return_value={"result": []})
        self.addCleanup(lambda: os.path.exists(self.state)
                        and os.unlink(self.state))

    def test_chat_id_captured_on_first_message_and_persisted(self):
        self.n.api.return_value = {"result": [
            {"update_id": 7, "message": {"text": "/start",
                                         "chat": {"id": 4242}}}]}
        self.n.poll_commands()
        self.assertEqual(self.n.chat_id, 4242)
        self.assertEqual(self.n.last_update_id, 7)
        again = tg.TelegramNotifier("TOKEN", state_file=self.state)
        self.assertEqual(again.chat_id, 4242)

    def test_foreign_chat_ignored(self):
        self.n.chat_id = 4242
        self.n.api.return_value = {"result": [
            {"update_id": 8, "message": {"text": "/pnl",
                                         "chat": {"id": 666}}}]}
        with mock.patch.object(tg, "read_trades", return_value=[]):
            self.n.poll_commands()
        sends = [c for c in self.n.api.call_args_list
                 if c[0][0] == "sendMessage"]
        self.assertEqual(sends, [])

    def test_pnl_command_sends_summary(self):
        self.n.chat_id = 4242
        self.n.api.return_value = {"result": [
            {"update_id": 9, "message": {"text": "/pnl",
                                         "chat": {"id": 4242}}}]}
        with mock.patch.object(tg, "read_trades",
                               return_value=[_row(146.82)]):
            self.n.poll_commands()
        sent = [c for c in self.n.api.call_args_list
                if c[0][0] == "sendMessage"][0]
        self.assertIn("+146.82", sent[1]["text"])

    def test_write_heartbeat(self):
        path = os.path.join(tempfile.mkdtemp(), "telegram.hb")
        now = datetime(2026, 7, 15, 12, tzinfo=UTC)
        tg.write_heartbeat(path, now)
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), now.isoformat())

    def test_send_without_chat_id_is_noop(self):
        self.n.send("bonjour")
        self.n.api.assert_not_called()

    def test_closed_deal_notification(self):
        t0 = int(NOW.timestamp())
        self.n.chat_id = 4242
        self.n.last_deal_ts = t0 - 60
        fake_mt5.history_deals_get.return_value = [
            SimpleNamespace(entry=1, magic=3001, time=t0, profit=292.19,
                            commission=-3.0, swap=0.0, symbol="GBPUSD")]
        tg.check_closed_deals(self.n, NOW)
        sent = [c for c in self.n.api.call_args_list
                if c[0][0] == "sendMessage"][0]
        self.assertIn("GBPUSD", sent[1]["text"])
        self.assertIn("breakout", sent[1]["text"])
        self.assertEqual(self.n.last_deal_ts, t0)

    def test_open_position_notification(self):
        self.n.chat_id = 4242
        self.n.open_tickets = [11]
        fake_mt5.positions_get.return_value = [
            SimpleNamespace(ticket=11, symbol="XAUUSD", magic=1001, type=0,
                            volume=0.12, profit=0.0),
            SimpleNamespace(ticket=12, symbol="EURUSD", magic=2002, type=1,
                            volume=1.0, profit=0.0),
            SimpleNamespace(ticket=13, symbol="EURUSD", magic=777, type=1,
                            volume=1.0, profit=0.0)]      # etranger : ignore
        tg.check_position_events(self.n)
        sends = [c for c in self.n.api.call_args_list
                 if c[0][0] == "sendMessage"]
        self.assertEqual(len(sends), 1)
        self.assertIn("EURUSD", sends[0][1]["text"])
        self.assertIn("SHORT", sends[0][1]["text"])
        self.assertEqual(sorted(self.n.open_tickets), [11, 12])


if __name__ == "__main__":
    unittest.main(verbosity=2)
