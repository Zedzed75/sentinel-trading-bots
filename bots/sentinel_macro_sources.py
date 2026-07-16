"""SENTINEL MACRO ANALYST - Pipeline d'ingestion des donnees (bot 7).

Trois familles de sources, chacune tolerant sa propre panne ([] en cas
d'echec, jamais d'exception propagee) :

A. Geopolitique & energie : RSS BBC World / Al Jazeera / FT Commodities
   (Reuters ne publie plus de RSS public : couvert via Google News),
   avec surveillance prioritaire des goulots d'etranglement energetiques.
B. Influenceurs & reseaux sociaux : plutot que des scrapers fragiles
   (Nitter, TruthSocial), des flux Google News RSS par personnalite,
   filtres par mots-cles lies aux actifs de la flotte ; des flux premium
   (API Twitter/agregateurs) peuvent etre branches via macro_config.json
   ("social_feeds": [urls RSS]).
C. Macro-economie : calendrier ForexFactory (impact High, USD/EUR/GBP).

Module sans etat et sans cle API : testable directement
(tests/test_sentinel_macro_analyst.py).
"""

import asyncio
import logging
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import httpx

log = logging.getLogger("macro")

FETCH_TIMEOUT = 20.0
TITLES_PER_FEED = 8

# --- A. Geopolitique & energie ----------------------------------------------
GEO_FEEDS = (
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://www.aljazeera.com/xml/rss/all.xml",
    "https://www.ft.com/commodities?format=rss",
)
# Mots-cles de surveillance prioritaire (marques ⚠ URGENT dans le dossier)
PRIORITY_KEYWORDS = ("strait of hormuz", "bab al-mandab", "bab el-mandeb",
                     "opec+", "opec", "red sea", "escalation", "blockade",
                     "sanctions")

# --- B. Influenceurs & reseaux sociaux ---------------------------------------
SOCIAL_FIGURES = ("Donald Trump", "Elon Musk", "US State Department",
                  "China foreign ministry", "Federal Reserve")
# Seuls les messages touchant nos actifs sont conserves
ASSET_KEYWORDS = ("tariff", "oil", "gold", "inflation", "fed", "china",
                  "iran", "saudi", "opec", "dollar", "rates")

# --- C. Macro-economie --------------------------------------------------------
CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
MAJOR_CURRENCIES = ("USD", "EUR", "GBP")

# --- D. Bank desks & recherche sell-side --------------------------------------
# Flux publics (FT Markets, analyses FXStreet) + Google News par banque ;
# des flux specialises (eFX Data...) peuvent s'ajouter via macro_config.json
# ("bank_feeds": [urls RSS]).
BANK_FEEDS = (
    "https://www.ft.com/markets?format=rss",
    "https://www.fxstreet.com/rss/analysis",
)
BANK_NAMES = ("goldman sachs", "jpmorgan", "jp morgan", "morgan stanley",
              "citi", "ubs", "deutsche bank", "saxo")
FLOW_KEYWORDS = ("target", "forecast", "support", "resistance", "positioning",
                 "order flow", "liquidity", "stop", "long", "short",
                 "objectif", "gold", "xau", "eurusd", "brent", "wti")


