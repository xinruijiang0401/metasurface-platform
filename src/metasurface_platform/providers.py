"""Optional local model adapter; numerical claims always come from solvers."""
from __future__ import annotations

import copy
import json
import math
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .spec import DEFAULTS,DeterministicRequirementProvider


class OllamaRequirementProvider:
    name = "ollama_structured_requirements"

    def __init__(self,model,endpoint="http://127.0.0.1:11434",timeout=90):
        if not model or not endpoint.startswith(("http://127.0.0.1:","http://localhost:")):
            raise ValueError("Configure a local Ollama model and localhost endpoint")
        self.model,self.endpoint,self.timeout = model,endpoint.rstrip("/"),timeout

    def connection_status(self):
        """Check the configured local Ollama endpoint without claiming model use.

        The workbench calls this from an explicit status endpoint.  In particular,
        a configured model is not reported as connected until this request has
        actually reached ``/api/tags`` and the response has been inspected.
        ``reachable`` describes the HTTP endpoint while ``model_available``
        describes the selected model independently.
        """
        status = {
            "provider": self.name,
            "configured": True,
            "checked": True,
            "reachable": False,
            "model": self.model,
            "model_available": False,
            "endpoint": self.endpoint,
            "error": None,
        }
        request = Request(self.endpoint + "/api/tags", method="GET")
        try:
            with urlopen(request, timeout=min(float(self.timeout), 5.0)) as response:
                outer = json.load(response)
            status["reachable"] = True
            tags = outer.get("models", []) if isinstance(outer, dict) else []
            names = [item.get("name") for item in tags if isinstance(item, dict) and isinstance(item.get("name"), str)]
            status["model_available"] = any(_model_name_matches(self.model, name) for name in names)
            if not status["model_available"]:
                status["error"] = "Configured Ollama model is not listed by /api/tags"
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            # Keep the status payload useful to the UI while avoiding a traceback
            # for an optional local service that is not running.
            status["error"] = _short_error(exc)
        return status

    def review_config(self, config):
        """Ask the local model to review structured parameters only.

        This operation is deliberately separate from :meth:`parse`: the model
        receives a JSON configuration rather than a language prompt and is only
        allowed to return a human-readable summary and warnings.  It cannot
        modify the configuration or provide numerical performance results.
        Callers should use :func:`deterministic_config_review` if this fails.
        """
        if not isinstance(config, dict):
            raise ValueError("Structured design configuration must be an object")
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "summary": {"type": "string", "maxLength": 1200},
                "warnings": {"type": "array", "items": {"type": "string", "maxLength": 400}, "maxItems": 20},
            },
            "required": ["summary", "warnings"],
        }
        payload = {
            "model": self.model,
            "stream": False,
            "format": schema,
            "options": {"temperature": 0},
            "prompt": (
                "Review the following structured metasurface design parameters. "
                "Return JSON with only summary and warnings. Do not change, complete, "
                "or repeat the configuration. Do not estimate efficiency, focal spot, "
                "or any other numerical performance; those values come only from the "
                "numerical solver. Mention missing/default values or first-release "
                "scope limits when relevant.\nCONFIG:\n" + json.dumps(config, ensure_ascii=False, sort_keys=True)
            ),
        }
        request = Request(
            self.endpoint + "/api/generate",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=self.timeout) as response:
            outer = json.load(response)
        if not isinstance(outer, dict) or not isinstance(outer.get("response"), str):
            raise ValueError("Local model returned no structured review")
        parsed = json.loads(outer["response"])
        if not isinstance(parsed, dict) or set(parsed) != {"summary", "warnings"}:
            raise ValueError("Local model returned an unsupported review shape")
        summary = parsed["summary"]
        warnings = parsed["warnings"]
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 1200:
            raise ValueError("Local model returned an invalid review summary")
        if not isinstance(warnings, list) or len(warnings) > 20 or any(not isinstance(item, str) or not item.strip() or len(item) > 400 for item in warnings):
            raise ValueError("Local model returned invalid review warnings")
        return {
            "provider": "ollama_config_review",
            "summary": summary.strip(),
            "warnings": [item.strip() for item in warnings],
            "model": self.model,
            "solver_source": "Numerical performance is produced only by the solver pipeline.",
        }

    def parse(self,text):
        if not isinstance(text,str) or not text.strip():
            raise ValueError("Design requirement is empty")
        schema = {"type":"object","additionalProperties":False,
                  "properties":{"function":{"type":"string","enum":["metalens","unsupported"]},
                                "wavelength_nm":{"type":"number"},"diameter_um":{"type":"number"},
                                "focal_length_um":{"type":"number"},"beam_type":{"type":"string","enum":["plane","gaussian"]}},
                  "required":["function"]}
        payload = {"model":self.model,"stream":False,"format":schema,"options":{"temperature":0},
                   "prompt":"Extract a monochromatic focusing metasurface requirement as JSON. Convert wavelength to vacuum nm and geometry to um. Include only explicitly supplied quantities. Use function=unsupported for broadband, non-focusing, contradictory or unclear tasks. Never invent material, efficiency or manufacturing data.\nRequirement:\n"+text}
        request = Request(self.endpoint+"/api/generate",data=json.dumps(payload).encode("utf-8"),headers={"Content-Type":"application/json"})
        with urlopen(request,timeout=self.timeout) as response:
            outer = json.load(response)
        parsed = json.loads(outer["response"])
        if parsed.get("function")!="metalens" or set(parsed)-set(schema["properties"]):
            raise ValueError("Local model returned an unsupported or invalid requirement")
        result = copy.deepcopy(DEFAULTS)
        explicit = set()
        for key in ("wavelength_nm","diameter_um","focal_length_um"):
            if key in parsed:
                value = float(parsed[key])
                if not math.isfinite(value) or value<=0:
                    raise ValueError("Local model returned a nonpositive or nonfinite optical parameter")
                result[key] = value
                explicit.add(key)
        if "beam_type" in parsed:
            if parsed["beam_type"] not in {"plane","gaussian"}:
                raise ValueError("Local model returned an unsupported beam")
            result["beam"]["type"] = parsed["beam_type"]
        result.update({"input_text":text,"requirement_provider":self.name,"model":self.model,
                       "entry_mode":"traditional_optic" if explicit&{"diameter_um","focal_length_um"} else "function_wavelength",
                       "defaults_applied":{key:copy.deepcopy(result[key]) for key in ("wavelength_nm","diameter_um","focal_length_um","beam","seed") if key not in explicit},
                       "model_output_requires_review":True})
        return result


