# holdout 权威基线（发布版 3.2.4，2026-10-29）

## 方法

- **被测版本**：正式发布版 v3.2.4（tag `v3.2.4` → `2afe5889`，`git archive` 干净源码构建 wheel）。
- **数据**：官方 SWE-bench Pro V2 的 holdout 24 题（11 个仓库）。
- **判分**：官方 Harbor 验证器，`reward` 唯一判据；不是自评。
- **模型**：本机 Magpie `auto/deepseek-v4.1-flash`（Anthropic Messages 协议）。
- **重复**：每题 1 次；本地逐题流水线（拉镜像→跑→删），峰值磁盘 <15 GB。

## 结果：可评测 17 题，通过 12 题（70.6%）

| 任务实例 | 结果 | 用时 | 输入 token | 模型调用 |
|---|---|---:|---:|---:|
| NodeBB-397835 | PASS | 614s | 542,371 | 24 |
| ansible-de01db | PASS | 306s | 806,774 | 27 |
| ansible-fb144c | PASS | 655s | 1,126,670 | 38 |
| element-web-9bf779 | **FAIL** | 603s | 690,589 | 27 |
| element-web-d40516 | **FAIL** | 761s | 2,698,979 | 85 |
| flipt-756f00 | PASS | 2,531s | 3,707,207 | 119 |
| flipt-8bd360 | PASS | 2,048s | 1,672,026 | 63 |
| vuls-dc4964 | PASS | 2,762s | 1,129,759 | 41 |
| vuls-e52fa8 | PASS | 951s | 214,620 | 16 |
| openlibrary-03095 | PASS | 418s | 841,149 | 30 |
| openlibrary-3f7db6 | PASS | 131s | 109,855 | 10 |
| openlibrary-fdbc0d | PASS | 165s | 353,401 | 19 |
| navidrome-0130c6 | **FAIL** | 702s | 3,080,085 | 100 |
| navidrome-812dc2 | PASS | 951s | 562,646 | 19 |
| qutebrowser-6dd402 | PASS | 697s | 1,214,938 | 49 |
| qutebrowser-c09e14 | **FAIL** | 974s | 1,375,422 | 45 |
| tutao-518182 | **FAIL** | 1,180s | 3,048,067 | 100 |

合计：输入 **23,174,558**，输出 362,194，用时 **16,449s（4.6 小时）**。

## 不可评测 4 项（不是产品失败）

| 实例 | 原因 |
|---|---|
| protonmail-3a6790 | **musl 镜像**：sqlite-vec 无 musllinux wheel，产品装不上 |
| protonmail-cb8cc3 | 同上 |
| qutebrowser-527083 | 安装阶段 `uv TLS 下载失败`（astral.sh 瞬时不通） |
| tutao-de49d4 | 同上 |

另有 NodeBB-4327 与 teleport 2 题未纳入：
- NodeBB-4327：安装阶段 astral TLS 连续失败；
- teleport 2 题：musl 镜像，与 protonmail 同因。

## 关键观察

1. **同一仓库内方差极大**：navidrome 两个实例一个 PASS（562k token）一个 FAIL（3.08M token）；qutebrowser 同样一过一败。这说明**单次结果不能代表能力**，任何对照都必须多重复。
2. **失败集中在长尾探索**：所有 FAIL 的输入 token 都在 1.37M–3.08M，而多数 PASS 在 0.1M–1.2M。**token 爆炸与失败强相关**——这为后续优化指明方向（控制探索发散）。
3. **模型 API 不稳定**：多次运行中出现 1–4 次 `APIStatusError`，但多数最终仍成功；需在统计中单独标注。
4. **musl 镜像占 2 个仓库（4/24 题）**：这不是网络问题，是 C 阶段 runtime-support 未接线，属于覆盖率的结构性缺口。

## 与之前小样本对比的修正

此前用 4 题判断"优化候选无收益"，样本过小且方差主导。现在 17 题可评测样本给出的基线是 **12/17（70.6%）**，可作为后续任何优化的对照基线。

## 原始数据

- 逐题汇总：`C:\sb\tmp\holdout-baseline.json`
- 每个 trial 的官方验证器输出与轨迹：`C:\sb\tmp\hard\pipeline-holdout-release-r1\jobs\<job>\`
- 流水线与对账脚本：`C:\sb\pipelines\`（`run_pipeline.py`、`reconcile.py`、`final_baseline.py`）
