"""Design, inspect and compare measurements using immutable configurations."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime,timezone,timedelta
import json
from pathlib import Path
import uuid
import yaml

from .pipeline import run_design
from .server import serve
from .spec import parse_requirement
from .providers import get_requirement_provider
from .storage import write_json


def compare_measurements(run_directory,measurement_file,output_root):
    run_directory = Path(run_directory).resolve()
    result = json.loads((run_directory/"result.json").read_text(encoding="utf-8"))
    rows = []
    units = {"transmission_efficiency":"ratio","focusing_efficiency":"ratio","fwhm_x_um":"um","fwhm_y_um":"um",
             "estimated_focus_um":"um","strehl":"ratio"}
    with Path(measurement_file).open(encoding="utf-8-sig",newline="") as f:
        for row in csv.DictReader(f):
            metric = row["metric"]
            if metric not in units or row["unit"]!=units[metric]:
                raise ValueError(f"Unsupported measured metric or unit: {metric}")
            if not row.get("source","").strip():
                raise ValueError("Each measurement needs a real source/batch/file reference")
            measured = float(row["value"])
            prediction = result["metrics"].get(metric)
            if prediction is None:
                raise ValueError(f"No frozen prediction for {metric}")
            rows.append({**row,"prediction":prediction,"measured":measured,"difference":measured-prediction,
                         "uncertainty":float(row["uncertainty"]) if row.get("uncertainty") else None})
    output = Path(output_root)/"feedback"/(result["id"]+"-"+uuid.uuid4().hex[:6])
    output.mkdir(parents=True)
    data = {"design_id":result["id"],"created_at":datetime.now(timezone(timedelta(hours=8))).isoformat(),
            "source_file":str(Path(measurement_file).resolve()),"records":rows,"calibration_status":"comparison_only; material/process model not refitted"}
    write_json(output/"comparison.json",data)
    lines = ["# 冻结预测与用户测量对照","","保留原预测；仅作指标比较，尚未反演材料或工艺模型。","",
             "| 指标 | 事前预测 | 用户测量 | 差值 | 单位 | 来源 |","|---|---:|---:|---:|---|---|"]
    lines += [f"| {r['metric']} | {r['prediction']} | {r['measured']} | {r['difference']} | {r['unit']} | {r['source']} |" for r in rows]
    (output/"comparison.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
    return data


def main():
    parser = argparse.ArgumentParser(description="Open-source dielectric metalens design")
    parser.add_argument("--workspace",default=".",help="Output workspace (runs/cache remain here)")
    commands = parser.add_subparsers(dest="command",required=True)
    run = commands.add_parser("run",help="Run a YAML/JSON design")
    run.add_argument("config")
    nl = commands.add_parser("parse",help="Parse supported focusing requirements")
    nl.add_argument("text")
    web = commands.add_parser("serve",help="Start the local interactive workbench")
    web.add_argument("--port",type=int,default=8765)
    compare = commands.add_parser("compare",help="Compare real measurement CSV with frozen predictions")
    compare.add_argument("run_directory")
    compare.add_argument("measurements")
    args = parser.parse_args()
    if args.command=="run":
        config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8-sig"))
        result = run_design(config,args.workspace,lambda stage,progress:print(f"{100*progress:5.1f}% {stage}",flush=True))
        print(json.dumps({"id":result["id"],"metrics":result["metrics"],"validation":result["validation"]},ensure_ascii=False,indent=2))
    elif args.command=="parse":
        print(json.dumps(get_requirement_provider().parse(args.text),ensure_ascii=False,indent=2))
    elif args.command=="serve":
        serve(args.workspace,args.port)
    else:
        print(json.dumps(compare_measurements(args.run_directory,args.measurements,args.workspace),ensure_ascii=False,indent=2))


if __name__=="__main__":
    main()
