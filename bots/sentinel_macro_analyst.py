"""SENTINEL MACRO ANALYST (bot 7) - Boussole macro-economique multi-agents.

Ne trade pas et ne touche pas a MT5. Chaque matin :
- 08:00 UTC : ingere les trois familles de sources (geopolitique/energie,
  influenceurs/reseaux sociaux, calendrier macro - voir
  sentinel_macro_sources.py) puis reunit un CONSEIL de trois agents LLM
  specialises et asynchrones (Geopolitique, Macro & banques centrales,
  Sentiment/reseaux sociaux), tranche par un SYNTHETISEUR
  (sortie JSON structuree garantie par schema).
- 08:30 UTC : publie la "Meteo du Marche" sur le canal Telegram existant.

Modeles (API Anthropic, seule infra de cles du projet) : les agents de
raisonnement tournent sur claude-opus-4-8 (thinking adaptatif), le scanner
de sentiment sur claude-haiku-4-5 (rapide/economique, comme le haiku
demande par la spec), le synthetiseur sur claude-opus-4-8.

Sorties : bots/macro_weather.json ({"weather", "confidence", ...}) et le
rapport Telegram. INFORMATIF : aucun sizing modifie tant que le filtre
macro n'est pas backteste (docs/AMELIORATION_CONTINUE.md, roadmap 4).
Repli : toute panne (reseau, API LLM, flux) ecrit une meteo NEUTRE et se
logge sans jamais tuer le processus.

Config : bots/macro_config.json {"anthropic_api_key": "...",
"social_feeds": [urls RSS premium optionnelles]} (gitignore, voir
macro_config.example.json) ou ANTHROPIC_API_KEY. `--once` : pipeline
immediat puis exit.
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

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
COLLECT_HOUR = 8              # 08:00 UTC : ingestion + conseil LLM
SEND_HOUR, SEND_MINUTE = 8, 30  # 08:30 UTC : envoi Telegram
POLL_SECONDS = 30

MODEL_REASONING = "claude-opus-4-8"   # agents 1-2 et synthetiseur
MODEL_SCANNER = "claude-haiku-4-5"    # agent 3 (bruit social, cout minimal)
MAX_TOKENS = 16000            # analyses courtes, marge pour le thinking

FETCH_TIMEOUT = 20.0

_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(os.path.dirname(_DIR), "logs")
WEATHER_FILE = os.path.join(_DIR, "macro_weather.json")
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

# Le conseil : trois agents specialises, roles systeme distincts.
AGENTS = (
    {"cle": "geo", "nom": "Geopolitique",
     "model": MODEL_REASONING,
     "system": (
         "Tu es un analyste geopolitique senior specialise dans les goulots "
         "d'etranglement de l'energie (detroit d'Ormuz, Bab el-Mandeb, mer "
         "Rouge) et les metaux precieux. A partir du dossier, evalue "
         "l'impact des tensions du jour sur l'Or (XAU) et le Petrole "
         "(Brent/WTI) : risque d'escalade, primes de risque, flux refuge. "
         "3 a 5 phrases en francais, elements concrets du dossier, "
         "uniquement l'analyse.")},
    {"cle": "macro", "nom": "Macro & banques centrales",
     "model": MODEL_REASONING,
     "system": (
         "Tu es un economiste de marche. Analyse la politique de la Fed, "
         "l'inflation, les taux d'interet et le calendrier economique du "
         "dossier ; evalue l'impact attendu sur USD, EUR, GBP et les "
         "indices pour la journee (heures UTC des annonces). 3 a 5 phrases "
         "en francais, uniquement l'analyse.")},
    {"cle": "sentiment", "nom": "Sentiment & reseaux sociaux",
     "model": MODEL_SCANNER,
     "system": (
         "Tu es un specialiste de la psychologie des foules et du "
         "sentiment de marche. Analyse les declarations d'influenceurs du "
         "dossier (Trump, Musk, officiels US/Chine...) et detecte si une "
         "rumeur ou une annonce non conventionnelle peut creer une panique "
         "ou un mouvement violent a l'ouverture. Ignore le bruit sans lien "
         "avec l'or, le petrole ou les devises. 2 a 4 phrases en francais, "
         "uniquement l'analyse.")},
    {"cle": "flux", "nom": "Stratege de flux (sell-side)",
     "model": MODEL_SCANNER,
     "system": (
         "Tu es un stratege de marche sell-side dans une grande banque "
         "d'affaires. A partir des notes bank desks du dossier (Goldman "
         "Sachs, JPMorgan, Morgan Stanley, Citi...), analyse le "
         "positionnement des gros flux institutionnels, les niveaux de "
         "support/resistance cites et les zones probables de liquidation "
         "massive (stop hunting, poches de liquidite) sur l'Or et le "
         "Forex. Cite banque et niveau quand le dossier les donne ; "
         "n'invente JAMAIS un niveau de prix absent du dossier. 2 a 4 "
         "phrases en francais, uniquement l'analyse.")},
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
    "'aucune cible publiee') et le CONFLIT D'INTERET DU JOUR : le "
    "desaccord le plus significatif entre deux analystes et comment tu le "
    "tranches (ou 'aucun conflit notable'). Reponds en francais.")

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
    },
    "required": ["weather", "confidence", "focus", "geo_resume",
                 "macro_resume", "sentiment_resume", "banks_resume",
                 "conflict"],
    "additionalProperties": False,
}

NEUTRAL_FALLBACK = {
    "weather": "NEUTRE", "confidence": 0.0,
    "focus": "indisponible (repli automatique)",
    "geo_resume": "indisponible", "macro_resume": "indisponible",
    "sentiment_resume": "indisponible", "banks_resume": "indisponible",
    "conflict": "indisponible",
}


# ----------------------------------------------------------------------------
# Etat, fenetres horaires et fichiers (fonctions pures, testables)
# ----------------------------------------------------------------------------
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
    """Sortie 1 : le fichier partage macro_weather.json (ecriture atomique)."""
    save_json_atomic(path or WEATHER_FILE, {
        "weather": verdict["weather"],
        "confidence": round(float(verdict["confidence"]), 2),
        "focus": verdict["focus"],
        "date": now.date().isoformat(),
        "generated_at": now.isoformat(),
    })


# ----------------------------------------------------------------------------
# Le conseil multi-agents (3 specialistes en parallele, puis le synthetiseur)
# ----------------------------------------------------------------------------
async def _analysis(llm: AsyncAnthropic, agent: dict, dossier: str) -> str:
    kwargs: dict = {"model": agent["model"], "max_tokens": MAX_TOKENS,
                    "system": agent["system"],
                    "messages": [{"role": "user", "content": dossier}]}
    if agent["model"] == MODEL_REASONING:   # haiku 4.5 : pas d'adaptatif
        kwargs["thinking"] = {"type": "adaptive"}
    resp = await llm.messages.create(**kwargs)
    return next((b.text for b in resp.content if b.type == "text"), "").strip()


async def run_council(llm: AsyncAnthropic, dossier: str) -> dict:
    """Trois analyses en parallele, puis le synthetiseur (JSON par schema).

    Toute erreur (API, reseau, refus) => meteo NEUTRE, sans lever.
    """
    try:
        analyses = await asyncio.gather(*(_analysis(llm, a, dossier)
                                          for a in AGENTS))
        council = dossier + "".join(
            f"\n\nANALYSE {a['nom'].upper()} :\n{txt}"
            for a, txt in zip(AGENTS, analyses))
        resp = await llm.messages.create(
            model=MODEL_REASONING, max_tokens=MAX_TOKENS,
            system=PROMPT_SYNTH, thinking={"type": "adaptive"},
            output_config={"format": {"type": "json_schema",
                                      "schema": SYNTH_SCHEMA}},
            messages=[{"role": "user", "content": council}])
        if resp.stop_reason == "refusal":
            raise RuntimeError("verdict refuse par le modele")
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


# ----------------------------------------------------------------------------
# Telegram (reutilise les credentials du projet, jamais commites)
# ----------------------------------------------------------------------------
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


# ----------------------------------------------------------------------------
# Cycle principal
# ----------------------------------------------------------------------------
async def collect_and_judge(llm: AsyncAnthropic, state: dict, now: datetime):
    """08:00 : sources -> conseil -> macro_weather.json + rapport en attente."""
    cfg = load_json(CONFIG_FILE)
    sources = await collect_all(
        extra_social=tuple(cfg.get("social_feeds") or ()),
        extra_bank=tuple(cfg.get("bank_feeds") or ()), now=now)
    dossier = build_dossier(sources, now)
    verdict = await run_council(llm, dossier)
    write_weather(verdict, now)
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
        await send_telegram(state["report"])
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
