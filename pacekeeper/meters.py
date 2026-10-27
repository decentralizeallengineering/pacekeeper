"""Real 5h and 7d rate-limit readings.

The only source is the snapshot written by ``pacekeeper statusline``. Claude
Code passes the real ``rate_limits`` to the status line command on stdin;
nothing else persists them, so the statusline command saves them.

Percentages computed by tools that guess the quota from local history are not
the account's real figure, which is why this module derives none.
A snapshot is only as fresh as the last status line refresh, so every reading
carries its capture time and callers must treat an old one as unknown.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

from . import paths

WINDOWS = {"5h": 5 * 3600, "7d": 7 * 86400}
_KEYS = (("five_hour", "5h"), ("seven_day", "7d"))


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
    paths.write_json_atomic(path or paths.snapshot_path(),
                            {"captured_at": now if now is not None else time.time(), "rate_limits": rl})
    return True


def read_meters() -> dict[str, Meter]:
    """The saved status line reading. Empty means there is no usable reading."""
    return read_snapshot()
