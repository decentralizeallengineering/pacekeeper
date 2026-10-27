"""Track window rollovers: peak percent per meter and a plain-text log."""
from __future__ import annotations

import os
from pathlib import Path

from . import paths
from .meters import Meter


def state_file() -> Path:
    return Path(os.environ.get("PACEKEEPER_RESETS_STATE") or paths.state_dir() / "resets.json")


def log_file() -> Path:
    return Path(os.environ.get("PACEKEEPER_RESETS_LOG") or paths.state_dir() / "resets.log")


def _pct(v) -> str:
    return f"{v:.0f}%" if isinstance(v, (int, float)) else "?%"


def update(meters: dict[str, Meter], st: dict, now: float, write: bool = True) -> tuple[dict, list[str]]:
    """Fold the current readings into the state. Returns (state, rollover lines)."""
    lines: list[str] = []
    when = paths.fmt_time(now)
    for n, m in meters.items():
        rec = st.get(n, {})
        if write:
            if rec.get("resets_at") and m.resets_at > rec["resets_at"] + 60:
                lines.append(f"{when} ROLLOVER {n}: window ending {paths.fmt_time(rec['resets_at'])} peaked "
                             f"{_pct(rec.get('peak'))} (last sample {_pct(rec.get('last'))} at {rec.get('last_at', '?')}); "
                             f"new window ends {paths.fmt_time(m.resets_at)}, starts at {m.used:.0f}%")
                rec = {"resets_at": m.resets_at, "peak": m.used, "first_at": when}
            elif not rec.get("resets_at"):
                rec = {"resets_at": m.resets_at, "peak": m.used, "first_at": when}
            rec["peak"] = max(rec.get("peak", 0), m.used)
            rec.update(last=m.used, last_at=when, resets_at=m.resets_at)
            st[n] = rec
    return st, lines


def append_log(lines: list[str], meters: dict[str, Meter], now: float) -> None:
    p = log_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as fh:
        fh.writelines(ln + "\n" for ln in lines)
        fh.write(f"{paths.fmt_time(now)} SAMPLE " + " ".join(
            f"{n}={m.used:.0f}%@{paths.fmt_time(m.resets_at)}" for n, m in meters.items()) + "\n")


def note(text: str, now: float) -> None:
    p = log_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as fh:
        fh.write(f"{paths.fmt_time(now)} NOTE {text}\n")
