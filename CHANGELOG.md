# Changelog

## Unreleased

- Replaced vendor-name model routing with explicit protocol profiles for OpenAI Chat Completions, OpenAI Responses, Anthropic Messages, Gemini generateContent, and Ollama native chat. Profiles now require endpoint URL, model ID, context length, and maximum output tokens; the optional API key stays hidden in the terminal. Old model profiles are rejected rather than migrated.
- Added a text/tool/stream model capability probe and protocol-level HTTP contract tests. Optional LLM tool selection is disabled by default for endpoints without structured-output support.
- Keyless custom endpoints use a nonsecret SDK credential marker, preventing ambient provider API keys from being sent to a different base URL; explicit keys continue to work for authenticated endpoints.
- Refreshed the interactive CLI with a responsive startup view, command tables, Markdown answer rendering, tool completion feedback, per-action approval cards, and prompt-safe background notifications. Headless formats remain unchanged.
- Made `/help` complete and searchable with `/help <command>`; added `/new` for a fresh conversation, `/models` for profile listing, and an interactive `/model add` setup wizard.
- Replaced duplicate requirements lists with one cross-platform `uv.lock`; CI now installs the locked graph on Windows and Ubuntu for Python 3.11–3.13.
- Clarified documented 2.0 behavior for namespaced MCP tools, per-action approval, task recovery, protected search results, and guarded worktree delivery.

## 2.0.0

- Rebuilt the coding agent around LangChain `create_agent` and LangGraph checkpoints, stream events, and human-in-the-loop interrupts.
- Added an async terminal with text, JSON, and numbered JSONL one-shot output.
- Added workspace file, search, symbol, shell, Git, web, and project tools behind mode, path, and permission checks.
- Added LangChain middleware for context editing, summarization, retries, limits, todo lists, and optional tool selection. Unrecovered model failures surface as failed runs; automatic tool retries apply to read-oriented tools.
- Added model profiles, persistent sessions, graph checkpoint rewind, audit events, and local diagnostics.
- Added trusted project MCP servers with `mcp__` tool names, command hooks, Markdown slash commands, and background builder/planner/reviewer tasks.
- Added per-action approval decisions, bounded model-visible task waiting, explicit approval of paused child tasks, and profile snapshots for child recovery. Non-Git builders run read-only. Worktree application rejects active tasks, and cleanup preserves unapplied or changed work.
- Moved the installable package to `src/sayacode/` and replaced the quality gate with 2.0-only tests and source checks.
