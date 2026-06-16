"""Unit tests for the cross-sheet value-consistency checker.

Runs WITHOUT any real AI call:
  * normalize_value equivalences are pure functions.
  * The numeric-conflict and non-conflict compare_provenance cases are
    chosen so the AI reconciler is never invoked (clear numeric magnitudes
    or values that collapse to the same normalized key).
  * The ambiguous path stubs reconcile_text directly.

Usage (from backend/):
    python scripts/test_consistency.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import consistency
from app.consistency import normalize_value, compare_provenance


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


def _same_key(field: str, a: str, b: str) -> bool:
    ka, _ = normalize_value(field, a)
    kb, _ = normalize_value(field, b)
    return ka == kb


def _equal(field: str, a: str, b: str) -> bool:
    """Equal if normalized keys match OR magnitudes are within tolerance —
    mirrors compare_provenance's grouping logic."""
    ka, ma = normalize_value(field, a)
    kb, mb = normalize_value(field, b)
    if ka == kb:
        return True
    return consistency._equal_keys(ka, ma, kb, mb, field)


# ── normalize_value equivalences ────────────────────────────────────────────

def test_normalize_value() -> None:
    print("normalize_value equivalences:")

    # power: kVA <-> MVA, thousands separators
    check("2250kVA == 2.25MVA", _equal("transformer_kva", "2250 kVA", "2.25 MVA"))
    check("2250kVA == 2,250kVA", _equal("transformer_kva", "2250 kVA", "2,250kVA"))
    check("2250 == 2.25 MVA", _equal("transformer_kva", "2250", "2.25 MVA"))
    check("2250kVA != 2000kVA (distinct)",
          not _equal("transformer_kva", "2250 kVA", "2000 kVA"))

    # voltage: with/without trailing V
    check("480 == 480V", _equal("transformer_secondary_voltage", "480", "480V"))
    check("12470 == 12470 V",
          _equal("transformer_primary_voltage", "12470", "12470 V"))
    check("480 != 600", not _equal("transformer_secondary_voltage", "480", "600"))
    # combined voltage kept structured
    check("480Y/277 != 480",
          not _equal("poi_voltage", "480Y/277", "480"))

    # wire size: AWG / kcmil / MCM / '#6 AWG'
    check("#6 AWG == 6 AWG", _equal("conductor_size", "#6 AWG", "6 AWG"))
    check("6AWG == 6 AWG", _equal("conductor_size", "6AWG", "6 AWG"))
    check("500 kcmil == 500 MCM", _equal("conductor_size", "500 kcmil", "500 MCM"))

    # winding synonyms
    check("Yg == Wye-grounded",
          _equal("transformer_winding_config", "Yg", "Wye-grounded"))
    check("Delta-Wye Grounded == D-Yg",
          _equal("transformer_winding_config", "Delta-Wye Grounded", "D-Yg"))
    check("Delta == D", _equal("transformer_winding_config", "Delta", "D"))

    # numeric ratios within tolerance
    check("1.30 == 1.3", _equal("dc_ac_ratio", "1.30", "1.3"))
    check("1.30 == 1.298", _equal("dc_ac_ratio", "1.30", "1.298"))
    check("1.30 != 1.45", not _equal("dc_ac_ratio", "1.30", "1.45"))

    # leading-dot decimals — GCR/tilt are commonly written ".35" / ".5"
    check(".35 == 0.35 (gcr)", _equal("gcr", ".35", "0.35"))
    check(".5 == 0.5 (gcr)", _equal("gcr", ".5", "0.5"))
    check(".35 != .45 (gcr distinct)", not _equal("gcr", ".35", ".45"))
    _, _gmag = normalize_value("gcr", ".35")
    check("magnitude parsed for .35 -> 0.35", _gmag == 0.35, f"got {_gmag}")

    # dimensions with units
    check("15 ft == 15", _equal("row_pitch", "15 ft", "15"))
    check("25 deg == 25", _equal("tilt_angle", "25 deg", "25"))
    check("23'-6\" == 23.5 ft", _equal("row_pitch", "23'-6\"", "23.5 ft"))
    check("15 ft != 18 ft", not _equal("row_pitch", "15 ft", "18 ft"))

    # magnitude is returned when parseable
    _, mag = normalize_value("transformer_kva", "2.25 MVA")
    check("magnitude parsed for 2.25 MVA -> 2250.0", mag == 2250.0, f"got {mag}")


# ── compare_provenance: clear numeric conflict ──────────────────────────────

