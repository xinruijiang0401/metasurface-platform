"""Periodic RCWA with grcwa; no synthetic or effective-index responses.

Lengths passed to grcwa are in nm, so its frequency is 1 / wavelength_nm.
Time convention is exp(-i omega t), and a passive index is n + i k.
The default illumination travels from a lossless SiO2 half space, through
the air-background pillar layer, into air. The electric Jones matrix is
referenced from the entrance of the pillar layer to its exit plane; it is
NOT a power-normalized scattering matrix. Flux efficiencies are separately
computed by grcwa, including all propagating diffraction orders.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import asdict, dataclass
from importlib.metadata import version
from time import perf_counter
from typing import Any, Callable, Iterable

import numpy as np


@dataclass(frozen=True)
class RCWASettings:
    harmonics: int = 25
    grid_size: int = 64

    def __post_init__(self) -> None:
        if self.harmonics < 5 or self.grid_size < 16:
            raise ValueError("RCWA needs at least 5 harmonics and a 16-pixel grid.")


def _index(value: complex | float | Iterable[float]) -> complex:
    if isinstance(value, (list, tuple, np.ndarray)):
        value = complex(*value)
    value = complex(value)
    if not np.isfinite(value) or value.real <= 0 or value.imag < 0:
        raise ValueError("Refractive index must have n > 0, k >= 0 and be finite.")
    return value


def _settings(value: RCWASettings | dict | None) -> RCWASettings:
    return RCWASettings(**value) if isinstance(value, dict) else value or RCWASettings()


def _boundary_indices(substrate_index: complex, incidence: str) -> tuple[float, float]:
    substrate_index = _index(substrate_index)
    if substrate_index.imag != 0:
        raise ValueError("MVP requires a lossless substrate boundary; annotate any k=0 assumption.")
    if incidence == "substrate":
        return substrate_index.real, 1.0
    if incidence == "air":
        return 1.0, substrate_index.real
    raise ValueError("incidence must be 'substrate' or 'air'.")


def _thread_limit():
    try:
        from threadpoolctl import threadpool_limits

        return threadpool_limits(limits=1)
    except ImportError:
        return nullcontext()


def _new_problem(
    wavelength_nm: float,
    period_x_nm: float,
    period_y_nm: float,
    height_nm: float,
    n_in: float,
    n_out: float,
    settings: RCWASettings,
    epsilon_grid: np.ndarray | None,
):
    import grcwa

    grcwa.set_backend("numpy")
    obj = grcwa.obj(
        settings.harmonics,
        [period_x_nm, 0.0],
        [0.0, period_y_nm],
        1.0 / wavelength_nm,
        0.0,
        0.0,
        verbose=0,
    )
    obj.Add_LayerUniform(0.0, n_in**2)
    if epsilon_grid is None:
        obj.Add_LayerUniform(height_nm, 1.0)
    else:
        obj.Add_LayerGrid(height_nm, *epsilon_grid.shape)
    obj.Add_LayerUniform(0.0, n_out**2)
    obj.Init_Setup()
    if epsilon_grid is not None:
        max_g = np.max(np.abs(obj.G), axis=0)
        if np.any(np.asarray(epsilon_grid.shape) <= 4 * max_g):
            raise ValueError("Grid undersamples the retained Fourier-order differences.")
        obj.GridLayer_geteps(epsilon_grid.ravel())
    return obj


def _electric_fourier(obj, layer: int, z_nm: float) -> np.ndarray:
    result = obj.Solve_FieldFourier(layer, float(z_nm))
    # grcwa 0.1.2 returns [E,H]; newer releases wrap single z results in a list.
    if np.asarray(result).ndim == 4:
        result = result[0]
    return np.asarray(result[0], dtype=complex)


def _electric_grid(obj, layer: int, z_nm: float, shape: tuple[int, int]) -> np.ndarray:
    # Version 0.1.2 cannot reconstruct fields in uniform exterior layers via
    # Solve_FieldOnGrid. Use its own inverse-FFT routine on the public Fourier
    # fields, which supports both patterned and exterior sampling planes.
    from grcwa.fft_funs import get_ifft

    fields = _electric_fourier(obj, layer, float(z_nm))
    return np.stack([get_ifft(*shape, component, obj.G) for component in fields])


def _excite(obj, column: int) -> None:
    # At theta=phi=0, p is Ex and s is Ey. grcwa sets incident |H|=1,
    # hence incident |E|=1/n_in, which is corrected below.
    obj.MakeExcitationPlanewave(
        p_amp=1.0 if column == 0 else 0.0,
        p_phase=0.0,
        s_amp=1.0 if column == 1 else 0.0,
        s_phase=0.0,
        order=0,
    )


def _response(obj, n_in: float, n_out: float, with_fields: bool = False, grid_shape=None):
    zero = int(np.flatnonzero(np.all(np.asarray(obj.G) == 0, axis=1))[0])
    jones = np.empty((2, 2), complex)
    power_r = np.empty(2)
    power_t = np.empty(2)
    r_order = np.empty((2, obj.nG))
    t_order = np.empty((2, obj.nG))
    fields = []
    for column in range(2):
        _excite(obj, column)
        ro, to = obj.RT_Solve(normalize=1, byorder=1)
        r_order[column] = np.real(np.asarray(ro))
        t_order[column] = np.real(np.asarray(to))
        power_r[column] = r_order[column].sum()
        power_t[column] = t_order[column].sum()
        electric = _electric_fourier(obj, obj.Layer_N - 1, 0.0)
        jones[:, column] = electric[:2, zero] * n_in
        if with_fields:
            fields.append(
                {
                    "inside_midheight": _electric_grid(
                        obj, 1, obj.thickness_list[1] / 2, grid_shape
                    ) * n_in,
                    "output_plane": _electric_grid(
                        obj, obj.Layer_N - 1, 0.0, grid_shape
                    ) * n_in,
                }
            )
    # Independent electric-flux cross-check for the normal zero order.
    zero_from_e = n_out / n_in * np.sum(np.abs(jones) ** 2, axis=0)
    zero_flux = t_order[:, zero]
    if not np.allclose(zero_from_e, zero_flux, rtol=2e-6, atol=2e-7):
        raise RuntimeError("Electric Jones normalization disagrees with grcwa zero-order flux.")
    result = {
        "jones_t": jones,
        "power_R": power_r,
        "power_T": power_t,
        "power_T_zero": zero_flux,
        "power_absorbed": 1.0 - power_r - power_t,
        "diffraction_orders": np.asarray(obj.G, int),
        "power_R_by_order": r_order,
        "power_T_by_order": t_order,
        "actual_harmonics": int(obj.nG),
        "input_index": n_in,
        "output_index": n_out,
    }
    if with_fields:
        result["fields"] = fields
    arrays = [jones, power_r, power_t, zero_flux]
    if not all(np.all(np.isfinite(x)) for x in arrays):
        raise RuntimeError("Nonfinite RCWA response; change discretization and inspect solver.")
    return result


def solve_unit_cell(
    wavelength_nm: float,
    period_nm: float,
    height_nm: float,
    radius_nm: float,
    pillar_index: complex | float | Iterable[float],
    substrate_index: complex | float | Iterable[float],
    settings: RCWASettings | dict | None = None,
    incidence: str = "substrate",
    *,
    reference_jones: np.ndarray | None = None,
) -> dict[str, Any]:
    """Solve a circular pillar, or radius=0 for an analytic-grid-free reference.

    Jones rows/columns are [Ex,Ey]. ``jones_t_relative`` divides by the
    corresponding co-polarized empty-stack transmission; it is useful for
    phase lookup, but physical propagation must use ``jones_t``.
    """
    settings = _settings(settings)
    if min(wavelength_nm, period_nm, height_nm) <= 0 or not 0 <= radius_nm < period_nm / 2:
        raise ValueError("Positive lengths and 0 <= radius < period/2 are required.")
    pillar_index = _index(pillar_index)
    n_in, n_out = _boundary_indices(_index(substrate_index), incidence)
    started = perf_counter()
    epsilon = None
    if radius_nm > 0:
        xy = (np.arange(settings.grid_size) + 0.5) / settings.grid_size * period_nm
        xx, yy = np.meshgrid(xy - period_nm / 2, xy - period_nm / 2, indexing="ij")
        mask = xx**2 + yy**2 <= radius_nm**2
        epsilon = np.where(mask, pillar_index**2, 1.0 + 0j)
    with _thread_limit():
        obj = _new_problem(wavelength_nm, period_nm, period_nm, height_nm, n_in, n_out, settings, epsilon)
        result = _response(obj, n_in, n_out)
        if reference_jones is None:
            ref = _new_problem(wavelength_nm, period_nm, period_nm, height_nm, n_in, n_out, settings, None)
            reference_jones = _response(ref, n_in, n_out)["jones_t"]
    reference_jones = np.asarray(reference_jones, complex)
    reference_diagonal = np.diag(reference_jones)
    if reference_jones.shape != (2, 2) or np.any(np.abs(reference_diagonal) < 1e-12):
        raise ValueError("Reference must be a nonsingular 2x2 diagonal empty-stack Jones matrix.")
    result.update(
        {
            "reference_jones": reference_jones,
            "jones_t_relative": result["jones_t"] / reference_diagonal[np.newaxis, :],
            "metadata": {
                "method": "direct_periodic_RCWA",
                "solver": "grcwa",
                "solver_version": version("grcwa"),
                "settings": asdict(settings),
                "wavelength_nm": float(wavelength_nm),
                "period_nm": float(period_nm),
                "height_nm": float(height_nm),
                "radius_nm": float(radius_nm),
                "pillar_index": [pillar_index.real, pillar_index.imag],
                "substrate_index": [n_in if incidence == "substrate" else n_out, 0.0],
                "incidence": incidence,
                "phase_reference": "entrance_z=0 to exit_z=height; time exp(-i omega t)",
                "reference_definition": "same boundaries and air spacer of pillar height",
                "geometry_sampling": "binary dielectric on pixel-centred grid",
                "elapsed_seconds": perf_counter() - started,
            },
        }
    )
    return result


def build_response_library(
    wavelength_nm: float,
    candidates: list[dict[str, Any]],
    substrate_index: complex | float | Iterable[float],
    settings: RCWASettings | dict | None = None,
    incidence: str = "substrate",
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Scan each material/height/period candidate over its stated radii.

    Candidate keys: material_id, period_nm, height_nm, radii_nm and either
    pillar_index=[n,k] or n/k. Optional provenance keys are retained.
    """
    settings = _settings(settings)
    outputs = []
    for candidate in candidates:
        index = candidate.get("pillar_index", [candidate.get("n"), candidate.get("k", 0.0)])
        common = {
            "wavelength_nm": wavelength_nm,
            "period_nm": candidate["period_nm"],
            "height_nm": candidate["height_nm"],
            "pillar_index": index,
            "substrate_index": substrate_index,
            "settings": settings,
            "incidence": incidence,
        }
        ref = solve_unit_cell(radius_nm=0.0, **common)["reference_jones"]
        radii = np.asarray(candidate["radii_nm"], float)
        if radii.ndim != 1 or len(radii) < 2:
            raise ValueError("Each candidate requires at least two radii.")
        responses = []
        for number, radius in enumerate(radii, 1):
            responses.append(solve_unit_cell(radius_nm=float(radius), reference_jones=ref, **common))
            if progress:
                progress(f"{candidate['material_id']}, h={candidate['height_nm']} nm: {number}/{len(radii)}")
        output = dict(candidate)
        output.update({
            "wavelength_nm": wavelength_nm,
            "pillar_index": [_index(index).real, _index(index).imag],
            "substrate_index": [_index(substrate_index).real, _index(substrate_index).imag],
            "radii_nm": radii,
            "reference_jones": ref,
            "input_index": responses[0]["input_index"],
            "output_index": responses[0]["output_index"],
            "solver_records": [r["metadata"] for r in responses],
            "actual_harmonics": responses[0]["actual_harmonics"],
        })
        for key in ("jones_t", "jones_t_relative", "power_R", "power_T", "power_T_zero", "power_absorbed"):
            output[key] = np.stack([response[key] for response in responses])
        outputs.append(output)
    return {
        "metadata": {
            "solver": "grcwa",
            "solver_version": version("grcwa"),
            "settings": asdict(settings),
            "incidence": incidence,
            "response_type": "direct_simulation",
            "polarization_order": ["x", "y"],
            "approximation": "infinite periodic isolated geometry; local periodic lookup",
        },
        "candidates": outputs,
    }


