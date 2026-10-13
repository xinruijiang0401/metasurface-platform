"""Analytic response fixtures check physical conventions across module boundaries."""
import unittest
import numpy as np

from metasurface_platform.pipeline import prepare_config, _map_candidate
from metasurface_platform.beams import intensity
from metasurface_platform.optics import analyze_device, target_lens_phase
from metasurface_platform.tolerance import evaluate_tolerance


class PipelinePhysicsTests(unittest.TestCase):
    def setUp(self):
        # Constant power transmission with a full phase cycle; synthetic,
        # explicitly not treated as evidence about fabricated TiO2 pillars.
        radii = np.linspace(10, 130, 121)
        n_in, power = 1.46, .7
        scalar = np.sqrt(power * n_in) * np.exp(2j * np.pi * (radii - radii.min()) / np.ptp(radii))
        t = np.zeros((len(radii), 2, 2), dtype=complex)
        t[:, 0, 0] = t[:, 1, 1] = scalar
        self.candidate = {"radii_nm": radii, "period_nm": 300, "height_nm": 600,
                          "jones_t": t, "power_T": np.full((len(radii), 2), power),
                          "input_index": n_in, "output_index": 1}

    def test_substrate_jones_mapping_matches_flux_and_circular_polarization(self):
        for hand, sign in (("left", 1), ("right", -1)):
            cfg = prepare_config({"diameter_um": 8, "beam": {"normalization": "aperture_power", "value": .002,
                                  "medium_n": 1.46, "polarization": {"type": hand}}})
            mapped = _map_candidate(cfg, self.candidate)
            Iin, Iout = intensity(mapped["beam"]["field"]), intensity(mapped["output"])
            self.assertAlmostEqual(float(Iout.sum() / Iin.sum()), .7, places=12)
            mask = mapped["aperture"]
            ratio = mapped["output"][1][mask] / mapped["output"][0][mask]
            self.assertLess(np.max(np.abs(ratio - sign * 1j)), 1e-13)

    def test_curved_gaussian_input_phase_is_compensated_in_mapping(self):
        cfg = prepare_config({"diameter_um": 8, "focal_length_um": 100,
                              "beam": {"medium_n": 1.46, "waist_um": 8, "waist_z_um": 250}})
        mapped = _map_candidate(cfg, self.candidate)
        mask = mapped["aperture"]
        incident, output = mapped["beam"]["field"], mapped["output"]
        residual = np.angle(output[0] * np.conj(incident[0]) * np.exp(-1j * mapped["target_phase"]))
        self.assertLess(float(np.max(np.abs(residual[mask]))), .028)
        combined_phase = np.angle(output[0])
        ideal_phase = target_lens_phase(mapped["x"], mapped["y"], cfg["wavelength_m"], cfg["focal_length_m"]) + mapped["phase_offset_rad"]
        focused_residual = np.angle(np.exp(1j * (combined_phase - ideal_phase)))
        self.assertLess(float(np.max(np.abs(focused_residual[mask]))), .028)

    def test_nominal_tolerance_and_device_metrics_use_same_flux(self):
        cfg = prepare_config({"diameter_um": 8, "tolerance": {"samples": 2,
                              "common_diameter_sigma_nm": 0, "correlated_diameter_sigma_nm": 0,
                              "local_diameter_sigma_nm": 0}, "beam": {"medium_n": 1.46}})
        mapped = _map_candidate(cfg, self.candidate)
        analysis = analyze_device(mapped["beam"]["field"], mapped["output"], mapped["x"], mapped["y"],
                                  cfg["wavelength_m"], cfg["focal_length_m"], z_samples=3)
        cfg["roi_radius_um"] = analysis["metrics"]["roi_radius_um"]
        tol = evaluate_tolerance(cfg, self.candidate, mapped["x"], mapped["y"], mapped["aperture"],
                                 mapped["radius_map_nm"], mapped["beam"]["field"])
        self.assertAlmostEqual(tol["nominal"]["transmission_efficiency"], analysis["metrics"]["transmission_efficiency"], places=12)
        self.assertAlmostEqual(tol["nominal"]["focusing_efficiency"], analysis["metrics"]["focusing_efficiency"], places=12)


if __name__ == "__main__":
    unittest.main()
