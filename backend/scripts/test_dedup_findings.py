"""Unit tests for consolidate_overlapping_findings (dedup_findings.py).

Locks the behavior that collapses same-concept, multi-engine duplicate findings
(e.g. a grounding item flagged by ai_gnd_ + ai_gnd_deep_ + gnd_) into one — while
NEVER hiding a real Fail and NEVER merging genuinely distinct same-engine items.

Usage (from backend/):
    python scripts/test_dedup_findings.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.dedup_findings import (  # noqa: E402
    concept_signature,
    consolidate_overlapping_findings,
    _engine_family,
)

_passed = 0
_failed = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {name}")
    else:
        _failed += 1
        print(f"  FAIL  {name}  {extra}")


def f(category, item_key, title, status="Needs Review", page=1, evidence="", conf=0.7):
    return {
        "category": category, "item_key": item_key, "title": title, "status": status,
        "page_number": page, "evidence": evidence, "confidence": conf, "description": "",
    }


# ── 1. The grounding triple-fire → 1 ────────────────────────────────────────
mbj = [
    f("Grounding Diagram", "ai_gnd_Main Bonding Jumper", "Main Bonding Jumper — Validate per NEC 250.102(C)(1)"),
    f("Grounding Diagram", "ai_gnd_deep_Main bonding jumper sizing", "Main bonding jumper sizing per NEC 250.102(C)(1)"),
    f("Grounding Diagram", "gnd_bonding_content", "Main bonding jumper shown and sized per NEC 250.102"),
]
out = consolidate_overlapping_findings(mbj)
check("triple-fire MBJ merged to 1", len(out) == 1, str(len(out)))
check("merged status stays Needs Review", out and out[0]["status"] == "Needs Review")
check("consolidation note added", out and "Consolidated 3" in (out[0]["description"] or ""))


# ── 2. A real Fail is never hidden behind a duplicate ───────────────────────
mix = [
    f("Grounding Diagram", "ai_gnd_GEC", "Grounding Electrode Conductor (GEC) per NEC 250.66", status="Needs Review"),
    f("Grounding Diagram", "gnd_gec_content", "GEC sized per NEC 250.66", status="Fail"),
]
out = consolidate_overlapping_findings(mix)
check("GEC pair merged to 1", len(out) == 1)
check("Fail preserved over Needs Review", out and out[0]["status"] == "Fail")


# ── 3. Same concept but SAME engine → not merged ────────────────────────────
same_engine = [
    f("Grounding Diagram", "ai_gnd_deep_Transformer primary grounding connection", "Transformer primary grounding connection"),
    f("Grounding Diagram", "ai_gnd_deep_Transformer secondary grounding connection", "Transformer secondary grounding connection"),
]
out = consolidate_overlapping_findings(same_engine)
check("single-engine same-concept NOT merged", len(out) == 2, str(len(out)))


# ── 4. No recognized concept → passthrough untouched ────────────────────────
nosig = [
    f("Cover Sheet", "ai_cover_DER Number", "DER Number"),
    f("Title Block", "ai_tb_Sheet Number", "Sheet Number"),
]
out = consolidate_overlapping_findings(nosig)
check("no-signature findings pass through", len(out) == 2)


# ── 5. EGC and GEC must NOT be conflated ────────────────────────────────────
egc_gec = [
    f("Grounding Diagram", "ai_gnd_EGC", "Equipment Grounding Conductor (EGC) per NEC 250.122"),
    f("Grounding Diagram", "gnd_egc", "Equipment grounding conductor sizing"),
    f("Grounding Diagram", "ai_gnd_GEC", "Grounding Electrode Conductor (GEC) per NEC 250.66"),
    f("Grounding Diagram", "gnd_gec", "Grounding electrode conductor per NEC 250.66"),
]
out = consolidate_overlapping_findings(egc_gec)
check("EGC and GEC stay 2 distinct groups", len(out) == 2, str(len(out)))


# ── 6. Different category, same concept → not merged together ────────────────
cross_cat = [
    f("Grounding Diagram", "ai_gnd_MBJ", "Main bonding jumper per NEC 250.102"),
    f("Three Line Diagram", "ai_3ld_MBJ", "Main bonding jumper per NEC 250.102"),
]
out = consolidate_overlapping_findings(cross_cat)
check("same concept in different categories NOT merged", len(out) == 2)


# ── 7. Order preserved with an interleaved merge ────────────────────────────
ordered = [
    f("Cover Sheet", "ai_cover_X", "Cover thing A"),
    f("Grounding Diagram", "ai_gnd_MBJ", "Main bonding jumper per NEC 250.102"),
    f("Grounding Diagram", "gnd_mbj", "Main bonding jumper sized per NEC 250.102"),
    f("Labels", "ai_labels_Y", "Label thing B"),
]
out = consolidate_overlapping_findings(ordered)
check("order preserved; merge keeps first position",
      [o["item_key"] for o in out] == ["ai_cover_X", "ai_gnd_MBJ", "ai_labels_Y"], str([o["item_key"] for o in out]))


# ── 8. Helpers ──────────────────────────────────────────────────────────────
check("engine family ai_gnd", _engine_family("ai_gnd_Main Bonding Jumper") == "ai_gnd")
check("engine family ai_gnd_deep", _engine_family("ai_gnd_deep_X") == "ai_gnd_deep")
check("engine family gnd", _engine_family("gnd_bonding_content") == "gnd")
check("NEC signature requires the word NEC", concept_signature("X", "spacing is 250.5 ft") is None)
check("mapped NEC (250.66) unifies to GEC concept",
      concept_signature("Grounding Diagram", "sized per NEC 250.66") == "Grounding Diagram::grounding_electrode_conductor")
check("unmapped NEC falls back to nec: reference",
      concept_signature("Electrical Sheet", "comply with NEC 705.12") == "Electrical Sheet::nec:705.12")
check("keyword signature",
      concept_signature("Grounding Diagram", "Main Bonding Jumper present") == "Grounding Diagram::main_bonding_jumper")
check("transformer grounding keyword",
      concept_signature("Grounding Diagram", "Transformer secondary grounding connection") == "Grounding Diagram::transformer_grounding")


print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
