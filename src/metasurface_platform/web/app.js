"use strict";

// No CDN or build system: this client talks only to the local Python service.
const $ = (id) => document.getElementById(id);
const DEFAULT_CONFIG = {
  wavelength_nm: 532, focal_length_um: 100, diameter_um: 30, period_nm: 300,
  material: { preferred: "auto" }, assume_k_zero: true,
  beam: { type: "gaussian", normalization: "normalized", value: 1, waist_um: 12,
    waist_z_um: 0, center_x_um: 0, center_y_um: 0, theta_deg: 0, phi_deg: 0,
    polarization: { type: "linear", angle_deg: 0, ellipticity_deg: 0 } },
  simulation: { seed: 42, tolerance_samples: 12 },
  tolerance: { samples: 12, common_diameter_sigma_nm: 4, correlated_diameter_sigma_nm: 2,
    local_diameter_sigma_nm: 2, correlation_length_um: 3, height_sigma_nm: 8, threshold_fraction: .9 }
};
let currentConfig = structuredClone(DEFAULT_CONFIG);
let serviceStatus = null;
let activeJob = null;
let pollTimer = null;
let latestResult = null;
let previewSequence = 0;
let busy = false;
let requestSequence = 0;
let propagationSlices = [];
let resultConfigSignature = null;
let submittedConfig = null;
let previewConfigSignature = null;
let sceneMode = "structure";
let camera = { yaw: -0.55, pitch: 0.45, zoom: 1, panX: 0, panY: 0 };
let canvasSize = { width: 0, height: 0, ratio: 1 };
let pointerState = null;
let detectorDragEnabled = false;
let geometryRows = null;
let fieldTextures = [];
let probeImage = null;
let currentDetectorResult = null;
let agentStatus = null;

function beamSignature(config) {
  const beam = { ...config.beam };
  delete beam.medium_n; // The backend derives the substrate index from wavelength.
  return JSON.stringify({ wavelength_nm: config.wavelength_nm, diameter_um: config.diameter_um, beam });
}

function setMessage(message, kind = "") {
  $("message").textContent = message;
  $("message").className = `message ${kind}`;
  if (!message) $("message").classList.add("hidden");
}

async function api(path, body) {
  const response = await fetch(path, body === undefined ? { cache: "no-store" } : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
  });
  let data;
  try { data = await response.json(); }
  catch { throw new Error(`计算服务未返回 JSON（HTTP ${response.status}）。请检查服务日志。`); }
  if (!response.ok || (data.error && !data.id)) {
    const detail = data.error;
    throw new Error(typeof detail === "string" ? detail : detail?.message || data.message || `请求失败：HTTP ${response.status}`);
  }
  return data;
}

function numberValue(id, fallback = 0) {
  const value = Number($(id).value);
  return Number.isFinite(value) ? value : fallback;
}

function collectConfig() {
  const config = structuredClone(currentConfig);
  config.wavelength_nm = numberValue("wavelength", 532);
  config.focal_length_um = numberValue("focal-length", 100);
  config.diameter_um = numberValue("diameter", 30);
  config.period_nm = currentConfig.period_nm || 300;
  // SI values and derived limits in an archived normalized config belong to
  // that version. The server must recompute them from the edited user units.
  for (const key of ["wavelength_m", "diameter_m", "focal_length_m", "numerical_aperture", "first_release_limits"]) delete config[key];
  config.material = { ...config.material, preferred: $("material").value };
  config.assume_k_zero = $("assume-k-zero").checked;
  const normalization = $("normalization").value;
  const beamType = document.querySelector('input[name="beam-type"]:checked')?.value || "gaussian";
  config.beam = { ...config.beam, type: beamType === "plane_wave" ? "plane" : beamType,
    normalization, value: normalization === "peak_intensity" ? numberValue("peak-intensity", 1000)
      : ["beam_power", "aperture_power"].includes(normalization) ? numberValue("power", 1) / 1000 : 1,
    waist_um: numberValue("waist", 12), waist_z_um: numberValue("waist-z"),
    theta_deg: numberValue("theta"), phi_deg: numberValue("phi"),
    center_x_um: numberValue("center-x"), center_y_um: numberValue("center-y"),
    polarization: { type: $("polarization").value, angle_deg: numberValue("polarization-angle"),
      ellipticity_deg: numberValue("ellipticity") }
  };
  config.simulation = { ...config.simulation, seed: numberValue("seed", 42),
    tolerance_samples: numberValue("tolerance-samples", 12), harmonics: numberValue("harmonics", 81),
    grid_size: numberValue("grid-size", 128), propagation_padding: numberValue("propagation-padding", 3) };
  config.seed = config.simulation.seed;
  // Tolerance controls use the actual backend's diameter-error convention.
  const errorFields = {
    common_diameter_sigma_nm: "radius-bias", local_diameter_sigma_nm: "radius-noise",
    height_sigma_nm: "height-bias", correlated_diameter_sigma_nm: "correlated-error",
    correlation_length_um: "correlation-length"
  };
  config.tolerance = { ...DEFAULT_CONFIG.tolerance, ...config.tolerance, samples: config.simulation.tolerance_samples };
  for (const [key, id] of Object.entries(errorFields)) {
    config.tolerance[key] = numberValue(id);
  }
  config.detector = collectDetectorConfig();
  return config;
}

function collectDetectorConfig() {
  const requestedResolution = Number($("probe-resolution").value);
  return {
    center_x_um: numberValue("probe-x"), center_y_um: numberValue("probe-y"), center_z_um: numberValue("probe-z", numberValue("focal-length", 100)),
    normal_theta_deg: numberValue("probe-theta"), normal_phi_deg: numberValue("probe-phi"),
    width_um: numberValue("probe-width", numberValue("diameter", 30)), height_um: numberValue("probe-height", numberValue("diameter", 30)),
    roll_deg: numberValue("probe-roll"), resolution: Math.max(2, Math.min(128, Number.isFinite(requestedResolution) && requestedResolution > 0 ? Math.round(requestedResolution) : 128))
  };
}

function applyConfig(config) {
  currentConfig = { ...structuredClone(DEFAULT_CONFIG), ...structuredClone(config),
    beam: { ...DEFAULT_CONFIG.beam, ...config?.beam,
      polarization: { ...DEFAULT_CONFIG.beam.polarization, ...config?.beam?.polarization } },
    simulation: { ...DEFAULT_CONFIG.simulation, ...config?.simulation },
    material: { ...DEFAULT_CONFIG.material, ...config?.material },
    tolerance: { ...DEFAULT_CONFIG.tolerance, ...config?.tolerance } };
  if (config?.seed !== undefined && config?.simulation?.seed === undefined) currentConfig.simulation.seed = config.seed;
  if (config?.tolerance?.samples !== undefined && config?.simulation?.tolerance_samples === undefined) currentConfig.simulation.tolerance_samples = config.tolerance.samples;
  const entries = {
    wavelength: currentConfig.wavelength_nm, "focal-length": currentConfig.focal_length_um,
    diameter: currentConfig.diameter_um, period: currentConfig.period_nm,
    waist: currentConfig.beam.waist_um, "waist-z": currentConfig.beam.waist_z_um,
    theta: currentConfig.beam.theta_deg, phi: currentConfig.beam.phi_deg,
    "center-x": currentConfig.beam.center_x_um, "center-y": currentConfig.beam.center_y_um,
    "polarization-angle": currentConfig.beam.polarization.angle_deg,
    ellipticity: currentConfig.beam.polarization.ellipticity_deg,
    seed: currentConfig.simulation.seed, "tolerance-samples": currentConfig.simulation.tolerance_samples,
    harmonics: currentConfig.simulation.harmonics, "grid-size": currentConfig.simulation.grid_size,
    "propagation-padding": currentConfig.simulation.propagation_padding
  };
  for (const [id, value] of Object.entries(entries)) if (value !== undefined) $(id).value = value;
  $("material").value = currentConfig.material.preferred;
  if (!$("material").value) $("material").value = "auto";
  $("normalization").value = currentConfig.beam.normalization;
  $("polarization").value = currentConfig.beam.polarization.type;
  $("assume-k-zero").checked = currentConfig.assume_k_zero;
  const radioValue = currentConfig.beam.type === "plane_wave" ? "plane" : currentConfig.beam.type;
  const radio = document.querySelector(`input[name="beam-type"][value="${radioValue === "gaussian" ? "gaussian" : "plane"}"]`);
  if (radio) radio.checked = true;
  if (currentConfig.beam.normalization === "peak_intensity") $("peak-intensity").value = currentConfig.beam.value;
  if (["beam_power", "aperture_power"].includes(currentConfig.beam.normalization)) $("power").value = currentConfig.beam.value * 1000;
  for (const [key, id] of Object.entries({ common_diameter_sigma_nm: "radius-bias", local_diameter_sigma_nm: "radius-noise", height_sigma_nm: "height-bias", correlated_diameter_sigma_nm: "correlated-error", correlation_length_um: "correlation-length" })) {
    if (currentConfig.tolerance?.[key] !== undefined) $(id).value = currentConfig.tolerance[key];
    $(id).disabled = currentConfig.tolerance?.[key] === undefined;
  }
  const detector = currentConfig.detector || {};
  const detectorValues = { "probe-x": detector.center_x_um ?? 0, "probe-y": detector.center_y_um ?? 0,
    "probe-z": detector.center_z_um ?? currentConfig.focal_length_um, "probe-theta": detector.normal_theta_deg ?? 0,
    "probe-phi": detector.normal_phi_deg ?? 0, "probe-width": detector.width_um ?? currentConfig.diameter_um,
    "probe-height": detector.height_um ?? currentConfig.diameter_um, "probe-roll": detector.roll_deg ?? 0,
    "probe-resolution": Math.min(128, detector.resolution ?? 128) };
  for (const [id, value] of Object.entries(detectorValues)) { if ($(id)) $(id).value = value; }
  updateControls();
}

