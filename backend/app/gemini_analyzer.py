"""Gemini Flash vision-powered deep QC checks for planset pages.

Architecture
------------
* Each ``check_*`` function targets one or two related QC-checklist sections.
* A page is rendered to PNG bytes **once** and reused across checks that need it.
* Gemini is asked to return **JSON** so we can deterministically create issues.
* If Gemini is unavailable or the call fails the check degrades to
  ``Needs Review`` – the regex-based checks from ``analyzer.py`` still stand.
"""

from __future__ import annotations

import functools
import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any

import fitz

from .analyzer import (
    PageInfo,
    default_footer_bbox,
    ensure_dirs,
    expanded_rect,
    make_issue,
    parse_ai_bbox,
    rect_to_dict,
    render_issue_artifacts,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def render_page_to_bytes(doc: fitz.Document, page_number: int, zoom: float = 2.0) -> bytes:
    """Render a 1-based *page_number* to PNG bytes. Cached per (page, zoom)
    so the same page isn't re-rasterized for every vision check that
    references it."""
    page = doc[page_number - 1]
    cache_key = f"_qc_bytes_{zoom}"
    cached = getattr(page, cache_key, None)
    if cached is not None:
        return cached
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    data = pix.tobytes("png")
    try:
        setattr(page, cache_key, data)
    except Exception:
        pass
    return data


def render_page_preview(
    doc: fitz.Document,
    page_number: int,
    issue_id: str,
    run_dir: Path,
    zoom: float = 2.0,
) -> tuple[str | None, str | None]:
    """Save a full-page preview PNG and return (snippet_path, preview_path).

    Reuses the cached page image when available to avoid re-rasterizing the
    same page once per finding.
    """
    from .analyzer import _cached_page_image
    snippets_dir, previews_dir = ensure_dirs(run_dir)
    page = doc[page_number - 1]
    preview_path = previews_dir / f"{issue_id}.png"
    try:
        img = _cached_page_image(page, zoom=zoom)
        img.save(str(preview_path))
    except Exception:
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        pix.save(str(preview_path))
    return None, str(preview_path)


@functools.lru_cache(maxsize=1)
def _rule_title_index() -> dict[str, str]:
    """Map rule key → original human title from the registry. Lets V4
    vision findings show a readable title (``"XFMR primary V: six-document
    match"``) instead of a slugified rule key (``"V4 E 100 Xfmr Primary V
    Six Document Match"``).
    """
    try:
        from .rule_registry import get_rules
        return {r.key: r.title for r in get_rules()}
    except Exception:
        return {}


def _pretty_title_for(check_name: str) -> str:
    """Preferred display title for a finding's ``check`` field.

    Lookup order:
    1. Rule registry (V4-style rules).
    2. Known synthetic rule keys from the Other-Electrical catch-all.
    3. Slug → Title Case fallback (so legacy hard-coded prompts still work).
    """
    idx = _rule_title_index()
    if check_name in idx:
        return idx[check_name]
    try:
        from .v4_engine import OTHER_ELECTRICAL_RULE_TITLES
        if check_name in OTHER_ELECTRICAL_RULE_TITLES:
            return OTHER_ELECTRICAL_RULE_TITLES[check_name]
    except Exception:
        pass
    return check_name.replace("_", " ").title()


def _pick_page_for_finding(finding: dict, page_numbers: list[int]) -> int | None:
    """If the model reported which page of the batch the issue is on, return
    the corresponding actual PDF page number. Supports ``page_index`` (0-based
    into the submitted image set) and a numeric ``page`` field (either a
    batch index or an actual PDF page number).
    """
    if not page_numbers:
        return None
    idx = finding.get("page_index")
    if isinstance(idx, int) and 0 <= idx < len(page_numbers):
        return page_numbers[idx]
    raw = finding.get("page")
    if isinstance(raw, int):
        if raw in page_numbers:
            return raw
        if 1 <= raw <= len(page_numbers):
            return page_numbers[raw - 1]
    if isinstance(raw, str):
        m = re.search(r"\d+", raw)
        if m:
            n = int(m.group(0))
            if n in page_numbers:
                return n
            if 1 <= n <= len(page_numbers):
                return page_numbers[n - 1]
    return None


def _extract_location_hints(finding: dict) -> list[str]:
    """Pull searchable literal text excerpts out of a vision finding.

    Supports both ``location_text`` (single string) and ``location_texts``
    (list). Also mines short values out of the ``value`` field when it looks
    like a literal callout the model read off the drawing.
    """
    hints: list[str] = []
    seen: set[str] = set()

    def _add(x: Any) -> None:
        if not isinstance(x, str):
            return
        s = x.strip().strip('"\'')
        if not s or len(s) < 3 or len(s) > 80:
            return
        if s.lower() in ("null", "none", "n/a", "na", "unknown"):
            return
        if s in seen:
            return
        seen.add(s)
        hints.append(s)

    _add(finding.get("location_text"))
    loc_list = finding.get("location_texts")
    if isinstance(loc_list, list):
        for item in loc_list:
            _add(item)

    # A short literal ``value`` field (e.g. "500 kcmil AL") is often directly
    # searchable on the page too. Skip long prose values.
    val = finding.get("value")
    if isinstance(val, str) and 3 <= len(val.strip()) <= 40:
        _add(val)

    # The ``location`` field is human-readable (e.g. "AC Schedule, Row 3
    # (INV-1 to SWBD), FLA column"). It rarely matches verbatim, but the
    # token-level search fallback in analyzer._search_page_multi can still
    # use parts of it ("INV-1", "Row 3", "FLA") to anchor a bbox when
    # nothing else hits. Pass it through as an extra hint — the search
    # pipeline filters generic words.
    loc = finding.get("location")
    if isinstance(loc, str) and 5 <= len(loc.strip()) <= 120:
        _add(loc.strip())

    return hints


def _extract_json(text: str) -> list[dict]:
    """Best-effort extraction of a JSON array from Gemini's response."""
    # Try to find ```json ... ``` block first
    m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # Try the whole response
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict) and "findings" in parsed:
            return parsed["findings"]
        return [parsed]
    except json.JSONDecodeError:
        pass

    # Last resort – find outermost [ ... ]
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start: end + 1])
        except json.JSONDecodeError:
            pass

    logger.warning(
        "Could not parse JSON from Gemini response:\n%s", text[:500])
    return []


def _safe_gemini_call(func, *args, **kwargs) -> list[dict]:
    """Call *func* and return issues; on any error return empty list."""
    try:
        return func(*args, **kwargs)
    except Exception:
        logger.exception("Gemini check %s failed – skipping", func.__name__)
        return []


# ---------------------------------------------------------------------------
# Bbox rescue — second-pass deep-model call when text search + AI bbox fail
# ---------------------------------------------------------------------------


_BBOX_RESCUE_PROMPT_TEMPLATE = """\
You are looking at a single page from a solar PV planset. Below is a list of
QC findings that the previous pass identified on this page but for which it
did NOT return a usable location. For each finding, return a bounding box
(in normalized 0–1000 coordinates, top-left origin, y grows down) of where
the relevant area is — OR where it SHOULD be if the item is missing from
the drawing.

For values that are PRESENT on the page, return a tight bbox around the
cell, callout, or table row.

For values that are MISSING / NOT SHOWN, return the bbox of the empty area
where they SHOULD have been drawn — e.g. the empty schedule row, the blank
field next to a label, the SLD region where the missing equipment symbol
belongs. Do NOT return a generic full-page or full-half-page box; pick the
most specific empty region you can.

Findings to locate:

{findings_block}

Return ONLY a JSON array, one object per finding, in the same order:

[
  {{ "id": "<the id from the input>", "bbox_norm": [y0, x0, y1, x1] }}
]

Use the same id strings the input gives you. If a finding genuinely has no
sensible location on this page, omit it from the output array (do not
return a placeholder).
"""


def _rescue_missing_bboxes(
    doc,
    page_number: int,
    rescue_targets: list[dict],
) -> dict[str, dict]:
    """Second-pass Gemini call to fetch bboxes for findings that came back
    without one.

    Each ``rescue_targets`` entry is a dict with at least ``id``, ``check``,
    ``status``, ``location`` (the AI's human-readable description), and
    optionally ``value`` / ``evidence``. Returns a mapping
    ``id → {"bbox": fitz.Rect, "raw": [y0,x0,y1,x1]}`` for findings the
    rescue call could place.

    Costs one extra deep-model call per page that has any rescue targets;
    runs after the main per-category pass so it sees the rule context. On
    any failure (parse error, no targets, API error) returns an empty dict
    and the caller falls through to the full-page-preview path that was
    used before this rescue existed.
    """
    if not rescue_targets:
        return {}

    # Build the findings block. Keep it compact — Gemini is good at this and
    # we want the prompt cheap. Cap each entry to 240 chars including the
    # location text so a verbose finding can't crowd the others.
    lines: list[str] = []
    for idx, t in enumerate(rescue_targets):
        loc = (t.get("location") or "").strip().replace("\n", " ")[:120]
        val = (t.get("value") or "").strip().replace("\n", " ")[:60]
        ev  = (t.get("evidence") or "").strip().replace("\n", " ")[:120]
        # Tag id is just the index — short and stable, avoids needing to
        # echo the full check name.
        bits = [f"  ID {idx}: [{t.get('status', '?')}] {t.get('check', '?')}"]
        if loc:
            bits.append(f"    location: {loc}")
        if val:
            bits.append(f"    value: {val}")
        if ev:
            bits.append(f"    evidence: {ev[:120]}")
        lines.append("\n".join(bits))
    findings_block = "\n\n".join(lines)
    prompt = _BBOX_RESCUE_PROMPT_TEMPLATE.format(findings_block=findings_block)

    try:
        from .gemini_client import analyze_page_image
        image_bytes = render_page_to_bytes(doc, page_number)
        # Deep model — this is a vision-grounding task and the mini model is
        # noticeably worse at returning consistent normalized coordinates.
        raw = analyze_page_image(image_bytes, prompt, deep=True)
    except Exception:
        logger.exception(
            "Bbox rescue: page %d image / Gemini call failed", page_number,
        )
        return {}

    parsed = _extract_json(raw) or []
    page = doc[page_number - 1]
    out: dict[str, dict] = {}
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        eid = entry.get("id")
        # Accept either int (matching our index tags) or stringified int.
        try:
            idx = int(eid)
        except (TypeError, ValueError):
            continue
        if idx < 0 or idx >= len(rescue_targets):
            continue
        rect = parse_ai_bbox(entry.get("bbox_norm") or entry.get("bbox"), page)
        if rect is None:
            continue
        target_id = rescue_targets[idx].get("id")
        if target_id is None:
            continue
        out[str(target_id)] = {"bbox": rect, "raw": entry.get("bbox_norm")}

    if out:
        logger.info(
            "Bbox rescue: page %d — recovered %d/%d missing locations",
            page_number, len(out), len(rescue_targets),
        )
    return out


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

_TITLE_BLOCK_PROMPT = """\
You are a QC engineer reviewing a solar PV planset drawing page.

Examine the TITLE BLOCK area (usually bottom-right corner, sometimes the
right-side vertical strip on landscape D-size drawings) and check for the
following items:

1. **SOV** (Scope of Verification) present
2. **Date** is shown
3. **Designer name** is shown
4. **Engineer name** is shown
5. **Revision number** is shown
6. **EPC Logo** is present (any company logo in the title block)
7. **Project Name** is shown
8. **Sheet Number** (e.g. E-001, E-100) is shown

CRITICAL — LABEL vs VALUE:
Title blocks are printed templates with fixed FIELD LABELS like
"PROJECT NAME:", "DATE:", "DESIGNER:" etc. The actual VALUE is filled
in next to, above, below, or within a bordered box below the label.
- When a label and its value are BOTH visible, report the finding as
  "found: true" and put the VALUE in the "value" field. NEVER put the
  label text in the "value" field.
- Only mark a field as "found: false" if the value cell is truly blank /
  placeholder (e.g. literally shows "XXXX", "TBD", "---", or is empty).
- Do NOT treat the presence of a label like "PROJECT NAME" as a defect
  or placeholder. The label is expected template text.
- Do NOT treat ALL-CAPS or stylized text (e.g. "WELLINGTON SOLAR") as
  a placeholder just because of its formatting — it's almost certainly
  the real project name.

Return a JSON array of findings. Each finding:
```json
[
  {
    "check": "title_block_item_name",
    "found": true/false,
    "value": "the actual filled-in value, NOT the field label",
    "notes": "any observations"
  }
]
```
Only return the JSON array, no other text.
"""

_COVER_SHEET_PROMPT = """\
You are a QC engineer reviewing the COVER SHEET of a solar PV planset.

CRITICAL — LABEL vs VALUE:
Cover sheets and the right-side vertical title-block strip contain FIELD
LABELS printed as part of the template (e.g. "PROJECT NAME", "OWNER",
"EPC", "EOR", "SHEET NUMBER"). The actual VALUE is filled in next to,
above, below, or within a bordered box near the label.
- A label is NOT a defect. Do NOT flag "PROJECT NAME" (or similar) as
  missing/placeholder just because the capitalized label text appears.
- When you report a value, put the FILLED-IN value in the "value" field,
  NEVER the field label itself.
- Only mark a field "found: false" if the value cell is genuinely empty
  or contains an obvious placeholder ("XXXX", "TBD", "N/A", "---", or
  the project-info row literally reads "Project Name" as its own value).
- ALL-CAPS project names (e.g. "WELLINGTON SOLAR", "CAMP HALL 3") are
  valid values, NOT placeholders.

Extract and verify ALL of the following from this page:

**Project Info:**
1. Project Name
2. Project Address
3. Site Coordinates (lat/long)
4. Building Codes referenced (NEC year, state/county/city adopted codes)
5. DER Number — CONDITIONAL check. If a DER number is shown anywhere on the
   cover sheet, verify the value (format, plausibility) and emit a finding.
   If no DER number is shown, DO NOT emit a finding at all — skip it
   entirely. A missing DER number is NOT a defect (not every utility
   requires one). Do not output "Fail" or "Needs Review" for a missing
   DER number.

**Owner Info:**
6. Owner Name
7. Owner Address
8. Owner Telephone Number

**EPC Info:**
9. EPC Name
10. EPC Address
11. EPC Telephone Number

**Engineering Info:**
12. EOR (Engineer of Record) Name and License Number
13. Checker and Designer names

**Maps:**
14. County/State Map present (yes/no)
15. Array View / Site Overview present (yes/no)
16. North Arrow on array view (yes/no)
17. Vicinity Map present (yes/no)

**Drawing Index:**
18. Drawing Index table present (yes/no)
19. List all sheet numbers and names from the drawing index

Return a JSON array of findings:
```json
[
  {
    "check": "item_name",
    "found": true/false,
    "value": "extracted value or null",
    "notes": "observations"
  }
]
```
Only return the JSON array.
"""

