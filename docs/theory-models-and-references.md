# 计算模型、公式与参考来源

本文说明首版实际采用的公式、边界条件及外部参考。数值结果以运行目录中的配置、`manifest.json`、求解记录和字段数据为准；参考资料不代表本项目已经运行对应软件。

## 首版实际计算

### 周期单元与邻域：grcwa / RCWA

采用时间因子 `exp(-iωt)`，被动材料复折射率记为 `n + i k`。无源区域的频域 Maxwell 方程为：

```text
∇ × E =  i ω μ H
∇ × H = -i ω ε E
∇ × μ⁻¹ ∇ × E - ω² ε E = 0
```

RCWA 在横向周期方向对介电常数和电磁场作 Fourier 展开，并沿分层方向匹配各层的模态。周期边界满足 Floquet 条件

周期光栅严格耦合波方法的经典参考为 Moharam 与 Gaylord [1]。

```text
F(r + R) = F(r) exp(i k∥ · R)
```

首版为法向入射，因此横向 Bloch 相位为零，横向边界是周期性的。单元和 `3×1` 邻域都表示无限周期重复结构；邻域结果只能检查所选局部的周期耦合，不能替代有限整片器件的全波计算。该 RCWA 路径**没有放置 PML**：它使用周期模态和上下均匀介质中的出射衍射阶处理边界。PML 与周期边界不是同一种条件，也不能互相替代。

求解器分别激励 `Ex`、`Ey`，保存复 Jones 矩阵：

```text
[Ex_out, Ey_out]ᵀ = J_t [Ex_in, Ey_in]ᵀ
```

电场复振幅不能直接当功率透射率。功率由法向 Poynting 通量计算；对法向入射的单一零级透射，通量比含 `n_out / n_in` 因子。项目也将求解器逐衍射级通量与 Jones 零级结果交叉检查。

### 整片器件：局域响应查表 + 标量角谱传播

首版将周期单元的 Jones 响应映射到透镜采样面，再对两个横向偏振分量分别传播。均匀介质中的角谱传播为 [2, 3]

```text
E(x,y,z) = F⁻¹{ F[E(x,y,0)] · exp(i kz z) }
kz = sqrt((n k0)² - kx² - ky²),   k0 = 2π/λ
```

倏逝分量按正向衰减处理，并使用带限、有限采样和窗口检查。实现中的场幅度是功率归一化横向 Jones 代理量，单位为 `sqrt(W/m²)`；`Σ|E|²` 是相应的数学参考强度。它不是完整三维 Maxwell 电场，不能用于声称已得到纵向焦场、高 NA 矢量焦斑或完整成像 PSF。

平面波 / 基模高斯束以用户设定的强度或功率归一化。归一化模式只提供可重复的数学功率参考；即使用户填写了绝对功率或峰值强度，计算结果也不是光源校准或实验测量值。

### 可移动探测截面：从已保存复场做 RS-I 标量积分

探测器读取 `fields.h5` 中超表面出口面的 `output_power_field`，按探测平面的位置、法向、尺寸和采样点直接计算复场；未提供的平面不会绘制为测量结果。当前使用 Rayleigh–Sommerfeld 第一积分的标量形式 [4]：

```text
U(P) ≈ Σq U(Qq) · [zq / (i λ rq²)] · [1 - 1/(i k rq)] · exp(i k rq) · ΔAq
```

其中 `rq` 是源采样点到探测点的距离，`zq` 是两点的轴向间隔，`ΔAq` 为源面采样面积；当前 `k=2π/λ0`，即出口空气折射率取 `n≈1`，不包含空气色散。该接口能让用户转动探测平面并观察所计算的复振幅和 `Σ|E|²`，但它仍是标量衍射近似，不重建缺失的亚网格场，不计算磁场、完整 Poynting 向量、探测面坐标投影、有限基底反射或二次结构，也不是 COMSOL/FDTD 的全矢量探测器结果。输出中记录积分方法、源数据、采样稀疏化、收敛估计和限制条件。

为限制直接核的工作量，源面可能以整块面积平均后参与积分；边界残块的质心和窗口截断会带来空间矩误差。粗细采样只在探测器中心／边缘的少量点比较，属于采样敏感性提示，不是全平面收敛证明。探测器离开源面至少 `0.05λ` 只是避免核的数值奇点，不是模型精度保证。

## 边界条件应该如何选

| 计算对象 | 横向边界 | 传播方向边界 | 首版状态 |
|---|---|---|---|
| 一个周期单元 | 周期 / Floquet 周期 | 均匀介质端口与出射模态 | 已实现：RCWA / grcwa |
| 周期重复的 `3×1` 不同尺寸邻域 | 超胞周期 / Floquet 周期 | 均匀介质端口与出射模态 | 已实现：局部周期耦合抽查 |
| 有限大小的整片 FDTD / FEM | 外边界通常用横向 PML 截断开放空间 | 上下开放边界通常也需 PML 或匹配的端口处理 | 未实现 |
| 当前探测截面 | 无独立体网格边界；使用保存的有限出口面场并在有限口径外补零 | 出口面之后假设均匀空气、只考虑向前辐射 | 已实现：标量 RS-I 后处理 |

