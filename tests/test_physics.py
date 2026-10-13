"""Numerical oracles and capability-boundary checks, independent of UI plumbing."""
import math
import unittest
import numpy as np

from metasurface_platform.materials import MaterialCatalog, complex_index, _formula
from metasurface_platform.spec import parse_requirement, normalize_spec, validate_spec
from metasurface_platform.beams import make_grid, incident_field, jones_vector, intensity, coherent_sum
from metasurface_platform.optics import angular_spectrum, target_lens_phase, analyze_device


class MaterialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = MaterialCatalog()

    def test_silica_sellmeier_checkpoint_missing_k_is_not_zero(self):
        sample = self.catalog.evaluate("SiO2/Malitson", 532)
        self.assertAlmostEqual(sample["n"], 1.4607063448921334, places=10)
        self.assertIsNone(sample["k"])
        self.assertIsNone(sample["k_used"])
        with self.assertRaises(ValueError):
            complex_index(sample)
        assumed = self.catalog.evaluate("SiO2/Malitson", 532, assume_k_zero=True)
        self.assertIsNone(assumed["k"])
        self.assertTrue(assumed["assumed_k_zero"])
        self.assertEqual(complex_index(assumed).imag, 0)

    def test_out_of_range_is_never_extrapolated(self):
        sample = self.catalog.evaluate("SiO2/Malitson", 200, assume_k_zero=True)
        self.assertIsNone(sample["n"])
        self.assertFalse(sample["usable_for_simulation"])

    def test_sample_nodes_and_interpolation(self):
        source = self.catalog.sources["TiO2/Jolivet-amorphous"]
        block = next(b for b in source["blocks"] if b["type"] == "tabulated nk")
        a, b = block["values"][15:17]
        result = self.catalog.evaluate(source["source_id"], (a[0] + b[0]) * 500)
        self.assertAlmostEqual(result["n"], (a[1] + b[1]) / 2, places=12)
        self.assertAlmostEqual(result["k"], (a[2] + b[2]) / 2, places=12)
        self.assertFalse(result["assumed_k_zero"])

    def test_formula_1_and_2_distinguish_resonance_definition(self):
        c = [0, 1, 0.04]
        n1 = _formula({"type": "formula 1", "coeff": c}, 0.5)
        n2 = _formula({"type": "formula 2", "coeff": c}, 0.5)
        self.assertAlmostEqual(n1, math.sqrt(1 + .25 / (.25 - .04 ** 2)))
        self.assertAlmostEqual(n2, math.sqrt(1 + .25 / (.25 - .04)))

    def test_candidates_preserve_sample_identity(self):
        candidates = self.catalog.pick_candidates(532)
        self.assertTrue(candidates)
        self.assertTrue(all(item["index"] is not None and not item["direction"] for item in candidates))
        self.assertTrue(all(item["source_id"] != "Si3N4/Luke" for item in candidates))


class RequirementTests(unittest.TestCase):
    def test_two_natural_language_entries(self):
        demo = parse_requirement("设计一个在 532 nm 工作的聚焦超表面")
        self.assertEqual(demo["wavelength_nm"], 532)
        self.assertEqual(demo["diameter_um"], 30)
        self.assertIn("diameter_um", demo["defaults_applied"])
        traditional = parse_requirement("把工作波长532nm、焦距10mm、口径2mm的透镜改为超表面")
        self.assertAlmostEqual(traditional["focal_length_um"], 10000)
        self.assertAlmostEqual(traditional["diameter_um"], 2000)
        with self.assertRaisesRegex(ValueError, "preserved"):
            normalize_spec(traditional)

    def test_no_hidden_function_or_units_changes(self):
        with self.assertRaises(ValueError):
            parse_requirement("设计一个偏转器，波长532nm")
        with self.assertRaises(ValueError):
            parse_requirement("聚焦透镜波长532nm焦距100口径30um")
        with self.assertRaises(ValueError):
            parse_requirement("聚焦透镜波长532nm波长633nm")
        with self.assertRaises(ValueError):
            normalize_spec({"wavelength_nm": 532, "wavelength_m": 633e-9})

    def test_tilt_is_preview_only_until_angle_library_exists(self):
        spec = {"beam": {"theta_deg": 2}}
        validate_spec(spec, for_run=False)
        with self.assertRaisesRegex(ValueError, "normal-incidence"):
            normalize_spec(spec)


