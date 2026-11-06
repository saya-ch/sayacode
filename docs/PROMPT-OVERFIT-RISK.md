# 提示词过拟合风险分析（3.2.5 新增规则）

> 对象：`src/sayacode/prompts.py` 中 3.2.5（commit `13df615`）相对 3.2.4 新增/改写的规则。
> 方法：读改动的 diff、回溯每条规则的来源实验与样本量，按「是否针对个案加、是否经 holdout 验证、是否有副作用」评估。
> 结论：**1 条高风险（dispatch trigger）、1 条中高风险（不改既有测试）、3 条低风险的通用/成本规则**。

## 1. 规则清单与来源

| # | 行号 | 规则（摘要） | 3.2.5 前 | 来源证据 | 样本量/强度 |
|---|---|---|---|---|---|
| R1 | [prompts.py:22](../src/sayacode/prompts.py#L22) | **Dispatch trigger**：跨两个以上可独立调查的领域时派发只读子 Agent | 新增 | `docs/evaluation/2026-10-28-multi-agent-diagnosis.md`：22 次权威试验 0 派发；自主场景加规则前后均 0 派发 | 22 次观测（但规则未进权威 wheel）+ 自主场景 n=1；**弱** |
| R2 | [prompts.py:45](../src/sayacode/prompts.py#L45) | 批处理独立只读调用（列名 read_file/list_directory/grep_search/glob_search），说明省成本与延迟 | 由「Issue independent read-only tool calls together」改写 | `docs/evaluation/2026-10-28-iteration-01.md`：8 题 × 3 重复 A/B，token −10.1%、未缓存 −15.8%、输出 −23.3%，通过 24/24 | n=24 trial；**中（仅成本）** |
| R3 | [prompts.py:47](../src/sayacode/prompts.py#L47) | 新接口与旧接口并存；不改/不删既有公共函数、方法、类、属性 | 新增 | `docs/evaluation/2026-10-28-authoritative-iteration-01.md`：qutebrowser 个案把 `KeyInfo.to_int` 改名 `to_qt` 并改测试，reward 0→1 复跑 | 个案 n=1 + 复跑 1 次；**弱** |
| R4 | [prompts.py:48](../src/sayacode/prompts.py#L48) | 不改既有测试、不改测试函数名；原有测试仍须通过 | 新增 | `docs/evaluation/2026-10-29-iteration-report.md`：5 个失败（element×2、navidrome、qutebrowser、tutao）均与改既有测试相关；加规则复验 3/5 翻盘 | n=5 单次复验；**中** |
| R5 | [prompts.py:49](../src/sayacode/prompts.py#L49) | 现有测试已覆盖时不要新建重复测试文件 | 新增 | `docs/evaluation/2026-10-28-iteration-01.md` 产品侧改动说明 | 无独立实验；**弱** |

R1–R5 都写进**每一轮**的 system prompt（`build_system_prompt` 的 `Execution contract`），即每条规则在每次模型调用都重复计费。

## 2. 过拟合判据

对每条规则问四个问题，任一项为「是」即提高风险：

1. **来源粒度**：是否只由 1–5 个已知失败个案推动？
2. **是否经 holdout 验证**：是否有未参与调参的题集上的配对结果？
3. **是否约束行为而非给方法**：禁令式规则（"never…"）比方法式规则更容易在别的任务上误伤。
4. **是否与机制层重复**：已有机制兜底时，提示词规则可能只增加 token 与误伤面，而非净收益。

## 3. 逐条评估

### R1 Dispatch trigger —— 高风险（最该先验证）

- 来源粒度：22 次权威试验的**观测**是 `task_count=0`，但 3.2.5 之前的发布 wheel 的 prompts.py **没有**这条规则（3.2.4 wheel 的 prompts.py 162 行，无 "Dispatch trigger"）。也就是说，「加规则后 22 次仍 0 派发」从未在权威集上发生；可用的只有本地自主场景 n=1（加规则前后都 0）。
- 结论：这条规则**没有有效证据支持**，且引入了明确的成本风险：若模型开始派发，每个子 Agent 都会重复读文件、独立计费（多 agent 诊断里显式场景 29 次调用 / 36 万输入 token）。
- 副作用：判据「两个以上可独立调查的领域」在真实 SWE 任务上很常见（模块 + 测试、多个文件），可能把「本可单人完成的小改动」也派发出去，反而降低成功率、抬高成本。

### R2 批处理 —— 低风险（成本规则）

- 由 8 题 × 3 重复 A/B 支撑，方向一致（token 下降），但通过率 24/24 已饱和，**没有正确性维度的信号**。
- 规则把具体工具名写死（`read_file/list_directory/grep_search/glob_search`）；工具改名或新增检索工具时规则会过时。属于轻度过拟合到当前工具表。
- 不改变任务路径，只在「同一响应内多发只读调用」，误伤面小。

### R3 不改公共 API / 旧入口保留 —— 中高风险

- 来源是 qutebrowser 的单个事故（n=1）+ 一次复跑；`2026-10-29-final-full-report.md` 也指出「源码修复错误」类失败不是提示词能解决的。
- 规则的正面价值明确（旧入口保留是通用工程规范），但「never rename or delete…unless explicitly says」在任务**确实要求**重命名/删除时可能被模型误读为拒绝执行。需要 holdout 里有「要求删除旧接口」的题来验证不误伤。

### R4 不改既有测试 —— 中高风险

- 证据最强（5 个失败全部相关、3/5 翻盘），但 `iteration-report.md` 自己写明「**规则降低了改测试概率但无法消除**（qutebrowser 那次照样改了）」；且最终报告显示真正兜住的是**机制层** `restore_existing_test_files`，不是提示词。
- 风险：规则是禁令式，遇到「测试本身需要随行为更新」的合法场景会冲突；同时它无法防住「新建测试绕过」（qutebrowser r3）。
- 评估：保留可以，但应视为**辅助**，净收益需与机制层分开计量。

### R5 不新建重复测试文件 —— 低风险

- 无独立实验支撑，属通用实践；规则表述温和。与「新建测试绕过」无直接关系（那条是验证器匹配问题），不要指望它解决。

## 3.5 附：提示与工具集脱钩（已修，优先级高于过拟合讨论）

task-14 发现并已在本轮修复一个真实 bug：

- **现象**：`Dispatch trigger` 段（[prompts.py:22](../src/sayacode/prompts.py#L22)）**无条件**出现在系统提示里，而团队工具只在 `include_team_tools and team_enabled` 时才注册（[application.py:527](../src/sayacode/application.py#L527)）。`single`（`team_enabled=False`）会话里，提示仍教模型「可派发/应派发」。
- **实测后果**（task-14）：模型浪费 3 次 `list_skills` + shell 探测去找派发能力，并有「谎称已派发」的风险。
- **修复**：`build_system_prompt` 增加 `team_enabled: bool = True`（[prompts.py:116-122](../src/sayacode/prompts.py#L116-L122)，默认保持 `True` 的既有行为）；`team_enabled=False` 时由 `_role_rules`（[prompts.py:101-108](../src/sayacode/prompts.py#L101-L108)）把 Dispatch 段替换为 `_NO_TEAM_RULE`（[prompts.py:94-99](../src/sayacode/prompts.py#L94-L99)：「本会话没有团队工具，不要派发，也不要谎称已派发」）。调用处 [application.py:654](../src/sayacode/application.py#L654) 传 `include_team_tools and self.team_enabled`。
- **测试**：`test_prompts.py` 新增 3 例（False 不含 dispatch、True 含、子角色不受影响）；`test_team_configuration.py` 新增 1 例验证 application 真的把开关传进 prompt。
- **与过拟合的关系**：这是**一致性 bug**，不是过拟合；它说明 R1 这类「行为指令」必须与工具注册条件绑定。修掉之后，R1 是否该在 `team_enabled=True` 时继续保留，仍要按 §4 的 holdout 设计验证。

## 4. 需要 holdout 验证的清单（按优先级）

| 优先级 | 规则 | 验证设计 | 通过标准 |
|---|---|---|---|
| **P0** | R1 dispatch trigger | holdout 120 题 × 2 重复 ×（开/关 dispatch 规则）配对；记录 task_count、token、pass@1 | 无 pass@1 回归且 token 增幅 ≤10%；否则删规则 |
| **P0** | R3 不改公共 API | 专设 3–5 道「确实要求重命名/删除旧接口」的题，看是否被规则误伤 | 误伤率 0；qutebrowser 类题仍通过 |
| **P1** | R4 不改既有测试 | 与机制层拆分：A=仅提示词、B=提示词+机制、C=仅机制；配对重复 ≥3 | 提示词相对机制的增量收益 > 0 才保留 |
| **P1** | R5 不重复测试 | holdout 里出现「新建测试绕过」的题，比较新建测试文件数 | 新建测试率下降且 pass@1 不降 |
| **P2** | R2 批处理 | 现有 8 题×3 已显示省 token；在 holdout 上确认无质量回归 | pass@1 不降、token 不升 |
| P2 | todo 单一 plan（3.2.4 已有） | 非 3.2.5 新增，不纳入本轮 | — |

## 5. 建议

1. **不要因为 40 题开发集的个案继续加规则**；先把 R1、R3 放上 holdout 做配对，再决定去留。
2. R1 若无法证明收益，建议删除或改成「仅当用户明确要求并行调查时派发」，避免自主派发带来的成本与质量风险。
3. R4 的价值主要在机制层；提示词规则可作为辅助，但不能把「3/5 翻盘」当作提示词的独立功劳（有机制层混淆）。
4. 每条规则都在每轮重复计费，规则表越长，长上下文任务越吃亏；建议给规则表设上限并定期用 holdout 做「删规则」对照。

## 6. 局限

- 上述来源实验都是单次或小样本（n=1–5），本分析不新增实验，只做证据核对与设计建议。
- 没有可直接读取的「规则加/删」配对 holdout 数据；结论强度以引用文档为准。
