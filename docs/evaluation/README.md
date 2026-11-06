# SAYACODE 评测工作记录（主索引 / 台账）

> 🛑 **本文件已归档为历史记录（2026-11-07 标注）。它不是当前成绩的权威来源。**
>
> **当前权威数字看 `C:\develop\sayacode-bench\docs\RESULTS-LEDGER.md`。**
>
> 原因：本文件记录的是 **2026-10-21 ~ 10-29 的产品自建评测期**，与之后的 bench 正式评测在**六个维度上全都不同**，两边的数字**不可比较、不可互相替代**：
>
> | 维度 | 本文件（历史） | bench（现行） |
> |---|---|---|
> | 模型 | `auto/deepseek-v4.1-flash`（1M 上下文） | `auto-hy3`（192k） |
> | 题集 | 自造 32 题 / SWE-bench Pro holdout 24 题 | SWE-bench Pro V2 `sample-40-final` 40 题 |
> | 分母 | 可评测题数（24 减 4 musl = 20） | planned = 题数 × 重复（40×2 = 80） |
> | 制品 | v3.2.4（commit `2afe5889`） | 3.2.5 本地 wheel / 3.2.7 PyPI |
> | 判分 | 官方 verifier reward | 同左，但另有 planned/observed/partial 三口径 |
> | 缺失处理 | 未分类 | 7 类分类 + worst/best 界 |
>
> **特别提醒**：「本文件 §2 的 70.0%」与「bench 的 78.8%」**不是同一个东西的两次测量**，任何把它们并列或比较的表述都是错的。
>
> 本文件保留的价值在于：① §3 台账记录了 10-21~10-29 的真实实验序列（含被推翻的结论与推翻理由）；② §1.4 的环境适配经验（ghcr 镜像源、清华源、musl 预检、TLS 根因）至今仍适用；③ 若干失败归因（"5/5 与改既有测试相关"、机制层测试保护）是后续工作的输入。

> **本文件是 2026-10-29 之前 SAYACODE 自建评测的唯一入口与结论台账。**
> 最后更新：2026-10-29（内容未再维护；顶部横幅为 2026-11-07 追加）

---

## 0. 维护约定（每次实验必读）

1. **每次实验结束**：在第 3 节"结论台账"追加一行，必须包含：日期、实验名、数据集、样本量 n、量化结果、结论强度（强/中/弱）、原始数据路径、详细文档链接。
2. **基线变化时**：更新第 2 节"当前权威基线"的数字，并在变更日志记录旧值→新值。
3. **新文档命名**：`docs/evaluation/YYYY-MM-DD-<topic>.md`，并在第 5 节索引登记。
4. **状态可能反转**：任何结论若后续被更多重复证伪，**必须显式标注"已推翻"并写明原因**（本项目已发生过：单次 qutebrowser 0→1 后被否证）。
5. **强度定义**：强 = n≥5 或效应量极大且方向一致；中 = n=2–4 或方向一致但方差可见；弱 = n=1 或触发次数极少。

---

## 1. 评测体系

### 1.1 数据集

> ⚠️ **术语表（2026-11-07 追加）：「holdout」在本项目有三重含义，引用时必须写明是哪一种。**
>
> | 说法 | 实际指 | 出处 |
> |---|---|---|
> | 产品 evals 的 holdout | **自造** 32 题里的 24 题（20 自动 + 4 人工） | `evals/README.md` |
> | 本文件 §1.1 的 holdout | **SWE-bench Pro V2 官方 holdout split** 的 24 题 | `studies/primary-selection-v1.json` |
> | bench 的 holdout | **从 SWE-bench Pro V2 dev split 里选的 40 题**（最反直觉） | `sayacode-bench/studies/sample-40-final.json` |
>
> 三者互不重叠。只写「holdout」而不指明是哪一种，是过去多份文档互相矛盾的直接原因。

- **权威集**：SWE-bench Pro V2，642 题 / 11 仓库，revision `66f92766bba642462d4bbe5479e83f91f9211862`。
- **主选**：`studies/primary-selection-v1.json` → dev 8 + holdout 24（eligible 640）。**这里的 holdout 指官方 holdout split。**
- **自造集已弃用**：`evals/` 下 32 题正确率已饱和（dev 8/8 = 100%、holdout 20/20 = 100%，出处是 `2026-10-28-iteration-01.md:23-24`，**不是** `2026-10-21.md`——后者记的是 65.0%/63.3% 与 43 次连接失败）。仅保留作功能诊断/协作诊断场景（如 `evals/scenarios/collaboration.jsonl`、`evals/scenarios/context.jsonl`）。
- **结构性不可评测**：musl 基础镜像（teleport、protonmail，共 4/24 题）——sqlite-vec 0.1.9 无 musllinux wheel。

