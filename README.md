# SAYACODE 2.0

SAYACODE is a terminal coding assistant for a local workspace. It uses LangChain's `create_agent` for the agent and LangGraph for conversation state, checkpoints, interrupts, and task progress. The terminal adds workspace tools, permission decisions, model profiles, and a small JSONL event protocol.

## Install and start

Python 3.11–3.13 is required. CI targets Windows and Ubuntu for all three versions.

```bash
python -m pip install .
sayacode --version
sayacode --help
```

Start the interactive terminal and use `/model add` to configure an API protocol and endpoint:

```bash
sayacode --workspace /path/to/repository
```

In PowerShell:

```powershell
sayacode --workspace C:\path\to\repository
```

The setup wizard asks for six fields: **API protocol**, **base URL**, **API key** (optional), **model ID**, **context length**, and **maximum output tokens**. Choose the protocol your endpoint actually implements: `openai_chat_completions`, `openai_responses`, `anthropic_messages`, `gemini_generate_content`, or `ollama_native_chat`. For example, an OpenAI Chat Completions endpoint may use `https://api.openai.com/v1` as its base URL. The protocol determines the LangChain integration and request format; changing the URL alone does not convert one protocol into another. SAYACODE does not infer a protocol from a company or model name.

`/model add` stores a profile in `~/.sayacode/config.json` and generates its local name from the model ID. The key input is hidden and excluded from terminal input history; a key supplied there is saved in the local configuration file. Use `/models` to see configured models and protocols, `/model use <name>` to switch, and `/model test [name]` to check text, tool-call, and streaming capabilities. `--profile <name>` selects a saved profile at launch. A minimal configuration file looks like this:

