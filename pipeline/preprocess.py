"""Image + video probing: sha256, format/dim/frames, midpoint frame extraction.

For still images (PIL): handles JPEG/PNG/WEBP/GIF/BMP/TIFF + animated WEBP/GIF.
For videos (PyAV): handles MP4/MOV/MKV/WEBM — midpoint frame seek + decode.
"""
from __future__ import annotations

import hashlib
import io
import logging
from dataclasses import dataclass
from pathlib import Path

import av
from PIL import Image, ImageSequence, UnidentifiedImageError

log = logging.getLogger(__name__)


class CorruptSourceError(Exception):
    """Source file is unreadable (truncated, garbled, or wrong magic).

    Distinct from generic Exception so callers can demote these to SKIP
    rather than FAIL — re-running the pipeline won't fix a corrupt file.
    """

SUPPORTED_IMAGE_FORMATS = frozenset({"JPEG", "PNG", "WEBP", "GIF", "BMP", "TIFF"})
SUPPORTED_VIDEO_FORMATS = frozenset({"MP4", "MOV", "MKV", "WEBM"})
SUPPORTED_FORMATS = SUPPORTED_IMAGE_FORMATS | SUPPORTED_VIDEO_FORMATS

VIDEO_EXTS = frozenset({".mp4", ".mov", ".mkv", ".webm"})

_VIDEO_EXT_TO_FORMAT: dict[str, str] = {
    ".mp4":  "MP4",
    ".mov":  "MOV",
    ".mkv":  "MKV",
    ".webm": "WEBM",
}


@dataclass
class Probe:
    sha256: str
    size_bytes: int
    mtime: int
    format: str
    width: int
    height: int
    frames: int


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            buf = f.read(chunk)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTS


def _probe_video(path: Path) -> tuple[str, int, int, int]:
    try:
        with av.open(str(path)) as container:
            stream = next((s for s in container.streams if s.type == "video"), None)
            if stream is None:
                raise CorruptSourceError(f"no video stream in {path}")
            cc = stream.codec_context
            width = int(cc.width or 0)
            height = int(cc.height or 0)
            frames = int(stream.frames or 0)
            if frames <= 0 and stream.duration and stream.time_base and stream.average_rate:
                duration_s = float(stream.duration * stream.time_base)
                frames = max(1, int(duration_s * float(stream.average_rate)))
            if frames <= 0:
                frames = 1
    except (av.AVError, EOFError, OSError) as exc:
        raise CorruptSourceError(f"av cannot open {path}: {exc}") from exc
    fmt = _VIDEO_EXT_TO_FORMAT.get(path.suffix.lower(), "VIDEO")
    return fmt, width, height, frames


def probe(path: Path) -> Probe:
    st = path.stat()
    digest = sha256_file(path)
    if is_video(path):
        fmt, width, height, frames = _probe_video(path)
    else:
        try:
            with Image.open(path) as img:
                fmt = (img.format or "").upper()
                width, height = img.size
                frames = getattr(img, "n_frames", 1) or 1
        except (UnidentifiedImageError, OSError, SyntaxError) as exc:
            raise CorruptSourceError(f"PIL cannot open {path}: {exc}") from exc
    return Probe(
        sha256=digest,
        size_bytes=st.st_size,
        mtime=int(st.st_mtime),
        format=fmt,
        width=width,
        height=height,
        frames=frames,
    )


def _midpoint_video_frame(path: Path) -> Image.Image:
    try:
        with av.open(str(path)) as container:
            stream = next((s for s in container.streams if s.type == "video"), None)
            if stream is None:
                raise CorruptSourceError(f"no video stream in {path}")
            stream.thread_type = "AUTO"

            seek_pts = 0
            seekable = bool(stream.duration and stream.time_base)
            if seekable:
                seek_pts = int(stream.duration / 2)
                try:
                    container.seek(seek_pts, stream=stream, any_frame=False)
                except av.AVError:
                    seekable = False

            chosen = None
            for frame in container.decode(stream):
                chosen = frame
                if seekable and frame.pts is not None and frame.pts >= seek_pts:
                    break
            if chosen is None:
                raise CorruptSourceError(f"no decodable video frame in {path}")
            return chosen.to_image()
    except (av.AVError, EOFError, OSError) as exc:
        raise CorruptSourceError(f"av decode failed on {path}: {exc}") from exc


def _midpoint_image(path: Path) -> Image.Image:
    try:
        with Image.open(path) as img:
            n = getattr(img, "n_frames", 1) or 1
            target = max(0, n // 2)
            if n > 1:
                for i, frame in enumerate(ImageSequence.Iterator(img)):
                    if i == target:
                        return frame.convert("RGB").copy()
                return img.convert("RGB").copy()
            return img.convert("RGB").copy()
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise CorruptSourceError(f"PIL decode failed on {path}: {exc}") from exc


def load_representative_image(path: Path) -> Image.Image:
    """Midpoint frame as a full-resolution RGB PIL Image. Used by callers
    that need the raw pixels (e.g. OCR + VLM both consume it)."""
    if is_video(path):
        return _midpoint_video_frame(path)
    return _midpoint_image(path)


def encode_for_vlm(img: Image.Image, max_edge: int) -> bytes:
    if img.mode != "RGB":
        img = img.convert("RGB")
    w, h = img.size
    longest = max(w, h)
    if longest > max_edge:
        scale = max_edge / longest
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def load_representative_frame(path: Path, max_edge: int) -> bytes:
    return encode_for_vlm(load_representative_image(path), max_edge)
