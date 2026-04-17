"""Dynamic V4 rule engine — turns ``rules.yaml`` into AI vision checks.

Traditionally the analyzer has dispatched 25 hard-coded prompts
(``_COVER_SHEET_PROMPT``, ``_SLD_PROMPT``, ...). That made every added check
a code change and made the rule registry decorative.

This module replaces that dispatch model for any rule set expressed in V4
form — i.e. rules whose ``category`` is a literal sheet code (``E-100``) or
a conceptual V4 section (``Title Block``, ``Cross-Sheet``). The engine:

1. Groups rules by category.
2. Looks up the matching PDF pages (by literal sheet number, by title
   keywords, or with a category-specific strategy for non-sheet groups).
3. Builds one numbered-list prompt per category asking the AI to verify
   every rule in that group. The rule key travels with each emitted
   finding so results can be keyed back to their source rule.
4. Dispatches the call via the caller-supplied ``submit`` function (so
   the existing parallel-executor + progress pipeline is reused).

Callers pass in ``_gemini_page_check`` / ``_gemini_multi_page_check`` and a
prompt wrapper — the engine doesn't duplicate that infrastructure, it
plugs into it.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable

from .analyzer import PageInfo
from .rule_registry import Rule

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sheet-code matching
# ---------------------------------------------------------------------------


_CODE_NORMALIZE_RE = re.compile(r"[^A-Z0-9./-]")


def _normalize_sheet_code(code: str) -> str:
    """Upper-case + strip spaces; keep E-100 but drop stray punctuation."""
    return _CODE_NORMALIZE_RE.sub("", (code or "").upper()).strip()


def _expand_code_range(category: str) -> list[str]:
    """Turn a category label into a list of concrete sheet codes to match.

    Examples:
        "E-100"         -> ["E-100"]
        "E-101/E-102"   -> ["E-101", "E-102"]
        "E-500-E-504"   -> ["E-500", "E-501", "E-502", "E-503", "E-504"]
        "E-214-E-217"   -> ["E-214", "E-215", "E-216", "E-217"]
        "E-420-E-422"   -> ["E-420", "E-421", "E-422"]
        "Cross-Sheet"   -> []  (not a sheet code)
    """
    cat = (category or "").strip()

    # Slash-delimited list (E-101/E-102)
    if "/" in cat and cat.count("/") <= 3:
        parts = [p.strip() for p in cat.split("/") if p.strip()]
        if all(re.fullmatch(r"[A-Z]+-?\d+[A-Z]?", p, flags=re.I) for p in parts):
            return [_normalize_sheet_code(p) for p in parts]

    # Range like E-214-E-217 or E-500-E-504
    m = re.fullmatch(
        r"\s*([A-Z]+)-?(\d+)[A-Z]?\s*-\s*([A-Z]+)-?(\d+)[A-Z]?\s*",
        cat, flags=re.I,
    )
    if m:
        prefix_a, num_a, prefix_b, num_b = m.group(1), int(m.group(2)), m.group(3), int(m.group(4))
        if prefix_a.upper() == prefix_b.upper() and num_b >= num_a:
            return [f"{prefix_a.upper()}-{n:03d}" for n in range(num_a, num_b + 1)]

    # Single literal code
    if re.fullmatch(r"[A-Z]+-?\d+[A-Z]?", cat, flags=re.I):
        return [_normalize_sheet_code(cat)]

    return []


def find_pages_for_category(pages: list[PageInfo], category: str) -> list[PageInfo]:
    """Given a V4 category, find the PDF pages it refers to.

    For sheet-code categories we match ``page.sheet_number`` exactly
    (normalized). For unknown / conceptual categories, return an empty
    list — caller handles those via a strategy map.
    """
    targets = _expand_code_range(category)
    if not targets:
        return []
    targets_set = set(targets)
    out: list[PageInfo] = []
    for p in pages:
        code = _normalize_sheet_code(p.sheet_number or "")
        if not code:
            continue
        # Direct match
        if code in targets_set:
            out.append(p)
            continue
        # Stripped-letter match — E-100A counts as E-100
        stripped = re.sub(r"[A-Z]$", "", code)
        if stripped in targets_set:
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# Source-availability detection
# ---------------------------------------------------------------------------


# Maps free-text tokens seen in the V4 ``Source Information`` column to the
# canonical supporting-doc type tags used elsewhere in the app. When any of
# these tokens appears in a rule's source field, the rule is only evaluable
# if a doc with the corresponding type was uploaded.
_SOURCE_TOKEN_TO_DOC_TYPE: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bcesir\b|\binterconnection\s+study\b|\bhost\s+capacity\b", re.I), "cesr"),
    (re.compile(r"\bpvsyst\b|\benergy\s+model\b|\byield\s+report\b", re.I),          "pvsyst"),
    (re.compile(r"\bampacity\b|\bneher[-\s]*mcgrath\b",                       re.I), "ampacity"),
    (re.compile(r"\brelay\b|\bprotection\s+coord",                            re.I), "relay"),
    (re.compile(r"\b(asce|wind\s*load|snow\s*load|seismic|pile|structural)\b", re.I), "structural"),
    (re.compile(r"\bsubmittal\b|\bdatasheet\b|\bcut\s*sheet\b|\bshop\s*drawing\b", re.I), "datasheet"),
]

# Source tokens that mean "a file we never model" — when one of these is the
# ONLY dependency, the rule is always skippable unless the evidence block
# actually contains it. We key these to the generic "any extra doc uploaded"
# check rather than a specific doc_type, because the ingest framework
# doesn't have dedicated extractors for them yet.
_EXTRA_DOC_TOKENS: list[re.Pattern] = [
    re.compile(r"\bpvcase\b", re.I),
    re.compile(r"\bcab\s+calc", re.I),
    re.compile(r"\bstringing\s+(calc|export)", re.I),
    re.compile(r"\bvoltage\s*drop\b", re.I),
    re.compile(r"\bload\s+calc\b", re.I),
    re.compile(r"\bshort[-\s]*circuit\s+study\b", re.I),
    re.compile(r"\bgrounding\s+study\b|\bieee\s*80\b", re.I),
    re.compile(r"\barc\s*flash\s+study\b", re.I),
    re.compile(r"\bdas\s+(submittal|drawing|oem)\b", re.I),
    re.compile(r"\bswitchgear\s+oem\b", re.I),
    re.compile(r"\bsurvey\b|\balta\b", re.I),
    re.compile(r"\besb\b|\butility\s+standard", re.I),
]

# Tokens that are ALWAYS available — reference standards the AI can reason
# about from training, or sheets in the planset itself.
_ALWAYS_AVAILABLE: list[re.Pattern] = [
    re.compile(r"\bnec\b|\bnfpa\b|\bansi\b|\bieee\b|\basce\b|\bahj\b|\bosha\b", re.I),
    re.compile(r"\be-?\d{2,4}\b", re.I),     # E-100 etc. = planset sheet
    re.compile(r"\btitle\s+block\b", re.I),
    re.compile(r"\bcastillo\s+sop\b|\bsop\s*\d+\b", re.I),  # our own SOPs
    re.compile(r"\bjoe\s+training\b|\btraining\s+doc", re.I),
]


def available_doc_types(supporting_docs: list[dict] | None) -> set[str]:
    """Normalize uploaded supporting-doc records into the type tags used
    inside rule ``source`` fields."""
    if not supporting_docs:
        return set()
    tags: set[str] = set()
    for d in supporting_docs:
        t = d.get("doc_type") or ""
        if t:
            tags.add(t.lower())
        # Also mine the filename for extra hints (e.g. "bod.pdf")
        fn = (d.get("filename") or "").lower()
        if any(x in fn for x in ("bod", "tech_spec", "techspec", "owner_spec")):
            tags.add("bod")
        if "pvsyst" in fn:
            tags.add("pvsyst")
        if "cesir" in fn or "cesr" in fn:
            tags.add("cesr")
        if "stringing" in fn or "pvcase" in fn:
            tags.add("stringing")
        if "ampacity" in fn:
            tags.add("ampacity")
        if "cab" in fn and "calc" in fn:
            tags.add("cab_calcs")
    return tags


def source_is_available(source: str | None, available: set[str]) -> bool:
    """Given a rule's ``source`` string, is the referenced document present?

    Returns True if the rule can be evaluated against available inputs.
    Returns False only when the source CLEARLY points at an external doc
    the user did not upload. Ambiguous cases default to True — the vision
    model still has a chance to make a judgment call.
    """
    if not source:
        return True  # no declared source = use what's on the planset

    # Sources may list multiple refs; treat as "any present" semantics.
    # If the source contains BOTH an available and an unavailable token,
    # we still return True (ambiguous → let the model try).
    s = source

    # Reference standards / planset sheets — always available
    for rx in _ALWAYS_AVAILABLE:
        if rx.search(s):
            return True

    # Check each external-doc-type hint
    referenced_types: list[str] = []
    for rx, doc_type in _SOURCE_TOKEN_TO_DOC_TYPE:
        if rx.search(s):
            referenced_types.append(doc_type)

    referenced_extras: list[str] = []
    for rx in _EXTRA_DOC_TOKENS:
        if rx.search(s):
            referenced_extras.append(rx.pattern)

    if not referenced_types and not referenced_extras:
        # Source text exists but matches nothing we recognize — conservative
        # default: allow the check to proceed.
        return True

    # If ANY referenced doc type is present, we can evaluate.
    if any(t in available for t in referenced_types):
        return True
    # Extras aren't classified, but if *any* supporting doc was uploaded we
    # optimistically allow the check — evidence block may cover it.
    if referenced_extras and available:
        return True
    return False


def summarize_available_sources(supporting_docs: list[dict] | None) -> str:
    """Human-readable one-liner about which evidence sources are loaded."""
    if not supporting_docs:
        return "Only the planset PDF is in view. No external engineering documents (CESIR, PVSyst, ampacity, datasheets, BOD) have been uploaded."
    lines = []
    for d in supporting_docs:
        t = d.get("doc_type") or "generic"
        fn = d.get("filename") or "?"
        summary = d.get("summary") or ""
        summary = summary[:120] + "..." if len(summary) > 120 else summary
        lines.append(f"  - {t}: {fn}" + (f" ({summary})" if summary else ""))
    return (
        "External engineering documents uploaded and available as evidence "
        "for this run:\n" + "\n".join(lines)
    )


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------


_PROMPT_INTRO = """\
You are a senior solar PV QC engineer auditing the sheets shown for the
checks listed below. Each check has a unique rule key — report findings
using that key so results can be tracked.

