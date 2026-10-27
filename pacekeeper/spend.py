"""Token spend from Claude Code transcripts (reads only local files, no network).

Transcripts are JSONL files under <config>/projects/<project>/<session>.jsonl,
with sub-agent transcripts under <project>/<session>/subagents/. One API call is
streamed as several lines carrying the same message id, so records are
de-duplicated on (message id, request id) and the latest one wins.

Weight, in tokens: input + 1.25 * cache write + 0.1 * cache read + 5 * output.
It is a rough relative cost, models are not priced separately.
"""
from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import paths

W_IN, W_CW, W_CR, W_OUT = 1.0, 1.25, 0.1, 5.0


@dataclass
class Call:
    ts: float
    project: str
    session: str
    is_sub: bool
    model: str
    kind: str
    usage: dict
    file: str = ""


def weight(u: dict) -> float:
    return (u.get("input_tokens", 0) * W_IN + u.get("cache_creation_input_tokens", 0) * W_CW
            + u.get("cache_read_input_tokens", 0) * W_CR + u.get("output_tokens", 0) * W_OUT)


def kind_of(text: str) -> str:
    """Classify what a user-side message was, to see which turns cost the most."""
    t = text[:400]
    if "<task-notification" in t:
        return "task notification"
    if "Stop hook feedback" in t or "hook feedback" in t.lower():
        return "hook feedback"
    if "<command-name>" in t or "/loop" in t:
        return "command or loop"
    return "user prompt"


def _ts(rec: dict) -> float | None:
    try:
        return datetime.fromisoformat(str(rec["timestamp"]).replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError):
        return None


def _home_relative(p: str) -> str:
    home = str(Path.home())
    if p == home:
        return "~"
    if p.startswith(home + os.sep):
        return "~" + p[len(home):]
    return p


def transcript_files(root: Path) -> list[Path]:
    root = Path(root)
    files = list(root.glob("*/*.jsonl")) + list(root.glob("*/*/subagents/*.jsonl"))
    return sorted(files)


def collect(root, since: float, until: float | None = None, only_dir: str | None = None) -> dict[tuple, Call]:
    """Return de-duplicated assistant calls with since <= timestamp (< until)."""
    root = Path(root)
    recs: dict[tuple, Call] = {}
    for f in transcript_files(root):
        rel = f.relative_to(root).parts
        if only_dir and rel[0] != only_dir:
            continue
        try:
            if f.stat().st_mtime < since:
                continue
        except OSError:
            continue
        is_sub = len(rel) > 2
        session = rel[1] if is_sub else f.stem
        cwd = ""
        pending: list[tuple[tuple, Call]] = []
        last_kind = "user prompt"
        try:
            fh = open(f, errors="replace")
        except OSError:
            continue
        with fh:
            for i, line in enumerate(fh):
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                if not cwd and isinstance(rec.get("cwd"), str):
                    cwd = rec["cwd"]
                typ = rec.get("type")
                msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
                if typ == "user" and not is_sub:
                    c = msg.get("content")
                    if isinstance(c, str):
                        last_kind = kind_of(c)
                    elif isinstance(c, list) and c and isinstance(c[0], dict) and c[0].get("type") == "text":
                        last_kind = kind_of(str(c[0].get("text", "")))
                    continue
                u = msg.get("usage") if typ == "assistant" else None
                ts = _ts(rec)
                if not isinstance(u, dict) or ts is None or ts < since or (until is not None and ts >= until):
                    continue
                mid = msg.get("id")
                key = (mid, rec.get("requestId")) if mid else (str(f), i)
                pending.append((key, Call(ts, "", session, is_sub, str(msg.get("model", "")), last_kind, u, str(f))))
        label = _home_relative(cwd) if cwd else rel[0]
        for key, call in pending:
            call.project = label
            old = recs.get(key)
            if old is None or call.ts > old.ts or (call.ts == old.ts and old.file == call.file):
                recs[key] = call
    return recs


def load_roles() -> dict:
    p = os.environ.get("PACEKEEPER_ROLES") or str(paths.state_dir() / "roles.json")
    d = paths.load_json(p)
    return d if isinstance(d, dict) else {}


def group_key(call: Call, by: str, roles: dict) -> str:
    if by == "project":
        return call.project
    if by == "session":
        return f"{call.project}:{call.session[:8]}"
    if by == "model":
        return call.model or "unknown"
    if by == "role":
        for sid, label in roles.items():
            if call.session.startswith(sid):
                return str(label)
        return "subagent" if call.is_sub else "main"
    raise ValueError(by)


def summarize(calls: Iterable[Call], by: str = "project", roles: dict | None = None) -> list[dict]:
    roles = roles or {}
    agg: dict[str, list] = defaultdict(lambda: [0, 0, 0, 0, 0])
    for c in calls:
        a = agg[group_key(c, by, roles)]
        u = c.usage
        a[0] += 1
        a[1] += u.get("input_tokens", 0)
        a[2] += u.get("cache_creation_input_tokens", 0)
        a[3] += u.get("cache_read_input_tokens", 0)
        a[4] += u.get("output_tokens", 0)
    rows = []
    for k, a in agg.items():
        w = weight({"input_tokens": a[1], "cache_creation_input_tokens": a[2],
                    "cache_read_input_tokens": a[3], "output_tokens": a[4]})
        rows.append({"key": k, "calls": a[0], "input": a[1], "cache_write": a[2],
                     "cache_read": a[3], "output": a[4], "weight": w})
    rows.sort(key=lambda r: -r["weight"])
    return rows


def cost_report(root, hours: float, by: str, now: float, top: int = 18) -> tuple[list[dict], float]:
    calls = collect(root, now - hours * 3600).values()
    rows = summarize(calls, by, load_roles() if by == "role" else None)
    return rows[:top], sum(r["weight"] for r in rows)


def project_dir_name(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(path))


def audit(root, folder: str, hours: float, now: float) -> dict:
    """Totals and per-kind cost for the sessions run from one folder."""
    d = project_dir_name(folder)
    alt = os.path.realpath(folder).replace("/", "-")
    only = d if (Path(root) / d).is_dir() or not (Path(root) / alt).is_dir() else alt
    calls = collect(root, now - hours * 3600, only_dir=only).values()
    files = {c.file for c in calls}
    tot = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}
    turns = main_cr = sub_out = 0
    kinds: dict[str, list] = {}
    for c in calls:
        u = c.usage
        o, cr = u.get("output_tokens", 0), u.get("cache_read_input_tokens", 0)
        tot["input"] += u.get("input_tokens", 0)
        tot["output"] += o
        tot["cache_write"] += u.get("cache_creation_input_tokens", 0)
        tot["cache_read"] += cr
        if c.is_sub:
            sub_out += o
        else:
            turns += 1
            main_cr += cr
            k = kinds.setdefault(c.kind, [0, 0])
            k[0] += 1
            k[1] += o + cr // 10
    return {"folder": os.path.realpath(folder), "hours": hours, "files": len(files), "calls": len(list(calls)),
            "totals": tot, "subagent_output": sub_out, "main_calls": turns,
            "avg_context_read": main_cr // turns if turns else 0,
            "kinds": [{"kind": k, "calls": v[0], "weight": v[1]}
                      for k, v in sorted(kinds.items(), key=lambda x: -x[1][1])]}


def count_calls(root, since: float, now: float) -> int:
    """Number of de-duplicated assistant calls (main and sub-agent) since a time."""
    return len(collect(root, since, now + 1))
