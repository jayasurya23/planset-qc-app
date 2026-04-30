"""Compare our app's Gonzo findings against the 47 high-confidence
QC-engineer-bucketed annotations.

Usage from backend/:
    .venv/Scripts/python scripts/compare_gonzo_findings.py [run_id_prefix]

If no run_id given, uses the most recent run whose project_name starts
with "Gonzo".

Goal: surface the *bucket-level* gaps so we can talk about systemic
fixes, not per-defect overfitting. Per the rules we agreed on:
  - Need ≥3 missed defects in a bucket before drawing conclusions
  - Validate any rule change against the regression harness afterwards
"""
from __future__ import annotations

import csv
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    os.environ["PYTHONIOENCODING"] = "utf-8"
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
DB = BACKEND / "data" / "planset_qc.sqlite3"
BUCKETS_CSV = BACKEND / "data" / "gonzo_qc_full_bucketing.csv"

# Buckets that represent real defects (vs discussion / unclear / methodology).
DEFECT_BUCKETS = {
    "value-mismatch", "missing-callout", "spec-wrong",
    "cross-sheet-propagation", "cable-spec", "nec-violation", "cosmetic",
}


def load_bucketed() -> list[dict]:
    rows = []
    with BUCKETS_CSV.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            r["page"] = int(r["page"])
            rows.append(r)
    return rows


def find_run(prefix: str | None) -> tuple[str, str] | None:
    con = sqlite3.connect(DB)
    if prefix:
        row = con.execute(
            "SELECT id, project_name FROM runs WHERE id LIKE ? "
            "ORDER BY created_at DESC LIMIT 1", (prefix + "%",),
        ).fetchone()
    else:
        row = con.execute(
            "SELECT id, project_name FROM runs WHERE LOWER(project_name) LIKE 'gonzo%' "
            "ORDER BY created_at DESC LIMIT 1",
        ).fetchone()
    con.close()
    return row


def load_findings(run_id: str) -> list[dict]:
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT category, item_key, title, status, severity, evidence, "
        "page_number FROM issues WHERE run_id=? ORDER BY page_number, item_key",
        (run_id,),
    ).fetchall()
    con.close()
    return [
        {"category": r[0], "item_key": r[1], "title": r[2], "status": r[3],
         "severity": r[4], "evidence": r[5], "page": r[6]}
        for r in rows
    ]


def main() -> int:
    prefix = sys.argv[1] if len(sys.argv) > 1 else None
    run = find_run(prefix)
    if not run:
        print("No Gonzo run found. Submit one first.", file=sys.stderr)
        return 2
    run_id, project = run
    print(f"Run: {run_id[:8]}  {project}")
    print()

    bucketed = load_bucketed()
    findings = load_findings(run_id)

    # ── Findings summary ─────────────────────────────────────────────────
    finding_status = Counter(f["status"] for f in findings)
    print(f"App findings ({len(findings)} total): {dict(finding_status)}")

    # Just the ones that flag something actionable
    actionable = [f for f in findings if f["status"] in ("Fail", "Needs Review")]
    print(f"Actionable (Fail/NR): {len(actionable)}")
    print()

    # ── Ground-truth bucket distribution ─────────────────────────────────
    gt_by_bucket = defaultdict(list)
    for r in bucketed:
        gt_by_bucket[r["draft_bucket"]].append(r)
    defect_total = sum(len(v) for k, v in gt_by_bucket.items() if k in DEFECT_BUCKETS)
    print(f"Ground truth: {defect_total} confirmed-defect annotations "
          f"(of {len(bucketed)} total — others are discussion/unclear).")
    for b, items in sorted(gt_by_bucket.items(), key=lambda x: -len(x[1])):
        marker = " *" if b in DEFECT_BUCKETS else "  "
        print(f"  {marker} {len(items):3d}  {b}")
    print()

    # ── Page-level overlap (lightweight proxy for recall) ────────────────
    # Real recall = "did our finding correspond to the same defect?" That's a
    # text-similarity problem we can't fully automate. As a proxy: did we
    # produce ANY actionable finding on the same page as a ground-truth
    # defect? If we never even looked at a page where a defect lived,
    # that's a clear miss.
    gt_pages_by_bucket = defaultdict(set)
    for r in bucketed:
        if r["draft_bucket"] in DEFECT_BUCKETS:
            gt_pages_by_bucket[r["draft_bucket"]].add(r["page"])

    finding_pages_actionable = {f["page"] for f in actionable if f["page"]}
    print(f"Page-level overlap (proxy for recall):")
    print(f"  Pages with actionable findings: {len(finding_pages_actionable)}")
    print()
    for bucket, pages in sorted(gt_pages_by_bucket.items(), key=lambda x: -len(x[1])):
        hit = sum(1 for p in pages if p in finding_pages_actionable)
        print(f"  {bucket:30s}  GT-pages={len(pages):2d}  app-touched={hit:2d}  "
              f"({100*hit/max(len(pages),1):.0f}%)")
    print()

    # ── Item-level: GT defects on pages we never reviewed ────────────────
    cold_misses = [
        r for r in bucketed
        if r["draft_bucket"] in DEFECT_BUCKETS
           and r["page"] not in finding_pages_actionable
    ]
    print(f"Cold misses ({len(cold_misses)} ground-truth defects on pages where")
    print(f"the app emitted no actionable finding):")
    by_b = Counter(r["draft_bucket"] for r in cold_misses)
    for b, n in by_b.most_common():
        print(f"  {n:3d}  {b}")
    print()
    print("Sample cold misses (first 8):")
    for r in cold_misses[:8]:
        print(f"  [page {r['page']}, sheet {r['sheet_code']}, {r['draft_bucket']}]")
        print(f"    comment: {r['comment'][:100]!r}")
        if r["surrounding_text"]:
            print(f"    near:    {r['surrounding_text'][:140]!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