_SYSTEM_INFO_PROMPT = """\
You are a senior solar PV QC engineer auditing the SYSTEM INFORMATION TABLE of a
planset. This table is the single source of truth for the project — errors here
propagate to every other sheet, so EVERY numeric value must be validated, not
just extracted. The table layout is standardized across plansets; the defects
you are looking for are DATA errors (wrong numbers, unit mistakes, stale values
copied from a previous project, math that does not check out).

════════════════════════════════════════════════════════════════
STEP 1 — EXTRACT every value you can read. Be precise — include units.
════════════════════════════════════════════════════════════════

Read from the system info table FIRST. If a field is missing from the table,
look at datasheets, equipment list, SLD callouts, title block, and notes on
this page. Report WHERE you found each value.

Module: Make, Model, STC Watts (W), Voc, Vmp, Isc, Imp, Temp Coeff Voc (%/°C),
  Temp Coeff Isc, Temp Coeff Pmax, Bifaciality factor (if bifacial).
Array: Module Quantity (total), String Size (modules/string), String Quantity
  (total strings), Total DC Size (kWp or MWp).
Inverter: Make, Model, kVA, kW, AC Voltage, Max Vdc, MPPT Range (V), Inverter
  Quantity, Derated kVA/kW (if present).
System Totals: Total AC Size (kVA or MVA), DC/AC Ratio.
Layout: Racking Make/Model/Size, Pitch (ft or m), Interrow Spacing, GCR, Tilt
  Angle OR Tracker Rotation Range, Azimuth.
Site: Design Low Temp (°C), Design High Temp (°C), Ambient Temp (°C),
  Site Elevation (if shown).

════════════════════════════════════════════════════════════════
STEP 2 — VALIDATE with math. For each rule below, emit ONE finding.
════════════════════════════════════════════════════════════════

Show every calculation as "Expected: X (reason) / Found: Y → Pass|Fail" in the
"evidence" field. Use a 2% tolerance for DC total (rounding/binning), 0.5%
for ratios and string counts (must match exactly). When a math check fails,
use severity "high". When a value is simply missing, use "medium". Use "low"
only for cosmetic issues.

**M1 — Module count consistency:**
  Module Qty should equal String Size × String Quantity.
  e.g. 28 × 200 = 5600. Flag mismatch.

**M2 — Total DC size math:**
  Total DC (kW) ≈ Module Qty × Module STC Watts / 1000.
  e.g. 5600 × 550W / 1000 = 3,080 kWp. Allow ±2% for binning.

**M3 — Total AC size math:**
  Total AC (kVA) ≈ Inverter Qty × Inverter kVA rating.
  Must be exact. Flag any mismatch.

**M4 — DC/AC ratio math:**
  DC/AC = Total DC (kW) / Total AC (kW). Compare against stated ratio.
  Typical acceptable range 1.10–1.50. Flag values outside that range as
  "Needs Review".

**M5 — Unit sanity:**
  If "Total DC" is shown in the same units as a single module's watts,
  something is wrong. Example red flag: "Total DC: 550 W" (single module
  value in the total row). Flag any field whose order of magnitude looks
  inconsistent with its label (kW vs MW, V vs kV, A vs kA).

**M6 — Inverter make/model match:**
  Inverter make/model on the system info table MUST match the inverter
  datasheet (if visible) and the SLD equipment box (if visible). Flag any
  manufacturer/model mismatch — this is a common copy-paste error.

**M7 — Module make/model match:**
  Module make/model on the system info table MUST match the module datasheet
  if one is on the submitted pages. Flag mismatches.

**M8 — Electrical value match between table and datasheet:**
  Module Voc, Vmp, Isc, Imp, Pmax on the system info table MUST match the
  datasheet exactly (within 0.1). Flag any mismatch. This is where stale
  values from a previous job are most often left behind.

**M9 — Bifacial sanity:**
  If the module is bifacial (Bifaciality > 0), the inverter's max Isc rating
  must accommodate the bifacial gain. Note if bifaciality is declared but
  not reflected anywhere else. If NOT bifacial, there should be no bifacial
  factor or backside gain reported.

**M10 — Voltage/MPPT window (read-only sanity):**
  If module Voc and Temp Coeff Voc are shown, and design low temp is shown,
  compute cold Voc = Voc × (1 + TempCoeffVoc/100 × (Tmin − 25)). Then
  cold-string Voc = cold Voc × String Size. This MUST be < Inverter Max Vdc.
  If any input is missing, mark "Needs Review" with the missing fields.

**M11 — GCR vs Pitch sanity:**
  GCR = (module width along pitch axis) / Pitch. If module dimensions are
  visible, recompute; else just verify GCR is within 0.25–0.55. Flag values
  outside typical range.

**M12 — Azimuth/tilt plausibility:**
  Azimuth for fixed-tilt in N hemisphere typically 180° (south). Tracker
  systems typically list azimuth of 0° with a tilt RANGE (e.g. ±60°). Flag
  implausible combos (e.g. a tracker with a single fixed tilt angle).

**M13 — Derated rating check:**
  If derated kVA/kW is shown, it must be ≤ nameplate kVA/kW. Flag if
  greater. Note the derating factor for evidence.

**M14 — String fuse / Isc consistency:**
  If string fuse is shown, Isc × 1.56 should be ≤ string fuse rating.
  (1.56 = 1.25 × 1.25 NEC factor). Flag if over-fused.

**M15 — Blank / TBD / placeholder detection:**
  Flag any field that reads "TBD", "TODO", "XXX", "---", "N/A" where a
  real value is required, or an obvious placeholder like "Project Name"
  literally as the project name.

NOTE ON CONSISTENCY: the system info table is replicated identically
across every sheet of the planset by template — cross-sheet consistency
OF THE TABLE ITSELF is guaranteed by construction. Do NOT emit findings
about sysinfo differences between pages; that is not a real defect. The
only cross-source check still relevant is M7/M8 — comparing the sysinfo
table against the equipment datasheet on a submitted datasheet page.

════════════════════════════════════════════════════════════════
OUTPUT FORMAT
════════════════════════════════════════════════════════════════

Return a JSON array. Emit findings ONLY for the M1–M15 rules. Do NOT emit
per-field-group extractions — the extracted values from STEP 1 are used
internally for validation and should NOT appear as separate findings.
Do NOT emit any finding about "sysinfo table differs between pages";
the table is replicated by template and is always consistent.

EMIT-ON-FAILURE-ONLY rules: for M9 (bifacial sanity), M11 (GCR vs pitch),
M12 (azimuth/tilt plausibility), and M13 (derated rating), omit the
finding entirely when the check passes. Emit only when Fail or Needs
Review. These are low-yield sanity checks that dominate output length
when everything is normal.

For EACH emitted finding include the standard "location" / "location_text"
fields so the reviewer can highlight the value on the PDF.

```json
[
  {
    "check": "m2_total_dc_size_math",
    "status": "Pass|Fail|Needs Review",
    "value": "3080 kWp",
    "evidence": "Expected: 5600 modules × 550W = 3080 kWp. Found: 3078 kWp in system info table. Within 2% tolerance → Pass.",
    "location": "System Info Table, Row 'Total DC Size'",
    "location_text": "3078 kWp",
    "severity": "low|medium|high"
  }
]
```

Only return the JSON array.
"""

_SLD_PROMPT = """\
You are a senior electrical PE reviewing the AC SINGLE LINE DIAGRAM (SLD) of a solar PV power plant. You must READ every value on the drawing and VALIDATE it using NEC 2020 rules.

STEP 1 — EXTRACT all values you can read from the drawing. Be precise — read exact numbers, sizes, ratings.

STEP 2 — VALIDATE using these engineering rules:

**Inverter Section:**
1. Read inverter make, model, kVA, kW, AC voltage, quantity.
2. Calculate: Total inverter output current = Total kVA / (√3 × AC voltage). Read the FLA shown — does it match your calculation?
3. Read MPPT count and max DC input current per MPPT from the diagram.

**AC Cable Sizing (Inverter to Transformer):**
4. Read cable callout: size (AWG/kcmil), sets, material (CU/AL), insulation type, ground wire.
5. VALIDATE: For the FLA shown, multiply by 1.25 for continuous load. The cable ampacity at 75°C column (NEC 310.16) must be ≥ this value.
   - NEC 310.16 key values at 75°C for CU: #8=50A, #6=65A, #4=85A, #3=100A, #2=115A, #1=130A, 1/0=150A, 2/0=175A, 3/0=200A, 4/0=230A, 250=255A, 300=285A, 350=310A, 400=335A, 500=380A, 600=420A, 750=475A.
   - For AL at 75°C: #6=50A, #4=65A, #2=90A, 1/0=120A, 2/0=135A, 3/0=155A, 4/0=180A, 250=205A, 300=230A, 350=250A, 500=310A, 750=385A.
   - If multiple sets, each set carries FLA/(number of sets). Each set's cable must handle that.
6. If OCPD > 800A: NEC 240.4(C) says cable ampacity must be ≥ OCPD rating (no round-up rule).
   If OCPD ≤ 800A: NEC 240.4(B) allows next standard size up.
7. Read ground wire size. For parallel conductors, verify EGC is sized per NEC 250.122 based on OCPD rating:
   - 15A→#14, 20A→#12, 60A→#10, 100A→#8, 200A→#6, 300A→#4, 400A→#3, 500A→#2, 600A→#1, 800A→1/0, 1000A→2/0, 1200A→3/0, 1600A→4/0, 2000A→250, 2500A→350, 3000A→400, 4000A→500, 5000A→700, 6000A→800.

**Transformer:**
8. Read kVA rating. VALIDATE: XFMR kVA must be ≥ total inverter kVA output. Calculate and compare.
9. Read primary/secondary winding config (Delta/Wye), voltages, BIL.
   - For 15kV class: BIL should be 95kV. For 25kV: 125kV. For 34.5kV: 150kV.
10. Read cooling type. For pad-mount: ONAN is standard (100% capacity). ONAN/ONAF = 100%/133%.
11. Read impedance Z% and X/R. Standard Z% for 750-2500kVA is 5.75%. For 3750kVA: 5.75-6.0%.
12. Is transformer internal fuse and load-break switch shown?

**MV Equipment:**
13. Read surge arrestor MCOV rating. VALIDATE against system voltage:
    - 12470Y/7200 (4-wire multigrounded): MCOV should be 8.4kV
    - 13200Y/7620: MCOV should be 10(8.4) or 9(7.65)
    - 13800Y/7970: MCOV should be 10(8.4) and 12(10.2)
    - 24940Y/14400: MCOV should be 18(15.3)
    - 34500Y/19920: MCOV should be 27(22.0)
14. Read recloser: make, continuous A, interrupting kA, BIL. For 15kV class: BIL typically 110kV, continuous 630-800A, interrupting 12.5-16kA.
15. Read meter CT ratio and VT ratio. CT ratio should be appropriate for the FLA after transformer.
16. Read meter accuracy class (should be 0.3 for revenue metering).
17. Is GOAB labeled as "Main Service Disconnect"? Read its voltage and continuous A rating.

**MV Cable:**
18. Read MV cable: size, material (typically AL), insulation class (15kV/25kV/35kV), insulation type (TR-XLPE or EPR).
19. VALIDATE: For the MV FLA, multiply by 1.25. The cable ampacity must exceed this. Use direct-buried ampacities if direct buried.
    - 15kV AL direct buried (Table 310.60): 1/0=135A, 2/0=155A, 4/0=200A, 250=225A, 350=275A, 500=340A.
20. Is the MV cable direct buried or in conduit? If direct buried, check burial depth ≥ 36 inches.

**Cable Notation Check:**
21. Verify cable callouts follow proper format:
    - DC: (qty) #size AWG PV WIRE CU/AL, 90°C RATED WIRE, IN MIXED RACKING AND conduit
    - LV: (qty) size AWG/kcmil XHHW-2 AL/CU, (1) #size GND, @90°C RATED, IN conduit
    - MV: (qty) size AL (MV-105), kV CLASS, TR-XLPE 100%, INSULATED 3Ø CONDUCTORS, #size AL EGC, IN conduit

**Six Disconnect Rule — NEC 230.71:**
22. Count the number of disconnects serving the switchboard. NEC 230.71 limits this to SIX or fewer disconnects in a single enclosure. Count the main breakers and disconnects — flag if > 6.

**Grounding Transformer — CESIR match:**
23. If a grounding transformer is shown, does its configuration match the CESIR (interconnection agreement) requirements? Read the grounding transformer kVA and connection type.

**String Distribution:**
24. Read how strings are distributed between DCBs vs direct to inverter. Verify the total string count matches the system information table.

Return a JSON array of findings. For each finding, show your calculation if you validated a value:
```json
[
  {
    "check": "descriptive_check_name",
    "category": "inverter|cable|transformer|mv_equipment|mv_cable|ocpd|consistency",
    "status": "Pass|Fail|Needs Review",
    "value": "extracted value",
    "evidence": "Calculation: FLA=X, 1.25×X=Y, cable rated Z at 75°C → Pass/Fail",
    "severity": "low|medium|high"
  }
]
```
Only return the JSON array.
"""

_DC_DIAGRAM_PROMPT = """\
You are a senior electrical PE reviewing the DC LINE DIAGRAM of a solar PV planset. READ every value and VALIDATE against engineering rules.

STEP 1 — EXTRACT all values from the diagram.
STEP 2 — VALIDATE using these rules:

**Inverter/DCB Topology:**
1. Read the inverter make and model shown. Is this a string inverter (strings connect directly) or central inverter (strings go through DC combiner boxes first)?
2. Read how many MPPT inputs per inverter, and how many string inputs per MPPT.
3. Read the maximum DC input voltage, MPPT voltage range, and max Isc per input from the diagram.

**String Configuration — VALIDATE:**
4. Read modules per string. Read module Voc and temperature coefficient of Voc from any table shown.
5. CALCULATE: Voc_cold = modules_per_string × Voc × (1 + TempCoeff × (Tmin - 25)). Where Tmin is the design low temp (typically -20°C to -40°C depending on location).
   - This value MUST be < inverter max DC input voltage. Flag if it appears to exceed.
6. CALCULATE: Vmp_hot = modules_per_string × Vmp × (1 + TempCoeff × (Thot - 25)). Where Thot is design high temp.
   - This value should be within the MPPT voltage range. Flag if it appears below MPPT min.
7. Read the string Isc. Verify: Isc × 1.25 should be ≤ the string fuse rating shown.

**String and DCB Fuses:**
8. Read string fuse rating. Standard sizes per NEC 240.6: 15, 20, 25, 30, 35, 40, 45, 50, 60A etc.
   - String fuse should be ≥ 1.56 × Isc (which is 1.25 × 1.25 × Isc) for modules without listed fuse rating, OR follow module max series fuse rating from datasheet.
9. Read DCB output fuse rating if shown. Verify it is rated for the combined string currents.

**DC Disconnect:**
10. Read DC disconnect type and rating. Verify rated voltage ≥ system Voc at cold temp.

**DC Cable Sizing:**
11. Read PV wire size, material, insulation type. Typical: #10 AWG PV WIRE CU, 90°C rated.
    - PV wire ampacity at 90°C: #14=25A, #12=30A, #10=40A, #8=55A, #6=75A, #4=95A.
    - Verify: cable ampacity after derating ≥ 1.25 × Isc of the strings carried.
12. For DC homerun/whip cables from DCB to inverter, read size. Verify ampacity handles the combined current.

**Schedules — READ the data:**
13. If MPPT schedule is shown, read it. Verify total strings across all MPPTs matches the site total.
14. If harness schedule is shown, read cable lengths and sizes. Flag any unusually long runs (>500ft for #10 AWG).
15. If DCB/inverter schedule is shown, verify quantities match the site plan.

**Grounding:**
16. PV module grounding note present? (Equipment grounding through racking hardware or separate EGC)
17. Rack-to-CAB grounding conductor shown? What size?
18. CAB-to-DCB/Inverter grounding shown?

Return a JSON array. Show your calculations for any validated values:
```json
[
  {
    "check": "check_name",
    "status": "Pass|Fail|Needs Review",
    "value": "extracted value",
    "evidence": "Calculation: Voc_cold = 28×49.5V×1.145 = 1582V vs max 1500V → FAIL",
    "severity": "low|medium|high"
  }
]
```
Only return the JSON array.
"""

