"""Tests SENTINEL DASHBOARD (MT5 et psutil mockes, fichiers en tempdir).

Executer :  python -m unittest test_sentinel_dashboard -v
Garantie centrale : un JSON absent, vide ou corrompu ne fait jamais
planter l'interface (build_state repond toujours).
"""

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
        os.unlink(path)                    # fichier absent
    else:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
    return path


class TestRobustReads(unittest.TestCase):
    """Fichiers absents / vides / corrompus : jamais d'exception."""

    def test_load_json_missing_empty_corrupt(self):
        self.assertEqual(dash.load_json(tmpfile(None)), {})
        self.assertEqual(dash.load_json(tmpfile("")), {})
        self.assertEqual(dash.load_json(tmpfile("{pas du json")), {})
        self.assertEqual(dash.load_json(tmpfile("[1, 2]")), {})  # pas un objet
        self.assertEqual(dash.load_json(tmpfile('{"a": 1}')), {"a": 1})

    def test_read_trades_missing_and_corrupt(self):
        self.assertEqual(dash.read_trades(tmpfile(None, ".csv")), [])
        self.assertEqual(dash.read_trades(tmpfile("", ".csv")), [])
        # entete ok mais lignes pourries : ignorees sans planter
        path = tmpfile("close_time,strategy,pnl\n"
                       "pas-une-date,breakout,12.5\n"
                       "2026-07-15T10:00:00+00:00,breakout,pas-un-nombre\n"
                       "2026-07-15T10:00:00+00:00,breakout,42.0\n", ".csv")
        rows = dash.read_trades(path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["pnl"], 42.0)

    def test_build_state_survives_everything_missing(self):
        fake_mt5.account_info.return_value = None
        fake_mt5.positions_get.return_value = None
        with mock.patch.object(dash, "BOTS_DIR", tempfile.mkdtemp()), \
             mock.patch.object(dash, "LOG_DIR", tempfile.mkdtemp()), \
             mock.patch.object(dash, "TRADES_CSV", "introuvable.csv"):
            state = dash.build_state(NOW)
        self.assertFalse(state["compte"]["ok"])
        self.assertEqual(len(state["bots"]), 7)
        self.assertTrue(all(b["statut"] == "arrete" for b in state["bots"]))
        self.assertIsNone(state["jauge_jour"]["pct"])
        self.assertFalse(state["verrou_global"])
        self.assertEqual(state["positions"], [])


class TestStatusLogic(unittest.TestCase):
    def test_bot_status_priorities(self):
        self.assertEqual(dash.bot_status(10, 300, locked=True), "suspendu")
        self.assertEqual(dash.bot_status(10, 300, locked=False), "actif")
        self.assertEqual(dash.bot_status(301, 300, locked=False), "fige")
        self.assertEqual(dash.bot_status(None, 300, locked=False), "arrete")

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
        g = dash.daily_gauge(9800.0, 10000.0)      # -2% : moitie du seuil
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
             mock.patch.object(dash, "TRADES_CSV", "introuvable.csv"):
            self.assertTrue(dash.build_state(NOW)["marge_alerte"])

    def test_positions_filtered_by_magic(self):
        fake_mt5.positions_get.return_value = [
            SimpleNamespace(ticket=1, symbol="XAUUSD", type=0, volume=0.1,
                            profit=12.34, magic=1001),
            SimpleNamespace(ticket=2, symbol="EURUSD", type=1, volume=1.0,
                            profit=-5.0, magic=777),        # etranger
        ]
        pos = dash.open_positions()
        self.assertEqual(len(pos), 1)
        self.assertEqual(pos[0]["sens"], "LONG")
        self.assertEqual(pos[0]["strategie"], "breakout")


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

    def test_good_password_serves_state_and_page(self):
        with mock.patch.object(dash, "_credentials",
                               return_value=("sentinel", "bon")), \
             mock.patch.object(dash, "BOTS_DIR", tempfile.mkdtemp()), \
             mock.patch.object(dash, "LOG_DIR", tempfile.mkdtemp()):
            r = self.client.get("/api/state", auth=("sentinel", "bon"))
            page = self.client.get("/", auth=("sentinel", "bon"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["bots"]), 7)
        self.assertEqual(page.status_code, 200)
        self.assertIn("Sentinel", page.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
