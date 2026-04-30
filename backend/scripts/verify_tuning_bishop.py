"""End-to-end tuning verification on Bishop.

Re-analyzes the cached Bishop PDF with the current analyzer + gemini_analyzer
code and prints a per-category Pass/NR/Fail/Deferred table. Compares the four
"EXTRA" categories from the pre-tuning regression (Cross-Sheet, E-120,
Other Electrical, PVSyst Analysis Summary) against the expectation that their
NR+Fail count drops to ~0 because the tuning converts them to Deferred.

Run from the repo root:

    cd backend
    .venv/Scripts/python scripts/verify_tuning_bishop.py

Requires GEMINI_API_KEY in backend/.env and RULES_FILE=rules_v4_draft.yaml.
Cost: one full Gemini Flash pass over the Bishop PDF (~14-page planset).
"""
from __future__ import annotations

import os
import sys
import uuid
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
sys.path.insert(0, str(BACKEND))

# Load .env so analyzer picks up GEMINI_API_KEY / RULES_FILE
try:
    from dotenv import load_dotenv
    load_dotenv(BACKEND / ".env")
except ImportError:
    pass
os.environ.setdefault("RULES_FILE", "rules_v4_draft.yaml")

import fitz  # PyMuPDF
from app import analyzer


def find_bishop_pdf() -> Path:
    """Locate bishop_90.pdf from any cached run dir."""
    runs_dir = BACKEND / "data" / "runs"
    for d in runs_dir.glob("*"):
        for pdf in d.glob("bishop*.pdf"):
            return pdf
    # Fallback: look in 2026-04-16-AI QC
    commented = BACKEND.parent / "2026-04-16-AI QC" / "Commented PDFs"
    for pdf in commented.glob("Bishop*.pdf"):
        return pdf
    raise FileNotFoundError("bishop_90.pdf not found in data/runs or Commented PDFs/")


def main() -> int:
    bishop = find_bishop_pdf()
    print(f"[*] Analyzing {bishop.name} ({bishop.stat().st_size // 1024} KB)")

    run_id = str(uuid.uuid4())
    run_dir = BACKEND / "data" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    dst = run_dir / bishop.name
    dst.write_bytes(bishop.read_bytes())

    doc = fitz.open(dst)
    pages = analyzer.extract_pages(doc)
    print(f"[*] Opened {len(pages)} pages")

    issues, _cat, _sc, _summary = analyzer.analyze_pdf(
        doc=doc,
        pages=pages,
        run_id=run_id,
        run_dir=run_dir,
        project_details={
            "project_name": "Bishop 90% (tuning verify)",
            "design_stage": "90%",
        },
        supporting_docs=None,
    )
    doc.close()

    tally: Counter = Counter()
    for iss in issues:
        tally[(iss["category"], iss["status"])] += 1

    EXTRA_WATCH = {"Cross-Sheet", "E-120", "Other Electrical", "PVSyst Analysis Summary"}

    # Pre-tuning baseline from backend/data/regression/all_20260420_153806/summary.md
    PRE = {
        "Cross-Sheet": 1,
        "E-120": 3,
        "Other Electrical": 3,
        "PVSyst Analysis Summary": 1,
    }

    cats = sorted({c for (c, _) in tally})
    print()
    print(f"{'Category':<28} {'Pass':>5} {'NR':>4} {'Fail':>5} {'Def':>5}  Note")
    print("-" * 72)
    for c in cats:
        p = tally[(c, "Pass")]
        nr = tally[(c, "Needs Review")]
        f = tally[(c, "Fail")]
        d = tally[(c, "Deferred")]
        note = ""
        if c in EXTRA_WATCH:
            pre_nr = PRE.get(c, 0)
            delta = (nr + f) - pre_nr
            sign = "+" if delta > 0 else ""
            note = f"pre={pre_nr} -> now={nr+f} ({sign}{delta})"
        print(f"{c:<28} {p:>5} {nr:>4} {f:>5} {d:>5}  {note}")

    print()
    print(f"Run id: {run_id}")
    print(f"Total issues: {len(issues)}")
    print()
    print("If all four EXTRA_WATCH categories show now=0 (or close), the tuning")
    print("successfully converted them from EXTRA noise to Deferred.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
