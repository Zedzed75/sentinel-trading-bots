"""SENTINEL MACRO ANALYST - Data ingestion pipeline (bot 7).

Three source families, each tolerating its own failure ([] on error,
never a propagated exception):

A. Geopolitics & energy: BBC World / Al Jazeera / FT Commodities RSS
   (Reuters no longer publishes a public RSS: covered via Google News),
   with priority watch on energy chokepoints.
B. Influencers & social media: instead of fragile scrapers (Nitter,
   TruthSocial), Google News RSS feeds per public figure, filtered by
   keywords related to the fleet's assets; premium feeds (Twitter
   API/aggregators) can be plugged in via macro_config.json
   ("social_feeds": [RSS urls]).
C. Macro-economy: ForexFactory calendar (High impact, USD/EUR/GBP).

Stateless module without API keys: directly testable
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

# --- A. Geopolitics & energy --------------------------------------------------
GEO_FEEDS = (
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://www.aljazeera.com/xml/rss/all.xml",
    "https://www.ft.com/commodities?format=rss",
)
# Priority-watch keywords (flagged ⚠ URGENT in the dossier)
PRIORITY_KEYWORDS = ("strait of hormuz", "bab al-mandab", "bab el-mandeb",
                     "opec+", "opec", "red sea", "escalation", "blockade",
                     "sanctions")

# --- B. Influencers & social media --------------------------------------------
SOCIAL_FIGURES = ("Donald Trump", "Elon Musk", "US State Department",
                  "China foreign ministry", "Federal Reserve")
# Only messages touching our assets are kept
ASSET_KEYWORDS = ("tariff", "oil", "gold", "inflation", "fed", "china",
                  "iran", "saudi", "opec", "dollar", "rates")

# --- C. Macro-economy ---------------------------------------------------------
CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
MAJOR_CURRENCIES = ("USD", "EUR", "GBP")

# --- D. Bank desks & sell-side research ---------------------------------------
# Public feeds (FT Markets, FXStreet analyses) + Google News per bank;
# specialized feeds (eFX Data...) can be added via macro_config.json
# ("bank_feeds": [RSS urls]).
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
# Parsing and filtering (pure functions)
# ----------------------------------------------------------------------------
def parse_rss_titles(xml_text: str, limit: int = TITLES_PER_FEED) -> list[str]:
    """Titles of an RSS/Atom feed; [] if the XML is unreadable."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    titles = [(it.findtext("title") or "").strip() for it in root.iter("item")]
    if not titles:                                  # Atom: <entry><title>
        ns = "{http://www.w3.org/2005/Atom}"
        titles = [(e.findtext(f"{ns}title") or "").strip()
                  for e in root.iter(f"{ns}entry")]
    return [t for t in titles if t][:limit]


def flag_priority(titles: list[str]) -> list[str]:
    """Prefix ⚠ URGENT on titles containing a priority keyword
    (energy chokepoints, escalations, sanctions)."""
    out = []
    for t in titles:
        low = t.lower()
        hot = any(k in low for k in PRIORITY_KEYWORDS)
        out.append(("⚠ URGENT: " if hot else "") + t)
    # urgent ones first, relative order preserved
    return (sorted(out, key=lambda x: not x.startswith("⚠"))
            if any(x.startswith("⚠") for x in out) else out)


def filter_social(titles: list[str]) -> list[str]:
    """Keep only messages related to the fleet's assets (noise filter)."""
    return [t for t in titles
            if any(k in t.lower() for k in ASSET_KEYWORDS)]


def google_news_feed(figure: str, keywords: tuple | None = None) -> str:
    """Google News RSS feed for an entity x the target keywords."""
    assets = " OR ".join((keywords or ASSET_KEYWORDS)[:8])
    q = urllib.parse.quote(f'"{figure}" ({assets})')
    return (f"https://news.google.com/rss/search?q={q}"
            "&hl=en-US&gl=US&ceid=US:en")


def filter_bank(titles: list[str]) -> list[str]:
    """Keep only useful sell-side research: a named bank OR flow/levels
    vocabulary (targets, supports, positioning...)."""
    out = []
    for t in titles:
        low = t.lower()
        if (any(b in low for b in BANK_NAMES)
                or any(k in low for k in FLOW_KEYWORDS)):
            out.append(t)
    return out


