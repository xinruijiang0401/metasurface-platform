"""Synthetic response fixtures verify statistics and interpolation boundaries.

These fixtures exercise the tolerance algorithm; their optical constants are
not presented as an RCWA calculation or as a material/fabrication prediction.
"""
import unittest
import numpy as np

from metasurface_platform.beams import make_grid, incident_field
from metasurface_platform.optics import target_lens_phase
from metasurface_platform.tolerance import evaluate_tolerance, _interpolate_jones


class ToleranceTests(unittest.TestCase):
    def setUp(self):
        self.x, self.y = make_grid(8e-6, .25e-6, 3)
        X, Y = np.meshgrid(self.x, self.y)
        self.mask = X ** 2 + Y ** 2 <= (4e-6) ** 2
        self.config = {"wavelength_nm": 532, "focal_length_um": 50, "diameter_um": 8,
                       "seed": 42, "tolerance": {"samples": 12, "correlation_length_um": 1}}
        radii = np.linspace(10, 190, 181)
        amplitudes = .8 - .001 * (radii - 100)
        response = amplitudes * np.exp(1j * .03 * (radii - 100))
        jones = np.zeros((len(radii), 2, 2), dtype=complex)
        jones[:, 0, 0] = jones[:, 1, 1] = response
        self.candidate = {"radii_nm": radii, "jones_t": jones, "input_index": 1,
                          "output_index": 1, "height_nm": 600, "period_nm": 250,
                          "material_id": "test_synthetic_response"}
        phase = target_lens_phase(self.x, self.y, 532e-9, 50e-6)
        self.radius_map = 100 + phase / .03
        self.inc = incident_field(self.x, self.y, 532e-9,
                                  {"normalization": "aperture_power", "value": .001}, self.mask)["field"]

    def evaluate(self, config=None, **kwargs):
        return evaluate_tolerance(config or self.config, self.candidate, self.x, self.y,
                                  self.mask, self.radius_map, self.inc, **kwargs)

    def test_fixed_seed_and_finite_aperture_spatial_sigmas(self):
        a, b = self.evaluate(), self.evaluate()
        self.assertEqual(a, b)
        self.assertEqual(a["summary"]["evaluated_samples"], 12)
        self.assertEqual(a["summary"]["height_effect_status"], "not_evaluated")
        self.assertTrue(all(s["height_bias_nm"] == 0 for s in a["samples"]))
        for corr, local in a["empirical_spatial_std_nm"]["per_sample"]:
            self.assertAlmostEqual(corr, 2, places=12)
            self.assertAlmostEqual(local, 2, delta=.2)
        self.assertGreater(a["summary"]["mean_focusing_efficiency"], .2)
        self.assertLess(a["summary"]["mean_focusing_efficiency"], 1)
        self.assertEqual(len(a["sample_covariance"]["matrix"]), 2)

    def test_stronger_local_phase_errors_reduce_mean_focus(self):
        low = self.evaluate({**self.config, "tolerance": {"samples": 30, "common_diameter_sigma_nm": 0,
                            "correlated_diameter_sigma_nm": 0, "local_diameter_sigma_nm": 0, "correlation_length_um": 1}})
        high = self.evaluate({**self.config, "tolerance": {"samples": 30, "common_diameter_sigma_nm": 0,
                             "correlated_diameter_sigma_nm": 0, "local_diameter_sigma_nm": 15, "correlation_length_um": 1}})
        self.assertLess(high["summary"]["mean_focusing_efficiency"], low["summary"]["mean_focusing_efficiency"])
        self.assertGreater(high["summary"]["evaluated_samples"], 20)

    def test_out_of_range_realizations_rejected_without_clipping(self):
        boundary_map = np.full(self.mask.shape, self.candidate["radii_nm"][0])
        result = evaluate_tolerance(self.config, self.candidate, self.x, self.y,
                                    self.mask, boundary_map, self.inc)
        self.assertEqual(result["summary"]["evaluated_samples"], 0)
        self.assertEqual(result["rejected_fraction"], 1)
        self.assertIsNone(result["summary"]["conditional_pass_fraction"])
        self.assertTrue(all(s["focusing_efficiency"] is None and s["reason"] == "radius_outside_response_library" for s in result["samples"]))
        with self.assertRaisesRegex(ValueError, "outside"):
            _interpolate_jones(self.candidate["radii_nm"], self.candidate["jones_t"], np.array([191]))

    def test_actual_height_libraries_control_height_trend(self):
        minus = {**self.candidate, "height_nm": 584, "jones_t": self.candidate["jones_t"] * 1.04}
        plus = {**self.candidate, "height_nm": 616, "jones_t": self.candidate["jones_t"] * .96}
        result = self.evaluate(height_minus=minus, height_plus=plus)
        item = next(s for s in result["sensitivity"] if s["parameter"] == "height")
        self.assertGreater(item["minus_focusing_efficiency"], result["nominal"]["focusing_efficiency"])
        self.assertLess(item["plus_focusing_efficiency"], result["nominal"]["focusing_efficiency"])
        self.assertTrue(all(abs(s["height_bias_nm"]) <= 16 for s in result["samples"] if s["status"] == "evaluated"))
        with self.assertRaisesRegex(ValueError, "both"):
            self.evaluate(height_minus=minus)
        wrong = {**plus, "radii_nm": plus["radii_nm"] + 1}
        with self.assertRaisesRegex(ValueError, "identical"):
            self.evaluate(height_minus=minus, height_plus=wrong)

    def test_convex_height_interpolation_preserves_near_unity_passivity(self):
        # A pure phase slope around a near-unity response makes a linear
        # derivative approximation gain power. Passive endpoint interpolation
        # has an analytic convex result and does not need amplitude clipping.
        response = .995 * np.exp(1j * .03 * (self.candidate["radii_nm"] - 100))
        jones = np.zeros_like(self.candidate["jones_t"])
        jones[:, 0, 0] = jones[:, 1, 1] = response
        candidate = {**self.candidate, "jones_t": jones}
        minus = {**candidate, "height_nm": 576, "jones_t": jones * np.exp(-.6j)}
        plus = {**candidate, "height_nm": 624, "jones_t": jones * np.exp(.6j)}
        cfg = {**self.config, "tolerance": {"samples": 12, "height_sigma_nm": 8,
                    "common_diameter_sigma_nm": 0, "correlated_diameter_sigma_nm": 0,
                    "local_diameter_sigma_nm": 0, "correlation_length_um": 1}}
        result = evaluate_tolerance(cfg, candidate, self.x, self.y, self.mask,
                                    np.full(self.mask.shape, 100), self.inc,
                                    height_minus=minus, height_plus=plus)
        self.assertEqual(result["summary"]["evaluated_samples"], 12)
        for sample in result["samples"]:
            h = sample["height_bias_nm"]
            weight = abs(h) / 24
            endpoint = np.exp(.6j if h >= 0 else -.6j)
            analytic_T = .995 ** 2 * abs((1 - weight) + weight * endpoint) ** 2
            self.assertAlmostEqual(sample["transmission_efficiency"], analytic_T, places=12)
            self.assertLessEqual(sample["transmission_efficiency"], 1)
        self.assertEqual(result["assumptions"]["height_response_method"], "evaluated_piecewise_complex_response_interpolation")

    def test_height_outside_actual_library_is_rejected_without_extrapolation(self):
        minus = {**self.candidate, "height_nm": 576}
        plus = {**self.candidate, "height_nm": 624}
        cfg = {**self.config, "tolerance": {"samples": 12, "height_sigma_nm": 100,
                     "common_diameter_sigma_nm": 0, "correlated_diameter_sigma_nm": 0,
                     "local_diameter_sigma_nm": 0, "correlation_length_um": 1}}
        result = self.evaluate(cfg, height_minus=minus, height_plus=plus)
        rejected = [s for s in result["samples"] if s["status"] == "rejected"]
        self.assertTrue(rejected)
        self.assertTrue(all(s["reason"] == "height_outside_response_library_interval" for s in rejected))
        self.assertTrue(all(abs(s["height_bias_nm"]) > 24 for s in rejected))


if __name__ == "__main__":
    unittest.main()
