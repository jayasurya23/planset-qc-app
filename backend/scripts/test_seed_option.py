"""Verify the OPENAI_SEED option: helper logic + live acceptance by the model.

Run: PYTHONPATH=backend python backend/scripts/test_seed_option.py
"""
import os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app.gemini_client as gc  # noqa: E402  (loads backend/.env on import)

# --- helper unit checks (deterministic, no network) ---
os.environ.pop("OPENAI_SEED", None)
assert gc._openai_sampling_kwargs() == {}, "unset -> {}"
os.environ["OPENAI_SEED"] = "42"
assert gc._openai_sampling_kwargs() == {"seed": 42}, "set -> seed"
os.environ["OPENAI_SEED"] = "not-an-int"
assert gc._openai_sampling_kwargs() == {}, "bad value -> {} (no crash)"
print("HELPER_OK  unset={}, '42'->seed=42, bad->{} (ignored)")

# --- live probe: does the configured model accept `seed`? ---
os.environ["OPENAI_SEED"] = "42"
model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
try:
    out = gc.analyze_text("Reply with exactly the word OK.")  # real path: create(model, messages, seed=42)
    print(f"SEED_ACCEPTED  model={model}  reply={out[:30]!r}")
except Exception as e:
    print(f"SEED_REJECTED  model={model}  {type(e).__name__}: {str(e)[:220]}")
