# pacekeeper

Usage and pacing tools for Claude Code, in one stdlib-only Python CLI: `pacekeeper <cmd>`.

## Why

Tools that print a "percent of your limit" by guessing the quota from local history are often far off. The real 5h and 7d meter readings reach only the status line command, and nothing keeps them. pacekeeper saves them, refuses to present a frozen reading as current, and turns them into pacing advice (a percent-per-hour target), per-project token spend from your local transcripts, and a wakeup-interval policy.

## Install

    pip install .          # or run in place: python3 -m pacekeeper <cmd>

Python 3.9 or newer, no dependencies.

## Configure

1. Save the real meters. Set the Claude Code status line command to `pacekeeper statusline`. It reads the status line JSON on stdin, writes the `rate_limits` to a snapshot file, and prints `5h 12%  7d 40%`. To keep an existing status line, run `pacekeeper statusline --quiet` on the same stdin first.
2. Optional wind-down note: add `pacekeeper hook` as a `UserPromptSubmit` and `PostToolUse` hook command. Past a threshold it injects a one-line note into the session. It never blocks and exits 0.

Environment variables (all optional):

| Variable | Default | Meaning |
|---|---|---|
| `CLAUDE_CONFIG_DIR` | `~/.claude` | where transcripts (`projects/`) live |
| `PACEKEEPER_STATE_DIR` | `$XDG_STATE_HOME/pacekeeper` | snapshot, resets log, tick state, hook stamps |
| `PACEKEEPER_SNAPSHOT` | `<state>/usage-state.json` | snapshot file |
| `PACEKEEPER_MAX_AGE` | `180` | seconds before a reading counts as stale |
| `PACEKEEPER_TZ` | local zone | IANA zone for printed times |
| `PACEKEEPER_ROLES` | `<state>/roles.json` | JSON `{"session-id-prefix": "label"}` for `cost --by role` |
| `PACEKEEPER_CONFIG` | `<state>/config.json` | hook thresholds (below) |
| `PACEKEEPER_TICK_BASE` / `_MIN` / `_HIGH_5H` | `3600` / `900` / `90` | tick policy seconds and 5h percent |

Hook config, every key optional, with the defaults:

    {"tiers": [{"at": 80, "action": "wind down: finish the current task, start nothing new"},
               {"at": 95, "action": "pause: commit work and start no new agents until the reset"}],
     "throttle_seconds": 600, "final_window_minutes": 0, "max_age_seconds": 1800}

## Commands

| Command | What it does |
|---|---|
| `statusline` | status line command: save `rate_limits` from stdin |
| `usage [--json]` | 5h and 7d percent, reset times, pace. Exit 1 if no reading, 2 if stale |
| `pace [--json]` | per window: burn %/h, target %/h, verdict, when the window runs out |
| `budget [--used P --hours-left H --target T --cost-per-turn C]` | allowed %/h and turns left to land on T at the reset; missing values come from the meter and, for the cost per turn, from transcripts |
| `resets [--table] [--note TEXT]` | track window rollovers and peak percent in a log |
| `cost [hours] [--by project\|role\|session\|model]` | weighted token spend from transcripts |
| `audit [hours] [dir]` | token use of the sessions run from one folder, by kind of turn |
| `tick [--progress good\|bad\|quiet]` | seconds a session may sleep between self-wakeups |
| `hook` | the hook described above |

Spend weight is `input + 1.25 * cache write + 0.1 * cache read + 5 * output` tokens, a rough relative cost. Streamed repeats of one API call are de-duplicated by message id. The tick policy: good or quiet progress gives the base interval; bad progress gives the minimum interval for the first hour of an episode, then the base again; a 7d window used faster than its fair line, or a 5h window at 90 percent or more, doubles the interval with a floor of 1800 seconds.

## Example

    $ pacekeeper usage
    5h 83%  (resets in 1h59m, at 10-26 20:41)
    7d 40%  (resets in 3d11h, at 10-30 06:01)
    PACE  5h: 83% used, over pace (burn 27.66 %/h, target 8.5 %/h for the next 2.0 h); at this burn the window is gone in 0.61 h, 1.39 h before reset
    PACE  7d: 40% used, under pace (burn 0.47 %/h, target 0.72 %/h for the next 83.33 h)
    reading is 0s old

## Data sources

Meters come only from the status line JSON that Claude Code passes to the `statusline` command, and spend comes only from your local transcript files. pacekeeper makes no network calls and reads no login credentials.

## Tests

    python3 -m unittest discover -s tests

Five small tests run on synthetic transcript fixtures in `tests/fixtures`.

## Provenance

pacekeeper was written from scratch in Python for this repository. It is a rewrite of the author's own earlier private shell and Python scripts for the same jobs (usage reading, reset tracking, cost and token reporting, pacing, wakeup policy, and the wind-down hook). No code from third-party projects was copied. Parts of those scripts that depended on one private setup (session managers, role lists, service units, health checks, fixed dates and prices) were dropped rather than generalized.

## License

Apache License 2.0, see `LICENSE` and `NOTICE`. This project does not accept outside contributions; see `CONTRIBUTING.md`.
