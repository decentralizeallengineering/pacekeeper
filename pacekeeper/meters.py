"""Real 5h and 7d rate-limit readings.

The only source is the snapshot written by ``pacekeeper statusline``. Claude
Code passes the real ``rate_limits`` to the status line command on stdin;
nothing else persists them, so the statusline command saves them.
Every save also appends a sample to a history file beside the snapshot (one
per five minutes, eight days kept), which ``pacekeeper line`` uses for burn.

Percentages computed by tools that guess the quota from local history are not
the account's real figure, which is why this module derives none.
A snapshot is only as fresh as the last status line refresh, so every reading
carries its capture time and callers must treat an old one as unknown.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from . import paths

WINDOWS = {"5h": 5 * 3600, "7d": 7 * 86400}
_KEYS = (("five_hour", "5h"), ("seven_day", "7d"))
HISTORY_EVERY = 300          # seconds between history samples
HISTORY_KEEP = 8 * 86400     # seconds of history kept


@dataclass
class Meter:
    name: str
    used: float          # percent of the window's quota, 0-100
    resets_at: float     # epoch seconds
    captured_at: float   # epoch seconds
    source: str          # always "snapshot"

    def age(self, now: float) -> float:
        return max(0.0, now - self.captured_at)


def max_age() -> float:
    return float(os.environ.get("PACEKEEPER_MAX_AGE", "180"))


def parse_snapshot(data: dict) -> dict[str, Meter]:
    out: dict[str, Meter] = {}
    rl = (data or {}).get("rate_limits") or {}
    cap = float(data.get("captured_at") or 0) if data else 0.0
    for key, name in _KEYS:
        v = rl.get(key) or {}
        try:
            used, rs = float(v["used_percentage"]), float(v["resets_at"])
        except (KeyError, TypeError, ValueError):
            continue
        out[name] = Meter(name, used, rs, cap, "snapshot")
    return out


def read_snapshot(path=None) -> dict[str, Meter]:
    return parse_snapshot(paths.load_json(path or paths.snapshot_path()))


def write_snapshot(payload: dict, now: float | None = None, path=None) -> bool:
    """Save the rate_limits of a status line payload. False if it has none."""
    rl = (payload or {}).get("rate_limits")
    if not isinstance(rl, dict) or not parse_snapshot({"rate_limits": rl, "captured_at": 1}):
        return False
    t = now if now is not None else time.time()
    paths.write_json_atomic(path or paths.snapshot_path(), {"captured_at": t, "rate_limits": rl})
    try:
        record_history(rl, t, path)
    except Exception:
        pass  # history is best effort; the snapshot is already saved
    return True


def history_path(path=None) -> Path:
    """usage-history.jsonl beside the snapshot file."""
    return Path(path or paths.snapshot_path()).with_name("usage-history.jsonl")


def read_history(path=None) -> list[dict]:
    rows: list[dict] = []
    try:
        with open(history_path(path)) as fh:
            for ln in fh:
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(r, dict) and "t" in r:
                    rows.append(r)
    except OSError:
        pass
    return rows


def record_history(rl: dict, now: float, path=None) -> None:
    """Append {t, 5h: {used, resets_at}, 7d: {...}} at most every HISTORY_EVERY seconds."""
    rows = read_history(path)
    if rows and now - float(rows[-1]["t"]) < HISTORY_EVERY:
        return
    ms = parse_snapshot({"rate_limits": rl, "captured_at": now})
    if not ms:
        return
    rows = [r for r in rows if now - float(r["t"]) <= HISTORY_KEEP]
    rows.append({"t": round(now, 1), **{n: {"used": m.used, "resets_at": m.resets_at} for n, m in ms.items()}})
    p = history_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    with open(tmp, "w") as fh:
        fh.writelines(json.dumps(r) + "\n" for r in rows)
    os.replace(tmp, p)


def read_meters() -> dict[str, Meter]:
    """The saved status line reading. Empty means there is no usable reading."""
    return read_snapshot()
