# Contributing to SAYACODE 2.0

Use Python 3.11–3.13. Windows and Ubuntu are supported. Install development dependencies with `python -m pip install uv==0.12.5` followed by `uv sync --locked --extra dev`.

## Architecture

Keep the agent graph in LangChain `create_agent` and the conversation in LangGraph checkpoints. Use LangChain middleware for model behavior, tool calls, summarization, todo state, and human approval. The application package under `src/sayacode/` supplies workspace tools, policy, model/profile configuration, terminal presentation, and thin task metadata. Do not add a parallel transcript, a second agent loop, or a second tool scheduler.

The app boundary is asynchronous. New model or tool integrations should work through `ainvoke`/`astream_events` and preserve cancellation. Keep per-run workspace, session, policy, and output dependencies in the graph runtime context; avoid mutable module globals.

## Safety and behavior

- A read-only mode must deny writes at the tool boundary even if the model asks for them.
- Workspace path checks and protected file checks apply before a tool runs.
- An `ask` decision pauses the graph. The proposed call must run at most once after approval and never after rejection or missing approval.
- A checkpoint rewind changes graph state only. Do not present it as an undo for filesystem or Git effects.
- Project MCP servers and project command hooks require explicit trust for the resolved workspace.
- Audit and public JSONL output must omit hidden reasoning and credentials.
- Builder task delivery must be reviewed and explicitly applied from its worktree.

## Tests and checks

```bash
uv run --no-sync python scripts/check_release.py
uv run --no-sync python -m pytest -q
uv run --no-sync python -m ruff check src tests scripts
uv run --no-sync python -m mypy
```

Write tests for observable behavior: real LangGraph checkpoints, tool side effects, approval pause/resume, stream output, CLI exit codes, policy denial, and task delivery. Use fake models and temporary workspaces where possible. Verify async cancellation and process cleanup when changing streaming or shell execution.

The repository must contain the `src/sayacode/` implementation and 2.0 tests (`tests/test_v2_*.py`). The release check validates this layout. Update README and CHANGELOG when a public command or behavior changes.
