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
import mimetypes
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from webui import cache as webui_cache
from webui import events as webui_events
from webui import thumbs as webui_thumbs

log = logging.getLogger("vtag-server")

STATIC_DIR = Path(__file__).resolve().parent / "webui" / "static"
FIND_SCRIPT = Path(__file__).resolve().parent / "find.py"

_STATIC_MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}

_reindex_state: dict = {"pid": None, "started_at": None}
_reindex_lock = threading.Lock()

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
        # mkv/webm cannot carry embedded XMP — their payload lives in a
        # sibling `<name>.xmp` sidecar. One sidecar with payload = one tagged
        # matroska media file.
        sidecar_result = subprocess.run(
            [
                et, "-config", str(EXIFTOOL_CONFIG),
                "-r", "-q", "-q",
                "-ext", "xmp",
                "-if", "$XMP-vtag:Payload",
                "-p", ".",
                str(TARGET_DIR),
            ],
            capture_output=True,
            timeout=900,
        )
        tagged += sidecar_result.stdout.count(b".\n")
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

    log_n = int(progress.get("last_n", 0)) if progress else 0
    log_total = int(progress.get("last_total", 0)) if progress else 0
    log_eta = progress.get("eta_seconds") if progress else None
    done = progress.get("done") if progress else None
    scan_ready = scan_at > 0

    if done:
        tagged_now: int | None = int(done.get("tagged", 0)) + int(done.get("skipped", 0))
        total_now = int(done.get("total", 0)) or (log_total or scan_total)
        eta_seconds = 0
    elif log_total > 0:
        tagged_now = log_n
        total_now = log_total
        eta_seconds = int(log_eta) if log_eta is not None else 0
    elif scan_ready:
        tagged_now = min(scan_total, scan_tagged) if scan_total else scan_tagged
        total_now = scan_total
        avg = progress.get("avg_seconds") if progress else None
        untagged_for_eta = max(0, total_now - tagged_now)
        eta_seconds = int(untagged_for_eta * avg) if (avg and untagged_for_eta) else 0
    else:
        tagged_now = None
        total_now = scan_total
        eta_seconds = 0

    untagged = max(0, total_now - tagged_now) if tagged_now is not None else 0

    active_target = state.get("target_dir") if running else None

    return {
        "running": running,
        "pid": pid if running else None,
        "target_dir": active_target or (str(TARGET_DIR) if TARGET_DIR else None),
        "log": str(log_file) if log_file else None,
        "started_at": state.get("started_at"),
        "stopped_at": None if running else state.get("stopped_at"),
        "progress": progress,
        "tagged_count": tagged_now,
        "total_count": total_now,
        "untagged_count": untagged,
        "scan_at": scan_at,
        "scan_in_flight": scan_in_flight,
        "eta_seconds": eta_seconds,
        "vtag_bin": VTAG_BIN,
    }


def _start(target: Path | None = None) -> tuple[int, dict]:
    target = target or TARGET_DIR
    if not target or not target.exists():
        return HTTPStatus.BAD_REQUEST, {
            "error": f"target not set or does not exist: {target!s}"
        }
    if not target.is_dir():
        return HTTPStatus.BAD_REQUEST, {"error": f"target is not a directory: {target!s}"}
    state = _read_state()
    if state.get("pid") and _pid_alive(int(state["pid"])):
        return HTTPStatus.CONFLICT, {"error": "run already active", "pid": state["pid"]}
    if not shutil.which(VTAG_BIN):
        return HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"vtag binary not on PATH: {VTAG_BIN}"}

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = LOG_DIR / f"run-{stamp}.log"

    cmd = [VTAG_BIN, "tag", "-r", str(target)]
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
        "target_dir": str(target),
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


def _reindex_status() -> dict:
    with _reindex_lock:
        pid = _reindex_state.get("pid")
        if pid and _pid_alive(int(pid)):
            return {"running": True, "pid": int(pid), "started_at": _reindex_state.get("started_at")}
        if pid:
            _reindex_state["pid"] = None
        return {"running": False, "pid": None, "started_at": _reindex_state.get("started_at")}


