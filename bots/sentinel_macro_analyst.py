"""SENTINEL MACRO ANALYST (bot 7) - Boussole macro-economique multi-agents.

Ne trade pas et ne touche pas a MT5. Chaque matin :
- 08:00 UTC : ingere les trois familles de sources (geopolitique/energie,
  influenceurs/reseaux sociaux, calendrier macro - voir
  sentinel_macro_sources.py) puis reunit un CONSEIL de trois agents LLM
  specialises et asynchrones (Geopolitique, Macro & banques centrales,
  Sentiment/reseaux sociaux), tranche par un SYNTHETISEUR
  (sortie JSON structuree garantie par schema).
- 08:30 UTC : publie la "Meteo du Marche" sur le canal Telegram existant.

Modeles (API Anthropic) : mapping fin par agent, surchargeable via
macro_config.json "model_mapping" - defauts : geo/macro sur claude-fable-5
(effort low + fallback serveur opus en cas de refus classifieur),
sentiment/flux sur claude-haiku-4-5, juge sur claude-opus-4-8. Sobriete de
tokens : dossiers sectorises par agent, limites de mots, MAX_TOKENS 4000.

Sorties : macro_weather.json (statut instantane), macro_history.json
(archive quotidienne compacte pour la validation statistique) et le
rapport Telegram. INFORMATIF : aucun sizing modifie tant que le filtre
macro n'est pas backteste (roadmap 4). Repli : toute panne ecrit une
meteo NEUTRE sans jamais tuer le processus.

Config : bots/macro_config.json (gitignore, voir l'exemple) ou
ANTHROPIC_API_KEY. `--once` : pipeline immediat puis exit.
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone

import httpx
from anthropic import AsyncAnthropic

from sentinel_macro_sources import build_dossier, collect_all

# --- Configuration ---
COLLECT_HOUR = 8              # 08:00 UTC : ingestion + conseil LLM
SEND_HOUR, SEND_MINUTE = 8, 30  # 08:30 UTC : envoi Telegram
POLL_SECONDS = 30

# Modeles par agent, surchargeables via macro_config.json "model_mapping"
# (memes cles) : fable-5 = raisonnement geo/macro (effort low, fallback
# serveur opus sur refus), haiku-4-5 = scanners, opus-4-8 = juge.
DEFAULT_MODELS = {
    "agent_geopolitics": "claude-fable-5",
    "agent_macro": "claude-fable-5",
    "agent_sentiment": "claude-haiku-4-5",
    "agent_flow_trader": "claude-haiku-4-5",
    "agent_juge_synthesizer": "claude-opus-4-8",
}
MAX_TOKENS, FETCH_TIMEOUT = 4000, 20.0  # plafond dur par appel ; timeout

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

# Meteo -> (emoji, reco bots 1&3 directionnels, reco bot 2 stat-arb/reversion)
WEATHER_META = {
    "ORAGEUX": ("\U0001f32a️",
                "\U0001f7e2 Priorite haute (recherche de momentum)",
                "\U0001f7e1 Prudence autour des annonces (spread instable)"),
    "CALME": ("☀️",
              "\U0001f7e1 Prudence (faux breakouts probables)",
              "\U0001f7e2 Conditions favorables (retour a la moyenne)"),
    "NEUTRE": ("⛅",
               "\U0001f7e1 Regime normal, pas de biais",
               "\U0001f7e1 Regime normal, pas de biais"),
}

AGENTS = (
    {"cle": "geo", "nom": "Geopolitique",
     "model_key": "agent_geopolitics", "sections": ("calendar", "geo"),
     "system": (
         "Tu es un analyste geopolitique senior specialise dans les goulots "
         "d'etranglement de l'energie (detroit d'Ormuz, Bab el-Mandeb, mer "
         "Rouge) et les metaux precieux. A partir du dossier, evalue "
         "l'impact des tensions du jour sur l'Or (XAU) et le Petrole "
         "(Brent/WTI) : risque d'escalade, primes de risque, flux refuge. "
         "3 a 5 phrases, 80 mots maximum, en francais, elements "
         "concrets du dossier, uniquement l'analyse.")},
    {"cle": "macro", "nom": "Macro & banques centrales",
     "model_key": "agent_macro", "sections": ("calendar", "geo"),
     "system": (
         "Tu es un economiste de marche. Analyse la politique de la Fed, "
         "l'inflation, les taux d'interet et le calendrier economique du "
         "dossier ; evalue l'impact attendu sur USD, EUR, GBP et les "
         "indices pour la journee (heures UTC des annonces). 3 a 5 "
         "phrases, 80 mots maximum, en francais, uniquement l'analyse.")},
    {"cle": "sentiment", "nom": "Sentiment & reseaux sociaux",
     "model_key": "agent_sentiment", "sections": ("social",),
     "system": (
         "Tu es un specialiste de la psychologie des foules et du "
         "sentiment de marche. Analyse les declarations d'influenceurs du "
         "dossier (Trump, Musk, officiels US/Chine...) et detecte si une "
         "rumeur ou une annonce non conventionnelle peut creer une panique "
         "ou un mouvement violent a l'ouverture. Ignore le bruit sans lien "
         "avec l'or, le petrole ou les devises. 2 a 4 phrases, 60 mots "
         "maximum, en francais, uniquement l'analyse.")},
    {"cle": "flux", "nom": "Stratege de flux (sell-side)",
     "model_key": "agent_flow_trader", "sections": ("banks",),
     "system": (
         "Tu es un stratege de marche sell-side dans une grande banque "
         "d'affaires. A partir des notes bank desks du dossier (Goldman "
         "Sachs, JPMorgan, Morgan Stanley, Citi...), analyse le "
         "positionnement des gros flux institutionnels, les niveaux de "
         "support/resistance cites et les zones probables de liquidation "
         "massive (stop hunting, poches de liquidite) sur l'Or et le "
         "Forex. Cite banque et niveau quand le dossier les donne ; "
         "n'invente JAMAIS un niveau de prix absent du dossier. 2 a 4 "
         "phrases, 60 mots maximum, en francais, uniquement l'analyse.")},
)

PROMPT_SYNTH = (
    "Tu es le SYNTHETISEUR d'un conseil de quatre analystes (geopolitique, "
    "macro/banques centrales, sentiment/reseaux sociaux, stratege de flux "
    "sell-side). Lis le dossier puis leurs analyses, confronte-les et "
    "tranche la meteo du jour pour un desk quantitatif : ORAGEUX (forte "
    "volatilite directionnelle attendue, favorable au trend/breakout), "
    "CALME (marche plat/compresse, favorable au stat-arb et a la "
    "reversion) ou NEUTRE (aucun signal dominant). Donne une confiance "
    "entre 0 et 1, le focus principal du jour (annonce ou theme, heure UTC "
    "si connue), un resume d'une phrase de chaque analyse, une ligne "
    "'cibles bancaires' (banque + niveau si le dossier en donne, sinon "
    "'aucune cible publiee'), le CONFLIT D'INTERET DU JOUR : le "
    "desaccord le plus significatif entre deux analystes et comment tu le "
    "tranches (ou 'aucun conflit notable'), et primary_asset : l'actif de "
    "la flotte le plus concerne par le focus du jour (AUCUN si aucun ne "
    "domine). Reponds en francais, chaque champ en une seule phrase dense.")

FLEET_ASSETS = ("XAUUSD", "XTIUSD", "XBRUSD", "EURUSD", "GBPUSD", "US500", "AUCUN")

SYNTH_SCHEMA = {
    "type": "object",
    "properties": {
        "weather": {"type": "string", "enum": ["ORAGEUX", "CALME", "NEUTRE"]},
        "confidence": {"type": "number"},
        "focus": {"type": "string"},
        "geo_resume": {"type": "string"},
        "macro_resume": {"type": "string"},
        "sentiment_resume": {"type": "string"},
        "banks_resume": {"type": "string"},
        "conflict": {"type": "string"},
        "primary_asset": {"type": "string", "enum": list(FLEET_ASSETS)},
    },
    "required": ["weather", "confidence", "focus", "geo_resume",
                 "macro_resume", "sentiment_resume", "banks_resume",
                 "conflict", "primary_asset"],
    "additionalProperties": False,
}

NEUTRAL_FALLBACK = {
    "weather": "NEUTRE", "confidence": 0.0,
    "focus": "indisponible (repli automatique)",
    "geo_resume": "indisponible", "macro_resume": "indisponible",
    "sentiment_resume": "indisponible", "banks_resume": "indisponible",
    "conflict": "indisponible", "primary_asset": "AUCUN",
}


# --- Etat, fenetres horaires et fichiers (fonctions pures, testables) ---
def load_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_json_atomic(path: str, payload: dict):
    """Temporaire + os.replace : l'etat precedent survit a un crash."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    os.replace(tmp, path)


