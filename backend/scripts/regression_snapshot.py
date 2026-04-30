"""Lightweight regression harness — status-count drift detector.

Runs against the SQLite run history (no new Gemini calls). For each tracked
planset in the test set, pulls the most-recent run, compares its Pass / NR /
Fail / Deferred counts against a saved baseline, and flags anything outside
tolerance.

Use this to catch the kind of regression where a prompt change or rule edit
silently shifts the bucket distribution without the recall/precision harness
catching it (those need ground-truth labels this harness doesn't require).

Usage from backend/:

    # Snapshot current state as the new baseline
    .venv/Scripts/python scripts/regression_snapshot.py capture

    # Compare latest runs to the baseline — CI-friendly exit codes
    .venv/Scripts/python scripts/regression_snapshot.py check

The baseline lives at ``backend/scripts/regression_baseline.json`` and is
git-tracked. Commit it when intentional changes shift the counts.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

# Force UTF-8 stdout so the box-drawing characters render on Windows cp1252.
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    os.environ["PYTHONIOENCODING"] = "utf-8"
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
DB_PATH = BACKEND / "data" / "planset_qc.sqlite3"
BASELINE = HERE / "regression_baseline.json"

# Test set — PDFs whose count distribution we want to track over time.
# Matched by (case-insensitive) substring of original_filename; the most
# recent run with a matching filename is taken as "current".
TEST_SET: list[str] = [
    "Wellington IFP",
    "Clay Center",
    "Greensburg",
    "Zia Hills IFC",
    "Gonzo",  # only project with ground-truth annotations (150 QC + 32 client)
]

# Tolerances for a count delta: max(TOLERANCE_PCT, TOLERANCE_ABS).
# Prompt outputs have some Gemini-side variance; anything within these
# bounds is expected drift, not a regression.
TOLERANCE_PCT = 0.15   # 15%
TOLERANCE_ABS = 5      # or 5 findings, whichever is larger

TRACKED_STATUSES = ("Pass", "Fail", "Needs Review", "Deferred")


def latest_run_for(con: sqlite3.Connection, substr: str) -> dict | None:
    """Most-recent run whose original_filename contains substr (case-insensitive)."""
    row = con.execute(
        """SELECT id, project_name, original_filename, created_at, status_counts_json
             FROM runs
            WHERE LOWER(original_filename) LIKE ?
         ORDER BY created_at DESC LIMIT 1""",
        (f"%{substr.lower()}%",),
    ).fetchone()
    if not row:
        return None
    counts = json.loads(row[4])
    return {
        "id": row[0],
        "project_name": row[1],
        "original_filename": row[2],
        "created_at": row[3],
        "counts": {s: counts.get(s, 0) for s in TRACKED_STATUSES},
        "total": sum(counts.values()),
    }


def capture() -> int:
    con = sqlite3.connect(DB_PATH)
    snap: dict[str, dict] = {}
    missing: list[str] = []
    for substr in TEST_SET:
        run = latest_run_for(con, substr)
        if run is None:
            missing.append(substr)
            continue
        snap[substr] = {
            "run_id": run["id"][:8],
            "created_at": run["created_at"],
            "counts": run["counts"],
            "total": run["total"],
        }
    con.close()

    if missing:
        print(f"WARNING: no runs found for: {missing}")
        print("Run these plansets through the UI first, then re-capture.")

    if not snap:
        print("No baseline captured — empty test set.")
        return 1

    BASELINE.write_text(json.dumps(snap, indent=2), encoding="utf-8")
    print(f"Baseline captured ({len(snap)} plansets) → {BASELINE.relative_to(BACKEND.parent)}")
    for k, v in snap.items():
        print(f"  {k:25s}  run={v['run_id']}  total={v['total']}  {v['counts']}")
    return 0


def _delta_row(label: str, baseline: int, current: int) -> tuple[str, bool]:
    delta = current - baseline
    pct = (delta / max(baseline, 1)) * 100
    tolerance = max(int(baseline * TOLERANCE_PCT), TOLERANCE_ABS)
    out_of_bounds = abs(delta) > tolerance
    marker = " ⚠" if out_of_bounds else ""
    sign = "+" if delta > 0 else ""
    return (
        f"  {label:14s}  base={baseline:4d}  now={current:4d}  "
        f"Δ={sign}{delta:+4d} ({sign}{pct:+.1f}%)  tol=±{tolerance}{marker}",
        out_of_bounds,
    )


def check() -> int:
    if not BASELINE.exists():
        print(f"No baseline at {BASELINE}. Run `capture` first.", file=sys.stderr)
        return 2

    snap = json.loads(BASELINE.read_text(encoding="utf-8"))
    con = sqlite3.connect(DB_PATH)
    any_breach = False
    any_missing = False
    print(f"Tolerance: ±{int(TOLERANCE_PCT*100)}% or ±{TOLERANCE_ABS} findings (whichever is larger)")
    print()
    for substr, base in snap.items():
        current = latest_run_for(con, substr)
        print(f"── {substr} ──")
        if current is None:
            print(f"  ❌ no current run found. (baseline: run {base['run_id']}, "
                  f"total {base['total']})")
            any_missing = True
            continue
        if current["id"][:8] == base["run_id"]:
            print(f"  (same run as baseline — re-run to get fresh snapshot)")
            print(f"  run={current['id'][:8]}  total={current['total']}")
            continue
        print(f"  baseline: {base['run_id']} ({base['created_at'][:19]})")
        print(f"  current:  {current['id'][:8]} ({current['created_at'][:19]})")
        total_line, total_breach = _delta_row("Total", base["total"], current["total"])
        print(total_line)
        if total_breach:
            any_breach = True
        for s in TRACKED_STATUSES:
            line, breach = _delta_row(s, base["counts"].get(s, 0), current["counts"].get(s, 0))
            print(line)
            if breach:
                any_breach = True
    con.close()
    print()
    if any_breach:
        print("⚠  One or more counts drifted outside tolerance.")
        print("   Investigate the cause, then either fix the regression OR")
        print("   re-capture the baseline if the drift is intentional.")
        return 1
    if any_missing:
        print("⚠  Some plansets in the test set have no current run.")
        return 1
    print("✔  All tracked plansets within tolerance.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=("capture", "check"),
                   help="capture: snapshot latest runs as new baseline. "
                        "check: compare latest runs to baseline.")
    args = p.parse_args()
    return capture() if args.mode == "capture" else check()


if __name__ == "__main__":
    sys.exit(main())
