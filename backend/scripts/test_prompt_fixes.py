"""Static prompt-contract regression tests for the E1300 QC-feedback fixes.

These are deterministic string assertions (no model calls) that lock in the
four prompt/rules fixes from the run d8a0b104 audit and guard against silent
regression — including the over-correction guardrails (e.g. MCOV must still
Needs-Review unknown grounding; BIL must still flag below 95 kV).

Run:  PYTHONPATH=backend python backend/scripts/test_prompt_fixes.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import inspect  # noqa: E402
import yaml  # noqa: E402
from app import gemini_analyzer  # noqa: E402
from app.gemini_analyzer import _SLD_PROMPT  # noqa: E402

_FAILS: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _FAILS.append(name)


print("Item 1 — MCOV (grounding-aware, 12.47 kV false-fail cleared):")
check("12.47 kV row now ACCEPTs 7.65 kV", "ACCEPT 7.65kV" in _SLD_PROMPT)
check("old sole-value '...should be 8.4kV' premise removed",
      "12470Y/7200 (4-wire multigrounded): MCOV should be 8.4kV" not in _SLD_PROMPT)
check("guardrail: unknown grounding -> Needs Review (no silent Pass)",
      "Needs Review" in _SLD_PROMPT and "do NOT assume multigrounded and do NOT Pass" in _SLD_PROMPT)
check("guardrail: ungrounded still requires higher MCOV (IEEE C62.22)",
      "ungrounded" in _SLD_PROMPT and "C62.22" in _SLD_PROMPT)

print("Item 2 — recloser BIL (accept {95,110}, still flag below 95):")
check("accepts 95kV OR 110kV", "95kV OR 110kV" in _SLD_PROMPT)
check("old 'BIL typically 110kV' premise removed", "BIL typically 110kV" not in _SLD_PROMPT)
check("guardrail: still flags BIL below 95kV", "BELOW 95kV" in _SLD_PROMPT)

print("Item 3 — KNAN/ONAN ester cooling recognized:")
check("recognizes KNAN", "KNAN" in _SLD_PROMPT)
check("recognizes KNAF", "KNAF" in _SLD_PROMPT)
check("explicitly do NOT flag KNAN/KNAF", "Do NOT flag KNAN or KNAF" in _SLD_PROMPT)
check("guardrail: still flags genuine fluid/class mismatch",
      "mineral-oil unit carrying a K-class" in _SLD_PROMPT)

print("Decimal-separator guardrail (Bagby 4,779.000 kW false positive):")
# _GLOBAL_INSTRUCTIONS is a local inside run_gemini_checks and is appended to
# EVERY vision prompt — assert against the function source so the guardrail
# can't be dropped without failing here.
_src = inspect.getsource(gemini_analyzer.run_gemini_checks)
check("global instructions carry the number-reading rule (9b)",
      "READING NUMBERS OFF DRAWINGS" in _src)
check("uses the real failing value as the worked example",
      '"4,779.000 kW" is 4779 kW' in _src and '"4,779,000 kW"' in _src)
check("guardrail: comma = thousands, period = decimal point",
      "A comma is a THOUSANDS" in _src and "a period is a DECIMAL" in _src)
check("guardrail: 10^n discrepancy must be re-read + cross-checked first",
      "power of ten" in _src and "cross-check" in _src.lower())
check("guardrail: degrade to Needs Review, never Fail, on residual doubt",
      '"Needs Review" (not\n   "Fail")' in _src or 'as "Needs Review" (not' in _src)

print("Item 4 — 3LD keyword rule no longer claims 'no cable sizes':")
rules = yaml.safe_load(
    (Path(__file__).resolve().parents[1] / "app" / "rules.yaml").read_text(encoding="utf-8"))
checks = rules.get("checks") or rules.get("rules") or []
# rules.yaml is a flat list under some key; find the 3LD equipment rule
def _find(key):
    def walk(o):
        if isinstance(o, dict):
            if o.get("key") == key:
                return o
            for v in o.values():
                r = walk(v)
                if r:
                    return r
        elif isinstance(o, list):
            for v in o:
                r = walk(v)
                if r:
                    return r
        return None
    return walk(rules)

rule = _find("three_line_equipment_content")
check("three_line_equipment_content rule found", rule is not None)
if rule:
    check("title retitled to 'Equipment labels with ratings present'",
          rule.get("title") == "Equipment labels with ratings present")
    check("title no longer asserts '(no cable sizes)'",
          "no cable sizes" not in (rule.get("title") or "").lower())

print()
if _FAILS:
    print(f"FAILED ({len(_FAILS)}): {_FAILS}")
    sys.exit(1)
print("ALL PROMPT-CONTRACT CHECKS PASSED")
