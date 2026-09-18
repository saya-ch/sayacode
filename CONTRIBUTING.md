# 贡献指南

本文件说明 SAYACODE 的分支策略、质量门禁与提交流程。目标只有一条：
**让「本地全绿」和「CI 全绿」是同一件事。**

## 一、分支策略

```
main                     ← 永远可发布；只能通过 PR 合入
 ├─ feat/<主题>          ← 新功能
 ├─ fix/<主题>           ← 缺陷修复
 └─ refactor/<主题>      ← 重构（行为不变）
```

- **不直接推 `main`。** 所有改动走 PR，CI 全绿才合。
- 一个分支一件事。同时改三个模块的大重构，请拆成三个 PR —— 与本仓库的
  「注释去历史化」「模块拆分」同类工作尤其如此。
- **同一天不要两个人同时大重构同一批文件。** 本仓库历史上出现过两个人
  并发重构、全量测试互相踩的现象：一方删了方法，另一方还在调它。
  PR 粒度小、合入快，是唯一可靠的解法。

## 二、本地环境必须与 CI 对齐

CI 装的是 `pip install -e ".[dev]"`（含全部 provider 集成）。**本地少装可选
依赖会让「本地红、CI 绿」——本仓库已因此吃过亏**（`1.4.0` 发布时记录过
「本地全绿 ≠ CI 全绿」，后来反过来又出现「本地红、CI 绿」）。

```bash
pip install -e ".[dev]"          # 必装
pip install langchain-anthropic langchain-google-genai   # 可选 provider，装了才能全绿
```

判据很简单：`python -m pytest -q` 应当 **0 failed**。若还有 5 个
`test_model_contract` / `test_model_providers` 的失败，就是上面两个包没装。

## 三、质量门禁（与 CI 完全一致）

```bash
python -m ruff check .                    # 静态检查
python -m mypy                            # 类型检查（机器契约模块）
python scripts/check_release.py           # 发布检查：密钥扫描 / 构建产物
python scripts/check_coverage.py          # 按包覆盖率门槛
python -m pytest -q                       # 全量测试
```

另有按需运行的**不稳定用例检测**（不进常规 CI，避免三倍 CI 时间）：

```bash
python scripts/check_flaky.py                     # 全量连跑 3 轮
python scripts/check_flaky.py --runs 5            # 加大轮数
python scripts/check_flaky.py --pattern tests/test_delegate_cancel.py
```

它连跑多轮并把结果分成三类：**每轮都失败**（真实缺陷）、**部分轮失败**（flaky，
非零退出）、从未失败。CI 里对应一个手动触发的 `flaky-check` 作业。
**不要用「加重跑」来掩盖抖动** —— 抖动的测试消耗所有人的信任，应当被暴露和修掉。

覆盖率门槛在 `scripts/check_coverage.py` 的 `COVERAGE_FLOORS`。**门槛只能上调，
不能为了过 CI 而下调**；确需下调必须在 PR 描述里写明原因与补偿计划。

## 四、提交信息

沿用现有的 Conventional Commits 风格，描述用中文：

```
feat(plan): 自主计划执行改用 StateGraph 编排
fix(tools): 并行批处理透传 contextvars，避免 trace 断裂
refactor(agent): 拆出流事件抽取与用量统计
```

常用类型：`feat` / `fix` / `refactor` / `docs` / `test` / `chore` / `ci` / `release`。

提交信息写**为什么**，不写「修改了某文件」。同一件工作的多个步骤可以
分多次提交，但每次提交都应能独立通过门禁。

## 五、测试约定

- 新功能必须带测试；修 bug 优先补一条**能复现该 bug** 的回归测试。
- 架构红线写在 `tests/test_architecture_boundaries.py`：禁止旧实现回潮、
  禁止 `globals()` 魔法、禁止在运行时层重声明 provider 默认值。
- **并发测试只允许一边看表。** 工作线程与主线程用同一个超时会互相抢先，
  在高负载下必然抖动（本仓库踩过：同一用例连跑三遍才稳定）。工作线程用
  无限等待，裁判权只留在主线程侧。
- 涉及时序的断言，避免依赖真实时钟；用可注入的时钟或事件同步。

## 六、全量测试红了怎么办

先按这个顺序排查，别急着改代码：

1. **是不是方法搬家了？** 失败信息形如 `AttributeError: ... has no attribute _x`，
   通常是重构把方法挪到了新模块，而调用方还没跟上。
2. **这个测试文件最近 5 分钟被谁动过？** 并发编辑期间，已加载的旧模块与
   刚落盘的新测试会互相矛盾。
3. **单独重跑这个文件过不过？** 过 → 大概率是顺序污染或并发编辑；
   不过 → 才是真缺陷。

三条全中基本可判定为编辑竞态，重跑即可，不必改代码。

## 七、PR 检查清单

- [ ] 分支基于最新 `main`，改动只围绕一个主题
- [ ] `python -m pytest -q` 本地 0 failed
- [ ] `ruff` 与 `mypy` 通过
- [ ] `python scripts/check_coverage.py` 通过（动了门槛则在描述里说明）
- [ ] 行为变化已写入 `CHANGELOG.md` 的「未发布」段
- [ ] 新增公开符号带中文 docstring
- [ ] 注释陈述**现状与机理**，不写「之前/曾经/实测」式历史叙述

## 八、发布流程

1. 汇总 `CHANGELOG.md` 的「未发布」段，定版本号并新建 `## [x.y.z]` 小节。
2. 更新 `lib/_version.py`（版本号唯一事实来源）。
3. 提交 `release: x.y.z`，打 tag `vx.y.z` 并推送。
4. tag 会触发 CI 的 `publish` job 构建并发布到 PyPI。
5. 确认 CI 三平台（Ubuntu / Windows × Python 3.11–3.13）全绿后再宣告完成。
