"""Cross-page value-consistency checker for solar plansets.

This module compares the SAME design field as it was read off DIFFERENT
planset sheets and surfaces a finding when two planset pages disagree.

It reads from the analyzer's ``provenance`` map — a dict keyed by
``project_details`` field name whose value is a list of *sightings*::

    provenance["transformer_kva"] = [
        {"value": "2250", "source": "E-100 SLD", "page_number": 7},
        {"value": "2000 kVA", "source": "cover system-info", "page_number": 1},
        {"value": "2250 kVA", "source": "user project_details", "page_number": None},
    ]

Only sightings that came from a PLANSET PAGE (``page_number is not None``)
are compared against each other — a user-entered or supporting-doc value is
authoritative and is never itself flagged as a conflict (it is, however,
noted as the canonical reference).

The public surface is:

  * ``normalize_value(field, raw) -> (key, magnitude|None)`` — canonicalize a
    raw value for comparison.
  * ``compare_provenance(provenance, page_map=None) -> list[dict]`` — produce
    cross-sheet conflict findings.
  * ``reconcile_text(field, sightings) -> dict`` — AI fallback for
    free-text / unit-uncertain fields.

Nothing here renders crops; the analyzer fills in ``snippet_path`` /
``page_preview_path`` / ``bbox`` for each location after we hand it the
``locations`` list.
"""

from __future__ import annotations

import json
import logging
import re

log = logging.getLogger(__name__)

# Numeric ratio / unit tolerances.
_RATIO_TOL = 0.02          # 1.30 == 1.298 (within ±0.02 absolute)
_MAG_REL_TOL = 0.01        # generic magnitudes equal within 1% relative
_DIM_TOL = 0.05            # spacing/pitch/tilt/azimuth absolute tolerance after unit-normalize
_AI_CONFLICT_CONFIDENCE = 0.6  # reconcile_text conflict ≥ this → Fail, else Needs Review


# ── Field classification ────────────────────────────────────────────────────
# Fields whose values are free-text / structured labels that normalize_value
# cannot confidently reduce to a single comparable number. These go through
# the AI reconciler when planset sightings disagree.
_AMBIGUOUS_FIELDS = {
    "transformer_winding_config",
    "inverter_make", "inverter_model",
    "module_make", "module_model",
    "racking_make", "racking_model", "racking_type",
    "utility_name", "utility_feeder",
    "inverter_mppt_range",  # "860-1300" — structured range, treat as text
}

# Fields measured as a physical dimension/angle with units (ft/m/deg). They
# get a slightly looser absolute tolerance and unit-normalization.
_DIMENSION_FIELDS = {
    "pole_spacing", "pile_spacing", "row_pitch", "pitch", "interrow_spacing",
    "tilt_angle", "azimuth", "tracker_rotation",
}

# Fields that are pure ratios (compared with the ratio tolerance).
_RATIO_FIELDS = {"dc_ac_ratio", "gcr", "transformer_xr_ratio"}

# Fields carrying an electrical winding/voltage form.
_WINDING_FIELDS = {"transformer_winding_config"}
_VOLTAGE_FIELDS = {
    "transformer_primary_voltage", "transformer_secondary_voltage",
    "poi_voltage", "inverter_ac_voltage", "inverter_max_vdc",
}
_AWG_FIELDS = set()  # wire-size fields use the AWG normalizer via heuristic below


# ── Low-level canonicalizers ────────────────────────────────────────────────

_THOUSANDS_RE = re.compile(r"(?<=\d),(?=\d)")


def _strip_thousands(s: str) -> str:
    return _THOUSANDS_RE.sub("", s)


def _first_float(s: str) -> float | None:
    """Parse the first numeric token (sign + digits + optional decimal)."""
    m = re.search(r"[-+]?(?:\d+(?:\.\d+)?|\.\d+)", _strip_thousands(s))
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _mags_equal(a: float, b: float, *, abs_tol: float = 0.0, rel_tol: float = _MAG_REL_TOL) -> bool:
    if a == b:
        return True
    if abs(a - b) <= abs_tol:
        return True
    scale = max(abs(a), abs(b), 1e-9)
    return abs(a - b) / scale <= rel_tol


# kVA / MVA / kW / MW power normalization → canonical kVA-equivalent magnitude.
_POWER_UNIT_RE = re.compile(r"(?i)\b(mva|kva|mw|kw|va|w)\b")