_THREE_LINE_PROMPT = """\
You are a QC engineer reviewing the THREE LINE DIAGRAM (3LD) of a solar PV planset.

Check the following on this page:

1. **Equipment Labels** – Are all equipment shown with labels and ratings?
2. **No Cable Sizes** – The 3LD should NOT show cable sizes or FLA (those belong on the SLD)
3. **CT/VT Arrangement** – Are CTs and VTs shown for metering? What ratios?
4. **Breaker Phases** – Are breakers shown with correct phase count (3P vs 2P vs 1P)?
5. **Transformer Windings** – Are LV and HV winding configurations shown (Delta/Wye)?
6. **Transformer Grounding** – Are transformer grounding connections shown?
7. **Internal Fuse/Switch** – Is transformer internal fuse and switch shown?
8. **Consistency** – Do equipment names and ratings match what would be on the SLD?

Return a JSON array:
```json
[
  {
    "check": "check_name",
    "status": "Pass|Fail|Needs Review",
    "value": "what you found",
    "evidence": "details",
    "severity": "low|medium|high"
  }
]
```
Only return the JSON array.
"""

_SITE_PLAN_PROMPT = """\
You are a QC engineer reviewing the SITE PLAN of a solar PV planset.

Check for the following items:

1. **North Arrow** – Is there a north arrow?
2. **Scale** – Is a scale bar or scale notation shown?
3. **Property Line** – Is the property line shown and labeled?
4. **Lease Line / Parcel ID** – Shown?
5. **Fence** – Is the perimeter fence shown?
6. **Gate** – Gate location and size shown?
7. **Access Road** – Road width and turnaround radius shown?
8. **Equipment Pad** – Are equipment pad locations shown?
9. **Setback Dimensions** – Dimensions from property line shown?
10. **Racking Callouts** – Racking size and quantity noted?
11. **MV Run / CAB Line** – Are MV cable run and CAB lines shown?
12. **Weather Station** – Weather sensor location noted?
13. **Section View** – Is there a racking section view showing pitch, interrow spacing, height?
14. **Topo Lines** – Are topographic lines with elevation shown?
15. **Setbacks** – Wetland, gas line, wells setbacks shown?

Return a JSON array:
```json
[
  {
    "check": "check_name",
    "found": true/false,
    "evidence": "what you see",
    "severity": "low|medium|high"
  }
]
```
Only return the JSON array.
"""

_GROUNDING_PROMPT = """\
You are a senior electrical PE reviewing the GROUNDING DIAGRAM of a solar PV planset. READ all conductor sizes and VALIDATE against NEC tables.

**TRACE THE FULL GROUNDING CIRCUIT — from PV rack to POI:**

1. **PV Module/Rack Grounding:** Read the EGC size from rack to CAB. Read the EGC from CAB to DCB/inverter.
2. **DC Equipment Grounding:** Does the DC grounding section match the equipment configuration shown on the DC SLD?

3. **Equipment Grounding Conductor (EGC) — VALIDATE per NEC 250.122:**
   Read the OCPD rating protecting each circuit, then verify EGC size:
   - 15A→#14 CU, 20A→#12, 60A→#10, 100A→#8, 200A→#6, 300A→#4, 400A→#3, 500A→#2, 600A→#1, 800A→1/0, 1000A→2/0, 1200A→3/0, 1600A→4/0, 2000A→250kcmil, 2500A→350, 3000A→400, 4000A→500, 5000A→700, 6000A→800.
   - For AL: 15A→#12, 20A→#10, 60A→#8, 100A→#6, 200A→#4, 300A→#2, 400A→#1, 500A→1/0, 600A→2/0, 800A→3/0, 1000A→4/0, 1200A→250, 1600A→350, 2000A→400, 2500A→600, 3000A→600.

4. **Grounding Electrode Conductor (GEC) — VALIDATE per NEC 250.66:**
   Read the largest ungrounded supply conductor size, then verify GEC:
   - CU supply ≤ #2 → GEC = #8 CU. #1 or 1/0 → #6. 2/0 or 3/0 → #4. Over 3/0 through 350 → #2. Over 350 through 600 → 1/0. Over 600 through 1100 → 2/0. Over 1100 → 3/0.
   - AL supply: 1/0 or smaller → #6 AL. 2/0 or 3/0 → #4. 4/0 or 250 → #2. Over 250 through 500 → 1/0. Over 500 through 900 → 3/0. Over 900 through 1750 → 4/0. Over 1750 → 250kcmil.

5. **Main Bonding Jumper — VALIDATE per NEC 250.102(C)(1):**
   Same table as GEC (NEC 250.66) based on largest ungrounded conductor.

6. **Transformer Grounding:**
   - Primary side: Read grounding connection. For delta primary, there should be NO neutral/ground connection on primary (ungrounded).
   - Secondary side: Read grounding connection. For wye secondary, neutral should be solidly grounded with a properly sized GEC.
   - Read grounding transformer details if shown (for delta-wye with high-impedance grounding).

7. **Grounding Ring:** Is a bare copper ground ring shown around equipment pad? Read conductor size (typically #2 or #4/0 bare CU). Verify it connects to ground rods.

8. **Ground Rods:** Are ground rods shown? Typical: 5/8" × 8ft copper-clad. Spacing ≥ 2× rod length (16ft).

9. **Code References:** Are NEC article references shown (250.66, 250.122, 250.102)?

Return a JSON array with your validation calculations:
```json
[
  {
    "check": "check_name",
    "status": "Pass|Fail|Needs Review",
    "found": true/false,
    "value": "extracted conductor sizes",
    "evidence": "OCPD=400A → NEC 250.122 requires #3 CU EGC, drawing shows #4 → FAIL (undersized)",
    "severity": "low|medium|high"
  }
]
```
Only return the JSON array.
"""

_ELECTRICAL_SHEET_PROMPT = """\
You are a senior electrical PE reviewing the ELECTRICAL SCHEDULE / CALCULATION SHEET (E-300 series) of a solar PV planset. READ every number in the tables and VALIDATE the math.

This is the most critical sheet in the planset — wrong values here mean wrong cable sizes in the field.

**1. PV SYSTEM PARAMETERS — Read and Validate:**
- Read: Module Voc, Vmp, Isc, Imp, Pmax, temperature coefficients (%/°C for Voc, Isc).
- Read: Inverter max Vdc, MPPT range (Vmin to Vmax), max Idc per input, max Isc per input.
- Read: Modules per string, strings per inverter.
- CALCULATE: Voc_string = modules × Voc. Check this against inverter max Vdc.
- CALCULATE: Total DC kW = modules × strings × Pmax / 1000. Compare to the total shown.
- CALCULATE: DC/AC ratio = Total DC kW / Total AC kW. Compare to the value shown.

**2. STRINGS PER RACEWAY — Read and Validate:**
- Read the Isc value used (should be SAM Isc if available, not just STC Isc).
- Read the 1.25 continuous use factor. It should be applied ONCE (not 1.25 × 1.25 double-derated).
- Read Adjusted Isc = Isc × 1.25.
- Read temperature correction factor from the table. Verify against NEC 310.15(B)(1):
  At 30°C ambient: factor=1.00 for 75°C wire, 1.00 for 90°C wire.
  At 35°C: 0.94 for 75°C, 0.96 for 90°C.
  At 40°C: 0.88 for 75°C, 0.91 for 90°C.
  At 45°C: 0.82 for 75°C, 0.87 for 90°C.
- Read conduit fill derate factor. For 4-6 conductors: 0.80. For 7-9: 0.70. For 10-20: 0.50. For 21-30: 0.45.
- CALCULATE: Final NEC Ampacity = Base ampacity × temp factor × fill factor. This must be ≥ Adjusted Isc.
- Read max strings per conduit for each case shown. Verify the math.

**3. DC CIRCUIT SCHEDULE — Read Every Row:**
- For each circuit row, read: circuit name, distance (ft), Isc, conductor size, material, insulation, conduit size, conduit fill %.
- Spot-check: Does cable ampacity at 90°C (for PV wire) ≥ 1.25 × Isc after derating?
  PV wire at 90°C: #14=25A, #12=30A, #10=40A, #8=55A.
- Verify: 2 current-carrying conductors for DC (+ and -).
- Verify: Ground wire is NOT upsized beyond NEC 250.122 minimum for DC circuits.
- Read conduit fill %. Flag if > 40%.

**4. AC CIRCUIT SCHEDULE — Read Every Row and Validate:**
- For each circuit row, read: circuit name, voltage, FLA, OCPD size, conductor size, # sets, # wires/set, material, insulation, GND size, conduit size, fill %.
- VALIDATE FLA: FLA = kVA / (√3 × voltage) for 3-phase. Check the shown value.
- VALIDATE OCPD:
  If ≤ 800A: NEC 240.4(B) allows next standard size up (15,20,25,30,35,40,45,50,60,70,80,90,100,110,125,150,175,200,225,250,300,350,400,450,500,600,700,800).
  If > 800A: NEC 240.4(C) requires cable ampacity ≥ OCPD rating.
- VALIDATE cable: Use 75°C column (NEC 310.16) for termination rating.
  CU 75°C: #8=50, #6=65, #4=85, #3=100, #2=115, #1=130, 1/0=150, 2/0=175, 3/0=200, 4/0=230, 250=255, 300=285, 350=310, 400=335, 500=380, 600=420, 750=475.
  Cable ampacity per set = table value × temp_derate × fill_derate. This × (# sets) must be ≥ 1.25 × FLA.
- VALIDATE wires/set: 3-phase circuit = 3 or 4 wires/set (3 phase + neutral if needed). Single phase = 2 wires.
- VALIDATE GND: Per NEC 250.122, EGC sized for OCPD rating. For parallel conductors, each set gets the same EGC size. If phase conductors are upsized above minimum, GND must be proportionally upsized.
- Read conduit fill %. Flag if > 40%.

**5. MV CIRCUIT SCHEDULE — Read and Validate:**
- Read: FLA, cable size, material, insulation class, # conductors.
- CALCULATE: 1.25 × FLA. This must be < cable ampacity rating.
  MV cable ampacities (15kV, direct buried, 90°C): AL 1/0=135A, 4/0=200A, 250=225A, 350=275A, 500=340A.
- Verify: EGC is a separate conductor (NOT the cable concentric neutral).
- Read conduit fill %. Flag if > 40%.

**6. VOLTAGE DROP — Read and Validate:**
- Read DC voltage drop %, AC voltage drop %, MV voltage drop %.
- Read total AC+DC voltage drop. Compare to client criteria (typically 3% max).
- Flag if any section shows 0% or is blank (not calculated).
- Flag if total exceeds client criteria.

For EVERY finding, you MUST:
1. State the EXACT location: table name, row label/number, column header.
2. Quote the EXACT value you read from the drawing.
3. Show your validation calculation step by step.
4. State Pass or Fail with a clear one-line conclusion.

IMPORTANT FORMATTING RULES:
- Each finding must be a SEPARATE JSON object (do not combine multiple issues into one).
- The "location" field must pinpoint WHERE the issue is (e.g. "AC Schedule, Row 3 (INV-1 to SWBD), FLA column").
- The "evidence" field must be a concise sentence: what you read, what you calculated, and whether it passes.
- Do NOT dump raw JSON objects into the evidence — write it as readable text.
- Only flag as "Fail" when the math is clearly wrong. If you can't read a value clearly, use "Needs Review".

Return a JSON array:
```json
[
  {
    "check": "ac_schedule_fla_validation",
    "status": "Pass",
    "location": "AC Schedule, Row 2 (INV-1 to SWBD-1), FLA column",
    "evidence": "FLA shown as 380A. Calculated: 275kVA / (480V × 1.732) = 331A. Shown value 380A appears to include 1.15 oversize factor — acceptable.",
    "severity": "low"
  },
  {
    "check": "dc_schedule_ampacity",
    "status": "Fail",
    "location": "DC Schedule, Row CB10-HAR1, Ampacity column",
    "evidence": "Isc = 14.7A, 1.25 × 14.7 = 18.4A required. #10 AWG PV wire at 90°C = 40A, derated by 0.91 (temp) × 0.50 (10 conductors) = 18.2A. 18.2A < 18.4A — FAILS by 0.2A.",
    "severity": "high"
  }
]
```
Only return the JSON array.
"""

_ELEVATION_PROMPT = """\
You are a QC engineer reviewing ELEVATION DETAILS of a solar PV planset.

Check:
1. **Pile Spacing** – Is pile spacing dimensioned?
2. **Pile Type** – Is the pile type noted (driven steel, helical, etc.)?
3. **Pile Depth** – Is minimum pile embedment depth shown?
4. **Clearance from Grade** – Is the minimum module clearance from grade shown?
5. **Equipment Clearances** – Are front, side, and back clearances for inverters/equipment shown?
6. **Clearances vs NEC 110.26(A)(1)** – READ the front/side/rear clearance
   dimensions shown and VALIDATE them against the NEC working-space table
   for the system voltage. For 0–150V to ground: Condition 1 = 3 ft,
   Cond 2 = 3 ft, Cond 3 = 3 ft. For 151–600V: 3/3.5/4 ft.
   For 601–2500V: 3/4/5 ft. For 2501–9000V: 4/5/6 ft. For 9001–25000V: 5/6/9 ft.
   Flag "Fail" with the required vs shown value if below minimum. DO NOT
   check for the presence of an NEC table on the drawing — the NEC tables
   are a validation REFERENCE, not required planset content.
7. **Sweep/Bend Distance** – Are conduit sweep or bend distances noted?
8. **CAB Details** – CAB spacing, sag, and clearance from grade shown?
9. **Weather Sensors** – Are weather sensor mounting locations shown?

Return a JSON array:
```json
[
  {
    "check": "check_name",
    "found": true/false,
    "value": "extracted value if applicable",
    "evidence": "details",
    "severity": "low|medium|high"
  }
]
```
Only return the JSON array.
"""

