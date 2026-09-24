<div align="center">
  <img src="https://raw.githubusercontent.com/saya-ch/sayacode/main/assets/image2.png" alt="SAYACODE 项目横幅" width="100%">

  <h1>SAYACODE</h1>
  <p><strong>把编程 Agent 的工作过程，带到你看得见、管得住的本机工作台。</strong></p>
  <p>从一次提问到多 Agent 协作：对话、计划、工具、审批与代码交付，集中在一个浏览器页面。</p>

  <p>
    <a href="#快速开始">快速开始</a> ·
    <a href="#工作台">探索工作台</a> ·
    <a href="#运行架构">运行架构</a> ·
    <a href="https://github.com/saya-ch/sayacode/blob/main/docs/webui.md">使用指南</a> ·
    <a href="https://github.com/saya-ch/sayacode/blob/main/CHANGELOG.md">更新日志</a>
  </p>

  <p>
    <img src="https://img.shields.io/badge/Python-3.11%20%E2%80%93%203.13-334155" alt="Python 3.11 至 3.13">
    <img src="https://img.shields.io/badge/Version-3.0.0-EF5DA8" alt="版本 3.0.0">
    <img src="https://img.shields.io/badge/License-MIT-334155" alt="MIT 许可证">
  </p>
</div>

---

SAYACODE 是基于 **LangChain / LangGraph** 的本机编程助手。左侧管理工作区与会话，中间呈现对话和工具轨迹，右侧展示计划、子 Agent、审批与代码交付。Agent 由 LangChain `create_agent` 构建；LangGraph 管理消息、待办、检查点和审批中断。Web 页面只展示和操作运行结果，不另建一套 Agent 执行协议。

**3.0 的核心变化：**交互式 TUI 已由 FastAPI + React 工作台取代；适合脚本和 CI 的无头 CLI 继续保留。

## 快速开始

需要 **Windows 或 Linux**、**Python 3.11–3.13**。安装已发布的包无需 Node.js：

```bash
python -m pip install --upgrade sayacode
sayacode --workspace .
```

也可以从源码启动：

```bash
git clone https://github.com/saya-ch/sayacode.git
cd sayacode
uv sync --locked
uv run sayacode --workspace .
```

`sayacode` 启动后会打印本机地址并尝试打开浏览器。关闭浏览器标签不会结束已启动的 Agent 运行；结束 CLI 进程会按配置宽限期停止后台工作并保存检查点。若不希望自动打开浏览器，使用 `sayacode --no-open`；如需指定端口，使用 `sayacode --port 8765`。默认只监听 `127.0.0.1`，`--port 0` 自动选择可用端口。

首次打开页面后，从输入框底部的设置按钮进入“模型连接”，添加并启用模型。左侧可用目录选择器加入工作区、新建会话，再在底部输入任务；不会自动扫描整台电脑。模型尚未配置时，页面仍可打开并完成设置。

界面语言可在设置中选择中文、English 或跟随浏览器；Agent 回答语言仍遵循用户输入和运行偏好。

