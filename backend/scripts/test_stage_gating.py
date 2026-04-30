"""Smoke test: run the V4 engine's gating path on real training PDFs
at each design stage, stubbing out the AI dispatcher so no Gemini calls
happen. Counts dispatched vs deferred rules per stage.

Usage (from backend/):
    RULES_FILE=rules_v4_draft.yaml .venv/Scripts/python.exe scripts/test_stage_gating.py
"""
from __future__ import annotations

import os
import sys
from collections import Counter
from pathlib import Path

os.environ.setdefault("RULES_FILE", "rules_v4_draft.yaml")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fitz  # PyMuPDF

from app import v4_engine
from app.analyzer import extract_pages
from app.rule_registry import get_rules, stage_index

SAMPLES = {
    "30%_HILLSBORO": r"d:\code\QAQC\planset-qc-app\backend\data\runs\1677ef30-4309-44c9-88ac-d00c6d87eb1d\HILLSBORO - 30%.pdf",
    "90%_GIRARD":    r"d:\code\QAQC\planset-qc-app\backend\data\runs\475c53f8-898d-4966-955b-68fe04d34db0\Girard - 90%.pdf",
    "IFC_ZIA":       r"d:\code\QAQC\planset-qc-app\backend\data\runs\154278c0-a94e-42c9-aa77-e5f5b9520e25\247-169- Zia Hills IFC Rev1.pdf",
}

STAGES = ("30", "60", "90", "IFC")


def run_one(pdf_path: str, design_stage: str) -> dict:
    doc = fitz.open(pdf_path)
    pages = extract_pages(doc)
    rules = get_rules()

    dispatched_categories: list[tuple[str, int]] = []

    def fake_submit(func, *args, **kwargs):
        # args layout per v4_engine.run_v4_checks:
        #   page_check(..., category, key_prefix, label, deep=...) — label at args[5 or 8]
        # We only care about counting "a dispatch happened for <category>"
        # The category name is args[6] in the page_check call.
        category = args[6] if len(args) > 6 else "?"
        dispatched_categories.append((category, 1))
        return []

    deferred = v4_engine.run_v4_checks(
        doc=doc,
        pages=pages,
        rules=rules,
        submit=fake_submit,
        prompt_wrap=lambda s: s,
        deep_for=lambda _cat: False,
        page_check=lambda *a, **kw: None,
        multi_page_check=lambda *a, **kw: None,
        run_id="test",
        run_dir=Path(pdf_path).parent,
        supporting_docs=None,
        design_stage=design_stage,
    )

    doc.close()
    return {
        "pdf": Path(pdf_path).name,
        "stage": design_stage,
        "deferred_count": len(deferred),
        "deferred_by_category": Counter(d["category"] for d in deferred),
        "dispatched_categories": Counter(c for c, _ in dispatched_categories),
    }


def expected_fire_count(stage: str) -> int:
    """Number of rules that SHOULD fire at a given stage (ignoring sheet lenience)."""
    cutoff = stage_index(stage)
    return sum(1 for r in get_rules() if stage_index(r.min_stage) <= cutoff)


def main() -> None:
    print(f"Total rules loaded: {len(get_rules())}")
    print()
    for stage in STAGES:
        print(f"### Expected rules firing at stage={stage}: "
              f"{expected_fire_count(stage)} / {len(get_rules())}")
    print()

    for tag, pdf_path in SAMPLES.items():
        if not Path(pdf_path).exists():
            print(f"SKIP {tag}: {pdf_path} not found")
            continue
        print(f"\n### {tag} ({Path(pdf_path).name}) ###")
        for stage in STAGES:
            r = run_one(pdf_path, stage)
            n_dispatched = sum(r["dispatched_categories"].values())
            print(f"  stage={stage}: {n_dispatched} categor{'y' if n_dispatched == 1 else 'ies'} dispatched, "
                  f"{r['deferred_count']} rules deferred")
            if r["deferred_by_category"]:
                top5 = r["deferred_by_category"].most_common(5)
                print(f"    deferred (top 5 cats): {top5}")
            if r["dispatched_categories"]:
                print(f"    dispatched cats: {sorted(r['dispatched_categories'].keys())}")


if __name__ == "__main__":
    main()
