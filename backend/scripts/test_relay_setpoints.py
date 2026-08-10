"""IEEE 1547 trip setpoints must be correct, and matched by VALUE not by label.

Two separate defects, found together while preparing to enumerate ai_relay.

1. TRANSCRIPTION. Both reference tables in _RELAY_SETTINGS_PROMPT carried
   UF1 = 56.5 Hz / 0.16 s and UF2 = 58.5 Hz / 300 s. IEEE 1547-2018 Table 18
   is the other way round. The tables were also internally inconsistent: every
   other pair in them follows "element 2 is further from nominal and trips
   faster" (OV2 1.20 p.u. / 0.16 s vs OV1 1.10 / 2.0; UV2 0.45 / 0.16 vs UV1
   0.70 / 2.0; OF2 62.0 / 0.16 vs OF1 61.2 / 300) — only the UF rows inverted
   it. A reference table the model is told to compare against must be right,
   because enumeration will make any error in it reproducible.

2. LABEL vs VALUE. IEEE 1547 fixes the required trip POINTS; it does not fix
   which element number a designer assigns to them, and relay platforms are
   numbered by the engineer. Production shows a real drawing labelling
   58.5 Hz / 300 s as "UF2" (Raven, ai_relay_recloser_uf2_setting, Pass). That
   planset has correct protection with the labels transposed. Simply swapping
   the table would have turned 28 passing findings into false Fails, so the
   prompt now matches on the setpoint SET and demotes a pure labelling
   difference to low-severity Needs Review.

Run: PYTHONPATH=backend python backend/scripts/test_relay_setpoints.py
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.gemini_analyzer import _RELAY_SETTINGS_PROMPT as P  # noqa: E402

_FAILS: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _FAILS.append(name)


def rows() -> dict[str, list[tuple[str, str]]]:
    """Every '| FUNC | dev | value | ... | time |' row, keyed by function."""
    out: dict[str, list[tuple[str, str]]] = {}
    for line in P.splitlines():
        m = re.match(r"\s*\|\s*(OV\d|UV\d|OF\d|UF\d)\s*\|", line)
        if not m:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        value = next((c for c in cells[2:] if re.search(r"\d", c) and c != "—"), "")
        time = next((c for c in reversed(cells) if c.endswith("s")), "")
        out.setdefault(m.group(1), []).append((value, time))
    return out


R = rows()

print("IEEE 1547-2018 Table 18 — frequency trip settings:")
check("UF1 = 58.5 Hz / 300 s in every table",
      bool(R.get("UF1")) and all("58.5" in v and "300" in t for v, t in R["UF1"]))
check("UF2 = 56.5 Hz / 0.16 s in every table",
      bool(R.get("UF2")) and all("56.5" in v and "0.16" in t for v, t in R["UF2"]))
check("OF1 = 61.2 Hz / 300 s (unchanged)",
      bool(R.get("OF1")) and all("61.2" in v and "300" in t for v, t in R["OF1"]))
check("OF2 = 62.0 Hz / 0.16 s (unchanged)",
      bool(R.get("OF2")) and all("62.0" in v and "0.16" in t for v, t in R["OF2"]))

print("The old transcription is gone:")
check("no table pairs UF2 with 300 s",
      not any("58.5" in v and "300" in t for v, t in R.get("UF2", [])))
check("no table pairs UF1 with 0.16 s",
      not any("56.5" in v and "0.16" in t for v, t in R.get("UF1", [])))

print("Internal convention — element 2 is further from nominal and faster:")
for fn, near, far in (("UF", "UF1", "UF2"), ("OF", "OF1", "OF2")):
    n = R.get(near, [("", "")])[0]
    f = R.get(far, [("", "")])[0]
    check(f"{fn}: element 1 trips slower than element 2",
          "300" in n[1] and "0.16" in f[1])
check("UV: element 1 (0.70 p.u.) slower than element 2 (0.45 p.u.)",
      "2.0s" in R.get("UV1", [("", "")])[0][1]
      and "0.16" in R.get("UV2", [("", "")])[0][1])
check("OV: element 1 (1.10 p.u.) slower than element 2 (1.20 p.u.)",
      "2.0s" in R.get("OV1", [("", "")])[0][1]
      and "0.16" in R.get("OV2", [("", "")])[0][1])

print("Both the recloser and inverter tables were corrected:")
check("UF1 appears in 2 tables", len(R.get("UF1", [])) == 2)
check("UF2 appears in 2 tables", len(R.get("UF2", [])) == 2)

print("Matching is by VALUE, not by element label:")
check("prompt tells the model to match on setpoint values",
      "SETPOINT VALUES, not on the element label" in P)
check("prompt states IEEE does not fix element numbering",
      "does NOT fix which element number" in P)
check("required underfrequency set is stated explicitly",
      "58.5 Hz / 300 s" in P and "56.5 Hz / 0.16 s" in P)
check("required overfrequency set is stated explicitly",
      "61.2 Hz / 300 s" in P and "62.0 Hz / 0.16 s" in P)
check("a transposed label is Needs Review, never a Fail",
      "never a Fail" in P and "Needs Review at LOW severity" in P)
check("Fail is reserved for a missing or deviating trip point",
      "MISSING" in P and "DEVIATES" in P)
check("evidence must name which required point each value satisfies",
      "which required point each one satisfies" in P)

print("Enumerated registry (when relay is enumerated) must agree with the standard:")
# The draft registry carried the SAME inverted UF pairing as the prompt, in 4
# of its 37 checks. Enumeration replaces the hand-written prompt entirely, so a
# registry that reverts the values would silently undo the fix above. This guard
# is mechanical on purpose — it should not depend on a reviewer noticing.
from app import check_ids as ci  # noqa: E402

# (element, required magnitude, required clearing time)
IEEE_1547_CAT_I = (
    ("UF1", "58.5", "300"), ("UF2", "56.5", "0.16"),
    ("OF1", "61.2", "300"), ("OF2", "62.0", "0.16"),
    ("OV1", "1.10", "2.0"), ("OV2", "1.20", "0.16"),
    ("UV1", "0.70", "2.0"), ("UV2", "0.45", "0.16"),
)
# Pairings that are WRONG — the inversion this file exists to prevent.
FORBIDDEN = (("UF1", "56.5"), ("UF2", "58.5"),
             ("OF1", "62.0"), ("OF2", "61.2"))

relay_checks = ci.checks_for("relay")
if not relay_checks:
    print("  SKIP  relay not enumerated yet — guard is armed for when it is")
else:
    joined = " ".join((c.instruction or "") + " " + (c.title or "")
                      for c in relay_checks)
    for elem, wrong in FORBIDDEN:
        near = re.search(
            rf"{elem}[^.\n]{{0,90}}{re.escape(wrong)}|{re.escape(wrong)}[^.\n]{{0,90}}{elem}",
            joined)
        check(f"registry never pairs {elem} with {wrong}", near is None)
    for elem, mag, _t in IEEE_1547_CAT_I:
        mentions = [c for c in relay_checks
                    if elem in ((c.instruction or "") + (c.title or ""))]
        if mentions:
            ok = any(mag in (c.instruction or "") for c in mentions)
            check(f"registry states {elem} = {mag}", ok)
    check("registry keeps value-based matching, not label-based",
          "element number" in joined.lower() or "setpoint value" in joined.lower())

print()
if _FAILS:
    print(f"FAILED ({len(_FAILS)}): {_FAILS}")
    sys.exit(1)
print("ALL RELAY SETPOINT CHECKS PASSED")
