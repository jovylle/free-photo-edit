"""Pytest bootstrap: load free-photo-edit/api/ as importable package
`free_photo_edit_api` (the directory name `free-photo-edit` is not a valid
Python identifier). Mirrors photo-fusion/api/conftest.py."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

API_DIR = Path(__file__).resolve().parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_pkg = _load("free_photo_edit_api", API_DIR / "__init__.py")
_pkg.__path__ = [str(API_DIR)]
for _sub in ("prompts", "credits", "engines", "server"):
    _p = API_DIR / f"{_sub}.py"
    if _p.exists():
        _load(f"free_photo_edit_api.{_sub}", _p)
