"""Power-normalized transverse Jones fields on an explicit incident reference plane.

Fields use sqrt(W/m²) amplitudes, not Maxwell electric-field units. The unit
solver must supply a power-normalized transmission coefficient before applying
it. Complete vector propagation and pulsed/incoherent inputs are outside v0.1.
"""
from __future__ import annotations

import math
import numpy as np
from scipy.fft import next_fast_len


def make_grid(diameter_m: float, dx_m: float, pad_factor: float = 2) -> tuple[np.ndarray, np.ndarray]:
    if not all(math.isfinite(v) and v > 0 for v in (diameter_m, dx_m, pad_factor)) or pad_factor < 1:
        raise ValueError("Diameter and sampling must be positive; pad_factor must be at least one")
    n = next_fast_len(int(math.ceil(diameter_m * pad_factor / dx_m)))
    if n % 2:
        n = next_fast_len(n + 1)
    if n > 2048:
        raise ValueError(f"Propagation grid {n} exceeds first-release 2048 limit; do not silently resample the requested device")
    axis = (np.arange(n) - n // 2) * dx_m
    return axis.copy(), axis.copy()


def jones_vector(polarization: dict | str | None = None) -> np.ndarray:
    """Exp(-iωt) convention; circular handedness is defined by components.

    'left' = (1,+i)/sqrt(2) and 'right' = (1,-i)/sqrt(2), avoiding the
    incompatible handedness names used when viewing toward/along propagation.
    Elliptical: major-axis angle and signed ellipticity angle [-45°,45°].
    """
    p = {"type": polarization} if isinstance(polarization, str) else (polarization or {})
    kind = p.get("type", "linear")
    angle = math.radians(float(p.get("angle_deg", 0)))
    if kind == "linear":
        vector = np.array([math.cos(angle), math.sin(angle)], dtype=complex)
    elif kind in {"left", "left_circular", "lcp"}:
        vector = np.array([1, 1j], dtype=complex) / math.sqrt(2)
    elif kind in {"right", "right_circular", "rcp"}:
        vector = np.array([1, -1j], dtype=complex) / math.sqrt(2)
    elif kind == "elliptical":
        chi_deg = float(p.get("ellipticity_deg", 20))
        if not -45 <= chi_deg <= 45:
            raise ValueError("Ellipticity angle must be within [-45,45] degrees")
        chi = math.radians(chi_deg)
        vector = np.array([math.cos(chi) * math.cos(angle) - 1j * math.sin(chi) * math.sin(angle),
                           math.cos(chi) * math.sin(angle) + 1j * math.sin(chi) * math.cos(angle)])
    elif kind == "jones":
        raw = p.get("components")
        if raw is None or len(raw) != 2:
            raise ValueError("Custom Jones components need [[Re Ex,Im Ex],[Re Ey,Im Ey]]")
        vector = np.array([complex(*v) if isinstance(v, (list, tuple)) else complex(v) for v in raw])
    else:
        raise ValueError(f"Unsupported polarization: {kind}")
    norm = np.linalg.norm(vector)
    if not math.isfinite(norm) or norm == 0:
        raise ValueError("Jones vector must have a finite, nonzero norm")
    return vector / norm


def _grid_spacing(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    if len(x) < 2 or len(y) < 2:
        raise ValueError("The field grid requires at least two samples per axis")
    dx, dy = float(x[1] - x[0]), float(y[1] - y[0])
    if dx <= 0 or dy <= 0 or not np.allclose(np.diff(x), dx, rtol=1e-8, atol=1e-15) or not np.allclose(np.diff(y), dy, rtol=1e-8, atol=1e-15):
        raise ValueError("Field axes must be uniformly spaced and increasing")
    return dx, dy


def intensity(field: np.ndarray) -> np.ndarray:
    field = np.asarray(field)
    return np.sum(np.abs(field) ** 2, axis=0) if field.ndim == 3 else np.abs(field) ** 2


def incident_field(x: np.ndarray, y: np.ndarray, wavelength_m: float, beam: dict,
                   aperture: np.ndarray | None = None) -> dict:
    if not math.isfinite(wavelength_m) or wavelength_m <= 0:
        raise ValueError("Wavelength must be positive")
    dx, dy = _grid_spacing(x, y)
    X, Y = np.meshgrid(x, y)
    X = X - float(beam.get("center_x_um", 0)) * 1e-6
    Y = Y - float(beam.get("center_y_um", 0)) * 1e-6
    medium_n = float(beam.get("medium_n", 1))
    if not math.isfinite(medium_n) or medium_n <= 0:
        raise ValueError("Incident medium refractive index must be positive")
    theta = math.radians(float(beam.get("theta_deg", 0)))
    phi = math.radians(float(beam.get("phi_deg", 0)))
    if not math.isfinite(theta) or abs(theta) > math.radians(10):
        raise ValueError("Scalar preview supports beam tilts up to 10 degrees")
    sx, sy, sz = math.sin(theta) * math.cos(phi), math.sin(theta) * math.sin(phi), math.cos(theta)
    k = 2 * math.pi * medium_n / wavelength_m
    carrier = np.exp(1j * k * (sx * X + sy * Y))
    kind = beam.get("type", "plane")
    metadata = {"type": kind, "medium_n": medium_n, "reference_plane_z_m": 0.0,
                "amplitude_units": "sqrt(W/m^2), power-normalized transverse Jones surrogate",
                "phasor_convention": "exp(-i omega t)", "circular_convention": "left=(1,+i)/sqrt(2), right=(1,-i)/sqrt(2)",
                "theta_deg": math.degrees(theta), "preview_only_tilt": theta != 0}
    waist_m = None
    if kind == "plane":
        amplitude = carrier
    elif kind == "gaussian":
        waist_m = float(beam.get("waist_um", 12)) * 1e-6
        if not math.isfinite(waist_m) or waist_m <= 0:
            raise ValueError("Gaussian waist radius w0 must be positive")
        if waist_m < 2 * wavelength_m / medium_n:
            raise ValueError("Gaussian waist below two wavelengths needs a nonparaxial field model")
        zr = math.pi * medium_n * waist_m ** 2 / wavelength_m
        z = -float(beam.get("waist_z_um", 0)) * 1e-6 + sx * X + sy * Y
        w = waist_m * np.sqrt(1 + (z / zr) ** 2)
        r2 = X ** 2 + Y ** 2 - (sx * X + sy * Y) ** 2
        # 1/R=z/(z²+zR²), including a numerically safe exact waist plane.
        amplitude = (waist_m / w * np.exp(-r2 / w ** 2)
                     * np.exp(1j * k * r2 * z / (2 * (z ** 2 + zr ** 2)) - 1j * np.arctan(z / zr)) * carrier)
        metadata.update({"waist_radius_m": waist_m, "waist_z_m": float(beam.get("waist_z_um", 0)) * 1e-6,
                         "rayleigh_range_m": zr, "waist_definition": "1/e^2 intensity radius",
                         "peak_intensity_reference": "at the beam waist, measured perpendicular to propagation"})
    else:
        raise ValueError("First release supports plane and Gaussian beams only")
    mask = np.ones(X.shape, dtype=bool) if aperture is None else np.asarray(aperture, dtype=bool)
    if mask.shape != X.shape or not np.any(mask):
        raise ValueError("Aperture must be a nonempty mask matching the field grid")
    norm = beam.get("normalization", "normalized")
    aliases = {"I0": "peak_intensity", "Pbeam": "beam_power", "Paperture": "aperture_power"}
    norm = aliases.get(norm, norm)
    value = float(beam.get("value", 1))
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Beam intensity/power reference must be positive and finite")
    # axial flux cos(theta); both polarization components sum to this flux.
    amplitude = amplitude * math.sqrt(sz)
    raw_aperture_power = float(np.sum(np.abs(amplitude[mask]) ** 2) * dx * dy)
    if raw_aperture_power <= 0 or not math.isfinite(raw_aperture_power):
        raise ValueError("Beam misses aperture or underflows numerically")
    if norm == "peak_intensity":
        scale = math.sqrt(value)
    elif norm == "beam_power":
        if waist_m is None:
            raise ValueError("An infinite plane wave has no finite total power; choose aperture_power or peak_intensity")
        scale = math.sqrt(2 * value / (math.pi * waist_m ** 2))
    elif norm in {"aperture_power", "normalized"}:
        scale = math.sqrt(value / raw_aperture_power)
    else:
        raise ValueError("Normalization must be normalized, peak_intensity (W/m²), beam_power (W), or aperture_power (W)")
    jones = jones_vector(beam.get("polarization"))
    unmasked = jones[:, None, None] * (scale * amplitude)[None, :, :]
    field = unmasked * mask[None, :, :]
    I = intensity(field)
    metadata.update({"normalization": norm, "normalization_value": value,
                     "power_interpretation": "mathematical W reference; no measured source power" if norm == "normalized" else "specified optical power/intensity",
                     "aperture_truncation": True, "coherence": "single monochromatic coherent beam"})
    return {"field": field, "unmasked_field": unmasked, "intensity": I, "jones": jones,
            "power_w": float(np.sum(I) * dx * dy), "metadata": metadata}


def coherent_sum(fields: list[np.ndarray], relative_phases_rad: list[float] | None = None,
                 *, same_wavelength_and_reference: bool = False) -> np.ndarray:
    """Only sum complex fields when coherence and reference planes are explicit."""
    if not same_wavelength_and_reference:
        raise ValueError("Coherent summation requires identical wavelength, reference plane, normalization and known relative phases")
    if not fields or any(f.shape != fields[0].shape for f in fields):
        raise ValueError("Coherent fields require a common nonempty grid and component shape")
    if relative_phases_rad is None and len(fields) > 1:
        raise ValueError("Multiple coherent beams need explicit relative phases, including explicit zero for in-phase illumination")
    phases = relative_phases_rad if relative_phases_rad is not None else [0.0]
    if len(phases) != len(fields) or not np.all(np.isfinite(phases)):
        raise ValueError("Provide one finite relative phase per beam")
    return np.sum([np.asarray(f) * np.exp(1j * p) for f, p in zip(fields, phases)], axis=0)