### 1.2 运行方式（官方验证器）
- Harness：Harbor 0.24.0；入口 `sayacode_bench.execution.run_official_trial`。
- 适配：`SayacodeAgent` 在任务容器内安装产品 wheel；agent 网络 `no-network`，经 `IsolatedDockerEnvironment`（`saya-model-gateway`）访问本机 Magpie。
- 模型：本机 Magpie 网关 `auto/deepseek-v4.1-flash`（Anthropic Messages 协议）。
- 判分：官方 verifier 的 `reward`（0/1）为唯一判据；`success` 由 reward 决定。

### 1.3 关键脚本与路径
| 用途 | 路径 |
|---|---|
| 逐题流水线（拉→跑→删） | `C:\sb\pipelines\run_pipeline.py` |
| 结果对账（trial 级） | `C:\sb\pipelines\reconcile.py` |
| 基线汇总 | `C:\sb\pipelines\final_baseline.py` |
| 单实验跑批模板 | `C:\sb\tmp\run_*.py` |
| 镜像拉取重试工具 | `C:\sb\tmp\pull_util.py` |
| 原始结果根目录 | `C:\sb\tmp\hard\<experiment>\summary.json` |

### 1.4 环境适配（网络/安装）
- ghcr 拉取：`ghcr.nju.edu.cn` 镜像源（实测 8 MB/s vs 直连 40 KB/s）。
- 容器内安装：`UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple`（7.4 MB/s），`UV_HTTP_TIMEOUT=600`，安装超时 2400s + 整链重试 3 次。
- musl 预检：拉取后 5 秒判定基础系统，避免每题 10 分钟无效安装。
- 已知根因：Clash `verge-mihomo` 核心偶发不启动会导致 astral.sh 不可达（表现为 `uv TLS 下载失败 curl exit=35`）。

---

## 2. 历史基线（发布版 v3.2.4，commit `2afe5889`）

> 🛑 **这不是"当前"基线。** 它对应 **v3.2.4 + deepseek-v4.1-flash + SWE-bench Pro holdout 24 题**。
> 当前成绩见 bench 的 [RESULTS-LEDGER](C:/develop/sayacode-bench/docs/RESULTS-LEDGER.md)（hy3 + 40 题 + 3.2.5/3.2.7）。
> 两组数字的模型、题集、分母、制品全都不同，**不可比较**。

| 指标 | 数值 | 备注 |
|---|---|---|
| holdout 总题 | 24 | |
| musl 不可评测 | 4 | teleport×2、protonmail×2 |
| **可评测** | **20** | |
| **通过** | **14（70.0%）** | 官方 reward |
| 失败 | 6 | element×2、navidrome-0130、qutebrowser-c09e、tutao-518、NodeBB-4327 |

逐题数据：`C:\sb\tmp\hard\holdout-baseline.json`；文档 `2026-10-29-holdout-baseline.md`。

---

## 3. 结论台账

> 每行一次实验。强度见第 0 节定义。**特别注意"已推翻"标记**。

