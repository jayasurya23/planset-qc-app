"""Phase 0 tests for the chat-copilot foundations.

Covers, deterministically (no network):
  1. Calc provenance round-trip: nec_ref + calc_computed survive
     make_issue -> insert_run -> get_run against a fresh temp DB.
  2. Migration: an old-schema issues table (without the new columns) is
     upgraded in place by init_db().
  3. Ruleset fingerprint: stable file name + sha256.
  4. resolve_finding_source: >=95% of the 212 REAL item_keys from prod run
     d8a0b104 resolve to a rule or a vision family (measured baseline: 21%
     registry-only).
  5. stream_chat: provider decoupling (CHAT_* independent of AI_PROVIDER),
     delta/done event protocol, usage capture — via a stubbed OpenAI client.
  6. _pdf_pages_as_images renders a real (tiny) PDF to PNG bytes.

Run:  PYTHONPATH=backend python backend/scripts/test_phase0_chat_foundations.py
"""
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Point the DB at a throwaway dir BEFORE importing app modules.
_TMP = tempfile.mkdtemp(prefix="phase0_db_")
os.environ["PLANSET_DATA_DIR"] = _TMP
os.environ["AI_PROVIDER"] = "anthropic"   # deliberately != chat default
os.environ.pop("CHAT_PROVIDER", None)
os.environ.pop("CHAT_MODEL", None)

from app import db  # noqa: E402
from app.analyzer import make_issue  # noqa: E402
from app import gemini_client as gc  # noqa: E402
from app.rule_registry import (  # noqa: E402
    resolve_finding_source, rules_fingerprint,
)

_FAILS: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _FAILS.append(name)


# ── 1. provenance round-trip ────────────────────────────────────────────────
print("1. Calc provenance round-trip:")
db.init_db()
run_id = str(uuid.uuid4())
computed = {"string_voc_cold": 1213.4, "inverter_max_vdc": 1000.0,
            "vdc_margin_pct": -21.3}
issue = make_issue(
    run_id, "calc_string_voc_cold", "DC Line Diagram",
    "String Voc cold check", "desc", "Fail",
    evidence="String Voc(cold) 1213.4V > max Vdc 1000V (Ref: NEC 690.7)",
    nec_ref="NEC 690.7", calc_computed=computed,
)
plain = make_issue(run_id, "ai_sld_something", "AC Single Line Diagram",
                   "Vision finding", "desc", "Pass")
run = {
    "id": run_id, "project_name": "PhaseZero", "original_filename": "t.pdf",
    "created_at": "2026-07-31T00:00:00+00:00", "pdf_path": "t.pdf",
    "page_count": 1, "status_counts": {}, "categories": [],
    "summary": {"calc_inputs": {"module_voc": "49.8"},
                **{f"rules_{k}" if k in ("file", "sha256") else k: v
                   for k, v in rules_fingerprint().items()}},
}
db.insert_run(run, [issue, plain])
back = db.get_run(run_id)
bi = {i["item_key"]: i for i in back["issues"]}
check("nec_ref persisted + returned",
      bi["calc_string_voc_cold"]["nec_ref"] == "NEC 690.7")
check("calc_computed round-trips as dict",
      bi["calc_string_voc_cold"]["calc_computed"] == computed)
check("non-calc issue has None provenance (no crash)",
      bi["ai_sld_something"]["nec_ref"] is None
      and bi["ai_sld_something"]["calc_computed"] is None)
check("calc_inputs snapshot in summary",
      back["summary"]["calc_inputs"] == {"module_voc": "49.8"})

# ── 2. migration of an old-schema DB ────────────────────────────────────────
print("2. Old-schema migration:")
import sqlite3  # noqa: E402
with sqlite3.connect(db.DB_PATH) as conn:
    cols_before = {r[1] for r in conn.execute("PRAGMA table_info(issues)")}
    conn.execute("ALTER TABLE issues DROP COLUMN nec_ref")
    conn.execute("ALTER TABLE issues DROP COLUMN calc_computed_json")
db.init_db()  # must re-add them via the SELECT-probe-then-ALTER path
with sqlite3.connect(db.DB_PATH) as conn:
    cols_after = {r[1] for r in conn.execute("PRAGMA table_info(issues)")}
check("init_db() migrates missing provenance columns",
      {"nec_ref", "calc_computed_json"} <= cols_after)

