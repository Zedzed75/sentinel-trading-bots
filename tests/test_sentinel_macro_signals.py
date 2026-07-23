"""Bot 7 v2 signal-layer tests (pure helpers, SQLite in tempdir).

Run:  python -m unittest test_sentinel_macro_signals -v
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots"))
import sentinel_macro_signals as msig  # noqa: E402
import sentinel_macro_sources as ms  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 7, 18, 8, 5, tzinfo=UTC)
SIGNAL = {"asset_affected": "XAUUSD", "macro_bias": "BEARISH",
          "confidence_score": 85,
          "rationale": "Powell's hawkish tone strengthens the DXY.",
          "action_for_mt5": "BLOCK_BUY_SIGNALS"}


class TestExtendedEntities(unittest.TestCase):
    """Layer 1: the critical macro entities pass the zero-token filter."""

    def test_new_entities_are_kept(self):
        titles = ["ECB surprises with an emergency meeting",
                  "NFP payrolls smash expectations",
                  "US CPI hotter than forecast",
                  "PPI cools for a third month",
                  "FOMC minutes reveal a split committee",
                  "Celebrity gossip of the day"]
        kept = ms.filter_social(titles)
        self.assertEqual(len(kept), 5)
        self.assertNotIn("Celebrity gossip of the day", kept)


class TestTriageHelpers(unittest.TestCase):
    def test_flatten_excludes_calendar_and_caps(self):
        sources = {"geo": ["g1"], "social": ["s1"], "banks": ["b1"],
                   "calendar": ["- 12:30 UTC [USD] CPI"]}
        self.assertEqual(msig.flatten_items(sources), ["g1", "s1", "b1"])
        many = {"geo": [f"t{i}" for i in range(100)]}
        self.assertEqual(len(msig.flatten_items(many)),
                         msig.MAX_TRIAGE_ITEMS)

    def test_numbered_format(self):
        self.assertEqual(msig.numbered(["a", "b"]), "0. a\n1. b")

    def test_qualified_threshold_and_bad_indices(self):
        items = ["a", "b", "c"]
        verdict = {"scores": [{"index": 0, "score": 9},
                              {"index": 1, "score": 6},
                              {"index": 2, "score": 7},
                              {"index": 99, "score": 10}]}   # out of range
        self.assertEqual(msig.qualified(items, verdict), ["a", "c"])
        self.assertEqual(msig.qualified(items, {}), [])

    def test_clamp_confidence(self):
        self.assertEqual(msig.clamp_signal(dict(SIGNAL,
                         confidence_score=150))["confidence_score"], 100)
        self.assertEqual(msig.clamp_signal(dict(SIGNAL,
                         confidence_score=-5))["confidence_score"], 0)


class TestOutputs(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.file = os.path.join(tmp, "macro_signal.json")
        self.db = os.path.join(tmp, "arbitrage.db")

    def test_signal_file_atomic_with_metadata(self):
        msig.write_signal(SIGNAL, NOW, kept=2, total=40, path=self.file)
        with open(self.file, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["action_for_mt5"], "BLOCK_BUY_SIGNALS")
        self.assertEqual(data["date"], "2026-07-18")
        self.assertEqual(data["triage_kept"], 2)
        self.assertEqual(data["triage_total"], 40)
        self.assertFalse(os.path.exists(self.file + ".tmp"))

    def test_db_upsert_same_day_no_duplicates(self):
        msig.insert_signal_db(SIGNAL, NOW, 2, 40, self.db)
        msig.insert_signal_db(dict(SIGNAL, confidence_score=60), NOW,
                              1, 30, self.db)
        con = sqlite3.connect(self.db)
        rows = con.execute("SELECT confidence_score, triage_kept"
                           " FROM macro_signals").fetchall()
        con.close()
        self.assertEqual(rows, [(60, 1)])

    def test_db_write_never_raises(self):
        # invalid path (a directory): logged, not raised
        msig.insert_signal_db(SIGNAL, NOW, 0, 0, tempfile.mkdtemp())

    def test_schema_is_strict(self):
        self.assertFalse(msig.SIGNAL_SCHEMA["additionalProperties"])
        self.assertEqual(set(msig.SIGNAL_SCHEMA["required"]),
                         set(msig.SIGNAL_SCHEMA["properties"]))
        self.assertIn("BLOCK_BUY_SIGNALS",
                      msig.SIGNAL_SCHEMA["properties"]
                      ["action_for_mt5"]["enum"])
        self.assertFalse(msig.TRIAGE_SCHEMA["additionalProperties"])

    def test_schema_accepts_usdcnh(self):
        # 2026-07-23: USDCNH diversification (PBoC/Fed policy divergence)
        self.assertIn("USDCNH", msig.SIGNAL_ASSETS)
        self.assertIn("USDCNH",
                      msig.SIGNAL_SCHEMA["properties"]
                      ["asset_affected"]["enum"])
        self.assertEqual(
            msig.clamp_signal(dict(SIGNAL, asset_affected="USDCNH")
                              )["asset_affected"], "USDCNH")


if __name__ == "__main__":
    unittest.main(verbosity=2)
