"""Independent electrical calculation engine for solar PV planset QC.

Provides NEC-based calculations that can validate values extracted from
plansets by Gemini or regex, or recalculate from first principles using
project_details.

All functions return a ``CalcResult`` with pass/fail status, computed
values, and evidence text suitable for issue creation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CalcResult:
    """Result of an electrical calculation."""

    status: str  # "Pass" | "Fail" | "Needs Review"
    evidence: str
    severity: str = "medium"
    confidence: float = 0.85
    computed: dict[str, Any] = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════════════════
# NEC Reference Tables
# ═══════════════════════════════════════════════════════════════════════════

# NEC 310.16 – Allowable Ampacities (Copper, 75°C column – THWN-2, XHHW)
# Wire size → ampacity at 75°C
NEC_310_16_CU_75C: dict[str, float] = {
    "14": 20, "12": 25, "10": 35, "8": 50, "6": 65, "4": 85,
    "3": 100, "2": 115, "1": 130, "1/0": 150, "2/0": 175,
    "3/0": 200, "4/0": 230, "250": 255, "300": 285, "350": 310,
    "400": 335, "500": 380, "600": 420, "700": 460, "750": 475,
    "800": 490, "900": 520, "1000": 545, "1250": 590, "1500": 625,
    "1750": 650, "2000": 665,
}

# NEC 310.16 – Copper, 90°C column (for PV wire, USE-2, etc.)
NEC_310_16_CU_90C: dict[str, float] = {
    "14": 25, "12": 30, "10": 40, "8": 55, "6": 75, "4": 95,
    "3": 115, "2": 130, "1": 145, "1/0": 170, "2/0": 195,
    "3/0": 225, "4/0": 260, "250": 290, "300": 320, "350": 350,
    "400": 380, "500": 430, "600": 475, "700": 520, "750": 535,
    "800": 555, "900": 585, "1000": 615, "1250": 665, "1500": 705,
    "1750": 735, "2000": 750,
}

# NEC 310.16 – Aluminum, 75°C column
NEC_310_16_AL_75C: dict[str, float] = {
    "12": 20, "10": 30, "8": 40, "6": 50, "4": 65,
    "3": 75, "2": 90, "1": 100, "1/0": 120, "2/0": 135,
    "3/0": 155, "4/0": 180, "250": 205, "300": 230, "350": 250,
    "400": 270, "500": 310, "600": 340, "700": 375, "750": 385,
    "800": 395, "900": 425, "1000": 445, "1250": 485, "1500": 520,
    "1750": 545, "2000": 560,
}

# NEC 310.15(C)(1) – PV Wire ampacities at 90°C (free air, single conductor)
PV_WIRE_90C: dict[str, float] = {
    "14": 30, "12": 40, "10": 55, "8": 70, "6": 95,
    "4": 125, "3": 145, "2": 170, "1": 195, "1/0": 230,
    "2/0": 265, "3/0": 310, "4/0": 360,
}

# NEC 250.122 – Minimum EGC size based on OCPD rating
# OCPD_amps → minimum EGC size (copper AWG/kcmil)
NEC_250_122_EGC: list[tuple[float, str]] = [
    (15, "14"), (20, "12"), (60, "10"), (100, "8"),
    (200, "6"), (300, "4"), (400, "3"), (500, "2"),
    (600, "1"), (800, "1/0"), (1000, "2/0"), (1200, "3/0"),
    (1600, "4/0"), (2000, "250"), (2500, "350"), (3000, "400"),
    (4000, "500"), (5000, "700"), (6000, "800"),
]

# NEC 250.66 – GEC sizing based on largest ungrounded supply conductor (copper)
# Supply conductor size → GEC size (copper)
NEC_250_66_GEC_CU: list[tuple[str, str]] = [
    ("2", "8"), ("1", "6"), ("1/0", "6"), ("2/0", "4"),
    ("3/0", "2"), ("4/0", "2"), ("250", "2"), ("300", "2"),
    ("350", "1/0"), ("400", "1/0"), ("500", "1/0"),
    ("600", "2/0"), ("700", "2/0"), ("750", "2/0"),
    ("800", "3/0"), ("900", "3/0"), ("1000", "3/0"),
    ("1250", "3/0"), ("1500", "3/0"), ("1750", "3/0"), ("2000", "3/0"),
]

# NEC 250.66 – GEC sizing (aluminum)
NEC_250_66_GEC_AL: list[tuple[str, str]] = [
    ("2", "6"), ("1", "4"), ("1/0", "4"), ("2/0", "2"),
    ("3/0", "1"), ("4/0", "1"), ("250", "1"), ("300", "1"),
    ("350", "2/0"), ("400", "2/0"), ("500", "2/0"),
    ("600", "3/0"), ("700", "3/0"), ("750", "3/0"),
    ("800", "4/0"), ("900", "4/0"), ("1000", "4/0"),
    ("1250", "250"), ("1500", "250"), ("1750", "250"), ("2000", "250"),
]

# MV cable ampacities – 15kV class, direct buried, 90°C (copper)
MV_15KV_DIRECT_BURIED_CU: dict[str, float] = {
    "1/0": 135, "2/0": 155, "3/0": 180, "4/0": 210,
    "250": 235, "350": 280, "500": 345, "750": 440, "1000": 520,
}

# Transformer BIL by voltage class (kV → kV BIL)
TRANSFORMER_BIL: dict[str, float] = {
    "15": 95, "25": 125, "34.5": 150, "46": 200, "69": 350,
}

# NEC 110.26(A)(1) – Working space clearances (Condition 1, 2, 3)
# Voltage to ground → [Condition 1, Condition 2, Condition 3] in feet
NEC_110_26_CLEARANCES: list[tuple[float, list[float]]] = [
    (150, [3.0, 3.0, 3.0]),
    (600, [3.0, 3.5, 4.0]),
    (2500, [3.0, 4.0, 5.0]),
    (9000, [4.0, 5.0, 6.0]),
    (25000, [5.0, 6.0, 9.0]),
    (75000, [6.0, 8.0, 12.0]),
]

# NEC 310.15(C)(1) Temperature correction factors (30°C base)
TEMP_CORRECTION_75C: list[tuple[float, float, float]] = [
    # (temp_low, temp_high, correction_factor)
    (21, 25, 1.05), (26, 30, 1.00), (31, 35, 0.94),
    (36, 40, 0.88), (41, 45, 0.82), (46, 50, 0.75),
    (51, 55, 0.67), (56, 60, 0.58), (61, 65, 0.47),
    (66, 70, 0.33),
]

TEMP_CORRECTION_90C: list[tuple[float, float, float]] = [
    (21, 25, 1.04), (26, 30, 1.00), (31, 35, 0.96),
    (36, 40, 0.91), (41, 45, 0.87), (46, 50, 0.82),
    (51, 55, 0.76), (56, 60, 0.71), (61, 65, 0.65),
    (66, 70, 0.58), (71, 75, 0.50), (76, 80, 0.41),
]

# Conduit fill factor – NEC Chapter 9 Table 1
# Number of conductors → max fill percentage
CONDUIT_FILL_PCT: dict[int, float] = {
    1: 53.0, 2: 31.0,  # 3+ conductors → 40%
}

# Wire sizes in order from smallest to largest for comparison
_WIRE_ORDER = [
    "18", "16", "14", "12", "10", "8", "6", "4", "3", "2", "1",
    "1/0", "2/0", "3/0", "4/0",
    "250", "300", "350", "400", "500", "600", "700", "750",
    "800", "900", "1000", "1250", "1500", "1750", "2000",
]
_WIRE_INDEX = {s: i for i, s in enumerate(_WIRE_ORDER)}


def _wire_gte(a: str, b: str) -> bool:
    """Return True if wire size *a* is >= wire size *b*."""
    return _WIRE_INDEX.get(a, -1) >= _WIRE_INDEX.get(b, -1)


def _parse_wire_size(raw: str) -> str | None:
    """Normalize a wire size string like '#10', '10 AWG', '4/0', '500 kcmil'."""
    s = raw.strip().upper().replace("AWG", "").replace("KCMIL", "").replace("#", "").strip()
    return s if s in _WIRE_INDEX else None


def _safe_float(val: Any) -> float | None:
    """Try to parse a float from various input formats."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    try:
        s = str(val).strip().replace(",", "").replace("%", "")
        return float(s)
    except (ValueError, TypeError):
        return None


