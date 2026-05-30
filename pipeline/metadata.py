"""In-file metadata: read/write the vtag payload as XMP via exiftool."""
from __future__ import annotations

import base64
import json
import logging
import shutil
import subprocess
from dataclasses import asdict
from pathlib import Path

from . import schema

log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent / "exiftool_config.pl"

XMP_WRITABLE_FORMATS = frozenset({
    "JPEG", "PNG", "WEBP", "TIFF", "GIF", "MP4", "MOV", "MKV", "WEBM",
})


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


def _read_payload_field(image_path: Path) -> str | None:
    et = _exiftool()
    args = [
        et, "-config", str(CONFIG_PATH),
        "-j", "-G1", "-s",
        "-XMP-vtag:Payload",
        str(image_path),
    ]
    try:
        result = subprocess.run(args, capture_output=True, timeout=30)
    except Exception as exc:
        log.debug("exiftool read failed on %s: %s", image_path, exc)
        return None
    if result.returncode != 0:
        log.debug("exiftool nonzero on %s: %s", image_path, result.stderr.decode(errors="replace"))
        return None
    try:
        rows = json.loads(result.stdout.decode("utf-8") or "[]")
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


def already_tagged(image_path: Path, sha256: str) -> schema.TaggedImage | None:
    existing = read(image_path)
    if existing is None:
        return None
    if existing.schema_version != schema.SCHEMA_VERSION:
        return None
    if existing.source.sha256 != sha256:
        return None
    return existing


def write(image_path: Path, tagged: schema.TaggedImage) -> None:
    et = _exiftool()
    fmt = tagged.source.format.upper()
    if fmt not in XMP_WRITABLE_FORMATS:
        raise MetadataError(f"XMP not writable for format {fmt!r} ({image_path})")

    description = tagged.description or ""
    title_bits = [tagged.content_type]
    if tagged.template:
        title_bits.append(tagged.template)
    if tagged.category:
        title_bits.append(tagged.category)
    title = " / ".join(b for b in title_bits if b)

    args = [
        et, "-config", str(CONFIG_PATH),
        "-overwrite_original",
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

    args.append(str(image_path))

    result = subprocess.run(args, capture_output=True, timeout=60)
    if result.returncode != 0:
        raise MetadataError(
            f"exiftool write failed on {image_path}: {result.stderr.decode(errors='replace')}"
        )
