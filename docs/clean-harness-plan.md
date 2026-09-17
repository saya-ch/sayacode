# 干净的 langchain/langgraph + rich：目标架构（C 方案全量）

> 起因：用户要求"干净的 langchain/langgraph + rich coding agent，充分利用 harness 工程"。
> 范围确认：一次做到 D（含 Supervisor 收编）。勘测见 4 份子报告（turn 链路 / session-memory /
> 权限-安全-hooks / team），关键证据行号均已钉住，此处只写结论与落点。

## 0. 原则（违反任何一条都要在提交信息里说明理由）

1. **框架管执行，我们只写政策和展示**：graph、middleware、checkpoint、store、interrupt、
   streaming 全部用 harness 原生；自制执行机制（mailbox 调度、字符串事件协议、手拼消息史）退役。
2. **保留真正的业务资产**：provider 兼容层（`lib/models/compat.py`，langchain-openai 会丢非标字段，
   这是我们的真增值）、全部工具实现、安全策略语义、压缩语义、中文 rich TUI。
3. **兼容性承诺**：`~/.sayacode/` 用户配置不动；session JSON v2 保持可读（只增不改，迁移脚本一次性）。
4. **每阶段独立可验收**：A、B、C 各自全绿（pytest + ruff + mypy + coverage + 真 PTY）才进下一阶段。

## 1. 现状 → 目标映射

| # | 自制机制 | 目标落点 | 阶段 |
|---|---|---|---|
| 1 | `AgentRunner` 手搭 ReAct（`create_react_agent` 裸调）+ `ConversationManager` 手拼消息 | `langchain.agents.create_agent(model, tools, middleware=[...], checkpointer, store)`；`run/stream_run` 瘦成"重试+恢复"薄循环 | A |
| 2 | `PromptBuilder` + `ContextPackager` + `reminders` 拼 system prompt | `SayaPromptMiddleware`（`wrap_model_call` + `override(system_message)`，即官方 `dynamic_prompt` 机制；每轮 `refresh()` 一次，与今天同成本） | A |
| 3 | `SessionManager` JSON（schema v2）+ 三档压缩（70/80/90%）+ `metadata.compressed` 契约 + `compact_*.json` 归档链 | checkpointer（`thread_id=session_id`，SQLite）接管"存消息"；压缩三档 + `compressed` 标记 + 归档链**原样保留在 session.py**（框架无等价物，不硬搬），外层在压缩后 `update_state` 同步图状态；session.json 保留为人类可读镜像 | A |
| 4 | `MemoryManager`（交互/工具计数）+ `project_memory`（SAYACODE.md 文件链） | `MemoryManager` JSON 仍是权威；store 只收回合摘要镜像（`remember_turn`，供未来子 agent 读）。project_memory 文件链不动（store 迁移是 C 阶段的事） | A |
| 5 | 权限链（mode deny > session > policy + 危险地板）+ `confirm_callback` 弹窗 + `DenialTracker` | `SayaPermissionMiddleware.wrap_tool_call` 前置：deny 直接短路，ask → `interrupt()`（框架 HITL），批准经 `grant_once`（防恢复后内联 check 双弹窗；放共享状态，跨实例可见；`reset` 连带清） | A |
| 6 | `SafetyChecker` 两套等级 + `check_file/delete_danger` | `SayaSafetyMiddleware.wrap_tool_call` 内层：**只否决**（判据用工具体实际走的 `tools/safety` 原语——实测 `SafetyChecker.check_file_operation` 在 Windows 下漏拦 System32 写，外层必须用严的那套）；ask 升级仍由体内联检查负责 | A |
| 7 | hooks（Pre/Post/Failure 串行短路 + 超时）+ `ToolAbortController` | `SayaHookMiddleware`：工具事件进 `wrap_tool_call`（顺序最外层，保持 Pre 先于权限；被拦 sniff 沿用 `_tool_result_was_blocked` 标记表）；`UserPromptSubmit/SessionStart/End` 不动（进 `wrap_model_call` 会变成每步触发，语义变了） | A |
| 8 | `[思考:]/[调用工具:]/[工具结果:]` 带内字符串协议（发射 4 处全在 `agent.py`，消费 1 处在 theme） | `StreamEvent` 强类型事件（`text/reasoning/tool_start/tool_result/tool_error`）；agent 只发射事件，theme 只消费事件；`_parse_tool_stream_message` 保留作兼容层 | B |
| 9 | `/team` 自制编排：mailbox 文件总线 + 多进程 worker + `TeamConfig/WorkerState` | `langgraph-supervisor`（新增依赖，0.0.31 可用）接管**调度**；执行层暂留进程 + worktree（崩溃隔离 + 可观测性，见风险） | C |
| 10 | `TeamWorktreeManager` 隔离 + `get_delivery` 交付链 | **保留**（supervisor 无工作区隔离，并发 builder 必冲突）；交付改成"分支 + diff 摘要"工具结果回传 | C |

