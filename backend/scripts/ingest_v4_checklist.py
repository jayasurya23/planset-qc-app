"""One-shot converter: V4 QC Checklist workbook → rules.yaml draft.

Reads the ``V4 Final QC Checklist`` sheet from the Castillo V4 xlsx, converts
each check row into a deterministic ``rules.yaml`` entry, and writes the
result to a **new** file so the user can diff against the live `rules.yaml`
before promoting. Does NOT overwrite existing rules.

Usage
-----
    python backend/scripts/ingest_v4_checklist.py \\
        --input  "S:/…/Claude V4 Final.xlsx" \\
        --output  backend/app/rules_v4_draft.yaml

Behavior
--------
- Preserves V4 section taxonomy as the rule ``category`` (E-001, E-010,
  E-100, …) so the UI groups findings the way engineers already think.
- ``Item to Check`` → rule title, ``What to Verify`` → description,
  ``Source Information`` → new ``source`` field.
- Duplicate rows past the ``MP Comments`` divider are merged into the
  canonical rule and their free-form annotations are appended to the
  description with a ``NOTE:`` prefix.
- Severity is heuristic (keyword-driven); ``check_type`` defaults to
  ``gemini_vision``; ``confidence`` defaults to 0.75.
- Prints a summary report with row counts, unmapped rows, and any parse
  warnings so the diff is easy to audit.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Any

import openpyxl
import yaml


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


# Desired category display order. V4 sections first (by sheet code), then
# the conceptual gates at the top and cross-sheet at the bottom.
CATEGORY_ORDER: list[str] = [
    "AI Input Gate",
    "BOD / Due Diligence",
    "Title Block",
    "E-001",
    "E-002",
    "E-010",
    "E-011",
    "E-050",
    "E-100",
    "E-101/E-102",
    "E-103/E-104",
    "E-106",
    "E-107",
    "E-110",
    "E-120",
    "E-130",
    "E-140",
    "E-200",
    "E-210",
    "E-214-E-217",
    "E-300",
    "E-400",
    "E-420-E-422",
    "E-450",
    "E-500-E-504",
    "E-601",
    "E-900",
    "Cross-Sheet",
]


# Tokens that bump severity to HIGH when present in the description.
HIGH_SEVERITY_HINTS = [
    "kaic", "fault", "undersized", "insufficient", "safety",
    "critical", "mismatch", "must match", "must not exceed",
    "propagation failure", "ampacity", "hazard", "arc flash",
    "bil", "interrupt", "over-dutied", "exceeds rating",
    "minimum required", "not rated for", "wrong size",
]

# Tokens that push severity DOWN to low.
LOW_SEVERITY_HINTS = [
    "typo", "formatting", "stylistic", "note style", "cosmetic",
    "tab order", "page numbering",
]


# Header row marker inside the sheet — rows after this one are re-entries
# of earlier checks carrying free-form Manjil comments.
MP_COMMENTS_HEADER = "Item to Check"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clean(v: Any) -> str:
    """Normalize a cell value to a trimmed string (empty if missing)."""
    if v is None:
        return ""
    s = str(v).replace("\r\n", "\n").replace("\r", "\n").strip()
    return "" if s == "None" else s


def _slug(s: str, max_len: int = 40) -> str:
    """Filesystem/yaml-safe slug."""
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    if len(s) > max_len:
        s = s[:max_len].rstrip("_")
    return s or "unnamed"


def _guess_severity(text: str) -> str:
    t = text.lower()
    for hint in HIGH_SEVERITY_HINTS:
        if hint in t:
            return "high"
    for hint in LOW_SEVERITY_HINTS:
        if hint in t:
            return "low"
    return "medium"


# Regex for code citations that make excellent keyword-search tokens.
_CODE_CITATION_RE = re.compile(
    r"\b(NEC|NFPA|IEEE|ANSI|ASTM|ASCE|IBC|UL|NEMA|OSHA)\s+"
    r"(?:C?\d{2,4}(?:[.-]\d+)*(?:\([A-Za-z0-9]+\))?)",
    re.IGNORECASE,
)

# Tokens that strongly suggest "this rule is a text-presence check that can
# run as a keyword match instead of burning a vision call."
_NOTE_PRESENCE_TOKENS = (
    "note present", "note shown", "note stated", "note listed", "note cited",
    "note referenced", "note populated", "note (", "notes present",
    "code year note", "splice prohibition note", "stranding note",
)


# Specific electrical / structural tokens that are distinctive enough to
# search for directly. Each maps to the search tokens we'll look for on
# the drawing — typically the token itself plus close variants.
_DISTINCT_TOKEN_RULES: list[tuple[re.Pattern, list[str]]] = [
    (re.compile(r"\b%\s*z\b", re.I),             ["%Z", "% Z", "IMPEDANCE"]),
    (re.compile(r"\bx\s*/\s*r\b", re.I),         ["X/R"]),
    (re.compile(r"\bkAIC\b", re.I),              ["kAIC", "KA IC", "INTERRUPT"]),
    (re.compile(r"\bMCOV\b"),                    ["MCOV"]),
    (re.compile(r"\bBIL\b"),                     ["BIL", "BASIC IMPULSE"]),
    (re.compile(r"\bAF\s*/\s*AT\b", re.I),       ["AF/AT", "AMPERE FRAME"]),
    (re.compile(r"\bNMOT\b"),                    ["NMOT", "NOCT"]),
    (re.compile(r"\b(T|tamb)\b.*\b(ashrae)\b", re.I),  ["ASHRAE", "TAMB"]),
    (re.compile(r"\barc[-\s]?flash\s+labels?\b", re.I), ["ARC FLASH", "ARC-FLASH"]),
    (re.compile(r"\bGOAB\b"),                    ["GOAB"]),
    (re.compile(r"\bSOV\b"),                     ["SOV", "SCOPE OF"]),
    (re.compile(r"\bC[CP]T\b"),                  ["CPT", "CONTROL POWER"]),
]


# Title verbs that indicate "presence" (as opposed to "calculation" or
# "comparison"). When present alongside a distinctive token, the rule is
# a keyword candidate.
_PRESENCE_VERBS = (
    "present", "shown", "stated", "listed", "cited", "noted", "populated",
    "indicated", "called out", "annotated",
)


_COMPARE_TOKENS = (
    "cross-sheet", "cross sheet", "propagation", "identical",
    "consistent across", "six-document", "five-document", "four-document",
    "= e-", "matches e-", "= submittal", " vs ", " versus ", "reconciled",
    "character-identical", "exact match",
)


# Categories that are process gates — never calc / keyword candidates.
_NON_CHECKABLE_CATEGORIES = {
    "AI Input Gate",
    "BOD / Due Diligence",
}


# Match the rule TITLE (not description) to an existing calc function.
# Title-only matching keeps the classifier precise — descriptions often
# reference other rules' formulas which would cause false positives.
_CALC_TITLE_PATTERNS: list[tuple[re.Pattern, str]] = [
    # E-120 string-voltage math
    (re.compile(r"\b(voc[_ -]?cold|string\s+voc|vmp[_ -]?hot|string\s+vmp)\b", re.I),
     "validate_stringing"),
    # E-106 fuse sizing (NEC 690.9)
    (re.compile(r"\b(fuse[:\s].*690\.?9|nec\s*690\.?9|string\s+fuse\s+(rating|sizing)|fuse\s+sizing)\b", re.I),
     "validate_fuse_sizing"),
    # Transformer kVA vs inverter output
    (re.compile(r"\bxfmr\s+kva\s*>=\s*|transformer\s+kva\s*>=|aux\s+xfmr\s+kva\s*>=", re.I),
     "validate_transformer"),
    # MV conductor math — checked BEFORE generic "1.25 × FLA" so MV wins
    (re.compile(r"\bmv\s+conductor\b|nec\s*310\.?60|nec\s*art\s*315", re.I),
     "validate_mv_ampacity"),
    # AC feeder conductor / breaker 1.25 * FLA (non-MV)
    (re.compile(r"\b(feeder\s+(breaker|conductor).*1\.25|1\.25\s*[×x*]\s*fla(?!\s*\()|breaker\s*>=\s*1\.25)\b", re.I),
     "validate_ac_ampacity"),
    # Conduit fill
    (re.compile(r"\bconduit\s+fill\s*(<=|≤|\bmax\b|40\s*%)", re.I),
     "validate_conduit_fill"),
    # NEC 110.26 working clearances
    (re.compile(r"\bnec\s*110\.?26|working\s+(space|clearance)\b", re.I),
     "validate_nec_clearances"),
    # DC conductor ampacity (narrow pattern — don't match DAS/etc.)
    (re.compile(r"\bdc\s+conductor\s+(ampacity|meets|per)|pv\s+wire\s+ampacity", re.I),
     "validate_dc_ampacity"),
    # EGC sizing (NEC 250.122)
    (re.compile(r"\begc\s+(sized|sizing|upsized|per\s+nec\s*250\.?122)", re.I),
     "validate_egc_sizing"),
    # GEC sizing (NEC 250.66)
    (re.compile(r"\b(gec\s+sized|grounding\s+electrode\s+conductor\s+(siz|per\s+nec\s*250\.?66))", re.I),
     "validate_gec_sizing"),
    # Voltage drop math (not the "VD workbook exists" input-gate rule)
    (re.compile(r"\bvoltage\s+drop\b.*(limits?|meets|<=|≤|criteria)", re.I),
     "validate_voltage_drop"),
]


def _classify_check_type(
    title: str,
    verify: str,
    source: str | None,
    category: str | None = None,
) -> tuple[str, dict]:
    """Pick a check_type for a V4 rule and return any extra fields it needs.

    Conservative: most rules stay as ``gemini_vision`` (the default). Only
    reclassify when we can extract a specific, deterministic search token
    or match a rule whose formula is implemented in ``electrical_calcs.py``.

    Returns (check_type, extra_fields_dict).
    """
    text = f"{title}\n{verify}".strip()
    lower = text.lower()

    # ── Never reclassify process-gate categories ─────────────────────────
    if category in _NON_CHECKABLE_CATEGORIES:
        return "gemini_vision", {}

    # ── Electrical calc — highest priority ───────────────────────────────
    # If the TITLE matches a known formula pattern, delegate to the
    # corresponding calc function. Title-only match avoids false positives
    # from descriptions that cite other rules' formulas.
    for rx, fn_name in _CALC_TITLE_PATTERNS:
        if rx.search(title):
            return "electrical_calc", {"calc_function": fn_name}

    # ── Veto: comparison / cross-sheet rules stay vision ─────────────────
    # These rules require READING multiple sources and comparing values;
    # a text search for one side of the comparison would pass even when
    # the other side says something different. Keep them as vision.
    if any(tok in lower for tok in _COMPARE_TOKENS):
        return "gemini_vision", {}

    # ── 1. Code-citation-based keyword check ─────────────────────────────
    # If the rule description contains a specific NEC/IEEE/etc. citation
    # AND the rule is clearly about "a note is present on the drawing",
    # convert it to a keyword search for that citation.
    citations = _CODE_CITATION_RE.findall(text)
    citation_matches = _CODE_CITATION_RE.finditer(text)
    citation_strings = list({m.group(0).strip() for m in citation_matches})

    is_note_presence = any(tok in lower for tok in _NOTE_PRESENCE_TOKENS)
    # Also accept titles that explicitly end with " note" or begin with "Note"
    if not is_note_presence and (
        lower.endswith(" note") or lower.startswith("note ") or " note " in (" " + lower + " ")
    ):
        # Still require something specific to search for — otherwise vision
        if citation_strings:
            is_note_presence = True

    if is_note_presence and citation_strings:
        # Use the citations as keywords. Multiple citations → any match passes.
        return "keyword", {
            "keywords": citation_strings,
            "min_matches": 1,
            # No search_scope restriction — the note may live on any sheet.
        }

    # ── 2. NEC code year note (special case) ─────────────────────────────
    # "NEC code year note matches E-001" — search the cover sheet for the
    # adopted NEC year. We don't know which year up front so we list the
    # plausible current set.
    if "code year" in lower and "nec" in lower:
        return "keyword", {
            "keywords": ["NEC 2017", "NEC 2020", "NEC 2023"],
            "min_matches": 1,
            "search_scope": "cover",
        }

    # ── 3. Distinctive electrical tokens + presence verb ─────────────────
    # e.g. "XFMR %Z and X/R shown (even at 30%)" → search for "%Z" / "X/R"
    # Requires the title/description to mention the token AND some variant
    # of "present/shown/listed/etc." — so we don't misclassify rules that
    # say "calculate %Z" or "compare %Z against …".
    has_presence_verb = any(v in lower for v in _PRESENCE_VERBS)
    if has_presence_verb:
        matched_tokens: list[str] = []
        for rx, search_terms in _DISTINCT_TOKEN_RULES:
            if rx.search(text):
                matched_tokens.extend(search_terms)
        if matched_tokens:
            # Deduplicate while preserving order
            seen_tok: set[str] = set()
            unique = [t for t in matched_tokens if not (t in seen_tok or seen_tok.add(t))]
            return "keyword", {
                "keywords": unique,
                "min_matches": 1,
            }

    # ── Default: leave as a vision check ─────────────────────────────────
    return "gemini_vision", {}


def _normalize_status(raw: str) -> str:
    """Collapse the V3→V4 column to a clean tag. Long free-form text is
    treated as 'annotation' (preserved separately)."""
    raw = raw.strip()
    if not raw:
        return ""
    upper = raw.upper()
    if upper in ("V3", "V4-NEW", "V4-ENH", "V4-FIX", "V4-ENHANCED"):
        return upper
    # MP-style short tags
    if upper.startswith("MP") and len(raw) < 30:
        return "MP"
    # anything longer — free-form annotation, not a tag
    if len(raw) > 30:
        return "ANNOTATION"
    return upper


def _make_rule_key(category: str, title: str, seen: dict[str, int]) -> str:
    """Produce a unique snake_case rule key."""
    cat_slug = _slug(category, 20)
    title_slug = _slug(title, 45)
    base = f"v4_{cat_slug}_{title_slug}"
    if base not in seen:
        seen[base] = 1
        return base
    seen[base] += 1
    return f"{base}_{seen[base]}"


# ---------------------------------------------------------------------------
# Main ingestion
# ---------------------------------------------------------------------------


def ingest(xlsx_path: Path) -> tuple[list[dict], dict[str, Any]]:
    """Return (rules list, summary report dict)."""
    wb = openpyxl.load_workbook(str(xlsx_path), data_only=True)
    sh = wb["V4 Final QC Checklist"]

    rules: "OrderedDict[tuple[str, str], dict]" = OrderedDict()
    warnings: list[str] = []
    row_counts = Counter()

    # Pass 1 — pick up every (section, item) row, including the duplicates
    # in the MP Comments block. We key on the pair so duplicates merge.
    header_seen = False
    for row_idx, row in enumerate(sh.iter_rows(min_row=5, values_only=True), start=5):
        row = tuple((row + (None, None, None, None, None))[:5])
        section = _clean(row[0])
        item    = _clean(row[1])
        verify  = _clean(row[2])
        source  = _clean(row[3])
        status  = _clean(row[4])

        # Skip the second "Section | Item to Check | …" header row — it
        # marks the start of the MP Comments block but everything past it
        # is still data we want to merge into existing rules.
        if item == MP_COMMENTS_HEADER and verify == "What to Verify":
            header_seen = True
            continue

        if not item or not verify:
            continue
        if verify.startswith("#NAME?"):
            warnings.append(f"row {row_idx}: '{section} / {item}' has #NAME? in verify column — skipping description")
            verify = ""

        norm_status = _normalize_status(status)
        key = (section, item)

        if key in rules:
            # Duplicate — merge Manjil's annotation into existing rule.
            existing = rules[key]
            if norm_status == "ANNOTATION" and status and status not in (existing.get("annotations") or ""):
                existing.setdefault("annotations", []).append(status)
                row_counts["merged_annotations"] += 1
            elif norm_status and norm_status not in (existing.get("statuses") or []):
                existing.setdefault("statuses", []).append(norm_status)
            continue

        # First time we see this check — create the entry.
        rules[key] = {
            "section": section,
            "item": item,
            "verify": verify,
            "source": source,
            "statuses": [norm_status] if norm_status and norm_status != "ANNOTATION" else [],
            "annotations": [status] if norm_status == "ANNOTATION" else [],
            "row": row_idx,
        }
        row_counts["new_rule"] += 1
        row_counts[f"section:{section}"] += 1
        if header_seen:
            row_counts["created_after_mp_divider"] += 1

    # Convert merged dict → final rules list in original file order.
    out_rules: list[dict] = []
    seen_keys: dict[str, int] = {}
    for (section, item), data in rules.items():
        desc_parts = [data["verify"]] if data["verify"] else []
        for note in data.get("annotations") or []:
            desc_parts.append(f"NOTE: {note}")
        description = "\n".join(desc_parts).strip()
        statuses = data.get("statuses") or []
        primary_status = statuses[0] if statuses else "V3"

        rule_key = _make_rule_key(section or "misc", item, seen_keys)
        severity = _guess_severity(f"{item} {data['verify']}")
        check_type, extras = _classify_check_type(item, data["verify"], data["source"], section)
        row_counts[f"check_type:{check_type}"] += 1

        rule_dict: dict[str, Any] = {
            "key": rule_key,
            "category": section or "Misc",
            "title": item,
            "description": description or item,
            "source": data["source"] or None,
            "severity": severity,
            "check_type": check_type,
            "confidence": 0.75,
            "v4_status": primary_status,
            "v4_row": data["row"],
        }
        # Merge any extra fields (keywords, search_scope, min_matches, ...)
        for k, v in extras.items():
            rule_dict[k] = v
        out_rules.append(rule_dict)

    summary = {
        "input_file": str(xlsx_path),
        "total_rules": len(out_rules),
        "row_counts": dict(row_counts),
        "warnings": warnings,
        "by_status": Counter(r["v4_status"] for r in out_rules),
        "by_severity": Counter(r["severity"] for r in out_rules),
        "by_check_type": Counter(r["check_type"] for r in out_rules),
        "by_category": Counter(r["category"] for r in out_rules),
    }
    return out_rules, summary


def write_yaml(rules: list[dict], dest: Path, source_xlsx: Path) -> None:
    # Build a clean, ordered category_order that covers every category the
    # rules actually use (in case we saw a section not in CATEGORY_ORDER).
    used = list(dict.fromkeys(r["category"] for r in rules))
    ordered: list[str] = [c for c in CATEGORY_ORDER if c in used]
    extras = [c for c in used if c not in ordered]
    ordered.extend(extras)

    # Drop None-valued fields so the output stays tight.
    clean_rules: list[dict] = []
    for r in rules:
        clean = {k: v for k, v in r.items() if v is not None and v != ""}
        clean_rules.append(clean)

    doc: dict[str, Any] = OrderedDict()
    doc["# Generated by"] = "backend/scripts/ingest_v4_checklist.py"
    doc["# Source"] = str(source_xlsx)
    doc["# Do not edit by hand"] = "re-run the ingester instead"
    doc["category_order"] = ordered
    doc["rules"] = clean_rules

    with dest.open("w", encoding="utf-8") as f:
        # Write leading comments manually so PyYAML doesn't quote the keys.
        f.write(f"# Generated by backend/scripts/ingest_v4_checklist.py\n")
        f.write(f"# Source: {source_xlsx}\n")
        f.write(f"# Do not edit by hand — re-run the ingester instead.\n\n")
        yaml.safe_dump(
            {"category_order": ordered, "rules": clean_rules},
            f,
            sort_keys=False,
            width=100,
            allow_unicode=True,
            default_flow_style=False,
        )


def print_report(summary: dict, out_path: Path) -> None:
    print()
    print("=" * 60)
    print(f"Wrote: {out_path}")
    print("=" * 60)
    print(f"Source workbook: {summary['input_file']}")
    print(f"Total rules emitted: {summary['total_rules']}")
    print()
    print("By V3/V4 status:")
    for k, v in summary["by_status"].most_common():
        print(f"  {k or '(none)':<12} {v}")
    print()
    print("By severity (heuristic):")
    for k, v in summary["by_severity"].most_common():
        print(f"  {k:<8} {v}")
    print()
    print("By check_type (classification result):")
    for k, v in summary.get("by_check_type", Counter()).most_common():
        print(f"  {k:<18} {v}")
    print()
    print("By category (top 10):")
    for k, v in summary["by_category"].most_common(10):
        print(f"  {k:<25} {v}")
    print()
    if summary.get("warnings"):
        print(f"Warnings ({len(summary['warnings'])}):")
        for w in summary["warnings"][:10]:
            print(f"  - {w}")
        if len(summary["warnings"]) > 10:
            print(f"  … {len(summary['warnings']) - 10} more")
    else:
        print("No warnings.")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", required=True, type=Path,
                        help="Path to the Claude V4 Final.xlsx workbook.")
    parser.add_argument("--output", required=True, type=Path,
                        help="Where to write the draft rules YAML.")
    args = parser.parse_args()

    if not args.input.exists():
        print(f"error: input file not found: {args.input}", file=sys.stderr)
        return 2

    rules, summary = ingest(args.input)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(rules, args.output, args.input)
    print_report(summary, args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