function showTab(kind, name) {
  document.querySelectorAll(`[data-${kind}-tab]`).forEach((button) => {
    const selected = button.dataset[`${kind}Tab`] === name;
    button.classList.toggle("selected", selected);
    button.setAttribute("aria-selected", String(selected));
  });
  document.querySelectorAll(`[data-${kind}-section]`).forEach((section) => {
    section.classList.toggle("hidden", section.dataset[`${kind}Section`] !== name);
  });
  if (kind === "config") {
    const pageName = name === "compute" ? "simulation" : "parameters";
    document.querySelectorAll("[data-sidebar-page]").forEach((page) => page.classList.toggle("hidden", page.dataset.sidebarPage !== pageName));
    document.querySelectorAll("[data-sidebar-tab]").forEach((button) => {
      const selected = button.dataset.sidebarTab === pageName;
      button.classList.toggle("selected", selected); button.setAttribute("aria-selected", String(selected));
    });
    if (name === "compute") document.querySelectorAll('[data-config-tab="compute"]').forEach((button) => button.classList.add("selected"));
  }
}

function showSidebarPage(name) {
  document.querySelectorAll("[data-sidebar-page]").forEach((page) => page.classList.toggle("hidden", page.dataset.sidebarPage !== name));
  document.querySelectorAll("[data-sidebar-tab]").forEach((button) => {
    const selected = button.dataset.sidebarTab === name;
    button.classList.toggle("selected", selected);
    button.setAttribute("aria-selected", String(selected));
  });
}

function updateControls() {
  const config = collectConfig();
  const gaussian = config.beam.type === "gaussian";
  document.querySelectorAll(".gaussian-field").forEach((element) => element.classList.toggle("hidden", !gaussian));
  $("intensity-field").classList.toggle("hidden", config.beam.normalization !== "peak_intensity");
  $("power-field").classList.toggle("hidden", !["beam_power", "aperture_power"].includes(config.beam.normalization));
  $("ellipticity-field").classList.toggle("hidden", config.beam.polarization.type !== "elliptical");
  $("polarization-angle-field").classList.toggle("hidden", ["left_circular", "right_circular"].includes(config.beam.polarization.type));
  $("beam-definition").textContent = gaussian
    ? "w₀ 是强度降至峰值 1/e² 处的半径；器件为 z = 0，正 z₀ 表示束腰位于器件下游。"
    : "平面波无有限总功率；功率输入采用器件口径内功率。倾斜入射可预览，完整设计需要对应入射角的响应库。";
  const powerOption = $("normalization").querySelector('[value="beam_power"]');
  if (powerOption) powerOption.disabled = !gaussian;
  if (!gaussian && config.beam.normalization === "beam_power") { $("normalization").value = "aperture_power"; return updateControls(); }
  const count = Math.PI * (config.diameter_um * 1000 / 2 / config.period_nm) ** 2;
  $("scale-note").classList.toggle("large", count > 100000 || config.beam.theta_deg !== 0);
  $("scale-note").textContent = config.beam.theta_deg !== 0
    ? "当前单元库为法向入射；非零倾角仅支持光场预览。"
    : count > 100000 ? `当前估计 ${formatNumber(count, 0)} 个单元。大规模任务需缩小口径或增加周期。`
      : `约 ${formatNumber(count, 0)} 个单元 · 本地微米级器件示例`;
  $("changed-note").classList.toggle("hidden", !resultConfigSignature || JSON.stringify(config) === resultConfigSignature);
  $("preview-changed").classList.toggle("hidden", !previewConfigSignature || beamSignature(config) === previewConfigSignature);
  updatePolarization(config.beam.polarization);
  updateBeamFacts(config.beam);
  updateMaterialLegend();
  updateDetectorSummary();
  drawScene();
}

function formatNumber(value, decimals = 3) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
  return Number(value).toLocaleString("zh-CN", { maximumFractionDigits: decimals });
}

function formatOpticalConstant(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return number !== 0 && Math.abs(number) < 0.0001 ? number.toExponential(3) : formatNumber(number, 6);
}

function updatePolarization(polarization) {
  const labels = { linear: "线偏振", left_circular: "左圆偏振", right_circular: "右圆偏振", elliptical: "椭圆偏振" };
  const circular = ["left_circular", "right_circular"].includes(polarization.type);
  const angle = circular ? 0 : polarization.angle_deg;
  const ratio = polarization.type === "linear" ? .02 : circular ? 1 : Math.abs(Math.tan(polarization.ellipticity_deg * Math.PI / 180));
  const ellipse = $("polarization-ellipse");
  ellipse.setAttribute("ry", String(36 * ratio));
  ellipse.setAttribute("transform", `rotate(${-angle} 60 50)`);
  $("polarization-title").textContent = `${labels[polarization.type] || polarization.type}${circular ? "" : ` · ${formatNumber(angle, 1)}°`}`;
}

function updateBeamFacts(beam) {
  const rows = [ ["光束", beam.type === "gaussian" ? "高斯基模" : "平面波"],
    [beam.type === "gaussian" ? "束腰" : "波长", beam.type === "gaussian" ? `${formatNumber(beam.waist_um)} μm` : `${$("wavelength").value} nm`],
    ["入射", beam.theta_deg === 0 && beam.center_x_um === 0 && beam.center_y_um === 0 ? "法向 · 居中" : `θ ${formatNumber(beam.theta_deg, 1)}° · x/y ${formatNumber(beam.center_x_um, 1)}/${formatNumber(beam.center_y_um, 1)} μm`],
    ["归一化", beam.normalization === "normalized" ? "口径内 1 W · 数学参考" : beam.normalization === "peak_intensity" ? `${formatNumber(beam.value)} W/m²` : `${formatNumber(beam.value * 1000)} mW${beam.normalization === "aperture_power" ? " · 口径内" : " · 全光束"}`] ];
  $("beam-facts").replaceChildren(...rows.map(([label, value]) => {
    const row = document.createElement("div");
    const dt = document.createElement("dt"); dt.textContent = label;
    const dd = document.createElement("dd"); dd.textContent = value;
    row.append(dt, dd); return row;
  }));
}

function updateMaterialLegend() {
  const name = $("material")?.selectedOptions?.[0]?.textContent || "候选介质柱";
  if ($("pillar-legend-name")) $("pillar-legend-name").textContent = name.replace("自动筛选 · ", "") || "候选介质柱";
}

function updateDetectorSummary() {
  const detector = collectDetectorConfig();
  if ($("probe-summary-text")) $("probe-summary-text").textContent = `中心 (${formatNumber(detector.center_x_um, 1)}, ${formatNumber(detector.center_y_um, 1)}, ${formatNumber(detector.center_z_um, 1)}) μm · θ ${formatNumber(detector.normal_theta_deg, 1)}° · ${detector.width_um} × ${detector.height_um} μm`;
  if ($("probe-state") && !currentDetectorResult) { $("probe-state").className = "probe-state"; $("probe-state").lastChild.textContent = "截面数据待计算"; }
}

function resizeSceneCanvas() {
  const canvas = $("scene-canvas");
  if (!canvas) return;
  const box = canvas.getBoundingClientRect();
  const ratio = Math.max(1, Math.min(2, window.devicePixelRatio || 1));
  canvas.width = Math.max(1, Math.round(box.width * ratio)); canvas.height = Math.max(1, Math.round(box.height * ratio));
  canvasSize = { width: box.width, height: box.height, ratio };
  drawScene();
}

function projectPoint(x, y, z, scale = 1) {
  const { width, height } = canvasSize;
  const yaw = camera.yaw, pitch = camera.pitch;
  const cy = Math.cos(yaw), sy = Math.sin(yaw), cp = Math.cos(pitch), sp = Math.sin(pitch);
  const ax = x * cy - y * sy, ay = x * sy + y * cy;
  const depth = ay * sp + z * cp, vertical = ay * cp - z * sp;
  return { x: width / 2 + (ax * scale * camera.zoom) + camera.panX, y: height / 2 - (vertical * scale * camera.zoom) + camera.panY, depth };
}

function roundedRect(ctx, x, y, w, h, r) {
  const radius = Math.min(r, Math.abs(w) / 2, Math.abs(h) / 2);
  ctx.beginPath(); ctx.moveTo(x + radius, y); ctx.arcTo(x + w, y, x + w, y + h, radius); ctx.arcTo(x + w, y + h, x, y + h, radius); ctx.arcTo(x, y + h, x, y, radius); ctx.arcTo(x, y, x + w, y, radius); ctx.closePath();
}

function drawMaterialLegend(entries) {
  const legend = $("canvas-legend"); if (!legend) return;
  legend.replaceChildren(...entries.map((entry) => {
    const row = document.createElement("div"); row.className = "legend-entry";
    row.style.setProperty("--swatch", entry.color);
    const swatch = document.createElement("span"); swatch.className = "legend-swatch";
    const label = document.createElement("span"); label.textContent = entry.label;
    const detail = document.createElement("small"); detail.textContent = entry.detail || "";
    row.append(swatch, label, detail); return row;
  }));
}

