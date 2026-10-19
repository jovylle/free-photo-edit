"""Engine abstraction for free-photo-edit.

OpenAiEngine (primary, paid): OpenAI images/edits, server-side only.
  POST https://api.openai.com/v1/images/edits multipart: image + prompt +
  model gpt-image-2.5-sunburst + size 1024x1024 + quality medium (~2.6¢/edit).
  input_fidelity is NEVER sent (sunburst 400s on it; fidelity is built-in).
  Key OPENAI_API_KEY from env, server-side only.

WorkersAiEngine (free fallback): Cloudflare Workers AI REST API.
  POST https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{model}
  JSON body: prompt + image_b64 + num_steps + strength + guidance + width/height.
  Default model @cf/black-forest-labs/flux-2-dev. Override via WORKERS_AI_MODEL.
  Kept behind FREE_PHOTO_EDIT_ENGINE=workers-ai: its live edit path is broken.

PollinationsEngine (v1 stub): returns 501 not-configured so the fallback
slots in later without a rewrite.

SPIKE outcome (2026-10-11, verified against docs + live probes):
- schnell = text-to-image only (no image input). Unusable.
- SDXL-base documents image_b64 but live graph rejects it (400 code 3030).
- SD-v1.5-inpainting documents image_b64 + optional mask, but live graph
  demands mask_image AND its backend rejects valid PNG+JPEG uploads
  (500 UnidentifiedImageError). REST img2img path effectively broken.
- flux-2-dev is the only Workers AI model with documented image inputs via
  multipart — but its endpoint 500s on image input as of 2026-10-17.
- Auth: needs token with Workers AI Read+Edit (wrangler token → 401).
- 2026-10-17: OpenAI gpt-image-1-mini wired as default engine (reliable,
  ~1.3¢/edit, 1024px). Switch via FREE_PHOTO_EDIT_ENGINE.
- 2026-10-17 fidelity probes, same photo + app-exact prompts (mini outputs
  repainted faces/garbled ID+phone text): mini+input_fidelity=high -> 400
  invalid_input_fidelity_model (mini supports only low); 1.5+high -> 9.09c
  but zoom-cropped the ID card out; sunburst medium -> full scene kept,
  text readable, 2.64c; sunburst+input_fidelity=high -> 400 (omit it).
  Default is now gpt-image-2.5-sunburst (~2.6c/edit).
"""

from __future__ import annotations

import base64
import json
import os

import httpx

from .prompts import build_edit_prompt

DEFAULT_MODEL = "@cf/black-forest-labs/flux-2-dev"
SIZE = 512
FLUX_STEPS = 25
OPENAI_URL = "https://api.openai.com/v1/images/edits"
OPENAI_MODEL = "gpt-image-2.5-sunburst"
OPENAI_SIZE = "1024x1024"
OPENAI_QUALITY = "medium"

PNG_SIG = b"\x89PNG\r\n\x1a\n"
JPEG_SIG = b"\xff\xd8\xff"


def _sniff_mime(raw: bytes) -> str:
    if raw.startswith(JPEG_SIG):
        return "image/jpeg"
    if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def _sniff_ext(raw: bytes) -> str:
    return {"image/jpeg": ".jpg", "image/webp": ".webp"}.get(
        _sniff_mime(raw), ".png")


class EngineError(RuntimeError):
    pass


class BaseEngine:
    name = "base"

    def edit(self, input_bytes: bytes, prompt: str) -> bytes:
        raise NotImplementedError


class OpenAiEngine(BaseEngine):
    """OpenAI images/edits (gpt-image-2.5-sunburst). Key from env, server-side only."""

    name = "openai"
    TIMEOUT_S = 300

    def __init__(self, api_key: str | None = None, _client=None):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        if not self.api_key:
            raise EngineError("OPENAI_API_KEY is not set")
        self._client = _client  # injectable for tests

    def edit(self, input_bytes: bytes, prompt: str) -> bytes:
        if not input_bytes:
            raise EngineError("no input image provided")
        full_prompt = (
            prompt if prompt.startswith("[free-photo-edit")
            else build_edit_prompt(prompt)
        )
        headers = {"Authorization": f"Bearer {self.api_key}"}
        data = {"model": OPENAI_MODEL, "prompt": full_prompt,
                "size": OPENAI_SIZE, "quality": OPENAI_QUALITY}
        files = {"image": ("input" + _sniff_ext(input_bytes), input_bytes,
                           _sniff_mime(input_bytes))}
        try:
            if self._client is None:
                with httpx.Client(timeout=self.TIMEOUT_S) as c:
                    resp = c.post(OPENAI_URL, headers=headers,
                                  data=data, files=files)
            else:
                resp = self._client.post(OPENAI_URL, headers=headers,
                                         data=data, files=files,
                                         timeout=self.TIMEOUT_S)
        except Exception as e:
            raise EngineError(f"openai request failed: {type(e).__name__}") from e
        if resp.status_code != 200:
            snippet = (getattr(resp, "text", "") or "")[:500]
            raise EngineError(f"openai error: HTTP {resp.status_code}: {snippet}")
        return self._extract_bytes(resp)

    @staticmethod
    def _extract_bytes(resp) -> bytes:
        content = getattr(resp, "content", b"") or b""
        try:
            payload = json.loads(content.decode("utf-8", "replace"))
            img_b64 = ((payload.get("data") or [{}])[0] or {}).get("b64_json")
        except Exception:
            img_b64 = None
        if not isinstance(img_b64, str) or not img_b64:
            raise EngineError("openai error: no image in response")
        try:
            return base64.b64decode(img_b64)
        except Exception as e:
            raise EngineError("openai error: bad base64 image") from e


