"""A2 + A3: page routing must be disjoint, and three rules were miscalibrated.

From the 2026-08-03 production audit of all 26 runs (docs/audits/):

A2 — PAGE MISROUTING, ~113 fabricated Fails, the single largest confirmed
false-positive mechanism. "EQUIPMENT PAD" sat in both the pad-detail and
equipment-area keyword lists and both took match [0], so on nine runs the two
families reviewed the SAME sheet. On Rock Run all seven structural pad checks
(rebar schedule, concrete PSI, anchor bolts, edge details, drainage...) ran
against "Sheet E-200 (Inverter Zone Map)" and failed it for content a zone map
can never carry. `ai_pad` problem-rate: 56 Fail of 63 findings = 88%.
The same shape hit `ai_elev` (bare "ELEVATION" grabbing CAB/pole sheets, 22
Fails) and `ai_gnd` (claiming "GROUND PLAN", which is the site-grounding
family's own sheet, then contradicting the deep grounding pass in one report).

A3 — MISCALIBRATED RULES, ~100 Fail+NR at ~100% FP rate:
  * validate_conduit_fill / validate_voltage_drop read nothing from the
    planset and returned Needs Review unconditionally — a checklist item
    restated at the reviewer. 11/11 and 9/11 projects, byte-identical text.
  * The 3LD prompt demanded the diagram NOT show cable sizes or FLA. No NEC
    provision prohibits information on a drawing, the firm's own checklist
    says the opposite, and every stamped 3LD in production shows FLA. 15
    Fails, 7/7 projects.
  * found:false -> Fail converted the model's own "no DER number is shown, so
    this check is skipped" into a defect. 58 hard Fails across 11/11 projects.

Run: PYTHONPATH=backend python backend/scripts/test_routing_and_calibration.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import gemini_analyzer as ga  # noqa: E402
from app.electrical_calcs import (  # noqa: E402
    validate_conduit_fill, validate_voltage_drop,
)
from app.rule_registry import load_rules  # noqa: E402

_FAILS: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _FAILS.append(name)


def matches(title: str, keywords: tuple, exclude: tuple = ()) -> bool:
    return ga._sheet_title_matches(title, keywords, exclude)


# The four keyword sets as dispatched in run_gemini_checks.
PAD_KW = ("PAD DETAIL", "SLAB DETAIL", "CONCRETE PAD", "FOUNDATION DETAIL",
          "REBAR", "PAD SCHEDULE")
PAD_EX = ("GROUNDING", "FEEDER", "ZONE", "MAP", "PLAN")
EAF_KW = ("EQUIPMENT AREA", "EQUIPMENT PAD", "PAD FEEDER", "EQUIPMENT FEEDER")
EAF_EX = ("GROUNDING", "DETAIL", "SLAB", "FOUNDATION")
ELEV_KW = ("ELEVATION", "EQUIPMENT DETAIL", "EQUIPMENT ELEVATION")
ELEV_EX = ("CAB", "HANGER", "CABLE", "TRENCH", "POLE")
GND_KW = ("GROUNDING", "GND", "EARTHING")
GND_EX = ("SITE", "OVERALL", "PLAN")
GP_KW = ("GROUNDING PLAN", "GROUND PLAN", "SITE GROUNDING", "GND PLAN")

print("A2 — the sheets that actually caused the misroute:")
# The Rock Run sheet that absorbed seven structural pad Fails.
zone = "INVERTER ZONE MAP"
check("inverter zone map no longer matches ai_pad", not matches(zone, PAD_KW, PAD_EX))
check("inverter zone map does not match ai_eaf either",
      not matches(zone, EAF_KW, EAF_EX))
for sheet in ("EQUIPMENT PAD FEEDER PLAN", "EQUIPMENT AREA FEEDER PLAN"):
    check(f"{sheet!r} -> eaf only",
          matches(sheet, EAF_KW, EAF_EX) and not matches(sheet, PAD_KW, PAD_EX))
for sheet in ("EQUIPMENT PAD DETAIL", "PAD DETAIL", "CONCRETE PAD DETAILS",
              "SLAB DETAIL", "FOUNDATION DETAIL"):
    check(f"{sheet!r} -> pad only",
          matches(sheet, PAD_KW, PAD_EX) and not matches(sheet, EAF_KW, EAF_EX))

print("A2 — no sheet title can satisfy both pad and eaf:")
CORPUS = [
    "COVER SHEET", "INVERTER ZONE MAP", "EQUIPMENT PAD DETAIL",
    "EQUIPMENT PAD FEEDER PLAN", "EQUIPMENT AREA FEEDER PLAN", "PAD DETAIL",
    "CONCRETE PAD DETAILS", "SLAB DETAIL", "FOUNDATION DETAIL",
    "EQUIPMENT PAD GROUNDING", "PAD FEEDER PLAN", "EQUIPMENT FEEDER PLAN",
    "AC SINGLE LINE DIAGRAM", "GROUNDING DIAGRAM", "OVERALL SITE GROUNDING PLAN",
    "SITE GROUNDING PLAN", "GROUNDING DETAILS", "ARRAY ELEVATION",
    "EQUIPMENT ELEVATION", "CAB HANGER ELEVATION", "POLE ELEVATION",
    "CABLE TRAY ELEVATION", "TRENCHING DETAILS", "EQUIPMENT DETAIL",
]
both = [t for t in CORPUS
        if matches(t, PAD_KW, PAD_EX) and matches(t, EAF_KW, EAF_EX)]
check(f"zero pad/eaf collisions across {len(CORPUS)} titles (got {both})", both == [])

print("A2 — grounding families are disjoint:")
gnd_gp = [t for t in CORPUS if matches(t, GND_KW, GND_EX) and matches(t, GP_KW)]
check(f"zero ai_gnd / ai_gndplan collisions (got {gnd_gp})", gnd_gp == [])
check("'OVERALL SITE GROUNDING PLAN' -> gndplan only",
      matches("OVERALL SITE GROUNDING PLAN", GP_KW)
      and not matches("OVERALL SITE GROUNDING PLAN", GND_KW, GND_EX))
check("'GROUNDING DIAGRAM' still routes to ai_gnd",
      matches("GROUNDING DIAGRAM", GND_KW, GND_EX))
check("'GROUNDING DETAILS' still routes to ai_gnd",
      matches("GROUNDING DETAILS", GND_KW, GND_EX))

print("A2 — elevation no longer poaches other families' sheets:")
for sheet in ("CAB HANGER ELEVATION", "POLE ELEVATION", "CABLE TRAY ELEVATION"):
    check(f"{sheet!r} excluded from ai_elev", not matches(sheet, ELEV_KW, ELEV_EX))
for sheet in ("ARRAY ELEVATION", "EQUIPMENT ELEVATION"):
    check(f"{sheet!r} still routes to ai_elev", matches(sheet, ELEV_KW, ELEV_EX))

print("A2 — coverage is preserved (a real pad sheet is still reviewed):")
check("a planset with a pad detail sheet still routes it",
      any(matches(t, PAD_KW, PAD_EX) for t in CORPUS))
check("a planset with an equipment-area sheet still routes it",
      any(matches(t, EAF_KW, EAF_EX) for t in CORPUS))

print("A2 — replay against the REAL sheet titles that caused the 12 collisions:")
PAD_FALLBACK = ("EQUIPMENT PAD",)


def route(titles: dict[int, str]) -> dict[str, int | None]:
    """Mirror the dispatch order and page-claiming in run_gemini_checks."""
    claimed: dict[int, str] = {}

    def hits(kw, ex=()):
        return sorted(p for p, t in titles.items() if matches(t, kw, ex))

    def claim(cands, fam):
        for p in cands:
            if p not in claimed:
                claimed[p] = fam
                return p
        return None

    out = {}
    out["ai_elev"] = claim(hits(ELEV_KW, ELEV_EX), "ai_elev")
    out["ai_gnd"] = claim(hits(GND_KW, GND_EX), "ai_gnd")
    out["ai_eaf"] = claim(hits(EAF_KW, EAF_EX), "ai_eaf")
    pad_c = hits(PAD_KW, PAD_EX) or hits(PAD_FALLBACK, PAD_EX)
    out["ai_pad"] = claim(pad_c, "ai_pad")
    out["ai_gndplan"] = claim(hits(GP_KW), "ai_gndplan")
    return out


# Rock Run: two identically-titled pad sheets. Before the fix both families
# took p13 and the seven structural checks failed the feeder sheet.
rock = route({13: "EQUIPMENT PAD.", 18: "EQUIPMENT PAD.", 5: "AC SINGLE LINE DIAGRAM"})
check("Rock Run: eaf keeps p13", rock["ai_eaf"] == 13)
check("Rock Run: pad gets the SECOND pad sheet (p18), not p13",
      rock["ai_pad"] == 18)

# Bagby: the real structural sheet is titled "REBAR SCHEDULE MV PAD" — the
# narrowed keyword list alone would have skipped it entirely.
bagby = route({26: "EQUIPMENT PAD 1", 27: "REBAR SCHEDULE MV PAD",
               13: "GROUNDING DIAGRAM"})
check("Bagby: eaf takes the pad plan (p26)", bagby["ai_eaf"] == 26)
check("Bagby: pad finds the rebar schedule (p27) — no false negative",
      bagby["ai_pad"] == 27)
check("Bagby: grounding diagram still routed", bagby["ai_gnd"] == 13)

# E1300: the second pad-ish sheet is a grounding sheet and must stay excluded.
e1300 = route({17: "EQUIPMENT PAD", 23: "EQUIPMENT PAD/ INVERTER GROUNDING",
               22: "GROUNDING DIAGRAM"})
check("E1300: eaf takes p17", e1300["ai_eaf"] == 17)
check("E1300: pad does NOT claim the grounding sheet", e1300["ai_pad"] is None)
check("E1300: grounding sheet goes to ai_gnd", e1300["ai_gnd"] == 22)

# Coal City: single "EQUIPMENT PAD FEEDER" sheet — a plan, not a detail.
coal = route({23: "EQUIPMENT PAD FEEDER"})
check("Coal City: eaf takes the feeder sheet", coal["ai_eaf"] == 23)
check("Coal City: pad correctly finds nothing (one Deferred row, not 7 Fails)",
      coal["ai_pad"] is None)

print("A2 — no family shares a page in any real-title scenario:")
for label, r in (("Rock Run", rock), ("Bagby", bagby), ("E1300", e1300),
                 ("Coal City", coal)):
    used = [p for p in r.values() if p]
    check(f"{label}: every reviewed page belongs to exactly one family",
          len(used) == len(set(used)))

print("A3 — stub calcs defer instead of flagging every project:")
cf = validate_conduit_fill({})
check("conduit fill is Deferred, not Needs Review", cf.status == "Deferred")
check("conduit fill says why it deferred",
      "not computed" in cf.evidence and "Chapter 9" in cf.evidence)
vd = validate_voltage_drop({"total_dc_kw": "4779", "total_ac_kva": "2475"})
check("voltage drop is Deferred, not Needs Review", vd.status == "Deferred")
check("voltage drop states the notes are advisory",
      "Informational Note" in vd.evidence and "90.5(C)" in vd.evidence)
check("voltage drop severity lowered to low", vd.severity == "low")
vd_missing = validate_voltage_drop({})
check("voltage drop with no inputs still Needs Review (nothing to defer on)",
      vd_missing.status == "Needs Review")

print("A3 — the duplicate voltage-drop rule is retired:")
rules, _category_order = load_rules()
keys = {r.key for r in rules}
check("electrical_vd_client_criteria removed",
      "electrical_vd_client_criteria" not in keys)
check("electrical_voltage_drop retained", "electrical_voltage_drop" in keys)
vd_rules = [r for r in rules if getattr(r, "calc_function", None) == "validate_voltage_drop"]
check(f"exactly one rule binds validate_voltage_drop (got {len(vd_rules)})",
      len(vd_rules) == 1)

print("A3 — the invented 3LD requirement is gone:")
p = ga._THREE_LINE_PROMPT
check("prompt no longer says the 3LD should NOT show cable sizes",
      "should NOT show cable sizes" not in p)
check("prompt states showing them is normal",
      "is NOT a defect" in p or "NORMAL" in p)

print("A3 — conditional-presence items stop being hard Fails:")
for name in ("DER Number", "der_number", "DER No", "SOV", "ai_tb_p1_SOV",
             "SOV / Date / Designer"):
    check(f"{name!r} recognised as conditional", ga._is_conditional_presence(name))
for name in ("Equipment Grounding Conductor Size", "Rebar Schedule",
             "Transformer BIL", "Consistency", "Under Voltage Relay",
             "Ground Rods"):
    check(f"{name!r} NOT treated as conditional",
          not ga._is_conditional_presence(name))

print("A3 — status mapping:")
check("missing DER number -> Needs Review, not Fail",
      ga._status_from_finding("", False, "DER Number") == "Needs Review")
check("missing SOV -> Needs Review, not Fail",
      ga._status_from_finding("", False, "SOV") == "Needs Review")
check("a genuinely missing item is still a Fail",
      ga._status_from_finding("", False, "Rebar Schedule") == "Fail")
check("explicit model status always wins",
      ga._status_from_finding("Fail", False, "DER Number") == "Fail")
check("found:true still Pass", ga._status_from_finding("", True, "SOV") == "Pass")
check("no signal at all -> Needs Review",
      ga._status_from_finding("", None, "Anything") == "Needs Review")

# ── ai_equip: the Engineered Equipment List must find its own sheet ────────
# Highland N1 (run 41fd9652): the keywords required "EQUIPMENT LIST" or
# "EQUIPMENT SCHEDULE", so a sheet titled plainly "ENGINEERED EQUIPMENT"
# (E-050) never matched, while "BOM" (E-604, the 13.2 kV riser-pole bill of
# materials) did. The checklist then failed the riser-pole BOM for carrying no
# inverter kVA, no inverter kW, no transformer kVA, no recloser specs, no
# manufacturers and no models — six HIGH Fails of that run's seventeen, all
# fabricated, all from one page mismatch.
print("ai_equip routing — Highland N1 regression:")

EQUIP_KW = ("ENGINEERED EQUIPMENT", "MAJOR EQUIPMENT", "EQUIPMENT LIST",
            "EQUIPMENT SCHEDULE")
EQUIP_EXCLUDE = ("POLE", "RISER", "TRENCH", "CONDUIT", "GROUNDING")

check("matches the sheet titled plainly 'ENGINEERED EQUIPMENT'",
      matches("ENGINEERED EQUIPMENT", EQUIP_KW, EQUIP_EXCLUDE))
check("still matches 'ENGINEERED EQUIPMENT LIST'",
      matches("ENGINEERED EQUIPMENT LIST", EQUIP_KW, EQUIP_EXCLUDE))
check("still matches 'MAJOR EQUIPMENT SCHEDULE'",
      matches("MAJOR EQUIPMENT SCHEDULE", EQUIP_KW, EQUIP_EXCLUDE))
check("does NOT match a bare 'BOM' sheet",
      not matches("BOM", EQUIP_KW, EQUIP_EXCLUDE))
check("does NOT match a riser-pole materials list",
      not matches("SUGGESTED MATERIAL LIST - 13.2KV RISER POLE",
                               EQUIP_KW, EQUIP_EXCLUDE))
check("does NOT match a pole equipment list (that is the pole family)",
      not matches("POLE EQUIPMENT LIST", EQUIP_KW, EQUIP_EXCLUDE))
check("does NOT match a grounding equipment schedule",
      not matches("GROUNDING EQUIPMENT SCHEDULE",
                               EQUIP_KW, EQUIP_EXCLUDE))

# The six checks that fabricated Fails on the riser-pole BOM. Naming them
# keeps the regression legible if the keywords are ever widened again.
for cid in ("inverter_kva_rating", "inverter_kw_rating", "transformer_kva_rating",
            "recloser_specs", "manufacturers", "models"):
    check(f"ai_equip_{cid} is a real check that needs the right sheet",
          isinstance(cid, str) and bool(cid))

print()
if _FAILS:
    print(f"FAILED ({len(_FAILS)}): {_FAILS}")
    sys.exit(1)
print("ALL ROUTING + CALIBRATION CHECKS PASSED")
