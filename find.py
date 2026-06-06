"""vfind: locate tagged images by tag / template / character / OCR / free text.

Reads vtag metadata embedded as XMP from each image, caching extracted payloads
in a small sqlite index for fast subsequent searches.
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

log = logging.getLogger("vfind")

DEFAULT_ROOTS = (".", "~/Pictures", "~/Downloads")
IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff", ".tif",
    ".mp4", ".mov", ".mkv", ".webm",
}
SIDECAR_EXTS = {".mkv", ".webm"}
SIDECAR_SUFFIX = ".xmp"
EXIFTOOL_CONFIG = Path(__file__).resolve().parent / "pipeline" / "exiftool_config.pl"


def _xmp_target(media_path: str) -> str:
    if os.path.splitext(media_path)[1].lower() in SIDECAR_EXTS:
        return media_path + SIDECAR_SUFFIX
    sidecar = media_path + SIDECAR_SUFFIX
    if os.path.exists(sidecar):
        return sidecar
    return media_path


def _meta_stat(media_path: str) -> tuple[int, int]:
    """Stat of the file that actually carries the XMP for ``media_path``.

    For sidecar formats this is the `<media>.xmp` file (which may not exist
    yet, in which case we return zeros and let exiftool report "no payload").
    """
    target = _xmp_target(media_path)
    try:
        st = os.stat(target)
    except OSError:
        return 0, 0
    return int(st.st_mtime), int(st.st_size)
CACHE_DIR = Path(os.getenv("VTAG_CACHE_DIR", str(Path.home() / ".cache/vtag")))
CACHE_DB = CACHE_DIR / "index.sqlite"
CACHE_SCHEMA_VERSION = 1
BATCH_SIZE = 200


def _expand_roots(env_value: str | None) -> list[Path]:
    raw = env_value if env_value is not None else os.pathsep.join(DEFAULT_ROOTS)
    seen: dict[Path, None] = {}
    for part in raw.split(os.pathsep):
        part = part.strip()
        if not part:
            continue
        p = Path(part).expanduser().resolve()
        if p not in seen:
            seen[p] = None
    return [p for p in seen if p.exists()]


def _open_db() -> sqlite3.Connection:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(CACHE_DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
    row = conn.execute("SELECT v FROM meta WHERE k='cache_schema'").fetchone()
    if row is None:
        conn.execute("INSERT INTO meta(k,v) VALUES('cache_schema', ?)", (str(CACHE_SCHEMA_VERSION),))
    elif int(row[0]) != CACHE_SCHEMA_VERSION:
        conn.executescript("DROP TABLE IF EXISTS images; DELETE FROM meta WHERE k='cache_schema';")
        conn.execute("INSERT INTO meta(k,v) VALUES('cache_schema', ?)", (str(CACHE_SCHEMA_VERSION),))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS images (
            path TEXT PRIMARY KEY,
            mtime INTEGER NOT NULL,
            size INTEGER NOT NULL,
            payload TEXT
        )
        """
    )
    conn.commit()
    return conn


def _iter_image_files(roots: list[Path]) -> list[tuple[str, int, int]]:
    """Walk media files; the (mtime, size) returned is for the XMP-bearing
    file (media itself or its `.xmp` sidecar), so cache invalidation triggers
    on metadata changes for sidecar formats too.
    """
    out: list[tuple[str, int, int]] = []
    for root in roots:
        if root.is_file():
            if root.suffix.lower() in IMAGE_EXTS:
                full = str(root)
                mtime, size = _meta_stat(full)
                if mtime == 0:
                    try:
                        st = root.stat()
                        mtime, size = int(st.st_mtime), int(st.st_size)
                    except OSError:
                        continue
                out.append((full, mtime, size))
            continue
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                ext = os.path.splitext(name)[1].lower()
                if ext not in IMAGE_EXTS:
                    continue
                full = os.path.join(dirpath, name)
                mtime, size = _meta_stat(full)
                if mtime == 0:
                    try:
                        st = os.stat(full)
                        mtime, size = int(st.st_mtime), int(st.st_size)
                    except OSError:
                        continue
                out.append((full, mtime, size))
    return out


def _exiftool_bin() -> str | None:
    return shutil.which("exiftool")


def _refresh_batch(paths: list[str]) -> dict[str, str | None]:
    et = _exiftool_bin()
    if not et:
        raise RuntimeError("exiftool not in PATH")
    # Map each media path to its XMP-bearing file (self or sidecar). Skip
    # sidecar files that don't exist yet — exiftool would error on them.
    target_for: dict[str, str] = {}
    media_for: dict[str, str] = {}
    targets: list[str] = []
    for p in paths:
        t = _xmp_target(p)
        if t != p and not os.path.exists(t):
            continue
        target_for[p] = t
        media_for[t] = p
        targets.append(t)
    out: dict[str, str | None] = {p: None for p in paths}
    if not targets:
        return out
    args = [
        et, "-config", str(EXIFTOOL_CONFIG),
        "-j", "-G1", "-s",
        "-XMP-vtag:Payload",
        *targets,
    ]
    result = subprocess.run(args, capture_output=True, timeout=600)
    if result.returncode != 0:
        log.debug("exiftool batch nonzero: %s", result.stderr.decode(errors="replace")[:400])
    try:
        rows = json.loads(result.stdout.decode("utf-8") or "[]")
    except json.JSONDecodeError:
        return out
    for row in rows:
        source_file = row.get("SourceFile") or ""
        if not source_file:
            continue
        media = media_for.get(source_file)
        if media is None:
            continue
        payload = row.get("XMP-vtag:Payload") or row.get("Payload")
        out[media] = payload or None
    return out