READ the drawings carefully. For each numbered check decide ONE of:
- **Pass** — the criterion is clearly met on the sheet(s) shown.
- **Fail** — clearly violated. State the value you read and the value
  required, with the math if applicable.
- **Needs Review** — the check IS in scope for these sheets (the
  sheet-under-review has fields that should contain the required value),
  but the value is blurry, ambiguous, or you cannot verify without more
  context.

**SKIP entirely (emit NO finding)** when:
- The rule targets a document or sheet that is NOT in view
  (e.g. a check that says "E-300 cable schedule" when you are only
  shown E-100; a check that says "compare to inverter submittal" when
  no submittal is provided).
- The rule requires an external workbook (PVSyst, ampacity study, CAB
  calcs, stringing export, short-circuit study) unless its values are
  also in a Supporting Documents evidence block appended to this
  prompt.
- The rule asks you to check a physical property only visible with site
  access (existing pole locations, as-built conditions).

Better to skip than to spam "Needs Review". Only emit findings for rules
you could actually evaluate against what is shown above.

Do NOT invent values. Only emit one finding per rule key.
"""


_PROMPT_OUTPUT_FORMAT = """\

Return ONLY a JSON array. Emit one object PER RULE YOU EVALUATED
(Pass, Fail, or Needs Review). **Omit rules you skipped** — do not emit
an object for out-of-scope rules.

