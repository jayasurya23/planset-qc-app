"""Whole pages render at the measured-best zoom; regions render bigger.

This got shipped wrong once. An "image budget" was derived from OpenAI's
documented detail="high" preprocessing -- fit 2048, short side 768 -- and every
page render dropped to 1152 px. Those are gpt-4-vision's rules; the pipeline
runs gpt-5.4-mini, and it starved the model badly enough that Highland N1's
illegibility admissions went from 5 to 17 between runs.

So the zoom is now scored on the task. Ask the model to transcribe every
legible string on a sheet and check the answers against the PDF's own text
layer, which is free ground truth on a vector drawing. Two plansets, the three
sheets in each with the most text at or below 8 pt, seven zooms, three trials
-- 126 calls:

    zoom   pixels        KB   recall_small   precision
    0.75   1944x1296    331   0.424          0.871
    1.00   2592x1728    475   0.455          0.908
    1.25   3240x2160    648   0.369          0.859
    1.50   3888x2592    819   0.339          0.873
    2.00   5184x3456   1125   0.341          0.866
    2.50   6480x4320   1507   0.228          0.905
    3.00   7776x5184   1875   0.232          0.880

Sheets differ wildly in difficulty, so the pooled deviations are large and the
comparison that matters is PAIRED -- each zoom against 2.0 on the same sheet:

    1.00 vs 2.00   +0.114 mean recall, better on 6 of 6 sheets
    0.75 vs 2.00   +0.083 mean recall, better on 6 of 6 sheets
    2.50 vs 2.00   -0.112 mean recall, better on 0 of 6 sheets

More pixels stop helping around one-per-point and then hurt. Precision moves
the same way, so this is not recall bought with invention.

Recall on small text tops out near 0.46 even at the best zoom: the model reads
less than half the fine print on a sheet however it is rendered. That is the
case FOR the region re-read, which renders a crop far larger than the sheet it
came from.

Run: PYTHONPATH=backend python backend/scripts/test_vision_sampling.py
"""
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fitz  # noqa: E402
from PIL import Image  # noqa: E402

from app.gemini_analyzer import (  # noqa: E402
    _ai_bbox_of,
    _apply_region_rereads,
    _text_anchored,
    _gemini_multi_page_check,
    _gemini_page_check,
    _ILLEGIBILITY_RE,
    _reread_candidate,
    _region_for_finding,
    _region_reread,
    LEGACY_VISION_ZOOM,
    MAX_REGION_REREADS_PER_PAGE,
    PAGE_VISION_ZOOM,
    REGION_MAX_ZOOM,
    REGION_MIN_PT,
    REGION_TARGET_LONG_PX,
    region_render_rect,
    render_page_to_bytes,
    render_region_to_bytes,
)

_FAILS: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _FAILS.append(name)


TEXT_PT = 3 / 32 * 72          # smallest meaningful CAD text, 6.75 pt
SHEET = (2592.0, 1728.0)       # every page in the corpus


def sheet_doc():
    doc = fitz.open()
    page = doc.new_page(width=1728, height=2592)
    page.insert_text((300, 900), "3-1/C 500 kcmil AL + #2 AWG CU EGC",
                     fontsize=TEXT_PT, rotate=270)
    page.insert_text((300, 1400), "1200 A OCPD", fontsize=TEXT_PT, rotate=270)
    page.insert_text((600, 900), "XFMR-1 3750 kVA", fontsize=18, rotate=270)
    page.set_rotation(270)
    return doc, page


def size_of(png: bytes):
    return Image.open(io.BytesIO(png)).size


# ── whole pages ──────────────────────────────────────────────────────────
print("A whole page renders at the measured-best zoom:")
doc, page = sheet_doc()
default = render_page_to_bytes(doc, 1)
check(f"default zoom is {PAGE_VISION_ZOOM} -- one pixel per PDF point",
      size_of(default) == (int(SHEET[0]), int(SHEET[1])))
check("an explicit zoom is still honoured",
      size_of(render_page_to_bytes(doc, 1, zoom=LEGACY_VISION_ZOOM)) == (5184, 3456))
check("the cache does not confuse two zooms",
      size_of(render_page_to_bytes(doc, 1)) == (int(SHEET[0]), int(SHEET[1])))
