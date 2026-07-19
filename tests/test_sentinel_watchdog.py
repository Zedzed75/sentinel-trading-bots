"""SENTINEL WATCHDOG regression tests (static checks on ops/watchdog.ps1).

Run:  python -m unittest test_sentinel_watchdog -v
Core guarantee: the watchdog supervises the whole fleet - the 8 bots AND
the mobile dashboard (port 8787) - and launches the dashboard from the
repo root (it does not live in bots/).
"""

import os
import re
import unittest

WATCHDOG = os.path.join(os.path.dirname(__file__), "..", "ops", "watchdog.ps1")

FLEET = [
    "sentinel_risk_orchestrator.py",
    "sentinel_bot.py",
    "sentinel_alpha_compound.py",
    "sentinel_trend.py",
    "sentinel_trade_analytics.py",
    "sentinel_telegram.py",
    "sentinel_macro_analyst.py",
    "sentinel_arbitrage.py",
    "sentinel_dashboard.py",
]


class TestWatchdogFleet(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(WATCHDOG, encoding="utf-8") as fh:
            cls.src = fh.read()
        m = re.search(r"\$Bots\s*=\s*@\(([^)]*)\)", cls.src)
        assert m, "$Bots list not found in watchdog.ps1"
        cls.bots = re.findall(r'"([^"]+)"', m.group(1))

    def test_whole_fleet_supervised(self):
        for script in FLEET:
            self.assertIn(script, self.bots, f"{script} not watched")

    def test_orchestrator_restarted_first(self):
        self.assertEqual(self.bots[0], "sentinel_risk_orchestrator.py")

    def test_dashboard_launched_from_repo_root(self):
        # Get-BotDir must special-case the dashboard to $Root and the
        # restart must use it instead of a hardcoded $BotsDir.
        self.assertRegex(
            self.src,
            r'Get-BotDir[^}]*"sentinel_dashboard\.py"[^}]*\$Root',
        )
        self.assertIn("-WorkingDirectory (Get-BotDir $bot)", self.src)
        self.assertNotIn("-WorkingDirectory $BotsDir", self.src)

    def test_ntp_guard_present_and_delegated_to_elevated_task(self):
        # The watchdog runs Limited: it must never call Start-Service
        # W32Time directly, only trigger the elevated on-demand task
        # SentinelTimeSync; Telegram alert on the down transition only.
        self.assertIn("W32Time", self.src)
        self.assertIn("SentinelTimeSync", self.src)
        self.assertNotIn("Start-Service W32Time", self.src)
        self.assertIn("NtpWasDown", self.src)
        self.assertIn("Check-TimeService", self.src)

    def test_dashboard_has_no_heartbeat_threshold(self):
        # Request-driven process: no .hb file, so no freeze threshold
        # (a stale entry would make the watchdog kill a healthy server).
        m = re.search(r"\$HbLimitSec\s*=\s*@\{([^}]*)\}", self.src)
        self.assertIsNotNone(m)
        self.assertNotIn("sentinel_dashboard.py", m.group(1))
