# vtag

Local-VLM image tagging CLI.

Tags images with a local Ollama VLM (Qwen2.5-VL) + RapidOCR and embeds the full structured payload **directly inside each image** as XMP metadata (custom `XMP-vtag` namespace + standard `XMP-dc:Subject`/`Description`/`Title`). No sidecar files. `vfind` searches the embedded metadata, caching extractions in `~/.cache/vtag/index.sqlite`.

## Design

- VLM call serialized through `gpu-lock` (own GPU only; no cloud).
- Payload schema is versioned (`pipeline/schema.py`); the embedded JSON blob lives in `XMP-vtag:Payload` (base64-encoded), with `Sha256` + `SchemaVersion` peers for fast cache checks.
- Standard XMP fields (`dc:Subject`, `dc:Description`, `dc:Title`) carry tags / description / type so any external viewer (Lightroom, digiKam, gThumb, file managers) sees them.
- Pure Python; deps via `uv` (`pyproject.toml`). Requires `exiftool` on PATH.

## Use

```
vtag tag <path> [-r] [-f]    # tag a file or directory
vtag show <image>            # human-readable summary
vtag tags <image>            # flat tag list, one per line
vtag info <image>            # raw embedded payload as JSON
vtag migrate <path> [--delete]   # one-shot: read legacy .tags.json sidecars and embed into images

vfind <terms…>               # search tagged images (AND on tags)
vfind --text <substr>        # OCR substring filter
vfind --type meme            # content_type filter
vfind --since YYYY-MM-DD     # mtime cutoff
vfind -F <regex>             # regex over description/context/punchline
vfind --no-refresh           # query cache without rescanning files
vfind --reindex              # discard cache for the listed roots and re-read
```

## Layout

```
cli.py            vtag entrypoint
find.py           vfind entrypoint (sqlite-backed)
config.py         knobs (Ollama URL, timeouts, model)
pipeline/
  preprocess.py        probe + representative-frame extraction
  ocr.py               RapidOCR wrapper
  vlm.py               Ollama VLM call + prompt
  postprocess.py       JSON salvage + tag normalization
  schema.py            TaggedImage dataclass + caps
  metadata.py          XMP read/write via exiftool (XMP-vtag namespace + dc:* mirrors)
  exiftool_config.pl   custom XMP-vtag namespace declaration
  ollama_lifecycle.py  evict-others-before-load
  dispatcher.py        glue (cache → ocr → vlm → embed)
```

## Runtime

- Ollama on the host; model defaults in `config.py`.
- `exiftool` must be on PATH (used by both `vtag` and `vfind`).
- `gpu_lock` is a sibling Python package; consumers add it to `PYTHONPATH`.
- vfind cache: `~/.cache/vtag/index.sqlite` (override with `VTAG_CACHE_DIR`).
- See [mandragora](https://github.com/mvrozanti/mandragora) `nix/pkgs/vtag-cli.nix` for the Nix wrapper used in production.

## Format support

XMP write is attempted for: JPEG, PNG, WEBP, TIFF, GIF, MP4, MOV, MKV, WEBM. Other containers are unsupported — tagging will fail loudly rather than silently fall back to a sidecar.