# The regression this file exists to prevent. 0.444 was shipped once, on a
# theory rather than a measurement, and cost ~25% of the model's reading.
check("never starved to the old gpt-4-vision 'budget' of 1152 px",
      size_of(default)[0] > 1200)
check("and never inflated past where recall starts falling",
      size_of(default)[0] <= 3300)

# ── the region rect ──────────────────────────────────────────────────────
print("A region is padded, floored and kept on the page:")
tiny = fitz.Rect(1000, 800, 1059, 809)          # a bare conductor callout
r = region_render_rect(page, tiny)
check("padded beyond the bare hit", r.width > tiny.width and r.height > tiny.height)
check(f"floored to at least {REGION_MIN_PT[0]}x{REGION_MIN_PT[1]} pt",
      r.width >= REGION_MIN_PT[0] - 0.01 and r.height >= REGION_MIN_PT[1] - 0.01)
check("inside the page", page.rect.contains(r))

corner = fitz.Rect(page.rect.x1 - 12, page.rect.y1 - 8, page.rect.x1 - 2, page.rect.y1 - 2)
rc = region_render_rect(page, corner)
check("a hit in the very corner keeps its full size, shifted not trimmed",
      rc.width >= REGION_MIN_PT[0] - 0.01 and rc.height >= REGION_MIN_PT[1] - 0.01)
check("  and is still inside the page", page.rect.contains(rc))
check("a region larger than the page clamps to it",
      page.rect.contains(region_render_rect(
          page, fitz.Rect(0, 0, page.rect.x1 + 500, page.rect.y1 + 500))))

# ── the region render ────────────────────────────────────────────────────
print("A region re-read is bigger than the sheet pass, which is the whole point:")
png, region, zoom = render_region_to_bytes(doc, 1, tiny)
check(f"zoom {zoom:.1f} exceeds the page zoom {PAGE_VISION_ZOOM}",
      zoom > PAGE_VISION_ZOOM)
check(f"3/32\" text {TEXT_PT * PAGE_VISION_ZOOM:.0f} px on the sheet "
      f"-> {TEXT_PT * zoom:.0f} px on the region",
      TEXT_PT * zoom > TEXT_PT * PAGE_VISION_ZOOM * 3)
check(f"capped at {REGION_MAX_ZOOM}", zoom <= REGION_MAX_ZOOM)
check("and it costs no more than a couple of whole-sheet images",
      len(png) < len(default) * 2)

# A region big enough that the target would shrink it must not be shrunk:
# a re-read that renders smaller than the pass it is correcting is worthless.
wide = fitz.Rect(0, 0, page.rect.x1, page.rect.y1 * 0.9)
_, _, z_wide = render_region_to_bytes(doc, 1, wide)
check(f"a near-full-page region never renders below the page zoom ({z_wide:.2f})",
      z_wide >= PAGE_VISION_ZOOM)

pin = fitz.Rect(900, 900, 902, 902)             # pathologically small
_, _, z_small = render_region_to_bytes(doc, 1, pin)
check(f"a pinpoint hit is still capped ({z_small:.1f})", z_small <= REGION_MAX_ZOOM)
check(f"the target long side is a plain pixel count ({REGION_TARGET_LONG_PX:.0f})",
      REGION_TARGET_LONG_PX > 0)
doc.close()

# ── the re-read trigger ──────────────────────────────────────────────────
print("Only an admission of illegibility triggers a re-read:")
TRIGGERS = [
    ("The dimension text is not legible at this resolution", True),
    ("Value present but too small to read", True),
    ("Text is illegible in the provided image", True),
    ("unable to read the conductor callout", True),
    ("could not be read from the drawing", True),
    # "not shown" is an absence CLAIM, not an admission of blindness.
    ("The EGC size is not shown on this sheet", False),
    ("Legible and correct per NEC 250.122", False),
    ("the schedule is readable and complete", False),
    ("", False),
]
for text, want in TRIGGERS:
    check(f"{'fires' if want else 'quiet'} on {text[:44]!r}",
          bool(_ILLEGIBILITY_RE.search(text)) is want)

