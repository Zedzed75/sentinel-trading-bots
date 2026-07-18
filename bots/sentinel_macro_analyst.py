"""SENTINEL MACRO ANALYST (bot 7) - Multi-agent macro-economic compass.

Does not trade and never touches MT5. Every morning:
- 08:00 UTC: ingests the three source families (geopolitics/energy,
  influencers/social media, macro calendar - see
  sentinel_macro_sources.py) then convenes a COUNCIL of specialized,
  asynchronous LLM agents (Geopolitics, Macro & central banks,
  Sentiment/social media), decided by a SYNTHESIZER
  (structured JSON output guaranteed by schema).
- 08:30 UTC: publishes the "Market Weather" on the existing Telegram
  channel.

Models (Anthropic API): fine-grained mapping per agent, overridable via
macro_config.json "model_mapping" - defaults: geo/macro on claude-fable-5
(low effort + server-side opus fallback on classifier refusal),
sentiment/flow on claude-haiku-4-5, judge on claude-opus-4-8. Token
economy: per-agent sectorized dossiers, word limits, MAX_TOKENS 4000.

Outputs: macro_weather.json (instant status), macro_history.json
(compact daily archive for statistical validation) and the Telegram
report. INFORMATIONAL: no sizing is modified until the macro filter is
backtested (roadmap 4). Fallback: any failure writes a NEUTRAL weather
without ever killing the process.

Config: bots/macro_config.json (gitignored, see the example) or
ANTHROPIC_API_KEY. `--once`: immediate pipeline then exit.
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone

import httpx
from anthropic import AsyncAnthropic

import sentinel_macro_signals as msig
from sentinel_macro_sources import build_dossier, collect_all

# --- Configuration ---
COLLECT_HOUR = 8              # 08:00 UTC: ingestion + LLM council
SEND_HOUR, SEND_MINUTE = 8, 30  # 08:30 UTC: Telegram send
POLL_SECONDS = 30

# Models per agent, overridable via macro_config.json "model_mapping"
# (same keys): fable-5 = geo/macro reasoning (low effort, server-side
# opus fallback on refusal), haiku-4-5 = scanners, opus-4-8 = judge.
DEFAULT_MODELS = {
    "agent_geopolitics": "claude-fable-5",
    "agent_macro": "claude-fable-5",
    "agent_sentiment": "claude-haiku-4-5",
    "agent_flow_trader": "claude-haiku-4-5",
    "agent_juge_synthesizer": "claude-opus-4-8",
    "agent_triage": "claude-haiku-4-5",
    "agent_analyst": "claude-opus-4-8",
}
MAX_TOKENS, FETCH_TIMEOUT = 4000, 20.0  # hard cap per call; timeout

_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(os.path.dirname(_DIR), "logs")
WEATHER_FILE = os.path.join(_DIR, "macro_weather.json")
HISTORY_FILE = os.path.join(_DIR, "macro_history.json")
STATE_FILE = os.path.join(_DIR, "macro_state.json")
CONFIG_FILE = os.path.join(_DIR, "macro_config.json")
TELEGRAM_CONFIG = os.path.join(_DIR, "telegram_config.json")
TELEGRAM_STATE = os.path.join(_DIR, "telegram_state.json")
HEARTBEAT_FILE = os.path.join(LOG_DIR, "sentinel_macro_analyst.hb")

log = logging.getLogger("macro")

# Weather -> (emoji, reco for directional bots 1&3, reco for bot 2 stat-arb/reversion)
WEATHER_META = {
    "STORMY": ("\U0001f32a️",
               "\U0001f7e2 High priority (momentum hunting)",
               "\U0001f7e1 Caution around releases (unstable spread)"),
    "CALM": ("☀️",
             "\U0001f7e1 Caution (false breakouts likely)",
             "\U0001f7e2 Favourable conditions (mean reversion)"),
    "NEUTRAL": ("⛅",
                "\U0001f7e1 Normal regime, no bias",
                "\U0001f7e1 Normal regime, no bias"),
}

# Legacy French values still present in files written before the
# English migration (macro_weather.json, macro_history.json).
LEGACY_VALUES = {"ORAGEUX": "STORMY", "CALME": "CALM", "NEUTRE": "NEUTRAL",
                 "AUCUN": "NONE"}

AGENTS = (
    {"key": "geo", "name": "Geopolitics",
     "model_key": "agent_geopolitics", "sections": ("calendar", "geo"),
     "system": (
         "You are a senior geopolitical analyst specialized in energy "
         "chokepoints (Strait of Hormuz, Bab el-Mandeb, Red Sea) and "
         "precious metals. From the dossier, assess the impact of today's "
         "tensions on Gold (XAU) and Oil (Brent/WTI): escalation risk, "
         "risk premiums, safe-haven flows. 3 to 5 sentences, 80 words "
         "maximum, in English, concrete elements from the dossier, "
         "analysis only.")},
    {"key": "macro", "name": "Macro & central banks",
     "model_key": "agent_macro", "sections": ("calendar", "geo"),
     "system": (
         "You are a market economist. Analyze Fed policy, inflation, "
         "interest rates and the economic calendar in the dossier; assess "
         "the expected impact on USD, EUR, GBP and indices for the day "
         "(UTC times of the releases). 3 to 5 sentences, 80 words "
         "maximum, in English, analysis only.")},
    {"key": "sentiment", "name": "Sentiment & social media",
     "model_key": "agent_sentiment", "sections": ("social",),
     "system": (
         "You are a specialist in crowd psychology and market sentiment. "
         "Analyze the influencer statements in the dossier (Trump, Musk, "
         "US/China officials...) and detect whether a rumour or an "
         "unconventional announcement could create a panic or a violent "
         "move at the open. Ignore noise unrelated to gold, oil or "
         "currencies. 2 to 4 sentences, 60 words maximum, in English, "
         "analysis only.")},
    {"key": "flow", "name": "Flow strategist (sell-side)",
     "model_key": "agent_flow_trader", "sections": ("banks",),
     "system": (
         "You are a sell-side market strategist at a major investment "
         "bank. From the bank-desk notes in the dossier (Goldman Sachs, "
         "JPMorgan, Morgan Stanley, Citi...), analyze the positioning of "
         "large institutional flows, the quoted support/resistance levels "
         "and the likely zones of massive liquidation (stop hunting, "
         "liquidity pockets) on Gold and Forex. Quote bank and level when "
         "the dossier provides them; NEVER invent a price level absent "
         "from the dossier. 2 to 4 sentences, 60 words maximum, in "
         "English, analysis only.")},
)

PROMPT_SYNTH = (
    "You are the SYNTHESIZER of a council of four analysts (geopolitics, "
    "macro/central banks, sentiment/social media, sell-side flow "
    "strategist). Read the dossier then their analyses, confront them and "
    "decide today's weather for a quantitative desk: STORMY (strong "
    "directional volatility expected, favourable to trend/breakout), "
    "CALM (flat/compressed market, favourable to stat-arb and "
    "reversion) or NEUTRAL (no dominant signal). Give a confidence "
    "between 0 and 1, the main focus of the day (release or theme, UTC "
    "time if known), a one-sentence summary of each analysis, a 'bank "
    "targets' line (bank + level if the dossier provides one, otherwise "
    "'no published target'), the CONFLICT OF THE DAY: the most "
    "significant disagreement between two analysts and how you settle it "
    "(or 'no notable conflict'), and primary_asset: the fleet asset most "
    "concerned by today's focus (NONE if none dominates). Answer in "
    "English, each field in a single dense sentence.")

FLEET_ASSETS = ("XAUUSD", "XTIUSD", "XBRUSD", "EURUSD", "GBPUSD", "US500", "NONE")

SYNTH_SCHEMA = {
    "type": "object",
    "properties": {
        "weather": {"type": "string", "enum": ["STORMY", "CALM", "NEUTRAL"]},
        "confidence": {"type": "number"},
        "focus": {"type": "string"},
        "geo_summary": {"type": "string"},
        "macro_summary": {"type": "string"},
        "sentiment_summary": {"type": "string"},
        "banks_summary": {"type": "string"},
        "conflict": {"type": "string"},
        "primary_asset": {"type": "string", "enum": list(FLEET_ASSETS)},
    },
    "required": ["weather", "confidence", "focus", "geo_summary",
                 "macro_summary", "sentiment_summary", "banks_summary",
                 "conflict", "primary_asset"],
    "additionalProperties": False,
}

NEUTRAL_FALLBACK = {
    "weather": "NEUTRAL", "confidence": 0.0,
    "focus": "unavailable (automatic fallback)",
    "geo_summary": "unavailable", "macro_summary": "unavailable",
    "sentiment_summary": "unavailable", "banks_summary": "unavailable",
    "conflict": "unavailable", "primary_asset": "NONE",
}


# --- State, time windows and files (pure, testable functions) ---
def load_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_json_atomic(path: str, payload: dict):
    """Temp file + os.replace: the previous state survives a crash."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    os.replace(tmp, path)


