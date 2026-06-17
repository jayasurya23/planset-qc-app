"""Unit tests for sheet-title page targeting in gemini_analyzer.

Regression guard for the "EQUIPMENT PAD GROUNDING" bug: the structural
PAD/Slab and Equipment Area Feeder checks used the broad keyword
"EQUIPMENT PAD", which substring-matched the *grounding* sheet
"EQUIPMENT PAD GROUNDING" (E-501) and emitted ~13 false "pad dimensions
missing / pile details missing" fails on a sheet that legitimately shows
none. The fix adds an ``exclude=("GROUNDING",)`` filter so those checks
skip the grounding sheet (which the Grounding check owns) while still
matching genuine pad-detail / feeder sheets.

Runs WITHOUT any AI call — pure string matching.

Usage (from backend/):
    python scripts/test_page_targeting.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.gemini_analyzer import _sheet_title_matches


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


# Mirror the real keyword tuples used in run_gemini_checks so this test
# documents the actual production configuration.
EAF_KW = ("EQUIPMENT AREA", "EQUIPMENT PAD", "PAD FEEDER", "EQUIPMENT FEEDER")
PAD_KW = ("PAD DETAIL", "SLAB DETAIL", "CONCRETE PAD", "EQUIPMENT PAD",
          "FOUNDATION DETAIL")
GND_KW = ("GROUNDING", "GROUND PLAN", "GND", "EARTHING")
EXCL = ("GROUNDING",)


# ── The bug: grounding sheet must NOT be claimed by PAD / EAF ───────────────
GROUNDING_SHEET = "EQUIPMENT PAD GROUNDING"

check("grounding sheet rejected by PAD check",
      not _sheet_title_matches(GROUNDING_SHEET, PAD_KW, EXCL),
      "PAD check still matches EQUIPMENT PAD GROUNDING")

check("grounding sheet rejected by EAF check",
      not _sheet_title_matches(GROUNDING_SHEET, EAF_KW, EXCL),
      "EAF check still matches EQUIPMENT PAD GROUNDING")

check("grounding sheet WOULD match without the exclude (proves keyword overlap)",
      _sheet_title_matches(GROUNDING_SHEET, PAD_KW),
      "EQUIPMENT PAD no longer overlaps — keyword set changed?")

check("grounding sheet still owned by Grounding check",
      _sheet_title_matches(GROUNDING_SHEET, GND_KW),
      "Grounding check no longer matches its own sheet")

# Variants of grounding titles
for title in ("EQUIPMENT PAD GROUNDING PLAN", "EQUIPMENT GROUNDING DETAILS",
              "Inverter Pad Grounding"):
    check(f"PAD rejects {title!r}",
          not _sheet_title_matches(title, PAD_KW, EXCL))
    check(f"EAF rejects {title!r}",
          not _sheet_title_matches(title, EAF_KW, EXCL))


# ── Genuine pad / feeder sheets must STILL match ────────────────────────────
check("real pad-detail sheet still matches PAD",
      _sheet_title_matches("EQUIPMENT PAD DETAILS", PAD_KW, EXCL))

check("concrete pad detail still matches PAD",
      _sheet_title_matches("CONCRETE PAD DETAIL", PAD_KW, EXCL))

check("slab detail still matches PAD",
      _sheet_title_matches("INVERTER SLAB DETAIL", PAD_KW, EXCL))

check("real EAF sheet still matches EAF",
      _sheet_title_matches("EQUIPMENT AREA FEEDER PLAN", EAF_KW, EXCL))

check("equipment pad layout still matches EAF",
      _sheet_title_matches("EQUIPMENT PAD LAYOUT", EAF_KW, EXCL))


# ── Basic semantics ─────────────────────────────────────────────────────────
check("case-insensitive match",
      _sheet_title_matches("equipment pad details", PAD_KW, EXCL))

check("empty title -> no match (no crash)",
      not _sheet_title_matches("", PAD_KW, EXCL))

check("None-ish title -> no match (no crash)",
      not _sheet_title_matches(None, PAD_KW, EXCL))  # type: ignore[arg-type]

check("no keyword -> no match",
      not _sheet_title_matches("SITE PLAN", PAD_KW, EXCL))

check("default exclude is empty (keyword-only matches)",
      _sheet_title_matches("EQUIPMENT PAD GROUNDING", ("EQUIPMENT PAD",)))


print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