class WorkersAiEngine(BaseEngine):
    """Workers AI img2img via REST. Keys from env, server-side only."""

    name = "workers-ai"
    TIMEOUT_S = 300

    def __init__(self, account_id: str | None = None,
                 api_token: str | None = None,
                 model: str | None = None,
                 _client=None):
        self.account_id = account_id or os.environ.get("CF_ACCOUNT_ID", "")
        self.api_token = api_token or os.environ.get("CLOUDFLARE_API_TOKEN", "")
        if not self.account_id or not self.api_token:
            raise EngineError("CF_ACCOUNT_ID / CLOUDFLARE_API_TOKEN is not set")
        self.model = model or os.environ.get("WORKERS_AI_MODEL", DEFAULT_MODEL)
        self._client = _client  # injectable for tests

    def _url(self) -> str:
        return (
            f"https://api.cloudflare.com/client/v4/accounts/"
            f"{self.account_id}/ai/run/{self.model}"
        )

    def edit(self, input_bytes: bytes, prompt: str) -> bytes:
        if not input_bytes:
            raise EngineError("no input image provided")
        full_prompt = (
            prompt if prompt.startswith("[free-photo-edit")
            else build_edit_prompt(prompt)
        )
        headers = {"Authorization": f"Bearer {self.api_token}"}
        data = {"prompt": full_prompt, "width": str(SIZE),
                "height": str(SIZE), "steps": str(FLUX_STEPS)}
        files = {"image": ("input" + _sniff_ext(input_bytes), input_bytes,
                           _sniff_mime(input_bytes))}
        try:
            if self._client is None:
                with httpx.Client(timeout=self.TIMEOUT_S) as c:
                    resp = c.post(self._url(), headers=headers,
                                  data=data, files=files)
            else:
                resp = self._client.post(self._url(), headers=headers,
                                         data=data, files=files,
                                         timeout=self.TIMEOUT_S)
        except Exception as e:
            raise EngineError(f"workers-ai request failed: {type(e).__name__}") from e
        if resp.status_code != 200:
            snippet = (getattr(resp, "text", "") or "")[:500]
            raise EngineError(f"workers-ai error: HTTP {resp.status_code}: {snippet}")
        raw = self._extract_bytes(resp)
        if not raw:
            raise EngineError("workers-ai error: empty response")
        return raw

    @staticmethod
    def _extract_bytes(resp) -> bytes:
        content = getattr(resp, "content", b"") or b""
        ctype = ""
        try:
            ctype = (resp.headers.get("content-type", "") or "").lower()
        except Exception:
            pass
        if "application/json" in ctype:
            try:
                payload = json.loads(content.decode("utf-8", "replace"))
            except ValueError:
                return content
            img_b64 = (payload.get("result") or {}).get("image")
            if isinstance(payload.get("result"), str):
                img_b64 = payload["result"]  # some models return raw b64
            if img_b64:
                import base64
                try:
                    return base64.b64decode(img_b64)
                except Exception:
                    return b""
            return b""
        return bytes(content)


class PollinationsEngine(BaseEngine):
    """Fallback stub (v1): 501 until a Seed key is wired."""

    name = "pollinations"

    def edit(self, input_bytes: bytes, prompt: str) -> bytes:
        raise EngineError("501 not-configured: PollinationsEngine stub (v1)")


def select_engine() -> BaseEngine:
    """Env switch: FREE_PHOTO_EDIT_ENGINE = openai (default) | workers-ai | pollinations."""
    key = (os.environ.get("FREE_PHOTO_EDIT_ENGINE") or "openai").strip().lower()
    if key == "openai":
        return OpenAiEngine()
    if key == "workers-ai":
        return WorkersAiEngine()
    if key == "pollinations":
        return PollinationsEngine()
    raise EngineError(f"unknown FREE_PHOTO_EDIT_ENGINE '{key}'")