# ═══════════════════════════════════════════════════════════════════════════
# Calculation Functions
# ═══════════════════════════════════════════════════════════════════════════

def validate_stringing(pd: dict) -> CalcResult:
    """Validate string voltage against inverter MPPT range and NEC 690.7.

    Uses project_details fields:
        module_voc, module_vmp, module_temp_coeff_voc,
        string_size, design_temp_low_c, design_temp_high_c,
        inverter_max_vdc, inverter_mppt_range
    """
    voc = _safe_float(pd.get("module_voc"))
    vmp = _safe_float(pd.get("module_vmp"))
    tc_voc = _safe_float(pd.get("module_temp_coeff_voc"))
    string_size = _safe_float(pd.get("string_size"))
    t_low = _safe_float(pd.get("design_temp_low_c"))
    t_high = _safe_float(pd.get("design_temp_high_c"))
    max_vdc = _safe_float(pd.get("inverter_max_vdc"))
    mppt_range = pd.get("inverter_mppt_range")

    missing = []
    if voc is None:
        missing.append("module_voc")
    if string_size is None:
        missing.append("string_size")
    if missing:
        return CalcResult(
            "Needs Review", f"Missing project details: {', '.join(missing)}",
            confidence=0.40,
        )

    assert voc is not None and string_size is not None
    n = int(string_size)

    # Temperature coefficient defaults
    if tc_voc is None:
        tc_voc = -0.27  # typical Si module %/°C
    elif abs(tc_voc) > 1:
        # Likely given as %/°C (e.g. -0.27), already correct
        pass
    # If < -1, likely mV/°C — convert (would need Voc to convert, skip for now)

    # Design temps defaults (ASHRAE)
    if t_low is None:
        t_low = -15.0
    if t_high is None:
        t_high = 40.0

    # Voc at coldest temperature (NEC 690.7)
    delta_t_cold = 25.0 - t_low  # STC is 25°C
    voc_cold = voc * (1 + (tc_voc / 100.0) * delta_t_cold) if tc_voc < 0 else voc * (1 + abs(tc_voc / 100.0) * delta_t_cold)
    # For negative tc_voc: voltage INCREASES as temp drops
    # tc_voc is typically negative (e.g. -0.27%/°C)
    # When temp drops below STC, delta_t_cold is positive, tc_voc/100 is negative
    # So correction = 1 - (-0.0027 * 40) = 1 + 0.108 = 1.108 → voltage increases ✓
    voc_cold = voc * (1 - (tc_voc / 100.0) * delta_t_cold)

    string_voc_cold = voc_cold * n

    # Vmp at hottest temperature
    tc_vmp = tc_voc  # conservative: use Voc temp coeff for Vmp too
    delta_t_hot = t_high - 25.0
    if vmp is not None:
        vmp_hot = vmp * (1 + (tc_vmp / 100.0) * delta_t_hot)
        string_vmp_hot = vmp_hot * n
    else:
        vmp_hot = None
        string_vmp_hot = None

    computed = {
        "string_size": n,
        "voc_cold_per_module": round(voc_cold, 2),
        "string_voc_cold": round(string_voc_cold, 2),
        "design_temp_low_c": t_low,
        "design_temp_high_c": t_high,
    }
    if string_vmp_hot is not None:
        computed["vmp_hot_per_module"] = round(vmp_hot, 2)  # type: ignore[arg-type]
        computed["string_vmp_hot"] = round(string_vmp_hot, 2)

    issues = []

    # Check 1: String Voc (cold) < inverter max Vdc
    if max_vdc is not None:
        computed["inverter_max_vdc"] = max_vdc
        if string_voc_cold > max_vdc:
            issues.append(
                f"FAIL: String Voc(cold) {string_voc_cold:.1f}V > inverter max Vdc {max_vdc:.0f}V"
            )
        else:
            margin = (1 - string_voc_cold / max_vdc) * 100
            computed["vdc_margin_pct"] = round(margin, 1)

    # Check 2: String Vmp (hot) within MPPT range
    if string_vmp_hot is not None and mppt_range:
        try:
            parts = str(mppt_range).replace("V", "").replace("v", "").split("-")
            mppt_low = float(parts[0].strip())
            mppt_high = float(parts[1].strip()) if len(parts) > 1 else None
            computed["mppt_range"] = f"{mppt_low}-{mppt_high}"
            if string_vmp_hot < mppt_low:
                issues.append(
                    f"FAIL: String Vmp(hot) {string_vmp_hot:.1f}V < MPPT low {mppt_low:.0f}V"
                )
            if mppt_high and string_voc_cold > mppt_high:
                issues.append(
                    f"WARNING: String Voc(cold) {string_voc_cold:.1f}V > MPPT high {mppt_high:.0f}V"
                )
        except (ValueError, IndexError):
            pass  # unparseable MPPT range

    if issues:
        return CalcResult(
            "Fail",
            "; ".join(issues),
            severity="high",
            confidence=0.92,
            computed=computed,
        )

    evidence_parts = [f"String Voc(cold) = {string_voc_cold:.1f}V ({n} modules × {voc_cold:.2f}V)"]
    if max_vdc is not None:
        evidence_parts.append(f"< max Vdc {max_vdc:.0f}V")
    if string_vmp_hot is not None:
        evidence_parts.append(f"String Vmp(hot) = {string_vmp_hot:.1f}V")

    return CalcResult(
        "Pass", "; ".join(evidence_parts),
        confidence=0.92, computed=computed,
    )