def _model_name_matches(configured, listed):
    """Match Ollama's optional ``:tag`` suffix without accepting wildcards."""
    if configured == listed:
        return True
    if listed.startswith(configured + ":"):
        return True
    if configured.startswith(listed + ":"):
        return True
    return False


def _short_error(exc):
    if isinstance(exc, HTTPError):
        return f"Ollama HTTP {exc.code}"
    if isinstance(exc, URLError):
        reason = getattr(exc, "reason", None)
        return f"Ollama endpoint unavailable: {reason or 'connection failed'}"
    return f"Ollama status check failed: {exc}"


def deterministic_config_review(config, *, reason="ollama_not_configured"):
    """Review structured parameters without a model and without solver metrics.

    The fallback is intentionally conservative.  It reports validation issues
    as warnings and leaves the caller's configuration untouched.  This is used
    both when Ollama is not configured and when a configured local endpoint
    cannot produce a valid structured review.
    """
    warnings = []
    valid = isinstance(config, dict)
    if not valid:
        warnings.append("结构化配置必须是 JSON 对象。")
        config = {}
    required = ("wavelength_nm", "diameter_um", "focal_length_um")
    missing = [key for key in required if key not in config]
    if missing:
        warnings.append("缺少参数，将使用默认值或在运行前拒绝：" + ", ".join(missing))
    bounds = {"wavelength_nm": (450.0, 700.0), "diameter_um": (2.0, 80.0)}
    for key, (lo, hi) in bounds.items():
        if key not in config:
            continue
        try:
            value = float(config[key])
            if not math.isfinite(value) or not lo <= value <= hi:
                warnings.append(f"{key}={config[key]!r} 超出首版范围 [{lo:g}, {hi:g}]。")
        except (TypeError, ValueError):
            warnings.append(f"{key} 不是有限数字。")
    if "focal_length_um" in config:
        try:
            focal_length = float(config["focal_length_um"])
            if not math.isfinite(focal_length) or focal_length <= 0:
                warnings.append("focal_length_um 必须为正的有限数字。")
            elif "diameter_um" in config:
                diameter = float(config["diameter_um"])
                if math.isfinite(diameter) and diameter > 0:
                    na = math.sin(math.atan(diameter / (2 * focal_length)))
                    if na > 0.3:
                        warnings.append(f"估算 NA={na:.3f} 超过首版标量传播范围 0.3，需要更高保真验证。")
        except (TypeError, ValueError):
            pass
    beam = config.get("beam") if isinstance(config.get("beam"), dict) else {}
    if beam and beam.get("type", "plane") not in {"plane", "gaussian"}:
        warnings.append("首版仅支持 plane 或 gaussian 入射光束。")
    if config.get("assume_k_zero") is True:
        warnings.append("当前允许缺失消光系数时使用 k=0；材料记录会保留该假设。")
    defaults = config.get("defaults_applied")
    if defaults:
        warnings.append("配置包含默认参数；请在运行设计前确认默认值。")
    if not warnings:
        summary = "结构化参数通过基础范围检查；材料、响应库、布局和性能仍由数值求解器计算。"
    else:
        summary = "已完成本地确定性检查；请处理警告后再运行数值设计。"
    return {
        "provider": "deterministic_fallback",
        "summary": summary,
        "warnings": warnings,
        "fallback_reason": reason,
        "solver_source": "数值性能只来自求解器流水线；本地检查不生成性能数值。",
    }


