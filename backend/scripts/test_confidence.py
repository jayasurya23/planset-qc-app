"""Confidence has to mean something, because the UI sorts on it.

Every finding the vision path produced carried the constant 0.72 -- 6,043 of
the 8,229 findings in the production corpus, including 576 of the 634 Fails.
The UI shows it on every row AND offers "sort by confidence", so a reviewer's
triage control sorted three-quarters of their queue into one arbitrary tie.

It was backwards, too: the deterministic checks in analyzer.py carry considered
per-check values (0.96 for "found sheet numbers on all pages", 0.55 for "title
parsing is inherently fragile"), so the checks least able to be wrong varied and
the ones most able to be wrong were identical.

The weights in app/confidence.py are judgements, not measurements -- there is
no labelled ground truth to fit them to yet. So these tests pin the ORDERING
and the invariants, which is what the sort control actually consumes, and
deliberately do not assert exact values.

Run: PYTHONPATH=backend python backend/scripts/test_confidence.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import confidence as C  # noqa: E402

_FAILS: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _FAILS.append(name)


def s(**kw) -> float:
    return C.score_ai_finding(**kw)


# ── the evidence classifiers ─────────────────────────────────────────────
print("An absence CLAIM is not the same as an admission of blindness:")
ABSENCE = [
    "The working clearance is not shown on this sheet",
    "Conductor size not provided for this circuit",
    "The grounding electrode conductor is missing from the schedule",
    "No such detail found on the drawing",
    "Unable to locate the transformer nameplate",
]
ILLEGIBLE = [
    "The dimension text is not legible at this resolution",
    "Too small to read",
    "The callout is illegible",
]
NEITHER = [
    "Conductor reads 500 kcmil AL, compliant with NEC 310.16",
    "The schedule is readable and complete",
]
for t in ABSENCE:
    check(f"absence: {t[:46]!r}", C.is_absence_claim(t) and not C.admits_illegible(t))
for t in ILLEGIBLE:
    check(f"illegible: {t[:44]!r}", C.admits_illegible(t))
for t in NEITHER:
    check(f"neither: {t[:46]!r}",
          not C.is_absence_claim(t) and not C.admits_illegible(t))
check("empty evidence is neither",
      not C.is_absence_claim("") and not C.admits_illegible(None))

# ── the ordering the sort control depends on ─────────────────────────────
print("Corroboration ranks above a bare pointer, which ranks above nothing:")
anchored = s(text_anchored=True, evidence="reads 500 kcmil AL")
bbox_only = s(model_bbox=True, evidence="conductor appears undersized")
unlocated = s(evidence="the schedule seems incomplete")
check(f"text-anchored {anchored} > model bbox {bbox_only}", anchored > bbox_only)
check(f"model bbox {bbox_only} > unlocated {unlocated}", bbox_only > unlocated)

print("An unanchored absence claim is discounted:")
plain = s(model_bbox=True, evidence="the conductor is 500 kcmil AL")
absent = s(model_bbox=True, evidence="the conductor size is not shown")
check(f"absence {absent} < ordinary {plain}", absent < plain)

print("...but an ANCHORED absence claim is not:")
# The model found the label and says the value beside it is blank. That is a
# real, checkable observation, not the class the audit flagged.
anchored_absent = s(text_anchored=True, evidence="the EGC column is not shown for this row")
check(f"anchored absence {anchored_absent} == anchored plain {anchored}",
      anchored_absent == anchored)

print("Admitting it could not see outranks every other penalty:")
illegible = s(model_bbox=True, evidence="the dimension text is not legible")
check(f"illegible {illegible} < unanchored absence {absent}", illegible < absent)
worst = s(evidence="too small to read")
check(f"illegible AND unlocated {worst} is the floor of these", worst < illegible)

print("A magnified re-read that agreed is corroboration:")
before = s(model_bbox=True, evidence="the callout is not legible")
after = s(model_bbox=True, reread_resolved=True, text_anchored=True,
          evidence="the callout reads 1200 A OCPD")
check(f"re-read resolved {after} > the illegible finding it replaced {before}",
      after > before)

print("Citing a supporting document helps a little:")
check("cited > uncited",
      s(text_anchored=True, cites_supporting_doc=True, evidence="x")
      > s(text_anchored=True, evidence="x"))

# ── invariants ───────────────────────────────────────────────────────────
print("The number stays inside a usable band:")
best = s(text_anchored=True, reread_resolved=True, cites_supporting_doc=True,
         evidence="reads 1200 A OCPD")
check(f"best case {best} <= {C.MAX}", best <= C.MAX)
check(f"worst case {worst} >= {C.MIN}", worst >= C.MIN)
check("never claims certainty", best < 1.0)
check("never claims worthlessness", worst > 0.0)

print("It actually varies, which is the entire point:")
CASES = [
    dict(text_anchored=True, reread_resolved=True, cites_supporting_doc=True, evidence="reads X"),
    dict(text_anchored=True, evidence="reads X"),
    dict(model_bbox=True, evidence="appears undersized"),
    dict(model_bbox=True, evidence="not shown"),
    dict(evidence="seems incomplete"),
    dict(model_bbox=True, evidence="not legible"),
    dict(evidence="too small to read"),
]
spread = sorted({s(**c) for c in CASES})
check(f"{len(spread)} distinct values across realistic findings: {spread}",
      len(spread) >= 6)
check("and none of them is the old constant 0.72", 0.72 not in spread)

print("Two findings differing only in corroboration cannot tie:")
check("anchored != unanchored",
      s(text_anchored=True, evidence="e") != s(evidence="e"))

print("Explanations line up with the score:")
kw = dict(model_bbox=True, evidence="the dimension text is not legible")
why = C.explain(**kw)
check("says how it was located", any("bounding box" in r for r in why))
check("says it admitted blindness", any("could not be read" in r for r in why))
check("an unlocated anchored-free finding says so",
      any("could not be located" in r for r in C.explain(evidence="x")))

print()
if _FAILS:
    print(f"FAILED ({len(_FAILS)}): {_FAILS}")
    sys.exit(1)
print("ALL CONFIDENCE CHECKS PASSED")