def _normalize_power(raw: str) -> tuple[str, float | None]:
    """Canonicalize a power rating to kVA-equivalent magnitude.

    2250 kVA == 2.25 MVA == "2,250kVA" → magnitude 2250.0.
    A bare number with no unit is assumed to already be in the field's
    native unit (kVA for transformer_kva, etc.) and returned as-is.
    """
    s = _strip_thousands(raw)
    num = _first_float(s)
    if num is None:
        return _text_key(raw), None
    unit_m = _POWER_UNIT_RE.search(s)
    unit = (unit_m.group(1).lower() if unit_m else "")
    factor = {
        "mva": 1000.0, "kva": 1.0, "va": 0.001,
        "mw": 1000.0, "kw": 1.0, "w": 0.001,
    }.get(unit, 1.0)
    mag = num * factor
    return f"pow:{round(mag, 4)}", mag


# Voltage normalization. "480" == "480V"; combined forms ("480Y/277",
# "12470Y/7200") are kept structured (digits + winding letters) so 480Y/277
# never collapses to a bare 480.
_VOLT_COMBINED_RE = re.compile(r"\d[\d,]*\s*[A-Za-z]?\s*/\s*\d[\d,]*")


def _normalize_voltage(raw: str) -> tuple[str, float | None]:
    s = _strip_thousands(raw).strip()
    if _VOLT_COMBINED_RE.search(s):
        # Structured combined voltage — keep the whole token (sans spaces,
        # trailing 'V', uppercased) so 480Y/277 != 277/480 etc.
        compact = re.sub(r"\s+", "", s).upper().rstrip("V")
        return f"voltp:{compact}", None
    num = _first_float(s)
    if num is None:
        return _text_key(raw), None
    return f"volt:{round(num, 4)}", num


# AWG / kcmil / MCM wire-size normalization. "#6 AWG" == "6 AWG" == "6AWG";
# "500 kcmil" == "500 MCM".
_AWG_RE = re.compile(r"(?i)#?\s*(\d+(?:/0)?|\d+)\s*(awg|kcmil|mcm|cmil)?")


def _normalize_awg(raw: str) -> tuple[str, float | None]:
    s = raw.strip()
    m = re.search(r"(?i)#?\s*(\d+)\s*(awg|kcmil|mcm)?", _strip_thousands(s))
    if not m:
        return _text_key(raw), None
    size = m.group(1)
    unit = (m.group(2) or "awg").lower()
    if unit == "mcm":
        unit = "kcmil"
    try:
        mag = float(size)
    except ValueError:
        mag = None
    return f"wire:{size}{unit}", mag


# Winding-config synonyms. Yg == Wye-grounded == Y(grounded); Delta == D.
_WINDING_TOKEN_MAP = [
    (re.compile(r"(?i)\b(wye[\s-]*grounded|grounded[\s-]*wye|y\s*g|yg|wye\s*gnd)\b"), "Yg"),
    (re.compile(r"(?i)\b(wye|star)\b"), "Y"),
    (re.compile(r"(?i)\b(delta)\b"), "D"),
]


def _normalize_winding(raw: str) -> tuple[str, float | None]:
    """Reduce a winding-config string to ordered canonical tokens.

    "Delta-Wye Grounded" -> "D-Yg"; "Δ-Yg" handled via the literal letters.
    Single-letter forms (Y / D / Yg) are normalized too. Order is preserved
    (primary-secondary) so D-Yg != Yg-D.
    """
    s = raw.strip()
    # Replace any explicit delta glyph.
    s = s.replace("Δ", "Delta").replace("∆", "Delta")
    canon = s
    for rx, repl in _WINDING_TOKEN_MAP:
        canon = rx.sub(repl, canon)
    # Collapse to the sequence of canonical tokens (Yg, Y, D) in order.
    tokens = re.findall(r"Yg|Y|D", canon)
    if not tokens:
        return _text_key(raw), None
    return "wind:" + "-".join(tokens), None


def _normalize_ratio(raw: str) -> tuple[str, float | None]:
    num = _first_float(raw)
    if num is None:
        return _text_key(raw), None
    # Key is advisory; equality is decided by the magnitude tolerance in
    # _equal_keys (so 1.30 / 1.3 / 1.298 group together within ±_RATIO_TOL).
    return f"ratio:{round(num, 4)}", num


# Dimension/angle normalization → canonical feet (or degrees) magnitude.
_DIM_UNIT_RE = re.compile(r"(?i)(feet|foot|ft|meters?|metre?s?|m|deg(?:rees?)?|°|inch(?:es)?|in|\")")


