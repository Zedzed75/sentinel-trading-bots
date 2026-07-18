"""SENTINEL ARBITRAGE tests (bot 8) - SQLite in tempdir, no MT5/network.

Run:  python -m unittest test_sentinel_arbitrage -v
Covered: idempotent migration, alignment/winner rules, 22:00 UTC window,
daily upsert without duplicates, summary KPIs, Excel-friendly export.
"""

import csv
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots"))
import sentinel_arbitrage as ar  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 7, 18, 22, 0, tzinfo=UTC)
WEATHER = {"weather": "STORMY", "focus": "US CPI at 14:30 UTC",
           "date": "2026-07-18"}


def _trade(pnl, strategy="breakout", symbol="XAUUSD.p", direction="long",
           close=NOW):
    return {"pnl": pnl, "strategy": strategy, "symbol": symbol,
            "direction": direction.upper(), "close_time": close}


def temp_db():
    path = os.path.join(tempfile.mkdtemp(), "arbitrage.db")
    return ar.init_db(path), path


class TestMigration(unittest.TestCase):
    def test_init_db_creates_table_and_is_idempotent(self):
        con, path = temp_db()
        con.close()
        con = ar.init_db(path)                    # second run: no error
        cols = [c[1] for c in con.execute(
            "PRAGMA table_info(arbitrage_logs)").fetchall()]
        self.assertEqual(cols, ["id", "date_utc", "asset", "direction",
                                "mt5_action", "bot7_view", "is_aligned",
                                "pnl", "winner_arbitrage"])
        con.close()


class TestArbitrationRules(unittest.TestCase):
    def test_aligned_strategy_wins_nothing_to_arbitrate(self):
        row = ar.arbitrate(_trade(-120.0, strategy="trend"), WEATHER)
        self.assertTrue(row["is_aligned"])        # STORMY favours trend
        self.assertEqual(row["winner_arbitrage"], ar.WINNER_ALIGNED)

    def test_divergent_profit_goes_to_mt5(self):
        row = ar.arbitrate(_trade(450.0, strategy="statarb"), WEATHER)
        self.assertFalse(row["is_aligned"])       # STORMY vs statarb
        self.assertEqual(row["winner_arbitrage"], ar.WINNER_MT5)

    def test_divergent_loss_goes_to_bot7(self):
        row = ar.arbitrate(_trade(-350.0, strategy="reversion"), WEATHER)
        self.assertFalse(row["is_aligned"])
        self.assertEqual(row["winner_arbitrage"], ar.WINNER_BOT7)

    def test_divergent_flat_pnl_has_no_winner(self):
        row = ar.arbitrate(_trade(0.0, strategy="statarb"), WEATHER)
        self.assertEqual(row["winner_arbitrage"], ar.WINNER_FLAT)

    def test_neutral_weather_aligns_everyone(self):
        neutral = dict(WEATHER, weather="NEUTRAL")
        for strat in ("breakout", "trend", "reversion", "statarb"):
            self.assertTrue(ar.arbitrate(_trade(-1.0, strategy=strat),
                                         neutral)["is_aligned"])

    def test_calm_weather_favours_mean_reversion(self):
        calm = dict(WEATHER, weather="CALM")
        self.assertTrue(ar.arbitrate(_trade(1.0, strategy="statarb"),
                                     calm)["is_aligned"])
        self.assertFalse(ar.arbitrate(_trade(1.0, strategy="breakout"),
                                      calm)["is_aligned"])

    def test_missing_weather_keeps_row_with_null_alignment(self):
        row = ar.arbitrate(_trade(10.0), None)
        self.assertIsNone(row["is_aligned"])
        self.assertEqual(row["bot7_view"], "unavailable")
        self.assertEqual(row["winner_arbitrage"], ar.WINNER_NO_VIEW)

    def test_row_fields(self):
        row = ar.arbitrate(_trade(42.5, direction="short",
                                  symbol="XTIUSD.p"), WEATHER)
        self.assertEqual(row["asset"], "XTIUSD.p")
        self.assertEqual(row["direction"], "SHORT")
        self.assertEqual(row["mt5_action"], "Short execution (breakout)")
        self.assertIn("STORMY (US CPI", row["bot7_view"])
        self.assertEqual(row["pnl"], 42.5)

    def test_day_weather_rejects_stale_snapshot(self):
        self.assertIsNotNone(ar.day_weather(WEATHER, NOW.date()))
        self.assertIsNone(ar.day_weather(dict(WEATHER, date="2026-07-17"),
                                         NOW.date()))
        self.assertIsNone(ar.day_weather({}, NOW.date()))