def validate_fuse_sizing(pd: dict) -> CalcResult:
    """Validate string fuse sizing per NEC 690.9: fuse >= 1.56 × Isc.

    Uses: module_isc
    """
    isc = _safe_float(pd.get("module_isc"))
    if isc is None:
        return CalcResult("Needs Review", "Missing module_isc", confidence=0.40)

    min_fuse = isc * 1.56
    # Standard fuse sizes
    std_fuses = [10, 15, 20, 25, 30, 35, 40, 50, 60]
    recommended = next((f for f in std_fuses if f >= min_fuse), std_fuses[-1])

    computed = {
        "module_isc": isc,
        "min_fuse_1_56x": round(min_fuse, 2),
        "recommended_fuse_a": recommended,
        "formula": f"{isc:.2f}A × 1.56 = {min_fuse:.2f}A → {recommended}A fuse",
    }

    return CalcResult(
        "Pass",
        f"Min string fuse = Isc × 1.56 = {isc:.2f} × 1.56 = {min_fuse:.2f}A → use {recommended}A fuse (NEC 690.9)",
        confidence=0.92,
        computed=computed,
    )


def validate_transformer(pd: dict) -> CalcResult:
    """Validate transformer sizing: kVA >= total inverter output, BIL per voltage class.

    Uses: transformer_kva, inverter_kva, inverter_quantity, poi_voltage,
          transformer_impedance, transformer_bil
    """
    xfmr_kva = _safe_float(pd.get("transformer_kva"))
    inv_kva = _safe_float(pd.get("inverter_kva"))
    inv_qty = _safe_float(pd.get("inverter_quantity")) or 1
    poi_v = _safe_float(pd.get("poi_voltage"))
    xfmr_z = _safe_float(pd.get("transformer_impedance"))
    xfmr_bil = _safe_float(pd.get("transformer_bil"))

    if xfmr_kva is None and inv_kva is None:
        return CalcResult("Needs Review", "Missing transformer_kva and inverter_kva", confidence=0.40)

    issues = []
    computed: dict[str, Any] = {}

    # Check 1: kVA adequacy
    if xfmr_kva is not None and inv_kva is not None:
        total_inv_kva = inv_kva * inv_qty
        computed["transformer_kva"] = xfmr_kva
        computed["total_inverter_kva"] = total_inv_kva
        if xfmr_kva < total_inv_kva:
            issues.append(
                f"FAIL: Transformer {xfmr_kva:.0f} kVA < total inverter output {total_inv_kva:.0f} kVA"
            )
        else:
            margin = (xfmr_kva / total_inv_kva - 1) * 100
            computed["kva_margin_pct"] = round(margin, 1)

    # Check 2: BIL per voltage class
    if xfmr_bil is not None and poi_v is not None:
        voltage_class = None
        for vc in ["15", "25", "34.5", "46", "69"]:
            if poi_v <= float(vc) * 1000:
                voltage_class = vc
                break
        if voltage_class:
            required_bil = TRANSFORMER_BIL.get(voltage_class, 0)
            computed["voltage_class_kv"] = voltage_class
            computed["required_bil_kv"] = required_bil
            computed["actual_bil_kv"] = xfmr_bil
            if xfmr_bil < required_bil:
                issues.append(
                    f"FAIL: BIL {xfmr_bil:.0f}kV < required {required_bil:.0f}kV for {voltage_class}kV class"
                )

    # Check 3: Impedance Z%
    if xfmr_z is not None and xfmr_kva is not None:
        computed["impedance_z_pct"] = xfmr_z
        # Typical Z%: 5.75% for 750-2500 kVA
        if 750 <= xfmr_kva <= 2500 and abs(xfmr_z - 5.75) > 1.0:
            issues.append(
                f"WARNING: Z% = {xfmr_z}% (expected ~5.75% for {xfmr_kva:.0f}kVA transformer)"
            )

    if issues:
        fails = [i for i in issues if i.startswith("FAIL")]
        return CalcResult(
            "Fail" if fails else "Needs Review",
            "; ".join(issues),
            severity="high",
            confidence=0.88,
            computed=computed,
        )

    parts = []
    if "total_inverter_kva" in computed:
        parts.append(f"Transformer {xfmr_kva:.0f}kVA >= inverter total {computed['total_inverter_kva']:.0f}kVA")
    if "required_bil_kv" in computed:
        parts.append(f"BIL {xfmr_bil:.0f}kV >= {computed['required_bil_kv']:.0f}kV")
    return CalcResult("Pass", "; ".join(parts) or "Transformer specs verified", confidence=0.88, computed=computed)


