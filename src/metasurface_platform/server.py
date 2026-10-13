"""Local, dependency-free HTTP workbench with asynchronous numerical jobs."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
from threading import Lock
import traceback
from urllib.parse import unquote, urlparse
import uuid

from . import __version__
from .pipeline import default_config, prepare_config, preview_beam, run_design
from .detector import measure_job
from .spec import parse_requirement
from .providers import agent_connection_status, get_requirement_provider, review_structured_config
from .storage import jsonable


def public_result(result):
    result = json.loads(json.dumps(result))
    prefix = "/runs/"+result["id"]+"/"
    for key,value in result.get("plots",{}).items():
        if isinstance(value,list):
            for item in value:
                item["url"] = prefix+item["url"]
        elif isinstance(value,str):
            result["plots"][key] = prefix+value
    for item in result.get("artifacts",[]):
        item["url"] = prefix+item["url"]
    result["report_url"] = prefix+result["report_url"]
    return result


class Workbench:
    def __init__(self,root):
        self.root = Path(root).resolve()
        self.jobs = {}
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.compute_lock = Lock()
        self.requirement_provider = get_requirement_provider()

    def saved_jobs(self):
        jobs = list(self.jobs.values())
        ids = {j["id"] for j in jobs}
        for path in sorted((self.root/"runs").glob("*/result.json"),reverse=True):
            result = json.loads(path.read_text(encoding="utf-8"))
            if result["id"] not in ids:
                jobs.append({"id":result["id"],"status":"completed","stage":"complete","progress":1.,
                             "result":public_result(result)})
        return jobs

    def run(self,config):
        prepare_config(config)
        if self.compute_lock.locked():
            raise ValueError("已有设计任务在计算，请等待完成后提交新版本")
        request_id = "job-"+uuid.uuid4().hex[:12]
        self.jobs[request_id] = {"id":request_id,"status":"queued","stage":"queued","progress":0,"config":config}
        def work():
            with self.compute_lock:
                job = self.jobs[request_id]
                job["status"] = "running"
                def progress(stage,value):
                    job.update({"stage":stage,"progress":value})
                try:
                    result = run_design(config,self.root,progress)
                    job.update({"status":"completed","stage":"complete","progress":1.,"result":public_result(result)})
                except Exception as exc:
                    traceback.print_exc()
                    job.update({"status":"failed","error":str(exc)})
        self.executor.submit(work)
        return dict(self.jobs[request_id])


class Handler(BaseHTTPRequestHandler):
    server_version = "Metasurface/"+__version__

    @property
    def workbench(self):
        return self.server.workbench

    def log_message(self,format,*args):
        return

    def send_json(self,value,status=200):
        data = json.dumps(jsonable(value),ensure_ascii=False,allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Cache-Control","no-store")
        self.send_header("Content-Length",str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        try:
            if path == "/api/agent/status":
                # This is the explicit connection action.  A configured local
                # Ollama provider performs a real /api/tags request here; the
                # deterministic provider never pretends that a model is online.
                status = agent_connection_status(self.workbench.requirement_provider, check_connection=True)
                self.send_json(status)
                return
            if path == "/api/status":
                jobs = self.workbench.saved_jobs()
                provider = self.workbench.requirement_provider
                agent = agent_connection_status(provider, check_connection=False)
                self.send_json({"version":__version__,"default_config":default_config(),"jobs":jobs,
                                "available":True,"parser":provider.name,
                                "agent":{**agent,
                                         # Keep these aliases for older clients;
                                         # the explicit /api/agent/status payload
                                         # is the authoritative connection check.
                                         "ollama_configured":agent["configured"],
                                         "connection_checked":agent["checked"]},
                                "compute_backend":"grcwa / scalar angular spectrum",
                                "computational_methods":{
                                    "unit_response":{"backend":"grcwa","method":"periodic RCWA","boundary":"x/y periodic; z incoming/outgoing modes; no PML"},
                                    "device_field":{"method":"local periodic response + band-limited scalar angular spectrum"},
                                    "detector":{"method":"scalar Rayleigh-Sommerfeld-I","endpoint":"POST /api/probe"},
                                    "comsol":{"status":"reference only; not integrated or cross-validated"}}})
                return
            if path.startswith("/api/jobs/"):
                parts = path.split("/")
                job = next((j for j in self.workbench.saved_jobs() if j["id"]==parts[3]),None)
                if job is None:
                    self.send_json({"error":"Unknown job"},404)
                elif len(parts)>4 and parts[4]=="artifacts":
                    self.send_json({"artifacts":job.get("result",{}).get("artifacts",[])})
                else:
                    self.send_json(job)
                return
            if path.startswith("/runs/"):
                base = (self.workbench.root/"runs").resolve()
                target = (self.workbench.root/path.lstrip("/")).resolve()
            elif path.startswith("/previews/"):
                base = (self.workbench.root/"previews").resolve()
                target = (self.workbench.root/path.lstrip("/")).resolve()
            else:
                base = Path(__file__).parent/"web"
                target = (base/("index.html" if path=="/" else path.lstrip("/"))).resolve()
            if not target.is_relative_to(base.resolve()) or not target.is_file():
                self.send_json({"error":"File not found"},404)
                return
            data = target.read_bytes()
            mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if target.suffix==".js":
                mime = "application/javascript"
            self.send_response(200)
            self.send_header("Content-Type",mime+("; charset=utf-8" if mime.startswith("text/") or target.suffix==".js" else ""))
            self.send_header("Content-Length",str(len(data)))
            if target.suffix in {".gds",".oas",".h5",".csv",".yaml"}:
                self.send_header("Content-Disposition",f'attachment; filename="{target.name}"')
            self.end_headers()
            self.wfile.write(data)
        except Exception as exc:
            self.send_json({"error":str(exc)},500)

    def do_POST(self):
        try:
            size = int(self.headers.get("Content-Length","0"))
            if not 0 < size <= 1_000_000:
                raise ValueError("Invalid JSON request size")
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc != self.headers.get("Host"):
                raise ValueError("Use the local workbench origin")
            body = json.loads(self.rfile.read(size))
            path = urlparse(self.path).path
            if path=="/api/parse":
                parsed = self.workbench.requirement_provider.parse(body.get("text",""))
                config = default_config()
                config.update(parsed)
                self.send_json({"config":config,"defaults_applied":parsed["defaults_applied"],"provider":parsed["requirement_provider"]})
            elif path=="/api/agent/review":
                config = body.get("config")
                if not isinstance(config, dict):
                    raise ValueError("/api/agent/review requires a structured config object")
                # Review is advisory.  It never returns or mutates a model
                # generated configuration; numerical performance is reserved
                # for /api/run and the configured solver pipeline.
                validated_config = prepare_config(config)
                self.send_json(review_structured_config(self.workbench.requirement_provider, validated_config))
            elif path=="/api/preview":
                if not self.workbench.compute_lock.acquire(blocking=False):
                    raise ValueError("已有计算任务在运行，预览将在任务完成后可用")
                try:
                    preview_id = uuid.uuid4().hex[:12]
                    result = preview_beam(body["config"],self.workbench.root/"previews"/preview_id)
                    result["plots"] = {k:f"/previews/{preview_id}/{v}" for k,v in result["plots"].items()}
                finally:
                    self.workbench.compute_lock.release()
                self.send_json(result)
            elif path=="/api/run":
                self.send_json(self.workbench.run(body["config"]),202)
            elif path=="/api/probe":
                # The detector consumes the persisted result id, not the
                # transient queue id. measure_job confines all reads to runs/.
                job_id = body.get("job_id")
                config = body.get("config")
                if not isinstance(job_id,str) or not isinstance(config,dict):
                    raise ValueError("/api/probe requires a result job_id and detector config")
                result = measure_job(self.workbench.root/"runs",job_id,config)
                self.send_json(result)
            else:
                self.send_json({"error":"Unknown API endpoint"},404)
        except (ValueError,KeyError,TypeError) as exc:
            self.send_json({"error":str(exc)},400)
        except Exception as exc:
            traceback.print_exc()
            self.send_json({"error":str(exc)},500)


def serve(root,port=8765,host="127.0.0.1"):
    if host not in {"127.0.0.1","localhost"}:
        raise ValueError("This prototype runs on localhost")
    server = ThreadingHTTPServer((host,port),Handler)
    server.workbench = Workbench(root)
    print(f"Metasurface workbench: http://{host}:{port}",flush=True)
    try:
        server.serve_forever()
    finally:
        server.workbench.executor.shutdown(wait=False)
        server.server_close()