def validate_convergence(
    wavelength_nm: float,
    period_nm: float,
    height_nm: float,
    radius_nm: float,
    pillar_index: complex | float | Iterable[float],
    substrate_index: complex | float | Iterable[float],
    settings_sequence: list[RCWASettings | dict] | None = None,
    incidence: str = "substrate",
    phase_tolerance_rad: float = 0.10,
    power_tolerance: float = 0.02,
) -> dict[str, Any]:
    """Separate harmonic and pixel-grid checks at one representative geometry.

    The default sequence fixes the grid while increasing harmonics, then
    fixes harmonics while increasing the grid. A single sampled geometry
    does not certify an entire response library.
    """
    sequence = settings_sequence or [RCWASettings(25, 64), RCWASettings(49, 64), RCWASettings(49, 96)]
    if len(sequence) < 3:
        raise ValueError("At least three settings are needed to check both resolutions.")
    records = []
    previous = None
    for setting in sequence:
        setting = _settings(setting)
        response = solve_unit_cell(wavelength_nm, period_nm, height_nm, radius_nm, pillar_index, substrate_index, setting, incidence)
        record = {
            "settings": asdict(setting),
            "actual_harmonics": response["actual_harmonics"],
            "power_R": response["power_R"].tolist(),
            "power_T": response["power_T"].tolist(),
            "phase_rad": np.angle(np.diag(response["jones_t_relative"])).tolist(),
            "amplitude": np.abs(np.diag(response["jones_t"])).tolist(),
            "max_energy_balance_error": float(np.max(np.abs(response["power_absorbed"]))) if _index(pillar_index).imag == 0 else None,
        }
        if previous is not None:
            phase_delta = np.angle(np.diag(response["jones_t"]) * np.conj(np.diag(previous["jones_t"])))
            record["phase_delta_rad"] = float(np.max(np.abs(phase_delta)))
            record["power_delta"] = float(max(np.max(np.abs(response[k] - previous[k])) for k in ("power_R", "power_T", "power_T_zero")))
            record["passes_increment"] = bool(record["phase_delta_rad"] <= phase_tolerance_rad and record["power_delta"] <= power_tolerance)
        records.append(record)
        previous = response
    independent_axes = {
        "harmonics": any(a["settings"]["grid_size"] == b["settings"]["grid_size"] and a["settings"]["harmonics"] != b["settings"]["harmonics"] for a, b in zip(records, records[1:])),
        "grid": any(a["settings"]["harmonics"] == b["settings"]["harmonics"] and a["settings"]["grid_size"] != b["settings"]["grid_size"] for a, b in zip(records, records[1:])),
    }
    return {
        "method": "direct_RCWA_discretization_comparison",
        "radius_nm": radius_nm,
        "records": records,
        "tested_axes": independent_axes,
        "converged": bool(all(independent_axes.values()) and all(r.get("passes_increment", True) for r in records)),
        "thresholds": {"phase_rad": phase_tolerance_rad, "absolute_power": power_tolerance},
        "scope": "representative single geometry only; library-wide certification is not implied",
    }


