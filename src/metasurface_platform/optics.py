"""Band-limited scalar angular-spectrum propagation and explicit power metrics.

Each Jones component propagates independently through an isotropic output
medium. This model does not validate electromagnetic coupling or longitudinal
fields; a high-NA result requires a vector solver and finite-region checks.
"""
from __future__ import annotations

import math
import numpy as np
from scipy.fft import fft2, ifft2, fftfreq, fftshift

from .beams import intensity, _grid_spacing


def angular_spectrum(field: np.ndarray, dx_m: float, wavelength_m: float,
                     z_m: float, medium_n: float = 1, bandlimit: bool = True,
                     dy_m: float | None = None) -> np.ndarray:
    field = np.asarray(field, dtype=complex)
    if field.ndim not in {2, 3}:
        raise ValueError("Expected scalar [y,x] or Jones [component,y,x] field")
    dy_m = dx_m if dy_m is None else dy_m
    if not all(math.isfinite(v) and v > 0 for v in (dx_m, dy_m, wavelength_m, medium_n)):
        raise ValueError("Grid, wavelength and refractive index must be positive")
    if not math.isfinite(z_m) or z_m < 0:
        raise ValueError("Propagation distance must be finite and nonnegative; backward evanescent growth is unsupported")
    if z_m == 0:
        return field.copy()
    ny, nx = field.shape[-2:]
    FX, FY = np.meshgrid(fftfreq(nx, dx_m), fftfreq(ny, dy_m))
    wavelength_medium = wavelength_m / medium_n
    inv_lambda = 1 / wavelength_medium
    q = inv_lambda ** 2 - FX ** 2 - FY ** 2
    # Positive imaginary kz decays for exp(-iωt) forward propagation.
    kz = 2 * math.pi * np.sqrt(q.astype(complex))
    transfer = np.exp(1j * z_m * kz)
    if bandlimit:
        # Sampling of kz requires |d phase / d fx| <= pi / delta_fx.
        # The coupled ellipses also avoid aliasing at diagonal frequencies.
        lx, ly = nx * dx_m, ny * dy_m
        valid = ((1 + (2 * z_m / lx) ** 2) * FX ** 2 + FY ** 2 <= inv_lambda ** 2)
        valid &= (FX ** 2 + (1 + (2 * z_m / ly) ** 2) * FY ** 2 <= inv_lambda ** 2)
        transfer *= valid
    return ifft2(fft2(field, axes=(-2, -1)) * transfer, axes=(-2, -1))


def target_lens_phase(x: np.ndarray, y: np.ndarray, wavelength_m: float,
                      focal_length_m: float, output_n: float = 1) -> np.ndarray:
    if not all(math.isfinite(v) and v > 0 for v in (wavelength_m, focal_length_m, output_n)):
        raise ValueError("Lens wavelength, focal length and output index must be positive")
    X, Y = np.meshgrid(x, y)
    return -2 * math.pi * output_n / wavelength_m * (np.sqrt(X ** 2 + Y ** 2 + focal_length_m ** 2) - focal_length_m)


def _fwhm(axis: np.ndarray, profile: np.ndarray, peak_index: int) -> float | None:
    """Linearly interpolated crossings of the connected peak, not distant lobes."""
    profile = np.asarray(profile, dtype=float)
    half = profile[peak_index] / 2
    if not math.isfinite(half) or half <= 0:
        return None
    left = peak_index
    while left > 0 and profile[left] >= half:
        left -= 1
    right = peak_index
    while right < len(profile) - 1 and profile[right] >= half:
        right += 1
    if profile[left] >= half or profile[right] >= half:
        return None
    def crossing(i: int, j: int) -> float:
        return float(axis[i] + (half - profile[i]) * (axis[j] - axis[i]) / (profile[j] - profile[i]))
    return crossing(right - 1, right) - crossing(left, left + 1)


def _edge_fraction(I: np.ndarray, width: int = 4) -> float:
    mask = np.ones(I.shape, dtype=bool)
    if min(I.shape) <= 2 * width:
        return 1.0
    mask[width:-width, width:-width] = False
    return float(np.sum(I[mask]) / max(np.sum(I), np.finfo(float).tiny))


