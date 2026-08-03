"""Voltage unit normalization (bare-kV bug found on prod run 0909b346 / Bagby).

The extractor returned poi_voltage="34.5" for a drawing reading "34.5 kV",
which silently corrupted three engineering results:
  * NEC 110.26 working clearances  3.0/3.0/3.0 ft  instead of 5.0/6.0/9.0 ft
  * transformer BIL class          95 kV required  instead of 150 kV
  * MV FLA                         41,837 A        instead of 41.8 A

Run: PYTHONPATH=backend python backend/scripts/test_voltage_units.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.electrical_calcs import (  # noqa: E402
    _volts, validate_ac_ampacity, validate_mv_ampacity,
    validate_nec_clearances, validate_transformer,
)

_FAILS: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _FAILS.append(name)


print("Normalizer — bare kV becomes volts:")
for raw, want in [("34.5", 34500.0), ("12.47", 12470.0), ("4.16", 4160.0),
                  ("69", 69000.0), ("0.8", 800.0)]:
    check(f"'{raw}' -> {want:.0f} V", _volts({"poi_voltage": raw}, "poi_voltage") == want)

print("Normalizer — real volt values pass through unchanged:")
for raw, want in [("208", 208.0), ("240", 240.0), ("277", 277.0), ("480", 480.0),
                  ("600", 600.0), ("690", 690.0), ("800", 800.0),
                  ("34500", 34500.0), ("12470", 12470.0)]:
    check(f"'{raw}' stays {want:.0f} V", _volts({"poi_voltage": raw}, "poi_voltage") == want)

print("Normalizer — explicit units and messy strings:")
check("'34.5 kV' -> 34500", _volts({"v": "34.5 kV"}, "v") == 34500.0)
check("'12.47kV' -> 12470", _volts({"v": "12.47kV"}, "v") == 12470.0)
check("'480 V' -> 480", _volts({"v": "480 V"}, "v") == 480.0)
check("'34,500' (comma) -> 34500", _volts({"v": "34,500"}, "v") == 34500.0)
check("key precedence: first present wins",
      _volts({"a": "", "b": "480"}, "a", "b") == 480.0)
check("missing/garbage -> None",
      _volts({}, "nope") is None and _volts({"v": "n/a"}, "v") is None)
check("zero/negative ignored", _volts({"v": "0"}, "v") is None)

print("Downstream calcs with the real Bagby inputs (poi_voltage='34.5'):")
bagby = {"poi_voltage": "34.5", "transformer_kva": "2500",
         "inverter_kva": "250", "transformer_bil": "150"}

clr = validate_nec_clearances(bagby)
check("NEC 110.26 uses the 25 kV row (5/6/9 ft), not the 150 V row",
      clr.computed.get("condition_3_ft") == 9.0
      and clr.computed.get("condition_1_ft") == 5.0)
check("clearance evidence reports 34500 V, not 34 V", "34500" in clr.evidence)

xfmr = validate_transformer(bagby)
check("transformer BIL class resolves to 34.5 kV -> 150 kV required",
      xfmr.computed.get("required_bil_kv") == 150.0
      and xfmr.computed.get("voltage_class_kv") == "34.5")

mv = validate_mv_ampacity(bagby)
check("MV FLA ~41.8 A (not 41,837 A)", 40.0 < mv.computed["mv_fla"] < 45.0)
check("MV cable sizing is sane (<= 4/0), not '>1000 kcmil'",
      mv.computed["min_mv_wire"] in ("1/0", "2/0", "3/0", "4/0"))

print("Regression — an LV secondary must NOT be rescaled:")
lv = {"inverter_kva": "275", "inverter_quantity": "9",
      "transformer_secondary_voltage": "800"}
ac = validate_ac_ampacity(lv)
check("800 V secondary stays 800 V", ac.computed["voltage"] == 800.0)
check("per-inverter FLA ~198 A at 800 V (sanity)",
      190.0 < ac.computed["fla_per_inverter"] < 205.0)

print()
if _FAILS:
    print(f"FAILED ({len(_FAILS)}): {_FAILS}")
    sys.exit(1)
print("ALL VOLTAGE-UNIT CHECKS PASSED")
