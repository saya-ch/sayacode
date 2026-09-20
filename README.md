# SAYACODE 2.0

SAYACODE 是面向本地工作区的终端编程助手。它使用 LangChain 的 `create_agent` 构建智能体，并使用 LangGraph 管理对话状态、检查点、中断和任务进度。终端在此基础上提供工作区工具、权限决策、模型配置和小型 JSONL 事件协议。

源码职责和依赖方向见 [代码结构说明](docs/architecture.md)。

## 安装与启动

需要 Python 3.11 至 3.13。持续集成覆盖 Windows 和 Ubuntu 上的三个版本。

```bash
python -m pip install .
sayacode --version
sayacode --help
```

启动交互式终端，并使用 `/model add` 配置接口协议和服务端点：

```bash
sayacode --workspace /path/to/repository
```

在 PowerShell 中：

```powershell
sayacode --workspace C:\path\to\repository
```

设置向导会依次询问六个字段：**接口协议**、**服务地址**、**接口密钥**、**模型编号**、**上下文长度**和**最大输出 token 数**。请选择服务端实际实现的协议：`openai_chat_completions`、`openai_responses`、`anthropic_messages`、`gemini_generate_content` 或 `ollama_native_chat`。例如，OpenAI 对话补全服务端可以使用 `https://api.openai.com/v1` 作为服务地址。协议决定 LangChain 集成方式和请求格式，仅修改地址不会把一种协议转换成另一种协议。SAYACODE 不会根据公司名或模型名推测协议。

`/model add` 把配置存入 `~/.sayacode/config.json`，并根据模型编号生成本地名称。**接口密钥默认必填。**托管服务端通常需要有效密钥，缺失或错误的密钥会返回 HTTP 401。向导拒绝空密钥，且不会读取环境变量。只有确认服务端确实不需要鉴权时，才在隐藏的密钥提示处输入 `none`。密钥保存在本地配置文件中，隐藏输入不会记入终端输入历史。要修正已有配置，可输入 `/model key <name>`，在隐藏提示处填入真实密钥；输入 `none` 则表示清空，用于无鉴权服务端。不要把密钥写在斜杠命令中。使用 `/models` 查看已配置的模型和协议，使用 `/model use <name>` 切换，使用 `/model test [name]` 检查文本、工具调用和流式能力。`--profile <name>` 在启动时选择已保存的配置。最小配置文件的样子如下：

```json
{
  "default_profile": "main",
  "default_trust": "ask",
  "profiles": {
    "main": {
      "name": "main",
      "protocol": "openai_chat_completions",
      "base_url": "https://api.openai.com/v1",
      "api_key": "YOUR_API_KEY",
      "model_id": "YOUR_MODEL_ID",
      "context_length": 128000,
      "max_output_tokens": 8192
    }
  }
}
```

如需单次覆盖，请同时提供 `--protocol`、`--base-url`、`--model-id`、`--context-length` 和 `--max-output-tokens`，再加上 ** `--api-key <key>` ** 或 ** `--no-api-key` ** 其中之一。两个鉴权选项互斥，仅在服务端不需要鉴权时使用 `--no-api-key`。两种 token 数量都接受 `128000`、`256k`、`1M` 这类写法。Shell 命令参数可能被本机其他进程看到，因此保存密钥建议使用已保存的配置。无密钥配置会在官方软件包要求时提供无害占位符，而不会从环境中读取可能敏感的服务商密钥。智能体会把工具结构通过所选 LangChain 适配器传递。当服务端支持结构化输出时，可在配置中把 `tool_selector_max_tools` 设为正数，以启用官方的大模型工具筛选，这会增加一次模型调用。已配置的上下文长度用于决定自动总结时机（同时为 Ollama 配置 `num_ctx`）。当前设置支持上述五种线路协议和显式接口密钥鉴权；引入全新协议或自定义鉴权方式需要另一个官方适配器或额外配置。

配置文件格式是全新设计的。旧的 `provider`、`model`、`config_fields`、`user_policy` 和 `mode` 条目会被拒绝，请改用协议配置和 `default_trust`。已有会话检查点不会迁移。

安装依赖后，使用 `sayacode` 或 `python -m sayacode` 启动；源码检出也使用这两个入口。

## 单次执行与交互使用

运行 `sayacode` 会打开基于 prompt-toolkit 和 Rich 的终端。交互界面展示当前模型、信任等级、会话、工具进度和后台任务通知。斜杠命令支持补全；`/help` 列出全部内置命令，`/help <command>` 显示某条命令的用法和示例。使用 `/new` 开始新对话，使用 `/models` 列出模型配置，使用 `/model add` 打开设置向导，使用 `/quit` 退出。助手文本在流式输出时按 Markdown 渲染；单次执行的 `text/json/jsonl` 输出保持纯文本，便于机器读取。

```bash
sayacode --workspace . -p "Summarize this repository"
sayacode -p "Check the current changes" --trust read_only --output-format json
sayacode -p "Explain this error" --output-format jsonl
echo "Explain this log" | sayacode -p - --output-format json
```

