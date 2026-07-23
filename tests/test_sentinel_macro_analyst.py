"""SENTINEL MACRO ANALYST v2 tests (LLM and network mocked).

Run:  python -m unittest test_sentinel_macro_analyst -v
pytest-compatible. Covered: NEUTRAL fallback on API failure, shared
JSON format, strict 08:00/08:30 UTC windows, source noise filtering
and the multi-agent council (4 + synthesizer).
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

VERDICT = {"weather": "STORMY", "confidence": 0.85,
           "focus": "US CPI at 14:30 UTC",
           "geo_summary": "risk premium on Brent (Red Sea)",
           "macro_summary": "CPI decisive for the Fed's trajectory",
           "sentiment_summary": "aggressive tariff statements",
           "banks_summary": "JPMorgan bullish bias XAUUSD targets 4200",
           "conflict": "geo oil upside vs GS desk capitulation: "
                       "settled as volatile",
           "primary_asset": "XTIUSD"}


SOURCES = {"geo": ["geo title"], "social": ["social title"],
           "calendar": ["- 12:30 UTC [USD] CPI"], "banks": ["GS note"]}


def _llm_response(text, stop_reason="end_turn"):
    return SimpleNamespace(stop_reason=stop_reason,
                           content=[SimpleNamespace(type="text", text=text)])


def _llm(normal=None, beta=None):
    """Two-path mock: messages.create (haiku/opus) and
    beta.messages.create (fable, server-side fallback)."""
    llm = mock.MagicMock()
    llm.messages.create = mock.AsyncMock(side_effect=normal)
    llm.beta.messages.create = mock.AsyncMock(side_effect=beta)
    return llm


class TestSendWindow(unittest.TestCase):
    """Strict windows: ingestion 08:00, send 08:30, once per day."""

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
        stale = {"report_day": "2026-07-15"}      # yesterday's report
        self.assertFalse(ma.should_send(stale, self._at(8, 30)))


class TestModelMapping(unittest.TestCase):
    """Fine-grained model mapping + configuration override."""

    def test_default_mapping(self):
        self.assertEqual(ma.DEFAULT_MODELS, {
            "agent_geopolitics": "claude-fable-5",
            "agent_macro": "claude-fable-5",
            "agent_sentiment": "claude-haiku-4-5",
            "agent_flow_trader": "claude-haiku-4-5",
            "agent_juge_synthesizer": "claude-opus-4-8",
            "agent_triage": "claude-haiku-4-5",
            "agent_analyst": "claude-opus-4-8"})

    def test_config_overrides_one_agent(self):
        with mock.patch.object(ma, "load_json", return_value={
                "model_mapping": {"agent_macro": "claude-opus-4-8"}}):
            models = ma.agent_models()
        self.assertEqual(models["agent_macro"], "claude-opus-4-8")
        self.assertEqual(models["agent_geopolitics"], "claude-fable-5")

    def test_kwargs_per_model_family(self):
        fable = ma._llm_kwargs("claude-fable-5")
        self.assertNotIn("thinking", fable)          # built-in thinking
        self.assertEqual(fable["fallbacks"], [{"model": "claude-opus-4-8"}])
        self.assertEqual(fable["output_config"], {"effort": "low"})
        self.assertIn("server-side-fallback-2026-06-01", fable["betas"])
        self.assertEqual(ma._llm_kwargs("claude-haiku-4-5"), {})
        opus = ma._llm_kwargs("claude-opus-4-8")
        self.assertEqual(opus["thinking"], {"type": "adaptive"})
        self.assertEqual(opus["output_config"], {"effort": "medium"})


class TestCouncil(unittest.IsolatedAsyncioTestCase):
    """The council: 4 specialized agents in parallel + 1 synthesizer."""

    async def test_four_agents_then_synth_routed_by_model(self):
        llm = _llm(normal=[_llm_response("sentiment analysis"),
                           _llm_response("flow analysis"),
                           _llm_response(json.dumps(VERDICT))],
                   beta=[_llm_response("geo analysis"),
                         _llm_response("eco analysis")])
        verdict = await ma.run_council(llm, SOURCES, DAY)
        self.assertEqual(verdict["weather"], "STORMY")
        self.assertEqual(verdict["confidence"], 0.85)
        self.assertEqual(llm.beta.messages.create.await_count, 2)  # fable
        self.assertEqual(llm.messages.create.await_count, 3)  # haiku+judge
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
        self.assertIn("SOCIAL MEDIA", sentiment_msg)
        self.assertNotIn("GEOPOLITICS", sentiment_msg)  # section excluded
        self.assertNotIn("BANK DESKS", sentiment_msg)
        judge_msg = (llm.messages.create.await_args_list[2]
                     .kwargs["messages"][0]["content"])
        self.assertIn("GEOPOLITICS", judge_msg)     # the judge sees everything
        self.assertIn("BANK DESKS", judge_msg)

    async def test_api_failure_falls_back_to_neutral(self):
        llm = _llm(normal=RuntimeError("API unavailable"),
                   beta=RuntimeError("API unavailable"))
        verdict = await ma.run_council(llm, SOURCES, DAY)
        self.assertEqual(verdict["weather"], "NEUTRAL")
        self.assertEqual(verdict["confidence"], 0.0)

    async def test_refusal_falls_back_to_neutral(self):
        llm = _llm(normal=[_llm_response("s"), _llm_response("f"),
                           _llm_response("", stop_reason="refusal")],
                   beta=[_llm_response("g"), _llm_response("m")])
        self.assertEqual((await ma.run_council(llm, SOURCES, DAY))["weather"],
                         "NEUTRAL")

    async def test_confidence_clamped(self):
        llm = _llm(normal=[_llm_response("s"), _llm_response("f"),
                           _llm_response(json.dumps(dict(VERDICT,
                                                         confidence=7.5)))],
                   beta=[_llm_response("g"), _llm_response("m")])
        self.assertEqual((await ma.run_council(llm, SOURCES,
                                               DAY))["confidence"], 1.0)


SIGNAL = {"asset_affected": "XAUUSD", "macro_bias": "BEARISH",
          "confidence_score": 85,
          "rationale": "Hawkish Fed tone pressures gold.",
          "action_for_mt5": "BLOCK_BUY_SIGNALS"}


class TestSignalPipeline(unittest.IsolatedAsyncioTestCase):
    """v2 layers: Haiku batch triage -> Opus strict-JSON analyst."""

    def setUp(self):
        import sentinel_macro_signals as msig
        self.msig = msig
        tmp = tempfile.mkdtemp()
        self.patches = [
            mock.patch.object(msig, "SIGNAL_FILE",
                              os.path.join(tmp, "macro_signal.json")),
            mock.patch.object(msig, "SIGNALS_DB",
                              os.path.join(tmp, "arbitrage.db")),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def _signal_file(self):
        with open(self.msig.SIGNAL_FILE, encoding="utf-8") as fh:
            return json.load(fh)

    async def test_triage_then_analyst_with_strict_schemas(self):
        triage = {"scores": [{"index": 0, "score": 9},
                             {"index": 1, "score": 3}]}
        llm = _llm(normal=[_llm_response(json.dumps(triage)),
                           _llm_response(json.dumps(SIGNAL))])
        signal = await ma.run_signal_pipeline(
            llm, dict(SOURCES, geo=["Fed shock", "noise item"]), DAY)
        self.assertEqual(signal["action_for_mt5"], "BLOCK_BUY_SIGNALS")
        calls = llm.messages.create.await_args_list
        self.assertEqual(calls[0].kwargs["model"], "claude-haiku-4-5")
        self.assertEqual(calls[0].kwargs["output_config"]["format"]
                         ["schema"], self.msig.TRIAGE_SCHEMA)
        self.assertEqual(calls[1].kwargs["model"], "claude-opus-4-8")
        self.assertEqual(calls[1].kwargs["output_config"]["format"]
                         ["schema"], self.msig.SIGNAL_SCHEMA)
        self.assertIn("Fed shock", calls[1].kwargs["messages"][0]["content"])
        self.assertNotIn("noise item",
                         calls[1].kwargs["messages"][0]["content"])
        self.assertEqual(self._signal_file()["asset_affected"], "XAUUSD")

    async def test_analyst_gets_web_search_but_not_triage(self):
        # Native web_search (GA _20260209) on the analyst only, capped by
        # max_uses; no code_execution declared alongside (the _20260209
        # variant embeds its own environment); triage stays tool-free.
        triage = {"scores": [{"index": 0, "score": 9}]}
        llm = _llm(normal=[_llm_response(json.dumps(triage)),
                           _llm_response(json.dumps(SIGNAL))])
        await ma.run_signal_pipeline(llm, SOURCES, DAY)
        calls = llm.messages.create.await_args_list
        self.assertNotIn("tools", calls[0].kwargs)
        tools = calls[1].kwargs["tools"]
        self.assertEqual([t["type"] for t in tools], ["web_search_20260209"])
        self.assertEqual(tools[0]["name"], "web_search")
        self.assertLessEqual(tools[0]["max_uses"], 5)

    async def test_analyst_json_read_from_last_text_block(self):
        # With web_search enabled, search result blocks and interim text
        # can precede the final JSON: the parser must take the LAST text
        # block, not the first.
        triage = {"scores": [{"index": 0, "score": 9}]}
        analyst = _llm_response(json.dumps(SIGNAL))
        analyst.content = [
            mock.Mock(type="server_tool_use"),
            mock.Mock(type="web_search_tool_result"),
            mock.Mock(type="text", text="Searching for context..."),
            mock.Mock(type="text", text=json.dumps(SIGNAL)),
        ]
        llm = _llm(normal=[_llm_response(json.dumps(triage)), analyst])
        signal = await ma.run_signal_pipeline(llm, SOURCES, DAY)
        self.assertEqual(signal["action_for_mt5"], "BLOCK_BUY_SIGNALS")

    async def test_low_scores_save_the_heavy_model(self):
        triage = {"scores": [{"index": 0, "score": 5}]}
        llm = _llm(normal=[_llm_response(json.dumps(triage))])
        signal = await ma.run_signal_pipeline(llm, SOURCES, DAY)
        self.assertEqual(signal, self.msig.NO_SIGNAL)
        self.assertEqual(llm.messages.create.await_count, 1)  # triage only
        self.assertEqual(self._signal_file()["action_for_mt5"], "NONE")

    async def test_empty_sources_zero_llm_calls(self):
        llm = _llm(normal=[])
        signal = await ma.run_signal_pipeline(
            llm, {"geo": [], "social": [], "banks": [], "calendar": []},
            DAY)
        self.assertEqual(signal, self.msig.NO_SIGNAL)
        llm.messages.create.assert_not_awaited()

    async def test_failure_falls_back_to_no_signal(self):
        llm = _llm(normal=RuntimeError("API unavailable"))
        signal = await ma.run_signal_pipeline(llm, SOURCES, DAY)
        self.assertEqual(signal, self.msig.NO_SIGNAL)
        self.assertEqual(self._signal_file()["confidence_score"], 0)

    async def test_confidence_clamped(self):
        triage = {"scores": [{"index": 0, "score": 9}]}
        llm = _llm(normal=[_llm_response(json.dumps(triage)),
                           _llm_response(json.dumps(
                               dict(SIGNAL, confidence_score=150)))])
        signal = await ma.run_signal_pipeline(llm, SOURCES, DAY)
        self.assertEqual(signal["confidence_score"], 100)


class TestWeatherFile(unittest.TestCase):
    def test_json_format_and_atomic_write(self):
        path = os.path.join(tempfile.mkdtemp(), "macro_weather.json")
        ma.write_weather(VERDICT, DAY, path)
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["weather"], "STORMY")
        self.assertEqual(data["confidence"], 0.85)
        self.assertEqual(data["date"], "2026-07-16")
        self.assertFalse(os.path.exists(path + ".tmp"))

    def test_fallback_written_when_pipeline_fails(self):
        tmp = tempfile.mkdtemp()
        llm = _llm(RuntimeError("total outage"))

        async def scenario():
            with mock.patch.object(ma, "WEATHER_FILE",
                                   os.path.join(tmp, "w.json")), \
                 mock.patch.object(ma, "STATE_FILE",
                                   os.path.join(tmp, "s.json")), \
                 mock.patch.object(ma, "HISTORY_FILE",
                                   os.path.join(tmp, "h.json")), \
                 mock.patch.object(ma.msig, "SIGNAL_FILE",
                                   os.path.join(tmp, "sig.json")), \
                 mock.patch.object(ma.msig, "SIGNALS_DB",
                                   os.path.join(tmp, "arb.db")), \
                 mock.patch.object(ma, "collect_all", mock.AsyncMock(
                     return_value={"geo": [], "social": [], "calendar": [],
                                   "banks": []})):
                await ma.collect_and_judge(llm, {}, DAY)
                with open(ma.WEATHER_FILE, encoding="utf-8") as fh:
                    return json.load(fh)

        data = asyncio.run(scenario())          # must never raise
        self.assertEqual(data["weather"], "NEUTRAL")
        self.assertEqual(data["confidence"], 0.0)


class TestHistory(unittest.TestCase):
    """macro_history.json archive: creation, upsert of the day, sort, I/O."""

    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "macro_history.json")

    def _read(self):
        with open(self.path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_created_with_compact_entry(self):
        ma.append_history(VERDICT, DAY, self.path)   # missing file: created
        hist = self._read()
        self.assertEqual(hist, [{"date": "2026-07-16",
                                 "weather": "STORMY", "confidence": 0.85,
                                 "focus": "US CPI at 14:30 UTC",
                                 "primary_asset": "XTIUSD"}])

    def test_same_day_rerun_updates_without_duplicate(self):
        ma.append_history(VERDICT, DAY, self.path)
        ma.append_history(dict(VERDICT, weather="CALM", confidence=0.5),
                          DAY, self.path)            # rerun the same day
        hist = self._read()
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["weather"], "CALM")

    def test_sorted_by_date(self):
        ma.append_history(VERDICT, DAY, self.path)
        ma.append_history(dict(VERDICT, weather="CALM"),
                          DAY.replace(day=14), self.path)  # earlier day
        self.assertEqual([e["date"] for e in self._read()],
                         ["2026-07-14", "2026-07-16"])

    def test_legacy_french_values_migrated(self):
        # archive written before the English migration: values translated
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump([{"date": "2026-07-10", "weather": "ORAGEUX",
                        "confidence": 0.7, "focus": "CPI",
                        "primary_asset": "AUCUN"},
                       {"date": "2026-07-11", "weather": "CALME",
                        "confidence": 0.6, "focus": "PMI",
                        "primary_asset": "XAUUSD"}], fh)
        ma.append_history(VERDICT, DAY, self.path)
        hist = self._read()
        self.assertEqual([e["weather"] for e in hist],
                         ["STORMY", "CALM", "STORMY"])
        self.assertEqual(hist[0]["primary_asset"], "NONE")
        self.assertEqual(hist[1]["primary_asset"], "XAUUSD")

    def test_io_error_never_raises(self):
        # invalid path (a directory): logs and continues, never raises
        ma.append_history(VERDICT, DAY, tempfile.mkdtemp())

    def test_corrupt_history_recreated(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{not an array")
        ma.append_history(VERDICT, DAY, self.path)
        self.assertEqual(len(self._read()), 1)

    def test_primary_asset_constrained_to_fleet(self):
        enum = ma.SYNTH_SCHEMA["properties"]["primary_asset"]["enum"]
        self.assertIn("XAUUSD", enum)
        self.assertIn("NONE", enum)
        self.assertIn("primary_asset", ma.SYNTH_SCHEMA["required"])
        self.assertEqual(ma.NEUTRAL_FALLBACK["primary_asset"], "NONE")


class TestSafePrint(unittest.TestCase):
    """Regression for issue #23: --once must not crash on cp1252 consoles."""

    def _cp1252_stdout(self):
        import io
        return io.TextIOWrapper(io.BytesIO(), encoding="cp1252",
                                errors="strict")

    def test_plain_print_would_crash_but_safe_print_does_not(self):
        report = ma.format_report(VERDICT, DAY)   # starts with an emoji
        fake = self._cp1252_stdout()
        with mock.patch.object(sys, "stdout", fake):
            with self.assertRaises(UnicodeEncodeError):
                print(report, flush=True)         # the old behaviour
            ma.safe_print(report)                 # must not raise
            fake.flush()
        payload = fake.buffer.getvalue().decode("cp1252")
        self.assertIn("MARKET WEATHER", payload)  # report still displayed

    def test_safe_print_passthrough_on_utf8(self):
        import io
        fake = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        with mock.patch.object(sys, "stdout", fake):
            ma.safe_print("héllo \U0001f916")
            fake.flush()
        self.assertIn("\U0001f916",
                      fake.buffer.getvalue().decode("utf-8"))