def parse_calendar(events: list, now: datetime) -> list[str]:
    """Today's releases: High impact, major currencies, times in UTC."""
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
                       + (f" (forecast {e['forecast']})"
                          if e.get("forecast") else ""))
        except (KeyError, TypeError, ValueError):
            continue
    return out


# ----------------------------------------------------------------------------
# Asynchronous collection (one source failing does not affect the others)
# ----------------------------------------------------------------------------
async def _fetch_titles(client: httpx.AsyncClient, url: str) -> list[str]:
    try:
        resp = await client.get(url, timeout=FETCH_TIMEOUT,
                                follow_redirects=True)
        return parse_rss_titles(resp.text)
    except Exception as exc:
        log.warning("Feed unavailable (%s): %s", url, exc)
        return []


async def fetch_geopolitics(client: httpx.AsyncClient) -> list[str]:
    """Source A: geopolitics/energy titles, urgent ones first."""
    feeds = await asyncio.gather(*(_fetch_titles(client, u)
                                   for u in GEO_FEEDS))
    return flag_priority([t for feed in feeds for t in feed])


async def fetch_social(client: httpx.AsyncClient,
                       extra_feeds: tuple = ()) -> list[str]:
    """Source B: influencer statements filtered by assets.

    extra_feeds: optional premium RSS feeds (macro_config.json).
    """
    urls = [google_news_feed(f) for f in SOCIAL_FIGURES] + list(extra_feeds)
    feeds = await asyncio.gather(*(_fetch_titles(client, u) for u in urls))
    return filter_social([t for feed in feeds for t in feed])


async def fetch_bankdesk(client: httpx.AsyncClient,
                         extra_feeds: tuple = ()) -> list[str]:
    """Source D: bank-desk positioning and sell-side research."""
    banks = ("Goldman Sachs", "JPMorgan", "Morgan Stanley", "Citi")
    urls = (list(BANK_FEEDS)
            + [google_news_feed(b, ("gold", "eurusd", "oil", "forecast",
                                    "target")) for b in banks]
            + list(extra_feeds))
    feeds = await asyncio.gather(*(_fetch_titles(client, u) for u in urls))
    return filter_bank([t for feed in feeds for t in feed])


async def fetch_calendar(client: httpx.AsyncClient,
                         now: datetime) -> list[str]:
    """Source C: today's economic calendar."""
    try:
        resp = await client.get(CALENDAR_URL, timeout=FETCH_TIMEOUT)
        return parse_calendar(resp.json(), now)
    except Exception as exc:
        log.warning("Economic calendar unavailable: %s", exc)
        return []


async def collect_all(extra_social: tuple = (), extra_bank: tuple = (),
                      now: datetime | None = None) -> dict:
    """The four families in parallel: geo, social, calendar, banks."""
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
    ("calendar", "TODAY'S MAJOR MACRO RELEASES (High impact, USD/EUR/GBP)",
     "no major release"),
    ("geo", "GEOPOLITICS & ENERGY (⚠ = priority watch)",
     "feeds unavailable"),
    ("social", "INFLUENCER STATEMENTS & SOCIAL MEDIA (asset-filtered)",
     "no relevant statement"),
    ("banks", "BANK DESKS & SELL-SIDE RESEARCH (positioning, levels)",
     "no bank note"),
)


def build_dossier(sources: dict, now: datetime,
                  only: tuple | None = None) -> str:
    """Dossier handed to the agents; `only` restricts to the sections the
    agent needs (token economy: each specialist only receives its own
    sources, only the synthesizer sees everything)."""
    def block(title, lines, empty):
        return f"{title}:\n" + ("\n".join(
            ln if ln.startswith(("-", "⚠")) else f"- {ln}" for ln in lines)
            if lines else f"- {empty}")
    parts = [f"Date: {now:%A %Y-%m-%d} ({now:%H:%M} UTC)"]
    parts += [block(title, sources.get(key, []), empty)
              for key, title, empty in _BLOCKS
              if only is None or key in only]
    return "\n\n".join(parts)
