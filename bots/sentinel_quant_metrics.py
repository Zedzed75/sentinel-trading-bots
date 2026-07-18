"""SENTINEL - Pure quantitative metrics (bot 8, sentinel_arbitrage.py).

Basic-math implementations only (no heavy library): the module is
imported by the arbitrage worker and its outputs are published to the
dashboard through arbitrage_summary.json. Directly testable
(tests/test_sentinel_quant_metrics.py).
"""

import math
from collections import OrderedDict
from datetime import date

TRADING_DAYS_PER_YEAR = 252


def win_rate(pnls: list[float]) -> float | None:
    """(winning trades / total trades) * 100; None without trades."""
    if not pnls:
        return None
    return round(sum(1 for p in pnls if p > 0) / len(pnls) * 100, 2)


def profit_factor(pnls: list[float]) -> float | None:
    """Gross gains / gross losses; None when there is no loss (division
    by zero: the ratio is undefined, not infinite in reports)."""
    gains = sum(p for p in pnls if p > 0)
    losses = -sum(p for p in pnls if p < 0)
    if losses <= 0:
        return None
    return round(gains / losses, 2)


def daily_returns(rows: list[tuple[date, float]]) -> list[float]:
    """Aggregate per-trade PnL into one net return per calendar day
    (chronological). Input: (close date, pnl) tuples in any order."""
    per_day: OrderedDict[date, float] = OrderedDict()
    for d, pnl in sorted(rows, key=lambda r: r[0]):
        per_day[d] = per_day.get(d, 0.0) + float(pnl)
    return list(per_day.values())


def sharpe_annualized(returns: list[float]) -> float | None:
    """(mean return / sample std dev) * sqrt(252); None if fewer than
    two samples or zero variance (ratio undefined)."""
    n = len(returns)
    if n < 2:
        return None
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    if var <= 0:
        return None
    return round(mean / math.sqrt(var) * math.sqrt(TRADING_DAYS_PER_YEAR), 2)


def max_drawdown(pnls: list[float]) -> float:
    """Largest peak-to-trough loss on the cumulative PnL curve, in
    currency (positive number; 0.0 without trades or losses)."""
    cum = peak = dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return round(dd, 2)


def max_drawdown_pct(pnls: list[float],
                     capital_base: float | None) -> float | None:
    """Max drawdown as a negative percentage of the equity peak
    (capital_base + cumulative PnL at the peak); None without a base."""
    if not capital_base or capital_base <= 0:
        return None
    cum, peak, worst = 0.0, 0.0, 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        equity_peak = capital_base + peak
        if equity_peak > 0:
            worst = max(worst, (peak - cum) / equity_peak)
    return round(-worst * 100, 2)


def compute_all(rows: list[tuple[date, float]],
                capital_base: float | None = None) -> dict:
    """The four KPIs of the daily arbitrage summary."""
    pnls = [pnl for _, pnl in sorted(rows, key=lambda r: r[0])]
    return {
        "trades": len(pnls),
        "win_rate": win_rate(pnls),
        "profit_factor": profit_factor(pnls),
        "sharpe": sharpe_annualized(daily_returns(rows)),
        "max_drawdown": max_drawdown(pnls),
        "max_drawdown_pct": max_drawdown_pct(pnls, capital_base),
        "total_pnl": round(sum(pnls), 2),
    }
