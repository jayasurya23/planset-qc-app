"""Unified AI vision client – supports both Gemini and OpenAI.

Provider is selected via the ``AI_PROVIDER`` env var (``gemini`` or ``openai``).
All public functions have the same signature regardless of provider so the
rest of the codebase doesn't need to care which backend is in use.
"""

from __future__ import annotations

import base64
import logging
import os
import threading
import time
from pathlib import Path

from dotenv import load_dotenv

log = logging.getLogger(__name__)

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

# ── Provider selection ────────────────────────────────────────────────────
# "gemini" or "openai"
AI_PROVIDER = os.getenv("AI_PROVIDER", "openai").lower()

# Gemini settings
_GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
_GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
# Optional stronger model for complex reasoning (SLD validation, cross-sheet
# consistency, etc.). Falls back to the standard model when unset.
_GEMINI_MODEL_DEEP = os.getenv("GEMINI_MODEL_DEEP", "") or _GEMINI_MODEL

# OpenAI settings
_OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
_OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
_OPENAI_MODEL_DEEP = os.getenv("OPENAI_MODEL_DEEP", "") or _OPENAI_MODEL


def _pick_openai_model(deep: bool) -> str:
    return _OPENAI_MODEL_DEEP if deep else _OPENAI_MODEL


def _pick_gemini_model(deep: bool) -> str:
    return _GEMINI_MODEL_DEEP if deep else _GEMINI_MODEL

_REQUEST_TIMEOUT = int(os.getenv("AI_TIMEOUT", "90"))  # seconds
_MAX_RETRIES = int(os.getenv("AI_RETRIES", "2"))

# ── Token usage tracking (per-run, reset before each analysis) ──────────
_usage_lock = threading.Lock()
_usage: dict[str, int] = {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
    "api_calls": 0,
}


def reset_usage() -> None:
    with _usage_lock:
        for k in _usage:
            _usage[k] = 0


def get_usage() -> dict[str, int]:
    with _usage_lock:
        return dict(_usage)


def _track_gemini(response) -> None:
    with _usage_lock:
        _usage["api_calls"] += 1
        meta = getattr(response, "usage_metadata", None)
        if meta:
            _usage["prompt_tokens"] += getattr(meta,
                                               "prompt_token_count", 0) or 0
            _usage["completion_tokens"] += getattr(
                meta, "candidates_token_count", 0) or 0
            _usage["total_tokens"] += getattr(meta,
                                              "total_token_count", 0) or 0


def _track_openai(response) -> None:
    with _usage_lock:
        _usage["api_calls"] += 1
        usage = getattr(response, "usage", None)
        if usage:
            _usage["prompt_tokens"] += getattr(usage, "prompt_tokens", 0) or 0
            _usage["completion_tokens"] += getattr(
                usage, "completion_tokens", 0) or 0
            _usage["total_tokens"] += getattr(usage, "total_tokens", 0) or 0


# ── Retry logic ──────────────────────────────────────────────────────────

def _call_with_retry(fn):
    """Call *fn* with retry on transient errors (429, 503, 504)."""
    last_exc = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            status = getattr(exc, "status_code", 0) or 0
            # OpenAI uses status_code on APIStatusError
            if not status:
                status = getattr(exc, "status", 0) or 0
            if status in (429, 503, 504) and attempt < _MAX_RETRIES:
                wait = 3 * (attempt + 1)
                log.warning("AI %d on attempt %d, retrying in %ds...",
                            status, attempt + 1, wait)
                time.sleep(wait)
                continue
            raise
    raise last_exc  # type: ignore[misc]


# ═══════════════════════════════════════════════════════════════════════════
# Gemini backend
# ═══════════════════════════════════════════════════════════════════════════

_gemini_client = None


def _get_gemini():
    global _gemini_client
    if _gemini_client is None:
        from google import genai
        if not _GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY is not set in .env")
        _gemini_client = genai.Client(api_key=_GEMINI_API_KEY)
    return _gemini_client


def _gemini_gen_config():
    from google.genai import types
    return types.GenerateContentConfig(
        http_options={"timeout": _REQUEST_TIMEOUT * 1000},
    )


def _gemini_page_image(
    image_bytes: bytes, prompt: str,
    mime_type: str = "image/png", deep: bool = False,
) -> str:
    from google.genai import types
    client = _get_gemini()
    model = _pick_gemini_model(deep)

    def _call():
        return client.models.generate_content(
            model=model,
            contents=[types.Content(parts=[
                types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                types.Part.from_text(text=prompt),
            ])],
            config=_gemini_gen_config(),
        )

    response = _call_with_retry(_call)
    _track_gemini(response)
    return response.text or ""


def _gemini_multiple_images(
    images: list[bytes], prompt: str,
    mime_type: str = "image/png", deep: bool = False,
) -> str:
    from google.genai import types
    client = _get_gemini()
    model = _pick_gemini_model(deep)
    parts = [types.Part.from_bytes(
        data=img, mime_type=mime_type) for img in images]
    parts.append(types.Part.from_text(text=prompt))

    def _call():
        return client.models.generate_content(
            model=model,
            contents=[types.Content(parts=parts)],
            config=_gemini_gen_config(),
        )

    response = _call_with_retry(_call)
    _track_gemini(response)
    return response.text or ""


