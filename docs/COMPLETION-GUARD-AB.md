# 纯文本收尾保护 A/B 设计（CompletionGuard）

> 状态：设计稿，未实际跑批。开关已在产品与 bench 落地，本文只定义如何验证。

## 1. 机制与开关

产品侧 `src/sayacode/agent/graph.py::CompletionGuardMiddleware`：当**本轮已经出现过工具结果**、最后一条模型消息**没有工具调用**、且不是输出截断时，注入一次「继续完成并做真实验证」的提示并跳回模型；**每轮最多 1 次**。

- 产品开关：`Profile.completion_guard`，默认 `False`（`src/sayacode/config.py`）。
- bench 开关：variant 的 `profile_overrides` 白名单已加入 `completion_guard`（`src/sayacode_bench/worker.py::PROFILE_OVERRIDES`），因此 A/B 用**同一个 wheel、两个 variant** 即可，不需要重装产品。
- 该保护是行为改变：开启后每轮可能多一次模型调用（成本与上下文增长），因此必须先 A/B 再决定是否默认开启。

## 2. 假设

| 编号 | 内容 | 判定 |
|---|---|---|
| H1（主） | 开启收尾保护后，提前收工类失败减少，官方 `reward` pass@1 提升 | 配对差 ≥ +10pp 且区间不含 0 |
| H0 | 无差异或负向 | 区间包含 0 或为负 |
| H2（机制） | 收益来自「把纯文本收尾变成继续执行」：guard 触发过的 trial 翻转率显著高于未触发者 | 触发子集翻转率 > 未触发子集 |
| 成本假设 | 每 trial 多 1–2 次模型调用，token 增幅 < 15% | 见护栏 |

**触发场景证据**：vuls `dc496468b9e-r1` 仅 1 次模型调用 / 51 output token 即 `run.completed`；NodeBB `05f2236-r2` 最后停在「Now let me verify…」。注意前者**零工具活动**，当前守卫条件不触发（守卫要求本轮已有工具结果），A/B 需同时报告该子集的命中率，避免把「零工具空转」误算进 guard 收益。

## 3. 样本量

依据 `docs/evaluation/2026-10-29-task-count-power.md`（80% power, α=0.05）：

| 要检测的差异 | 每组题量 |
|---|---:|
| 20pp | 83 |
| 15pp | 147 |
| 10pp | 330 |

推荐：

- **正式**：120 题（按仓库分层随机，含 HARD-51 层）× 2 重复 × 2 组 = **480 trials**（约 13h / ¥24 量级）。可检测约 18pp。
- **小样本冒烟**：holdout 20 题 × 2 重复 × 2 组 = **80 trials**，只看方向与过程指标，不宣称显著。
- 重复用于抵消同题方差；**检测版本差异靠题量，不靠重复**。

## 4. 指标与护栏

**主指标（结果）**
- 官方 verifier `reward` 的 pass@1（题级配对 McNemar；任务块 bootstrap 与仓库簇 bootstrap 区间）。
- 按层报告：HARD-51 与整体分层，不混算。

**过程指标**
- `premature_completion_rate`：trial 结束时无写文件/无执行命令/最后一条为纯文本的比例。
- `guard_trigger_rate`：本轮注入过 `sayacode_continue_nudge` 的 trial 比例；**若 < 5%，样本不足，判为无效应并停止**。
- 每 trial 模型调用数（均值/P95/P99）、input/output token（区分 cache_read）、wall time、超时率。

**护栏（任一不满足即判负向，不进入默认开启）**
1. 已通过题不得回归：现有 holdout 20 题基线 14/20，开启后不得低于基线。
2. 调用数 P99 增幅 ≤ 20%，超时率 ≤ 基线 +2pp。
3. 安全边界不变：`read_only` 与 planner/reviewer 仍不可写；不得因提示产生越权调用。
4. 不得出现无限续写：单 trial 注入次数 ≤ 1（实现已固定），若日志出现 >1 即视为缺陷。
5. 成本预算不得超批（按 `paid-stop-fraction` 止损）。

## 5. 执行命令（不要直接跑，按 env-runner 的路径替换占位）

```powershell
# 0) 重建产品 wheel（含 completion_guard 字段与收尾保护）
cd C:\develop\sayacode
uv build --wheel

# 1) 两个 variant 只差 completion_guard
# guard-off.json: {"id":"guard-off","team_enabled":true,"profile_overrides":{},"instruction_append":""}
# guard-on.json : {"id":"guard-on","team_enabled":true,"profile_overrides":{"completion_guard":true},"instruction_append":""}

# 2) 小样本冒烟：holdout 20 题 × 2 重复 × 2 组
cd C:\develop\sayacode-bench
uv run --frozen python scripts/run_holdout.py --tasks-root <v2/tasks> --wheel <whl> `
  --requirements <req.txt> --profile <profile.json> --variant <guard-off.json> `
  --out C:\sb\tmp\completion-guard\off --repeats 2
uv run --frozen python scripts/run_holdout.py --tasks-root <v2/tasks> --wheel <whl> `
  --requirements <req.txt> --profile <profile.json> --variant <guard-on.json> `
  --out C:\sb\tmp\completion-guard\on --repeats 2

# 3) 正式 120 题 × 2 重复 × 2 variant（manifest 驱动，配对）
uv run --frozen sayacode-bench freeze --phase holdout <其余冻结参数> `
  --variant guard-off=<guard-off.json> --variant guard-on=<guard-on.json> --repeats 2 `
  --output <study.json>
uv run --frozen sayacode-bench run --manifest <study.json> --cache <cache> --output <out>
```

## 6. 判读

- 先看 `guard_trigger_rate`：过低说明机制没被触发，A/B 无意义。
- 再看 `premature_completion_rate` 是否下降，以及**翻转的 trial 是否正是 guard 触发过的**（H2 机制证据）。
- 最后用正式统计层判 pass@1 差异；小样本只作方向判断。
- 若 H1 不成立但成本也几乎不变，可保留默认关闭；若出现回归，直接回滚（改回 `completion_guard=False` 即可，无需改代码）。
