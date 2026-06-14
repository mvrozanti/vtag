"""Shared run-state for vtag tag runs.

A single JSON file (`run-state.json`) records the currently-active tag run so
that any launcher — the webui's POST /api/start, the CLI, or a systemd unit
wrapping the CLI — appears identically in the webui runner tab. The CLI writes
this file itself (via `begin`/`finish`); the server reads it for /api/status
and writes its own copy when it spawns a run.

Schema (kept byte-compatible with server.py's reader):
    pid, pgid, log, cmd, target_dir, started_at, stopped_at
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

STATE_DIR = Path(os.getenv("VTAG_STATE_DIR", str(Path.home() / ".local/share/vtag")))
LOG_DIR = Path(os.getenv("VTAG_LOG_DIR", str(STATE_DIR / "logs")))
STATE_FILE = STATE_DIR / "run-state.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def write(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, STATE_FILE)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _active(state: dict) -> bool:
    pid = state.get("pid")
    return bool(pid) and pid_alive(int(pid)) and state.get("stopped_at") is None


def begin(cmd: list[str], target_dir: str) -> Path | None:
    """Claim run-state for this process. Returns the log path to tee progress
    into, or None if another live run already owns the state (don't hijack it).
    """
    existing = read()
    if _active(existing) and int(existing["pid"]) != os.getpid():
        return None

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = LOG_DIR / f"run-{stamp}.log"
    write({
        "pid": os.getpid(),
        "pgid": os.getpgrp(),
        "log": str(log_path),
        "cmd": cmd,
        "target_dir": target_dir,
        "started_at": _now_iso(),
        "stopped_at": None,
    })
    return log_path


def finish(log_path: Path | None) -> None:
    """Mark this process's run as stopped, if we still own the state."""
    if log_path is None:
        return
    state = read()
    if state.get("pid") == os.getpid() and state.get("stopped_at") is None:
        state["stopped_at"] = _now_iso()
        write(state)
