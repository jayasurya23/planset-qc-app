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

from .analyzer import PageInfo, make_issue
from .rule_registry import Rule, stage_index

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
    (re.compile(r"\bbod\b|\btech(?:nical)?\s+spec\b|\bowner\s+(spec|criteria)\b|"
                r"\bproject\s+tech\s+spec\b|\bexhibit\s+[a-z]\b", re.I), "bod"),
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

For each rule you should emit exactly ONE of: Pass, Fail, Needs Review,
or SKIP (emit no object). Skipping is a first-class outcome — use it
aggressively when the rule fundamentally can't be evaluated from what
you can see. A finding that ends in "cannot be confirmed from this
sheet alone" is worse than no finding at all; it wastes reviewer time.

- **Pass** — the criterion is clearly met on the sheet(s) shown.
- **Fail** — clearly violated. State the value you read and the value
  required, with the math if applicable. PREFER FAIL OVER NEEDS REVIEW
  when you can see the defect.
- **Needs Review** — use ONLY when the relevant area is visible AND the
  value on THIS sheet is genuinely ambiguous (blurry, partially obscured,
  two conflicting values side-by-side). The reviewer's action is
  "zoom in on this specific spot on this specific sheet."
- **SKIP** — emit no finding for this rule.

=== HARD SKIP TRIGGERS (emit NO finding, do NOT emit NR) ===

If the rule you are evaluating has ANY of these properties, SKIP it —
do not try to partially evaluate, do not NR, just omit the object.

1. The rule title/description contains an "=" comparison between sheet
   codes or documents, e.g.:
     "E-001 = E-050 = E-100 = E-200"
     "E-300 = E-210"
     "E-100 = E-103: same devices"
   ...AND any of those referenced sheets is not visible in the images.

2. The rule mentions phrases like:
     "six-document match", "five-source match", "cross-propagation",
     "matches submittal", "matches CESIR", "matches transmittal",
     "per PVCase export", "CAB calcs Excel", "civil X-Prop",
     "per stringing calc", "matches BOD"
   ...AND the referenced external doc is NOT listed in AVAILABLE EVIDENCE.

3. Your intended evidence sentence would contain any of these phrases:
     "not in view"           "not shown"
     "not available"         "not visible"
     "cannot be confirmed"   "cannot be verified"
     "is not provided"       "is not included"
     "cannot be evaluated"   "no … in view"
     "from this sheet alone" "without access to"
     "the other sheet(s) are not"  "field-by-field comparison cannot"
   If you catch yourself writing any of these → STOP, emit nothing.

4. The rule is asking about a feature this sheet doesn't have
   (e.g. an AC-disconnect check on a datasheet page, a fence spec
   check on a cover sheet).

5. The rule's category clearly doesn't match the sheet you're looking at.

=== WHEN TO PASS vs NR ===

Prefer PASS when:
- The relevant content is visible and nothing looks wrong at a glance.
- The rule is satisfied by what's shown, even if not every sub-detail
  is individually zoom-verified.
- The sheet isn't obviously broken and nothing on it contradicts the rule.

Only emit NR when a reviewer's concrete action is "look closer at
THIS marking on THIS sheet" — e.g. a number that looks wrong but
you can't be sure, two nameplates that disagree, a partially
obscured callout. NR is for on-sheet ambiguity, never for
missing-other-sheet situations.

Do NOT invent values. One finding per rule key. Many skips are
expected — do not pad the output.
"""


_PROMPT_OUTPUT_FORMAT = """\

Return ONLY a JSON array. For each rule in CHECKS, emit at most one
object — Pass, Fail, or Needs Review — OR nothing at all (skip).
Re-read the HARD SKIP TRIGGERS block before writing each finding.
It is normal for many rules in a category to skip; the array length
may be well below the number of checks.

[
  {
    "check": "the_rule_key",
    "status": "Pass" | "Fail" | "Needs Review",
    "severity": "low" | "medium" | "high",
    "value": "the value you read on the drawing (short)",
    "evidence": "one-sentence explanation showing the math/comparison "
                "(or a short reason for NR)",
    "location": "table/row/sheet where you found it",
    "location_text": "short literal searchable excerpt (3-30 chars)"
  }
]

The "check" field MUST match one of the rule keys from the CHECKS list
above exactly. Do not invent new keys.
"""


_CROSS_SHEET_PREAMBLE = """\

=== CROSS-SHEET CATEGORY — ADDITIONAL GATE ===

This category's rules compare values across MULTIPLE sheets
(e.g. "E-001 = E-050 = E-100 = E-200"). You are only given a
representative subset of the planset's pages, NOT every sheet.