function drawScene() {
  const canvas = $("scene-canvas"); if (!canvas || !canvasSize.width) return;
  const ctx = canvas.getContext("2d"); const ratio = canvasSize.ratio;
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0); ctx.clearRect(0, 0, canvasSize.width, canvasSize.height);
  const config = collectConfig();
  const diameter = Number(config.diameter_um) || 30;
  const opticalZScale = Math.max(.004, Math.min(.2, diameter * .2 / Math.max(diameter, Number(config.focal_length_um) || 100)));
  const pillarHeight = Math.max(1, diameter * .05 / .6);
  const baseScale = Math.min(canvasSize.width, canvasSize.height) / Math.max(1, diameter * 1.7);
  const scale = baseScale * (sceneMode === "beam" ? .92 : 1);
  const substrate = 3;
  const plane = (z, width, height, color, alpha = 1, dash = false) => {
    const corners = [[-width / 2, -height / 2, z], [width / 2, -height / 2, z], [width / 2, height / 2, z], [-width / 2, height / 2, z]].map((p) => projectPoint(...p, scale));
    ctx.save(); ctx.globalAlpha = alpha; ctx.fillStyle = color; ctx.strokeStyle = color; ctx.lineWidth = 1; if (dash) ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(corners[0].x, corners[0].y); corners.slice(1).forEach((p) => ctx.lineTo(p.x, p.y)); ctx.closePath(); ctx.fill(); ctx.stroke(); ctx.restore();
  };
  plane(0, diameter * 1.2, diameter * 1.2, "#4c8399", .18);
  const rows = Array.isArray(geometryRows) ? geometryRows : null;
  const materialName = latestResult?.material_selection?.selected?.material_family || $("material")?.value || "候选介质柱";
  const pillarColor = materialName === "TiO2" ? "#d59c62" : materialName === "Si3N4" ? "#79b4bd" : "#ab9678";
  const pillarCount = rows?.length || 64;
  const drawn = rows ? (() => {
    const stride = Math.max(1, Math.ceil(rows.length / 450));
    return rows.filter((_, index) => index % stride === 0).slice(0, 450);
  })() : Array.from({ length: pillarCount }, (_, i) => {
    const n = 9, xi = i % n - (n - 1) / 2, yi = Math.floor(i / n) - (n - 1) / 2;
    return { x_nm: xi * diameter * 1000 / n, y_nm: yi * diameter * 1000 / n, radius_nm: 42, height_nm: 220 };
  }).filter((row) => Math.hypot(row.x_nm, row.y_nm) <= diameter * 500);
  const sorted = drawn.map((row) => {
    const x = Number(row.x_nm || 0) / 1000, y = Number(row.y_nm || 0) / 1000, z = Number(row.height_nm || 0) / 1000 * pillarHeight;
    return { row, x, y, z, point: projectPoint(x, y, z, scale), base: projectPoint(x, y, .08, scale) };
  }).sort((a, b) => a.point.depth - b.point.depth);
  for (const item of sorted) {
    const r = Math.max(1.3, Number(item.row.radius_nm || 30) / 1000 * scale * 1.25);
    const dx = item.point.x - item.base.x, dy = item.point.y - item.base.y, length = Math.hypot(dx, dy);
    const nx = length ? -dy / length * r : r, ny = length ? dx / length * r : 0;
    ctx.save(); ctx.fillStyle = pillarColor; ctx.globalAlpha = rows ? .88 : .5; ctx.strokeStyle = rows ? "#f1c58b" : "#d2b891"; ctx.lineWidth = .6;
    ctx.beginPath(); ctx.moveTo(item.base.x - nx, item.base.y - ny); ctx.lineTo(item.base.x + nx, item.base.y + ny); ctx.lineTo(item.point.x + nx, item.point.y + ny); ctx.lineTo(item.point.x - nx, item.point.y - ny); ctx.closePath(); ctx.fill(); ctx.stroke();
    ctx.beginPath(); ctx.ellipse(item.point.x, item.point.y, r, Math.max(1, r * .42), 0, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
    ctx.restore();
  }
  if (sceneMode === "beam") drawBeam(ctx, scale, diameter);
  if (sceneMode === "result" && fieldTextures.length) drawFieldPlanes(ctx, scale, diameter, opticalZScale);
  drawDetector(ctx, scale, diameter, opticalZScale);
  const labels = [{ color: "#4c8399", label: "熔融石英 / SiO₂ 基底", detail: "基底" }, { color: pillarColor, label: materialName === "auto" ? "候选介质柱" : materialName, detail: rows ? "运行版图 · pillars.csv" : "几何示意" }];
  if (sceneMode === "beam") labels.push({ color: "#61d5c6", label: config.beam.type === "gaussian" ? "高斯入射光场" : "平面波入射光场", detail: "解析三维示意" });
  if (sceneMode === "result" && fieldTextures.length) labels.push({ color: "#e178a0", label: "计算场截面", detail: "实际运行切片" });
  drawMaterialLegend(labels);
  if ($("canvas-scale-note")) {
    const sampling = rows && rows.length > 450 ? `均匀显示 450/${rows.length.toLocaleString("zh-CN")}` : rows ? `显示 ${rows.length.toLocaleString("zh-CN")}` : "示意阵列";
    $("canvas-scale-note").textContent = `${rows ? "计算版图" : "几何示意"} · ${sampling} 个单元 · z 压缩 / 柱高夸大显示 · ${materialName}`;
  }
}

function drawBeam(ctx, scale, diameter) {
  const config = collectConfig(), beam = config.beam, gaussian = beam.type === "gaussian";
  const theta = Number(beam.theta_deg || 0) * Math.PI / 180, phi = Number(beam.phi_deg || 0) * Math.PI / 180;
  const span = Math.max(5, diameter * .42), cx = Number(beam.center_x_um || 0), cy = Number(beam.center_y_um || 0);
  const k = [Math.sin(theta) * Math.cos(phi), Math.sin(theta) * Math.sin(phi), Math.cos(theta)];
  const uBasis = [Math.cos(theta) * Math.cos(phi), Math.cos(theta) * Math.sin(phi), -Math.sin(theta)];
  const vBasis = [-Math.sin(phi), Math.cos(phi), 0];
  const waist = Number(beam.waist_um || 12), waistZ = Number(beam.waist_z_um || 0), wavelengthUm = Number(config.wavelength_nm || 532) / 1000;
  const rayleigh = Math.PI * waist * waist / Math.max(.01, wavelengthUm);
  const radiusAt = (z) => gaussian ? Math.min(diameter * .68, waist * Math.sqrt(1 + ((z - waistZ) / rayleigh) ** 2) * 2) : diameter * .52;
  const atSource = radiusAt(-span), atDevice = radiusAt(0), grid = [-1, -.5, 0, .5, 1];
  const sourceCenter = [cx - (k[0] / Math.max(.05, k[2])) * span, cy - (k[1] / Math.max(.05, k[2])) * span, -span];
  const deviceCenter = [cx, cy, 0];
  ctx.save(); ctx.lineWidth = gaussian ? 1.1 : .9;
  for (const a of grid) for (const b of grid) {
    const radial = Math.hypot(a, b); if (radial > 1.001) continue;
    const alpha = gaussian ? Math.max(.08, Math.exp(-2 * radial * radial) * .7) : .5;
    const source = [0, 1, 2].map((j) => sourceCenter[j] + atSource * (a * uBasis[j] + b * vBasis[j]));
    const device = [0, 1, 2].map((j) => deviceCenter[j] + atDevice * (a * uBasis[j] + b * vBasis[j]));
    const p0 = projectPoint(source[0], source[1], source[2], scale), p1 = projectPoint(device[0], device[1], device[2], scale);
    ctx.strokeStyle = `rgba(102,220,205,${alpha})`; ctx.beginPath(); ctx.moveTo(p0.x, p0.y); ctx.lineTo(p1.x, p1.y); ctx.stroke();
  }
  const waistAt = Math.max(-span, Math.min(0, waistZ));
  const beamCenter = [cx + (k[0] / Math.max(.05, k[2])) * waistAt, cy + (k[1] / Math.max(.05, k[2])) * waistAt, waistAt];
  const center = projectPoint(...beamCenter, scale), glowRadius = Math.max(10, radiusAt(waistAt) * scale * .34);
  if (gaussian) {
    const glow = ctx.createRadialGradient(center.x, center.y, 1, center.x, center.y, glowRadius); glow.addColorStop(0, "rgba(107,230,211,.27)"); glow.addColorStop(1, "rgba(107,230,211,0)"); ctx.fillStyle = glow; ctx.beginPath(); ctx.arc(center.x, center.y, glowRadius, 0, Math.PI * 2); ctx.fill();
  }
  const angle = Number(beam.polarization?.angle_deg || 0) * Math.PI / 180;
  const electricEnd = projectPoint(beamCenter[0] + Math.cos(angle) * 1.8, beamCenter[1] + Math.sin(angle) * 1.8, beamCenter[2], scale);
  ctx.strokeStyle = "#d4fff2"; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(center.x - 6, center.y - 6); ctx.lineTo(electricEnd.x, electricEnd.y); ctx.stroke();
  ctx.restore();
}

function drawFieldPlanes(ctx, scale, diameter, opticalZScale) {
  for (const texture of fieldTextures.slice(0, 2)) {
    if (!texture.image?.complete || !texture.image.naturalWidth) continue;
    const z = Number(texture.z_um || numberValue("focal-length", 100)); const w = Number(texture.width_um || diameter), h = Number(texture.height_um || diameter);
    const planeZ = z * opticalZScale;
    const corners = [[-w / 2, -h / 2, planeZ], [w / 2, -h / 2, planeZ], [w / 2, h / 2, planeZ], [-w / 2, h / 2, planeZ]].map((p) => projectPoint(...p, scale));
    const minX = Math.min(...corners.map((p) => p.x)), maxX = Math.max(...corners.map((p) => p.x)), minY = Math.min(...corners.map((p) => p.y)), maxY = Math.max(...corners.map((p) => p.y));
    ctx.save(); ctx.globalAlpha = .48; ctx.drawImage(texture.image, minX, minY, maxX - minX, maxY - minY); ctx.strokeStyle = "#e178a0"; ctx.setLineDash([4, 3]); ctx.strokeRect(minX, minY, maxX - minX, maxY - minY); ctx.restore();
  }
}

