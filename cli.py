"""vtag CLI: tag a file or recursively tag a directory."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path

import config
import runstate
from pipeline import dispatcher, metadata, preprocess, schema

log = logging.getLogger("vtag")

_FILE_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def _run_cmd(args: argparse.Namespace, label: str) -> list[str]:
    cmd = ["vtag", "tag"]
    if args.recursive:
        cmd.append("-r")
    if args.force:
        cmd.append("-f")
    if args.from_file:
        cmd += ["--from-file", label]
    else:
        cmd.append(label)
    return cmd


def _attach_file_log(log_path: Path | None) -> logging.Handler | None:
    if log_path is None:
        return None
    handler = logging.FileHandler(log_path)
    handler.setFormatter(logging.Formatter(_FILE_LOG_FORMAT))
    logging.getLogger("vtag").addHandler(handler)
    return handler


def _detach_file_log(handler: logging.Handler | None) -> None:
    if handler is None:
        return
    logging.getLogger("vtag").removeHandler(handler)
    handler.close()

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff", ".tif",
              ".mp4", ".mov", ".mkv", ".webm"}
SIDECAR_SUFFIX = ".tags.json"


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=getattr(logging, level.upper(), logging.INFO),
    )
    for noisy in ("httpx", "httpcore", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _iter_images(root: Path, recursive: bool) -> list[Path]:
    if root.is_file():
        return [root]
    if not recursive:
        return sorted(p for p in root.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS)
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXTS)


async def _busy_handler(busy):
    log.warning("GPU busy: %s; retrying in %.0fs", busy, config.GPU_BUSY_RETRY_SECONDS)


async def cmd_tag(args: argparse.Namespace) -> int:
    if args.from_file:
        list_path = Path(args.from_file).expanduser()
        if not list_path.exists():
            log.error("list file does not exist: %s", list_path)
            return 2
        images = []
        for line in list_path.read_text().splitlines():
            s = line.strip()
            if not s:
                continue
            p = Path(s).expanduser()
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
                images.append(p)
        if not images:
            log.warning("no taggable images listed in %s", list_path)
            return 0
        run_label = str(list_path)
        single_file = False
    else:
        if not args.path:
            log.error("provide a path or --from-file")
            return 2
        root = Path(args.path).expanduser().resolve()
        if not root.exists():
            log.error("path does not exist: %s", root)
            return 2
        images = _iter_images(root, recursive=args.recursive)
        if not images:
            log.warning("no images found under %s", root)
            return 0
        run_label = str(root)
        single_file = root.is_file()

    total = len(images)
    tagged_n = 0
    skipped_n = 0
    failed_n = 0
    i = 0

    if single_file:
        try:
            if not args.force:
                p = preprocess.probe(images[0])
                cached = metadata.already_tagged(images[0], p.sha256)
                if cached is not None:
                    log.info("[1/1] SKIP %s (already tagged)", images[0])
                    return 0
            result = await dispatcher.tag_image_with_retry(
                images[0], force=args.force, on_busy=_busy_handler,
            )
            log.info(
                "[1/1] OK %.1fs %s -- %s",
                result.elapsed_seconds, images[0].name, _summary(result.tagged),
            )
            return 0
        except preprocess.CorruptSourceError as exc:
            log.warning("[1/1] SKIP %s (corrupt: %s)", images[0], exc)
            return 0
        except Exception as exc:
            log.exception("[1/1] FAIL %s: %s", images[0], exc)
            return 1

    log_path = runstate.begin(_run_cmd(args, run_label), run_label)
    if log_path is None:
        log.warning("another vtag run owns run-state; this run won't appear in the webui")
    file_handler = _attach_file_log(log_path)

    try:
        if config.EXIFTOOL_DAEMON:
            metadata.start_daemon()
        try:
            for chunk_start in range(0, total, config.BATCH_SIZE):
                chunk = images[chunk_start : chunk_start + config.BATCH_SIZE]
                async for path, result, exc in dispatcher.tag_batch(
                    chunk, force=args.force, on_busy=_busy_handler,
                ):
                    i += 1
                    if exc is not None:
                        if isinstance(exc, preprocess.CorruptSourceError):
                            skipped_n += 1
                            log.warning("[%d/%d] SKIP %s (corrupt: %s)", i, total, path, exc)
                            continue
                        failed_n += 1
                        log.error("[%d/%d] FAIL %s: %s", i, total, path, exc)
                        if args.fail_fast:
                            return 1
                        continue
                    assert result is not None
                    if result.cached:
                        skipped_n += 1
                        log.info("[%d/%d] SKIP %s (already tagged)", i, total, path)
                    else:
                        tagged_n += 1
                        log.info(
                            "[%d/%d] OK %.1fs %s -- %s",
                            i, total, result.elapsed_seconds, path.name,
                            _summary(result.tagged),
                        )
        finally:
            if config.EXIFTOOL_DAEMON:
                metadata.stop_daemon()

        log.info(
            "done: %d tagged, %d skipped, %d failed (total %d)",
            tagged_n, skipped_n, failed_n, total,
        )
        return 0 if failed_n == 0 else 1
    finally:
        _detach_file_log(file_handler)
        runstate.finish(log_path)


def _summary(tagged) -> str:
    bits = [tagged.content_type]
    if tagged.template:
        bits.append(f"tpl={tagged.template}")
    if tagged.user_labels:
        bits.append("labels=" + ",".join(tagged.user_labels[:4]))
    if tagged.text_ocr:
        snippet = " | ".join(tagged.text_ocr)[:60]
        bits.append(f'text="{snippet}"')
    return " ".join(bits)


def _require_meta(image_path: Path) -> schema.TaggedImage | None:
    if not image_path.exists():
        log.error("image not found: %s", image_path)
        return None
    t = metadata.read(image_path)
    if t is None:
        log.error("no embedded vtag metadata in %s (run `vtag tag %s` first)", image_path, image_path)
        return None
    return t


def cmd_info(args: argparse.Namespace) -> int:
    image_path = Path(args.path).expanduser().resolve()
    t = _require_meta(image_path)
    if t is None:
        return 1 if image_path.exists() else 2
    print(json.dumps(asdict(t), indent=2, ensure_ascii=False, sort_keys=False))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    image_path = Path(args.path).expanduser().resolve()
    t = _require_meta(image_path)
    if t is None:
        return 1 if image_path.exists() else 2
    out: list[str] = []
    head_bits = [t.content_type]
    if t.template:
        head_bits.append(f"tpl={t.template}")
    if t.category:
        head_bits.append(f"category={t.category}")
    out.append(" · ".join(head_bits))
    if t.user_labels:
        out.append("Labels: " + ", ".join(t.user_labels))
    if t.cultural_refs:
        out.append("References: " + ", ".join(t.cultural_refs))
    if t.description:
        out.append("")
        out.append(t.description)
    if t.context:
        out.append("Context: " + t.context)
    if t.punchline:
        out.append("Punchline: " + t.punchline)
    if t.text_ocr:
        out.append("")
        out.append("Text in image:")
        for line in t.text_ocr:
            out.append(f"  {line}")
    out.append("")
    out.append(f"Tags ({len(t.tags)}):")
    for i in range(0, len(t.tags), 6):
        out.append("  " + "  ".join(t.tags[i:i+6]))
    print("\n".join(out))
    return 0


def cmd_tags(args: argparse.Namespace) -> int:
    image_path = Path(args.path).expanduser().resolve()
    t = _require_meta(image_path)
    if t is None:
        return 1 if image_path.exists() else 2
    for tag in t.tags:
        print(tag)
    return 0


def cmd_label(args: argparse.Namespace) -> int:
    image_path = Path(args.path).expanduser().resolve()
    existing = _require_meta(image_path)
    if existing is None:
        return 1 if image_path.exists() else 2

    if not args.add and not args.remove and not args.clear:
        if existing.user_labels:
            for lab in existing.user_labels:
                print(lab)
        else:
            print("(no labels)")
        return 0

    new = [] if args.clear else list(existing.user_labels)
    for lab in args.add or []:
        n = schema.normalize_tag(lab)
        if n and n not in new:
            new.append(n)
    for lab in args.remove or []:
        n = schema.normalize_tag(lab)
        if n in new:
            new.remove(n)

    try:
        written = metadata.set_user_labels(image_path, new)
    except metadata.MetadataError as exc:
        log.error("%s", exc)
        return 1
    log.info("labels on %s: %s", image_path.name, ", ".join(written) or "(none)")
    return 0


CHARACTER_VOCAB = frozenset({
    "pepe", "apu", "apustaja", "wojak", "doomer", "soyjak", "soyboy",
    "coomer", "groyper", "brainlet", "npc", "chad", "gigachad", "virgin",
    "stacy", "becky", "trad", "soyak", "soijak",
})


def _is_character_tag(tag: str) -> bool:
    # char:* meta-tags are always character-derived; for the rest, a tag is a
    # character tag if any underscore/colon token matches the vocabulary
    # (catches `apustaja`, `apustaja_adjacent`, `apustaja_feet_focus_meme`).
    if tag.startswith("char:"):
        return True
    tokens = tag.replace(":", "_").split("_")
    return any(tok in CHARACTER_VOCAB for tok in tokens)


def cmd_drop_characters(args: argparse.Namespace) -> int:
    root = Path(args.path).expanduser().resolve()
    if not root.exists():
        log.error("path does not exist: %s", root)
        return 2
    images = _iter_images(root, recursive=args.recursive)
    if not images:
        log.warning("no images found under %s", root)
        return 0

    total = len(images)
    cleaned = skipped = failed = 0
    if config.EXIFTOOL_DAEMON:
        metadata.start_daemon()
    try:
        for i, img in enumerate(images, 1):
            tagged = metadata.read(img)
            if tagged is None:
                skipped += 1
                continue
            kept_tags = [t for t in tagged.tags if not _is_character_tag(t)]
            kept_refs = [r for r in tagged.cultural_refs
                         if not any(tok in CHARACTER_VOCAB for tok in r.replace(":", "_").split("_"))]
            removed = (len(tagged.tags) - len(kept_tags)) + (len(tagged.cultural_refs) - len(kept_refs))
            if removed == 0:
                skipped += 1
                continue
            tagged.tags = kept_tags
            tagged.cultural_refs = kept_refs
            if args.dry_run:
                cleaned += 1
                log.info("[%d/%d] would clean %s (drop %d character tags)",
                         i, total, img.name, removed)
                continue
            try:
                metadata.write(img, tagged)
                cleaned += 1
                if args.verbose:
                    log.info("[%d/%d] cleaned %s (-%d)", i, total, img.name, removed)
            except metadata.MetadataError as exc:
                failed += 1
                log.warning("[%d/%d] write failed %s: %s", i, total, img, exc)
    finally:
        if config.EXIFTOOL_DAEMON:
            metadata.stop_daemon()

    log.info("drop-characters: %d cleaned, %d skipped, %d failed (total %d)",
             cleaned, skipped, failed, total)
    return 0 if failed == 0 else 1


def cmd_migrate(args: argparse.Namespace) -> int:
    root = Path(args.path).expanduser().resolve()
    if not root.exists():
        log.error("path does not exist: %s", root)
        return 2

    sidecars = sorted(root.rglob("*" + SIDECAR_SUFFIX)) if root.is_dir() else [root]
    sidecars = [p for p in sidecars if p.is_file() and p.name.endswith(SIDECAR_SUFFIX)]
    if not sidecars:
        log.warning("no %s files under %s", SIDECAR_SUFFIX, root)
        return 0

    migrated = 0
    skipped = 0
    failed = 0
    total = len(sidecars)

    for i, sc in enumerate(sidecars, 1):
        image_path = sc.with_name(sc.name[: -len(SIDECAR_SUFFIX)])
        if not image_path.exists():
            log.warning("[%d/%d] image missing for %s", i, total, sc)
            skipped += 1
            continue
        try:
            tagged = schema.TaggedImage.from_json_file(sc)
        except Exception as exc:
            log.warning("[%d/%d] unreadable sidecar %s: %s", i, total, sc, exc)
            failed += 1
            continue
        try:
            metadata.write(image_path, tagged)
        except metadata.MetadataError as exc:
            log.warning("[%d/%d] embed failed for %s: %s", i, total, image_path, exc)
            failed += 1
            continue
        if args.delete:
            try:
                sc.unlink()
            except OSError as exc:
                log.warning("[%d/%d] could not delete %s: %s", i, total, sc, exc)
        migrated += 1
        if args.verbose:
            log.info("[%d/%d] migrated %s", i, total, image_path)

    log.info(
        "migrate: %d embedded, %d skipped, %d failed (total %d)",
        migrated, skipped, failed, total,
    )
    return 0 if failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vtag", description=__doc__)
    parser.add_argument("--log-level", default=config.LOG_LEVEL, help="DEBUG/INFO/WARNING/ERROR")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_tag = sub.add_parser("tag", help="Tag an image or directory")
    p_tag.add_argument("path", nargs="?", help="Image file or directory")
    p_tag.add_argument("--from-file", help="Newline-separated list of image paths to tag")
    p_tag.add_argument("-r", "--recursive", action="store_true", help="Recurse into subdirs")
    p_tag.add_argument("-f", "--force", action="store_true", help="Re-tag even if already embedded")
    p_tag.add_argument("-v", "--verbose", action="store_true", help="Log SKIPs")
    p_tag.add_argument("--fail-fast", action="store_true", help="Abort on first failure")

    p_show = sub.add_parser("show", help="Human-readable summary of an image's tags")
    p_show.add_argument("path", help="Image file")

    p_tags = sub.add_parser("tags", help="Print just the flat tag list (one per line)")
    p_tags.add_argument("path", help="Image file")

    p_info = sub.add_parser("info", help="Print embedded vtag payload as JSON")
    p_info.add_argument("path", help="Image file")

    p_mig = sub.add_parser("migrate", help="Embed legacy .tags.json sidecars into images as XMP")
    p_mig.add_argument("path", help="Image file or directory")
    p_mig.add_argument("--delete", action="store_true", help="Delete the .tags.json after successful embed")
    p_mig.add_argument("-v", "--verbose", action="store_true", help="Log each migrated file")

    p_label = sub.add_parser("label", help="Add/remove/list your own labels on an image")
    p_label.add_argument("path", help="Image file")
    p_label.add_argument("-a", "--add", action="append", metavar="LABEL", help="Add a label (repeatable)")
    p_label.add_argument("-x", "--remove", action="append", metavar="LABEL", help="Remove a label (repeatable)")
    p_label.add_argument("--clear", action="store_true", help="Remove all labels")

    p_drop = sub.add_parser("drop-characters", help="Strip retired character tags from already-tagged files (metadata only, no GPU)")
    p_drop.add_argument("path", help="Image file or directory")
    p_drop.add_argument("-r", "--recursive", action="store_true", help="Recurse into subdirs")
    p_drop.add_argument("-v", "--verbose", action="store_true", help="Log each cleaned file")
    p_drop.add_argument("--dry-run", action="store_true", help="Report what would change, write nothing")

    args = parser.parse_args(argv)
    _setup_logging(args.log_level)

    if args.cmd == "tag":
        return asyncio.run(cmd_tag(args))
    if args.cmd == "show":
        return cmd_show(args)
    if args.cmd == "tags":
        return cmd_tags(args)
    if args.cmd == "info":
        return cmd_info(args)
    if args.cmd == "migrate":
        return cmd_migrate(args)
    if args.cmd == "label":
        return cmd_label(args)
    if args.cmd == "drop-characters":
        return cmd_drop_characters(args)

    parser.error(f"unknown command {args.cmd!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