For each Cross-Sheet rule:
- List in your head the sheets/docs the rule names.
- If you can actually see ALL of them in the images → evaluate normally.
- If ANY referenced sheet or doc is not in the images → SKIP the rule.
  Do NOT emit "E-100 shows X but E-103 is not visible" as an NR.
  That is exactly the failure mode we are trying to eliminate — skip it.

A Cross-Sheet category response of 2–5 Pass/Fail findings and many
skips is correct. A response full of NRs that say "other sheet not
shown" is wrong.
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
    header = f"{_PROMPT_INTRO}"
    if category == "Cross-Sheet":
        header += _CROSS_SHEET_PREAMBLE
    header += f"\n\n=== CONTEXT ===\nSheet / section under review: {category}"
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
    """Bucket **gemini_vision** rules by category, preserving input order.

    Rules tagged as ``keyword`` / ``electrical_calc`` / ``sheet_existence``
    / etc. are handled by their dedicated legacy loops in ``analyzer.py``
    and are deliberately excluded here so the V4 engine only ever dispatches
    vision calls.
    """
    out: dict[str, list[Rule]] = {}
    for r in rules:
        if r.check_type and r.check_type != "gemini_vision":
            continue
        out.setdefault(r.category, []).append(r)
    return out


def _defer_issue(
    run_id: str,
    rule: Rule,
    planset_stage: str,
    reason_override: str | None = None,
    key_prefix: str = "stage_deferred",
) -> dict:
    """Build a 'Deferred' issue record — shown as N/A in the UI.

    Called in two scenarios:
    1. Stage gating (min_stage > selected stage, sheet absent).
    2. Cross-ref filter (rule needs sheets/docs we don't have).

    ``reason_override`` lets callers customize the human-readable evidence.
    """
    if reason_override:
        evidence = f"Deferred: {reason_override}."
    else:
        evidence = (
            f"Deferred: this check applies at stage {rule.min_stage}+ "
            f"(selected stage: {planset_stage}). Target sheet not present in "
            f"the planset, so the rule was not evaluated."
        )
    return make_issue(
        run_id=run_id,
        item_key=f"{key_prefix}_{rule.key}",
        category=rule.category,
        title=rule.title,
        description=rule.description,
        status="Deferred",
        severity=rule.severity or "low",
        evidence=evidence,
        confidence=1.0,
    )


# ---------------------------------------------------------------------------
# Cross-reference filter — rules whose description demands sheets/docs that
# aren't available cannot be evaluated, and the vision model tends to emit
# "Needs Review: other sheet not in view" instead of skipping. Better to
# defer them at the engine level before they ever reach the prompt.
# ---------------------------------------------------------------------------


_RULE_SHEET_CODE_RE = re.compile(r"\b([EC]-\d{2,3}[A-Z]?)\b", re.I)

# Phrases in a rule title/description that signal a dependency on a specific
# external engineering document. Tags match the keys produced by
# ``available_doc_types``.
_CROSS_DOC_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bcab\s+(calc|weight)", re.I), "cab_calcs"),
    (re.compile(r"\bstringing\s+(calc|export|file)\b|\bpvcase\b", re.I), "stringing"),
    (re.compile(r"\bciv(?:il)?\s*x-?prop\b|\bx-?prop\s+civil", re.I), "civil_xprop"),
    (re.compile(r"\bmatch(?:es)?\s+cesir\b|\bper\s+cesir\b|\bcesir\s+impact\b", re.I), "cesir"),
    (re.compile(r"\b(?:matches?|per)\s+pvsyst\b", re.I), "pvsyst"),
    (re.compile(r"\b(?:matches?|per)\s+transmittal\b", re.I), "transmittal"),
    (re.compile(r"\b(?:matches?|per)\s+bod\b", re.I), "bod"),
    (re.compile(r"\b(?:matches?|per)\s+(?:contract|legal\s+name|permit)\b", re.I), "contract"),
    (re.compile(r"\bmatches?\s+(submittal|datasheet|cut\s?sheet|shop\s?drawing)\b", re.I), "datasheet"),
    (re.compile(r"\b(?:ampacity|neher-?mcgrath)\s+(table|calc)", re.I), "ampacity"),
    # BOD / tech spec language
    (re.compile(r"\b(per|from)\s+(project|tech(?:nical)?)\s+(spec|criteria)\b|\bproject[-\s]specific\b", re.I), "bod"),
    # Owner criteria
    (re.compile(r"\bowner\s+(criteria|spec)\b|\b(per|from)\s+owner\b", re.I), "bod"),
    # Castillo SOP — treated as uploadable; rules referencing it defer unless
    # the SOP doc is supplied with the run.
    (re.compile(r"\b(per|from)\s+SOP\b|\bCastillo\s+SOP\b", re.I), "sop"),
    # Geotech / site-condition-dependent rules
    (re.compile(r"\bsite\s+conditions\b|\bgeotech\b|\bcorrosion\s+report\b", re.I), "geotech"),
    # Civil plans — access roads, grading, X-Prop drawings
    (re.compile(r"\bcivil\s+plan\b|\b(access\s+road|road)\s+(widths?|dim).*\bciv(il)?\b", re.I), "civil_plan"),
    # USACE wetland delineation
    (re.compile(r"\bUSACE\b|\bwetland\s+delineation\b", re.I), "usace"),
    # FEMA flood zone / BFE (base flood elevation)
    (re.compile(r"\bFEMA\b|\bBFE\b|\bflood\s+(zone|study|map)\b", re.I), "fema"),
    # 811 utility locate tickets
    (re.compile(r"\b811\s+locate\b|\butility\s+locate\b", re.I), "utility_locate"),
    # Pipeline / drain-tile crossing agreements
    (re.compile(r"\bcrossing\s+agreement\b|\bpipeline\s+crossing\b|\bdrain\s+tile\b", re.I), "crossing_agreement"),
    # Owner's formula (e.g. MET station count formula from BOD)
    (re.compile(r"\bowner'?s?\s+formula\b|\bowner\s+formula\b", re.I), "bod"),
]

