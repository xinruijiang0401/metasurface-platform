"""Tolerance under explicit assumed errors; this is not fabrication yield.

Radius responses are interpolated only inside a measured/simulated library.
Height dependence comes from piecewise interpolation between actual RCWA
libraries, including the nominal height rather than extrapolated derivatives.
No extrapolation or clipping is used to manufacture plausible performance.
"""
from __future__ import annotations

import math
import numpy as np
from scipy.ndimage import gaussian_filter

from .beams import intensity, _grid_spacing
from .optics import angular_spectrum


DEFAULT_ERRORS = {
    "samples": 12, "common_diameter_sigma_nm": 4.0,
    "correlated_diameter_sigma_nm": 2.0, "local_diameter_sigma_nm": 2.0,
    "correlation_length_um": 3.0, "height_sigma_nm": 8.0,
    "threshold_fraction": 0.9,
}


def _response_table(candidate: dict) -> tuple[np.ndarray, np.ndarray]:
    radii = np.asarray(candidate["radii_nm"], dtype=float)
    jones = np.asarray(candidate["jones_t"], dtype=complex)
    if radii.ndim != 1 or len(radii) < 2 or not np.all(np.isfinite(radii)) or np.any(np.diff(radii) <= 0):
        raise ValueError("Tolerance library needs a strictly increasing radius axis with at least two values")
    if jones.shape != (len(radii), 2, 2) or not np.all(np.isfinite(jones)):
        raise ValueError("Tolerance requires finite physical Jones matrices with shape [radius,2,2]")
    return radii, jones


def _interpolate_jones(radii: np.ndarray, jones: np.ndarray, values: np.ndarray) -> np.ndarray:
    if np.any(values < radii[0]) or np.any(values > radii[-1]) or not np.all(np.isfinite(values)):
        raise ValueError("radius_outside_response_library")
    result = np.empty((2, 2) + values.shape, dtype=complex)
    for i in range(2):
        for j in range(2):
            result[i, j] = (np.interp(values, radii, jones[:, i, j].real)
                            + 1j * np.interp(values, radii, jones[:, i, j].imag))
    return result


def _unit_std_pattern(rng: np.random.Generator, shape: tuple[int, int], mask: np.ndarray,
                      sigma_yx: tuple[float, float] | None = None) -> np.ndarray:
    pattern = rng.normal(size=shape)
    if sigma_yx is not None:
        pattern = gaussian_filter(pattern, sigma=sigma_yx, mode="reflect")
    values = pattern[mask]
    std = float(np.std(values))
    if std <= 1e-12:
        raise ValueError("Aperture/grid too small to estimate the requested spatial-error distribution")
    return (pattern - float(np.mean(values))) / std


