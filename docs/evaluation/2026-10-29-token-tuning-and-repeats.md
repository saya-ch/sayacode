# token 调参与重复实验小结（2026-10-29 下午）

## 1. keep 参数调参：目标未达成，但揭示了重要边界

目标：keep=3→8 救回 flipt-756 同时保住 ~-82% token。**未达成**——没有一组参数能同时保住三道关键题。

### 完整参数矩阵（官方验证器）

| 题 | 基线（无控制） | keep3 / trigger40k | keep3 / trigger60k | keep8 / trigger40k |
|---|---|---|---|---|
| flipt-756 | **PASS 3.71M** | FAIL 813k | FAIL 733k | FAIL 1.86M |
| navidrome-0130 | FAIL 3.08M | **PASS 1.06M** | FAIL 169k | FAIL 781k |
| qutebrowser-c09e | FAIL 1.38M | 未测 | 未测 | **PASS 659k** |

### 结论
1. **token 控制无普适最优参数**，效果高度任务依赖。
2. **flipt-756 属"长上下文依赖型"**：任何清理都伤它（三种配置全 FAIL），基线 3.71M 反而 PASS。
3. **navidrome 只认 keep3/trigger40k**（keep=8 与 trigger=60k 都 FAIL）。
4. **qutebrowser 在 keep8 下 PASS**（659k，比基线 -52%），但该题失败以"改测试"为主因，与 token 控制关系弱。
5. 此前"40k 推荐"需修正为：**40k/keep3 在 6 题样本上 -82% token、成功率持平**（+navidrome / -flipt），但这是样本内平衡，非普适最优。

## 2. 机制扩展：剔除新增测试文件（已实现）

针对 qutebrowser r3 的"新建 test_guiprocess_cleanup.py 绕过"行为，在 `worker.py` 增加 `strip_new_test_files`：
- 补丁中**新增**的测试文件从临时索引移除（工作区保留），使最终 patch 不含它们；
- 官方验证器按 fixtures 判分，新增测试对评分无益，剔除可防止掩盖缺陷。
- 单测 2 项通过；bench 全套 **1363 passed**（无回归）。
- 待真实触发验证（需 agent 再次新建测试文件）。

## 3. C 阶段 musl 接线评估：不实施

实测关键：**sqlite-vec 0.1.9 无 sdist**，只有 5 个平台预编译 wheel（无 musllinux）。alpine 编译链可装，但 pip 无从源码构建，需自行编译 `vec0.so` 并组装 wheel，预计 1.5–2.5 天且有不确定性。影响 4/24 题。详见 `2026-10-29-c-stage-musl-assessment.md`。

## 4. 产品改动（含无回归证据）

| 文件 | 改动 | 验证 |
|---|---|---|
| `src/sayacode/config.py` | 新增 `context_edit_keep: int = 3` + 校验 | 30 项 config 测试通过 |
| `src/sayacode/agent/graph.py` | `ClearToolUsesEdit(trigger=..., keep=profile.context_edit_keep)` | 编译+测试 |
| 产品全套 | — | 481 passed + **4 pre-existing 失败**（git stash 证明与本次改动无关）|
| `sayacode-bench/worker.py` | `strip_new_test_files` | bench 1363 passed |
| `sayacode-bench/adapter.py` | 安装整链重试 3 次 | bench 全套通过 |

**4 个 pre-existing 失败**：`test_execution::test_driver_saves_abort...`、`test_repository::test_same_scope...`、`test_task_safety::test_non_git_builder...`、`test_security_boundaries::test_official_search...`。均为环境相关，与本次改动无关。

## 5. ≥5 重复实验（进行中）

为验证核心结论的稳定性，正在跑基线（release，无控制）在 **flipt-756、navidrome-0130** 上各 3 次（+已有 1 次 = 4 次/题）。目的：
- 确认 flipt-756 基线 PASS 是否稳定（决定"token 控制伤害"结论）；
- 确认 navidrome-0130 基线 FAIL 是否稳定（决定"翻盘"结论）。

数据：`C:\sb\tmp\hard\baseline-reps\summary.json`。