单次模式不会请求终端审批。需要审批的工具调用会暂停；退出码 `3` 表示有审批或后台任务需要处理，`1` 表示失败，`0` 表示完成。模型启动的后台任务会在单次进程保持打开期间等待完成，最终状态会包含在 JSON 或 JSONL 输出中。`json` 返回单个结果对象。`jsonl` 按编号写入带版本的 `run.started`、助手、工具和任务事件，最后以 `run.completed`、`run.paused` 或 `run.failed` 事件结束。私有思考过程和已知凭证字段会从公开输出中省略或脱敏。未恢复的模型和图执行失败返回非零退出码；可恢复的工具错误仍可能得到完整回答。

使用 `--workspace`、`--session`、`--new-session` 和 `--trust read_only|ask|full` 选择起始工作区、会话和信任等级。相对文件路径从工作区解析，绝对路径可以访问主机任意位置。`/trust` 切换当前会话，`/trust default <level>` 设置新会话默认值。在任务文字中直接要求规划或评审即可，两者不是全局模式。`/lang auto|zh|en` 和 `/style` 只改变展示偏好，不改变信任等级。

## 工具与信任

模型的精简工具目录覆盖文件读取与精确编辑、官方文件搜索、符号与工程分析、受限 Shell 执行、只读 Git 查询、已保存输出和网页搜索。Git 改动通过 Shell 完成。`/tools` 展示目录，`/tools <name>` 展示某个工具的参数。超过 64 KiB 的输出会保存在已配置的输出目录下，并返回预览和定位符。使用 `/settings set output_limit_bytes <bytes>` 修改该阈值。LangGraph 原生 ToolNode 支持单轮模型并发调用多个独立工具，不需要重复的批量分发器。

信任分为三级。`read_only` 自动放行已知只读工具和内置网页搜索，拒绝文件修改和未知 MCP 工具，但**每条 Shell 命令**之前都会询问；已批准的 Shell 命令仍可能修改主机。`ask`（默认值）自动放行读取，并在每次副作用调用前暂停，包括普通编辑、删除、Shell、构建器委派和未知 MCP 工具。`full` 跳过审批。在交互终端中，每个待定操作都要单独决策：`y` 表示批准一次，`s` 表示在 `ask` 信任下记住当前会话中的完全相同调用，`N` 表示拒绝。`/trust clear` 清除已记住的调用。LangChain 的人工介入中间件会对待定调用建立检查点，并按上述决策恢复执行。`/trace` 读取本地审计记录。

任何信任等级都不提供操作系统沙箱。文件工具接受工作区之外的绝对路径，Shell 命令以本地用户权限运行。对于 `.env`、私钥或凭证文件没有特殊排除，读取后内容可能送达已配置的模型。官方 glob 与 grep 搜索中间件从所选工作区开始搜索，而直接文件工具和 Shell 可以访问其他路径。模型失败会在配置次数内重试，之后按失败上报；工具自动重试仅限面向读取的工具，不包括编辑或 Shell 命令。

默认信任等级与其他设置一起存放在 `~/.sayacode/config.json`；每个会话的等级和精确调用授权保存在 LangGraph Store 中。旧模式和细粒度权限文件不再读取或迁移。`SAYACODE_HOME` 可更改用户状态目录。请把受信任工程、钩子和模型可触达工具视为具有用户权限的本地代码。

## 会话、计划与任务

LangGraph 检查点是会话的真实来源。`/session list`、`/session new`、`/session use <id>`、`/session rename <title>`、`/history` 和 `/status` 提供终端视图。`/compact` 总结较早消息；`/rewind` 列出检查点，`/rewind <index-or-id>` 把较早图状态分叉为下一轮起点。回退只改变对话状态，不会撤销文件、Shell 或 Git 效果。`/todos` 展示当前图线程中原生 `TodoListMiddleware` 列表。

`/team` 管理后台构建、规划和评审任务。每个任务拥有独立图线程，并在派发时继承父任务信任等级。Git 构建器在单独工作树中启动，工作树包含源码检出脏状态的快照。工作树用于组织交付物，**不是**安全边界，全局文件或 Shell 操作仍可改动工作树之外的路径。工作树改动的交付是显式的：

```text
/team spawn reviewer Inspect the authentication flow
/team spawn builder Fix the failing parser test
/team list
/team wait <task-id>
/team diff <task-id>
/team apply <task-id>
/team cleanup <task-id>
```

