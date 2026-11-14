"""Boot smoke test: the documented run command must actually serve.

Runtime entrypoint is `api.server:app` (`api` is a real package with
__init__.py). The `free_photo_edit_api` name exists only inside pytest via
the conftest bootstrap, so uvicorn cannot import it — that mismatch was the
2026-10-17 run error this test guards against.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_documented_uvicorn_command_serves(tmp_path):
    port = _free_port()
    env = {**os.environ,
           "FREE_PHOTO_EDIT_DB": str(tmp_path / "credits.db"),
           "FREE_PHOTO_EDIT_STORAGE": str(tmp_path / "storage")}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "api.server:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        deadline = time.time() + 15
        last = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/api/credits", timeout=1) as r:
                    assert r.status == 200
                    return
            except Exception as e:  # noqa: BLE001
                last = e
                if proc.poll() is not None:
                    out = (proc.stdout.read() or b"").decode("utf-8", "replace")
                    pytest.fail(f"uvicorn exited: {out[:800]}")
                time.sleep(0.15)
        pytest.fail(f"server never served: {last!r}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