function drawDetector(ctx, scale, diameter, opticalZScale) {
  const d = collectDetectorConfig(); const theta = d.normal_theta_deg * Math.PI / 180, phi = d.normal_phi_deg * Math.PI / 180;
  const normal = [Math.sin(theta) * Math.cos(phi), Math.sin(theta) * Math.sin(phi), Math.cos(theta)];
  const reference = Math.abs(normal[0]) < .9 ? [1, 0, 0] : [0, 1, 0];
  const dot = reference[0] * normal[0] + reference[1] * normal[1] + reference[2] * normal[2];
  let e1 = [reference[0] - dot * normal[0], reference[1] - dot * normal[1], reference[2] - dot * normal[2]];
  const norm = Math.hypot(...e1); e1 = e1.map((value) => value / norm);
  let e2 = [normal[1] * e1[2] - normal[2] * e1[1], normal[2] * e1[0] - normal[0] * e1[2], normal[0] * e1[1] - normal[1] * e1[0]];
  const roll = d.roll_deg * Math.PI / 180, croll = Math.cos(roll), sroll = Math.sin(roll);
  const e1r = e1.map((value, index) => croll * value + sroll * e2[index]); const e2r = e2.map((value, index) => -sroll * e1[index] + croll * value);
  const center = [d.center_x_um, d.center_y_um, d.center_z_um];
  const corners = [[-1, -1], [1, -1], [1, 1], [-1, 1]].map(([u, v]) => projectPoint(center[0] + u * d.width_um * e1r[0] / 2 + v * d.height_um * e2r[0] / 2, center[1] + u * d.width_um * e1r[1] / 2 + v * d.height_um * e2r[1] / 2, (center[2] + u * d.width_um * e1r[2] / 2 + v * d.height_um * e2r[2] / 2) * opticalZScale, scale));
  ctx.save(); ctx.globalAlpha = detectorDragEnabled ? .75 : .48; ctx.strokeStyle = currentDetectorResult ? "#e7a2c1" : "#aeb8c1"; ctx.fillStyle = currentDetectorResult ? "rgba(231,162,193,.16)" : "rgba(174,184,193,.11)"; ctx.setLineDash([5, 3]); ctx.beginPath(); ctx.moveTo(corners[0].x, corners[0].y); corners.slice(1).forEach((point) => ctx.lineTo(point.x, point.y)); ctx.closePath(); ctx.fill(); ctx.stroke(); ctx.setLineDash([]); const cp = projectPoint(center[0], center[1], center[2] * opticalZScale, scale); ctx.fillStyle = "#e7a2c1"; ctx.beginPath(); ctx.arc(cp.x, cp.y, 3, 0, Math.PI * 2); ctx.fill(); ctx.restore();
}

function loadGeometryForResult(result, id) {
  geometryRows = null;
  const runId = result?.id || id; if (!runId) { drawScene(); return; }
  const url = `/runs/${encodeURIComponent(runId)}/pillars.csv`;
  fetch(url).then((response) => response.ok ? response.text() : "").then((text) => {
    if (!text) return;
    const lines = text.trim().split(/\r?\n/); const headers = lines.shift().split(",");
    geometryRows = lines.map((line) => { const values = line.split(","); return Object.fromEntries(headers.map((key, index) => [key, Number(values[index]) || 0])); });
    drawScene();
  }).catch(() => {});
}

function loadFieldTextures(plots = {}) {
  fieldTextures = [];
  const slices = Array.isArray(plots.slices) ? plots.slices : [];
  const values = slices.length ? [slices[Math.floor(slices.length / 2)]] : [plots.focus, plots.input_intensity].filter(Boolean);
  values.forEach((plot) => {
    const url = safeLocalUrl(plot?.url || plot); if (!url) return;
    const image = new Image(); image.onload = () => drawScene(); image.src = url; fieldTextures.push({ image, z_um: plot?.z_um || numberValue("focal-length", 100), width_um: numberValue("diameter", 30), height_um: numberValue("diameter", 30) });
  });
  if (sceneMode === "result") {
    $("scene-title").textContent = fieldTextures.length ? "计算场结果视图" : "等待实际运行切片";
    $("scene-state-tag").textContent = fieldTextures.length ? "COMPUTED RESULT" : "NO FIELD DATA";
  }
}

function safeLocalUrl(value) {
  if (typeof value !== "string") return null;
  try { const url = new URL(value, location.origin); return url.origin === location.origin && ["http:", "https:"].includes(url.protocol) ? url.href : null; }
  catch { return null; }
}

function renderPlot(id, plot) {
  const url = safeLocalUrl(typeof plot === "object" && plot ? plot.url || plot.path : plot);
  if (!url) return;
  const container = $(id);
  const link = document.createElement("a"); link.href = url; link.target = "_blank"; link.rel = "noopener";
  const img = document.createElement("img"); img.src = url; img.alt = container.closest("article")?.querySelector("h3")?.textContent || "计算结果";
  // These plots are the primary numerical evidence in the workbench.  They
  // can live inside a collapsed <details> panel, where browser lazy loading
  // is allowed to defer them indefinitely; load them eagerly so the result is
  // available as soon as the API response arrives.
  img.loading = "eager";
  img.addEventListener("error", () => { container.replaceChildren(); const p = document.createElement("p"); p.className = "field-help"; p.textContent = "结果图加载失败。请从“版图与报告”查看输出文件。"; container.append(p); });
  link.append(img); container.replaceChildren(link);
}

function renderPlots(plots = {}) {
  const mapping = {
    "plot-input-intensity": ["input_intensity", "incident_intensity", "beam_intensity"],
    "plot-input-phase": ["input_phase", "incident_phase", "beam_phase"],
    "plot-library": ["library", "unit_response", "library_response", "response"],
    "plot-layout": ["layout", "radius_map", "geometry"],
    "plot-target-phase": ["target_phase", "phase_target"],
    "plot-phase-error": ["phase_error"],
    "plot-convergence": ["convergence"],
    "plot-realized-phase": ["realized_phase", "phase_realized", "mapped_phase"],
    "plot-focus": ["focus", "focal_intensity", "focus_intensity", "psf"],
    "plot-xz": ["xz", "propagation", "propagation_xz"],
    "plot-slices": ["axial", "linecuts", "axial_profile"],
    "plot-yz": ["yz"],
    "plot-far-field": ["far_field"],
    "plot-near-field": ["near_field"],
    "plot-tolerance": ["tolerance", "tolerance_distribution", "error_distribution"]
  };
  for (const [id, aliases] of Object.entries(mapping)) {
    const key = aliases.find((name) => plots[name]);
    if (key) renderPlot(id, plots[key]);
  }
  if (Array.isArray(plots.slices) && plots.slices.length) {
    propagationSlices = plots.slices;
    $("slice-control").classList.remove("hidden");
    $("slice-slider").max = String(plots.slices.length - 1);
    const focal = numberValue("focal-length", 100);
    let closest = 0;
    propagationSlices.forEach((slice, index) => {
      if (Math.abs(slice.z_um - focal) < Math.abs(propagationSlices[closest].z_um - focal)) closest = index;
    });
    $("slice-slider").value = String(closest);
    $("slice-min").textContent = `${formatNumber(plots.slices[0].z_um, 1)} μm`;
    $("slice-max").textContent = `${formatNumber(plots.slices.at(-1).z_um, 1)} μm`;
    renderSlice(closest);
  } else if (plots.slices) renderPlot("plot-slices", plots.slices);
}

function renderSlice(index) {
  const slice = propagationSlices[index];
  if (!slice) return;
  renderPlot("plot-slices", slice.url);
  $("slice-position").textContent = `z = ${formatNumber(slice.z_um, 2)} μm`;
}

function renderDefaults(defaults) {
  const note = $("defaults-note").lastElementChild;
  if (!defaults || (Array.isArray(defaults) && !defaults.length) || (!Array.isArray(defaults) && !Object.keys(defaults).length)) {
    note.textContent = "参数已解析；未提供项使用当前首版配置。请检查目标器件和入射光束参数。"; return;
  }
  const labels = { wavelength_nm: "工作波长 nm", focal_length_um: "焦距 μm", diameter_um: "口径 μm", period_nm: "周期 nm", "beam.type": "光束类型", seed: "随机种子" };
  const items = Array.isArray(defaults) ? defaults.map((entry) => typeof entry === "string" ? entry : JSON.stringify(entry))
    : Object.entries(defaults).map(([key, value]) => key === "beam" && typeof value === "object"
      ? `入射光束：${value.type === "gaussian" ? "高斯光束" : "平面波"} / ${value.polarization?.type === "linear" ? `线偏振 ${value.polarization.angle_deg || 0}°` : value.polarization?.type || "默认偏振"}`
      : `${labels[key] || key}: ${typeof value === "object" ? JSON.stringify(value) : value}`);
  note.textContent = `补齐的默认参数：${items.join("；")}。全部参数可在配置中查看与修改。`;
}

function setBusy(value) {
  busy = value;
  $("run-design").disabled = value;
  $("load-example").disabled = value;
  $("preview").disabled = value;
}

