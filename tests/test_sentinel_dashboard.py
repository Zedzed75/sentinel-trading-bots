"""SENTINEL DASHBOARD tests (MT5 and psutil mocked, files in tempdir).

Run:  python -m unittest test_sentinel_dashboard -v
Core guarantee: a missing, empty or corrupt JSON never crashes the
interface (build_state always answers).
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

if not isinstance(sys.modules.get("MetaTrader5"), mock.MagicMock):
    sys.modules["MetaTrader5"] = mock.MagicMock()
fake_mt5 = sys.modules["MetaTrader5"]
fake_mt5.POSITION_TYPE_BUY = 0
fake_mt5.POSITION_TYPE_SELL = 1

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import sentinel_dashboard as dash  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


def tmpfile(content: str | None, suffix=".json") -> str:
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    if content is None:
        os.unlink(path)                    # missing file
    else:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
    return path


class TestRobustReads(unittest.TestCase):
    """Missing / empty / corrupt files: never an exception."""

    def test_load_json_missing_empty_corrupt(self):
        self.assertEqual(dash.load_json(tmpfile(None)), {})
        self.assertEqual(dash.load_json(tmpfile("")), {})
        self.assertEqual(dash.load_json(tmpfile("{not json")), {})
        self.assertEqual(dash.load_json(tmpfile("[1, 2]")), {})  # not an object
        self.assertEqual(dash.load_json(tmpfile('{"a": 1}')), {"a": 1})

    def test_read_trades_missing_and_corrupt(self):
        self.assertEqual(dash.read_trades(tmpfile(None, ".csv")), [])
        self.assertEqual(dash.read_trades(tmpfile("", ".csv")), [])
        # valid header but rotten lines: ignored without crashing
        path = tmpfile("close_time,strategy,pnl\n"
                       "not-a-date,breakout,12.5\n"
                       "2026-07-15T10:00:00+00:00,breakout,not-a-number\n"
                       "2026-07-15T10:00:00+00:00,breakout,42.0\n", ".csv")
        rows = dash.read_trades(path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["pnl"], 42.0)

    def test_build_state_survives_everything_missing(self):
        fake_mt5.account_info.return_value = None
        fake_mt5.positions_get.return_value = None
        with mock.patch.object(dash, "BOTS_DIR", tempfile.mkdtemp()), \
             mock.patch.object(dash, "LOG_DIR", tempfile.mkdtemp()), \
             mock.patch.object(dash, "TRADES_CSV", "missing.csv"):
            state = dash.build_state(NOW)
        self.assertFalse(state["account"]["ok"])
        self.assertEqual(len(state["bots"]), 8)
        self.assertTrue(all(b["status"] == "stopped" for b in state["bots"]))
        self.assertIsNone(state["daily_gauge"]["pct"])
        self.assertFalse(state["global_lock"])
        self.assertEqual(state["positions"], [])


class TestStatusLogic(unittest.TestCase):
    def test_bot_status_priorities(self):
        self.assertEqual(dash.bot_status(10, 300, locked=True), "suspended")
        self.assertEqual(dash.bot_status(10, 300, locked=False), "active")
        self.assertEqual(dash.bot_status(301, 300, locked=False), "frozen")
        self.assertEqual(dash.bot_status(None, 300, locked=False), "stopped")

    def test_heartbeat_age(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "sentinel_bot.hb"), "w",
                  encoding="utf-8") as fh:
            fh.write((NOW - timedelta(seconds=42)).isoformat())
        with mock.patch.object(dash, "LOG_DIR", d):
            self.assertAlmostEqual(
                dash.heartbeat_age("sentinel_bot.py", NOW), 42.0)
            self.assertIsNone(dash.heartbeat_age("sentinel_trend.py", NOW))

    def test_day_stats_filters_today(self):
        trades = [
            {"pnl": 10.0, "strategy": "trend", "close_time": NOW},
            {"pnl": -4.0, "strategy": "trend", "close_time": NOW},
            {"pnl": 99.0, "strategy": "trend",
             "close_time": NOW - timedelta(days=1)},
        ]
        st = dash.day_stats(trades, NOW)
        self.assertEqual(st["trend"], {"pnl": 6.0, "n": 2})


class TestGaugeAndAccount(unittest.TestCase):
    def test_daily_gauge_values(self):
        g = dash.daily_gauge(9800.0, 10000.0)      # -2%: half the threshold
        self.assertEqual(g["pct"], -2.0)
        self.assertEqual(g["used"], 0.5)
        self.assertEqual(dash.daily_gauge(10400.0, 10000.0)["used"], 0.0)
        self.assertEqual(dash.daily_gauge(9000.0, 10000.0)["used"], 1.0)
        self.assertIsNone(dash.daily_gauge(None, 10000.0)["pct"])
        self.assertIsNone(dash.daily_gauge(9800.0, None)["pct"])

    def test_margin_alert_flag(self):
        fake_mt5.account_info.return_value = SimpleNamespace(
            balance=1e4, equity=1e4, margin_free=100.0, margin_level=120.0,
            currency="EUR")
        fake_mt5.positions_get.return_value = []
        with mock.patch.object(dash, "BOTS_DIR", tempfile.mkdtemp()), \
             mock.patch.object(dash, "LOG_DIR", tempfile.mkdtemp()), \
             mock.patch.object(dash, "TRADES_CSV", "missing.csv"):
            self.assertTrue(dash.build_state(NOW)["margin_alert"])

    def test_positions_filtered_by_magic(self):
        fake_mt5.positions_get.return_value = [
            SimpleNamespace(ticket=1, symbol="XAUUSD", type=0, volume=0.1,
                            profit=12.34, magic=1001),
            SimpleNamespace(ticket=2, symbol="EURUSD", type=1, volume=1.0,
                            profit=-5.0, magic=777),        # foreign
        ]
        pos = dash.open_positions()
        self.assertEqual(len(pos), 1)
        self.assertEqual(pos[0]["side"], "LONG")
        self.assertEqual(pos[0]["strategy"], "breakout")


class TestWeather(unittest.TestCase):
    """Bot 7's weather: robust read + stale flag."""

    def test_read_weather_missing_or_corrupt_is_none(self):
        with mock.patch.object(dash, "BOTS_DIR", tempfile.mkdtemp()):
            self.assertIsNone(dash.read_weather(NOW))

    def test_read_weather_valid_and_stale_flag(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "macro_weather.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"weather": "STORMY", "confidence": 0.76,
                       "focus": "CPI", "date": "2026-07-15"}, fh)
        with mock.patch.object(dash, "BOTS_DIR", d):
            w = dash.read_weather(NOW)                # NOW = 2026-07-15
            self.assertEqual(w["weather"], "STORMY")
            self.assertFalse(w["stale"])
            w2 = dash.read_weather(NOW + timedelta(days=3))
            self.assertTrue(w2["stale"])              # weather of another day

    def test_read_weather_migrates_legacy_french_file(self):
        # macro_weather.json written before the English migration
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "macro_weather.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"weather": "ORAGEUX", "confidence": 0.7,
                       "focus": "CPI", "geo_resume": "risk premium",
                       "date": "2026-07-15"}, fh)
        with mock.patch.object(dash, "BOTS_DIR", d):
            w = dash.read_weather(NOW)
        self.assertEqual(w["weather"], "STORMY")
        self.assertEqual(w["geo_summary"], "risk premium")

    def test_build_state_includes_weather_none_as_skeleton(self):
        fake_mt5.account_info.return_value = None
        fake_mt5.positions_get.return_value = None
        with mock.patch.object(dash, "BOTS_DIR", tempfile.mkdtemp()), \
             mock.patch.object(dash, "LOG_DIR", tempfile.mkdtemp()), \
             mock.patch.object(dash, "TRADES_CSV", "missing.csv"):
            self.assertIsNone(dash.build_state(NOW)["weather"])


