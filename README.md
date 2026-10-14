# 超表面开源设计平台 v0.1

一个可独立安装的 Python 开源原型：将需求与用户光束配置连接到样品级材料筛选、真实 RCWA 单元库、圆柱金属透镜布局、光场传播、误差评估、交互可视化及 GDSII/OASIS 输出。代码采用 GPL-3.0-or-later；材料数据单独保留 CC0 来源及校验值。

当前能力是**单波长、单层介质圆柱、法向入射、低 NA 小口径透镜**。计算完成表示该版本结果已生成，不代表已通过全部制造/实验验收；结果与报告分别记录数值检查、局域周期模型偏差与待复核项。

## 本机快速开始

Windows 首次下载或克隆本项目后，在 PowerShell 中切换到本目录并运行 `./bootstrap.ps1`，安装本项目使用的 Python 和锁定依赖；首次安装需要网络。安装完成后可双击 `open-workbench.cmd`，它会在后台启动服务并打开浏览器；重复运行会复用已经就绪的服务。也可在 PowerShell 中切换到本目录启动：

```powershell
.\start-workbench.ps1
```

关闭启动终端或浏览器后，后台服务继续运行；注销或重启后需再次打开启动入口。日志保存在 `.runtime/workbench-8765.stdout.log` 和 `.runtime/workbench-8765.stderr.log`，监听进程号保存在 `.runtime/workbench-8765.pid`。`-NoBrowser` 只启动服务；`-Foreground` 在未启动服务时保留前台调试方式，调试终端需要保持打开。

浏览器访问 http://127.0.0.1:8765 。首页打开可交互设计画布，参数和仿真控制在侧栏；提供器件结构、入射光场、结果三种画布模式，可拖动旋转视角和单独移动探测器。界面不放自然语言需求输入框；侧栏助手显示可配置的本地 provider，并可基于当前参数启动自动设计。结构尺寸和色标以材料标识，运行前会标明几何示意，运行后再显示已保存的版图或数值场数据。探测器平面支持位置、法向、宽高、面内角度和 64 / 128 采样，截面来自已保存出口场的标量 RS-I 计算；缺数据时明确显示不可用，不画占位结果。倾斜入射光束可以预览，完整器件设计仍限法向入射响应库。

用 VS Code 打开 `metasurface-platform.code-workspace`。`.vscode` 已配置本项目 Python、Web 服务、端到端示例与测试任务；Python 扩展提供调试功能。

## 从源码安装与重跑

Windows 可运行 `bootstrap.ps1`，在本项目内下载固定 uv/Python 并安装锁定依赖，不要求预装全局 Python。首次安装需要网络。

```powershell
./bootstrap.ps1
.\.venv\Scripts\python.exe -m metasurface_platform parse '设计一个在532 nm工作的聚焦超表面'
.\.venv\Scripts\python.exe -m metasurface_platform run examples/metalens-532nm.yaml
.\.venv\Scripts\python.exe -m pytest --disable-warnings
```

