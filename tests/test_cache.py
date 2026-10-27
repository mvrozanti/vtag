import base64
import json
import os
import sqlite3

import find
from webui import cache


def payload(sha):
    body = {"tags": ["x"], "source": {"sha256": sha}}
    return base64.b64encode(json.dumps(body).encode()).decode()


def index(tmp_path, monkeypatch, rows):
    db = tmp_path / "index.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE IF EXISTS images")
    conn.execute("CREATE TABLE images (path TEXT PRIMARY KEY, mtime INTEGER NOT NULL, size INTEGER NOT NULL, payload TEXT)")
    conn.executemany("INSERT INTO images VALUES (?,?,?,?)", rows)
    conn.commit()
    conn.close()
    monkeypatch.setattr(cache, "CACHE_DB", db)
    return cache._build((str(tmp_path),))


def media(tmp_path, name, data, mtime):
    p = tmp_path / name
    p.write_bytes(data)
    os.utime(p, (mtime, mtime))
    return str(p)


def row(path, sha):
    mtime, size = find.index_stat(path)
    return (path, mtime, size, payload(sha))


def test_replaced_file_drops_the_stale_payload_and_its_id(tmp_path, monkeypatch):
    sha = "ab" * 32
    original = media(tmp_path, "orig.jpg", b"first", 1_000)
    recycled = media(tmp_path, "raw(7).jpg", b"first", 1_000)
    stale = row(recycled, sha)
    media(tmp_path, "raw(7).jpg", b"a different picture", 2_000)
    snap = index(tmp_path, monkeypatch, [row(original, sha), stale])
    by_path = {d.path: d for d in snap.docs}
    assert by_path[recycled].payload is None
    assert by_path[recycled].id != sha
    assert snap.get(sha).path == original


def test_copies_sharing_a_payload_sha_get_distinct_ids(tmp_path, monkeypatch):
    sha = "cd" * 32
    a = media(tmp_path, "a.jpg", b"same", 2_000)
    b = media(tmp_path, "b.jpg", b"same", 1_000)
    snap = index(tmp_path, monkeypatch, [row(a, sha), row(b, sha)])
    ids = {d.path: d.id for d in snap.docs}
    assert ids[a] == sha and ids[b] != sha
    assert {snap.get(i).path for i in ids.values()} == {a, b}


def test_reused_name_gets_a_new_untagged_id(tmp_path, monkeypatch):
    p = media(tmp_path, "raw.jpg", b"one", 1_000)
    first = index(tmp_path, monkeypatch, [(p, *find.index_stat(p), None)]).docs[0]
    media(tmp_path, "raw.jpg", b"another one", 2_000)
    assert not cache.unchanged(first)
    second = index(tmp_path, monkeypatch, [(p, *find.index_stat(p), None)]).docs[0]
    assert second.id != first.id
