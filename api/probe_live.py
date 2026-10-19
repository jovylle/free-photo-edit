"""One-shot live probe for free-photo-edit (dev only, 1 upstream call).

Usage (from free-photo-edit/):
    .venv/bin/python api/probe_live.py

Reads .env and picks the engine via FREE_PHOTO_EDIT_ENGINE (default openai:
one real edit, ~2.6¢; workers-ai stays free). Builds a 512x512 JPEG fixture
(stdlib PNG + macOS sips, no new deps), sends it through select_engine(),
prints PROBE_OK or PROBE_FAIL. Not part of the test suite.
Import-safe (probe runs only as __main__).
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import conftest  # noqa: E402,F401  (bootstraps free_photo_edit_api)
from free_photo_edit_api.engines import select_engine  # noqa: E402


def solid_png(w: int, h: int, rgb: tuple = (140, 140, 200)) -> bytes:
    """Minimal stdlib PNG encoder (no new deps): solid-color fixture."""
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)

    def chunk(tag: bytes, data: bytes) -> bytes:
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(
            ">I", zlib.crc32(c) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def png_to_jpeg(png_bytes: bytes) -> bytes:
    """macOS sips PNG -> JPEG (the inpainting backend failed to decode PNG)."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(png_bytes)
        src = f.name
    dst = src + ".jpg"
    try:
        subprocess.run(["sips", "-s", "format", "jpeg", src, "--out", dst],
                       check=True, capture_output=True)
        with open(dst, "rb") as f:
            return f.read()
    finally:
        for p in (src, dst):
            try:
                os.unlink(p)
            except OSError:
                pass


def main() -> None:
    jpeg = png_to_jpeg(solid_png(512, 512))
    assert jpeg[:2] == b"\xff\xd8", "sips did not produce a JPEG"
    try:
        out = select_engine().edit(
            jpeg, "make the sky warm sunset tones, keep everything else"
        )
        print("PROBE_OK bytes=", len(out), "sig=", out[:8].hex())
    except Exception as e:  # noqa: BLE001
        print("PROBE_FAIL", type(e).__name__, str(e)[:300])


if __name__ == "__main__":
    main()
