"""Reviewable requirements, deterministic language entry points and SI conversion."""
from __future__ import annotations

import copy
import math
import re
from typing import Protocol


DEFAULTS = {
    "schema_version": "0.1", "function": "metalens", "wavelength_nm": 532.0,
    "diameter_um": 30.0, "focal_length_um": 100.0, "seed": 7,
    "assume_k_zero": False,
    "beam": {"type": "plane", "normalization": "normalized", "value": 1.0,
             "waist_um": 12.0, "waist_z_um": 0.0, "center_x_um": 0.0,
             "center_y_um": 0.0, "theta_deg": 0.0, "phi_deg": 0.0,
             "polarization": {"type": "linear", "angle_deg": 0.0}},
}
LIMITS = {"wavelength_nm": [450.0, 700.0], "diameter_um": [2.0, 80.0],
          "na_max": 0.3, "theta_deg_max": 10.0, "max_grid_size": 2048}
UNIT_METERS = {"m": 1.0, "mm": 1e-3, "um": 1e-6, "μm": 1e-6,
               "µm": 1e-6, "nm": 1e-9, "毫米": 1e-3, "微米": 1e-6, "纳米": 1e-9}
QUANTITY = r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*(毫米|微米|纳米|mm|um|μm|µm|nm|m)(?![A-Za-z])"


class RequirementProvider(Protocol):
    """Replaceable language-to-configuration interface; no solver access implied."""
    def parse(self, text: str) -> dict: ...


class DeterministicRequirementProvider:
    name = "deterministic_rule_parser"

    def parse(self, text: str) -> dict:
        return parse_requirement(text)


