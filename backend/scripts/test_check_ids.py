"""B1: the registry, not the model, must decide a finding's identity.

Measured over 26 production runs, the four self-naming vision families emitted
623 / 156 / 152 / 86 distinct check names (ai_sld / ai_consistency / ai_relay /
ai_gnd_deep) for 711 / 315 / 174 / 107 findings — roughly one name per finding
in the worst case. On triplicate runs of one unchanged document they scored 0%
key stability while every fixed-checklist family scored 100%, and on the
Nessler project five runs of the identical PDF produced problem lists whose
consecutive overlap fell to 14%.

The properties this suite pins down:
  * an enumerated family's item_key comes from the registry, not the model, and
    survives re-casing, re-wording, punctuation and ordinal prefixes;
  * a family with NO registry keeps the old free-form behaviour, so enumeration
    can roll out one family at a time without touching the rest;
  * a defect no id covers is NEVER dropped and never forced into an unrelated
    id — it goes to a content-keyed open bucket and is counted, because a
    registry that quietly narrows coverage would improve every stability metric
    while missing real defects.

Run: PYTHONPATH=backend python backend/scripts/test_check_ids.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import check_ids as ci  # noqa: E402

_FAILS: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _FAILS.append(name)


FIXTURE = """
families:
  grounding:
    - id: egc_size_250_122
      title: EGC sizing per NEC 250.122
      instruction: Compare the shown EGC against Table 250.122 for the OCPD rating.
      nec_ref: NEC 250.122
      aliases:
        - "Equipment grounding conductor size validation"
        - "EGC validation per NEC 250.122"
    - id: ground_rods
      title: Ground rods and spacing
      instruction: Confirm rods are shown with spacing at least twice their length.
      aliases:
        - "Ground rods shown and spacing"
        - "Ground rods presence and spacing"