class TestSources(unittest.TestCase):
    """Ingestion pipeline: parsing, noise filtering, priorities."""

    def test_calendar_filters_today_high_impact_majors(self):
        events = [
            {"title": "CPI y/y", "country": "USD", "impact": "High",
             "date": "2026-07-16T14:30:00+02:00", "forecast": "3.1%"},
            {"title": "CPI tomorrow", "country": "USD", "impact": "High",
             "date": "2026-07-17T14:30:00+02:00"},
            {"title": "minor PMI", "country": "USD", "impact": "Low",
             "date": "2026-07-16T10:00:00+02:00"},
            {"title": "BoJ rates", "country": "JPY", "impact": "High",
             "date": "2026-07-16T04:00:00+02:00"},
            {"no": "fields"},
        ]
        lines = ms.parse_calendar(events, DAY)
        self.assertEqual(len(lines), 1)
        self.assertIn("12:30 UTC [USD] CPI y/y (forecast 3.1%)", lines[0])

    def test_rss_and_atom_parsed_corrupt_ignored(self):
        rss = ("<rss><channel><item><title>Gold hits record</title></item>"
               "<item><title>Oil slides</title></item></channel></rss>")
        atom = ('<feed xmlns="http://www.w3.org/2005/Atom">'
                "<entry><title>Brent up</title></entry></feed>")
        self.assertEqual(ms.parse_rss_titles(rss),
                         ["Gold hits record", "Oil slides"])
        self.assertEqual(ms.parse_rss_titles(atom), ["Brent up"])
        self.assertEqual(ms.parse_rss_titles("<not xml"), [])

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

    def test_social_filter_keeps_china_entities(self):
        # 2026-07-23: USDCNH diversification (PBoC/Fed policy divergence)
        titles = ["PBoC sets Yuan fixing stronger than expected",
                  "Beijing unveils new stimulus package",
                  "State Council approves fiscal measures",
                  "Celebrity gossip of the day"]
        kept = ms.filter_social(titles)
        self.assertEqual(len(kept), 3)
        self.assertNotIn("Celebrity gossip of the day", kept)

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
        self.assertIn("TODAY'S MAJOR MACRO RELEASES", d)
        self.assertIn("GEOPOLITICS & ENERGY", d)
        self.assertIn("SOCIAL MEDIA", d)
        self.assertIn("BANK DESKS & SELL-SIDE RESEARCH", d)
        self.assertIn("no major release", d)
        self.assertIn("feeds unavailable", d)
        self.assertIn("no relevant statement", d)
        self.assertIn("no bank note", d)


class TestReport(unittest.TestCase):
    def test_report_structure(self):
        report = ma.format_report(VERDICT, DAY)
        self.assertIn("[SENTINEL BOT 7]", report)
        self.assertIn("(2026-07-16)", report)
        self.assertIn("STORMY (Confidence: 85%)", report)
        self.assertIn("US CPI at 14:30 UTC", report)
        self.assertIn("THE COUNCIL", report)
        self.assertIn("Geopolitics:", report)
        self.assertIn("Macro:", report)
        self.assertIn("Sentiment:", report)
        self.assertIn("ANALYSTS & BANK DESKS (Agent 4)", report)
        self.assertIn("JPMorgan bullish bias XAUUSD targets 4200", report)
        self.assertIn("CONFLICT OF THE DAY", report)
        self.assertIn("FLEET RECOMMENDATION", report)
        self.assertIn("High priority", report)      # STORMY -> bots 1&3

    def test_calm_weather_favors_statarb(self):
        report = ma.format_report(dict(VERDICT, weather="CALM"), DAY)
        self.assertIn("false breakouts likely", report)
        self.assertIn("Favourable conditions", report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