# ── 3. ruleset fingerprint ──────────────────────────────────────────────────
print("3. Ruleset fingerprint:")
fp = rules_fingerprint()
check("fingerprint has file + 64-char sha256",
      fp["file"].endswith(".yaml") and len(fp["sha256"]) == 64)

# ── 4. finding-source resolution on the REAL run ────────────────────────────
print("4. resolve_finding_source vs prod run d8a0b104:")
dump = Path(r"D:\tmp\run_dump.json")
if dump.exists():
    issues = json.loads(dump.read_text(encoding="utf-8"))["issues"]
    kinds = {"rule": 0, "vision_family": 0, "unknown": 0}
    unknown_keys = []
    for i in issues:
        k = resolve_finding_source(i["item_key"])["kind"]
        kinds[k] += 1
        if k == "unknown":
            unknown_keys.append(i["item_key"])
    total = len(issues)
    rate = 100 * (kinds["rule"] + kinds["vision_family"]) / total
    print(f"     resolved {rate:.1f}%  ({kinds}, unknown={sorted(set(unknown_keys))[:6]})")
    check("deep-prefix wins (ai_gnd_deep_ -> Grounding Diagram)",
          resolve_finding_source("ai_gnd_deep_Grounding ring / ground rods")
          ["category"] == "Grounding Diagram")
    check(">=95% of real findings resolve to a source", rate >= 95.0)
else:
    check("run_dump.json present for resolution measurement", False)

# ── 5. stream_chat: decoupling + event protocol (stubbed) ───────────────────
print("5. stream_chat:")
cfg = gc.get_chat_config()
check("chat provider decoupled from AI_PROVIDER (anthropic analysis, openai chat)",
      gc.AI_PROVIDER == "anthropic" and cfg["provider"] == "openai")
check("chat model defaults to the deep OpenAI model", cfg["model"] == gc._OPENAI_MODEL_DEEP)


class _StubStream:
    """Mimics the OpenAI streaming iterator incl. the usage-only final chunk."""

    def __iter__(self):
        def d(text):
            return SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content=text))],
                usage=None)
        yield d("Hel")
        yield d("lo")
        yield SimpleNamespace(choices=[], usage=SimpleNamespace(
            prompt_tokens=120, completion_tokens=8))


class _StubCompletions:
    def __init__(self):
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return _StubStream()


stub = _StubCompletions()
gc._openai_client = SimpleNamespace(chat=SimpleNamespace(completions=stub))
gc.reset_usage()
events = list(gc.stream_chat(
    [{"role": "user", "content": "hi"}], system="You are the QC copilot."))
check("streaming requested with usage capture",
      stub.last_kwargs["stream"] is True
      and stub.last_kwargs["stream_options"] == {"include_usage": True})
check("system prompt prepended as system message",
      stub.last_kwargs["messages"][0] == {"role": "system", "content": "You are the QC copilot."})
check("delta events stream text",
      [e["text"] for e in events if e["type"] == "delta"] == ["Hel", "lo"])
done = events[-1]
check("done event carries model + usage",
      done["type"] == "done" and done["usage"] == {"prompt_tokens": 120, "completion_tokens": 8})
check("chat usage lands in shared counters",
      gc.get_usage()["total_tokens"] == 128)
check("model override honored (bake-off hook)",
      list(gc.stream_chat([{"role": "user", "content": "x"}], model="gpt-5.5"))
      and stub.last_kwargs["model"] == "gpt-5.5")

# ── 6. PDF page rendering for vision input ──────────────────────────────────
print("6. PDF -> images:")
import fitz  # noqa: E402
pdf = fitz.open()
page = pdf.new_page(width=200, height=100)
page.insert_text((20, 50), "XFMR 2500 kVA")
pdf_bytes = pdf.tobytes()
pdf.close()
imgs = gc._pdf_pages_as_images(pdf_bytes)
check("renders one PNG page", len(imgs) == 1 and imgs[0][:4] == b"\x89PNG")
check("corrupt input degrades to [] (no crash)",
      gc._pdf_pages_as_images(b"not a pdf") == [])

print()
if _FAILS:
    print(f"FAILED ({len(_FAILS)}): {_FAILS}")
    sys.exit(1)
print("ALL PHASE 0 FOUNDATION CHECKS PASSED")