class TestSchedule(unittest.TestCase):
    def test_runs_once_per_day_from_22h(self):
        self.assertFalse(ar.should_run({}, NOW.replace(hour=21, minute=59)))
        self.assertTrue(ar.should_run({}, NOW))
        self.assertTrue(ar.should_run({}, NOW.replace(hour=23)))
        done = {"last_run_day": "2026-07-18"}
        self.assertFalse(ar.should_run(done, NOW))
        self.assertTrue(ar.should_run({"last_run_day": "2026-07-17"}, NOW))


class TestDailyBatch(unittest.TestCase):
    def setUp(self):
        self.con, self.db = temp_db()
        self.tmp = os.path.dirname(self.db)
        self.state_file = os.path.join(self.tmp, "arbitrage_state.json")
        self.summary = os.path.join(self.tmp, "arbitrage_summary.json")
        self.export = os.path.join(self.tmp, "arbitrage_export.csv")
        self.addCleanup(self.con.close)
        self.patches = [
            mock.patch.object(ar, "STATE_FILE", self.state_file),
            mock.patch.object(ar, "SUMMARY_FILE", self.summary),
            mock.patch.object(ar, "EXPORT_CSV", self.export),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def _run(self, trades, weather=WEATHER, now=NOW, state=None):
        with mock.patch.object(ar, "read_trades", return_value=trades), \
             mock.patch.object(ar, "load_json",
                               side_effect=lambda p:
                               weather if p == ar.WEATHER_FILE else {}):
            ar.run_arbitration(self.con, state if state is not None else {},
                               now)

    def _rows(self):
        return self.con.execute(
            "SELECT asset, is_aligned, pnl, winner_arbitrage"
            " FROM arbitrage_logs ORDER BY id").fetchall()

    def test_one_row_per_trade_and_state_saved(self):
        state = {}
        self._run([_trade(450.0, strategy="statarb"),
                   _trade(-80.0, strategy="trend")], state=state)
        rows = self._rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][3], ar.WINNER_MT5)
        self.assertEqual(rows[1][3], ar.WINNER_ALIGNED)
        self.assertEqual(state["last_run_day"], "2026-07-18")
        with open(self.state_file, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["last_run_day"], "2026-07-18")

    def test_rerun_same_day_replaces_without_duplicates(self):
        self._run([_trade(10.0)])
        self._run([_trade(20.0), _trade(30.0)])
        rows = self._rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual([r[2] for r in rows], [20.0, 30.0])

    def test_no_trade_day_writes_no_signal_row(self):
        self._run([])
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "-")
        self.assertEqual(rows[0][2], 0.0)

    def test_yesterdays_trades_excluded(self):
        old = _trade(99.0, close=NOW.replace(day=17))
        self._run([old, _trade(5.0)])
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], 5.0)

    def test_summary_kpis_written(self):
        self._run([_trade(100.0), _trade(-50.0, strategy="statarb")])
        with open(self.summary, encoding="utf-8") as fh:
            m = json.load(fh)
        self.assertEqual(m["trades"], 2)
        self.assertEqual(m["win_rate"], 50.0)
        self.assertEqual(m["profit_factor"], 2.0)
        self.assertEqual(m["total_pnl"], 50.0)

    def test_summary_ignores_no_signal_rows(self):
        self._run([])
        with open(self.summary, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["trades"], 0)

    def test_export_csv_excel_friendly(self):
        self._run([_trade(450.0, strategy="statarb"), _trade(-80.0)])
        with open(self.export, "rb") as fh:
            raw = fh.read()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))   # UTF-8 BOM
        with open(self.export, encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.reader(fh))
        self.assertEqual(tuple(rows[0]), ar.EXPORT_HEADERS)
        self.assertEqual(rows[1][6], "+450.00")
        self.assertEqual(rows[1][5], "NO")
        self.assertEqual(rows[2][5], "YES")                # STORMY+breakout

    def test_run_cycle_respects_window(self):
        with mock.patch.object(ar, "run_arbitration") as run:
            ar.run_cycle(self.con, {}, now=NOW.replace(hour=12))
            run.assert_not_called()
            ar.run_cycle(self.con, {}, now=NOW)
            run.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