_FEEDER_PLAN_PROMPT = """\
You are a QC engineer reviewing the AC/DC FEEDER PLAN of a solar PV planset.

Check:
1. **Scale and North Arrow** – Present?
2. **Legend** – Does the legend match the cable types used on the plan?
3. **Circuit Arrows** – Are arrows shown indicating circuit count / direction?
4. **Trench Reference** – Is there a reference to trenching detail sheets?
5. **CAB Fill Reference** – Is worst-case CAB fill identified?
6. **DCB/Inverter Labels** – Are all DCBs and inverters numbered/labeled?
7. **Tracker Motor Cables** – Are cables for tracker motors shown (if applicable)?
8. **Equipment Pads** – Are all equipment pad locations shown?
9. **Fence and Road** – Are fence line and access road shown?

Return a JSON array:
```json
[
  {
    "check": "check_name",
    "found": true/false,
    "evidence": "details",
    "severity": "low|medium|high"
  }
]
```
Only return the JSON array.
"""

_POLE_LINEUP_PROMPT = """\
You are a QC engineer reviewing the POLE LINE UP drawing of a solar PV planset.

Check:
1. **North Arrow and Scale** – Present?
2. **Assumed Note** – If the pole lineup is assumed, is there a note stating that?
3. **Pole Names/Numbers** – Are poles named and numbered?
4. **POI Label** – Is the Point of Interconnection labeled with utility name, voltage, feeder grounding, pole number, feeder number?
5. **Fault Current** – Is fault current shown (ultimate or exact)?
6. **Dimensions** – Distance between poles shown? Distance from access road/fence/gate?
7. **Underground MV Run** – Is the underground MV cable run from the equipment area shown?
8. **Overhead Lines** – Are new vs existing overhead lines distinguished?
9. **Delineation Line** – Is there a delineation line for client vs utility scope?
10. **Legend** – Is the correct legend table shown?

Return a JSON array:
```json
[
  {
    "check": "check_name",
    "found": true/false,
    "value": "extracted value if applicable",
    "evidence": "details",
    "severity": "low|medium|high"
  }
]
```
Only return the JSON array.
"""

_EQUIPMENT_LIST_PROMPT = """\
You are a QC engineer reviewing the ENGINEERED EQUIPMENT LIST of a solar PV planset.

Check:
1. **Equipment Names** – Are all equipment items named with a consistent naming format?
2. **Quantities** – Are quantities shown for each equipment item?
3. **Manufacturers** – Is the manufacturer listed for each item?
4. **Models** – Is the model number listed for each item?
5. **Ratings** – Are electrical ratings shown (voltage, current, kVA, etc.)?
6. **Completeness** – List all equipment categories you can find (inverters, transformers, switchgear, panels, cables, racking, etc.)

**IMPORTANT — READ and EXTRACT these specific values from the equipment list:**
7. **Inverter kVA rating** – Read the exact kVA value shown for the inverter
8. **Inverter kW rating** – Read the exact kW value shown for the inverter
9. **Transformer kVA rating** – Read the exact kVA value shown for the transformer
10. **Recloser specs** – Make, continuous amps, interrupting kA

These values will be cross-checked against the SLD, system info table, and datasheets. \
If any value is missing from the equipment list, flag it.

Return a JSON array:
```json
[
  {
    "check": "check_name",
    "status": "Pass|Fail|Needs Review",
    "value": "what you found",
    "evidence": "details",
    "severity": "low|medium|high"
  }
]
```
Only return the JSON array.
"""

_RELAY_SETTINGS_PROMPT = """\
You are a senior protection engineer reviewing the RELAY AND INVERTER SETTINGS sheet of a solar PV planset. READ every setting value and VALIDATE against IEEE 1547-2018.

**STEP 1 — Read Header Values:**
1. Read Line-to-Line Voltage shown. Read Line-to-Ground voltage. Verify L-G = L-L / √3.
2. Read VT Ratio. For Tavrida recloser, default should be 234.5 (for 34.5kV) or appropriate for system voltage.
3. Read CT Ratio. Default 300:1 if no submittal. Verify primary amps makes sense for the system FLA.
4. Read AC MW and AC MVA shown. Verify AC MVA matches SLD transformer rating.
5. Read FLA shown. CALCULATE: FLA = MVA × 1000 / (√3 × kV_L-L). Compare to shown value.
6. Read Operating Power Factor (should be 1.0 absorbing/consuming for most solar).

**STEP 2 — Recloser Settings — Read and Validate against IEEE 1547:**

Read each trip function and compare to IEEE 1547 Category I defaults:

| Function | Device # | Category I Setting | Clearing Time |
|----------|----------|-------------------|---------------|
| OV2 | 59-2 | 1.20 p.u. | 0.16s |
| OV1 | 59-1 | 1.10 p.u. | 2.0s |
| UV1 | 27-1 | 0.70 p.u. | 2.0s |
| UV2 | 27-2 | 0.45 p.u. | 0.16s |
| OF2 | 81O-2 | 62.0 Hz | 0.16s |
| OF1 | 81O-1 | 61.2 Hz | 300.0s |
| UF2 | 81U-2 | 58.5 Hz | 300.0s |
| UF1 | 81U-1 | 56.5 Hz | 0.16s |

For each setting:
- Read the p.u. voltage or Hz shown.
- Read the clearing time shown.
- CALCULATE the actual voltage: V_actual = p.u. × V_L-N (for recloser, referenced to VT secondary).
- Compare to IEEE 1547 defaults. Flag any deviation.

**STEP 3 — Inverter Settings — Read and Validate:**

Read inverter AC Output L-L voltage and L-N voltage. Verify L-N = L-L / √3.
Read each inverter trip setting and validate against IEEE 1547 Category I:

| Function | Device # | Setting (p.u. of nominal) | Voltage (V) | Clearing Time |
|----------|----------|--------------------------|-------------|---------------|
| OV2 | 59-2 | 1.20 | = 1.20 × V_L-N | 0.16s |
| OV1 | 59-1 | 1.10 | = 1.10 × V_L-N | 2.0s |
| UV1 | 27-1 | 0.70 | = 0.70 × V_L-N | 2.0s |
| UV2 | 27-2 | 0.45 | = 0.45 × V_L-N | 0.16s |
| OF2 | 81O-2 | 62.0 Hz | — | 0.16s |
| OF1 | 81O-1 | 61.2 Hz | — | 300.0s |
| UF2 | 81U-2 | 58.5 Hz | — | 300.0s |
| UF1 | 81U-1 | 56.5 Hz | — | 0.16s |

CALCULATE each voltage value and compare to what's shown. For example, if inverter L-N = 277V, then OV2 voltage = 1.20 × 277 = 332.4V.

**STEP 4 — Non-IEEE Functions (if present):**
- Read Inst OC (50P) pickup in amps (primary and secondary).
- Read Time OC (51P) curve type, time dial, pickup.
- Read Ground OC (51G/51N) settings.
- Are coordination curve plots pasted into the spreadsheet?
- If no electrical studies have been conducted, this section should be hidden/blank.

**STEP 5 — Return-to-Service Settings (if shown):**
- Read Min V and Max V return-to-service settings (typically 0.95 and 1.05 p.u.).
- Read Min F and Max F (typically 59.5 Hz and 60.5 Hz).
- Read reconnect time (typically 300s).

Return a JSON array with your calculations:
```json
[
  {
    "check": "check_name",
    "status": "Pass|Fail|Needs Review",
    "value": "extracted setting value",
    "evidence": "OV2: shown 1.20 p.u. = 332.4V, clearing 0.16s — matches IEEE 1547 Cat I → Pass",
    "severity": "low|medium|high"
  }
]
```
Only return the JSON array.
"""

_AUX_SLD_PROMPT = """\
You are a QC engineer reviewing the AUXILIARY SINGLE LINE DIAGRAM (AUX SLD) or
aux-power / aux-loads schedule of a solar PV planset.

For EACH numbered check: the item is REQUIRED on a complete aux power
design. If the item is clearly visible on the sheet → Pass with the
value you read. If the item is NOT shown / missing / not called out →
Fail with evidence "Not shown on this sheet". Only use Needs Review
when the item is partially shown or ambiguous. Do not Pass by default.

PASS CRITERION — READ CAREFULLY:
You may ONLY emit status="Pass" when the item is explicitly, clearly
visible on the sheet with a readable value. If the item is "not shown"
/ "not indicated" / "missing" / "implied" / "ambiguous" → status="Fail"
(or "Needs Review" if truly ambiguous). Never emit Pass with a "value"
field that contains words like "not shown", "not indicated", "missing",
"implied", "N/A" — those are Fails. The only legitimate N/A Pass is for
rules explicitly marked "fixed-tilt → Pass (N/A)" in the check list,
and only when the system is actually fixed-tilt.

OUTPUT-LENGTH RULE: for valid status="Pass" findings, emit ONLY the
minimal fields — { "check": "...", "status": "Pass", "value": "what you read (1 short phrase)" }.
Omit "location", "evidence", and "severity" on Pass findings. Reviewers
don't need provenance text when the check genuinely passes. For Fail /
Needs Review, keep the full "location" + "evidence" + "severity".

1. **Aux Transformer** – An aux transformer MUST be shown with its
   rating (kVA, voltage). Missing → Fail.
2. **Single Phase Check** – If the aux transformer is single phase, it
   MUST NOT show WYE/DELTA winding. Wrong winding → Fail.
3. **Aux Power Phase** – Aux power phase (3-phase or 1-phase) MUST be
   stated. Missing → Fail.
4. **Aux Panel** – An aux panel / panelboard MUST be shown with its
   rating. Missing → Fail.

**Loads — each of the following MUST appear as a labeled load on the
aux SLD / aux loads schedule. If not shown → Fail:**
5. **DAS Equipment** – Data acquisition system as a load.
6. **Data Controller** – Data controller as a separate load.
7. **Tracker Controller** – If this is a tracker system, tracker
   controller must appear. If fixed-tilt, emit Pass (N/A).
8. **Outlets** – GFCI outlet circuit.
9. **UPS** – Uninterruptible power supply circuit.
10. **Weather Station** – Weather station power circuit.

**Motors (tracker systems only; fixed-tilt → Pass N/A):**
11. **Motor Power** – Motor power (W or A per motor) MUST be specified.
12. **Motor Configuration** – 1 motor per tracker or 1 motor per array
    MUST be clear. Ambiguous → Fail.
13. **Motor Daisy Chain** – Daisy-chain topology MUST be shown.

**Equipment:**
14. **Breaker Ratings** – All breaker ratings MUST be shown.
15. **Cable and Conduit** – Cable sizes and conduit sizes MUST be shown.
16. **CAB Insulation** – For cables in CAB, motor wire MUST be sunlight
    resistant (if motors aren't in CAB, Pass N/A).

Return a JSON array:
```json
[
  {
    "check": "check_name",
    "status": "Pass|Fail|Needs Review",
    "value": "extracted value",
    "evidence": "details",
    "severity": "low|medium|high"
  }
]
```
Only return the JSON array.
"""

_COMM_DIAGRAM_PROMPT = """\
You are a QC engineer reviewing the COMMUNICATION DIAGRAM of a solar PV planset.

Check:
1. **DAS Equipment** – Is DAS (data acquisition system) equipment shown?
2. **Inverter Daisy Chains** – Are inverters shown in daisy chain configuration?
3. **Equipment Count** – Does the number of equipment shown match the number of pads?
4. **Weather Sensors** – Are all weather sensors accounted for and shown?
5. **UPS** – Is a UPS shown for the DAS system?
6. **DAS to POI Connection** – Does DAS connect to POI through fiber optic?
7. **POI to Recloser** – Does POI connect to recloser through ethernet?
8. **Line Types** – Are different line types used to distinguish fiber, ethernet, RS485, etc.?
9. **Tracker Communication** – If tracker system, is tracker communication network shown?

Return a JSON array:
```json
[
  {
    "check": "check_name",
    "status": "Pass|Fail|Needs Review",
    "found": true/false,
    "evidence": "details",
    "severity": "low|medium|high"
  }
]
```
Only return the JSON array.
"""

_TRENCHING_PROMPT = """\
You are a QC engineer reviewing the TRENCHING DETAILS of a solar PV planset.

Check:
1. **Trench Depth vs Voltage** – Is the trench depth appropriate for the circuit voltage? (LV: 24" min, MV: 36" min per NEC 300.5)
2. **Conduit Spacing** – Is conduit spacing dimensioned? Does it match the feeder plan count?
3. **Direct Buried vs Conduit** – Is it clear which cables are direct buried vs in conduit?
4. **Dimensions** – Are all trench dimensions clearly shown (width, depth, spacing)?
5. **Worst Case** – Does this appear to show the worst-case trench configuration (not just an easy case)?
6. **Trench Headers** – Do the trench section headers/labels match the feeder plan references?
7. **GND Wire** – Is a ground wire shown in the conduit/trench?
8. **Comms Cable** – Are communication cables shown in the trench?
9. **Ampacity Notes** – Are there notes about ampacity derating for burial depth and soil conditions?
10. **Sheet Reference** – Is there a reference back to the feeder plan sheet?

Return a JSON array:
```json
[
  {
    "check": "check_name",
    "found": true/false,
    "evidence": "details",
    "severity": "low|medium|high"
  }
]
```
Only return the JSON array.
"""

_CAB_DETAILS_PROMPT = """\
You are a QC engineer reviewing the CAB (Cable Aerial Bridge) or CABLE HANGER DETAILS of a solar PV planset.

Check:
1. **CAB Section Weight** – Is the CAB section weight calculated or referenced?
2. **SAG** – Is the allowable cable sag specified?
3. **CAB Section Fill** – Is the cable tray/hanger fill percentage calculated?
4. **Conductor Grouping** – Are conductors properly grouped by voltage class (DC vs AC vs Comms)?
5. **Standard Details** – Are standard mounting/hanger details shown?
6. **Pile Depth** – Is the CAB support pile embedment depth shown?
7. **Arrangement** – Does the CAB cross-section arrangement appear logical?

Return a JSON array:
```json
[
  {
    "check": "check_name",
    "found": true/false,
    "value": "extracted value if applicable",
    "evidence": "details",
    "severity": "low|medium|high"
  }
]
```
Only return the JSON array.
"""

_LABELS_PROMPT = """\
You are a QC engineer reviewing the LABELS sheet of a solar PV planset.

Check:
1. **NEC References** – Are NEC code references shown on the labels? Are they correct?
2. **Voltage Values** – Do the voltage values on labels match the electrical calculation sheet?
3. **Current Values** – Do the current values on labels match the calculation sheet?
4. **GOAB Label** – Is the GOAB switch labeled as "Main Service Disconnect"?
5. **Client Info** – Is the client's name and phone number shown on applicable labels?
6. **Warning Labels** – Are appropriate warning/danger labels included (arc flash, PV disconnect, etc.)?

Return a JSON array:
```json
[
  {
    "check": "check_name",
    "status": "Pass|Fail|Needs Review",
    "found": true/false,
    "evidence": "details",
    "severity": "low|medium|high"
  }
]
```
Only return the JSON array.
"""