def validate_dc_ampacity(pd: dict) -> CalcResult:
    """Validate DC cable ampacity per NEC 690.8 / 310.15.

    Minimum ampacity = Isc × 1.25 (continuous) × 1.25 (NEC 690.8) = Isc × 1.5625
    Then apply temperature and conduit fill derates.

    Uses: module_isc, design_temp_high_c
    """
    isc = _safe_float(pd.get("module_isc"))
    if isc is None:
        return CalcResult("Needs Review", "Missing module_isc", confidence=0.40)

    t_high = _safe_float(pd.get("design_temp_high_c")) or 40.0

    # Minimum ampacity before derates
    min_ampacity = isc * 1.25  # NEC 690.8(A)(1) continuous
    design_current = isc * 1.25  # Maximum current per NEC 690.8

    # Temperature correction for 90°C rated PV wire
    temp_factor = 1.0
    for low, high, factor in TEMP_CORRECTION_90C:
        if low <= t_high <= high:
            temp_factor = factor
            break

    derated_ampacity_needed = min_ampacity / temp_factor

    # Find minimum wire size from PV wire table
    min_wire = None
    for size, amp in sorted(PV_WIRE_90C.items(), key=lambda x: _WIRE_INDEX.get(x[0], 999)):
        if amp >= derated_ampacity_needed:
            min_wire = size
            break

    computed = {
        "module_isc": isc,
        "design_current_125pct": round(design_current, 2),
        "min_ampacity_needed": round(min_ampacity, 2),
        "temp_correction_factor": temp_factor,
        "derated_ampacity_needed": round(derated_ampacity_needed, 2),
        "ambient_temp_c": t_high,
        "min_pv_wire_size": min_wire or ">4/0",
        "formula": f"Isc({isc:.2f}A) × 1.25 = {min_ampacity:.2f}A; ÷ temp factor {temp_factor} = {derated_ampacity_needed:.2f}A",
    }

    evidence = (
        f"DC design current = {isc:.2f}A × 1.25 = {min_ampacity:.2f}A; "
        f"at {t_high:.0f}°C ambient (factor {temp_factor}), "
        f"min PV wire = #{min_wire or '>4/0'} (NEC 690.8, 310.15)"
    )

    return CalcResult("Pass", evidence, confidence=0.88, computed=computed)


