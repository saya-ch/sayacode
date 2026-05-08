from lib.runtime import RuntimeContext
from lib.agent import SAIAgent
from lib.tools import ToolFactory, configure_tool_workspace
from lib.tools.file_tools import (
    batch_edit,
    get_default_workspace as get_file_workspace,
    reset_workspace as reset_file_workspace,
    use_workspace as use_file_workspace,
)
from lib.tools.git_tools import get_default_workspace as get_git_workspace
from lib.tools.project_tools import get_default_workspace as get_project_workspace
from lib.tools.shell_tools import get_default_workspace as get_shell_workspace
from lib.core.hooks import HookRuntime
from lib.core.permissions import (
    PermissionRuntime,
    permission_runtime_session,
    set_permission_confirm_callback,
)
from lib.core.private_io import write_private_json
from concurrent.futures import ThreadPoolExecutor
import subprocess
import sys


class DummyModel:
    model_name = "unit"
    model_type = "dummy"
    context_window = 4096

    def chat(self, messages):
        return "ok"

    def bind_tools(self, tools):
        self.bound_tools = tools
        return self


class DummyRuntime:
    def __init__(self):
        self.shutdown_called = False

    def shutdown(self):
        self.shutdown_called = True


def _tool_by_name(tools, name):
    return next(tool for tool in tools if tool.name == name)


def _write_hook_script(path, log_path):
    path.write_text(
        "\n".join([
            "import json",
            "import sys",
            "payload = json.load(sys.stdin)",
            "with open(sys.argv[1], 'a', encoding='utf-8') as f:",
            "    f.write(payload['event'] + ':' + payload['payload'].get('tool_name', '') + '\\n')",
        ]),
        encoding="utf-8",
    )
    return [sys.executable, str(path), str(log_path)]


def test_runtime_context_resolves_workspace_paths(tmp_path):
    context = RuntimeContext(
        workspace=tmp_path,
        model_type="ollama",
        model_name="unit",
        model_config={"context_window": 4096},
    )

    assert context.resolve_workspace_path("src/app.py") == (tmp_path / "src" / "app.py").resolve()
    assert context.context_window == 4096


def test_tool_factory_binds_tools_to_runtime_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
    workspace_one = tmp_path / "one"
    workspace_two = tmp_path / "two"
    workspace_one.mkdir()
    workspace_two.mkdir()
    (workspace_one / "marker.txt").write_text("workspace-one", encoding="utf-8")
    (workspace_two / "marker.txt").write_text("workspace-two", encoding="utf-8")

    context_one = RuntimeContext(
        workspace=workspace_one,
        model_type="ollama",
        model_name="unit",
        model_config={},
    )
    read_tool = _tool_by_name(ToolFactory(context_one), "read_file")

    configure_tool_workspace(str(workspace_two))
    result = read_tool.invoke({"path": "marker.txt"})

    assert "workspace-one" in result
    assert get_file_workspace() == workspace_two.resolve()
    assert get_shell_workspace() == workspace_two.resolve()


def test_sai_agent_default_tools_do_not_mutate_global_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
    workspace_one = tmp_path / "one"
    workspace_two = tmp_path / "two"
    workspace_one.mkdir()
    workspace_two.mkdir()
    (workspace_one / "marker.txt").write_text("agent-one", encoding="utf-8")
    (workspace_two / "marker.txt").write_text("agent-two", encoding="utf-8")
    configure_tool_workspace(str(workspace_two))

    agent = SAIAgent(model=DummyModel(), workspace=workspace_one)
    read_tool = _tool_by_name(agent.tools, "read_file")
    result = read_tool.invoke({"path": "marker.txt"})

    assert "agent-one" in result
    assert "agent-two" not in result
    assert get_file_workspace() == workspace_two.resolve()
    assert get_shell_workspace() == workspace_two.resolve()


