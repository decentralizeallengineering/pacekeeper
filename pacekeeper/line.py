"""The linear line for the 7d window: how far usage is ahead of an even burn to the reset.

The window runs from the last reset to the next one, as set by ``reset`` in the
config (for example ``"Wed 12:59 America/Los_Angeles"``, or env PACEKEEPER_RESET).
A straight line runs from 0 percent at the window start to 100 percent at its end.
``delta`` is used minus that line, in points. ``burn`` comes from the snapshot
history (the last 12 hours of this window, else the average since the window
start). ``hours_to_line`` is how long that burn takes to cross the line: 0 when
already over, None when the burn is at or below the line's own rate.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import meters as mt
from . import paths

DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
WEEK_S = 7 * 86400
LOOKBACK_S = 12 * 3600
MIN_SPAN_S = 3600
DEFAULT_MARGIN = 2.0


def _config() -> dict:
    from .hook import config_path
    d = paths.load_json(config_path())
    return d if isinstance(d, dict) else {}


def reset_spec() -> str | None:
    return os.environ.get("PACEKEEPER_RESET") or _config().get("reset") or None


def margin() -> float:
    v = os.environ.get("PACEKEEPER_MARGIN") or _config().get("margin")
    try:
        return float(v) if v is not None else DEFAULT_MARGIN
    except (TypeError, ValueError):
        return DEFAULT_MARGIN


def parse_reset(spec: str):
    """(weekday 0-6 Monday first, hour, minute, tzinfo or None) from 'Wed 12:59 America/Los_Angeles'."""
    parts = str(spec).split()
    if len(parts) < 2 or parts[0][:3].lower() not in DAYS:
        raise ValueError(f"reset must look like 'Wed 12:59 America/Los_Angeles', got {spec!r}")
    hh, sep, mm = parts[1].partition(":")
    if not sep or not hh.isdigit() or not mm.isdigit():
        raise ValueError(f"reset time must be HH:MM, got {parts[1]!r}")
    hour, minute = int(hh), int(mm)
    if hour > 23 or minute > 59:
        raise ValueError(f"reset time out of range: {parts[1]!r}")
    tz = ZoneInfo(" ".join(parts[2:])) if len(parts) > 2 else None
    return DAYS[parts[0][:3].lower()], hour, minute, tz


def window(spec: str, now: float) -> tuple[float, float]:
    """(start, end) epoch seconds of the 7d window that contains now."""
    wd, hour, minute, tz = parse_reset(spec)
    tz = tz or paths.tzinfo()
    wall = datetime.fromtimestamp(now, tz).replace(tzinfo=None)
    start = wall.replace(hour=hour, minute=minute, second=0, microsecond=0) \
        - timedelta(days=(wall.weekday() - wd) % 7)
    if start.replace(tzinfo=tz).timestamp() > now:
        start -= timedelta(days=7)
    end = start + timedelta(days=7)
    return start.replace(tzinfo=tz).timestamp(), end.replace(tzinfo=tz).timestamp()


def history_points(rows: list[dict], end: float) -> list[tuple[float, float]]:
    """(t, used) samples of the 7d meter that belong to the window ending at `end`."""
    out = []
    for r in rows:
        m = r.get("7d")
        try:
            if isinstance(m, dict) and abs(float(m["resets_at"]) - end) < 60:
                out.append((float(r["t"]), float(m["used"])))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out)


def burn(points, start: float, now: float, used: float, elapsed_s: float) -> tuple[float | None, str]:
    """Percent per hour, and where it came from."""
    lo = max(start, now - LOOKBACK_S)
    pts = [(t, u) for t, u in points if lo <= t <= now] + [(now, used)]
    span = pts[-1][0] - pts[0][0]
    if span >= MIN_SPAN_S:
        return max(0.0, (pts[-1][1] - pts[0][1]) / (span / 3600)), f"history over the last {span / 3600:.1f} h"
    if elapsed_s >= MIN_SPAN_S:
        return used / (elapsed_s / 3600), "average since the window start"
    return None, "too little data"


def status(now: float, meters: dict | None = None, spec: str | None = None,
           history: list[dict] | None = None) -> dict | None:
    """The line numbers for now, or None when there is no 7d reading.

    Raises ValueError when no reset is configured or it does not parse.
    """
    spec = spec or reset_spec()
    if not spec:
        raise ValueError("no reset configured: set \"reset\" in the config, e.g. \"Wed 12:59 America/Los_Angeles\"")
    ms = mt.read_meters() if meters is None else meters
    m = ms.get("7d")
    if not m:
        return None
    start, end = window(spec, now)
    total, elapsed = end - start, now - start
    linear = 100.0 * elapsed / total
    delta = m.used - linear
    line_rate = 100.0 * 3600 / total
    rows = mt.read_history() if history is None else history
    b, basis = burn(history_points(rows, end), start, now, m.used, elapsed)
    hours = None
    if delta > 0:
        hours = 0.0
    elif b is not None and b > line_rate:
        hours = (linear - m.used) / (b - line_rate)
    age = m.age(now)
    mg = margin()
    out = {
        "reset": spec, "window_start": start, "window_end": end,
        "used": m.used, "linear": round(linear, 2), "delta": round(delta, 2),
        "over": delta > mg, "margin": mg,
        "burn_pct_per_hour": None if b is None else round(b, 2), "burn_basis": basis,
        "line_pct_per_hour": round(line_rate, 3),
        "hours_to_line": None if hours is None else round(hours, 2),
        "age_seconds": int(age), "stale": age > mt.max_age(),
        "meter_resets_at": m.resets_at,
    }
    if abs(m.resets_at - end) > 3600:
        out["reset_mismatch_hours"] = round((m.resets_at - end) / 3600, 2)
    return out


def render(st: dict) -> list[str]:
    """Plain-text lines for `pacekeeper line`."""
    sign = "+" if st["delta"] >= 0 else ""
    where = "OVER the line" if st["over"] else "at or under the line"
    lines = [f"7d {st['used']:.1f}% used, linear {st['linear']:.1f}%, delta {sign}{st['delta']:.1f} pts "
             f"({where}, margin {st['margin']:g})"]
    if st["burn_pct_per_hour"] is None:
        lines.append(f"burn unknown ({st['burn_basis']})")
    else:
        lines.append(f"burn {st['burn_pct_per_hour']:.2f} %/h ({st['burn_basis']}), line rate {st['line_pct_per_hour']:.2f} %/h")
    if st["delta"] > 0:
        lines.append("already over the line")
    elif st["hours_to_line"] is not None:
        lines.append(f"at this burn it crosses the line in {st['hours_to_line']:.1f} h")
    else:
        lines.append("at this burn it does not cross the line")
    lines.append(f"reset {st['reset']}: window {paths.fmt_time(st['window_start'])} to "
                 f"{paths.fmt_time(st['window_end'])}; reading is {st['age_seconds']}s old")
    if "reset_mismatch_hours" in st:
        lines.append(f"note: the meter's reset differs from the config by {st['reset_mismatch_hours']:+.1f} h; "
                     "check the reset setting")
    return lines
