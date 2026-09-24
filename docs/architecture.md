# SAYACODE 架构

本页描述 SAYACODE 3.0 的代码结构。运行入口是本机 Web 服务，`-p` 和 `--doctor` 仍走无头 CLI。浏览器、FastAPI 和事件广播负责呈现与交互；Agent 循环、工具调度、消息状态、待办、摘要、检查点与审批中断由 LangChain / LangGraph 承担。

## 依赖与所有权

```text
浏览器 frontend/ ──HTTP / SSE──> web/ ──协议调用──> host/
                                                 │
无头 CLI ─────────────────────────────────────────┼──> application.py
                                                 │          │
                                                 │          ├─ agent/ + approvals/
                                                 │          ├─ tasks/ + memory/
                                                 │          └─ tools/ + extensions/
                                                 └─ AgentRuntime / Store
```

| 层 | 唯一职责 |
| --- | --- |
| `frontend/` | React + TypeScript + Vite 界面；通过类型化 HTTP API 取快照和提交操作，通过 SSE 看增量事件。 |
| `web/` | FastAPI 请求模型、响应投影、本机访问校验、静态文件和 SSE。路由不持有 Agent 任务。 |
| `host/` | 一个进程内协调器：工作区目录、共享运行时、任务句柄、按工作区装配资源、事件广播与产品操作。 |
| `application.py` | 为一个工作区组装模型、图、工具、中间件、MCP、Hook、Skill 和记忆。 |
| `agent/` | 官方模型适配、`create_agent` 图、LangGraph 检查点、流事件、摘要与恢复。 |
| `tasks/` | 可继续子 Agent、持久 Inbox、运行生命周期与可选 Git worktree 交付。 |
| `approvals/` | 静态信任策略、Jev 审理、官方 HITL 中断与恢复。 |
| `tools/` | 文件、Shell、只读 Git、搜索与代码分析等原生 LangChain 工具。 |
| `extensions/` | MCP、Skill、Hook 与项目或用户说明文件。 |
| `memory/` | LangGraph Store 上的跨会话记忆检索、学习、失效与提交。 |
| `cli/` | `sayacode` Web 启动器、`-p` 无头运行、`--doctor` 和 JSONL 序列化。 |

`web/` 只依赖宿主协议，不解析 CLI 命令，也不直接运行图。`host/` 通过 `application.py` 使用原有 Agent 能力；单次无头任务仍可使用同一应用层。产品层不定义另一个通用 Agent Runner 或工具调度器，同一轮的多个工具由官方 ToolNode 执行。

## 启动与进程生命周期

`sayacode.cli.main:main` 和 `python -m sayacode` 使用同一入口。普通启动由 `cli/web.py` 在 `127.0.0.1` 创建 Uvicorn 服务；`WebHost.open()` 打开一次 `AgentRuntime` 和一次 `TaskManager`，加载起始工作区。未配置模型仍可启动页面，图和模型在实际运行时构造。

用户在页面添加工作区时，`host/workspaces.py` 将规范化路径记录到 LangGraph Store；不会扫描整台机器。每个已打开的工作区只装配一套自己的 MCP、Hook、Skill 和记忆资源，共享进程内的 checkpoint 连接和任务管理器。启动时对持久任务只做一次孤儿协调，防止一个工作区误判另一个工作区的活动任务。

浏览器发送消息时，FastAPI 只校验并返回 `202` 和运行标识；宿主创建 `asyncio.Task`，由应用层流式运行图。断开页面连接不会取消运行。服务退出时，对运行任务请求 `RunControl.request_drain()`，按可配置宽限期等待，再执行任务管理器与运行时收尾。宽限期后的强制取消可能留下无法确认的在途系统操作，后续应检查任务状态再恢复。

## 持久状态与实时事件

```text
LangGraph checkpoint：消息、待办、摘要、工具消息、审批中断
LangGraph Store：工作区与会话目录、任务关系、Inbox、交付和长期记忆
进程内宿主：运行句柄、RunControl、近期事件与 SSE 订阅者
浏览器：当前视图、表单与快照缓存
```