def analyze_device(incident: np.ndarray, transmitted: np.ndarray,
                   x: np.ndarray, y: np.ndarray, wavelength_m: float,
                   focal_length_m: float, roi_radius_m: float | None = None,
                   ideal_transmitted: np.ndarray | None = None,
                   z_samples: int = 41) -> dict:
    """Predict focus, xz/yz cuts and far-field angular spectrum in air.

    Efficiency denominator is incident power on the lens aperture; throughput
    uses the transmitted reference plane; focusing ROI is centered on the
    detected focal-plane peak and its size is included in the report.
    """
    incident, transmitted = np.asarray(incident), np.asarray(transmitted)
    dx, dy = _grid_spacing(x, y)
    shape = (len(y), len(x))
    if incident.shape != transmitted.shape or incident.shape[-2:] != shape:
        raise ValueError("Incident and transmitted fields must match both axes")
    if not math.isfinite(focal_length_m) or focal_length_m <= 0 or z_samples < 2:
        raise ValueError("Positive focal distance and at least two z samples are required")
    pin = float(np.sum(intensity(incident)) * dx * dy)
    ptrans = float(np.sum(intensity(transmitted)) * dx * dy)
    if pin <= 0 or ptrans <= 0 or not math.isfinite(pin + ptrans):
        raise ValueError("Incident and transmitted powers must be finite and positive")
    focal = angular_spectrum(transmitted, dx, wavelength_m, focal_length_m, dy_m=dy)
    I = intensity(focal)
    peak_y, peak_x = np.unravel_index(np.argmax(I), I.shape)
    X, Y = np.meshgrid(x, y)
    # Effective illuminated aperture support, not padded propagation-window width.
    support = intensity(incident) > intensity(incident).max() * 1e-12
    diameter = 2 * float(np.max(np.sqrt(X[support] ** 2 + Y[support] ** 2)))
    if roi_radius_m is None:
        roi_radius_m = 1.22 * wavelength_m * focal_length_m / max(diameter, dx)
    if not math.isfinite(roi_radius_m) or roi_radius_m <= 0:
        raise ValueError("Focusing ROI radius must be finite and positive")
    radius = np.sqrt((X - x[peak_x]) ** 2 + (Y - y[peak_y]) ** 2)
    roi = radius <= roi_radius_m
    pfocal = float(np.sum(I) * dx * dy)
    proi = float(np.sum(I[roi]) * dx * dy)
    flat_order = np.argsort(radius.ravel())
    ee_power = np.cumsum(I.ravel()[flat_order]) * dx * dy
    radii = radius.ravel()[flat_order]
    def ee_radius(fraction: float) -> float:
        index = min(int(np.searchsorted(ee_power, pfocal * fraction)), len(radii) - 1)
        return float(radii[index])
    metrics = {
        "incident_aperture_power_w": pin, "transmitted_plane_power_w": ptrans,
        "focal_plane_power_w": pfocal, "roi_power_w": proi,
        "transmission_efficiency": ptrans / pin,
        "focusing_efficiency": proi / pin,
        "encircled_energy_roi_fraction": proi / pfocal,
        "encircled_energy_50_radius_um": ee_radius(0.5) * 1e6,
        "encircled_energy_80_radius_um": ee_radius(0.8) * 1e6,
        "roi_radius_um": roi_radius_m * 1e6,
        "roi_definition": "circular ROI centered on the focal-plane intensity peak",
        "peak_x_um": float(x[peak_x] * 1e6), "peak_y_um": float(y[peak_y] * 1e6),
        "peak_intensity_w_per_m2": float(I.max()),
        "fwhm_x_um": None if (w := _fwhm(x, I[peak_y, :], peak_x)) is None else w * 1e6,
        "fwhm_y_um": None if (w := _fwhm(y, I[:, peak_x], peak_y)) is None else w * 1e6,
        "propagation_power_fraction": pfocal / ptrans,
        "window_edge_power_fraction": _edge_fraction(I),
        "strehl": None,
        "prediction_method": "bandlimited_scalar_angular_spectrum_of_local_periodic_unit_response",
    }
    if ideal_transmitted is not None:
        ideal_transmitted = np.asarray(ideal_transmitted)
        if ideal_transmitted.shape != transmitted.shape:
            raise ValueError("Ideal comparison field must use identical grid and component shape")
        pi = float(np.sum(intensity(ideal_transmitted)) * dx * dy)
        ideal_focal = angular_spectrum(ideal_transmitted, dx, wavelength_m, focal_length_m, dy_m=dy)
        ideal_peak = float(intensity(ideal_focal).max())
        if pi <= 0 or ideal_peak <= 0:
            raise ValueError("Ideal comparison field must have positive power and focus")
        metrics["strehl"] = float((I.max() / ptrans) / (ideal_peak / pi))
        metrics["strehl_definition"] = "peak intensity per transmitted watt divided by ideal same-incident-field, same-aperture scalar lens; throughput factored out"
    z = np.linspace(0, 1.6 * focal_length_m, int(z_samples))
    xz = np.empty((len(z), len(x)))
    yz = np.empty((len(z), len(y)))
    for i, distance in enumerate(z):
        current = intensity(angular_spectrum(transmitted, dx, wavelength_m, float(distance), dy_m=dy))
        xz[i] = current[peak_y, :]
        yz[i] = current[:, peak_x]
    F = fftshift(fft2(transmitted, axes=(-2, -1)), axes=(-2, -1)) * dx * dy
    angular_density = intensity(F)
    fx, fy = fftshift(fftfreq(len(x), dx)), fftshift(fftfreq(len(y), dy))
    ux, uy = wavelength_m * fx, wavelength_m * fy
    UX, UY = np.meshgrid(ux, uy)
    propagating = UX ** 2 + UY ** 2 < 1
    far_intensity = np.where(propagating, angular_density, 0)
    warnings = []
    if pfocal / ptrans < 0.98:
        warnings.append("More than 2% of transmitted power is rejected by propagation band limit/evanescent filtering; enlarge window and check convergence")
    if metrics["window_edge_power_fraction"] > 0.01:
        warnings.append("More than 1% of focal-plane power reaches window edges; increase padding before trusting ROI metrics")
    return {"metrics": metrics, "focal_field": focal, "focal_intensity": I,
            "xz_intensity": xz, "yz_intensity": yz, "z_m": z,
            "far_field_intensity": far_intensity,
            "far_field_direction_cosine_x": ux, "far_field_direction_cosine_y": uy,
            "far_field_theta_x_deg": np.degrees(np.arcsin(np.clip(ux, -1, 1))),
            "far_field_theta_y_deg": np.degrees(np.arcsin(np.clip(uy, -1, 1))),
            "far_field_definition": "Fourier angular-spectrum density |F(fx,fy)|²; not a fully vectorial radiation pattern or a camera PSF",
            "psf_definition": "coherent monochromatic point-focus intensity for the specified illumination; imaging PSF needs the relevant object model and illumination coherence",
            "warnings": warnings}
