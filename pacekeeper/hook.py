"""Claude Code hook that injects a wind-down warning when 5h usage is high.

Wire it to the UserPromptSubmit and PostToolUse hooks as ``pacekeeper hook``.
It reads the 5h meter saved by ``pacekeeper statusline`` and, past a threshold,
prints a short ``additionalContext`` note so the session can finish gracefully.
It never blocks and always exits 0; on any error it stays silent.

Configuration is a JSON file, $PACEKEEPER_CONFIG or <state dir>/config.json.
Every key is optional. Defaults:

  {"tiers": [{"at": 80, "action": "wind down: finish the current task, start nothing new"},
             {"at": 95, "action": "pause: commit work and start no new agents until the reset"}],
   "throttle_seconds": 600,
   "final_window_minutes": 0,
   "max_age_seconds": 1800}

``tiers``: from the highest ``at`` that the 5h percentage has reached.
``throttle_seconds``: minimum gap between notes per session unless the tier changes.
``final_window_minutes``: stay silent this long before the reset, so the last
minutes of a window can be spent (0 disables).
``max_age_seconds``: ignore a snapshot older than this.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import meters as mt
from . import paths

DEFAULTS = {
    "tiers": [
        {"at": 80, "action": "wind down: finish the current task, start nothing new"},
        {"at": 95, "action": "pause: commit work and start no new agents until the reset"},
    ],
    "throttle_seconds": 600,
    "final_window_minutes": 0,
    "max_age_seconds": 1800,
}


def config_path() -> Path:
    return Path(os.environ.get("PACEKEEPER_CONFIG") or paths.state_dir() / "config.json")


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    user = paths.load_json(config_path())
    if isinstance(user, dict):
        cfg.update({k: v for k, v in user.items() if k in DEFAULTS})
    return cfg


def pick_tier(used: float, cfg: dict) -> tuple[int, dict | None]:
    """Return (1-based tier number, tier) for the highest threshold reached."""
    tiers = sorted((t for t in cfg["tiers"] if "at" in t), key=lambda t: float(t["at"]))
    hit = (0, None)
    for i, t in enumerate(tiers, 1):
        if used >= float(t["at"]):
            hit = (i, t)
    return hit


def decide(session: str, now: float, cfg: dict | None = None, meters=None,
           stamp_dir: Path | None = None) -> str | None:
    """The note to inject, or None. Reads the snapshot and records a throttle stamp."""
    cfg = cfg or load_config()
    meters = mt.read_snapshot() if meters is None else meters
    m = meters.get("5h")
    if not m or now - m.captured_at > float(cfg["max_age_seconds"]) or m.resets_at <= now:
        return None
    if m.resets_at - now <= float(cfg["final_window_minutes"]) * 60:
        return None
    n, tier = pick_tier(m.used, cfg)
    if not tier:
        return None
    stamp = Path(stamp_dir or paths.state_dir() / "hook-stamps") / (session or "default")
    stamp.parent.mkdir(parents=True, exist_ok=True)
    last = paths.load_json(stamp)
    if last.get("tier") == n and now - float(last.get("t", 0)) < float(cfg["throttle_seconds"]):
        return None
    paths.write_json_atomic(stamp, {"t": now, "tier": n})
    return (f"5h usage is at {m.used:.0f}% (resets at {paths.fmt_time(m.resets_at)}): {tier['action']}.")


def run(stdin_text: str, now: float) -> str | None:
    """Return the hook JSON line to print, or None."""
    try:
        d = json.loads(stdin_text) if stdin_text.strip() else {}
    except ValueError:
        d = {}
    if not isinstance(d, dict):
        d = {}
    try:
        msg = decide(str(d.get("session_id") or ""), now)
    except Exception:
        return None
    if not msg:
        return None
    ev = d.get("hook_event_name") or "UserPromptSubmit"
    return json.dumps({"hookSpecificOutput": {"hookEventName": ev, "additionalContext": msg}})