```json
{
  "default_profile": "main",
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

For a one-run override, supply `--protocol`, `--base-url`, `--model-id`, `--context-length`, and `--max-output-tokens` together; `--api-key` is optional. Both token counts accept values such as `128000`, `256k`, and `1M`. Shell command arguments can be visible to other local processes, so prefer a saved profile for credentials. An empty key is treated as keyless access: the official SDK receives a harmless placeholder rather than reading a possibly sensitive provider key from the environment, including for Ollama's optional bearer authentication. Supply the real key explicitly for hosted endpoints. The Agent passes tool schemas through the selected LangChain adapter. Optional official LLM tool selection can be enabled by setting `tool_selector_max_tools` to a positive number in a profile when the endpoint supports structured output; this adds another model call. The configured context length informs automatic summary timing (and configures `num_ctx` for Ollama). The setup supports the five listed wire protocols and explicit API-key authentication; an arbitrary new protocol or custom authentication scheme requires another official adapter or additional configuration.

The profile format is intentionally new. Configurations with former `provider`, `model`, or `config_fields` keys are rejected; replace those entries with explicit protocol profiles.

`python run.py` and `sayacode.bat` launch the source checkout after dependencies are installed. `python -m sayacode` is also available.

## One-shot and interactive use

Running `sayacode` opens a prompt-toolkit/Rich terminal. The interactive view shows the active model, mode, session, tool progress, and background task notifications. Slash commands have completion; `/help` lists every built-in command and `/help <command>` shows usage with examples. Use `/new` to start a fresh conversation, `/models` to list model profiles, `/model add` to open the setup wizard, and `/quit` to exit. Assistant text is rendered as Markdown while it streams; one-shot `text/json/jsonl` output remains plain and machine readable.

```bash
sayacode --workspace . -p "Summarize this repository"
sayacode -p "Check the current changes" --mode review --output-format json
sayacode -p "Explain this error" --output-format jsonl
echo "Explain this log" | sayacode -p - --output-format json
```

One-shot mode never asks for terminal approval. A tool call that needs approval pauses; exit code `3` means approval or a background task needs attention, `1` means failure, and `0` means completed. A model-started background task is awaited while the one-shot process remains open, and its final status is included in JSON/JSONL output. `json` returns one result object. `jsonl` writes numbered, versioned `run.started`, assistant/tool/task events, and a terminal `run.completed`, `run.paused`, or `run.failed` event. Private reasoning and known credential fields are omitted or redacted from that public stream. Unrecovered model and graph failures return a nonzero exit code; a recoverable tool error may still lead to a completed answer.

Select a workspace, session, and mode with `--workspace`, `--session`, `--new-session`, and `--mode build|plan|review`. `build` can edit files under its permission policy. `plan` and `review` deny mutations at the tool boundary. `/mode` changes the active terminal mode. `/lang auto|zh|en` and `/style` change presentation preferences; neither changes tool permissions.

## Tools and permissions

The built-in tools cover workspace file reads and edits, search, symbols/project analysis, bounded shell execution, Git operations, system information, saved output, and web search. `/tools` shows a compact catalog, and `/tools <name>` shows one tool's parameters. Output over 64 KiB is saved under the configured output directory and returned as a preview and locator. `/settings set output_limit_bytes <bytes>` changes that threshold.

Permission rules have session, project, and user scopes. Matching explicit denials win across scopes; other matches use session, project, then user precedence. Mode and workspace path restrictions always apply. By default, workspace reads and ordinary file edits are allowed; shell execution, deletion, web search, and external Git actions ask for approval. Use `/permissions` to inspect rules and `/permissions <allow|ask|deny> <session|project|user> <tool> [path=<glob>] [command=<glob>]` to change one; `/permissions set <scope> <tool> <action>` is also supported. In the interactive terminal, each pending action gets its own decision: `y` approves once, `s` saves permission for that exact call in the current session, `p` saves it for that exact call in user settings, and `N` rejects it. LangChain's human-in-the-loop middleware checkpoints the pending calls and resumes with those decisions. `/trace` reads local audit entries.

Structured file tools reject paths outside the workspace and recognized sensitive files such as `.env`, private keys, and credential files. The glob/grep search layer filters protected paths and matches from the result shown to the model. This filtering is a result boundary, not an operating-system sandbox: a shell command approved by the user runs with the local user's privileges and may access paths outside the workspace. Model failures are retried within the configured limit and then reported as failures; automatic tool retry is restricted to read-oriented tools rather than edits or shell commands.

Project policy lives at `<workspace>/.sayacode/policy.json`. User policy is stored with other settings in `~/.sayacode/config.json`. `SAYACODE_HOME` changes the user state directory. Treat a trusted project, its hooks, and its model-accessible tools as local code with your user privileges.

## Sessions, plans, and tasks

LangGraph checkpoints are the conversation source of truth. `/session list`, `/session new`, `/session use <id>`, `/session rename <title>`, `/history`, and `/status` provide the terminal view. `/compact` summarizes older messages; `/rewind` lists checkpoints, and `/rewind <index-or-id>` forks an earlier graph state for the next turn. Rewind changes conversation state; it does not undo file, shell, or Git effects. `/plan` displays the native `TodoListMiddleware` list held in the current graph thread.

`/team` manages background builder, planner, and reviewer tasks. Each task has its own graph thread and status record. Builders work in isolated Git worktrees; the source checkout's dirty state is preserved. Delivery is explicit:

```text
/team spawn reviewer Inspect the authentication flow
/team spawn builder Fix the failing parser test
/team list
/team wait <task-id>
/team diff <task-id>
/team apply <task-id>
/team cleanup <task-id>
```

`/team stop`, `/team resume`, and `/team followup <task-id> <message>` are also available. A task paused for approval can be inspected with `/team pending <task-id>` and handled interactively with `/team approve <task-id>` or `/team reject <task-id>`; the terminal asks for a decision on each pending action. The model can delegate a task and use `task_status`, `task_wait` (bounded wait), and `task_delivery` to inspect it. Child results are available through those tools and terminal notifications; they are not automatically inserted into the parent's conversation.

A builder in a workspace without a committed Git checkout runs read-only in `plan` mode instead of receiving a worktree. Writable builders receive a snapshot of the source checkout, including its current tracked and untracked changes; their edits stay in the worktree until `/team apply`. Applying delivery while the child is active is rejected. Cleanup refuses to remove unapplied changes, changes made after a delivery, or ignored files still in the worktree. Inspect `/team diff` before applying or cleaning up. The task retains its model profile snapshot for later recovery; public task output masks its API key. After an abnormal CLI exit, unfinished tasks are marked interrupted with unconfirmed effects and require an explicit `/team resume`. Task status notifications appear in the live terminal; there is no separate background service after the terminal exits. The shutdown drain period defaults to 10 seconds and can be changed with `/settings set shutdown_grace_seconds <seconds>`.

## MCP, hooks, and custom commands

The LangChain MCP adapter loads configured servers and exposes their tools through the same graph and permission layer. Their model-visible names start with `mcp__`; a server tool called `lookup` appears as `mcp__lookup` (names containing unsupported characters are normalized). External MCP calls require approval by default. A project `.mcp.json` file is not activated until `/mcp trust` is run in that workspace. For example, replace the script path below with a trusted MCP server implementation:

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

`/mcp status`, `/mcp reload`, `/mcp untrust`, `/mcp add <name> <command> [args...]`, and `/mcp remove <name>` manage servers. Trust applies to the current resolved workspace path. Server-provided tool names must remain distinct after `mcp__` normalization.

Command hooks support `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `ToolFailure`, and `SessionEnd`. User hooks live at `~/.sayacode/hooks.json`; project hooks at `<workspace>/.sayacode/hooks.json` require `/hooks trust`. `/hooks status`, `/hooks untrust`, `/hooks reload`, and `/hooks audit` show their state. Hooks receive a JSON event on stdin, run with a timeout, and can block prompt or tool execution when configured as blocking.

