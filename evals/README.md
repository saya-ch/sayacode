# SAYACODE Agent 效果评估

> 🛑 **状态（2026-11-07 标注）：本目录的 32 题任务集已退役，不再用于正式成绩。**
>
> - **正确率维度已饱和**：dev 8/8 = 100%、holdout 20/20 = 100%（`../docs/evaluation/2026-10-28-iteration-01.md:23-24`），无法再衡量改进。
> - **机制收益（team vs single）无法判断**：首轮 60 trial 里有 43 次连接类失败，样本与服务波动不足以支持结论（见 §下文与 `../docs/evaluation/2026-10-21.md`）——这两件事**不矛盾**：饱和说的是"做对的比例到顶了"，无法判断说的是"两组谁更好测不出来"。
> - **当前正式成绩在 `C:\develop\sayacode-bench\docs\RESULTS-LEDGER.md`**（hy3 + SWE-bench Pro V2 40 题 + planned/observed/partial 三口径）。本目录的 70.0%/65.0% 与它**模型、题集、分母、制品全都不同，不可比较**。
> - **仍然有价值、且 bench 没有的资产**：`scenarios/collaboration*.jsonl`（唯一能触发 `task_count=2` 的场景）与 `scenarios/context.jsonl`（**唯一能压到摘要触发线的场景**，bench 现有题的输入峰值 102k 远低于 150.4k 触发线）。**这两个不要删。**
>
> **术语提醒**：「holdout」在本仓库有三重含义，见 `../docs/evaluation/README.md` §1.1 的术语表，引用时必须写明是哪一种。

本目录在冻结任务和独立环境中调用现有 SAYACODE 应用，比较单 Agent 与允许异步子 Agent 协作的配置。运行器负责准备输入、容器生命周期、判分和汇总，Agent 循环仍由产品中的 LangChain / LangGraph 执行。

这里提供评估工具与初始任务集。已经完成的真实运行、无效批次和未完成项见 [首轮评估记录](../docs/evaluation/2026-10-21.md)。指标只来自对应结果目录，不填写估计值、示例成功率或榜单排名；当前样本与服务波动不足以支持机制收益结论。

## 当前研究问题

- 相同模型、任务、工具和上下文策略下，开放团队工具是否改变任务质量、耗时和总用量？
- 哪些任务实际派发子 Agent？派发是否带来重复调查、协调负担或交付失败？
- 失败发生在模型接入、执行、产物验收还是人工审查？

`single` 不注册团队工具；`team` 注册团队工具，由模型自主决定是否派发。两组均保留工具式规划，不能将对比结论写成“规划机制的收益”。协作组未派发子任务也是结果，应查看实际子任务数。

## 任务与证据边界

任务集当前为 **v0.2，共 32 题：8 道 dev、24 道 holdout**。holdout 包含 20 道自动验收编程题与 4 道必须人工复核的理解／审查题。v0.2 澄清了初版未公开的文件保护条件；这些任务已经用于开发排错，不能再称全新未见的盲测集。

- 全部是人工构造的 Python 标准库小仓库，不是企业真实任务或生产用户日志。
- dev 与 holdout 按 fixture 家族拆分；同一家族内仍有相关性，不能把所有题当成独立项目。
- 编程题使用外部行为检查、公开回归和受保护文件检查；不要求固定工具顺序。
- 审查题的关键词检查仅作筛查，不能替代人工依据 `human_rubric` 判分。
- 多轮题在前一轮和任务族结束后发送固定 followup，不模拟运行中插话。
- 当前短多轮题不保证实际触发摘要，**不能据此声称验证了长上下文压缩或压缩保真**。

逐题定义、来源、规则与冻结信息见 [任务目录](cases/catalog.md)、[开发集](cases/dev.jsonl)、[验收集](cases/holdout.jsonl) 和 [冻结清单](cases/freeze.json)。发现规则错误应更新数据集身份、记录更正原因并重新实验，不改分来美化既有成绩。

## 目录职责

```text
evals/
  cases/                   冻结题面、检查规则与来源
  fixtures/                每题复制使用的初始仓库
  profiles/commandcode.json 声明的实验模型参数，不含 API 密钥
  catalog.py               冻结校验、任务读取与源文件摘要
  run.py                   宿主调度、容器、元数据与判分入口
  worker.py                容器中调用现有产品应用，收集状态与用量
  graders.py               外部验收和文件状态检查
  report.py                原始结果汇总与逐题配对
  diagnose.py              只读分析原生审计，区分终态故障与恢复
  Dockerfile               专用实验镜像
```

## 运行前准备

从仓库根目录执行，使用项目 Python 开发环境，准备可运行 Linux 容器的 Docker。Windows 可使用 Docker Desktop 的 Linux 容器模式。运行器还会调用宿主 Git。