def solve_supercell(
    wavelength_nm: float,
    period_nm: float,
    height_nm: float,
    radii_nm: Iterable[float] | np.ndarray,
    pillar_index: complex | float | Iterable[float],
    substrate_index: complex | float | Iterable[float],
    settings: RCWASettings | dict | None = None,
    incidence: str = "substrate",
    with_fields: bool = True,
) -> dict[str, Any]:
    """RCWA for a periodically repeated neighbourhood of different pillars.

    Input radius grid axes are x,y; a 1-D sequence forms an Nx1 supercell.
    Exit-plane per-cell averages contain evanescent orders and diagnose
    neighbouring coupling. They are NOT finite-device focusing efficiencies.
    """
    settings = _settings(settings or RCWASettings(49, 64))
    radii = np.asarray(radii_nm, float)
    if radii.ndim == 1:
        radii = radii[:, np.newaxis]
    if radii.ndim != 2 or not radii.size or np.any(radii <= 0) or np.any(radii >= period_nm / 2):
        raise ValueError("Supercell radii must form a nonempty 1-D or 2-D grid within (0,period/2).")
    if min(wavelength_nm, period_nm, height_nm) <= 0:
        raise ValueError("All lengths must be positive.")
    index = _index(pillar_index)
    n_in, n_out = _boundary_indices(_index(substrate_index), incidence)
    sx, sy = radii.shape
    grid_shape = (settings.grid_size * sx, settings.grid_size * sy)
    px, py = sx * period_nm, sy * period_nm
    x = (np.arange(grid_shape[0]) + 0.5) / grid_shape[0] * px
    y = (np.arange(grid_shape[1]) + 0.5) / grid_shape[1] * py
    xx, yy = np.meshgrid(x, y, indexing="ij")
    mask = np.zeros(grid_shape, bool)
    for ix in range(sx):
        for iy in range(sy):
            mask |= (xx - (ix + 0.5) * period_nm)**2 + (yy - (iy + 0.5) * period_nm)**2 <= radii[ix, iy]**2
    epsilon = np.where(mask, index**2, 1.0 + 0j)
    with _thread_limit():
        obj = _new_problem(wavelength_nm, px, py, height_nm, n_in, n_out, settings, epsilon)
        result = _response(obj, n_in, n_out, with_fields=with_fields, grid_shape=grid_shape)
    result.update({
        "metadata": {
            "method": "direct_periodic_supercell_RCWA",
            "solver": "grcwa",
            "solver_version": version("grcwa"),
            "settings": asdict(settings),
            "radii_nm": radii.tolist(),
            "period_nm": period_nm,
            "height_nm": height_nm,
            "incidence": incidence,
            "scope": "periodically repeated neighbourhood; not a finite full-device electromagnetic simulation",
            "field_normalization": "E divided by incident electric amplitude; grid axes x,y",
            "exit_reference": "z=height, immediately outside pillars",
        },
        "x_nm": x,
        "y_nm": y,
        "epsilon_grid": epsilon,
    })
    if with_fields:
        local_jones = np.empty((sx, sy, 2, 2), complex)
        for column, fields in enumerate(result["fields"]):
            for ix in range(sx):
                for iy in range(sy):
                    region = fields["output_plane"][:2, ix * settings.grid_size:(ix + 1) * settings.grid_size, iy * settings.grid_size:(iy + 1) * settings.grid_size]
                    local_jones[ix, iy, :, column] = region.mean(axis=(1, 2))
        result["local_jones_t_exit_mean"] = local_jones
    return result
