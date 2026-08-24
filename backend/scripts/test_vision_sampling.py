"""Whole pages render at the long-standing zoom; regions render bigger.

An earlier version of this file asserted an "image budget": OpenAI at
detail="high" fits an image into 2048x2048 then scales the short side to 768,
so anything larger was wasted upload. Those are gpt-4-vision's rules. This
pipeline runs gpt-5.4-mini, which uses considerably more of what it is given,
and rendering to that budget starved it.

Measured by asking the model to transcribe every legible string on one
production sheet and counting how many appear in the PDF's own text layer:

    zoom 0.444   1151x768     75 KB    77 strings verified
    zoom 0.750   1944x1296   161 KB    91
    zoom 1.000   2592x1728   233 KB    93
    zoom 1.500   3888x2592   399 KB    95
    zoom 2.000   5184x3456   566 KB    87
    zoom 3.000   7776x5184   947 KB    87

Repeated trials put 0.444 at 60-77 verified against 85-87 for 2.0. It showed
up in production as Highland N1's illegibility admissions going from 5 to 17
between runs. So: whole pages are back on 2.0 until a multi-page, multi-trial
sweep settles where the plateau actually is. The 1.0-1.5 band looks better
than 2.0 on both quality and payload, but that is one sample per point, and
one sample per point is what caused this.

What survives is the half that was never in doubt: a crop of one region can be
rendered far larger than the sheet it came from, and that is the only way to
give the model more detail than the sheet-level pass already has.

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
print("A whole page renders at the zoom it always has:")
doc, page = sheet_doc()
default = render_page_to_bytes(doc, 1)
explicit = render_page_to_bytes(doc, 1, zoom=LEGACY_VISION_ZOOM)
check(f"default == legacy zoom {LEGACY_VISION_ZOOM}", size_of(default) == size_of(explicit))
check(f"which is {size_of(default)[0]}x{size_of(default)[1]} for a corpus sheet",
      size_of(default) == (5184, 3456))
check("an explicit zoom is still honoured",
      size_of(render_page_to_bytes(doc, 1, zoom=1.0)) == (2592, 1728))
check("the cache does not confuse two zooms",
      size_of(render_page_to_bytes(doc, 1)) == (5184, 3456))
# The regression this file now exists to prevent: something computing a
# "budget" and quietly shrinking the page out from under the model.
check("the default is never starved below the sheet's own point size",
      size_of(default)[0] >= int(SHEET[0]))

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
check(f"zoom {zoom:.1f} exceeds the page zoom {LEGACY_VISION_ZOOM}",
      zoom > LEGACY_VISION_ZOOM)
check(f"3/32\" text {TEXT_PT * LEGACY_VISION_ZOOM:.0f} px on the sheet "
      f"-> {TEXT_PT * zoom:.0f} px on the region",
      TEXT_PT * zoom > TEXT_PT * LEGACY_VISION_ZOOM * 3)
check(f"capped at {REGION_MAX_ZOOM}", zoom <= REGION_MAX_ZOOM)
check("and it costs less than the whole-sheet image it supplements",
      len(png) < len(default))

# A region big enough that the target would shrink it must not be shrunk:
# a re-read that renders smaller than the pass it is correcting is worthless.
wide = fitz.Rect(0, 0, page.rect.x1, page.rect.y1 * 0.9)
_, _, z_wide = render_region_to_bytes(doc, 1, wide)
check(f"a near-full-page region never renders below the page zoom ({z_wide:.2f})",
      z_wide >= LEGACY_VISION_ZOOM)

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

print()
if _FAILS:
    print(f"FAILED ({len(_FAILS)}): {_FAILS}")
    sys.exit(1)
print("ALL VISION-SAMPLING CHECKS PASSED")
