"""Portable, explicit result serialization and immutable run records."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import numpy as np
import h5py


def jsonable(value):
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, np.ndarray):
        return jsonable(value.tolist())
    if isinstance(value, (complex, np.complexfloating)):
        return [float(value.real), float(value.imag)]
    if isinstance(value, np.generic):
        return jsonable(value.item())
    if isinstance(value, float) and not np.isfinite(value):
        return None
    if isinstance(value, Path):
        return str(value)
    return value


def write_json(path, value):
    Path(path).write_text(json.dumps(jsonable(value), ensure_ascii=False,
                                    indent=2, allow_nan=False), encoding="utf-8")


def digest(value):
    return hashlib.sha256(json.dumps(jsonable(value), sort_keys=True,
                                    ensure_ascii=False).encode()).hexdigest()


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save_library(path, library):
    """HDF5 arrays plus JSON metadata; loading never unpickles objects."""
    with h5py.File(path, "w") as f:
        f.attrs["metadata"] = json.dumps(jsonable(library["metadata"]), ensure_ascii=False)
        for i, candidate in enumerate(library["candidates"]):
            group = f.create_group(str(i))
            info = {}
            for key, value in candidate.items():
                if isinstance(value, np.ndarray):
                    group.create_dataset(key, data=value, compression="gzip")
                else:
                    info[key] = jsonable(value)
            group.attrs["info"] = json.dumps(info, ensure_ascii=False)


def load_library(path):
    with h5py.File(path, "r") as f:
        result = {"metadata": json.loads(f.attrs["metadata"]), "candidates": []}
        for key in sorted(f.keys(), key=int):
            group = f[key]
            candidate = json.loads(group.attrs["info"])
            candidate.update({name: group[name][()] for name in group.keys()})
            result["candidates"].append(candidate)
    return result


def save_fields(path, fields, metadata):
    with h5py.File(path, "w") as f:
        f.attrs["metadata"] = json.dumps(jsonable(metadata), ensure_ascii=False)
        for name, data in fields.items():
            if isinstance(data, np.ndarray):
                ds = f.create_dataset(name, data=data, compression="gzip")
                ds.attrs["complex_representation"] = "native complex" if np.iscomplexobj(data) else "real"
