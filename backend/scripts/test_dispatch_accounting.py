"""A vision check family must never disappear from a report without saying so.

Prod evidence (Bagby.pdf, run 0909b346): the same document produced 266, then
225, then 225 findings across three runs. A ~41-finding swing is roughly one
entire ai_sld dispatch, and nothing in the report said a check had not run —
"no findings" and "never ran" rendered identically, i.e. as silence.

Silence on a QC report reads as "this sheet is clean". For a planset heading
to a construction site that is the most expensive possible failure mode, so a
family that returns nothing now emits a Deferred `<prefix>_review_incomplete`
row, and every dispatch is recorded in the run summary.

Runs the real run_gemini_checks against a synthetic PDF with the model stubbed.

Run: PYTHONPATH=backend python backend/scripts/test_dispatch_accounting.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fitz  # noqa: E402

from app import gemini_analyzer, gemini_client  # noqa: E402
from app.analyzer import PageInfo  # noqa: E402

_FAILS: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _FAILS.append(name)


def build_doc() -> fitz.Document:
    """A small planset whose sheet titles match several check families."""
    doc = fitz.open()
    for title in ("COVER SHEET", "SINGLE LINE DIAGRAM", "GROUNDING PLAN",
                  "EQUIPMENT AREA PLAN", "RELAY SETTINGS"):
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 72), title, fontsize=14)
    return doc


def page_infos() -> list[PageInfo]:
    titles = ["COVER SHEET", "SINGLE LINE DIAGRAM", "GROUNDING PLAN",
              "EQUIPMENT AREA PLAN", "RELAY SETTINGS"]
    return [
        PageInfo(number=i + 1, text=t, sheet_number=f"E-{100 + i}", sheet_title=t)
        for i, t in enumerate(titles)
    ]


def run(stub_response: str) -> tuple[list[dict], list[dict]]:
    """Run every vision check with the model stubbed to one canned response."""
    gemini_client.analyze_page_image = lambda *a, **k: stub_response
    gemini_client.analyze_multiple_images = lambda *a, **k: stub_response
    doc = build_doc()
    pages = page_infos()
    dispatch: list[dict] = []
    with tempfile.TemporaryDirectory() as tmp:
        issues = gemini_analyzer.run_gemini_checks(
            doc=doc,
            pages=pages,
            page_map={p.sheet_number: p for p in pages},
            run_id="test-run",
            run_dir=Path(tmp),
            actual_numbers=[p.sheet_number for p in pages],
            use_deep=False,
            out_dispatch=dispatch,
        )
    doc.close()
    return issues, dispatch


print("Every family returns nothing — each must be accounted for:")
issues, dispatch = run("[]")
incomplete = [i for i in issues if i["item_key"].endswith("_review_incomplete")]
check("at least one family was dispatched", len(dispatch) > 0)
check("a dispatch record carries the pages it was given",
      any(d["pages"] for d in dispatch))
check("a dispatch record carries its item_key prefix",
      all(d["prefix"] or d["prefix"] is None for d in dispatch)
      and any(d["prefix"] for d in dispatch))
check("empty families produced _review_incomplete rows", len(incomplete) > 0)
check("one row per empty family, no duplicates",
      len({i["item_key"] for i in incomplete}) == len(incomplete))
check("rows are Deferred, never Pass — unreviewed is not passing",
      all(i["status"] == "Deferred" for i in incomplete))
check("rows say the sheet was not assessed",
      all("unreviewed" in (i["evidence"] or "") for i in incomplete))
check("every dispatched family with a prefix is accounted for",
      {d["prefix"] for d in dispatch if d["prefix"]}
      == {i["item_key"].replace("_review_incomplete", "") for i in incomplete})
check("findings_returned recorded as 0",
      all(d["findings_returned"] == 0 for d in dispatch))

print("Keys are code-minted, so they are stable across runs:")
issues2, _ = run("[]")
check("identical incomplete keys on a second run",
      {i["item_key"] for i in issues2 if i["item_key"].endswith("_review_incomplete")}
      == {i["item_key"] for i in incomplete})

print("Families that DO return findings must not be flagged incomplete:")
good = json.dumps([
    {"check": "thing_one", "status": "Pass", "evidence": "looks fine"},
    {"check": "thing_two", "status": "Fail", "evidence": "wrong value",
     "location_text": "COVER SHEET"},
])
issues3, dispatch3 = run(good)
incomplete3 = [i for i in issues3 if i["item_key"].endswith("_review_incomplete")]
check("no _review_incomplete rows when findings came back", incomplete3 == [])
check("dispatch records show findings returned",
      all(d["findings_returned"] and d["findings_returned"] > 0 for d in dispatch3))
check("the real findings survived", len(issues3) > 0)
check("ok flag set true", all(d["ok"] is True for d in dispatch3))

print("A model that returns unparseable junk is treated as incomplete:")
issues4, _ = run("I'm sorry, I can't help with that.")
incomplete4 = [i for i in issues4 if i["item_key"].endswith("_review_incomplete")]
check("junk response yields incomplete rows, not silence", len(incomplete4) > 0)

print("A crashed provider call is distinguishable from an empty one:")
# _safe_gemini_call swallows exceptions and returns [], so relying on it left
# record["ok"] permanently True and a 500 from the provider was reported as
# "the AI review returned no findings" — i.e. as a clean sheet. One of those
# is worth re-running, the other is a real (if empty) review.


def boom(*a, **k):
    raise RuntimeError("provider 500")


gemini_client.analyze_page_image = boom
gemini_client.analyze_multiple_images = boom
doc = build_doc()
pages = page_infos()
crash_dispatch: list[dict] = []
with tempfile.TemporaryDirectory() as tmp:
    crash_issues = gemini_analyzer.run_gemini_checks(
        doc=doc, pages=pages, page_map={p.sheet_number: p for p in pages},
        run_id="crash-run", run_dir=Path(tmp),
        actual_numbers=[p.sheet_number for p in pages],
        use_deep=False, out_dispatch=crash_dispatch,
    )
doc.close()
crash_rows = [i for i in crash_issues
              if i["item_key"].endswith("_review_incomplete")]
check("crashed calls are recorded ok=False",
      crash_dispatch and all(d["ok"] is False for d in crash_dispatch))
check("crash still produces incomplete rows", len(crash_rows) > 0)
check("the row says an error occurred, not 'no findings'",
      all("raised an error" in (i["evidence"] or "") for i in crash_rows))
check("an empty (non-crashing) run says the opposite",
      all("returned no findings" in (i["evidence"] or "") for i in incomplete))

print("The dispatch record is JSON-serialisable for the run summary:")
try:
    json.dumps(dispatch)
    ok = True
except (TypeError, ValueError):
    ok = False
check("json.dumps(vision_dispatch) works", ok)

print()
if _FAILS:
    print(f"FAILED ({len(_FAILS)}): {_FAILS}")
    sys.exit(1)
print("ALL DISPATCH-ACCOUNTING CHECKS PASSED")