[
  {
    "check": "the_rule_key",
    "status": "Pass" | "Fail" | "Needs Review",
    "severity": "low" | "medium" | "high",
    "value": "the value you read on the drawing (short)",
    "evidence": "one-sentence explanation showing the math/comparison",
    "location": "table/row/sheet where you found it",
    "location_text": "short literal searchable excerpt (3-30 chars)"
  }
]

The "check" field MUST match one of the rule keys from the CHECKS list
above exactly. Do not invent new keys. Every emitted finding must be
something you could actually see on the provided sheet(s).
"""


def _rule_to_block(rule: Rule, idx: int) -> str:
    """Format one rule as a numbered checklist block."""
    lines = [f"{idx}. [rule_key: {rule.key}] {rule.title}"]
    if rule.description and rule.description != rule.title:
        # Collapse multi-line descriptions to avoid blowing up token count
        desc = rule.description.replace("\n", " ").strip()
        if len(desc) > 320:
            desc = desc[:317] + "..."
        lines.append(f"   Verify: {desc}")
    if rule.source:
        lines.append(f"   Source: {rule.source}")
    return "\n".join(lines)


def build_category_prompt(
    category: str,
    rules: list[Rule],
    sheet_hint: str = "",
    sources_note: str = "",
) -> str:
    """Produce a full vision prompt for all rules in a category."""
    header = f"{_PROMPT_INTRO}\n\n=== CONTEXT ===\nSheet / section under review: {category}"
    if sheet_hint:
        header += f"\n{sheet_hint}"
    if sources_note:
        header += f"\n\n=== AVAILABLE EVIDENCE ===\n{sources_note}"
    header += "\n\n=== CHECKS ==="

    blocks = [_rule_to_block(r, i) for i, r in enumerate(rules, start=1)]
    body = "\n\n".join(blocks)

    return f"{header}\n{body}\n{_PROMPT_OUTPUT_FORMAT}"


# ---------------------------------------------------------------------------
# Category strategy — which pages go with each category
# ---------------------------------------------------------------------------


# Categories we never run here (handled elsewhere or out-of-scope for the
# planset itself).
_SKIP_CATEGORIES: set[str] = {
    "AI Input Gate",
    "BOD / Due Diligence",
}


def _pages_for_title_block(pages: list[PageInfo]) -> list[PageInfo]:
    """Title Block checks: spot-check across a representative sample."""
    if not pages:
        return []
    sample_idxs = {0, len(pages) // 2, len(pages) - 1}
    return [pages[i] for i in sorted(sample_idxs) if 0 <= i < len(pages)]


_OTHER_ELECTRICAL_CATEGORY = "Other Electrical"


_OTHER_ELECTRICAL_PROMPT = """\
You are a senior solar PV QC engineer reviewing an electrical drawing sheet
that falls outside the V4 checklist's explicit sheet-code categories
(e.g. an extension sheet like E-201, E-301, a legend, or a detail sheet).
Apply ONLY the generic QC checks below — no category-specific math, no
cross-sheet comparisons.

