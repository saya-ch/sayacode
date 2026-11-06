# 多 Agent 协作诊断（2026-10-28）

## 结论先行

- **异步子 Agent 机制端到端可用**：显式要求协作时，真实派发了 planner 与 reviewer 两个子任务，0 失败，功能检查全部通过。
- **模型不会自主派发**：22 次权威 SWE-bench Pro 试验中 team 工具全部可用，但 `task_count=0`、协作工具调用 0 次。
- **加入「派发触发规则」后仍未触发**：自主版场景（不显式要求派发）在加规则前后都是 `task_count=0`。

## 证据

### 1. 权威运行中的协作使用率

| 指标 | 结果 |
|---|---|
| 权威试验数 | 22 |
| 派发过子任务的试验 | **0** |
| 协作工具事件出现次数 | **0** |
| team_enabled 是否暴露 | 是（bench worker 传 `team_enabled=true`） |

排除"配置没开"的可能：worker 明确传入 `team_enabled`，`delegate_to_subagent` 等工具在工具表里。

### 2. 显式协作场景（要求派发）

`evals/scenarios/collaboration.jsonl`，要求派发 planner + reviewer：

| 指标 | 结果 |
|---|---|
| 结果 | completed / 通过，132s |
| task_count | **2** |
| child_roles | planner, reviewer |
| 失败子任务 | 0 |
| model calls / 输入 token | 29 / 360,928 |

**机制可用，且子任务能正常完成并把结论交回主线。**

### 3. 自主派发诊断（不要求派发）

由显式场景派生出 `collaboration-autonomous.jsonl`（功能要求相同，去除派发指令）：

| 版本 | task_count | model calls | 输入 token | 结果 |
|---|---:|---:|---:|---|
| 加规则前 | 0 | 8 | 60,054 | 通过，440s |
| 加规则后 | 0 | 12 | 102,554 | 通过，310s |

加入规则（"当请求跨两个以上可独立调查的领域时派发只读子 Agent，并在其运行时继续自己的工作；单个小修复不要派发"）后，**仍未自主派发**，且 token 更高（单次样本，不能定性）。

## 判断

- 该任务规模仍是"单仓库中等改动"，模型判断自己做更快，这是**合理的成本收益判断**，不一定是缺陷。
- 要证明多 Agent 的净收益，需要**任务本身确实可并行拆分**（跨模块、多个独立子目标），并做配对重复与成本统计；现有 32 题自构造集和 SWE-bench dev 单题都不满足。
- 触发规则保留与否应看后续在"可并行任务"上的实验；当前证据不足以宣称它有益，也不足以判定有害。

## 复现

```
python -m evals.run run --catalog evals/scenarios/collaboration.jsonl \
  --split dev --repeats 1 --concurrency 1 --variants team \
  --profile evals/profiles/magpie-deepseek.json --output <仓库外目录>
```

原始结果：`C:\develop\sayacode-evaluations\20261028-collab-01`（显式）、`...20261028-collab-autonomous-before/after`（自主）。
