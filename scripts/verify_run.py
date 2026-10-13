"""Verify saved evidence without rerunning expensive solvers."""
import json
from pathlib import Path
import sys
import hashlib
import h5py
import numpy as np

root = Path(sys.argv[1]).resolve()
manifest = json.loads((root/"manifest.json").read_text(encoding="utf-8"))
result = json.loads((root/"result.json").read_text(encoding="utf-8"))
for name,expected in manifest["files"].items():
    assert hashlib.sha256((root/name).read_bytes()).hexdigest()==expected,name
checks = json.loads((root/"convergence.json").read_text(encoding="utf-8"))
assert checks["all_used_sizes_passed"]
assert result["validation"]["layout_passed"]
metrics = result["metrics"]
assert 0<=metrics["focusing_efficiency"]<=metrics["transmission_efficiency"]<=1.001
with h5py.File(root/"fields.h5") as f:
    assert f["incident_power_field"].shape==f["output_power_field"].shape
    assert np.iscomplexobj(f["focal_field"][()])
    assert np.isfinite(f["near_field_E_over_Einc"][()]).all()
tolerance = json.loads((root/"tolerance.json").read_text(encoding="utf-8"))
assert len(tolerance["samples"])==tolerance["summary"]["requested_samples"]
print(json.dumps({"design_id":result["id"],"files_verified":len(manifest["files"]),
                  "unit_convergence":True,"layout_roundtrip":True,
                  "design_acceptance":result["design_acceptance"],"evaluated_tolerance_samples":tolerance["summary"]["evaluated_samples"]},indent=2))