def agent_connection_status(provider, *, check_connection=False):
    """Return explicit configured-versus-checked status for the assistant UI."""
    if isinstance(provider, OllamaRequirementProvider):
        if check_connection:
            return provider.connection_status()
        return {
            "provider": provider.name,
            "configured": True,
            "checked": False,
            "reachable": None,
            "model": provider.model,
            "model_available": None,
            "endpoint": provider.endpoint,
            "error": None,
        }
    return {
        "provider": getattr(provider, "name", "deterministic_rule_parser"),
        "configured": False,
        "checked": False,
        "reachable": None,
        "model": None,
        "model_available": None,
        "endpoint": None,
        "error": None,
    }


def review_structured_config(provider, config):
    """Review a validated config using Ollama if available, else deterministic checks."""
    if isinstance(provider, OllamaRequirementProvider):
        status = provider.connection_status()
        if not status["reachable"]:
            return deterministic_config_review(config, reason="ollama_unreachable")
        if not status["model_available"]:
            return deterministic_config_review(config, reason="ollama_model_unavailable")
        try:
            return provider.review_config(config)
        except Exception:
            # A configured endpoint can still fail the generation or return an
            # invalid schema.  The UI must see that the review was deterministic.
            return deterministic_config_review(config, reason="ollama_review_failed")
    return deterministic_config_review(config, reason="ollama_not_configured")


def get_requirement_provider():
    model = os.environ.get("METASURFACE_OLLAMA_MODEL")
    return OllamaRequirementProvider(model,os.environ.get("METASURFACE_OLLAMA_ENDPOINT","http://127.0.0.1:11434")) if model else DeterministicRequirementProvider()
