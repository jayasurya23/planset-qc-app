"""Measure run-to-run reproducibility across repeat analyses of one planset.

The analyzer is LLM-driven, so re-running the same PDF does not reproduce the
same report. This script turns that into numbers so a stabilization change can
be judged by evidence instead of impression.

Three distinct things vary, and they are NOT equally bad:

  IDENTITY churn   the same finding comes back under a different item_key, so
                   it looks new. Cosmetic in isolation, but it destroys
                   run-to-run comparison and inflates the apparent diff.
  JUDGMENT churn   the same item_key changes status (Pass <-> Fail). This is
                   the one an engineer actually feels.
  INPUT churn      extraction returned a different set of spec fields, so
                   deterministic calcs silently fall back to "Deferred".

Headline metric is REPRODUCIBILITY: of every finding seen in any run, what
fraction appeared in EVERY run with an IDENTICAL status. Everything else in
the report exists to explain that number.

Usage (local):
    python backend/scripts/variance_report.py --db data/planset_qc.sqlite3 --root <run_id>

Usage (prod container — upload to the Azure Files share, then exec):
    python /home/data/variance_report.py --db /home/data/planset_qc.sqlite3 \
        --root 0909b346-46dc-4536-bc63-025816dc063b --json /home/data/variance.json

Pass explicit ids instead of a root when the runs are not versions of each
other (e.g. three separate uploads of the same file):
    --runs id1,id2,id3
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:  # keep the script runnable against a bare DB with no app on the path
    from app.rule_registry import resolve_finding_source
except Exception:  # noqa: BLE001
    resolve_finding_source = None  # type: ignore[assignment]

_AI_FAMILY = re.compile(r"^(ai_[a-z0-9]+(?:_deep)?)_")


def family_of(item_key: str) -> str:
    """Group an item_key with its siblings so churn can be blamed on a prompt.

    Prefers the registry's own resolver — it knows which prompt constant owns
    each dynamic ai_* prefix — and falls back to a prefix regex so the report
    still works when run against a database without the app importable.
    """
    if resolve_finding_source is not None:
        try:
            src = resolve_finding_source(item_key)
            if src.get("kind") == "vision_family":
                return f"ai:{src['family']}"
            if src.get("kind") == "rule":
                rule = src.get("rule")
                return f"rule:{getattr(rule, 'category', None) or 'uncategorized'}"
        except Exception:  # noqa: BLE001 — never let classification break the report
            pass
    match = _AI_FAMILY.match(item_key)
    if match:
        return f"ai:{match.group(1)}"
    return f"rule:{item_key.split('_')[0]}"


def load_runs(db: str, root: str | None, run_ids: list[str] | None) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("SELECT key_schema_version FROM runs LIMIT 1")
        key_col = "key_schema_version"
    except sqlite3.OperationalError:  # DB predates the stamp
        key_col = "NULL AS key_schema_version"
    if run_ids:
        rows = [
            conn.execute(
                f"SELECT id, version, {key_col} FROM runs WHERE id=?", (rid,)
            ).fetchone()
            for rid in run_ids
        ]
        rows = [r for r in rows if r is not None]
    else:
        rows = conn.execute(
            f"SELECT id, version, {key_col} FROM runs "
            "WHERE root_run_id=? OR id=? ORDER BY version, created_at",
            (root, root),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        rid = row["id"]
        issues = {
            r["item_key"]: r["status"]
            for r in conn.execute(
                "SELECT item_key, status FROM issues WHERE run_id=?", (rid,)
            )
        }
        summary_row = conn.execute(
            "SELECT summary_json FROM runs WHERE id=?", (rid,)
        ).fetchone()
        summary = json.loads(summary_row["summary_json"] or "{}") if summary_row else {}
        out.append({
            "run_id": rid,
            "version": row["version"] or 1,
            "label": f"v{row['version'] or 1}",
            "issues": issues,
            "calc_inputs": summary.get("calc_inputs") or {},
            "rules_sha": str(summary.get("rules_sha256") or "")[:12],
            "key_schema": row["key_schema_version"] or 1,
        })
    conn.close()
    return out


def analyse(runs: list[dict[str, Any]]) -> dict[str, Any]:
    key_sets = [set(r["issues"]) for r in runs]
    union: set[str] = set().union(*key_sets)
    common: set[str] = set(key_sets[0]).intersection(*key_sets[1:])

    def statuses(key: str) -> list[str]:
        return [r["issues"][key] for r in runs]

    agreed = {k for k in common if len(set(statuses(k))) == 1}
    flips = sorted(common - agreed)

    fail_sets = [{k for k, s in r["issues"].items() if s == "Fail"} for r in runs]
    fail_union: set[str] = set().union(*fail_sets)
    fail_common: set[str] = set(fail_sets[0]).intersection(*fail_sets[1:])

    by_family: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: {"all": set(), "common": set(), "agreed": set()}
    )
    for key in union:
        fam = by_family[family_of(key)]
        fam["all"].add(key)
        if key in common:
            fam["common"].add(key)
        if key in agreed:
            fam["agreed"].add(key)

    # Extraction: which spec fields were captured by some runs but not others.
    # Runs with an empty snapshot predate the calc_inputs feature — counting
    # them would report every field as unstable, which is an artefact, not churn.
    snapshot_runs = [r for r in runs if r["calc_inputs"]]
    field_runs: dict[str, list[str]] = defaultdict(list)
    for run in snapshot_runs:
        for field in run["calc_inputs"]:
            field_runs[field].append(run["label"])
    unstable_fields = (
        {f: labels for f, labels in field_runs.items() if len(labels) != len(snapshot_runs)}
        if len(snapshot_runs) >= 2
        else {}
    )

    return {
        "runs": [
            {
                "label": r["label"],
                "run_id": r["run_id"],
                "findings": len(r["issues"]),
                "fails": len(fail_sets[i]),
                "calc_input_fields": len(r["calc_inputs"]),
                "rules_sha": r["rules_sha"],
                "key_schema": r["key_schema"],
            }
            for i, r in enumerate(runs)
        ],
        "key_schema_mismatch": len({r["key_schema"] for r in runs}) > 1,
        "union_keys": len(union),
        "common_keys": len(common),
        "agreed_keys": len(agreed),
        "key_stability_pct": round(100 * len(common) / len(union), 1) if union else 0.0,
        "status_agreement_pct": (
            round(100 * len(agreed) / len(common), 1) if common else 0.0
        ),
        "reproducibility_pct": round(100 * len(agreed) / len(union), 1) if union else 0.0,
        "fail_jaccard_pct": (
            round(100 * len(fail_common) / len(fail_union), 1) if fail_union else 100.0
        ),
        "fail_union": len(fail_union),
        "fail_common": len(fail_common),
        "flips": [{"item_key": k, "statuses": statuses(k)} for k in flips],
        "families": {
            fam: {
                "total": len(v["all"]),
                "common": len(v["common"]),
                "agreed": len(v["agreed"]),
                "key_stability_pct": round(100 * len(v["common"]) / len(v["all"]), 1),
            }
            for fam, v in by_family.items()
        },
        "unstable_calc_inputs": unstable_fields,
    }


def render(report: dict[str, Any]) -> str:
    lines: list[str] = []
    add = lines.append
    add("=" * 72)
    add("RUN-TO-RUN REPRODUCIBILITY")
    add("=" * 72)
    for r in report["runs"]:
        add(
            f"  {r['label']:<4} {r['run_id'][:8]}  findings={r['findings']:<4} "
            f"fails={r['fails']:<3} calc_inputs={r['calc_input_fields']:<3} "
            f"keys=v{r['key_schema']}  rules={r['rules_sha']}"
        )
    if report["key_schema_mismatch"]:
        add("")
        add("  !! THESE RUNS USE DIFFERENT item_key CONVENTIONS.")
        add("     Findings cannot be matched across that boundary — every")
        add("     stability number below is meaningless. Re-run the older")
        add("     version under the current schema before comparing.")
    shas = {r["rules_sha"] for r in report["runs"] if r["rules_sha"]}
    if len(shas) > 1:
        add("  !! runs used DIFFERENT rules files — variance is partly explained by that")
    add("")
    add(f"  REPRODUCIBILITY      {report['reproducibility_pct']:>6}%   "
        f"({report['agreed_keys']}/{report['union_keys']} findings seen in every run "
        f"with identical status)")
    add(f"    identity  stability {report['key_stability_pct']:>6}%   "
        f"({report['common_keys']}/{report['union_keys']} item_keys present in every run)")
    add(f"    judgment  agreement {report['status_agreement_pct']:>6}%   "
        f"({report['agreed_keys']}/{report['common_keys']} of those hold one status)")
    add(f"    Fail-set  overlap   {report['fail_jaccard_pct']:>6}%   "
        f"({report['fail_common']}/{report['fail_union']} Fails common to every run)")
    add("")
    add("KEY STABILITY BY FAMILY  (present in every run / total seen)")
    ranked = sorted(
        report["families"].items(), key=lambda kv: (-kv[1]["total"], kv[0])
    )
    for fam, v in ranked:
        flag = "  <-- churn" if v["key_stability_pct"] < 50 and v["total"] >= 5 else ""
        add(f"  {fam:<28} {v['common']:>3}/{v['total']:<4} = "
            f"{v['key_stability_pct']:>5}%{flag}")
    add("")
    if report["flips"]:
        add(f"STATUS FLIPS  ({len(report['flips'])} keys present every run but graded differently)")
        for f in report["flips"]:
            add(f"  {' -> '.join(f['statuses']):<38} {f['item_key']}")
    else:
        add("STATUS FLIPS  none")
    add("")
    if report["unstable_calc_inputs"]:
        add("EXTRACTION CHURN  (spec fields captured by some runs but not all)")
        for field, labels in sorted(report["unstable_calc_inputs"].items()):
            add(f"  {field:<38} present in {', '.join(labels)}")
        add("  These silently flip deterministic calcs to 'Deferred: missing inputs'.")
    else:
        add("EXTRACTION CHURN  none — every run captured the same spec fields")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=os.environ.get("QC_DB", "data/planset_qc.sqlite3"))
    ap.add_argument("--root", help="root_run_id — compares every version of that run")
    ap.add_argument("--runs", help="comma-separated run ids to compare instead")
    ap.add_argument("--json", dest="json_out", help="also write the raw report here")
    args = ap.parse_args()

    if not args.root and not args.runs:
        ap.error("pass --root <run_id> or --runs <id,id,...>")
    run_ids = [r.strip() for r in args.runs.split(",")] if args.runs else None

    runs = load_runs(args.db, args.root, run_ids)
    if len(runs) < 2:
        print(f"need at least 2 runs to compare; found {len(runs)}")
        return 1

    report = analyse(runs)
    print(render(report))
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=1), encoding="utf-8")
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