def test_sai_agent_close_releases_owned_mcp_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    agent = SAIAgent(model=DummyModel(), workspace=workspace)
    runtime = DummyRuntime()
    agent._mcp_runtime = runtime

    agent.close()

    assert runtime.shutdown_called is True
    assert agent._mcp_runtime is None


def test_tool_factory_isolates_file_shell_git_and_project_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
    workspace_one = tmp_path / "one"
    workspace_two = tmp_path / "two"
    workspace_one.mkdir()
    workspace_two.mkdir()
    (workspace_one / "marker.txt").write_text("one", encoding="utf-8")
    (workspace_one / "one-only.txt").write_text("git-one", encoding="utf-8")
    (workspace_two / "marker.txt").write_text("two", encoding="utf-8")
    (workspace_two / "two-only.txt").write_text("git-two", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=workspace_one, check=True, capture_output=True, text=True)
    subprocess.run(["git", "init"], cwd=workspace_two, check=True, capture_output=True, text=True)
    set_permission_confirm_callback(lambda request: True)

    try:
        context_one = RuntimeContext(
            workspace=workspace_one,
            model_type="ollama",
            model_name="unit",
            model_config={},
        )
        tools = ToolFactory(context_one)
        read_file = _tool_by_name(tools, "read_file")
        shell = _tool_by_name(tools, "execute_command_tool")
        git_status = _tool_by_name(tools, "git_status")
        summary = _tool_by_name(tools, "get_project_summary")

        configure_tool_workspace(str(workspace_two))

        assert "one" in read_file.invoke({"path": "marker.txt"})
        assert "one" in shell.invoke({
            "command": "python -c \"import pathlib; print(pathlib.Path.cwd().name)\"",
            "timeout": 30,
        })
        git_result = git_status.invoke({})
        assert "one-only.txt" in git_result
        assert "two-only.txt" not in git_result
        assert "one" in summary.invoke({})

        assert get_file_workspace() == workspace_two.resolve()
        assert get_shell_workspace() == workspace_two.resolve()
        assert get_git_workspace() == workspace_two.resolve()
        assert get_project_workspace() == workspace_two.resolve()
    finally:
        set_permission_confirm_callback(None)


def test_tool_factory_uses_runtime_permission_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
    workspace_one = tmp_path / "one"
    workspace_two = tmp_path / "two"
    workspace_one.mkdir()
    workspace_two.mkdir()
    policy_dir = workspace_one / ".sayacode"
    policy_dir.mkdir()
    write_private_json(policy_dir / "permissions.json", {
        "default": "ask",
        "tools": {"delete_file": "allow"},
    })
    set_permission_confirm_callback(None)

    context_one = RuntimeContext(
        workspace=workspace_one,
        model_type="ollama",
        model_name="unit",
        model_config={},
    )
    context_two = RuntimeContext(
        workspace=workspace_two,
        model_type="ollama",
        model_name="unit",
        model_config={},
    )

    del_one = _tool_by_name(ToolFactory(context_one), "delete_file")
    del_two = _tool_by_name(ToolFactory(context_two), "delete_file")
    configure_tool_workspace(str(workspace_two))

    allowed = del_one.invoke({"path": "dummy1.txt"})
    blocked = del_two.invoke({"path": "dummy2.txt"})

    assert "文件/目录不存在" in allowed
    assert "文件/目录不存在" in blocked  # all tools default to allow
    assert get_file_workspace() == workspace_two.resolve()


def test_tool_factory_uses_explicit_runtime_permission_service(tmp_path, monkeypatch):
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy_dir = workspace / ".sayacode"
    policy_dir.mkdir()
    write_private_json(policy_dir / "permissions.json", {
        "default": "ask",
        "tools": {"delete_file": "allow"},
    })
    set_permission_confirm_callback(lambda request: True)

    try:
        context = RuntimeContext(
            workspace=workspace,
            model_type="ollama",
            model_name="unit",
            model_config={},
        )
        context.permissions = PermissionRuntime()
        del_tool = _tool_by_name(ToolFactory(context), "delete_file")

        result = del_tool.invoke({"path": "dummy.txt"})

        assert "文件/目录不存在" in result
        assert context.permissions.audit_log[-1]["tool"] == "delete_file"
    finally:
        set_permission_confirm_callback(None)


