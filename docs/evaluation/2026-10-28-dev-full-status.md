# 权威 dev 全集状态汇总（2026-10-28）

本文汇总在官方 SWE-bench Pro V2 dev 选择（8 题）上的全部真实运行，模型统一为本机 Magpie `auto/deepseek-v4.1-flash`（Anthropic Messages 协议），验证器为官方 Harbor 验证器。

## 逐题状态

| 任务 | 语言 | 有效运行 | 通过 | reward | 备注 |
|---|---|---:|---:|---|---|
| navidrome | Go | 1 | 1 | 1.0 | 早期 wheel |
| openlibrary | Python | 2 | 2 | 1.0 / 1.0 | 优化 wheel |
| ansible | Python | 2 | 2 | 1.0 / 1.0 | 优化 wheel |
| NodeBB | JS | 1 | 1 | 1.0 | 另 1 次基础设施错误（29s，不计） |
| element-web | TypeScript | 1 | 1 | 1.0 | 早期 wheel |
| qutebrowser | Python | 3 | 2 | 1.0 / 0.0 / 1.0 | 失败为「删旧接口+改测试」型 |
| vuls | Go | 2 | 1 | 1.0 / 0.0 | 单次最长 2,636s |
| teleport | Go | 0 | 0 | — | **musl 镜像，SAYACODE 装不上 sqlite-vec** |

**有效运行合计 12 次，通过 10 次（83%）**；另有 NodeBB 基础设施错误 1 次、teleport 安装失败 2 次。

7/8 题至少通过一次；teleport 因平台依赖问题完全无法安装被测产品。

## teleport 阻塞的真实原因（B 阶段问题在线复现）

任务镜像为 musllinux 基础，容器内安装 SAYACODE 时报：

```
× No solution found when resolving dependencies:
  Because sqlite-vec==0.1.9 has no wheels with a matching platform tag
  (e.g., `musllinux_1_2_x86_64`) and you require sqlite-vec==0.1.9 ...
```

这正是 `sayacode-bench` 46 镜像安装预检中第 8/17/27 项失败的同款原因。此前已在本机验证过修复方案（从官方源码重编 sqlite-vec 的 musl 派生 wheel，产出 `passed=true` 的 provenance），但要真正让 teleport/vuls 类 musl 镜像可跑，需要实现 C 阶段的 runtime-support 接线：把派生 musl wheel 与派生 requirements 注入安装流程（见 `C_RUNTIME_SUPPORT_WIRING_REVIEW.md`）。这是较大的独立工作，未在本轮实施。

## 方差警示（重要）

同一任务、同一模型、同一 wheel 的多次运行差异极大：

| 任务 | 输入 token 范围 | 用时范围 |
|---|---|---|
| openlibrary | 203k – 6,350k（约 30 倍） | 103s – 944s |
| qutebrowser | 1,300k – 16,008k | 390s – 2,944s |
| vuls | 696k – 1,150k | 2,281s – 2,636s |

成功率也非确定性：同一 wheel 下 qutebrowser 一次通过一次失败、vuls 一次通过一次失败。

**因此当前 n=1~3 的结果不能用于宣称稳定收益；需要每格 ≥5 次重复并按任务块配对，才能给出可信区间。**

## 本轮已落地的产品改动

1. `src/sayacode/prompts.py`：新增「新增接口须与旧接口并存、不得重命名/删除现有公开 API」与「不得改写既有测试或重命名测试函数」两条规则。
2. `src/sayacode/audit.py` / `diagnostics.py`：记录并汇总 prompt 缓存命中量。
3. `evals/worker.py` / `report.py`：评测报告展示缓存命中率。
4. `tests/agent/test_prompts.py`：新增回归护栏，断言上述两条规则存在（4 个角色各断言一次）。
5. `tests/evals/test_execution.py`：缓存用量聚合用例。

## 精简提示词实验结论

单独的 30% 提示词压缩使全部三项目标变差（成功率 75% vs 86%，总输入 +86%，总时长 +55%），已回退，保留完整版提示。详见 `2026-10-28-ab-condensed.md`。

## 尚未完成 / 建议下一步

1. **C 阶段 runtime-support 接线**：让 musl 镜像能用上派生的 sqlite-vec wheel，解锁 teleport 等任务。
2. **提高重复次数**（每格 ≥5）以给出可信的成功率与 token 区间；本轮已证明单次结果不可靠。
3. **降低 token 的真正杠杆是上下文管理**，不是系统提示长度：系统提示仅约占每次调用 500 tokens，而单次任务输入达 70 万～1,600 万 tokens，差距来自会话增长。建议在固定成功率前提下实验 `summary_trigger` / context-editing 策略。
4. 完整 48 次预注册开发研究仍应走 `sayacode-bench` 的 `freeze` 门禁流程，本轮为探索性运行。