def test_compare_conflict() -> None:
    print("compare_provenance — numeric conflict:")
    provenance = {
        "transformer_kva": [
            {"value": "2250 kVA", "source": "E-100 SLD", "page_number": 7},
            {"value": "2000 kVA", "source": "cover system-info", "page_number": 1},
        ],
    }
    findings = compare_provenance(provenance)
    check("exactly one finding emitted", len(findings) == 1,
          f"got {len(findings)}")
    if not findings:
        return
    f = findings[0]
    check("status is Fail", f["status"] == "Fail", f"got {f['status']}")
    check("severity high", f["severity"] == "high", f"got {f['severity']}")
    check("category Cross-Sheet Consistency",
          f["category"] == "Cross-Sheet Consistency", f"got {f['category']}")
    check("item_key consistency_transformer_kva",
          f["item_key"] == "consistency_transformer_kva", f"got {f['item_key']}")
    locs = f.get("locations") or []
    check("2 locations", len(locs) == 2, f"got {len(locs)}")
    if len(locs) == 2:
        pages = {l["page_number"] for l in locs}
        check("locations point at pages 7 and 1", pages == {7, 1}, f"got {pages}")
        values = {l["value"] for l in locs}
        check("location values present", values == {"2250 kVA", "2000 kVA"},
              f"got {values}")
        check("primary mirrors locations[0]",
              locs[0]["page_number"] == 7 and locs[0]["source_label"] == "E-100 SLD")
        check("bbox left None for analyzer to render",
              all(l["bbox"] is None for l in locs))


# ── compare_provenance: non-conflict (same value formatted differently) ─────