# Phrases indicating the rule demands 2+ independent sources in view at once.
_MULTI_SOURCE_PATTERN = re.compile(
    r"\b(?:six|five|four|seven|eight)[-\s]?(?:document|source)\b|"
    r"\bcross[-\s]?propagation\b|"
    r"\bconsisten\w*\s+across\s+(?:the\s+)?set\b|"
    r"\bfield[-\s]?by[-\s]?field\b",
    re.I,
)

_DOC_FRIENDLY_NAMES: dict[str, str] = {
    "datasheet":   "equipment submittal",
    "cesir":       "CESIR",
    "pvsyst":      "PVSyst export",
    "transmittal": "transmittal",
    "bod":         "BOD / project tech spec",
    "contract":    "contract / permit doc",
    "cab_calcs":   "CAB calcs",
    "stringing":   "stringing calcs",
    "civil_xprop": "civil X-Prop",
    "ampacity":    "ampacity table",
    "sop":                "Castillo SOP reference",
    "geotech":            "geotech / site-conditions report",
    "civil_plan":         "civil plan set",
    "usace":              "USACE wetland delineation",
    "fema":               "FEMA flood map / BFE",
    "utility_locate":     "811 utility locate",
    "crossing_agreement": "pipeline / drain-tile crossing agreement",
}


def _required_sheets(rule: Rule) -> set[str]:
    """Sheet codes the rule depends on, excluding its own category sheet(s)."""
    text = f"{rule.title or ''} {rule.description or ''}"
    codes = {
        _normalize_sheet_code(m.group(1).upper())
        for m in _RULE_SHEET_CODE_RE.finditer(text)
    }
    codes -= set(_expand_code_range(rule.category))
    return codes


def _required_docs(rule: Rule) -> set[str]:
    text = f"{rule.title or ''} {rule.description or ''}"
    return {tag for rx, tag in _CROSS_DOC_PATTERNS if rx.search(text)}


