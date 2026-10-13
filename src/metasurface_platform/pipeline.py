"""End-to-end, source-traceable single-layer dielectric metalens workflow."""
from __future__ import annotations

import copy
import csv
from datetime import datetime, timezone, timedelta
from importlib.metadata import version
import json
from pathlib import Path
import platform
import shutil
import uuid

import numpy as np
import yaml
from scipy.ndimage import zoom

from . import __version__
from .beams import incident_field, intensity, make_grid
from .layout import export_layout
from .materials import MaterialCatalog
from .optics import angular_spectrum, analyze_device, target_lens_phase
from .plotting import field_plot, library_plot, axial_plot, tolerance_plot, convergence_plot
from .solver import RCWASettings, build_response_library, solve_supercell, validate_convergence
from .spec import DEFAULTS, normalize_spec, validate_spec
from .storage import digest, jsonable, load_library, save_library, save_fields, sha256_file, write_json
from .tolerance import evaluate_tolerance

DEFAULT_SIMULATION = {"seed": 42, "tolerance_samples": 12, "harmonics": 81, "grid_size": 128,
                      "propagation_padding": 3, "heights_nm": [600, 750], "periods_nm": [280, 300], "radii_count": 15}
DEFAULT_FABRICATION = {"template": "reference-dielectric-etch-v0.1", "minimum_width_nm": 80,
                       "minimum_gap_nm": 60, "maximum_aspect_ratio": 8,
                       "diameter_bias_sigma_nm": 4, "height_bias_sigma_nm": 8,
                       "local_diameter_sigma_nm": 2, "spatial_diameter_sigma_nm": 2,
                       "correlation_length_um": 3}


def default_config():
    config = copy.deepcopy(DEFAULTS)
    config.update({"beam": {**config["beam"], "type": "gaussian"}, "seed": 42,
                   "period_nm": 300, "assume_k_zero": True,
                   "material": {"preferred": "auto"}, "simulation": copy.deepcopy(DEFAULT_SIMULATION),
                   "fabrication": copy.deepcopy(DEFAULT_FABRICATION)})
    return config


def prepare_config(raw, *, for_run=True):
    config = default_config()
    for key, value in raw.items():
        if isinstance(value, dict) and isinstance(config.get(key), dict):
            config[key].update(copy.deepcopy(value))
        else:
            config[key] = copy.deepcopy(value)
    if "assume_missing_k_zero" in raw and "assume_k_zero" not in raw:
        config["assume_k_zero"] = bool(raw["assume_missing_k_zero"])
    requested_seed = float(raw.get("simulation",{}).get("seed", raw.get("seed",42)))
    if not np.isfinite(requested_seed) or requested_seed<0 or not requested_seed.is_integer():
        raise ValueError("Random seed must be a nonnegative integer")
    config["seed"] = int(requested_seed)
    config["simulation"]["seed"] = config["seed"]
    if for_run:
        config = normalize_spec(config)
    else:
        validate_spec(config, for_run=False)
        config.update({"wavelength_m": config["wavelength_nm"]*1e-9, "diameter_m": config["diameter_um"]*1e-6,
                       "focal_length_m": config["focal_length_um"]*1e-6})
    sim = config["simulation"]
    if not 5 <= int(sim["radii_count"]) <= 41 or not 5 <= int(sim["harmonics"]) <= 169 or not 32 <= int(sim["grid_size"]) <= 256:
        raise ValueError("Unsupported library discretization; radii 5–41, harmonics 5–169, grid 32–256")
    if not 2 <= int(sim["propagation_padding"]) <= 5:
        raise ValueError("Propagation padding must be 2–5")
    if int(sim["harmonics"])!=81 or int(sim["grid_size"])!=128:
        raise ValueError("Validated v0.1 library baseline uses harmonics=81 and grid_size=128; alternative resolutions need a matching convergence schedule")
    if not 1 <= len(sim["periods_nm"]) <= 3 or not 1 <= len(sim["heights_nm"]) <= 3:
        raise ValueError("At most three candidate periods and heights are supported per task")
    config.setdefault("tolerance", {})
    fab = config["fabrication"]
    config["tolerance"] = {"samples": int(sim["tolerance_samples"]),
                           "common_diameter_sigma_nm": fab["diameter_bias_sigma_nm"],
                           "correlated_diameter_sigma_nm": fab["spatial_diameter_sigma_nm"],
                           "local_diameter_sigma_nm": fab["local_diameter_sigma_nm"],
                           "height_sigma_nm": fab["height_bias_sigma_nm"],
                           "correlation_length_um": fab["correlation_length_um"],
                           **config["tolerance"]}
    return config


