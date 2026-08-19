"""A sheet's title must survive being wrapped across lines in a title block.

Every page-routing decision in the tool is a keyword match against the sheet
title, so a truncated title silently sends a checklist to the wrong drawing —
or to no drawing at all.

Measured over 40 production plansets / 1,587 sheets, the single-line extractor
produced junk or truncated titles on 623 sheets (39%):

  E-050  "MAJOR ENGINEERED"    the sheet is MAJOR ENGINEERED EQUIPMENT LIST
  E-200  "CAB PILES"           the sheet is INVERTER ZONE MAP
  E-202  "OHU"                 the sheet is COMMUNICATION ROUTING DIAGRAM
  E-201  "SCALE: 1\" = 70'"     it captured the scale note
  E-400  "GRADE"               the sheet is ELEVATION DETAILS
  E-140  "MP"                  the sheet is DC SYSTEM VALUES

"MAJOR ENGINEERED" is exactly why the equipment checklist could not find its
own sheet on Highland N1 and graded a riser-pole BOM instead, producing six
HIGH Fails. Two whole families (ai_comm, ai_cfp) matched NOTHING in the entire
corpus because their sheets' titles came out as junk.

After: 124 junk (7%), and no family is dead.

Run: PYTHONPATH=backend python backend/scripts/test_sheet_titles.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fitz  # noqa: E402

from app.analyzer import (  # noqa: E402
    _gather_title, _title_line_ok, guess_sheet_title,
)

_FAILS: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _FAILS.append(name)


def title_block(lines: list[str]) -> tuple[fitz.Document, fitz.Page]:
    """A page whose lower-right corner carries these lines, as CAD does."""
    doc = fitz.open()
    page = doc.new_page(width=1728, height=1120)
    y = 900
    for line in lines:
        page.insert_text((1250, y), line, fontsize=11)
        y += 22
    return doc, page


print("A wrapped sheet name is assembled, not truncated:")
doc, page = title_block(["E-050", "MAJOR ENGINEERED", "EQUIPMENT LIST"])
got = guess_sheet_title(page, page.get_text("text"), "E-050")
check("E-050 -> full name, not just the first line",
      got is not None and "ENGINEERED EQUIPMENT" in got.upper())
check("the routing keyword now matches",
      got is not None and "ENGINEERED EQUIPMENT" in got.upper())
doc.close()

doc, page = title_block(["E-104", "THREE LINE DIAGRAM -", "SHEET 1 OF 3"])
got = guess_sheet_title(page, page.get_text("text"), "E-104")
check("a continuation line is joined",
      got is not None and got.upper().startswith("THREE LINE DIAGRAM"))
doc.close()

print("Title-block metadata is not mistaken for a sheet name:")
for junk in ('SCALE: 1" = 70\'', "03/13/26", "4488", "1", "NO.", "SHEET 1"):
    check(f"{junk!r} rejected", not _title_line_ok(junk))
for real in ("TITLE SHEET", "INVERTER ZONE MAP", "ELEVATION DETAILS",
             "COMMUNICATION ROUTING DIAGRAM", "MAJOR ENGINEERED"):
    check(f"{real!r} accepted", _title_line_ok(real, first=True))

print("A title stops where a note or legend block begins:")
doc, page = title_block(["E-103", "AUXILLARY POWER DIAGRAM",
                         "NOTE: CONTRACTOR TO INFORM ENGINEER"])
got = guess_sheet_title(page, page.get_text("text"), "E-103")
check("the note is not swallowed into the title",
      got is not None and "NOTE" not in got.upper())
check("the real name survives",
      got is not None and "AUXILLARY POWER DIAGRAM" in got.upper())
doc.close()

print("...but a sheet genuinely NAMED for notes keeps its name:")
check("'GENERAL NOTES' is a valid first line", _title_line_ok("GENERAL NOTES", first=True))
check("'GENERAL NOTES' is not a valid continuation",
      not _title_line_ok("GENERAL NOTES", first=False))
check("'LEGEND' is a valid first line", _title_line_ok("LEGEND", first=True))
doc, page = title_block(["E-002", "GENERAL NOTES"])
got = guess_sheet_title(page, page.get_text("text"), "E-002")
check("E-002 keeps GENERAL NOTES",
      got is not None and "GENERAL NOTES" in got.upper())
doc.close()

print("_gather_title mechanics:")
lines = ["MAJOR ENGINEERED", "EQUIPMENT LIST", "SCALE: 1:100"]
check("joins forward and stops at metadata",
      _gather_title(lines, 0, 1) == "MAJOR ENGINEERED EQUIPMENT LIST")
check("joins backward in reading order",
      _gather_title(["ELEVATION", "DETAILS"], 1, -1) == "ELEVATION DETAILS")
check("caps at three lines",
      len((_gather_title(["AA", "BB", "CC", "DD"] , 0, 1) or "").split()) <= 3)
check("returns None when the first line is unusable",
      _gather_title(["4488", "REAL TITLE"], 0, 1) is None)
check("length is bounded",
      len(_gather_title(["X" * 40, "Y" * 40, "Z" * 40], 0, 1) or "") <= 90)

print("No sheet number means no guessed title:")
doc, page = title_block(["SOMETHING"])
check("returns None without a sheet number",
      guess_sheet_title(page, page.get_text("text"), None) is None)
doc.close()

print()
if _FAILS:
    print(f"FAILED ({len(_FAILS)}): {_FAILS}")
    sys.exit(1)
print("ALL SHEET-TITLE CHECKS PASSED")
