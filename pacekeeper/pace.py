"""Pacing arithmetic: how fast a window is being used versus how fast it can be."""
from __future__ import annotations

import os

from .meters import WINDOWS, Meter


def window_pace(m: Meter, now: float) -> dict:
    """Burn rate, sustainable rate and verdict for one meter, in percent per hour.

    The window start is resets_at minus the window length, which is exact.
    """
    total = WINDOWS[m.name]
    remaining = m.resets_at - now
    if remaining <= 0:
        return {"window": m.name, "expired": True, "used": m.used}
    elapsed = max(60.0, total - remaining)
    elapsed_h, remaining_h = elapsed / 3600, max(remaining, 60.0) / 3600
    fair = 100.0 * elapsed / total
    burn = m.used / elapsed_h
    target = max(0.0, 100.0 - m.used) / remaining_h
    over = m.used / fair if fair > 0 else 0.0
    res = {"window": m.name, "expired": False, "used": m.used, "fair_pct": round(fair, 1),
           "burn_pct_per_hour": round(burn, 2), "target_pct_per_hour": round(target, 2),
           "over_pace_factor": round(over, 2), "remaining_hours": round(remaining_h, 2)}
    if over > 1.05:
        res["verdict"] = "over pace"
    elif over < 0.8:
        res["verdict"] = "under pace"
    else:
        res["verdict"] = "on pace"
    if burn > target and burn > 0:
        gone = max(0.0, 100.0 - m.used) / burn
        res["exhausted_in_hours"] = round(gone, 2)
        res["exhausted_before_reset_hours"] = round(remaining_h - gone, 2)
    return res


def advice(p: dict) -> str:
    if p.get("expired"):
        return f"{p['window']}: reset time has passed; wait for a fresh reading"
    line = (f"{p['window']}: {p['used']:.0f}% used, {p['verdict']} "
            f"(burn {p['burn_pct_per_hour']} %/h, target {p['target_pct_per_hour']} %/h "
            f"for the next {p['remaining_hours']} h)")
    if "exhausted_in_hours" in p:
        line += (f"; at this burn the window is gone in {p['exhausted_in_hours']} h, "
                 f"{p['exhausted_before_reset_hours']} h before reset")
    return line


def tick_interval(progress: str, meters: dict[str, Meter], state: dict, now: float,
                  stale: bool = False) -> tuple[int, str, dict]:
    """Seconds between self-wakeups, a reason, and the new state.

    progress: "good", "bad" or "quiet", supplied by the caller.
    Rules: good or quiet = the base interval (default 3600). Bad = the minimum
    interval (default 900) for the first 60 minutes of a bad episode, then the
    base interval again. A 7d window used faster than its fair line, or a 5h
    window at or above PACEKEEPER_TICK_HIGH_5H (default 90) percent, doubles the
    interval with a floor of 1800. Never below the minimum.
    """
    base = int(os.environ.get("PACEKEEPER_TICK_BASE", "3600"))
    low = int(os.environ.get("PACEKEEPER_TICK_MIN", "900"))
    high5 = float(os.environ.get("PACEKEEPER_TICK_HIGH_5H", "90"))
    st = dict(state)
    if progress == "bad":
        start = st.get("stepin_start") or now
        st["stepin_start"] = start
        if now - start <= 3600:
            secs, why = low, "progress bad: stepping in (first hour)"
        else:
            secs, why = base, "progress bad over an hour: relax and hand the cause to someone else"
    else:
        st.pop("stepin_start", None)
        secs, why = base, "progress good" if progress == "good" else "quiet"
    if stale or not meters:
        why += "; usage unknown, no adjustment"
    else:
        m7, m5 = meters.get("7d"), meters.get("5h")
        p7 = window_pace(m7, now) if m7 else None
        over7 = bool(p7) and not p7["expired"] and p7["over_pace_factor"] > 1.0
        high = bool(m5) and m5.resets_at > now and m5.used >= high5
        if over7 or high:
            secs = max(1800, secs * 2)
            why += "; " + ("7d over its line" if over7 else "5h near its limit") + ", doubled"
    return max(secs, low), why, st


def budget(used: float, hours_left: float, target: float = 100.0,
           cost_per_turn: float | None = None) -> dict:
    """Allowed spend per hour to land on ``target`` percent at the reset.

    cost_per_turn is the percent of the window one turn costs; with it the
    result also says how many turns fit in the remaining room.
    """
    room = max(0.0, target - used)
    out = {"used": used, "target": target, "hours_left": hours_left, "room_pct": round(room, 2),
           "allowed_pct_per_hour": round(room / hours_left, 2) if hours_left > 0 else None}
    if cost_per_turn and cost_per_turn > 0:
        out["cost_per_turn_pct"] = round(cost_per_turn, 4)
        out["turns_left"] = int(room / cost_per_turn)
        if hours_left > 0:
            out["turns_per_hour"] = round(room / cost_per_turn / hours_left, 1)
    return out
