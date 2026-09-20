# Changelog

## Unreleased

- Refreshed the interactive CLI with a responsive startup view, command tables, Markdown answer rendering, tool completion feedback, per-action approval cards, and prompt-safe background notifications. Headless formats remain unchanged.
- Made `/help` complete and searchable with `/help <command>`; the overview now prominently shows `/session new` and `/reset` for starting a new conversation.
- Replaced duplicate requirements lists with one cross-platform `uv.lock`; CI now installs the locked graph on Windows and Ubuntu for Python 3.11–3.13.
- Clarified documented 2.0 behavior for namespaced MCP tools, per-action approval, task recovery, protected search results, and guarded worktree delivery.

## 2.0.0

- Rebuilt the coding agent around LangChain `create_agent` and LangGraph checkpoints, stream events, and human-in-the-loop interrupts.
- Added an async terminal with text, JSON, and numbered JSONL one-shot output.
- Added workspace file, search, symbol, shell, Git, web, and project tools behind mode, path, and permission checks.
- Added LangChain middleware for context editing, summarization, retries, limits, todo lists, and tool selection (enabled by default with a 12-tool limit). Unrecovered model failures surface as failed runs; automatic tool retries apply to read-oriented tools.
- Added model profiles, persistent sessions, graph checkpoint rewind, audit events, and local diagnostics.
- Added trusted project MCP servers with `mcp__` tool names, command hooks, Markdown slash commands, and background builder/planner/reviewer tasks.
- Added per-action approval decisions, bounded model-visible task waiting, explicit approval of paused child tasks, and profile snapshots for child recovery. Non-Git builders run read-only. Worktree application rejects active tasks, and cleanup preserves unapplied or changed work.
- Moved the installable package to `src/sayacode/` and replaced the quality gate with 2.0-only tests and source checks.