def _feet_from_ft_in(raw: str) -> float | None:
    """Parse architectural feet-inches like 23'-6" or 23' 6\" → 23.5 ft."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*'\s*[-\s]?\s*(\d+(?:\.\d+)?)?\s*\"?", raw)
    if not m:
        return None
    feet = float(m.group(1))
    inches = float(m.group(2)) if m.group(2) else 0.0
    return feet + inches / 12.0


def _normalize_dimension(field: str, raw: str) -> tuple[str, float | None]:
    s = _strip_thousands(raw).strip()
    # Angles (tilt/azimuth/tracker_rotation): keep degrees.
    is_angle = field in {"tilt_angle", "azimuth", "tracker_rotation"} or "°" in s or re.search(r"(?i)\bdeg", s)
    if is_angle:
        num = _first_float(s)
        if num is None:
            return _text_key(raw), None
        return f"deg:{round(num, 2)}", num
    # Architectural feet-inches first (23'-6").
    feet = _feet_from_ft_in(s)
    if feet is not None:
        return f"len:{round(feet, 3)}", feet
    num = _first_float(s)
    if num is None:
        return _text_key(raw), None
    unit_m = _DIM_UNIT_RE.search(s)
    unit = (unit_m.group(1).lower() if unit_m else "ft")
    if unit in ("m", "meter", "meters", "metre", "metres"):
        feet_val = num * 3.280839895
    elif unit in ("in", "inch", "inches", '"'):
        feet_val = num / 12.0
    else:  # ft / feet / foot / unitless
        feet_val = num
    return f"len:{round(feet_val, 3)}", feet_val


def _text_key(raw: str) -> str:
    """Lowercased, whitespace-collapsed text key for free-text comparison."""
    return "txt:" + re.sub(r"\s+", " ", str(raw).strip().lower())


def _looks_like_power_field(field: str) -> bool:
    return any(t in field for t in ("kva", "_kw", "mva", "_mw")) or field in {
        "transformer_kva", "transformer_total_kva", "inverter_kva",
        "inverter_kw", "total_ac_kva", "total_dc_kw",
    }


def _looks_like_wire_field(field: str) -> bool:
    return any(t in field for t in ("awg", "kcmil", "conductor", "wire", "_gauge"))


# ── Public: normalize_value ─────────────────────────────────────────────────

def normalize_value(field: str, raw) -> tuple[str, float | None]:
    """Canonicalize *raw* (the value of *field*) for cross-sheet comparison.

    Returns ``(key, magnitude)`` where ``key`` is a string that is equal for
    values that should be considered the same, and ``magnitude`` is a float
    when the value is numerically parseable (else ``None``).

    Equivalences handled:
      * power: 2250 kVA == 2.25 MVA == "2,250kVA"
      * voltage: 480 == 480V; combined 480Y/277 kept structured
      * wire size: "#6 AWG" == "6 AWG" == "6AWG"; kcmil == MCM
      * winding: Yg == Wye-grounded == Y(grounded); Delta == D
      * ratios: 1.30 == 1.3 == 1.298 (±0.02)
      * dimensions: ft/m/deg unit-normalized with a small tolerance
    """
    if raw is None:
        return "txt:", None
    raw_s = str(raw).strip()
    if not raw_s:
        return "txt:", None

    f = (field or "").lower()

    if f in _RATIO_FIELDS:
        return _normalize_ratio(raw_s)
    if f in _WINDING_FIELDS:
        return _normalize_winding(raw_s)
    if f in _VOLTAGE_FIELDS:
        return _normalize_voltage(raw_s)
    if f in _DIMENSION_FIELDS:
        return _normalize_dimension(f, raw_s)
    if _looks_like_power_field(f):
        return _normalize_power(raw_s)
    if _looks_like_wire_field(f):
        return _normalize_awg(raw_s)

    # Generic: a clean numeric value is compared numerically; otherwise text.
    num = _first_float(raw_s)
    # Only treat as a pure number when the string is essentially just that
    # number (plus optional unit) — avoids collapsing distinct labels.
    if num is not None and re.fullmatch(r"[-+]?(?:[\d,]+(?:\.\d+)?|\.\d+)\s*[A-Za-z%°/]*", raw_s):
        return f"num:{round(num, 4)}", num
    return _text_key(raw_s), None


# ── Sighting helpers ────────────────────────────────────────────────────────

def _planset_sightings(sightings: list[dict]) -> list[dict]:
    """Sightings that came from a planset page (have a page_number)."""
    out = []
    for s in sightings or []:
        if not isinstance(s, dict):
            continue
        pn = s.get("page_number")
        if pn is not None:
            out.append(s)
    return out


def _reference_sighting(sightings: list[dict]) -> dict | None:
    """A user / supporting-doc sighting (page_number is None) used as the
    canonical reference value, if any."""
    for s in sightings or []:
        if isinstance(s, dict) and s.get("page_number") is None and s.get("value"):
            return s
    return None


def _equal_keys(key_a: str, mag_a, key_b: str, mag_b, field: str) -> bool:
    """Two normalized values are equal if their keys match OR their numeric
    magnitudes are within the field-appropriate tolerance."""
    if key_a == key_b:
        return True
    if mag_a is None or mag_b is None:
        return False
    f = (field or "").lower()
    if f in _RATIO_FIELDS:
        return _mags_equal(mag_a, mag_b, abs_tol=_RATIO_TOL, rel_tol=0.0)
    if f in _DIMENSION_FIELDS:
        return _mags_equal(mag_a, mag_b, abs_tol=_DIM_TOL, rel_tol=_MAG_REL_TOL)
    return _mags_equal(mag_a, mag_b, rel_tol=_MAG_REL_TOL)


def _group_sightings(field: str, sightings: list[dict]) -> list[dict]:
    """Group planset sightings by normalized equivalence.

    Returns a list of groups; each group is::
        {"key": str, "magnitude": float|None,
         "members": [sighting, ...], "values": [raw_value, ...]}
    """
    groups: list[dict] = []
    for s in sightings:
        raw = s.get("value")
        key, mag = normalize_value(field, raw)
        placed = False
        for g in groups:
            if _equal_keys(g["key"], g["magnitude"], key, mag, field):
                g["members"].append(s)
                g["values"].append(raw)
                if g["magnitude"] is None and mag is not None:
                    g["magnitude"] = mag
                placed = True
                break
        if not placed:
            groups.append({"key": key, "magnitude": mag, "members": [s], "values": [raw]})
    return groups


# ── AI reconciler ───────────────────────────────────────────────────────────

_RECONCILE_PROMPT = """\
You are a senior solar PV professional engineer auditing a planset for
CROSS-SHEET CONSISTENCY. You are given the SAME design field as it was read
off MULTIPLE sheets of the same planset. Decide whether the readings are
EQUIVALENT (just formatted differently) or a genuine CONFLICT.