def write_heartbeat(path: str = HEARTBEAT_FILE,
                    now: datetime | None = None):
    """Liveness timestamp after each successful cycle (read by the watchdog)."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write((now or datetime.now(timezone.utc)).isoformat())
    except OSError:
        pass


def should_collect(state: dict, now: datetime) -> bool:
    """Ingestion + council once per day, from 08:00 UTC."""
    return (now.hour >= COLLECT_HOUR
            and state.get("last_collect_day") != now.date().isoformat())


def should_send(state: dict, now: datetime) -> bool:
    """Send once per day, strictly from 08:30 UTC, and only if today's
    report has been prepared."""
    today = now.date().isoformat()
    return (state.get("report_day") == today
            and state.get("last_send_day") != today
            and (now.hour, now.minute) >= (SEND_HOUR, SEND_MINUTE))


def write_weather(verdict: dict, now: datetime, path: str | None = None):
    """Output 1: the shared macro_weather.json file (atomic write).

    Also contains the council's summaries and the conflict of the day:
    the dashboard shows them in its Debate/Targets/Conflict tabs."""
    save_json_atomic(path or WEATHER_FILE, {
        "weather": verdict["weather"],
        "confidence": round(float(verdict["confidence"]), 2),
        "focus": verdict["focus"],
        "geo_summary": verdict.get("geo_summary", ""),
        "macro_summary": verdict.get("macro_summary", ""),
        "sentiment_summary": verdict.get("sentiment_summary", ""),
        "banks_summary": verdict.get("banks_summary", ""),
        "conflict": verdict.get("conflict", ""),
        "primary_asset": verdict.get("primary_asset", "NONE"),
        "date": now.date().isoformat(),
        "generated_at": now.isoformat(),
    })


def append_history(verdict: dict, now: datetime, path: str | None = None):
    """Daily archive (array sorted by date, upsert of the day) for
    statistical validation; NEVER raises (non-blocking I/O).

    Legacy French values (ORAGEUX...) in existing entries are migrated
    to English on the fly - the whole file is rewritten each day."""
    try:
        hist = []
        try:
            with open(path or HISTORY_FILE, encoding="utf-8") as fh:
                data = json.load(fh)
            hist = data if isinstance(data, list) else []
        except (OSError, ValueError):
            pass                                  # missing file: created
        for e in hist:                            # legacy value migration
            e["weather"] = LEGACY_VALUES.get(e.get("weather"), e.get("weather"))
            e["primary_asset"] = LEGACY_VALUES.get(e.get("primary_asset"),
                                                   e.get("primary_asset"))
        today = now.date().isoformat()
        hist = [e for e in hist if e.get("date") != today]
        hist.append({"date": today, "weather": verdict["weather"],
                     "confidence": round(float(verdict["confidence"]), 2),
                     "focus": verdict["focus"],
                     "primary_asset": verdict.get("primary_asset", "NONE")})
        hist.sort(key=lambda e: e.get("date", ""))
        save_json_atomic(path or HISTORY_FILE, hist)
    except Exception as exc:
        log.warning("Weather archiving KO (%s): report not blocked.", exc)


# --- The multi-agent council (4 specialists in parallel, then the synthesizer) ---
def agent_models() -> dict:
    """Effective mapping: defaults + macro_config.json overrides."""
    return DEFAULT_MODELS | (load_json(CONFIG_FILE).get("model_mapping")
                             or {})


def _llm_kwargs(model: str) -> dict:
    """API rules per family: fable = built-in thinking + low effort +
    opus fallback; haiku = neither adaptive nor effort; opus = adaptive."""
    if "fable" in model:
        return {"betas": ["server-side-fallback-2026-06-01"],
                "fallbacks": [{"model": "claude-opus-4-8"}],
                "output_config": {"effort": "low"}}
    if "haiku" in model:
        return {}
    return {"thinking": {"type": "adaptive"},
            "output_config": {"effort": "medium"}}


async def _create(llm: AsyncAnthropic, model: str, **kw):
    """LLM call with the family's parameters; raises on refusal."""
    extra = _llm_kwargs(model)
    if "output_config" in kw:                 # merge format + effort
        kw["output_config"] = (extra.pop("output_config", {})
                               | kw["output_config"])
    api = llm.beta.messages if "betas" in extra else llm.messages
    resp = await api.create(model=model, max_tokens=MAX_TOKENS,
                            **extra, **kw)
    if resp.stop_reason == "refusal":
        raise RuntimeError(f"refusal from model {model}")
    return resp