"""


def load_fixture(text: str) -> None:
    """Point the module at a throwaway registry and clear its caches."""
    tmp = Path(tempfile.mkdtemp()) / "check_ids.yaml"
    tmp.write_text(text, encoding="utf-8")
    ci._REGISTRY_PATH = tmp
    ci._load.cache_clear()
    ci._lookup.cache_clear()


load_fixture(FIXTURE)

print("Registry loading:")
check("family resolves from prefix", ci.family_for_prefix("ai_gnd_deep") == "grounding")
check("both grounding dispatches share one registry",
      ci.family_for_prefix("ai_gnd") == ci.family_for_prefix("ai_gnd_deep"))
check("2 checks loaded", len(ci.checks_for("grounding")) == 2)
check("unregistered family -> None", ci.family_for_prefix("ai_sld") is None)

print("The key comes from the registry, whatever the model calls it:")
# NOTE the prefix: grounding normalizes to ai_gnd for BOTH dispatches. Only one
# of the single/deep passes runs on a given planset (the deep pass supersedes
# the single one), so keying off the dispatch would make a finding's identity
# depend on how many grounding sheets the document happened to have.
EXPECT = "ai_gnd_egc_size_250_122"
for name in ("egc_size_250_122", "EGC_SIZE_250_122", "  egc_size_250_122  ",
             "5. egc_size_250_122", "egc-size-250-122",
             "Equipment grounding conductor size validation",
             "EGC validation per NEC 250.122",
             "EGC sizing per NEC 250.122"):
    key, is_open = ci.canonical_key("ai_gnd_deep", name, {"evidence": "x"})
    check(f"{name!r} -> {EXPECT}", key == EXPECT and not is_open)

print("The two real prod wordings of one check now agree:")
a, _ = ci.canonical_key("ai_gnd_deep", "Ground rods shown and spacing", {"evidence": "8 rods"})
b, _ = ci.canonical_key("ai_gnd_deep", "Ground rods presence and spacing", {"evidence": "8 rods"})
check("v2 and v3 wordings collapse to one key", a == b == "ai_gnd_ground_rods")

print("Single-page and deep dispatches produce the SAME key:")
sp, _ = ci.canonical_key("ai_gnd", "ground_rods", {"evidence": "x"})
dp, _ = ci.canonical_key("ai_gnd_deep", "ground_rods", {"evidence": "x"})
check("dispatch route does not change identity", sp == dp == "ai_gnd_ground_rods")
check("key_prefix_for normalizes the deep prefix",
      ci.key_prefix_for("ai_gnd_deep") == "ai_gnd")
check("unregistered prefix is left alone",
      ci.key_prefix_for("ai_cover") == "ai_cover")

print("Unknown ids go to the open bucket — never dropped, never mis-filed:")
k1, o1 = ci.canonical_key("ai_gnd_deep", "some_check_nobody_enumerated",
                          {"evidence": "bus 3 is unlabelled"})
check("routed to the open bucket", k1.startswith("ai_gnd_open_") and o1)
check("not forced onto an unrelated id", "egc" not in k1 and "ground_rods" not in k1)
k2, _ = ci.canonical_key("ai_gnd_deep", "a completely different name",
                         {"evidence": "bus 3 is unlabelled"})
check("same evidence -> same key even when the NAME churns", k1 == k2)
k3, _ = ci.canonical_key("ai_gnd_deep", "some_check_nobody_enumerated",
                         {"evidence": "a different defect entirely"})
check("different evidence -> different key", k3 != k1)

print("Explicit open_findings entries bypass id matching:")
k4, o4 = ci.canonical_key("ai_gnd_deep", "ground_rods",
                          {"evidence": "z", "_exploratory": True})
check("an exploratory finding stays exploratory", o4 and k4.startswith("ai_gnd_open_"))
check("it does not squat on the enumerated id", k4 != "ai_gnd_ground_rods")

print("One check id can fire twice on a sheet without losing an instance:")
# Two different feeders can both be undersized. Without a discriminator the
# per-call dedup keeps the first and silently discards the rest, which would
# turn the enumeration itself into a finding-loss mechanism.
i1, _ = ci.canonical_key("ai_gnd_deep", "egc_size_250_122",
                         {"evidence": "a", "instance": "XFMR-1"})
i2, _ = ci.canonical_key("ai_gnd_deep", "egc_size_250_122",
                         {"evidence": "b", "instance": "XFMR-2"})
check("two instances of one id get distinct keys", i1 != i2)
check("both keep the canonical id", i1.startswith("ai_gnd_egc_size_250_122__")
      and i2.startswith("ai_gnd_egc_size_250_122__"))
check("instance slug is normalized", i1 == "ai_gnd_egc_size_250_122__xfmr_1")
check("no instance -> bare canonical key",
      ci.canonical_key("ai_gnd_deep", "egc_size_250_122", {"evidence": "a"})[0]
      == "ai_gnd_egc_size_250_122")

print("Families without a registry are untouched:")
k5, o5 = ci.canonical_key("ai_sld", "free_form_name_the_model_chose", {"evidence": "x"})
check("free-form key preserved", k5 == "ai_sld_free_form_name_the_model_chose" and not o5)
k6, _ = ci.canonical_key("ai_sld", "ai_sld_already_prefixed", {"evidence": "x"})
check("no double prefix", k6 == "ai_sld_already_prefixed")

print("Report titles come from the registry:")
check("enumerated check gets its human title",
      ci.title_for("ai_gnd_deep", "Ground rods shown and spacing") == "Ground rods and spacing")
check("unknown check has no registry title",
      ci.title_for("ai_gnd_deep", "nope") is None)
check("unregistered family has no registry title",
      ci.title_for("ai_sld", "anything") is None)

print("The prompt block is the v4-style contract:")
block = ci.checklist_block("grounding")
check("ids appear verbatim for the model to echo",
      "[check_id: egc_size_250_122]" in block)
check("titles are shown", "EGC sizing per NEC 250.122" in block)
check("instructions are carried", "Table 250.122" in block)
check("NEC reference is carried", "NEC 250.122" in block)
check("model is told not to invent ids",
      "Do NOT invent" in block and "verbatim" in block)
check("the escape hatch is advertised", "open_findings" in block)
check("escape hatch is framed as required, not optional",
      "required, not optional" in block.lower())
check("unregistered family yields no block", ci.checklist_block("sld") == "")

print("Registry fingerprint is stamped for run comparison:")
fp = ci.registry_fingerprint()
check("per-family counts recorded", fp["families"] == {"grounding": 2})
check("content hash present", isinstance(fp["sha256"], str) and len(fp["sha256"]) == 64)
load_fixture(FIXTURE + "    - id: gec_size_250_66\n      title: GEC sizing\n"
                       "      instruction: Check GEC against Table 250.66.\n")
check("hash changes when the registry changes",
      ci.registry_fingerprint()["sha256"] != fp["sha256"])
check("new check is loaded", len(ci.checks_for("grounding")) == 3)

print("A broken registry must not stop an analysis:")
load_fixture("families: [this is not valid: {{{")
check("malformed YAML falls back to free-form",
      ci.canonical_key("ai_gnd_deep", "x", {"evidence": "y"})
      == ("ai_gnd_deep_x", False))
check("and reports no families", ci.registry_fingerprint()["families"] == {})

print()
if _FAILS:
    print(f"FAILED ({len(_FAILS)}): {_FAILS}")
    sys.exit(1)
print("ALL CHECK-ID REGISTRY CHECKS PASSED")
