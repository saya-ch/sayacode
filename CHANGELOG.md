# 变更日志

本文件记录 SAYACODE 每个版本的显著变更。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [未发布]

### 新增

- **运行追踪**：`trace_id` 随 ContextVar 贯穿一次请求，审计事件、工具调用、
  Hook 触发与后台委托共享同一 id；新增 `/trace` 命令列出最近追踪并展开调用树。
- **工具耗时**：工具审计事件携带 `duration_ms`，回答「这一轮慢在哪一步」不再靠猜。
- **自主计划执行**：`plan_create` / `plan_update` / `plan_get` 三个工具 + 状态图编排
  （`planner → evaluator → executor/replan → finish`），支持按表推进、销项与停滞重规划。
- **子 Agent 委托**：`delegate_to_subagent`（同步）、`delegate_async` / `delegate_poll`
  （异步派单与汇聚）、`delegate_resume`（同 thread 追问）、`delegate_cancel`（提前取消）、
  `delegate_notifications`（完成推送）。并行委托上限 10，未开跑直接撤回、已开跑中止收尾。
- **中文注释**：`lib/` 与 `tests/` 的公开符号补齐中文 docstring，公开符号覆盖率 100%。
- **架构可视化**：`docs/architecture.html` 与三张分层/时序/治理图。
- **会话回退（`/rewind`）**：用 LangGraph 原生的 `get_state_history()` 选出轮次边界、
  再用 `update_state()` 从旧 checkpoint 分叉，同步截断 `SessionManager` 镜像与
  交互记忆（文件修改记录不回退：磁盘改动并未撤销）；
  `/rewind <n>` 回退最近 n 轮。两侧必须一起退——只改图的话，下次在新进程里
  按全量导入会把回退悄悄覆盖掉。

### 变更

- `lib/agent.py` 拆分：流事件抽取迁至 `lib/agent_stream.py`，Token 用量统计迁至
  `lib/agent_usage.py`，主文件只保留 turn 编排。
- **MCP 大输出不再丢尾部（已落地）**：超长结果原先被硬截断且无法找回；现在完整内容落盘到工作区，
  模型只拿到预览 + 定位符，可经 `read_file` 再取。MCP 工具同时改用 LangChain 的
  `content_and_artifact`：结构化产物与模型可见文本分离，artifact 不进入模型视野。
- MCP 传输保持自研实现：实测 LangChain 官方 adapter 每次工具调用重建会话
  （0.58s vs 长连接 0.002s），不适合高频 Agent 循环。
- 计划循环改用 LangGraph `StateGraph`，控制态随 checkpointer 持久化。
- `requests` 依赖移除，HTTP 客户端统一为 `httpx`。
- 注释去历史化：`lib/` 与 `tests/` 不再保留「之前/曾经/实测」式叙述，只陈述现状与机理。

### 修复

- **团队 checkpointer 并发写**：多个 `TeamManager` 各开连接写同一 sqlite 文件会
  触发 `database is locked`；改为路径级共享单连接 + WAL + 30s 忙等待。
- **后台委托线程**：改为守护线程，进程退出不再被后台任务挂住；工作线程异常不再
  向外抛给无人接收的调用栈（状态已落盘、事件已唤醒）。
- **通知双重消费**：交互循环改用只读 `peek_notifications()` + 已打印集合，
  不再吃掉模型侧 `delegate_notifications` 的推送。
- **`SAIAgent.close()`**：连同图 checkpointer 连接一起关闭，消除
  `ResourceWarning: unclosed database`。
- **并行工具上下文**：批处理的线程池显式透传 `contextvars`，并行工具不再丢失 `trace_id`。
- **发布门禁误报**：`scripts/check_release.py` 的「已移除包管理器」检查原本按子串匹配，
- **审计脱敏误报**：`redact_value` 对键名含 `KEY`/`TOKEN`/`SECRET` 的任何值一律抹成 `***`，
- **审批真值误判（fail-closed 加固）**：`SayaPermissionMiddleware` 原先按真值判断恢复载荷，
- **拒绝误判（子串 → 前缀锚定）**：`_tool_result_was_blocked` 原先在**全文任意位置**匹配
  「安全警告」「操作已中止」等字样，于是读一份含这些短语的源码（项目自己的
  `file_tools.py` 就含「安全警告」）会被当成权限拒绝——触发假的 `ToolFailure` 事件
  与 `allowed=False` 审计。现在要求**首行以 ⚠️ 开头**且命中标记；`batch_edit` 的多行
  部分失败报告也不再被误判为整次拒绝。
  `"no"`、`1`、非空列表这类**非布尔真值都会被当成批准**。现在只认显式布尔（`True` 或
  `{"approved": True}`），其余按畸形拒绝，并在审计里记 `approval: malformed`。
  于是 `input_tokens` 这类**计数**也被当成凭据，指标失去可观测性；数值不可能是凭据，已放行。
  会把 `uvicorn` 这类正常依赖当成残留引用并中断流水线；改为整词匹配。`tests/` 中
  四处假凭据改为分段常量，不再命中密钥扫描。新增 `tests/test_release_gate.py`
  锁住引用检查行为。
- **析构兜底释放 sqlite 连接**：`AgentRunner.close()` 的契约写着「rebuild/析构时调用」，
  但全库没有析构函数，调用方漏掉 `close()` 时连接只能等 GC —— 全量测试因此产生
  72 条 `ResourceWarning: unclosed database`。补 `__del__` 兜底后告警降为 0。
