"""Quant metrics tests (bot 8) - pure math, no I/O.

Run:  python -m unittest test_sentinel_quant_metrics -v
"""

import math
import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots"))
import sentinel_quant_metrics as qm  # noqa: E402

D = date(2026, 7, 13)


def _rows(pnls, one_per_day=True):
    if one_per_day:
        return [(date(2026, 7, 1 + i), p) for i, p in enumerate(pnls)]
    return [(D, p) for p in pnls]


class TestWinRate(unittest.TestCase):
    def test_known_value(self):
        # 13 wins out of 21 trades -> 61.90%
        pnls = [10.0] * 13 + [-5.0] * 8
        self.assertEqual(qm.win_rate(pnls), 61.90)

    def test_zero_pnl_is_not_a_win(self):
        self.assertEqual(qm.win_rate([0.0, 10.0]), 50.0)

    def test_empty(self):
        self.assertIsNone(qm.win_rate([]))


class TestProfitFactor(unittest.TestCase):
    def test_known_value(self):
        # gains 420 / losses 250 -> 1.68
        self.assertEqual(qm.profit_factor([300.0, 120.0, -250.0]), 1.68)

    def test_division_by_zero_gives_none(self):
        self.assertIsNone(qm.profit_factor([100.0, 50.0]))  # no loss
        self.assertIsNone(qm.profit_factor([]))


class TestSharpe(unittest.TestCase):
    def test_known_value(self):
        # returns 1,2,3: mean=2, sample std=1 -> 2*sqrt(252) = 31.75
        self.assertEqual(qm.sharpe_annualized([1.0, 2.0, 3.0]),
                         round(2 * math.sqrt(252), 2))

    def test_undefined_cases(self):
        self.assertIsNone(qm.sharpe_annualized([]))
        self.assertIsNone(qm.sharpe_annualized([5.0]))       # single sample
        self.assertIsNone(qm.sharpe_annualized([2.0, 2.0]))  # zero variance

    def test_daily_aggregation_feeds_sharpe(self):
        # two trades on the same day count as ONE daily return
        rows = [(D, 10.0), (D, -4.0), (date(2026, 7, 14), 2.0)]
        self.assertEqual(qm.daily_returns(rows), [6.0, 2.0])


class TestMaxDrawdown(unittest.TestCase):
    def test_peak_to_trough(self):
        # curve: 100, 300, 150, 250 -> peak 300, trough 150 -> DD 150
        self.assertEqual(qm.max_drawdown([100.0, 200.0, -150.0, 100.0]),
                         150.0)

    def test_no_loss_or_empty(self):
        self.assertEqual(qm.max_drawdown([50.0, 50.0]), 0.0)
        self.assertEqual(qm.max_drawdown([]), 0.0)

    def test_pct_uses_equity_peak(self):
        # base 10000, peak +300 (=10300), trough +150 -> -150/10300 = -1.46%
        pnls = [100.0, 200.0, -150.0]
        self.assertEqual(qm.max_drawdown_pct(pnls, 10000.0), -1.46)
        self.assertIsNone(qm.max_drawdown_pct(pnls, None))
        self.assertIsNone(qm.max_drawdown_pct(pnls, 0.0))


class TestComputeAll(unittest.TestCase):
    def test_bundle(self):
        rows = _rows([100.0, -50.0, 200.0, -50.0])
        m = qm.compute_all(rows, capital_base=10000.0)
        self.assertEqual(m["trades"], 4)
        self.assertEqual(m["win_rate"], 50.0)
        self.assertEqual(m["profit_factor"], 3.0)
        self.assertEqual(m["total_pnl"], 200.0)
        self.assertEqual(m["max_drawdown"], 50.0)
        self.assertIsNotNone(m["sharpe"])
        self.assertLess(m["max_drawdown_pct"], 0)

    def test_empty_bundle_never_raises(self):
        m = qm.compute_all([])
        self.assertEqual(m["trades"], 0)
        self.assertIsNone(m["win_rate"])
        self.assertIsNone(m["profit_factor"])
        self.assertIsNone(m["sharpe"])
        self.assertEqual(m["max_drawdown"], 0.0)
        self.assertIsNone(m["max_drawdown_pct"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
