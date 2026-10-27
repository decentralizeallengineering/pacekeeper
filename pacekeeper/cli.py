"""pacekeeper command line: one entry point over the meters, spend and pacing modules."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import __version__, paths
from . import hook as hk
from . import meters as mt
from . import pace as pc
from . import resets as rs
from . import spend as sp


def _now() -> float:
    return float(os.environ.get("PACEKEEPER_NOW") or time.time())


def cmd_statusline(a) -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = {}
    saved = mt.write_snapshot(payload if isinstance(payload, dict) else {}, _now())
    if not a.quiet:
        m = mt.parse_snapshot({"rate_limits": (payload or {}).get("rate_limits"), "captured_at": 1}) if saved else {}
        print("  ".join(f"{n} {v.used:.0f}%" for n, v in m.items()) or "pacekeeper: no rate_limits yet")
    return 0


def _read(a, now):
    return mt.read_meters()


def cmd_usage(a) -> int:
    now = _now()
    ms = _read(a, now)
    if not ms:
        print(f"pacekeeper: no usable reading (snapshot: {paths.snapshot_path()})", file=sys.stderr)
        print("  Run `pacekeeper statusline` as your Claude Code status line command.", file=sys.stderr)
        return 1
    age = max(m.age(now) for m in ms.values())
    stale = age > mt.max_age()
    paces = {n: pc.window_pace(m, now) for n, m in ms.items()}
    if a.json:
        out = {"meters": {n: {"used_percentage": m.used, "resets_at": m.resets_at, "source": m.source}
                          for n, m in ms.items()},
               "pace": paces, "age_seconds": int(age), "stale": stale}
        print(json.dumps(out))
    else:
        for n, m in ms.items():
            print(f"{n} {m.used:.0f}%  (resets in {paths.fmt_left(m.resets_at - now)}, at {paths.fmt_time(m.resets_at)})")
        for p in paces.values():
            print("PACE  " + pc.advice(p))
        print(f"reading is {int(age)}s old")
    if stale:
        print(f"STALE: reading is {int(age)}s old (max {mt.max_age():.0f}s). The status line is not refreshing, "
              "so the number is frozen. Treat the budget as unknown.", file=sys.stderr)
        return 2
    return 0


def cmd_pace(a) -> int:
    now = _now()
    ms = _read(a, now)
    if not ms:
        print("pacekeeper: no usable reading", file=sys.stderr)
        return 1
    paces = {n: pc.window_pace(m, now) for n, m in ms.items()}
    if a.json:
        print(json.dumps(paces))
    else:
        for p in paces.values():
            print(pc.advice(p))
    return 0


def cmd_resets(a) -> int:
    now = _now()
    if a.note is not None:
        rs.note(a.note, now)
        print("noted")
        return 0
    ms = _read(a, now)
    st = paths.load_json(rs.state_file())
    st, lines = rs.update(ms, st, now, write=not a.table)
    print(f"{'meter':6} {'now%':>5} {'peak%':>5}  {'resets at':<12} {'left':>7}  age")
    for n, m in ms.items():
        left = int(m.resets_at - now)
        print(f"{n:6} {m.used:5.0f} {st.get(n, {}).get('peak', m.used):5.0f}  {paths.fmt_time(m.resets_at):<12} "
              f"{max(left, 0) // 3600:>4}h{max(left, 0) % 3600 // 60:02d}m  {int(m.age(now))}s")
    if not a.table and ms:
        paths.write_json_atomic(rs.state_file(), st)
        rs.append_log(lines, ms, now)
        for ln in lines:
            print(ln)
    return 0 if ms else 1


def cmd_cost(a) -> int:
    now = _now()
    rows, tot = sp.cost_report(paths.projects_dir(), a.hours, a.by, now, a.top)
    if a.json:
        print(json.dumps({"hours": a.hours, "by": a.by, "total_weight": tot, "rows": rows}))
        return 0
    print(f"last {a.hours:g}h  {a.by:<30}{'calls':>6}{'in+cw K':>9}{'cread K':>9}{'out K':>7}{'W K':>8}{'%':>5}")
    for r in rows:
        print(f"{'':<10}{r['key'][:30]:<30}{r['calls']:>6}{(r['input'] + r['cache_write']) / 1000:>9.0f}"
              f"{r['cache_read'] / 1000:>9.0f}{r['output'] / 1000:>7.0f}{r['weight'] / 1000:>8.0f}"
              f"{100 * r['weight'] / (tot or 1):>5.0f}")
    print(f"{'':<10}{'TOTAL':<30}{sum(r['calls'] for r in rows):>6}{'':>9}{'':>9}{'':>7}{tot / 1000:>8.0f}")
    return 0


def cmd_audit(a) -> int:
    r = sp.audit(paths.projects_dir(), a.dir, a.hours, _now())
    if a.json:
        print(json.dumps(r))
        return 0
    M = lambda n: f"{n / 1e6:.2f}M"
    t = r["totals"]
    print(f"audit {r['folder']}, last {a.hours:g} h ({r['files']} transcript files, {r['calls']} unique calls)")
    print(f"output {M(t['output'])} (subagents {M(r['subagent_output'])}), cache read {M(t['cache_read'])}, "
          f"cache write {M(t['cache_write'])}, fresh input {M(t['input'])}")
    if r["main_calls"]:
        print(f"main-session calls {r['main_calls']}, average context read per call {r['avg_context_read'] // 1000}K")
    for k in r["kinds"]:
        print(f"  after {k['kind']}: {k['calls']} calls, weight {M(k['weight'])}")
    print("Cut the top item if it is waste: chatter, polling, re-reading, oversized context, or blocked stops.")
    return 0


def cmd_tick(a) -> int:
    now = _now()
    ms = mt.read_meters()
    stale = bool(ms) and max(m.age(now) for m in ms.values()) > mt.max_age()
    sf = paths.state_dir() / "tick.json"
    secs, why, st = pc.tick_interval(a.progress, ms, paths.load_json(sf), now, stale)
    if not a.dry_run:
        paths.write_json_atomic(sf, st)
    print(secs, why)
    return 0


def cmd_hook(a) -> int:
    try:
        out = hk.run(sys.stdin.read(), _now())
        if out:
            print(out)
    except Exception:
        pass
    return 0


def cmd_budget(a) -> int:
    now = _now()
    used, hours = a.used, a.hours_left
    since = None
    if used is None or hours is None:
        ms = _read(a, now)
        m = ms.get(a.window)
        if not m or m.resets_at <= now:
            print(f"pacekeeper: no usable {a.window} reading; pass --used and --hours-left", file=sys.stderr)
            return 1
        used = m.used if used is None else used
        hours = (m.resets_at - now) / 3600 if hours is None else hours
        since = m.resets_at - mt.WINDOWS[a.window]
    cost = a.cost_per_turn
    if cost is None and used and not a.no_estimate:
        since = since if since is not None else now - mt.WINDOWS[a.window]
        n = sp.count_calls(paths.projects_dir(), since, now)
        cost = used / n if n else None
    r = pc.budget(used, hours, a.target, cost)
    if a.json:
        print(json.dumps(r))
        return 0
    if r["allowed_pct_per_hour"] is None:
        print("window is over: no hours left")
        return 0
    print(f"used {used:.0f}%, target {a.target:g}%, {hours:.2f} h left: allowed {r['allowed_pct_per_hour']} %/h "
          f"({r['room_pct']}% of room)")
    if "turns_left" in r:
        print(f"at {r['cost_per_turn_pct']}% per turn: {r['turns_left']} turns left, {r['turns_per_hour']} per hour")
    else:
        print("turns left: unknown (pass --cost-per-turn PCT, or have transcripts and a nonzero used%)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pacekeeper", description="Claude Code usage meters, spend and pacing advice.")
    p.add_argument("--version", action="version", version=f"pacekeeper {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="<cmd>")

    s = sub.add_parser("statusline", help="status line command: save rate_limits from stdin")
    s.add_argument("--quiet", action="store_true", help="print nothing")
    s.set_defaults(fn=cmd_statusline)

    s = sub.add_parser("usage", help="real 5h and 7d meters, reset times and pace; exit 2 if stale, 1 if missing")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_usage)

    s = sub.add_parser("pace", help="percent-per-hour target and verdict for each window")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_pace)

    s = sub.add_parser("resets", help="track window rollovers and peaks")
    s.add_argument("--table", action="store_true", help="print only, write nothing")
    s.add_argument("--note", metavar="TEXT", help="log a calibration note, for example a reading from the app")
    s.set_defaults(fn=cmd_resets)

    s = sub.add_parser("cost", help="weighted token spend from transcripts")
    s.add_argument("hours", nargs="?", type=float, default=24)
    s.add_argument("--by", choices=["project", "role", "session", "model"], default="project")
    s.add_argument("--top", type=int, default=18)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_cost)

    s = sub.add_parser("audit", help="token use of the sessions run from one folder")
    s.add_argument("hours", nargs="?", type=float, default=6)
    s.add_argument("dir", nargs="?", default=os.getcwd())
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_audit)

    s = sub.add_parser("hook", help="Claude Code hook: print a wind-down note when 5h usage is high")
    s.set_defaults(fn=cmd_hook)

    s = sub.add_parser("budget", help="allowed percent per hour and turns left to reach a target at the reset")
    s.add_argument("--used", type=float, help="percent used (default: from the meter)")
    s.add_argument("--hours-left", type=float, help="hours to the reset (default: from the meter)")
    s.add_argument("--target", type=float, default=100.0, help="percent to land on at the reset (default 100)")
    s.add_argument("--window", choices=["5h", "7d"], default="5h", help="meter to read missing values from")
    s.add_argument("--cost-per-turn", type=float, metavar="PCT", help="percent of the window one turn costs")
    s.add_argument("--no-estimate", action="store_true", help="do not estimate the cost per turn from transcripts")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_budget)

    s = sub.add_parser("tick", help="seconds a session may wait between self-wakeups, from current usage")
    s.add_argument("--progress", choices=["good", "bad", "quiet"], default="quiet")
    s.add_argument("--dry-run", action="store_true", help="do not save the step-in state")
    s.set_defaults(fn=cmd_tick)
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