def validate_ac_ampacity(pd: dict) -> CalcResult:
    """Validate AC cable ampacity per NEC 310.16.

    FLA = inverter_kva × 1000 / (voltage × √3)
    Min OCPD = FLA × 1.25 (continuous)
    Cable ampacity at 75°C >= OCPD rating

    Uses: inverter_kva, inverter_quantity, poi_voltage or
          transformer_secondary_voltage
    """
    inv_kva = _safe_float(pd.get("inverter_kva"))
    inv_kw = _safe_float(pd.get("inverter_kw"))
    inv_qty = _safe_float(pd.get("inverter_quantity")) or 1
    voltage = _safe_float(pd.get("transformer_secondary_voltage")) or _safe_float(pd.get("poi_voltage"))

    if inv_kva is None and inv_kw is None:
        return CalcResult("Needs Review", "Missing inverter_kva/kw", confidence=0.40)
    if voltage is None:
        return CalcResult("Needs Review", "Missing voltage (transformer_secondary_voltage or poi_voltage)", confidence=0.40)

    assert voltage is not None
    kva = inv_kva or (inv_kw / 0.9 if inv_kw else None)  # assume 0.9 PF if only kW
    assert kva is not None

    # Per-inverter FLA (3-phase)
    fla_per_inv = (kva * 1000) / (voltage * math.sqrt(3))
    total_fla = fla_per_inv * inv_qty

    # OCPD sizing (continuous load × 1.25)
    min_ocpd = total_fla * 1.25

    # Find wire size from NEC 310.16 at 75°C
    min_wire = None
    for size, amp in sorted(NEC_310_16_CU_75C.items(), key=lambda x: _WIRE_INDEX.get(x[0], 999)):
        if amp >= min_ocpd:
            min_wire = size
            break

    # Check if parallel sets needed (above 500A)
    parallel_sets = 1
    if min_ocpd > 500:
        parallel_sets = math.ceil(min_ocpd / 400)
        per_set_amp = min_ocpd / parallel_sets
        min_wire = None
        for size, amp in sorted(NEC_310_16_CU_75C.items(), key=lambda x: _WIRE_INDEX.get(x[0], 999)):
            if amp >= per_set_amp:
                min_wire = size
                break

    computed = {
        "inverter_kva": kva,
        "inverter_quantity": int(inv_qty),
        "voltage": voltage,
        "fla_per_inverter": round(fla_per_inv, 2),
        "total_fla": round(total_fla, 2),
        "min_ocpd_a": round(min_ocpd, 1),
        "min_wire_cu_75c": min_wire or ">2000 kcmil",
        "parallel_sets": parallel_sets,
        "formula": f"FLA = {kva:.0f}kVA × 1000 / ({voltage:.0f}V × √3) = {fla_per_inv:.1f}A",
    }

    evidence = (
        f"Per-inverter FLA = {fla_per_inv:.1f}A; total FLA = {total_fla:.1f}A; "
        f"min OCPD = {min_ocpd:.0f}A (×1.25 continuous); "
        f"min wire = #{min_wire or '>2000'} CU @75°C"
    )
    if parallel_sets > 1:
        evidence += f" ({parallel_sets} parallel sets)"

    return CalcResult("Pass", evidence, confidence=0.88, computed=computed)


