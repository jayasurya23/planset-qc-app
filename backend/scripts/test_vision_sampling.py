"""Render to the image budget, and buy resolution where it is needed.

Every vision provider shrinks an image before the model sees it, and none
upscale. OpenAI at detail="high" fits the image into a 2048 square, then
scales so the SHORT side is 768. On this corpus -- 2592 x 1728 pt sheets --
that means the model has never seen more than 1152 x 768, whatever we sent.

The pipeline sent zoom 2.0, i.e. 5184 x 3456, on every vision call of every
check of every run: 430 KB uploaded to deliver 69 KB of information. Rendering
the vector straight to the target is also slightly sharper than rendering huge
and letting the provider's resampler discard pixels (0.74 vs 0.69 mean
|Laplacian| over the same 3/32" conductor callout), so this is not a
quality-for-cost trade.

The budget is per IMAGE, not per page, which is what makes a region re-read
worth having: a crop that is a twentieth of the sheet can be rendered an order
of magnitude larger and still arrive inside the same envelope. 3/32" text goes
from 3 px at sheet level to roughly 35 px on the region -- the difference
between the 96 findings in production whose evidence admits they could not read
the drawing, and an answer.

Run: PYTHONPATH=backend python backend/scripts/test_vision_sampling.py
"""
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fitz  # noqa: E402
from PIL import Image  # noqa: E402

from app.gemini_analyzer import (  # noqa: E402
    _ILLEGIBILITY_RE,
    _region_for_finding,
    _region_reread,
    LEGACY_VISION_ZOOM,
    MAX_REGION_REREADS_PER_PAGE,
    REGION_MAX_ZOOM,
    REGION_MIN_PT,
    region_render_rect,
    render_page_to_bytes,
    render_region_to_bytes,
)
from app.gemini_client import (  # noqa: E402
    OPENAI_MAX_BOX,
    OPENAI_SHORT_SIDE,
    vision_zoom_for_page,
)

_FAILS: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _FAILS.append(name)


TEXT_PT = 3 / 32 * 72          # smallest meaningful CAD text, 6.75 pt
SHEET = (2592.0, 1728.0)       # every page in the corpus


def openai_would_reduce_to(w, h):
    """The provider's own preprocessing, so the tests assert against the rule
    rather than against our implementation of it."""
    if max(w, h) > OPENAI_MAX_BOX:
        s = OPENAI_MAX_BOX / max(w, h)
        w, h = round(w * s), round(h * s)
    if min(w, h) > OPENAI_SHORT_SIDE:
        s = OPENAI_SHORT_SIDE / min(w, h)
        w, h = round(w * s), round(h * s)
    return w, h


def sheet_doc():
    """A corpus-geometry sheet with 3/32" text on it."""
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


# ── the budget itself ────────────────────────────────────────────────────
print("The budget lands a page exactly on what the provider will use:")
z = vision_zoom_for_page(*SHEET, provider="openai")
got = (round(SHEET[0] * z), round(SHEET[1] * z))
check(f"corpus sheet -> {got[0]}x{got[1]}", got == (1152, 768))
check("and the provider would not shrink that further",
      openai_would_reduce_to(*got) == got)

# A portrait page is bound by the same short-side rule on its other axis.
zp = vision_zoom_for_page(612, 792, provider="openai")
check("letter portrait: short side hits 768", round(612 * zp) == 768)

# A very wide page is bound by the 2048 box, not the short side.
zw = vision_zoom_for_page(5000, 800, provider="openai")
check("very wide page: long-side cap binds", round(5000 * zw) == OPENAI_MAX_BOX)
check("  and the short side stays under 768", round(800 * zw) <= OPENAI_SHORT_SIDE)

print("Anthropic has its own envelope:")
za = vision_zoom_for_page(*SHEET, provider="anthropic")
check("long edge <= 1568", round(SHEET[0] * za) <= 1568)
check("area <= 1.15 MP", (SHEET[0] * za) * (SHEET[1] * za) <= 1_150_001)

print("An unmodelled provider changes nothing:")
check("gemini -> None", vision_zoom_for_page(*SHEET, provider="gemini") is None)
check("unknown -> None", vision_zoom_for_page(*SHEET, provider="wat") is None)
check("degenerate page -> None", vision_zoom_for_page(0, 100, provider="openai") is None)

