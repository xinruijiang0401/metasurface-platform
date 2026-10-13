import json
import h5py
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.request import urlopen,Request
from urllib.error import HTTPError
import pytest
import numpy as np

from metasurface_platform.pipeline import prepare_config
from metasurface_platform.server import Handler,Workbench
from metasurface_platform.cli import compare_measurements
from metasurface_platform.storage import sha256_file


def test_seed_precedence_and_no_silent_truncation():
    assert prepare_config({"seed":123})["seed"]==123
    assert prepare_config({"seed":123,"simulation":{"seed":8}})["seed"]==8
    with pytest.raises(ValueError,match="integer"):
        prepare_config({"seed":1.5})


def test_local_http_api_units_and_path_resolution(tmp_path):
    service = ThreadingHTTPServer(("127.0.0.1",0),Handler)
    service.workbench = Workbench(tmp_path)
    thread = Thread(target=service.serve_forever,daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{service.server_port}"
    try:
        with urlopen(base+"/api/status") as response:
            assert json.load(response)["default_config"]["wavelength_nm"]==532
        payload = json.dumps({"text":"把532nm焦距10mm口径2mm的透镜改为超表面"}).encode()
        with urlopen(Request(base+"/api/parse",data=payload,headers={"Content-Type":"application/json"})) as response:
            assert json.load(response)["config"]["diameter_um"]==pytest.approx(2000)
        with pytest.raises(HTTPError) as error:
            urlopen(base+"/runs/../../pyproject.toml")
        assert error.value.code==404
    finally:
        service.shutdown()
        service.server_close()
        service.workbench.executor.shutdown()


def test_agent_review_api_uses_structured_config_and_explicit_deterministic_status(tmp_path):
    service = ThreadingHTTPServer(("127.0.0.1",0),Handler)
    service.workbench = Workbench(tmp_path)
    thread = Thread(target=service.serve_forever,daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{service.server_port}"
    config = {"wavelength_nm":532,"diameter_um":30,"focal_length_um":100,
              "assume_k_zero":True,"beam":{"type":"gaussian"}}
    try:
        with urlopen(base+"/api/agent/status") as response:
            status = json.load(response)
        assert status["provider"] == "deterministic_rule_parser"
        assert status["configured"] is False
        assert status["checked"] is False
        assert status["reachable"] is None
        payload = json.dumps({"config":config}).encode()
        with urlopen(Request(base+"/api/agent/review",data=payload,headers={"Content-Type":"application/json"})) as response:
            review = json.load(response)
        assert review["provider"] == "deterministic_fallback"
        assert review["fallback_reason"] == "ollama_not_configured"
        assert review["solver_source"]
        assert any("k=0" in warning for warning in review["warnings"])
        assert "config" not in review
        with pytest.raises(HTTPError) as error:
            payload = json.dumps({"config":{"wavelength_nm":800,"diameter_um":30,"focal_length_um":100}}).encode()
            urlopen(Request(base+"/api/agent/review",data=payload,headers={"Content-Type":"application/json"}))
        assert error.value.code == 400
    finally:
        service.shutdown()
        service.server_close()
        service.workbench.executor.shutdown()


def test_detector_api_uses_saved_complex_fields_and_persistent_run_id(tmp_path):
    run_id = "20261013-205733-73b840"
    run = tmp_path/"runs"/run_id
    run.mkdir(parents=True)
    axis = (np.arange(16)-8)*.25e-6
    X,Y = np.meshgrid(axis,axis)
    aperture = X*X+Y*Y <= (1.5e-6)**2
    field = np.zeros((2,16,16),dtype=complex)
    field[0] = np.exp(-(X*X+Y*Y)/(1.0e-6)**2)*aperture
    with h5py.File(run/"fields.h5","w") as handle:
        handle["x_m"] = axis
        handle["y_m"] = axis
        handle["aperture_mask"] = aperture
        handle["output_power_field"] = field
        handle.attrs["metadata"] = json.dumps({"config":{"wavelength_nm":532,
                                                            "beam":{"normalization":"normalized"}}})
    (run/"config.json").write_text(json.dumps({"wavelength_nm":532}),encoding="utf-8")

    service = ThreadingHTTPServer(("127.0.0.1",0),Handler)
    service.workbench = Workbench(tmp_path)
    thread = Thread(target=service.serve_forever,daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{service.server_port}"
    try:
        payload = json.dumps({"job_id":run_id,"config":{"center_z_um":5,"width_um":5,
                            "height_um":5,"resolution":12}}).encode()
        with urlopen(Request(base+"/api/probe",data=payload,headers={"Content-Type":"application/json"})) as response:
            result = json.load(response)
        assert result["status"] == "computed"
        assert len(result["intensity"]) == 12
        assert len(result["field_real"]) == 2
        assert result["metadata"]["method"].startswith("direct scalar Rayleigh-Sommerfeld")
        with pytest.raises(HTTPError) as error:
            urlopen(Request(base+"/api/probe",data=json.dumps({"job_id":"job-ephemeral-123","config":{}}).encode(),
                            headers={"Content-Type":"application/json"}))
        assert error.value.code == 400
    finally:
        service.shutdown()
        service.server_close()
        service.workbench.executor.shutdown()


def test_measurement_comparison_retains_frozen_prediction(tmp_path):
    run = tmp_path/"run"
    run.mkdir()
    frozen = run/"result.json"
    frozen.write_text(json.dumps({"id":"fixture-design","metrics":{"transmission_efficiency":.8}}),encoding="utf-8")
    before = sha256_file(frozen)
    file = tmp_path/"measurement-test-fixture.csv"
    file.write_text("metric,value,unit,uncertainty,source\ntransmission_efficiency,0.7,ratio,0.01,synthetic unit-test fixture\n",encoding="utf-8")
    feedback = compare_measurements(run,file,tmp_path)
    assert feedback["records"][0]["difference"]==pytest.approx(-.1)
    assert feedback["calibration_status"].startswith("comparison_only")
    assert sha256_file(frozen)==before
    file.write_text("metric,value,unit,uncertainty,source\ntransmission_efficiency,0.7,ratio,,\n",encoding="utf-8")
    with pytest.raises(ValueError,match="source"):
        compare_measurements(run,file,tmp_path)
