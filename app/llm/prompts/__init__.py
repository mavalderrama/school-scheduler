"""Prompts compartidos por los tres proveedores. Un archivo .md por prompt."""

from __future__ import annotations

from functools import cache
from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent


@cache
def load_prompt(name: str) -> str:
    """Devuelve el contenido de `prompts/<name>.md` (cacheado)."""
    path = _PROMPTS_DIR / f"{name}.md"
    if not path.is_file():
        raise FileNotFoundError(f"no existe el prompt {name!r} en {_PROMPTS_DIR}")
    return path.read_text(encoding="utf-8").strip()
