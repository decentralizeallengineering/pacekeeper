"""Locations, time formatting and small file helpers shared by every command."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path


def config_dir() -> Path:
    """Claude Code config directory: $CLAUDE_CONFIG_DIR, else ~/.claude."""
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def projects_dir() -> Path:
    return config_dir() / "projects"


def state_dir() -> Path:
    """pacekeeper's own state: $PACEKEEPER_STATE_DIR, else $XDG_STATE_HOME/pacekeeper."""
    env = os.environ.get("PACEKEEPER_STATE_DIR")
    if env:
        return Path(env)
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "pacekeeper"


def snapshot_path() -> Path:
    return Path(os.environ.get("PACEKEEPER_SNAPSHOT") or state_dir() / "usage-state.json")


def tzinfo():
    """$PACEKEEPER_TZ (an IANA name) if valid, else the machine's local zone."""
    name = os.environ.get("PACEKEEPER_TZ")
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:
            pass
    return datetime.now().astimezone().tzinfo


def fmt_time(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tzinfo()).strftime("%m-%d %H:%M")


def fmt_left(seconds: float) -> str:
    r = int(seconds)
    if r <= 0:
        return "now"
    if r >= 86400:
        return f"{r // 86400}d{r % 86400 // 3600}h"
    if r >= 3600:
        return f"{r // 3600}h{r % 3600 // 60}m"
    return f"{r // 60}m"


def load_json(path, default=None):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {} if default is None else default


def write_json_atomic(path, obj) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(obj, fh)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
