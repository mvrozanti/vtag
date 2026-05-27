# vtag

Local-VLM image tagging CLI.

Tags images with a local Ollama VLM (Qwen2.5-VL) + RapidOCR, writes XMP metadata into the image and a `.tags.json` sidecar. Search results with `vfind`.

## Design

- VLM call serialized through `gpu-lock` (own GPU only; no cloud).
- Sidecar schema is versioned (`pipeline/schema.py`); `vfind` searches over sidecars without re-running the VLM.
- Pure Python; deps via `uv` (`pyproject.toml`).

## Use

```
vtag tag <path> [-r] [-f]    # tag a file or directory
vtag show <image>            # human-readable summary
vtag tags <image>            # flat tag list, one per line
vtag info <image>            # raw sidecar JSON

vfind <terms…>               # search tagged images (AND on tags)
vfind --text <substr>        # OCR substring filter
vfind --type meme            # content_type filter
vfind --since YYYY-MM-DD     # mtime cutoff
vfind -F <regex>             # regex over description/context/punchline
```

## Layout

```
cli.py            vtag entrypoint
find.py           vfind entrypoint
config.py         knobs (Ollama URL, timeouts, model)
pipeline/
  preprocess.py     probe + representative-frame extraction
  ocr.py            RapidOCR wrapper
  vlm.py            Ollama VLM call + prompt
  postprocess.py    JSON salvage + tag normalization
  schema.py         TaggedImage dataclass + caps
  sidecar.py        .tags.json + XMP write
  ollama_lifecycle  evict-others-before-load
  dispatcher.py     glue (cache → ocr → vlm → sidecar)
```

## Runtime

- Ollama on the host; model defaults in `config.py`.
- `gpu_lock` is a sibling Python package; consumers add it to `PYTHONPATH`.
- See [mandragora](https://github.com/mvrozanti/mandragora) `nix/pkgs/vtag-cli.nix` for the Nix wrapper used in production.