# ----------------------------------------------------------------------------
# Parsing et filtrage (fonctions pures)
# ----------------------------------------------------------------------------
def parse_rss_titles(xml_text: str, limit: int = TITLES_PER_FEED) -> list[str]:
    """Titres d'un flux RSS/Atom ; [] si le XML est illisible."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    titles = [(it.findtext("title") or "").strip() for it in root.iter("item")]
    if not titles:                                  # Atom : <entry><title>
        ns = "{http://www.w3.org/2005/Atom}"
        titles = [(e.findtext(f"{ns}title") or "").strip()
                  for e in root.iter(f"{ns}entry")]
    return [t for t in titles if t][:limit]


def flag_priority(titles: list[str]) -> list[str]:
    """Prefixe ⚠ URGENT les titres contenant un mot-cle prioritaire
    (goulots d'etranglement energetiques, escalades, sanctions)."""
    out = []
    for t in titles:
        low = t.lower()
        hot = any(k in low for k in PRIORITY_KEYWORDS)
        out.append(("⚠ URGENT : " if hot else "") + t)
    # les urgents d'abord, l'ordre relatif est conserve
    return (sorted(out, key=lambda x: not x.startswith("⚠"))
            if any(x.startswith("⚠") for x in out) else out)


def filter_social(titles: list[str]) -> list[str]:
    """Ne garde que les messages lies aux actifs de la flotte (anti-bruit)."""
    return [t for t in titles
            if any(k in t.lower() for k in ASSET_KEYWORDS)]


def google_news_feed(figure: str, keywords: tuple | None = None) -> str:
    """Flux Google News RSS pour une entite x des mots-cles cibles."""
    assets = " OR ".join((keywords or ASSET_KEYWORDS)[:8])
    q = urllib.parse.quote(f'"{figure}" ({assets})')
    return (f"https://news.google.com/rss/search?q={q}"
            "&hl=en-US&gl=US&ceid=US:en")


def filter_bank(titles: list[str]) -> list[str]:
    """Ne garde que la recherche sell-side utile : une banque nommee OU un
    vocabulaire de flux/niveaux (cibles, supports, positionnement...)."""
    out = []
    for t in titles:
        low = t.lower()
        if (any(b in low for b in BANK_NAMES)
                or any(k in low for k in FLOW_KEYWORDS)):
            out.append(t)
    return out


def parse_calendar(events: list, now: datetime) -> list[str]:
    """Annonces du jour : impact High, devises majeures, heures en UTC."""
    out = []
    for e in events or []:
        try:
            if (e.get("impact") != "High"
                    or e.get("country") not in MAJOR_CURRENCIES):
                continue
            date = datetime.fromisoformat(e["date"]).astimezone(timezone.utc)
            if date.date() != now.date():
                continue
            out.append(f"- {date:%H:%M} UTC [{e['country']}] {e['title']}"
                       + (f" (prevision {e['forecast']})"
                          if e.get("forecast") else ""))
        except (KeyError, TypeError, ValueError):
            continue
    return out


# ----------------------------------------------------------------------------
# Collecte asynchrone (une panne de source n'affecte pas les autres)
# ----------------------------------------------------------------------------
async def _fetch_titles(client: httpx.AsyncClient, url: str) -> list[str]:
    try:
        resp = await client.get(url, timeout=FETCH_TIMEOUT,
                                follow_redirects=True)
        return parse_rss_titles(resp.text)
    except Exception as exc:
        log.warning("Flux indisponible (%s) : %s", url, exc)
        return []


async def fetch_geopolitics(client: httpx.AsyncClient) -> list[str]:
    """Source A : titres geopolitique/energie, urgences en tete."""
    feeds = await asyncio.gather(*(_fetch_titles(client, u)
                                   for u in GEO_FEEDS))
    return flag_priority([t for feed in feeds for t in feed])


async def fetch_social(client: httpx.AsyncClient,
                       extra_feeds: tuple = ()) -> list[str]:
    """Source B : declarations d'influenceurs filtrees par actifs.

    extra_feeds : flux RSS premium optionnels (macro_config.json).
    """
    urls = [google_news_feed(f) for f in SOCIAL_FIGURES] + list(extra_feeds)
    feeds = await asyncio.gather(*(_fetch_titles(client, u) for u in urls))
    return filter_social([t for feed in feeds for t in feed])


async def fetch_bankdesk(client: httpx.AsyncClient,
                         extra_feeds: tuple = ()) -> list[str]:
    """Source D : positionnement des bank desks et recherche sell-side."""
    banks = ("Goldman Sachs", "JPMorgan", "Morgan Stanley", "Citi")
    urls = (list(BANK_FEEDS)
            + [google_news_feed(b, ("gold", "eurusd", "oil", "forecast",
                                    "target")) for b in banks]
            + list(extra_feeds))
    feeds = await asyncio.gather(*(_fetch_titles(client, u) for u in urls))
    return filter_bank([t for feed in feeds for t in feed])


async def fetch_calendar(client: httpx.AsyncClient,
                         now: datetime) -> list[str]:
    """Source C : calendrier economique du jour."""
    try:
        resp = await client.get(CALENDAR_URL, timeout=FETCH_TIMEOUT)
        return parse_calendar(resp.json(), now)
    except Exception as exc:
        log.warning("Calendrier economique indisponible : %s", exc)
        return []


async def collect_all(extra_social: tuple = (), extra_bank: tuple = (),
                      now: datetime | None = None) -> dict:
    """Les quatre familles en parallele : geo, social, calendar, banks."""
    now = now or datetime.now(timezone.utc)
    async with httpx.AsyncClient() as client:
        geo, social, calendar, banks = await asyncio.gather(
            fetch_geopolitics(client),
            fetch_social(client, extra_social),
            fetch_calendar(client, now),
            fetch_bankdesk(client, extra_bank))
    return {"geo": geo, "social": social, "calendar": calendar,
            "banks": banks}


_BLOCKS = (
    ("calendar", "ANNONCES MACRO MAJEURES DU JOUR (impact High, USD/EUR/GBP)",
     "aucune annonce majeure"),
    ("geo", "GEOPOLITIQUE & ENERGIE (⚠ = surveillance prioritaire)",
     "flux indisponibles"),
    ("social", "DECLARATIONS D'INFLUENCEURS & RESEAUX SOCIAUX (filtrees actifs)",
     "aucune declaration pertinente"),
    ("banks", "BANK DESKS & RECHERCHE SELL-SIDE (positionnement, niveaux)",
     "aucune note bancaire"),
)


def build_dossier(sources: dict, now: datetime,
                  only: tuple | None = None) -> str:
    """Dossier remis aux agents ; `only` restreint aux sections utiles a
    l'agent (economie de tokens : chaque specialiste ne recoit que ses
    sources, seul le synthetiseur voit tout)."""
    def block(title, lines, empty):
        return f"{title} :\n" + ("\n".join(
            ln if ln.startswith(("-", "⚠")) else f"- {ln}" for ln in lines)
            if lines else f"- {empty}")
    parts = [f"Date : {now:%A %d/%m/%Y} ({now:%H:%M} UTC)"]
    parts += [block(title, sources.get(key, []), empty)
              for key, title, empty in _BLOCKS
              if only is None or key in only]
    return "\n\n".join(parts)