## 2. Phase A 详细设计（执行核中间件化）

文件：新增 `lib/core/middleware.py`（Hook / Permission / Safety / Prompt 四个中间件），
`AgentRunner` 改调 `create_agent(..., middleware=[...], checkpointer=SqliteSaver, store=InMemoryStore)`。
没有 compaction 中间件——压缩语义（阈值/归档/`compressed` 标记）框架无等价物，
硬搬只会把三档预算和归档链丢掉；压缩仍由 session.py 驱动，外层压缩后
`update_state` 同步图状态。崩溃恢复语义与今天完全一致（今天 crash 也丢当轮镜像）。

- 中间件顺序 = 今天的执行顺序：Hook-Pre → Permission → Safety → 执行 → Hook-Post/Failure。
  今天 PreToolUse 先于权限（`tools/__init__` wrapper L193 vs 工具体内 L255），搬过去时**保持这个先后**，
  测 Déjà vu：已有测试钉住顺序就地改名，不重写断言语义。
- `_classify_error` 四分类 + `_MAX_RETRIES=3` + `prompt_too_long→压缩→重建` 保留在外层薄循环
  （框架的 `ModelRetry/ToolRetry` 覆盖不了"压缩后重建消息"这个自制语义）。
- `memory.store` 落点：`finish_turn` 后 `remember_turn` 写回合摘要镜像进 store
 （`MemoryManager` JSON 仍是权威；画像拼装 `get_recent_context` 不动，不提前消费）。
- `lib/__init__.py` eager-import agent 是 17 秒冷启动的根因之一：本阶段把顶层 import 改成
  惰性（`__getattr__`），`providers.py` 四个可选 SDK 改按需解析。这是 A 的附带收益，不单独立项。

## 3. Phase B 详细设计（结构化流事件）

- `lib/runtime/events.py` 是实在的 headless JSONL 事件系统（不是空壳）：`StreamEvent`
  落这里，与 JsonlEventWriter 共用脱敏与版本约定，不另起第三个事件系统。
- `StreamEvent = {kind, text?, tool?, preview?, error?}` frozen dataclass；
  `SAIAgent._extract_*` 四个发射点改 return 事件；`_extract_stream_delta` 改 `Iterator[StreamEvent]`；
  theme 的 `render_streaming_agent_message(chunks: Iterable[StreamEvent|str])` 双签收（str 走兼容解析）。
- 验收：现有 5 条流测试全部改名不断言（事件版 + 字符串兼容版各一套），真 PTY 重跑。

## 4. Phase C 详细设计（Supervisor 收编）

- `pyproject` 加 `langgraph-supervisor`；supervisor 图节点 = builder/planner/reviewer（system prompt
  沿用现有 mode prompt + role_prompt 语义）。
- mailbox 降级为审计日志（只写不读）；`WorkerManager` 进程池保留给 builder（崩溃隔离），planner/reviewer
  进图内调用；`max_workers=4` 在图内用 Semaphore 复刻。
- `shared-builder` 在图内同进程竞写更危险：收编后默认禁用，需 `--allow-shared-builder` 显式开。
- `/team result/wait/diff` 输出形状不变（契约冻结），内部改读图 state + checkpointer。

## 5. 不碰的东西

- `lib/models/`（刚重写完，最干净的一块）。
- 全部工具实现（只改挂载方式，不改行为）。
- rich TUI 形态（行式 REPL，不做全屏）。
- `~/.sayacode/*` 所有用户文件格式。

## 6. 验收（每阶段）

`pytest` 全绿 + ruff + mypy + coverage 门槛 + `check_release` + 真 PTY 回放 + 真网关 E2E。
回退敏感性规矩不变：每个新语义都要有"回退→红"的实证。