Markdown slash commands can be placed in `<workspace>/.sayacode/commands/`, `<workspace>/.claude/commands/`, `~/.sayacode/commands/`, or `~/.claude/commands/`. `/commands` lists discovered commands. A nested file such as `commands/ops/review.md` can be invoked as `/ops:review`; `$ARGUMENTS` and `$1`, `$2`, etc. expand from the invocation. These files provide prompts to the agent; they do not bypass tool policy.

## Local files and diagnostics

`SAYACODE_HOME` defaults to `~/.sayacode`. The principal files are `config.json` for product preferences/profiles, `checkpoints.sqlite3` for graph history, `store.sqlite3` for thread/task metadata, `audit.jsonl` for audit events, plus `outputs/` and `worktrees/`. Conversation messages are not copied into a second transcript database.

```bash
sayacode --doctor
sayacode --doctor --json
sayacode --doctor --bundle support.json
```

`/doctor` runs the same local checks from the interactive terminal. A support bundle contains diagnostic status rather than API keys.

## Development

```bash
python -m pip install uv==0.12.5
uv sync --locked --extra dev
uv run --no-sync python scripts/check_release.py
uv run --no-sync python -m pytest -q
uv run --no-sync python -m ruff check src tests scripts
uv run --no-sync python -m mypy
uv build
```

The release check validates the 2.0 package layout, exact direct pins and the universal `uv.lock`, compiles `src/`, `tests/`, and `scripts/`, and runs the full test suite, Ruff, MyPy, and CLI startup checks. CI installs from the same lock on Windows and Ubuntu across Python 3.11–3.13. Its package job builds wheel and source distributions, installs the wheel in a clean environment using locked dependencies, and checks the installed CLI. `pyproject.toml` is the sole dependency declaration; `uv.lock` records the platform-specific resolutions and artifact hashes.

The new implementation is under `src/sayacode/`. The terminal is a product adapter around LangChain and LangGraph; changes to graph state, tool calls, and approvals should be verified through the public app/CLI and a real checkpointer.

MIT License.