def test_batch_edit_enforces_permission_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    permissions = PermissionRuntime()
    permissions.configure_workspace(workspace)
    permissions.set_session_rules({"batch_edit": "deny"}, source="test")
    token = use_file_workspace(workspace)
    try:
        with permission_runtime_session(permissions):
            result = batch_edit.invoke({
                "edits": [{
                    "path": "blocked.txt",
                    "operation": "write",
                    "content": "blocked",
                }],
            })
    finally:
        reset_file_workspace(token)

    assert "Permission denied" in result
    assert not (workspace / "blocked.txt").exists()


def test_tool_factory_uses_explicit_runtime_hook_service(tmp_path, monkeypatch):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    log_path = tmp_path / "hook.log"
    script_path = tmp_path / "hook.py"
    monkeypatch.setenv("SAYACODE_HOME", str(home))

    context = RuntimeContext(
        workspace=workspace,
        model_type="ollama",
        model_name="unit",
        model_config={},
    )
    context.hooks = HookRuntime()

    home.mkdir(parents=True)
    write_private_json(home / "hooks.json", {
        "hooks": {
            "PreToolUse": [{
                "name": "late-hook",
                "command": _write_hook_script(script_path, log_path),
            }]
        }
    })
    read_tool = _tool_by_name(ToolFactory(context), "read_file")

    read_tool.invoke({"path": "missing.txt"})

    assert not log_path.exists()


def test_runtime_bound_tools_are_context_local_under_threads(tmp_path, monkeypatch):
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
    workspace_one = tmp_path / "one"
    workspace_two = tmp_path / "two"
    fallback_workspace = tmp_path / "fallback"
    workspace_one.mkdir()
    workspace_two.mkdir()
    fallback_workspace.mkdir()
    (workspace_one / "marker.txt").write_text("thread-one", encoding="utf-8")
    (workspace_two / "marker.txt").write_text("thread-two", encoding="utf-8")
    set_permission_confirm_callback(lambda request: True)

    try:
        context_one = RuntimeContext(
            workspace=workspace_one,
            model_type="ollama",
            model_name="unit",
            model_config={},
        )
        context_two = RuntimeContext(
            workspace=workspace_two,
            model_type="ollama",
            model_name="unit",
            model_config={},
        )
        tools_one = ToolFactory(context_one)
        tools_two = ToolFactory(context_two)
        read_one = _tool_by_name(tools_one, "read_file")
        read_two = _tool_by_name(tools_two, "read_file")
        shell_one = _tool_by_name(tools_one, "execute_command_tool")
        shell_two = _tool_by_name(tools_two, "execute_command_tool")
        configure_tool_workspace(str(fallback_workspace))

        def call_pair(read_tool, shell_tool, expected_text, expected_cwd):
            for _ in range(3):
                assert expected_text in read_tool.invoke({"path": "marker.txt"})
                result = shell_tool.invoke({
                    "command": f'"{sys.executable}" -c "import pathlib; print(pathlib.Path.cwd().name)"',
                    "timeout": 30,
                })
                assert expected_cwd in result

        with ThreadPoolExecutor(max_workers=2) as pool:
            future_one = pool.submit(call_pair, read_one, shell_one, "thread-one", "one")
            future_two = pool.submit(call_pair, read_two, shell_two, "thread-two", "two")
            future_one.result()
            future_two.result()

        assert get_file_workspace() == fallback_workspace.resolve()
        assert get_shell_workspace() == fallback_workspace.resolve()
        assert get_git_workspace() == fallback_workspace.resolve()
        assert get_project_workspace() == fallback_workspace.resolve()
    finally:
        set_permission_confirm_callback(None)
