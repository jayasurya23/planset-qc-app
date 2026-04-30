"""Bucket all 128 commented Gonzo QC-engineer annotations.

Improved bucketer using patterns confirmed during the 30-sample review:

  - "based on" / "vs" / sheet-code references in comment → cross-sheet-propagation
  - "correct?" / "different" / explicit spec keywords     → value-mismatch
  - "where is" / "label only has" / "needs to show"       → missing-callout
  - When the comment alone is generic, fall through to scan the surrounding
    text for spec/equipment context before defaulting to unclear.

Output: backend/data/gonzo_qc_full_bucketing.csv (all 128 with text).
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    os.environ["PYTHONIOENCODING"] = "utf-8"
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
SOURCE = BACKEND / "data" / "gonzo_qc_engineer_annotations.json"
OUT = BACKEND / "data" / "gonzo_qc_full_bucketing.csv"


# ── Pattern signal ──────────────────────────────────────────────────────────

SHEET_REF = re.compile(r"\b[ECMS]-\d{2,3}\b", re.I)
SPEC_KEYWORDS = re.compile(
    r"\b(fla|kva|kw|kw\b|amp|ampacity|vdc|voc|vmp|isc|imp|mppt|"
    r"breaker|model|make|datasheet|schedule|fuse|conduit|wire|cable|"
    r"egc|gec|spd|surge|recloser|xfmr|transformer|inverter|combiner|"
    r"pad|lug|rating|kva|kaic|setpoint|relay)\b",
    re.I,
)
LABEL_WORDS = re.compile(r"\b(label|callout|note|tag|legend|symbol)\b", re.I)


def _bucket(content: str, surrounding: str, sheet_code: str | None) -> tuple[str, float]:
    """Return (bucket, confidence)."""
    c = (content or "").strip()
    cl = c.lower()
    nl = (surrounding or "").lower()
    on_sheet = (sheet_code or "").upper()

    # 1. Cosmetic — typos, formatting, line weight
    if any(k in cl for k in ("typo", "spelling", "stale", "leftover from",
                              "wrong project name", "line weight", "color",
                              "different than other", "scale ann")):
        return "cosmetic", 0.85

    # 2. NEC violation — explicit code reference or known code-keyed terms
    if re.search(r"\bnec\b|\b250\.\d|\b690\.\d|\b310\.\d|\b240\.\d|\bclearance\b", cl):
        return "nec-violation", 0.85

    # 3. Cross-sheet propagation — references to other sheet OR to the site
    # plan / civil layout / BOD as the disagreement source.
    other_sheet_refs = [
        s.upper() for s in SHEET_REF.findall(c) if s.upper() != on_sheet
    ]
    if other_sheet_refs:
        return "cross-sheet-propagation", 0.85
    if any(k in cl for k in ("based on", "according to", "per the", "vs ")):
        return "cross-sheet-propagation", 0.70
    if "site plan" in cl or "civil" in cl or "x-prop" in cl:
        return "cross-sheet-propagation", 0.80

    # 4. Module-distribution change — frequent Gonzo pattern, treat as
    # cross-sheet-propagation (a value updated late and not propagated).
    if "module distribution" in cl or ("module" in cl and "new" in cl):
        return "cross-sheet-propagation", 0.85

    # 5. Missing callout — request to add a label, detail, or value.
    if any(k in cl for k in ("where is", "where's", "missing", "not shown",
                              "show ", "needs ", "should show", "label only",
                              "label has", "no callout", "no label",
                              "no detail", "needs a", "needs to be shown")):
        return "missing-callout", 0.80

    # 6. Spec wrong / value mismatch — explicit replacement requests OR
    # comparison ("different", "correct?", spec keyword + question).
    if any(k in cl for k in ("change to", "should be", "list ", "use ",
                              "delete ", "remove ", "add ", "replace ",
                              "different", "correct?", "wrong size", "should read")):
        # Distinguish: "should be X" with a number → value-mismatch
        if SPEC_KEYWORDS.search(c) or re.search(r"\d", c):
            return "value-mismatch", 0.75
        return "spec-wrong", 0.65

    # 7. Cable / conduit-specific
    if re.search(r"\bmv cable\b|\bawg\b|\bkcmil\b|\bschdl\b|\bshdl\b|"
                 r"\bschedule\s*40\b|\bschedule\s*80\b|\btray\b|\bcab\b",
                 cl):
        return "cable-spec", 0.70

    # 8. Methodology / approach — "discuss", "confirm with", "approach"
    if any(k in cl for k in ("discuss", "confirm with", "approach",
                              "talk about", "consult", "design with")):
        return "methodology", 0.75

    # 9. Discussion — questions or open-ended remarks ending in '?'
    if cl.endswith("?") or cl.startswith(("why ", "how ", "is this",
                                            "are these", "do we", "can we",
                                            "should we")):
        return "discussion", 0.65

    # 10. Generic "fix" / "check" comments — lean on surrounding text.
    short_generic = cl in (
        "fix", "fix this", "fix if needed", "recheck", "recheck and update",
        "update", "check", "check this", "verify", "review", "on", "ok",
    )
    if short_generic and surrounding:
        if SPEC_KEYWORDS.search(surrounding):
            return "value-mismatch", 0.45  # surrounding is spec-y → likely a number
        if LABEL_WORDS.search(surrounding):
            return "missing-callout", 0.45
        return "unclear", 0.35

    # 11. Spec keyword in comment with no clear verb → value-mismatch
    if SPEC_KEYWORDS.search(c):
        return "value-mismatch", 0.55

    # 12. Label-related
    if LABEL_WORDS.search(c):
        return "missing-callout", 0.55

    # Fallback
    return "unclear", 0.30


def main() -> int:
    records = json.loads(SOURCE.read_text(encoding="utf-8"))
    with_text = [r for r in records if (r.get("content") or "").strip()]
    print(f"Bucketing {len(with_text)} commented annotations…")

    rows = []
    for r in with_text:
        b, conf = _bucket(r["content"], r.get("surrounding_text") or "",
                          r.get("sheet_code"))
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

    dist = Counter(r["draft_bucket"] for r in rows)
    conf_avg = sum(float(r["draft_confidence"]) for r in rows) / len(rows)
    print()
    print(f"Final distribution ({len(rows)} comments):")
    for b, n in dist.most_common():
        print(f"  {n:3d}  ({100*n/len(rows):4.0f}%)  {b}")
    print()
    print(f"Avg confidence: {conf_avg:.2f}")
    print(f"Low-confidence rows (<0.45): "
          f"{sum(1 for r in rows if float(r['draft_confidence']) < 0.45)}")
    print()
    print(f"CSV: {OUT.relative_to(BACKEND.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
