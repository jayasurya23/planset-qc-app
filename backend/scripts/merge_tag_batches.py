"""Merge tags_batch_*.json (produced by in-context Claude tagging) into
backend/data/training_docs/index.yaml.

The heuristic tagger (tag_training_docs.py --heuristic-only) seeded index.yaml
with primary_category guesses + page_count/char_len. This script upgrades
those entries with the in-context Claude tags — summary, checkpoints,
secondary_categories, refined primary_category, and free-form tags — without
losing page_count/char_len metadata from the prior run.

Usage::

    python backend/scripts/merge_tag_batches.py \\
        --batches-dir outputs/ \\
        [--index-out backend/data/training_docs/index.yaml]

By default the script looks for outputs/tags_batch_*.json relative to repo
root and writes to backend/data/training_docs/index.yaml.
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

# Ensure backend/ is on sys.path so we can reuse load_index / save_index.
HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
PROJECT = BACKEND.parent
sys.path.insert(0, str(BACKEND))

from app.training_docs import (  # noqa: E402
    INDEX_YAML,
    TrainingChunk,
    parse_master_context,
    save_index,
)

try:
    import yaml  # type: ignore
except ImportError:
    print("ERROR: PyYAML is required (pip install pyyaml).", file=sys.stderr)
    sys.exit(1)


def load_existing_index(path: Path) -> dict[str, dict]:
    """Return {source_pdf: dict} from the existing index.yaml, or {}."""
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = data.get("chunks", [])
    return {e["source_pdf"]: e for e in entries if "source_pdf" in e}


def load_batch_tags(batches_dir: Path) -> dict[str, dict]:
    """Load and union tags_batch_*.json from the given directory."""
    merged: dict[str, dict] = {}
    files = sorted(glob.glob(str(batches_dir / "tags_batch_*.json")))
    if not files:
        raise FileNotFoundError(
            f"No tags_batch_*.json files found in {batches_dir}"
        )
    for fp in files:
        with open(fp, encoding="utf-8") as f:
            entries = json.load(f)
        for e in entries:
            pdf = e.get("source_pdf")
            if not pdf:
                continue
            merged[pdf] = e
        print(f"[+] Loaded {len(entries):>2} entries from {Path(fp).name}")
    return merged


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--batches-dir",
        default=str(PROJECT / "outputs"),
        help="Directory containing tags_batch_*.json (default: outputs/)",
    )
    ap.add_argument(
        "--index-out",
        default=str(INDEX_YAML),
        help=f"Output index.yaml path (default: {INDEX_YAML})",
    )
    args = ap.parse_args()

    batches_dir = Path(args.batches_dir)
    index_out = Path(args.index_out)

    # Load all chunks (bodies + page_count + char_len parsed from master txt).
    chunks = parse_master_context()
    print(f"[*] Parsed {len(chunks)} chunks from MASTER_TRAINING_CONTEXT.txt")

    # Load existing index metadata (may contain heuristic tags).
    existing = load_existing_index(index_out)
    print(f"[*] Existing index entries: {len(existing)}")

    # Load batch tags (the in-context Claude output to be merged in).
    batch_tags = load_batch_tags(batches_dir)
    print(f"[*] Batch tag entries: {len(batch_tags)}")

    # Merge: batch tags win over heuristic tags, but we keep page_count/char_len.
    missing: list[str] = []
    for c in chunks:
        bt = batch_tags.get(c.source_pdf)
        if bt:
            c.primary_category = bt.get("primary_category")
            c.secondary_categories = list(bt.get("secondary_categories") or [])
            c.summary = bt.get("summary", "") or ""
            c.checkpoints = list(bt.get("checkpoints") or [])
            c.tags = list(bt.get("tags") or [])
        else:
            # Fall back to existing heuristic tags if no batch entry.
            xt = existing.get(c.source_pdf, {})
            c.primary_category = xt.get("primary_category")
            c.secondary_categories = list(xt.get("secondary_categories") or [])
            c.summary = xt.get("summary", "") or ""
            c.checkpoints = list(xt.get("checkpoints") or [])
            c.tags = list(xt.get("tags") or [])
            if not c.primary_category:
                missing.append(c.source_pdf)

    if missing:
        print(f"[!] {len(missing)} chunks have no tagging after merge:")
        for pdf in missing:
            print(f"    - {pdf}")

    # Write the merged index.
    save_index(chunks, index_out)
    tagged = sum(1 for c in chunks if c.primary_category)
    print(f"[OK] Wrote {tagged}/{len(chunks)} tagged chunks to {index_out}")

    # Summary: count per primary_category to sanity-check distribution.
    from collections import Counter
    cats = Counter(c.primary_category for c in chunks if c.primary_category)
    print("[*] Primary category distribution:")
    for cat, n in cats.most_common():
        print(f"    {cat:<25} {n}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