async function previewField() {
  if (!$("design-form").reportValidity()) return;
  const sequence = ++previewSequence;
  $("preview").disabled = true;
  $("toolbar-status").textContent = "正在计算光场预览";
  setMessage("");
  try {
    const config = collectConfig();
    const data = await api("/api/preview", { config });
    if (sequence !== previewSequence) return;
    previewConfigSignature = beamSignature(config);
    renderPlots(data.plots || data.result?.plots || {});
    latestResult = { ...(latestResult || {}), status: data.status || "computed", plots: data.plots || {} };
    $("preview-changed").classList.toggle("hidden", beamSignature(collectConfig()) === previewConfigSignature);
    $("toolbar-status").textContent = "光场预览已计算";
    showTab("result", "field");
    const message = data.metadata?.note || data.metadata?.normalization_note;
    if (message) setMessage(message);
  } catch (error) { setMessage(error.message, "error"); $("toolbar-status").textContent = "光场预览失败"; }
  finally { if (!busy) $("preview").disabled = false; }
}

function statusValue(status, key, legacyKey) {
  if (status?.[key] !== undefined) return status[key];
  return status?.agent?.[key] !== undefined ? status.agent[key] : status?.agent?.[legacyKey];
}

function formatAgentConnection(status) {
  const provider = statusValue(status, "provider") || status?.parser || "local";
  const configured = statusValue(status, "configured", "ollama_configured");
  const checked = statusValue(status, "checked", "connection_checked");
  const reachable = statusValue(status, "reachable");
  const modelAvailable = statusValue(status, "model_available");
  const model = statusValue(status, "model");
  const isRule = provider === "deterministic_rule_parser" || provider === "local";
  const valueLabel = (value, pending = "未检查") => value === true ? "是" : value === false ? "否" : pending;
  const state = isRule
    ? "本地确定性规则解析器"
    : !configured ? "未配置"
      : checked !== true ? "已配置 · 待连接检查"
        : reachable !== true ? "连接失败"
          : modelAvailable === true ? "已连接 · 模型可用"
            : modelAvailable === false ? "已连接 · 模型不可用" : "已连接 · 模型待确认";
  const facts = `配置：${valueLabel(configured, "未配置")} · 连接：${checked === true ? valueLabel(reachable) : "未检查"} · 模型：${checked === true ? valueLabel(modelAvailable) : "未检查"}`;
  return { provider, configured, checked, reachable, modelAvailable, model, isRule, state, facts };
}

function updateProviderStatus(status) {
  const info = formatAgentConnection(status);
  const providerLabel = info.model ? `${info.provider} · ${info.model}` : info.provider;
  if ($("agent-provider")) $("agent-provider").textContent = `${providerLabel} · ${info.facts}`;
  if ($("agent-provider-detail")) $("agent-provider-detail").textContent = providerLabel;
  if ($("agent-connection-title")) $("agent-connection-title").textContent = info.isRule ? "本地规则解析器" : info.provider;
  if ($("agent-provider-state")) $("agent-provider-state").textContent = info.state;
  const connection = $("agent-connection");
  if (connection) connection.title = status?.error ? String(status.error) : info.facts;
}

function addAgentMessage(text, kind = "agent") {
  const thread = $("chat-thread"); if (!thread) return;
  const bubble = document.createElement("div"); bubble.className = `chat-bubble ${kind === "user" ? "user-bubble" : "agent-bubble"}`;
  const who = document.createElement("span"); who.textContent = kind === "user" ? "当前配置" : "MetaOpen 助手";
  const p = document.createElement("p"); p.textContent = text; bubble.append(who, p); thread.append(bubble); thread.scrollTop = thread.scrollHeight;
}

async function reviewCurrentConfig() {
  if (busy) return;
  const config = collectConfig(); addAgentMessage(`检查当前参数：${config.wavelength_nm} nm · ${config.diameter_um} μm 口径 · ${config.beam.type} 光束`, "user");
  addAgentMessage("正在提交结构化参数给助手检查。此检查只提供建议，不会启动设计任务或生成性能数值。", "agent");
  try {
    const review = await api("/api/agent/review", { config });
    const provider = review?.provider || "unknown";
    const lines = [];
    if (review?.summary) lines.push(review.summary);
    if (provider === "ollama_config_review") {
      lines.push(`检查来源：本地 Ollama 模型${review.model ? `（${review.model}）` : ""}。`);
    } else if (provider === "deterministic_fallback") {
      lines.push(`检查来源：本地确定性 fallback（${review.fallback_reason || "原因未提供"}），未调用外部模型。`);
    } else {
      lines.push(`检查来源：${provider}。`);
    }
    const warnings = Array.isArray(review?.warnings) ? review.warnings : review?.warnings ? [String(review.warnings)] : [];
    if (warnings.length) lines.push(`警告：${warnings.join("；")}`);
    if (review?.solver_source) lines.push(`数值来源：${review.solver_source}`);
    addAgentMessage(lines.join("\n"), "agent");
  } catch (error) {
    addAgentMessage(`助手检查失败：${error.message}`, "agent");
  }
}

async function connectAgent() {
  const button = $("agent-connect");
  if (button) button.disabled = true;
  addAgentMessage("正在检查本地模型或规则解析器连接状态。", "agent");
  try {
    agentStatus = await api("/api/agent/status");
    updateProviderStatus(agentStatus);
    const info = formatAgentConnection(agentStatus);
    const detail = info.isRule
      ? "当前使用本地确定性规则解析器；没有配置外部模型。"
      : `${info.provider}${info.model ? `（${info.model}）` : ""}：${info.state}。${agentStatus.error ? ` ${agentStatus.error}` : ""}`;
    addAgentMessage(`${detail}\n${info.facts}`, "agent");
  } catch (error) {
    addAgentMessage(`连接检查失败：${error.message}`, "agent");
  } finally {
    if (button) button.disabled = false;
  }
}

const STAGES = {
  queued: ["requirements", "设计任务已排队"], requirements: ["requirements", "校验目标与入射光场"],
  config: ["requirements", "校验目标与入射光场"], materials: ["materials", "筛选材料样品与参考工艺"],
  material_selection: ["materials", "筛选材料样品与参考工艺"], library: ["library", "计算单元响应与收敛"],
  rcwa: ["library", "计算周期单元复透射响应"], unit_library: ["library", "建立单元复响应库"],
  layout: ["layout", "映射目标相位与器件布局"], design: ["layout", "映射目标相位与器件布局"],
  propagation: ["propagation", "计算器件后的光场传播"], validation: ["propagation", "关键区域电磁复核"],
  supercell: ["propagation", "周期超胞电磁复核"], tolerance: ["tolerance", "采样假设制造误差"],
  export: ["export", "输出版图与可复现报告"], complete: ["export", "设计计算已完成"]
};

function renderJob(job) {
  const rawStage = String(job.stage || "准备设计任务");
  let stageKey = "requirements";
  if (/版图|报告|保存|完成/.test(rawStage)) stageKey = "export";
  else if (/容差|误差|制造/.test(rawStage)) stageKey = "tolerance";
  else if (/超胞|近场|光场|传播|邻近/.test(rawStage)) stageKey = "propagation";
  else if (/映射|布局|波前/.test(rawStage)) stageKey = "layout";
  else if (/单元|响应|库|收敛|rcwa/i.test(rawStage)) stageKey = "library";
  else if (/材料|工艺|选材/.test(rawStage)) stageKey = "materials";
  const stage = STAGES[job.stage] || [stageKey, rawStage];
  const done = ["completed", "complete", "succeeded", "finished"].includes(job.status);
  const failed = ["failed", "error"].includes(job.status);
  const progress = done ? 100 : Math.max(0, Math.min(100, Number(job.progress || 0) * (Number(job.progress) > 0 && Number(job.progress) <= 1 ? 100 : 1)));
  $("job-banner").className = `job-banner${done ? " finished" : failed ? " failed" : ""}`;
  $("job-title").textContent = done ? "设计计算已完成" : failed ? "设计任务未完成" : stage[1];
  $("job-progress").textContent = `${Math.round(progress)}%`;
  $("progress-bar").style.width = `${progress}%`;
  $("job-detail").textContent = failed ? (typeof job.error === "string" ? job.error : job.error?.message || "查看计算服务日志以定位原因。")
    : job.message || job.detail || (done ? "当前设计的配置、计算数据、版图与报告已保存。" : "运行设置、数据来源与求解器版本将随结果保存。");
  $("result-id").textContent = job.id || "计算中";
  $("result-id").title = job.id || "";
  $("toolbar-status").textContent = done ? "结果已保存" : failed ? "任务失败" : `正在运行 · ${stage[1]}`;
  return { done, failed };
}

function clearProbeResult(message = "探测器参数已更新；请重新计算截面。") {
  currentDetectorResult = null; probeImage = null;
  const target = $("plot-probe");
  if (target) { const empty = document.createElement("div"); empty.className = "plot-empty"; empty.textContent = message; target.replaceChildren(empty); }
  if ($("probe-result-label")) $("probe-result-label").textContent = "无计算数据";
  if ($("probe-status-card")) $("probe-status-card").textContent = message;
  if ($("probe-state")) { $("probe-state").className = "probe-state"; $("probe-state").lastChild.textContent = "截面数据待计算"; }
  drawScene();
}

