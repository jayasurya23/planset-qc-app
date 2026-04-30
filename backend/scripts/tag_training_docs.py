"""Tag the 41 Castillo training-doc chunks with V4 categories via Gemini.

Reads MASTER_TRAINING_CONTEXT.txt, splits it into one chunk per source PDF,
and issues one Gemini call per chunk to produce:

    - primary V4 category (one of V4_CATEGORIES, or "Other")
    - secondary categories (up to 2)
    - one-line summary (<=140 chars)
    - 3-6 testable checkpoints (short imperative assertions)
    - 3-8 free-form tags

Results are written to ``backend/data/training_docs/index.yaml`` — which the
V4 prompt builder consumes via ``app.training_docs.get_chunks_for_category``
when ``ENABLE_TRAINING_RAG=1`` is set.

Usage::

    cd backend
    .venv/Scripts/python scripts/tag_training_docs.py
    # or dry-run with filename heuristic only (no API calls):
    .venv/Scripts/python scripts/tag_training_docs.py --heuristic-only
    # or re-tag only chunks that are missing tags:
    .venv/Scripts/python scripts/tag_training_docs.py --only-missing
    # or limit to first N chunks (smoke test):
    .venv/Scripts/python scripts/tag_training_docs.py --max 3

Requires GEMINI_API_KEY in backend/.env unless --heuristic-only is passed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
sys.path.insert(0, str(BACKEND))

from app.training_docs import (  # type: ignore  # noqa: E402
    TrainingChunk,
    parse_master_context,
    load_index,
    save_index,
    INDEX_YAML,
    MASTER_TXT,
)

# V4 categories — kept in sync with regression_v4.V4_CATEGORIES and the
# rules_v4_draft.yaml taxonomy.
V4_CATEGORIES: list[str] = [
    "AI Input Gate", "BOD / Due Diligence", "Cross-Sheet", "Title Block",
    "E-001", "E-002", "E-010", "E-011", "E-050",
    "E-100", "E-101/E-102", "E-103/E-104", "E-106", "E-107", "E-110",
    "E-120", "E-130", "E-140",
    "E-200", "E-210", "E-214-E-217",
    "E-300", "E-400", "E-420-E-422", "E-450",
    "E-500-E-504", "E-601", "E-900",
]


# ---------------------------------------------------------------------------
# Heuristic fallback — filename-based seed tagging
# ---------------------------------------------------------------------------

# Maps a lowercased keyword/phrase to a V4 category. First match wins.
FILENAME_HEURISTICS: list[tuple[str, str]] = [
    # Cables — SLD notation + MV cable spec
    ("cable _callout_", "E-100"),               # SLD drafting convention
    ("mv cable shields", "E-101/E-102"),
    ("concentric neutrals", "E-101/E-102"),
    ("aerial cable", "E-107"),
    ("above-grade cable", "E-107"),

    # Grounding
    ("ground rods", "E-103/E-104"),
    ("grounding material", "E-103/E-104"),

    # Ratings family
    ("circuit breaker ratings", "E-200"),
    ("breaker trip settings", "E-200"),
    ("panel and transformer required impedance", "E-400"),
    ("equipment bil", "E-200"),
    ("disconnect switch certifications", "E-210"),
    ("transformer tap", "E-400"),

    # Relay / coordination
    ("mv relay coordination", "E-210"),
    ("relay tcc", "E-210"),
    ("relay setting xls", "E-210"),

    # Surge arresters → E-300 family (protective devices)
    ("surge arresters", "E-300"),

    # Reclosers
    ("reclosers", "E-210"),
    ("recloser", "E-210"),

    # DAS / submittal review
    ("das submittal", "E-900"),
    ("das voltage sensing", "E-900"),
    ("switchboard reviews", "E-400"),
    ("aux power", "E-400"),
    ("auxiliary power transformers", "E-400"),

    # Checking / QC meta
    ("nec design version", "BOD / Due Diligence"),
    ("nec design version for illinois", "BOD / Due Diligence"),
    ("proper voltage notation", "Title Block"),
    ("voltage notation", "Title Block"),
    ("qc and excel", "Cross-Sheet"),
    ("30% drawing sets", "AI Input Gate"),
    ("initial 30% drawing sets", "AI Input Gate"),

    # Inverter disconnects
    ("inverter disconnects", "E-140"),

    # Arc flash
    ("arc flash", "E-500-E-504"),

    # Site access / NGrid / pole
    ("pv site access", "BOD / Due Diligence"),
    ("pole numbering", "E-601"),
    ("ngrid pole", "E-601"),
    ("national grid pole", "E-601"),
    ("pv fence warning signs", "E-601"),

    # Cost savings / long runs
    ("long cable runs", "E-101/E-102"),
    ("equipment cost savings", "E-400"),
    ("expert needed", "AI Input Gate"),
    ("expertise needed", "AI Input Gate"),
]


def heuristic_tag(chunk: TrainingChunk) -> str:
    """Filename-based seed: best-guess V4 category. Returns 'Other' if no hit."""
    lo = chunk.source_pdf.lower()
    for key, cat in FILENAME_HEURISTICS:
        if key in lo:
            return cat
    return "Other"


# ---------------------------------------------------------------------------
# Gemini-based tagging
# ---------------------------------------------------------------------------

TAG_PROMPT_TEMPLATE = """You are an electrical engineering QC specialist analyzing a Castillo Engineering training memo authored by Joe Jancauskas.