默认配置 [commandcode.json](profiles/commandcode.json) 连接本机 Command Code Go 反代：

| 参数 | 本轮声明 |
|---|---|
| 协议 | `openai_chat_completions` |
| 容器内地址 | `http://host.docker.internal:8787/v1` |
| 模型 ID | `deepseek/deepseek-v4.1-flash` |
| API Key | `proxy-managed`，反代支持的非秘密标记；真实凭据留在反代进程 |
| 实验上下文窗口 | 65,536 tokens |
| 最大输出 | 8,192 tokens |
| 自动摘要阈值 | 声明窗口的 80% |
| 上下文编辑 | 关闭 |
| 跨会话记忆 | 默认关闭，使用全新独立状态 |

65,536 是统一实验预算，**不是测得的模型最大窗口**。模型名称来自反代配置与响应，未独立验证上游权重身份。默认地址必须能从容器访问；仅在宿主能请求成功不足以证明容器链路可用。其他 Docker 环境可能需要自行提供相应宿主名称解析。

不读取用户的 `.sayacode`。每次 trial 都建立新的工作区、配置、会话和状态目录。需要认证的其他代理应使用仓库外配置，通过 `--profile` 指定；密钥不要写入提交、公开报告或共享日志。

## 先校验，再小规模运行

以下为 PowerShell 示例；所有结果目录使用仓库外的新绝对路径，不与其他实验共用。目录已存在时运行器拒绝覆盖。

```powershell
# 只校验冻结任务，不调用模型，也不需要启动实验容器。
python -m evals.run validate

# 构建专用镜像；真实运行也会按源文件身份构建镜像。
python -m evals.run build

# 无模型的初始 fixture 判分自检。
python -m evals.run check-fixtures --split dev --repeats 1 --concurrency 2 --variants single --output C:\develop\sayacode-eval-results\fixture-check-01

# 先选择开发题验证模型接入、判分与预算。
python -m evals.run run --split dev --cases dev-inventory-isolation dev-notes-parent --repeats 1 --concurrency 2 --variants single team --profile evals/profiles/commandcode.json --output C:\develop\sayacode-eval-results\dev-smoke-01

# 开发链路稳定后运行完整开发集。
python -m evals.run run --split dev --repeats 3 --concurrency 2 --variants single team --profile evals/profiles/commandcode.json --output C:\develop\sayacode-eval-results\dev-comparison-01

# 冻结配置后运行验收集；不要看完成绩再针对本批题调参。
python -m evals.run run --split holdout --repeats 3 --concurrency 2 --variants single team --profile evals/profiles/commandcode.json --output C:\develop\sayacode-eval-results\holdout-comparison-01
```

`--cases` 只能选择当前 `--split` 中的 ID。默认重复 3 次、并发 2、变体为 `single team`。`--seed` 固定试验顺序的打乱方式，不保证远端模型采样可重复。24 × 2 × 3 表示 144 次完整任务试验，并非 144 次模型请求；一次任务可能有多轮、摘要和子 Agent 调用。

支持用 `--catalog` 选择独立专题、`--variant-profile 组名=路径` 指定不同组的模型参数、`--team-variants` 指定开放团队工具的组。默认只有 `team` 组开放团队工具。摘要专题两组都使用单 Agent：

```powershell
python -m evals.run run --catalog evals/scenarios/context.jsonl --split dev --variants summary_off summary_on --variant-profile summary_off=evals/profiles/summary-off.json --variant-profile summary_on=evals/profiles/summary-on.json --repeats 3 --concurrency 2 --output C:\develop\sayacode-eval-results\summary-01
```

这些命令会真实消耗所配置接口的额度，先跑少量开发任务并检查服务限制。任务族任一线程最后一次模型终态明确返回 401、403 或 429 时，运行器停止本实验后续派发并记录中止原因；已经由后续成功调用恢复的旧错误不会触发这一终态判定。停止控制不依赖代码验收是否通过，不自动换模型、账号或购买额度。Ctrl+C 仅清理本实验拥有的容器，清理失败仍保存中止原因。该控制已通过本地模拟 429 的真实图链路故障注入，并以离线测试覆盖子线程故障；不把模拟接口当真实模型效果。

`check-fixtures` 不调用模型，初始缺陷被隐藏检查检出是预期现象；它用于确认规则有区分能力，**不可当成模型失败率、基线成绩或 Agent 效力报告**。运行失败、超时和基础设施错误应保留，不能只挑成功目录重新汇总。

## 实验容器的能力与限制