| 日期 | 实验 | 数据集/题 | n | 量化结果 | 强度 | 文档/数据 |
|---|---|---|---|---|---|---|
| 10-21 | 自造 32 题评测集（**多 agent 对照，非饱和结论**）| evals 32 | 20/组 | single **65.0%**（39/60）、team **63.3%**（38/60）；**43/120 因连接类失败未完成**；结论「不能证明多 Agent 提升质量或速度」 | 中（负结果） | `2026-10-21.md` |
| 10-28 | 自造 32 题**饱和**确认 | dev 8 + holdout 24 | 1 | dev **8/8 = 100%**、holdout **20/20 = 100%**；结论「该集已被 DeepSeek-V4.1-Flash 完全饱和，无法再衡量改进」 | 强 | `2026-10-28-iteration-01.md:23-24,28` |
| 10-28 | 提示词精简 A/B（-30%） | dev 7–8 | 2 版 | **失败**：token 未降（13.7M→25.5M，含 49min 超时），提示词仅占输入 ~5% | 强（证伪） | `2026-10-28-ab-condensed.md` |
| 10-28 | 提示词兼容规则（不改既有测试/接口） | qutebrowser | 1 | 单次 reward 0→1，1869/1869，390s，1.30M token（vs 761s/3.33M）| **已推翻（单次）**：后续同规则仍 FAIL | `2026-10-28-authoritative-iteration-01.md` |
| 10-28 | 发布版 vs 优化候选配对 | 4 题 | 1 | 成功持平 2/3，候选 token +27.6%/时长 +40%（qutebrowser 两版都 FAIL） | 中 | `2026-10-28-release-baseline-ab.md` |
| 10-28 | 多 agent 协作诊断 | 权威 22 + 协作场景 | 22/1 | 22 次**0 自主派发**；显式要求时真实派发 2 子任务/0 失败/132s/36万 token | 中 | `2026-10-28-multi-agent-diagnosis.md` |
| 10-29 | 失败归因（5 个 FAIL） | holdout 5 | 5 | **5/5 与"改既有测试"相关** | 强 | `2026-10-29-iteration-report.md` |
| 10-29 | 兼容规则复验（失败题重跑） | 5 | 1 | **3/5 翻盘**（element×2、navidrome-0130），12/17→15/17 | 中 | `2026-10-29-iteration-report.md` |
| 10-29 | Token 控制 40k（ClearToolUses+早摘要） | 6 | 1 | 总 token **17.18M→3.11M（-82%）**，成功 4/6 持平（+navidrome / -flipt-756） | 强（-82%）/中（成功率） | `2026-10-29-iteration-report.md` |
| 10-29 | Token 调参 60k | 4 | 1 | **更差**：navidrome 由 PASS→FAIL；结论 40k 优于 60k | 中 | `2026-10-29-final-full-report.md` |
| 10-29 | 机制层测试保护 | qutebrowser/tutao | 6 | 单测+bench 1362 passed；**真实触发 1 次**并正确恢复既有测试，但该题仍 FAIL（源码没修对） | 弱 | `2026-10-29-final-full-report.md` |
| 10-29 | TLS 补齐重试 | 2 | 1 | qutebrowser-5270、tutao-de49 由 N/A→**PASS**（941s/848s） | 中 | `2026-10-29-final-full-report.md` |
| 10-29 | 产品 `context_edit_keep` 可配置 | 单测+全套 | — | 字段/校验/传参到位；产品全套 481 passed + **4 pre-existing 失败**（已用 git stash 证明与本次改动无关：test_execution/test_repository/test_task_safety/test_security_boundaries） | 强（无回归） | 本文件 §4 |
| 10-29 | worker 机制扩展：剔除新增测试文件 | 单测 | — | `strip_new_test_files` 实现，2 项单测通过，bench 全套 **1363 passed** | 强（无回归） | `tests/worker/test_restore_test_files.py` |
| 10-29 | keep=8 调参（trigger=40k, wheel-keep8） | flipt/navidrome/qutebrowser | 1 | flipt **FAIL**(1.86M)、navidrome **FAIL**(781k)、qutebrowser **PASS**(659k) → **无一组参数同时保住三题** | 中 | `C:\sb\tmp\hard\keep8\` |
| 10-29 | C 阶段 musl 接线评估 | 4 题（teleport/protonmail） | — | 实测：sqlite-vec 0.1.9 **无 sdist**（仅 5 个平台 wheel）；alpine 编译链可装但 pip 无从源码构建 → **结论不实施**，需 1.5–2.5 天且不确定 | 强（实测） | `2026-10-29-c-stage-musl-assessment.md` |
| 10-29 | 基线重复实验（flipt/navidrome ×3） | 2 题 | 3 | flipt **3/3 PASS**（token 917k/1.66M/3.71M）；navidrome **FAIL/FAIL/PASS**（基线 1/3 PASS） | 强（重复） | `C:\sb\tmp\hard\baseline-reps\` |
| 10-29 | 规模化评测评估（HARD-51 + 云） | 分析 | — | HARD-51 共 51 题、与现集仅重叠 1 题、可评测 40 题、本地已具备；官方无托管服务，云 VM+隧道可行 | 强（实测文档） | `2026-10-29-scaled-evaluation-assessment.md` |
| 10-29 | 云评测成本方案（≤¥50） | 估算 | — | 4核8G+100G 按量：40 题×1 约 3h/¥3–5；40 题×5 重复约 12–15h/¥15–25；¥50 够 10–25 批；Magpie Docker 可搬配置 | 中（估算） | `2026-10-29-cloud-eval-cost-plan.md` |
| 10-29 | 题目数量与统计功效 | 分析 | — | 全量可评测 **519 题**；估计通过率 60/100/200 题→±11/9/6pp；检测差异（80% power）10/15/20pp 需 330/147/83 题每组 | 强（公式+实测池） | `2026-10-29-task-count-power.md` |
| 10-29 | **评价方法论**：partial credit + 过程指标 | 21 trial 复用 | 21 | 同批数据 pass@1=66.7% vs **平均 partial=81.8%**；3 个 reward=0 实为 partial≥75%；空转 1 个、平均 61 步；落地 `analyze_eval.py` 一条命令出全部指标 | 强（实测） | `2026-10-29-evaluation-methodology.md` |

**题量建议**：¥50 内最优 **120–180 题 × 2 重复**（¥12–18，13–19h），可检测约 15–18pp 差异；**测不出 10pp 以下的优化效应**（需 300+ 题/组），须在报告中如实标注。

**⚠️ 结论修正（样本过小实证）**：此前"keep3/40k 翻盘 navidrome-0130"**已推翻**——基线重复显示 navidrome 无控制时也有 1/3 PASS，单次 PASS 不足以证明翻盘。flipt-756 的"基线 3/3 PASS vs token 控制 0/3"仍成立。

### Token 控制全参数矩阵（关键边界，官方验证器）

| 题 | 基线（无控制） | keep3/trigger40k | keep3/trigger60k | keep8/trigger40k |
|---|---|---|---|---|
| flipt-756 | **PASS 3.71M** | FAIL 813k | FAIL 733k | FAIL 1.86M |
| navidrome-0130 | FAIL 3.08M | **PASS 1.06M** | FAIL 169k | FAIL 781k |
| qutebrowser-c09e | FAIL 1.38M | 未测 | 未测 | **PASS 659k** |

**结论（修正此前"40k 推荐"的表述）**：token 控制效果**高度任务依赖，无普适最优参数**。40k/keep3 在 6 题样本上 -82% token、成功率持平（+navidrome / -flipt）；但"救回 flipt-756"的目标**未达成**（三种参数全 FAIL，该题属长上下文依赖型）。keep=8 反而使 navidrome 失败。若上线 token 控制，需接受"省 token 但少数长上下文任务受损"。

### 关键机制发现（跨实验）
- **Token 爆炸本质**：工具输出仅占输入 ~5%，**95% 是每轮重复携带的历史** → 杠杆在上下文管理而非提示词长度。
- **失败与 token 强相关**：失败题输入 137万–308万；多数 PASS 在 11万–120万。
- **机制层 > 提示词层**：改既有测试可被机制保护兜住；源码修复错误/新建测试绕过则不能。
- **单次方差极大**：同题可一过一败，任何对照需 ≥5 重复。

---

## 4. 待办与进行中

| 状态 | 事项 | 备注 |
|---|---|---|
| 进行中 | token keep 调参（keep=3→8，trigger=40k） | config/graph 已改+测试+wheel-keep8 已建；正在跑 flipt-756/navidrome-0130/qutebrowser-c09e |
| 进行中 | 机制保护扩展：剔除"新建测试文件"绕过 | `strip_new_test_files` 已实现+单测+bench 1363 passed，待实测触发 |
| 待办 | 关键题 ≥5 次重复建立统计功效 | flipt-756、navidrome-0130、qutebrowser-c09e |
| 已评估（暂不做） | C 阶段 musl 接线（4/24 题） | 1.5–2.5 天；sqlite-vec 无 sdist 需自行编译，见 `2026-10-29-c-stage-musl-assessment.md` |

---

## 5. 文档索引（时间序）

| 文档 | 主题 |
|---|---|
| `2026-10-21.md` | 自造评测集（已弃用） |
| `2026-10-28-iteration-01.md` | 首轮迭代 |
| `2026-10-28-authoritative-iteration-01.md` | 权威集首轮（含 qutebrowser 事故） |
| `2026-10-28-ab-condensed.md` | 提示词精简 A/B（失败） |
| `2026-10-28-dev-full-status.md` | dev 全量状态 |
| `2026-10-28-multi-agent-diagnosis.md` | 多 agent 协作诊断 |
| `2026-10-28-release-baseline-ab.md` | 发布版为基线配对 |
| `2026-10-29-holdout-baseline.md` | holdout 权威基线 |
| `2026-10-29-iteration-report.md` | 失败归因 + 三层优化 |
| `2026-10-29-final-full-report.md` | 全量最终报告 |

---

## 6. 变更日志

| 日期 | 变更 |
|---|---|
| 2026-10-29 | 建立本主索引与台账；汇总 11 份历史文档结论；标注 qutebrowser 单次结论已推翻；登记进行中任务（keep 调参） |
| 2026-10-29 | 登记 `context_edit_keep` 产品改动（含 4 个 pre-existing 失败已证明无关）、worker 剔除新增测试扩展（bench 1363 passed）、keep=8 实验进行中 |