`/team stop`、`/team resume` 和 `/team followup <task-id> <message>` 同样可用。因审批暂停的任务可用 `/team pending <task-id>` 查看，并用 `/team approve <task-id>` 或 `/team reject <task-id>` 交互处理；终端会对每个待定操作请求决策。模型可以委派任务，并使用 `task_status`、`task_wait`（限时等待）和 `task_delivery` 查看任务。当子任务完成、失败、暂停或停止时，其父智能体会收到通知，并在当前父任务运行结束后，在同一 LangGraph 线程上开始新一轮运行。通知通过运行上下文提供任务编号和状态，不会伪造用户消息，也不会复制子任务记录。父任务可调用 `task_status` 获取结果。如果父任务运行请求审批，请在终端使用 `/approve [session-id]` 或 `/reject [session-id]`。通知能感知检查点，不会在不确定的强制中断后盲目重放。

非 Git 工作区中的构建器直接在共享工作区运行，不使用工作树。Git 工作树交付只包含该工作树内部改动，其他位置的编辑不会被 `/team diff` 或 `/team apply` 捕获。子任务运行期间禁止应用交付物。清理会拒绝删除未应用的改动、交付后新增的改动，以及工作树中仍存在的忽略文件。应用或清理前请先检查 `/team diff`。任务保留模型配置快照以便后续恢复；公开任务输出会遮蔽接口密钥。命令行异常退出后，未完成任务会标记为中断且效果未确认，需要显式 `/team resume`。任务状态通知出现在实时终端中；终端退出后没有独立后台服务。关闭等待期默认为 10 秒，可用 `/settings set shutdown_grace_seconds <seconds>` 修改。

## MCP、钩子与自定义命令

LangChain MCP 适配器加载已配置的服务端，并通过同一图和信任层暴露其工具。模型可见名称以 `mcp__` 开头；名为 `lookup` 的服务端工具显示为 `mcp__lookup`（含不支持字符的名称会被归一化）。未知 MCP 工具在 `read_only` 下拒绝，在 `ask` 下需要审批，在 `full` 下直接运行。项目 `.mcp.json` 文件需在该工作区运行 `/mcp trust` 后才会激活。例如，请把下面的脚本路径替换为受信任的 MCP 服务端实现：

```json
{
  "mcpServers": {
    "project-server": {
      "command": "python",
      "args": ["path/to/server.py"]
    }
  }
}
```

`/mcp status`、`/mcp reload`、`/mcp untrust`、`/mcp add <name> <command> [args...]` 和 `/mcp remove <name>` 用于管理服务端。信任作用于当前解析后的工作区路径。服务端提供的工具名经 `mcp__` 归一化后必须互不相同。

命令钩子支持 `SessionStart`、`UserPromptSubmit`、`PreToolUse`、`PostToolUse`、`ToolFailure` 和 `SessionEnd`。用户钩子位于 `~/.sayacode/hooks.json`；项目钩子位于 `<workspace>/.sayacode/hooks.json`，需要 `/hooks trust`。`/hooks status`、`/hooks untrust`、`/hooks reload` 和 `/hooks audit` 展示其状态。钩子从标准输入接收 JSON 事件，限时运行，配置为阻塞时可以阻止提示或工具执行。

Markdown 斜杠命令可放在 `<workspace>/.sayacode/commands/`、`<workspace>/.claude/commands/`、`~/.sayacode/commands/` 或 `~/.claude/commands/`。`/commands` 列出发现的命令。`commands/ops/review.md` 这类嵌套文件可用 `/ops:review` 调用；`$ARGUMENTS` 以及 `$1`、`$2` 等按调用参数展开。这些文件只向智能体提供提示，不绕过工具策略。

## 本地文件与诊断

`SAYACODE_HOME` 默认为 `~/.sayacode`。主要文件包括存产品偏好和配置的 `config.json`、存图历史的 `checkpoints.sqlite3`、存线程和任务元数据的 `store.sqlite3`、存审计事件的 `audit.jsonl`，以及 `outputs/` 和 `worktrees/`。会话消息不会再复制到第二个记录数据库。

```bash
sayacode --doctor
sayacode --doctor --json
sayacode --doctor --bundle support.json
```

`/doctor` 在交互终端中运行相同的本地检查。支持包只含诊断状态，不含接口密钥。

## 开发

```bash
python -m pip install uv==0.12.5
uv sync --locked --extra dev
uv run --no-sync python scripts/check_release.py
uv run --no-sync python -m pytest -q
uv run --no-sync python -m ruff check src tests scripts
uv run --no-sync python -m mypy
uv build
```

发布检查验证 2.0 包布局、精确直接锁定版本和通用 `uv.lock`，编译 `src/`、`tests/` 和 `scripts/`，并运行完整测试、Ruff、MyPy 和命令行启动检查。持续集成在 Windows 和 Ubuntu 上，对 Python 3.11 至 3.13 使用同一锁文件安装。打包任务构建 wheel 和源码分发包，在干净环境中用锁定依赖安装 wheel 并检查已安装命令行。`pyproject.toml` 是唯一的依赖声明；`uv.lock` 记录各平台解析结果和产物哈希。

新实现位于 `src/sayacode/`。终端是包在 LangChain 和 LangGraph 之外的产品适配层；图状态、工具调用和审批改动应通过公开的应用或命令行接口，配合真实检查点验证。

MIT 许可证。
