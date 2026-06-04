"""Parse FAIL/SKIP events out of vtag run logs with full paths + reasons.

Log line formats produced by cli.py:
  <ts> [INFO] vtag: [N/T] OK <secs>s <basename> -- <summary>
  <ts> [INFO] vtag: [N/T] SKIP <full/path> (<reason>)
  <ts> [ERROR] vtag: [N/T] FAIL <full/path>: <error>

OK lines lose the full path (only basename) — for "latest tagged" use the
cache module instead.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Iterable

log = logging.getLogger("vtag-webui.events")

EVENT_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[,\.]\d+)?)"
    r"\s+\[(?P<lvl>[A-Z]+)\]\s+\S+:\s+"
    r"\[(?P<n>\d+)/(?P<t>\d+)\]\s+(?P<kind>OK|FAIL|SKIP)\s+(?P<tail>.+)$",
    re.M,
)
_FAIL_TAIL = re.compile(r"^(?P<path>.+?):\s+(?P<reason>.+)$")
_SKIP_TAIL = re.compile(r"^(?P<path>.+?)\s+\((?P<reason>[^)]+)\)\s*$")

DEFAULT_TAIL_BYTES = 1_000_000


def _read_tail(path: Path, tail_bytes: int) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
            return f.read().decode("utf-8", errors="replace")
    except OSError as exc:
        log.warning("log read failed for %s: %s", path, exc)
        return ""


def _parse_tail(kind: str, tail: str) -> tuple[str, str]:
    tail = tail.rstrip()
    if kind == "FAIL":
        m = _FAIL_TAIL.match(tail)
        if m:
            return m.group("path"), m.group("reason")
        return tail, ""
    if kind == "SKIP":
        m = _SKIP_TAIL.match(tail)
        if m:
            return m.group("path"), m.group("reason")
        return tail, ""
    return tail, ""


def iter_events(log_path: Path, *, kinds: set[str], tail_bytes: int = DEFAULT_TAIL_BYTES) -> Iterable[dict]:
    blob = _read_tail(log_path, tail_bytes)
    if not blob:
        return
    for m in EVENT_RE.finditer(blob):
        kind = m.group("kind")
        if kind not in kinds:
            continue
        path, reason = _parse_tail(kind, m.group("tail"))
        yield {
            "kind": kind,
            "ts": m.group("ts"),
            "n": int(m.group("n")),
            "total": int(m.group("t")),
            "path": path,
            "basename": os.path.basename(path) if path else "",
            "reason": reason,
        }


def latest_log(log_dir: Path) -> Path | None:
    if not log_dir.exists():
        return None
    candidates = sorted(log_dir.glob("run-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def latest_events(
    log_dir: Path,
    *,
    kind: str,
    limit: int = 100,
    tail_bytes: int = DEFAULT_TAIL_BYTES,
) -> list[dict]:
    log_path = latest_log(log_dir)
    if log_path is None:
        return []
    kinds = {kind.upper()}
    events = list(iter_events(log_path, kinds=kinds, tail_bytes=tail_bytes))
    if limit and len(events) > limit:
        events = events[-limit:]
    events.reverse()
    return events
