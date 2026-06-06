"""In-file metadata: read/write the vtag payload as XMP via exiftool.

Two execution modes:
  * One-shot subprocess per call (default for ad-hoc CLI invocations).
  * Persistent `exiftool -stay_open True -@ -` daemon (started by long runs
    via ``start_daemon()``) — avoids the ~100 ms cold-spawn cost per file.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
import threading
from dataclasses import asdict
from pathlib import Path

from . import schema

log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent / "exiftool_config.pl"

# exiftool can embed XMP directly into these container formats.
XMP_EMBED_FORMATS = frozenset({
    "JPEG", "PNG", "WEBP", "TIFF", "GIF", "MP4", "MOV",
})
# exiftool refuses to write into matroska — payload goes into a `<file>.xmp`
# sidecar instead.
XMP_SIDECAR_FORMATS = frozenset({"MKV", "WEBM"})
XMP_WRITABLE_FORMATS = XMP_EMBED_FORMATS | XMP_SIDECAR_FORMATS

SIDECAR_EXTS = frozenset({".mkv", ".webm"})
SIDECAR_SUFFIX = ".xmp"


def xmp_target(media_path: Path) -> Path:
    """Path that actually carries the XMP payload for this media file.

    Matroska always uses a `<name>.xmp` sidecar. For embeddable containers
    the file itself, unless a sibling sidecar already exists — which happens
    when MP4/MOV embedded writes fail (e.g. fragmented `sidx`) and fall back
    to a sidecar. Once a sidecar exists, it wins on read and on rewrite.
    """
    if media_path.suffix.lower() in SIDECAR_EXTS:
        return media_path.with_name(media_path.name + SIDECAR_SUFFIX)
    sidecar = media_path.with_name(media_path.name + SIDECAR_SUFFIX)
    if sidecar.exists():
        return sidecar
    return media_path


def _sidecar_path(media_path: Path) -> Path:
    return media_path.with_name(media_path.name + SIDECAR_SUFFIX)


def is_sidecar_format(media_path: Path) -> bool:
    return media_path.suffix.lower() in SIDECAR_EXTS


class MetadataError(RuntimeError):
    pass


def _exiftool() -> str:
    path = shutil.which("exiftool")
    if not path:
        raise MetadataError("exiftool not in PATH")
    return path


def _encode_payload(tagged: schema.TaggedImage) -> str:
    raw = json.dumps(asdict(tagged), ensure_ascii=False, sort_keys=False).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def _decode_payload(b64: str) -> schema.TaggedImage:
    raw = base64.b64decode(b64.encode("ascii"))
    data = json.loads(raw.decode("utf-8"))
    src = schema.Source(**data.pop("source", {}))
    mdl = schema.Model(**data.pop("model", {}))
    return schema.TaggedImage(source=src, model=mdl, **data)


def _build_write_args(
    image_path: Path,
    tagged: schema.TaggedImage,
    *,
    target_override: Path | None = None,
) -> list[str]:
    description = tagged.description or ""
    title_bits = [tagged.content_type]
    if tagged.template:
        title_bits.append(tagged.template)
    if tagged.category:
        title_bits.append(tagged.category)
    title = " / ".join(b for b in title_bits if b)

    args: list[str] = [
        "-overwrite_original",
        "-m",
        "-q", "-q",
        "-codedcharacterset=utf8",
        "-XMP-dc:Subject=",
        "-IPTC:Keywords=",
    ]
    for tag in tagged.tags:
        args.append(f"-XMP-dc:Subject+={tag}")
        args.append(f"-IPTC:Keywords+={tag}")
    if description:
        args.append(f"-XMP-dc:Description={description}")
        args.append(f"-EXIF:ImageDescription={description}")
    if title:
        args.append(f"-XMP-dc:Title={title}")

    args.append(f"-XMP-vtag:Sha256={tagged.source.sha256}")
    args.append(f"-XMP-vtag:SchemaVersion={tagged.schema_version}")
    args.append(f"-XMP-vtag:Payload={_encode_payload(tagged)}")
    target = target_override if target_override is not None else xmp_target(image_path)
    args.append(str(target))
    return args


def _build_read_args(image_path: Path) -> list[str]:
    return [
        "-j", "-G1", "-s",
        "-XMP-vtag:Payload",
        str(xmp_target(image_path)),
    ]


class ExiftoolDaemon:
    """Persistent ``exiftool -stay_open True`` process.

    Thread-safe: each ``execute`` call takes an internal lock so concurrent
    callers don't interleave commands on the shared stdin/stdout pipes.
    """

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._seq = 0
        self._stderr_path: Path | None = None
        self._stderr_offset = 0

    def start(self) -> None:
        with self._lock:
            if self._proc and self._proc.poll() is None:
                return
            self._spawn_locked()

    def _spawn_locked(self) -> None:
        et = _exiftool()
        # exiftool needs a real file (or /dev/null) for stderr because we read
        # delimited output from stdout; stderr is captured to a tempfile we
        # tail per-execute to surface write errors.
        stderr_file = Path(os.environ.get("TMPDIR", "/tmp")) / f"vtag-exiftool-{os.getpid()}.err"
        try:
            stderr_file.unlink()
        except FileNotFoundError:
            pass
        stderr_file.touch()
        self._stderr_path = stderr_file
        self._stderr_offset = 0
        cmd = [
            et,
            "-config", str(CONFIG_PATH),
            "-stay_open", "True",
            "-@", "-",
        ]
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=open(stderr_file, "ab"),
            text=True,
            bufsize=1,
        )
        self._seq = 0
        log.info("exiftool daemon started (pid=%s)", self._proc.pid)

    def stop(self) -> None:
        with self._lock:
            proc = self._proc
            if proc is None:
                return
            self._proc = None
            try:
                if proc.poll() is None:
                    assert proc.stdin is not None
                    proc.stdin.write("-stay_open\nFalse\n")
                    proc.stdin.flush()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=2)
            except Exception as exc:
                log.warning("exiftool daemon shutdown failed: %s", exc)
            finally:
                try:
                    if proc.stdin and not proc.stdin.closed:
                        proc.stdin.close()
                except Exception:
                    pass
                if self._stderr_path:
                    try:
                        self._stderr_path.unlink()
                    except FileNotFoundError:
                        pass
                    self._stderr_path = None

    def execute(self, args: list[str], *, timeout: float = 60.0) -> tuple[str, str]:
        """Run one ``-execute<n>`` block. Returns ``(stdout_text, stderr_text)``.

        Raises ``MetadataError`` if the daemon is dead and respawn fails twice
        in a row, or if no ``{ready<n>}`` marker appears within ``timeout``.
        """
        last_exc: Exception | None = None
        for attempt in range(2):
            with self._lock:
                if self._proc is None or self._proc.poll() is not None:
                    self._spawn_locked()
                self._seq += 1
                seq = self._seq
                ready = f"{{ready{seq}}}"
                proc = self._proc
                assert proc is not None and proc.stdin is not None and proc.stdout is not None
                try:
                    payload = "\n".join(args) + f"\n-execute{seq}\n"
                    proc.stdin.write(payload)
                    proc.stdin.flush()
                    stderr_pre = self._stderr_size_locked()
                    deadline = _monotonic() + timeout
                    chunks: list[str] = []
                    while True:
                        line = proc.stdout.readline()
                        if not line:
                            raise MetadataError("exiftool daemon closed stdout")
                        if line.rstrip("\r\n") == ready:
                            break
                        chunks.append(line)
                        if _monotonic() > deadline:
                            raise MetadataError(f"exiftool daemon timed out after {timeout}s")
                    stderr_text = self._read_stderr_since_locked(stderr_pre)
                    return "".join(chunks), stderr_text
                except (BrokenPipeError, MetadataError, OSError) as exc:
                    last_exc = exc
                    log.warning("exiftool daemon failure (attempt %d): %s", attempt + 1, exc)
                    self._kill_locked()
                    continue
        raise MetadataError(f"exiftool daemon unusable: {last_exc}")

    def _stderr_size_locked(self) -> int:
        if not self._stderr_path:
            return 0
        try:
            return self._stderr_path.stat().st_size
        except FileNotFoundError:
            return 0

    def _read_stderr_since_locked(self, start: int) -> str:
        if not self._stderr_path:
            return ""
        try:
            with self._stderr_path.open("rb") as f:
                f.seek(start)
                return f.read().decode("utf-8", errors="replace")
        except OSError:
            return ""

    def _kill_locked(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2)
        except Exception:
            pass


def _monotonic() -> float:
    import time
    return time.monotonic()


_DAEMON: ExiftoolDaemon | None = None
_DAEMON_LOCK = threading.Lock()


def start_daemon() -> None:
    global _DAEMON
    with _DAEMON_LOCK:
        if _DAEMON is None:
            _DAEMON = ExiftoolDaemon()
        _DAEMON.start()


def stop_daemon() -> None:
    global _DAEMON
    with _DAEMON_LOCK:
        if _DAEMON is None:
            return
        _DAEMON.stop()
        _DAEMON = None


def _daemon() -> ExiftoolDaemon | None:
    return _DAEMON


def _run_oneshot(args: list[str], *, timeout: float) -> tuple[int, bytes, bytes]:
    et = _exiftool()
    cmd = [et, "-config", str(CONFIG_PATH), *args]
    result = subprocess.run(cmd, capture_output=True, timeout=timeout)
    return result.returncode, result.stdout, result.stderr


def _read_payload_field(image_path: Path) -> str | None:
    target = xmp_target(image_path)
    if not target.exists():
        return None
    args = _build_read_args(image_path)
    daemon = _daemon()
    try:
        if daemon is not None:
            stdout_text, stderr_text = daemon.execute(args, timeout=30)
            if stderr_text.strip():
                log.debug("exiftool read stderr on %s: %s", image_path, stderr_text.strip()[:200])
            stdout = stdout_text.encode("utf-8")
        else:
            rc, stdout, stderr = _run_oneshot(args, timeout=30)
            if rc != 0:
                log.debug("exiftool nonzero on %s: %s", image_path, stderr.decode(errors="replace"))
                return None
    except Exception as exc:
        log.debug("exiftool read failed on %s: %s", image_path, exc)
        return None
    try:
        rows = json.loads(stdout.decode("utf-8") or "[]")
    except json.JSONDecodeError:
        return None
    if not rows:
        return None
    row = rows[0]
    return row.get("XMP-vtag:Payload") or row.get("Payload")


def read(image_path: Path) -> schema.TaggedImage | None:
    blob = _read_payload_field(image_path)
    if not blob:
        return None
    try:
        return _decode_payload(blob)
    except Exception as exc:
        log.warning("payload decode failed on %s: %s", image_path, exc)
        return None


def already_tagged(image_path: Path, sha256: str | None = None) -> schema.TaggedImage | None:
    """Return existing TaggedImage if the image already carries vtag metadata
    of the current schema version.

    NOTE: ``sha256`` is intentionally NOT compared against ``source.sha256``.
    Writing the XMP payload mutates the file's bytes, so the stored sha (taken
    pre-write) and a freshly-computed sha (taken post-write) will always
    disagree on a re-run. Use ``-f``/``force=True`` upstream to re-tag when
    the underlying image content has actually changed.
    """
    existing = read(image_path)
    if existing is None:
        return None
    if existing.schema_version != schema.SCHEMA_VERSION:
        return None
    return existing


MP4_LIKE_FORMATS = frozenset({"MP4", "MOV"})


def _do_write(image_path: Path, tagged: schema.TaggedImage, target_override: Path | None) -> None:
    args = _build_write_args(image_path, tagged, target_override=target_override)
    daemon = _daemon()
    if daemon is not None:
        _stdout, stderr_text = daemon.execute(args, timeout=60)
        if stderr_text.strip():
            raise MetadataError(
                f"exiftool write failed on {image_path}: {stderr_text.strip()[:400]}"
            )
        return
    rc, _stdout, stderr = _run_oneshot(args, timeout=60)
    if rc != 0:
        raise MetadataError(
            f"exiftool write failed on {image_path}: {stderr.decode(errors='replace')}"
        )


def write(image_path: Path, tagged: schema.TaggedImage) -> None:
    fmt = tagged.source.format.upper()
    if fmt not in XMP_WRITABLE_FORMATS:
        raise MetadataError(f"XMP not writable for format {fmt!r} ({image_path})")

    try:
        _do_write(image_path, tagged, target_override=None)
    except MetadataError as exc:
        # MP4/MOV write can fail when exiftool refuses to rewrite the
        # container (fragmented MP4 with sidx, etc.) — fall back to a
        # `<file>.xmp` sidecar so the payload still lives on disk.
        if fmt in MP4_LIKE_FORMATS:
            sidecar = _sidecar_path(image_path)
            log.warning(
                "embedded write failed on %s, retrying as sidecar %s: %s",
                image_path, sidecar.name, str(exc)[:200],
            )
            _do_write(image_path, tagged, target_override=sidecar)
            return
        raise
