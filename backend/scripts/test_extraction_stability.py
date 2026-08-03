"""Spec extraction must be complete, or say that it isn't.

Prod evidence (Bagby.pdf, run 0909b346): two consecutive runs of the SAME
document captured 29 and 27 calc-input fields. `poi_voltage` was read on one
run and missed on the next, which flipped four NEC calcs to "Deferred: calc
inputs not yet extracted" — a deferral that looks, on the report, like the
tool chose not to check rather than could not.

Root causes addressed here:
  * poi_voltage was only ever sought on page 1, though POI voltage is
    normally called out on the single-line, not the cover.
  * The extraction prompt says "OMIT fields you cannot find with high
    confidence", so presence is a per-call confidence sample, not a fact.
  * The cover pass ran on the cheap per-page model even during a deep run.
  * _parse_extract_json took the FIRST fenced block, and the prompt embeds
    its own ```json schema — a model that echoed the schema had the schema
    parsed as its answer.

Run: PYTHONPATH=backend python backend/scripts/test_extraction_stability.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fitz  # noqa: E402

from app import gemini_client  # noqa: E402
from app.analyzer import (  # noqa: E402
    HIGH_VALUE_FIELDS, PageInfo, _is_blank_spec, _parse_extract_json,
    extract_specs_from_pages,
)

_FAILS: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _FAILS.append(name)


print("_parse_extract_json — the schema echo must not win:")
schema_echo = (
    'Here is the schema I will fill in:\n'
    '```json\n{"poi_voltage": "POI voltage in VOLTS", '
    '"transformer_kva": "transformer rating"}\n```\n'
    'And here are the actual values:\n'
    '```json\n{"poi_voltage": "34500", "transformer_kva": "2500"}\n```'
)
got = _parse_extract_json(schema_echo)
check("returns the answer, not the schema", got.get("poi_voltage") == "34500")
check("second field also from the answer", got.get("transformer_kva") == "2500")
check("single fenced block still works",
      _parse_extract_json('```json\n{"a": "1"}\n```') == {"a": "1"})
check("bare object still works", _parse_extract_json('{"a": "1"}') == {"a": "1"})
check("prose-wrapped object still works",
      _parse_extract_json('Sure!\n{"a": "1"}\nDone') == {"a": "1"})
check("garbage -> empty dict", _parse_extract_json("nope") == {})
check("array -> empty dict (we want an object)",
      _parse_extract_json('```json\n[1,2]\n```') == {})

print("Placeholder answers are absences, not values:")
# The model answers "N/A" / "TBD" / "not shown" instead of omitting a field it
# cannot read. Counting those as present suppressed the extraction_incomplete
# warning while the calc still deferred — the exact silent-deferral this work
# exists to remove. _record() already rejected them; the new code paths did not.
for placeholder in ("", "   ", "N/A", "n/a", "TBD", "not shown", "None",
                    "null", "-", "?"):
    check(f"{placeholder!r} counts as missing", _is_blank_spec(placeholder))
for real in ("34500", "0", "0.0", "4/0 AL", "34.5 kV"):
    check(f"{real!r} counts as present", not _is_blank_spec(real))
check("None counts as missing", _is_blank_spec(None))

print("HIGH_VALUE_FIELDS covers what the NEC calcs actually gate on:")
check("poi_voltage listed", "poi_voltage" in HIGH_VALUE_FIELDS)
check("transformer voltages listed",
      "transformer_primary_voltage" in HIGH_VALUE_FIELDS
      and "transformer_secondary_voltage" in HIGH_VALUE_FIELDS)
check("transformer_kva listed", "transformer_kva" in HIGH_VALUE_FIELDS)


def build_doc():
    doc = fitz.open()
    for title in ("COVER SHEET", "AC SINGLE LINE DIAGRAM"):
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 72), title, fontsize=14)
    return doc


def infos():
    return [
        PageInfo(number=1, text="COVER SHEET", sheet_number="E-000",
                 sheet_title="COVER SHEET"),
        PageInfo(number=2, text="AC SINGLE LINE DIAGRAM", sheet_number="E-100",
                 sheet_title="AC SINGLE LINE DIAGRAM"),
    ]


print("Retry pass — a cover that omits poi_voltage gets a second look:")
calls: list[dict] = []


def fake_multi(images, prompt, mime_type="image/png", deep=False):
    calls.append({"prompt": prompt, "deep": deep, "n_images": len(images)})
    if "=== FOCUS ===" in prompt:          # the single-line retry
        return json.dumps({"poi_voltage": "34500"})
    return json.dumps({                    # the cover pass — no poi_voltage
        "module_stc_watts": "590", "string_size": "25",
        "transformer_kva": "2500", "transformer_primary_voltage": "34500",
        "transformer_secondary_voltage": "800", "transformer_impedance": "5.75",
        "inverter_kva": "275", "total_ac_kva": "2475",
    })


gemini_client.analyze_multiple_images = fake_multi
doc = build_doc()
report: dict = {}
merged, _prov = extract_specs_from_pages(doc, infos(), out_report=report)
doc.close()

check("cover pass ran deep", calls and calls[0]["deep"] is True)
check("a retry call was made", any("=== FOCUS ===" in c["prompt"] for c in calls))
retry = next((c for c in calls if "=== FOCUS ===" in c["prompt"]), None)
check("retry asked only for the missing field",
      retry is not None and "poi_voltage" in retry["prompt"])
check("retry did not re-request fields already found",
      retry is not None and "transformer_kva" not in retry["prompt"].split("=== FOCUS ===")[1])
check("retry ran deep too", retry is not None and retry["deep"] is True)
check("poi_voltage recovered from the single-line", merged.get("poi_voltage") == "34500")
check("report says nothing high-value is missing",
      report.get("high_value_missing") == [])
check("report records that the retry fired",
      report.get("high_value_retried") is True)
check("report targeted the single-line sheet", report.get("retry_pages") == [2])
check("report counts fields", report.get("field_count") == len(merged))

print("No retry when the cover already had everything:")
calls.clear()


def fake_complete(images, prompt, mime_type="image/png", deep=False):
    calls.append({"prompt": prompt, "deep": deep})
    return json.dumps({f: "1" for f in HIGH_VALUE_FIELDS})


gemini_client.analyze_multiple_images = fake_complete
doc = build_doc()
report2: dict = {}
extract_specs_from_pages(doc, infos(), out_report=report2)
doc.close()
check("no FOCUS retry issued",
      not any("=== FOCUS ===" in c["prompt"] for c in calls))
check("report shows retry did not fire",
      report2.get("high_value_retried") is False)
check("nothing missing", report2.get("high_value_missing") == [])

print("Missing field survives a failed retry and is reported, not hidden:")
calls.clear()


def fake_nothing(images, prompt, mime_type="image/png", deep=False):
    return json.dumps({"module_stc_watts": "590"})


gemini_client.analyze_multiple_images = fake_nothing
doc = build_doc()
report3: dict = {}
merged3, _ = extract_specs_from_pages(doc, infos(), out_report=report3)
doc.close()
check("high-value fields reported missing",
      set(report3.get("high_value_missing") or []) == set(HIGH_VALUE_FIELDS))
check("the field really is absent (not faked)", "poi_voltage" not in merged3)
check("what WAS found is still kept", merged3.get("module_stc_watts") == "590")

print("A crashing retry must not lose the cover pass:")
calls.clear()


def fake_boom(images, prompt, mime_type="image/png", deep=False):
    if "=== FOCUS ===" in prompt:
        raise RuntimeError("provider 500")
    return json.dumps({"transformer_kva": "2500"})


gemini_client.analyze_multiple_images = fake_boom
doc = build_doc()
report4: dict = {}
merged4, _ = extract_specs_from_pages(doc, infos(), out_report=report4)
doc.close()
check("cover-pass value survived the retry crash",
      merged4.get("transformer_kva") == "2500")
check("report still produced", report4.get("field_count") is not None)
check("poi_voltage still listed missing",
      "poi_voltage" in (report4.get("high_value_missing") or []))

print()
if _FAILS:
    print(f"FAILED ({len(_FAILS)}): {_FAILS}")
    sys.exit(1)
print("ALL EXTRACTION-STABILITY CHECKS PASSED")