# ── the page render ──────────────────────────────────────────────────────
print("render_page_to_bytes now renders to the budget:")
doc, page = sheet_doc()
adaptive = render_page_to_bytes(doc, 1)
legacy = render_page_to_bytes(doc, 1, zoom=LEGACY_VISION_ZOOM)
a_size, l_size = size_of(adaptive), size_of(legacy)
check(f"adaptive render is {a_size[0]}x{a_size[1]}", a_size == (1152, 768))
check("legacy render was 5184x3456", l_size == (5184, 3456))
check("both reduce to the SAME thing the model sees",
      openai_would_reduce_to(*l_size) == a_size)
check(f"payload falls {len(legacy) / len(adaptive):.1f}x", len(adaptive) < len(legacy) / 3)
check("an explicit zoom is still honoured", size_of(render_page_to_bytes(doc, 1, zoom=1.0)) == (2592, 1728))
check("the cache does not confuse adaptive with explicit",
      size_of(render_page_to_bytes(doc, 1)) == (1152, 768))

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

huge = fitz.Rect(0, 0, page.rect.x1 + 500, page.rect.y1 + 500)
check("a region larger than the page clamps to it",
      page.rect.contains(region_render_rect(page, huge)))

# ── the region render ────────────────────────────────────────────────────
print("A region re-read buys real resolution, and costs less than a page:")
png, region, zoom = render_region_to_bytes(doc, 1, tiny)
sheet_zoom = vision_zoom_for_page(*SHEET, provider="openai")
text_sheet = TEXT_PT * sheet_zoom
text_region = TEXT_PT * zoom
check(f"3/32\" text {text_sheet:.1f} px at sheet level -> {text_region:.1f} px on the region",
      text_region > text_sheet * 8)
check("which clears the ~8 px floor where small CAD text stops resolving",
      text_region >= 8)
check("the rendered image is within the provider envelope",
      openai_would_reduce_to(*size_of(png)) == size_of(png))
check("and it costs no more than one whole-sheet image", len(png) <= len(adaptive))

pin = fitz.Rect(900, 900, 902, 902)             # pathologically small
_, _, z_small = render_region_to_bytes(doc, 1, pin)
check(f"zoom is capped at {REGION_MAX_ZOOM} for a pinpoint hit", z_small <= REGION_MAX_ZOOM)

doc.close()

# ── the re-read trigger ──────────────────────────────────────────────────
print("Only an admission of illegibility triggers a re-read:")
TRIGGERS = [
    ("The dimension text is not legible at this resolution", True),
    ("Value present but too small to read", True),
    ("Text is illegible in the provided image", True),
    ("unable to read the conductor callout", True),
    ("could not be read from the drawing", True),
    ("resolution is too low to resolve it", True),
    # "not shown" is an absence CLAIM, not an admission of blindness. It may
    # well be right, and re-reading every one of them would be expensive.
    ("The EGC size is not shown on this sheet", False),
    ("Legible and correct per NEC 250.122", False),
    ("the schedule is readable and complete", False),
    ("", False),
]
for text, want in TRIGGERS:
    check(f"{'fires ' if want else 'quiet '} on {text[:44]!r}",
          bool(_ILLEGIBILITY_RE.search(text)) is want)


# ── placing the region ───────────────────────────────────────────────────
print("The re-read only fires when it can place the region:")
doc, page = sheet_doc()

located = _region_for_finding(page, {"location_text": "1200 A OCPD"})
check("a location hint that hits the text layer places it", located is not None)
check("  and it lands on the page", located is not None and page.rect.contains(located))

by_bbox = _region_for_finding(page, {"location_bbox_norm": [200, 300, 260, 420]})
check("a model bbox places it when no hint matches", by_bbox is not None)

check("no hint, no bbox -> no re-read",
      _region_for_finding(page, {"location_text": "ZZZ NOT ON THIS DRAWING"}) is None)


# ── the re-read call ─────────────────────────────────────────────────────
print("The verdict is only taken when it is usable:")
import app.gemini_client as _client

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
check("the crop is magnified well past the sheet pass",
      v is not None and v["zoom"] > 4)
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

print("The number of extra calls per page is bounded:")
check(f"cap is a small integer ({MAX_REGION_REREADS_PER_PAGE})",
      isinstance(MAX_REGION_REREADS_PER_PAGE, int)
      and 1 <= MAX_REGION_REREADS_PER_PAGE <= 8)


print()
if _FAILS:
    print(f"FAILED ({len(_FAILS)}): {_FAILS}")
    sys.exit(1)
print("ALL VISION-SAMPLING CHECKS PASSED")
