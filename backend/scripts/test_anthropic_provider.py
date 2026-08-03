"""Tests for the Anthropic (Claude) provider in gemini_client.

Deterministic — no network calls. Verifies provider dispatch, content-block
shapes, refusal guarding, thinking-block skipping, and usage tracking by
stubbing the Anthropic client. A live smoke test runs only when
ANTHROPIC_API_KEY is set.

Run:  PYTHONPATH=backend python backend/scripts/test_anthropic_provider.py
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ["AI_PROVIDER"] = "anthropic"
import app.gemini_client as gc  # noqa: E402

_FAILS: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _FAILS.append(name)


# ── stub client capturing requests ──────────────────────────────────────────
class _StubMessages:
    def __init__(self, response):
        self.response = response
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return self.response


def _resp(blocks, stop_reason="end_turn", inp=100, out=20):
    return SimpleNamespace(
        content=blocks, stop_reason=stop_reason, stop_details=None,
        usage=SimpleNamespace(input_tokens=inp, output_tokens=out),
    )


def _text_block(t):
    return SimpleNamespace(type="text", text=t)


def _thinking_block():
    return SimpleNamespace(type="thinking", thinking="")


print("Provider selection:")
check("AI_PROVIDER resolves to anthropic", gc.AI_PROVIDER == "anthropic")
models = gc.get_active_models()
check("get_active_models reports anthropic", models["provider"] == "anthropic")
check("default model is claude-opus-5",
      models["standard"] == "claude-opus-5" and models["deep"] == "claude-opus-5")

print("Request shape (stubbed client):")
stub = _StubMessages(_resp([_thinking_block(), _text_block("OK")]))
gc._anthropic_client = SimpleNamespace(messages=stub)
gc.reset_usage()

out = gc.analyze_page_image(b"\x89PNG-fake", "Describe this sheet.")
kw = stub.last_kwargs
check("dispatches to Claude messages.create", kw is not None)
check("model + max_tokens set",
      kw["model"] == "claude-opus-5" and kw["max_tokens"] == 16000)
blocks = kw["messages"][0]["content"]
check("image block shape (base64 source)",
      blocks[0]["type"] == "image" and blocks[0]["source"]["type"] == "base64"
      and blocks[0]["source"]["media_type"] == "image/png")
check("prompt rides as trailing text block",
      blocks[-1] == {"type": "text", "text": "Describe this sheet."})
check("thinking block skipped, text extracted", out == "OK")

print("Multi-image + text + document paths:")
gc.analyze_multiple_images([b"a", b"b", b"c"], "Compare sheets.")
check("3 image blocks + 1 text block",
      len(stub.last_kwargs["messages"][0]["content"]) == 4)
gc.analyze_text("Plain question")
check("text-only content is a plain string",
      stub.last_kwargs["messages"][0]["content"] == "Plain question")
gc.analyze_document(b"%PDF-1.4 fake", "application/pdf", "Read this datasheet.")
doc = stub.last_kwargs["messages"][0]["content"][0]
check("PDF sent as native document block (not text fallback)",
      doc["type"] == "document" and doc["source"]["media_type"] == "application/pdf")

print("Refusal guard + usage tracking:")
stub.response = _resp([], stop_reason="refusal", inp=50, out=0)
out = gc.analyze_text("anything")
check("refusal returns empty string (no crash on empty content)", out == "")
u = gc.get_usage()
check("usage accumulated across calls (input+output→prompt/completion/total)",
      u["api_calls"] == 5 and u["prompt_tokens"] == 450
      and u["completion_tokens"] == 80 and u["total_tokens"] == 530)

# ── optional live probe ─────────────────────────────────────────────────────
if os.getenv("ANTHROPIC_API_KEY"):
    gc._anthropic_client = None  # rebuild real client
    try:
        reply = gc.analyze_text("Reply with exactly the word OK.")
        print(f"  LIVE  model={models['standard']} reply={reply[:20]!r}")
    except Exception as e:  # noqa: BLE001
        print(f"  LIVE-FAIL  {type(e).__name__}: {str(e)[:160]}")
        _FAILS.append("live probe")
else:
    print("  (live probe skipped — ANTHROPIC_API_KEY not set)")

print()
if _FAILS:
    print(f"FAILED ({len(_FAILS)}): {_FAILS}")
    sys.exit(1)
print("ALL ANTHROPIC PROVIDER CHECKS PASSED")
