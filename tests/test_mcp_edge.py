# mcp_runtime 边角：fake 进程走完客户端与运行时分支（不 spawn 真进程）。

import json

import pytest

import lib.core.mcp_runtime as mr
from lib.core.mcp_runtime import (
    MCPRuntime,
    MCPRuntimeError,
    MCPServerClient,
    MCPServerConfig,
    MCPToolInfo,
)


class FakeStdin:
    # 记录写入的假 stdin。
    def __init__(self, fail=False):
        self.written = []
        self._fail = fail

    def write(self, text):
        if self._fail:
            raise OSError("closed")
        self.written.append(text)

    def flush(self):
        pass


class FakeProc:
    # 可编排的假 Popen。
    def __init__(self, poll_seq=None, stdout_lines=None, stderr_lines=None):
        self._polls = list(poll_seq or [None])
        self.stdin = FakeStdin()
        self.stdout = list(stdout_lines or [])
        self.stderr = list(stderr_lines or [])
        self.pid = 99999
        self.returncode = None
        self.killed = 0
        self.terminated = 0

    def poll(self):
        value = self._polls.pop(0) if len(self._polls) > 1 else self._polls[0]
        if value is not None:
            self.returncode = value
        return value

    def kill(self):
        self.killed += 1

    def terminate(self):
        self.terminated += 1

    def wait(self, timeout=None):
        return 0


def _client():
    # 带假配置的客户端固件。
    from pathlib import Path as _Path

    cfg = MCPServerConfig(name="s", command="cmd", args=["a"], env={"K": "v"})
    return MCPServerClient(cfg, _Path("."))


def _reply(rid, result=None, error=None):
    # 单条 JSON-RPC 响应行。
    msg = {"jsonrpc": "2.0", "id": rid}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    return json.dumps(msg)


