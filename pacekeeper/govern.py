"""PreToolUse gate: while 7d usage is over the linear line, no opus or sonnet agents.

Wire it as a PreToolUse hook with matcher "Agent|Task|Workflow", command
``pacekeeper govern``. It reads the hook JSON on stdin. When ``pacekeeper line``
reports a delta above the margin (default 2 points):

  Agent or Task whose model is opus or sonnet is denied. The model comes from
  tool_input.model, else the subagent type's ``model:`` in <config>/agents/<type>.md,
  else CLAUDE_CODE_SUBAGENT_MODEL. Haiku and unknown models are allowed.
  Workflow is denied.

At or under the line everything is allowed. It fails open (allows, with a note on
stderr) when the snapshot is missing or stale, or no reset is configured. It never
decides for other tools.

Deny output, exit 0, on stdout (the Claude Code PreToolUse schema):
  {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                          "permissionDecisionReason": "pace +X pts over linear line: use haiku or wait"}}
Allow prints nothing.
"""
from __future__ import annotations

import json
import os
import sys

from . import line as ln
from . import paths

TOOLS = ("Agent", "Task", "Workflow")
DENY_MODELS = ("opus", "sonnet")


def _note(msg: str) -> None:
    sys.stderr.write(f"pacekeeper govern: {msg}\n")


def _frontmatter_model(path) -> str:
    try:
        with open(path, errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return ""
    if not lines or lines[0].strip() != "---":
        return ""
    for raw in lines[1:]:
        if raw.strip() == "---":
            break
        k, _, v = raw.partition(":")
        if k.strip() == "model":
            return v.strip().strip("\"'").lower()
    return ""


def model_of(tool_input: dict) -> str:
    """The model an Agent or Task call will run on, lower case; empty when unknown."""
    m = tool_input.get("model")
    if isinstance(m, str) and m.strip():
        return m.strip().lower()
    st = tool_input.get("subagent_type")
    if isinstance(st, str) and st.strip() and "/" not in st and ".." not in st:
        fm = _frontmatter_model(paths.config_dir() / "agents" / (st.strip() + ".md"))
        if fm:
            return fm
    return (os.environ.get("CLAUDE_CODE_SUBAGENT_MODEL") or "").strip().lower()


def decide(payload: dict, now: float) -> str | None:
    """The deny JSON line to print, or None to allow."""
    tool = payload.get("tool_name")
    if tool not in TOOLS:
        return None
    try:
        st = ln.status(now)
    except ValueError as e:
        _note(f"allowing, the line is not configured ({e})")
        return None
    if st is None:
        _note("allowing, no 7d snapshot yet")
        return None
    if st["stale"]:
        _note(f"allowing, the snapshot is {st['age_seconds']}s old")
        return None
    if not st["over"]:
        return None
    tag = f"pace +{st['delta']:.1f} pts over linear line"
    if tool == "Workflow":
        reason = f"{tag}: Workflow denied, wait for the line"
    else:
        tin = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
        if not any(k in model_of(tin) for k in DENY_MODELS):
            return None
        reason = f"{tag}: use haiku or wait"
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                              "permissionDecisionReason": reason}})


def fire_log_path():
    """govern-fires.jsonl in the pacekeeper state dir: one line per deny."""
    return paths.state_dir() / "govern-fires.jsonl"


def _log_fire(payload: dict, out: str, now: float) -> None:
    try:
        p = fire_log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tin = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
        row = {"t": round(now, 1), "tool": payload.get("tool_name"),
               "subagent_type": tin.get("subagent_type"), "model": model_of(tin),
               "reason": json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]}
        with open(p, "a") as fh:
            fh.write(json.dumps(row) + "\n")
    except Exception:
        pass  # the log is best effort; the deny still stands


def run(stdin_text: str, now: float) -> str | None:
    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        out = decide(payload, now)
    except Exception as e:  # fail open on anything unexpected
        _note(f"allowing, error: {e}")
        return None
    if out:
        _log_fire(payload, out, now)
    return out