def test_compare_no_conflict() -> None:
    print("compare_provenance — non-conflict (no AI call):")

    # Sabotage the AI reconciler so the test fails loudly if it is ever
    # reached for these numeric/equivalent cases.
    def _boom(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("reconcile_text must NOT be called for equivalent numeric values")

    orig = consistency.reconcile_text
    consistency.reconcile_text = _boom
    try:
        provenance = {
            # 2250 kVA == 2.25 MVA == 2,250kVA — all the same magnitude.
            "transformer_kva": [
                {"value": "2250 kVA", "source": "E-100 SLD", "page_number": 7},
                {"value": "2.25 MVA", "source": "cover system-info", "page_number": 1},
                {"value": "2,250kVA", "source": "E-902 datasheet", "page_number": 30},
            ],
            # 480 == 480V
            "transformer_secondary_voltage": [
                {"value": "480", "source": "E-100 SLD", "page_number": 7},
                {"value": "480V", "source": "cover system-info", "page_number": 1},
            ],
            # single planset sighting — nothing to compare
            "dc_ac_ratio": [
                {"value": "1.30", "source": "cover system-info", "page_number": 1},
            ],
        }
        findings = compare_provenance(provenance)
        check("no findings for equivalent values", len(findings) == 0,
              f"got {len(findings)}: {[f['item_key'] for f in findings]}")
    finally:
        consistency.reconcile_text = orig


# ── compare_provenance: user value is authoritative, not a conflict ─────────

def test_user_value_not_conflict() -> None:
    print("compare_provenance — user-vs-planset is NOT a conflict:")
    provenance = {
        "transformer_kva": [
            {"value": "2250 kVA", "source": "E-100 SLD", "page_number": 7},
            {"value": "2000 kVA", "source": "user project_details", "page_number": None},
        ],
    }
    findings = compare_provenance(provenance)
    check("no finding when only planset-vs-user disagree", len(findings) == 0,
          f"got {len(findings)}")


# ── compare_provenance: ambiguous path uses stubbed reconcile_text ──────────

def test_ambiguous_path_stubbed() -> None:
    print("compare_provenance — ambiguous winding (stubbed AI):")
    provenance = {
        "transformer_winding_config": [
            {"value": "Delta-Wye", "source": "E-100 SLD", "page_number": 7},
            {"value": "Wye-Wye", "source": "E-902 datasheet", "page_number": 30},
        ],
    }

    orig = consistency.reconcile_text
    consistency.reconcile_text = lambda field, sightings: {
        "verdict": "conflict", "canonical_value": "Delta-Wye",
        "outlier_pages": [30], "reason": "stubbed", "confidence": 0.8,
    }
    try:
        findings = compare_provenance(provenance)
        check("one finding for ambiguous conflict", len(findings) == 1,
              f"got {len(findings)}")
        if findings:
            check("Fail at confidence >= 0.6",
                  findings[0]["status"] == "Fail", f"got {findings[0]['status']}")

        # low confidence -> Needs Review
        consistency.reconcile_text = lambda field, sightings: {
            "verdict": "conflict", "canonical_value": "Delta-Wye",
            "outlier_pages": [30], "reason": "stubbed-low", "confidence": 0.4,
        }
        findings = compare_provenance(provenance)
        check("Needs Review at confidence < 0.6",
              findings and findings[0]["status"] == "Needs Review",
              f"got {findings[0]['status'] if findings else 'none'}")

        # consistent -> no finding
        consistency.reconcile_text = lambda field, sightings: {
            "verdict": "consistent", "canonical_value": "Delta-Wye",
            "outlier_pages": [], "reason": "same", "confidence": 0.9,
        }
        findings = compare_provenance(provenance)
        check("consistent verdict -> no finding", len(findings) == 0,
              f"got {len(findings)}")
    finally:
        consistency.reconcile_text = orig


def test_gcr_leading_dot_no_false_positive() -> None:
    print("compare_provenance — gcr .35 vs 0.35 (no false positive):")
    findings = compare_provenance({
        "gcr": [
            {"value": ".35", "source": "C-101 array layout", "page_number": 3},
            {"value": "0.35", "source": "cover system-info", "page_number": 1},
        ],
    })
    check("no finding for .35 vs 0.35", len(findings) == 0, f"got {len(findings)}")


def test_sheet_number_backfill() -> None:
    print("compare_provenance — sheet_number backfilled from page_map:")

    class _P:
        def __init__(self, number, sheet_number):
            self.number = number
            self.sheet_number = sheet_number

    page_map = {"E-100": _P(7, "E-100"), "G-001": _P(1, "G-001")}
    findings = compare_provenance({
        "transformer_kva": [
            {"value": "2250 kVA", "source": "E-100 SLD", "page_number": 7},
            {"value": "2000 kVA", "source": "cover system-info", "page_number": 1},
        ],
    }, page_map)
    check("one finding", len(findings) == 1, f"got {len(findings)}")
    if findings:
        sheets = {l["page_number"]: l["sheet_number"] for l in findings[0]["locations"]}
        check("sheet_number backfilled (7->E-100, 1->G-001)",
              sheets.get(7) == "E-100" and sheets.get(1) == "G-001", f"got {sheets}")


def test_filter_cross_sheet() -> None:
    print("filter_cross_sheet_findings — false-positive gate (stubbed AI):")
    issues = [
        {"category": "Cross-Sheet Consistency", "status": "Fail", "auto_status": "Fail",
         "title": "Total DC size", "item_key": "ai_consistency_total_dc",
         "description": "d", "evidence": "Found: none"},
        {"category": "Cross-Sheet Consistency", "status": "Needs Review", "auto_status": "Needs Review",
         "title": "Transformer kVA", "item_key": "ai_consistency_xfmr",
         "description": "d", "evidence": "2250 vs 2000"},
        {"category": "Cross-Sheet Consistency", "status": "Fail", "auto_status": "Fail",
         "title": "VT ratio", "item_key": "ai_consistency_vt",
         "description": "d", "evidence": "illegible"},
        {"category": "AC Single Line Diagram", "status": "Fail", "auto_status": "Fail",
         "title": "Other", "item_key": "ai_sld_x", "description": "d", "evidence": "e"},
        {"category": "Cross-Sheet Consistency", "status": "Pass", "auto_status": "Pass",
         "title": "OK", "item_key": "ai_consistency_ok", "description": "d", "evidence": "agree"},
    ]

    def stub(finding):
        t = finding.get("title")
        if t == "Total DC size":
            return {"verdict": "false_positive", "confidence": 0.9, "reason": "no value"}
        if t == "Transformer kVA":
            return {"verdict": "genuine", "confidence": 0.9, "reason": "real conflict"}
        if t == "VT ratio":
            return {"verdict": "uncertain", "confidence": 0.3, "reason": "illegible"}
        return {"verdict": "genuine", "confidence": 0.9, "reason": "x"}

    out = consistency.filter_cross_sheet_findings(issues, verifier=stub)
    titles = {i["title"] for i in out}
    by = {i["title"]: i for i in out}
    check("confident FP dropped", "Total DC size" not in titles, f"got {titles}")
    check("genuine kept", "Transformer kVA" in titles)
    check("uncertain Fail kept but downgraded to NR",
          by.get("VT ratio", {}).get("status") == "Needs Review",
          f"got {by.get('VT ratio', {}).get('status')}")
    check("non-cross-sheet untouched (still Fail)",
          by.get("Other", {}).get("status") == "Fail")
    check("cross-sheet Pass untouched (not checked)",
          by.get("OK", {}).get("status") == "Pass")
    check("4 findings kept (1 dropped)", len(out) == 4, f"got {len(out)}")


def main() -> int:
    test_normalize_value()
    test_compare_conflict()
    test_compare_no_conflict()
    test_user_value_not_conflict()
    test_ambiguous_path_stubbed()
    test_gcr_leading_dot_no_false_positive()
    test_sheet_number_backfill()
    test_filter_cross_sheet()
    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
