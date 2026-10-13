"""GDSII/OASIS export and round-trip geometry checks for circular pillars."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from typing import Any, Iterable

import numpy as np
from scipy.spatial import cKDTree


@contextmanager
def _native_io_path(path: Path, *, write: bool = False):
    """Give gdstk's C stdio an ASCII path on Windows without changing cwd.

    Python uses Windows wide-character filesystem APIs, but gdstk 0.9.61's
    fopen paths cannot open a Unicode absolute path. An ASCII relative path
    still works when the actual current directory contains Unicode. For a
    Unicode suffix/filename or a different drive, stage the binary file in
    an ASCII-named temporary directory below the current project directory,
    then copy using Python's Unicode-aware file APIs. No process-global cwd
    mutation is needed, and both normal and exceptional exits clean staging.
    """
    path = path.resolve()
    if os.name != "nt":
        yield str(path)
        return
    try:
        relative = os.path.relpath(path, Path.cwd())
        relative.encode("ascii")
    except (ValueError, UnicodeEncodeError):
        with TemporaryDirectory(prefix=".gdstk-io-", dir=Path.cwd()) as temporary:
            staged = Path(temporary) / ("layout" + path.suffix)
            native = os.path.relpath(staged, Path.cwd())
            if not write:
                shutil.copyfile(path, staged)
            yield native
            if write:
                shutil.copyfile(staged, path)
    else:
        yield relative


def check_geometry(
    x_nm: Iterable[float],
    y_nm: Iterable[float],
    radius_nm: Iterable[float],
    *,
    min_feature_nm: float = 80.0,
    min_gap_nm: float = 40.0,
    aperture_diameter_nm: float | None = None,
) -> dict[str, Any]:
    """Check analytic circular geometry before polygon discretization.

    The aperture check applies to full pillar extents, not just centres.
    Rules are assumptions from a reference process, not foundry sign-off.
    """
    x, y, radius = (np.asarray(v, float).ravel() for v in (x_nm, y_nm, radius_nm))
    if not (len(x) == len(y) == len(radius)) or not len(x):
        raise ValueError("x, y and radius must have equal nonzero length.")
    if not all(np.isfinite(a).all() for a in (x, y, radius)) or np.any(radius <= 0):
        raise ValueError("Layout requires finite coordinates and strictly positive radii.")
    if min_feature_nm <= 0 or min_gap_nm < 0:
        raise ValueError("Feature rule must be positive; gap rule must be nonnegative.")
    centres = np.column_stack((x, y))
    tree = cKDTree(centres)
    # Any pair closer than 2*max(radius)+rule can violate the rule.
    pairs = sorted(tree.query_pairs(float(2 * radius.max() + min_gap_nm)))
    violations = []
    minimum_gap = None
    for i, j in pairs:
        gap = float(np.linalg.norm(centres[i] - centres[j]) - radius[i] - radius[j])
        minimum_gap = gap if minimum_gap is None else min(minimum_gap, gap)
        if gap < min_gap_nm - 1e-7:
            violations.append({"indices": [i, j], "gap_nm": gap})
    # Find a guaranteed global minimum surface gap, including pairs outside
    # the violation search radius, with a bound established by a nearest pair.
    if len(radius) > 1:
        _, nearest = tree.query(centres, k=2)
        bound = min(float(np.linalg.norm(centres[i] - centres[j]) - radius[i] - radius[j]) for i, j in enumerate(nearest[:, 1]))
        for i, j in tree.query_pairs(float(max(0, 2 * radius.max() + bound + 1e-6))):
            gap = float(np.linalg.norm(centres[i] - centres[j]) - radius[i] - radius[j])
            bound = min(bound, gap)
        minimum_gap = bound
    small = np.flatnonzero(2 * radius < min_feature_nm - 1e-7).tolist()
    outside = []
    if aperture_diameter_nm is not None:
        outside = np.flatnonzero(np.hypot(x, y) + radius > aperture_diameter_nm / 2 + 1e-7).tolist()
    return {
        "passed": not (violations or small or outside),
        "pillar_count": int(len(radius)),
        "minimum_diameter_nm": float(2 * radius.min()),
        "minimum_gap_nm": minimum_gap,
        "feature_violations": small,
        "gap_violations": violations,
        "aperture_violations": outside,
        "rules": {"min_feature_nm": min_feature_nm, "min_gap_nm": min_gap_nm, "aperture_diameter_nm": aperture_diameter_nm},
        "scope": "circular pillars only; reference-process rule check, not complete foundry DRC",
    }


def _roundtrip_check(path: Path, expected_centres_um, expected_radius_um, layer, datatype, tolerance_nm, rules):
    import gdstk

    with _native_io_path(path) as native_path:
        library = gdstk.read_gds(native_path) if path.suffix == ".gds" else gdstk.read_oas(native_path)
    tops = library.top_level()
    if len(tops) != 1:
        raise RuntimeError("Round-trip layout must contain exactly one top-level device cell.")
    polygons = tops[0].get_polygons()
    if len(polygons) != len(expected_radius_um):
        raise RuntimeError("Round-trip pillar count differs from source layout.")
    centres, radii, widths = [], [], []
    for polygon in polygons:
        if polygon.layer != layer or polygon.datatype != datatype:
            raise RuntimeError("Round-trip layer or datatype differs from requested values.")
        points = np.asarray(polygon.points)
        centre = (points.max(axis=0) + points.min(axis=0)) / 2
        centres.append(centre)
        radii.append(float(np.max(np.linalg.norm(points - centre, axis=1))))
        edges = np.roll(points, -1, axis=0) - points
        edge_length = np.linalg.norm(edges, axis=1)
        edges = edges[edge_length > 1e-15]
        edge_length = edge_length[edge_length > 1e-15]
        normals = np.column_stack((-edges[:, 1], edges[:, 0])) / edge_length[:, None]
        projections = points @ normals.T
        widths.append(float(np.min(np.ptp(projections, axis=0)) * 1000))
    centres = np.asarray(centres)
    radii = np.asarray(radii)
    distances, indices = cKDTree(centres).query(expected_centres_um)
    if len(set(indices.tolist())) != len(indices):
        raise RuntimeError("Round-trip pillar matching is not one-to-one.")
    max_centre_error = float(distances.max() * 1000)
    max_radius_error = float(np.max(np.abs(radii[indices] - expected_radius_um)) * 1000)
    if max_centre_error > tolerance_nm + 1e-6 or max_radius_error > tolerance_nm + 1e-6:
        raise RuntimeError("Round-trip coordinate/radius error exceeds export tolerance.")
    if not np.isclose(library.unit, 1e-6, rtol=1e-10, atol=0) or not np.isclose(library.precision, 1e-9, rtol=1e-10, atol=0):
        raise RuntimeError("Round-trip length unit or precision is incorrect.")
    readback_geometry = check_geometry(centres[:, 0] * 1000, centres[:, 1] * 1000, radii * 1000, **rules)
    # A circumscribed-disc gap check is conservative for inscribed circles.
    # Feature width is checked on actual polygon edges after quantization.
    minimum_width = float(min(widths))
    if minimum_width < rules["min_feature_nm"] - 1e-7 or not readback_geometry["passed"]:
        raise RuntimeError("Exported polygons fail reference geometry rules after quantization; increase design margins.")
    return {
        "passed": True,
        "file": str(path),
        "pillar_count": len(polygons),
        "unit_m": library.unit,
        "precision_m": library.precision,
        "layer": layer,
        "datatype": datatype,
        "max_centre_error_nm": max_centre_error,
        "max_radius_error_nm": max_radius_error,
        "minimum_polygon_width_nm": minimum_width,
        "geometry_after_quantization": readback_geometry,
        "gap_check_method": "conservative enclosing circles of exported polygons",
    }


def export_layout(
    x_nm: Iterable[float],
    y_nm: Iterable[float],
    radius_nm: Iterable[float],
    output_dir: str | Path,
    *,
    name: str = "metalens",
    min_feature_nm: float = 80.0,
    min_gap_nm: float = 40.0,
    aperture_diameter_nm: float | None = None,
    layer: int = 1,
    datatype: int = 0,
    polygon_tolerance_nm: float = 0.25,
) -> dict[str, Any]:
    """Write both GDSII and OASIS using micrometre user units and 1 nm precision.

    Raises on source-rule failures or unsuccessful read-back. Geometry
    approximates circles with polygons and may move by <=1 nm when written.
    """
    import gdstk

    if not 0 <= layer <= 65535 or not 0 <= datatype <= 65535:
        raise ValueError("Layer and datatype must lie between 0 and 65535.")
    if polygon_tolerance_nm <= 0 or not name or Path(name).name != name:
        raise ValueError("Use a positive polygon tolerance and a simple filename stem.")
    x, y, radius = (np.asarray(v, float).ravel() for v in (x_nm, y_nm, radius_nm))
    geometry = check_geometry(x, y, radius, min_feature_nm=min_feature_nm, min_gap_nm=min_gap_nm, aperture_diameter_nm=aperture_diameter_nm)
    if not geometry["passed"]:
        raise ValueError(f"Layout fails reference process geometry rules: {geometry}")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    library = gdstk.Library(unit=1e-6, precision=1e-9)
    top = library.new_cell("METASURFACE")
    cells = {}
    for number, r in enumerate(np.unique(radius)):
        cell = library.new_cell(f"PILLAR_{number:04d}")
        cell.add(gdstk.ellipse((0.0, 0.0), float(r / 1000), tolerance=polygon_tolerance_nm / 1000, layer=layer, datatype=datatype))
        cells[float(r)] = cell
    for px, py, r in zip(x, y, radius):
        top.add(gdstk.Reference(cells[float(r)], origin=(float(px / 1000), float(py / 1000))))
    gds_path = output_dir / f"{name}.gds"
    oas_path = output_dir / f"{name}.oas"
    with _native_io_path(gds_path, write=True) as native_path:
        library.write_gds(native_path)
    with _native_io_path(oas_path, write=True) as native_path:
        library.write_oas(native_path)
    expected_centres = np.column_stack((x, y)) / 1000
    roundtrips = [_roundtrip_check(path, expected_centres, radius / 1000, layer, datatype, 1.5, geometry["rules"]) for path in (gds_path, oas_path)]
    return {
        "gds_path": str(gds_path),
        "oas_path": str(oas_path),
        "geometry": geometry,
        "roundtrip": roundtrips,
        "polygon_tolerance_nm": polygon_tolerance_nm,
        "quantization_nm": 1.0,
        "process_status": "reference process only; fabrication requires qualified process review",
    }
