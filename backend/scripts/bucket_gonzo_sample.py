"""Sample 30 of the 128 QC-engineer comments and assign a draft bucket.

Output: backend/data/gonzo_qc_bucketing_sample.csv

The CSV is for you to review:
  - reviewer_correction column is intentionally blank — fill it with the
    correct bucket if my draft is wrong, or "OK" / leave blank if right.
  - notes column for anything that doesn't fit, ambiguous cases, or rules
    we're missing entirely.

After you review, I take the corrections and either:
  (a) refine the bucket schema, then re-bucket the full 128
  (b) re-label and ship a final ground-truth file
"""
from __future__ import annotations

import csv
import json
import os
import random
import re
import sys
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    os.environ["PYTHONIOENCODING"] = "utf-8"
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
SOURCE = BACKEND / "data" / "gonzo_qc_engineer_annotations.json"
OUT = BACKEND / "data" / "gonzo_qc_bucketing_sample.csv"
SAMPLE_SIZE = 30
RANDOM_SEED = 42  # reproducible sample


# Draft bucket schema (rough first cut). The reviewer will tell us which to
# rename, merge, split, or add. Definitions intentionally short — refine
# after the sample review.
BUCKETS = {
    "cross-sheet-propagation":
        "Value/spec was updated on one sheet but not propagated to others "
        "(module qty, inverter qty, ratings).",
    "value-mismatch":
        "A number/value on the sheet looks wrong vs another source on the "
        "planset OR vs the BOD/IA.",
    "spec-wrong":
        "Specific spec value is incorrect (wrong wire size, wrong fuse "
        "rating, wrong schedule).",
    "missing-callout":
        "A required label, tag, dimension, or note isn't shown.",
    "nec-violation":
        "Code violation: clearance, ampacity, fuse, EGC sizing, conduit fill.",
    "cable-spec":
        "Cable / conduit description quibble (size, type, count, MV cable "
        "elements, schedule mismatch).",
    "cosmetic":
        "Typo, formatting, line weight, leftover stale text, scale issue.",
    "methodology":
        "Reviewer asks designer to discuss/confirm an approach (no defect "
        "asserted yet).",
    "discussion":
        "Reviewer's question or note that isn't asserting a defect.",
    "unclear":
        "Comment is too vague to bucket without additional context "
        "('FIX IF NEEDED', 'WHERE DID YOU FIND THIS').",
}


def _bucket(content: str, surrounding: str) -> tuple[str, float]:
    """Return (bucket, confidence) — confidence 0..1."""
    c = (content or "").lower()
    near = (surrounding or "").lower()

    # High-signal patterns first
    if "module distribution" in c or ("module" in c and "new" in c and "fix" in c):
        return "cross-sheet-propagation", 0.85
    if re.search(r"\bnec\b|\bclearance\b|\bampacity\b|\bfuse\b|\begc\b|\b250\.", c):
        return "nec-violation", 0.75
    if any(k in c for k in ("conduit", "mv cable", "shdl", "kcmil", "awg", "wire size")):
        return "cable-spec", 0.70
    if any(k in c for k in ("typo", "spelling", "stale", "leftover", "wrong project")):
        return "cosmetic", 0.85
    if any(k in c for k in ("discuss", "confirm with", "approach", "talk about", "consult")):
        return "methodology", 0.75
    if c.strip().endswith("?") or c.strip().startswith("why ") or c.strip().startswith("where "):
        return "discussion", 0.65
    if any(k in c for k in ("change to", "should be", "list ", "use ", "delete", "remove", "add")):
        # Could be value-mismatch, spec-wrong, or methodology — call it spec-wrong
        return "spec-wrong", 0.55
    if any(k in c for k in ("missing", "not shown", "where is", "needs ", "show ")):
        return "missing-callout", 0.65
    if c.strip() in ("fix", "fix if needed", "fix this", "recheck", "recheck and update", "update"):
        return "unclear", 0.55
    # Fallback
    return "unclear", 0.30


def main() -> int:
    records = json.loads(SOURCE.read_text(encoding="utf-8"))
    with_text = [r for r in records if (r.get("content") or "").strip()]
    print(f"Total annotations: {len(records)}")
    print(f"With comment text: {len(with_text)}")
    if len(with_text) < SAMPLE_SIZE:
        print(f"Adjusting sample to {len(with_text)} (smaller than {SAMPLE_SIZE})")
    random.seed(RANDOM_SEED)
    sample = random.sample(with_text, k=min(SAMPLE_SIZE, len(with_text)))

    rows = []
    for r in sample:
        b, conf = _bucket(r["content"], r.get("surrounding_text") or "")
        rows.append({
            "annotation_id":     r["annotation_id"],
            "page":              r["page"],
            "sheet_code":        r["sheet_code"] or "",
            "type":              r["type"],
            "comment":           r["content"],
            "surrounding_text":  (r.get("surrounding_text") or "")[:140],
            "draft_bucket":      b,
            "draft_confidence":  f"{conf:.2f}",
            "reviewer_correction": "",
            "notes":             "",
        })
    rows.sort(key=lambda x: (x["page"], x["annotation_id"]))

    with OUT.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    # Bucket distribution preview
    from collections import Counter
    dist = Counter(r["draft_bucket"] for r in rows)
    print()
    print(f"Draft bucket distribution (sample of {len(rows)}):")
    for b, n in dist.most_common():
        print(f"  {n:3d}  {b}")
    print()
    print(f"CSV written: {OUT.relative_to(BACKEND.parent)}")
    print()
    print("Bucket definitions:")
    for b, defn in BUCKETS.items():
        print(f"  {b}: {defn}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
