#!/usr/bin/env python3
"""One-shot: move content_type=meme files from PhoneDownloads into the 4chan archive root.

Dry-run by default; pass --execute to act. Dedupes by sha256 against the full
destination tree, verifies every copy, and carries .xmp sidecars along.
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import shutil
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("route-memes")

DEFAULT_SRC = Path.home() / "Documents" / "PhoneDownloads"
DEFAULT_DEST = Path("/mnt/toshiba/hdd/gdrive/Levv/4chan")
SIDECAR_SUFFIX = ".xmp"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def enumerate_memes(src: Path, dest: Path) -> list[Path]:
    roots = f"{src}:{dest}"
    proc = subprocess.run(
        ["vfind", "--roots", roots, "--type", "meme"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode not in (0, 1):
        log.error("vfind failed (rc=%d): %s", proc.returncode, proc.stderr.strip())
        sys.exit(2)
    src_prefix = str(src.resolve())
    return [
        Path(line)
        for line in proc.stdout.splitlines()
        if line.startswith(src_prefix + "/")
    ]


def dest_hashes(dest: Path) -> set[str]:
    hashes: set[str] = set()
    files = [p for p in dest.rglob("*") if p.is_file()]
    log.info("hashing %d existing archive files", len(files))
    for i, p in enumerate(files, 1):
        try:
            hashes.add(sha256_of(p))
        except OSError as exc:
            log.warning("unreadable archive file %s: %s", p, exc)
        if i % 1000 == 0:
            log.info("hashed %d/%d", i, len(files))
    return hashes


def unique_dest(dest_dir: Path, name: str, short_hash: str) -> Path:
    candidate = dest_dir / name
    if not candidate.exists():
        return candidate
    return dest_dir / f"{candidate.stem}-{short_hash[:8]}{candidate.suffix}"


def move_verified(src: Path, dest: Path, src_hash: str) -> bool:
    partial = dest.with_suffix(dest.suffix + ".partial")
    shutil.copy2(src, partial)
    if sha256_of(partial) != src_hash:
        log.error("hash mismatch on copy: %s -> %s; leaving source", src, partial)
        partial.unlink(missing_ok=True)
        return False
    partial.rename(dest)
    src.unlink()
    return True


def move_sidecar(src: Path, dest: Path, dry_run: bool) -> None:
    sidecar = Path(str(src) + SIDECAR_SUFFIX)
    if not sidecar.exists():
        return
    sidecar_dest = Path(str(dest) + SIDECAR_SUFFIX)
    if dry_run:
        log.info("would move sidecar: %s -> %s", sidecar, sidecar_dest)
        return
    shutil.copy2(sidecar, sidecar_dest)
    sidecar.unlink()
    log.info("sidecar: %s -> %s", sidecar, sidecar_dest)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", type=Path, default=DEFAULT_SRC)
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    parser.add_argument("--execute", action="store_true",
                        help="Actually move files (default: dry-run)")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=getattr(logging, args.log_level.upper(), logging.INFO),
    )
    dry_run = not args.execute

    if not args.src.is_dir() or not args.dest.is_dir():
        log.error("src or dest missing: %s, %s", args.src, args.dest)
        return 2

    memes = enumerate_memes(args.src, args.dest)
    log.info("%d meme(s) under %s%s", len(memes), args.src,
             " [DRY-RUN]" if dry_run else "")
    if not memes:
        return 0

    seen = dest_hashes(args.dest)

    moved = duped = collided = failed = 0
    for src in memes:
        try:
            src_hash = sha256_of(src)
        except OSError as exc:
            log.error("unreadable source %s: %s", src, exc)
            failed += 1
            continue

        if src_hash in seen:
            duped += 1
            if dry_run:
                log.info("would dedupe-remove: %s", src)
            else:
                Path(str(src) + SIDECAR_SUFFIX).unlink(missing_ok=True)
                src.unlink()
                log.info("dup-removed: %s", src)
            continue

        dest = unique_dest(args.dest, src.name, src_hash)
        if dest.name != src.name:
            collided += 1

        if dry_run:
            log.info("would move: %s -> %s", src, dest)
            move_sidecar(src, dest, dry_run=True)
            seen.add(src_hash)
            moved += 1
            continue

        try:
            if move_verified(src, dest, src_hash):
                move_sidecar(src, dest, dry_run=False)
                seen.add(src_hash)
                moved += 1
                log.info("moved: %s -> %s", src, dest)
            else:
                failed += 1
        except OSError as exc:
            log.error("move failed %s: %s", src, exc)
            failed += 1

    log.info("summary%s: %d moved, %d deduped, %d name-collisions, %d failed",
             " [DRY-RUN]" if dry_run else "", moved, duped, collided, failed)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