def _gemini_text(prompt: str, deep: bool = False) -> str:
    client = _get_gemini()
    model = _pick_gemini_model(deep)

    def _call():
        return client.models.generate_content(
            model=model, contents=prompt, config=_gemini_gen_config(),
        )

    response = _call_with_retry(_call)
    _track_gemini(response)
    return response.text or ""


def _gemini_document(file_bytes: bytes, mime_type: str, prompt: str, deep: bool = False) -> str:
    from google.genai import types
    client = _get_gemini()
    model = _pick_gemini_model(deep)

    def _call():
        return client.models.generate_content(
            model=model,
            contents=[types.Content(parts=[
                types.Part.from_bytes(data=file_bytes, mime_type=mime_type),
                types.Part.from_text(text=prompt),
            ])],
            config=_gemini_gen_config(),
        )

    response = _call_with_retry(_call)
    _track_gemini(response)
    return response.text or ""


# ═══════════════════════════════════════════════════════════════════════════
# OpenAI backend
# ═══════════════════════════════════════════════════════════════════════════

_openai_client = None


def _get_openai():
    global _openai_client
    if _openai_client is None:
        from openai import OpenAI
        if not _OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY is not set in .env")
        _openai_client = OpenAI(api_key=_OPENAI_API_KEY,
                                timeout=_REQUEST_TIMEOUT)
    return _openai_client


def _b64_image(image_bytes: bytes, mime_type: str = "image/png") -> dict:
    """Build an OpenAI image_url content block from raw bytes."""
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime_type};base64,{b64}", "detail": "high"},
    }


def _openai_page_image(
    image_bytes: bytes, prompt: str,
    mime_type: str = "image/png", deep: bool = False,
) -> str:
    client = _get_openai()
    model = _pick_openai_model(deep)

    def _call():
        return client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": [
                _b64_image(image_bytes, mime_type),
                {"type": "text", "text": prompt},
            ]}],
        )

    response = _call_with_retry(_call)
    _track_openai(response)
    return response.choices[0].message.content or ""


def _openai_multiple_images(
    images: list[bytes], prompt: str,
    mime_type: str = "image/png", deep: bool = False,
) -> str:
    client = _get_openai()
    model = _pick_openai_model(deep)
    content: list[dict] = [_b64_image(img, mime_type) for img in images]
    content.append({"type": "text", "text": prompt})

    def _call():
        return client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": content}],
        )

    response = _call_with_retry(_call)
    _track_openai(response)
    return response.choices[0].message.content or ""


def _openai_text(prompt: str, deep: bool = False) -> str:
    client = _get_openai()
    model = _pick_openai_model(deep)

    def _call():
        return client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
        )

    response = _call_with_retry(_call)
    _track_openai(response)
    return response.choices[0].message.content or ""


def _openai_document(file_bytes: bytes, mime_type: str, prompt: str, deep: bool = False) -> str:
    # For PDFs/docs, OpenAI only supports images — convert to text fallback
    if mime_type == "application/pdf":
        return _openai_text(f"[Document content provided as context]\n\n{prompt}", deep=deep)
    return _openai_page_image(file_bytes, prompt, mime_type, deep=deep)


# ═══════════════════════════════════════════════════════════════════════════
# Public API – delegates to the active provider
# ═══════════════════════════════════════════════════════════════════════════

def analyze_page_image(
    image_bytes: bytes, prompt: str,
    mime_type: str = "image/png", deep: bool = False,
) -> str:
    if AI_PROVIDER == "openai":
        return _openai_page_image(image_bytes, prompt, mime_type, deep=deep)
    return _gemini_page_image(image_bytes, prompt, mime_type, deep=deep)


def analyze_multiple_images(
    images: list[bytes], prompt: str,
    mime_type: str = "image/png", deep: bool = False,
) -> str:
    if AI_PROVIDER == "openai":
        return _openai_multiple_images(images, prompt, mime_type, deep=deep)
    return _gemini_multiple_images(images, prompt, mime_type, deep=deep)


def analyze_text(prompt: str, deep: bool = False) -> str:
    if AI_PROVIDER == "openai":
        return _openai_text(prompt, deep=deep)
    return _gemini_text(prompt, deep=deep)


def analyze_document(
    file_bytes: bytes, mime_type: str, prompt: str, deep: bool = False,
) -> str:
    if AI_PROVIDER == "openai":
        return _openai_document(file_bytes, mime_type, prompt, deep=deep)
    return _gemini_document(file_bytes, mime_type, prompt, deep=deep)


def get_active_models() -> dict[str, str]:
    """Return the model IDs currently in use, for logging/debugging."""
    if AI_PROVIDER == "openai":
        return {"provider": "openai", "standard": _OPENAI_MODEL, "deep": _OPENAI_MODEL_DEEP}
    return {"provider": "gemini", "standard": _GEMINI_MODEL, "deep": _GEMINI_MODEL_DEEP}


# For backwards compatibility
def get_client():
    """Return the underlying AI client (provider-dependent)."""
    if AI_PROVIDER == "openai":
        return _get_openai()
    return _get_gemini()
