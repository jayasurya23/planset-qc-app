"""Text-anchored highlights must land on the page, including rotated sheets.

Production evidence: 1,257 of 1,267 pages across every stored planset carry
/Rotate 270 — 99% of the corpus. Solar plansets are drawn on E-size sheets
authored portrait and displayed landscape, so this is the normal case here,
not an edge case.

The bug: page.search_for() returns rectangles in the page's UNROTATED
(MediaBox) space, while get_pixmap, page.rect clamping, the x2 scale onto the
rendered image, and parse_ai_bbox all work in DISPLAY space. On a rotated page
those two spaces are transposed. Measured on a real sheet before the fix,
39% of text matches resolved outside the rendered image — for example 'E-101'
at y=2410 scaled to y=4820 on an image only 3456px tall. Those findings got a
blank snippet or a highlight drawn over the wrong part of the drawing, which
is the artifact a QC engineer clicks to see WHERE the problem is.

Notably the AI-supplied bbox path (parse_ai_bbox) was always correct, so the
two anchoring paths inside one function disagreed with each other.

Run: PYTHONPATH=backend python backend/scripts/test_page_rotation.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fitz  # noqa: E402

from app.analyzer import (  # noqa: E402
    _cached_text_lines,
    _expand_hit,
    _search_page_multi,
    parse_ai_bbox,
)

_FAILS: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _FAILS.append(name)


def build_page(rotation: int, needle: str = "E-101") -> tuple[fitz.Document, fitz.Page]:
    """A sheet with known text, rotated the way real plansets are."""
    doc = fitz.open()
    page = doc.new_page(width=1728, height=2592)  # portrait authoring size
    page.insert_text((200, 400), needle, fontsize=48)
    page.insert_text((900, 1800), "TRANSFORMER", fontsize=48)
    # Near the bottom of the AUTHORED page. On a 270-rotated sheet the
    # displayed height is only 1728pt, so an unrotated y of ~2400 is exactly
    # the case that used to resolve off-canvas — real sheet numbers and title
    # blocks live down here ('E-101' was found at y=2410 in production).
    page.insert_text((300, 2400), "SHEETNUM", fontsize=48)
    if rotation:
        page.set_rotation(rotation)
    return doc, page


ZOOM = 2.0


def in_image(rect: fitz.Rect, page: fitz.Page) -> bool:
    """Does this rect, scaled the way render_issue_artifacts scales it, land
    inside the rendered pixmap?"""
    w, h = page.rect.width * ZOOM, page.rect.height * ZOOM
    return (rect.x0 * ZOOM >= -1 and rect.y0 * ZOOM >= -1
            and rect.x1 * ZOOM <= w + 1 and rect.y1 * ZOOM <= h + 1)


for rot in (0, 90, 180, 270):
    print(f"/Rotate {rot}:")
    doc, page = build_page(rot)
    pix = page.get_pixmap(matrix=fitz.Matrix(ZOOM, ZOOM), alpha=False)

    rects = _search_page_multi(page, ["E-101"])
    check(f"  text found on a {rot}-rotated page", len(rects) > 0)
    if rects:
        r = rects[0]
        check(f"  match lands inside the {pix.width}x{pix.height} image",
              in_image(r, page))
        check("  rect is normalized (x0<=x1, y0<=y1)",
              r.x0 <= r.x1 and r.y0 <= r.y1)
        check("  rect has real area", r.width > 1 and r.height > 1)
        # the two anchoring paths must agree about which space they are in
        ai = parse_ai_bbox([0, 0, 1000, 1000], page)
        check("  agrees with the AI-bbox path's coordinate space",
              ai is not None and r.x1 <= ai.x1 + 1 and r.y1 <= ai.y1 + 1)
    doc.close()

print("Every match on every rotation lands in-image:")
off = 0
total = 0
for rot in (0, 90, 180, 270):
    doc, page = build_page(rot)
    for needle in ("E-101", "TRANSFORMER", "SHEETNUM"):
        for r in _search_page_multi(page, [needle]):
            total += 1
            if not in_image(r, page):
                off += 1
    doc.close()
check(f"0 of {total} matches off-canvas (got {off})", off == 0)

print("Unrotated pages are untouched (transform is identity):")
doc, page = build_page(0)
plain = page.search_for("E-101")
viaf = _search_page_multi(page, ["E-101"])
check("same rect as a raw search_for on an unrotated page",
      bool(plain) and bool(viaf)
      and abs(plain[0].x0 - viaf[0].x0) < 0.01
      and abs(plain[0].y0 - viaf[0].y0) < 0.01)
doc.close()

print("A rotated page genuinely transposes the axes (the bug was real):")
doc, page = build_page(270)
raw = page.search_for("SHEETNUM")            # unrotated space
fixed = _search_page_multi(page, ["SHEETNUM"])   # display space
check("raw search_for exceeds the displayed height",
      bool(raw) and raw[0].y1 > page.rect.height)
check("corrected rect does not", bool(fixed) and fixed[0].y1 <= page.rect.height)
doc.close()

print("No text on the page is handled without raising:")
doc = fitz.open()
blank = doc.new_page(width=1728, height=2592)
blank.set_rotation(270)
check("empty result, no exception", _search_page_multi(blank, ["nothing"]) == [])
doc.close()

print("The line fallback in _expand_hit works on rotated pages too:")
# Same defect, one layer down and easy to miss: get_text("dict") reports line
# bboxes in UNROTATED space, but _line_for_rect compares them against
# display-space hits. Findings inside a detected table still expanded (
# find_tables returns display space), so only FREE TEXT — plan notes,
# dimension callouts, general notes — silently kept a cramped word-level box.
doc = fitz.open()
page = doc.new_page(width=1728, height=2592)
# A note low on the authored page, i.e. inside the band that used to be lost.
# rotate=270 makes it read upright once the page rotation is applied, and it
# advances along +y, so start far enough up the page that the whole line fits.
page.insert_text((300, 1900), "WORKING CLEARANCE 3 FT 6 IN MIN, NEC 110.26(A)(1)",
                 fontsize=24, rotate=270)
page.set_rotation(270)

lines = _cached_text_lines(page)
check("line rects are reported in display space",
      bool(lines) and all(r.x1 <= page.rect.width + 1
                          and r.y1 <= page.rect.height + 1 for r in lines))

hit = _search_page_multi(page, ["WORKING CLEARANCE"])
check("the note is found at all", bool(hit))
if hit:
    grown = _expand_hit(page, hit[0])
    check("a free-text hit expands to its whole line",
          grown.get_area() > hit[0].get_area() * 1.2)
    check("the expanded line still lands inside the page",
          grown.x1 <= page.rect.width + 1 and grown.y1 <= page.rect.height + 1)
    check("the expansion contains the original hit", grown.contains(hit[0]))
doc.close()

print("Unrotated pages are unaffected (identity transform):")
doc = fitz.open()
flat = doc.new_page(width=1728, height=1120)
flat.insert_text((200, 200), "WORKING CLEARANCE 3 FT 6 IN MIN, NEC 110.26(A)(1)",
                 fontsize=24)
hit = _search_page_multi(flat, ["WORKING CLEARANCE"])
check("found on an unrotated page", bool(hit))
if hit:
    grown = _expand_hit(flat, hit[0])
    check("still expands to its line", grown.get_area() > hit[0].get_area() * 1.2)
    check("still inside the page",
          grown.x1 <= flat.rect.width + 1 and grown.y1 <= flat.rect.height + 1)
doc.close()

print()
if _FAILS:
    print(f"FAILED ({len(_FAILS)}): {_FAILS}")
    sys.exit(1)
print("ALL PAGE-ROTATION CHECKS PASSED")
