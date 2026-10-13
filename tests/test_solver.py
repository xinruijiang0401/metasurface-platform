"""Independent Fresnel limits, flux conservation and layout read-back tests."""

from pathlib import Path

import numpy as np
import pytest

from metasurface_platform.layout import check_geometry, export_layout
from metasurface_platform.solver import RCWASettings, solve_supercell, solve_unit_cell, validate_convergence


def test_empty_stack_matches_fresnel_amplitude_power_and_reference_phase():
    wavelength, height, substrate = 532.0, 610.0, 1.46
    response = solve_unit_cell(wavelength, 300, height, 0, 2.4, substrate, RCWASettings(9, 32))
    electric_t = 2 * substrate / (substrate + 1) * np.exp(2j * np.pi * height / wavelength)
    fresnel_r = ((substrate - 1) / (substrate + 1)) ** 2
    np.testing.assert_allclose(response["jones_t"], np.eye(2) * electric_t, atol=1e-10)
    np.testing.assert_allclose(response["power_R"], fresnel_r, atol=1e-10)
    np.testing.assert_allclose(response["power_T"], 1 - fresnel_r, atol=1e-10)
    np.testing.assert_allclose(response["jones_t_relative"], np.eye(2), atol=1e-10)


def test_lossless_pillar_conserves_flux_and_preserves_circle_polarization_symmetry():
    response = solve_unit_cell(532, 300, 600, 90, 2.4, 1.46, RCWASettings(25, 64))
    np.testing.assert_allclose(response["power_R"] + response["power_T"], 1, atol=2e-8)
    np.testing.assert_allclose(response["jones_t"][0, 0], response["jones_t"][1, 1], atol=1e-8)
    np.testing.assert_allclose([response["jones_t"][0, 1], response["jones_t"][1, 0]], 0, atol=1e-8)
    electric_flux = np.abs(np.diag(response["jones_t"])) ** 2 / 1.46
    np.testing.assert_allclose(electric_flux, response["power_T_zero"], atol=1e-8)


def test_passive_lossy_pillar_absorbs_positive_power():
    response = solve_unit_cell(532, 300, 600, 90, 2.4 + 0.08j, 1.46, RCWASettings(25, 64))
    assert np.all(response["power_absorbed"] > 0)
    assert np.all(response["power_T"] >= 0)
    assert np.all(response["power_R"] >= 0)


def test_air_incidence_has_correct_power_normalization():
    response = solve_unit_cell(532, 300, 600, 0, 2.4, 1.46, RCWASettings(9, 32), "air")
    np.testing.assert_allclose(response["power_T"], 1.46 * np.abs(np.diag(response["jones_t"])) ** 2, atol=1e-9)


def test_supercell_uniform_neighbourhood_agrees_with_periodic_unit_limit():
    # Every cell has the same radius. Its exact zero-order response must
    # approach the unit-cell answer; finite truncation can differ. A loose
    # discretization tolerance checks normalization and field extraction.
    unit = solve_unit_cell(532, 300, 600, 90, 2.4, 1.46, RCWASettings(25, 64))
    supercell = solve_supercell(532, 300, 600, [90, 90, 90], 2.4, 1.46, RCWASettings(81, 64))
    np.testing.assert_allclose(supercell["power_T"], unit["power_T"], atol=0.04)
    np.testing.assert_allclose(supercell["power_T"] + supercell["power_R"], 1, atol=2e-7)
    assert supercell["fields"][0]["output_plane"].shape == (3, 192, 64)
    assert supercell["local_jones_t_exit_mean"].shape == (3, 1, 2, 2)


def test_convergence_report_checks_both_harmonics_and_grid():
    report = validate_convergence(532, 300, 600, 90, 2.4, 1.46, [RCWASettings(9, 32), RCWASettings(25, 32), RCWASettings(25, 48)])
    assert report["tested_axes"] == {"harmonics": True, "grid": True}
    assert len(report["records"]) == 3
    assert report["converged"] == all(row.get("passes_increment", True) for row in report["records"])


def test_layout_roundtrip_preserves_count_coordinates_units_layers(tmp_path: Path):
    result = export_layout([-300, 0, 300], [0, 0, 0], [60, 85, 100], tmp_path, aperture_diameter_nm=1100, layer=7, datatype=2)
    assert result["geometry"]["passed"]
    assert result["geometry"]["minimum_gap_nm"] == 115
    for readback in result["roundtrip"]:
        assert readback["passed"]
        assert readback["pillar_count"] == 3
        assert readback["layer"] == 7 and readback["datatype"] == 2
        assert Path(readback["file"]).is_file()


def test_reference_process_rules_reject_undersized_gap_and_pillar_extent():
    result = check_geometry([0, 100], [0, 0], [40, 40], min_gap_nm=40, aperture_diameter_nm=250)
    assert not result["passed"]
    assert result["gap_violations"][0]["gap_nm"] == 20
    assert result["aperture_violations"] == [1]


def test_layout_supports_unicode_output_directory_and_filename(tmp_path: Path):
    working_directory = Path.cwd()
    result = export_layout(
        [-300, 0, 300], [0, 0, 0], [60, 85, 100],
        tmp_path / "中文加工输出", name="金属透镜",
        aperture_diameter_nm=1100, layer=7, datatype=2,
    )
    assert Path.cwd() == working_directory
    for readback in result["roundtrip"]:
        assert readback["passed"]
        assert readback["pillar_count"] == 3
        assert readback["layer"] == 7 and readback["datatype"] == 2
        assert Path(readback["file"]).is_file()
    assert not list(working_directory.glob(".gdstk-io-*"))


def test_invalid_geometry_and_absorbing_boundary_are_explicit_errors():
    with pytest.raises(ValueError):
        solve_unit_cell(532, 300, 600, 150, 2.4, 1.46)
    with pytest.raises(ValueError):
        solve_unit_cell(532, 300, 600, 90, 2.4, 1.46 + 0.01j)
