"""vtag-server: tiny HTTP control plane for vtag runs.

GET  /               HTML status page
GET  /api/status     JSON: active pid, progress, last result, target dir
POST /api/start      start a recursive tag run for VTAG_HUB_TARGET_DIR
POST /api/stop       SIGTERM the active run (and its process group)
GET  /api/log        plain-text tail of the active (or most recent) log
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger("vtag-server")

HOST = os.getenv("VTAG_HUB_LISTEN_HOST", "0.0.0.0")
PORT = int(os.getenv("VTAG_HUB_LISTEN_PORT", "8093"))
TARGET_DIR = Path(os.getenv("VTAG_HUB_TARGET_DIR", "")).expanduser()
VTAG_BIN = os.getenv("VTAG_HUB_VTAG_BIN", "vtag")
STATE_DIR = Path(os.getenv("VTAG_STATE_DIR", str(Path.home() / ".local/share/vtag")))
LOG_DIR = Path(os.getenv("VTAG_LOG_DIR", str(STATE_DIR / "logs")))
STATE_FILE = STATE_DIR / "run-state.json"
LOG_TAIL_BYTES = 32 * 1024

PROGRESS_RE = re.compile(r"\[(\d+)/(\d+)\]\s+(OK|FAIL|SKIP)\b")
OK_TIME_RE = re.compile(r"\[(\d+)/(\d+)\]\s+OK\s+([\d.]+)s\b")
DONE_RE = re.compile(r"done:\s+(\d+)\s+tagged,\s+(\d+)\s+skipped,\s+(\d+)\s+failed\s+\(total\s+(\d+)\)")

EXIFTOOL_CONFIG = Path(__file__).resolve().parent / "pipeline" / "exiftool_config.pl"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff", ".tif",
              ".mp4", ".mov", ".mkv", ".webm"}
SCAN_TTL_SECONDS = 600.0

_scan_cache = {"tagged": 0, "total": 0, "at": 0.0, "scanning": False}
_scan_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _count_images_under(root: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if os.path.splitext(name)[1].lower() in IMAGE_EXTS:
                total += 1
    return total


def _scan_target_blocking() -> None:
    if not TARGET_DIR or not TARGET_DIR.exists():
        with _scan_lock:
            _scan_cache.update(scanning=False)
        return
    et = shutil.which("exiftool")
    if not et:
        with _scan_lock:
            _scan_cache.update(scanning=False)
        return
    started = time.time()
    try:
        total = _count_images_under(TARGET_DIR)
        ext_args: list[str] = []
        for ext in IMAGE_EXTS:
            ext_args += ["-ext", ext.lstrip(".")]
        # One recursive exiftool invocation lets exiftool stream reads + filter
        # in a single process — much faster than chunking the file list through
        # python+subprocess on an HDD under concurrent vtag-tag load.
        result = subprocess.run(
            [
                et, "-config", str(EXIFTOOL_CONFIG),
                "-r", "-q", "-q",
                *ext_args,
                "-if", "$XMP-vtag:Payload",
                "-p", ".",
                str(TARGET_DIR),
            ],
            capture_output=True,
            timeout=900,
        )
        tagged = result.stdout.count(b".\n")
        with _scan_lock:
            _scan_cache.update(tagged=tagged, total=total, at=time.time(), scanning=False)
        log.info("scan: %d tagged / %d total in %.1fs", tagged, total, time.time() - started)
    except Exception as exc:
        log.warning("scan failed after %.1fs: %s", time.time() - started, exc)
        with _scan_lock:
            _scan_cache["scanning"] = False


def _kick_scan_if_stale() -> None:
    """Launch a background scan if cache is empty or older than SCAN_TTL_SECONDS."""
    with _scan_lock:
        if _scan_cache["scanning"]:
            return
        age = time.time() - _scan_cache["at"]
        if _scan_cache["at"] > 0 and age < SCAN_TTL_SECONDS:
            return
        _scan_cache["scanning"] = True
    threading.Thread(target=_scan_target_blocking, daemon=True).start()


def _read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, STATE_FILE)


def _pid_alive(pid: int) -> bool:
    try:
        wpid, _ = os.waitpid(pid, os.WNOHANG)
        if wpid == pid:
            return False
        if wpid == 0:
            return True
    except ChildProcessError:
        pass

    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


def _scan_log(path: Path) -> dict:
    out = {
        "last_n": 0, "last_total": 0,
        "ok": 0, "fail": 0, "skip": 0,
        "avg_seconds": None, "images_per_min": None, "eta_seconds": None,
        "done": None,
    }
    if not path.exists():
        return out
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > LOG_TAIL_BYTES:
                f.seek(size - LOG_TAIL_BYTES)
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return out
    for m in PROGRESS_RE.finditer(tail):
        out["last_n"] = max(out["last_n"], int(m.group(1)))
        out["last_total"] = max(out["last_total"], int(m.group(2)))
        kind = m.group(3)
        if kind == "OK":
            out["ok"] += 1
        elif kind == "FAIL":
            out["fail"] += 1
        elif kind == "SKIP":
            out["skip"] += 1
    times = [float(m.group(3)) for m in OK_TIME_RE.finditer(tail)]
    if times:
        recent = times[-40:]
        avg = sum(recent) / len(recent)
        out["avg_seconds"] = round(avg, 2)
        out["images_per_min"] = round(60.0 / avg, 2) if avg > 0 else None
        remaining = max(0, out["last_total"] - out["last_n"])
        out["eta_seconds"] = int(remaining * avg) if remaining and avg > 0 else 0
    dm = None
    for m in DONE_RE.finditer(tail):
        dm = m
    if dm:
        out["done"] = {
            "tagged": int(dm.group(1)),
            "skipped": int(dm.group(2)),
            "failed": int(dm.group(3)),
            "total": int(dm.group(4)),
        }
    return out


def _latest_log() -> Path | None:
    if not LOG_DIR.exists():
        return None
    logs = sorted(LOG_DIR.glob("run-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return logs[0] if logs else None


def _status() -> dict:
    state = _read_state()
    pid = state.get("pid")
    log_path = state.get("log")
    running = bool(pid) and _pid_alive(int(pid)) and state.get("stopped_at") is None
    log_file = Path(log_path) if log_path else _latest_log()
    progress = _scan_log(log_file) if log_file else {}

    _kick_scan_if_stale()
    with _scan_lock:
        scan_tagged = int(_scan_cache["tagged"])
        scan_total = int(_scan_cache["total"])
        scan_at = float(_scan_cache["at"])
        scan_in_flight = bool(_scan_cache["scanning"])

    new_ok = int(progress.get("ok", 0)) if progress else 0
    scan_ready = scan_at > 0
    if scan_ready:
        tagged_now: int | None = min(scan_total, scan_tagged + new_ok) if scan_total else (scan_tagged + new_ok)
        untagged = max(0, scan_total - tagged_now)
    else:
        tagged_now = None
        untagged = 0
    avg = progress.get("avg_seconds") if progress else None
    eta_seconds = int(untagged * avg) if (avg and untagged) else 0

    return {
        "running": running,
        "pid": pid if running else None,
        "target_dir": str(TARGET_DIR) if TARGET_DIR else None,
        "log": str(log_file) if log_file else None,
        "started_at": state.get("started_at"),
        "stopped_at": None if running else state.get("stopped_at"),
        "progress": progress,
        "tagged_count": tagged_now,
        "total_count": scan_total,
        "untagged_count": untagged,
        "scan_at": scan_at,
        "scan_in_flight": scan_in_flight,
        "eta_seconds": eta_seconds,
        "vtag_bin": VTAG_BIN,
    }


def _start() -> tuple[int, dict]:
    if not TARGET_DIR or not TARGET_DIR.exists():
        return HTTPStatus.BAD_REQUEST, {
            "error": f"VTAG_HUB_TARGET_DIR not set or does not exist: {TARGET_DIR!s}"
        }
    state = _read_state()
    if state.get("pid") and _pid_alive(int(state["pid"])):
        return HTTPStatus.CONFLICT, {"error": "run already active", "pid": state["pid"]}
    if not shutil.which(VTAG_BIN):
        return HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"vtag binary not on PATH: {VTAG_BIN}"}

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = LOG_DIR / f"run-{stamp}.log"

    cmd = [VTAG_BIN, "tag", "-r", str(TARGET_DIR)]
    fh = log_path.open("w")
    proc = subprocess.Popen(
        cmd,
        stdout=fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )

    new_state = {
        "pid": proc.pid,
        "pgid": proc.pid,
        "log": str(log_path),
        "cmd": cmd,
        "target_dir": str(TARGET_DIR),
        "started_at": _now_iso(),
        "stopped_at": None,
    }
    _write_state(new_state)
    log.info("started vtag run pid=%d log=%s", proc.pid, log_path)
    return HTTPStatus.ACCEPTED, {"pid": proc.pid, "log": str(log_path)}


def _stop() -> tuple[int, dict]:
    state = _read_state()
    pid = state.get("pid")
    if not pid or not _pid_alive(int(pid)):
        return HTTPStatus.NOT_FOUND, {"error": "no active run"}
    try:
        os.killpg(int(state.get("pgid", pid)), signal.SIGTERM)
    except (ProcessLookupError, PermissionError) as exc:
        return HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"kill failed: {exc}"}
    for _ in range(20):
        time.sleep(0.25)
        if not _pid_alive(int(pid)):
            break
    else:
        try:
            os.killpg(int(state.get("pgid", pid)), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    state["stopped_at"] = _now_iso()
    _write_state(state)
    return HTTPStatus.OK, {"stopped": True, "pid": pid}


def _read_log_tail() -> str:
    state = _read_state()
    log_path = Path(state.get("log") or "")
    if not log_path.exists():
        latest = _latest_log()
        if latest is None:
            return ""
        log_path = latest
    try:
        size = log_path.stat().st_size
        with log_path.open("rb") as f:
            if size > LOG_TAIL_BYTES:
                f.seek(size - LOG_TAIL_BYTES)
            return f.read().decode("utf-8", errors="replace")
    except OSError as exc:
        return f"(log read failed: {exc})"


HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>vtag.mvr.ac · runner</title>
<style>
:root { --bg:#0e0f12; --fg:#d9d9d9; --muted:#888; --accent:#6cf; --hover:#1a1c22; --border:#2a2c33; }
* { box-sizing: border-box; }
body { font-family: system-ui, sans-serif; background: var(--bg); color: var(--fg); margin: 0; padding: 24px; line-height: 1.45; }
h1 { margin: 0 0 4px 0; font-size: 1.4rem; }
.muted { color: var(--muted); }
.row { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; margin: 12px 0; }
.card { background: var(--hover); border: 1px solid var(--border); border-radius: 8px; padding: 16px; margin: 12px 0; }
button { background: #1a1c22; color: var(--fg); border: 1px solid var(--border); border-radius: 6px; padding: 8px 14px; font-size: 0.95rem; cursor: pointer; }
button:hover { border-color: var(--accent); }
button[disabled] { opacity: 0.4; cursor: not-allowed; }
.bar { background: #2a2c33; height: 12px; border-radius: 6px; overflow: hidden; flex: 1; min-width: 200px; }
.bar-fill { background: var(--accent); height: 100%; width: 0%; transition: width 0.3s; }
.kv { display: grid; grid-template-columns: 110px 1fr; gap: 4px 12px; font-size: 0.9rem; }
.kv .k { color: var(--muted); }
pre { background: #08090b; border: 1px solid var(--border); border-radius: 6px; padding: 12px; overflow: auto; max-height: 380px; font-size: 0.82rem; }
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; vertical-align: middle; }
.dot.on { background: #4ade80; }
.dot.off { background: #555; }
</style>
</head>
<body>
<h1>vtag · runner</h1>
<div class="muted">local VLM image tagger · status / restart</div>

<div class="card">
  <div class="row">
    <div><span class="dot off" id="dot"></span><span id="state">…</span></div>
    <div style="flex:1"></div>
    <button id="btn-start">start run</button>
    <button id="btn-stop" disabled>stop run</button>
    <button id="btn-refresh">refresh</button>
  </div>
  <div class="row">
    <div class="bar"><div class="bar-fill" id="bar"></div></div>
    <div class="muted" id="progress">–</div>
  </div>
  <div class="kv">
    <div class="k">target</div><div id="target">–</div>
    <div class="k">pid</div><div id="pid">–</div>
    <div class="k">started</div><div id="started">–</div>
    <div class="k">log</div><div id="log">–</div>
    <div class="k">ok / fail / skip</div><div id="counts">–</div>
    <div class="k">avg / rate</div><div id="speed">–</div>
    <div class="k">eta</div><div id="eta">–</div>
    <div class="k">done</div><div id="done">–</div>
  </div>
</div>

<div class="card">
  <div class="row" style="justify-content:space-between"><b>log tail</b><span class="muted" id="log-meta"></span></div>
  <pre id="log-pre">(loading)</pre>
</div>

<script>
function fmtDuration(s) {
  s = Math.round(s);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${sec}s`;
  return `${sec}s`;
}
async function api(path, opts) {
  const r = await fetch(path, opts);
  return [r.status, await r.json().catch(() => ({}))];
}
async function logTail() {
  const r = await fetch('/api/log');
  return await r.text();
}
async function refresh() {
  const [_s, st] = await api('/api/status');
  document.getElementById('state').textContent = st.running ? 'running' : 'idle';
  document.getElementById('dot').className = 'dot ' + (st.running ? 'on' : 'off');
  document.getElementById('target').textContent = st.target_dir || '(unset)';
  document.getElementById('pid').textContent = st.pid || '–';
  document.getElementById('started').textContent = st.started_at || '–';
  document.getElementById('log').textContent = st.log || '–';
  const p = st.progress || {};
  document.getElementById('counts').textContent = `${p.ok||0} / ${p.fail||0} / ${p.skip||0}`;
  const tagged = st.tagged_count || 0;
  const total = st.total_count || 0;
  if (total) {
    const pct = Math.round(100 * tagged / total);
    document.getElementById('bar').style.width = pct + '%';
    const flight = st.scan_in_flight ? ' · rescanning' : '';
    document.getElementById('progress').textContent = `${tagged} / ${total} tagged (${pct}%)${flight}`;
  } else {
    document.getElementById('bar').style.width = '0%';
    document.getElementById('progress').textContent = st.scan_in_flight ? 'scanning…' : '–';
  }
  document.getElementById('speed').textContent = (p.avg_seconds && p.images_per_min)
    ? `${p.avg_seconds.toFixed(1)}s/img · ${p.images_per_min.toFixed(1)} img/min`
    : '–';
  const eta = st.eta_seconds != null && st.eta_seconds > 0 ? st.eta_seconds : 0;
  document.getElementById('eta').textContent = eta > 0 ? fmtDuration(eta) : '–';
  document.getElementById('done').textContent = p.done
    ? `tagged ${p.done.tagged} · skipped ${p.done.skipped} · failed ${p.done.failed} · total ${p.done.total}`
    : '–';
  document.getElementById('btn-start').disabled = st.running;
  document.getElementById('btn-stop').disabled = !st.running;
  document.getElementById('log-meta').textContent = st.log ? st.log.split('/').pop() : '';
  document.getElementById('log-pre').textContent = await logTail();
}
document.getElementById('btn-start').onclick = async () => {
  const [s, b] = await api('/api/start', {method:'POST'});
  if (s >= 400) alert(b.error || 'start failed');
  await refresh();
};
document.getElementById('btn-stop').onclick = async () => {
  const [s, b] = await api('/api/stop', {method:'POST'});
  if (s >= 400) alert(b.error || 'stop failed');
  await refresh();
};
document.getElementById('btn-refresh').onclick = refresh;
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "vtag-server/0.1"

    def log_message(self, fmt: str, *args) -> None:
        log.info("%s - %s", self.client_address[0], fmt % args)

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _text(self, code: int, body: str, content_type: str = "text/plain; charset=utf-8") -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(data)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/" or self.path == "/index.html":
            self._text(HTTPStatus.OK, HTML, "text/html; charset=utf-8")
            return
        if self.path == "/healthz":
            self._json(HTTPStatus.OK, {"ok": True})
            return
        if self.path == "/api/status":
            self._json(HTTPStatus.OK, _status())
            return
        if self.path == "/api/log":
            self._text(HTTPStatus.OK, _read_log_tail())
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path == "/api/start":
            code, body = _start()
            self._json(code, body)
            return
        if self.path == "/api/stop":
            code, body = _stop()
            self._json(code, body)
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})


def main() -> int:
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log.info("vtag-server listening on %s:%d (target=%s)", HOST, PORT, TARGET_DIR or "<unset>")
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