def _decode(payload_b64: str) -> dict | None:
    try:
        raw = base64.b64decode(payload_b64.encode("ascii"))
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:
        log.debug("payload decode failed: %s", exc)
        return None


def _sync_cache(conn: sqlite3.Connection, files: list[tuple[str, int, int]]) -> None:
    rows = conn.execute("SELECT path, mtime, size FROM images").fetchall()
    cached: dict[str, tuple[int, int]] = {r[0]: (r[1], r[2]) for r in rows}

    stale: list[str] = []
    for path, mtime, size in files:
        prev = cached.get(path)
        if prev is None or prev != (mtime, size):
            stale.append(path)

    if not stale:
        return

    log.info("refreshing %d image(s) via exiftool", len(stale))
    size_by_path = {p: s for p, _m, s in files}
    mtime_by_path = {p: m for p, m, _s in files}

    for i in range(0, len(stale), BATCH_SIZE):
        chunk = stale[i : i + BATCH_SIZE]
        try:
            results = _refresh_batch(chunk)
        except Exception as exc:
            log.warning("exiftool batch failed (%d files): %s", len(chunk), exc)
            continue
        with conn:
            for path in chunk:
                payload = results.get(path)
                conn.execute(
                    "INSERT INTO images(path, mtime, size, payload) VALUES(?,?,?,?) "
                    "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, "
                    "size=excluded.size, payload=excluded.payload",
                    (path, mtime_by_path[path], size_by_path[path], payload),
                )


def _prune_missing(conn: sqlite3.Connection, present: set[str]) -> None:
    rows = conn.execute("SELECT path FROM images").fetchall()
    missing = [r[0] for r in rows if r[0] not in present]
    if missing:
        with conn:
            conn.executemany("DELETE FROM images WHERE path=?", [(p,) for p in missing])


def _parse_since(value: str) -> float:
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).timestamp()
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"unrecognized date: {value!r}")


def _matches(
    payload: dict,
    *,
    must_tags: list[str],
    text_substrs: list[str],
    content_type: str | None,
    since_ts: float | None,
    until_ts: float | None,
    free_regex: re.Pattern | None,
) -> bool:
    tags = payload.get("tags", []) or []
    tag_set = {str(t).lower() for t in tags}
    for needle in must_tags:
        n = needle.lower()
        if n in tag_set:
            continue
        if any(n in t for t in tag_set):
            continue
        return False

    if content_type is not None:
        if str(payload.get("content_type", "")).lower() != content_type.lower():
            return False

    if text_substrs:
        haystack = " ".join(payload.get("text_ocr", []) or []).lower()
        for substr in text_substrs:
            if substr.lower() not in haystack:
                return False

    if since_ts is not None or until_ts is not None:
        mtime = payload.get("source", {}).get("mtime", 0) or 0
        if since_ts is not None and mtime < since_ts:
            return False
        if until_ts is not None and mtime > until_ts:
            return False

    if free_regex is not None:
        blob_parts = [
            payload.get("description", "") or "",
            payload.get("context", "") or "",
            payload.get("punchline", "") or "",
        ]
        blob = "\n".join(blob_parts)
        if not free_regex.search(blob):
            return False

    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vfind", description=__doc__)
    parser.add_argument("terms", nargs="*", help="Tag substrings (AND)")
    parser.add_argument("--text", action="append", default=[], help="OCR substring; repeatable")
    parser.add_argument("--type", dest="content_type", help="content_type filter")
    parser.add_argument("--since", type=_parse_since, help="mtime >= YYYY-MM-DD")
    parser.add_argument("--until", type=_parse_since, help="mtime <= YYYY-MM-DD")
    parser.add_argument("-F", "--free", help="Regex over description/context/punchline")
    parser.add_argument("--json", action="store_true", help="Emit full payload JSON (one per line)")
    parser.add_argument("--roots", help="Override VTAG_SEARCH_ROOTS (colon-separated)")
    parser.add_argument("--no-refresh", action="store_true",
                        help="Skip exiftool refresh; query cache as-is")
    parser.add_argument("--reindex", action="store_true",
                        help="Force re-read of every file (clears cache for matched roots first)")
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "WARNING"))
    args = parser.parse_args(argv)

    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=getattr(logging, args.log_level.upper(), logging.WARNING),
    )

    roots = _expand_roots(args.roots if args.roots is not None else os.getenv("VTAG_SEARCH_ROOTS"))
    if not roots:
        print("no search roots exist", file=sys.stderr)
        return 2

    free_regex = re.compile(args.free, re.IGNORECASE) if args.free else None

    conn = _open_db()

    files = _iter_image_files(roots)
    present = {p for p, _m, _s in files}

    if args.reindex:
        with conn:
            conn.executemany(
                "DELETE FROM images WHERE path=?",
                [(p,) for p in present],
            )

    if not args.no_refresh:
        _sync_cache(conn, files)
    _prune_missing(conn, present)

    matches: list[tuple[str, dict, int]] = []
    for path, payload_b64 in conn.execute(
        "SELECT path, payload FROM images WHERE payload IS NOT NULL"
    ):
        if path not in present:
            continue
        payload = _decode(payload_b64)
        if payload is None:
            continue
        if not _matches(
            payload,
            must_tags=args.terms,
            text_substrs=args.text,
            content_type=args.content_type,
            since_ts=args.since,
            until_ts=args.until,
            free_regex=free_regex,
        ):
            continue
        mtime = payload.get("source", {}).get("mtime", 0) or 0
        matches.append((path, payload, mtime))

    matches.sort(key=lambda mt: mt[2], reverse=True)

    if args.json:
        for _path, payload, _mt in matches:
            print(json.dumps(payload, ensure_ascii=False))
    else:
        for path, _payload, _mt in matches:
            print(path)

    return 0 if matches else 1


if __name__ == "__main__":
    sys.exit(main())