def _reindex_start() -> tuple[int, dict]:
    status = _reindex_status()
    if status["running"]:
        return HTTPStatus.CONFLICT, {"error": "reindex already running", **status}
    if not FIND_SCRIPT.exists():
        return HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"find.py not found at {FIND_SCRIPT}"}
    env = os.environ.copy()
    roots = env.get("VTAG_SEARCH_ROOTS")
    if not roots and TARGET_DIR:
        env["VTAG_SEARCH_ROOTS"] = str(TARGET_DIR)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    log_path = LOG_DIR / f"reindex-{stamp}.log"
    try:
        fh = open(log_path, "wb")
    except OSError as exc:
        return HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"log open failed: {exc}"}
    try:
        proc = subprocess.Popen(
            [sys.executable, str(FIND_SCRIPT), "--reindex", "--json", "--log-level", "INFO"],
            stdout=fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            env=env,
        )
    except OSError as exc:
        fh.close()
        return HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"spawn failed: {exc}"}
    finally:
        fh.close()
    with _reindex_lock:
        _reindex_state["pid"] = proc.pid
        _reindex_state["started_at"] = _now_iso()
        _reindex_state["log"] = str(log_path)
    return HTTPStatus.ACCEPTED, {"started": True, "pid": proc.pid, "log": str(log_path)}


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

    def _bytes(self, code: int, body: bytes, content_type: str, *, cache: str = "no-store") -> None:
        self.send_response(code)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", cache)
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, rel_path: str) -> None:
        rel_path = rel_path.lstrip("/")
        if not rel_path:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        target = (STATIC_DIR / rel_path).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self._json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
            return
        if not target.is_file():
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        suffix = target.suffix.lower()
        ctype = _STATIC_MIME.get(suffix) or mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        try:
            data = target.read_bytes()
        except OSError as exc:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"read failed: {exc}"})
            return
        self._bytes(HTTPStatus.OK, data, ctype, cache="no-cache, must-revalidate")

    def _serve_thumb(self, sha: str) -> None:
        sha = (sha or "").lower()
        if not re.fullmatch(r"[0-9a-f]{16,64}", sha):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid sha"})
            return
        existing = webui_thumbs.thumb_path(sha)
        if existing.exists() and existing.stat().st_size > 0:
            try:
                data = existing.read_bytes()
            except OSError as exc:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
                return
            self._bytes(HTTPStatus.OK, data, "image/jpeg", cache="public, max-age=86400")
            return
        conn = webui_cache.open_ro()
        if conn is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "cache not built"})
            return
        try:
            hit = webui_cache.by_sha(conn, sha)
        finally:
            conn.close()
        if hit is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "sha not in cache"})
            return
        source_path, _payload = hit
        out = webui_thumbs.ensure(sha, source_path)
        if out is None or not out.exists():
            self._json(HTTPStatus.NOT_FOUND, {"error": "thumbnail unavailable"})
            return
        try:
            data = out.read_bytes()
        except OSError as exc:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return
        self._bytes(HTTPStatus.OK, data, "image/jpeg", cache="public, max-age=86400")

    def _serve_raw(self, sha: str) -> None:
        sha = (sha or "").lower()
        if not re.fullmatch(r"[0-9a-f]{16,64}", sha):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid sha"})
            return
        conn = webui_cache.open_ro()
        if conn is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "cache not built"})
            return
        try:
            hit = webui_cache.by_sha(conn, sha)
        finally:
            conn.close()
        if hit is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "sha not in cache"})
            return
        source_path, _payload = hit
        p = Path(source_path)
        try:
            size = p.stat().st_size
        except OSError as exc:
            self._json(HTTPStatus.NOT_FOUND, {"error": f"file missing: {exc}"})
            return
        ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
        if p.suffix.lower() == ".mkv":
            ctype = "video/x-matroska"
        range_hdr = self.headers.get("Range")
        start, end = 0, size - 1
        status = HTTPStatus.OK
        if range_hdr:
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", range_hdr.strip())
            if not m or (m.group(1) == "" and m.group(2) == ""):
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("content-range", f"bytes */{size}")
                self.end_headers()
                return
            s, e = m.group(1), m.group(2)
            if s == "":
                n = int(e)
                start = max(0, size - n)
                end = size - 1
            else:
                start = int(s)
                end = int(e) if e else size - 1
                end = min(end, size - 1)
            if start > end or start >= size:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("content-range", f"bytes */{size}")
                self.end_headers()
                return
            status = HTTPStatus.PARTIAL_CONTENT
        length = end - start + 1
        self.send_response(status)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(length))
        self.send_header("accept-ranges", "bytes")
        self.send_header("cache-control", "private, max-age=3600")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("content-range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with open(p, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    buf = f.read(min(65536, remaining))
                    if not buf:
                        break
                    self.wfile.write(buf)
                    remaining -= len(buf)
        except (BrokenPipeError, ConnectionResetError):
            return
        except OSError as exc:
            log.warning("raw read error: %s", exc)
            return

    def _serve_recent(self, qs: dict[str, list[str]]) -> None:
        try:
            limit = max(1, min(200, int(qs.get("limit", ["60"])[0])))
            offset = max(0, int(qs.get("offset", ["0"])[0]))
        except ValueError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "limit/offset must be int"})
            return
        content_type = qs.get("content_type", [None])[0]
        conn = webui_cache.open_ro()
        if conn is None:
            self._json(HTTPStatus.OK, {"cache_available": False, "items": [], "meta": webui_cache.cache_meta(None)})
            return
        try:
            items = webui_cache.recent(conn, limit=limit, offset=offset, content_type=content_type)
            meta = webui_cache.cache_meta(conn)
        finally:
            conn.close()
        self._json(HTTPStatus.OK, {"cache_available": True, "items": items, "meta": meta})

    def _serve_search(self, qs: dict[str, list[str]]) -> None:
        try:
            limit = max(1, min(1000, int(qs.get("limit", ["300"])[0])))
        except ValueError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "limit must be int"})
            return
        query = qs.get("q", [""])[0] or ""
        content_type = qs.get("type", [None])[0]
        conn = webui_cache.open_ro()
        if conn is None:
            self._json(HTTPStatus.OK, {"cache_available": False, "items": [], "total": 0})
            return
        try:
            items, total = webui_cache.search(
                conn, query=query, content_type=content_type, limit=limit,
            )
        finally:
            conn.close()
        self._json(HTTPStatus.OK, {
            "cache_available": True,
            "items": items,
            "total": total,
            "returned": len(items),
        })

    def _serve_item(self, qs: dict[str, list[str]]) -> None:
        sha = (qs.get("sha", [""])[0] or "").lower()
        if not re.fullmatch(r"[0-9a-f]{16,64}", sha):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid sha"})
            return
        conn = webui_cache.open_ro()
        if conn is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "cache not built"})
            return
        try:
            hit = webui_cache.by_sha(conn, sha)
        finally:
            conn.close()
        if hit is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "sha not in cache"})
            return
        path, payload = hit
        self._json(HTTPStatus.OK, {"path": path, "payload": payload})

    def _serve_events(self, qs: dict[str, list[str]]) -> None:
        kind = (qs.get("kind", ["fail"])[0] or "fail").upper()
        if kind not in {"FAIL", "SKIP", "OK"}:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "kind must be fail|skip|ok"})
            return
        try:
            limit = max(1, min(500, int(qs.get("limit", ["100"])[0])))
        except ValueError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "limit must be int"})
            return
        events = webui_events.latest_events(LOG_DIR, kind=kind, limit=limit)
        log_path = webui_events.latest_log(LOG_DIR)
        self._json(HTTPStatus.OK, {
            "kind": kind,
            "items": events,
            "log": str(log_path) if log_path else None,
        })

    def _serve_cache_meta(self) -> None:
        conn = webui_cache.open_ro()
        try:
            meta = webui_cache.cache_meta(conn)
        finally:
            if conn is not None:
                conn.close()
        meta["reindex"] = _reindex_status()
        self._json(HTTPStatus.OK, meta)

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        path, qs = parsed.path, parse_qs(parsed.query)
        if path == "/" or path == "/index.html":
            self._serve_static("index.html")
            return
        if path.startswith("/static/"):
            self._serve_static(path[len("/static/"):])
            return
        if path == "/healthz":
            self._json(HTTPStatus.OK, {"ok": True})
            return
        if path == "/api/status":
            self._json(HTTPStatus.OK, _status())
            return
        if path == "/api/log":
            self._text(HTTPStatus.OK, _read_log_tail())
            return
        if path == "/api/recent":
            self._serve_recent(qs)
            return
        if path == "/api/search":
            self._serve_search(qs)
            return
        if path == "/api/item":
            self._serve_item(qs)
            return
        if path == "/api/thumb":
            self._serve_thumb(qs.get("sha", [""])[0])
            return
        if path == "/api/raw":
            self._serve_raw(qs.get("sha", [""])[0])
            return
        if path == "/api/events":
            self._serve_events(qs)
            return
        if path == "/api/cache_meta":
            self._serve_cache_meta()
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def _read_json_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            return {}
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == "/api/start":
            body_in = self._read_json_body()
            target_raw = body_in.get("target")
            target = Path(target_raw).expanduser() if target_raw else None
            code, body = _start(target)
            self._json(code, body)
            return
        if path == "/api/stop":
            code, body = _stop()
            self._json(code, body)
            return
        if path == "/api/reindex":
            code, body = _reindex_start()
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