async def _analysis(llm: AsyncAnthropic, agent: dict, sources: dict,
                    now: datetime, models: dict) -> str:
    """One specialist: only receives its dossier sections (tokens)."""
    dossier = build_dossier(sources, now, only=agent["sections"])
    resp = await _create(llm, models[agent["model_key"]],
                         system=agent["system"],
                         messages=[{"role": "user", "content": dossier}])
    return next((b.text for b in resp.content if b.type == "text"), "").strip()


async def run_council(llm: AsyncAnthropic, sources: dict,
                      now: datetime) -> dict:
    """Four analyses in parallel, then the synthesizer (schema JSON).

    Any error (API, network, refusal) => NEUTRAL weather, without raising.
    """
    try:
        models = agent_models()
        analyses = await asyncio.gather(*(_analysis(llm, a, sources, now,
                                                    models) for a in AGENTS))
        council = build_dossier(sources, now) + "".join(
            f"\n\n{a['name'].upper()} ANALYSIS:\n{txt}"
            for a, txt in zip(AGENTS, analyses))
        resp = await _create(
            llm, models["agent_juge_synthesizer"], system=PROMPT_SYNTH,
            output_config={"format": {"type": "json_schema",
                                      "schema": SYNTH_SCHEMA}},
            messages=[{"role": "user", "content": council}])
        verdict = json.loads(next(b.text for b in resp.content
                                  if b.type == "text"))
        verdict["confidence"] = min(1.0, max(0.0,
                                             float(verdict["confidence"])))
        if verdict["weather"] not in WEATHER_META:
            raise ValueError(f"unknown weather {verdict['weather']!r}")
        return verdict
    except Exception as exc:
        log.error("LLM council failed (%s): NEUTRAL weather fallback.", exc)
        return dict(NEUTRAL_FALLBACK)