class TestActions(unittest.TestCase):
    """PANIC (close all + global lock) and FORCE RUN bot 7."""

    def setUp(self):
        self.client = TestClient(dash.app)
        self.tmp = tempfile.mkdtemp()
        self.auth = ("sentinel", "bon")
        self.creds = mock.patch.object(dash, "_credentials",
                                       return_value=self.auth)
        self.creds.start()
        self.addCleanup(self.creds.stop)

    def test_actions_require_auth(self):
        self.assertEqual(self.client.post("/api/panic").status_code, 401)
        self.assertEqual(self.client.post("/api/forcerun").status_code, 401)

    def test_panic_closes_positions_and_locks(self):
        fake_mt5.positions_get.return_value = [
            SimpleNamespace(ticket=1, symbol="XAUUSD", type=0, volume=0.1,
                            profit=0.0, magic=1001),
            SimpleNamespace(ticket=2, symbol="EURUSD", type=1, volume=1.0,
                            profit=0.0, magic=777),          # foreign
        ]
        fake_mt5.symbol_info_tick.return_value = SimpleNamespace(
            ask=4100.0, bid=4099.5)
        fake_mt5.order_send.return_value = SimpleNamespace(retcode=10009)
        fake_mt5.TRADE_RETCODE_DONE = 10009
        with mock.patch.object(dash, "BOTS_DIR", self.tmp), \
             mock.patch.object(dash.psutil, "process_iter",
                               return_value=[]):
            r = self.client.post("/api/panic", auth=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertIn("1 position(s) closed", r.text)
        lock = dash.load_json(os.path.join(self.tmp,
                                           "orchestrator_state.json"))
        self.assertTrue(lock["locked"])               # global lock engaged

    def test_forcerun_spawns_bot7_once(self):
        with mock.patch.object(dash.subprocess, "Popen") as popen:
            r = self.client.post("/api/forcerun", auth=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Bot 7", r.text)
        args = popen.call_args[0][0]
        self.assertIn("sentinel_macro_analyst.py", args)
        self.assertIn("--once", args)


class TestMockMode(unittest.TestCase):
    """--mock: fake data without MT5, actions disabled."""

    def test_mock_state_and_disabled_actions(self):
        client = TestClient(dash.app)
        with mock.patch.object(dash, "MOCK", True), \
             mock.patch.object(dash, "_credentials",
                               return_value=("sentinel", "bon")):
            state = dash.build_state()
            self.assertIn("MOCK", state["time"])
            self.assertEqual(len(state["bots"]), 8)
            self.assertEqual(state["weather"]["weather"], "STORMY")
            r = client.post("/api/panic", auth=("sentinel", "bon"))
        self.assertIn("mock mode", r.text)

    def test_insights_terminal_look_no_chat_bubbles(self):
        # institutional terminal redesign: flat bordered cards, no bubbles
        client = TestClient(dash.app)
        with mock.patch.object(dash, "MOCK", True), \
             mock.patch.object(dash, "_credentials",
                               return_value=("sentinel", "bon")):
            page = client.get("/", auth=("sentinel", "bon")).text
        self.assertNotIn("chat-bubble", page)
        self.assertNotIn("chat chat-start", page)
        self.assertNotIn("tabs-lifted", page)
        self.assertIn("ins-card", page)
        self.assertIn("border-left-color:#3B82F6", page)   # geopolitics
        self.assertIn("border-left-color:#10B981", page)   # macro
        self.assertIn("border-left-color:#F59E0B", page)   # sentiment
        self.assertIn("margin-bottom: 1.5rem", page)       # nav isolation
        self.assertIn("gap: 2rem", page)                   # tab alignment

    def test_insights_responsive_hybrid_mobile_swipe_desktop_grid(self):
        # 2026-07-23: mobile swipe (scroll-snap) <1024px, 3-col grid >=1024px
        client = TestClient(dash.app)
        with mock.patch.object(dash, "MOCK", True), \
             mock.patch.object(dash, "_credentials",
                               return_value=("sentinel", "bon")):
            page = client.get("/", auth=("sentinel", "bon")).text
        # mobile-first: swipeable, snapping, one full-width panel at a time
        self.assertIn("scroll-snap-type: x mandatory", page)
        self.assertIn("scroll-snap-align: start", page)
        self.assertIn("overflow-x: auto", page)
        self.assertIn('class="insights-container"', page)
        self.assertIn('class="ins-panel"', page)
        # desktop (>=1024px): nav hidden, 3-column grid, equal-height panels
        self.assertIn("@media (min-width: 1024px)", page)
        self.assertIn("display: none", page)
        self.assertIn("grid-template-columns: repeat(3, 1fr)", page)
        self.assertIn("gap: 1.5rem", page)
        self.assertIn("height: 100%", page)
        # header sync: clickable tabs + JS keeps .active in sync on scroll
        self.assertIn('class="insights-tabs-nav"', page)
        self.assertIn('class="ins-tab active"', page)
        self.assertIn("data-panel=\"panel-theses\"", page)
        self.assertIn("data-panel=\"panel-banks\"", page)
        self.assertIn("data-panel=\"panel-conflict\"", page)
        self.assertIn("IntersectionObserver", page)
        self.assertIn("classList.toggle(\"active\"", page)
        # legacy radio-hack tabs fully retired
        self.assertNotIn('type="radio"', page)

    def test_global_wrapper_widens_on_desktop(self):
        # 2026-07-23: fix - the page shell stayed mobile-width (max-w-2xl)
        # on desktop, squeezing every section into a narrow center column
        client = TestClient(dash.app)
        with mock.patch.object(dash, "MOCK", True), \
             mock.patch.object(dash, "_credentials",
                               return_value=("sentinel", "bon")):
            page = client.get("/", auth=("sentinel", "bon")).text
        self.assertIn("max-w-2xl", page)               # mobile default kept
        self.assertIn("lg:max-w-[1400px]", page)        # desktop breakout
        self.assertIn("lg:w-[92%]", page)
        self.assertIn("lg:grid-cols-4", page)           # fleet panel spreads


class TestArbitrage(unittest.TestCase):
    """Bot 8 section: KPI cards, filtered/paginated datatable, UX rules."""

    ROWS = [
        # (date_utc, asset, direction, mt5_action, bot7_view, aligned, pnl, winner)
        ("2026-07-17T21:05:00+00:00", "XAUUSD.p", "LONG",
         "Long execution (breakout)", "STORMY (CPI)", 1, 450.0, "ALIGNED."),
        ("2026-07-17T18:40:00+00:00", "SpotBrent", "SHORT",
         "Short execution (statarb)", "STORMY (CPI)", 0, -350.0,
         "Bot 7 (macro) was right. The semantic filter saw it coming."),
        ("2026-07-16T15:12:00+00:00", "EURUSD.p", "SHORT",
         "Short execution (reversion)", "CALM (quiet)", 1, 86.4, "ALIGNED."),
    ]

    def setUp(self):
        self.client = TestClient(dash.app)
        self.auth = ("sentinel", "bon")
        tmp = tempfile.mkdtemp()
        self.db = os.path.join(tmp, "arbitrage.db")
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE arbitrage_logs (id INTEGER PRIMARY KEY"
                    " AUTOINCREMENT, date_utc TIMESTAMP, asset VARCHAR,"
                    " direction VARCHAR, mt5_action VARCHAR, bot7_view"
                    " VARCHAR, is_aligned BOOLEAN, pnl FLOAT,"
                    " winner_arbitrage VARCHAR)")
        con.executemany("INSERT INTO arbitrage_logs (date_utc, asset,"
                        " direction, mt5_action, bot7_view, is_aligned,"
                        " pnl, winner_arbitrage) VALUES (?,?,?,?,?,?,?,?)",
                        self.ROWS)
        con.commit()
        con.close()
        self.summary = os.path.join(tmp, "arbitrage_summary.json")
        with open(self.summary, "w", encoding="utf-8") as fh:
            json.dump({"trades": 3, "win_rate": 61.90, "profit_factor": 1.68,
                       "sharpe": 2.15, "max_drawdown": 740.0,
                       "max_drawdown_pct": -7.40, "total_pnl": 186.4}, fh)
        for p in (mock.patch.object(dash, "ARBITRAGE_DB", self.db),
                  mock.patch.object(dash, "ARBITRAGE_SUMMARY", self.summary),
                  mock.patch.object(dash, "_credentials",
                                    return_value=self.auth)):
            p.start()
            self.addCleanup(p.stop)

    def _get(self, url):
        return self.client.get(url, auth=self.auth)

    def _pad_to(self, n):
        """Filler rows so the table reaches the minimal KPI sample."""
        con = sqlite3.connect(self.db)
        con.executemany(
            "INSERT INTO arbitrage_logs (date_utc, asset, direction,"
            " mt5_action, bot7_view, is_aligned, pnl, winner_arbitrage)"
            " VALUES (?,?,?,?,?,?,?,?)",
            [(f"2026-07-{1 + i:02d}T10:00:00+00:00", "XAUUSD.p", "LONG",
              "Long execution (breakout)", "CALM (quiet)", 1, 10.0,
              "ALIGNED.") for i in range(n - 3)])
        con.commit()
        con.close()

    def test_small_sample_hides_global_kpis(self):
        # N < 10: numeric counters are statistically meaningless - show
        # the neutral acquisition banner instead (PO spec, chantier 4.3).
        html = self._get("/partial/arbitrage").text
        self.assertIn("En cours d'acquisition (N = 3/10)", html)
        self.assertNotIn("61.90%", html)
        self.assertNotIn("1.68", html)

    def test_sample_gate_uses_unfiltered_count(self):
        # The gate reflects the WHOLE table, not the filtered subset:
        # filtering down to 1 row must not hide KPIs once N >= 10.
        self._pad_to(10)
        html = self._get("/partial/arbitrage?asset=EURUSD.p").text
        self.assertIn("61.90%", html)
        self.assertNotIn("En cours d'acquisition", html)

    def test_kpi_cards_and_thresholds(self):
        self._pad_to(10)
        html = self._get("/partial/arbitrage").text
        self.assertIn("61.90%", html)                 # win rate
        self.assertIn('text-success">1.68', html)     # PF >= 1.5 -> green
        # threshold colors: amber between 1.1 and 1.5, red below 1.1
        with open(self.summary, "w", encoding="utf-8") as fh:
            json.dump({"trades": 3, "win_rate": 40.0, "profit_factor": 1.25,
                       "sharpe": None, "max_drawdown": 0.0,
                       "max_drawdown_pct": None, "total_pnl": 0.0}, fh)
        self.assertIn('text-warning">1.25', self._get("/partial/arbitrage").text)
        with open(self.summary, "w", encoding="utf-8") as fh:
            json.dump({"trades": 3, "win_rate": 40.0, "profit_factor": 0.80,
                       "sharpe": None, "max_drawdown": 0.0,
                       "max_drawdown_pct": None, "total_pnl": 0.0}, fh)
        self.assertIn('text-error">0.80', self._get("/partial/arbitrage").text)

    def test_table_ux_rules(self):
        self._pad_to(10)
        html = self._get("/partial/arbitrage").text
        self.assertIn('badge-success badge-xs">YES', html)
        self.assertIn('badge-warning badge-xs">NO', html)
        self.assertIn("+450.00", html)
        self.assertIn("-350.00", html)
        self.assertIn("italic opacity-60", html)      # divergent row greyed
        self.assertIn("-7.40%", html)                 # max DD card
        self.assertIn("2.15", html)                   # sharpe card
        # 2026-07-23: table fills its container instead of being squeezed
        self.assertIn('overflow-x-auto w-full', html)
        self.assertIn('table table-xs w-full', html)

    def test_asset_filter_and_api(self):
        r = self._get("/api/arbitrage?asset=XAUUSD.p").json()
        self.assertEqual(r["total"], 1)
        self.assertEqual(r["rows"][0]["asset"], "XAUUSD.p")
        self.assertEqual(sorted(r["assets"]),
                         ["EURUSD.p", "SpotBrent", "XAUUSD.p"])

    def test_date_filter(self):
        r = self._get("/api/arbitrage?start=2026-07-17&end=2026-07-17").json()
        self.assertEqual(r["total"], 2)
        r2 = self._get("/api/arbitrage?end=2026-07-16").json()
        self.assertEqual(r2["total"], 1)

    def test_pagination(self):
        with mock.patch.object(dash, "ARBITRAGE_PER_PAGE", 2):
            r = self._get("/api/arbitrage?page=2").json()
        self.assertEqual(r["pages"], 2)
        self.assertEqual(len(r["rows"]), 1)
        self.assertEqual(r["rows"][0]["asset"], "EURUSD.p")  # oldest last

    def test_missing_db_never_500(self):
        with mock.patch.object(dash, "ARBITRAGE_DB",
                               os.path.join(tempfile.mkdtemp(), "no.db")):
            r = self._get("/partial/arbitrage")
        self.assertEqual(r.status_code, 200)
        self.assertIn("no arbitrage yet", r.text)


class TestMacroSignalSection(unittest.TestCase):
    """Bot 7 v2 signal card: today's flag, gate badge, history, no key leak."""

    TODAY = datetime.now(timezone.utc).date().isoformat()

    def setUp(self):
        self.client = TestClient(dash.app)
        self.auth = ("sentinel", "bon")
        tmp = tempfile.mkdtemp()
        self.sig = os.path.join(tmp, "macro_signal.json")
        self.cfg = os.path.join(tmp, "macro_config.json")
        self.db = os.path.join(tmp, "arbitrage.db")
        with open(self.sig, "w", encoding="utf-8") as fh:
            json.dump({"asset_affected": "XAUUSD", "macro_bias": "BEARISH",
                       "confidence_score": 85, "rationale": "hawkish Fed",
                       "action_for_mt5": "BLOCK_BUY_SIGNALS",
                       "triage_kept": 3, "triage_total": 42,
                       "date": self.TODAY}, fh)
        with open(self.cfg, "w", encoding="utf-8") as fh:
            json.dump({"anthropic_api_key": "sk-ant-SECRET-KEY-123",
                       "macro_gate_enabled": False}, fh)
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE macro_signals (id INTEGER PRIMARY KEY"
                    " AUTOINCREMENT, date_utc TIMESTAMP, asset_affected"
                    " VARCHAR, macro_bias VARCHAR, confidence_score"
                    " INTEGER, rationale VARCHAR, action_for_mt5 VARCHAR,"
                    " triage_kept INTEGER, triage_total INTEGER)")
        con.execute("INSERT INTO macro_signals (date_utc, asset_affected,"
                    " macro_bias, confidence_score, rationale,"
                    " action_for_mt5, triage_kept, triage_total) VALUES"
                    " ('2026-07-17T08:05:00+00:00', 'XTIUSD', 'BULLISH',"
                    " 72, 'supply risk', 'BLOCK_SELL_SIGNALS', 2, 38)")
        con.commit()
        con.close()
        for p in (mock.patch.object(dash, "MACRO_SIGNAL_FILE", self.sig),
                  mock.patch.object(dash, "MACRO_CONFIG_FILE", self.cfg),
                  mock.patch.object(dash, "ARBITRAGE_DB", self.db),
                  mock.patch.object(dash, "_credentials",
                                    return_value=self.auth)):
            p.start()
            self.addCleanup(p.stop)

    def test_fragment_shows_signal_and_gate_off(self):
        html = self.client.get("/partial/signal", auth=self.auth).text
        self.assertIn("XAUUSD", html)
        self.assertIn("BEARISH", html)
        self.assertIn("BLOCK_BUY_SIGNALS", html)
        self.assertIn("85%", html)
        self.assertIn("GATE OFF", html)
        self.assertIn("BULLISH", html)            # history row
        self.assertIn("2/38", html)               # triage counts

    def test_gate_on_badge(self):
        with open(self.cfg, "w", encoding="utf-8") as fh:
            json.dump({"anthropic_api_key": "sk-ant-SECRET-KEY-123",
                       "macro_gate_enabled": True}, fh)
        self.assertIn("GATE ON",
                      self.client.get("/partial/signal", auth=self.auth).text)

    def test_api_key_never_leaks(self):
        for url in ("/partial/signal", "/api/signal"):
            self.assertNotIn("sk-ant",
                             self.client.get(url, auth=self.auth).text)

    def test_stale_signal_shows_skeleton(self):
        with open(self.sig, "w", encoding="utf-8") as fh:
            json.dump({"asset_affected": "XAUUSD", "date": "2026-07-01",
                       "action_for_mt5": "BLOCK_BUY_SIGNALS"}, fh)
        html = self.client.get("/partial/signal", auth=self.auth).text
        self.assertIn("no signal for today", html)
        self.assertIsNone(self.client.get("/api/signal",
                                          auth=self.auth).json()["today"])

    def test_missing_db_and_files_never_500(self):
        tmp = tempfile.mkdtemp()
        with mock.patch.object(dash, "MACRO_SIGNAL_FILE",
                               os.path.join(tmp, "none.json")), \
             mock.patch.object(dash, "MACRO_CONFIG_FILE",
                               os.path.join(tmp, "none2.json")), \
             mock.patch.object(dash, "ARBITRAGE_DB",
                               os.path.join(tmp, "none.db")):
            r = self.client.get("/partial/signal", auth=self.auth)
        self.assertEqual(r.status_code, 200)


class TestAuth(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(dash.app)
        fake_mt5.account_info.return_value = None
        fake_mt5.positions_get.return_value = None

    def test_no_password_configured_is_503(self):
        with mock.patch.object(dash, "_credentials",
                               return_value=("sentinel", "")):
            r = self.client.get("/api/state", auth=("sentinel", "x"))
        self.assertEqual(r.status_code, 503)

    def test_wrong_password_is_401(self):
        with mock.patch.object(dash, "_credentials",
                               return_value=("sentinel", "bon")):
            self.assertEqual(self.client.get("/api/state").status_code, 401)
            r = self.client.get("/api/state", auth=("sentinel", "mauvais"))
        self.assertEqual(r.status_code, 401)

    def test_live_fragment_side_colors_distinct_from_pnl(self):
        # LONG/SHORT in blue/amber: green/red stay reserved for PnL.
        fake_mt5.positions_get.return_value = [
            SimpleNamespace(ticket=1, symbol="XAUUSD", type=0, volume=0.1,
                            profit=12.34, magic=1001),
            SimpleNamespace(ticket=2, symbol="XAUUSD", type=1, volume=0.2,
                            profit=-5.0, magic=1001),
        ]
        with mock.patch.object(dash, "_credentials",
                               return_value=("sentinel", "bon")), \
             mock.patch.object(dash, "BOTS_DIR", tempfile.mkdtemp()), \
             mock.patch.object(dash, "LOG_DIR", tempfile.mkdtemp()):
            r = self.client.get("/partial/live", auth=("sentinel", "bon"))
        self.assertEqual(r.status_code, 200)
        self.assertIn('badge-info">LONG', r.text)
        self.assertIn('badge-warning">SHORT', r.text)
        self.assertNotIn('badge-success">LONG', r.text)
        self.assertNotIn('badge-error">SHORT', r.text)

    def test_good_password_serves_state_and_page(self):
        with mock.patch.object(dash, "_credentials",
                               return_value=("sentinel", "bon")), \
             mock.patch.object(dash, "BOTS_DIR", tempfile.mkdtemp()), \
             mock.patch.object(dash, "LOG_DIR", tempfile.mkdtemp()):
            r = self.client.get("/api/state", auth=("sentinel", "bon"))
            page = self.client.get("/", auth=("sentinel", "bon"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["bots"]), 8)
        self.assertEqual(page.status_code, 200)
        self.assertIn("Sentinel", page.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
