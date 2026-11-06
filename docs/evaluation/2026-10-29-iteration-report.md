# 优化迭代报告：失败归因、三层优化与 token 控制权衡（2026-10-29）

基线：发布版 3.2.4，holdout 17 题可评测，12/17 通过（70.6%）。
本日完成：失败归因 → 提示词优化复验 → 机制层保护 → token 控制权衡曲线。

## 1. 失败归因（5 个 FAIL 全部分类）

| 实例 | 验证器证据 | 归因 |
|---|---|---|
| element-9bf | 51 项必需测试缺 41 | **灾难性改测试** |
| element-d405 | 缺 1（ExportE2eKeysDialog renders） | 改测试 |
| qutebrowser-c09e（发布版） | 缺 1（test_guiprocess::test_cleanup） | 改测试 |
| navidrome-0130 | TestScanner 0/1 | 改测试（walk_dir_tree_test.go/refresher_test.go 被 patch） |
| tutao-518 | 缺 4（DesktopDownloadManagerTest） | 改测试 |

**5 个失败全部与"Agent 修改既有测试"相关**——验证器在干净沙箱重放补丁并重置测试面，Agent 对测试的修改只会让必需测试消失。

## 2. 提示词层（兼容规则）复验：3/5 翻盘

用优化 wheel（含"不改既有测试/不改公开接口"规则）重跑 5 个失败实例：

| 实例 | 发布版 | 优化 wheel |
|---|---|---|
| element-9bf | FAIL（缺 41） | **PASS**（394k token，比发布版少 43%） |
| element-d405 | FAIL（缺 1） | **PASS** |
| navidrome-0130 | FAIL | **PASS** |
| qutebrowser-c09e | FAIL（缺 1） | FAIL（缺 96——这次改了 2 个测试文件，更糟） |
| tutao-518 | FAIL（缺 4） | FAIL（缺 4） |

结论：**规则降低了改测试概率但无法消除**（qutebrowser 那次照样改了）。成功率 12/17 → 15/17（88.2%）。

## 3. 机制层测试保护（新）：实现完成，效果待配对验证

在 `sayacode-bench/worker.py` 的补丁采集阶段加入 `restore_existing_test_files`：
- Agent 修改/删除的**既有**测试文件自动恢复为 base 版本（新增测试保留）
- 判定规则精确匹配测试路径（目录段 tests/test/spec + 常见命名），避免误伤源码
- 恢复清单写入 `restored-test-files.json` 供审计

验证状态：
- 单元测试通过；bench 全套 **1362 passed** 无回归
- 实战：qutebrowser 机制版 PASS（但该次 Agent 恰好没改测试，保护未触发，**不能归功于保护**）；tutao 机制版空转（patch=0）
- **待办**：对 qutebrowser/tutao 做 ≥3 次配对重复，统计"改测试"发生率与保护触发率

## 4. Token 爆炸真相（数据修正认知）

| 试验 | 轮数 | 输入总量 | 工具输出总量 |
|---|---:|---:|---:|
| navidrome-0130 FAIL | 100 | 3.08M | 0.33MB |
| tutao-518 FAIL | 98 | 3.05M | 0.24MB |
| flipt-756 PASS | 115 | 3.71M | 0.52MB |

**工具输出只占输入的 ~5%；95% 是每轮重复携带的历史上下文**。压缩提示词（已证伪）和限制单次输出都不是杠杆；**杠杆是上下文编辑与更早摘要**。

## 5. Token 控制 A/B：-82% token，成功率持平，但参数过激

配置：`context_edit_trigger=40000`（ClearToolUsesEdit，keep=3）+ `summary_trigger_ratio=0.6`，release wheel，6 题：

| 题 | 基线 | token 控制 | 结果 |
|---|---|---|---|
| navidrome-0130 | FAIL 3.08M | **PASS 1.06M** | ✓ 翻盘 |
| tutao-518 | FAIL 3.05M | FAIL 391k | token -87% |
| flipt-756 | PASS 3.71M | **FAIL 813k** | ✗ **反向** |
| flipt-8bd | PASS 1.67M | PASS 503k | ✓ -70% |
| navidrome-812 | PASS 563k | PASS 326k | ✓ -42% |
| openlibrary-3f7d | PASS 110k | PASS 26k | ✓ -77% |
| **合计** | **17.18M** | **3.11M（-82%）** | 成功 4/6 → 4/6 |

- 6 题 token **省 82%**；4 个 PASS 题保持通过且全部省 42–77%
- **flipt-756 反向**：keep=3 保留的近期工具结果太少，长任务丢关键上下文
- **下一步调参**：`keep=3→8` 或 `context_edit_trigger=40000→60000`，先复验 flipt-756 是否恢复 PASS

## 6. C 阶段 musl 接线评估（列为后续工程）

- 影响：protonmail×2、teleport×2（4/24 题）完全无法评测
- 工程量：1–2 天——sqlite-vec 0.1.9 无 musllinux wheel，需 alpine 编译链产出 wheel + 绕过/扩展 hash 锁定 + adapter 条件化安装
- 优先级低于 token 控制（后者影响全部题）与机制保护（影响失败题）

## 原始数据

- 机制重试：`C:\sb\tmp\hard\mech-retry\summary.json`
- token A/B：`C:\sb\tmp\hard\token-ab\summary.json`
- 优化重试：`C:\sb\tmp\hard\opt-retry-failed\summary.json`
- holdout 基线：`C:\sb\tmp\hard\holdout-baseline.json`
