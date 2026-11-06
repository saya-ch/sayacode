# 摘要失败降级核验（3.2.6）

> 目标：确认 v1 `flipt-756f00f-r1` 的「Previous conversation was too long to summarize」丢任务问题在 3.2.6 不再复现。
> 组成：代码审查（必做）+ 3.2.6 单题实测 + summary-on/off 小样本 A/B。

## 1. 问题回顾

v1 的 flipt-756f00f-r1 在上下文压力下触发自动摘要；官方 `SummarizationMiddleware` 在待摘要消息被 `trim_tokens_to_summarize` 裁成空时，返回占位文本 **"Previous conversation was too long to summarize."**，并把它当作摘要写回上下文。结果是真实任务历史被一句占位替换，任务目标丢失。

## 2. 代码审查（3.2.6）

实现文件：[summary.py](../src/sayacode/agent/summary.py)，装配点 [graph.py:316](../src/sayacode/agent/graph.py#L316)（`ResilientSummarizationMiddleware` 替换官方 `SummarizationMiddleware`）。

### 2.1 拦截点（不会再把占位当摘要）

- [summary.py:57-61](../src/sayacode/agent/summary.py#L57-L61)：同步/异步摘要生成都经过 `_require_usable`。
- [summary.py:63-71](../src/sayacode/agent/summary.py#L63-L71)：`text.strip()` 为空 → `SummaryUnavailable("empty_summary")`；包含 `too long to summarize`（大小写不敏感）→ `SummaryUnavailable("too_long_to_summarize")`。
- 异常在 [summary.py:39-55](../src/sayacode/agent/summary.py#L39-L55) 被捕获并进入降级；**官方的 `_build_new_messages(summary)` 不会被调用**，因此占位文本不可能进入消息历史。

### 2.2 降级路径（保留原任务 + 最近 N 条）

- [summary.py:80-102](../src/sayacode/agent/summary.py#L80-L102)：保留 `messages[0]`（首条任务）与 `messages[cutoff:]`（最近 N 条），删除中间消息（`RemoveMessage`），再追加一条 `HumanMessage`，`additional_kwargs = {"sayacode_summary_failed": True, "reason": ...}`。
- [summary.py:104-113](../src/sayacode/agent/summary.py#L104-L113)：裁剪点从目标位置向前找最近的 `HumanMessage`，保证删除区是完整回合，不切断 AI/Tool 配对。
- 去重：已有 `sayacode_summary_failed` 标记时直接返回 `None`，不会重复插入。
- 消息不足 `keep+1` 或待删消息缺 `id` 时，**只插入标记不做裁剪**，最坏情况是上下文不降但任务不丢。

### 2.3 不会掩盖真正异常

`before_model`/`abefore_model` 只捕获 `SummaryUnavailable`；摘要模型的网络/限流异常仍按官方语义向上抛出，不会被伪装成降级。

### 2.4 测试

[tests/agent/test_summary_fallback.py](../tests/agent/test_summary_fallback.py) 4 例：占位摘要降级（保留首条与最近两条、删中间、标记正确、无占位文本）、空摘要降级、正常摘要走官方路径、标记只写一次。运行 **4 passed**。

## 3. 3.2.6 单题实测（flipt-756f00f）

- 产品：自建 `sayacode-3.2.6-py3-none-any.whl`（commit `69f60d9`）。
- 题目：`instance_flipt-io__flipt-756f00f79ba8abf9fe53f3c6c818123b42eb7355`，profile `magpie-hy3-v2.json`（anthropic_messages，192k 上下文，0.8 摘要比例，keep=12）。
- 命令：`run_formal_240.py --sample <单题> --agents sayacode --repeats 1 --concurrency 1`。

**结果（2026-11-03 实测）**

| 指标 | 值 |
|---|---|
| status / reward | `completed` / **1.0** |
| 用时 | 24:15 |
| 模型调用 | 57 次（reported 57，无缺失） |
| input / output token | 2,724,809 / 71,532 |
| checkpoint 消息数 | 136 |
| `sayacode_summary_failed` 标记 | **0** |
| 撞输出上限（32k）次数 | 0 |
| 首条任务消息 | 完整保留（`A code repository is available in the /app directory…`） |

**解读**：该题在 3.2.6 下**没有触发摘要降级**（平均单次输入约 4.8 万 token，未达到 192k 配置的摘要阈值），因此本次实测证明的是「任务目标不丢 + 该题通过」，**没有覆盖降级分支**。降级分支由 §2 代码审查 + 4 个单元测试保证；其真实触发由 §4 的 `summary-on`（触发阈值 16k）验证。

> 说明：该 trial 使用的 wheel 构建于 02:04 的 team-prompt 修复之前，但两次构建的**摘要实现完全相同**，team-prompt 修复与摘要路径无关，不影响本题结论。


## 4. summary-on / summary-off 小样本 A/B

设计：同一题（flipt-756f00f）、同一 wheel（3.2.6）、同一验证器，各 1 次；只切换摘要参数。

- `summary-on`：`summary_trigger_tokens=16384`、`summary_keep_messages=2`、`context_length=65536`、`max_output_tokens=8192`（evals/profile `summary-on.json` 原值；仅把 protocol/base_url/model_id 适配到评测网关 `auto-hy3`）。
- `summary-off`：同文件但 `summary_trigger_tokens=null`（不摘要）。

### 4.1 summary-on（压力摘要）

| 指标 | 值 |
|---|---|
| status / reward | `completed` / **0.0** |
| 用时 / 模型调用 | 271.8s / 13 |
| input / output token | 151,894 / 8,730 |
| checkpoint 消息数 | 24 |
| `sayacode_summary_failed` 标记 | **1**（reason=`too_long_to_summarize`） |
| 撞输出上限 | 0 |

**关键发现（比“是否降级”更重要）**：降级路径确实被触发且按设计工作——占位没有被写回，标记消息存在，最近消息保留。但 `messages[0]` 已经不是原始任务，而是**更早一次“成功”摘要**产生的摘要消息（`additional_kwargs.lc_source=summarization`，内容为 “Here is a summary of the conversation to date…”）。最终模型明确回复：

> “The original task is not recoverable from the retained context. The only preserved message is the meta-notification … No actual task goal, requirements, or decisions were carried over.”

也就是说：**激进摘要（16k 触发 / keep=2）在第一次摘要时就把原任务压成了摘要；后续降级只能保住这段摘要，无法恢复原任务** → 该题 reward 0（同一题在默认配置下是 PASS 1.0）。

**改进建议（未在本轮实现）**：
1. 降级时应识别 `messages[0]` 是否已是摘要消息（`lc_source=summarization`）；若是，说明原任务已丢失，仅靠“保留首条”无效。
2. 更根本：首次摘要前把**原始任务消息**固定保存在 state（或摘要时强制保留原文），降级时用它恢复首条，而不是保留摘要。
3. 或提高摘要触发阈值/保留条数，避免过早摘要（默认 192k/0.8/keep12 的 flipt 未触发摘要且 PASS）。

### 4.2 summary-off（不摘要）

| 指标 | 值 |
|---|---|
| status / reward | `completed` / **0.0** |
| 用时 / 模型调用 | 819.3s / 19 |
| input / output token | 445,415 / 62,638 |
| checkpoint 消息数 | 53 |
| `sayacode_summary_failed` 标记 | 0（未摘要，符合预期） |
| 首条消息 | **原始任务完整保留** |
| final.response | **空字符串** |

**关键发现**：消息 33/41/50/52 的 `usage_metadata.output_tokens = 8192`（正好等于 `max_output_tokens=8192`），且 content 只有 `reasoning`、没有 `text` 或 `tool_call`；随后各插入一条 `sayacode_continuation` 消息（34/42/51）。即：

1. **截断续写中间件在真实环境被触发**（`output_tokens == max_output_tokens` 且无 tool_calls → 续写），印证 3.2.6 的截断修复有效；
2. 但 `hy3` 的 reasoning 一次就吃光 8192 输出预算，续写 3 次仍全是 reasoning，最终 `final.response=""`。

### 4.3 A/B 结论

| 配置 | 任务保留 | 降级标记 | token(in/out) | 调用 | 用时 | reward |
|---|---|---|---:|---:|---:|---:|
| 默认 profile（192k / 32k 输出 / 0.8 / keep12） | 是 | 0 | 2.72M / 71.5k | 57 | 24:15 | **1.0** |
| summary-on（16k 触发 / keep2 / 64k / 8k 输出） | **否**（首条已是摘要） | 1 | 151.9k / 8.7k | 13 | 4:32 | 0.0 |
| summary-off（无摘要 / 64k / 8k 输出） | 是 | 0 | 445.4k / 62.6k | 19 | 13:40 | 0.0 |

**结论**：
- 降级机制本身按设计工作（不写占位、标记正确、最近消息保留）。
- 但**降级无法挽回已被前一次成功摘要覆盖的原任务**：on 配置下首条已是摘要，模型明确说原任务不可恢复。
- 两个 evals profile 都因 `max_output_tokens=8192` 对 reasoning 模型过小而失败（off 尤其明显：reasoning 吃光预算 → 续写循环 → 空响应）。
- **默认评测 profile（32k 输出）该题 PASS**，说明问题主要出在 profile 预算与摘要激进度，而非降级代码。



## 5. 结论

1. **3.2.6 的摘要降级有效**：占位摘要与空摘要都不会再被写回；降级会保留首条消息与最近 N 条、插入 `sayacode_summary_failed` 标记（可被审计读取）。4 个单元测试 + 代码审查确认。
2. **默认评测配置下不再丢任务**：3.2.6 在默认 profile（192k / 32k 输出 / 0.8）跑 flipt 单题 **reward 1.0**，任务消息完整、无降级标记、57 次调用。
3. **但降级不能挽回更早的摘要损失**：`summary-on`（16k 触发 / keep=2）首次摘要就把原任务压成摘要，降级只能保住这段摘要，模型明确回复「原任务不可恢复」→ reward 0。若要彻底防丢任务，需要「原始任务消息固定保留」的机制（本轮未实现，仅建议）。
4. **evals profile 的 8192 输出预算不适合 reasoning 模型**：`summary-off` 实测 reasoning 吃光 8192 → 截断续写真实触发 3 次仍无正文 → 空响应。正式 A/B 应使用 `max_output_tokens ≥ 32000`。
5. **局限**：每档仅 1 次、单题，只作方向性判断；不构成统计结论。若要定量结论，需按 `docs/COMPLETION-GUARD-AB.md` 的样本量设计做配对重复。
6. **过程说明**：`summary-on` 首次尝试因 `openai_chat_completions` 端点 404 失败（profile 适配问题），第二次因旧 job 目录冲突未真正运行；最终改用 `anthropic_messages` 端点与干净 out 目录重跑，表中只列有效样本，失败尝试不计入对比。另：本地 installer 预置曾因上传前未建目录导致 `docker compose cp` 失败（已修复为上传前 `mkdir -p`）。

