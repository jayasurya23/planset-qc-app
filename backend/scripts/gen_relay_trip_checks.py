"""Expand ONE reviewed trip-check template into the 16 relay/inverter checks.

WHY THIS IS GENERATED RATHER THAN AUTHORED. The 16 trip checks differ only in
device, element, and setpoint; their instructions were ~3,000 characters each
and near-identical. Authoring them by hand had two consequences, both observed:

  * the IEEE 1547-2018 underfrequency values were inverted in some checks and
    not others (UF1 paired with 56.5 Hz, UF2 with 58.5 Hz — Table 18 is the
    reverse), because nothing forced the sixteen copies to agree; and
  * ~47,000 of the registry's ~71,000 instruction characters were these
    near-duplicates, which is what exceeded a single model response.

Here the setpoints live in ONE table, reviewed once. A value can no longer be
right in relay_uf1 and wrong in inverter_uf1, because both are rendered from
the same row.

Usage:
    python backend/scripts/gen_relay_trip_checks.py \\
        --template D:/tmp/relay_trip_template.json \\
        --round1   D:/tmp/relay_final.json \\
        --out      D:/tmp/relay_trip_checks.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# IEEE 1547-2018 Category I defaults. Guarded by
# backend/scripts/test_relay_setpoints.py, which fails the build on the known
# inverted pairings. Element numbering is the DESIGNER's choice and is
# deliberately not used for matching — see the template's PASS branch.
#   element, ANSI device, quantity, Category I magnitude, max clearing time
TRIP_FUNCTIONS = [
    ("OV2", "59-2", "voltage",   "1.20 p.u.", "0.16 s"),
    ("OV1", "59-1", "voltage",   "1.10 p.u.", "2.0 s"),
    ("UV1", "27-1", "voltage",   "0.70 p.u.", "2.0 s"),
    ("UV2", "27-2", "voltage",   "0.45 p.u.", "0.16 s"),
    ("OF2", "81O-2", "frequency", "62.0 Hz",  "0.16 s"),
    ("OF1", "81O-1", "frequency", "61.2 Hz",  "300 s"),
    ("UF1", "81U-1", "frequency", "58.5 Hz",  "300 s"),
    ("UF2", "81U-2", "frequency", "56.5 Hz",  "0.16 s"),
]

DEVICES = [("relay", "recloser"), ("inverter", "inverter")]


# Phrases that turn the shared procedure from "about THIS check" into "about
# whichever check cites it", so it can be stated once for the whole family.
_GENERIC = {
    "{DEVICE}": "the device named in the check",
    "{ELEMENT}": "the element named in the check",
    "{ANSI}": "that element's ANSI number",
    "{QUANTITY}": "the quantity named in the check",
    "{MAGNITUDE}": "the magnitude stated in the check",
    "{TIME}": "the maximum clearing time stated in the check",
}


def build_preamble(template: dict) -> str:
    """The grading procedure, stated ONCE for the family.

    Substituting each check's specifics into its own copy produced 16 blocks of
    ~9,200 characters — 143,000 for one call, three times the pre-enumeration
    size. The procedure is identical across all 16; only the element, device and
    setpoint differ, and those stay in the per-check line.
    """
    body = template["instruction_template"]
    for ph, generic in _GENERIC.items():
        body = body.replace(ph, generic)
    both_tables = "\n\n".join(
        t for t in (template.get("category_table_voltage", ""),
                    template.get("category_table_frequency", "")) if t)
    body = body.replace("{CATEGORY_TABLE}", both_tables)
    header = (
        "=== SHARED TRIP-POINT PROCEDURE ===\n"
        "Every trip-point check below is graded by this one procedure. Read it "
        "once and apply it to each check using that check's own device, "
        "element and setpoint.\n\n"
    )
    return header + body


def build(template: dict, round1: dict) -> list[dict]:
    """Render 16 checks, carrying each id's aliases forward from round 1."""
    prior = {c["id"]: c for c in round1.get("checks", [])}
    tmpl = template["instruction_template"]
    title_tmpl = template.get("title_template") or "{DEVICE} {ELEMENT} trip setting"
    tables = {
        "voltage": template.get("category_table_voltage", ""),
        "frequency": template.get("category_table_frequency", ""),
    }
    refs = {
        "voltage": template.get("nec_ref_voltage", ""),
        "frequency": template.get("nec_ref_frequency", ""),
    }

    out: list[dict] = []
    missing: list[str] = []
    for prefix, device_word in DEVICES:
        for elem, ansi, quantity, magnitude, time in TRIP_FUNCTIONS:
            cid = f"{prefix}_{elem.lower()}"
            subs = {
                "{DEVICE}": device_word,
                "{ELEMENT}": elem,
                "{ANSI}": ansi,
                "{QUANTITY}": quantity,
                "{MAGNITUDE}": magnitude,
                "{TIME}": time,
                "{CATEGORY_TABLE}": tables[quantity],
            }
            # Only the distinguishing facts; the procedure lives in the
            # family preamble rendered once above the checklist.
            instruction = (
                f"Grade the {device_word} {quantity} trip point {elem} ({ansi}). "
                f"Its IEEE 1547-2018 Category I default is {magnitude} with a "
                f"maximum clearing time of {time}. Apply the SHARED TRIP-POINT "
                f"PROCEDURE stated above in full — establish the applicable "
                f"abnormal operating performance category first, match the "
                f"candidate row by SETPOINT VALUE rather than element label, "
                f"treat the clearing time as a maximum, and use the ordered "
                f"status ladder. Grade only this one point and only on the "
                f"{device_word}.")
            title = title_tmpl
            for k, v in subs.items():
                title = title.replace(k, v)
            if "{" in instruction:
                missing.append(f"{cid}: unsubstituted placeholder")
            was = prior.get(cid, {})
            out.append({
                "id": cid,
                "title": title.strip(),
                "instruction": instruction.strip(),
                "nec_ref": refs.get(quantity) or was.get("nec_ref", ""),
                # These checks all state their own ladder, so absence_is is
                # recorded for the register but no generic line is rendered.
                "absence_is": was.get("absence_is", "defect"),
                "aliases": was.get("aliases", []),
            })
    if missing:
        print("TEMPLATE PROBLEMS:")
        for m in missing:
            print("  -", m)
        sys.exit(1)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--template", required=True)
    ap.add_argument("--round1", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--preamble-out", required=True)
    a = ap.parse_args()

    template = json.loads(Path(a.template).read_text(encoding="utf-8"))
    round1 = json.loads(Path(a.round1).read_text(encoding="utf-8"))
    checks = build(template, round1)

    # The guard this file exists to make unnecessary — assert it anyway.
    forbidden = (("uf1", "56.5"), ("uf2", "58.5"), ("of1", "62.0"), ("of2", "61.2"))
    for c in checks:
        for elem, wrong in forbidden:
            if c["id"].endswith(elem) and wrong in c["instruction"].split("CATEGORY")[0]:
                print(f"REFUSING: {c['id']} carries the inverted value {wrong}")
                return 1

    total = sum(len(c["instruction"]) for c in checks)
    print(f"generated {len(checks)} trip checks, {total} instruction chars")
    for c in checks:
        alias_n = len(c["aliases"])
        print(f"  {c['id']:<16} {len(c['instruction']):>5} chars  aliases={alias_n}")
    if any(not c["aliases"] for c in checks):
        print("\nNOTE: some checks carry no aliases — their production names "
              "would stop resolving. Verify against the round-1 registry.")
    pre = build_preamble(template)
    Path(a.preamble_out).write_text(pre, encoding="utf-8")
    Path(a.out).write_text(json.dumps({"checks": checks}, indent=1), encoding="utf-8")
    print(f"\nshared preamble : {len(pre):>7,} chars (rendered ONCE per family)")
    print(f"16 check lines  : {total:>7,} chars")
    print(f"combined        : {len(pre) + total:>7,} chars — against "
          f"{len(pre) * 16:,} if the procedure were repeated per check")
    print(f"wrote {a.out} and {a.preamble_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
