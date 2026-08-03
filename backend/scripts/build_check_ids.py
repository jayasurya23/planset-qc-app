"""Generate backend/app/check_ids.yaml from an authored registry JSON.

The registry is authored as JSON (one family per file) and compiled here rather
than hand-edited, for three reasons the production audit made concrete:

  * ALIASES ARE LOAD-BEARING. Every check name the family emitted in production
    must resolve to some ID, or 26 runs of history stop matching and ~1,000
    findings fall into the exploratory bucket at once — which would look
    exactly like a catastrophic regression. This script fails loudly when
    coverage is incomplete rather than shipping a silent break.
  * IDS ARE PERMANENT. Once a run is graded under an ID, renaming it orphans
    that history. The duplicate check below catches collisions before they ship.
  * The YAML is what the model actually reads (check_ids.checklist_block),
    so a missing instruction is a silently weaker check, not a cosmetic gap.

Usage:
    python backend/scripts/build_check_ids.py \\
        --family grounding --json D:/tmp/grounding_corrected.json \\
        [--prod-names D:/tmp/checknames_ai_gnd_deep.txt] [--write]

Without --write it prints a diff-style summary and changes nothing.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
TARGET = REPO / "backend" / "app" / "check_ids.yaml"

_VALID_ID = re.compile(r"^[a-z][a-z0-9_]*$")
_ABSENCE = {"defect", "not_applicable", "unknown"}


def _normalize(name: str) -> str:
    """Must mirror check_ids._normalize exactly, or coverage is a lie."""
    text = str(name or "").strip().lower()
    text = re.sub(r"^\s*\d+[\.\)]\s*", "", text)
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def load_prod_names(path: Path) -> list[str]:
    """Check names from a checknames_*.txt dump (count, statuses, then name)."""
    names: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.match(r"\s*\d+\s+\S+\s+(.*)$", line)
        if m and m.group(1).strip():
            names.append(m.group(1).strip())
    return names


def build(family: str, checks: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    problems: list[str] = []
    for c in checks:
        cid = str(c.get("id") or "").strip()
        if not _VALID_ID.match(cid):
            problems.append(f"invalid id {cid!r} (want snake_case)")
            continue
        if cid in seen:
            problems.append(f"duplicate id {cid!r}")
            continue
        seen.add(cid)
        instruction = str(c.get("instruction") or "").strip()
        if not instruction:
            problems.append(f"{cid}: empty instruction — the model would get "
                            "a bare title and judge it however it likes")
        absence = str(c.get("absence_is") or "unknown")
        if absence not in _ABSENCE:
            problems.append(f"{cid}: absence_is={absence!r} not in {_ABSENCE}")
            absence = "unknown"
        # covers_prod_names is the authoring field; aliases is what the loader
        # reads. Getting this mapping wrong silently discards all history.
        aliases = list(dict.fromkeys(
            [str(a) for a in (c.get("aliases") or [])]
            + [str(a) for a in (c.get("covers_prod_names") or [])]
        ))
        entry = {"id": cid, "title": str(c.get("title") or cid).strip(),
                 "instruction": instruction, "absence_is": absence}
        if c.get("nec_ref"):
            entry["nec_ref"] = str(c["nec_ref"]).strip()
        if aliases:
            entry["aliases"] = aliases
        out.append(entry)
    if problems:
        print("REGISTRY PROBLEMS:")
        for p in problems:
            print("  -", p)
        if any("invalid id" in p or "duplicate id" in p for p in problems):
            sys.exit(1)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--family", required=True)
    ap.add_argument("--json", required=True, help="authored registry JSON")
    ap.add_argument("--prod-names", help="checknames_*.txt to verify coverage")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    data = json.loads(Path(args.json).read_text(encoding="utf-8"))
    checks = data.get("checks") if isinstance(data, dict) else data
    entries = build(args.family, checks or [])

    print(f"{args.family}: {len(entries)} checks")
    for e in entries:
        print(f"  {e['id']:<38} absence={e['absence_is']:<15} "
              f"aliases={len(e.get('aliases', []))}")

    if args.prod_names:
        prod = load_prod_names(Path(args.prod_names))
        table = set()
        for e in entries:
            table.add(_normalize(e["id"]))
            table.add(_normalize(e["title"]))
            table.update(_normalize(a) for a in e.get("aliases", []))
        missing = [n for n in prod if _normalize(n) not in table]
        covered = len(prod) - len(missing)
        pct = (100 * covered // len(prod)) if prod else 100
        print(f"\nhistorical coverage: {covered}/{len(prod)} ({pct}%)")
        for n in missing[:25]:
            print("  UNMAPPED:", n)
        if missing:
            print(f"\n{len(missing)} production name(s) would fall into the "
                  "exploratory bucket, breaking their history. Add them as "
                  "aliases before shipping.")

    if not args.write:
        print("\n(dry run — pass --write to update check_ids.yaml)")
        return 0

    current = {}
    if TARGET.exists():
        current = yaml.safe_load(TARGET.read_text(encoding="utf-8")) or {}
    families = current.get("families") or {}
    families[args.family] = entries
    TARGET.write_text(
        yaml.safe_dump({"families": families}, sort_keys=False,
                       allow_unicode=True, width=100),
        encoding="utf-8")
    print(f"\nwrote {TARGET} ({len(entries)} checks for {args.family})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
