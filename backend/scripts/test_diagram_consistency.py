"""Unit tests for the vision-based diagram-consistency pass.

Runs WITHOUT any real AI call: the ``ai_call`` and ``render`` functions are
stubbed with synthetic annotations, so the whole pipeline is exercised
deterministically.

Asserts:
  (a) two sheets, same label, conflicting numeric values -> ONE Fail finding
      with 2 locations;
  (b) same label, equivalent values (18 ft vs "18'-0\"") -> NO finding;
  (c) a recognized label (transformer kVA) is routed to provenance (appears in
      the provenance dict) and NOT double-flagged by the open-ended check;
  (d) the label->field mapper maps a few representative labels.

Usage (from backend/):
    python scripts/test_diagram_consistency.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import consistency, diagram_consistency
from app.diagram_consistency import (
    map_label_to_field,
    cross_check_annotations,
    run_diagram_consistency,
    select_diagram_pages,
)


_passed = 0
_failed = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {name}")
    else:
        _failed += 1
        print(f"  FAIL  {name}  {extra}")


# ── Synthetic PageInfo + stubs ──────────────────────────────────────────────

class _Page:
    """Stand-in for analyzer.PageInfo (duck-typed: number/sheet_number/title)."""

    def __init__(self, number, sheet_number, sheet_title=None, text=""):
        self.number = number
        self.sheet_number = sheet_number
        self.sheet_title = sheet_title
        self.text = text


def _make_stub_ai(by_sheet: dict):
    """Build an ``ai_call(image, prompt)`` returning the JSON the model would
    emit for a sheet. ``image`` here is the sheet_number string (our stub
    ``render`` returns it verbatim)."""
    import json

    def _ai(image, prompt):
        sheet = image if isinstance(image, str) else image.decode("utf-8")
        return json.dumps(by_sheet.get(sheet, []))

    return _ai


def _stub_render(page):
    # Skip real rasterization: hand the sheet number to the stubbed AI.
    return page.sheet_number


# ── (d) label -> field mapper ───────────────────────────────────────────────

def test_label_field_mapper() -> None:
    print("map_label_to_field — representative labels:")
    cases = [
        ("Transformer T1 kVA", "equipment_rating", "transformer_kva"),
        ("XFMR T2 kVA", "equipment_rating", "transformer_kva"),
        ("Row pitch", "dimension", "row_pitch"),
        ("Pole spacing", "dimension", "pole_spacing"),
        ("Pile spacing", "dimension", "pile_spacing"),
        ("Inverter INV-1 kW", "equipment_rating", "inverter_kw"),
        ("Secondary voltage", "voltage", "transformer_secondary_voltage"),
        ("GCR", "label", "gcr"),
        ("Module azimuth", "dimension", "azimuth"),
    ]
    for label, cat, expected in cases:
        got = map_label_to_field(label, cat)
        check(f"'{label}' -> {expected}", got == expected, f"got {got}")

    # Un-recognized labels return None (flow to the open-ended check).
    for label in ("Recloser R1 amps", "Some random callout", "Module count"):
        got = map_label_to_field(label, "other")
        check(f"'{label}' unmapped (None)", got is None, f"got {got}")


# ── (a) conflicting numeric values -> one Fail finding, 2 locations ─────────

def test_numeric_conflict() -> None:
    print("cross_check_annotations — numeric conflict:")
    annotations = [
        {"label": "Pile count", "value": "240", "category": "count",
         "page_number": 5, "sheet_number": "C-101"},
        {"label": "Pile count", "value": "260", "category": "count",
         "page_number": 8, "sheet_number": "C-102"},
    ]
    findings = cross_check_annotations(annotations)
    check("exactly one finding", len(findings) == 1, f"got {len(findings)}")
    if not findings:
        return
    f = findings[0]
    check("status Fail", f["status"] == "Fail", f"got {f['status']}")
    check("category Cross-Sheet Consistency",
          f["category"] == "Cross-Sheet Consistency", f"got {f['category']}")
    check("item_key diagram_*", f["item_key"].startswith("diagram_"),
          f"got {f['item_key']}")
    locs = f.get("locations") or []
    check("2 locations", len(locs) == 2, f"got {len(locs)}")
    if len(locs) == 2:
        pages = {l["page_number"] for l in locs}
        check("locations on pages 5 and 8", pages == {5, 8}, f"got {pages}")
        values = {l["value"] for l in locs}
        check("location values present", values == {"240", "260"}, f"got {values}")
        check("source_label is '<sheet> drawing'",
              all(l["source_label"].endswith("drawing") for l in locs),
              f"got {[l['source_label'] for l in locs]}")
        check("bbox left None for analyzer to render",
              all(l["bbox"] is None for l in locs))


# ── (b) equivalent dimension values -> no finding ───────────────────────────

def test_equivalent_no_finding() -> None:
    print("cross_check_annotations — equivalent dimension (18 ft vs 18'-0\"):")

    # Sabotage the AI reconciler so the test fails loudly if it is reached —
    # equivalent numeric dimensions must normalize to the same key with no AI.
    def _boom(*a, **k):  # pragma: no cover
        raise AssertionError("reconcile_text must NOT be called for equivalent values")

    orig = consistency.reconcile_text
    consistency.reconcile_text = _boom
    try:
        annotations = [
            {"label": "Row pitch", "value": "18 ft", "category": "dimension",
             "page_number": 3, "sheet_number": "C-101"},
            {"label": "Row pitch", "value": "18'-0\"", "category": "dimension",
             "page_number": 6, "sheet_number": "C-102"},
        ]
        findings = cross_check_annotations(annotations)
        check("no finding for equivalent dimensions", len(findings) == 0,
              f"got {len(findings)}")
    finally:
        consistency.reconcile_text = orig


def test_single_page_no_finding() -> None:
    print("cross_check_annotations — same label, single page -> no finding:")
    annotations = [
        {"label": "Pile count", "value": "240", "category": "count",
         "page_number": 5, "sheet_number": "C-101"},
        {"label": "Pile count", "value": "260", "category": "count",
         "page_number": 5, "sheet_number": "C-101"},  # same page — not cross-sheet
    ]
    findings = cross_check_annotations(annotations)
    check("no finding when only one page involved", len(findings) == 0,
          f"got {len(findings)}")


def test_garbage_label_ignored() -> None:
    print("cross_check_annotations — garbage/short labels ignored:")
    annotations = [
        {"label": "X", "value": "1", "category": "other",
         "page_number": 1, "sheet_number": "E-100"},
        {"label": "X", "value": "2", "category": "other",
         "page_number": 2, "sheet_number": "E-101"},
        {"label": "12", "value": "1", "category": "other",
         "page_number": 1, "sheet_number": "E-100"},
        {"label": "12", "value": "2", "category": "other",
         "page_number": 2, "sheet_number": "E-101"},
    ]
    findings = cross_check_annotations(annotations)
    check("no finding for single-char / digit-only labels", len(findings) == 0,
          f"got {len(findings)}")


def test_metadata_label_ignored() -> None:
    print("cross_check_annotations — title-block metadata ignored:")
    annotations = [
        {"label": "Sheet name", "value": "THREE LINE DIAGRAM - SHEET 1",
         "category": "label", "page_number": 8, "sheet_number": "E-105"},
        {"label": "Sheet name", "value": "Single Line Diagram - Sheet 1",
         "category": "label", "page_number": 6, "sheet_number": "E-100"},
        {"label": "Sheet number", "value": "E-105", "category": "label",
         "page_number": 8, "sheet_number": "E-105"},
        {"label": "Sheet number", "value": "E-100", "category": "label",
         "page_number": 6, "sheet_number": "E-100"},
        {"label": "Revision", "value": "A", "category": "label",
         "page_number": 8, "sheet_number": "E-105"},
        {"label": "Revision", "value": "B", "category": "label",
         "page_number": 6, "sheet_number": "E-100"},
    ]
    findings = cross_check_annotations(annotations)
    check("no finding for sheet name / number / revision metadata",
          len(findings) == 0,
          f"got {len(findings)}: {[f['item_key'] for f in findings]}")


def test_multi_instance_label_ignored() -> None:
    print("cross_check_annotations — multi-instance label (FLA) ignored:")
    # E-100 lists FLA for several pieces of equipment -> multiple distinct
    # values on ONE page -> per-instance, not a single cross-sheet value.
    annotations = [
        {"label": "FLA", "value": "1786.5 A", "category": "count",
         "page_number": 6, "sheet_number": "E-100"},
        {"label": "FLA", "value": "82.8 A", "category": "count",
         "page_number": 6, "sheet_number": "E-100"},
        {"label": "FLA", "value": "41.4 A", "category": "count",
         "page_number": 6, "sheet_number": "E-100"},
        {"label": "FLA", "value": "198.5 A", "category": "count",
         "page_number": 7, "sheet_number": "E-101"},
    ]
    findings = cross_check_annotations(annotations)
    check("no finding for multi-instance FLA label", len(findings) == 0,
          f"got {len(findings)}")


# ── (c) recognized label routed to provenance, not double-flagged ───────────

def test_mapped_label_routed_to_provenance() -> None:
    print("run_diagram_consistency — recognized label -> provenance:")

    # Sheet E-100 and a schedule E-300 both read a transformer kVA (a MAPPED
    # label) AND a Recloser rating (an UN-mapped label) that conflicts.
    by_sheet = {
        "E-100": [
            {"label": "Transformer T1 kVA", "value": "2500 kVA",
             "category": "equipment_rating"},
            {"label": "Recloser R1 rating", "value": "630 A", "category": "schedule"},
        ],
        "E-300": [
            {"label": "Transformer T1 kVA", "value": "2000 kVA",
             "category": "equipment_rating"},
            {"label": "Recloser R1 rating", "value": "400 A", "category": "schedule"},
        ],
    }
    pages = [
        _Page(7, "E-100", "ELECTRICAL ONE LINE DIAGRAM"),
        _Page(20, "E-300", "CONDUCTOR SCHEDULE"),
    ]

    provenance: dict = {}
    findings = run_diagram_consistency(
        pages, provenance,
        render=_stub_render, ai_call=_make_stub_ai(by_sheet),
    )

    # (c1) transformer_kva (mapped) lands in provenance, NOT in the returned
    # open-ended findings.
    check("transformer_kva merged into provenance",
          "transformer_kva" in provenance, f"keys={list(provenance.keys())}")
    if "transformer_kva" in provenance:
        vals = {s["value"] for s in provenance["transformer_kva"]}
        check("both kVA sightings recorded in provenance",
              vals == {"2500 kVA", "2000 kVA"}, f"got {vals}")
        pages_seen = {s["page_number"] for s in provenance["transformer_kva"]}
        check("provenance sightings carry page numbers",
              pages_seen == {7, 20}, f"got {pages_seen}")

    mapped_in_findings = any("transformer" in f["item_key"] for f in findings)
    check("transformer_kva NOT double-flagged in open-ended findings",
          not mapped_in_findings,
          f"findings={[f['item_key'] for f in findings]}")

    # (c2) the UN-mapped Recloser rating IS cross-checked (conflict 630A/400A).
    recloser = [f for f in findings if "recloser" in f["item_key"]]
    check("unmapped Recloser conflict flagged", len(recloser) == 1,
          f"got {len(recloser)}: {[f['item_key'] for f in findings]}")
    if recloser:
        locs = recloser[0].get("locations") or []
        check("Recloser finding has 2 locations", len(locs) == 2, f"got {len(locs)}")


def test_select_diagram_pages_cap() -> None:
    print("select_diagram_pages — selection + cap:")
    pages = [
        _Page(1, "G-001", "COVER SHEET"),
        _Page(2, "E-100", "ELECTRICAL ONE LINE DIAGRAM"),
        _Page(3, "E-101", "THREE LINE DIAGRAM"),
        _Page(4, "C-101", "ARRAY LAYOUT PLAN"),
        _Page(5, "C-102", "PILE LAYOUT"),
        _Page(6, "E-300", "CONDUCTOR SCHEDULE"),
        _Page(7, "E-500", "ELECTRICAL DETAILS"),
        _Page(8, "X-999", "RANDOM NON DIAGRAM"),
    ]
    selected = select_diagram_pages(pages)
    nums = {p.number for p in selected}
    check("cover/random NOT selected", 1 not in nums and 8 not in nums,
          f"got {nums}")
    check("one-line E-100 selected", 2 in nums, f"got {nums}")
    check("schedule E-300 selected", 6 in nums, f"got {nums}")
    check("detail E-500 selected", 7 in nums, f"got {nums}")

    # Cap: build 15 one-line-ish sheets, expect <= 10 selected.
    many = [_Page(i, f"E-10{i}", "SINGLE LINE DIAGRAM") for i in range(15)]
    sel = select_diagram_pages(many)
    check("selection capped at <= 10", len(sel) <= 10, f"got {len(sel)}")


def test_defensive_on_bad_ai() -> None:
    print("extract_diagram_annotations — defensive on AI/parse failure:")

    def _bad_ai(image, prompt):
        raise RuntimeError("simulated API failure")

    page = _Page(2, "E-100", "ONE LINE DIAGRAM")
    out = diagram_consistency.extract_diagram_annotations(
        page, render=_stub_render, ai_call=_bad_ai,
    )
    check("AI failure -> [] (no crash)", out == [], f"got {out}")

    def _garbage_ai(image, prompt):
        return "this is not json at all {{{"

    out2 = diagram_consistency.extract_diagram_annotations(
        page, render=_stub_render, ai_call=_garbage_ai,
    )
    check("garbage AI -> [] (no crash)", out2 == [], f"got {out2}")


def main() -> int:
    test_label_field_mapper()
    test_numeric_conflict()
    test_equivalent_no_finding()
    test_single_page_no_finding()
    test_garbage_label_ignored()
    test_metadata_label_ignored()
    test_multi_instance_label_ignored()
    test_mapped_label_routed_to_provenance()
    test_select_diagram_pages_cap()
    test_defensive_on_bad_ai()

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