function renderProbeResult(data) {
  if (data?.status !== "computed") {
    const reason = data?.reason || "服务未返回已计算的探测器数据。";
    clearProbeResult(`截面结果不可用：${reason}`); return;
  }
  const values = data.intensity;
  if (!Array.isArray(values) || !values.length || !Array.isArray(values[0]) || !values[0].length || !values.flat().every(Number.isFinite)) {
    clearProbeResult("截面结果不可用：返回数据不包含有效的数值强度矩阵。"); return;
  }
  const rows = values.length, cols = values[0].length;
  const imageCanvas = document.createElement("canvas"); imageCanvas.width = cols; imageCanvas.height = rows;
  const ctx = imageCanvas.getContext("2d"), pixels = ctx.createImageData(cols, rows);
  const peak = Math.max(...values.map((row) => Math.max(...row)), 1e-30);
  for (let y = 0; y < rows; y++) for (let x = 0; x < cols; x++) {
    const v = Math.max(0, Math.log10(1 + 999 * values[y][x] / peak) / 3); const i = (y * cols + x) * 4;
    pixels.data[i] = Math.round(18 + 237 * Math.min(1, 1.8 * v));
    pixels.data[i + 1] = Math.round(27 + 190 * Math.max(0, Math.min(1, 2.2 * v - .45)));
    pixels.data[i + 2] = Math.round(42 + 170 * Math.max(0, 1 - Math.abs(v - .55) * 1.7)); pixels.data[i + 3] = 255;
  }
  ctx.putImageData(pixels, 0, 0);
  const image = document.createElement("img"); image.src = imageCanvas.toDataURL("image/png"); image.alt = "由本地探测器计算得到的强度分布";
  const link = document.createElement("a"); link.href = image.src; link.target = "_blank"; link.rel = "noopener"; link.append(image);
  $("plot-probe").replaceChildren(link); probeImage = image; currentDetectorResult = data; drawScene();
  const geometry = data.metadata?.geometry || collectDetectorConfig();
  const label = `RS-I · ${geometry.width_um || "—"} × ${geometry.height_um || "—"} μm · ${cols} × ${rows}`;
  $("probe-result-label").textContent = label;
  $("probe-status-card").textContent = `已计算 · ${data.metadata?.method || "标量传播"}。配置会随“导出配置”保存；绝对强度是否可追溯见强度标尺说明。`;
  $("probe-state").className = "probe-state ready"; $("probe-state").lastChild.textContent = "截面已计算";
}

async function runProbe() {
  const id = latestResult?.id;
  if (!id || latestResult?.status !== "completed" && !latestResult?.metrics) {
    clearProbeResult("当前没有已保存的计算版本。请先运行设计，再计算探测器截面。"); return;
  }
  const config = collectDetectorConfig();
  if (Number($("probe-resolution").value) > 128) $("probe-status-card").textContent = "后端单次上限为 128 × 128；本次已自动限制为 128 × 128。";
  $("run-probe").disabled = true; $("run-probe-side").disabled = true; $("probe-status-card").textContent = "正在从已保存的出口面场计算探测器截面…";
  try {
    const data = await api("/api/probe", { job_id: id, config });
    renderProbeResult(data);
  } catch (error) { clearProbeResult(`截面计算不可用：${error.message}`); }
  finally { $("run-probe").disabled = false; $("run-probe-side").disabled = false; }
}

function installCanvasControls() {
  const canvas = $("scene-canvas"); if (!canvas) return;
  const observer = new ResizeObserver(resizeSceneCanvas); observer.observe($("canvas-wrap"));
  canvas.addEventListener("pointerdown", (event) => {
    pointerState = { x: event.clientX, y: event.clientY, mode: detectorDragEnabled && !event.shiftKey ? "detector" : event.shiftKey ? "pan" : "orbit" };
    canvas.setPointerCapture(event.pointerId);
  });
  canvas.addEventListener("pointermove", (event) => {
    if (!pointerState) return;
    const dx = event.clientX - pointerState.x, dy = event.clientY - pointerState.y; pointerState.x = event.clientX; pointerState.y = event.clientY;
    if (pointerState.mode === "orbit") { camera.yaw += dx * .009; camera.pitch = Math.max(-1.25, Math.min(1.25, camera.pitch + dy * .009)); }
    else if (pointerState.mode === "pan") { camera.panX += dx; camera.panY += dy; }
    else {
      const scale = Math.max(1, Math.min(canvasSize.width, canvasSize.height) / Math.max(1, numberValue("diameter", 30) * 1.7) * camera.zoom);
      const angle = camera.yaw; const worldX = dx / scale, worldY = -dy / scale;
      $("probe-x").value = numberValue("probe-x") + worldX * Math.cos(angle) + worldY * Math.sin(angle);
      $("probe-y").value = numberValue("probe-y") - worldX * Math.sin(angle) + worldY * Math.cos(angle);
      updateDetectorSummary();
    }
    drawScene();
  });
  for (const name of ["pointerup", "pointercancel", "lostpointercapture"]) canvas.addEventListener(name, () => { pointerState = null; });
  canvas.addEventListener("wheel", (event) => { event.preventDefault(); camera.zoom = Math.max(.55, Math.min(2.8, camera.zoom * (event.deltaY < 0 ? 1.08 : .92))); drawScene(); }, { passive: false });
  canvas.addEventListener("contextmenu", (event) => event.preventDefault());
  resizeSceneCanvas();
}

async function startRun(event) {
  event.preventDefault();
  if (busy || !$("design-form").reportValidity()) return;
  if (collectConfig().beam.theta_deg !== 0) { setMessage("首版单元库采用法向入射。倾斜入射光场可预览；请将倾角设为 0° 后运行完整设计。", "error"); return; }
  clearTimeout(pollTimer);
  setBusy(true); setMessage("");
  const sequence = ++requestSequence;
  try {
    submittedConfig = collectConfig();
    const data = await api("/api/run", { config: submittedConfig });
    if (sequence !== requestSequence) return;
    activeJob = data.id || data.job_id;
    if (!activeJob) throw new Error("计算服务未返回设计任务 ID。");
    renderJob({ ...data, id: activeJob, progress: data.progress || 0 });
    showTab("result", "verification");
    await pollJob(activeJob, sequence);
  } catch (error) { setBusy(false); setMessage(error.message, "error"); }
}

async function pollJob(id, sequence, failures = 0) {
  if (sequence !== requestSequence) return;
  try {
    const job = await api(`/api/jobs/${encodeURIComponent(id)}`);
    if (sequence !== requestSequence) return;
    if (!submittedConfig && (job.config || job.result?.config)) {
      applyConfig(job.config || job.result.config);
      submittedConfig = collectConfig();
    }
    const state = renderJob(job);
    if (state.done) {
      setBusy(false);
      await renderResult(job.result || {}, id);
      const review = job.result?.design_acceptance === "requires_review_of_recorded_model_or_numerical_checks";
      setMessage(review ? "计算已完成，当前方案存在待复核项。请先查看验证状态与报告；导出文件作为设计草案保留。" : "计算完成。请查看材料依据、性能验证和设计报告，确认首版适用范围。", review ? "" : "success");
      return;
    }
    if (state.failed) { setBusy(false); setMessage(typeof job.error === "string" ? job.error : job.error?.message || "设计计算失败。", "error"); return; }
    pollTimer = setTimeout(() => pollJob(id, sequence), 1500);
  } catch (error) {
    if (failures < 3) { pollTimer = setTimeout(() => pollJob(id, sequence, failures + 1), 2000); return; }
    setBusy(false); setMessage(`暂时无法读取设计任务：${error.message}。任务可能仍在后台运行，可刷新页面恢复结果。`, "error");
  }
}

function pick(object, keys) { for (const key of keys) if (object?.[key] !== undefined && object[key] !== null) return object[key]; return null; }

function renderMetrics(metrics = {}) {
  const focus = pick(metrics, ["estimated_focus_um", "focus_z_um", "focal_z_um", "peak_z_um", "best_focus_um", "focus_position_um"]);
  const efficiency = pick(metrics, ["focusing_efficiency", "focus_efficiency", "efficiency", "focusing_efficiency_aperture"]);
  const fwhm = pick(metrics, ["fwhm_um", "fwhm_x_um", "focus_fwhm_um"]);
  const samples = pick(metrics, ["tolerance_samples", "samples", "tolerance_sample_count"]);
  const values = [ ["轴向估计焦点", focus, "μm", "离散传播扫描结果"],
    ["聚焦效率", efficiency === null ? null : efficiency * 100, "%", "积分定义详见报告"],
    ["焦斑 FWHM · x", fwhm, "μm", "实际焦面切片"],
    ["容差采样", samples, "次", "假设误差下的评估"],
    ["透射效率", metrics.transmission_efficiency === undefined ? null : metrics.transmission_efficiency * 100, "%", "相对入射口径内功率"],
    ["Strehl 比", metrics.strehl ?? null, "", "同功率理想波前对照"],
    ["焦斑 FWHM · y", metrics.fwhm_y_um ?? null, "μm", "正交焦面切片"],
    ["聚焦效率积分半径", metrics.roi_radius_um ?? null, "μm", "以实际焦面峰值为中心"] ];
  $("metrics").replaceChildren(...values.map(([label, value, unit, detail]) => {
    const article = document.createElement("article");
    const span = document.createElement("span"); span.textContent = label;
    const strong = document.createElement("strong"); strong.textContent = formatNumber(value, 3);
    if (value !== null) { const suffix = document.createElement("i"); suffix.textContent = unit; strong.append(suffix); }
    const small = document.createElement("small"); small.textContent = detail;
    article.append(span, strong, small); return article;
  }));
}

