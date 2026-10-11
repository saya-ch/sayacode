# SAYACODE 贡献指南

使用 Python 3.11 至 3.13。支持 Windows 和 Ubuntu。先用 `python -m pip install uv==0.12.5`，再用 `uv sync --locked --extra dev` 安装开发依赖。

## 架构

目录边界与依赖方向见 [代码结构说明](docs/architecture.md)。

智能体图保留在 LangChain `create_agent` 中，对话保留在 LangGraph 检查点中。模型行为、工具调用、总结、待办状态和人工审批使用 LangChain 中间件。`src/sayacode/` 下的应用包提供面向 WebHost 的文件与 Shell 工具、信任决策、模型与配置管理、Web 投影和轻量任务元数据。不要新增平行记录、第二个智能体循环或第二个工具调度器。

应用边界是异步的。新的模型或工具集成应走 `ainvoke` 或 `astream_events`，并保留取消能力。把每次运行的工作区、会话、策略和输出依赖放在图运行时上下文中，避免可变模块全局变量。

## 安全与行为

- `read_only` 信任不向模型提供文件写入、Shell 和未知 MCP 工具；它仍可更新待办、检查点和任务消息，但不是操作系统沙箱。
- `workspace_auto` 信任只对内置文件写入工具自动放行工作区内路径；Shell 和其他副作用调用仍逐次审批。
- 绝对文件路径可以访问起始工作区之外的主机。没有操作系统沙箱，也没有敏感路径例外。
- `ask` 信任会在每次副作用调用前暂停；只有完全相同的调用可被记住，且仅对当前会话有效。`full` 信任跳过审批。
- `ask` 决策会暂停图。获批调用最多运行一次，拒绝或缺失审批时绝不运行。
- 检查点回退只允许已完成轮次，只改变图状态。不要把它说成文件系统或 Git 效果的撤销；工具中间态、审批态和产品状态写回点必须拒绝回退。
- 项目 MCP 服务端和项目命令钩子需要对解析后的工作区显式信任。
- 审计和公开 JSONL 输出必须省略隐藏思考过程和凭证。
- 构建器工作树用于组织 Git 交付物，不提供隔离。工作树之外的全局编辑不会记入交付差异。

## 测试与检查

```bash
uv run --no-sync python scripts/check_release.py
uv run --no-sync python -m pytest -q
uv run --no-sync python -m ruff check src tests scripts
uv run --no-sync python -m mypy
```

为可观察行为写测试：真实 LangGraph 检查点、工具副作用、审批暂停与恢复、流式输出、命令行退出码、策略拒绝和任务交付。尽量使用伪造模型和临时工作区。改动流式传输或 Shell 执行时，验证异步取消和进程清理。

仓库必须包含 `src/sayacode/` 实现和按领域分组的 `tests/**/test_*.py`。发布检查验证该布局。Web 行为变化还要同步构建 `src/sayacode/web/static/`；公开命令或行为变化时，请同步更新 README 和 CHANGELOG。
