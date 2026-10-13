"""Numerical checks for arbitrary-plane scalar detector propagation."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np
from scipy.interpolate import RegularGridInterpolator

from metasurface_platform.detector import measure_plane, measure_job, MAX_KERNEL_PAIRS
from metasurface_platform.optics import angular_spectrum


class DetectorTests(unittest.TestCase):
    def make_run(self, *, n=64, dx_um=.2, radius_um=5, beam_waist_um=2.5, wavelength_nm=532):
        temp = tempfile.TemporaryDirectory()
        run = Path(temp.name)
        axis = (np.arange(n) - n // 2) * dx_um * 1e-6
        X, Y = np.meshgrid(axis, axis)
        aperture = X * X + Y * Y <= (radius_um * 1e-6) ** 2
        amp = np.exp(-(X * X + Y * Y) / (beam_waist_um * 1e-6) ** 2) * aperture
        field = np.zeros((2, n, n), complex)
        field[0] = amp * np.exp(1j * .2 * X / (wavelength_nm * 1e-9))
        field[1] = .2j * amp
        metadata = {"config": {"wavelength_nm": wavelength_nm,
                               "beam": {"normalization": "normalized", "type": "gaussian"}},
                    "intensity_units": "reference power density"}
        with h5py.File(run / "fields.h5", "w") as f:
            f["x_m"] = axis
            f["y_m"] = axis
            f["output_power_field"] = field
            f["aperture_mask"] = aperture
            f.attrs["metadata"] = json.dumps(metadata)
        (run / "config.json").write_text(json.dumps(metadata["config"]), encoding="utf-8")
        (run / "result.json").write_text(json.dumps({"material_selection": {"selected": {"source_id": "test/sample"}}}), encoding="utf-8")
        return temp, run, axis, field, aperture

    def test_horizontal_plane_matches_independent_angular_spectrum(self):
        temp, run, axis, field, _ = self.make_run()
        self.addCleanup(temp.cleanup)
        z = 8e-6
        request = {"center_x_um": 0, "center_y_um": 0, "center_z_um": z * 1e6,
                   "normal_theta_deg": 0, "normal_phi_deg": 0,
                   "width_um": 10, "height_um": 10, "roll_deg": 0,
                   "resolution": [31, 31]}
        request["resolution"] = 31  # agreed UI form: scalar square grid
        result = measure_plane(run, request)
        self.assertEqual(result["status"], "computed")
        self.assertLessEqual(result["metadata"]["work"]["kernel_source_point_pairs"], MAX_KERNEL_PAIRS)
        field_rs = np.asarray(result["field_real"]) + 1j * np.asarray(result["field_imag"])
        propagated = angular_spectrum(field, axis[1] - axis[0], 532e-9, z, bandlimit=False)
        interp = RegularGridInterpolator((axis, axis), propagated[0], bounds_error=False, fill_value=0)
        u, v = np.meshgrid(np.asarray(result["x_um"]) * 1e-6, np.asarray(result["y_um"]) * 1e-6)
        expected = interp(np.column_stack((v.ravel(), u.ravel()))).reshape(u.shape)
        measured = field_rs[0]
        relative = np.linalg.norm(measured - expected) / np.linalg.norm(expected)
        self.assertLess(relative, .12)
        self.assertTrue(np.allclose(result["intensity"], np.sum(np.abs(field_rs) ** 2, axis=0)))
        self.assertEqual(result["metadata"]["source_material_sample"], "test/sample")
        self.assertFalse(result["metadata"]["absolute_scale_is_measured"])

    def test_z_energy_trend_and_rotation_basis(self):
        temp, run, *_ = self.make_run()
        self.addCleanup(temp.cleanup)
        base = {"center_x_um": 0, "center_y_um": 0, "normal_theta_deg": 0,
                "width_um": 20, "height_um": 20, "resolution": [35, 35]}
        near = measure_plane(run, {**base, "center_z_um": 10})
        far = measure_plane(run, {**base, "center_z_um": 30})
        self.assertGreater(float(np.max(near["intensity"])), float(np.max(far["intensity"])))
        roll = measure_plane(run, {**base, "center_z_um": 10, "roll_deg": 37})
        u = np.asarray(roll["metadata"]["geometry"]["basis_u"])
        v = np.asarray(roll["metadata"]["geometry"]["basis_v"])
        n = np.asarray(roll["metadata"]["geometry"]["normal"])
        self.assertAlmostEqual(np.dot(u, v), 0, places=13)
        self.assertAlmostEqual(np.dot(u, n), 0, places=13)
        self.assertTrue(np.allclose(np.cross(u, v), n, atol=1e-13))
        self.assertAlmostEqual(u[0], np.cos(np.deg2rad(37)), places=13)
        self.assertAlmostEqual(u[1], np.sin(np.deg2rad(37)), places=13)

    def test_tilted_plane_geometry_and_backplane_rejection(self):
        temp, run, *_ = self.make_run()
        self.addCleanup(temp.cleanup)
        tilted = measure_plane(run, {"center_um": [1, -2, 15], "normal_theta_deg": 20,
                                     "normal_phi_deg": 90, "size_um": [12, 8],
                                     "rotation_deg": 15, "resolution": [25, 17]})
        self.assertEqual(tilted["intensity"].shape, (17, 25))
        self.assertTrue(np.allclose(tilted["metadata"]["geometry"]["normal"],
                                    [0.0, np.sin(np.deg2rad(20)), np.cos(np.deg2rad(20))]))
        with self.assertRaisesRegex(ValueError, "downstream"):
            measure_plane(run, {"center_um": [0, 0, .001], "normal_theta_deg": 100,
                                "width_um": 20, "height_um": 20, "resolution": [8, 8]})

    def test_adaptive_cell_average_respects_kernel_cap_and_reports_sampling(self):
        temp, run, *_ = self.make_run(n=300, dx_um=.2, radius_um=18, beam_waist_um=8)
        self.addCleanup(temp.cleanup)
        result = measure_plane(run, {"center_um": [0, 0, 25], "normal_theta_deg": 0,
                                     "width_um": 30, "height_um": 30,
                                     "resolution": [128, 128]})
        grid = result["metadata"]["source_grid"]
        self.assertGreater(grid["integration_stride"], 1)
        self.assertIn("area-averaged", grid["coarse_complex_field"])
        self.assertLessEqual(result["metadata"]["work"]["kernel_source_point_pairs"], MAX_KERNEL_PAIRS)
        self.assertEqual(np.asarray(result["intensity"]).shape, (128, 128))
        self.assertIn(result["metadata"]["convergence_check"]["status"], {"evaluated", "not_evaluated_work_limit"})

    def test_missing_fields_and_safe_job_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(measure_plane(tmp, {})["status"], "unavailable")
            self.assertEqual(measure_job(tmp, "20261013-205733-73b840", {})["status"], "unavailable")
            with self.assertRaisesRegex(ValueError, "job_id"):
                measure_job(tmp, "..\\outside", {})
            with self.assertRaisesRegex(ValueError, "persistent run ID"):
                measure_job(tmp, "job-ephemeral-123", {})
            run_id = "20261013-205733-73b840"
            run = Path(tmp) / "runs" / run_id
            run.mkdir(parents=True)
            # A persistent ID is routed to the result directory, while the
            # absent field dataset gets a useful explicit unavailable status.
            self.assertEqual(measure_job(tmp, run_id, {})["status"], "unavailable")

    def test_persistent_run_adapter_returns_json_safe_detector_arrays(self):
        temp, source, *_ = self.make_run(n=32, dx_um=.3, radius_um=4, beam_waist_um=2)
        self.addCleanup(temp.cleanup)
        with tempfile.TemporaryDirectory() as root:
            run_id = "20261013-205733-73b840"
            destination = Path(root) / "runs" / run_id
            destination.mkdir(parents=True)
            for name in ("fields.h5", "config.json", "result.json"):
                shutil.copy2(source / name, destination / name)
            result = measure_job(root, run_id, {"center_z_um": 8, "width_um": 8,
                                                "height_um": 8, "resolution": 12})
            self.assertEqual(result["status"], "computed")
            self.assertEqual(result["intensity"].shape, (12, 12))
            self.assertEqual(result["field_real"].shape, (2, 12, 12))
            self.assertIn("absolute_scale_is_measured", result["metadata"])

    def test_resolution_and_detector_extent_are_bounded(self):
        temp, run, *_ = self.make_run()
        self.addCleanup(temp.cleanup)
        with self.assertRaisesRegex(ValueError, "no larger"):
            measure_plane(run, {"center_z_um": 10, "width_um": 101, "height_um": 20})
        with self.assertRaisesRegex(ValueError, "resolution"):
            measure_plane(run, {"center_z_um": 10, "width_um": 20, "height_um": 20,
                                "resolution": [129, 8]})
        with self.assertRaisesRegex(ValueError, "resolution"):
            measure_plane(run, {"center_z_um": 10, "width_um": 20, "height_um": 20,
                                "resolution": True})


if __name__ == "__main__":
    unittest.main()