_ELECTRICAL_SHEET_DEEP_PROMPT = """\
You are an experienced electrical QC engineer reviewing the ELECTRICAL SCHEDULE / CALCULATION SHEETS (E-300 series) of a solar PV planset. This is a CRITICAL review sheet.

Examine ALL tables and calculations visible on these pages. Check the following in detail:

**1. PV System Parameters:**
- Weather data source shown?
- PV module electrical values (Voc, Vmp, Isc, Imp, Pmax, temp coefficients)
- Inverter electrical values (max Vdc, MPPT range, max Idc, max Isc per input)
- All cases shown for modules per string and strings per inverter?
- SAM (System Advisor Model) values for bifacial module if applicable?
- Is stringing OK? Check: Voc at cold temp < max inverter Vdc, Vmp at hot temp within MPPT range

**2. Strings Per Raceway:**
- Adjusted Isc shown (SAM Isc if available)?
- 1.25 derate factor applied (single 1.25, not double-derated)?
- Temperature correction factor applied?
- Conduit fill derate factor applied?
- Free Air and Conduit tables shown if applicable?
- Max strings per conduit for each case?

**3. DC Circuit Schedule:**
- Cable sizing: number of sets, 2 wires for DC, cable size, AL vs CU, cable insulation, GND wire (no upsize for DC)
- Conduit sizing: material, direct buried flag, conduit fill percentage
- Termination rating noted?

**4. AC Circuit Schedule:**
- Voltage and FLA shown for each circuit?
- OCPD sizing: below 800A uses round-up rule, above 800A requires ampacity >= OCPD
- Cable sizing: number of sets (within equipment limit), wires per set (4/3/2 based on phase), wire size at 75°C column, cable insulation > circuit voltage, terminal rating, GND wire upsized for parallel phase conductors
- Cable derate factors applied?
- Conduit fill < 40%?

**5. MV Circuit Schedule:**
- 1.25 × FLA < wire ampacity rating?
- GND wire shown? (No concentric neutral used for EGC)
- Conduit fill < 40%?

**6. Voltage Drop Summary:**
- DC, LV-AC, and MV voltage drop values populated (not blank)?
- Total AC+DC voltage drop within client criteria?

Return a JSON array:
```json
[
  {
    "check": "check_name",
    "status": "Pass|Fail|Needs Review",
    "value": "extracted value if applicable",
    "evidence": "detailed findings",
    "severity": "low|medium|high"
  }
]
```
Only return the JSON array.
"""

_CROSS_SHEET_CONSISTENCY_PROMPT = """\
You are a senior electrical PE performing a CROSS-SHEET CONSISTENCY AUDIT on a solar PV planset. You are shown pages from different sections. READ actual values from EACH page and COMPARE them.

This is the most important QC check — inconsistencies between sheets cause construction errors.

**For EACH value below, READ it from EVERY page where it appears, then compare:**

**1. Inverter:**
- Read inverter make/model from each page. Are they identical?
- Read inverter kVA/kW rating from each page. Do they match?
- Read inverter quantity from each page. Same count everywhere?
- Read inverter AC output voltage from each page.

**2. Transformer:**
- Read transformer kVA from each page.
- Read primary voltage and winding (Delta/Wye) from each page.
- Read secondary voltage and winding from each page.
- Read BIL from each page.
- VALIDATE: XFMR kVA ≥ total inverter kVA. Calculate both and compare.

**3. Cables:**
- Read AC cable size/sets from SLD. Compare to equipment list if shown.
- Read MV cable size from SLD. Compare to other sheets.
- Read DC cable size. Compare across DC SLD and schedules.

**4. Protection/Switchgear:**
- Read breaker/OCPD ratings from each page. Match?
- Read GOAB rating. Is it labeled "Main Service Disconnect" everywhere?
- Read recloser specs (make, continuous A, interrupting kA, BIL) from each page.

**5. POI/Metering:**
- Read POI voltage from each page. Consistent?
- Read CT ratio from each page. Match?
- Read VT ratio. Match?
- Read fault current value if shown.

**6. Pole Names/Numbers:**
- Read each pole name/number from every page. Are they identical across SLD, 3LD, Pole Line Up, and Pole Elevation?
- Read the number of poles shown on each sheet. Same?

**7. System Totals:**
- Read total DC size from each page where shown.
- Read total AC size from each page.
- Read DC/AC ratio if shown.
- Read total number of strings if shown.

For EACH inconsistency: state exactly what value you read on which page.
For consistent values: briefly confirm they match.

Return a JSON array:
```json
[
  {
    "check": "consistency_check_name",
    "status": "Pass|Fail",
    "value": "SLD: 2000kVA, Equipment List: 2500kVA",
    "evidence": "Transformer kVA mismatch: SLD page shows 2000 kVA but Equipment List shows 2500 kVA",
    "severity": "high"
  }
]
```
Only return the JSON array.
"""

_EQUIP_AREA_FEEDER_PROMPT = """\
You are a QC engineer reviewing the EQUIPMENT AREA FEEDER PLAN of a solar PV planset.

Check:
1. **Equipment Faded** – Is equipment properly faded/greyed to show wiring clearly?
2. **Wiring Types** – Are AC, DC, MV, and communication cables all shown?
3. **AUX Rack** – Is the AUX rack/cabinet shown? Check against DAS vendor drawings.
4. **Aux Power** – Is aux power routing shown?
5. **NEC Clearances** – Are working clearances dimensioned per NEC 110.26?
   - 0-150V: 36" front clearance. 151-600V: 36" front. 601V-2500V: 36" front.
   - Width: 30" minimum or width of equipment, whichever is greater.
   - Height: 6.5 feet minimum.
6. **Equipment Clearances** – Are manufacturer-required clearances shown?
7. **Pile Details** – For inverter/aux rack piles: count, spacing, type, depth shown?
8. **Pad Type** – Is it individual or combined pad? Is gravel/material specified?
9. **Inverter Clearances** – Min clearance from side, front, and back dimensioned?
10. **NEC 110.26 validation** – READ the shown clearance dimensions and
    VALIDATE against the NEC 110.26(A)(1) working-space table for the
    system voltage. Flag any dimension below the required minimum with
    "Fail" and evidence "Required: X ft (NEC 110.26, <voltage> band) /
    Shown: Y ft". DO NOT mark this as Fail just because an NEC table is
    not printed on the drawing — the NEC is a validation REFERENCE,
    not required planset content.

Return a JSON array:
```json
[
  {
    "check": "check_name",
    "found": true/false,
    "evidence": "details",
    "severity": "low|medium|high"
  }
]
```
Only return the JSON array.
"""

_COMM_FEEDER_PLAN_PROMPT = """\
You are a QC engineer reviewing the COMMUNICATION FEEDER PLAN of a solar PV planset.

Check:
1. **String Wires Frozen** – Are the string wires and callouts frozen/faded?
2. **PV Racks Faded** – Are the PV racks faded/greyed?
3. **Scale and North Arrow** – Present?
4. **Sensor Names** – Are individual sensor names and locations from DAS listed? READ each sensor name.
5. **Visibility** – Is the plot clear and readable?
6. **Legend** – Does the legend match the communication cables used?
7. **Cable Types** – Are fiber, ethernet, RS485, and other comm types distinguishable?
8. **Weather Sensors** – Are all weather sensor locations shown?
9. **DAS Equipment** – Is the DAS equipment location shown?

Return a JSON array:
```json
[
  {
    "check": "check_name",
    "found": true/false,
    "evidence": "details",
    "severity": "low|medium|high"
  }
]
```
Only return the JSON array.
"""

_PAD_SLAB_PROMPT = """\
You are a QC engineer reviewing PAD / SLAB DETAILS of a solar PV planset.

Check:
1. **Rebar Schedule** – Is a rebar schedule shown? What sizes and spacing?
2. **Pad Dimensions** – Are pad dimensions clearly shown (length, width, thickness)?
3. **Concrete Specs** – Is concrete strength (PSI) specified?
4. **Edge Details** – Are edge/form details shown?
5. **Anchor Bolts** – Are anchor bolt patterns and sizes shown?
6. **Grounding Connection** – Is a grounding connection to the pad shown?
7. **Drainage** – Is drainage/grading around the pad addressed?

Return a JSON array:
```json
[
  {
    "check": "check_name",
    "found": true/false,
    "value": "extracted value if applicable",
    "evidence": "details",
    "severity": "low|medium|high"
  }
]
```
Only return the JSON array.
"""

_POLE_DETAILS_PROMPT = """\
You are a QC engineer reviewing POLE DETAILS / POLE ELEVATION of a solar PV planset.

Check:
1. **Latest Template** – Does this appear to use a standard/current template?
2. **Pole Names** – Are pole names consistent with the SLD, 3LD, and Pole Line Up sheets? READ each pole name.
3. **Configuration** – Does the pole configuration match the SLD (equipment mounted, conductors, insulators)?
4. **Unnecessary Details** – Are there any unnecessary or outdated details that should be removed?
5. **Equipment Callouts** – Are all mounted equipment items called out (recloser, fuses, arrestors, disconnects, metering)?
6. **Conductor Types** – Are overhead and underground conductor types shown?
7. **Heights/Clearances** – Are pole heights and conductor clearances dimensioned?

Return a JSON array:
```json
[
  {
    "check": "check_name",
    "status": "Pass|Fail|Needs Review",
    "value": "extracted value",
    "evidence": "details",
    "severity": "low|medium|high"
  }
]
```
Only return the JSON array.
"""

_GROUNDING_PLAN_PROMPT = """\
You are a QC engineer reviewing the OVERALL SITE GROUNDING PLAN of a solar PV planset.

Check:
1. **PV Racks Faded** – Are the PV racks faded/greyed for clarity?
2. **Consistency with Grounding SLD** – Does the site grounding plan match the grounding SLD?
3. **Ground Ring** – Is a continuous ground ring shown around the site/equipment area?
4. **Ground Rod Locations** – Are ground rod locations shown with spacing?
5. **EGC Routing** – Is the equipment grounding conductor routing shown from arrays to equipment area?
6. **Latest Template** – Does this use the latest standard template?
7. **Conductor Sizes** – Are grounding conductor sizes called out?
8. **Test Wells** – Are grounding test well locations shown?

Return a JSON array:
```json
[
  {
    "check": "check_name",
    "found": true/false,
    "evidence": "details",
    "severity": "low|medium|high"
  }
]
```
Only return the JSON array.
"""

_PVSYST_PROMPT = """\
You are a QC engineer reviewing PVSyst simulation parameters for a solar PV planset.

Check ALL of the following against the planset and standard Castillo defaults:

**1. Project Info:**
- Location matches project site?
- System Type: Fixed Tilt or Tracker — matches the planset?
- DC & AC capacities match the planset system information table?

**2. Equipment:**
- Module type and count — matches planset?
- Inverter type and count — matches planset?

**3. Losses — READ and VALIDATE each value:**
- Soiling Loss: Should be 3% (default)
- Thermal Loss: Uc = 25, Uv = 1.2 (VALIDATE these exact values)
- Wiring Loss: Should reference E-300 sheet in planset (default total ~1.5%)
- Albedo: Should use SolarAnywhere data or get from SAM
- Rack Height: Check Elevation Detail (default 1.8m if not provided)
- Other Losses: LID, quality, mismatch — use PVSyst defaults if no data

**4. Shading & Simulation:**
- Near Shading: "Fast" method with 80% electrical effect (except First Solar modules)
- Is the shading model appropriate for the site?

**5. Advanced:**
- Backtracking: MUST be enabled if using trackers
- Bifacial: MUST be enabled if using bifacial modules

Return a JSON array:
```json
[
  {
    "check": "check_name",
    "status": "Pass|Fail|Needs Review",
    "value": "extracted value",
    "evidence": "Expected: X, Found: Y — reason for status",
    "severity": "low|medium|high"
  }
]
```
Only return the JSON array.
"""

# ---------------------------------------------------------------------------
# Individual check functions
# ---------------------------------------------------------------------------


def _gemini_page_check(
    doc: fitz.Document,
    page_number: int,
    prompt: str,
    run_id: str,
    run_dir: Path,
    category: str,
    item_key_prefix: str,
    default_title: str,
    deep: bool = False,
) -> list[dict]:
    """Send a page to Gemini with *prompt* and convert response to issues."""
    from .gemini_client import analyze_page_image

    image_bytes = render_page_to_bytes(doc, page_number)
    raw = analyze_page_image(image_bytes, prompt, deep=deep)
    findings = _extract_json(raw)

    issues: list[dict] = []
    seen_keys: set[str] = set()
    # Findings that came back with status Fail/NR but no usable bbox after
    # text search + AI-supplied bbox. Each entry remembers what's needed to
    # re-render its issue once the rescue pass returns a location.
    rescue_pending: list[dict] = []
    for i, finding in enumerate(findings):
        check_name = finding.get("check") or finding.get(
            "field") or f"item_{i}"
        status_raw = finding.get("status", "")
        found = finding.get("found")

        # Determine status
        if status_raw in ("Pass", "Fail", "Needs Review"):
            status = status_raw
        elif found is True:
            status = "Pass"
        elif found is False:
            status = "Fail"
        else:
            status = "Needs Review"

        severity = finding.get("severity", "medium")
        if severity not in ("low", "medium", "high"):
            severity = "medium"

        location = finding.get("location") or ""
        value = finding.get("value") or ""
        evidence = finding.get("evidence") or finding.get("notes") or ""

        # Build structured evidence text
        parts: list[str] = []
        if location:
            parts.append(f"Location: {location}")
        if evidence:
            parts.append(evidence if isinstance(evidence, str) else str(evidence))
        if value and isinstance(value, str):
            parts.append(f"Value: {value}")
        full_evidence = " | ".join(parts) if parts else ""

        # Demote evidence-less "Needs Review" findings to "Deferred" so they
        # no longer count as EXTRA noise in the regression scorecard. An NR
        # with no location/value/notes is unreviewable anyway.
        if status == "Needs Review" and not full_evidence:
            status = "Deferred"
            full_evidence = (
                "Deferred: Gemini flagged a potential issue but returned no "
                "location, value, or evidence text — unreviewable without a "
                "second look."
            )

        # Avoid the double-prefix rule_key bug: if the model already emitted a
        # fully-qualified check name (e.g. "gen_placeholders") that starts with
        # the category prefix, use it as-is.
        if check_name.startswith(item_key_prefix):
            item_key = check_name
        else:
            item_key = f"{item_key_prefix}_{check_name}"

        # Per-call dedup: Gemini sometimes emits the same finding multiple
        # times in a single response (e.g. 3x "gen_placeholders" on one page).
        if item_key in seen_keys:
            continue
        seen_keys.add(item_key)

        title = _pretty_title_for(check_name)
        issue_id = str(uuid.uuid4())

        # Generate page preview for Fail / Needs Review items. Highlight every
        # literal text excerpt the model returned so the reviewer can see the
        # exact callouts that triggered the finding.
        snippet_path, preview_path, bbox_dict = None, None, None
        needs_rescue = False
        if status in ("Fail", "Needs Review"):
            hints = _extract_location_hints(finding)
            # AI-supplied normalized bbox — used as ``fallback_bbox`` so the
            # renderer prefers literal-text matches but always has a focused
            # region to draw when text search fails (paraphrased excerpts,
            # scanned PDFs, missing-item findings).
            ai_bbox = parse_ai_bbox(
                finding.get("location_bbox_norm")
                or finding.get("location_bbox"),
                doc[page_number - 1],
            )
            if hints or ai_bbox:
                snippet_path, preview_path, bbox_dict = render_issue_artifacts(
                    doc, issue_id, page_number, run_dir,
                    target_texts=hints,
                    fallback_bbox=ai_bbox,
                )
            if bbox_dict is None:
                # Mark for second-pass deep-model rescue. Don't render the
                # full-page preview yet — if the rescue succeeds, we want
                # the focused highlight rendered instead.
                needs_rescue = True

        # Supporting-document citation. The AI emits these only when the
        # finding's evidence actually came from one of the uploaded
        # supporting docs (CESIR, BOD, submittal, etc.) — see global
        # instruction 10b. Stored alongside the issue so the UI can render
        # a citation panel.
        src_filename = finding.get("source_doc_filename")
        src_page = finding.get("source_doc_page")
        src_excerpt = finding.get("source_doc_excerpt")
        try:
            src_page_int = int(src_page) if src_page not in (None, "") else None
        except (TypeError, ValueError):
            src_page_int = None

        new_issue = make_issue(
            run_id=run_id,
            item_key=item_key,
            category=category,
            title=title,
            description=f"[AI] {default_title}: {title}",
            status=status,
            severity=severity,
            page_number=page_number,
            evidence=full_evidence,
            confidence=0.72,
            snippet_path=snippet_path,
            page_preview_path=preview_path,
            bbox=bbox_dict,
            source_doc_filename=src_filename if isinstance(src_filename, str) else None,
            source_doc_page=src_page_int,
            source_doc_excerpt=src_excerpt if isinstance(src_excerpt, str) else None,
        )
        issues.append(new_issue)
        if needs_rescue:
            rescue_pending.append({
                "id": new_issue["id"],
                "issue_idx": len(issues) - 1,
                "issue_id_str": new_issue["id"],
                "check": check_name,
                "status": status,
                "location": location,
                "value": value if isinstance(value, str) else "",
                "evidence": evidence if isinstance(evidence, str) else "",
                "hints": hints if status in ("Fail", "Needs Review") else [],
            })

    # Second-pass bbox rescue. One deep-model call per page, batching all
    # findings that came back without a usable bbox. Re-renders the issue
    # artifacts and patches the issue records in place.
    if rescue_pending:
        rescued = _rescue_missing_bboxes(doc, page_number, rescue_pending)
        for entry in rescue_pending:
            issue_idx = entry["issue_idx"]
            issue = issues[issue_idx]
            recovered = rescued.get(entry["id"])
            if recovered:
                sp, pp, bd = render_issue_artifacts(
                    doc, issue["id"], page_number, run_dir,
                    target_texts=entry["hints"],
                    fallback_bbox=recovered["bbox"],
                )
                issue["snippet_path"] = sp
                issue["page_preview_path"] = pp
                issue["bbox"] = bd
            elif issue["page_preview_path"] is None:
                # Rescue couldn't place this finding — still render a plain
                # full-page preview so the reviewer at least gets the page.
                _, pp = render_page_preview(
                    doc, page_number, issue["id"], run_dir,
                )
                issue["page_preview_path"] = pp

    return issues