- **静默失败可见化**：计划落盘、记忆恢复、worker 邮箱投递失败原本被
  `except Exception: pass` 吞掉（表现为「工具报成功、磁盘上什么都没写」）；
  改为 warning 级日志，其余清理类路径改 debug 级。
- **POSIX 菜单按键卡死**：`_read_menu_key` 的 termios 分支读到 ESC 后会继续阻塞读
  两个字符，单独按 ESC 时终端就停在那里；改为只消费终端已就绪的转义序列字节。
  该分支此前没有任何测试覆盖（Windows 走 msvcrt、Linux CI 无 TTY）。

### 工程化

- **运行追踪**：新增 `lib/core/tracing.py`（`trace_session` / `span` / `traced`）与
  `/trace` 命令；工具审计事件带 `duration_ms`。
- **原生中间件接入（计划中，未落地）**：上下文剪枝（`ContextEditingMiddleware`）与
  单轮调用护栏（`ToolCallLimitMiddleware` / `ModelCallLimitMiddleware`）暂未接入，
  当前仍为自研四层中间件（Hook→Permission→Safety→Prompt）。
- **模型调用可观测**：新增 `ModelCallTraceHandler`（框架 `BaseCallbackHandler`），把每次模型
  调用的耗时与 token 用量写进同一条 trace；`/trace` 因此能回答「模型这一步花了多久、用掉多少」。
- **工具结果契约（artifact，未装配）**：新增 `lib/core/tool_result.py` 声明 artifact 形状（`tool` /
  `outcome` / `chars` / `spill_path`）并提供校验。每个工具结果都会补上 `{tool, outcome, chars}`
  （工具自己产出的 artifact 原样保留），审计据此结构化地看到「这次调用是什么结局」，
  `ToolMessage.artifact` 从「产出但无人消费」变成有真实消费者；形状不合契约会记 warning
  但仍被记录（可观测性契约失败不改变模型可见文本）。
- **覆盖率门禁（与实测对齐）**：按包门槛为 `lib 60% / core 71% / models 79% / tools 51% / cli 39%`
  （实测 Windows／Python 3.13：`lib 62.0%` · `core 72.6%` · `models 80.6%` · `tools 52.3%` · `cli 40.2%`，见 `scripts/check_coverage.py`）。
- **不稳定用例检测**：新增 `scripts/check_flaky.py`，连跑多轮区分「真实失败」与
  「flaky」；CI 增加手动触发的 `flaky-check` 作业。
- **协作规范**：新增 `CONTRIBUTING.md`（分支策略、本地/CI 对齐、质量门禁、
  PR 清单与全量测试排障顺序）与本文件。
- **URL 校验去重**：新增 `lib/core/urls.py`，把 `state.py`、`runtime/model_profiles.py`、
  `cli/configure.py` 中三份逐字重复的 base URL 校验收敛为单一实现，
  避免各处分别修 bug 后行为漂移。
- **终端读键去重**：新增 `lib/cli/ttykeys.py`（`raw_terminal` / `read_escape_tail`），
  菜单与权限提示共用同一份 POSIX 转义序列解析。

### 测试

- 全量约 710 用例通过（Windows／Python 3.13 实测，见 `scripts/check_coverage.py`），
  `lib` 整体行覆盖率约 62%。全绿以 `python -m pytest -q` 0 failed 为准
  （缺厂商包时补装 `langchain-anthropic` / `langchain-google-genai`）。
- 修正三处依赖「本机是否装了厂商包」的用例，改为 monkeypatch 模拟，
  使断言不再随环境漂移。
- 新增 `tests/test_tracing.py`、`tests/test_plan_execute.py`、`tests/test_async_delegate.py`、
  `tests/test_parallel_delegate.py`、`tests/test_delegate_cancel.py`、
  `tests/test_delegate_followup.py`、`tests/test_delegate_push.py` 等。
- 安装 `langchain-anthropic` 与 `langchain-google-genai` 后本地全量与 CI 一致全绿。

## [1.4.0] - 2025

### 新增

- **结构化流事件**：`StreamEvent` 取代 `[思考: ...]` / `[调用工具: ...]` 带内字符串协议，
  agent 层只发射事件、theme 层只消费事件，两侧不再共享字符串格式约定。
- **思考链与工具活动实时展示**：流式渲染按发生顺序**持久**打印，不再只有一个「思考中…」；
  停摆期间状态行耗时继续走。
- **执行核中间件化**：Hook → Permission → Safety → Prompt 四层中间件进入 LangGraph 图内，
  政策判定与工具执行分离。
- **导入惰性化**：`lib` 顶层导出按 PEP 562 延迟解析，轻量模块不再拖入整个 agent 栈。

### 修复

- Windows 控制台 `cp1252` 编码不了脚本中的中文，导致 CI 全红。
- 非交互模式下不再提问，并正确读取记住的工作区。
- 上下文压缩失败不再静默，删除判据不再污染只读工具。
- 危险工具底线不可再被绕过，会话授权可撤销。

### 变更

- 模型层重写为声明式 provider 目录，协议类直接继承 LangChain 官方集成；
  Gemini 由 653 行手写 REST 改为 `langchain-google-genai`。
- 多 Agent 调度改由 `langgraph-supervisor` 接管，图状态用 `langgraph-checkpoint-sqlite` 持久化。
- 覆盖率门禁由裸 `pytest` 改为按包校验（`scripts/check_coverage.py`）。

[未发布]: https://github.com/saya-ch/sayacode/compare/v1.4.0...HEAD
[1.4.0]: https://github.com/saya-ch/sayacode/releases/tag/v1.4.0