def evaluate_tolerance(config: dict, candidate: dict, x: np.ndarray, y: np.ndarray,
                       aperture: np.ndarray, radius_map_nm: np.ndarray,
                       incident_field: np.ndarray, *,
                       height_minus: dict | None = None, height_plus: dict | None = None) -> dict:
    """Sensitivity and seeded MC of whole-device focusing efficiency in air.

    Errors are diameters (thus radius perturbations are divided by two).
    Correlation length is the 1/e radius of the covariance after Gaussian
    filtering: filter sigma = correlation length/2. Spatial realizations are
    renormalized to the specified finite-aperture standard deviation.
    """
    settings = dict(DEFAULT_ERRORS)
    settings.update(config.get("tolerance", {}))
    n_samples = int(settings["samples"])
    if n_samples < 1 or n_samples > 500 or float(settings["samples"]) != n_samples:
        raise ValueError("Tolerance sample count must be an integer between 1 and 500")
    sigma_keys = ("common_diameter_sigma_nm", "correlated_diameter_sigma_nm", "local_diameter_sigma_nm", "height_sigma_nm")
    for key in sigma_keys:
        settings[key] = float(settings[key])
        if not math.isfinite(settings[key]) or settings[key] < 0:
            raise ValueError(f"{key} must be finite and nonnegative")
    corr_length = float(settings["correlation_length_um"]) * 1e-6
    fraction = float(settings["threshold_fraction"])
    if not math.isfinite(corr_length) or corr_length <= 0 or not 0 < fraction <= 1:
        raise ValueError("Correlation length must be positive and threshold_fraction in (0,1]")
    dx, dy = _grid_spacing(x, y)
    mask = np.asarray(aperture, dtype=bool)
    radius_map = np.asarray(radius_map_nm, dtype=float)
    field_in = np.asarray(incident_field, dtype=complex)
    shape = (len(y), len(x))
    if mask.shape != shape or radius_map.shape != shape or field_in.shape != (2,) + shape or np.count_nonzero(mask) < 4:
        raise ValueError("Tolerance requires an aperture and radius map on the same Jones-field grid")
    radii, jones = _response_table(candidate)
    if not np.all(np.isfinite(radius_map[mask])) or np.any(radius_map[mask] < radii[0]) or np.any(radius_map[mask] > radii[-1]):
        raise ValueError("Nominal design already lies outside the response library")
    nin, nout = float(candidate["input_index"]), float(candidate["output_index"])
    if not all(math.isfinite(v) and v > 0 for v in (nin, nout)) or nout != 1:
        raise ValueError("Tolerance propagation requires positive real boundary indices and air output")
    wl = float(config.get("wavelength_m", float(config.get("wavelength_nm", 532)) * 1e-9))
    focal = float(config.get("focal_length_m", float(config.get("focal_length_um", 100)) * 1e-6))
    pin = float(np.sum(intensity(field_in) * mask) * dx * dy)
    if pin <= 0 or not math.isfinite(pin):
        raise ValueError("Incident aperture power must be positive")
    height_tables, height_range = None, None
    height_status = "not_evaluated"
    if (height_minus is None) != (height_plus is None):
        raise ValueError("Provide both height-minus and height-plus actual RCWA libraries")
    if height_minus is not None:
        rm, jm = _response_table(height_minus)
        rp, jp = _response_table(height_plus)
        h0, hm, hp = float(candidate["height_nm"]), float(height_minus["height_nm"]), float(height_plus["height_nm"])
        if not np.array_equal(rm, radii) or not np.array_equal(rp, radii) or not hm < h0 < hp:
            raise ValueError("Height interpolation libraries need identical radius axes and bracketing actual heights")
        for changed in (height_minus, height_plus):
            for key in ("input_index", "output_index", "period_nm", "material_id"):
                if key in candidate and key in changed and changed[key] != candidate[key]:
                    raise ValueError(f"Height library must keep {key} identical to nominal")
        height_tables = (jm, jp)
        height_range = (hm - h0, hp - h0)
        height_status = "evaluated_piecewise_complex_response_interpolation"
    factor = math.sqrt(nout / nin)

    def transmitted(delta_diameter: np.ndarray | float, delta_height: float = 0) -> np.ndarray:
        values = radius_map[mask] + np.asarray(delta_diameter)[mask] / 2 if np.ndim(delta_diameter) else radius_map[mask] + delta_diameter / 2
        table = _interpolate_jones(radii, jones, values)
        if height_tables is not None:
            if not height_range[0] <= delta_height <= height_range[1]:
                raise ValueError("height_outside_response_library_interval")
            if delta_height < 0:
                weight = delta_height / height_range[0]
                endpoint = _interpolate_jones(radii, height_tables[0], values)
            else:
                weight = delta_height / height_range[1]
                endpoint = _interpolate_jones(radii, height_tables[1], values)
            # Convex interpolation of physical electric Jones matrices with
            # identical boundary indices preserves passive operator bounds.
            # A derivative added to t can increase |t| above unity even when
            # all three direct responses are passive, so it is not used here.
            table = (1 - weight) * table + weight * endpoint
        output = np.zeros_like(field_in)
        output[:, mask] = factor * np.einsum("ijn,jn->in", table, field_in[:, mask])
        input_local = intensity(field_in)[mask]
        output_local = intensity(output)[mask]
        # Retain a defensive physical check for bad/nonpassive source tables,
        # rather than clip amplitude or report invented >100% transmission.
        illuminated = input_local > input_local.max() * 1e-12
        if np.any(output_local[illuminated] > input_local[illuminated] * 1.001):
            raise ValueError("interpolated_height_model_nonpassive")
        return output

    nominal_out = transmitted(0)
    nominal_focus = intensity(angular_spectrum(nominal_out, dx, wl, focal, dy_m=dy))
    py, px = np.unravel_index(np.argmax(nominal_focus), shape)
    X, Y = np.meshgrid(x, y)
    diameter = float(config.get("diameter_m", float(config.get("diameter_um", 30)) * 1e-6))
    roi_radius = float(config.get("roi_radius_um", 1.22 * wl * focal / diameter * 1e6)) * 1e-6
    if roi_radius <= 0 or not math.isfinite(roi_radius):
        raise ValueError("Tolerance focusing ROI must have a positive finite radius")
    roi = (X - x[px]) ** 2 + (Y - y[py]) ** 2 <= roi_radius ** 2

    def measure(output: np.ndarray) -> dict:
        focal_I = intensity(angular_spectrum(output, dx, wl, focal, dy_m=dy))
        return {"focusing_efficiency": float(np.sum(focal_I[roi]) * dx * dy / pin),
                "transmission_efficiency": float(np.sum(intensity(output)) * dx * dy / pin)}

    nominal = measure(nominal_out)
    threshold = nominal["focusing_efficiency"] * fraction
    seed = int(config.get("seed", 7))
    # Separate streams: adding a sensitivity plot does not change MC samples.
    seed_sequence = np.random.SeedSequence(seed)
    rng_sensitivity, rng_mc = [np.random.default_rng(s) for s in seed_sequence.spawn(2)]
    filter_sigma = (corr_length / (2 * dy), corr_length / (2 * dx))
    sensitivity = []
    patterns = {
        "common_diameter": np.ones(shape),
        "correlated_diameter": _unit_std_pattern(rng_sensitivity, shape, mask, filter_sigma),
        "local_diameter": _unit_std_pattern(rng_sensitivity, shape, mask),
    }
    for name, pattern in patterns.items():
        sigma = settings[name + "_sigma_nm"]
        item = {"parameter": name, "sigma_nm": sigma, "status": "evaluated", "method": "fixed_spatial_realization_plus_minus_one_sigma"}
        changes = []
        for sign, label in ((-1, "minus"), (1, "plus")):
            try:
                performance = measure(transmitted(sign * sigma * pattern))
                item[label + "_focusing_efficiency"] = performance["focusing_efficiency"]
                changes.append(performance["focusing_efficiency"] - nominal["focusing_efficiency"])
            except ValueError as exc:
                item[label + "_status"] = "rejected"
                item[label + "_reason"] = str(exc)
                item["status"] = "partially_evaluated"
        item["max_absolute_efficiency_change"] = max(map(abs, changes)) if changes else None
        if len(changes) == 2 and sigma > 0:
            item["symmetric_derivative_per_nm"] = (changes[1] - changes[0]) / (2 * sigma)
        sensitivity.append(item)
    height_item = {"parameter": "height", "sigma_nm": settings["height_sigma_nm"], "status": height_status}
    if height_tables is not None:
        for sign, label in ((-1, "minus"), (1, "plus")):
            try:
                height_item[label + "_focusing_efficiency"] = measure(transmitted(0, sign * settings["height_sigma_nm"]))["focusing_efficiency"]
            except ValueError as exc:
                height_item[label + "_status"] = "rejected"
                height_item[label + "_reason"] = str(exc)
    else:
        height_item["reason"] = "No actual bracketing-height RCWA libraries supplied; height influence omitted, not inferred from scalar phase"
    sensitivity.append(height_item)
    samples, draws, empirical_spatial = [], [], []
    for number in range(n_samples):
        common = float(rng_mc.normal(scale=settings["common_diameter_sigma_nm"]))
        dh = float(rng_mc.normal(scale=settings["height_sigma_nm"])) if height_tables is not None else 0.0
        corr = _unit_std_pattern(rng_mc, shape, mask, filter_sigma) * settings["correlated_diameter_sigma_nm"]
        # Local errors remain independent Gaussian draws; their finite-sample
        # mean/std naturally fluctuate. Only the correlated field is rescaled.
        local = rng_mc.normal(scale=settings["local_diameter_sigma_nm"], size=shape)
        item = {"sample": number, "status": "evaluated", "diameter_bias_nm": common,
                "height_bias_nm": dh, "height_effect_status": height_status,
                "focusing_efficiency": None, "transmission_efficiency": None}
        empirical_spatial.append([float(np.std(corr[mask])), float(np.std(local[mask]))])
        draws.append([common, dh])
        try:
            item.update(measure(transmitted(common + corr + local, dh)))
            item["passes_assumed_threshold"] = item["focusing_efficiency"] >= threshold
        except ValueError as exc:
            item["status"] = "rejected"
            item["reason"] = str(exc)
            item["passes_assumed_threshold"] = None
        samples.append(item)
    accepted = [s for s in samples if s["status"] == "evaluated"]
    efficiencies = np.asarray([s["focusing_efficiency"] for s in accepted], dtype=float)
    summary = {
        "requested_samples": n_samples, "evaluated_samples": len(accepted),
        "rejected_samples": n_samples - len(accepted),
        "rejected_fraction": (n_samples - len(accepted)) / n_samples,
        "mean_focusing_efficiency": float(np.mean(efficiencies)) if efficiencies.size else None,
        "std_focusing_efficiency": float(np.std(efficiencies, ddof=1)) if efficiencies.size > 1 else None,
        "quantiles": {str(q): float(np.quantile(efficiencies, q)) if efficiencies.size else None for q in (.05, .5, .95)},
        "assumed_threshold_focusing_efficiency": threshold,
        "assumed_threshold_fraction_of_nominal": fraction,
        "conditional_pass_fraction": float(np.mean(efficiencies >= threshold)) if efficiencies.size else None,
        "pass_fraction_denominator": "evaluated samples only; rejected samples are unknown, not fabrication failures",
        "height_effect_status": height_status,
    }
    covariance = np.cov(np.asarray(draws).T, ddof=1).tolist() if n_samples > 1 else None
    return {
        "label": "假设误差下的容差评估", "nominal": nominal, "samples": samples,
        "sensitivity": sensitivity, "summary": summary,
        "rejected_fraction": summary["rejected_fraction"],
        "sample_covariance": {"variables": ["whole_aperture_diameter_bias_nm", "whole_aperture_height_bias_nm"], "units": "nm^2", "matrix": covariance},
        "empirical_spatial_std_nm": {"variables": ["correlated_diameter", "local_diameter"], "per_sample": empirical_spatial},
        "assumptions": {
            **settings, "seed": seed, "distributions": "zero-mean independent normal whole-aperture diameter/height; Gaussian-filtered diameter field; zero-mean local normal diameter field",
            "diameter_to_radius": "radius error = diameter error/2", "correlation_length_definition": "1/e covariance radius; Gaussian filter sigma=length/2",
            "spatial_std_normalization": "correlated field is mean-centered and renormalized over finite aperture; local errors are independent Gaussian draws with naturally fluctuating finite-sample mean/std",
            "radius_response_method": "linear interpolation of real/imaginary physical Jones components; no extrapolation",
            "height_response_method": height_status,
            "height_validity_interval_nm": list(height_range) if height_range is not None else None,
            "height_limitation": "piecewise convex complex-Jones interpolation between actual minus/nominal/plus RCWA libraries; interpolation accuracy between height nodes is not independently verified; outside interval rejected",
            "roi_radius_um": roi_radius * 1e6, "roi_center_um": [float(x[px] * 1e6), float(y[py] * 1e6)],
            "roi_definition": "fixed nominal focal-peak center and radius for every perturbed sample",
            "model_scope": "local-periodic piecewise complex-response interpolation in radius and height plus scalar propagation; no full-device FDTD, neighbor-coupling perturbation or measured process statistics",
            "local_error_grid": "one independent local diameter perturbation per radius-map sample; caller must use one sample per physical unit cell",
            "interpretation": "assumed-threshold pass fraction is conditional model output, not manufacturing yield",
            "polarization": "full 2x2 stored Jones matrix applied to the specified incident field; isotropic circular-pillar first-release model",
        },
    }