For each check below, if you see a problem on THIS sheet, emit one finding
using the given rule key. If the check does NOT apply or everything looks
fine for that check, DO NOT emit an object for it — just omit it.

CHECKS:
1. [gen_placeholders]  Placeholders / incomplete fields — "TBD", "XXX",
   "TODO", "???", empty values where numbers are expected, "NO" in a
   submittal column.
2. [gen_title_block]   Title block completeness — project name, sheet
   number, revision, date, designer / engineer / PE fields populated.
3. [gen_revisions]     Revision clouds — present ONLY on current-revision
   changes, not stale from prior revisions.
4. [gen_stale_text]    Stale / orphaned text — leftover from another
   project (wrong project name, wrong client, old revision dates).
5. [gen_units]         Units — every numeric value carries units (V, A,
   kVA, kW, ft, in, deg, %). Flag missing units on labeled values.
6. [gen_scale]         Scale annotation + north arrow — on any plan or
   detail sheet with dimensions, scale is shown and bar scale matches
   the written scale.
7. [gen_line_weights]  Line weights / color — for utility-submission
   sheets, expect black linework only. Reviewer redlines are OK (not
   designer errors); designer-drawn color IS a defect.
8. [gen_system_info]   Value contradictions — if the sheet mentions
   module make/model, inverter make/model/kVA/kW, total DC kW or AC kVA,
   values must match the system info on the cover sheet.
9. [gen_notes]         Notes block — notes numbered, readable, latest
   version (not stale from a prior code cycle).
10. [gen_typos]        Typos / spelling / inconsistent terminology.

SPECIAL CASE — CLEAN SHEET:
If NONE of the 10 checks finds a problem on this sheet (which is common
for detail/legend/extension sheets), emit EXACTLY ONE finding:

    {"check": "gen_clean", "status": "Pass",
     "evidence": "Sheet reviewed; no generic QC issues found.",
     "location": "whole sheet", "severity": "low"}

Do NOT emit a "Needs Review" for clean sheets. Clean means Pass.

