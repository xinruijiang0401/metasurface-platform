# Third-party notices

The original code in this project is licensed under **GNU GPL version 3 or, at your option, any later version (`GPL-3.0-or-later`)**. See `LICENSE`. Third-party software and material data retain their own licenses. This inventory records the release dependencies and provenance; it does not replace their complete license and copyright notices.

## Runtime dependencies

The following versions are pinned in `pyproject.toml`. License entries were checked against the installed distributions' metadata and accompanying license files on 2026-10-13.

| Dependency | Version | Upstream license / notice | Source |
|---|---|---|---|
| NumPy | 2.2.6 | BSD 3-Clause; copyright NumPy Developers. Binary wheels include additional bundled-library notices. | [NumPy](https://numpy.org/) |
| SciPy | 1.15.3 | BSD 3-Clause; copyright Enthought, Inc. and SciPy Developers. Binary wheels include additional bundled-library notices. | [SciPy](https://scipy.org/) |
| Autograd | 1.8.0 | MIT; copyright President and Fellows of Harvard University. | [Autograd](https://github.com/HIPS/autograd) |
| grcwa | 0.1.2 | GNU GPL **version 3 or later**; copyright 2020 Weiliang Jin. The distribution's `LICENSE` explicitly permits version 3 or any later version. | [grcwa](https://github.com/weiliangjinca/grcwa) |
| gdstk | 0.9.61 | Boost Software License 1.0 (`BSL-1.0`). | [gdstk](https://github.com/heitzmann/gdstk) |
| h5py | 3.14.0 | BSD 3-Clause. Its wheel may also contain HDF5 and other separate notices. | [h5py](https://www.h5py.org/) |
| PyYAML | 6.0.2 | MIT. | [PyYAML](https://pyyaml.org/) |
| Matplotlib | 3.10.3 | Matplotlib License Agreement for versions 1.3.0 and later, derived from the PSF license; copyright 2012– Matplotlib Development Team. The distribution also includes font and other notices. | [Matplotlib license](https://matplotlib.org/3.10.3/project/license.html) |
| threadpoolctl | 3.6.0 | BSD 3-Clause. | [threadpoolctl](https://github.com/joblib/threadpoolctl) |

Build dependency: `setuptools 80.9.0` (MIT). Optional test dependency: `pytest 8.4.1` (MIT). Python itself and any separately installed browser/test runtime retain their respective upstream licenses. The project does not change these upstream packages.

The project uses grcwa through its public Python interfaces. Choosing the project's GPL-3.0-or-later license is consistent with the grcwa license; the permissive dependencies keep their original notices. If distributing dependencies or a bundled executable/environment, retain the license files of the exact distributed wheels and all their bundled components. The short table above is not a complete notice bundle for redistributing those binaries.

## Optical-constant database

The packaged `src/metasurface_platform/data/materials_catalog_v2.json` is a sample-specific extraction of the [refractiveindex.info database](https://github.com/polyanskiy/refractiveindex.info-database). Its recorded database license is **CC0 1.0 Universal (`CC0-1.0`)**, separate from the project's software license.

- Upstream repository: `polyanskiy/refractiveindex.info-database`.
- Fixed upstream commit: `c5c2f188e848453def5970e347399d653df2ffc2`.
- Retrieval date recorded in the catalog: `2026-10-13`.
- Each sample retains its original source ID, database page, commit-specific raw URL, source hash, literature reference and DOI links where supplied. The catalog also records its own schema and relevant validity ranges; runtime records include the catalog hash.
- Initial families include SiO₂, Si₃N₄, TiO₂, crystalline and amorphous Si, GaN, Ge, CaF₂, Au and Al, with additional collected families. Inclusion in this data catalog does not mean that every family is supported by the first-release dielectric-pillar design pipeline.

The exact sample paths and citations are authoritative in the catalog. Missing extinction coefficients remain missing in the source data; an explicitly enabled lossless assumption is a simulation decision rather than a modification to the cited measurement.

CC0 applies to the database content distributed under that dedication. The original journal articles, publisher figures, and other external assets have their own terms; retaining bibliographic metadata does not relicense those works. This release does not claim ownership of the underlying experimental measurements.

## References outside the active implementation

DFlat, TORCWA, S4, waveprop, POPPY, Meep, SALib and KLayout were considered as workflow references. They are not bundled or represented as active first-release backends by these notices. Their names do not imply endorsement, numerical validation against them, or implementation of their capabilities.
