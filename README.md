# Sentinel Trading Bots

Fleet of independent MetaTrader 5 algorithmic trading bots (Pepperstone),
built with TDD. Demo account recommended.

## Structure

```
bots/     the 8 bots (no cross-imports) + pure modules
          (sentinel_signals.py bot 1, sentinel_macro_sources.py bot 7,
          sentinel_quant_metrics.py bot 8)
tests/    the 12 test suites (254 tests, MT5 mocked)
docs/     ARCHITECTURE.md (the code), STRATEGIE.md (the investing),
          AMELIORATION_CONTINUE.md (strategy measurement and correction)
research/ backtest_sentinel.py (replays the bots' rules on broker
          history, anti-overfitting grids)
sentinel_dashboard.py + templates/  mobile web dashboard (read-only)
```

## Bots

| File | Strategy | Risk | Circuit breaker |
|---|---|---|---|
| `sentinel_bot.py` | M30 breakout (Asian range) on XAUUSD only (suspended on EURUSD/GBPUSD on 2026-07-15, see AMELIORATION_CONTINUE.md) + M5 Mean Reversion (Bollinger/RSI) on all three assets; asymmetric VIX filter (gold only) | 1.5% of balance/trade, SL=1.5xATR(14) M30, TP 1:2, 50% partial + break-even at 1R | -4% equity/day (00:00 UTC reference), locked until the next day |
| `sentinel_alpha_compound.py` | Stat-arb: Brent/WTI cointegration (ADF test), entry at \|z\|>=2, exit convergence/48xM15 time stop/4-sigma stop | Dynamic Half-Kelly on equity (5% cap, 1% floor before 10 trades) | -15% of the historical equity peak, permanent lock |
| `sentinel_trend.py` | Trend following (time-series momentum): Donchian 55 H4 breakout, opposite channel 20 exit, on XAUUSD, EURUSD, GBPUSD, US500, XTIUSD | 1% of equity/trade (0.5% on EURUSD/GBPUSD/XTIUSD since 2026-07-15, see AMELIORATION_CONTINUE.md), hard SL 2xATR(14), no TP | -15% of the historical equity peak, permanent lock |
| `sentinel_risk_orchestrator.py` | Does not trade: 10% annualized vol targeting (writes `risk_scale.json`, applied by all bots), directional concentration alert | reduces sizes when account vol rises (0.25 floor) | -10% GLOBAL from the equity peak: closes the whole fleet (Sentinel magics only), permanent lock |
| `sentinel_trade_analytics.py` | Does not trade: rebuilds closed trades from the MT5 history (Sentinel magics) and publishes `logs/trades.csv` + `logs/analytics.html` (win rate, profit factor, expectancy, max DD per strategy/symbol over 7d/30d/total, plus the breakdown by UTC open hour) | none (read-only) | none |
| `sentinel_macro_analyst.py` | Does not trade, never touches MT5: daily multi-agent "market weather" — ingests at 08:00 UTC three source families (geopolitics/energy with chokepoint watch (Hormuz/Red Sea), influencer statements filtered by assets, economic calendar), plus sell-side bank-desk notes (GS, JPM, MS, Citi via FT/FXStreet/Google News), convenes a council of 4 specialized LLM agents (Geo/Macro on claude-fable-5 low effort with server-side opus fallback, Sentiment/Flow on claude-haiku-4-5, mapping overridable via macro_config.json) decided by a claude-opus-4-8 judge (structured JSON); token economy: per-agent sectorized dossiers, word limits, max_tokens 4000, writes `bots/macro_weather.json` + the daily archive `bots/macro_history.json` (upsert by date, for the weather x PnL statistical validation) and sends the Telegram report at 08:30 UTC. **v2 agentic signal pipeline** (Cost Control): local zero-token entity filter (Fed/ECB/NFP/CPI/PPI/FOMC/OPEC...) -> ONE Haiku 4.5 batch triage scoring each item 1-10 (score < 7 stops there) -> Opus 4.8 analyst forced to a strict JSON schema via structured output (`asset_affected`, `macro_bias`, `confidence_score`, `rationale`, `action_for_mt5`), written to `bots/macro_signal.json` + the `macro_signals` table (bots/arbitrage.db). Bots 1 & 3 read the flag before each NEW entry, but enforcement is behind `macro_gate_enabled` in macro_config.json (**default false** — informational until the macro filter is backtested, roadmap 4; bot 8's arbitrage table is the validation dataset). Automatic NEUTRAL/NO_SIGNAL fallback on any failure | none (read-only) | none |
| `sentinel_arbitrage.py` | Does not trade: daily (22:00 UTC) technical-vs-semantic arbitration — compares the day's closed trades (bot 5's journal) against bot 7's 08:30 weather snapshot, writes one row per trade into the `arbitrage_logs` SQLite table (`bots/arbitrage.db`, clean dataset for future ML), decides who was right on divergences, publishes `bots/arbitrage_summary.json` (win rate, profit factor, annualized Sharpe, max drawdown — shown as KPI cards on the dashboard) and the Excel-friendly `logs/arbitrage_export.csv`. Alignment: STORMY favours breakout/trend, CALM favours reversion/statarb, NEUTRAL favours everyone. `--once` runs the arbitration immediately | none (read-only) | none |
| `sentinel_telegram.py` | Does not trade: Telegram notifications (opens, closes with PnL, circuit breakers, 18:00 UTC daily report with a reminder of suspended/reduced pairs and their review deadline) and commands `/status` (equity, positions, locks with deadline, entry windows open/closed per strategy, processes) and `/pnl` (profit/loss day/7d/30d/total per strategy) | none (read-only) | none |

