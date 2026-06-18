"""Consolidate overlapping findings that flag the SAME concept from different
check engines.

On dense electrical sheets (grounding, single-line, three-line) the same item
is reviewed by several independent passes — a per-sheet AI vision check
(``ai_gnd_*``), a multi-page "deep" vision check (``ai_gnd_deep_*``), and a
deterministic keyword check (``gnd_*``). Each can independently return a
finding (often "Needs Review") for the same NEC item, e.g. the main bonding
jumper gets flagged 3x. That inflates the review list without adding distinct
information.

This module merges such duplicates: findings in the SAME category that share a
concept signature (an explicit "NEC 250.x" reference or a specific concept
keyword) AND come from at least two DIFFERENT check engines are collapsed into
one. The survivor keeps the MOST SEVERE status in the group (so a real Fail is
never hidden behind a duplicate) and the richest evidence, with a short note
recording the consolidation. Findings with no recognized concept signature are
never touched.
"""
from __future__ import annotations

import re
from typing import Any

# Explicit "NEC 250.102" / "per NEC 250.66" style reference (requires the word
# NEC so a stray numeric value can't masquerade as a code reference).
_NEC_RE = re.compile(r"nec[\s.\-]*(\d{2,3}\.\d+)", re.I)

# Specific concept keywords (matched against the finding TITLE). Order matters:
# the first match wins. These are the items that demonstrably double-fire.
_KEYWORD_GROUPS: list[tuple[str, tuple[str, ...]]] = [
    ("main_bonding_jumper", ("main bonding jumper", "bonding jumper", " mbj")),
    ("grounding_electrode_conductor", ("grounding electrode conductor", "(gec)", " gec ")),
    ("equipment_grounding_conductor", ("equipment grounding conductor", "(egc)", " egc ")),
    ("transformer_grounding", ("transformer grounding", "transformer primary grounding",
                               "transformer secondary grounding", "xfmr grounding")),
    ("ground_rod", ("ground rod",)),
    ("grounding_ring", ("grounding ring", "ground ring", "ground loop")),
    ("code_references", ("code reference", "code callout", "nec reference", "code references")),
    ("ct_vt_arrangement", ("ct/vt arrangement", "ct vt arrangement", "ct/pt arrangement",
                           "ct & vt", "ct and vt")),
    ("meter_accuracy", ("meter accuracy",)),
]

_SEVERITY = {"Fail": 4, "Needs Review": 3, "Pass": 2, "Deferred": 1}

# NEC articles that map to one of the concepts above, so a finding that cites
# only the code (e.g. "GEC sized per NEC 250.66") unifies with one that uses the
# spelled-out concept ("Grounding Electrode Conductor…"). Unlisted articles fall
# back to a per-reference signature.
_NEC_TO_CONCEPT = {
    "250.102": "main_bonding_jumper",
    "250.66": "grounding_electrode_conductor",
    "250.122": "equipment_grounding_conductor",
}


def concept_signature(category: str, title: str, evidence: str = "") -> str | None:
    """A stable per-concept key, or None when the finding isn't dedupable.

    A spelled-out concept keyword takes precedence; otherwise an explicit NEC
    reference is used (mapped to its concept when known, so the two forms unify).
    """
    title_l = (title or "").lower()
    for canon, kws in _KEYWORD_GROUPS:
        if any(k in title_l for k in kws):
            return f"{category}::{canon}"
    m = _NEC_RE.search(f"{title or ''} {evidence or ''}")
    if m:
        nec = m.group(1)
        return f"{category}::{_NEC_TO_CONCEPT.get(nec, 'nec:' + nec)}"
    return None


def _engine_family(item_key: str) -> str:
    """The check engine that produced a finding, e.g. ai_gnd_deep / ai_gnd / gnd."""
    m = re.match(r"(ai_[a-z0-9]+_deep|ai_[a-z0-9]+|[a-z0-9]+)", item_key or "")
    return m.group(1) if m else (item_key or "")


def _rank(f: dict[str, Any]) -> tuple:
    return (
        _SEVERITY.get(f.get("status"), 0),
        1 if f.get("page_number") is not None else 0,
        len(f.get("evidence") or ""),
        f.get("confidence") or 0.0,
    )


def _merge(group: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick the best representative; note the consolidation; preserve severity."""
    rep = dict(max(group, key=_rank))
    note = f"Consolidated {len(group)} overlapping QC checks covering this item."
    desc = (rep.get("description") or "").rstrip()
    rep["description"] = f"{desc}\n\n{note}".strip() if desc else note
    return rep


def consolidate_overlapping_findings(
    issues: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Collapse same-concept, multi-engine duplicate findings into one each.

    Order-preserving (a merged group takes the position of its first member).
    Only merges a group when its members span >=2 distinct check engines, so
    genuinely distinct same-engine findings are left alone.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    sequence: list[tuple[str, Any]] = []
    for f in issues:
        sig = concept_signature(
            f.get("category", "") or "", f.get("title", "") or "", f.get("evidence", "") or ""
        )
        if sig is None:
            sequence.append(("pass", f))
            continue
        if sig not in groups:
            groups[sig] = []
            sequence.append(("group", sig))
        groups[sig].append(f)

    out: list[dict[str, Any]] = []
    for kind, ref in sequence:
        if kind == "pass":
            out.append(ref)
            continue
        group = groups[ref]
        engines = {_engine_family(g.get("item_key", "")) for g in group}
        if len(group) > 1 and len(engines) >= 2:
            out.append(_merge(group))
        else:
            out.extend(group)  # single, or all from one engine -> keep as-is
    return out