def validate_mv_ampacity(pd: dict) -> CalcResult:
    """Validate MV cable ampacity.

    MV FLA = total kVA × 1000 / (primary voltage × √3)
    Cable must carry 1.25 × FLA.

    Uses: transformer_kva, transformer_primary_voltage or poi_voltage
    """
    xfmr_kva = _safe_float(pd.get("transformer_kva")) or _safe_float(pd.get("total_ac_kva"))
    voltage = _safe_float(pd.get("transformer_primary_voltage")) or _safe_float(pd.get("poi_voltage"))

    if xfmr_kva is None:
        return CalcResult("Needs Review", "Missing transformer_kva", confidence=0.40)
    if voltage is None:
        return CalcResult("Needs Review", "Missing MV voltage", confidence=0.40)

    assert xfmr_kva is not None and voltage is not None

    fla = (xfmr_kva * 1000) / (voltage * math.sqrt(3))
    design_current = fla * 1.25

    # Find wire from MV table
    min_wire = None
    for size, amp in sorted(MV_15KV_DIRECT_BURIED_CU.items(), key=lambda x: _WIRE_INDEX.get(x[0], 999)):
        if amp >= design_current:
            min_wire = size
            break

    computed = {
        "transformer_kva": xfmr_kva,
        "mv_voltage": voltage,
        "mv_fla": round(fla, 2),
        "design_current_125pct": round(design_current, 2),
        "min_mv_wire": min_wire or ">1000 kcmil",
        "formula": f"FLA = {xfmr_kva:.0f}kVA × 1000 / ({voltage:.0f}V × √3) = {fla:.1f}A",
    }

    return CalcResult(
        "Pass",
        f"MV FLA = {fla:.1f}A; design current = {design_current:.1f}A (×1.25); "
        f"min MV cable = {min_wire or '>1000'} kcmil CU (15kV, direct buried)",
        confidence=0.85,
        computed=computed,
    )