def preview_beam(raw, directory):
    config = prepare_config(raw, for_run=False)
    catalog = MaterialCatalog()
    substrate = catalog.evaluate("SiO2/Malitson", config["wavelength_nm"], assume_k_zero=config["assume_k_zero"])
    config["beam"]["medium_n"] = substrate["n"]
    x, y = make_grid(config["diameter_m"], 300e-9, pad_factor=2)
    X, Y = np.meshgrid(x, y)
    aperture = X*X+Y*Y <= (config["diameter_m"]/2)**2
    beam = incident_field(x, y, config["wavelength_m"], config["beam"], aperture)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    unmasked = beam["unmasked_field"]
    component = int(np.argmax(np.sum(np.abs(unmasked)**2, axis=(1,2))))
    mask = intensity(unmasked) > intensity(unmasked).max()*1e-6
    field_plot(directory/"input_intensity.png", intensity(unmasked), x*1e6, y*1e6,
               "User-specified beam / substrate-side input", "Reference power density")
    field_plot(directory/"input_phase.png", np.angle(unmasked[component]), x*1e6, y*1e6,
               "Input wavefront at device reference plane", "Phase (rad)", phase=True, mask=mask)
    return {"metadata": jsonable(beam["metadata"]), "incident_aperture_power_w": beam["power_w"],
            "substrate_n": substrate["n"], "normalization": config["beam"]["normalization"],
            "plots": {"input_intensity": "input_intensity.png", "input_phase": "input_phase.png"}}


def _candidate_specs(config, catalog, substrate):
    names = ["TiO2/Siefke", "Si3N4/Luke"]
    preferred = config["material"].get("preferred", "auto")
    materials = [catalog.evaluate(name, config["wavelength_nm"], config["assume_k_zero"]) for name in names]
    materials = [m for m in materials if m["usable_for_simulation"] and m["k_used"] < .05 and (preferred == "auto" or preferred in (m["source_id"], m["material_family"], m["material_id"]))]
    if not materials:
        raise ValueError("No supported low-loss sample is usable; check wavelength, preferred sample and explicit missing-k assumption")
    specs = []
    fab, sim = config["fabrication"], config["simulation"]
    for m in materials:
        for period in sim["periods_nm"]:
            if not 200 <= period <= 450 or period >= config["wavelength_nm"]/substrate["n"]:
                continue
            for height in sim["heights_nm"]:
                if not 250 <= height <= 1000:
                    raise ValueError("First-release reference process supports candidate heights 250–1000 nm")
                lo = max(fab["minimum_width_nm"]/2+2, height/fab["maximum_aspect_ratio"]/2+2)
                hi = (period-fab["minimum_gap_nm"])/2-2
                if hi-lo < 20:
                    continue
                specs.append({**m, "candidate_id": f"{m['source_id']}-p{period}-h{height}",
                              "period_nm": period, "height_nm": height,
                              "pillar_index": m["index"], "radii_nm": np.linspace(lo, hi, sim["radii_count"])})
    if not specs:
        raise ValueError("No material/height/period satisfies this process and closed diffraction-order constraints")
    return specs


