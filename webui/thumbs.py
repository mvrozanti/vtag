"""Thumbnail cache keyed by sha256.

Pillow opens the source, downscales to ~256 px on the long edge, writes a JPEG
under `~/.cache/vtag/thumbs/<sha>.jpg`. Video formats and unreadable images
return None — the client renders a placeholder tile.
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

log = logging.getLogger("vtag-webui.thumbs")

try:
    from PIL import Image, UnidentifiedImageError
except ImportError:
    Image = None  # type: ignore[assignment]
    UnidentifiedImageError = Exception  # type: ignore[assignment,misc]

CACHE_DIR = Path(os.getenv("VTAG_CACHE_DIR", str(Path.home() / ".cache/vtag")))
THUMB_DIR = CACHE_DIR / "thumbs"
THUMB_EDGE = int(os.getenv("VTAG_THUMB_EDGE", "256"))
THUMB_QUALITY = int(os.getenv("VTAG_THUMB_QUALITY", "82"))

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi"}

_locks_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}


def _lock_for(sha: str) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(sha)
        if lock is None:
            lock = threading.Lock()
            _locks[sha] = lock
        return lock


def _drop_lock(sha: str) -> None:
    with _locks_guard:
        _locks.pop(sha, None)


def thumb_path(sha: str) -> Path:
    return THUMB_DIR / f"{sha}.jpg"


def ensure(sha: str, source_path: str) -> Path | None:
    if not sha or not source_path:
        return None
    if Image is None:
        log.warning("Pillow not available; cannot generate thumbnails")
        return None
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    out = thumb_path(sha)
    try:
        if out.exists() and out.stat().st_size > 0:
            return out
    except OSError:
        pass
    ext = os.path.splitext(source_path)[1].lower()
    if ext in VIDEO_EXTS:
        return None
    lock = _lock_for(sha)
    with lock:
        try:
            if out.exists() and out.stat().st_size > 0:
                return out
        except OSError:
            pass
        try:
            with Image.open(source_path) as im:
                im = im.convert("RGB")
                im.thumbnail((THUMB_EDGE, THUMB_EDGE), Image.LANCZOS)
                tmp = out.with_suffix(".jpg.tmp")
                im.save(tmp, "JPEG", quality=THUMB_QUALITY, optimize=True)
                os.replace(tmp, out)
        except (OSError, UnidentifiedImageError, ValueError) as exc:
            log.warning("thumbnail failed for %s: %s", source_path, exc)
            try:
                out.with_suffix(".jpg.tmp").unlink(missing_ok=True)
            except OSError:
                pass
            _drop_lock(sha)
            return None
    _drop_lock(sha)
    return out
