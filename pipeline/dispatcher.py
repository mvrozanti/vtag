"""Single entry: tag_image(path). Batch entry: tag_batch(paths).

tag_batch holds the gpu-lock once for an entire chunk and keeps Ollama's
VLM resident across all images in the chunk (avoiding per-image evict +
reload). OCR + frame decode run concurrently with the VLM via a small
asyncio queue.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator

import config
from gpu_lock import GpuBusy, gpu_lock

from . import metadata, ocr, ollama_lifecycle, postprocess, preprocess, schema, vlm

log = logging.getLogger(__name__)


@dataclass
class TagResult:
    tagged: schema.TaggedImage
    cached: bool
    elapsed_seconds: float


@dataclass
class _PreparedItem:
    path: Path
    probe: preprocess.Probe | None = None
    ocr_phrases: list[str] = field(default_factory=list)
    image_bytes: bytes = b""
    cached: schema.TaggedImage | None = None
    error: BaseException | None = None


_EOS = object()


async def tag_image(image_path: Path, *, force: bool = False) -> TagResult:
    """Single-image path: per-image lock, per-image evict. Used by `vtag tag <file>`."""
    started = time.time()
    p = preprocess.probe(image_path)

    if not force:
        cached = metadata.already_tagged(image_path, p.sha256)
        if cached is not None:
            return TagResult(
                tagged=cached,
                cached=True,
                elapsed_seconds=time.time() - started,
            )

    loop = asyncio.get_running_loop()
    ocr_phrases = await loop.run_in_executor(None, ocr.extract, image_path)
    ocr_text = ocr.render_for_prompt(ocr_phrases)

    image_bytes = await loop.run_in_executor(
        None, preprocess.load_representative_frame, image_path, config.VLM_MAX_EDGE
    )

    prompt = vlm.render_prompt(ocr_text)
    source = schema.Source(
        path=str(image_path.resolve()),
        sha256=p.sha256,
        mtime=p.mtime,
        size_bytes=p.size_bytes,
        format=p.format,
        width=p.width,
        height=p.height,
        frames=p.frames,
    )
    model = schema.Model(
        vlm=config.VLM_MODEL,
        ocr=ocr.version_string() if ocr_phrases else "",
        prompt_version=vlm.PROMPT_VERSION,
    )

    async with gpu_lock.acquire_async(
        "vtag", expected_seconds=config.GPU_LOCK_EXPECTED_SECONDS
    ):
        await ollama_lifecycle.evict_others(config.OLLAMA_BASE_URL, keep=config.VLM_MODEL)
        try:
            vlm_raw = await vlm.call(
                base_url=config.OLLAMA_BASE_URL,
                model=config.VLM_MODEL,
                image_bytes=image_bytes,
                prompt=prompt,
                temperature=config.VLM_TEMPERATURE,
                timeout_seconds=config.VLM_TIMEOUT_SECONDS,
            )
        finally:
            await ollama_lifecycle.evict_model(config.OLLAMA_BASE_URL, config.VLM_MODEL)

    tagged = postprocess.build_tagged_image(
        vlm_raw=vlm_raw,
        ocr_phrases=ocr_phrases,
        source=source,
        model=model,
    )
    metadata.write(image_path, tagged)
    return TagResult(
        tagged=tagged,
        cached=False,
        elapsed_seconds=time.time() - started,
    )


async def tag_image_with_retry(
    image_path: Path,
    *,
    force: bool = False,
    on_busy=None,
) -> TagResult:
    elapsed_busy = 0.0
    while True:
        try:
            return await tag_image(image_path, force=force)
        except GpuBusy as busy:
            if on_busy is not None:
                await on_busy(busy)
            if elapsed_busy >= config.GPU_BUSY_RETRY_CAP_SECONDS:
                raise
            wait = config.GPU_BUSY_RETRY_SECONDS
            await asyncio.sleep(wait)
            elapsed_busy += wait


async def _prepare_one(
    loop: asyncio.AbstractEventLoop, path: Path, force: bool
) -> _PreparedItem:
    item = _PreparedItem(path=path)
    try:
        if not force:
            cached = await loop.run_in_executor(None, metadata.already_tagged, path, None)
            if cached is not None:
                item.cached = cached
                return item
        probe = await loop.run_in_executor(None, preprocess.probe, path)
        item.probe = probe
        item.ocr_phrases = await loop.run_in_executor(None, ocr.extract, path)
        item.image_bytes = await loop.run_in_executor(
            None, preprocess.load_representative_frame, path, config.VLM_MAX_EDGE
        )
    except BaseException as exc:
        item.error = exc
    return item


async def _consume_one(item: _PreparedItem) -> TagResult:
    started = time.time()
    if item.cached is not None:
        return TagResult(
            tagged=item.cached,
            cached=True,
            elapsed_seconds=0.0,
        )
    probe = item.probe
    assert probe is not None
    ocr_text = ocr.render_for_prompt(item.ocr_phrases)
    prompt = vlm.render_prompt(ocr_text)
    source = schema.Source(
        path=str(item.path.resolve()),
        sha256=probe.sha256,
        mtime=probe.mtime,
        size_bytes=probe.size_bytes,
        format=probe.format,
        width=probe.width,
        height=probe.height,
        frames=probe.frames,
    )
    model = schema.Model(
        vlm=config.VLM_MODEL,
        ocr=ocr.version_string() if item.ocr_phrases else "",
        prompt_version=vlm.PROMPT_VERSION,
    )
    vlm_raw = await vlm.call(
        base_url=config.OLLAMA_BASE_URL,
        model=config.VLM_MODEL,
        image_bytes=item.image_bytes,
        prompt=prompt,
        temperature=config.VLM_TEMPERATURE,
        timeout_seconds=config.VLM_TIMEOUT_SECONDS,
        keep_alive=config.VLM_KEEP_ALIVE,
    )
    tagged = postprocess.build_tagged_image(
        vlm_raw=vlm_raw,
        ocr_phrases=item.ocr_phrases,
        source=source,
        model=model,
    )
    metadata.write(item.path, tagged)
    return TagResult(
        tagged=tagged,
        cached=False,
        elapsed_seconds=time.time() - started,
    )


async def tag_batch(
    paths: list[Path],
    *,
    force: bool = False,
    on_busy=None,
) -> AsyncIterator[tuple[Path, TagResult | None, BaseException | None]]:
    """Yield (path, result, error) for each path inside one gpu-lock hold.

    Errors during prep / consume become a non-None ``error`` field; the
    generator never raises mid-stream so a single bad image cannot abort
    the rest of the chunk. ``GpuBusy`` at acquire time is retried for the
    whole chunk per config.GPU_BUSY_RETRY_* (raises after the cap).
    """
    if not paths:
        return

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=2)
    expected_seconds = max(
        config.GPU_LOCK_EXPECTED_SECONDS,
        float(len(paths)) * 30.0,
    )

    async def producer() -> None:
        try:
            for path in paths:
                item = await _prepare_one(loop, path, force)
                await queue.put(item)
        finally:
            await queue.put(_EOS)

    elapsed_busy = 0.0
    while True:
        producer_task: asyncio.Task | None = None
        try:
            async with gpu_lock.acquire_async("vtag", expected_seconds=expected_seconds):
                log.info("batch acquired gpu-lock, %d image(s)", len(paths))
                try:
                    await ollama_lifecycle.evict_others(
                        config.OLLAMA_BASE_URL, keep=config.VLM_MODEL
                    )
                    producer_task = asyncio.create_task(producer())
                    while True:
                        item = await queue.get()
                        if item is _EOS:
                            break
                        if item.error is not None:
                            yield item.path, None, item.error
                            continue
                        try:
                            result = await _consume_one(item)
                            yield item.path, result, None
                        except BaseException as exc:
                            yield item.path, None, exc
                finally:
                    try:
                        await ollama_lifecycle.evict_model(
                            config.OLLAMA_BASE_URL, config.VLM_MODEL
                        )
                    except Exception as exc:
                        log.warning("evict_model at batch end failed: %s", exc)
                    if producer_task is not None:
                        producer_task.cancel()
                        with contextlib.suppress(BaseException):
                            await producer_task
            return
        except GpuBusy as busy:
            if on_busy is not None:
                await on_busy(busy)
            if elapsed_busy >= config.GPU_BUSY_RETRY_CAP_SECONDS:
                raise
            await asyncio.sleep(config.GPU_BUSY_RETRY_SECONDS)
            elapsed_busy += config.GPU_BUSY_RETRY_SECONDS