## Usage

```
pip install -r requirements.txt
python bots/sentinel_risk_orchestrator.py   # bot 4 first (writes risk_scale.json)
python bots/sentinel_bot.py                 # bot 1 (intraday multi-asset)
python bots/sentinel_alpha_compound.py      # bot 2 (Brent/WTI spread)
python bots/sentinel_trend.py               # bot 3 (H4 trend following)
python bots/sentinel_trade_analytics.py     # bot 5 (trade analysis)
python bots/sentinel_telegram.py            # bot 6 (mobile notifications)
python bots/sentinel_macro_analyst.py       # bot 7 (daily macro weather)
python bots/sentinel_arbitrage.py           # bot 8 (daily arbitration 22:00 UTC)
```

### Macro weather (bot 7)

Copy `bots/macro_config.example.json` to `bots/macro_config.json`
(gitignored) and put an Anthropic API key in it (or define
`ANTHROPIC_API_KEY`). Manual test: `python bots/sentinel_macro_analyst.py
--once` (immediate pipeline + send). Without a key, the bot waits
passively.

### Mobile dashboard (sentinel_dashboard.py)

Mobile-first web page (FastAPI + Jinja2 + DaisyUI/HTMX via CDN, forest
theme, live fragment refreshed every 10 s): bot 7's dynamic weather
header (red STORMY / green CALM / grey NEUTRAL + confidence + focus),
Debate/Bank Targets/Conflict tabs, balance/equity/margin (alert < 150%),
RUNNING/STOPPED status and day PnL of the 8 bots, -4% circuit-breaker
gauge, open positions, CPU/RAM/watchdog. On top: bot 8's quant KPI cards
(win rate, profit factor with color thresholds, Sharpe, max drawdown)
and the filterable/paginated technical-vs-semantic arbitrage table.
Below the live block: bot 7 v2's macro-signal card (asset, bias, action,
confidence, rationale, GATE ON/OFF badge) with the recent signal history
from the `macro_signals` table. Two confirmation-protected
actions: 🚨 PANIC (closes all Sentinel positions and engages the GLOBAL
lock — human unlock) and 🔄 FORCE RUN bot 7 (immediate weather). Missing/
corrupt files => grey skeletons, never a 500.
Local test without MT5: `python sentinel_dashboard.py --mock`.

1. Copy `dashboard_config.example.json` to `dashboard_config.json`
   (gitignored) and set a strong password: the server refuses to start
   without one, and every access requires this Basic Auth.
2. `python sentinel_dashboard.py [port]` (default 8787), then open
   `http://<ip>:<port>/` on the phone.
3. Outside the local network, serve over HTTPS (`uvicorn
   sentinel_dashboard:app --ssl-keyfile ... --ssl-certfile ...`) or use
   a tunnel/VPN; never expose the Basic Auth over HTTP on the Internet.

### Telegram (bot 6)

1. On Telegram, talk to `@BotFather`: `/newbot`, pick a name, copy the
   token.
2. Copy `bots/telegram_config.example.json` to
   `bots/telegram_config.json` and paste the token (gitignored file,
   never committed).
3. Send `/start` to the created bot: the chat is registered
   automatically, notifications and commands (`/status`, `/pnl`) are
   active.

The watchdog also sends a Telegram alert on every bot restart.

Prerequisites: Pepperstone MT5 terminal installed (path in `main()`),
"Algo Trading" enabled. Only one instance of each bot at a time.

The `*_state.json` files (balance references, Kelly history, locks) are
created on the first cycle and are not versioned; all state writes are
atomic (temp file + rename).

Each bot writes `logs/<bot>.hb` after each successful cycle: the
watchdog restarts a process that is alive but frozen (heartbeat too
old), with a Telegram alert.

Trading windows (real UTC, server candles converted automatically):
breakout 08:00-16:00 and reversion 13:00-18:00 (bot 1), new entries
07:00-20:00 (bot 2), no opening 21:00-23:00 (bot 3, rollover). Exits
and circuit breakers are never blocked by a window.
`FORCE_TRADING_HOURS` (sentinel_bot.py): `True` = bypass bot 1's
windows for live testing; keep `False` in production.

## Tests

254 tests, MT5, yfinance, psutil and LLM mocked (runnable without a
terminal):

```
python -m unittest discover -s tests -v
```

The CI (GitHub Actions, `windows-latest`) runs them on every push.

## Contribution rules (branch protection)

The `master` branch is protected:

1. **No direct push to `master`**: every change goes through a branch
   then a pull request.
2. **One validation (approved review) is required** to merge a PR; a new
   series of commits invalidates previous approvals.
3. **The CI must be green** (the `test` job, the full suite) before the
   merge, and the branch must be up to date with `master`.
4. **Force-push and deletion of `master` are forbidden.**

These rules apply to **everyone, administrators included**
(enforce_admins active). A PR's author cannot approve their own PR.

**Backup validation (no other dev available)**: an administrator can
apply the `validation-solo` label to the PR; the `validation-solo.yml`
workflow then has the `github-actions` bot approve the PR. Applying the
label is the act of validation (traced in the PR); the CI remains
mandatory. As an absolute last resort, the administrator can temporarily
suspend the protection in Settings > Branches, then re-enable it
immediately afterwards.

Typical workflow:

```
git checkout -b feature/my-change
# ... commits ...
git push -u origin feature/my-change
gh pr create            # then review + green CI -> merge
```