function renderMaterials(selection) {
  if (!selection) return;
  const chosen = selection.selected || selection.chosen || selection.best || selection;
  const candidates = Array.isArray(selection) ? selection : selection.candidates || selection.ranking || [chosen];
  $("material-candidates").replaceChildren(...candidates.map((candidate) => {
    if (typeof candidate !== "object" || candidate === null) candidate = { name: String(candidate) };
    const row = document.createElement("div"); row.className = "material-row";
    const family = candidate.material_family || candidate.source_id?.split("/")[0];
    const shortNames = { TiO2: "TiO₂", Si3N4: "Si₃N₄", SiO2: "SiO₂", GaN: "GaN" };
    const name = shortNames[family] || candidate.material_id || candidate.material || candidate.name || candidate.sample_name || candidate.sample_id || candidate.source_id || "材料样品";
    const selected = candidate.selected || candidate === chosen || candidate.source_id && candidate.source_id === chosen.source_id && candidate.period_nm === chosen.period_nm && candidate.height_nm === chosen.height_nm || candidate.sample_id && candidate.sample_id === chosen.sample_id || candidate.material && candidate.material === chosen.material;
    if (selected) row.classList.add("chosen");
    const strong = document.createElement("strong"); strong.textContent = name;
    const info = document.createElement("div"); info.className = "material-info";
    const details = [candidate.source_id || candidate.sample_id || candidate.sample, candidate.reason || candidate.selection_reason || candidate.source,
      candidate.period_nm && candidate.height_nm ? `周期 ${candidate.period_nm} nm · 柱高 ${candidate.height_nm} nm${candidate.mean_transmission !== undefined ? ` · 平均透射 ${formatNumber(candidate.mean_transmission * 100, 1)}%` : ""}` : "",
      candidate.n !== undefined ? `n = ${formatOpticalConstant(candidate.n)}` : "", candidate.k !== undefined && candidate.k !== null ? `k = ${formatOpticalConstant(candidate.k)}` : ""];
    if (selected && chosen.sample) details.push(`源样品：${chosen.sample}`);
    if (candidate.k === null && candidate.k_used === 0) details.push("k 原始缺失 · 明示计算假设 k = 0");
    for (const detail of details.filter(Boolean)) { const small = document.createElement("small"); small.textContent = typeof detail === "object" ? JSON.stringify(detail) : detail; info.append(small); }
    const badge = document.createElement("span"); badge.className = "material-badge"; badge.textContent = selected ? "采用样品" : "候选";
    row.append(strong, info, badge); return row;
  }));
}

const ARTIFACT_DESCRIPTIONS = {
  gds: "GDSII 制造版图 · 单元与图层", gdsii: "GDSII 制造版图 · 单元与图层",
  oas: "OASIS 制造版图", oasis: "OASIS 制造版图",
  h5: "HDF5 数值结果 · 单元响应与光场", hdf5: "HDF5 数值结果 · 单元响应与光场",
  yaml: "YAML 设计配置 · 可独立重跑", yml: "YAML 设计配置 · 可独立重跑",
  json: "结构化计算记录与配置", md: "计算依据、容差与适用范围", png: "实际计算结果图像", csv: "计算数据表"
};

function renderArtifacts(artifacts) {
  if (!Array.isArray(artifacts)) artifacts = Object.entries(artifacts || {}).map(([name, url]) => ({ name, url }));
  const links = [];
  for (const artifact of artifacts) {
    const url = safeLocalUrl(artifact.url || artifact.path);
    if (!url) continue;
    const name = artifact.name || url.split("/").pop();
    const extension = name.split(".").pop().toLowerCase();
    const link = document.createElement("a"); link.className = "artifact-link"; link.href = url; link.download = name;
    const type = document.createElement("span"); type.className = "file-type"; type.textContent = extension.toUpperCase();
    const info = document.createElement("div"); info.className = "file-info";
    const strong = document.createElement("strong"); strong.textContent = name;
    const small = document.createElement("small"); small.textContent = artifact.description || ARTIFACT_DESCRIPTIONS[extension] || artifact.kind || "设计输出文件";
    const arrow = document.createElement("span"); arrow.className = "download-arrow"; arrow.textContent = "↓";
    info.append(strong, small); link.append(type, info, arrow); links.push(link);
  }
  if (links.length) $("artifact-list").replaceChildren(...links);
}

function renderNotes(id, title, items) {
  if (!items || Array.isArray(items) && !items.length) return;
  const box = $(id); box.classList.remove("hidden");
  const strong = document.createElement("strong"); strong.textContent = title;
  const list = document.createElement("ul");
  const values = Array.isArray(items) ? items : typeof items === "object" ? Object.entries(items).map(([key, value]) => `${key}: ${typeof value === "object" ? JSON.stringify(value) : value}`) : [items];
  for (const text of values) { const li = document.createElement("li"); li.textContent = typeof text === "object" ? JSON.stringify(text) : String(text); list.append(li); }
  box.replaceChildren(strong, list);
}

async function renderResult(result, id) {
  result = structuredClone(result);
  const runDirectoryId = result.id || id;
  const runUrl = (value) => typeof value === "string" && !value.startsWith("/") && !/^https?:\/\//.test(value) ? `/runs/${encodeURIComponent(runDirectoryId)}/${value}` : value;
  for (const [key, value] of Object.entries(result.plots || {})) {
    if (Array.isArray(value)) result.plots[key] = value.map((slice) => ({ ...slice, url: runUrl(slice.url) }));
    else if (typeof value === "string") result.plots[key] = runUrl(value);
    else if (value?.url) result.plots[key] = { ...value, url: runUrl(value.url) };
  }
  if (Array.isArray(result.artifacts)) result.artifacts = result.artifacts.map((file) => ({ ...file, url: runUrl(file.url || file.path) }));
  result.report_url = runUrl(result.report_url);
  latestResult = result;
  latestResult.status = result.status || "computed";
  $("result-id").textContent = runDirectoryId || id || "未保存";
  $("result-id").title = runDirectoryId || id || "";
  loadGeometryForResult(result, runDirectoryId);
  loadFieldTextures(result.plots || {});
  renderAcceptance(result);
  resultConfigSignature = JSON.stringify(submittedConfig || collectConfig());
  $("changed-note").classList.toggle("hidden", JSON.stringify(collectConfig()) === resultConfigSignature);
  renderPlots(result.plots || {});
  currentDetectorResult = null;
  probeImage = null;
  if ($("plot-probe")) { const empty = document.createElement("div"); empty.className = "plot-empty"; empty.textContent = "设计版本更新后，请按需重新计算探测器截面。"; $("plot-probe").replaceChildren(empty); }
  if ($("probe-result-label")) $("probe-result-label").textContent = "无计算数据";
  if ($("probe-status-card")) $("probe-status-card").textContent = "选择当前版本后，调用探测器截面计算。";
  if (result.plots?.input_intensity) {
    previewConfigSignature = beamSignature(submittedConfig || collectConfig());
    $("preview-changed").classList.toggle("hidden", beamSignature(collectConfig()) === previewConfigSignature);
  }
  renderMetrics({ ...result.metrics, tolerance_samples: result.tolerance_summary?.requested_samples ?? result.metrics?.tolerance_samples });
  renderMaterials(result.material_selection || result.materials);
  $("design-method").textContent = "周期单元直接仿真";
  $("verification-method").textContent = "传播计算 / 超胞复核";
  $("toolbar-status").textContent = "计算结果已载入";
  renderNotes("design-notes", "单元响应与映射依据", result.design_notes || result.library?.notes);
  const limitationTranslations = {
    "Restricted deterministic natural-language parser; replaceable model interface": "自然语言入口采用受限规则解析器，模型接口可替换。",
    "Scalar propagation / normal incidence only": "采用标量角谱传播，单元响应库仅覆盖法向入射。",
    "Periodic supercell check; no finite-device FDTD": "提供周期超胞复核，未完成有限尺寸整片器件 FDTD。",
    "Assumed process statistics; not calibrated yield": "采用假设制程统计，结果不构成校准后的良率预测。",
    "Experimental comparison needs real user measurements": "实验对照与模型校准需要用户提供真实测量数据。"
  };
  renderNotes("validation-notes", "验证依据与适用范围", result.limitations?.map((value) => limitationTranslations[value] || value) || result.validation_notes || [
    "入射光场从 SiO₂ 基底侧进入，输出至空气；单元库针对法向入射。",
    "整片传播采用局部周期近似，关键区域采用周期超胞复核；首版未完成全器件 FDTD。",
    "容差结果基于假设误差。实验反馈未接入时，报告不构成制造良率预测。"
  ]);
  const validationLabels = { unit_selected_sizes_passed: "所选单元尺寸收敛", propagation: "传播窗口与采样", coupling: "周期超胞数值增量", layout_passed: "版图尺寸与间隙", local_periodic_model_requires_review: "局部周期近似适用性" };
  if (result.validation) renderNotes("validation-checks", "当前版本的真实检查状态", Object.entries(result.validation).map(([key, value]) => {
    if (key.includes("requires_review")) return `${validationLabels[key] || key}：${value ? "需要独立复核，当前局部周期模型不能视为已验证" : "未记录额外待复核标记"}`;
    const passed = typeof value === "boolean" ? value : value?.passed;
    let detail = "";
    if (key === "propagation") detail = `；扩大窗口的峰值变化 ${formatNumber(value.padding_peak_relative_change * 100)}%，减半采样间距的峰值变化 ${formatNumber(value.half_dx_peak_relative_change * 100)}%，阈值 ${formatNumber(value.threshold_relative * 100)}%。${value.warnings?.length ? "存在附加传播警告，详见原始检查记录。" : ""}`;
    else if (key === "coupling") detail = `；两级精度的通量差 ${formatNumber(value.power_delta * 100)} 百分点，相位差 ${formatNumber(value.phase_delta_rad)} rad。`;
    else if (typeof value === "object") detail = `；${JSON.stringify(value)}`;
    return `${validationLabels[key] || key}：${passed === true ? "通过当前检查" : passed === false ? "需要复核" : "查看原始记录"}${detail}`;
  }).concat(["周期超胞数值收敛不等于全器件局部周期近似已得到证明；请查看邻近单元差异与适用范围。"]));
  if (result.artifacts) renderArtifacts(result.artifacts);
  else try { const data = await api(`/api/jobs/${encodeURIComponent(id)}/artifacts`); renderArtifacts(data.artifacts || data); }
  catch (error) { setMessage(`计算已完成，但文件列表读取失败：${error.message}`, "error"); }
  let provenance = result.provenance || result.metadata;
  if (!provenance) {
    const manifest = result.artifacts?.find((file) => file.name === "manifest.json");
    const manifestUrl = safeLocalUrl(manifest?.url);
    if (manifestUrl) try {
      const response = await fetch(manifestUrl);
      if (response.ok) {
        const data = await response.json();
        provenance = { "配置 SHA256": data.config_sha256, "随机种子": data.seed,
          "Python": data.python, "平台版本": data.platform_version,
          "材料数据库版本": data.material_database_commit, "材料目录 SHA256": data.material_catalog_sha256,
          "依赖版本": data.dependencies, "计算来源": data.evidence };
      }
    } catch { /* The complete manifest remains a downloadable artifact. */ }
  }
  if (provenance) renderProvenance(provenance, id);
  $("report-preview").textContent = JSON.stringify({ design_id: id, metrics: result.metrics, material_selection: result.material_selection, limitations: result.limitations, provenance }, null, 2);
  const reportUrl = safeLocalUrl(result.report_url);
  if (reportUrl) {
    try { const response = await fetch(reportUrl); if (response.ok) $("report-preview").textContent = await response.text(); }
    catch { /* Structured result remains available if the report cannot be loaded. */ }
  }
}

