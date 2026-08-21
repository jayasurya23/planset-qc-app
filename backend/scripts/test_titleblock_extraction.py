"""Sheet number and sheet title must come from the title block, not the drawing.

Both are read with geometry, and both read that geometry in the wrong
coordinate space on rotated pages -- the third and fourth instances of the
defect class test_page_rotation.py covers.

  first_sheet_number   get_text("words") reports rects in AUTHORED space, but
                       the band test compared y0 against page.rect.height (a
                       DISPLAY height) and the sort took the "bottom-most"
                       token. On /Rotate 270 authored y IS display x, so the
                       filter meant to select the bottom 35% of the sheet
                       actually selected the right 57%, and "bottom-most" meant
                       "right-most". A key-plan cross-reference in the top-right
                       corner outscored the real sheet number.

  guess_sheet_title    default_footer_bbox() is derived from page.rect (DISPLAY
                       space) and handed to page.get_textbox(), which clips in
                       AUTHORED space. On a rotated page that rect lands off the
                       authored page entirely: get_textbox returned "", the
                       `or text` fallback substituted the whole page, and every
                       footer-scoped strategy silently became a whole-page scan.
                       Measured on a production-geometry sheet: 0 characters
                       before, 63 after -- and the 63 are exactly the title
                       block.

Rotating the rects alone is not enough for the sheet number: the old sort takes
the bottom-most token, so a general note low on the left of the drawing then
beats the title block. Sheet numbers sit in the bottom RIGHT corner on both
common title-block layouts, so ranking is by proximity to that corner.

Run: PYTHONPATH=backend python backend/scripts/test_titleblock_extraction.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fitz  # noqa: E402

from app.analyzer import (  # noqa: E402
    default_footer_bbox,
    first_sheet_number,
    guess_sheet_title,
)

_FAILS: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _FAILS.append(name)


# ── page builders ────────────────────────────────────────────────────────
# The corpus is authored portrait 1728x2592 and displayed landscape 2592x1728
# with a title block down the right-hand edge. Laying content out in DISPLAY
# coordinates and mapping back keeps the fixtures readable:
#     display (dx, dy)  ->  authored (1728 - dy, dx)

TITLE_BLOCK = [
    (2202, 1350, "SHEET NUMBER", 9),
    (2340, 1405, "E-101", 40),
    (2202, 1505, "MAJOR ENGINEERED", 21),
    (2202, 1539, "EQUIPMENT LIST", 21),
]


def rotated_sheet(body=()):
    """Body content is emitted BEFORE the title block, so any strategy that
    depends on content order rather than geometry will pick the body up first.
    """
    doc = fitz.open()
    page = doc.new_page(width=1728, height=2592)
    for dx, dy, txt, size in list(body) + TITLE_BLOCK:
        page.insert_text((1728 - dy, dx), txt, fontsize=size, rotate=270)
    page.set_rotation(270)
    return doc, page


def flat_sheet():
    """Same layout authored natively landscape, with no /Rotate. Every
    transform below must be the identity here."""
    doc = fitz.open()
    page = doc.new_page(width=2592, height=1728)
    for dx, dy, txt, size in TITLE_BLOCK:
        page.insert_text((dx, dy), txt, fontsize=size)
    return doc, page


def number_of(page):
    return first_sheet_number(page, page.get_text("text"))


def title_of(page):
    text = page.get_text("text")
    return guess_sheet_title(page, text, first_sheet_number(page, text))


# ── the sheet number comes from the title block ──────────────────────────
print("The sheet number survives decoys elsewhere on the drawing:")
DECOYS = [
    ("clean title block", []),
    # display y=188 is the TOP of the sheet. The band test used to accept this
    # and reject the real number, because it was testing the wrong axis.
    ("key-plan cross-reference, top right", [(2350, 188, "C-201", 11)]),
    # The case a rotation-only fix regresses on: genuinely bottom-most, but far
    # from the title block.
    ("general note, bottom left", [(120, 1650, "C-301", 11)]),
    ("detail callout, mid sheet", [(300, 300, "E-505", 14)]),
    ("all three at once", [(2350, 188, "C-201", 11),
                           (120, 1650, "C-301", 11),
                           (300, 300, "E-505", 14)]),
]
for name, body in DECOYS:
    doc, page = rotated_sheet(body)
    check(f"{name} -> E-101", number_of(page) == "E-101")
    doc.close()

print("Unrotated pages are unaffected:")
doc, page = flat_sheet()
check("flat sheet still reads its title block", number_of(page) == "E-101")
doc.close()

# ── the footer scope actually scopes ─────────────────────────────────────
print("default_footer_bbox reaches the title block, not empty space:")
doc, page = rotated_sheet()
footer = default_footer_bbox(page) * page.derotation_matrix
footer.normalize()
scoped = page.get_textbox(footer) or ""
check("the de-rotated footer rect returns text at all", len(scoped) > 0)
check("and it is the title block", "MAJOR ENGINEERED" in scoped)
check("not the whole page", len(scoped) < len(page.get_text("text")))
doc.close()

print("A body callout repeating the sheet number cannot steal the title:")
# Emitted first, so the whole-page fallback reaches it before the title block.
# Before the fix this returned 'SEE DETAIL 3 THIS SHEET'.
doc, page = rotated_sheet([(300, 300, "E-101", 14),
                           (300, 330, "SEE DETAIL 3 THIS SHEET", 12)])
got = title_of(page)
check(f"title is the sheet name, got {got!r}",
      got is not None and got.startswith("MAJOR ENGINEERED"))
check("not the text following the body callout",
      got is not None and "SEE DETAIL" not in got)
doc.close()

print("The ordinary case is unchanged:")
doc, page = rotated_sheet()
check("wrapped title still assembles",
      title_of(page) == "MAJOR ENGINEERED EQUIPMENT LIST")
doc.close()
doc, page = flat_sheet()
check("unrotated title still assembles",
      title_of(page) == "MAJOR ENGINEERED EQUIPMENT LIST")
doc.close()

print()
if _FAILS:
    print(f"FAILED ({len(_FAILS)}): {_FAILS}")
    sys.exit(1)
print("ALL TITLE-BLOCK EXTRACTION CHECKS PASSED")
