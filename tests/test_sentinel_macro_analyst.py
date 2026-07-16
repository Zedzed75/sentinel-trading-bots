"""Tests SENTINEL MACRO ANALYST v2 (LLM et reseau mockes).

Executer :  python -m unittest test_sentinel_macro_analyst -v
Compatibles pytest. Points couverts : repli NEUTRE sur echec d'API,
format du JSON partage, fenetres 08:00/08:30 UTC strictes, filtrage
anti-bruit des sources et conseil multi-agents (3 + synthetiseur).
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots"))
import sentinel_macro_analyst as ma  # noqa: E402
import sentinel_macro_sources as ms  # noqa: E402

UTC = timezone.utc
DAY = datetime(2026, 7, 16, 8, 30, tzinfo=UTC)

VERDICT = {"weather": "ORAGEUX", "confidence": 0.85,
           "focus": "CPI US a 14:30 UTC",
           "geo_resume": "prime de risque sur le Brent (mer Rouge)",
           "macro_resume": "CPI decisif pour la trajectoire de la Fed",
           "sentiment_resume": "declarations tarifaires agressives",
           "banks_resume": "JPMorgan biais acheteur XAUUSD vise 4200",
           "conflict": "geo hausse petrole vs desk GS capitulation : "
                       "tranche volatil"}


SOURCES = {"geo": ["titre geo"], "social": ["titre social"],
           "calendar": ["- 12:30 UTC [USD] CPI"], "banks": ["note GS"]}


def _llm_response(text, stop_reason="end_turn"):
    return SimpleNamespace(stop_reason=stop_reason,
                           content=[SimpleNamespace(type="text", text=text)])


def _llm(normal=None, beta=None):
    """Mock a deux chemins : messages.create (haiku/opus) et
    beta.messages.create (fable, fallback serveur)."""
    llm = mock.MagicMock()
    llm.messages.create = mock.AsyncMock(side_effect=normal)
    llm.beta.messages.create = mock.AsyncMock(side_effect=beta)
    return llm


class TestSendWindow(unittest.TestCase):
    """Respect strict des fenetres : ingestion 08:00, envoi 08:30, une fois."""

    def _at(self, h, m=0):
        return DAY.replace(hour=h, minute=m)

    def test_collect_from_8am_once_per_day(self):
        self.assertFalse(ma.should_collect({}, self._at(7, 59)))
        self.assertTrue(ma.should_collect({}, self._at(8, 0)))
        self.assertTrue(ma.should_collect({}, self._at(15, 0)))
        done = {"last_collect_day": "2026-07-16"}
        self.assertFalse(ma.should_collect(done, self._at(9, 0)))
        self.assertTrue(ma.should_collect(
            {"last_collect_day": "2026-07-15"}, self._at(8, 0)))

    def test_send_strictly_from_8h30_and_once(self):
        ready = {"report_day": "2026-07-16"}
        self.assertFalse(ma.should_send(ready, self._at(8, 29)))
        self.assertTrue(ma.should_send(ready, self._at(8, 30)))
        self.assertTrue(ma.should_send(ready, self._at(17, 0)))
        sent = dict(ready, last_send_day="2026-07-16")
        self.assertFalse(ma.should_send(sent, self._at(8, 31)))

    def test_no_send_without_todays_report(self):
        self.assertFalse(ma.should_send({}, self._at(8, 30)))
        stale = {"report_day": "2026-07-15"}      # rapport d'hier
        self.assertFalse(ma.should_send(stale, self._at(8, 30)))


class TestModelMapping(unittest.TestCase):
    """Decoupage fin des modeles + surcharge par configuration."""

    def test_default_mapping(self):
        self.assertEqual(ma.DEFAULT_MODELS, {
            "agent_geopolitics": "claude-fable-5",
            "agent_macro": "claude-fable-5",
            "agent_sentiment": "claude-haiku-4-5",
            "agent_flow_trader": "claude-haiku-4-5",
            "agent_juge_synthesizer": "claude-opus-4-8"})

    def test_config_overrides_one_agent(self):
        with mock.patch.object(ma, "load_json", return_value={
                "model_mapping": {"agent_macro": "claude-opus-4-8"}}):
            models = ma.agent_models()
        self.assertEqual(models["agent_macro"], "claude-opus-4-8")
        self.assertEqual(models["agent_geopolitics"], "claude-fable-5")

    def test_kwargs_per_model_family(self):
        fable = ma._llm_kwargs("claude-fable-5")
        self.assertNotIn("thinking", fable)          # thinking integre
        self.assertEqual(fable["fallbacks"], [{"model": "claude-opus-4-8"}])
        self.assertEqual(fable["output_config"], {"effort": "low"})
        self.assertIn("server-side-fallback-2026-06-01", fable["betas"])
        self.assertEqual(ma._llm_kwargs("claude-haiku-4-5"), {})
        opus = ma._llm_kwargs("claude-opus-4-8")
        self.assertEqual(opus["thinking"], {"type": "adaptive"})
        self.assertEqual(opus["output_config"], {"effort": "medium"})


class TestCouncil(unittest.IsolatedAsyncioTestCase):
    """Le conseil : 4 agents specialises en parallele + 1 synthetiseur."""

    async def test_four_agents_then_synth_routed_by_model(self):
        llm = _llm(normal=[_llm_response("analyse sentiment"),
                           _llm_response("analyse flux"),
                           _llm_response(json.dumps(VERDICT))],
                   beta=[_llm_response("analyse geo"),
                         _llm_response("analyse eco")])
        verdict = await ma.run_council(llm, SOURCES, DAY)
        self.assertEqual(verdict["weather"], "ORAGEUX")
        self.assertEqual(verdict["confidence"], 0.85)
        self.assertEqual(llm.beta.messages.create.await_count, 2)  # fable
        self.assertEqual(llm.messages.create.await_count, 3)  # haiku+juge
        beta_models = [c.kwargs["model"]
                       for c in llm.beta.messages.create.await_args_list]
        self.assertEqual(beta_models, ["claude-fable-5"] * 2)
        normal = [c.kwargs["model"]
                  for c in llm.messages.create.await_args_list]
        self.assertEqual(normal, ["claude-haiku-4-5", "claude-haiku-4-5",
                                  "claude-opus-4-8"])
        self.assertTrue(all(c.kwargs["max_tokens"] == ma.MAX_TOKENS
                            for c in llm.messages.create.await_args_list))

    async def test_sectorized_dossiers_save_tokens(self):
        llm = _llm(normal=[_llm_response("s"), _llm_response("f"),
                           _llm_response(json.dumps(VERDICT))],
                   beta=[_llm_response("g"), _llm_response("m")])
        await ma.run_council(llm, SOURCES, DAY)
        sentiment_msg = (llm.messages.create.await_args_list[0]
                         .kwargs["messages"][0]["content"])
        self.assertIn("RESEAUX SOCIAUX", sentiment_msg)
        self.assertNotIn("GEOPOLITIQUE", sentiment_msg)  # section exclue
        self.assertNotIn("BANK DESKS", sentiment_msg)
        judge_msg = (llm.messages.create.await_args_list[2]
                     .kwargs["messages"][0]["content"])
        self.assertIn("GEOPOLITIQUE", judge_msg)     # le juge voit tout
        self.assertIn("BANK DESKS", judge_msg)

    async def test_api_failure_falls_back_to_neutral(self):
        llm = _llm(normal=RuntimeError("API indisponible"),
                   beta=RuntimeError("API indisponible"))
        verdict = await ma.run_council(llm, SOURCES, DAY)
        self.assertEqual(verdict["weather"], "NEUTRE")
        self.assertEqual(verdict["confidence"], 0.0)

    async def test_refusal_falls_back_to_neutral(self):
        llm = _llm(normal=[_llm_response("s"), _llm_response("f"),
                           _llm_response("", stop_reason="refusal")],
                   beta=[_llm_response("g"), _llm_response("m")])
        self.assertEqual((await ma.run_council(llm, SOURCES, DAY))["weather"],
                         "NEUTRE")

    async def test_confidence_clamped(self):
        llm = _llm(normal=[_llm_response("s"), _llm_response("f"),
                           _llm_response(json.dumps(dict(VERDICT,
                                                         confidence=7.5)))],
                   beta=[_llm_response("g"), _llm_response("m")])
        self.assertEqual((await ma.run_council(llm, SOURCES,
                                               DAY))["confidence"], 1.0)


class TestWeatherFile(unittest.TestCase):
    def test_json_format_and_atomic_write(self):
        path = os.path.join(tempfile.mkdtemp(), "macro_weather.json")
        ma.write_weather(VERDICT, DAY, path)
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["weather"], "ORAGEUX")
        self.assertEqual(data["confidence"], 0.85)
        self.assertEqual(data["date"], "2026-07-16")
        self.assertFalse(os.path.exists(path + ".tmp"))

    def test_fallback_written_when_pipeline_fails(self):
        tmp = tempfile.mkdtemp()
        llm = _llm(RuntimeError("panne totale"))

        async def scenario():
            with mock.patch.object(ma, "WEATHER_FILE",
                                   os.path.join(tmp, "w.json")), \
                 mock.patch.object(ma, "STATE_FILE",
                                   os.path.join(tmp, "s.json")), \
                 mock.patch.object(ma, "collect_all", mock.AsyncMock(
                     return_value={"geo": [], "social": [], "calendar": [],
                                   "banks": []})):
                await ma.collect_and_judge(llm, {}, DAY)
                with open(ma.WEATHER_FILE, encoding="utf-8") as fh:
                    return json.load(fh)

        data = asyncio.run(scenario())          # ne doit jamais lever
        self.assertEqual(data["weather"], "NEUTRE")
        self.assertEqual(data["confidence"], 0.0)


class TestSources(unittest.TestCase):
    """Pipeline d'ingestion : parsing, filtrage anti-bruit, priorites."""

    def test_calendar_filters_today_high_impact_majors(self):
        events = [
            {"title": "CPI y/y", "country": "USD", "impact": "High",
             "date": "2026-07-16T14:30:00+02:00", "forecast": "3.1%"},
            {"title": "CPI demain", "country": "USD", "impact": "High",
             "date": "2026-07-17T14:30:00+02:00"},
            {"title": "PMI mineur", "country": "USD", "impact": "Low",
             "date": "2026-07-16T10:00:00+02:00"},
            {"title": "taux BoJ", "country": "JPY", "impact": "High",
             "date": "2026-07-16T04:00:00+02:00"},
            {"pas": "de champs"},
        ]
        lines = ms.parse_calendar(events, DAY)
        self.assertEqual(len(lines), 1)
        self.assertIn("12:30 UTC [USD] CPI y/y (prevision 3.1%)", lines[0])

    def test_rss_and_atom_parsed_corrupt_ignored(self):
        rss = ("<rss><channel><item><title>Gold hits record</title></item>"
               "<item><title>Oil slides</title></item></channel></rss>")
        atom = ('<feed xmlns="http://www.w3.org/2005/Atom">'
                "<entry><title>Brent up</title></entry></feed>")
        self.assertEqual(ms.parse_rss_titles(rss),
                         ["Gold hits record", "Oil slides"])
        self.assertEqual(ms.parse_rss_titles(atom), ["Brent up"])
        self.assertEqual(ms.parse_rss_titles("<pas du xml"), [])

    def test_priority_keywords_flagged_and_first(self):
        titles = ["Weather sunny in Paris",
                  "Tankers rerouted from Strait of Hormuz",
                  "OPEC+ weighs output cut"]
        flagged = ms.flag_priority(titles)
        self.assertTrue(flagged[0].startswith("⚠ URGENT"))
        self.assertTrue(flagged[1].startswith("⚠ URGENT"))
        self.assertEqual(flagged[2], "Weather sunny in Paris")

    def test_social_filter_keeps_only_asset_related(self):
        titles = ["Trump announces new tariffs on China imports",
                  "Musk posts meme about cats",
                  "Fed rates decision looms",
                  "Celebrity gossip of the day"]
        kept = ms.filter_social(titles)
        self.assertEqual(len(kept), 2)
        self.assertIn("Trump announces new tariffs on China imports", kept)
        self.assertIn("Fed rates decision looms", kept)

    def test_google_news_feed_url(self):
        url = ms.google_news_feed("Donald Trump")
        self.assertIn("news.google.com/rss/search", url)
        self.assertIn("%22Donald%20Trump%22", url)
        self.assertIn("tariff", url)

    def test_bank_filter_keeps_named_banks_and_flow_vocab(self):
        titles = ["Goldman Sachs sees Brent at 95 by December",
                  "Recipe of the week: pasta",
                  "Key resistance holds for EURUSD bulls",
                  "Local news roundup"]
        kept = ms.filter_bank(titles)
        self.assertEqual(len(kept), 2)
        self.assertIn("Goldman Sachs sees Brent at 95 by December", kept)
        self.assertIn("Key resistance holds for EURUSD bulls", kept)

    def test_dossier_sections_and_missing_sources(self):
        d = ms.build_dossier({"geo": [], "social": [], "calendar": [],
                              "banks": []}, DAY)
        self.assertIn("ANNONCES MACRO MAJEURES", d)
        self.assertIn("GEOPOLITIQUE & ENERGIE", d)
        self.assertIn("RESEAUX SOCIAUX", d)
        self.assertIn("BANK DESKS & RECHERCHE SELL-SIDE", d)
        self.assertIn("aucune annonce majeure", d)
        self.assertIn("flux indisponibles", d)
        self.assertIn("aucune declaration pertinente", d)
        self.assertIn("aucune note bancaire", d)


class TestReport(unittest.TestCase):
    def test_report_structure(self):
        report = ma.format_report(VERDICT, DAY)
        self.assertIn("[SENTINEL BOT 7]", report)
        self.assertIn("(16/07/2026)", report)
        self.assertIn("ORAGEUX (Confiance : 85%)", report)
        self.assertIn("CPI US a 14:30 UTC", report)
        self.assertIn("LE CONSEIL", report)
        self.assertIn("Geopolitique :", report)
        self.assertIn("Macro :", report)
        self.assertIn("Sentiment :", report)
        self.assertIn("ANALYSTES & BANK DESKS (Agent 4)", report)
        self.assertIn("JPMorgan biais acheteur XAUUSD vise 4200", report)
        self.assertIn("CONFLIT D'INTERET DU JOUR", report)
        self.assertIn("PRECONISATION FLOTTE", report)
        self.assertIn("Priorite haute", report)      # ORAGEUX -> bots 1&3

    def test_calm_weather_favors_statarb(self):
        report = ma.format_report(dict(VERDICT, weather="CALME"), DAY)
        self.assertIn("faux breakouts probables", report)
        self.assertIn("Conditions favorables", report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