# ── placing the region ───────────────────────────────────────────────────
print("The re-read only fires when it can place the region:")
doc, page = sheet_doc()
located = _region_for_finding(page, {"location_text": "1200 A OCPD"})
check("a location hint that hits the text layer places it", located is not None)
check("  and it lands on the page", located is not None and page.rect.contains(located))
check("a model bbox places it when no hint matches",
      _region_for_finding(page, {"location_bbox_norm": [200, 300, 260, 420]}) is not None)
check("no hint, no bbox -> no re-read",
      _region_for_finding(page, {"location_text": "ZZZ NOT ON THIS DRAWING"}) is None)

# ── the re-read call ─────────────────────────────────────────────────────
print("The verdict is only taken when it is usable:")
import app.gemini_client as _client  # noqa: E402

_calls = []


def _stub(reply):
    def _fn(image_bytes, prompt, mime_type="image/png", deep=False):
        _calls.append({"bytes": len(image_bytes), "prompt": prompt, "deep": deep})
        return reply
    return _fn


_real = _client.analyze_page_image
FINDING = {"location_text": "1200 A OCPD"}

_client.analyze_page_image = _stub(
    '{"readable": true, "status": "Pass", "value": "1200 A OCPD",'
    ' "evidence": "the callout reads 1200 A OCPD"}')
v = _region_reread(doc, 1, FINDING, "EGC sizing", "Needs Review", "not legible")
check("a clean answer comes back parsed", v is not None and v["status"] == "Pass")
check("  it reports what it read", v is not None and v["value"] == "1200 A OCPD")
check("  and the region it read", v is not None and page.rect.contains(v["region"]))
check("it used the deep model", bool(_calls) and _calls[-1]["deep"] is True)
check("the prompt tells the model how much bigger this is",
      bool(_calls) and "larger" in _calls[-1]["prompt"])

_client.analyze_page_image = _stub("this is not json at all")
check("garbage -> no verdict, finding untouched",
      _region_reread(doc, 1, FINDING, "c", "Fail", "illegible") is None)

_client.analyze_page_image = _stub('{"readable": true, "status": "Maybe"}')
check("an invented status -> no verdict",
      _region_reread(doc, 1, FINDING, "c", "Fail", "illegible") is None)

_client.analyze_page_image = _stub(
    '{"readable": false, "status": "Needs Review", "evidence": "still cannot tell"}')
still = _region_reread(doc, 1, FINDING, "c", "Fail", "illegible")
check("still-unreadable comes back readable=false so the caller skips it",
      still is not None and still["readable"] is False)


def _boom(*a, **k):
    raise RuntimeError("provider down")


_client.analyze_page_image = _boom
check("a provider failure is swallowed, not raised",
      _region_reread(doc, 1, FINDING, "c", "Fail", "illegible") is None)

_client.analyze_page_image = _real
doc.close()

check(f"extra calls per page are bounded ({MAX_REGION_REREADS_PER_PAGE})",
      isinstance(MAX_REGION_REREADS_PER_PAGE, int)
      and 1 <= MAX_REGION_REREADS_PER_PAGE <= 8)

# ── both check paths must actually use it ────────────────────────────────
print("Every path that produces findings gets the re-read:")
# This is the test that was missing. The re-read lived inside the single-page
# check, so multi-page findings never got a second look -- and on one
# production run every illegibility admission happened to be multi-page, so
# the feature fired zero times while appearing to work.
import inspect  # noqa: E402

for fn in (_gemini_page_check, _gemini_multi_page_check):
    src = inspect.getsource(fn)
    check(f"{fn.__name__} decides candidates with the shared predicate",
          "_reread_candidate(" in src)
    check(f"{fn.__name__} runs the shared post-pass", "_apply_region_rereads(" in src)
    check(f"{fn.__name__} records which page to re-read", '"page":' in src)

print("The cap is enforced by the predicate, not by each caller:")
full = [{}] * MAX_REGION_REREADS_PER_PAGE
check("a full queue takes no more", not _reread_candidate("Fail", "not legible", full))
check("an empty queue takes one", _reread_candidate("Fail", "not legible", []))
check("a Pass is never re-read", not _reread_candidate("Pass", "not legible", []))
check("an absence claim is never re-read",
      not _reread_candidate("Fail", "the EGC size is not shown", []))