- rootfs 只读，使用 UID 1000，移除额外 capabilities，启用 `no-new-privileges`。
- 仅绑定本次 trial 的 `/workspace` 与 `/state`，临时目录使用独立 tmpfs。
- 题目完整定义、隐藏检查和其他试验目录不挂入 Agent 容器；仅发送用户任务和运行配置。
- 运行预算由任务定义控制；超时会停止容器，避免仍在写入时判分。
- **网络未关闭**，需要访问宿主反代；不能声称网络隔离或完全没有外部信息渠道。
- 这是评估环境的隔离措施，不代表 SAYACODE 产品新增了用户工具沙箱。

判分在任务族收尾后进行，检查规则由宿主传入容器执行。它减少 Agent 直接读取完整评分合同的机会，仍不能保证彻底防止恶意代码针对评分环境作弊；当前用途是受控工程实验。

## 结果与复现身份

```text
<output>/
  manifest.json            源文件、Git、数据集、镜像和公开模型配置身份
  source.zip               本批运行代码与固定输入，不包含模型凭据
  trials/<case>--<variant>--<repeat>/
    case.json              本次题面与评分规则，仅保留在宿主
    workspace/             初始仓库与最终修改
    state/                 独立状态、事件、审计、快照和 worker 结果
    worker.log             运行输出（正常启动 worker 时）
    result.json            运行状态、验收、用量和检查详情
  summary.json             机器可读汇总
  report.md                中文报告
```

`manifest.json` 记录 Git SHA、脏文件路径、源文件内容摘要、冻结清单摘要、各题摘要与镜像 ID。源摘要用于覆盖 Git SHA 未涵盖的未提交代码。复现必须同时保留这些身份和参数；相同模型名不保证远端服务未变化。

每条 `result.json` 中，`status=completed` 只表示执行结束。`automatic_passed` 是检查结果；`passed` 才是任务验收，人工审查未完成时为 `null`。不要把退出正常、任务结束或回复“完成”当成成功。

默认用量从原生模型审计按 run ID 去重，覆盖全部已观测线程。缺少 usage、失败调用或强制停止造成的数据缺口明确标注，不用零伪造完整计量。当前没有可信订阅计价快照，**费用保持未知**；token 数不等于实际订阅扣费。

回调层模型调用次数不等于实际 HTTP 请求次数，SDK 与反代内部还可能重试。本地故障注入已经观察到一次原生模型调用对应三次 SDK HTTP 尝试；当前没有将这些层次伪装成一个完整的请求账本。模型失败只记录异常类型链和可得的状态码，不记录异常正文或请求／响应内容。

原始目录可能包含代码、模型回复和详细轨迹，分享前单独检查敏感内容；仓库中不保存这些原始实验产物。

## 报告解释与人工复核

运行结束自动生成报告，也可重新汇总：

```powershell
python -m evals.report C:\develop\sayacode-eval-results\holdout-comparison-01
python -m evals.diagnose C:\develop\sayacode-eval-results\holdout-comparison-01
```

代码接口为 `generate_report(results_root: Path, *, expected_repeats: int | None = None) -> dict`；默认从 manifest 读取预期重复数，可用 `--expected-repeats` 覆盖。

诊断文件不会改写评分结果。`abort.json` 表示实验中止；`invalid.json` 用于明确标记外部条件导致比较失效。两种情况下原始记录仍保留，报告不允许把它们当成完整机制比较。`eligible_for_comparison` 只作这一层完整性检查，不证明样本充分或因果有效。

- 主指标只使用不需要人工复核的任务，列原始 N、成功、失败和未判。
- 人工题的自动筛查与人工验收分开；待审不算失败，也不算成功。存在待审时整体成功率为未知，已判成功率仅供参考。
- 状态计数保留运行失败、暂停、超时和基础设施错误，不能删除后缩小分母。
- 重复稳定性只使用完整 `1..R` 且已判的题，单独列出未完成题。
- 两组仅按共同 repeat 逐题配对；重复试验不能当成独立题目制造置信区间。
- 报告耗时中位数和 P95，小样本长尾标为不稳定。
- 只有完整费用和完整验收时才计算每次成功费用，且计入失败试验消耗。

人工复核需要读取 `human_rubric`、对应源码、最终回答和轨迹，记录逐条判断、复核者与依据。当前不提供自动替代人工的语义评分，也不将关键词通过的审查题自动改为成功。应先形成独立复核记录，再决定是否更新验收记录并重新生成报告，保留修改前结果与来源。

## 后续扩展

首轮结果和未完成项见 [2026-10-21 评估记录](../docs/evaluation/2026-10-21.md)。摘要与显式协作场景已独立准备，仍须在接口可用时完成实际验证。后续增加真实历史任务、记忆失效专题及公开 benchmark 薄适配；不同数据集和机制分别报告，不把基础设施测试、交互契约与真实任务效果合成一个总分。