Field: {field}

Readings (JSON):
{readings}

Treat these as EQUIVALENT (NOT a conflict):
  - 2250 kVA == 2.25 MVA == "2,250 kVA"
  - 480 == 480V; 12470 == 12470 V
  - "#6 AWG" == "6 AWG" == "6AWG"; 500 kcmil == 500 MCM
  - Yg == Wye-grounded == "Y (grounded)"; Delta == D
  - 1.30 == 1.3 (ratios within ±0.02)
A CONFLICT is when the underlying engineering value genuinely differs
(e.g. 2250 kVA on one sheet vs 2000 kVA on another, or Delta-Wye vs
Wye-Wye), which would be a real QC defect.

Output STRICT JSON, no prose, exactly this shape:
{{
  "verdict": "consistent" | "conflict",
  "canonical_value": "the value that is most likely correct / authoritative",
  "outlier_pages": [list of page numbers that disagree],
  "reason": "one short sentence",
  "confidence": 0.0-1.0
}}
If you are unsure, return "conflict" with a LOW confidence so a human reviews.
"""


def _parse_json_object(raw: str) -> dict:
    """Pull a single JSON object out of a (possibly fenced) AI response."""
    if not raw:
        return {}
    try:
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        text = m.group(1) if m else raw
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            return {}
        parsed = json.loads(text[start:end + 1])
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def reconcile_text(field: str, sightings: list[dict]) -> dict:
    """Ask the deep text model whether differing free-text readings of
    *field* are equivalent or a genuine conflict.

    Defensive: if the AI call raises or returns garbage, we return a
    low-confidence ``conflict`` so a human reviews — we never crash the
    analysis.
    """
    readings = [
        {
            "value": s.get("value"),
            "sheet": s.get("sheet_number") or s.get("source"),
            "page": s.get("page_number"),
        }
        for s in sightings
    ]
    fallback = {
        "verdict": "conflict",
        "canonical_value": readings[0]["value"] if readings else None,
        "outlier_pages": [],
        "reason": "AI reconciliation unavailable; flagged for human review.",
        "confidence": 0.3,
    }
    try:
        from .gemini_client import analyze_text
    except Exception:
        log.exception("reconcile_text: could not import analyze_text")
        return fallback

    prompt = _RECONCILE_PROMPT.format(
        field=field,
        readings=json.dumps(readings, ensure_ascii=False, indent=2),
    )
    try:
        raw = analyze_text(prompt, deep=True)
    except Exception:
        log.exception("reconcile_text: AI call failed for field %s", field)
        return fallback

    parsed = _parse_json_object(raw)
    verdict = str(parsed.get("verdict", "")).strip().lower()
    if verdict not in ("consistent", "conflict"):
        return fallback
    try:
        confidence = float(parsed.get("confidence", 0.3))
    except (TypeError, ValueError):
        confidence = 0.3
    confidence = max(0.0, min(1.0, confidence))
    outliers = parsed.get("outlier_pages") or []
    if not isinstance(outliers, list):
        outliers = []
    return {
        "verdict": verdict,
        "canonical_value": parsed.get("canonical_value"),
        "outlier_pages": outliers,
        "reason": str(parsed.get("reason", "")).strip() or "(no reason given)",
        "confidence": confidence,
    }


# ── Same-referent verification (vision) ─────────────────────────────────────
# A candidate cross-sheet conflict is only a REAL defect when the two readings
# describe the SAME physical/engineering quantity. They might instead be two
# DIFFERENT things that were mistakenly grouped — pile-to-pile spacing WITHIN a
# row vs ROW-to-row pitch; the N-S vs E-W array dimension; transformer T1's kVA
# vs T2's kVA; inverter INV-1 vs INV-2; a typical callout vs a specific one.
# Those are NOT conflicts. ``verify_same_referent`` looks at the highlighted
# page previews of the conflicting locations and asks the vision model whether
# they refer to the same quantity. It is DEFENSIVE: on ANY error it returns the
# UNKNOWN verdict (``same_referent: None``) — it never fabricates a 'different'
# verdict and never raises.

# Cap the locations a single verification call inspects (cost + prompt size).
_MAX_REFERENT_LOCATIONS = 3

_SAME_REFERENT_PROMPT = """\
You are a senior solar PV professional engineer auditing a planset for
CROSS-SHEET CONSISTENCY. An automated checker read a value described as
'{field}' off DIFFERENT sheets and the values DISAGREE, so it wants to flag a
conflict. Before flagging, confirm the two readings actually describe the SAME
engineering quantity.

