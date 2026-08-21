"""How much should a reviewer trust one AI finding?

Every finding the vision path produced carried the literal constant 0.72:
6,043 of the 8,229 findings in the production corpus, including 576 of the
634 Fails. The UI shows that number on every row *and* offers "sort by
confidence" as a triage control, so three-quarters of the reviewer's queue
sorted into a single arbitrary tie.

It was worse than uninformative, it was backwards. The deterministic checks in
``analyzer.py`` carry considered per-check values — 0.96 for "found sheet
numbers on all pages", 0.55 for "title parsing is inherently fragile" — so the
checks least able to be wrong were the ones that varied, and the ones most
able to be wrong were all identical.

This module says only what can actually be known at the moment a finding is
created. It is deliberately a small, readable, additive model rather than
anything learned: there is no labelled ground truth to learn from yet (that is
Horizon C in the false-positive audit), and a transparent number a QC engineer
can argue with beats an opaque one they cannot.

The weights are judgements, not measurements. What the tests pin down is the
ORDERING, because the ordering is what the sort control actually uses.
"""
from __future__ import annotations

import re

# Starting point for a finding with nothing either way said about it.
BASE = 0.55

# The words the model quoted are present in the PDF's text layer. This is the
# strongest corroboration available without a human: these are vector PDFs, so
# a text-layer hit means the model read something that is genuinely on the
# page rather than reconstructing a plausible callout.
TEXT_ANCHORED = 0.22

# It returned a bounding box but nothing corroborates what is inside it.
MODEL_BBOX_ONLY = 0.04

# It could not point anywhere at all, even after the bbox rescue pass.
UNLOCATED = -0.12

# A magnified region re-read looked again and agreed. See
# gemini_analyzer._region_reread.
REREAD_RESOLVED = 0.12

# "X is not shown". The false-positive audit found absence claims to be the
# highest-FP class in the corpus, because absence is exactly what a model
# cannot verify from an image it could not fully resolve.
ABSENCE_CLAIM = -0.10

# The evidence says, in the model's own words, that it could not read the
# drawing. Whatever else is true, this finding is not evidence of a defect.
ILLEGIBLE = -0.18

# The finding cites an uploaded supporting document (CESIR, BOD, submittal),
# which means it was checked against something external to the drawing.
SUPPORTING_DOC = 0.06

# Never 0 and never 1. The tool is not certain and should not claim to be;
# a floor keeps a weak finding visible rather than sorting it into oblivion.
MIN = 0.10
MAX = 0.95


_ABSENCE_CLAIM_RE = re.compile(
    "(?i)(?:"
    "not\\s+shown|not\\s+provided|not\\s+indicated|not\\s+specified|"
    "not\\s+labell?ed|not\\s+present|not\\s+found|not\\s+included|"
    "no\\s+(?:such\\s+)?(?:value|dimension|callout|label|note|detail|schedule)|"
    "is\\s+missing|are\\s+missing|appears?\\s+to\\s+be\\s+missing|"
    "could\\s+not\\s+locate|unable\\s+to\\s+locate|absent\\s+from"
    ")"
)

# Kept here rather than in the analyzer so the trigger and the scoring penalty
# can never drift apart.
ILLEGIBILITY_RE = re.compile(
    "(?i)\\b(?:"
    "not\\s+legible|illegible|not\\s+readable|unreadable|"
    "cannot\\s+be\\s+read|can'?t\\s+be\\s+read|could\\s+not\\s+be\\s+read|"
    "too\\s+small\\s+to\\s+read|too\\s+small\\s+to\\s+resolve|"
    "unable\\s+to\\s+read|unable\\s+to\\s+resolve|"
    "resolution\\s+(?:is\\s+)?too\\s+low|not\\s+clearly\\s+legible"
    ")\\b"
)


def is_absence_claim(evidence: str | None) -> bool:
    return bool(evidence) and bool(_ABSENCE_CLAIM_RE.search(evidence))


def admits_illegible(evidence: str | None) -> bool:
    return bool(evidence) and bool(ILLEGIBILITY_RE.search(evidence))


def score_ai_finding(
    *,
    evidence: str | None = None,
    text_anchored: bool = False,
    model_bbox: bool = False,
    reread_resolved: bool = False,
    cites_supporting_doc: bool = False,
) -> float:
    """Confidence for one finding produced by the vision path.

    ``text_anchored``  the literal text it quoted was found in the text layer
    ``model_bbox``     it supplied a bounding box (weaker than the above)
    ``reread_resolved`` a magnified region re-read looked again and agreed
    """
    score = BASE

    if text_anchored:
        score += TEXT_ANCHORED
    elif model_bbox:
        score += MODEL_BBOX_ONLY
    else:
        score += UNLOCATED

    if reread_resolved:
        score += REREAD_RESOLVED
    if cites_supporting_doc:
        score += SUPPORTING_DOC

    if admits_illegible(evidence):
        score += ILLEGIBLE
    elif is_absence_claim(evidence) and not text_anchored:
        # An absence claim that IS anchored is a different animal: the model
        # found the label and is saying the value beside it is blank, which is
        # a real and checkable observation.
        score += ABSENCE_CLAIM

    return round(min(MAX, max(MIN, score)), 2)


def explain(**kwargs) -> list[str]:
    """The reasons behind a score, for logging and for anyone asking why."""
    reasons: list[str] = []
    if kwargs.get("text_anchored"):
        reasons.append("quoted text found in the PDF text layer")
    elif kwargs.get("model_bbox"):
        reasons.append("located only by the model's own bounding box")
    else:
        reasons.append("could not be located on the page")
    if kwargs.get("reread_resolved"):
        reasons.append("confirmed by a magnified region re-read")
    if kwargs.get("cites_supporting_doc"):
        reasons.append("cites an uploaded supporting document")
    evidence = kwargs.get("evidence")
    if admits_illegible(evidence):
        reasons.append("evidence admits the drawing could not be read")
    elif is_absence_claim(evidence) and not kwargs.get("text_anchored"):
        reasons.append("unanchored absence claim")
    return reasons