def _map_candidate(config, candidate, valid=None):
    period_m = candidate["period_nm"]*1e-9
    x, y = make_grid(config["diameter_m"], period_m, config["simulation"]["propagation_padding"])
    X, Y = np.meshgrid(x, y)
    # Full circles and their polygon quantization remain within the aperture.
    aperture = np.hypot(X,Y) <= config["diameter_m"]/2-(candidate["radii_nm"].max()+2)*1e-9
    beam = incident_field(x, y, config["wavelength_m"], config["beam"], aperture)
    jones = np.asarray(beam["jones"])
    matrices = candidate["jones_t"]*np.sqrt(candidate["output_index"]/candidate["input_index"])
    responses = np.einsum("nij,j->ni", matrices, jones)
    dominant = int(np.argmax(np.abs(jones)))
    spatial_phase = np.angle(beam["field"][dominant]/jones[dominant])
    target = target_lens_phase(x, y, config["wavelength_m"], config["focal_length_m"])-spatial_phase
    guard = 8.0
    permitted = (candidate["radii_nm"] >= candidate["radii_nm"].min()+guard)&(candidate["radii_nm"] <= candidate["radii_nm"].max()-guard)
    permitted &= np.mean(candidate["power_T"],axis=1) > .15
    if valid is not None:
        permitted &= valid
    if permitted.sum() < 3:
        raise ValueError("Too few converged, transmissive sizes after fabrication/tolerance guard")
    allowed = np.flatnonzero(permitted)
    values = target[aperture]
    weights = intensity(beam["field"])[aperture]
    weights /= weights.sum()
    best = None
    # First-order expected response error under the declared diameter errors.
    derivative = np.gradient(responses,candidate["radii_nm"],axis=0)
    radius_variance = sum(float(config["tolerance"].get(k,default))**2 for k,default in
                          (("common_diameter_sigma_nm",4),("correlated_diameter_sigma_nm",2),("local_diameter_sigma_nm",2)))/4
    risk_penalty = radius_variance*np.sum(abs(derivative)**2,axis=1)
    for offset in np.linspace(-np.pi, np.pi, 32, endpoint=False):
        desired = np.exp(1j*(values+offset))[:,None]*jones
        cost = np.sum(np.abs(responses[allowed,None,:]-desired[None,:,:])**2,axis=2)
        cost += risk_penalty[allowed,None]
        indices = np.argmin(cost, axis=0)
        score = float(np.sum(cost[indices,np.arange(len(values))]*weights))
        if best is None or score < best[0]:
            best = score, offset, allowed[indices]
    score, offset, chosen = best
    ids = np.zeros(aperture.shape,int)
    ids[aperture] = chosen
    radius_map = candidate["radii_nm"][ids]
    output = np.einsum("yxij,jyx->iyx", matrices[ids], beam["field"])
    return {"x": x,"y":y,"X":X,"Y":Y,"aperture":aperture,"beam":beam,"indices":ids,
            "radius_map_nm":radius_map,"output":output,"target_phase":target+offset,
            "phase_offset_rad":float(offset),"score":score,"used_indices":np.unique(chosen),
            "objective":"complex response residual + first-order expected response error under declared diameter variance"}


