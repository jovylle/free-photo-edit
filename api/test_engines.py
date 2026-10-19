"""FPE-2 tests: WorkersAiEngine (mocked HTTP), PollinationsEngine 501 stub,
4-preset prompt builder. No live AI calls."""

from __future__ import annotations

import base64
import json

import pytest

from free_photo_edit_api import prompts
from free_photo_edit_api.engines import (
    EngineError,
    OpenAiEngine,
    PollinationsEngine,
    WorkersAiEngine,
    select_engine,
)

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


class FakeResp:
    def __init__(self, status=200, content=PNG):
        self.status_code = status
        self.content = content
        self.text = "" if status == 200 else "upstream boom"


class FakeClient:
    def __init__(self, resp=None):
        self.resp = resp or FakeResp()
        self.sent = {}

    def post(self, url, headers=None, data=None, files=None, json=None,
             timeout=None):
        self.sent = {"url": url, "headers": headers, "data": data,
                     "files": files, "json": json}
        return self.resp


def _engine(resp=None):
    return WorkersAiEngine(
        account_id="acct123", api_token="tok123", _client=FakeClient(resp)
    )


def test_workers_ai_returns_png_bytes():
    eng = _engine()
    out = eng.edit(b"fake-jpeg-bytes", "make the sky sunset")
    assert out.startswith(b"\x89PNG\r\n\x1a\n")
    sent = eng._client.sent
    assert "flux-2-dev" in sent["url"]
    assert sent["headers"]["Authorization"] == "Bearer tok123"
    assert "sunset" in sent["data"]["prompt"]
    assert sent["data"]["width"] == "512" and sent["data"]["height"] == "512"
    assert sent["data"]["steps"] == "25"  # docs example value; 8 gave 500s
    name, content, mime = sent["files"]["image"]
    assert content == b"fake-jpeg-bytes"
    assert mime.startswith("image/")


def test_workers_ai_sends_image_multipart():
    eng = _engine()
    eng.edit(b"fake-jpeg-bytes", "make the sky sunset")
    files = eng._client.sent["files"]
    assert "image" in files  # flux-2-dev documented image-input field
    assert eng._client.sent["json"] is None  # multipart, not JSON


def test_workers_ai_non200_raises():
    eng = _engine(FakeResp(status=500, content=b"err"))
    with pytest.raises(EngineError):
        eng.edit(b"img", "do stuff")


def test_workers_ai_missing_keys_raises(monkeypatch):
    monkeypatch.delenv("CF_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    with pytest.raises(EngineError):
        WorkersAiEngine()


def test_pollinations_stub_is_501():
    eng = PollinationsEngine()
    with pytest.raises(EngineError) as ei:
        eng.edit(b"img", "do stuff")
    assert "501" in str(ei.value) or "not-configured" in str(ei.value)


# --- openai (paid primary) ---


class JsonResp:
    def __init__(self, status=200, payload=b""):
        self.status_code = status
        self.content = payload
        self.text = payload.decode("utf-8", "replace")


def _openai(resp=None, api_key="tok123"):
    if resp is None:
        img_b64 = base64.b64encode(PNG).decode()
        resp = JsonResp(payload=json.dumps(
            {"data": [{"b64_json": img_b64}]}).encode())
    return OpenAiEngine(api_key=api_key, _client=FakeClient(resp))


def test_openai_returns_png_bytes():
    eng = _openai()
    out = eng.edit(b"fake-jpeg-bytes", "make the sky sunset")
    assert out == PNG
    sent = eng._client.sent
    assert sent["url"] == "https://api.openai.com/v1/images/edits"
    assert sent["headers"]["Authorization"] == "Bearer tok123"
    assert sent["data"]["model"] == "gpt-image-2.5-sunburst"
    assert sent["data"]["size"] == "1024x1024"
    assert sent["data"]["quality"] == "medium"
    assert "sunset" in sent["data"]["prompt"]
    name, content, mime = sent["files"]["image"]
    assert content == b"fake-jpeg-bytes"
    assert mime.startswith("image/")
    assert sent["json"] is None  # multipart, not JSON


def test_openai_omits_input_fidelity():
    # sunburst 400s on input_fidelity (probe E 2026-10-17); fidelity built-in.
    eng = _openai()
    eng.edit(b"img", "do stuff")
    assert "input_fidelity" not in eng._client.sent["data"]


def test_openai_wraps_bare_instruction():
    eng = _openai()
    eng.edit(b"img", "make it pop")
    assert eng._client.sent["data"]["prompt"].startswith("[free-photo-edit")


def test_openai_non200_raises():
    eng = _openai(JsonResp(status=400, payload=b'{"error": {"message": "x"}}'))
    with pytest.raises(EngineError):
        eng.edit(b"img", "do stuff")


def test_openai_non_json_raises():
    eng = _openai(JsonResp(payload=b"<html>oh no</html>"))
    with pytest.raises(EngineError):
        eng.edit(b"img", "do stuff")


def test_openai_missing_image_raises():
    eng = _openai(JsonResp(payload=b'{"data": []}'))
    with pytest.raises(EngineError):
        eng.edit(b"img", "do stuff")


def test_openai_missing_key_raises(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(EngineError):
        OpenAiEngine()


def test_select_engine_default_is_openai(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "tok123")
    monkeypatch.delenv("FREE_PHOTO_EDIT_ENGINE", raising=False)
    assert isinstance(select_engine(), OpenAiEngine)


def test_select_engine_switch_and_unknown(monkeypatch):
    monkeypatch.setenv("CF_ACCOUNT_ID", "acct123")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok123")
    monkeypatch.setenv("FREE_PHOTO_EDIT_ENGINE", "workers-ai")
    assert isinstance(select_engine(), WorkersAiEngine)
    monkeypatch.setenv("FREE_PHOTO_EDIT_ENGINE", "pollinations")
    assert isinstance(select_engine(), PollinationsEngine)
    monkeypatch.setenv("FREE_PHOTO_EDIT_ENGINE", "nope")
    with pytest.raises(EngineError):
        select_engine()


# --- prompts ---


def test_preset_names():
    assert set(prompts.PRESETS) == {"background", "clothes", "restore", "custom"}


def test_custom_returns_verbatim():
    assert prompts.resolve_preset("custom", "gawing beach") == "gawing beach"


def test_background_fills_detail():
    out = prompts.resolve_preset("background", "white beach")
    assert "white beach" in out


def test_background_needs_detail():
    with pytest.raises(ValueError):
        prompts.resolve_preset("background", "")


def test_clothes_and_restore_append_detail():
    assert "red dress" in prompts.resolve_preset("clothes", "red dress")
    assert "scratch" in prompts.resolve_preset("restore", "fix scratch")


def test_build_prompt_quotes_verbatim_and_invariants():
    full = prompts.build_edit_prompt("gawing formal", preset="custom")
    assert '"gawing formal"' in full
    assert "identity" in full.lower()
    assert "custom" in full or "formal" in full


def test_build_prompt_empty_raises():
    with pytest.raises(ValueError):
        prompts.build_edit_prompt("   ")
