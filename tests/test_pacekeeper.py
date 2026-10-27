"""Small tests on synthetic transcripts and snapshots. Run: python3 -m unittest discover -s tests"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from pacekeeper import meters, pace, spend

PROJECTS = HERE / "fixtures" / "projects"
NOW = datetime(2026, 1, 1, 13, 0, tzinfo=timezone.utc).timestamp()


def snapshot(five, seven, now, five_left=2 * 3600, seven_left=3 * 86400, captured=None):
    return {"captured_at": now if captured is None else captured,
            "rate_limits": {"five_hour": {"used_percentage": five, "resets_at": now + five_left},
                            "seven_day": {"used_percentage": seven, "resets_at": now + seven_left}}}


class SpendTests(unittest.TestCase):
    def test_cost_dedups_streamed_calls_and_groups(self):
        rows = {r["key"]: r for r in spend.summarize(spend.collect(PROJECTS, NOW - 24 * 3600).values())}
        self.assertEqual(set(rows), {"/srv/demo/alpha", "/srv/demo/beta"})
        a = rows["/srv/demo/alpha"]
        self.assertEqual(a["calls"], 4)                 # m1 once, m2, m3, subagent m4; m0 is out of window
        self.assertEqual(a["output"], 50 + 100 + 200 + 40)
        self.assertEqual(a["cache_read"], 1000 + 2000 + 5000 + 100)
        self.assertAlmostEqual(spend.weight({"input_tokens": 10, "cache_creation_input_tokens": 100,
                                             "cache_read_input_tokens": 1000, "output_tokens": 50}),
                               10 + 125 + 100 + 250)

        calls = spend.collect(PROJECTS, NOW - 24 * 3600).values()
        roles = {r["key"]: r["calls"] for r in spend.summarize(calls, "role")}
        self.assertEqual(roles, {"main": 4, "subagent": 1})
        models = {r["key"] for r in spend.summarize(calls, "model")}
        self.assertEqual(models, {"model-a", "model-b"})

    def test_audit_splits_turn_kinds(self):
        r = spend.audit(PROJECTS, "/srv/demo/alpha", 24, NOW)
        kinds = {k["kind"]: k["calls"] for k in r["kinds"]}
        self.assertEqual(kinds, {"user prompt": 2, "task notification": 1})
        self.assertEqual(r["subagent_output"], 40)


class MeterTests(unittest.TestCase):
    def test_pace_numbers_and_stale_age(self):
        ms = meters.parse_snapshot(snapshot(50, 70, NOW, captured=NOW - 600))
        self.assertEqual(ms["5h"].age(NOW), 600)
        p = pace.window_pace(ms["7d"], NOW)       # 70% used, 4 of 7 days elapsed
        self.assertEqual(p["verdict"], "over pace")
        self.assertAlmostEqual(p["burn_pct_per_hour"], 70 / 96, places=2)
        self.assertAlmostEqual(p["target_pct_per_hour"], 30 / 72, places=2)
        self.assertIn("exhausted_before_reset_hours", p)
        b = pace.budget(60, 2, 95, 0.5)
        self.assertEqual((b["allowed_pct_per_hour"], b["turns_left"]), (17.5, 70))

    def test_tick_policy_steps_in_then_relaxes_and_doubles_when_over(self):
        ms = meters.parse_snapshot(snapshot(10, 10, NOW))
        secs, _, st = pace.tick_interval("bad", ms, {}, NOW)
        self.assertEqual(secs, 900)
        secs, _, _ = pace.tick_interval("bad", ms, st, NOW + 3601)
        self.assertEqual(secs, 3600)
        secs, why, _ = pace.tick_interval("good", meters.parse_snapshot(snapshot(10, 90, NOW)), {}, NOW)
        self.assertEqual(secs, 7200)
        self.assertIn("doubled", why)


class CliTests(unittest.TestCase):
    def run_cli(self, env_dir, *args, stdin=""):
        env = dict(os.environ, PACEKEEPER_STATE_DIR=env_dir, PACEKEEPER_NOW=str(NOW),
                   PACEKEEPER_TZ="UTC", CLAUDE_CONFIG_DIR=env_dir)
        return subprocess.run([sys.executable, "-m", "pacekeeper", *args], input=stdin, capture_output=True,
                              text=True, cwd=ROOT, env=env, timeout=30)

    def test_statusline_snapshot_usage_hook_and_resets(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self.run_cli(d, "--help").returncode, 0)
            self.assertEqual(self.run_cli(d, "usage").returncode, 1)         # nothing saved yet
            r = self.run_cli(d, "statusline", stdin=json.dumps(snapshot(83, 40, NOW)))
            self.assertEqual(r.stdout.strip(), "5h 83%  7d 40%")
            u = self.run_cli(d, "usage", "--json")
            self.assertEqual(u.returncode, 0)
            self.assertEqual(json.loads(u.stdout)["meters"]["5h"]["used_percentage"], 83)
            h = self.run_cli(d, "hook", stdin=json.dumps({"session_id": "s1"}))
            self.assertIn("wind down", h.stdout)
            self.assertEqual(self.run_cli(d, "hook", stdin=json.dumps({"session_id": "s1"})).stdout, "")  # throttled
            # a later window rolls over: the log records the peak of the old one
            self.run_cli(d, "resets")
            later = dict(os.environ, PACEKEEPER_STATE_DIR=d, PACEKEEPER_NOW=str(NOW + 3 * 3600), PACEKEEPER_TZ="UTC",
                         CLAUDE_CONFIG_DIR=d)
            subprocess.run([sys.executable, "-m", "pacekeeper", "statusline", "--quiet"], cwd=ROOT, env=later, timeout=30,
                           input=json.dumps(snapshot(5, 41, NOW + 3 * 3600)), text=True)
            out = subprocess.run([sys.executable, "-m", "pacekeeper", "resets"], cwd=ROOT, env=later, timeout=30,
                                 capture_output=True, text=True).stdout
            self.assertIn("ROLLOVER 5h", out)
            self.assertIn("peaked 83%", out)
            stale = subprocess.run([sys.executable, "-m", "pacekeeper", "usage"], cwd=ROOT, timeout=30, capture_output=True,
                                   text=True, env=dict(later, PACEKEEPER_NOW=str(NOW + 99999)))
            self.assertEqual(stale.returncode, 2)


if __name__ == "__main__":
    unittest.main()
