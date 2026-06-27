"""Ollama VRAM coordination: poll /api/ps, force-unload via keep_alive=0.

The implementation now lives in the shared `gpu_lock_ollama` module (single
source of truth on the gpu-lock PYTHONPATH), so llm-via-telegram and vtag
no longer carry divergent copies. This shim re-exports it under the
historical import path.
"""
from __future__ import annotations

from gpu_lock_ollama import evict_model, evict_others, wait_for_unload

__all__ = ["wait_for_unload", "evict_others", "evict_model"]