Linux/macOS 使用 Python 3.12：

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-lock.txt
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/python -m metasurface_platform run examples/metalens-532nm.yaml
.venv/bin/python -m metasurface_platform serve --port 8765
```

已运行验证的宿主是 Windows/Python 3.12.11。Linux/macOS 安装命令尚未在本机执行；首次端到端扫描耗时取决于 CPU，已有响应库可按完整缓存键复用。

## 第一版边界与计算依据

- 任务：450–700 nm，口径 2–80 μm，空气中 NA≤0.3。更大口径请求保留原参数并报范围错误，不自动缩成示例。
- 需求接口：界面从结构化参数直接设计，不显示自然语言输入框；CLI/API 保留“功能＋波长”和“传统光学参数”两类解析接口。默认解析器是确定性中文/英文规则，解析结果只是配置，不构成计算性能。
- 可选本地 Ollama 结构化解析接口：设置 `METASURFACE_OLLAMA_MODEL` 后启动服务，默认端点 `http://127.0.0.1:11434`；适配器采用 JSON schema 与输出检查。界面只显示 provider 配置状态，不假定模型已经可连通；数值性能始终来自数值求解。
- 光束：平面波/高斯束；归一化、峰值强度、整束功率或孔径内功率；线/左右圆/椭圆 Jones 偏振。默认归一化为数学功率参考，可算效率比，不代表实测光源功率。
- 材料：包内含 39 条公开来源供查询；本版自动设计候选仅 TiO2/Siefke 与 Si3N4/Luke，基底 SiO2/Malitson。无 k 样品需显式允许无损假设，来源缺值仍保留 null。
- 单元：grcwa 直接求复 Jones 响应及各阶通量，联合比较材料/周期/高度/半径。物理 Jones 与功率振幅分别处理。主库 81 谐波请求/128 网格，选定尺寸追加独立谐波与网格检查；未通过的尺寸剔除。
- 设计：复响应残差、真实响应导数产生的一阶误差惩罚、半径容差余量与制造约束；公共相位搜索。属于离散候选上的稳健选择，没有全局最优保证。
- 传播：带限标量角谱，补零及空间重采样比较；输出相干焦斑和角谱，不作为未经定义的成像 PSF/MTF或全矢量焦场。
- 近场：一个 3×1 周期超胞的实际 Maxwell 电场。标为 E/E_inc 场增强，不能当成整片 FDTD 或局部功率密度。
- 探测截面：读取指定设计版本 `fields.h5` 的出口复场，按用户平面执行直接标量 RS-I；报告标量空气传播假设、源面粗化及局部采样对照。它不是全矢量测量或 COMSOL 结果。
- 容差：共同、空间相关、局部直径误差与共同高度误差；真实三高度库内的有界复响应插值，越界样本拒绝。输出假设误差下的性能分布，尚无真实制程良率。
- 版图：GDSII/OASIS 圆柱加工层、单位/层号/位置/实际多边形尺寸/间隙回读；中文 Windows 路径经过实际写读测试。尚无加工厂专用标记、曝光分割与制造签核。
- 验证局限：半无限基底入射、空气出射；未加入基底背面、侧壁角、粗糙度、材料漂移、有限整器件 FDTD 与实验模型反演。

完整边界见 [验证范围](docs/validation-scope.md)，各第三方许可见 [来源与许可](THIRD_PARTY_NOTICES.md)。

RCWA、角谱传播、可移动探测截面（RS-I）、周期 / PML 边界选择及开源 / COMSOL 方法参考见 [计算模型、公式与参考来源](docs/theory-models-and-references.md)。COMSOL 目前仅作建模方法参考，不是已接入的求解后端或已完成的交叉验证。

## 版本数据与交付文件

每次计算在 `runs/<独立ID>/` 保存：

| 文件 | 用途 |
|---|---|
| config.yaml / config.json | 实际采用参数、用户光束、工艺与误差假设 |
| manifest.json | 材料/代码校验值、依赖/求解器版本、随机种子、证据类型 |
| unit-library.h5 / height-minus.h5 / height-plus.h5 | 直接计算的复 Jones 响应及通量 |
| fields.h5 / pillars.csv | 复光场、传播/近场数组、逐柱位置与几何 |
| convergence.json / propagation-check.json / coupling-check.json | 数值收敛与模型边界记录 |
| tolerance.json / layout-check.json | 假设容差与版图回读结果 |
| metalens.gds / metalens.oas | 层号 1、微米 user unit、1 nm 数据库精度 |
| plots/ / report.md / result.json | 数值图、报告及前端可读取结果 |

`cache/` 只复用与材料、几何、求解器和设置完全匹配的库。包内数据是用于独立安装的版本化快照；其原始来源来自正式项目的材料目录，不改变外部兼容链接。

## 用户测量接入

复制 `examples/measurement-template.csv`，填写 `metric,value,unit,uncertainty,source`。支持透射率/聚焦效率（unit=`ratio`）、FWHM/焦距（unit=`um`）及明确同条件的峰值比；每行必须给真实数据来源，输入光束、ROI和检测条件应与预测一致。

```powershell
.\.venv\Scripts\python.exe -m metasurface_platform compare runs/<设计ID> measurements.csv
```

比较结果保存到新的 `feedback/` 记录，不覆盖事前预测。本版实现指标对照，材料/制程反演与独立实测校准仍待接入；没有测量时不生成实验数据。

## 测试与浏览器验证

Python 测试检查材料范围/缺值、光束/单位、解析与限值、归一化、解析传播基准、真实 Fresnel/RCWA、超胞、版图回读、容差及可替换模型接口。

本机有 Chrome/Edge 与 Node 时，可执行无 npm 依赖的真实浏览器检查：

```powershell
node tests/browser-smoke.mjs --url http://127.0.0.1:8765
```

独立重验已保存设计：`python scripts/verify_run.py runs/<设计ID>`。本版测试基线与实际运行边界见 [首版验证记录](docs/first-release-validation.md)；界面截图位于 `docs/screenshots/`。
