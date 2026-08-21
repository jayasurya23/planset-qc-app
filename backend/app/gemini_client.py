"""Unified AI vision client – supports Gemini, OpenAI, and Anthropic (Claude).

Provider is selected via the ``AI_PROVIDER`` env var (``gemini``, ``openai``,
or ``anthropic``). All public functions have the same signature regardless of
provider so the rest of the codebase doesn't need to care which backend is in
use.
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

# Anthropic (Claude) settings — AI_PROVIDER=anthropic
_ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
_ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-5")
_ANTHROPIC_MODEL_DEEP = os.getenv("ANTHROPIC_MODEL_DEEP", "") or _ANTHROPIC_MODEL
# Claude requires an explicit max_tokens (hard cap on thinking + response).
_ANTHROPIC_MAX_TOKENS = int(os.getenv("ANTHROPIC_MAX_TOKENS", "16000"))


def _pick_openai_model(deep: bool) -> str:
    return _OPENAI_MODEL_DEEP if deep else _OPENAI_MODEL


def _openai_sampling_kwargs() -> dict:
    """Optional best-effort determinism control for the OpenAI calls.

    Set OPENAI_SEED (int) to pass a chat-completion ``seed`` so identical inputs
    return near-identical output — useful for reproducing a run and for clean
    before/after regression diffs. This is BEST-EFFORT per OpenAI: a model or
    infra update that changes ``system_fingerprint`` can still shift results,
    and it does not resolve genuine model judgment differences on ambiguous
    drawings. Unset -> normal sampling (default, unchanged behavior).

    ``temperature`` is intentionally not exposed: the gpt-5.x reasoning models
    ignore/reject it. Read at call time so it can be toggled without reimport.
    """
    seed = os.getenv("OPENAI_SEED")
    if seed not in (None, ""):
        try:
            return {"seed": int(seed)}
        except ValueError:
            pass
    return {}


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
            **_openai_sampling_kwargs(),
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
            **_openai_sampling_kwargs(),
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
            **_openai_sampling_kwargs(),
        )

    response = _call_with_retry(_call)
    _track_openai(response)
    return response.choices[0].message.content or ""


def _pdf_pages_as_images(file_bytes: bytes, max_pages: int = 8) -> list[bytes]:
    """Render the first *max_pages* PDF pages to PNG bytes (PyMuPDF, 1.5x).

    Same rendering approach supporting_docs.py uses. Returns [] if the PDF
    can't be opened so callers can fall back gracefully.
    """
    try:
        import fitz
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        images = []
        for i in range(min(doc.page_count, max_pages)):
            pix = doc[i].get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
            images.append(pix.tobytes("png"))
        doc.close()
        return images
    except Exception:  # noqa: BLE001 — corrupt/locked PDF: degrade, don't crash
        log.warning("PDF render for vision input failed", exc_info=True)
        return []


def _openai_document(file_bytes: bytes, mime_type: str, prompt: str, deep: bool = False) -> str:
    # OpenAI chat vision takes images, not PDFs — render pages to PNGs so the
    # model actually SEES the document (the old text-only fallback answered
    # without any document content at all).
    if mime_type == "application/pdf":
        images = _pdf_pages_as_images(file_bytes)
        if images:
            return _openai_multiple_images(images, prompt, deep=deep)
        return _openai_text(f"[Document could not be rendered]\n\n{prompt}", deep=deep)
    return _openai_page_image(file_bytes, prompt, mime_type, deep=deep)


# ═══════════════════════════════════════════════════════════════════════════
# Anthropic (Claude) backend
# ═══════════════════════════════════════════════════════════════════════════

_anthropic_client = None


def _get_anthropic():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic
        if not _ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY is not set in .env")
        # max_retries=0: transient errors are handled by our _call_with_retry
        # (the SDK's APIStatusError carries .status_code, which it checks).
        _anthropic_client = anthropic.Anthropic(
            api_key=_ANTHROPIC_API_KEY,
            timeout=float(_REQUEST_TIMEOUT),
            max_retries=0,
        )
    return _anthropic_client


def _pick_anthropic_model(deep: bool) -> str:
    return _ANTHROPIC_MODEL_DEEP if deep else _ANTHROPIC_MODEL


def _anthropic_image_block(image_bytes: bytes, mime_type: str = "image/png") -> dict:
    """Build an Anthropic base64 image content block from raw bytes."""
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": mime_type,
            "data": base64.b64encode(image_bytes).decode("utf-8"),
        },
    }


def _anthropic_text_of(response) -> str:
    """Extract text from a Messages response.

    Skips thinking blocks (adaptive thinking is on by default on claude-opus-5)
    and guards stop_reason == "refusal" — safety classifiers can decline a
    request with HTTP 200 and empty/partial content, so never index content[0]
    unconditionally.
    """
    if getattr(response, "stop_reason", None) == "refusal":
        log.warning("Anthropic request refused: %s",
                    getattr(response, "stop_details", None))
        return ""
    return "".join(
        block.text for block in response.content
        if getattr(block, "type", "") == "text"
    )


def _track_anthropic(response) -> None:
    with _usage_lock:
        _usage["api_calls"] += 1
        usage = getattr(response, "usage", None)
        if usage:
            inp = getattr(usage, "input_tokens", 0) or 0
            out = getattr(usage, "output_tokens", 0) or 0
            _usage["prompt_tokens"] += inp
            _usage["completion_tokens"] += out
            _usage["total_tokens"] += inp + out


def _anthropic_message(content, deep: bool):
    """Shared single-turn Messages call for the Claude paths."""
    client = _get_anthropic()
    model = _pick_anthropic_model(deep)

    def _call():
        return client.messages.create(
            model=model,
            max_tokens=_ANTHROPIC_MAX_TOKENS,
            messages=[{"role": "user", "content": content}],
        )

    response = _call_with_retry(_call)
    _track_anthropic(response)
    return _anthropic_text_of(response)


def _anthropic_page_image(
    image_bytes: bytes, prompt: str,
    mime_type: str = "image/png", deep: bool = False,
) -> str:
    return _anthropic_message(
        [_anthropic_image_block(image_bytes, mime_type),
         {"type": "text", "text": prompt}],
        deep,
    )


def _anthropic_multiple_images(
    images: list[bytes], prompt: str,
    mime_type: str = "image/png", deep: bool = False,
) -> str:
    content: list[dict] = [_anthropic_image_block(img, mime_type) for img in images]
    content.append({"type": "text", "text": prompt})
    return _anthropic_message(content, deep)


def _anthropic_text(prompt: str, deep: bool = False) -> str:
    return _anthropic_message(prompt, deep)


def _anthropic_document(file_bytes: bytes, mime_type: str, prompt: str, deep: bool = False) -> str:
    # Unlike OpenAI, Claude reads PDFs natively via a document content block —
    # no text-only fallback needed for supporting documents.
    if mime_type == "application/pdf":
        b64 = base64.b64encode(file_bytes).decode("utf-8")
        return _anthropic_message(
            [{"type": "document",
              "source": {"type": "base64", "media_type": "application/pdf",
                         "data": b64}},
             {"type": "text", "text": prompt}],
            deep,
        )
    return _anthropic_page_image(file_bytes, prompt, mime_type, deep=deep)


# ═══════════════════════════════════════════════════════════════════════════
# Public API – delegates to the active provider
# ═══════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════
# Image budget
#
# Every provider shrinks an image before the model sees it, so uploading a
# raster bigger than the budget costs bandwidth, latency and memory and buys
# nothing. It is also, measurably, slightly worse: rasterising far above the
# budget and letting the provider's resampler throw pixels away is softer than
# rendering the vector straight to the target size. Measured on a 3/32"
# conductor callout, mean |Laplacian| over the same 156x34 crop:
#
#     render 5184px, provider downsamples to 1152px   0.69
#     render 1152px directly                          0.74
#
# On this corpus (2592 x 1728 pt sheets) the pipeline's fixed zoom 2.0 sent
# 5184x3456 and OpenAI reduced it to 1152x768 every time: 430 KB uploaded to
# deliver 69 KB of actual information.
#
# The optimum upload is the largest image that survives preprocessing
# UNCHANGED, because no provider upscales.
# ═══════════════════════════════════════════════════════════════════════════

# OpenAI, detail="high": fit inside a 2048 square, then scale so the SHORT
# side is 768. An image is untouched when max <= 2048 and min <= 768.
OPENAI_MAX_BOX = 2048
OPENAI_SHORT_SIDE = 768

# Anthropic: long edge capped, and an overall pixel budget.
ANTHROPIC_LONG_EDGE = 1568
ANTHROPIC_MAX_PIXELS = 1_150_000

# get_pixmap rounds a dimension UP, and 150 * (768/150) can evaluate to
# 768.0000000000001, which ceils to 769 -- one pixel over the budget is enough
# to trigger the server-side rescale this function exists to avoid. Trim by a
# relative hair: enough to absorb the float error, far too small to cost a
# pixel of real resolution.
_CEIL_GUARD = 1.0 - 1e-9


def vision_zoom_for_page(
    width_pt: float, height_pt: float, provider: str | None = None,
) -> float | None:
    """Render zoom that lands a page of this size exactly on the provider's
    budget — no server-side downsample, no wasted upload.

    Returns ``None`` when the provider's preprocessing is not modelled here,
    which tells the caller to keep whatever zoom it was going to use. Being
    wrong about a provider costs real quality, so an unknown one changes
    nothing.
    """
    if width_pt <= 0 or height_pt <= 0:
        return None
    provider = (provider or AI_PROVIDER or "").lower()
    long_pt, short_pt = max(width_pt, height_pt), min(width_pt, height_pt)

    if provider == "openai":
        # Short side to 768, but never let the long side exceed the 2048 box.
        return _CEIL_GUARD * min(OPENAI_SHORT_SIDE / short_pt,
                                 OPENAI_MAX_BOX / long_pt)

    if provider == "anthropic":
        by_edge = ANTHROPIC_LONG_EDGE / long_pt
        by_area = (ANTHROPIC_MAX_PIXELS / (width_pt * height_pt)) ** 0.5
        return _CEIL_GUARD * min(by_edge, by_area)

    # Gemini and anything else: not modelled, so do not touch it.
    return None


def analyze_page_image(
    image_bytes: bytes, prompt: str,
    mime_type: str = "image/png", deep: bool = False,
) -> str:
    if AI_PROVIDER == "anthropic":
        return _anthropic_page_image(image_bytes, prompt, mime_type, deep=deep)
    if AI_PROVIDER == "openai":
        return _openai_page_image(image_bytes, prompt, mime_type, deep=deep)
    return _gemini_page_image(image_bytes, prompt, mime_type, deep=deep)


def analyze_multiple_images(
    images: list[bytes], prompt: str,
    mime_type: str = "image/png", deep: bool = False,
) -> str:
    if AI_PROVIDER == "anthropic":
        return _anthropic_multiple_images(images, prompt, mime_type, deep=deep)
    if AI_PROVIDER == "openai":
        return _openai_multiple_images(images, prompt, mime_type, deep=deep)
    return _gemini_multiple_images(images, prompt, mime_type, deep=deep)


def analyze_text(prompt: str, deep: bool = False) -> str:
    if AI_PROVIDER == "anthropic":
        return _anthropic_text(prompt, deep=deep)
    if AI_PROVIDER == "openai":
        return _openai_text(prompt, deep=deep)
    return _gemini_text(prompt, deep=deep)


def analyze_document(
    file_bytes: bytes, mime_type: str, prompt: str, deep: bool = False,
) -> str:
    if AI_PROVIDER == "anthropic":
        return _anthropic_document(file_bytes, mime_type, prompt, deep=deep)
    if AI_PROVIDER == "openai":
        return _openai_document(file_bytes, mime_type, prompt, deep=deep)
    return _gemini_document(file_bytes, mime_type, prompt, deep=deep)


# ═══════════════════════════════════════════════════════════════════════════
# Chat (streaming, multi-turn) — used by the QC copilot, NOT the analysis
# pipeline. Deliberately decoupled: CHAT_PROVIDER / CHAT_MODEL are independent
# of AI_PROVIDER so the tuned analysis stack stays frozen while the chat model
# can be chosen (and A/B'd) separately.
# ═══════════════════════════════════════════════════════════════════════════

def _chat_provider() -> str:
    return os.getenv("CHAT_PROVIDER", "openai").lower()


def _chat_model() -> str:
    m = os.getenv("CHAT_MODEL", "").strip()
    if m:
        return m
    # Default to the deep analysis model of the chosen chat provider — chat is
    # low-volume and quality-sensitive, so the stronger tier is the right floor.
    if _chat_provider() == "anthropic":
        return _ANTHROPIC_MODEL_DEEP
    return _OPENAI_MODEL_DEEP


def get_chat_config() -> dict[str, str]:
    """Chat provider/model currently in effect, for logging and the UI."""
    return {"provider": _chat_provider(), "model": _chat_model()}


def stream_chat(messages: list[dict], system: str | None = None,
                model: str | None = None):
    """Stream a multi-turn chat completion as an event generator.

    ``messages`` is a list of {"role": "user"|"assistant", "content": str}.
    ``system`` is the system prompt (kept separate because Anthropic takes it
    as a top-level param while OpenAI takes it as a leading message).
    ``model`` overrides CHAT_MODEL for one call (used by the model bake-off).

    Yields event dicts the SSE endpoint can forward directly:
        {"type": "delta", "text": "..."}     — incremental text
        {"type": "done", "model": ..., "usage": {prompt_tokens, completion_tokens}}

    No mid-stream retry: a transient failure surfaces to the caller, which can
    simply re-send the turn (chat turns are cheap and idempotent to retry).
    """
    provider = _chat_provider()
    use_model = model or _chat_model()
    if provider == "anthropic":
        yield from _anthropic_stream_chat(messages, system, use_model)
    else:
        yield from _openai_stream_chat(messages, system, use_model)


def _openai_stream_chat(messages: list[dict], system: str | None, model: str):
    client = _get_openai()
    msgs = ([{"role": "system", "content": system}] if system else []) + messages
    stream = client.chat.completions.create(
        model=model,
        messages=msgs,
        stream=True,
        # Final chunk carries usage (empty choices) when this is set.
        stream_options={"include_usage": True},
        **_openai_sampling_kwargs(),
    )
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    for chunk in stream:
        if chunk.choices:
            delta = chunk.choices[0].delta
            if delta and delta.content:
                yield {"type": "delta", "text": delta.content}
        u = getattr(chunk, "usage", None)
        if u:
            usage = {"prompt_tokens": getattr(u, "prompt_tokens", 0) or 0,
                     "completion_tokens": getattr(u, "completion_tokens", 0) or 0}
    with _usage_lock:
        _usage["api_calls"] += 1
        _usage["prompt_tokens"] += usage["prompt_tokens"]
        _usage["completion_tokens"] += usage["completion_tokens"]
        _usage["total_tokens"] += usage["prompt_tokens"] + usage["completion_tokens"]
    yield {"type": "done", "model": model, "usage": usage}


def _anthropic_stream_chat(messages: list[dict], system: str | None, model: str):
    client = _get_anthropic()
    kwargs: dict = {
        "model": model,
        "max_tokens": _ANTHROPIC_MAX_TOKENS,
        "messages": messages,
    }
    if system:
        kwargs["system"] = system
    with client.messages.stream(**kwargs) as stream:
        for text in stream.text_stream:
            yield {"type": "delta", "text": text}
        final = stream.get_final_message()
    _track_anthropic(final)
    u = getattr(final, "usage", None)
    yield {"type": "done", "model": model, "usage": {
        "prompt_tokens": getattr(u, "input_tokens", 0) or 0 if u else 0,
        "completion_tokens": getattr(u, "output_tokens", 0) or 0 if u else 0,
    }}


def get_active_models() -> dict[str, str]:
    """Return the model IDs currently in use, for logging/debugging."""
    if AI_PROVIDER == "anthropic":
        return {"provider": "anthropic", "standard": _ANTHROPIC_MODEL, "deep": _ANTHROPIC_MODEL_DEEP}
    if AI_PROVIDER == "openai":
        return {"provider": "openai", "standard": _OPENAI_MODEL, "deep": _OPENAI_MODEL_DEEP}
    return {"provider": "gemini", "standard": _GEMINI_MODEL, "deep": _GEMINI_MODEL_DEEP}


# For backwards compatibility
def get_client():
    """Return the underlying AI client (provider-dependent)."""
    if AI_PROVIDER == "openai":
        return _get_openai()
    return _get_gemini()