async def run_signal_pipeline(llm: AsyncAnthropic, sources: dict,
                              now: datetime) -> dict:
    """Layers 2+3 of the v2 pipeline (Cost Control by design).

    ONE batch triage call (fast model, 1-10 scores) drops everything
    below the threshold; only the qualified items reach the heavy
    analyst, which answers through a strict JSON schema. Any failure
    => NO_SIGNAL, without raising; the flag file and the macro_signals
    table are written in every case (calendar continuity).
    """
    items, kept = [], []
    signal = dict(msig.NO_SIGNAL)
    try:
        models = agent_models()
        items = msig.flatten_items(sources)
        if items:
            resp = await _create(
                llm, models["agent_triage"], system=msig.PROMPT_TRIAGE,
                output_config={"format": {"type": "json_schema",
                                          "schema": msig.TRIAGE_SCHEMA}},
                messages=[{"role": "user",
                           "content": msig.numbered(items)}])
            verdict = json.loads(next(b.text for b in resp.content
                                      if b.type == "text"))
            kept = msig.qualified(items, verdict)
        if kept:
            dossier = ("QUALIFIED NEWS (triage >= "
                       f"{msig.TRIAGE_THRESHOLD}/10):\n"
                       + "\n".join(f"- {t}" for t in kept)
                       + "\n\n" + build_dossier(sources, now,
                                                only=("calendar",)))
            resp = await _create(
                llm, models["agent_analyst"], system=msig.PROMPT_ANALYST,
                output_config={"format": {"type": "json_schema",
                                          "schema": msig.SIGNAL_SCHEMA}},
                messages=[{"role": "user", "content": dossier}])
            signal = msig.clamp_signal(
                json.loads(next(b.text for b in resp.content
                                if b.type == "text")))
    except Exception as exc:
        log.error("Signal pipeline failed (%s): NO_SIGNAL fallback.", exc)
        signal = dict(msig.NO_SIGNAL)
    msig.write_signal(signal, now, len(kept), len(items))
    msig.insert_signal_db(signal, now, len(kept), len(items))
    log.info("Macro signal %s: %s %s (confidence %d, %d/%d qualified) "
             "-> macro_signal.json + macro_signals", now.date(),
             signal["asset_affected"], signal["action_for_mt5"],
             signal["confidence_score"], len(kept), len(items))
    return signal


