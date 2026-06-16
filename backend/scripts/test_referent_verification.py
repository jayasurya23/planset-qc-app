"""Unit tests for SAME-REFERENT VERIFICATION of cross-sheet conflicts.

Runs WITHOUT any real AI call: ``vision_call`` and ``render_page`` are stubbed
so the whole verify -> decide pipeline is exercised deterministically.

Covers:
  * verify_same_referent parses a model verdict and is DEFENSIVE on error
    (returns the UNKNOWN verdict, never raises).
  * the DECISION RULES (apply_referent_verdict + the cost-cap loop the analyzer
    emission path uses):
      - different referents (False, conf 0.9)      -> SUPPRESSED (not emitted).
      - same referent (True)                       -> kept with original Fail.
      - unknown/error (None)                       -> kept, DOWNGRADED to
                                                      Needs Review + note.
      - low-confidence different (False, conf 0.5) -> kept, downgraded (NOT
                                                      suppressed).
      - cost cap: > _MAX_REFERENT_CHECKS conflicts -> overflow are Needs
                                                      Review, none silently
                                                      dropped.

Usage (from backend/):
    python scripts/test_referent_verification.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import consistency
from app.consistency import (
    verify_same_referent,
    apply_referent_verdict,
    _INCONCLUSIVE_REFERENT_NOTE,
    _REFERENT_DROP_CONFIDENCE,
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


# ── Stubs ────────────────────────────────────────────────────────────────────

def _stub_render(page_number):
    """Pretend to render a page -> non-empty bytes (so an image is available)."""
    return f"img-page-{page_number}".encode("utf-8")


def _make_vision(verdict_obj=None, *, raise_exc=False, raw=None):
    """Build a ``vision_call(images, prompt)`` that returns the JSON for
    *verdict_obj* (or a raw string, or raises)."""
    def _call(images, prompt):
        if raise_exc:
            raise RuntimeError("simulated vision API failure")
        if raw is not None:
            return raw
        return json.dumps(verdict_obj)
    return _call


def _two_locations():
    """Two distinct conflicting locations with preview paths absent (so the
    injected render_page is used)."""
    return [
        {"page_number": 7, "sheet_number": "C-101", "value": "10'",
         "source_label": "C-101 drawing", "page_preview_path": None,
         "snippet_path": None, "bbox": None},
        {"page_number": 8, "sheet_number": "C-102", "value": "8'",
         "source_label": "C-102 drawing", "page_preview_path": None,
         "snippet_path": None, "bbox": None},
    ]


def _fail_finding():
    """A candidate conflict finding in its default (pre-verification) Fail
    state — mirrors what _build_finding produces for a clear numeric conflict."""
    return {
        "category": "Cross-Sheet Consistency",
        "item_key": "consistency_pile_spacing",
        "title": "Cross-sheet mismatch: Pile Spacing",
        "description": "The value for Pile Spacing disagrees across planset sheets.",
        "severity": "high",
        "status": "Fail",
        "auto_status": "Fail",
        "confidence": 0.9,
        "evidence": "Conflicting Pile Spacing values.",
        "locations": _two_locations(),
    }


# ── verify_same_referent: parsing + defensiveness ────────────────────────────

def test_verify_parses_verdicts() -> None:
    print("verify_same_referent — parses model verdicts:")
    locs = _two_locations()

    v_same = verify_same_referent(
        "Pile Spacing", locs,
        render_page=_stub_render,
        vision_call=_make_vision({
            "same_referent": True, "confidence": 0.92,
            "reason": "both are pile-to-pile spacing within a row",
            "what_each_refers_to": {"C-101": "pile spacing", "C-102": "pile spacing"},
        }),
    )
    check("same -> same_referent True", v_same["same_referent"] is True, f"got {v_same}")
    check("same -> confidence 0.92", abs(v_same["confidence"] - 0.92) < 1e-9, f"got {v_same}")
    check("same -> what_each_refers_to carried",
          isinstance(v_same.get("what_each_refers_to"), dict)
          and "C-101" in v_same["what_each_refers_to"], f"got {v_same}")

    v_diff = verify_same_referent(
        "Pile Spacing", locs,
        render_page=_stub_render,
        vision_call=_make_vision({
            "same_referent": False, "confidence": 0.88,
            "reason": "one is pile spacing, the other is row-to-row pitch",
        }),
    )
    check("different -> same_referent False", v_diff["same_referent"] is False, f"got {v_diff}")
    check("different -> confidence 0.88", abs(v_diff["confidence"] - 0.88) < 1e-9, f"got {v_diff}")


def test_verify_defensive() -> None:
    print("verify_same_referent — defensive (UNKNOWN on any failure):")
    locs = _two_locations()

    v_exc = verify_same_referent(
        "Pile Spacing", locs,
        render_page=_stub_render, vision_call=_make_vision(raise_exc=True),
    )
    check("vision raises -> same_referent None", v_exc["same_referent"] is None, f"got {v_exc}")
    check("vision raises -> confidence 0.0", v_exc["confidence"] == 0.0, f"got {v_exc}")
    check("vision raises -> reason 'verification unavailable'",
          v_exc["reason"] == "verification unavailable", f"got {v_exc}")

    v_garbage = verify_same_referent(
        "Pile Spacing", locs,
        render_page=_stub_render, vision_call=_make_vision(raw="not json at all {{{"),
    )
    check("garbage JSON -> same_referent None", v_garbage["same_referent"] is None, f"got {v_garbage}")

    v_no_call = verify_same_referent("Pile Spacing", locs, render_page=_stub_render, vision_call=None)
    check("no vision_call -> UNKNOWN", v_no_call["same_referent"] is None, f"got {v_no_call}")

    # Only one distinct location -> nothing to compare -> UNKNOWN.
    one = [_two_locations()[0]]
    v_one = verify_same_referent(
        "Pile Spacing", one, render_page=_stub_render,
        vision_call=_make_vision({"same_referent": False, "confidence": 0.9}),
    )
    check("single location -> UNKNOWN (no compare)", v_one["same_referent"] is None, f"got {v_one}")

    # render fails for both AND no preview path -> < 2 images -> UNKNOWN.
    def _bad_render(pn):
        raise RuntimeError("render boom")

    v_norender = verify_same_referent(
        "Pile Spacing", locs, render_page=_bad_render,
        vision_call=_make_vision({"same_referent": False, "confidence": 0.9}),
    )
    check("render fails -> UNKNOWN (no images)", v_norender["same_referent"] is None, f"got {v_norender}")

    # NEVER fabricates a 'different' verdict on error.
    check("error never yields 'different'",
          all(v["same_referent"] is not False
              for v in (v_exc, v_garbage, v_no_call, v_one, v_norender)))


# ── apply_referent_verdict: the DECISION RULES ───────────────────────────────

def test_different_referents_suppressed() -> None:
    print("decision — different referents (False, 0.9) -> SUPPRESSED:")
    finding = _fail_finding()
    keep = apply_referent_verdict(finding, {
        "same_referent": False, "confidence": 0.9,
        "reason": "pile spacing vs row pitch",
    })
    check("not emitted (suppressed)", keep is False, f"keep={keep}")


def test_same_referent_kept_as_fail() -> None:
    print("decision — same referent (True) -> kept with original Fail:")
    finding = _fail_finding()
    keep = apply_referent_verdict(finding, {
        "same_referent": True, "confidence": 0.95,
        "reason": "both are the same pile spacing",
        "what_each_refers_to": {"C-101": "pile spacing", "C-102": "pile spacing"},
    })
    check("emitted (kept)", keep is True, f"keep={keep}")
    check("status stays Fail", finding["status"] == "Fail", f"got {finding['status']}")
    check("auto_status stays Fail", finding["auto_status"] == "Fail", f"got {finding['auto_status']}")
    check("severity stays high", finding["severity"] == "high", f"got {finding['severity']}")
    check("what_each_refers_to appended to description",
          "Same-referent confirmed" in finding["description"], finding["description"])
    check("inconclusive note NOT added on confident SAME",
          _INCONCLUSIVE_REFERENT_NOTE not in finding["description"])


def test_unknown_downgraded() -> None:
    print("decision — unknown/error (None) -> kept, downgraded to Needs Review:")
    finding = _fail_finding()
    keep = apply_referent_verdict(finding, {
        "same_referent": None, "confidence": 0.0, "reason": "verification unavailable",
    })
    check("emitted (kept)", keep is True, f"keep={keep}")
    check("status downgraded to Needs Review",
          finding["status"] == "Needs Review", f"got {finding['status']}")
    check("auto_status downgraded to Needs Review",
          finding["auto_status"] == "Needs Review", f"got {finding['auto_status']}")
    check("inconclusive note appended to description",
          _INCONCLUSIVE_REFERENT_NOTE in finding["description"], finding["description"])
    check("inconclusive note appended to evidence",
          _INCONCLUSIVE_REFERENT_NOTE in finding["evidence"], finding["evidence"])


def test_low_confidence_different_downgraded_not_suppressed() -> None:
    print("decision — low-conf different (False, 0.5) -> kept + downgraded:")
    finding = _fail_finding()
    keep = apply_referent_verdict(finding, {
        "same_referent": False, "confidence": 0.5,
        "reason": "weak lean toward different",
    })
    check("NOT suppressed (kept)", keep is True, f"keep={keep}")
    check("status downgraded to Needs Review (not Fail)",
          finding["status"] == "Needs Review", f"got {finding['status']}")
    check("inconclusive note appended",
          _INCONCLUSIVE_REFERENT_NOTE in finding["description"], finding["description"])

    # Boundary: exactly at the drop threshold IS suppressed.
    boundary = _fail_finding()
    keep_boundary = apply_referent_verdict(boundary, {
        "same_referent": False, "confidence": _REFERENT_DROP_CONFIDENCE,
        "reason": "at threshold",
    })
    check("at threshold (0.75) -> suppressed", keep_boundary is False, f"keep={keep_boundary}")

    # Just below threshold is kept + downgraded.
    below = _fail_finding()
    keep_below = apply_referent_verdict(below, {
        "same_referent": False, "confidence": _REFERENT_DROP_CONFIDENCE - 0.01,
        "reason": "just below threshold",
    })
    check("just below threshold -> kept + Needs Review",
          keep_below is True and below["status"] == "Needs Review",
          f"keep={keep_below} status={below['status']}")


# ── Cost cap: mirrors the analyzer emission loop's cap behavior ──────────────

def _simulate_emission(findings, verdict_for, *, max_checks, vision_call, render_page):
    """Reproduce the analyzer emission path's verify -> decide + cost-cap loop
    using the REAL consistency helpers. Returns the list of EMITTED findings
    (suppressed ones excluded) and the number of vision calls made."""
    emitted = []
    calls = 0
    for finding in findings:
        locations = finding.get("locations") or []
        field_label = finding.get("title") or finding.get("item_key") or "value"
        if calls >= max_checks:
            consistency._downgrade_finding_to_needs_review(finding)
        else:
            calls += 1
            verdict = consistency.verify_same_referent(
                field_label, locations,
                render_page=render_page, vision_call=vision_call,
            )
            keep = consistency.apply_referent_verdict(finding, verdict)
            if not keep:
                continue  # suppressed
        emitted.append(finding)
    return emitted, calls


def test_cost_cap() -> None:
    print("cost cap — overflow conflicts are Needs Review, none silently dropped:")
    from app.diagram_consistency import run_diagram_consistency  # noqa: F401 (import sanity)

    # Read the cap the analyzer uses. It's a local constant in analyzer.py; the
    # contract is _MAX_REFERENT_CHECKS = 15, so build cap+5 conflicts.
    max_checks = 15
    n = max_checks + 5

    # Every verdict says SAME (True) so NONE would be suppressed — this isolates
    # the cap behavior: the first `max_checks` are verified (stay Fail), the
    # overflow are emitted UNVERIFIED as Needs Review.
    findings = []
    for i in range(n):
        f = _fail_finding()
        f["item_key"] = f"consistency_field_{i}"
        f["title"] = f"Cross-sheet mismatch: Field {i}"
        findings.append(f)

    emitted, calls = _simulate_emission(
        findings, None, max_checks=max_checks,
        vision_call=_make_vision({"same_referent": True, "confidence": 0.95,
                                  "reason": "same"}),
        render_page=_stub_render,
    )

    check("vision calls capped at max_checks", calls == max_checks, f"got {calls}")
    check("NONE silently dropped (all emitted)", len(emitted) == n, f"got {len(emitted)}")

    verified = [f for f in emitted if f["status"] == "Fail"]
    overflow = [f for f in emitted if f["status"] == "Needs Review"]
    check("first max_checks stay Fail (verified SAME)",
          len(verified) == max_checks, f"got {len(verified)}")
    check("overflow are Needs Review (unverified)",
          len(overflow) == n - max_checks, f"got {len(overflow)}")
    check("every overflow carries the inconclusive note",
          all(_INCONCLUSIVE_REFERENT_NOTE in f["description"] for f in overflow))


def test_cost_cap_with_suppression() -> None:
    print("cost cap + suppression — within-cap different-things are dropped:")
    max_checks = 15
    n = max_checks + 3
    findings = []
    for i in range(n):
        f = _fail_finding()
        f["item_key"] = f"consistency_field_{i}"
        findings.append(f)

    # Within the cap the model says DIFFERENT (confidently) -> suppressed.
    emitted, calls = _simulate_emission(
        findings, None, max_checks=max_checks,
        vision_call=_make_vision({"same_referent": False, "confidence": 0.9,
                                  "reason": "different quantities"}),
        render_page=_stub_render,
    )
    check("vision calls capped at max_checks", calls == max_checks, f"got {calls}")
    # First max_checks verified & dropped; the overflow (n-max_checks) emitted NR.
    check("within-cap confident-different all suppressed; overflow kept as NR",
          len(emitted) == n - max_checks
          and all(f["status"] == "Needs Review" for f in emitted),
          f"emitted={len(emitted)} statuses={[f['status'] for f in emitted]}")


def main() -> int:
    test_verify_parses_verdicts()
    test_verify_defensive()
    test_different_referents_suppressed()
    test_same_referent_kept_as_fail()
    test_unknown_downgraded()
    test_low_confidence_different_downgraded_not_suppressed()
    test_cost_cap()
    test_cost_cap_with_suppression()
    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