print("Applying a verdict rewrites status, confidence and evidence together:")
doc, page = sheet_doc()
issues = [{"id": "i1", "item_key": "ai_x", "status": "Needs Review",
           "auto_status": "Needs Review", "confidence": 0.41,
           "evidence": "the callout is not legible", "snippet_path": None,
           "page_preview_path": None, "bbox": None}]
pending = [{"issue_idx": 0, "page": 1, "finding": {"location_text": "1200 A OCPD"},
            "check": "EGC sizing", "status": "Needs Review",
            "evidence": "the callout is not legible"}]

_client.analyze_page_image = _stub(
    '{"readable": true, "status": "Pass", "value": "1200 A OCPD",'
    ' "evidence": "the callout reads 1200 A OCPD"}')
_apply_region_rereads(doc, Path("/tmp"), issues, pending)
got = issues[0]
check(f"status resolved Needs Review -> {got['status']}", got["status"] == "Pass")
check("auto_status moves with it", got["auto_status"] == "Pass")
check(f"confidence rose from 0.41 to {got['confidence']}", got["confidence"] > 0.41)
check("evidence records the magnification", "region re-read at" in got["evidence"])
check("evidence records what was read", "1200 A OCPD" in got["evidence"])
check("the original evidence is kept, not replaced",
      "not legible" in got["evidence"])

print("An unusable verdict leaves the finding exactly as it was:")
issues2 = [dict(issues[0], status="Needs Review", confidence=0.41,
                evidence="the callout is not legible")]
_client.analyze_page_image = _stub('{"readable": false, "status": "Needs Review"}')
_apply_region_rereads(doc, Path("/tmp"), issues2, [dict(pending[0])])
check("status untouched", issues2[0]["status"] == "Needs Review")
check("confidence untouched", issues2[0]["confidence"] == 0.41)
check("evidence untouched", issues2[0]["evidence"] == "the callout is not legible")

_client.analyze_page_image = _real
doc.close()


# ── what "corroborated" is allowed to mean ───────────────────────────────
print("Corroboration requires a real quote, not a near miss:")
doc, page = sheet_doc()
CASES = [
    (["1200 A OCPD"], True, "the exact string on the page"),
    (["1200 A OCPD,"], True, "trailing punctuation, absorbed by the fuzzy variants"),
    (["3-1/C 500 kcmil AL"], True, "another real callout"),
    # The trap. _search_page_multi falls back to single tokens so a highlight
    # still lands somewhere useful; "conductor" alone matching is fine for
    # drawing a box and worthless as evidence the model read anything.
    (["the conductor looks undersized"], False, "a paraphrase sharing one word"),
    (["the drawing appears incomplete"], False, "pure prose"),
    (["ZZZQQQ NOT ON THIS SHEET"], False, "absent text"),
    ([], False, "no hints at all"),
]
for hints, want, why in CASES:
    check(f"{'anchored' if want else 'not anchored'}: {why}",
          _text_anchored(doc, 1, hints) is want)

print("A model bbox is taken only when it parses:")
check("a sane normalised box is accepted",
      _ai_bbox_of({"location_bbox_norm": [200, 300, 260, 420]}, doc, 1) is not None)
check("nothing supplied -> None", _ai_bbox_of({}, doc, 1) is None)
check("garbage -> None", _ai_bbox_of({"location_bbox_norm": "banana"}, doc, 1) is None)

print("The highlight path keeps its fallback, because it has a different job:")
from app.analyzer import _search_page_multi  # noqa: E402
# The fallback tries adjacent word PAIRS before single tokens and caps at
# four, so it reaches this phrase through "500 kcmil" while the strict pass,
# which only accepts the whole needle and its fuzzy variants, does not.
PARAPHRASE = ["500 kcmil AL feeder is undersized"]
check("a paraphrase still places a highlight",
      bool(_search_page_multi(page, PARAPHRASE)))
check("but does not count as corroboration",
      not _search_page_multi(page, PARAPHRASE, token_fallback=False))
check("and the strict pass is never wider than the default",
      len(_search_page_multi(page, PARAPHRASE, token_fallback=False))
      <= len(_search_page_multi(page, PARAPHRASE)))
doc.close()


print()
if _FAILS:
    print(f"FAILED ({len(_FAILS)}): {_FAILS}")
    sys.exit(1)
print("ALL VISION-SAMPLING CHECKS PASSED")
