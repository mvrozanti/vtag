import base64
import json
import os
import sqlite3

import cli
import find
from pipeline import metadata
from webui import cache


def payload(sha):
    body = {"tags": ["x"], "content_type": "meme", "source": {"sha256": sha}}
    return base64.b64encode(json.dumps(body).encode()).decode()


def media(path, data, mtime):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))
    return str(path)


def row(path, sha=None):
    mtime, size = find.index_stat(path)
    return (path, mtime, size, payload(sha) if sha else None)


def build(tmp_path, monkeypatch, rows, roots):
    db = tmp_path / "index.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE images (path TEXT PRIMARY KEY, mtime INTEGER NOT NULL, size INTEGER NOT NULL, payload TEXT)")
    conn.executemany("INSERT INTO images VALUES (?,?,?,?)", rows)
    conn.commit()
    conn.close()
    monkeypatch.setattr(cache, "CACHE_DB", db)
    return cache._build(tuple(str(r) for r in roots))


def test_snapshot_covers_every_root_and_nothing_outside(tmp_path, monkeypatch):
    phone, chan, elsewhere = tmp_path / "phone", tmp_path / "chan", tmp_path / "elsewhere"
    tagged = media(chan / "a.jpg", b"a", 1_000)
    untagged = media(phone / "b.jpg", b"b", 2_000)
    stray = media(elsewhere / "c.jpg", b"c", 3_000)
    snap = build(tmp_path, monkeypatch, [row(tagged, "aa" * 32), row(untagged), row(stray, "cc" * 32)], [phone, chan])
    assert {d.path for d in snap.docs} == {tagged, untagged}
    assert snap.meta()["count"] == 2 and snap.meta()["tagged"] == 1


def test_search_shows_a_copy_once_and_keeps_the_original(tmp_path, monkeypatch):
    sha = "dd" * 32
    original = media(tmp_path / "chan" / "meme.jpg", b"same", 1_000)
    copy = media(tmp_path / "phone" / "raw.jpg", b"same", 2_000)
    other = media(tmp_path / "phone" / "other.jpg", b"other", 3_000)
    snap = build(tmp_path, monkeypatch, [row(original, sha), row(copy, sha), row(other, "ee" * 32)],
                 [tmp_path / "chan", tmp_path / "phone"])
    items, total, _fuzzy = snap.search(query="")
    assert total == 2
    assert {i["path"] for i in items} == {original, other}


def test_untagged_lists_only_library_files_without_payload_newest_first(tmp_path, monkeypatch):
    phone, chan, elsewhere = tmp_path / "phone", tmp_path / "chan", tmp_path / "elsewhere"
    old = media(phone / "old.jpg", b"o", 1_000)
    new = media(chan / "new.png", b"n", 2_000)
    done = media(chan / "done.jpg", b"d", 3_000)
    media(elsewhere / "stray.jpg", b"s", 4_000)
    monkeypatch.setattr(find, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(find, "CACHE_DB", tmp_path / "cache" / "index.sqlite")
    monkeypatch.setattr(find, "_refresh_batch", lambda paths: {p: (payload("ff" * 32) if p == done else None) for p in paths})
    monkeypatch.setenv("VTAG_SEARCH_ROOTS", f"{phone}{os.pathsep}{chan}")
    found = cli._untagged_in_library(cli._library_roots())
    assert [str(p) for p in found] == [new, old]


def test_sidecar_roots_send_tags_to_a_sidecar(tmp_path, monkeypatch):
    inside = tmp_path / "phone" / "x.jpg"
    outside = tmp_path / "chan" / "y.jpg"
    monkeypatch.setenv("VTAG_SIDECAR_ROOTS", str(tmp_path / "phone"))
    assert metadata._write_target(inside) == inside.with_name("x.jpg.xmp")
    assert metadata._write_target(outside) is None