function renderAcceptance(result) {
  const box = $("acceptance-banner");
  const review = result.design_acceptance === "requires_review_of_recorded_model_or_numerical_checks";
  const accepted = result.design_acceptance === "checks_passed_with_declared_scope";
  box.className = `acceptance-banner${review ? " review" : accepted ? " accepted" : " hidden"}`;
  box.textContent = review ? "计算已完成 · 设计需要复核：当前模型或数值检查存在待复核项。文件是可追溯设计草案，尚未证明可制造性能。"
    : "当前数值检查通过 · 结论限于报告所声明的模型与范围，不包含全器件 FDTD 与实验验证。";
}

async function readArchivedConfig(job) {
  if (job.config || job.result?.config) return job.config || job.result.config;
  const result = job.result || {};
  const file = result.artifacts?.find((artifact) => artifact.name === "config.json");
  const fileUrl = file?.url || `/runs/${encodeURIComponent(result.id || job.id)}/config.json`;
  const url = safeLocalUrl(fileUrl.startsWith("/") || /^https?:\/\//.test(fileUrl) ? fileUrl : `/runs/${encodeURIComponent(result.id || job.id)}/${fileUrl}`);
  if (!url) return null;
  try { const response = await fetch(url); if (response.ok) return await response.json(); } catch {}
  return null;
}

function renderProvenance(metadata, id) {
  const container = $("provenance"); container.classList.remove("hidden");
  const title = document.createElement("strong"); title.textContent = "计算可追溯记录";
  const dl = document.createElement("dl");
  const data = { "设计版本": id, ...metadata };
  for (const [key, value] of Object.entries(data)) {
    const row = document.createElement("div");
    const dt = document.createElement("dt"); dt.textContent = key;
    const dd = document.createElement("dd"); dd.textContent = typeof value === "object" ? JSON.stringify(value) : String(value);
    row.append(dt, dd); dl.append(row);
  }
  container.replaceChildren(title, dl);
}

async function loadExample() {
  if (busy) return;
  setMessage("");
  applyConfig(serviceStatus?.default_config || DEFAULT_CONFIG);
  try {
    serviceStatus = await api("/api/status");
    const jobs = Array.isArray(serviceStatus.jobs) ? serviceStatus.jobs : [];
    const complete = jobs.find((job) => ["completed", "complete", "succeeded", "finished"].includes(job.status));
    if (complete) {
      const job = await api(`/api/jobs/${encodeURIComponent(complete.id)}`);
      const archivedConfig = await readArchivedConfig(job);
      if (archivedConfig) applyConfig(archivedConfig);
      submittedConfig = collectConfig();
      renderJob(job); await renderResult(job.result || {}, complete.id);
      setMessage("已载入本机保存的计算版本。原始配置与报告可从“版图与报告”下载。", "success");
      showTab("result", "verification");
    } else {
      setMessage("首版示例参数已载入。点击“运行完整设计”，生成本机可复现的计算结果。", "success");
      setMessage("示例参数已载入。可在画布中查看结构示意，点击“预览光场”或“运行设计”启动实际计算。", "success");
    }
  } catch (error) { setMessage(`示例参数已载入；${error.message}`, "error"); }
}

function downloadConfig() {
  const blob = new Blob([JSON.stringify(collectConfig(), null, 2) + "\n"], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a"); link.href = url; link.download = "metasurface-config.json"; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function initialize() {
  document.querySelectorAll("[data-config-tab]").forEach((button) => button.addEventListener("click", () => showTab("config", button.dataset.configTab)));
  document.querySelectorAll("[data-result-tab]").forEach((button) => button.addEventListener("click", () => showTab("result", button.dataset.resultTab)));
  document.querySelectorAll("[data-sidebar-tab]").forEach((button) => button.addEventListener("click", () => showSidebarPage(button.dataset.sidebarTab)));
  document.querySelectorAll("[data-scene-mode]").forEach((button) => button.addEventListener("click", () => {
    sceneMode = button.dataset.sceneMode;
    document.querySelectorAll("[data-scene-mode]").forEach((item) => item.classList.toggle("active", item === button));
    const labels = { structure: ["参数化结构示意", "GEOMETRY VIEW"], beam: ["三维入射光场示意", "ANALYTIC BEAM"], result: [fieldTextures.length ? "计算场结果视图" : "等待实际运行切片", fieldTextures.length ? "COMPUTED RESULT" : "NO FIELD DATA"] };
    $("scene-title").textContent = labels[sceneMode][0]; $("scene-state-tag").textContent = labels[sceneMode][1]; drawScene();
  }));
  $("design-form").addEventListener("input", updateControls);
  $("design-form").addEventListener("change", updateControls);
  $("design-form").addEventListener("submit", startRun);
  $("run-design").addEventListener("click", startRun);
  $("preview").addEventListener("click", previewField);
  $("slice-slider").addEventListener("input", () => renderSlice(Number($("slice-slider").value)));
  $("load-example").addEventListener("click", loadExample);
  $("parameter-reset").addEventListener("click", loadExample);
  $("download-config").addEventListener("click", downloadConfig);
  $("methods-toggle").addEventListener("click", () => {
    const panel = $("methods-panel"), open = panel.classList.toggle("hidden");
    $("methods-toggle").setAttribute("aria-expanded", String(!open));
  });
  $("reset-camera").addEventListener("click", () => { camera = { yaw: -.55, pitch: .45, zoom: 1, panX: 0, panY: 0 }; drawScene(); });
  $("probe-drag-toggle").addEventListener("click", () => {
    detectorDragEnabled = !detectorDragEnabled; $("probe-drag-toggle").classList.toggle("active", detectorDragEnabled);
    $("probe-drag-toggle").setAttribute("aria-pressed", String(detectorDragEnabled)); drawScene();
  });
  $("run-probe").addEventListener("click", runProbe); $("run-probe-side").addEventListener("click", runProbe);
  ["probe-x", "probe-y", "probe-z", "probe-theta", "probe-phi", "probe-width", "probe-height", "probe-roll", "probe-resolution"].forEach((id) => $(id).addEventListener("input", () => { updateDetectorSummary(); clearProbeResult(); }));
  $("agent-review").addEventListener("click", reviewCurrentConfig); $("agent-connect").addEventListener("click", connectAgent);
  const resolution = $("probe-resolution");
  [...resolution.options].forEach((option) => { if (Number(option.value) > 128) option.remove(); });
  installCanvasControls();
  applyConfig(DEFAULT_CONFIG);
  try {
    serviceStatus = await api("/api/status");
    updateProviderStatus(serviceStatus);
    if (serviceStatus.default_config) applyConfig(serviceStatus.default_config);
    $("connection").className = "connection connected";
    $("connection").lastChild.textContent = ` 本地计算服务已就绪${serviceStatus.version ? ` · ${serviceStatus.version}` : ""}`;
    const jobs = serviceStatus.jobs || [];
    const running = jobs.find((job) => ["queued", "running", "pending"].includes(job.status));
    if (running) {
      activeJob = running.id; setBusy(true); renderJob(running);
      const sequence = ++requestSequence; pollJob(running.id, sequence);
    } else {
      const saved = (serviceStatus.jobs || []).find((job) => ["completed", "complete", "succeeded", "finished"].includes(job.status));
      if (saved?.result) {
        const archivedConfig = await readArchivedConfig(saved); if (archivedConfig) applyConfig(archivedConfig);
        submittedConfig = collectConfig(); renderJob(saved); await renderResult(saved.result, saved.id);
        setMessage("已恢复最近保存的设计版本。该计算与当前表单修改分开保留。", "success");
      }
    }
    // `/api/status` only reports the configured provider.  Resolve the
    // assistant's actual reachability and model availability through its
    // explicit status endpoint, without affecting the ordinary service/jobs
    // initialization above.
    try {
      agentStatus = await api("/api/agent/status");
      updateProviderStatus(agentStatus);
    } catch (agentError) {
      // Older/static servers may not expose the optional assistant endpoint;
      // keep the service status visible and let the explicit connect action
      // surface the actionable error later.
      if ($("agent-connection")) $("agent-connection").title = agentError.message;
    }
  } catch (error) {
    $("connection").className = "connection error";
    $("connection").lastChild.textContent = " 计算服务未连接";
    setMessage(`请使用项目的 serve 命令启动本地服务。${error.message}`, "error");
  }
}

initialize();
