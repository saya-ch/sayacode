import json
import sys

from lib.core.hooks import (
    configure_hooks_workspace,
    get_hook_audit_log,
    render_hook_status,
    trigger_hook_event,
    trust_hook_workspace,
)
from lib.tools import configure_tool_workspace, read_file


def _write_hook_script(path, exit_code: int = 0):
    path.write_text(
        "\n".join(
            [
                "import json",
                "import sys",
                "payload = json.load(sys.stdin)",
                "with open(sys.argv[1], 'a', encoding='utf-8') as f:",
                "    f.write(payload['event'] + ':' + payload['payload'].get('tool_name', '') + '\\n')",
                f"sys.exit({exit_code})",
            ]
        ),
        encoding="utf-8",
    )


def _write_user_hooks(home, event, command, blocking=True):
    home.mkdir(parents=True, exist_ok=True)
    (home / "hooks.json").write_text(
        json.dumps({
            "hooks": {
                event: [{
                    "name": "test-hook",
                    "command": command,
                    "blocking": blocking,
                    "timeout": 5,
                }]
            }
        }),
        encoding="utf-8",
    )


def test_hook_event_runs_user_command(tmp_path, monkeypatch):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    log_path = tmp_path / "hook.log"
    script_path = tmp_path / "hook.py"
    _write_hook_script(script_path)
    monkeypatch.setenv("SAYACODE_HOME", str(home))
    _write_user_hooks(home, "PreToolUse", [sys.executable, str(script_path), str(log_path)])

    configure_hooks_workspace(workspace)
    result = trigger_hook_event("PreToolUse", {"tool_name": "read_file"})

    assert result is None
    assert "PreToolUse:read_file" in log_path.read_text(encoding="utf-8")


def test_project_hooks_require_trust(tmp_path, monkeypatch):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    project_hook_dir = workspace / ".sayacode"
    project_hook_dir.mkdir(parents=True)
    log_path = tmp_path / "project-hook.log"
    script_path = tmp_path / "hook.py"
    _write_hook_script(script_path)
    monkeypatch.setenv("SAYACODE_HOME", str(home))
    (project_hook_dir / "hooks.json").write_text(
        json.dumps({
            "hooks": {
                "SessionStart": [{
                    "command": [sys.executable, str(script_path), str(log_path)]
                }]
            }
        }),
        encoding="utf-8",
    )

    configure_hooks_workspace(workspace)
    trigger_hook_event("SessionStart", {})
    assert not log_path.exists()

    trust_hook_workspace(workspace)
    trigger_hook_event("SessionStart", {})
    assert "SessionStart:" in log_path.read_text(encoding="utf-8")


def test_blocking_hook_blocks_event(tmp_path, monkeypatch):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    log_path = tmp_path / "blocking.log"
    script_path = tmp_path / "block.py"
    _write_hook_script(script_path, exit_code=7)
    monkeypatch.setenv("SAYACODE_HOME", str(home))
    _write_user_hooks(home, "UserPromptSubmit", [sys.executable, str(script_path), str(log_path)])

    configure_hooks_workspace(workspace)
    result = trigger_hook_event("UserPromptSubmit", {"input": "blocked"})

    assert result is not None
    assert "blocked UserPromptSubmit" in result


def test_tool_wrapper_emits_hooks(tmp_path, monkeypatch):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    log_path = tmp_path / "tool-hook.log"
    script_path = tmp_path / "hook.py"
    _write_hook_script(script_path)
    monkeypatch.setenv("SAYACODE_HOME", str(home))
    _write_user_hooks(home, "PreToolUse", [sys.executable, str(script_path), str(log_path)])

    configure_tool_workspace(str(workspace))
    read_file.invoke({"path": "missing.txt"})

    assert "PreToolUse:read_file" in log_path.read_text(encoding="utf-8")
    assert "test-hook" in str(get_hook_audit_log())


def test_hook_status_renders(tmp_path, monkeypatch):
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
    configure_hooks_workspace(tmp_path)

    status = render_hook_status()

    assert "Hook Status" in status
    assert "User hooks" in status