def write_heartbeat(path: str = HEARTBEAT_FILE,
                    now: datetime | None = None):
    """Estampille de vie apres chaque cycle reussi (lue par le watchdog)."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write((now or datetime.now(timezone.utc)).isoformat())
    except OSError:
        pass


def should_collect(state: dict, now: datetime) -> bool:
    """Ingestion + conseil une seule fois par jour, a partir de 08:00 UTC."""
    return (now.hour >= COLLECT_HOUR
            and state.get("last_collect_day") != now.date().isoformat())


def should_send(state: dict, now: datetime) -> bool:
    """Envoi une seule fois par jour, a partir de 08:30 UTC strictement,
    et uniquement si le rapport du jour a ete prepare."""
    today = now.date().isoformat()
    return (state.get("report_day") == today
            and state.get("last_send_day") != today
            and (now.hour, now.minute) >= (SEND_HOUR, SEND_MINUTE))


def write_weather(verdict: dict, now: datetime, path: str | None = None):
    """Sortie 1 : le fichier partage macro_weather.json (ecriture atomique).

    Contient aussi les resumes du conseil et le conflit du jour : le
    dashboard les affiche dans ses onglets Debat/Cibles/Conflit."""
    save_json_atomic(path or WEATHER_FILE, {
        "weather": verdict["weather"],
        "confidence": round(float(verdict["confidence"]), 2),
        "focus": verdict["focus"],
        "geo_resume": verdict.get("geo_resume", ""),
        "macro_resume": verdict.get("macro_resume", ""),
        "sentiment_resume": verdict.get("sentiment_resume", ""),
        "banks_resume": verdict.get("banks_resume", ""),
        "conflict": verdict.get("conflict", ""),
        "primary_asset": verdict.get("primary_asset", "AUCUN"),
        "date": now.date().isoformat(),
        "generated_at": now.isoformat(),
    })


def append_history(verdict: dict, now: datetime, path: str | None = None):
    """Archive quotidienne (tableau trie par date, upsert du jour) pour
    la validation statistique ; ne leve JAMAIS (E/S non bloquante)."""
    try:
        hist = []
        try:
            with open(path or HISTORY_FILE, encoding="utf-8") as fh:
                data = json.load(fh)
            hist = data if isinstance(data, list) else []
        except (OSError, ValueError):
            pass                                  # fichier absent : cree
        today = now.date().isoformat()
        hist = [e for e in hist if e.get("date") != today]
        hist.append({"date": today, "weather": verdict["weather"],
                     "confidence": round(float(verdict["confidence"]), 2),
                     "focus": verdict["focus"],
                     "primary_asset": verdict.get("primary_asset", "AUCUN")})
        hist.sort(key=lambda e: e.get("date", ""))
        save_json_atomic(path or HISTORY_FILE, hist)
    except Exception as exc:
        log.warning("Archivage meteo KO (%s) : rapport non bloque.", exc)


# --- Le conseil multi-agents (4 specialistes en parallele, puis le synthetiseur) ---
def agent_models() -> dict:
    """Mapping effectif : defauts + surcharges de macro_config.json."""
    return DEFAULT_MODELS | (load_json(CONFIG_FILE).get("model_mapping")
                             or {})


def _llm_kwargs(model: str) -> dict:
    """Regles API par famille : fable = thinking integre + effort low +
    fallback opus ; haiku = ni adaptatif ni effort ; opus = adaptatif."""
    if "fable" in model:
        return {"betas": ["server-side-fallback-2026-06-01"],
                "fallbacks": [{"model": "claude-opus-4-8"}],
                "output_config": {"effort": "low"}}
    if "haiku" in model:
        return {}
    return {"thinking": {"type": "adaptive"},
            "output_config": {"effort": "medium"}}


async def _create(llm: AsyncAnthropic, model: str, **kw):
    """Appel LLM avec les parametres de la famille ; leve sur refus."""
    extra = _llm_kwargs(model)
    if "output_config" in kw:                 # fusion format + effort
        kw["output_config"] = (extra.pop("output_config", {})
                               | kw["output_config"])
    api = llm.beta.messages if "betas" in extra else llm.messages
    resp = await api.create(model=model, max_tokens=MAX_TOKENS,
                            **extra, **kw)
    if resp.stop_reason == "refusal":
        raise RuntimeError(f"refus du modele {model}")
    return resp


async def _analysis(llm: AsyncAnthropic, agent: dict, sources: dict,
                    now: datetime, models: dict) -> str:
    """Un specialiste : ne recoit que ses sections du dossier (tokens)."""
    dossier = build_dossier(sources, now, only=agent["sections"])
    resp = await _create(llm, models[agent["model_key"]],
                         system=agent["system"],
                         messages=[{"role": "user", "content": dossier}])
    return next((b.text for b in resp.content if b.type == "text"), "").strip()


async def run_council(llm: AsyncAnthropic, sources: dict,
                      now: datetime) -> dict:
    """Quatre analyses en parallele, puis le synthetiseur (JSON par schema).

    Toute erreur (API, reseau, refus) => meteo NEUTRE, sans lever.
    """
    try:
        models = agent_models()
        analyses = await asyncio.gather(*(_analysis(llm, a, sources, now,
                                                    models) for a in AGENTS))
        council = build_dossier(sources, now) + "".join(
            f"\n\nANALYSE {a['nom'].upper()} :\n{txt}"
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
            raise ValueError(f"meteo inconnue {verdict['weather']!r}")
        return verdict
    except Exception as exc:
        log.error("Conseil LLM en echec (%s) : repli meteo NEUTRE.", exc)
        return dict(NEUTRAL_FALLBACK)


def format_report(verdict: dict, now: datetime) -> str:
    """Sortie 2 : le rapport Telegram structure."""
    emoji, reco_dir, reco_range = WEATHER_META[verdict["weather"]]
    pct = round(float(verdict["confidence"]) * 100)
    return (f"\U0001f916 [SENTINEL BOT 7] — METEO DU MARCHE ({now:%d/%m/%Y})\n"
            f"\nMeteo : {emoji} {verdict['weather']} (Confiance : {pct}%)\n"
            f"Focus : {verdict['focus']}\n"
            f"\n\U0001f9ed LE CONSEIL :\n"
            f"• Geopolitique : \"{verdict['geo_resume']}\"\n"
            f"• Macro : \"{verdict['macro_resume']}\"\n"
            f"• Sentiment : \"{verdict['sentiment_resume']}\"\n"
            f"\n\U0001f4bc ANALYSTES & BANK DESKS (Agent 4) :\n"
            f"• {verdict['banks_resume']}\n"
            f"\n\U0001f3af CONFLIT D'INTERET DU JOUR :\n"
            f"{verdict['conflict']}\n"
            f"\n\U0001f3af PRECONISATION FLOTTE :\n"
            f"• Bot 1 (Breakout) & Bot 3 (Trend) : {reco_dir}\n"
            f"• Bot 2 (Stat-arb) & Reversion : {reco_range}\n"
            f"\n(Informatif : aucun sizing n'est modifie tant que le filtre "
            f"macro n'est pas backteste - AMELIORATION_CONTINUE.md)")


# --- Telegram (reutilise les credentials du projet, jamais commites) ---
async def send_telegram(text: str) -> bool:
    token = (load_json(TELEGRAM_CONFIG).get("token")
             or os.environ.get("TELEGRAM_BOT_TOKEN"))
    chat_id = load_json(TELEGRAM_STATE).get("chat_id")
    if not token or not chat_id:
        log.warning("Telegram non configure (token/chat_id) : envoi saute.")
        return False
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=FETCH_TIMEOUT)
        return bool(resp.json().get("ok"))
    except Exception as exc:
        log.error("Envoi Telegram KO : %s", exc)
        return False


# --- Cycle principal ---
async def collect_and_judge(llm: AsyncAnthropic, state: dict, now: datetime):
    """08:00 : sources -> conseil -> macro_weather.json + rapport en attente."""
    cfg = load_json(CONFIG_FILE)
    sources = await collect_all(
        extra_social=tuple(cfg.get("social_feeds") or ()),
        extra_bank=tuple(cfg.get("bank_feeds") or ()), now=now)
    verdict = await run_council(llm, sources, now)
    write_weather(verdict, now)
    append_history(verdict, now)
    state["last_collect_day"] = now.date().isoformat()
    state["report_day"] = now.date().isoformat()
    state["report"] = format_report(verdict, now)
    save_json_atomic(STATE_FILE, state)
    log.info("Meteo du %s : %s (confiance %.2f) -> macro_weather.json",
             now.date(), verdict["weather"], verdict["confidence"])


async def run_cycle(llm: AsyncAnthropic, state: dict,
                    now: datetime | None = None):
    now = now or datetime.now(timezone.utc)
    if should_collect(state, now):
        await collect_and_judge(llm, state, now)
    if should_send(state, now):
        if await send_telegram(state.get("report", "")):
            log.info("Rapport meteo envoye sur Telegram.")
        state["last_send_day"] = now.date().isoformat()
        save_json_atomic(STATE_FILE, state)


def anthropic_key() -> str | None:
    return (load_json(CONFIG_FILE).get("anthropic_api_key")
            or os.environ.get("ANTHROPIC_API_KEY"))


async def main_async(once: bool = False) -> int:
    key = anthropic_key()
    if not key:
        log.warning("Pas de cle API Anthropic : creer bots/macro_config.json "
                    "(voir macro_config.example.json). En attente...")
    while not key:                    # attente passive, watchdog-friendly
        write_heartbeat()
        await asyncio.sleep(60)
        key = anthropic_key()
    llm = AsyncAnthropic(api_key=key)
    state = load_json(STATE_FILE)
    if once:                          # pipeline immediat (test manuel)
        now = datetime.now(timezone.utc)
        await collect_and_judge(llm, state, now)
        print(state["report"])
        if await send_telegram(state["report"]):
            state["last_send_day"] = now.date().isoformat()
            save_json_atomic(STATE_FILE, state)   # pas de double envoi
        return 0
    log.info("Demarrage SENTINEL MACRO ANALYST (ingestion %02d:00, envoi "
             "%02d:%02d UTC, %d agents + synthetiseur)",
             COLLECT_HOUR, SEND_HOUR, SEND_MINUTE, len(AGENTS))
    while True:
        try:
            await run_cycle(llm, state)
            write_heartbeat()
        except Exception as exc:      # jamais de crash : repli et on continue
            log.exception("Erreur inattendue : %s", exc)
        await asyncio.sleep(POLL_SECONDS)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    return asyncio.run(main_async(once="--once" in sys.argv))


if __name__ == "__main__":
    raise SystemExit(main())