class TestClient:
    def test_active(self):
        c = _client()
        assert c.active is False
        c.process = FakeProc(poll_seq=[None])
        assert c.active is True
        c.process = FakeProc(poll_seq=[0])
        assert c.active is False

    def test_start_disabled(self):
        cfg = MCPServerConfig(name="s", command="cmd", disabled=True)
        from pathlib import Path as _Path

        with pytest.raises(MCPRuntimeError):
            MCPServerClient(cfg, _Path(".")).start()

    def test_start_active_noop(self):
        c = _client()
        c.process = FakeProc(poll_seq=[None])
        c.start()

    def test_write_no_stdin(self):
        c = _client()
        with pytest.raises(MCPRuntimeError):
            c._write_json({})
        proc = FakeProc()
        proc.stdin = None
        c.process = proc
        with pytest.raises(MCPRuntimeError):
            c._write_json({})

    def test_write_ok(self):
        c = _client()
        c.process = FakeProc()
        c._write_json({"a": 1})
        assert len(c.process.stdin.written) == 1
        c._notify("m", {"x": 1})
        assert len(c.process.stdin.written) == 2

    def test_request_no_process(self):
        with pytest.raises(MCPRuntimeError):
            _client()._request("m")

    def test_request_exited(self):
        c = _client()
        proc = FakeProc(poll_seq=[1])
        c.process = proc
        with pytest.raises(MCPRuntimeError) as exc:
            c._request("m")
        assert "exited" in str(exc.value)

    def test_request_ok(self):
        c = _client()
        proc = FakeProc(poll_seq=[None] * 10)
        c.process = proc
        c._stdout_queue.put(_reply(1, {"v": 1}))
        assert c._request("m") == {"v": 1}

    def test_request_skips_noise(self):
        c = _client()
        c.process = FakeProc(poll_seq=[None] * 10)
        c._stdout_queue.put("not-json")
        c._stdout_queue.put(_reply(99, {"v": 9}))
        c._stdout_queue.put(_reply(1, {"v": 1}))
        assert c._request("m") == {"v": 1}

    def test_request_error(self):
        c = _client()
        c.process = FakeProc(poll_seq=[None] * 10)
        c._next_id = 0
        c._stdout_queue.put(_reply(1, error={"code": -1, "message": "bad"}))
        with pytest.raises(MCPRuntimeError):
            c._request("m")

    def test_request_scalar_result(self):
        c = _client()
        c.process = FakeProc(poll_seq=[None] * 10)
        c._stdout_queue.put(_reply(1, result=42))
        assert c._request("m") == {"value": 42}

    def test_request_empty_then_reply(self):
        import queue as _queue

        c = _client()
        c.process = FakeProc(poll_seq=[None] * 10)
        c._stdout_queue.put(_reply(1, {"v": 1}))
        real_get = c._stdout_queue.get
        calls = {"n": 0}

        def flaky(timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _queue.Empty()
            return real_get(timeout)

        c._stdout_queue.get = flaky
        assert c._request("m") == {"v": 1}

    def test_request_timeout(self):
        c = _client()
        c.process = FakeProc(poll_seq=[None] * 10)
        with pytest.raises(MCPRuntimeError) as exc:
            c._request("m", timeout=0)
        assert "timed out" in str(exc.value)

    def test_reader_no_pipes(self):
        c = _client()
        c.process = FakeProc()
        c.process.stdout = None
        c._start_reader_threads()

    def test_reader_threads(self):
        import time as _time

        c = _client()
        proc = FakeProc(stdout_lines=["hello\n"], stderr_lines=["e1\n", "e2\n"])
        c.process = proc
        c._start_reader_threads()
        deadline = _time.monotonic() + 5
        while c._stdout_queue.empty() and _time.monotonic() < deadline:
            _time.sleep(0.01)
        assert c._stdout_queue.get_nowait() == "hello"
        deadline = _time.monotonic() + 5
        while len(c._stderr_lines) < 2 and _time.monotonic() < deadline:
            _time.sleep(0.01)
        assert c._stderr_lines == ["e1", "e2"]

    def test_shutdown_variants(self, monkeypatch):
        c = _client()
        c.shutdown()
        c.process = FakeProc(poll_seq=[0])
        c.shutdown()
        c.process = FakeProc(poll_seq=[None])
        seen = []
        monkeypatch.setattr(mr, "_terminate_process_tree", lambda p: seen.append(p))
        c.shutdown()
        assert seen != []

    def test_status(self):
        c = _client()
        c.process = FakeProc(poll_seq=[None])
        c._stderr_lines = ["e"]
        c.tools = [{}, {}]
        st = c.status()
        assert st["active"] is True and st["tools"] == 2

    def test_call_tool(self, monkeypatch):
        c = _client()
        monkeypatch.setattr(c, "_request", lambda *a, **k: {"content": [{"type": "text", "text": "hi"}]})
        assert c.call_tool("t", {}) == "hi"


class TestRuntime:
    def test_configure_same_no_shutdown(self, tmp_path, monkeypatch):
        rt = MCPRuntime()
        rt.configure_workspace(tmp_path)
        calls = []
        monkeypatch.setattr(rt, "shutdown", lambda: calls.append(1))
        rt.configure_workspace(tmp_path)
        assert calls == []
        assert rt.config_path is not None

    def test_load_untrusted(self, tmp_path):
        (tmp_path / ".mcp.json").write_text('{"mcpServers": {"s": {"command": "x"}}}', encoding="utf-8")
        rt = MCPRuntime()
        rt.configure_workspace(tmp_path)
        assert rt.load_tools() == []
        assert "trust" in rt.errors

    def test_load_no_servers(self, tmp_path):
        rt = MCPRuntime()
        rt.configure_workspace(tmp_path)
        assert rt.load_tools() == []
        assert rt.errors == {}

    def test_load_subset_and_failure(self, tmp_path, monkeypatch):
        from lib.core.mcp_runtime import trust_mcp_workspace

        (tmp_path / ".mcp.json").write_text(
            '{"mcpServers": {"good": {"command": "x"}, "bad": {"command": ""}}}',
            encoding="utf-8",
        )
        trust_mcp_workspace(tmp_path)
        rt = MCPRuntime()
        rt.configure_workspace(tmp_path)
        monkeypatch.setattr(mr.MCPServerClient, "start", lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
        assert rt.load_tools(server_names=["good"]) == []
        assert "good" in rt.errors

    def test_load_subset_success(self, tmp_path, monkeypatch):
        from lib.core.mcp_runtime import trust_mcp_workspace

        (tmp_path / ".mcp.json").write_text(
            '{"mcpServers": {"s1": {"command": "x"}, "s2": {"command": "y"}}}',
            encoding="utf-8",
        )
        trust_mcp_workspace(tmp_path)
        rt = MCPRuntime()
        rt.configure_workspace(tmp_path)

        def fake_start(self):
            self.tools = [{"name": "echo", "description": "d", "inputSchema": {}}]

        monkeypatch.setattr(mr.MCPServerClient, "start", fake_start)
        tools = rt.load_tools(server_names=["s1"])
        assert len(tools) == 1 and "s1" in rt.clients
        assert "s2" not in rt.clients
        rt.shutdown()

    def test_call_unknown(self, tmp_path):
        rt = MCPRuntime()
        rt.configure_workspace(tmp_path)
        assert "not registered" in rt.call_tool("ghost", {})
        assert "not registered" in rt._call_tool("ghost", {}, emit_events=False)

    def test_call_hook_blocked(self, tmp_path, monkeypatch):
        rt = MCPRuntime()
        rt.configure_workspace(tmp_path)
        rt.tools_by_alias["a"] = MCPToolInfo(alias="a", server_name="s", name="n", description="d", input_schema={})
        monkeypatch.setattr(mr, "trigger_hook_event", lambda *a, **k: "blocked!")
        assert "blocked!" in rt.call_tool("a", {})

    def test_call_permission_denied(self, tmp_path, monkeypatch):
        rt = MCPRuntime()
        rt.configure_workspace(tmp_path)
        rt.tools_by_alias["a"] = MCPToolInfo(alias="a", server_name="s", name="n", description="d", input_schema={})
        monkeypatch.setattr(mr, "trigger_hook_event", lambda *a, **k: "")
        monkeypatch.setattr(mr, "enforce_tool_permission", lambda *a, **k: "denied!")
        assert rt.call_tool("a", {}) == "denied!"
        assert rt._call_tool("a", {}, emit_events=False) == "denied!"

    def test_call_no_server(self, tmp_path, monkeypatch):
        rt = MCPRuntime()
        rt.configure_workspace(tmp_path)
        rt.tools_by_alias["a"] = MCPToolInfo(alias="a", server_name="s", name="n", description="d", input_schema={})
        monkeypatch.setattr(mr, "trigger_hook_event", lambda *a, **k: "")
        monkeypatch.setattr(mr, "enforce_tool_permission", lambda *a, **k: None)
        assert "not running" in rt.call_tool("a", {})
        assert "not running" in rt._call_tool("a", {}, emit_events=False)

    def test_call_client_raises(self, tmp_path, monkeypatch):
        rt = MCPRuntime()
        rt.configure_workspace(tmp_path)
        rt.tools_by_alias["a"] = MCPToolInfo(alias="a", server_name="s", name="n", description="d", input_schema={})
        rt.clients["s"] = FakeClient()
        monkeypatch.setattr(mr, "trigger_hook_event", lambda *a, **k: "")
        monkeypatch.setattr(mr, "enforce_tool_permission", lambda *a, **k: None)
        assert "failed" in rt.call_tool("a", {})
        assert "failed" in rt._call_tool("a", {}, emit_events=False)

    def test_call_ok(self, tmp_path, monkeypatch):
        rt = MCPRuntime()
        rt.configure_workspace(tmp_path)
        rt.tools_by_alias["a"] = MCPToolInfo(alias="a", server_name="s", name="n", description="d", input_schema={})
        rt.clients["s"] = OkClient()
        monkeypatch.setattr(mr, "trigger_hook_event", lambda *a, **k: "")
        monkeypatch.setattr(mr, "enforce_tool_permission", lambda *a, **k: None)
        assert rt.call_tool("a", {}) == "done"

    def test_call_with_scopes(self, tmp_path, monkeypatch):
        from lib.core.permissions import PermissionRuntime

        rt = MCPRuntime(permissions=PermissionRuntime(), hooks=object())
        rt.configure_workspace(tmp_path)
        assert "not registered" in rt.call_tool("ghost", {})

    def test_load_no_workspace(self):
        rt = MCPRuntime()
        assert isinstance(rt.load_tools(), list)

    def test_status_shutdown(self, tmp_path):
        rt = MCPRuntime()
        rt.configure_workspace(tmp_path)
        st = rt.status()
        assert st["workspace"] == str(tmp_path.resolve())
        assert st["tools"] == [] and st["errors"] == {}
        rt.shutdown()
        mr.shutdown_mcp_runtime()
        assert "not registered" in mr.call_mcp_tool("ghost")


class Simple:
    pass


class FakeClient(Simple):
    def call_tool(self, name, args):
        raise RuntimeError("remote down")

    def shutdown(self):
        pass

    def status(self):
        return {}


class OkClient(Simple):
    def call_tool(self, name, args):
        return "done"

    def shutdown(self):
        pass

    def status(self):
        return {}


class TestHelpers:
    def test_load_configs(self, tmp_path):
        from lib.core.mcp_runtime import _load_server_configs

        assert _load_server_configs(tmp_path) == {}
        (tmp_path / ".mcp.json").write_text("{broken", encoding="utf-8")
        assert _load_server_configs(tmp_path) == {}
        (tmp_path / ".mcp.json").write_text('{"mcpServers": "oops"}', encoding="utf-8")
        assert _load_server_configs(tmp_path) == {}
        (tmp_path / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"a": "oops", "b": {"command": ""}, "c": {"command": "x", "disabled": True},
                                       "Weird Name!": {"command": "x", "args": "oops", "env": "oops", "cwd": "sub"}}}),
            encoding="utf-8",
        )
        cfgs = _load_server_configs(tmp_path)
        assert "c" in cfgs and cfgs["c"].disabled is True
        assert "weird_name" in cfgs
        assert cfgs["weird_name"].args == [] and cfgs["weird_name"].env == {}

    def test_cwd_outside(self, tmp_path):
        from lib.core.mcp_runtime import _load_server_configs

        (tmp_path / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"a": {"command": "x", "cwd": "../evil"}}}),
            encoding="utf-8",
        )
        with pytest.raises(MCPRuntimeError):
            _load_server_configs(tmp_path)

    def test_resolve_cwd(self, tmp_path):
        from lib.core.mcp_runtime import _resolve_server_cwd

        assert _resolve_server_cwd(tmp_path, None) == tmp_path
        assert _resolve_server_cwd(tmp_path, "sub") == (tmp_path / "sub").resolve()

    def test_tool_info(self):
        from lib.core.mcp_runtime import _build_tool_info

        info = _build_tool_info("s", {})
        assert info.alias == "mcp_s_tool" and "MCP tool" in info.description
        info = _build_tool_info("s", {"name": "Echo!", "description": "d", "inputSchema": "oops"})
        assert info.alias == "mcp_s_echo"

    def test_langchain_tool(self):
        from lib.core.mcp_runtime import _build_langchain_tool

        info = MCPToolInfo(alias="mcp_s_echo", server_name="s", name="echo",
                           description="d", input_schema={"type": "object", "properties": {
                               "msg": {"type": "string", "description": "m"},
                               "bad-name": {"type": "string"},
                               "n": "oops"},
                               "required": ["msg"]})
        tool = _build_langchain_tool(info, caller=lambda alias, args: f"{alias}:{args}")
        assert "'msg': 'hi'" in tool.invoke({"msg": "hi"})
        tool2 = _build_langchain_tool(info)
        assert tool2.name == "mcp_s_echo"

    def test_type_python(self):
        from lib.core.mcp_runtime import _json_type_to_python

        assert _json_type_to_python({"type": ["null", "integer"]}) is int
        assert _json_type_to_python({"type": "weird"}) is not None
        assert _json_type_to_python({}) is not None

    def test_format_result(self):
        from lib.core.mcp_runtime import _format_tool_result

        assert _format_tool_result({"content": [{"type": "text", "text": "hi"}]}) == "hi"
        out = _format_tool_result({"content": [{"a": 1}, "x", {"type": "text", "text": ""}]})
        assert out == '{"a": 1}\nx'
        assert _format_tool_result({"other": 1}) != ""
        assert _format_tool_result({"content": [{"type": "text", "text": "bad"}], "isError": True}).startswith("❌")
        # 截断不再是格式化函数的职责：面向模型的包装层会把完整内容落盘，
        # 只把预览 + 定位符交给模型（见 _spill_oversized_result）。
        big = _format_tool_result({"content": [{"type": "text", "text": "x" * 20000}]})
        assert len(big) == 20000 and "truncated" not in big

    def test_normalize(self):
        from lib.core.mcp_runtime import _normalize_component

        assert _normalize_component("Weird Name!") == "weird_name"
        assert _normalize_component("___") == "item"
        assert _normalize_component("") == "item"

    def test_read_json(self, tmp_path):
        from lib.core.mcp_runtime import _read_json_file

        assert _read_json_file(tmp_path / "nope") == {}
        (tmp_path / "b.json").write_text("{broken", encoding="utf-8")
        assert _read_json_file(tmp_path / "b.json") == {}
        (tmp_path / "b.json").write_text("[1]", encoding="utf-8")
        assert _read_json_file(tmp_path / "b.json") == {}

    def test_platform_kwargs(self, monkeypatch):
        import sys as _sys
        from lib.core.mcp_runtime import _popen_platform_kwargs

        assert "creationflags" in _popen_platform_kwargs()
        monkeypatch.setattr(_sys, "platform", "linux")
        assert _popen_platform_kwargs() == {"start_new_session": True}

    def test_terminate_posix(self, monkeypatch):
        import sys as _sys
        from lib.core.mcp_runtime import _terminate_process_tree

        monkeypatch.setattr(_sys, "platform", "linux")
        _terminate_process_tree(FakeProc(poll_seq=[None]))
        proc = FakeProc(poll_seq=[None])

        def bad_wait(timeout=None):
            raise RuntimeError("stuck")

        proc.wait = bad_wait
        _terminate_process_tree(proc)
        assert proc.killed == 1

        class Stubborn(FakeProc):
            def kill(self):
                raise RuntimeError("gone")

        stubborn = Stubborn(poll_seq=[None])
        stubborn.wait = bad_wait
        _terminate_process_tree(stubborn)

    def test_terminate(self, monkeypatch):
        import subprocess as _sp
        from lib.core.mcp_runtime import _terminate_process_tree

        _terminate_process_tree(FakeProc(poll_seq=[0]))
        _terminate_process_tree(FakeProc(poll_seq=[None]))
        monkeypatch.setattr(_sp, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no taskkill")))
        proc = FakeProc(poll_seq=[None])
        _terminate_process_tree(proc)
        assert proc.killed == 1

        class Stubborn(FakeProc):
            def kill(self):
                raise RuntimeError("gone")

        _terminate_process_tree(Stubborn(poll_seq=[None]))

    def test_trust_roundtrip(self, tmp_path):
        from lib.core.mcp_runtime import (
            is_mcp_workspace_trusted,
            trust_mcp_workspace,
            untrust_mcp_workspace,
        )

        assert is_mcp_workspace_trusted(tmp_path) is False
        trust_mcp_workspace(tmp_path)
        assert is_mcp_workspace_trusted(tmp_path) is True
        trust_mcp_workspace(tmp_path)
        untrust_mcp_workspace(tmp_path)
        assert is_mcp_workspace_trusted(tmp_path) is False

    def test_module_wrappers(self, tmp_path, monkeypatch):
        import lib.core.mcp_runtime as _mr

        monkeypatch.setattr(_mr, "_RUNTIME", MCPRuntime())
        _mr.configure_mcp_workspace(tmp_path)
        assert _mr.load_mcp_tools() == []
        assert _mr.reload_mcp_tools() == []
