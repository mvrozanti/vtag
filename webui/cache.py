"""Read-only access to vfind's SQLite index (`~/.cache/vtag/index.sqlite`).

The webui layer reads cached payloads without ever writing — refresh remains
vfind's job. Concurrent vfind writers are safe thanks to WAL mode.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

log = logging.getLogger("vtag-webui.cache")

CACHE_DIR = Path(os.getenv("VTAG_CACHE_DIR", str(Path.home() / ".cache/vtag")))
CACHE_DB = CACHE_DIR / "index.sqlite"

_BY_SHA_SCAN_CAP = 20000


def db_exists() -> bool:
    return CACHE_DB.exists()


def open_ro() -> sqlite3.Connection | None:
    if not CACHE_DB.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.OperationalError as exc:
        log.warning("open_ro failed: %s", exc)
        return None
    conn.row_factory = sqlite3.Row
    return conn


def decode_payload(payload_b64: str | None) -> dict | None:
    if not payload_b64:
        return None
    try:
        raw = base64.b64decode(payload_b64.encode("ascii"))
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:
        log.debug("payload decode failed: %s", exc)
        return None


def _card(path: str, mtime: int, payload: dict) -> dict[str, Any]:
    source = payload.get("source") or {}
    tags = payload.get("tags") or []
    return {
        "sha": str(source.get("sha256") or ""),
        "path": path,
        "basename": os.path.basename(path),
        "content_type": str(payload.get("content_type") or "other"),
        "template": str(payload.get("template") or ""),
        "tagged_at": str(payload.get("tagged_at") or ""),
        "mtime": int(mtime),
        "tags_top": [str(t) for t in tags[:8]],
        "tags_count": len(tags),
        "width": int(source.get("width") or 0),
        "height": int(source.get("height") or 0),
        "format": str(source.get("format") or ""),
    }


def recent(
    conn: sqlite3.Connection,
    *,
    limit: int = 60,
    offset: int = 0,
    content_type: str | None = None,
) -> list[dict[str, Any]]:
    # Pull a larger page when filtering so the post-decode filter still yields ~limit.
    raw_limit = limit * 4 if content_type else limit
    rows = conn.execute(
        "SELECT path, mtime, payload FROM images "
        "WHERE payload IS NOT NULL "
        "ORDER BY mtime DESC LIMIT ? OFFSET ?",
        (raw_limit, offset),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        payload = decode_payload(row["payload"])
        if payload is None:
            continue
        if content_type and str(payload.get("content_type") or "").lower() != content_type.lower():
            continue
        out.append(_card(row["path"], int(row["mtime"]), payload))
        if len(out) >= limit:
            break
    return out


def _payload_matches(path: str, payload: dict, needle: str) -> bool:
    if needle in os.path.basename(path).lower():
        return True
    for t in payload.get("tags") or []:
        if needle in str(t).lower():
            return True
    for c in payload.get("characters") or []:
        if needle in str(c).lower():
            return True
    if needle in str(payload.get("template") or "").lower():
        return True
    if needle in str(payload.get("content_type") or "").lower():
        return True
    for line in payload.get("text_ocr") or []:
        if needle in str(line).lower():
            return True
    for field in ("description", "context", "punchline"):
        if needle in str(payload.get(field) or "").lower():
            return True
    return False


def search(
    conn: sqlite3.Connection,
    *,
    query: str | None = None,
    content_type: str | None = None,
    limit: int = 300,
) -> tuple[list[dict[str, Any]], int]:
    """Filter the whole cache by free substring + content_type. Returns
    (cards up to `limit`, total match count). Matches basename, tags,
    characters, template, content_type, OCR text, and the prose fields.
    """
    needle = (query or "").strip().lower()
    ctype = (content_type or "").strip().lower() or None
    items: list[dict[str, Any]] = []
    total = 0
    for row in conn.execute(
        "SELECT path, mtime, payload FROM images WHERE payload IS NOT NULL ORDER BY mtime DESC"
    ):
        payload = decode_payload(row["payload"])
        if payload is None:
            continue
        if ctype and str(payload.get("content_type") or "").lower() != ctype:
            continue
        if needle and not _payload_matches(row["path"], payload, needle):
            continue
        total += 1
        if len(items) < limit:
            items.append(_card(row["path"], int(row["mtime"]), payload))
    return items, total


def by_sha(conn: sqlite3.Connection, sha256: str) -> tuple[str, dict] | None:
    if not sha256:
        return None
    needle = sha256.lower()
    seen = 0
    for row in conn.execute(
        "SELECT path, payload FROM images WHERE payload IS NOT NULL ORDER BY mtime DESC"
    ):
        seen += 1
        payload = decode_payload(row["payload"])
        if payload is None:
            continue
        source = payload.get("source") or {}
        if str(source.get("sha256") or "").lower() == needle:
            return row["path"], payload
        if seen >= _BY_SHA_SCAN_CAP:
            break
    return None


def cache_meta(conn: sqlite3.Connection | None) -> dict[str, Any]:
    if conn is None:
        return {"db_exists": False, "count": 0, "last_mtime": 0}
    row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(MAX(mtime), 0) AS last FROM images WHERE payload IS NOT NULL"
    ).fetchone()
    return {
        "db_exists": True,
        "count": int(row["n"] or 0),
        "last_mtime": int(row["last"] or 0),
    }
