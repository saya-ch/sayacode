# 全量实验最终报告（2026-10-29）

本轮完成：token 调参、机制保护配对、TLS 补齐、NodeBB 重试。以下全部为官方 Harbor 验证器实测。

## 一、最终 holdout 基线（发布版 3.2.4）

| 类别 | 数量 |
|---|---|
| holdout 总题数 | 24 |
| 不可评测（musl 镜像，sqlite-vec 无 wheel） | 4（teleport×2、protonmail×2）|
| 可评测 | **20** |
| 通过 | **14（70.0%）** |
| 失败 | **6** |

补齐过程：TLS 重试把 qutebrowser-5270、tutao-de49 从 N/A 变为 **PASS**（941s / 848s）；NodeBB-4327 重试后可评测但 FAIL（648s）。

失败的 6 题：element-web×2、navidrome-0130、qutebrowser-c09e、tutao-518、NodeBB-4327。

## 二、三层优化的量化结果

### 1) 提示词兼容规则（针对失败题重跑）
5 个失败实例中 **3 个翻盘**（element×2、navidrome-0130）→ 12/17 → 15/17。

### 2) Token 控制（release wheel + ClearToolUsesEdit + 早摘要）

| trigger | 6/4 题成功率 | 总 token |
|---|---|---|
| 无控制（基线） | 4/6 | 17.18M |
| **40k（推荐）** | **4/6（持平）** | **3.11M（-82%）** |
| 60k | 3/4（更差） | — |

**40k 胜出**：navidrome-0130 在 40k 下 PASS（1.06M），在 60k 下 FAIL（169k，疑似提前放弃）；flipt-756 在 40k/60k 都 FAIL。结论：**保持 40k，不建议提高 trigger；若要改进应调 keep 而非 trigger**。

### 3) 机制层测试保护
实现 `restore_existing_test_files`（补丁采集前恢复被改/删的既有测试，保留新增测试），单测通过、bench 全套 1362 passed。

配对实测（qutebrowser/tutao 各 3 次）：

| 运行 | 结果 | 保护是否触发 |
|---|---|---|
| qutebrowser r1 | PASS | 否（未改测试） |
| qutebrowser r2 | N/A（Docker API 500） | — |
| qutebrowser r3 | FAIL | 否（新建 test_guiprocess_cleanup.py 绕过，非改既有测试） |
| tutao r1 | FAIL | **是**（恢复 DesktopDownloadManagerTest.ts） |
| tutao r2 | FAIL | 否（patch=0 空转） |
| tutao r3 | FAIL | 否 |

**结论**：机制保护**真实触发 1 次并正确恢复了既有测试**，但该题恢复后仍 FAIL——说明 tutao 的根因是**源码修复本身不对**，不是测试被改。机制保护能兜住"改既有测试"这一类，无法解决"源码没修对"和"新建测试绕过"。

## 三、失败分类学（本轮最重要的发现）

| 失败类型 | 证据 | 有效对策 |
|---|---|---|
| 改既有测试 | element×2、navidrome-0130、qutebrowser（发布版）、tutao | **提示词规则 + 机制保护**（已验证有效） |
| 源码修复错误 | tutao（保护恢复测试后仍 FAIL） | 需要更强模型/更多轮探索，非提示词能解决 |
| 新建测试绕过 | qutebrowser r3 新建 test_guiprocess_cleanup.py | 验证器按 fixtures 名匹配，机制无法覆盖 |
| 空转放弃 | tutao r2（patch=0）、navidrome 60k（169k） | token 控制过激会加剧 |

## 四、基础设施改进（本轮沉淀）

1. ghcr 拉取 40KB/s → `ghcr.nju.edu.cn` 镜像 8MB/s；容器安装 PyPI → 清华源
2. 安装阶段 astral.sh TLS 间歇失败 → adapter 整链重试 3 次
3. musl 镜像预检（5 秒判定跳过，省每题 10 分钟无效安装）
4. job 名撞名 bug（同仓库多实例）→ 唯一化
5. Harbor job 级结果缺 `finished_at` 被误判 infra → trial 级结果对账
6. Clash mihomo 核心偶发不启动 → 网络恢复后 astral 才可达（诊断出根因）

## 五、下一步（按收益排序）

1. **token 控制 keep 调参**（keep=3→8，保持 trigger=40k），目标救回 flipt-756 同时保住 -82%；
2. **统计功效**：每格 ≥5 次重复，现有多数为 1–3 次，方差仍大；
3. 机制保护扩展：覆盖"新建测试文件"的规避行为（需与验证器 fixtures 匹配逻辑配合）；
4. C 阶段 musl 接线（4/24 题，1–2 天工程）。

## 原始数据

- `C:\sb\tmp\hard\token-ab\summary.json`（40k 完整 6 题）
- `C:\sb\tmp\hard\token-t60\summary.json`（60k）
- `C:\sb\tmp\hard\mech-pair\summary.json` + 各 job 的 `restored-test-files.json`
- `C:\sb\tmp\hard\tls-retry\summary.json`、`nodebb-retry2\summary.json`
- `C:\sb\tmp\hard\holdout-baseline.json`
