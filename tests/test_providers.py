import json
from io import BytesIO
from http.server import BaseHTTPRequestHandler,HTTPServer
from threading import Thread
from urllib.error import URLError
from unittest.mock import patch
import pytest
from metasurface_platform.providers import (
    OllamaRequirementProvider,
    agent_connection_status,
    deterministic_config_review,
    review_structured_config,
)


def test_local_model_structured_adapter_preserves_units_and_large_request():
    class FakeLocalModel(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert payload["options"]["temperature"]==0
            assert payload["format"]["additionalProperties"] is False
            data = json.dumps({"response":json.dumps({"function":"metalens","wavelength_nm":532,"diameter_um":2000,"focal_length_um":10000})}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(data)
        def log_message(self,*args): pass
    server = HTTPServer(("127.0.0.1",0),FakeLocalModel)
    worker = Thread(target=server.serve_forever,daemon=True)
    worker.start()
    try:
        provider = OllamaRequirementProvider("test-fixture",f"http://127.0.0.1:{server.server_port}")
        config = provider.parse("把532nm焦距10mm口径2mm的透镜变成超表面")
        assert config["diameter_um"]==2000 and config["focal_length_um"]==10000
        assert config["model_output_requires_review"]
        assert "diameter_um" not in config["defaults_applied"]
    finally:
        server.shutdown()
        server.server_close()


def test_local_model_adapter_requires_explicit_local_endpoint():
    with pytest.raises(ValueError):
        OllamaRequirementProvider("model","https://example.com")


class FakeResponse(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


def fake_response(payload):
    return FakeResponse(json.dumps(payload).encode("utf-8"))


def test_connection_status_distinguishes_configured_from_reachable_and_model():
    provider = OllamaRequirementProvider("qwen3", "http://127.0.0.1:11434")
    configured = agent_connection_status(provider)
    assert configured["configured"] is True
    assert configured["checked"] is False
    assert configured["reachable"] is None
    assert configured["model_available"] is None

    with patch("metasurface_platform.providers.urlopen", return_value=fake_response({"models": [{"name": "qwen3:latest"}]})) as request:
        checked = provider.connection_status()
    assert request.call_args.args[0].full_url == "http://127.0.0.1:11434/api/tags"
    assert checked["configured"] is True and checked["checked"] is True
    assert checked["reachable"] is True and checked["model_available"] is True
    assert checked["model"] == "qwen3"


def test_connection_status_reports_reachable_endpoint_without_selected_model():
    provider = OllamaRequirementProvider("missing-model", "http://localhost:11434")
    with patch("metasurface_platform.providers.urlopen", return_value=fake_response({"models": [{"name": "other:latest"}]})):
        checked = provider.connection_status()
    assert checked["reachable"] is True
    assert checked["model_available"] is False
    assert "not listed" in checked["error"]


def test_ollama_reviews_structured_config_without_returning_a_modified_config():
    provider = OllamaRequirementProvider("qwen3", "http://127.0.0.1:11434")
    config = {"wavelength_nm": 532, "diameter_um": 30, "focal_length_um": 100,
              "beam": {"type": "gaussian", "polarization": {"type": "linear", "angle_deg": 0}}}
    replies = [
        fake_response({"models": [{"name": "qwen3:latest"}]}),
        fake_response({"response": json.dumps({"summary": "参数已整理，可按当前首版范围检查。", "warnings": ["请核对工艺假设。"]}, ensure_ascii=False)}),
    ]
    with patch("metasurface_platform.providers.urlopen", side_effect=replies) as request:
        result = review_structured_config(provider, config)
    assert [call.args[0].full_url for call in request.call_args_list] == [
        "http://127.0.0.1:11434/api/tags", "http://127.0.0.1:11434/api/generate"
    ]
    generated = json.loads(request.call_args_list[1].args[0].data.decode("utf-8"))
    assert generated["model"] == "qwen3"
    assert '"wavelength_nm": 532' in generated["prompt"]
    assert "Do not estimate efficiency" in generated["prompt"]
    assert result["provider"] == "ollama_config_review"
    assert result["summary"] == "参数已整理，可按当前首版范围检查。"
    assert result["warnings"] == ["请核对工艺假设。"]
    assert result["solver_source"]
    assert "config" not in result
    assert config["wavelength_nm"] == 532


def test_unreachable_ollama_uses_explicit_deterministic_fallback():
    provider = OllamaRequirementProvider("qwen3", "http://127.0.0.1:11434")
    config = {"wavelength_nm": 532, "diameter_um": 30, "focal_length_um": 100,
              "assume_k_zero": True, "defaults_applied": {"beam": {"type": "plane"}}}
    with patch("metasurface_platform.providers.urlopen", side_effect=URLError("offline")):
        result = review_structured_config(provider, config)
    assert result["provider"] == "deterministic_fallback"
    assert result["fallback_reason"] == "ollama_unreachable"
    assert any("k=0" in item for item in result["warnings"])
    assert any("默认参数" in item for item in result["warnings"])


def test_deterministic_review_reports_geometry_scope_without_model_call():
    result = deterministic_config_review({"wavelength_nm": 532, "diameter_um": 30,
                                          "focal_length_um": 10, "beam": {"type": "plane"}})
    assert result["provider"] == "deterministic_fallback"
    assert result["fallback_reason"] == "ollama_not_configured"
    assert any("超过首版标量传播范围" in item for item in result["warnings"])