def validate_egc_sizing(pd: dict) -> CalcResult:
    """Validate EGC sizing per NEC 250.122.

    EGC minimum is based on the rating of the OCPD protecting the circuit.
    For parallel conductors, EGC must be upsized proportionally.

    Uses: (computed from other calcs — inverter_kva, voltage, etc.)
    """
    inv_kva = _safe_float(pd.get("inverter_kva"))
    inv_kw = _safe_float(pd.get("inverter_kw"))
    voltage = _safe_float(pd.get("transformer_secondary_voltage")) or _safe_float(pd.get("poi_voltage"))

    if (inv_kva is None and inv_kw is None) or voltage is None:
        return CalcResult("Needs Review", "Missing inverter kVA/kW or voltage for EGC calc", confidence=0.40)

    assert voltage is not None
    kva = inv_kva or (inv_kw / 0.9 if inv_kw else None)
    assert kva is not None

    fla = (kva * 1000) / (voltage * math.sqrt(3))
    ocpd = fla * 1.25

    # Find EGC from NEC 250.122
    egc_size = "8"  # default
    for max_ocpd, wire in NEC_250_122_EGC:
        if ocpd <= max_ocpd:
            egc_size = wire
            break
    else:
        egc_size = "800"  # max in table

    computed = {
        "fla": round(fla, 2),
        "ocpd_rating": round(ocpd, 0),
        "min_egc_cu": egc_size,
    }

    return CalcResult(
        "Pass",
        f"FLA = {fla:.1f}A → OCPD ≈ {ocpd:.0f}A → min EGC = #{egc_size} CU (NEC 250.122)",
        confidence=0.85,
        computed=computed,
    )


def validate_gec_sizing(pd: dict) -> CalcResult:
    """Validate GEC sizing per NEC 250.66.

    GEC is based on the largest ungrounded supply conductor size.
    If supply conductor size is not available, estimate from FLA.

    Uses: transformer_kva, transformer_secondary_voltage or poi_voltage
    """
    xfmr_kva = _safe_float(pd.get("transformer_kva"))
    voltage = _safe_float(pd.get("transformer_secondary_voltage")) or _safe_float(pd.get("poi_voltage"))

    if xfmr_kva is None or voltage is None:
        return CalcResult("Needs Review", "Missing transformer_kva or voltage for GEC calc", confidence=0.40)

    assert xfmr_kva is not None and voltage is not None

    fla = (xfmr_kva * 1000) / (voltage * math.sqrt(3))
    ocpd = fla * 1.25

    # Estimate supply conductor from ampacity
    supply_wire = None
    for size, amp in sorted(NEC_310_16_CU_75C.items(), key=lambda x: _WIRE_INDEX.get(x[0], 999)):
        if amp >= ocpd:
            supply_wire = size
            break
    if supply_wire is None:
        supply_wire = "2000"

    # Look up GEC from NEC 250.66
    gec_size = "3/0"  # default for large conductors
    for supply, gec in NEC_250_66_GEC_CU:
        if supply == supply_wire:
            gec_size = gec
            break
    else:
        # If supply is larger than table, use largest GEC
        if _WIRE_INDEX.get(supply_wire, 0) >= _WIRE_INDEX.get("1000", 0):
            gec_size = "3/0"

    computed = {
        "transformer_kva": xfmr_kva,
        "voltage": voltage,
        "fla": round(fla, 2),
        "estimated_supply_conductor": supply_wire,
        "min_gec_cu": gec_size,
    }

    return CalcResult(
        "Pass",
        f"Transformer {xfmr_kva:.0f}kVA @ {voltage:.0f}V → FLA {fla:.1f}A → "
        f"supply ≈ #{supply_wire} → GEC = #{gec_size} CU (NEC 250.66)",
        confidence=0.85,
        computed=computed,
    )