Your job is to classify this memo against the V4 rule taxonomy so it can be retrieved as reference doctrine when the V4 engine evaluates solar PV plansets.

V4 CATEGORIES (choose EXACTLY ONE primary; up to TWO secondary):
- AI Input Gate — preconditions for running QC (drawing completeness, scope clarity)
- BOD / Due Diligence — basis of design, NEC version, permits, POI, state rules
- Cross-Sheet — consistency between sheets / xls / SLD
- Title Block — drawing title block fields, voltage notation, naming
- E-001 — index of drawings / general notes
- E-002 — symbols / abbreviations legend
- E-010 — general notes / standards references
- E-011 — general site plan
- E-050 — overall key plan
- E-100 — single line diagram (SLD) — drafting conventions, callouts
- E-101/E-102 — MV cable specs, shielded cables, concentric neutrals
- E-103/E-104 — grounding systems, ground rods, ground grid
- E-106 — bonding and lightning
- E-107 — above-grade / aerial cable systems
- E-110 — detailed one-line / protection
- E-120 — AC collection / inverter output AC
- E-130 — block diagram / functional block
- E-140 — inverter disconnects
- E-200 — equipment ratings (breakers, BIL, impedance)
- E-210 — protective relaying, reclosers, TCC coordination, settings
- E-214-E-217 — specific relay settings / tripping
- E-300 — surge arresters, overvoltage protection
- E-400 — transformers, aux power, switchboard reviews, panel impedance
- E-420-E-422 — transformer nameplate / tap settings
- E-450 — auxiliary distribution
- E-500-E-504 — arc flash, labels, safety
- E-601 — utility interconnect, poles, fence signs, site access
- E-900 — DAS, submittal reviews, metering/voltage sensing

Return STRICT JSON with this exact schema (no markdown, no commentary):
{{
  "primary_category": "<one of the above, or \\"Other\\" if none fits>",
  "secondary_categories": ["<optional, up to 2>"],
  "summary": "<one sentence, <=140 chars>",
  "checkpoints": ["<3-6 imperative assertions the memo says a reviewer must verify>"],
  "tags": ["<3-8 short free-form keywords>"]
}}

MEMO FILENAME: {filename}