def _report(directory, result, config, tolerance, validation, coupling, layout):
    m, c = result["metrics"], result["material_selection"]["selected"]
    absolute = config["beam"]["normalization"] != "normalized"
    text = f"""# 开源超表面设计报告 {result['id']}

版本：{__version__}；创建时间：{result['created_at']}。本报告来自实际 RCWA 与标量传播计算，没有实验测量。

## 公式、求解器与边界条件

单元 / 周期邻域采用 grcwa 的 RCWA Fourier 模态法：x/y 为周期边界（法向入射、零横向 Bloch 相位），z 向使用均匀介质中的入射 / 出射模态；本计算不使用 PML。整片器件使用局域周期单元复响应查表及带限标量角谱传播，探测截面 API 可从保存的出口复场调用标量 Rayleigh–Sommerfeld 第一积分。它们均按当前结果的求解器与配置记录，不代表有限器件全波仿真。

RS-I、RCWA、角谱传播、周期边界与有限域 PML 的公式及边界适用范围见 [`docs/theory-models-and-references.md`](../../docs/theory-models-and-references.md)。COMSOL Wave Optics 文档在该页列为 FEM / 频域方法参考；当前任务未调用 COMSOL，也没有 COMSOL 交叉验证数据。

## 设计与输入光束

- 真空波长 {config['wavelength_nm']:g} nm，名义口径 {config['diameter_um']:g} μm，目标焦距 {config['focal_length_um']:g} μm；焦距从柱层出口量起。
- 入射光束：{config['beam']['type']}；偏振：{config['beam']['polarization']}；归一化：{config['beam']['normalization']}。
- 入射侧为半无限 SiO₂，空气背景柱层，空气出射。未建模基底背面、有限基底厚度与实际镀膜。
- 材料样品：{c['source_id']}；n={c['n']:.6g}，来源 k={c['k']}，计算 k={c['k_used']}；缺失 k 的无损假设={c['assumed_k_zero']}。
- 基底：SiO2/Malitson，缺失 k 时仅在显式允许下使用 k=0。
- 联合筛选结果：周期 {c['period_nm']:g} nm、高度 {c['height_nm']:g} nm、{layout['geometry']['pillar_count']} 个圆柱。目标采用复响应残差与声明直径误差下的一阶响应敏感度惩罚，属于离散候选上的稳健选择。
- 工艺模板 {config['fabrication']['template']} 为参考假设，所需最小线宽 {config['fabrication']['minimum_width_nm']} nm、间隙 {config['fabrication']['minimum_gap_nm']} nm、最大深宽比 {config['fabrication']['maximum_aspect_ratio']}；尚无设备能力确认。

## 预测性能

| 指标 | 结果 |
|---|---:|
| 透射功率 / 孔径内入射功率 | {m['transmission_efficiency']:.6f} |
| 焦面 ROI 功率 / 孔径内入射功率 | {m['focusing_efficiency']:.6f} |
| 焦面 FWHM x / y | {m['fwhm_x_um']} / {m['fwhm_y_um']} μm |
| ROI 半径 | {m['roi_radius_um']:.6f} μm |
| 同瞳振幅归一化峰值比 | {m['strehl']} |
| 传播参考面功率保留比例 | {m['propagation_power_fraction']:.6f} |

效率分母采用有效器件孔径内入射功率，ROI 位于声明焦面并按峰值位置居中。此处为相干聚焦光场，不能无条件作为非相干成像 PSF/MTF。{'输入已给出绝对强度或功率，但结果仍为模型预测。' if absolute else '输入为数学功率参考；效率比有效，图中绝对场强/功率尺度不是实测激光功率。'}

## 数值验证与适用范围

设计验收状态：**{result['design_acceptance']}**。计算完成不代表制造或实验验收完成；局部周期模型偏差超门槛时版图保留为待复核设计草案。

单元直接求解保存完整电场 Jones 矩阵与 RCWA 通量；出口传播振幅按 sqrt(n_out/n_in) 转为功率尺度。主库设置 {config['simulation']['harmonics']} 谐波请求/{config['simulation']['grid_size']} 像素。

对最终使用的每个圆柱半径进行谐波及网格递增验证，采用相位差≤0.1 rad、绝对功率差≤0.02 的事先门槛；验证结果见 convergence.json。该结果限于离散选定尺寸与当前材料/波长，不是全参数或全器件收敛证明。未通过的尺寸不用于最终布局。

局部检查使用一个周期重复的 3×1 邻域，保存局部 E 场；高阶增量检查通过={coupling['numerical_check']['passed']}。最大局部出射平均场与局域单元模型相位偏差={coupling['max_local_phase_difference_rad']:.6g} rad。超胞不是有限整片 FDTD；它只提供所选邻域的耦合诊断，不能证明全部器件区域。

传播采用带限角谱和明确的补零窗口；补零增量与更细空间采样检查见 propagation-check.json。近场与整片场分别记录求解来源。

## 假设误差下的容差评估

三类直径误差、整体高度偏差及其分布见 tolerance.json。高度与半径使用真实 RCWA 三个高度库内的有界分段复响应插值；不把该代理结果标为每次直接电磁仿真。

样本数={len(tolerance['samples'])}，被拒绝比例={tolerance['summary']['rejected_fraction']}；超出库边界或违反无源模型约束的样本保留拒绝原因。均值、分位数和假设门槛通过比例仅描述已计算样本，不能作为已校准良率。尚未实现侧壁角、折射率漂移、缺柱与真实制程统计。

## 制造文件与复现

GDSII/OASIS 已按单位、层号、数目、位置、线宽、间隙和孔径回读检查；结果见 layout-check.json。版图仅含圆柱加工层，尚无对准标记、切割区域或曝光设备专用处理，不是可直接下单的完整工艺包。

config.yaml、config.json、manifest.json、unit-library.h5、fields.h5、pillars.csv、plots/ 与本报告共同构成该版本。保留种子、材料校验值、代码文件校验值和依赖版本。通过 CLI 的 run 命令重新运行配置；修改生成独立 ID，旧结果保留。

实验入口接受用户提供的有来源指标测量，保存原始事前预测并生成独立对照记录。没有实测输入时不输出实验数值或校准模型。
"""
    (directory/"report.md").write_text(text,encoding="utf-8")


