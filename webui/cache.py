"""Read-only, in-memory view of vfind's SQLite index (`~/.cache/vtag/index.sqlite`).

The webui never writes the index — refresh remains vfind's job. A snapshot of
every searchable row is decoded once and rebuilt when the database files
change. Searchable rows are every tagged file plus every untagged file under
`browse_roots`, so media can be located by filename before it is tagged.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fuzzymatch
import searchquery
from searchquery import Hit

log = logging.getLogger("vtag-webui.cache")

CACHE_DIR = Path(os.getenv("VTAG_CACHE_DIR", str(Path.home() / ".cache/vtag")))
CACHE_DB = CACHE_DIR / "index.sqlite"
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
REBUILD_MIN_INTERVAL = 5.0

TIER_NAME = 100.0
TIER_NAME_SQUASHED = 90.0
TIER_TEXT = 80.0
TIER_TEXT_SQUASHED = 70.0


def _media_kind(path: str) -> str:
    return "video" if os.path.splitext(path)[1].lower() in VIDEO_EXTS else "image"


def db_exists() -> bool:
    return CACHE_DB.exists()


def decode_payload(payload_b64: str | None) -> dict | None:
    if not payload_b64:
        return None
    try:
        raw = base64.b64decode(payload_b64.encode("ascii"))
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:
        log.debug("payload decode failed: %s", exc)
        return None


def path_id(path: str) -> str:
    return hashlib.sha256(path.encode("utf-8", "surrogateescape")).hexdigest()


@dataclass(slots=True)
class Doc:
    id: str
    path: str
    mtime: int
    payload: dict | None
    content_type: str
    media: str
    stem: str
    name: str
    name_squashed: str
    text: str
    text_squashed: str


def _text_fields(payload: dict | None, reldir: str) -> list[str]:
    parts = [reldir] if reldir else []
    if payload is None:
        return parts
    parts += [str(t) for t in (payload.get("tags") or []) if ":" not in str(t)]
    parts += [str(t) for t in (payload.get("user_labels") or [])]
    parts.append(str(payload.get("template") or ""))
    parts.append(str(payload.get("content_type") or ""))
    parts += [str(t) for t in (payload.get("text_ocr") or [])]
    return parts


def _reldir(path: str, roots: tuple[str, ...]) -> str:
    for root in roots:
        if path.startswith(root + os.sep):
            return os.path.dirname(path[len(root) + 1:])
    return ""


def _doc(path: str, mtime: int, payload: dict | None, roots: tuple[str, ...]) -> Doc:
    basename = os.path.basename(path)
    text = fuzzymatch.FIELD_BREAK.join(_text_fields(payload, _reldir(path, roots)))
    source = (payload or {}).get("source") or {}
    sha = str(source.get("sha256") or "").lower()
    return Doc(
        id=sha or path_id(path),
        path=path,
        mtime=int(mtime),
        payload=payload,
        content_type=str((payload or {}).get("content_type") or "").lower(),
        media=_media_kind(path),
        stem=os.path.splitext(basename)[0],
        name=basename.lower(),
        name_squashed=fuzzymatch.squash(basename),
        text=text.lower(),
        text_squashed=fuzzymatch.squash(text),
    )


def _term_scorer(term: str) -> searchquery.Scorer:
    needle = term.lower()
    squashed = fuzzymatch.squash(term)
    fuzzy = fuzzymatch.eligible(squashed)

    def run(doc: Doc) -> Hit | None:
        if needle and needle in doc.name:
            return Hit(TIER_NAME)
        if squashed and squashed in doc.name_squashed:
            return Hit(TIER_NAME_SQUASHED)
        if needle and needle in doc.text:
            return Hit(TIER_TEXT)
        if squashed and squashed in doc.text_squashed:
            return Hit(TIER_TEXT_SQUASHED)
        if fuzzy:
            q = fuzzymatch.quality(squashed, doc.stem)
            if q is not None:
                return Hit(q, fuzzy=True)
        return None

    return run


def card(doc: Doc, *, fuzzy: bool = False) -> dict[str, Any]:
    payload = doc.payload or {}
    source = payload.get("source") or {}
    tags = payload.get("tags") or []
    ext = os.path.splitext(doc.path)[1].lstrip(".").upper()
    return {
        "sha": doc.id,
        "path": doc.path,
        "basename": os.path.basename(doc.path),
        "content_type": str(payload.get("content_type") or ("other" if doc.payload else "")),
        "template": str(payload.get("template") or ""),
        "tagged_at": str(payload.get("tagged_at") or ""),
        "mtime": doc.mtime,
        "tags_top": [str(t) for t in tags[:8]],
        "tags_count": len(tags),
        "user_labels": [str(t) for t in (payload.get("user_labels") or [])],
        "width": int(source.get("width") or 0),
        "height": int(source.get("height") or 0),
        "format": str(source.get("format") or ext),
        "media": doc.media,
        "tagged": doc.payload is not None,
        "fuzzy": fuzzy,
    }


@dataclass(slots=True)
class Snapshot:
    docs: list[Doc]
    by_id: dict[str, Doc]
    tagged: int
    last_mtime: int

    def get(self, doc_id: str) -> Doc | None:
        return self.by_id.get(doc_id.lower()) if doc_id else None

    def search(
        self,
        *,
        query: str | None = None,
        content_type: str | None = None,
        media: str | None = None,
        limit: int = 300,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int, int]:
        raw = (query or "").strip()
        ctype = (content_type or "").strip().lower() or None
        mkind = (media or "").strip().lower() or None
        scorer = searchquery.compile_query(raw, _term_scorer) if raw else None

        hits: list[tuple[Hit, Doc]] = []
        for doc in self.docs:
            if ctype and doc.content_type != ctype:
                continue
            if mkind and doc.media != mkind:
                continue
            hit = scorer(doc) if scorer else Hit(0.0)
            if hit is not None:
                hits.append((hit, doc))
        hits.sort(key=lambda h: h[0].score, reverse=True)
        fuzzy = sum(1 for h, _ in hits if h.fuzzy)
        items = [card(doc, fuzzy=h.fuzzy) for h, doc in hits[offset:offset + limit]]
        return items, len(hits), fuzzy

    def meta(self) -> dict[str, Any]:
        return {
            "db_exists": True,
            "count": len(self.docs),
            "tagged": self.tagged,
            "last_mtime": self.last_mtime,
        }


def _signature() -> tuple:
    sig = []
    for p in (CACHE_DB, CACHE_DB.with_name(CACHE_DB.name + "-wal")):
        try:
            st = p.stat()
            sig.append((st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append(None)
    return tuple(sig)


def _open_ro() -> sqlite3.Connection | None:
    if not CACHE_DB.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.OperationalError as exc:
        log.warning("open_ro failed: %s", exc)
        return None
    conn.row_factory = sqlite3.Row
    return conn


def _build(roots: tuple[str, ...]) -> Snapshot | None:
    conn = _open_ro()
    if conn is None:
        return None
    clauses = ["payload IS NOT NULL"]
    params: list[str] = []
    for root in roots:
        clauses.append("(path >= ? AND path < ?)")
        params += [root + os.sep, root + chr(ord(os.sep) + 1)]
    sql = f"SELECT path, mtime, payload FROM images WHERE {' OR '.join(clauses)} ORDER BY mtime DESC"
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error as exc:
        log.warning("snapshot query failed: %s", exc)
        return None
    finally:
        conn.close()
    docs: list[Doc] = []
    by_id: dict[str, Doc] = {}
    tagged = 0
    for row in rows:
        payload = decode_payload(row["payload"])
        if row["payload"] and payload is None:
            continue
        if not os.path.exists(row["path"]):
            continue
        doc = _doc(row["path"], row["mtime"], payload, roots)
        docs.append(doc)
        by_id.setdefault(doc.id, doc)
        tagged += payload is not None
    last = max((d.mtime for d in docs), default=0)
    return Snapshot(docs=docs, by_id=by_id, tagged=tagged, last_mtime=last)


_lock = threading.Lock()
_state: dict[str, Any] = {"snap": None, "sig": None, "roots": None, "at": 0.0}


def snapshot(browse_roots: Iterable[Path | str] = ()) -> Snapshot | None:
    roots = tuple(str(Path(r)).rstrip(os.sep) for r in browse_roots if str(r))
    with _lock:
        sig = _signature()
        snap = _state["snap"]
        fresh = snap is not None and _state["roots"] == roots and (
            _state["sig"] == sig or time.monotonic() - _state["at"] < REBUILD_MIN_INTERVAL
        )
        if fresh:
            return snap
        started = time.monotonic()
        snap = _build(roots)
        _state.update(snap=snap, sig=sig, roots=roots, at=time.monotonic())
        if snap is not None:
            log.info("index snapshot: %d docs (%d tagged) in %.2fs",
                     len(snap.docs), snap.tagged, time.monotonic() - started)
        return snap