MEMO BODY:
{body}
""".strip()


def _clip_body(text: str, max_chars: int = 12000) -> str:
    """Trim overly long chunks to keep prompt size reasonable."""
    if len(text) <= max_chars:
        return text
    # Keep first 70% and last 20% so conclusions aren't cut.
    head = int(max_chars * 0.75)
    tail = max_chars - head - 20
    return text[:head] + "\n\n[...truncated for prompt budget...]\n\n" + text[-tail:]


_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def _parse_tag_response(raw: str) -> dict:
    """Best-effort JSON extraction from Gemini's reply."""
    raw = raw.strip()
    # Strip accidental markdown fences
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```\s*$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = _JSON_BLOCK.search(raw)
        if m:
            return json.loads(m.group(0))
        raise


def _normalize_category(cat: str | None) -> str | None:
    """Coerce LLM-proposed category to exact V4_CATEGORIES value, else None."""
    if not cat:
        return None
    c = cat.strip()
    if c == "Other":
        return "Other"
    if c in V4_CATEGORIES:
        return c
    # Case-insensitive fallback
    for vc in V4_CATEGORIES:
        if vc.lower() == c.lower():
            return vc
    # Prefix fallback — e.g. "E-101" -> "E-101/E-102"
    for vc in V4_CATEGORIES:
        if vc.startswith(c) or c.startswith(vc.split("/")[0]):
            return vc
    return None


def tag_via_gemini(chunk: TrainingChunk) -> dict:
    """Single Gemini call for one chunk. Returns parsed JSON dict.

    Always routes through Gemini (bypasses AI_PROVIDER=openai) because the
    training-doc tagger is intentionally a Gemini-only pass — it's a
    one-shot build step, not a production QC code path.
    """
    from app import gemini_client  # local import so heuristic-only doesn't need it
    body = _clip_body(chunk.body)
    prompt = TAG_PROMPT_TEMPLATE.format(filename=chunk.source_pdf, body=body)
    # Force Gemini path even if AI_PROVIDER=openai in .env
    raw = gemini_client._gemini_text(prompt, deep=False)  # noqa: SLF001
    return _parse_tag_response(raw)


def apply_tag_result(chunk: TrainingChunk, result: dict) -> None:
    """Merge LLM/heuristic result into the chunk in place."""
    chunk.primary_category = _normalize_category(result.get("primary_category"))
    chunk.secondary_categories = [
        c for c in (_normalize_category(x) for x in (result.get("secondary_categories") or []))
        if c and c != chunk.primary_category
    ][:2]
    chunk.summary = (result.get("summary") or "").strip()[:200]
    chunk.checkpoints = [str(x).strip() for x in (result.get("checkpoints") or [])][:6]
    chunk.tags = [str(x).strip().lower() for x in (result.get("tags") or [])][:8]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--heuristic-only", action="store_true",
                    help="Skip Gemini; tag using filename heuristic only.")
    ap.add_argument("--only-missing", action="store_true",
                    help="Tag only chunks that have no primary_category yet.")
    ap.add_argument("--max", type=int, default=None,
                    help="Cap for smoke-testing; omit for full run.")
    args = ap.parse_args()

    # Always merge prior index tags so partial runs (--max, --only-missing)
    # don't wipe the index. Only skip this if index doesn't exist yet.
    chunks = parse_master_context()
    print(f"[*] Parsed {len(chunks)} training chunks from {MASTER_TXT.name}")

    if INDEX_YAML.exists():
        existing = {c.source_pdf: c for c in load_index()}
        for c in chunks:
            if c.source_pdf in existing:
                e = existing[c.source_pdf]
                c.primary_category = e.primary_category
                c.secondary_categories = e.secondary_categories
                c.summary = e.summary
                c.checkpoints = e.checkpoints
                c.tags = e.tags

    # Target selection
    if args.only_missing:
        targets = [c for c in chunks if not c.primary_category]
    else:
        targets = list(chunks)
    if args.max:
        targets = targets[: args.max]
    print(f"[*] Tagging {len(targets)} chunks "
          f"(mode={'heuristic' if args.heuristic_only else 'gemini'})")

    for i, chunk in enumerate(targets, 1):
        print(f"  [{i:2d}/{len(targets)}] {chunk.source_pdf[:70]}", end=" ... ", flush=True)
        try:
            if args.heuristic_only:
                cat = heuristic_tag(chunk)
                result = {
                    "primary_category": cat,
                    "secondary_categories": [],
                    "summary": f"(heuristic tag from filename — '{cat}')",
                    "checkpoints": [],
                    "tags": [],
                }
            else:
                result = tag_via_gemini(chunk)
            apply_tag_result(chunk, result)
            print(f"-> {chunk.primary_category}")
        except Exception as e:
            print(f"FAILED: {e}")

    out = save_index(chunks)
    print(f"\n[OK] Wrote {out}")

    # Report: category distribution
    dist: dict[str, int] = {}
    untagged: list[str] = []
    for c in chunks:
        if c.primary_category:
            dist[c.primary_category] = dist.get(c.primary_category, 0) + 1
        else:
            untagged.append(c.source_pdf)
    print(f"\nCategory distribution ({len(chunks)} chunks):")
    for cat, n in sorted(dist.items(), key=lambda x: (-x[1], x[0])):
        print(f"  {n:3d}  {cat}")
    if untagged:
        print(f"\nUntagged ({len(untagged)}):")
        for u in untagged:
            print(f"   - {u}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