def run_design(raw, root, progress=None):
    root = Path(root).resolve()
    config = prepare_config(raw)
    run_id = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d-%H%M%S")+"-"+uuid.uuid4().hex[:6]
    directory = root/"runs"/run_id
    directory.mkdir(parents=True)
    plots_dir = directory/"plots"
    plots_dir.mkdir()
    def update(stage, value):
        if progress:
            progress(stage, value)
    update("材料样品与参考工艺筛选", .03)
    catalog = MaterialCatalog()
    substrate = catalog.evaluate("SiO2/Malitson",config["wavelength_nm"],config["assume_k_zero"])
    if not substrate["usable_for_simulation"]:
        raise ValueError("SiO2/Malitson has no reported k. Enable the visible, explicit k=0 assumption for this reference design")
    config["beam"]["medium_n"] = substrate["n"]
    specs = _candidate_specs(config,catalog,substrate)
    settings = RCWASettings(int(config["simulation"]["harmonics"]),int(config["simulation"]["grid_size"]))
    solver_adapter_hash = sha256_file(Path(__file__).parent/"solver.py")
    cache_key = digest({"specs":specs,"substrate":substrate,"settings":vars(settings),"grcwa":version("grcwa"),"platform":__version__,"solver_adapter_sha256":solver_adapter_hash})
    cache = root/"cache"
    cache.mkdir(exist_ok=True)
    library_path = cache/(cache_key+".h5")
    update("真实 RCWA 单元响应库扫描", .08)
    if library_path.exists():
        library = load_library(library_path)
    else:
        counter = [0]
        def scan_progress(message):
            counter[0] += 1
            update("RCWA: "+message,.08+.30*counter[0]/sum(len(s["radii_nm"]) for s in specs))
        library = build_response_library(config["wavelength_nm"],specs,substrate["index"],settings,progress=scan_progress)
        save_library(library_path,library)
    update("联合选材、膜厚、周期与整片结构映射",.40)
    ranking = []
    rejected_candidates = []
    for candidate in library["candidates"]:
        try:
            mapped = _map_candidate(config,candidate)
            ranking.append((mapped["score"],candidate,mapped))
        except ValueError as exc:
            rejected_candidates.append({"candidate_id":candidate["candidate_id"],"reason":str(exc)})
    if not ranking:
        raise ValueError("No scanned candidate can realize a stable layout: "+str(rejected_candidates))
    ranking.sort(key=lambda row:row[0])
    validations = []
    selected = mapped = None
    for _, candidate, preliminary in ranking:
        valid = np.ones(len(candidate["radii_nm"]),bool)
        tested = []
        # Test every admissible design size to allow remapping after exclusions.
        for i in range(len(valid)):
            radius = candidate["radii_nm"][i]
            if radius < candidate["radii_nm"].min()+8 or radius > candidate["radii_nm"].max()-8:
                continue
            update(f"选定候选收敛检查 r={radius:.1f} nm",.46)
            check = validate_convergence(config["wavelength_nm"],candidate["period_nm"],candidate["height_nm"],
                                         float(radius),candidate["pillar_index"],substrate["index"],
                                         [settings,RCWASettings(121,settings.grid_size),RCWASettings(121,192)])
            valid[i] = check["converged"]
            tested.append(check)
        try:
            remapped = _map_candidate(config,candidate,valid)
        except ValueError:
            continue
        selected,mapped,validations = candidate,remapped,tested
        break
    if selected is None:
        raise RuntimeError("No candidate has enough numerically converged usable sizes; increase resolution or change process/geometry")
    used_radii = selected["radii_nm"][mapped["used_indices"]]
    required_checks = [v for v in validations if np.any(np.isclose(v["radius_nm"],used_radii))]
    write_json(directory/"convergence.json",{"all_used_sizes_passed":all(v["converged"] for v in required_checks),
                                               "used_radii_nm":used_radii,"records":validations})
    save_library(directory/"unit-library.h5",library)
    update("整片光场传播与采样检查",.57)
    x,y,aperture = mapped["x"],mapped["y"],mapped["aperture"]
    incident,output = mapped["beam"]["field"],mapped["output"]
    ideal = np.abs(output)*np.exp(1j*target_lens_phase(x,y,config["wavelength_m"],config["focal_length_m"]))
    analysis = analyze_device(incident,output,x,y,config["wavelength_m"],config["focal_length_m"],ideal_transmitted=ideal)
    config["roi_radius_um"] = analysis["metrics"]["roi_radius_um"]
    analysis["metrics"]["strehl_definition"] = "peak per transmitted watt / ideal same spatial pupil amplitudes; component phases replaced by perfect focus phase"
    dx = float(x[1]-x[0])
    padded = np.pad(output,((0,0),(len(y)//2,len(y)//2),(len(x)//2,len(x)//2)))
    padded_focus = angular_spectrum(padded,dx,config["wavelength_m"],config["focal_length_m"])
    high = zoom(output.real,(1,2,2),order=0)+1j*zoom(output.imag,(1,2,2),order=0)
    high_focus = angular_spectrum(high,dx/2,config["wavelength_m"],config["focal_length_m"])
    nominal_peak = analysis["focal_intensity"].max()
    propagation_check = {"method":"same physical pupil: padding increment and piecewise-constant cell resampling",
                         "nominal_dx_nm":dx*1e9,"padding_peak_relative_change":float(abs(intensity(padded_focus).max()/nominal_peak-1)),
                         "half_dx_peak_relative_change":float(abs(intensity(high_focus).max()/nominal_peak-1)),
                         "threshold_relative":.05,"warnings":analysis["warnings"]}
    propagation_check["passed"] = propagation_check["padding_peak_relative_change"]<.05 and propagation_check["half_dx_peak_relative_change"]<.05
    write_json(directory/"propagation-check.json",propagation_check)
    update("关键区域周期超胞与局部近场复核",.65)
    row = int(np.argmin(abs(y)))
    valid_columns = np.flatnonzero(aperture[row])
    gradients = abs(np.diff(mapped["radius_map_nm"][row,valid_columns]))
    point = int(np.argmax(gradients)) if len(gradients) else 1
    start = max(0,min(point-1,len(valid_columns)-3))
    cols = valid_columns[start:start+3]
    local_radii = mapped["radius_map_nm"][row,cols]
    super_a = solve_supercell(config["wavelength_nm"],selected["period_nm"],selected["height_nm"],local_radii,
                            selected["pillar_index"],substrate["index"],RCWASettings(121,128),with_fields=False)
    super_b = solve_supercell(config["wavelength_nm"],selected["period_nm"],selected["height_nm"],local_radii,
                            selected["pillar_index"],substrate["index"],RCWASettings(169,128),with_fields=True)
    # Increase actual supercell resolution if the declared increments fail.
    for higher_order in (225,289,361):
        pd = float(np.max(abs(super_a["power_T"]-super_b["power_T"])))
        ph = float(np.max(abs(np.angle(np.diag(super_a["jones_t"])*np.conj(np.diag(super_b["jones_t"]))))))
        if pd<.02 and ph<.1:
            break
        update(f"周期超胞收敛细化：{higher_order} 谐波请求",.69)
        super_a = super_b
        super_b = solve_supercell(config["wavelength_nm"],selected["period_nm"],selected["height_nm"],local_radii,
                                 selected["pillar_index"],substrate["index"],RCWASettings(higher_order,128),with_fields=True)
    local_actual = super_b["local_jones_t_exit_mean"][:,0,0,0]
    local_lpa = selected["jones_t"][mapped["indices"][row,cols],0,0]
    power_delta = float(np.max(abs(super_a["power_T"]-super_b["power_T"])))
    phase_delta = float(np.max(abs(np.angle(np.diag(super_a["jones_t"])*np.conj(np.diag(super_b["jones_t"]))))))
    coupling = {"metadata":super_b["metadata"],"radii_nm":local_radii,
                "max_local_phase_difference_rad":float(np.max(abs(np.angle(local_actual*np.conj(local_lpa))))),
                "max_local_amplitude_difference":float(np.max(abs(abs(local_actual)-abs(local_lpa)))),
                "numerical_check":{"passed":power_delta<.02 and phase_delta<.1,"power_delta":power_delta,"phase_delta_rad":phase_delta},
                "power_T":super_b["power_T"],"scope":"single periodic 3x1 neighbourhood; local exit means include evanescent fields"}
    write_json(directory/"coupling-check.json",coupling)
    update("真实高度响应与假设制造误差分析",.74)
    height_libs = []
    height_directions = (-1,1) if config["tolerance"]["height_sigma_nm"]>0 else ()
    for direction in height_directions:
        height_spec = dict(selected)
        height_spec["height_nm"] = selected["height_nm"]+direction*3*config["tolerance"]["height_sigma_nm"]
        height_cache = cache/(digest({"height_spec":height_spec,"settings":vars(settings),"substrate":substrate,"version":__version__,"solver_adapter_sha256":solver_adapter_hash})+".h5")
        if height_cache.exists():
            extra = load_library(height_cache)
        else:
            extra = build_response_library(config["wavelength_nm"],[height_spec],substrate["index"],settings)
            save_library(height_cache,extra)
        height_libs.append(extra["candidates"][0])
    tolerance = evaluate_tolerance(config,selected,x,y,aperture,mapped["radius_map_nm"],incident,
                                    height_minus=height_libs[0] if height_libs else None,height_plus=height_libs[1] if height_libs else None)
    write_json(directory/"tolerance.json",tolerance)
    for i,extra in enumerate(height_libs):
        save_library(directory/f"height-{'minus' if i==0 else 'plus'}.h5",{"metadata":library["metadata"],"candidates":[extra]})
    update("GDSII/OASIS 导出与实际多边形回读",.84)
    layout = export_layout(mapped["X"][aperture]*1e9,mapped["Y"][aperture]*1e9,mapped["radius_map_nm"][aperture],directory,
                           min_feature_nm=config["fabrication"]["minimum_width_nm"],min_gap_nm=config["fabrication"]["minimum_gap_nm"],
                           aperture_diameter_nm=config["diameter_um"]*1000)
    write_json(directory/"layout-check.json",layout)
    with (directory/"pillars.csv").open("w",newline="",encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["x_nm","y_nm","radius_nm","height_nm","structure_id"])
        writer.writerows(zip(mapped["X"][aperture]*1e9,mapped["Y"][aperture]*1e9,mapped["radius_map_nm"][aperture],
                             np.full(aperture.sum(),selected["height_nm"]),mapped["indices"][aperture]))
    update("数据、图表与可复现报告输出",.92)
    plots = {}
    def add_plot(key):
        plots[key] = f"plots/{key}.png"
        return directory/plots[key]
    dominant = int(np.argmax(np.sum(abs(incident)**2,axis=(1,2))))
    field_plot(add_plot("input_intensity"),intensity(incident),x*1e6,y*1e6,"Incident field / effective aperture","Reference power density")
    field_plot(add_plot("input_phase"),np.angle(incident[dominant]),x*1e6,y*1e6,"Incident phase","Phase (rad)",phase=True,mask=aperture)
    library_plot(add_plot("library"),library["candidates"],selected)
    field_plot(add_plot("layout"),2*mapped["radius_map_nm"],x*1e6,y*1e6,"Circular pillar diameters / local periodic layout","Diameter (nm)",mask=aperture)
    realized = np.angle(output[dominant]*np.conj(incident[dominant]))
    target = np.angle(np.exp(1j*mapped["target_phase"]))
    error = np.angle(np.exp(1j*(realized-target)))
    for key,data,title in [("target_phase",target,"Target device phase"),("realized_phase",realized,"Mapped device phase"),("phase_error",error,"Wrapped phase residual")]:
        field_plot(add_plot(key),data,x*1e6,y*1e6,title,"Phase (rad)",phase=True,mask=aperture)
    field_plot(add_plot("focus"),analysis["focal_intensity"],x*1e6,y*1e6,"Coherent focal-plane intensity","Reference power density")
    axial_plot(add_plot("xz"),analysis["xz_intensity"],x*1e6,analysis["z_m"]*1e6,"x-z propagation cut")
    axial_plot(add_plot("yz"),analysis["yz_intensity"],y*1e6,analysis["z_m"]*1e6,"y-z propagation cut")
    field_plot(add_plot("far_field"),analysis["far_field_intensity"],analysis["far_field_direction_cosine_x"],analysis["far_field_direction_cosine_y"],"Far-field angular-spectrum density","Relative spectral density",xlabel="Direction cosine u_x",ylabel="Direction cosine u_y")
    fields_near = super_b["fields"][0]["inside_midheight"]
    near_intensity = np.sum(abs(fields_near)**2,axis=0).T
    field_plot(add_plot("near_field"),near_intensity,super_b["x_nm"]/1000,super_b["y_nm"]/1000,"Periodic 3x1 neighbourhood / mid-height field","|E / E_inc|^2 (x polarized)")
    tolerance_plot(add_plot("tolerance"),tolerance)
    convergence_plot(add_plot("convergence"),validations)
    slices = []
    slice_arrays = []
    for z_um in np.linspace(0,1.6*config["focal_length_um"],9):
        value = intensity(angular_spectrum(output,dx,config["wavelength_m"],z_um*1e-6))
        name = f"slice-{len(slices):02d}.png"
        field_plot(plots_dir/name,value,x*1e6,y*1e6,f"Propagation plane z={z_um:.1f} um","Reference power density")
        slices.append({"z_um":float(z_um),"url":"plots/"+name})
        slice_arrays.append(value)
    plots["slices"] = slices
    fields = {"x_m":x,"y_m":y,"aperture_mask":aperture,"incident_power_field":incident,"output_power_field":output,
              "radius_map_nm":mapped["radius_map_nm"],"unit_indices":mapped["indices"],"target_phase_rad":mapped["target_phase"],
              "focal_field":analysis["focal_field"],"focal_intensity":analysis["focal_intensity"],
              "xz_intensity":analysis["xz_intensity"],"yz_intensity":analysis["yz_intensity"],"z_m":analysis["z_m"],
              "near_field_E_over_Einc":fields_near,"near_field_x_nm":super_b["x_nm"],"near_field_y_nm":super_b["y_nm"],
              "slices_intensity":np.asarray(slice_arrays),"far_field_intensity":analysis["far_field_intensity"]}
    save_fields(directory/"fields.h5",fields,{"config":config,"near_field":super_b["metadata"],"far_field_definition":analysis["far_field_definition"],"intensity_units":"W/m2 mathematical power field; normalized input is reference only"})
    write_json(directory/"config.json",config)
    (directory/"config.yaml").write_text(yaml.safe_dump(jsonable(config),allow_unicode=True,sort_keys=False),encoding="utf-8")
    metrics = analysis["metrics"]
    metrics["estimated_focus_um"] = float(analysis["z_m"][np.argmax(np.max(analysis["xz_intensity"],axis=1))]*1e6)
    metrics["absolute_scale_is_measured"] = False
    summaries = [{k:c[k] for k in ("source_id","material_id","n","k","k_used","period_nm","height_nm")}|
                 {"score":score,"mean_transmission":float(np.mean(c["power_T"]))} for score,c,_ in ranking]
    selected_summary = {k:jsonable(v) for k,v in selected.items() if not isinstance(v,np.ndarray) and k!="solver_records"}
    result = {"id":run_id,"created_at":datetime.now(timezone(timedelta(hours=8))).isoformat(),"status":"completed",
              "metrics":metrics,"material_selection":{"selected":selected_summary,"candidates":summaries,"substrate":substrate},
              "computational_methods":{
                  "unit_response":{"method":"direct periodic RCWA","backend":"grcwa","boundary_conditions":"x/y periodic (zero Bloch phase at normal incidence); z homogeneous incoming/outgoing modes; no PML"},
                  "device_field":{"method":"local periodic Jones lookup + band-limited scalar angular spectrum","boundary_conditions":"finite sampled aperture and padded propagation window; not a finite-device PML simulation"},
                  "detector":{"method":"on-demand scalar Rayleigh-Sommerfeld first integral","source":"saved fields.h5 output_power_field; available at POST /api/probe"},
                  "comsol":{"status":"reference only; not connected, invoked, or cross-validated"},
                  "reference_document":"docs/theory-models-and-references.md"},
              "plots":plots,"report_url":"report.md","tolerance_summary":tolerance["summary"]|{"requested_samples":len(tolerance["samples"])},
              "design_objective":mapped["objective"],"rejected_candidates":rejected_candidates,
              "validation":{"unit_selected_sizes_passed":all(v["converged"] for v in required_checks),"propagation":propagation_check,
                            "coupling":coupling["numerical_check"],"layout_passed":True},
              "limitations":["Restricted deterministic natural-language parser; replaceable model interface", "Scalar propagation / normal incidence only", "Periodic supercell check; no finite-device FDTD", "Assumed process statistics; not calibrated yield", "Experimental comparison needs real user measurements"]}
    result["validation"]["local_periodic_model_requires_review"] = coupling["max_local_phase_difference_rad"]>.2 or coupling["max_local_amplitude_difference"]>.15
    result["design_acceptance"] = "checks_passed_with_declared_scope" if propagation_check["passed"] and coupling["numerical_check"]["passed"] and not result["validation"]["local_periodic_model_requires_review"] else "requires_review_of_recorded_model_or_numerical_checks"
    _report(directory,result,config,tolerance,validations,coupling,layout)
    deps = {name:version(name) for name in ("numpy","scipy","grcwa","autograd","gdstk","h5py","PyYAML","matplotlib","threadpoolctl")}
    manifest = {"schema_version":"0.1","id":run_id,"created_at":result["created_at"],"config_sha256":digest(config),
                "library_cache_key":cache_key,"seed":config["seed"],"python":platform.python_version(),"platform_version":__version__,
                "dependencies":deps,"material_database_commit":catalog.catalog.get("database_commit"),"material_catalog_sha256":catalog.sha256,
                "source_file_sha256":{p.name:sha256_file(p) for p in Path(__file__).parent.glob("*.py")},
                "evidence":{"unit_library":"direct periodic RCWA","device_fields":"local-periodic lookup + scalar propagation",
                            "tolerance":"bounded piecewise complex-response interpolation over actual RCWA radius and height libraries","experiment":"not supplied"},
                "files":{p.relative_to(directory).as_posix():sha256_file(p) for p in directory.rglob("*") if p.is_file()}}
    write_json(directory/"manifest.json",manifest)
    result["artifacts"] = [{"name":p.name,"url":p.relative_to(directory).as_posix(),"kind":p.suffix.lstrip(".")} for p in directory.iterdir() if p.is_file()]
    write_json(directory/"result.json",result)
    update("设计完成；报告与版图已保存",1.)
    return jsonable(result)