一个主会话或子 Agent 对应一个 `thread_id`。后续用户输入只追加增量消息；审批用相同线程上的 checkpoint 和 `Command(resume=...)` 继续。Store 不保存第二份聊天历史。页面重连时先读取线程快照，再从近期 SSE 缓冲补收事件；事件缓冲只为显示服务，超过缓冲或服务重启时重新读取快照。`EventHub` 对多个浏览器连接扇出事件，并保留工作区、线程、任务和运行标识，便于区分 SAYA 与各个子 Agent。

审计日志用于历史工具轨迹和诊断，不取代图中的消息。Web 响应对凭据字段脱敏；前端只接收展示所需字段，不读取原始任务档案或密钥。

## 子 Agent 协作

`builder`、`planner`、`reviewer` 各有独立图线程、待办与检查点。派发时传入目标和有界上下文快照，不把父线程历史复制给子线程。父子消息由 Store Inbox 持久保存；接收线程在下一次模型调用前通过中间件读取，空闲父线程可被完成通知触发继续运行。子线程不能直接改写父待办；父 Agent 根据结果自行调整计划。

Git 项目中，builder 可选用独立 worktree；快照包含派发时未提交和未跟踪文件。交付只计算相对派发基线的新增差异，用户在 Web 界面查看后显式应用。共享工作区模式直接修改当前文件，不产生可单独应用的交付。worktree 不约束 Shell 或绝对路径访问，因此只承担代码组织职责。

## 信任、审批与本机访问

`read_only` 不提供写文件、Shell 和未知 MCP；`ask` 对有副作用的操作请求人工批准；`jev` 在相同静态工具范围内进行风险审理并把不确定操作转给人工；`full` 不弹工具批准。会话持有自己的信任档，新会话继承用户默认值。审批提交携带 checkpoint 标识，宿主拒绝过期决定；批准后按最新拒绝规则复核实际调用。

Web 服务监听本机回环地址。启动地址中的一次令牌用于建立浏览器会话；写请求校验会话、来源和 CSRF。此边界保护本机 HTTP API，**不是**工具沙箱。文件工具可接受工作区外的绝对路径，Shell 以当前用户身份运行，使用完全信任前应理解其实际权限。

## 模型、Skill、MCP 与记忆

模型由用户选择协议并填写地址、密钥、ID 和 token 预算。官方 LangChain 提供商适配器负责各协议，不基于供应商名称猜测。Skill 从项目和用户目录发现，模型只先看到目录摘要，正文由 `load_skill` 按需进入当前线程状态。MCP 通过 LangChain `MCPAdapter` 连接，项目服务器先经过工作区信任。Hook 的模型和工具事件来自官方 middleware/callback，用户输入与会话事件由应用入口触发。

用户偏好和项目事实位于 LangGraph Store。模型请求中间件只投影当前相关的有效记录；LangMem 可提出带来源的修订，产品层检查作用域、版本、遗忘状态后提交。自动学习默认关闭。人工说明读取 `SAYACODE_HOME/instructions.md`、`SAYACODE.md` 和 `CLAUDE.md`，与自动学习记忆分开。

## 构建与验证

`pyproject.toml` 和 `uv.lock` 是 Python 依赖与构建基线；`frontend/package-lock.json` 固定前端依赖。前端构建写入 `src/sayacode/web/static/`，wheel 携带已构建资源，因此安装和启动 wheel 不需要 Node.js。源码开发和重建资源需要 Node.js。

```bash
uv sync --locked --extra dev
uv run --no-sync python -m pytest -q
uv run --no-sync python -m ruff check src tests scripts
uv run --no-sync python -m mypy src
cd frontend
npm ci
npm run build
npm test
```

发布检查还验证前端构建结果、wheel 安装与真实本机 HTTP 启动。测试以快照恢复、SSE 扇出、审批、跨工作区隔离、子任务生命周期和安装后启动等用户行为为准。用户操作步骤见 [WebUI 使用指南](webui.md)。
