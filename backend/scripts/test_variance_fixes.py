"""Phase 2 variance fixes — response parsing and page-aware dedup.

Measured on three deep runs of the same planset (Bagby.pdf, prod run
0909b346): only 27% of item_keys appeared in all three, and finding counts
swung 266/225/225. These are the silent data-loss paths found underneath that
number:

  * _extract_json returned parsed["findings"] and discarded every sibling
    key, so an `open_findings` escape hatch — the coverage safety net for any
    enumerated-check prompt — would have been dropped without a log line.
  * The last-resort bracket scan used find("[") + rfind("]"), which spans
    `], "open_findings": [` and fails to parse, losing the ENTIRE family.
  * A response truncated by a token ceiling parsed to [], so the whole check
    family vanished rather than keeping the findings that did close.
  * Multi-page dedup keyed on the check name alone and fired BEFORE page
    attribution, collapsing the same defect found on three sheets into one.

Run: PYTHONPATH=backend python backend/scripts/test_variance_fixes.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.gemini_analyzer import (  # noqa: E402
    _balanced_end, _cross_page_check_names, _extract_json,
    _findings_from_obj, _pick_page_for_finding, _salvage_objects,
)

_FAILS: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _FAILS.append(name)


print("_balanced_end — string-aware bracket matching:")
check("simple array", _balanced_end("[1, 2]", 0) == 6)
check("nested object", _balanced_end('{"a": {"b": [1]}}', 0) == 17)
check("bracket inside a string does not move depth",
      _balanced_end('["a ] b"]', 0) == 9)
check("escaped quote inside a string",
      _balanced_end('["say \\" ] now"]', 0) == 16)
check("unterminated array -> None", _balanced_end('[{"a": 1},', 0) is None)
check("non-bracket start -> None", _balanced_end("hello", 0) is None)

print("_extract_json — the ordinary shapes still work:")
check("bare array",
      _extract_json('[{"check": "a"}, {"check": "b"}]')
      == [{"check": "a"}, {"check": "b"}])
check("fenced array",
      _extract_json('```json\n[{"check": "a"}]\n```') == [{"check": "a"}])
check("prose around a fenced array",
      _extract_json('Here you go:\n```json\n[{"check": "a"}]\n```\nDone.')
      == [{"check": "a"}])
check("findings envelope",
      _extract_json('{"findings": [{"check": "a"}]}') == [{"check": "a"}])
check("bare object is treated as one finding",
      _extract_json('{"check": "a", "status": "Pass"}')
      == [{"check": "a", "status": "Pass"}])
check("garbage -> empty", _extract_json("no json here at all") == [])
check("non-dict array members are dropped",
      _extract_json('[{"check": "a"}, "stray", 7]') == [{"check": "a"}])

print("_extract_json — sibling arrays survive (the Tier B safety net):")
env = ('{"findings": [{"check": "sld_mcov"}], '
       '"open_findings": [{"check": "bus mislabelled", "status": "Fail"}]}')
got = _extract_json(env)
check("both arrays imported", len(got) == 2)
check("primary finding kept", got[0]["check"] == "sld_mcov")
check("exploratory finding kept", got[1]["check"] == "bus mislabelled")
check("exploratory finding is tagged", got[1].get("_exploratory") is True)
check("primary finding is NOT tagged", "_exploratory" not in got[0])
check("fenced envelope with siblings also works",
      len(_extract_json(f"```json\n{env}\n```")) == 2)
check("exploratory_findings alias",
      len(_extract_json('{"findings": [], "exploratory_findings": '
                        '[{"check": "x"}]}')) == 1)

print("_extract_json — unfenced envelope with a sibling array:")
# The old find('[')+rfind(']') span reads `[...], "open_findings": [...]`,
# which is not valid JSON, so the whole family was lost.
messy = ('Sure thing.\n{"findings": [{"check": "a"}], '
         '"open_findings": [{"check": "b"}]}\nHope that helps.')
got = _extract_json(messy)
check("recovers both arrays from surrounding prose", len(got) == 2)
check("does not return empty", got != [])

print("_extract_json — truncation salvage:")
truncated = ('[{"check": "a", "status": "Pass"}, '
             '{"check": "b", "status": "Fail"}, {"check": "c", "sta')
got = _extract_json(truncated)
check("salvages the complete objects", len(got) == 2)
check("salvaged content is right",
      [f["check"] for f in got] == ["a", "b"])
check("truncated-object remnant is not invented", "c" not in
      [f.get("check") for f in got])
check("_salvage_objects on an empty array start", _salvage_objects("[", 0) == [])

print("_findings_from_obj — envelope handling:")
check("no findings array -> None (caller treats as single finding)",
      _findings_from_obj({"check": "a"}) is None)
check("issues alias recognised",
      _findings_from_obj({"issues": [{"check": "a"}]}) == [{"check": "a"}])
check("empty findings array is an envelope, not a finding",
      _findings_from_obj({"findings": []}) == [])

print("_pick_page_for_finding — page attribution:")
pages = [6, 7, 8]
check("page_index 0-based into the batch",
      _pick_page_for_finding({"page_index": 1}, pages) == 7)
check("actual pdf page number passes through",
      _pick_page_for_finding({"page": 8}, pages) == 8)
check("no hint -> None", _pick_page_for_finding({"check": "a"}, pages) is None)
check("out-of-range index -> None",
      _pick_page_for_finding({"page_index": 9}, pages) is None)

print("_cross_page_check_names — only names that really span sheets:")
findings = [
    {"check": "egc_size", "page_index": 0},
    {"check": "egc_size", "page_index": 1},   # same check, different sheet
    {"check": "bil_rating", "page_index": 0},
    {"check": "bil_rating", "page_index": 0},  # same check, SAME sheet
    {"check": "no_page_hint"},
]
spanning = _cross_page_check_names(findings, pages)
check("a check seen on two sheets is flagged", "egc_size" in spanning)
check("a check repeated on one sheet is NOT flagged",
      "bil_rating" not in spanning)
check("a check with no page hint is NOT flagged",
      "no_page_hint" not in spanning)
check("empty input is safe", _cross_page_check_names([], pages) == set())

print("_cross_page_check_names — order independence (no ordering churn):")
# Suffixing only the *later* instances would make the bare key depend on
# response order, so the same document could churn keys between runs purely
# from the model reordering its output.
check("reversed response yields the identical set",
      _cross_page_check_names(list(reversed(findings)), pages) == spanning)

print("Regressions caught in adversarial review of this very change:")
# 1. A stray "{" in prose ahead of the payload used to abort the whole scan
#    (the loop `break`ed instead of continuing), so a real Fail vanished and
#    surfaced only as "unreviewed". The pre-change find("[")+rfind("]") got
#    this right, so it was a regression, not an old gap.
got = _extract_json(
    'Per note {see E-101 the EGC is undersized:\n'
    '[{"check": "egc", "status": "Fail"}]')
check("stray '{' in prose does not abort the scan",
      got == [{"check": "egc", "status": "Fail"}])
check("stray '[' in prose does not abort the scan either",
      _extract_json('Refer to [E-101]:\n[{"check": "a", "status": "Fail"}]')
      == [{"check": "a", "status": "Fail"}])

# 2. Returning the FIRST block that merely parsed meant a decoy or echoed
#    schema fence swallowed the real answer. ~20 prompts embed a fenced
#    ```json example, so this was reachable in production.
check("an empty leading envelope does not swallow a later populated fence",
      _extract_json(
          '```json\n{"findings": []}\n```\nActually, here they are:\n'
          '```json\n[{"check": "egc", "status": "Fail"}]\n```')
      == [{"check": "egc", "status": "Fail"}])
check("an echoed schema fence does not beat the real answer",
      _extract_json(
          '```json\n[{"check": "descriptive_check_name"}]\n```\n'
          'Now the findings:\n'
          '```json\n[{"check": "sld_mcov", "status": "Pass"}]\n```')
      == [{"check": "sld_mcov", "status": "Pass"}])
check("a genuinely empty result is still empty (not invented)",
      _extract_json('```json\n{"findings": []}\n```') == [])

# 3. Truncation salvage only handled a bare array; a truncated ENVELOPE
#    still lost everything, even though envelopes are now accepted.
check("truncated envelope salvages its complete findings",
      _extract_json('{"findings": [{"check": "a", "status": "Fail"}, {"che')
      == [{"check": "a", "status": "Fail"}])
check("truncated fenced envelope salvages too",
      len(_extract_json(
          '```json\n{"findings": [{"check": "a"}, {"check": "b"}, {"ch')) == 2)

# 4. Treating any lone object as a finding would turn an echoed config blob
#    into a junk row with a positional item_key.
check("a non-finding-shaped object is not imported as a finding",
      _extract_json('Config: {"model": "gpt", "temperature": 1}') == [])
check("a finding-shaped lone object still is",
      _extract_json('{"check": "a", "status": "Pass"}')
      == [{"check": "a", "status": "Pass"}])

print("regression — a realistic full response still parses:")
real = json.dumps([
    {"check": "Ground rods and spacing", "status": "Pass",
     "evidence": "8 rods @ 20 ft", "page_index": 0},
    {"check": "EGC sizing", "status": "Fail",
     "evidence": "#6 shown, 250.122 requires #4", "page_index": 2},
])
got = _extract_json(real)
check("2 findings", len(got) == 2)
check("evidence preserved", "250.122" in got[1]["evidence"])
check("page attribution still resolves",
      _pick_page_for_finding(got[1], pages) == 8)

print()
if _FAILS:
    print(f"FAILED ({len(_FAILS)}): {_FAILS}")
    sys.exit(1)
print("ALL VARIANCE-FIX CHECKS PASSED")
