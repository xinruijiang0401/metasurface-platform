"""Sample-specific public optical constants. Missing absorption is never filled silently."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Iterable

import numpy as np


def _formula(block: dict, wavelength_um: float) -> float:
    c = block["coeff"]
    l2 = wavelength_um ** 2
    kind = block["type"]
    if kind in {"formula 1", "formula 2"}:
        n2 = 1.0 + c[0]
        for i in range(1, len(c), 2):
            resonance = c[i + 1] ** 2 if kind == "formula 1" else c[i + 1]
            n2 += c[i] * l2 / (l2 - resonance)
    elif kind == "formula 4":
        n2 = (c[0] + c[1] * wavelength_um ** c[2] / (l2 - c[3] ** c[4])
              + c[5] * wavelength_um ** c[6] / (l2 - c[7] ** c[8]))
        for i in range(9, len(c), 2):
            n2 += c[i] * wavelength_um ** c[i + 1]
    else:
        raise ValueError(f"Unsupported dispersion formula: {kind}")
    if not math.isfinite(n2) or n2 <= 0:
        raise ValueError(f"Nonphysical refractive index from {kind}")
    return math.sqrt(n2)


def _supports(block: dict, wavelength_um: float) -> bool:
    return block["range"][0] <= wavelength_um <= block["range"][1]


def _interpolate(block: dict, wavelength_um: float, column: int) -> tuple[float, str]:
    values = np.asarray(block["values"], dtype=float)
    order = np.argsort(values[:, 0])
    values = values[order]
    if wavelength_um < values[0, 0] or wavelength_um > values[-1, 0]:
        raise ValueError("Tabulated data cannot be extrapolated")
    if np.any(np.diff(values[:, 0]) <= 0):
        raise ValueError("Tabulated wavelength values must be unique")
    exact = np.flatnonzero(values[:, 0] == wavelength_um)
    method = "source_table_exact" if exact.size else "linear_interpolation_in_wavelength"
    return float(np.interp(wavelength_um, values[:, 0], values[:, column])), method


class MaterialCatalog:
    """A versioned collection of distinct samples; no stitching across samples."""

    def __init__(self, path: str | Path | None = None):
        if path is None:
            packaged = Path(__file__).resolve().parent / "data" / "materials_catalog_v2.json"
            local = Path(__file__).resolve().parents[2] / "data" / "materials_catalog_v2.json"
            parent = Path(__file__).resolve().parents[3] / "data" / "materials" / "dispersion" / "materials_catalog_v2.json"
            path = packaged if packaged.exists() else local if local.exists() else parent
        self.path = Path(path).resolve()
        raw = self.path.read_bytes()
        self.sha256 = hashlib.sha256(raw).hexdigest()
        self.catalog = json.loads(raw.decode("utf-8-sig"))
        self.sources = {item["source_id"]: item for item in self.catalog["sources"]}

    def evaluate(self, source_id: str, wavelength_nm: float, assume_k_zero: bool = False) -> dict:
        if not math.isfinite(wavelength_nm) or wavelength_nm <= 0:
            raise ValueError("Wavelength must be finite and positive")
        if source_id not in self.sources:
            raise ValueError(f"Unknown material sample: {source_id}")
        source = self.sources[source_id]
        wavelength_um = wavelength_nm / 1000
        n_block = next((b for b in source["blocks"] if b["type"] != "tabulated k" and _supports(b, wavelength_um)), None)
        k_block = next((b for b in source["blocks"] if b["type"] in {"tabulated nk", "tabulated k"} and _supports(b, wavelength_um)), None)
        n, k, n_method, k_method = None, None, None, None
        if n_block is not None:
            if n_block["type"].startswith("formula"):
                n, n_method = _formula(n_block, wavelength_um), n_block["type"].replace(" ", "_")
            else:
                n, n_method = _interpolate(n_block, wavelength_um, 1)
        if k_block is not None:
            k, k_method = _interpolate(k_block, wavelength_um, 2 if k_block["type"] == "tabulated nk" else 1)
            if k < 0:
                raise ValueError(f"Passive sample {source_id} has negative k")
        has_k = any(b["type"] in {"tabulated k", "tabulated nk"} for b in source["blocks"])
        status = ("available_nk" if n is not None and k is not None else
                  "available_n_k_out_of_range" if n is not None and has_k else
                  "available_n_k_not_reported" if n is not None else
                  "available_k_n_out_of_range" if k is not None else "out_of_range")
        assumed = bool(n is not None and k is None and assume_k_zero)
        k_used = 0.0 if assumed else k
        return {
            "source_id": source_id, "material_id": source["material_id"],
            "material_family": source.get("material_family", source["chemical_formula"]),
            "chemical_formula": source["chemical_formula"], "sample": source["sample"],
            "wavelength_nm": wavelength_nm, "n": n, "k": k, "k_used": k_used,
            "assumed_k_zero": assumed, "status": status,
            "n_method": n_method, "k_method": k_method,
            "index": [n, k_used] if n is not None and k_used is not None else None,
            "usable_for_simulation": n is not None and k_used is not None,
            "source_range_n_um": [b["range"] for b in source["blocks"] if b["type"] != "tabulated k"],
            "source_range_k_um": [b["range"] for b in source["blocks"] if b["type"] in {"tabulated k", "tabulated nk"}],
            "direction": source.get("direction"), "film_thickness_m": source.get("film_thickness_m"),
            "temperature_K": source.get("temperature_K"), "substrate": source.get("substrate"),
            "source_comments": source.get("source_comments"), "notes": source.get("notes"),
            "retrieved_date": source.get("retrieved_date"),
            "reference": source.get("reference"), "doi_urls": source.get("doi_urls"),
            "database_page": source.get("database_page"), "raw_url": source.get("raw_url"),
            "license": source.get("license"), "source_sha256": source.get("sha256"),
            "catalog_sha256": self.sha256,
            "database_commit": self.catalog.get("database_commit"),
        }

    def samples(self, wavelength_nm: float, families: Iterable[str] | None = None,
                assume_k_zero: bool = False) -> list[dict]:
        allowed = set(families) if families is not None else None
        return [self.evaluate(key, wavelength_nm, assume_k_zero) for key, source in self.sources.items()
                if allowed is None or source.get("material_family", source["chemical_formula"]) in allowed]

    def pick_candidates(self, wavelength_nm: float, families: Iterable[str] = ("TiO2", "Si3N4"),
                        assume_k_zero: bool = False, max_k: float = 0.05) -> list[dict]:
        """Preselection only: final ranking must use numerical unit responses.

        Rutile ordinary/extraordinary indices are anisotropic sample components,
        so they are excluded from this first isotropic pillar solver.
        """
        candidates = self.samples(wavelength_nm, families, assume_k_zero)
        candidates = [item for item in candidates if item["usable_for_simulation"]
                      and item["k_used"] <= max_k and not item.get("direction")]
        return sorted(candidates, key=lambda item: (item["assumed_k_zero"], item["k_used"], -item["n"]))


def complex_index(sample: dict) -> complex:
    if not sample.get("usable_for_simulation"):
        raise ValueError("Missing n or k: explicitly permit assumed_k_zero before simulation")
    return complex(sample["n"], sample["k_used"])
