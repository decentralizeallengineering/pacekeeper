"""Tests for `line` (linear weekly line on a fixed clock) and `govern` (PreToolUse gate).

Run: python3 -m unittest discover -s tests
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from pacekeeper import govern as gv
from pacekeeper import line as ln
from pacekeeper import meters as mt

SPEC = "Wed 12:59 America/Los_Angeles"
WEEK = 7 * 86400
# Fri 2026-11-13 12:00 PST. The window opened Wed 2026-11-11 12:59 PST.
NOW = None  # set in setUpModule
WINDOW_START = None
WINDOW_END = None


def setUpModule():
    global NOW, WINDOW_START, WINDOW_END
    from datetime import datetime
    from zoneinfo import ZoneInfo
    NOW = datetime(2026, 11, 13, 12, 0, tzinfo=ZoneInfo("America/Los_Angeles")).timestamp()
    WINDOW_START, WINDOW_END = ln.window(SPEC, NOW)


def linear_at(now):
    return 100.0 * (now - WINDOW_START) / WEEK


class EnvCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.snap = root / "usage-state.json"
        env = {
            "PACEKEEPER_SNAPSHOT": str(self.snap),
            "PACEKEEPER_STATE_DIR": str(root / "state"),
            "PACEKEEPER_CONFIG": str(root / "no-config.json"),
            "PACEKEEPER_RESET": SPEC,
            "CLAUDE_CONFIG_DIR": str(root / "claude"),
            "PACEKEEPER_TZ": "America/Los_Angeles",
        }
        p = unittest.mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        for k in ("PACEKEEPER_MARGIN", "CLAUDE_CODE_SUBAGENT_MODEL", "PACEKEEPER_MAX_AGE"):
            os.environ.pop(k, None)
        self.root = root

    def write_snapshot(self, seven_used, captured=None):
        data = {"captured_at": NOW if captured is None else captured,
                "rate_limits": {"five_hour": {"used_percentage": 5.0, "resets_at": NOW + 3600},
                                "seven_day": {"used_percentage": seven_used, "resets_at": WINDOW_END}}}
        self.snap.write_text(json.dumps(data))


class LineTests(EnvCase):
    def test_window_opens_at_configured_reset(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        want = datetime(2026, 11, 11, 12, 59, tzinfo=ZoneInfo("America/Los_Angeles")).timestamp()
        self.assertEqual(WINDOW_START, want)
        self.assertEqual(WINDOW_END - WINDOW_START, WEEK)

    def test_delta_is_used_minus_linear(self):
        self.write_snapshot(40.0)
        st = ln.status(NOW)
        self.assertAlmostEqual(st["linear"], linear_at(NOW), places=2)
        self.assertAlmostEqual(st["delta"], 40.0 - linear_at(NOW), places=2)
        self.assertTrue(st["over"])
        self.assertEqual(st["hours_to_line"], 0.0)

    def test_flat_history_never_crosses(self):
        self.write_snapshot(10.0)
        hist = [{"t": NOW - 3600 * h, "7d": {"used": 10.0, "resets_at": WINDOW_END}} for h in (0, 6, 11)]
        st = ln.status(NOW, meters=mt.read_meters(), history=hist)
        self.assertLess(st["delta"], 0)
        self.assertIsNone(st["hours_to_line"])
        self.assertAlmostEqual(st["burn_pct_per_hour"], 0.0, places=2)

    def test_hours_to_cross_from_history_burn(self):
        # 25% now; 15% ten hours ago: burn 1.0 %/h against a line rate of 100/168 %/h.
        self.write_snapshot(25.0)
        hist = [{"t": NOW - 10 * 3600, "7d": {"used": 15.0, "resets_at": WINDOW_END}},
                {"t": NOW - 3600, "7d": {"used": 24.0, "resets_at": WINDOW_END}}]
        st = ln.status(NOW, history=hist)
        line_rate = 100.0 * 3600 / WEEK
        self.assertAlmostEqual(st["burn_pct_per_hour"], 1.0, places=2)
        want = (linear_at(NOW) - 25.0) / (1.0 - line_rate)
        self.assertAlmostEqual(st["hours_to_line"], want, places=1)
        self.assertFalse(st["over"])

    def test_render_and_json_fields(self):
        self.write_snapshot(40.0)
        st = ln.status(NOW)
        text = "\n".join(ln.render(st))
        self.assertIn("7d 40.0% used", text)
        self.assertIn("OVER the line", text)
        self.assertTrue({"used", "linear", "delta", "burn_pct_per_hour", "hours_to_line"} <= set(st))

    def test_no_snapshot_returns_none(self):
        self.assertIsNone(ln.status(NOW))

    def test_no_reset_raises(self):
        self.write_snapshot(40.0)
        os.environ.pop("PACEKEEPER_RESET", None)
        with self.assertRaises(ValueError):
            ln.status(NOW)


class GovernTests(EnvCase):
    def setUp(self):
        super().setUp()
        agents = self.root / "claude" / "agents"
        agents.mkdir(parents=True)
        (agents / "opus-builder.md").write_text("---\nname: opus-builder\nmodel: opus\n---\nBody\n")
        (agents / "hk-scout.md").write_text("---\nname: hk-scout\nmodel: haiku\n---\nBody\n")

    def decide(self, payload, now=None):
        out = gv.run(json.dumps(payload), NOW if now is None else now)
        return None if out is None else json.loads(out)

    def agent(self, tool_input, tool="Agent"):
        return {"tool_name": tool, "tool_input": tool_input}

    def assert_denied(self, out):
        self.assertIsNotNone(out)
        h = out["hookSpecificOutput"]
        self.assertEqual(h["hookEventName"], "PreToolUse")
        self.assertEqual(h["permissionDecision"], "deny")
        self.assertTrue(h["permissionDecisionReason"].startswith("pace +"))
        return h["permissionDecisionReason"]

    def test_deny_opus_subagent_from_frontmatter(self):
        self.write_snapshot(40.0)
        reason = self.assert_denied(self.decide(self.agent({"subagent_type": "opus-builder", "prompt": "x"})))
        self.assertIn("over linear line: use haiku or wait", reason)

    def test_deny_sonnet_from_tool_input(self):
        self.write_snapshot(40.0)
        self.assert_denied(self.decide(self.agent({"model": "sonnet"})))

    def test_deny_task_tool_with_full_model_id(self):
        self.write_snapshot(40.0)
        self.assert_denied(self.decide(self.agent({"model": "claude-opus-4-1"}, tool="Task")))

    def test_allow_haiku_when_over(self):
        self.write_snapshot(40.0)
        self.assertIsNone(self.decide(self.agent({"subagent_type": "hk-scout"})))
        self.assertIsNone(self.decide(self.agent({"model": "haiku"})))

    def test_allow_unknown_model_when_over(self):
        self.write_snapshot(40.0)
        self.assertIsNone(self.decide(self.agent({"subagent_type": "nobody"})))

    def test_allow_opus_when_under_line(self):
        self.write_snapshot(10.0)
        self.assertIsNone(self.decide(self.agent({"model": "opus"})))

    def test_workflow_denied_when_over_and_allowed_under(self):
        self.write_snapshot(40.0)
        reason = self.assert_denied(self.decide({"tool_name": "Workflow", "tool_input": {}}))
        self.assertIn("Workflow denied", reason)
        self.write_snapshot(10.0)
        self.assertIsNone(self.decide({"tool_name": "Workflow", "tool_input": {}}))

    def test_other_tools_never_decided(self):
        self.write_snapshot(40.0)
        self.assertIsNone(self.decide({"tool_name": "Bash", "tool_input": {"command": "ls"}}))

    def test_fails_open_on_stale_snapshot(self):
        self.write_snapshot(40.0, captured=NOW - 1000)
        err = io.StringIO()
        with unittest.mock.patch("sys.stderr", err):
            self.assertIsNone(self.decide(self.agent({"model": "opus"})))
        self.assertIn("allowing", err.getvalue())

    def test_fails_open_on_missing_snapshot(self):
        err = io.StringIO()
        with unittest.mock.patch("sys.stderr", err):
            self.assertIsNone(self.decide(self.agent({"model": "opus"})))
        self.assertIn("no 7d snapshot", err.getvalue())

    def test_fails_open_without_reset(self):
        self.write_snapshot(40.0)
        os.environ.pop("PACEKEEPER_RESET", None)
        err = io.StringIO()
        with unittest.mock.patch("sys.stderr", err):
            self.assertIsNone(self.decide(self.agent({"model": "opus"})))
        self.assertIn("not configured", err.getvalue())

    def test_garbage_stdin_allows(self):
        self.write_snapshot(40.0)
        self.assertIsNone(gv.run("not json", NOW))
        self.assertIsNone(gv.run("", NOW))

    def test_deny_writes_fire_log(self):
        self.write_snapshot(40.0)
        self.decide(self.agent({"model": "sonnet"}))
        rows = (gv.fire_log_path()).read_text().splitlines()
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0])["model"], "sonnet")

    def test_cli_prints_deny_json_and_nothing_when_allowed(self):
        self.write_snapshot(40.0)
        env = dict(os.environ, PYTHONPATH=str(ROOT), PACEKEEPER_NOW=repr(NOW))
        payload = json.dumps(self.agent({"subagent_type": "opus-builder"}))
        r = subprocess.run([sys.executable, "-m", "pacekeeper", "govern"], input=payload, text=True,
                           capture_output=True, env=env, cwd=str(ROOT), timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
        r = subprocess.run([sys.executable, "-m", "pacekeeper", "govern"],
                           input=json.dumps(self.agent({"model": "haiku"})), text=True,
                           capture_output=True, env=env, cwd=str(ROOT), timeout=60)
        self.assertEqual((r.returncode, r.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