def format_report(verdict: dict, now: datetime) -> str:
    """Output 2: the structured Telegram report."""
    emoji, reco_dir, reco_range = WEATHER_META[verdict["weather"]]
    pct = round(float(verdict["confidence"]) * 100)
    return (f"\U0001f916 [SENTINEL BOT 7] — MARKET WEATHER ({now:%Y-%m-%d})\n"
            f"\nWeather: {emoji} {verdict['weather']} (Confidence: {pct}%)\n"
            f"Focus: {verdict['focus']}\n"
            f"\n\U0001f9ed THE COUNCIL:\n"
            f"• Geopolitics: \"{verdict['geo_summary']}\"\n"
            f"• Macro: \"{verdict['macro_summary']}\"\n"
            f"• Sentiment: \"{verdict['sentiment_summary']}\"\n"
            f"\n\U0001f4bc ANALYSTS & BANK DESKS (Agent 4):\n"
            f"• {verdict['banks_summary']}\n"
            f"\n\U0001f3af CONFLICT OF THE DAY:\n"
            f"{verdict['conflict']}\n"
            f"\n\U0001f3af FLEET RECOMMENDATION:\n"
            f"• Bot 1 (Breakout) & Bot 3 (Trend): {reco_dir}\n"
            f"• Bot 2 (Stat-arb) & Reversion: {reco_range}\n"
            f"\n(Informational: no sizing is modified until the macro "
            f"filter is backtested - AMELIORATION_CONTINUE.md)")


# --- Telegram (reuses the project's credentials, never committed) ---
async def send_telegram(text: str) -> bool:
    token = (load_json(TELEGRAM_CONFIG).get("token")
             or os.environ.get("TELEGRAM_BOT_TOKEN"))
    chat_id = load_json(TELEGRAM_STATE).get("chat_id")
    if not token or not chat_id:
        log.warning("Telegram not configured (token/chat_id): send skipped.")
        return False
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=FETCH_TIMEOUT)
        return bool(resp.json().get("ok"))
    except Exception as exc:
        log.error("Telegram send KO: %s", exc)
        return False


# --- Main cycle ---
async def collect_and_judge(llm: AsyncAnthropic, state: dict, now: datetime):
    """08:00: sources -> council -> macro_weather.json + pending report."""
    cfg = load_json(CONFIG_FILE)
    sources = await collect_all(
        extra_social=tuple(cfg.get("social_feeds") or ()),
        extra_bank=tuple(cfg.get("bank_feeds") or ()), now=now)
    verdict = await run_council(llm, sources, now)
    write_weather(verdict, now)
    append_history(verdict, now)
    await run_signal_pipeline(llm, sources, now)
    state["last_collect_day"] = now.date().isoformat()
    state["report_day"] = now.date().isoformat()
    state["report"] = format_report(verdict, now)
    save_json_atomic(STATE_FILE, state)
    log.info("Weather for %s: %s (confidence %.2f) -> macro_weather.json",
             now.date(), verdict["weather"], verdict["confidence"])


async def run_cycle(llm: AsyncAnthropic, state: dict,
                    now: datetime | None = None):
    now = now or datetime.now(timezone.utc)
    if should_collect(state, now):
        await collect_and_judge(llm, state, now)
    if should_send(state, now):
        if await send_telegram(state.get("report", "")):
            log.info("Weather report sent to Telegram.")
        state["last_send_day"] = now.date().isoformat()
        save_json_atomic(STATE_FILE, state)


def anthropic_key() -> str | None:
    return (load_json(CONFIG_FILE).get("anthropic_api_key")
            or os.environ.get("ANTHROPIC_API_KEY"))


def safe_print(text: str):
    """Console-safe print: Windows consoles (cp1252) cannot encode the
    report's emoji - degrade the display rather than crash before the
    Telegram send (issue #23)."""
    try:
        print(text)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(text.encode(enc, errors="replace").decode(enc))


async def main_async(once: bool = False) -> int:
    key = anthropic_key()
    if not key:
        log.warning("No Anthropic API key: create bots/macro_config.json "
                    "(see macro_config.example.json). Waiting...")
    while not key:                    # passive wait, watchdog-friendly
        write_heartbeat()
        await asyncio.sleep(60)
        key = anthropic_key()
    llm = AsyncAnthropic(api_key=key)
    state = load_json(STATE_FILE)
    if once:                          # immediate pipeline (manual test)
        now = datetime.now(timezone.utc)
        await collect_and_judge(llm, state, now)
        safe_print(state["report"])
        if await send_telegram(state["report"]):
            state["last_send_day"] = now.date().isoformat()
            save_json_atomic(STATE_FILE, state)   # no double send
        return 0
    log.info("Starting SENTINEL MACRO ANALYST (ingestion %02d:00, send "
             "%02d:%02d UTC, %d agents + synthesizer)",
             COLLECT_HOUR, SEND_HOUR, SEND_MINUTE, len(AGENTS))
    while True:
        try:
            await run_cycle(llm, state)
            write_heartbeat()
        except Exception as exc:      # never crash: fall back and continue
            log.exception("Unexpected error: %s", exc)
        await asyncio.sleep(POLL_SECONDS)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    return asyncio.run(main_async(once="--once" in sys.argv))


if __name__ == "__main__":
    raise SystemExit(main())