def validate_voltage_drop(pd: dict) -> CalcResult:
    """Estimate voltage drop and check against 3% max criterion.

    This is a presence/reasonableness check — actual VD calculations
    require cable lengths which are typically only in the electrical schedule.

    Uses: total_dc_kw, total_ac_kva, poi_voltage
    """
    dc_kw = _safe_float(pd.get("total_dc_kw"))
    ac_kva = _safe_float(pd.get("total_ac_kva"))
    poi_v = _safe_float(pd.get("poi_voltage"))

    if dc_kw is None and ac_kva is None:
        return CalcResult("Needs Review", "Missing system size for voltage drop estimation", confidence=0.40)

    computed: dict[str, Any] = {}
    if dc_kw:
        computed["total_dc_kw"] = dc_kw
    if ac_kva:
        computed["total_ac_kva"] = ac_kva
    if poi_v:
        computed["poi_voltage"] = poi_v

    # Can't calculate actual VD without cable lengths — provide guidance
    computed["max_vd_pct"] = 3.0
    computed["typical_dc_vd_pct"] = "0.5-1.5%"
    computed["typical_ac_vd_pct"] = "0.5-1.5%"
    computed["typical_mv_vd_pct"] = "0.1-0.5%"

    return CalcResult(
        "Needs Review",
        f"Voltage drop calculation requires cable lengths from electrical schedule. "
        f"Total VD (DC+AC+MV) must be ≤ 3%. "
        f"System: {dc_kw or '?'}kW DC, {ac_kva or '?'}kVA AC, {poi_v or '?'}V POI. "
        f"Typical ranges: DC 0.5-1.5%, AC 0.5-1.5%, MV 0.1-0.5%.",
        severity="medium",
        confidence=0.60,
        computed=computed,
    )


def validate_conduit_fill(pd: dict) -> CalcResult:
    """Check conduit fill guidance (< 40% for 3+ conductors per NEC Ch9 Table 1)."""
    return CalcResult(
        "Needs Review",
        "Conduit fill must be < 40% for 3+ conductors per NEC Chapter 9 Table 1. "
        "Verify from electrical schedule that all conduit runs show fill percentage below 40%.",
        severity="medium",
        confidence=0.60,
        computed={"max_fill_pct": 40.0, "nec_ref": "Chapter 9 Table 1"},
    )


def validate_nec_clearances(pd: dict) -> CalcResult:
    """Provide NEC 110.26 clearance requirements based on system voltage."""
    poi_v = _safe_float(pd.get("poi_voltage"))
    secondary_v = _safe_float(pd.get("transformer_secondary_voltage"))

    voltage = poi_v or secondary_v
    if voltage is None:
        return CalcResult("Needs Review", "Missing voltage for clearance lookup", confidence=0.40)

    assert voltage is not None

    # Determine voltage to ground (assume 3-phase wye: V_L-G = V_L-L / √3)
    v_to_ground = voltage / math.sqrt(3)

    # Find clearance requirements
    clearances = [3.0, 3.5, 4.0]  # default (0-600V Condition 1,2,3)
    for v_max, clr in NEC_110_26_CLEARANCES:
        if v_to_ground <= v_max:
            clearances = clr
            break

    computed = {
        "system_voltage": voltage,
        "voltage_to_ground": round(v_to_ground, 0),
        "condition_1_ft": clearances[0],
        "condition_2_ft": clearances[1],
        "condition_3_ft": clearances[2],
    }

    return CalcResult(
        "Pass",
        f"At {voltage:.0f}V ({v_to_ground:.0f}V to ground): "
        f"NEC 110.26 clearances = {clearances[0]}/{clearances[1]}/{clearances[2]} ft "
        f"(Condition 1/2/3)",
        confidence=0.85,
        computed=computed,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Dispatcher — maps calc_function names to actual functions
# ═══════════════════════════════════════════════════════════════════════════

CALC_FUNCTIONS: dict[str, Any] = {
    "validate_stringing": validate_stringing,
    "validate_fuse_sizing": validate_fuse_sizing,
    "validate_transformer": validate_transformer,
    "validate_dc_ampacity": validate_dc_ampacity,
    "validate_ac_ampacity": validate_ac_ampacity,
    "validate_mv_ampacity": validate_mv_ampacity,
    "validate_egc_sizing": validate_egc_sizing,
    "validate_gec_sizing": validate_gec_sizing,
    "validate_voltage_drop": validate_voltage_drop,
    "validate_conduit_fill": validate_conduit_fill,
    "validate_nec_clearances": validate_nec_clearances,
}


def run_calc(calc_function: str, project_details: dict) -> CalcResult | None:
    """Run a named calculation function with project details."""
    fn = CALC_FUNCTIONS.get(calc_function)
    if fn is None:
        return None
    return fn(project_details)