周期侧边界会重复计算区域，适用于单元或周期超胞；PML 通过吸收层近似开放空间，适用于有限计算域外缘。若之后采用有限器件 FDTD/FEM，应分别定义侧向和传播方向的截断，并检查 PML 厚度、网格、域尺寸、周期条件和能量平衡的收敛，不能将超胞周期结果称作有限器件结果。

## 开源参考与 COMSOL 方法参考

| 主题 | 参考 | 本项目中的关系 |
|---|---|---|
| RCWA 周期单元 | [grcwa](https://github.com/weiliangjinca/grcwa) | 当前实际调用的开源求解器；运行记录保存版本及离散设置 |
| 元件级超表面设计 | [DFlat](https://github.com/DeanHazineh/DFlat) | 设计参数化与响应映射参考；不是当前首版器件求解后端 |
| RCWA 交叉实现 | [TORCWA](https://github.com/kch3782/torcwa)、[S4](https://github.com/victorliu/S4) | 可用于未来独立交叉校验；当前未在每次任务中调用 |
| 标量传播 / 傅里叶光学 | [waveprop](https://github.com/ebezzam/waveprop)、[POPPY](https://github.com/spacetelescope/poppy) | 角谱传播和光学验证参考；探测器的 RS-I 是项目内直接实现，未调用这两个包 |
| PML / 全波边界条件 | [Meep PML 文档](https://meep.readthedocs.io/en/latest/Perfectly_Matched_Layer/)、[Meep 边界条件说明](https://meep.readthedocs.io/en/latest/Introduction/#boundary-conditions) | 未来有限域 FDTD 的开放边界设置与注意事项参考；Meep 不是当前有限器件后端 |
| FEM / 频域全波建模 | [COMSOL Wave Optics Module](https://www.comsol.com/wave-optics-module) | 可用于以后建立频域 FEM、周期结构与开放边界的独立对照模型；本项目未连接 COMSOL、未运行 COMSOL，也未声称结果已通过 COMSOL 验证 |

公式与方法的出处用于读者核对模型，不表示本项目直接调用了论文作者的软件实现或商业软件。COMSOL 用户可在官方 [Wave Optics Module User's Guide](https://doc.comsol.com/6.3/doc/com.comsol.help.woptics/WaveOpticsModuleUsersGuide.pdf) 中查找周期性/Floquet 条件、端口和 PML 相关章节；实际仿真仍需按几何、入射场和端口参考面建立并做网格与边界收敛。

复核时建议先锁定同一波长、材料 `ε(λ)`、几何、入射偏振、相位约定、周期相位、端口参考面与功率归一化，然后分别做网格 / Fourier 阶数 / PML 收敛。只有独立计算确实运行并保存模型、设置和结果后，才把它标为交叉验证记录。

## 数据和结果标记

- **直接 RCWA**：grcwa 对指定周期、几何、材料及离散设置的求解记录。
- **插值 / 稳健性预测**：在有来源的 RCWA 响应样本之间计算；记录插值范围与拒绝样本。
- **角谱 / RS-I 传播**：由保存的出口复场和明确传播公式得到；在元数据中记录采样和边界假设。
- **COMSOL / Meep 交叉验证**：当前没有。不能仅凭引用手册、公式一致或软件可用而贴上“已验证”。
- **实验实测**：只有用户导入可追溯的测量数据后才可标记为实测，须保留条件及不确定度。

## 参考资料和许可

1. M. G. Moharam and T. K. Gaylord, “Rigorous coupled-wave analysis of planar-grating diffraction,” *Journal of the Optical Society of America* 71 (1981), 811–818. [https://doi.org/10.1364/JOSA.71.000811](https://doi.org/10.1364/JOSA.71.000811)
2. K. Matsushima and T. Shimobaba, “Band-limited angular spectrum method for numerical simulation of free-space propagation in far and near fields,” *Optics Express* 17 (2009), 19662–19673. [https://doi.org/10.1364/OE.17.019662](https://doi.org/10.1364/OE.17.019662)
3. J. W. Goodman, *Introduction to Fourier Optics*, 4th ed., W. H. Freeman, 2017, angular-spectrum propagation treatment.
4. F. Shen and A. Wang, “Fast-Fourier-transform based numerical integration method for the Rayleigh-Sommerfeld diffraction formula,” *Applied Optics* 45 (2006), 1102–1110. [https://doi.org/10.1364/AO.45.001102](https://doi.org/10.1364/AO.45.001102)

引用上游资料不改变其自身许可或论文版权。运行时依赖、材料数据库及许可记录见 [第三方来源与许可](../THIRD_PARTY_NOTICES.md)；材料样品级折射率来源和校验值见项目材料 catalog。除当前明确列出的依赖外，参考仓库不意味着代码已复制或软件已安装。
