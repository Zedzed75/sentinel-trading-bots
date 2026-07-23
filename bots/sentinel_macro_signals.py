"""SENTINEL MACRO ANALYST - Directional signal layers of bot 7 (v2).

Pure helpers for the agentic pipeline that turns qualified news into an
actionable macro signal (Cost Control by design):

- Layer 1 (zero token) lives in sentinel_macro_sources.py: local entity
  filtering drops the noise before any LLM sees it.
- Layer 2 (triage): ONE batch call on a fast model scores every
  remaining item 1-10; items below TRIAGE_THRESHOLD stop there.
- Layer 3 (analyst): the heavy model receives only the qualified items
  and must answer through a strict JSON schema (structured output -
  free text is impossible by construction).

Outputs: bots/macro_signal.json (flag read by the MT5 bots - enforcement
is behind macro_config.json "macro_gate_enabled", default OFF until the
macro filter is backtested, see AMELIORATION_CONTINUE.md roadmap 4) and
the macro_signals table in bots/arbitrage.db (bot 8's database, daily
upsert - the clean dataset for the weather x PnL validation).

The LLM calls themselves are orchestrated by sentinel_macro_analyst.py
(run_signal_pipeline); this module stays free of network access and is
directly testable (tests/test_sentinel_macro_signals.py).
"""

import json
import logging
import os
import sqlite3
from datetime import datetime

log = logging.getLogger("macro")

_DIR = os.path.dirname(os.path.abspath(__file__))
SIGNAL_FILE = os.path.join(_DIR, "macro_signal.json")
SIGNALS_DB = os.path.join(_DIR, "arbitrage.db")

TRIAGE_THRESHOLD = 7          # score >= 7 -> the item reaches the analyst
MAX_TRIAGE_ITEMS = 60         # hard cap on the batch (token control)

SIGNAL_ASSETS = ("XAUUSD", "XTIUSD", "XBRUSD", "EURUSD", "GBPUSD",
                 "US500", "USDCNH", "NONE")
SIGNAL_ACTIONS = ("BLOCK_BUY_SIGNALS", "BLOCK_SELL_SIGNALS",
                  "REDUCE_SIZE", "NONE")

PROMPT_TRIAGE = (
    "You are the triage analyst of a quantitative desk. Score each "
    "numbered news item from 1 to 10 for its potential to move the "
    "fleet's assets TODAY (gold, Brent/WTI, EURUSD, GBPUSD, US500, "
    "USDCNH). "
    "10 = likely violent repricing (war escalation, surprise central "
    "bank move, shock release); 5 = notable but priced in; 1 = noise. "
    "Score every item, nothing else.")

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"index": {"type": "integer"},
                               "score": {"type": "integer"}},
                "required": ["index", "score"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["scores"],
    "additionalProperties": False,
}

PROMPT_ANALYST = (
    "You are the senior macro strategist of a quantitative desk. From "
    "the highly qualified news below (already triaged, impact >= 7/10), "
    "produce ONE actionable macro thesis for the day: the single fleet "
    "asset most affected, the directional bias, a 0-100 confidence, a "
    "one-sentence rationale citing the dossier, and the protective "
    "action for the execution bots (BLOCK_BUY_SIGNALS if the bias "
    "argues against longs, BLOCK_SELL_SIGNALS against shorts, "
    "REDUCE_SIZE for two-sided event risk, NONE if no protection is "
    "warranted). Never invent facts absent from the dossier; if nothing "
    "dominates, answer NONE/NEUTRAL.")

SIGNAL_SCHEMA = {
    "type": "object",
    "properties": {
        "asset_affected": {"type": "string", "enum": list(SIGNAL_ASSETS)},
        "macro_bias": {"type": "string",
                       "enum": ["BULLISH", "BEARISH", "NEUTRAL"]},
        "confidence_score": {"type": "integer"},
        "rationale": {"type": "string"},
        "action_for_mt5": {"type": "string", "enum": list(SIGNAL_ACTIONS)},
    },
    "required": ["asset_affected", "macro_bias", "confidence_score",
                 "rationale", "action_for_mt5"],
    "additionalProperties": False,
}

NO_SIGNAL = {
    "asset_affected": "NONE", "macro_bias": "NEUTRAL",
    "confidence_score": 0,
    "rationale": "no qualified news (automatic fallback)",
    "action_for_mt5": "NONE",
}


# --- Layer 2 helpers (pure) ---------------------------------------------------
def flatten_items(sources: dict) -> list[str]:
    """The triage corpus: geo + social + bank titles (the calendar is
    factual, not news - it goes to the analyst dossier, not the triage)."""
    items = [t for key in ("geo", "social", "banks")
             for t in sources.get(key, [])]
    return items[:MAX_TRIAGE_ITEMS]


def numbered(items: list[str]) -> str:
    """The numbered corpus sent to the triage model."""
    return "\n".join(f"{i}. {t}" for i, t in enumerate(items))


def qualified(items: list[str], verdict: dict,
              threshold: int = TRIAGE_THRESHOLD) -> list[str]:
    """Items whose triage score reaches the threshold; out-of-range
    indices are ignored (defensive against a sloppy scorer)."""
    keep = {s["index"] for s in verdict.get("scores", [])
            if s.get("score", 0) >= threshold}
    return [t for i, t in enumerate(items) if i in keep]


def clamp_signal(raw: dict) -> dict:
    """Schema-validated already; clamp the confidence to [0, 100]."""
    out = dict(raw)
    out["confidence_score"] = min(100, max(0, int(raw["confidence_score"])))
    return out


# --- Outputs -------------------------------------------------------------------
def write_signal(signal: dict, now: datetime, kept: int, total: int,
                 path: str | None = None):
    """bots/macro_signal.json: the flag read by the MT5 bots (atomic)."""
    payload = signal | {"date": now.date().isoformat(),
                        "generated_at": now.isoformat(),
                        "triage_kept": kept, "triage_total": total}
    target = path or SIGNAL_FILE
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    os.replace(tmp, target)


def insert_signal_db(signal: dict, now: datetime, kept: int, total: int,
                     db_path: str | None = None):
    """Daily upsert into the macro_signals table (bot 8's SQLite file,
    dataset for the statistical validation); NEVER raises."""
    try:
        con = sqlite3.connect(db_path or SIGNALS_DB)
        try:
            con.execute("""
                CREATE TABLE IF NOT EXISTS macro_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date_utc TIMESTAMP NOT NULL,
                    asset_affected VARCHAR NOT NULL,
                    macro_bias VARCHAR NOT NULL,
                    confidence_score INTEGER NOT NULL,
                    rationale VARCHAR NOT NULL,
                    action_for_mt5 VARCHAR NOT NULL,
                    triage_kept INTEGER NOT NULL,
                    triage_total INTEGER NOT NULL
                )""")
            con.execute("DELETE FROM macro_signals WHERE date_utc LIKE ?",
                        (now.date().isoformat() + "%",))
            con.execute(
                "INSERT INTO macro_signals (date_utc, asset_affected,"
                " macro_bias, confidence_score, rationale, action_for_mt5,"
                " triage_kept, triage_total) VALUES (?,?,?,?,?,?,?,?)",
                (now.isoformat(), signal["asset_affected"],
                 signal["macro_bias"], signal["confidence_score"],
                 signal["rationale"], signal["action_for_mt5"],
                 kept, total))
            con.commit()
        finally:
            con.close()
    except sqlite3.Error as exc:
        log.warning("macro_signals DB write KO (%s): signal not blocked.",
                    exc)
