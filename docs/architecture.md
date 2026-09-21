# SAYACODE 代码结构

SAYACODE 是 LangChain `create_agent` 与 LangGraph 的终端产品适配。智能体循环、工具并行调用、对话消息、待办、中断和检查点由官方组件负责；本项目只实现终端交互、工具的系统适配、信任策略及后台任务交付。

## 依赖方向

```text
cli → application → agent / approvals / tasks / tools / extensions
                    ↓
             config / trust / paths / process
```

- `cli/` 解析输入、展示 Rich 内容、序列化 JSONL；不持有第二份会话状态。
- `application.py` 组装资源并协调一次运行。会话、模型配置、诊断、MCP 与后台任务的具体操作分别位于其领域模块。
- `agent/` 只构造官方模型和编译图，并调用 LangGraph 的流、检查点、恢复、摘要与回退接口。
- `approvals/` 保存静态策略、TypeSafe SDK 和审批中间件。Jev 通过异步 `after_model` 写入类型化判定，官方 HITL 只中断仍需人工处理的调用。
- `tools/` 暴露原生 LangChain 工具。`catalog.py` 只列出真实工具，不分发调用；同轮多工具调用由 ToolNode 执行。
- `tasks/` 保存 continuable 子 Agent 档案、进程内句柄、持久 Inbox、中间件和 Git worktree 交付。父子消息只在官方模型调用边界进入图状态。
- `extensions/` 管理 MCP、Hook、Markdown 命令和项目记忆。Hook 与 Shell 共用根目录的进程树清理函数，不导入整个工具目录。
- `config.py` 只解析配置与信任档位名称，不为解析配置加载 LangChain 中间件。

下层模块不在运行时导入 `application.py`；仅在类型检查时引用应用对象。领域模块之间不通过通用 `Runner`、`ToolExecutor` 或事件总线重新实现框架职责。

## 对外入口与状态

命令行入口为 `sayacode.cli.main:main`，`python -m sayacode` 使用同一入口。会话消息、待办和中断只存 LangGraph checkpoint；Store 只保存会话目录、任务关系、信任档位及交付元数据。`cli/events.py` 的 JSONL 是公开事件投影，不作为另一份历史。

本地四档信任为 `read_only`、`ask`、`jev`、`full`。只读档不注册 Shell；Jev 档与询问档使用相同工具范围，审理不能扩大静态策略权限。文件绝对路径和非只读档的 Shell 可以访问初始工作区外；不存在操作系统沙箱。builder 的 worktree 仅组织树内差异，不能保证树外操作被捕获。

计划由官方 `TodoListMiddleware` 的 `todos` 图状态和 `write_todos` 工具维护，不另建计划表。continuable 子 Agent 使用独立 `thread_id` 和检查点；派发时只复制当前目标和 Todo 快照。父子消息进入 Store Inbox，`TaskInboxMiddleware` 在接收线程下一次模型调用前把消息写入原生状态。子线程不能直接修改父 Todo；父 Agent 根据消息证据自行调用 `write_todos`。

## 测试与分发

`tests/` 按 `agent`、`cli`、`tools`、`tasks`、`extensions`、`integration` 分组。测试优先验证用户行为与真实图链路，不锁定私有类名或文件数量。根目录的 `pyproject.toml` 与 `uv.lock` 是唯一依赖与分发清单；发布门禁递归发现测试，构建 wheel 后在干净环境验证入口。