class BeamTests(unittest.TestCase):
    def setUp(self):
        self.x, self.y = make_grid(40e-6, .25e-6, 2)
        X, Y = np.meshgrid(self.x, self.y)
        self.mask = X ** 2 + Y ** 2 <= (20e-6) ** 2

    def test_aperture_power_and_jones_normalization(self):
        for pol in ({"type": "linear", "angle_deg": 31}, "left", "right", {"type": "elliptical", "ellipticity_deg": -13}):
            self.assertAlmostEqual(float(np.sum(np.abs(jones_vector(pol)) ** 2)), 1)
            beam = incident_field(self.x, self.y, 532e-9,
                                  {"type": "gaussian", "waist_um": 8, "normalization": "aperture_power", "value": .002, "polarization": pol}, self.mask)
            self.assertAlmostEqual(beam["power_w"], .002, places=14)
            self.assertTrue(np.all(beam["field"][:, ~self.mask] == 0))

    def test_gaussian_total_power_and_medium_rayleigh_range(self):
        beam = incident_field(self.x, self.y, 532e-9,
                              {"type": "gaussian", "waist_um": 5, "medium_n": 1.46, "normalization": "beam_power", "value": .003})
        self.assertAlmostEqual(beam["power_w"], .003, places=12)
        self.assertAlmostEqual(beam["metadata"]["rayleigh_range_m"], math.pi * 1.46 * (5e-6) ** 2 / 532e-9)

    def test_plane_total_power_and_coherence_are_explicit(self):
        with self.assertRaisesRegex(ValueError, "infinite plane"):
            incident_field(self.x, self.y, 532e-9, {"normalization": "beam_power"}, self.mask)
        field = np.ones((2, 4, 4), dtype=complex)
        with self.assertRaises(ValueError):
            coherent_sum([field, field])
        summed = coherent_sum([field, field], [0, math.pi], same_wavelength_and_reference=True)
        self.assertLess(np.max(np.abs(summed)), 1e-14)


class PropagationTests(unittest.TestCase):
    def test_fourier_mode_phase_is_exact_and_power_conserved(self):
        n, dx, wl, z = 128, .2e-6, 532e-9, 10e-6
        x = np.arange(n) * dx
        fx = 3 / (n * dx)
        field = np.exp(2j * math.pi * fx * x)[None, :] * np.ones((n, 1))
        propagated = angular_spectrum(field, dx, wl, z, bandlimit=False)
        phase = np.exp(2j * math.pi * z * math.sqrt(1 / wl ** 2 - fx ** 2))
        self.assertLess(float(np.max(np.abs(propagated - phase * field))), 3e-13)
        self.assertAlmostEqual(float(np.sum(np.abs(propagated) ** 2) / np.sum(np.abs(field) ** 2)), 1, places=13)

    def test_gaussian_propagation_grid_convergence_against_analytic_width(self):
        wl, w0, z = 532e-9, 5e-6, 100e-6
        expected_width = w0 * math.sqrt(1 + (z * wl / (math.pi * w0 ** 2)) ** 2)
        widths = []
        for dx in (.5e-6, .25e-6):
            x, y = make_grid(60e-6, dx, 2)
            X, Y = np.meshgrid(x, y)
            field = np.exp(-(X ** 2 + Y ** 2) / w0 ** 2)
            I = intensity(angular_spectrum(field, dx, wl, z))
            width = 2 * math.sqrt(float(np.sum(I * X ** 2) / np.sum(I)))
            widths.append(width)
            # Exact angular spectrum differs slightly from paraxial Gaussian law.
            self.assertLess(abs(width / expected_width - 1), .003)
        self.assertLess(abs(widths[0] / widths[1] - 1), 1e-5)

    def test_ideal_lens_focus_metrics_and_absolute_power_scale(self):
        x, y = make_grid(30e-6, .2e-6, 3)
        X, Y = np.meshgrid(x, y)
        mask = X ** 2 + Y ** 2 <= (15e-6) ** 2
        inc = incident_field(x, y, 532e-9, {"normalization": "aperture_power", "value": .002}, mask)["field"]
        ideal = inc * np.exp(1j * target_lens_phase(x, y, 532e-9, 100e-6))[None]
        result = analyze_device(inc, ideal * math.sqrt(.7), x, y, 532e-9, 100e-6,
                                ideal_transmitted=ideal, z_samples=3)
        metrics = result["metrics"]
        self.assertAlmostEqual(metrics["transmission_efficiency"], .7, places=12)
        self.assertAlmostEqual(metrics["strehl"], 1, places=12)
        self.assertGreater(metrics["encircled_energy_roi_fraction"], .75)
        self.assertLess(metrics["encircled_energy_roi_fraction"], .93)
        self.assertLess(abs(metrics["peak_x_um"]), .21)
        self.assertLess(abs(metrics["peak_y_um"]), .21)
        self.assertAlmostEqual(metrics["fwhm_x_um"], 1.028 * 532e-9 * 100e-6 / 30e-6 * 1e6, delta=.08)


if __name__ == "__main__":
    unittest.main()