> 完整操作见 [WebUI 使用指南](https://github.com/saya-ch/sayacode/blob/main/docs/webui.md)。

## 工作台

| 区域 | 内容与操作 |
| --- | --- |
| **工作区与会话** | 浏览并选择本机目录，搜索、切换、新建或删除独立会话。 |
| **对话与运行轨迹** | 在对话中查看紧凑的工具和模型活动；详细轨迹按运行分组，区分 SAYA 与每个子 Agent。 |
| **计划与协作** | 查看主 Agent 和子 Agent 的待办，使用关系图或列表追踪任务进度。 |
| **审批与交付** | 逐项核对待批准调用；查看独立 worktree 的差异，再显式应用到主工作区。 |
| **输入与设置** | 运行中先排队消息，再按需直接送往下一步骤；输入框提供附件、压缩、Skill、线程模型与信任档，完整配置集中在一个设置窗口。 |

运行事件通过本机 SSE 连接推送到页面。每条轨迹带所属线程；切换到子 Agent 可以查看其对话、工具调用和待办。页面断线后会重新读取检查点快照，并在可用范围内补收事件。浏览器只承担观察与操作，运行任务由 CLI 进程持有。

运行中继续输入时，消息先进入输入框旁的持久队列。排队项可编辑、删除，或点击“直接发送”让同一条消息尽快在 Agent 的下一模型步骤生效；已经发出的模型请求和正在执行的工具不会被这个操作中途改写。主会话的“停止”会同时请求所有子 Agent 停止，保留检查点和未处理交付，待用户显式继续。

## 模型接入

模型配置不预设供应商。添加模型时填写协议、接口地址、API Key、模型 ID、上下文长度和最大输出 token 数；只有端点明确无需认证时才选择无密钥。配置保存在本机，模型密钥不会自动从环境变量读取。

| 协议 | 配置值 |
| --- | --- |
| OpenAI Chat Completions | `openai_chat_completions` |
| OpenAI Responses API | `openai_responses` |
| Anthropic Messages | `anthropic_messages` |
| Gemini Native generateContent | `gemini_generate_content` |
| Ollama Native Chat | `ollama_native_chat` |

协议决定使用哪个官方 LangChain 模型适配器；SAYACODE 不根据 URL 或模型名称猜测供应商。Web 设置支持新增、修改、测试、启用和删除模型。模型测试会实际请求所配端点，请留意服务商费用。

无密钥模式不会读取环境变量中的服务商密钥。部分官方适配器要求提供非空密钥参数，SAYACODE 会传非秘密占位值；若自建端点严格拒绝任意认证头，需要在端点侧允许该占位值。

## 审批与执行边界

输入框底部可为当前线程选择模型和信任档；新会话读取用户默认值。主会话与每个子 Agent 都是独立线程，模型切换在下一次运行时生效。

| 档位 | 行为 |
| --- | --- |
| 只读 `read_only` | 提供读取、搜索、分析等工具；不提供写文件、Shell 和未知 MCP 工具 |
| 询问 `ask` | 只读操作直接执行；有副作用的操作逐项请求人工批准 |
| Jev 自动审理 `jev` | 对原本需要批准的操作进行风险审理；不确定或服务不可用时转人工 |
| 完全信任 `full` | 工具调用不弹出批准对话框 |

审批窗口展示工具与参数，可逐项批准或拒绝；询问档允许在本会话内记住完全相同的调用。审批继续使用 LangGraph 原生中断与恢复。SAYACODE **没有操作系统沙箱**：文件工具可以访问工作区外的绝对路径，Shell 使用当前登录用户权限。worktree 用于组织子任务的代码交付，不隔离其进程或工作区外路径。

Web 服务只绑定本机地址，并使用启动令牌、会话 Cookie 和请求校验保护本机 API；这不改变 Agent 工具的系统权限。

## 子 Agent 与交付

主 Agent 可以派发 `builder`、`planner` 和 `reviewer`。子 Agent 有独立 LangGraph 线程、检查点和待办，可以在主 Agent 继续工作时运行。任务卡显示标题、角色、状态和结果；完成后的结果进入父任务 Inbox，父 Agent 可在空闲时继续处理，也可对既有子 Agent 追问。

对 Git 工作区，builder 可使用独立 worktree，也可选择共享工作区。独立 worktree 会带入派发时的已提交、未提交和未跟踪内容，新增差异需在右侧查看并**显式应用**；发生冲突时不会部分应用。共享工作区直接修改文件，没有独立差异交付。非 Git 项目的 builder 使用共享工作区。

页面提供子任务的停止、恢复、追问、审批、差异查看与应用。停止主会话会一并停止其所有未结束子 Agent；单独停止子 Agent 只影响该任务。子 Agent 需要人工审批时，审批卡会指明对应线程。

## 扩展与记忆

- **MCP**：通过 LangChain `MCPAdapter` 接入，Web 设置可管理用户或项目服务器、项目信任和重载。MCP 工具仍经过会话信任策略。
- **Skill**：读取项目 `.agents/skills/<名称>/SKILL.md` 与用户 `SAYACODE_HOME/skills/<名称>/SKILL.md`。页面可查看并为当前线程启用；Agent 也能按需调用 Skill 工具加载正文与参考文件。Skill 不提升工具权限。
- **长期记忆**：用户偏好和项目事实保存在 LangGraph Store，会话消息仍保存在各自的 checkpoint。自动学习默认关闭，可在 Web 设置中启用、查找、查看依据、固定、更正、确认或遗忘记录。人工项目约定读取 `SAYACODE.md` 和 `CLAUDE.md`；用户说明读取 `SAYACODE_HOME/instructions.md`。详见[记忆系统设计](https://github.com/saya-ch/sayacode/blob/main/docs/design/memory-system.md)。
- **诊断**：Web 设置提供 Git 状态、项目分析、符号定位和运行检查。
- **会话与运行**：在当前线程查看工具目录、追踪和检查点；空闲时可手动聚焦摘要或回退对话。回退只改变图状态，不撤销文件。工作区 Hook 可在同一面板查看、信任和重载。

本地状态默认位于 `~/.sayacode/`，可用 `SAYACODE_HOME` 指定其他目录。

## 脚本与 CI：单次运行

`-p` 保留无需浏览器的单次任务接口，适合脚本和 CI：

```bash
sayacode -p "审查当前改动" --trust read_only
sayacode -p "分析项目入口" --output-format json
sayacode -p "检查构建失败" --output-format jsonl
```

`-p -` 从标准输入读取任务。输出格式有 `text`、`json`、`jsonl`；`--skill <名称>` 可以为单次任务启用 Skill。`--doctor` 运行无头诊断。无头执行遇到人工审批时保存暂停检查点并以退出码 `3` 结束。

| 退出码 | 含义 |
| --- | --- |
| `0` | 成功 |
| `1` | 运行失败 |
| `2` | 参数或配置错误 |
| `3` | 等待交互审批 |
| `130` | 用户中断 |

## 运行架构

```mermaid
flowchart LR
    UI[React / TypeScript 工作台] -->|HTTP + SSE| WEB[FastAPI Web API]
    WEB --> HOST[多工作区宿主]
    CLI[无头 CLI] --> APP[应用组装]
    HOST --> APP
    APP --> AGENT[LangChain create_agent]
    AGENT --> GRAPH[LangGraph 图、检查点与 Store]
    APP --> TASKS[子 Agent、Inbox 与可选 worktree]
    APP --> EXT[MCP、Skill、Hook 与工具]
```

**状态各有归属：**LangGraph checkpoint 保存消息、待办、摘要和审批中断；LangGraph Store 保存工作区、会话目录、任务关系、Inbox 和长期记忆；宿主仅保存进程内任务句柄与近期展示事件。页面和本地审计不保存第二份聊天历史。详细的生命周期与数据边界见[架构说明](https://github.com/saya-ch/sayacode/blob/main/docs/architecture.md)。

<details>
<summary>查看代码目录</summary>

```text
frontend/                   React、TypeScript、Vite 页面
src/sayacode/web/           FastAPI 路由、请求校验、静态资源
src/sayacode/host/          多工作区宿主、事件广播、产品操作
src/sayacode/application.py 单工作区 Agent 组装
src/sayacode/agent/          官方模型、图与检查点运行
src/sayacode/tasks/          可继续子 Agent、Inbox 与交付
src/sayacode/approvals/      信任策略、Jev 与原生审批
src/sayacode/tools/          文件、Shell、Git、搜索与分析
src/sayacode/extensions/     MCP、Skill、Hook 与人工说明
src/sayacode/memory/         跨会话记忆
src/sayacode/cli/            Web 启动器与无头输出
```

</details>

## 从 2.x 升级

3.0 是**破坏性交互更新**。普通 `sayacode` 改为启动本机 WebUI；原交互式 TUI 和斜杠命令入口已移除，相关操作转到页面。`-p`、`--doctor` 与三种无头输出格式继续可用。升级前请备份 `SAYACODE_HOME` 并阅读[更新日志](https://github.com/saya-ch/sayacode/blob/main/CHANGELOG.md)；不要将旧版终端交互教程直接用于 3.0。

## 开发与贡献

源码开发需要 Node.js 来构建前端；安装已构建的 wheel 不需要 Node.js。构建产物随 Python 包分发。常用检查：

```bash
uv sync --locked --extra dev
uv run --no-sync python -m pytest -q
uv run --no-sync python -m ruff check src tests scripts
uv run --no-sync python -m mypy src
cd frontend
npm ci
npm run build
```

发布检查还包含 wheel 安装后的本机 Web 冒烟。仓库使用 [MIT 许可证](https://github.com/saya-ch/sayacode/blob/main/LICENSE)；欢迎通过 [Issues](https://github.com/saya-ch/sayacode/issues) 反馈问题。