The images below are highlighted full-page previews of each sheet; the value in
question is highlighted on each. The readings are:
{readings}

Decide ONE of:
  * SAME quantity, two different stated values  -> a genuine QC conflict
    (same physical/engineering thing, sheets disagree). same_referent = true.
  * DIFFERENT quantities mistakenly compared      -> NOT a conflict.
    same_referent = false. Concrete examples of DIFFERENT quantities:
      - pile-to-pile spacing WITHIN a row vs ROW-to-row pitch,
      - the N-S array dimension vs the E-W array dimension,
      - two DIFFERENT tagged components (Transformer T1 vs T2, Inverter INV-1
        vs INV-2, Recloser R1 vs R2),
      - a TYPICAL ("TYP.") callout vs a specific-location callout,
      - a per-string value vs a per-array/total value.

Output STRICT JSON, no prose, EXACTLY this shape:
{{
  "same_referent": true | false,
  "confidence": 0.0-1.0,
  "reason": "<one sentence>",
  "what_each_refers_to": {{"<sheet or page>": "<short description>"}}
}}
If you cannot tell, return same_referent true with LOW confidence so a human
reviews (NEVER claim 'different' unless you are sure).
"""


def _distinct_verification_locations(locations: list[dict]) -> list[dict]:
    """Pick up to ``_MAX_REFERENT_LOCATIONS`` DISTINCT conflicting locations to
    show the model — one per (page, value) pair, preferring locations that
    already have a rendered preview image."""
    seen: set[tuple] = set()
    distinct: list[dict] = []
    for loc in locations or []:
        if not isinstance(loc, dict):
            continue
        key = (loc.get("page_number"), loc.get("value"))
        if key in seen:
            continue
        seen.add(key)
        distinct.append(loc)
    # Prefer locations whose preview is already rendered (free image), then the
    # rest, capping the total.
    with_preview = [l for l in distinct if l.get("page_preview_path") or l.get("snippet_path")]
    without = [l for l in distinct if l not in with_preview]
    ordered = with_preview + without
    return ordered[:_MAX_REFERENT_LOCATIONS]


_UNKNOWN_REFERENT = {
    "same_referent": None,
    "confidence": 0.0,
    "reason": "verification unavailable",
}


def _load_location_image(loc: dict, *, render_page, doc) -> bytes | None:
    """Get the highlighted page-preview image bytes for *loc*.

    Prefers an already-rendered preview file (page_preview_path, else
    snippet_path); falls back to ``render_page(page_number)`` -> bytes. Returns
    ``None`` (never raises) if no image can be obtained.
    """
    # 1. Reuse an already-rendered highlighted preview/snippet on disk.
    for key in ("page_preview_path", "snippet_path"):
        path = loc.get(key)
        if path:
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
                if data:
                    return data
            except Exception:
                log.debug("verify_same_referent: could not read %s=%s", key, path)
    # 2. Render the page on demand via the injected renderer.
    pn = loc.get("page_number")
    if pn is not None and callable(render_page):
        try:
            data = render_page(pn)
            if data:
                return data
        except Exception:
            log.debug("verify_same_referent: render_page failed for page %s", pn)
    return None


def verify_same_referent(field, locations, *, render_page=None, vision_call=None, doc=None) -> dict:
    """Decide whether a candidate conflict's locations describe the SAME
    engineering quantity (a real conflict) or DIFFERENT things mistakenly
    compared (a false positive).

    Makes ONE vision call over the highlighted page previews of the (up to 3)
    distinct conflicting locations and returns::

        {"same_referent": true|false|None,
         "confidence": 0.0-1.0,
         "reason": "<one sentence>",
         "what_each_refers_to": {"<sheet/page>": "<desc>", ...}}  # when present

    DEFENSIVE + CONSERVATIVE: on ANY render / AI / parse error — or when no
    images or no ``vision_call`` are available — returns the UNKNOWN verdict
    ``{"same_referent": None, "confidence": 0.0, "reason": "verification
    unavailable"}``. It NEVER fabricates a 'different' verdict on error and
    NEVER raises.
    """
    try:
        if not callable(vision_call):
            return dict(_UNKNOWN_REFERENT)
        chosen = _distinct_verification_locations(locations or [])
        if len(chosen) < 2:
            # Nothing meaningful to compare — leave it to the caller as unknown.
            return dict(_UNKNOWN_REFERENT)

        images: list[bytes] = []
        readings: list[dict] = []
        for loc in chosen:
            img = _load_location_image(loc, render_page=render_page, doc=doc)
            if img is None:
                continue
            images.append(img)
            readings.append({
                "sheet": loc.get("sheet_number") or loc.get("source_label") or "?",
                "page": loc.get("page_number"),
                "value": loc.get("value"),
            })
        if len(images) < 2:
            # Could not obtain at least two images to compare -> unknown.
            return dict(_UNKNOWN_REFERENT)

        prompt = _SAME_REFERENT_PROMPT.format(
            field=field,
            readings=json.dumps(readings, ensure_ascii=False, indent=2),
        )
        try:
            raw = vision_call(images, prompt)
        except Exception:
            log.exception("verify_same_referent: vision call failed for %s", field)
            return dict(_UNKNOWN_REFERENT)

        parsed = _parse_json_object(raw or "")
        if not parsed or "same_referent" not in parsed:
            return dict(_UNKNOWN_REFERENT)

        sr = parsed.get("same_referent")
        if sr is True or (isinstance(sr, str) and sr.strip().lower() in ("true", "yes", "same")):
            same = True
        elif sr is False or (isinstance(sr, str) and sr.strip().lower() in ("false", "no", "different")):
            same = False
        else:
            same = None

        try:
            conf = float(parsed.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        conf = max(0.0, min(1.0, conf))

        reason = str(parsed.get("reason", "")).strip() or "(no reason given)"
        result = {
            "same_referent": same,
            "confidence": conf if same is not None else 0.0,
            "reason": reason if same is not None else "verification unavailable",
        }
        wer = parsed.get("what_each_refers_to")
        if isinstance(wer, dict) and wer:
            # Stringify values defensively.
            result["what_each_refers_to"] = {
                str(k): str(v) for k, v in wer.items()
            }
        return result
    except Exception:
        # Absolute backstop — verification must never crash the caller.
        log.exception("verify_same_referent: unexpected failure for %s", field)
        return dict(_UNKNOWN_REFERENT)


# Confidence at/above which a 'different things' verdict is trusted enough to
# SUPPRESS the candidate conflict entirely.
_REFERENT_DROP_CONFIDENCE = 0.75

# Note appended to description/evidence when verification is inconclusive.
_INCONCLUSIVE_REFERENT_NOTE = (
    "Same-referent check inconclusive — confirm both values describe the "
    "same thing."
)


def _downgrade_finding_to_needs_review(finding: dict, note: str = _INCONCLUSIVE_REFERENT_NOTE) -> None:
    """Mutate *finding* to Needs Review and append *note* to description +
    evidence (idempotent — does not duplicate the note)."""
    finding["status"] = "Needs Review"
    finding["auto_status"] = "Needs Review"
    finding["severity"] = "medium"
    if note and note not in (finding.get("description") or ""):
        finding["description"] = (finding.get("description") or "").rstrip() + " " + note
    if note and note not in (finding.get("evidence") or ""):
        finding["evidence"] = (finding.get("evidence") or "").rstrip() + " " + note


def apply_referent_verdict(finding: dict, verdict: dict) -> bool:
    """Apply the same-referent DECISION RULES to *finding* in place.

    Returns ``True`` if the finding should be EMITTED (possibly mutated /
    downgraded) and ``False`` if it should be SUPPRESSED (a confirmed false
    positive). Bias is conservative — only a CONFIDENT 'different things'
    verdict suppresses; everything else is kept (downgraded to Needs Review
    when not a confident SAME verdict).

    Rules:
      * same_referent is False AND confidence >= 0.75 -> SUPPRESS (return False).
      * same_referent is True                          -> keep as-is; if the
        model said what each refers to, append that to the description.
      * same_referent is None (unknown/error) OR a low-confidence 'different'
        lean                                            -> keep but DOWNGRADE to
        Needs Review with the inconclusive note.
    """
    verdict = verdict or {}
    same = verdict.get("same_referent")
    try:
        conf = float(verdict.get("confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        conf = 0.0

    if same is False and conf >= _REFERENT_DROP_CONFIDENCE:
        return False  # SUPPRESS — confident different-things verdict.

    if same is True:
        wer = verdict.get("what_each_refers_to")
        if isinstance(wer, dict) and wer:
            detail = "; ".join(f"{k}: {v}" for k, v in wer.items())
            addition = f" Same-referent confirmed — {detail}."
            if addition.strip() not in (finding.get("description") or ""):
                finding["description"] = (finding.get("description") or "").rstrip() + addition
        return True  # keep existing Fail / Needs Review status.

    # Unknown/error, or low-confidence 'different' lean -> downgrade + keep.
    _downgrade_finding_to_needs_review(finding)
    return True


# ── Finding builder ─────────────────────────────────────────────────────────

def _field_label(field: str) -> str:
    return field.replace("_", " ").strip().title()


def _sighting_location(field: str, s: dict) -> dict:
    """Build one ``locations`` element from a planset sighting. bbox /
    snippet_path / page_preview_path are left None — the analyzer renders
    crops afterward."""
    return {
        "page_number": s.get("page_number"),
        "sheet_number": s.get("sheet_number"),
        "bbox": None,
        "snippet_path": None,
        "page_preview_path": None,
        "value": str(s.get("value")) if s.get("value") is not None else None,
        "source_label": s.get("source") or s.get("sheet_number") or "planset",
    }


def _describe_groups(field: str, groups: list[dict], reference: dict | None) -> tuple[str, str, str]:
    """Return (title, description, evidence) naming both sheets and values."""
    label = _field_label(field)

    def _grp_str(g: dict) -> str:
        m = g["members"][0]
        sheet = m.get("sheet_number") or m.get("source") or "?"
        pn = m.get("page_number")
        return f"{sheet} (p{pn}): {m.get('value')}"

    parts = "; ".join(_grp_str(g) for g in groups)
    title = f"Cross-sheet mismatch: {label}"
    desc = (
        f"The value for {label} disagrees across planset sheets — {parts}. "
        f"These should match across the SLD, cover system-info table, and any "
        f"datasheet that states the same field."
    )
    if reference and reference.get("value"):
        ref_src = reference.get("source") or "user/supporting-doc"
        desc += f" Reference value ({ref_src}): {reference.get('value')}."
    evidence = f"Conflicting {label} values: {parts}."
    return title, desc, evidence


def _build_finding(
    field: str,
    groups: list[dict],
    status: str,
    auto_status: str,
    confidence: float,
    reference: dict | None,
) -> dict:
    """Assemble a finding dict (NOT yet an issue) with a ``locations`` list."""
    title, desc, evidence = _describe_groups(field, groups, reference)
    severity = "high" if status == "Fail" else "medium"

    # locations: one per conflicting planset sighting (primary = first member
    # of the first group). De-dupe identical (page, value) pairs.
    locations: list[dict] = []
    seen: set[tuple] = set()
    for g in groups:
        for m in g["members"]:
            loc = _sighting_location(field, m)
            key = (loc["page_number"], loc["value"], loc["source_label"])
            if key in seen:
                continue
            seen.add(key)
            locations.append(loc)

    return {
        "category": "Cross-Sheet Consistency",
        "item_key": f"consistency_{field}",
        "title": title,
        "description": desc,
        "severity": severity,
        "status": status,
        "auto_status": auto_status,
        "confidence": round(confidence, 3),
        "evidence": evidence,
        "locations": locations,
    }


# ── Public: compare_provenance ──────────────────────────────────────────────

def compare_provenance(provenance: dict, page_map: dict | None = None) -> list[dict]:
    """Compare each field's planset sightings and return conflict findings.

    *provenance* maps field name → list of sightings (see module docstring).
    *page_map* (optional) maps sheet_number → PageInfo; unused for the core
    comparison but accepted for forward-compatibility / future bbox hints.

    Returns a list of finding dicts, each carrying a ``locations`` list with
    2+ elements. Only PLANSET-PAGE-vs-PLANSET-PAGE disagreements are flagged;
    a user-entered value is treated as authoritative and used only as a
    canonical reference note.
    """
    findings: list[dict] = []
    if not provenance:
        return findings

    # Reverse map page_number → sheet_number so each location can carry a
    # precise sheet code even though sightings only store a free-text source.
    page_to_sheet: dict[int, str] = {}
    if page_map:
        for pi in page_map.values():
            num = getattr(pi, "number", None)
            sn = getattr(pi, "sheet_number", None)
            if num is not None and sn:
                page_to_sheet[num] = sn

    for field, sightings in provenance.items():
        if not isinstance(sightings, list) or len(sightings) < 2:
            continue
        planset = _planset_sightings(sightings)
        if len(planset) < 2:
            continue

        groups = _group_sightings(field, planset)
        if len(groups) < 2:
            continue  # all planset sightings agree

        reference = _reference_sighting(sightings)
        f = (field or "").lower()

        # Decide CLEAR (numeric) vs AMBIGUOUS (free-text / AI).
        all_numeric = all(g["magnitude"] is not None for g in groups)
        is_ambiguous = (f in _AMBIGUOUS_FIELDS) or (not all_numeric)

        if not is_ambiguous:
            # CLEAR numeric conflict — magnitudes differ beyond tolerance.
            findings.append(
                _build_finding(
                    field, groups,
                    status="Fail", auto_status="Fail",
                    confidence=0.9, reference=reference,
                )
            )
            log.info(
                "consistency: CLEAR numeric conflict on %s across %d groups",
                field, len(groups),
            )
            continue

        # AMBIGUOUS — defer to the AI reconciler.
        verdict = reconcile_text(field, planset)
        if verdict.get("verdict") == "consistent":
            log.info("consistency: %s reconciled as consistent — no finding", field)
            continue
        conf = float(verdict.get("confidence", 0.3) or 0.3)
        if conf >= _AI_CONFLICT_CONFIDENCE:
            status = "Fail"
        else:
            status = "Needs Review"
        finding = _build_finding(
            field, groups,
            status=status, auto_status=status,
            confidence=conf, reference=reference,
        )
        reason = verdict.get("reason")
        if reason:
            finding["description"] += f" AI assessment: {reason}"
            finding["evidence"] += f" AI: {reason} (confidence {conf:.2f})."
        findings.append(finding)
        log.info(
            "consistency: AMBIGUOUS conflict on %s → %s (AI confidence %.2f)",
            field, status, conf,
        )

    # Backfill precise sheet codes onto every location from the page map.
    if page_to_sheet:
        for fnd in findings:
            for loc in fnd.get("locations") or []:
                if not loc.get("sheet_number") and loc.get("page_number") in page_to_sheet:
                    loc["sheet_number"] = page_to_sheet[loc["page_number"]]

    return findings