Return the JSON array with at most 10 findings (or the single gen_clean
Pass). The "check" field MUST be one of the bracketed rule keys above.
Do NOT invent keys. Do NOT re-emit generic "sheet reviewed" under
multiple keys.
"""


# Pretty titles for the catch-all's synthetic rule keys. Consumed by
# ``_pretty_title_for`` in gemini_analyzer so findings show readable names.
OTHER_ELECTRICAL_RULE_TITLES: dict[str, str] = {
    "gen_placeholders": "Placeholders / TBDs / empty fields",
    "gen_title_block":  "Title block completeness",
    "gen_revisions":    "Revision clouds current-only",
    "gen_stale_text":   "Stale / orphaned text from prior project",
    "gen_units":        "Units on every numeric value",
    "gen_scale":        "Scale annotation + north arrow",
    "gen_line_weights": "Line weights / color (black for utility)",
    "gen_system_info":  "Values match system information table",
    "gen_notes":        "Notes numbered / readable / current",
    "gen_typos":        "Typos / spelling / inconsistent terminology",
    "gen_clean":        "Sheet reviewed — no generic issues",
}


def _pages_for_cross_sheet(pages: list[PageInfo]) -> list[PageInfo]:
    """Cross-Sheet checks: bundle a representative page per major section."""
    out: list[PageInfo] = []
    prefixes_seen: set[str] = set()
    # Priority: early electrical sheets first. Sort by sheet number.
    def key(p: PageInfo) -> tuple[str, int]:
        c = _normalize_sheet_code(p.sheet_number or "")
        m = re.match(r"([A-Z]+)-?(\d+)", c)
        if m:
            return (m.group(1), int(m.group(2)))
        return ("Z", 9999)
    for p in sorted(pages, key=key):
        c = _normalize_sheet_code(p.sheet_number or "")
        m = re.match(r"([A-Z]+-?\d)", c)
        bucket = m.group(1) if m else c
        if bucket and bucket not in prefixes_seen:
            prefixes_seen.add(bucket)
            out.append(p)
        if len(out) >= 6:
            break
    return out


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def is_v4_ruleset(rules: list[Rule]) -> bool:
    """Heuristic: is this a V4-style rule set?

    V4 rules are ingested with a ``source`` and/or ``v4_status`` field. If
    a non-trivial fraction of the rules carry those, the engine takes
    over; otherwise the caller falls back to the legacy hard-coded
    prompts.
    """
    if not rules:
        return False
    tagged = sum(1 for r in rules if (r.source or r.v4_status))
    return tagged >= max(20, int(0.3 * len(rules)))


def group_rules(rules: list[Rule]) -> dict[str, list[Rule]]:
    """Bucket rules by category, preserving input order."""
    out: dict[str, list[Rule]] = {}
    for r in rules:
        out.setdefault(r.category, []).append(r)
    return out


def run_v4_checks(
    *,
    doc,
    pages: list[PageInfo],
    rules: list[Rule],
    submit: Callable[..., Any],
    prompt_wrap: Callable[[str], str],
    deep_for: Callable[[str], bool],
    page_check: Callable,
    multi_page_check: Callable,
    run_id: str,
    run_dir,
    supporting_docs: list[dict] | None = None,
) -> None:
    """Queue one vision call per category with its dynamic prompt.

    Parameters
    ----------
    submit          : the caller's thread-pool submitter (e.g. ``_safe_call``)
    prompt_wrap     : appends global instructions + project/evidence context
    deep_for(cat)   : returns True when this category should use the deep model
    page_check      : existing ``_gemini_page_check`` callable
    multi_page_check: existing ``_gemini_multi_page_check`` callable
    """
    grouped = group_rules(rules)
    available = available_doc_types(supporting_docs)
    sources_note = summarize_available_sources(supporting_docs)
    if available:
        logger.info("V4 engine: available supporting-doc types = %s",
                    sorted(available))
    else:
        logger.info("V4 engine: no supporting docs uploaded — "
                    "rules requiring external evidence will be filtered out")

    # Track which pages got at least one V4 dispatch so the catch-all
    # "Other Electrical" pass at the end only covers the uncovered ones.
    covered_pages: set[int] = set()

    for category, cat_rules in grouped.items():
        if category in _SKIP_CATEGORIES:
            logger.info("V4 engine: skipping %s (%d rules — handled elsewhere)",
                        category, len(cat_rules))
            continue

        # Pre-filter rules whose source requires a doc we don't have.
        kept = [r for r in cat_rules if source_is_available(r.source, available)]
        dropped = len(cat_rules) - len(kept)
        if dropped:
            logger.info(
                "V4 engine: %s — %d/%d rules filtered (external source unavailable)",
                category, dropped, len(cat_rules),
            )
        if not kept:
            logger.info("V4 engine: %s — all rules filtered, skipping category",
                        category)
            continue

        # Select pages for this category
        if category == "Title Block":
            target_pages = _pages_for_title_block(pages)
        elif category == "Cross-Sheet":
            target_pages = _pages_for_cross_sheet(pages)
        else:
            target_pages = find_pages_for_category(pages, category)

        if not target_pages:
            logger.info(
                "V4 engine: no pages matched category %r — skipping %d rules",
                category, len(kept),
            )
            continue

        # Sheet hint for the prompt
        sheet_hint = ""
        if category not in ("Title Block", "Cross-Sheet"):
            codes = sorted({(p.sheet_number or "?") for p in target_pages})
            sheet_hint = f"Sheets in view: {', '.join(codes)}"

        prompt = prompt_wrap(
            build_category_prompt(category, kept, sheet_hint, sources_note)
        )
        deep = deep_for(category)

        # Dispatch
        item_key_prefix = f"v4_{_normalize_sheet_code(category).lower() or 'cat'}"
        if len(target_pages) == 1:
            submit(
                page_check, doc, target_pages[0].number, prompt,
                run_id, run_dir, category, item_key_prefix, category,
                deep=deep,
            )
            covered_pages.add(target_pages[0].number)
        else:
            # Multi-page — cap to 5 pages to stay within vision context limits
            chosen = target_pages[:5]
            page_numbers = [p.number for p in chosen]
            submit(
                multi_page_check, doc, page_numbers, prompt,
                run_id, run_dir, category, item_key_prefix,
                f"{category} ({len(page_numbers)} pages)",
                deep=deep,
            )
            covered_pages.update(p.number for p in chosen)

    # ── Catch-all "Other Electrical" for uncovered pages ──────────────────
    # Some planset pages (extension sheets E-201..E-205, detail sheets D-100,
    # architectural sheets A-100, 800-series submittals, etc.) don't match any
    # V4 category. Run a generic QC prompt on them so every page in the
    # planset gets at least some review instead of zero coverage.
    uncovered = [p for p in pages if p.number not in covered_pages]
    # Exclude pages whose sheet number we couldn't read (rare) — generic
    # review on a truly blank title block isn't useful.
    uncovered = [p for p in uncovered if (p.sheet_title or "") or (p.sheet_number or "")]
    if uncovered:
        logger.info(
            "V4 engine: 'Other Electrical' catch-all — %d uncovered page(s): %s",
            len(uncovered),
            ", ".join(p.sheet_number or f"p{p.number}" for p in uncovered[:10]),
        )
        # Cap at 12 generic-review calls to keep cost bounded on huge plansets
        for p in uncovered[:12]:
            sheet_id = p.sheet_number or f"p{p.number}"
            sheet_hint = (
                f"Sheet code: {p.sheet_number or '(unknown)'}"
                + (f" / Title: {p.sheet_title}" if p.sheet_title else "")
            )
            full_prompt = prompt_wrap(
                _OTHER_ELECTRICAL_PROMPT + f"\n\n=== CONTEXT ===\n{sheet_hint}"
            )
            submit(
                page_check, doc, p.number, full_prompt,
                run_id, run_dir,
                _OTHER_ELECTRICAL_CATEGORY,
                f"v4_other_{_normalize_sheet_code(sheet_id).lower() or p.number}",
                f"Sheet {sheet_id}",
                deep=False,  # generic review doesn't need the deep model
            )


# ---------------------------------------------------------------------------
# Which categories justify the deep model
# ---------------------------------------------------------------------------


_DEEP_CATEGORIES: set[str] = {
    "E-100",            # AC SLD — NEC + math heavy
    "E-101/E-102",      # LV switchboard — same
    "E-103/E-104",      # 3-line
    "E-110",            # Relay settings / coordination
    "E-120",            # String voltage window math
    "E-106",            # Stringing / fuse math
    "E-300",            # Circuit schedule / ampacity
    "E-500-E-504",      # Grounding
    "Cross-Sheet",      # Cross-document consistency
}


def deep_for_category(category: str) -> bool:
    return category in _DEEP_CATEGORIES
