# 权威基准评测迭代 01（2026-10-28）

## 结论摘要

用**官方 SWE-bench Pro V2**（不是自造任务）在本机跑通了真实评测，并完成一次有依据的优化迭代：

- 基线 6 题中 **5 通过 / 1 失败**（qutebrowser 失败）。
- 失败根因是产品行为缺陷：**删除了既有公开接口并改写既有测试**，而官方验证器会重置测试面，导致 `test_key_info_to_int` 缺失。
- 优化后同一题 **1869/1869 官方必需测试全部通过，reward 1.0**，输入 token 从 3,330,622 降到 1,296,981（−61%），用时从 761s 降到 390s。
- 两个原本通过的任务（openlibrary、ansible）优化后仍通过，无正确性回退。

## 为什么换用权威数据集

自构造 32 题集已被 DeepSeek-V4.1-Flash 完全饱和（dev 8/8、holdout 20/20），无法衡量改进。本轮改用仓库既有 `sayacode-bench` 预注册的官方基准，任务、镜像与验证器全部来自上游。

## 评测基础设施（本机，已实测）

| 项目 | 结果 |
|---|---|
| 官方源码 | `SWE-bench Pro V2` 固定提交 `66f92766…`，重新 `git archive` 到本机，**6436 项官方 SHA256 全部通过**，642 个任务目录 |
| 官方门禁 | 单题 `sanity`：参考解 **reward=1**，空解 **reward=0**，`passed=true` |
| 运行环境 | Harbor 0.24.0 + Docker 29.7.2；`IsolatedDockerEnvironment` 隔离桥 + 固定 nginx 网关 |
| 模型接入 | 本机 Magpie 网关（`http://host.docker.internal:3425/v1`），Anthropic Messages 协议，`auto/deepseek-v4.1-flash` |
| 产品安装 | 官方 wheel + `product-requirements.txt`（带 hash）注入任务容器 |
| 镜像 | 6/8 个 dev 任务镜像已在本地（qutebrowser、openlibrary、ansible、NodeBB、element-web、navidrome） |

## 权威基线（dev 子集，官方验证器判分）

| 任务 | 语言 | reward | 用时 | 输入 token |
|---|---|---:|---:|---:|
| navidrome | Go | **1.0** | 232s | 198,591 |
| openlibrary | Python | **1.0** | 103s | 202,727 |
| ansible | Python | **1.0** | 137s | 232,070 |
| element-web | TypeScript | **1.0** | 433s | 784,677 |
| NodeBB | JavaScript | **1.0** | 531s | 3,958,100 |
| qutebrowser | Python | **0.0** | 761s | 3,330,622 |

通过 **5/6**。失败与成功都来自官方验证器，不是自评。

## 失败根因

qutebrowser 的官方必需测试共 1869 项，通过 1868 项，缺失：

```
tests/unit/keyinput/test_keyutils.py::test_key_info_to_int
```

任务要求**新增** `KeyInfo.to_qt` 接口，但没有要求删除旧接口。Agent 的实际做法是：

1. 把 `KeyInfo.to_int()` **改名**为 `to_qt()`，删除了旧入口；
2. 同时修改 `tests/unit/keyinput/test_keyutils.py`，把 `test_key_info_to_int` 改名为 `test_key_info_to_qt`。

官方验证器会「把测试面重置为基线并应用官方测试补丁」，因此 agent 对测试文件的修改被丢弃，而基线测试仍调用 `to_int` → 该测试无法通过。

对照实验：仅给 `focused` 变体的指令提示（强调最小补丁）**仍然失败**，输入 token 反而升到 4,407,810。说明一般性提示不足以纠正该行为。

## 产品优化

在 `src/sayacode/prompts.py` 的 `_WORK_RULES` 增加两条明确规则：

1. 请求指定新接口时，**与旧接口并存**；除非明确要求删除，不得重命名或删除现有公开函数/方法/类/属性，必须保持旧入口可用。
2. **不得修改既有测试来让改动通过，也不得重命名测试函数**；项目原有测试仍须通过。

从当前源码构建 wheel（`uv build --wheel`，含上述规则与审计缓存字段），用同一模型、同一任务、同一验证器重跑。

## 优化结果

| 指标 | 优化前 | 优化后 |
|---|---:|---:|
| reward | 0.0 | **1.0** |
| 官方必需测试 | 1868 / 1869 | **1869 / 1869** |
| 用时 | 761s | 390s |
| 输入 token | 3,330,622 | 1,296,981（−61%） |
| 输出 token | 43,629 | 28,801（−34%） |

回归检查（优化后 wheel）：

| 任务 | 结果 |
|---|---|
| openlibrary | reward 1.0（仍通过） |
| ansible | reward 1.0（仍通过） |

## 口径与限制

- **单次运行方差大**：openlibrary 基线 103s/203k，优化后 319s/2.12M；ansible 137s/232k → 186s/517k。正确性未回退，但 token 与耗时波动显著，不能把单次差异当作稳定收益。
- 本机 Magpie 网关返回的模型标识为 `auto/deepseek-v4.1-flash`，未独立验证上游权重版本。
- 只跑了 6 个 dev 任务中的 6 个可运行项（vuls、teleport 镜像未缓存）；不是完整 48 次开发研究，也不是官方榜单成绩。
- 该轮为探索性迭代，未走 `freeze` 预注册冻结流程；不替代 `sayacode-bench` 的正式研究结论。
- 未修改任何官方任务、镜像、验证器或评分规则。

## 下一步

1. 用优化后 wheel 重跑全部 6 个可用 dev 任务（含 2 次重复）确认收益稳定。
2. 补齐 vuls / teleport 镜像，覆盖完整 8 题 dev 选择。
3. 把「不得破坏既有公开接口 / 不得改测试」纳入回归测试，防止再次退化。
4. 收益确认后再考虑进入主最终与 Terminal 基准。