def _gemini_multi_page_check(
    doc: fitz.Document,
    page_numbers: list[int],
    prompt: str,
    run_id: str,
    run_dir: Path,
    category: str,
    item_key_prefix: str,
    default_title: str,
    deep: bool = False,
) -> list[dict]:
    """Send multiple pages to Gemini in one call."""
    from .gemini_client import analyze_multiple_images

    images = []
    # Use lower zoom for multi-page calls to reduce payload size and latency
    for pn in page_numbers:
        images.append(render_page_to_bytes(doc, pn, zoom=1.5))

    # Tell the model which image index goes with which finding so we can
    # attribute highlights back to the correct page.
    multi_page_suffix = (
        "\n\nMULTI-PAGE INSTRUCTIONS:\n"
        "The images above are provided in order. For EVERY finding, include "
        "a \"page_index\" field (0-based integer) indicating which image the "
        "issue was found on. Also include the \"location_text\" / "
        "\"location_texts\" fields as described above."
    )
    raw = analyze_multiple_images(images, prompt + multi_page_suffix, deep=deep)
    findings = _extract_json(raw)

    issues: list[dict] = []
    seen_keys: set[str] = set()
    # Multi-page rescue is bucketed by page number — one Gemini call per page
    # that has any rescue candidates, batching all findings for that page.
    rescue_by_page: dict[int, list[dict]] = {}
    for i, finding in enumerate(findings):
        check_name = finding.get("check") or finding.get(
            "field") or f"item_{i}"
        status_raw = finding.get("status", "")
        found = finding.get("found")

        if status_raw in ("Pass", "Fail", "Needs Review"):
            status = status_raw
        elif found is True:
            status = "Pass"
        elif found is False:
            status = "Fail"
        else:
            status = "Needs Review"

        severity = finding.get("severity", "medium")
        if severity not in ("low", "medium", "high"):
            severity = "medium"

        location = finding.get("location") or ""
        value = finding.get("value") or ""
        evidence = finding.get("evidence") or finding.get("notes") or ""

        # Build structured evidence text
        parts: list[str] = []
        if location:
            parts.append(f"Location: {location}")
        if evidence:
            parts.append(evidence if isinstance(evidence, str) else str(evidence))
        if value and isinstance(value, str):
            parts.append(f"Value: {value}")
        full_evidence = " | ".join(parts) if parts else ""

        # Cross-Sheet-safe NR-without-evidence gate: demote to Deferred so it
        # stops firing as EXTRA noise in the regression scorecard.
        if status == "Needs Review" and not full_evidence:
            status = "Deferred"
            full_evidence = (
                "Deferred: Gemini flagged a potential cross-sheet issue but "
                "returned no location, value, or evidence text — "
                "unreviewable without a second look."
            )

        # Avoid the double-prefix rule_key bug.
        if check_name.startswith(item_key_prefix):
            item_key = check_name
        else:
            item_key = f"{item_key_prefix}_{check_name}"

        # Per-call dedup.
        if item_key in seen_keys:
            continue
        seen_keys.add(item_key)

        title = _pretty_title_for(check_name)
        issue_id = str(uuid.uuid4())

        ref_page = page_numbers[0]
        snippet_path, preview_path, bbox_dict = None, None, None
        if status in ("Fail", "Needs Review"):
            hints = _extract_location_hints(finding)
            # The model sometimes reports which page it saw the issue on (e.g.
            # "Page 2" or "page_index": 1). Try to pick the right page out of
            # the set before falling back to a hint-based search.
            preferred_page = _pick_page_for_finding(finding, page_numbers)
            pages_to_try: list[int] = []
            if preferred_page:
                pages_to_try.append(preferred_page)
            pages_to_try.extend(p for p in page_numbers if p not in pages_to_try)

            if hints:
                for pn in pages_to_try:
                    sp, pp, bb = render_issue_artifacts(
                        doc, issue_id, pn, run_dir, target_texts=hints,
                    )
                    if bb is not None and pp:
                        # Only accept this page if at least one hint actually
                        # matched — render_issue_artifacts always returns
                        # something, so check that we didn't just land on the
                        # footer fallback.
                        page_obj = doc[pn - 1]
                        if any(page_obj.search_for(h) for h in hints):
                            snippet_path, preview_path, bbox_dict = sp, pp, bb
                            ref_page = pn
                            break

            # AI-supplied bbox fallback. Same logic as single-page check —
            # used when no hint matched and (especially) for findings about
            # missing items that have no text to search for. Anchor to the
            # preferred page if the model hinted one, else first page.
            if not preview_path:
                ref_page = preferred_page or page_numbers[0]
                ai_bbox = parse_ai_bbox(
                    finding.get("location_bbox_norm")
                    or finding.get("location_bbox"),
                    doc[ref_page - 1],
                )
                if ai_bbox:
                    snippet_path, preview_path, bbox_dict = render_issue_artifacts(
                        doc, issue_id, ref_page, run_dir,
                        target_texts=hints,
                        fallback_bbox=ai_bbox,
                    )

            # Defer fallback full-page preview — if we'll do a rescue pass,
            # we want the focused highlight rendered instead.

        # Supporting-document citation (see global instruction 10b).
        src_filename = finding.get("source_doc_filename")
        src_page = finding.get("source_doc_page")
        src_excerpt = finding.get("source_doc_excerpt")
        try:
            src_page_int = int(src_page) if src_page not in (None, "") else None
        except (TypeError, ValueError):
            src_page_int = None

        new_issue = make_issue(
            run_id=run_id,
            item_key=item_key,
            category=category,
            title=title,
            description=f"[AI] {default_title}: {title}",
            status=status,
            severity=severity,
            page_number=ref_page,
            evidence=full_evidence,
            confidence=0.72,
            snippet_path=snippet_path,
            page_preview_path=preview_path,
            bbox=bbox_dict,
            source_doc_filename=src_filename if isinstance(src_filename, str) else None,
            source_doc_page=src_page_int,
            source_doc_excerpt=src_excerpt if isinstance(src_excerpt, str) else None,
        )
        issues.append(new_issue)
        if status in ("Fail", "Needs Review") and bbox_dict is None:
            rescue_by_page.setdefault(ref_page, []).append({
                "id": new_issue["id"],
                "issue_idx": len(issues) - 1,
                "check": check_name,
                "status": status,
                "location": location,
                "value": value if isinstance(value, str) else "",
                "evidence": evidence if isinstance(evidence, str) else "",
                "hints": _extract_location_hints(finding),
            })

    # Per-page rescue pass. One deep-model call per page that has any
    # bbox-less Fail/NR findings, batching all findings for that page.
    for page_num, targets in rescue_by_page.items():
        rescued = _rescue_missing_bboxes(doc, page_num, targets)
        for entry in targets:
            issue = issues[entry["issue_idx"]]
            recovered = rescued.get(entry["id"])
            if recovered:
                sp, pp, bd = render_issue_artifacts(
                    doc, issue["id"], page_num, run_dir,
                    target_texts=entry["hints"],
                    fallback_bbox=recovered["bbox"],
                )
                issue["snippet_path"] = sp
                issue["page_preview_path"] = pp
                issue["bbox"] = bd
            elif issue["page_preview_path"] is None:
                _, pp = render_page_preview(
                    doc, page_num, issue["id"], run_dir,
                )
                issue["page_preview_path"] = pp

    return issues


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def _build_project_context(pd: dict | None) -> str:
    """Build a context block from project details for AI prompts."""
    if not pd:
        return ""
    lines = [
        "\n\n=== PROJECT DETAILS (provided by the engineer — compare against the drawing) ===",
    ]
    field_map = [
        ("project_name", "Project Name"),
        ("project_address", "Project Address"),
        ("site_coordinates", "Site Coordinates"),
        ("county", "County"), ("state", "State"),
        ("owner_name", "Owner"), ("epc_name", "EPC"),
        ("eor_name", "EOR"), ("eor_license", "EOR License"),
        ("utility_name", "Utility"), ("is_ngrid", "NGrid Utility"),
        ("poi_voltage", "POI Voltage"),
        ("feeder_grounding", "Feeder Grounding"),
        ("fault_current", "Fault Current"),
        ("module_make", "Module Make"), ("module_model", "Module Model"),
        ("module_stc_watts", "Module STC (W)"),
        ("module_voc", "Module Voc (V)"), ("module_vmp", "Module Vmp (V)"),
        ("module_isc", "Module Isc (A)"), ("module_imp", "Module Imp (A)"),
        ("module_temp_coeff_voc", "Voc Temp Coeff (%/°C)"),
        ("module_temp_coeff_isc", "Isc Temp Coeff (%/°C)"),
        ("is_bifacial", "Bifacial Module"),
        ("string_size", "String Size (modules/string)"),
        ("string_quantity", "Total Strings"),
        ("total_dc_kw", "Total DC (kW)"),
        ("inverter_make", "Inverter Make"), ("inverter_model", "Inverter Model"),
        ("inverter_kva", "Inverter kVA"), ("inverter_kw", "Inverter kW"),
        ("inverter_max_vdc", "Inverter Max Vdc"),
        ("inverter_mppt_range", "MPPT Range"),
        ("inverter_quantity", "Inverter Qty"),
        ("total_ac_kva", "Total AC (kVA)"),
        ("dc_ac_ratio", "DC/AC Ratio"),
        ("racking_type", "Racking Type (fixed/tracker)"),
        ("pitch", "Pitch"), ("interrow_spacing", "Interrow Spacing"),
        ("gcr", "GCR"), ("tilt_angle", "Tilt Angle"), ("azimuth", "Azimuth"),
        ("transformer_kva", "Transformer kVA"),
        ("transformer_primary_voltage", "XFMR Primary Voltage"),
        ("transformer_secondary_voltage", "XFMR Secondary Voltage"),
        ("transformer_winding_config", "XFMR Winding Config"),
        ("transformer_impedance", "XFMR Impedance Z%"),
        ("transformer_bil", "XFMR BIL (kV)"),
        ("recloser_make", "Recloser Make"),
        ("recloser_continuous_a", "Recloser Continuous (A)"),
        ("ct_ratio", "CT Ratio"), ("vt_ratio", "VT Ratio"),
        ("surge_arrestor_mcov", "Surge Arrestor MCOV"),
        ("ieee_category", "IEEE 1547 Category"),
        ("design_temp_low_c", "Design Low Temp (°C)"),
        ("design_temp_high_c", "Design High Temp (°C)"),
        ("ambient_temp_c", "Ambient Temp (°C)"),
        ("special_notes", "Special Notes"),
    ]
    for key, label in field_map:
        val = pd.get(key, "")
        if val:
            lines.append(f"- {label}: {val}")
    lines.append("=== END PROJECT DETAILS ===")
    lines.append(
        "\nIMPORTANT: Compare the values you read on the drawing against "
        "the project details above. Flag any MISMATCH between the project "
        "details and the drawing as a Fail with evidence showing both values."
    )
    return "\n".join(lines)


