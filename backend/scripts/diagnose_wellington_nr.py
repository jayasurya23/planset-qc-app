"""Diagnose why Wellington IFP latest run produced 149 Needs Review findings.

Run from the repo root on Windows:

    cd backend
    .venv\Scripts\python scripts\diagnose_wellington_nr.py

Dumps:
  1. Run metadata (design_stage, page count, status mix, run_id).
  2. NR breakdown by (category, item_key) — the noisiest rules first.
  3. NR breakdown by category.
  4. NR-per-page density (catches the catch-all Other Electrical pattern).
  5. Evidence-prefix clustering (80-char buckets) — flags hedgy language
     like "cannot be confirmed", "not visible", "appears to be".
  6. Pass/Fail/NR/Deferred ratio overall.

No Gemini calls; pure read-only SQL.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
DB = BACKEND / "data" / "planset_qc.sqlite3"

HEDGY_PATTERNS = (
    "cannot be confirmed",
    "cannot be verified",
    "cannot be evaluated",
    "not in view",
    "not visible",
    "not shown",
    "not provided",
    "not included",
    "not available",
    "from this sheet alone",
    "without access to",
    "appears to be",
    "appears",
    "likely",
    "may be",
    "unclear",
    "partially obscured",
    "other sheet",
    "blurry",
    "field-by-field comparison",
)


def find_wellington_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    rows = conn.execute(
        """SELECT id, project_name, original_filename, created_at,
                  page_count, status_counts_json, project_details_json
             FROM runs
            WHERE (project_name LIKE '%Wellington%' OR
                   original_filename LIKE '%Wellington%')
            ORDER BY created_at DESC
            LIMIT 1"""
    ).fetchall()
    return rows[0] if rows else None


def bucket_evidence(text: str) -> str:
    """Bucket NR evidence into a short signature for clustering."""
    if not text:
        return "<EMPTY>"
    t = text.lower()
    for pat in HEDGY_PATTERNS:
        if pat in t:
            return f"HEDGY: {pat}"
    # Fall back to the first 80 chars as a bucket — finds repeated phrases.
    return "STATED: " + re.sub(r"\s+", " ", text).strip()[:80]


def main() -> int:
    if not DB.exists():
        print(f"ERROR: {DB} not found. Run from backend/ with the venv.")
        return 2
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    run = find_wellington_run(conn)
    if run is None:
        print("No run found with 'Wellington' in project_name or filename.")
        return 1

    run_id = run["id"]
    print("=" * 78)
    print(f"Wellington run: {run_id}")
    print(f"  Project      : {run['project_name']}")
    print(f"  File         : {run['original_filename']}")
    print(f"  Created      : {run['created_at']}")
    print(f"  Page count   : {run['page_count']}")

    try:
        details = json.loads(run["project_details_json"] or "{}")
        print(f"  design_stage : {details.get('design_stage')!r}")
        print(f"  project_name : {details.get('project_name')!r}")
    except Exception:
        print("  (project_details_json unreadable)")

    try:
        mix = json.loads(run["status_counts_json"] or "{}")
        print(f"  Status mix   : {mix}")
    except Exception:
        mix = {}
    print("=" * 78)

    issues = conn.execute(
        """SELECT category, item_key, title, status, evidence, page_number
             FROM issues
            WHERE run_id = ?""",
        (run_id,),
    ).fetchall()

    by_status = Counter(i["status"] for i in issues)
    print(f"\nRaw issue-row status mix (n={len(issues)}): {dict(by_status)}")

    nrs = [i for i in issues if i["status"] == "Needs Review"]
    print(f"Needs Review total: {len(nrs)}\n")

    # ── 1. By category ────────────────────────────────────────────────────
    print("─" * 78)
    print("NRs by category (sorted desc):")
    cat_cnt: Counter = Counter(n["category"] for n in nrs)
    for cat, c in cat_cnt.most_common():
        pct = 100.0 * c / max(1, len(nrs))
        print(f"  {cat:30s} {c:4d}   {pct:5.1f}%")

    # ── 2. By (category, item_key) — noisiest rules ───────────────────────
    print()
    print("─" * 78)
    print("Top 25 noisiest rules (category :: item_key):")
    pair_cnt: Counter = Counter((n["category"], n["item_key"]) for n in nrs)
    for (cat, key), c in pair_cnt.most_common(25):
        print(f"  {c:3d}  [{cat:20s}] {key}")

    # ── 3. Per-page NR density ─────────────────────────────────────────────
    print()
    print("─" * 78)
    print("Top 15 pages by NR count (catches catch-all Other Electrical spray):")
    page_cnt: Counter = Counter(
        (n["page_number"] or 0, n["category"]) for n in nrs
    )
    for (pg, cat), c in page_cnt.most_common(15):
        print(f"  page {pg:3d}  [{cat:25s}] {c:3d} NRs")

    # ── 4. Evidence-prefix clustering ─────────────────────────────────────
    print()
    print("─" * 78)
    print("Top 25 evidence buckets (HEDGY: prefixed buckets should be demoted):")
    bucket_cnt: Counter = Counter(bucket_evidence(n["evidence"] or "") for n in nrs)
    for bucket, c in bucket_cnt.most_common(25):
        flag = " ⚠" if bucket.startswith("HEDGY:") or bucket == "<EMPTY>" else ""
        print(f"  {c:3d}  {bucket}{flag}")

    # ── 5. Summary — how many NRs are hedgy and could be Deferred ─────────
    hedgy = sum(
        1 for n in nrs
        if bucket_evidence(n["evidence"] or "").startswith("HEDGY:")
        or not (n["evidence"] or "").strip()
    )
    print()
    print("=" * 78)
    print(f"SUMMARY:")
    print(f"  Total NRs                    : {len(nrs)}")
    print(f"  NRs with hedgy/empty evidence: {hedgy}  ({100.0*hedgy/max(1,len(nrs)):.1f}%)")
    print(f"    ↑ these are low-value; a tighter prompt + the existing")
    print(f"      NR→Deferred gate in gemini_analyzer should eliminate most.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
