/** Browser smoke check using Node 22+ and Chrome/Edge's native DevTools protocol.
 * No npm packages. Start the Python server before running this file.
 * node tests/browser-smoke.mjs --url http://127.0.0.1:8765 [--run]
 */
import { spawn } from "node:child_process";
import { mkdir, readFile, writeFile, access, rm } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";
import { createServer } from "node:http";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const args = process.argv.slice(2);
const urlIndex = args.indexOf("--url");
let baseUrl = urlIndex >= 0 ? args[urlIndex + 1] : "http://127.0.0.1:8765";
const runDesign = args.includes("--run");
const staticOnly = args.includes("--static");
let staticService;
const staticCounters = { agentStatus: 0, agentReview: 0, designRun: 0 };
if (staticOnly) {
  staticService = createServer(async (request, response) => {
    if (request.url === "/api/status") { response.writeHead(200, { "Content-Type": "application/json" }); response.end(JSON.stringify({ version: "static-ui-check", jobs: [] })); return; }
    if (request.url === "/__smoke/stats") { response.writeHead(200, { "Content-Type": "application/json" }); response.end(JSON.stringify(staticCounters)); return; }
    if (request.url === "/api/agent/status") { staticCounters.agentStatus++; response.writeHead(200, { "Content-Type": "application/json" }); response.end(JSON.stringify({ provider: "deterministic_rule_parser", configured: false, checked: false, reachable: null, model: null, model_available: null, endpoint: null, error: null })); return; }
    if (request.url === "/api/agent/review" && request.method === "POST") { staticCounters.agentReview++; response.writeHead(200, { "Content-Type": "application/json" }); response.end(JSON.stringify({ provider: "deterministic_fallback", summary: "静态助手检查通过。", warnings: ["这是浏览器静态检查返回的示例警告。"], fallback_reason: "ollama_not_configured", solver_source: "静态 smoke stub；不生成性能数值。" })); return; }
    if (request.url === "/api/run" && request.method === "POST") { staticCounters.designRun++; response.writeHead(200, { "Content-Type": "application/json" }); response.end(JSON.stringify({ id: `job-smoke-${staticCounters.designRun}`, status: "queued" })); return; }
    const file = request.url === "/" ? "index.html" : request.url?.slice(1);
    if (!["index.html", "style.css", "app.js"].includes(file)) { response.writeHead(404); response.end(); return; }
    response.writeHead(200, { "Content-Type": file.endsWith(".css") ? "text/css" : file.endsWith(".js") ? "application/javascript" : "text/html" });
    response.end(await readFile(path.join(root, "src", "metasurface_platform", "web", file)));
  });
  await new Promise((resolve) => staticService.listen(0, "127.0.0.1", resolve));
  baseUrl = `http://127.0.0.1:${staticService.address().port}`;
}
const profile = path.join(root, ".runtime", `browser-profile-${process.pid}`);
const screenshots = path.join(root, ".runtime", "screenshots");
const candidates = [process.env.BROWSER_PATH,
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome"].filter(Boolean);
let executable;
for (const candidate of candidates) { try { await access(candidate); executable = candidate; break; } catch {} }
if (!executable) throw new Error("Chrome or Edge was not found; set BROWSER_PATH.");
await mkdir(profile, { recursive: true });
await mkdir(screenshots, { recursive: true });
const browser = spawn(executable, ["--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
  "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"], { stdio: "ignore", windowsHide: true });

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function waitFor(test, label, timeout = 15000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) { if (await test()) return; await delay(100); }
  throw new Error(`Timed out: ${label}`);
}
let socket;
try {
  let port;
  await waitFor(async () => {
    try { port = Number((await readFile(path.join(profile, "DevToolsActivePort"), "utf8")).split("\n")[0]); return port > 0; }
    catch { return false; }
  }, "browser startup");
  const target = await (await fetch(`http://127.0.0.1:${port}/json/new?${encodeURIComponent(baseUrl)}`, { method: "PUT" })).json();
  socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener("open", resolve, { once: true }); socket.addEventListener("error", reject, { once: true }); });
  let nextId = 0;
  const pending = new Map();
  const errors = [];
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.id) {
      const request = pending.get(message.id); pending.delete(message.id);
      if (message.error) request?.reject(new Error(message.error.message)); else request?.resolve(message.result);
    }
    if (message.method === "Runtime.exceptionThrown") errors.push(message.params.exceptionDetails.text);
  });
  function cdp(method, params = {}) { return new Promise((resolve, reject) => { const id = ++nextId; pending.set(id, { resolve, reject }); socket.send(JSON.stringify({ id, method, params })); }); }
  async function evaluate(expression) {
    const value = await cdp("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    if (value.exceptionDetails) throw new Error(value.exceptionDetails.exception?.description || value.exceptionDetails.text);
    return value.result.value;
  }
  await cdp("Runtime.enable"); await cdp("Page.enable");
  await cdp("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1180, deviceScaleFactor: 1, mobile: false });
  await waitFor(async () => evaluate("document.querySelector('#connection')?.classList.contains('connected')"), "local service connection");
  assert.equal(await evaluate("document.title"), "MetaOpen · 光学设计工作台");
  assert.equal(await evaluate("document.querySelectorAll('[data-result-tab]').length"), 4);
  assert.equal(await evaluate("document.querySelector('#scene-canvas') instanceof HTMLCanvasElement"), true, "main design canvas is present");
  assert.equal(await evaluate("document.querySelector('#scene-canvas').getBoundingClientRect().width > 0 && document.querySelector('#scene-canvas').getBoundingClientRect().height > 0"), true, "canvas is sized responsively");
  assert.equal(await evaluate("document.querySelector('textarea') === null && !Array.from(document.querySelectorAll('input')).some(input => /request|natural.?language/i.test(input.id + ' ' + input.name))"), true, "there is no natural-language request input");
  assert.equal(await evaluate("document.querySelectorAll('#probe-resolution option').length"), 1, "detector resolution is capped at 128 × 128");
  await evaluate("document.querySelector('[data-sidebar-tab=assistant]').click(); true");
  await evaluate("document.querySelector('#agent-connect').click(); true");
  await waitFor(async () => evaluate("document.querySelector('#agent-provider').textContent.includes('配置：')"), "explicit assistant connection status");
  assert.ok((await evaluate("document.querySelector('#agent-provider').textContent")).includes("连接："), "assistant status reports reachability");
  assert.ok((await evaluate("document.querySelector('#agent-provider-state').textContent")).includes("规则解析器"), "deterministic provider is labeled truthfully");
  const reviewResultId = await evaluate("document.querySelector('#result-id').textContent");
  await evaluate("document.querySelector('#agent-review').click(); true");
  await waitFor(async () => evaluate("document.querySelector('#chat-thread').textContent.includes('检查来源：')"), "assistant review response");
  assert.ok((await evaluate("document.querySelector('#chat-thread').textContent")).includes("fallback"), "assistant review identifies deterministic fallback");
  assert.equal(await evaluate("document.querySelector('#result-id').textContent"), reviewResultId, "assistant review does not create a design job");
  if (staticOnly) {
    const agentCalls = await (await fetch(`${baseUrl}/__smoke/stats`)).json();
    assert.ok(agentCalls.agentStatus >= 1, "connect button calls GET /api/agent/status");
    assert.equal(agentCalls.agentReview, 1, "review button calls POST /api/agent/review");
    assert.equal(agentCalls.designRun, 0, "review never calls POST /api/run");
  }
  await evaluate("document.querySelector('[data-sidebar-tab=parameters]').click(); true");
  await evaluate("document.querySelector('[data-config-tab=beam]').click(); document.querySelector('input[value=gaussian]').click(); document.querySelector('#waist').value=12; document.querySelector('#waist').dispatchEvent(new Event('input',{bubbles:true})); true");
  assert.equal(await evaluate("document.querySelector('[data-config-section=beam]').classList.contains('hidden')"), false);
  assert.equal(await evaluate("Number(document.querySelector('#waist').value)"), 12);
  await evaluate("document.querySelector('[data-sidebar-tab=simulation]').click(); true");
  assert.equal(await evaluate("document.querySelector('[data-sidebar-page=simulation]').classList.contains('hidden')"), false);
  await evaluate("document.querySelector('[data-sidebar-tab=parameters]').click(); document.querySelector('[data-scene-mode=beam]').click(); true");
  assert.equal(await evaluate("document.querySelector('#scene-state-tag').textContent"), "ANALYTIC BEAM");
  assert.ok(await evaluate("document.querySelector('#canvas-legend').textContent.includes('基底')"), "canvas identifies the substrate material");
  await evaluate("document.querySelector('#methods-toggle').click(); true");
  assert.equal(await evaluate("document.querySelector('#methods-panel').classList.contains('hidden')"), false);
  assert.equal(await evaluate("document.querySelector('#methods-toggle').getAttribute('aria-expanded')"), "true");
  await evaluate("document.querySelector('#methods-toggle').click(); document.querySelector('#probe-drag-toggle').click(); true");
  assert.equal(await evaluate("document.querySelector('#probe-drag-toggle').classList.contains('active')"), true);
  await evaluate("document.querySelector('#probe-drag-toggle').click(); document.querySelector('#reset-camera').click(); true");
  if (staticOnly) {
    await evaluate("document.querySelector('#run-probe').click(); true");
    assert.equal(await evaluate("document.querySelector('#plot-probe img') === null"), true, "no detector intensity is fabricated without a computed run");
    assert.ok((await evaluate("document.querySelector('#probe-status-card').textContent")).includes("没有已保存的计算版本"));
  }
  if (!staticOnly) {
  await evaluate("document.querySelector('#preview').click(); true");
  await waitFor(async () => evaluate("document.querySelector('#plot-input-intensity img')?.complete && document.querySelector('#plot-input-intensity img')?.naturalWidth > 0"), "calculated beam preview", 30000);
  await evaluate("document.querySelector('[data-config-tab=target]').click(); document.querySelector('#wavelength').value=532; document.querySelector('#focal-length').value=100; document.querySelector('#diameter').value=30; document.querySelector('#wavelength').dispatchEvent(new Event('input',{bubbles:true})); true");
  assert.equal(await evaluate("Number(document.querySelector('#wavelength').value)"), 532);
  assert.equal(await evaluate("Number(document.querySelector('#focal-length').value)"), 100);
  assert.equal(await evaluate("Number(document.querySelector('#diameter').value)"), 30);
  }
  const status = await (await fetch(`${baseUrl}/api/status`)).json();
  const completed = (status.jobs || []).some((job) => ["completed", "complete", "finished", "succeeded"].includes(job.status));
  if (completed) {
    await evaluate("document.querySelector('#load-example').click(); true");
    await waitFor(async () => evaluate("document.querySelectorAll('.artifact-link').length > 0"), "saved reproducible results");
    const files = await evaluate("Array.from(document.querySelectorAll('.artifact-link')).map(a=>({name:a.download,url:a.href}))");
    for (const extension of ["gds", "oas", "h5", "yaml", "json", "md"]) {
      const file = files.find((item) => item.name.endsWith(`.${extension}`));
      assert.ok(file, `download link for ${extension}`);
      const response = await fetch(file.url);
      assert.equal(response.status, 200, `${file.name} is available over the real API`);
      const bytes = new Uint8Array(await response.arrayBuffer());
      assert.ok(bytes.length > 20, `${file.name} contains actual data`);
      if (extension === "h5") assert.deepEqual([...bytes.slice(0, 8)], [137, 72, 68, 70, 13, 10, 26, 10]);
      if (extension === "oas") assert.equal(new TextDecoder().decode(bytes.slice(0, 13)), "%SEMI-OASIS\r\n");
    }
    await evaluate("document.querySelector('[data-result-tab=verification]').click(); true");
    await evaluate("document.querySelector('#slice-slider').value=0; document.querySelector('#slice-slider').dispatchEvent(new Event('input')); true");
    await waitFor(async () => evaluate("document.querySelector('#plot-slices img')?.complete && document.querySelector('#plot-slices img')?.naturalWidth > 0"), "z=0 propagation slice");
    assert.equal(await evaluate("document.querySelector('#slice-position').textContent"), "z = 0 μm");
    await evaluate("document.querySelector('#slice-slider').value=document.querySelector('#slice-slider').max; document.querySelector('#slice-slider').dispatchEvent(new Event('input')); true");
    await waitFor(async () => evaluate("document.querySelector('#plot-slices img')?.complete && document.querySelector('#plot-slices img')?.naturalWidth > 0"), "last propagation slice");
    assert.ok((await evaluate("document.querySelector('#slice-position').textContent")).includes("160"));
    await evaluate("document.querySelector('#slice-slider').value=5; document.querySelector('#slice-slider').dispatchEvent(new Event('input')); true");
    const acceptance = await evaluate("document.querySelector('#acceptance-banner').textContent");
    assert.ok(acceptance.includes("复核") || acceptance.includes("声明"), "design acceptance and scope are visible");
    assert.ok((await evaluate("document.querySelector('#validation-checks').textContent")).includes("局部周期"));
    await evaluate("document.querySelector('#run-probe').click(); true");
    await waitFor(async () => evaluate("document.querySelector('#probe-status-card').textContent.includes('已计算') || document.querySelector('#probe-status-card').textContent.includes('不可用')"), "probe calculation status", 60000);
    if (await evaluate("document.querySelector('#probe-status-card').textContent.includes('已计算')")) {
      assert.ok(await evaluate("document.querySelector('#plot-probe img')?.naturalWidth > 0"), "computed probe intensity renders from returned numeric data");
      assert.ok((await evaluate("document.querySelector('#probe-result-label').textContent")).includes("128 × 128"));
    } else {
      assert.equal(await evaluate("document.querySelector('#plot-probe img') === null"), true, "unavailable probe results remain blank");
    }
  }
  if (runDesign && !staticOnly) {
    const previousResultId = await evaluate("document.querySelector('#result-id')?.textContent || ''");
    await evaluate("document.querySelector('#assume-k-zero').checked=true; document.querySelector('input[value=gaussian]').checked=true; document.querySelector('#assume-k-zero').dispatchEvent(new Event('change',{bubbles:true})); true");
    assert.equal(await evaluate("document.querySelector('#design-form')?.checkValidity()"), true, "default and fractional physical parameter values satisfy native form validation");
    await evaluate("document.querySelector('#run-design').click(); true");
    try {
      await waitFor(async () => evaluate(`(document.querySelector('#result-id')?.textContent || '') !== ${JSON.stringify(previousResultId)} && (document.querySelector('#result-id')?.textContent || '').startsWith('job-')`), "new design job queued", 15000);
    } catch (error) {
      const diagnostics = await evaluate("JSON.stringify({resultId:document.querySelector('#result-id')?.textContent,message:document.querySelector('#message')?.textContent,buttonDisabled:document.querySelector('#run-design')?.disabled,formValid:document.querySelector('#design-form')?.checkValidity(),invalid:Array.from(document.querySelectorAll('#design-form :invalid')).map(x=>({id:x.id,value:x.value,validation:x.validationMessage})),jobText:document.querySelector('#job-banner')?.textContent})");
      throw new Error(`${error.message}; current page state: ${diagnostics}`);
    }
    await waitFor(async () => evaluate("document.querySelector('#job-banner').classList.contains('finished') || document.querySelector('#job-banner').classList.contains('failed')"), "end-to-end design", 240000);
    assert.equal(await evaluate("document.querySelector('#job-banner').classList.contains('failed')"), false);
    assert.ok(await evaluate("document.querySelectorAll('.artifact-link').length") > 0);
    assert.notEqual(await evaluate("document.querySelector('#result-id')?.textContent || ''"), previousResultId, "run-design created a new persisted job");
  }
  await waitFor(async () => evaluate("Array.from(document.images).filter(i=>!i.closest('.hidden')).every(i=>i.complete)"), "images loaded");
  assert.deepEqual(errors, [], "no uncaught browser exceptions");
  const screenshot = await cdp("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
  await writeFile(path.join(screenshots, "workbench-desktop.png"), Buffer.from(screenshot.data, "base64"));
  const overview = await cdp("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  await writeFile(path.join(screenshots, "workbench-overview.png"), Buffer.from(overview.data, "base64"));
  if (completed || runDesign) {
    for (const [tab, filename] of [["design", "workbench-design.png"], ["field", "workbench-input.png"], ["artifacts", "workbench-artifacts.png"]]) {
      await evaluate(`document.querySelector('[data-result-tab=${tab}]').click(); true`);
      await waitFor(async () => evaluate("Array.from(document.images).filter(i=>!i.closest('.hidden')).every(i=>i.complete)"), `${tab} images loaded`);
      const image = await cdp("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
      await writeFile(path.join(screenshots, filename), Buffer.from(image.data, "base64"));
    }
    await evaluate("document.querySelector('[data-result-tab=verification]').click(); true");
  }
  await cdp("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await delay(150);
  const mobile = await cdp("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
  await writeFile(path.join(screenshots, "workbench-mobile.png"), Buffer.from(mobile.data, "base64"));
  assert.equal(await evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), true, "mobile has no horizontal overflow");
  for (const tab of ["design", "field", "artifacts"]) {
    await evaluate(`document.querySelector('[data-result-tab=${tab}]').click(); true`);
    assert.equal(await evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), true, `mobile ${tab} has no horizontal overflow`);
  }
  console.log(staticOnly ? "PASS: real browser, canvas/sidebar modes, no language field, truthful detector-empty state, and responsive layout." : `PASS: real browser, local API, optical-field preview, canvas/sidebar modes, detector status, and responsive layout. Saved outputs: ${completed || runDesign ? "checked" : "not present; not checked"}.`);
  console.log(`Screenshots: ${screenshots}`);
} finally {
  socket?.close();
  browser.kill();
  staticService?.close();
  await delay(300);
  // The profile is an ephemeral browser helper under this repository's runtime.
  // Do not use recursive cross-shell deletion or target user browser profiles.
  const profileRoot = path.resolve(root, ".runtime");
  if (path.dirname(path.resolve(profile)) === profileRoot && path.basename(profile).startsWith("browser-profile-")) {
    try { await rm(profile, { recursive: true, force: true }); } catch { /* Browser child cleanup can finish after process exit. */ }
  }
}