def run_gemini_checks(
    doc: fitz.Document,
    pages: list[PageInfo],
    page_map: dict[str, PageInfo],
    run_id: str,
    run_dir: Path,
    actual_numbers: list[str],
    project_details: dict | None = None,
    use_deep: bool = True,
    supporting_docs: list[dict] | None = None,
    design_stage: str | None = None,
    progress_cb: Any = None,
    out_timings: list[dict] | None = None,
) -> list[dict]:
    """Run all Gemini-powered deep checks.  Returns a list of issue dicts.

    This is called from ``analyze_pdf`` after the regex-based checks.
    Each section is wrapped in ``_safe_call`` so a single failure
    does not block the rest.

    When ``use_deep`` is False, every call is forced onto the standard
    (mini) model — useful for cheap/fast scans. When True (default), the
    heavy reasoning checks (SLD, DC, TLD, relay, cross-sheet, electrical
    deep, system info) are routed through the configured deep model.

    ``supporting_docs`` is a list of SupportingDoc dicts produced by the
    ``/api/parse-supporting-docs`` endpoint. Their extracted specs are
    appended as an Evidence block to every vision prompt, and a dedicated
    consistency pass compares planset values against them.
    """

    def _deep(want: bool) -> bool:
        return bool(want and use_deep)

    import concurrent.futures as _cf
    import os as _os

    # Parallelize vision checks. Each call is independent and IO-bound (waiting
    # on the OpenAI/Gemini API), so threads work fine. Cap concurrency to stay
    # well inside provider rate limits and avoid local resource pressure.
    _max_workers = int(_os.getenv("AI_PARALLELISM", "6"))
    _pool = _cf.ThreadPoolExecutor(max_workers=_max_workers, thread_name_prefix="qc-vision")
    _futures: list[tuple[Any, str]] = []

    import time as _time

    def _safe_call(func, *args, **kwargs) -> list[dict]:
        """Submit the check to the thread pool and return immediately.

        Returns an empty list so the call site's ``all_issues.extend(...)``
        is a no-op; the real results are drained at the end of this
        function. Call sites do NOT need to change.

        When ``out_timings`` is provided, each dispatched call's wall-time
        (label + duration in seconds + deep flag) is appended to it. Used
        by the analyzer to surface per-category timing in the run summary.
        """
        default_label = args[5] if len(args) > 5 and isinstance(args[5], str) else func.__name__
        label = kwargs.pop("_label", default_label)
        deep_flag = bool(kwargs.get("deep", False))

        if out_timings is None:
            fut = _pool.submit(_safe_gemini_call, func, *args, **kwargs)
        else:
            def _timed(*a, **kw):
                t0 = _time.monotonic()
                try:
                    return _safe_gemini_call(*a, **kw)
                finally:
                    out_timings.append({
                        "label": label,
                        "duration_s": round(_time.monotonic() - t0, 2),
                        "deep": deep_flag,
                    })
            fut = _pool.submit(_timed, func, *args, **kwargs)

        _futures.append((fut, label))
        return []

    all_issues: list[dict] = []

    # Build project context to append to every AI prompt
    _ctx = _build_project_context(project_details)

    # Supporting-document evidence block (engineering source of truth).
    from .supporting_docs import build_evidence_context
    _evidence_ctx = build_evidence_context(supporting_docs)

    _GLOBAL_INSTRUCTIONS = """

CRITICAL FORMATTING RULES FOR ALL RESPONSES:
1. Include a "location" field in EVERY finding — specify the exact table name, row label/number,
   and column header where you found the issue (e.g. "AC Schedule, Row 3 (INV-1 to SWBD), FLA column").
2. Include a "location_text" field in EVERY finding — this is a LITERAL, VERBATIM short text
   excerpt copied EXACTLY as it appears on the drawing near the issue. It MUST be searchable
   via a plain text search on the PDF page.
   GOOD: the specific value that is wrong, e.g. "3,078 kWp", "500 kcmil AL", "FLA = 380A",
     "INV-01", "NOTE 3", "8.5 kA".
   BAD (do NOT use): row/column labels like "Total DC row", descriptive phrases like
     "the third entry", paraphrases, section headings without their value, or anything
     that is not a copy of a string actually printed on the drawing.
   CRITICAL: EVERY finding on the SAME page MUST have a DIFFERENT location_text
   pointing at the specific value that triggered it. Two findings sharing the
   same location_text almost always means one of them is mislocated — rewrite
   the location_text to point at the correct cell/callout.
3. Include "location_texts" (array, 2–5 items) when the finding compares values in
   TWO places (e.g. a mismatch between the system-info table and the datasheet);
   each entry MUST be a distinct literal excerpt. They'll all be highlighted.
4. Write "evidence" as a concise readable sentence — NOT raw JSON or dict dumps.
   Good: "FLA shown as 380A. Calculated: 275kVA / (480V × 1.732) = 331A. Matches within tolerance."
   Bad: "{'fla': 380, 'calc': 331}"
5. Only mark as "Fail" when you are CONFIDENT the value is wrong and can show the math.
   If the image is blurry, values are hard to read, or you're unsure — use "Needs Review", not "Fail".
6. Each distinct issue must be a SEPARATE JSON object — do not combine multiple problems into one finding.
7. For "Pass" items, keep evidence brief (one sentence). Save detail for failures.
8. **NEC / IEEE references are a REFERENCE resource you use to validate values,
   NOT required content on the planset.** If a check description says
   "Check NEC Table X" or "per NEC 310.16", it means: apply that table/rule
   from your engineering knowledge to validate the values shown on the
   drawing. DO NOT mark a finding as "Fail" simply because the planset
   does not reprint an NEC table or does not cite the NEC article number.
   The only exception is the REQUIRED WARNING LABELS check (NEC 690.56,
   705.12, etc.) — those labels MUST physically appear on the equipment
   and on the placards/labels sheet.
9. **Field LABELS vs VALUES.** Planset templates print fixed field labels
   like "PROJECT NAME", "OWNER", "DATE", "EPC", "DESIGNER", "REVISION",
   "SHEET NUMBER", etc. The filled-in value sits next to, above, or
   below each label (often in a bordered cell). NEVER treat the presence
   of a label as a defect or placeholder. NEVER put the label text into
   the "value" field of a finding — put the FILLED-IN value there. Only
   emit a "Fail"/"Needs Review" finding for a field if its value cell is
   truly empty or contains an obvious placeholder (XXXX, TBD, ---, N/A).
   All-caps project/owner names (e.g. "WELLINGTON SOLAR") are valid
   values, not placeholders.

10b. **SUPPORTING-DOC CITATION — required when the finding's evidence
    came from a supporting document.** When you validate a value against
    something in the AVAILABLE EVIDENCE block (CESIR, BOD / tech spec,
    PVSyst, transformer / inverter / module submittal, ampacity calc,
    etc.) — even on a Pass — include three fields so the reviewer can
    audit the citation:

      "source_doc_filename": "<filename>"      — exact filename from the
                                                  AVAILABLE EVIDENCE list
      "source_doc_page":     <integer page>    — 1-based page in that doc
                                                  (omit for single-page docs)
      "source_doc_excerpt":  "<verbatim quote>" — short literal text from
                                                  that doc (3–80 chars), the
                                                  exact text that supports
                                                  this finding

    Example (a Pass that confirms a value matches CESIR):
      "evidence": "Service voltage 480Y/277V matches utility CESIR.",
      "source_doc_filename": "Gonzo IA_20250915 (3).pdf",
      "source_doc_page": 7,
      "source_doc_excerpt": "Service Voltage: 480Y/277V"

    If the finding was determined entirely from the planset (no supporting
    doc consulted), OMIT all three fields. Do NOT make up a filename or
    page number — only cite what you actually used.

10. **BBOX COORDINATES — required on every Fail / Needs Review finding.**
    Include a "location_bbox_norm" field as a 4-element list of integers
    [y0, x0, y1, x1] giving the bounding box of the relevant area on the
    page, normalized to a 0–1000 scale (top-left origin, y grows down,
    x grows right). The image you are looking at may be rendered at any
    DPI, so use NORMALIZED coordinates not pixels.

    For a value that IS present on the page, the bbox should tightly
    enclose the cell, table, or callout containing the value. For a
    value that is MISSING / NOT SHOWN, return the bbox of where it
    SHOULD appear — e.g. the empty cell in the schedule, the empty area
    next to the label that has no value, the section of the SLD where
    the missing equipment symbol should be drawn. When location_text
    cannot be matched on the page text layer (paraphrased excerpt,
    scanned PDF, table cell layout), this bbox is the only way the
    reviewer gets a focused highlight, so it is REQUIRED for every Fail
    and Needs Review finding. Pass findings can omit it.

    For findings comparing two values on the same page, return both
    bboxes via "location_bboxes_norm": [ [y0,x0,y1,x1], [y0,x0,y1,x1] ].
    For multi-page findings (Cross-Sheet etc.), include a "location_page"
    field naming the sheet code (e.g. "E-100") that the bbox refers to.

    Example (a missing aux-XFMR cable on the cable schedule):
      "location": "E-300 cable schedule, between rows 8 and 9 (where the
                   aux feeder should be listed)",
      "location_text": "AUX FEEDER",     // may not match — that's why bbox
      "location_bbox_norm": [620, 50, 680, 950]  // narrow horizontal band
"""

    def _prompt(base: str) -> str:
        """Append global instructions, project context, and supporting-doc
        evidence (if any) to an AI prompt."""
        return base + _GLOBAL_INSTRUCTIONS + _ctx + _evidence_ctx

    # ── V4 rule engine branch ───────────────────────────────────────────
    # If the active rules.yaml is a V4-style set (rules carry ``source`` and
    # ``v4_status`` fields), dispatch dynamically from the rule registry
    # instead of running the hard-coded prompts below. The V4 engine reuses
    # _safe_call so parallelism, progress, caching, and highlighting work
    # unchanged.
    try:
        from .rule_registry import get_rules
        from . import v4_engine
        _all_rules = get_rules()
    except Exception:
        _all_rules = []

    if v4_engine.is_v4_ruleset(_all_rules):
        logger.info("V4 engine active (%d rules loaded)", len(_all_rules))
        _deferred = v4_engine.run_v4_checks(
            doc=doc,
            pages=pages,
            rules=_all_rules,
            submit=_safe_call,
            prompt_wrap=_prompt,
            deep_for=lambda cat: _deep(v4_engine.deep_for_category(cat)),
            page_check=_gemini_page_check,
            multi_page_check=_gemini_multi_page_check,
            run_id=run_id,
            run_dir=run_dir,
            supporting_docs=supporting_docs,
            design_stage=design_stage,
        )
        if _deferred:
            all_issues.extend(_deferred)

        # ── Legacy supplement ──────────────────────────────────────────
        # Fill coverage gaps where the V4 workbook taxonomy has no direct
        # equivalent to a V3 category with tuned hardcoded prompts. These
        # run alongside the V4 engine so their findings are additive.
        #
        # NOTE: the legacy "System Information Table" Gemini prompt was
        # removed — its 15 math checks now run as deterministic
        # electrical_calc rules (calc_module_count_consistency,
        # calc_total_dc_math, calc_total_ac_math, calc_dc_ac_ratio) plus
        # V4 Cross-Sheet for the make/model/spec comparisons. Saves ~50s
        # per run with equivalent coverage.

        # Helper: run a legacy V3 prompt on the first page matching any of
        # the given title keywords. No-op when no page matches.
        def _run_legacy_prompt(
            prompt_text: str, category: str, item_key: str, display_title: str,
            *title_keywords: str,
        ) -> None:
            for p in pages:
                title_up = (p.sheet_title or "").upper()
                if any(kw in title_up for kw in (k.upper() for k in title_keywords)):
                    _safe_call(
                        _gemini_page_check, doc, p.number, _prompt(prompt_text),
                        run_id, run_dir, category, item_key, display_title,
                    )
                    return

        # AUX SLD / AUX power — Hillsboro has "AUXILIARY LOADS" in the
        # title without an explicit "SLD" keyword, so match broadly.
        _run_legacy_prompt(
            _AUX_SLD_PROMPT, "AUX SLD", "ai_aux_legacy",
            "Auxiliary SLD / Aux Power Review",
            "AUX", "AUXILIARY",
        )

        # Elevation Details — V3 prompt catches pile depth/type/spacing,
        # CAB sweep/bend, weather-sensor placement, equipment clearances.
        # V4 has no dedicated category for this.
        _run_legacy_prompt(
            _ELEVATION_PROMPT, "Elevation Details", "ai_elev_legacy",
            "Elevation Details Review",
            "ELEVATION", "EQUIPMENT DETAIL", "EQUIPMENT ELEVATION",
        )

        # PAD / Slab Details — rebar, anchor bolts, concrete specs,
        # grounding connection, drainage, edge details.
        _run_legacy_prompt(
            _PAD_SLAB_PROMPT, "PAD / Slab Details", "ai_pad_legacy",
            "PAD / Slab Details Review",
            "PAD DETAIL", "SLAB DETAIL", "CONCRETE PAD",
            "EQUIPMENT PAD", "FOUNDATION DETAIL",
        )

        # Equipment Area Feeder Plan — pile details, clearances,
        # NEC 110.26 zone.
        _run_legacy_prompt(
            _EQUIP_AREA_FEEDER_PROMPT, "Equipment Area Feeder Plan",
            "ai_eaf_legacy", "Equipment Area Feeder Plan Review",
            "EQUIPMENT AREA", "EQUIPMENT PAD", "PAD FEEDER", "EQUIPMENT FEEDER",
        )

        # Drain the futures submitted by the engine and supplement —
        # skip the legacy hard-coded dispatches below.
        fmap = {fut: label for fut, label in _futures}
        total = len(fmap)
        done = 0
        try:
            for fut in _cf.as_completed(fmap):
                done += 1
                label = fmap[fut]
                if progress_cb is not None:
                    pct = int(40 + (48 * min(done, total) / max(1, total)))
                    try:
                        progress_cb(f"AI vision: {label} ({done}/{total})", pct)
                    except Exception:
                        pass
                try:
                    all_issues.extend(fut.result())
                except Exception:
                    logger.exception("V4 vision check '%s' failed", label)
        finally:
            _pool.shutdown(wait=True)
        return all_issues

    # ── Find pages by TITLE keywords (not E-number prefixes) ──
    def find_pages(*keywords: str) -> list[int]:
        """Return page numbers whose sheet title matches any keyword."""
        results: list[int] = []
        for p in pages:
            title = (p.sheet_title or "").upper()
            if any(kw.upper() in title for kw in keywords):
                results.append(p.number)
        return results

    # 1 ── Cover Sheet (always page 1) ─────────────────────────────────────
    all_issues.extend(
        _safe_call(
            _gemini_page_check, doc, 1, _prompt(_COVER_SHEET_PROMPT),
            run_id, run_dir, "Cover Sheet", "ai_cover", "Cover Sheet Review",
        )
    )

    # 2 ── System Information Table — REMOVED.
    # The 15 SysInfo math rules are now deterministic electrical_calc rules
    # (calc_module_count_consistency, calc_total_dc_math, calc_total_ac_math,
    # calc_dc_ac_ratio in rules_v4_draft.yaml) plus existing validate_stringing
    # / validate_fuse_sizing for M10/M14, plus V4 Cross-Sheet for M6/M7/M8
    # make/model comparisons. Saves ~50s per run.

    # 3 ── Title Block spot-check (sample 3 pages) ────────────────────────
    sample = list(dict.fromkeys([1, len(pages)//2, len(pages)]))
    for pn in sample:
        if pn >= 1:
            all_issues.extend(
                _safe_call(
                    _gemini_page_check, doc, pn, _prompt(_TITLE_BLOCK_PROMPT),
                    run_id, run_dir, "Title Block", f"ai_tb_p{pn}", f"Title Block (p.{pn})",
                )
            )

    # 4 ── Site Plan ──────────────────────────────────────────────────────
    sp = find_pages("SITE PLAN", "OVERALL SITE", "SITE LAYOUT")
    if sp:
        all_issues.extend(
            _safe_call(
                _gemini_page_check, doc, sp[0], _prompt(_SITE_PLAN_PROMPT),
                run_id, run_dir, "Site Plan", "ai_siteplan", "Site Plan Review",
            )
        )

    # 5 ── Pole Line Up ──────────────────────────────────────────────────
    pl = find_pages("POLE LINE", "POLE LINEUP",
                    "POLE ARRANGEMENT", "INTERCONNECTION")
    if pl:
        all_issues.extend(
            _safe_call(
                _gemini_page_check, doc, pl[0], _prompt(_POLE_LINEUP_PROMPT),
                run_id, run_dir, "Pole Line Up", "ai_pole", "Pole Line Up Review",
            )
        )

    # 6 ── Equipment List ────────────────────────────────────────────────
    eq = find_pages("EQUIPMENT LIST", "EQUIPMENT SCHEDULE",
                    "BOM", "BILL OF MATERIAL")
    if eq:
        all_issues.extend(
            _safe_call(
                _gemini_page_check, doc, eq[0], _prompt(_EQUIPMENT_LIST_PROMPT),
                run_id, run_dir, "Engineered Equipment List", "ai_equip", "Equipment List Review",
            )
        )

    # 7 ── AC Single Line Diagram (ALL pages) ─────────────────────────────
    sld = find_pages("SINGLE LINE", "SINGLE-LINE",
                     "SLD", "ONE LINE", "ONE-LINE")
    # exclude DC or AUX single lines
    sld = [p for p in sld
           if "DC" not in (pages[p-1].sheet_title or "").upper()
           and "AUX" not in (pages[p-1].sheet_title or "").upper()]
    if sld:
        # Always use multi-page so AI sees ALL SLD pages together for
        # cross-validation of cable sizes, equipment ratings, etc.
        all_issues.extend(
            _safe_call(
                _gemini_multi_page_check, doc, sld[:5], _prompt(_SLD_PROMPT),
                run_id, run_dir, "AC Single Line Diagram", "ai_sld",
                f"SLD NEC Review ({len(sld[:5])} pages)",
                deep=_deep(True),
            )
        )

    # 8 ── DC Line Diagram (ALL pages) ──────────────────────────────────
    dc = find_pages("DC LINE", "DC DIAGRAM", "DC SINGLE LINE",
                    "DC WIRING", "STRING DIAGRAM")
    if dc:
        all_issues.extend(
            _safe_call(
                _gemini_multi_page_check, doc, dc[:4], _prompt(_DC_DIAGRAM_PROMPT),
                run_id, run_dir, "DC Line Diagram", "ai_dc",
                f"DC Diagram NEC Review ({len(dc[:4])} pages)",
                deep=_deep(True),
            )
        )

    # 9 ── Three Line Diagram (ALL pages) ───────────────────────────────
    tld = find_pages("THREE LINE", "THREE-LINE", "3-LINE", "3LD", "3 LINE")
    if tld:
        all_issues.extend(
            _safe_call(
                _gemini_multi_page_check, doc, tld[:3], _prompt(_THREE_LINE_PROMPT),
                run_id, run_dir, "Three Line Diagram", "ai_3ld",
                f"3LD Review ({len(tld[:3])} pages)",
                deep=_deep(True),
            )
        )

    # 10 ── Feeder Plan ──────────────────────────────────────────────────
    fp = find_pages("FEEDER PLAN", "AC FEEDER", "DC FEEDER",
                    "CABLE PLAN", "WIRING PLAN")
    if fp:
        all_issues.extend(
            _safe_call(
                _gemini_page_check, doc, fp[0], _prompt(_FEEDER_PLAN_PROMPT),
                run_id, run_dir, "Feeder Plan", "ai_feeder", "Feeder Plan Review",
            )
        )

    # 11 ── Electrical Sheets ────────────────────────────────────────────
    es = find_pages("CIRCUIT SCHEDULE", "ELECTRICAL SCHEDULE", "CABLE SCHEDULE",
                    "AMPACITY", "WIRE SCHEDULE")
    if es:
        all_issues.extend(
            _safe_call(
                _gemini_page_check, doc, es[0], _prompt(_ELECTRICAL_SHEET_PROMPT),
                run_id, run_dir, "Electrical Sheet", "ai_elec", "Electrical Schedule Review",
            )
        )

    # 12 ── Elevation Details ────────────────────────────────────────────
    ev = find_pages("ELEVATION", "EQUIPMENT DETAIL", "EQUIPMENT ELEVATION")
    if ev:
        all_issues.extend(
            _safe_call(
                _gemini_page_check, doc, ev[0], _prompt(_ELEVATION_PROMPT),
                run_id, run_dir, "Elevation Details", "ai_elev", "Elevation Details Review",
            )
        )

    # 13 ── Grounding ────────────────────────────────────────────────────
    gnd = find_pages("GROUNDING", "GROUND PLAN", "GND", "EARTHING")
    if gnd:
        all_issues.extend(
            _safe_call(
                _gemini_page_check, doc, gnd[0], _prompt(_GROUNDING_PROMPT),
                run_id, run_dir, "Grounding Diagram", "ai_gnd", "Grounding Review",
            )
        )

    # 14 ── Relay and Inverter Settings ────────────────────────────────
    rs = find_pages("RELAY", "INVERTER SETTING",
                    "IEEE 1547", "PROTECTION SETTING")
    if rs:
        all_issues.extend(
            _safe_call(
                _gemini_page_check, doc, rs[0], _prompt(_RELAY_SETTINGS_PROMPT),
                run_id, run_dir, "Relay and Inverter Settings", "ai_relay",
                "Relay & Inverter Settings Review",
                deep=_deep(True),
            )
        )

    # 15 ── AUX SLD ────────────────────────────────────────────────────
    aux = find_pages("AUX", "AUXILIARY")
    # filter to SLD-type pages only
    aux = [p for p in aux
           if "SLD" in (pages[p-1].sheet_title or "").upper()
           or "SINGLE LINE" in (pages[p-1].sheet_title or "").upper()
           or "LINE DIAGRAM" in (pages[p-1].sheet_title or "").upper()
           or "AUXILIARY" in (pages[p-1].sheet_title or "").upper()]
    if aux:
        all_issues.extend(
            _safe_call(
                _gemini_page_check, doc, aux[0], _prompt(_AUX_SLD_PROMPT),
                run_id, run_dir, "AUX SLD", "ai_aux", "Auxiliary SLD Review",
            )
        )

    # 16 ── Communication Diagram ──────────────────────────────────────
    cd = find_pages("COMMUNICATION DIAGRAM", "COMM DIAGRAM", "DAS DIAGRAM",
                    "COMMUNICATION SLD", "SCADA")
    if cd:
        all_issues.extend(
            _safe_call(
                _gemini_page_check, doc, cd[0], _prompt(_COMM_DIAGRAM_PROMPT),
                run_id, run_dir, "Communication Diagram", "ai_comm",
                "Communication Diagram Review",
            )
        )

    # 17 ── Trenching Details ──────────────────────────────────────────
    tr = find_pages("TRENCH", "TRENCHING", "CONDUIT DETAIL", "BURIAL DETAIL")
    if tr:
        all_issues.extend(
            _safe_call(
                _gemini_page_check, doc, tr[0], _prompt(_TRENCHING_PROMPT),
                run_id, run_dir, "Trenching Details", "ai_trench",
                "Trenching Details Review",
            )
        )

    # 18 ── CAB / Cable Hanger Details ─────────────────────────────────
    cab = find_pages("CAB DETAIL", "CABLE HANGER", "CABLE TRAY", "CAB HANGER",
                     "CABLE BRIDGE")
    if cab:
        all_issues.extend(
            _safe_call(
                _gemini_page_check, doc, cab[0], _prompt(_CAB_DETAILS_PROMPT),
                run_id, run_dir, "CAB or Cable Hanger Details", "ai_cab",
                "CAB Details Review",
            )
        )

    # 19 ── Labels ─────────────────────────────────────────────────────
    lb = find_pages("LABEL", "PLACARD", "SIGNAGE")
    if lb:
        all_issues.extend(
            _safe_call(
                _gemini_page_check, doc, lb[0], _prompt(_LABELS_PROMPT),
                run_id, run_dir, "Labels", "ai_labels", "Labels Review",
            )
        )

    # 20 ── Equipment Area Feeder Plan ─────────────────────────────────
    eaf = find_pages("EQUIPMENT AREA", "EQUIPMENT PAD", "PAD FEEDER",
                     "EQUIPMENT FEEDER")
    if eaf:
        all_issues.extend(
            _safe_call(
                _gemini_page_check, doc, eaf[0], _prompt(_EQUIP_AREA_FEEDER_PROMPT),
                run_id, run_dir, "Equipment Area Feeder Plan", "ai_eaf",
                "Equipment Area Feeder Plan Review",
            )
        )

    # 21 ── Communication Feeder Plan ─────────────────────────────────
    cfp = find_pages("COMMUNICATION FEEDER", "COMM FEEDER",
                     "COMMUNICATION PLAN", "DAS FEEDER", "FIBER PLAN")
    if cfp:
        all_issues.extend(
            _safe_call(
                _gemini_page_check, doc, cfp[0], _prompt(_COMM_FEEDER_PLAN_PROMPT),
                run_id, run_dir, "Communication Feeder Plan", "ai_cfp",
                "Communication Feeder Plan Review",
            )
        )

    # 22 ── PAD / Slab Details ────────────────────────────────────────
    pad = find_pages("PAD DETAIL", "SLAB DETAIL", "CONCRETE PAD",
                     "EQUIPMENT PAD", "FOUNDATION DETAIL")
    if pad:
        all_issues.extend(
            _safe_call(
                _gemini_page_check, doc, pad[0], _prompt(_PAD_SLAB_PROMPT),
                run_id, run_dir, "PAD / Slab Details", "ai_pad",
                "PAD/Slab Details Review",
            )
        )

    # 23 ── Pole Details ──────────────────────────────────────────────
    pd = find_pages("POLE DETAIL", "POLE ELEVATION", "RISER POLE",
                    "POLE FRAMING")
    if pd:
        all_issues.extend(
            _safe_call(
                _gemini_page_check, doc, pd[0], _prompt(_POLE_DETAILS_PROMPT),
                run_id, run_dir, "Pole Details", "ai_poledet",
                "Pole Details Review",
            )
        )

    # 24 ── Overall Site Grounding Plan ───────────────────────────────
    gp = find_pages("GROUNDING PLAN", "GROUND PLAN", "SITE GROUNDING",
                    "GND PLAN")
    # Exclude grounding diagrams (SLD-type) — only the site plan
    gp = [p for p in gp
          if "DIAGRAM" not in (pages[p - 1].sheet_title or "").upper()
          and "SLD" not in (pages[p - 1].sheet_title or "").upper()]
    if gp:
        all_issues.extend(
            _safe_call(
                _gemini_page_check, doc, gp[0], _prompt(_GROUNDING_PLAN_PROMPT),
                run_id, run_dir, "Overall Site Grounding Plan", "ai_gndplan",
                "Overall Site Grounding Plan Review",
            )
        )

    # 25 ── Deep Electrical Sheet Check (multi-page) ──────────────────
    es_deep = find_pages("CIRCUIT SCHEDULE", "ELECTRICAL SCHEDULE",
                         "CABLE SCHEDULE", "AMPACITY", "WIRE SCHEDULE",
                         "E-300", "CALC SHEET")
    if es_deep and len(es_deep) > 1:
        all_issues.extend(
            _safe_call(
                _gemini_multi_page_check, doc, es_deep[:4], _prompt(_ELECTRICAL_SHEET_DEEP_PROMPT),
                run_id, run_dir, "Electrical Sheet", "ai_elec_deep",
                "Electrical Schedule Deep Review",
                deep=_deep(True),
            )
        )

    # 26 ── Deep Grounding multi-page trace ───────────────────────────
    gnd_all = find_pages("GROUNDING", "GROUND", "GND", "EARTHING")
    if gnd_all and len(gnd_all) > 1:
        all_issues.extend(
            _safe_call(
                _gemini_multi_page_check, doc, gnd_all[:3], _prompt(_GROUNDING_PROMPT),
                run_id, run_dir, "Grounding Diagram", "ai_gnd_deep",
                "Grounding Deep Multi-Page Review",
            )
        )

    # 27 ── Deep Relay Settings (multi-page if available) ─────────────
    rs_all = find_pages("RELAY", "INVERTER SETTING",
                        "IEEE 1547", "PROTECTION SETTING")
    if rs_all and len(rs_all) > 1:
        all_issues.extend(
            _safe_call(
                _gemini_multi_page_check, doc, rs_all[:3], _prompt(_RELAY_SETTINGS_PROMPT),
                run_id, run_dir, "Relay and Inverter Settings",
                "ai_relay_deep",
                "Relay & Inverter Settings Deep Review",
                deep=_deep(True),
            )
        )

    # 28 ── Cross-Sheet Consistency Check ─────────────────────────────
    # Gather representative pages from key sections for cross-referencing
    consistency_pages: list[int] = []
    if sld:
        consistency_pages.append(sld[0])
    if tld:
        consistency_pages.append(tld[0])
    if eq:
        consistency_pages.append(eq[0])
    if pl:
        consistency_pages.append(pl[0])
    if es:
        consistency_pages.append(es[0])
    # Also include relay settings for CT/VT ratio cross-check
    if rs:
        consistency_pages.append(rs[0])
    if len(consistency_pages) >= 2:
        # Limit to 3 pages to avoid long Gemini response times
        all_issues.extend(
            _safe_call(
                _gemini_multi_page_check, doc, consistency_pages[:3], _prompt(_CROSS_SHEET_CONSISTENCY_PROMPT),
                run_id, run_dir, "Cross-Sheet Consistency",
                "ai_consistency", "Cross-Sheet Consistency Check",
                deep=_deep(True),
            )
        )

    # 29 ── Supporting Document Consistency (planset vs CESR / PVSyst / …) ──
    if supporting_docs and consistency_pages:
        from .supporting_docs import (
            CONSISTENCY_CHECK_CATEGORY,
            CONSISTENCY_PROMPT,
        )
        all_issues.extend(
            _safe_call(
                _gemini_multi_page_check, doc, consistency_pages[:4],
                _prompt(CONSISTENCY_PROMPT),
                run_id, run_dir, CONSISTENCY_CHECK_CATEGORY,
                "ai_support_docs", "Supporting Document Consistency",
                deep=_deep(True),
            )
        )

    # ── Drain all submitted vision checks ──
    # Until this point every _safe_call(...) just queued the work and returned
    # an empty list. Now wait for results and stream progress as they finish.
    fmap = {fut: label for fut, label in _futures}
    total = len(fmap)
    done = 0
    try:
        for fut in _cf.as_completed(fmap):
            done += 1
            label = fmap[fut]
            if progress_cb is not None:
                pct = int(40 + (48 * min(done, total) / max(1, total)))
                try:
                    progress_cb(f"AI vision: {label} ({done}/{total})", pct)
                except Exception:
                    pass
            try:
                all_issues.extend(fut.result())
            except Exception:
                logger.exception("Parallel vision check '%s' failed", label)
    finally:
        _pool.shutdown(wait=True)

    return all_issues
