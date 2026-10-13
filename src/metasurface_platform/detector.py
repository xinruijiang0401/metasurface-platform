"""On-demand scalar diffraction to a movable detector plane.

The saved exit-plane Jones fields are propagated through air with a direct
Rayleigh-Sommerfeld first integral. This is a scalar, source-sampled model, not
a full-vector Maxwell solution or a finite-device FDTD calculation.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import re

import h5py
import numpy as np


MAX_RESOLUTION = 128
MAX_EXTENT_UM = 100.0
MAX_KERNEL_PAIRS = 20_000_000
MAX_MAIN_PAIRS = 16_000_000
MAX_CONVERGENCE_PAIRS = MAX_KERNEL_PAIRS - MAX_MAIN_PAIRS


def _unavailable(reason: str) -> dict:
    return {"status": "unavailable", "reason": reason,
            "metadata": {"method": "scalar Rayleigh-Sommerfeld first integral", "data_source": "saved output_power_field at the metasurface exit plane"}}


def _positive(value, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite positive number") from exc
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{label} must be a finite positive number")
    return result


def _integer(value, label: str, lo: int = 2, hi: int = MAX_RESOLUTION) -> int:
    if isinstance(value, (bool, np.bool_)):
        raise ValueError(f"{label} must be an integer in [{lo},{hi}]")
    try:
        num = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an integer in [{lo},{hi}]") from exc
    if not math.isfinite(num) or not num.is_integer() or not lo <= int(num) <= hi:
        raise ValueError(f"{label} must be an integer in [{lo},{hi}]")
    return int(num)


def _request_config(config: dict) -> dict:
    if not isinstance(config, dict):
        raise ValueError("Detector configuration must be a mapping")
    nested = config.get("detector", {})
    if nested is not None and not isinstance(nested, dict):
        raise ValueError("detector must be a mapping")
    result = dict(config)
    result.pop("detector", None)
    result.update(nested or {})
    return result


def _geometry(config: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, dict]:
    cfg = _request_config(config)
    if "center_um" in cfg:
        center_um = np.asarray(cfg["center_um"], dtype=float)
    else:
        center_um = np.asarray([cfg.get("center_x_um", 0), cfg.get("center_y_um", 0), cfg.get("center_z_um", 0)], dtype=float)
    if center_um.shape != (3,) or not np.all(np.isfinite(center_um)):
        raise ValueError("center_um or center_x_um/center_y_um/center_z_um must contain three finite coordinates")

    if "normal" in cfg:
        normal = np.asarray(cfg["normal"], dtype=float)
        if normal.shape != (3,) or not np.all(np.isfinite(normal)) or np.linalg.norm(normal) == 0:
            raise ValueError("normal must be a finite nonzero 3-vector")
        normal /= np.linalg.norm(normal)
        theta_deg = math.degrees(math.acos(float(np.clip(normal[2], -1, 1))))
        phi_deg = math.degrees(math.atan2(float(normal[1]), float(normal[0])))
    else:
        theta_deg = float(cfg.get("normal_theta_deg", 0))
        phi_deg = float(cfg.get("normal_phi_deg", 0))
        if not math.isfinite(theta_deg) or not 0 <= theta_deg <= 180 or not math.isfinite(phi_deg):
            raise ValueError("normal_theta_deg must be in [0,180] and normal_phi_deg finite")
        theta, phi = math.radians(theta_deg), math.radians(phi_deg)
        normal = np.array([math.sin(theta) * math.cos(phi), math.sin(theta) * math.sin(phi), math.cos(theta)])
        normal /= np.linalg.norm(normal)

    size = cfg.get("size_um")
    if size is not None:
        size = np.asarray(size, dtype=float)
        if size.shape != (2,):
            raise ValueError("size_um must contain width and height")
        width_um, height_um = map(float, size)
    else:
        width_um, height_um = cfg.get("width_um", 20), cfg.get("height_um", 20)
    width_um, height_um = _positive(width_um, "width_um"), _positive(height_um, "height_um")
    width_m, height_m = width_um * 1e-6, height_um * 1e-6
    if max(width_um, height_um) > MAX_EXTENT_UM:
        raise ValueError(f"Detector width and height must each be no larger than {MAX_EXTENT_UM:g} um")
    resolution = cfg.get("resolution", [64, 64])
    if np.isscalar(resolution):
        if isinstance(resolution, (bool, np.bool_)):
            raise ValueError("resolution must be a grid size or [nx, ny]")
        nx = ny = _integer(resolution, "resolution")
    else:
        if not isinstance(resolution, (list, tuple, np.ndarray)) or len(resolution) != 2:
            raise ValueError("resolution must be a grid size or [nx, ny]")
        nx = _integer(resolution[0], "resolution[0]")
        ny = _integer(resolution[1], "resolution[1]")
    roll_deg = float(cfg.get("roll_deg", cfg.get("rotation_deg", 0)))
    if not math.isfinite(roll_deg):
        raise ValueError("roll_deg must be finite")

    ref = np.array([1., 0., 0.]) if abs(normal[0]) < .9 else np.array([0., 1., 0.])
    e1 = ref - np.dot(ref, normal) * normal
    e1 /= np.linalg.norm(e1)
    e2 = np.cross(normal, e1)
    roll = math.radians(roll_deg)
    e1r = math.cos(roll) * e1 + math.sin(roll) * e2
    e2r = -math.sin(roll) * e1 + math.cos(roll) * e2
    u = ((np.arange(nx) + .5) / nx - .5) * width_m
    v = ((np.arange(ny) + .5) / ny - .5) * height_m
    U, V = np.meshgrid(u, v)
    center_m = center_um * 1e-6
    positions = center_m + U[..., None] * e1r + V[..., None] * e2r
    geometry = {
        "center_um": center_um.tolist(), "normal": normal.tolist(),
        "normal_theta_deg": theta_deg, "normal_phi_deg": phi_deg,
        "basis_u": e1r.tolist(), "basis_v": e2r.tolist(), "roll_deg": roll_deg,
        "width_um": float(width_um), "height_um": float(height_um),
        "resolution": [nx, ny], "sampling": "pixel centers over the stated full width and height",
    }
    return center_m, normal, e1r, e2r, positions, geometry


def _uniform_step(axis: np.ndarray, name: str) -> float:
    axis = np.asarray(axis, dtype=float)
    if axis.ndim != 1 or axis.size < 2 or not np.all(np.isfinite(axis)):
        raise ValueError(f"Saved {name} axis must contain at least two finite samples")
    delta = np.diff(axis)
    step = float(delta[0])
    if step <= 0 or not np.allclose(delta, step, rtol=1e-7, atol=1e-15):
        raise ValueError(f"Saved {name} axis must be uniformly spaced and increasing")
    return step


def _coarsen(field: np.ndarray, aperture: np.ndarray, x: np.ndarray, y: np.ndarray,
             stride: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, float]:
    """Area-average complex Jones samples into larger source cells.

    Zero field outside the illuminated aperture participates in block averages,
    preserving the discrete source integral while reducing kernel evaluations.
    """
    dx, dy = _uniform_step(x, "x"), _uniform_step(y, "y")
    if stride == 1:
        active = aperture.ravel()
        yy, xx = np.nonzero(aperture)
        return x[xx], y[yy], field[:, yy, xx], np.ones(len(xx), bool), dx * dy
    ny, nx = aperture.shape
    by, bx = math.ceil(ny / stride), math.ceil(nx / stride)
    py, px = by * stride - ny, bx * stride - nx
    padded = np.pad(field * aperture[None, :, :], ((0, 0), (0, py), (0, px)))
    blocks = padded.reshape(field.shape[0], by, stride, bx, stride).mean(axis=(2, 4))
    maskp = np.pad(aperture, ((0, py), (0, px)))
    active = maskp.reshape(by, stride, bx, stride).any(axis=(1, 3))
    iy, ix = np.nonzero(active)
    xx = x[0] + (ix * stride + (stride - 1) / 2) * dx
    yy = y[0] + (iy * stride + (stride - 1) / 2) * dy
    return xx, yy, blocks[:, iy, ix], active.ravel()[active.ravel()], stride * stride * dx * dy


def _active_blocks(aperture: np.ndarray, stride: int) -> int:
    ny, nx = aperture.shape
    by, bx = math.ceil(ny / stride), math.ceil(nx / stride)
    maskp = np.pad(aperture, ((0, by * stride - ny), (0, bx * stride - nx)))
    return int(maskp.reshape(by, stride, bx, stride).any(axis=(1, 3)).sum())


def _rs_integral(points: np.ndarray, source_x: np.ndarray, source_y: np.ndarray,
                 source_field: np.ndarray, cell_area: float, wavelength_m: float) -> np.ndarray:
    """Return [component, point] field using the outgoing RS-I kernel."""
    if not len(source_x):
        raise ValueError("No illuminated source cells are available")
    k = 2 * math.pi / wavelength_m
    result = np.zeros((source_field.shape[0], len(points)), dtype=complex)
    chunk_size = max(1, min(128, 400_000 // max(1, len(source_x))))
    sx = source_x[None, :]
    sy = source_y[None, :]
    for start in range(0, len(points), chunk_size):
        stop = min(len(points), start + chunk_size)
        px = points[start:stop, 0, None]
        py = points[start:stop, 1, None]
        dz = points[start:stop, 2, None]
        r = np.sqrt((px - sx) ** 2 + (py - sy) ** 2 + dz ** 2)
        kernel = (dz / (1j * wavelength_m * r ** 2)
                  * (1 - 1 / (1j * k * r)) * np.exp(1j * k * r))
        result[:, start:stop] = source_field @ kernel.T * cell_area
    return result


def _load_run_data(run_dir: Path):
    fields_path = run_dir / "fields.h5"
    if not fields_path.is_file():
        return None, _unavailable("This run has no saved fields.h5; arbitrary-plane measurement is unavailable")
    try:
        with h5py.File(fields_path, "r") as h5:
            required = ("x_m", "y_m", "output_power_field", "aperture_mask")
            missing = [name for name in required if name not in h5]
            if missing:
                return None, _unavailable("Saved field file is missing required datasets: " + ", ".join(missing))
            x, y = h5["x_m"][()], h5["y_m"][()]
            field = h5["output_power_field"][()]
            aperture = h5["aperture_mask"][()].astype(bool)
            metadata = json.loads(h5.attrs.get("metadata", "{}"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return None, _unavailable(f"Saved field data could not be read: {exc}")
    return {"x": x, "y": y, "field": field, "aperture": aperture,
            "fields_metadata": metadata}, None


def measure_plane(run_dir: str | Path, config: dict) -> dict:
    """Compute complex field and intensity on a user-positioned detector plane.

    Configuration accepts the exact flat interface keys
    ``center_x_um``, ``center_y_um``, ``center_z_um``, ``normal_theta_deg``,
    ``normal_phi_deg``, ``width_um``, ``height_um``, ``roll_deg``, and
    ``resolution=[nx,ny]``.  ``center_um=[x,y,z]``, ``normal=[nx,ny,nz]``,
    ``size_um=[w,h]``, and ``rotation_deg`` are also accepted as compact aliases.
    A nested ``detector`` map is accepted for UI payloads.
    """
    run_dir = Path(run_dir).resolve()
    loaded, unavailable = _load_run_data(run_dir)
    if unavailable:
        return unavailable
    x, y = loaded["x"], loaded["y"]
    field, aperture = loaded["field"], loaded["aperture"]
    if field.ndim != 3 or field.shape[0] != 2 or field.shape[1:] != aperture.shape or field.shape[1:] != (len(y), len(x)):
        return _unavailable("Saved output_power_field must have shape [2, len(y_m), len(x_m)]")
    if not np.all(np.isfinite(field)) or not aperture.any():
        return _unavailable("Saved source field is empty or contains nonfinite values")

    h5_config = loaded["fields_metadata"].get("config", {})
    if isinstance(h5_config, str):
        try:
            h5_config = json.loads(h5_config)
        except json.JSONDecodeError:
            h5_config = {}
    config_path = run_dir / "config.json"
    run_config = {}
    if config_path.is_file():
        try:
            run_config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            run_config = {}
    run_config = {**h5_config, **run_config}
    wavelength_m = run_config.get("wavelength_m")
    if wavelength_m is None and run_config.get("wavelength_nm") is not None:
        wavelength_m = float(run_config["wavelength_nm"]) * 1e-9
    if wavelength_m is None or not math.isfinite(float(wavelength_m)) or float(wavelength_m) <= 0:
        return _unavailable("Run wavelength is missing from config.json and fields.h5 metadata")
    wavelength_m = float(wavelength_m)

    _, _, _, _, positions, geometry = _geometry(config)
    clearance = .05 * wavelength_m
    if float(np.min(positions[..., 2])) <= clearance:
        raise ValueError(f"Every detector pixel must be at z > {clearance * 1e6:.6g} um downstream of the saved metasurface-exit plane")

    dx, dy = _uniform_step(x, "x"), _uniform_step(y, "y")
    pixel_count = int(np.prod(geometry["resolution"]))
    main_budget = MAX_MAIN_PAIRS
    stride = 1
    while _active_blocks(aperture, stride) * pixel_count > main_budget:
        stride += 1
        if stride > max(aperture.shape):
            raise ValueError("Unable to satisfy detector kernel work limit with the saved source grid")
    sx, sy, source_values, _, cell_area = _coarsen(field, aperture, x, y, stride)
    active_source_count = len(sx)
    if active_source_count * pixel_count > main_budget or (active_source_count * pixel_count + MAX_CONVERGENCE_PAIRS) > MAX_KERNEL_PAIRS:
        raise RuntimeError("Internal detector kernel-work bound was exceeded")
    flat_points = positions.reshape(-1, 3)
    coarse = _rs_integral(flat_points, sx, sy, source_values, cell_area, wavelength_m)
    field_plane = coarse.reshape(2, geometry["resolution"][1], geometry["resolution"][0])
    intensity_plane = np.sum(np.abs(field_plane) ** 2, axis=0)

    warnings = []
    convergence = {"status": "not_needed_full_source_sampling", "relative_field_l2": 0.0,
                   "probe_points": 0, "method": "source-cell averaging refinement at detector center/edges"}
    work_pairs = active_source_count * pixel_count
    if stride > 1:
        fine_stride = max(1, stride // 2)
        fx, fy, fine_values, _, fine_area = _coarsen(field, aperture, x, y, fine_stride)
        ny_det, nx_det = field_plane.shape[1:]
        ix = np.unique(np.rint(np.linspace(0, nx_det - 1, 3)).astype(int))
        iy = np.unique(np.rint(np.linspace(0, ny_det - 1, 3)).astype(int))
        probe_flat = np.array([j * nx_det + i for j in iy for i in ix], dtype=int)
        per_probe = len(sx) + len(fx)
        n_probe = min(len(probe_flat), MAX_CONVERGENCE_PAIRS // max(1, per_probe))
        if n_probe >= 4:
            select = np.rint(np.linspace(0, len(probe_flat) - 1, n_probe)).astype(int)
            probe_flat = probe_flat[select]
            coarse_check = _rs_integral(flat_points[probe_flat], sx, sy, source_values, cell_area, wavelength_m)
            fine_check = _rs_integral(flat_points[probe_flat], fx, fy, fine_values, fine_area, wavelength_m)
            denom = max(float(np.linalg.norm(fine_check)), np.finfo(float).tiny)
            relative_l2 = float(np.linalg.norm(coarse_check - fine_check) / denom)
            convergence = {"status": "evaluated", "relative_field_l2": relative_l2,
                           "coarse_source_stride": stride, "fine_source_stride": fine_stride,
                           "probe_points": len(probe_flat), "probe_kernel_pairs": int(per_probe * len(probe_flat)),
                           "method": "direct RS-I on a 3x3 detector center/edge subset using source-cell averages"}
            work_pairs += per_probe * len(probe_flat)
            if relative_l2 > .1:
                warnings.append("Coarse/finer source-cell integration differs by more than 10% in field L2 on probe points; increase saved source sampling or reduce detector resolution")
        else:
            convergence = {"status": "not_evaluated_work_limit", "coarse_source_stride": stride,
                           "fine_source_stride": fine_stride, "probe_points": 0,
                           "method": "refinement comparison omitted because it would exceed the kernel work cap"}
            warnings.append("Source-sampling refinement check could not fit within the 20-million kernel-pair work cap")
    if work_pairs > MAX_KERNEL_PAIRS:
        raise RuntimeError("Detector operation count exceeded the 20-million kernel-pair cap")

    nx, ny = geometry["resolution"]
    pixel_du = geometry["width_um"] / nx
    pixel_dv = geometry["height_um"] / ny
    coarse_dx, coarse_dy = stride * dx, stride * dy
    src_nyquist = min(1 / (2 * coarse_dx), 1 / (2 * coarse_dy))
    max_transverse_freq = 1 / wavelength_m
    support_fraction = min(1.0, src_nyquist / max_transverse_freq)
    if stride > 1:
        warnings.append("Source field was area-averaged to respect the direct-kernel cap; oblique/high-spatial-frequency components may be aliased")
    if support_fraction < .95:
        warnings.append("Effective source-grid Nyquist does not represent the full scalar propagating spatial-frequency disk")
    if geometry["normal_theta_deg"] > 30:
        warnings.append("Highly tilted detector orientation is evaluated with a scalar RS-I model; vector boundary-field accuracy is unverified")

    beam = run_config.get("beam", {}) if isinstance(run_config.get("beam", {}), dict) else {}
    normalization = beam.get("normalization", "unknown")
    absolute_user_scale = normalization in {"peak_intensity", "beam_power", "aperture_power", "Pbeam", "Paperture", "I0"}
    if normalization == "normalized":
        intensity_label = "reference_scale_power_density_w_m2_not_measured"
    else:
        intensity_label = "user_scale_power_density_w_m2_not_measured"

    result_file = run_dir / "result.json"
    sample_source = None
    if result_file.is_file():
        try:
            run_result = json.loads(result_file.read_text(encoding="utf-8"))
            sample_source = run_result.get("material_selection", {}).get("selected", {}).get("source_id")
        except (OSError, json.JSONDecodeError):
            pass
    metadata = {
        "status": "computed", "method": "direct scalar Rayleigh-Sommerfeld first integral (RS-I)",
        "kernel": "z/(i*lambda*r^2) * (1 - 1/(i*k*r)) * exp(i*k*r), exp(-i omega t) convention",
        "medium": "air, homogeneous half-space after the saved pillar-exit plane; n≈1 and air dispersion ignored",
        "source_plane_z_um": 0.0, "minimum_downstream_clearance_um": clearance * 1e6,
        "source_field": "fields.h5:output_power_field, shaped [Jones component,y,x], at metasurface pillar exit plane",
        "source_axes": "fields.h5:x_m and y_m are increasing sample-center axes; field sample cell area is dx*dy",
        "source_boundary_conditions": "finite saved exit-plane field with zero field outside saved aperture/grid; outgoing scalar radiation; no backward propagation, post-exit reflections, or subsequent structures",
        "source_material_sample": sample_source,
        "wavelength_nm": wavelength_m * 1e9,
        "field_quantity": "saved power-normalized transverse Jones surrogate; components propagated independently as scalar fields; no detector-plane vector projection or Ez/H reconstruction",
        "intensity_scale": intensity_label, "beam_normalization": normalization,
        "user_provided_absolute_scale": bool(absolute_user_scale), "absolute_scale_is_measured": False,
        "geometry": geometry,
        "source_grid": {"original_shape_yx": list(aperture.shape), "original_dx_nm": dx * 1e9,
                        "original_dy_nm": dy * 1e9, "source_aperture_cells": int(aperture.sum()),
                        "integration_stride": stride, "integration_cell_dx_nm": coarse_dx * 1e9,
                        "integration_cell_dy_nm": coarse_dy * 1e9,
                        "integration_source_cells": active_source_count,
                        "coarse_complex_field": "area-averaged over stride x stride source samples, including zero field outside aperture to preserve integrated source amplitude",
                        "effective_source_nyquist_cycles_per_m": src_nyquist,
                        "propagating_disk_radial_support_fraction": support_fraction,
                        "limitation": "source sampling limits arbitrary detector directions; no missing subcell/vector field is reconstructed; boundary residual blocks and source-cell averaging can bias spatial moments"},
        "detector_sampling": {"pixel_pitch_u_um": pixel_du, "pixel_pitch_v_um": pixel_dv,
                              "field_sampling_nyquist_u_cycles_per_m": 1 / (2 * pixel_du * 1e-6),
                              "field_sampling_nyquist_v_cycles_per_m": 1 / (2 * pixel_dv * 1e-6)},
        "convergence_check": convergence | {"interpretation": "sampling sensitivity on a small detector center/edge subset; not a full-plane convergence proof"},
        "work": {"kernel_source_point_pairs": int(work_pairs), "maximum_allowed_pairs": MAX_KERNEL_PAIRS,
                 "detector_pixels": pixel_count},
        "warnings": warnings,
        "limitations": ["scalar diffraction in air with n≈1; not full-vector Maxwell or FDTD", "oblique/high-NA vector accuracy is unverified",
                        "source cell averaging may alias high spatial frequencies", "no Fresnel reflection or finite substrate model",
                        "intensity absolute scale is not experimentally measured"],
        "open_source_references": [
            {"name": "waveprop", "url": "https://github.com/ebezzam/waveprop", "role": "open-source scalar diffraction/propagation reference; not invoked by this direct RS-I calculation"},
            {"name": "POPPY", "url": "https://github.com/spacetelescope/poppy", "role": "open-source scalar Fourier-optics reference; not invoked by this direct RS-I calculation"},
        ],
    }
    return {"status": "computed", "x_um": (((np.arange(nx) + .5) / nx - .5) * geometry["width_um"]),
            "y_um": (((np.arange(ny) + .5) / ny - .5) * geometry["height_um"]),
            "field_real": field_plane.real, "field_imag": field_plane.imag,
            "intensity": intensity_plane, "metadata": metadata}


def measure_job(runs_root: str | Path, job_id: str, config: dict) -> dict:
    """Job-ID adapter for a backend probe route; rejects path traversal."""
    if not isinstance(job_id, str) or not re.fullmatch(r"\d{8}-\d{6}-[0-9a-fA-F]{6}", job_id):
        raise ValueError("job_id must be a persistent run ID in YYYYMMDD-HHMMSS-xxxxxx form, not an ephemeral queue ID")
    root = Path(runs_root).resolve()
    if root.name.lower() != "runs":
        root = root / "runs"
    run_dir = (root / job_id).resolve()
    if not run_dir.is_relative_to(root):
        raise ValueError("job_id escapes the configured runs directory")
    if not run_dir.is_dir():
        return _unavailable("Run job is not available")
    return measure_plane(run_dir, config)