def _merge(base: dict, updates: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in updates.items():
        result[key] = _merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else copy.deepcopy(value)
    return result


def _quantity(text: str, labels: str) -> float | None:
    match = re.search(r"(?:" + labels + r")\s*(?:为|是|=|:|：)?\s*" + QUANTITY, text, re.I)
    return None if match is None else float(match.group(1)) * UNIT_METERS[match.group(2)]


def parse_requirement(text: str) -> dict:
    """Parse metalens requests. Parameter limits are checked after user review.

    Unknown tasks, conflicting explicit quantities and unspecified units fail,
    rather than silently replacing the requested device by the demo.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Please provide a nonempty design requirement")
    if not re.search(r"聚焦|透镜|metalens|focusing|lens", text, re.I):
        raise ValueError("First release supports monochromatic focusing metalenses; request a focusing device")
    if re.search(r"宽带|消色差|achromatic|broadband|多波长", text, re.I):
        raise ValueError("Broadband or achromatic requirements are outside this first release")
    result = copy.deepcopy(DEFAULTS)
    explicit = {}
    labels = {"wavelength_nm": r"工作波长|波长|wavelength", "diameter_um": r"口径|直径|diameter|aperture",
              "focal_length_um": r"焦距|focal\s*length"}
    for key, alternatives in labels.items():
        match = _quantity(text, alternatives)
        quantities = list(re.finditer(r"(?:" + alternatives + r")\s*(?:为|是|=|:|：)?\s*" + QUANTITY, text, re.I))
        vals = [float(m.group(1)) * UNIT_METERS[m.group(2)] for m in quantities]
        if len(vals) > 1 and not all(math.isclose(v, vals[0], rel_tol=1e-10) for v in vals):
            raise ValueError(f"Conflicting explicit {key} values; select one design wavelength/geometry")
        if match is not None:
            explicit[key] = match / (1e-9 if key == "wavelength_nm" else 1e-6)
        elif re.search(r"(?:" + alternatives + r")\s*(?:为|是|=|:|：)?\s*\d", text, re.I):
            raise ValueError(f"Explicit {key} requires a unit such as nm, um or mm")
    if "wavelength_nm" not in explicit:
        # Also support “在 532 nm 工作” where the label follows the quantity.
        all_q = list(re.finditer(QUANTITY, text, re.I))
        wavelength_q = [m for m in all_q if m.group(2) in {"nm", "纳米"}]
        if len(wavelength_q) == 1:
            explicit["wavelength_nm"] = float(wavelength_q[0].group(1))
    if re.search(r"高斯|gaussian", text, re.I):
        result["beam"]["type"] = "gaussian"
    if re.search(r"涡旋|vortex|平顶|top.hat|LG\b|HG\b", text, re.I):
        raise ValueError("First release beam interface supports plane and Gaussian beams")
    result.update(explicit)
    result["input_text"] = text
    result["entry_mode"] = "traditional_optic" if "focal_length_um" in explicit or "diameter_um" in explicit else "function_wavelength"
    result["requirement_provider"] = "deterministic_rule_parser"
    result["defaults_applied"] = {key: copy.deepcopy(result[key]) for key in ("wavelength_nm", "diameter_um", "focal_length_um", "beam", "seed") if key not in explicit}
    return result


def normalize_spec(spec: dict) -> dict:
    if not isinstance(spec, dict):
        raise ValueError("Design configuration must be a mapping")
    result = _merge(DEFAULTS, spec)
    defaults = dict(result.get("defaults_applied", {}))
    for key in ("wavelength_nm", "diameter_um", "focal_length_um", "beam", "seed"):
        if key not in spec:
            defaults[key] = copy.deepcopy(result[key])
    result["defaults_applied"] = defaults
    for key in ("wavelength_nm", "diameter_um", "focal_length_um"):
        value = float(result[key])
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{key} must be finite and positive")
        result[key] = value
    result["wavelength_m"] = result["wavelength_nm"] * 1e-9
    result["diameter_m"] = result["diameter_um"] * 1e-6
    result["focal_length_m"] = result["focal_length_um"] * 1e-6
    for key, units in (("wavelength_m", "wavelength_nm"), ("diameter_m", "diameter_um"), ("focal_length_m", "focal_length_um")):
        if key in spec and not math.isclose(float(spec[key]), result[key], rel_tol=1e-9):
            raise ValueError(f"Inconsistent {key} and {units} units")
    result["numerical_aperture"] = math.sin(math.atan(result["diameter_m"] / (2 * result["focal_length_m"])))
    validate_spec(result)
    result["first_release_limits"] = copy.deepcopy(LIMITS)
    return result


def validate_spec(spec: dict, for_run: bool = True) -> None:
    if spec.get("function", "metalens") != "metalens":
        raise ValueError("Only monochromatic single-layer dielectric metalenses are implemented")
    for key in ("wavelength_nm", "diameter_um"):
        lo, hi = LIMITS[key]
        value = float(spec.get(key, DEFAULTS[key]))
        if not math.isfinite(value) or not lo <= value <= hi:
            raise ValueError(f"{key}={value:g} is outside first-release range [{lo:g}, {hi:g}]; requested scale is preserved, not resized")
    f = float(spec.get("focal_length_um", DEFAULTS["focal_length_um"]))
    if not math.isfinite(f) or f <= 0:
        raise ValueError("Focal length must be finite and positive")
    na = math.sin(math.atan(float(spec.get("diameter_um", DEFAULTS["diameter_um"])) / (2 * f)))
    if na > LIMITS["na_max"]:
        raise ValueError(f"NA={na:.3f} exceeds scalar first-release limit {LIMITS['na_max']}; vector propagation and coupling validation are required")
    beam = spec.get("beam", DEFAULTS["beam"])
    if beam.get("type", "plane") not in {"plane", "gaussian"}:
        raise ValueError("Supported beam types are plane and gaussian")
    theta = float(beam.get("theta_deg", 0))
    if not math.isfinite(theta) or abs(theta) > LIMITS["theta_deg_max"]:
        raise ValueError("First-release beam tilt must be within ±10 degrees")
    if for_run and theta != 0:
        raise ValueError("The first-release unit library is normal-incidence only; preview supports tilt, but a nonzero tilt requires an angle-resolved library before design")
    seed = float(spec.get("seed", 7))
    if not math.isfinite(seed) or seed < 0 or not seed.is_integer():
        raise ValueError("Random seed must be a nonnegative integer")