def rule_cross_ref_unavailable(
    rule: Rule,
    planset_sheets: set[str],
    available_docs: set[str],
) -> str | None:
    """If this rule needs evidence we don't have, return a deferral reason.

    Returns ``None`` when the rule is evaluable as dispatched.
    """
    # Missing sheet cross-refs — defer when 2+ external sheets are named and
    # at least one is absent from the planset. A single stray code may be a
    # passing reference rather than a comparison target.
    required_sheets = _required_sheets(rule)
    missing_sheets = required_sheets - planset_sheets
    if missing_sheets and len(required_sheets) >= 2:
        codes = sorted(missing_sheets)
        label = ", ".join(codes[:3]) + ("…" if len(codes) > 3 else "")
        return f"Requires sheet(s) {label} not present in the planset"

    # Missing external-doc cross-refs.
    required_docs = _required_docs(rule)
    missing_docs = required_docs - available_docs
    if missing_docs:
        names = [_DOC_FRIENDLY_NAMES.get(d, d) for d in sorted(missing_docs)]
        label = " / ".join(names)
        return f"Requires {label} — not uploaded"

    # "six-document match" / "cross-propagation" / "consistent across set" —
    # need 2+ cross-ref sources actually in view (sheets-in-planset + docs).
    text = f"{rule.title or ''} {rule.description or ''}"
    if _MULTI_SOURCE_PATTERN.search(text):
        external_refs = len(required_sheets & planset_sheets) + len(available_docs)
        if external_refs < 2:
            return "Requires multiple cross-reference sources — only partial evidence available"

    return None


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
    design_stage: str | None = None,
) -> list[dict]:
    """Queue one vision call per category with its dynamic prompt.

    Parameters
    ----------
    submit          : the caller's thread-pool submitter (e.g. ``_safe_call``)
    prompt_wrap     : appends global instructions + project/evidence context
    deep_for(cat)   : returns True when this category should use the deep model
    page_check      : existing ``_gemini_page_check`` callable
    multi_page_check: existing ``_gemini_multi_page_check`` callable
    design_stage    : "30" / "60" / "90" / "IFC" / "AsBuilt". When set, rules
                      whose ``min_stage`` is later than this stage are deferred
                      UNLESS the target sheet is actually present in the PDF.

    Returns
    -------
    A list of "Deferred" issue dicts for rules that were gated out. The
    vision-dispatched findings flow back through ``submit`` / ``_futures``
    as before; only the synthetic deferred records are returned directly.
    """
    deferred_issues: list[dict] = []
    stage_cutoff = stage_index(design_stage)

    grouped = group_rules(rules)
    available = available_doc_types(supporting_docs)
    sources_note = summarize_available_sources(supporting_docs)

    # Sheets actually present in the uploaded planset — used by the cross-ref
    # filter to decide which rules can be evaluated at all.
    planset_sheets: set[str] = {
        _normalize_sheet_code(p.sheet_number or "")
        for p in pages if p.sheet_number
    }
    planset_sheets.discard("")
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

        # ── Stage gating (lenient for category defaults) ──────────────────
        # If a rule's min_stage is later than the selected design stage, we
        # normally defer it. BUT if the target sheet is actually present in
        # the PDF (target_pages non-empty for sheet-code categories), we let
        # it run anyway — content is there, check it.
        #
        # Exception: when ``min_stage`` came from an EXPLICIT per-rule override
        # (rule_overrides in stage_overrides.yaml), we treat it as authoritative
        # and skip the lenient bypass. The override exists precisely because
        # the rule shouldn't fire at earlier stages even when the sheet is
        # present — e.g. "No TBD at IFC" doesn't apply at 90% even though the
        # equipment-list sheet is always drawn.
        if design_stage:
            sheet_is_present = bool(target_pages) and category not in (
                "Title Block", "Cross-Sheet"
            )
            gated = []
            for r in kept:
                rule_cutoff = stage_index(r.min_stage)
                if rule_cutoff <= stage_cutoff:
                    gated.append(r)  # rule applies at-or-before selected stage
                elif sheet_is_present and not r.min_stage_explicit:
                    gated.append(r)  # lenient bypass — sheet drawn, category default
                else:
                    deferred_issues.append(_defer_issue(run_id, r, design_stage))
            deferred_count = len(kept) - len(gated)
            if deferred_count:
                logger.info(
                    "V4 engine: %s — %d/%d rule(s) deferred (stage=%s, "
                    "sheet_present=%s)",
                    category, deferred_count, len(kept), design_stage,
                    sheet_is_present,
                )
            kept = gated
            if not kept:
                continue

        # ── Cross-reference filter ────────────────────────────────────────
        # Rules whose title/description references sheets or external docs
        # we don't have cannot be evaluated; the vision model consistently
        # emits "Needs Review: other sheet not in view" for these. Defer
        # them at the engine level so they render as "N/A" with a precise
        # "Requires X" reason, instead of polluting the NR bucket.
        evaluable: list[Rule] = []
        xref_deferred = 0
        for r in kept:
            reason = rule_cross_ref_unavailable(r, planset_sheets, available)
            if reason:
                deferred_issues.append(_defer_issue(
                    run_id, r, design_stage or "",
                    reason_override=reason,
                    key_prefix="xref_deferred",
                ))
                xref_deferred += 1
            else:
                evaluable.append(r)
        if xref_deferred:
            logger.info(
                "V4 engine: %s — %d/%d rule(s) deferred (cross-ref evidence missing)",
                category, xref_deferred, len(kept),
            )
        kept = evaluable
        if not kept:
            continue

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

    if deferred_issues:
        logger.info(
            "V4 engine: %d rule(s) deferred for stage=%s",
            len(deferred_issues), design_stage,
        )
    return deferred_issues


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
