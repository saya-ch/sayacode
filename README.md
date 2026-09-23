<div align="center">
  <img src="https://raw.githubusercontent.com/saya-ch/sayacode/main/assets/image1.png" alt="SAYACODE 项目主视觉" width="100%">

  <h1>SAYACODE 2.2</h1>
  <p>在终端中阅读代码、实施修改并验证结果的编程 Agent。</p>

  <p>
    <a href="https://github.com/saya-ch/sayacode/actions/workflows/ci.yml"><img src="https://github.com/saya-ch/sayacode/actions/workflows/ci.yml/badge.svg" alt="CI 状态"></a>
    <img src="https://img.shields.io/badge/Python-3.11--3.13-3776AB" alt="Python 3.11 至 3.13">
    <a href="https://github.com/saya-ch/sayacode/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-MIT-111111" alt="MIT 许可证"></a>
  </p>
</div>

SAYACODE 使用 LangChain `create_agent` 执行工具调用，由 LangGraph 保存会话、待办、审批中断和检查点。它提供本地文件、Shell、只读 Git 查询、MCP 工具，以及可继续的后台子 Agent。终端会显示每个 Agent 的工具活动、审批请求和任务结果。

本文描述 2.2 系列与 `main` 分支的当前行为。2.x 是破坏性重写，不读取或迁移 1.4.0 的配置与会话；旧版保存在 [`legacy/1.4.0`](https://github.com/saya-ch/sayacode/tree/legacy/1.4.0)。

## 快速开始

需要 Windows 或 Linux，以及 Python 3.11、3.12 或 3.13。安装已发布版本：

```bash
python -m pip install --upgrade sayacode
sayacode --workspace .
```

从源码运行最新的 `main`：

```bash
git clone https://github.com/saya-ch/sayacode.git
cd sayacode
python -m pip install uv==0.12.5
uv sync --locked
uv run sayacode --workspace .
```

源码使用单一 `uv.lock`；已有 `uv` 时可跳过安装步骤。首次启动会引导配置模型；之后也可以输入 `/model add` 重新打开向导。直接运行 `sayacode` 时，当前目录就是默认工作区。

```powershell
sayacode --workspace "C:\develop\my-project"
```

交互终端可以直接输入任务。输入 `/` 会显示命令候选；继续输入可匹配命令和常用子命令，使用上下方向键选择，Enter 填入候选，再补参数或再次按 Enter 执行。底部状态栏显示当前模型、信任档位、会话、后台任务数和主 Agent 待办进度。

```text
❯ /model add
  选择接口协议，填写地址、密钥、模型 ID 和 token 预算

❯ 请检查这个项目的入口和测试，修复发现的问题并验证
SAYA  读取项目文件...
SAYA  更新待办...
SAYA  运行测试...
```

上面的对话是操作示意；实际工具、审批和子 Agent 活动由任务及信任档位决定。主 Agent 在终端中标为 `SAYA`，子 Agent 使用各自的标题、任务 ID 和线程颜色。

### 单次执行

已配置模型后，可在脚本或 CI 中运行一个任务：

```bash
sayacode -p "分析当前改动" --trust read_only
sayacode -p "审查当前改动" --output-format json
sayacode -p "检查项目" --output-format jsonl
```

`-p -` 从标准输入读取任务。输出格式支持 `text`、`json`、`jsonl`。无交互运行遇到需要人工审批的操作时会保存暂停状态并以退出码 `3` 结束；成功为 `0`，运行失败为 `1`，参数或配置错误为 `2`，用户中断为 `130`。`--skill` 必须与 `-p` 同用。

## 模型配置

SAYACODE 不预设供应商。每个模型配置都由用户明确填写协议、接口地址、API Key、模型 ID、上下文长度和最大输出 token 数。向导中的协议使用方向键选择。

| 接口协议 | 配置值 |
|---|---|
| OpenAI Chat Completions | `openai_chat_completions` |
| OpenAI Responses API | `openai_responses` |
| Anthropic Messages | `anthropic_messages` |
| Gemini Native generateContent | `gemini_generate_content` |
| Ollama Native Chat | `ollama_native_chat` |

协议决定使用哪个 LangChain 模型适配器；地址和模型 ID 不会被用来猜测供应商。API Key 在向导中隐藏输入，并保存在本地配置；SAYACODE 不从环境变量读取模型密钥。仅当端点无需认证时输入 `none`。

```text
/models                  查看已保存模型
/model add               添加模型
/model use <名称>        切换当前模型
/model key <名称>        在隐藏输入框更新密钥
/model test [名称]       检查文本、工具调用和流式输出
```

本地状态默认保存在 `~/.sayacode/`，可通过 `SAYACODE_HOME` 指定其他目录。需要检查配置与诊断时可使用 `/paths`、`/status` 和 `/doctor`。

## Skill：按需加载工作方法

Skill 使用标准 `SKILL.md`，用于保存可复用的任务流程和参考资料。SAYACODE 先让 Agent 看到名称与描述；只有显式启用或 Agent 调用 `load_skill` 时，正文才进入当前线程的 LangGraph 状态。Skill 不授予额外工具权限；其脚本仍需通过现有 Shell 工具和审批。

```text
<工作区>/.agents/skills/code-review/SKILL.md
~/.sayacode/skills/code-review/SKILL.md
```

设置 `SAYACODE_HOME` 后，用户级 Skill 位于该目录的 `skills/` 下。同名 Skill 以项目版本为准。一个最小的文件示例：

```markdown
---
name: code-review
description: 检查代码变更，报告能定位和验证的问题
---

# 代码审查

先读取相关文件和差异，再核对实际调用路径与测试结果。
报告问题位置、影响和验证方式。
```

```text
/skills                     列出当前工作区可用 Skill
/skill show code-review     查看正文
/skill use code-review      启用到当前会话
```

Agent 也能调用 `list_skills` 搜索目录、`load_skill` 激活所需 Skill，再用 `read_skill_resource` 按需读取同一 Skill 目录里的文本参考文件。正文有大小和上下文预算限制，较长材料宜放在引用文件中。创建上面的示例文件后，无交互运行可使用 `sayacode --skill code-review -p "审查当前改动"`。格式参考 [Agent Skills 规范](https://github.com/agentskills/agentskills/blob/main/docs/specification.mdx)。

## 跨会话记忆

长期记忆保存在本地 LangGraph Store，供同一用户的新会话读取；会话原文仍留在各自的 LangGraph 检查点中。记忆分为用户偏好和当前项目的事实或经验。Git worktree 与主工作树使用同一项目身份，但项目事实使用前会对照实际工作树；无法确认时显示为待核验。

记忆默认关闭。输入 `/memory` 打开方向键管理菜单；`/memory enable` 开启后，Agent 可在空闲时从有依据的对话中整理记忆。整理会调用已配置的模型，产生额外请求和费用；普通任务的成功不依赖整理是否成功。也可以关闭自动学习，仅手动保存：

```text
/memory                  浏览记忆与设置
/memory enable           开启记忆使用与自动学习
/memory recent           查看本轮已提供给模型的记忆及命中原因
/memory list             列出用户和项目记忆
/memory search uv        查找相关记录
/memory remember user 以后代码注释用中文
/memory remember project 这个项目使用 uv 管理依赖
/memory correct <ID> 以后代码注释用英文
/memory confirm <ID>     确认一条候选记忆
/memory pin <ID>         固定记忆
/memory unpin <ID>       取消固定
/memory forget <ID>      遗忘一条长期记忆
/memory use off          修改全局默认：不向主 Agent 注入旧记忆
/memory learn explicit   修改全局默认：仅手动记住
/memory session          查看当前会话的实际设置
/memory session use off  仅当前会话暂停向主 Agent 注入
/memory session learn auto   仅当前会话自动学习
/memory session use default  当前会话恢复继承全局读取设置
/memory model default    记忆整理复用当前主模型
/memory model <画像名>   记忆整理改用已保存的模型画像
/memory timeout 20       无交互整理最多额外等待 20 秒
```

`/memory` 菜单分别提供“本轮参考”、全局默认和当前会话设置。“本轮参考”表示记录已提供给模型，并显示命中原因；它不证明模型实际采用了该内容。`/memory show <ID>` 可查看来源和有界的依据摘要。会话级 `use`、`learn` 覆盖只影响当前会话；输入 `default` 可恢复继承全局，新会话使用全局默认。`use off` 只暂停把旧记忆提供给主 Agent；若 `learn auto` 仍开启，后台整理会读取相关旧记忆以避免重复与冲突。要停止额外模型读取和请求，请设置 `learn off` 或 `/memory disable`。每条记录显示范围、状态、来源和最后确认时间。固定会保留偏好，但项目事实仍需按当前工作树核验。用户当前的明确要求优先于旧记忆；临时要求不自动改成永久偏好。`forget` 会阻止该长期记忆再次注入，也会拦截尚未完成的旧整理工作；原始聊天和外部备份需要单独管理。

无交互任务结束时，CLI 会让本轮记忆整理到达完成、失败或延后状态后退出。`jsonl` 在最终 `run.completed` 或 `run.failed` 之前输出简短的 `memory.updated`、`memory.failed` 或 `memory.deferred` 事件；事件不包含记忆正文。整理失败不改变已完成主任务的退出码。

`text` 和 `json` 格式的主结果可能等待记忆整理宽限期，默认 30 秒，可用 `/memory timeout <秒>` 或配置中的 `memory.headless_timeout_seconds` 调整。超时后来源继续保留，状态标为 `memory.deferred`，供之后恢复。记忆整理默认复用主模型；`/memory model` 只从已保存的模型画像选择，不单独填写供应商或读取环境变量密钥。

人工维护的说明文件与自动记忆各有用途：用户说明放在 `SAYACODE_HOME/instructions.md`，项目约定放在 `SAYACODE.md` 或 `CLAUDE.md`。旧 `/memory init`、`/memory append` 已移除，旧 `memory.md` 与 `.sayacode/memory.md` 不会自动载入或迁移。从 2.1 升级且需要保留旧说明时，请人工检查内容，再把用户级说明复制到 `SAYACODE_HOME/instructions.md`，项目约定复制到项目的 `SAYACODE.md`；旧聊天记录不会被复制为长期记忆。完整的数据边界和失效规则见[记忆系统设计](https://github.com/saya-ch/sayacode/blob/v2.2.0/docs/design/memory-system.md)。

## 权限与审批

信任档位属于当前会话；新会话使用用户默认档位。切换档位使用 `/trust <档位>`，设置新会话默认值使用 `/trust default <档位>`。

| 档位 | 工具行为 |
|---|---|
| `read_only` | 提供读取、搜索、分析等工具；不提供 Shell、文件写入和 MCP 工具 |
| `ask` | 只读调用直接执行；有副作用的调用逐项请求批准 |
| `jev` | Jev 自动审理原本需批准的调用；不确定或服务异常时转人工 |
| `full` | 不弹出工具审批 |

```text
/trust ask
/reviewer setup
/reviewer test
/trust jev
```

`ask` 档可以在本会话记住**完全相同**的调用。审批卡片会显示工具、目标和脱敏参数；拒绝与批准走 LangGraph 原生中断恢复。SAYACODE 没有操作系统沙箱：文件工具可以接受工作区外的绝对路径，Shell 以当前用户权限执行。Git 变更也通过 Shell 进行；内建 Git 工具只做查询。

## 可继续的子 Agent

主 Agent 可以派发 `builder`、`planner` 和 `reviewer`。每个子 Agent 有独立线程、待办与检查点；主 Agent 在子 Agent 运行时可以继续工作。工具活动会在同一终端按线程标题和颜色区分。子 Agent 完成一轮后进入可继续的 `idle` 状态，结果进入父 Agent 的持久 Inbox；空闲的父 Agent 可自动继续处理结果。

```text
/team spawn planner 梳理认证流程
/team spawn builder 修复解析器并运行测试
/team list
/team status <任务 ID>
/team followup <任务 ID> 请再检查边界条件
```

Git 工作区中的 builder 默认创建独立 worktree，并带入派发时的已提交、未提交和未跟踪文件。差异需显式应用；如果希望 builder 直接共享父工作区，可选 `--shared`。非 Git 工作区中的 builder 使用共享工作区。

```text
/team spawn builder --shared 直接修改当前工作区
/team diff <任务 ID>       查看 worktree 交付
/team apply <任务 ID>      显式应用交付
/team cleanup <任务 ID>    清理已结束任务的 worktree
```

共享工作区没有独立差异和 `apply` 阶段。worktree 用来组织交付，不是安全沙箱。子 Agent 的待批准操作可以通过 `/team pending <任务 ID>` 查看，再用 `/team approve <任务 ID>` 或 `/team reject <任务 ID>` 处理。

## 会话、计划与上下文

| 命令 | 用途 |
|---|---|
| `/new` | 新建并切换会话 |
| `/session list`、`/session use <ID>` | 浏览和切换会话 |
| `/todos` | 查看当前线程的原生待办 |
| `/history` | 查看当前会话消息 |
| `/compact [关注点]` | 手动摘要旧消息 |
| `/rewind` | 查看检查点并从所选位置继续 |

复杂任务由 LangChain `TodoListMiddleware` 管理待办，Agent 可根据验证结果或子任务发现调整计划。消息、待办、摘要和审批中断保存在 LangGraph checkpoint；Store 保存会话目录、任务关系、Inbox 与交付元数据。

自动摘要默认按模型上下文与最大输出预算动态计算，触发目标不超过上下文窗口的约 80%；上下文编辑默认关闭。`/rewind` 只改变对话状态，不撤销文件或 Git 操作。

## MCP、Hook 与项目约定

SAYACODE 通过 LangChain `MCPAdapter` 发现 **MCP 工具**。用户级服务器在本地配置中管理；项目 `.mcp.json` 需要先显式信任工作区。MCP 工具仍经过当前会话的权限策略。

```text
/mcp status
/mcp trust
/mcp reload
/mcp untrust
```

Hook 支持 `SessionStart`、`UserPromptSubmit`、`PreToolUse`、`PostToolUse`、`ToolFailure`、`SessionEnd` 六类事件。`SAYACODE.md`、`CLAUDE.md` 提供人工项目约定，`/memory` 管理学习记忆；它们与 Skill 都不会改变工具权限。

## 架构与开发

```text
src/sayacode/
├── agent/          create_agent、模型、运行时与事件
├── approvals/      信任策略、Jev 审理与人工中断
├── tasks/          子 Agent 生命周期、Inbox 与 worktree
├── tools/          文件、Shell、Git、搜索与代码分析
├── extensions/     Skill、MCP、Hook 与人工说明
├── memory/         长期记忆、检索、学习与 Store 提交
├── cli/            交互终端、斜杠命令与单次输出
└── application.py  组装模型、工具、中间件和存储
```

SAYACODE 的产品代码负责工具和终端适配；Agent 循环、工具调度、状态持久化、摘要和审批中断由 LangChain / LangGraph 组件承担。架构细节见 [docs/architecture.md](https://github.com/saya-ch/sayacode/blob/main/docs/architecture.md)。

```bash
uv sync --locked --extra dev
uv run --no-sync python scripts/check_release.py
uv build
```

发布门禁运行 Pytest、Ruff、MyPy、锁文件检查、wheel 构建与安装后 CLI 冒烟。GitHub Actions 覆盖 Windows、Ubuntu 和 Python 3.11 至 3.13。问题与改进建议可提交到 [Issues](https://github.com/saya-ch/sayacode/issues)。

## 版本与许可

| 版本线 | 位置 | 说明 |
|---|---|---|
| 2.x | [`main`](https://github.com/saya-ch/sayacode/tree/main) | 当前架构与开发主线 |
| 1.4.0 | [`legacy/1.4.0`](https://github.com/saya-ch/sayacode/tree/legacy/1.4.0) | 历史版本 |

变更记录见 [CHANGELOG.md](https://github.com/saya-ch/sayacode/blob/main/CHANGELOG.md)。项目使用 [MIT 许可证](https://github.com/saya-ch/sayacode/blob/main/LICENSE)。
