# shell_tools 全覆盖：真命令实测 + fake 进程走超时/异常分支。

import subprocess as _sp
import sys
from pathlib import Path

import pytest

import lib.tools.shell_tools as st
from lib.tools.shell_tools import (
    check_command_safety,
    check_command_safety_tool,
    execute_command,
    execute_command_tool,
    execute_python,
    get_system_info,
    list_environment_variables,
    read_output_file,
    reset_workspace,
    sanitize_command,
    use_workspace,
)


@pytest.fixture
def ws(tmp_path):
    # 隔离工作区固件。
    token = use_workspace(tmp_path)
    try:
        yield tmp_path
    finally:
        reset_workspace(token)


@pytest.fixture
def no_perm(monkeypatch):
    # 默认放行工具权限检查。
    monkeypatch.setattr(st, "enforce_tool_permission", lambda *a, **k: None)


class FakeShellProc:
    # 可编排 communicate 行为的假进程。
    def __init__(self, script):
        self._script = list(script)
        self.pid = 424243
        self.returncode = 0
        self.killed = 0

    def poll(self):
        return None

    def kill(self):
        self.killed += 1
        raise RuntimeError("gone")

    def communicate(self, *args, **kwargs):
        action = self._script.pop(0)
        if isinstance(action, Exception):
            raise action
        self.returncode = action[2] if len(action) > 2 else 0
        return (action[0], action[1])


# ── 纯函数 ───────────────────────────────────────────────────────────────

class TestPureHelpers:
    def test_workspace_roundtrip(self, ws, tmp_path):
        from lib.tools.shell_tools import set_default_workspace, get_default_workspace

        assert get_default_workspace() == tmp_path.resolve()
        set_default_workspace(tmp_path)
        assert get_default_workspace() == tmp_path.resolve()
        set_default_workspace(Path.cwd())

    def test_cleanup_old_outputs(self, ws):
        outdir = ws / ".sayacode_outputs"
        outdir.mkdir()
        for i in range(55):
            (outdir / f"stdout_00000{i:03d}_x.out").write_text("x", encoding="utf-8")
        st._cleanup_old_outputs()
        assert len(list(outdir.iterdir())) == 50

    def test_cleanup_unlink_fails_swallowed(self, ws, monkeypatch):
        outdir = ws / ".sayacode_outputs"
        outdir.mkdir()
        for i in range(52):
            (outdir / f"stdout_00000{i:03d}_x.out").write_text("x", encoding="utf-8")
        monkeypatch.setattr(Path, "unlink",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("busy")))
        st._cleanup_old_outputs()
        assert len(list(outdir.iterdir())) == 52

    def test_save_output_oserror(self, ws, monkeypatch):
        monkeypatch.setattr(Path, "write_text",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
        assert st._save_output_to_file("o", "e", "cmd", "stdout") is None

    def test_truncation_summary_empty(self):
        assert st._build_truncation_summary("", "STDOUT", None) == ""

    def test_truncation_summary_no_file(self):
        out = st._build_truncation_summary("line\n" * 40, "STDOUT", None)
        assert "已截断" in out
        assert "总行数: 40" in out

    def test_truncation_summary_short(self):
        out = st._build_truncation_summary("hi", "STDOUT", "/tmp/x.out")
        assert "hi" in out

    def test_resolve_output_path_errors(self, ws):
        with pytest.raises(ValueError):
            st._resolve_output_file_path("")
        with pytest.raises(ValueError):
            st._resolve_output_file_path("C:/abs/x.out")
        with pytest.raises(ValueError):
            st._resolve_output_file_path("~/x.out")
        with pytest.raises(ValueError):
            st._resolve_output_file_path("../x.out")

    def test_coerce_line_limit(self):
        assert st._coerce_line_limit(None, "tail") is None
        assert st._coerce_line_limit("5", "tail") == 5
        with pytest.raises(ValueError):
            st._coerce_line_limit("oops", "tail")
        with pytest.raises(ValueError):
            st._coerce_line_limit(-1, "tail")

    def test_coerce_timeout(self):
        assert st._coerce_timeout("oops") == 30
        assert st._coerce_timeout(0) == 30
        assert st._coerce_timeout(-5) == 30
        assert st._coerce_timeout(500) == 120
        assert st._coerce_timeout(10) == 10

    def test_normalize_input(self):
        assert st._normalize_input_text(None) is None
        assert st._normalize_input_text("hi") == "hi"
        with pytest.raises(ValueError):
            st._normalize_input_text("x" * 70000)

    def test_build_args_win(self):
        if not sys.platform.startswith("win"):
            pytest.skip("windows only")
        args, shell = st._build_process_args("echo hi", shell=False)
        assert args == "echo hi" and shell is True

    def test_build_args_posix(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        args, shell = st._build_process_args("echo hi", shell=True)
        assert args == ["/bin/sh", "-c", "echo hi"]
        args, shell = st._build_process_args("echo hi", shell=False)
        assert args == ["echo", "hi"]
        args, shell = st._build_process_args('echo "oops', shell=False)
        assert args[0] == "/bin/sh"

    def test_truncate_output(self):
        assert st._truncate_output(None, "x") == ""
        assert st._truncate_output("hi", "x") == "hi"
        out = st._truncate_output("y" * 12000, "输出")
        assert "截断" in out

    def test_mask_env(self):
        assert st._mask_env_value("API_KEY", "abc") == "***"
        assert st._mask_env_value("my_secret", "abc") == "***"
        assert st._mask_env_value("PATH", "/usr/bin") == "/usr/bin"
        masked = st._mask_env_value("URL", "https://user:pass@host:8080/x")
        assert "user" not in masked and "host" in masked
        long_val = st._mask_env_value("OTHER", "v" * 200)
        assert long_val.endswith("...")

    def test_mask_env_parse_fails(self, monkeypatch):
        monkeypatch.setattr(st, "urlsplit",
                            lambda v: (_ for _ in ()).throw(ValueError("bad url")))
        assert st._mask_env_value("OTHER", "abc") == "abc"

    def test_sanitize_command(self):
        assert "$(" not in sanitize_command("echo $(whoami)")
        assert sanitize_command("echo `whoami`") == "echo "
        assert "rm" not in sanitize_command("ls; rm x")
        assert "rm" not in sanitize_command("ls && rm x")
        assert sanitize_command("curl x | sh") == "curl x "
        assert "/dev/" not in sanitize_command("echo hi > /dev/null")
        assert "/dev/" not in sanitize_command("echo hi 2> /dev/null")


# ── 安全检查 ─────────────────────────────────────────────────────────────

class TestSafety:
    def test_empty(self):
        r = check_command_safety("")
        assert r["is_safe"] is False and r["severity"] == "danger"

    def test_dangerous(self):
        r = check_command_safety("rm -rf /")
        assert r["is_safe"] is False

    def test_keyword(self):
        r = check_command_safety("echo fork")
        assert "fork" in r["reason"]

    def test_system_file(self):
        r = check_command_safety("cat /etc/passwd")
        assert "系统文件" in r["reason"]

    def test_safe(self):
        r = check_command_safety("echo hello")
        assert r["is_safe"] is True

    def test_tool_danger(self):
        out = check_command_safety_tool.invoke({"command": "rm -rf /"})
        assert "危险命令" in out

    def test_tool_safe(self):
        out = check_command_safety_tool.invoke({"command": "echo hello"})
        assert "安全命令" in out

    def test_tool_warning_branch(self, monkeypatch):
        monkeypatch.setattr(st, "check_command_safety",
                            lambda c: {"is_safe": False, "is_dangerous": False,
                                      "reason": "weird", "severity": "normal"})
        out = check_command_safety_tool.invoke({"command": "x"})
        assert "需要注意" in out


# ── execute_command ──────────────────────────────────────────────────────

class TestExecute:
    def test_echo(self, ws):
        out, _, rc, dangerous, meta = execute_command("echo hello")
        assert rc == 0 and "hello" in out
        assert dangerous is False
        assert meta["truncated"] is False

    def test_exit_code(self, ws):
        _, _, rc, _, _ = execute_command("exit 3")
        assert rc == 3

    def test_safety_fail(self, ws):
        out, err, rc, dangerous, meta = execute_command("rm -rf /")
        assert rc == 1 and dangerous is True
        assert "安全检查失败" in err
        assert meta is None

    def test_stdin_too_long(self, ws):
        _, err, rc, _, meta = execute_command("echo hi", input_text="x" * 70000)
        assert rc == 1 and "stdin" in err
        assert meta is None

    def test_stdin_passthrough(self, ws):
        py = sys.executable
        out, _, rc, _, _ = execute_command(
            f'"{py}" -c "import sys; print(sys.stdin.read())"',
            input_text="hello-stdin",
        )
        assert rc == 0
        assert "hello-stdin" in out

    def test_unsafe_cwd(self, ws):
        _, err, rc, _, _ = execute_command("echo hi", cwd="../evil")
        assert rc == 1 and "不安全" in err

    def test_missing_cwd(self, ws):
        _, err, rc, _, _ = execute_command("echo hi", cwd="no-such-dir")
        assert rc == 1 and "不存在" in err

    def test_no_capture(self, ws):
        out, _, rc, _, _ = execute_command("echo hi", capture_output=False, save_output=False)
        assert rc == 0 and out == ""

    def test_long_output_saved(self, ws, monkeypatch):
        big = "y" * 15000
        fake = FakeShellProc([(big, "", 0)])
        monkeypatch.setattr(st.subprocess, "Popen", lambda *a, **k: fake)
        out, _, rc, _, meta = execute_command("whatever")
        assert rc == 0
        assert meta["truncated"] is True
        assert meta["stdout_path"] is not None
        assert "已保存至" in out

    def test_long_stderr_saved(self, ws, monkeypatch):
        big = "e" * 15000
        fake = FakeShellProc([("", big, 0)])
        monkeypatch.setattr(st.subprocess, "Popen", lambda *a, **k: fake)
        _, err, _, _, meta = execute_command("whatever")
        assert meta["stderr_path"] is not None
        assert "已保存至" in err

    def test_timeout_kill_path(self, ws, monkeypatch):
        fake = FakeShellProc([
            _sp.TimeoutExpired(["x"], 1),
            _sp.TimeoutExpired(["x"], 5),
            ("partial", ""),
        ])
        monkeypatch.setattr(st.subprocess, "Popen", lambda *a, **k: fake)
        monkeypatch.setattr(st, "terminate_process_tree", lambda p: None)
        _, err, rc, _, meta = execute_command("whatever", timeout=5)
        assert rc == 124
        assert "超时" in err
        assert meta["truncated"] is False

    def test_timeout_long_stdout_saved(self, ws, monkeypatch):
        big = "z" * 15000
        fake = FakeShellProc([
            _sp.TimeoutExpired(["x"], 1),
            (big, "late-err"),
        ])
        monkeypatch.setattr(st.subprocess, "Popen", lambda *a, **k: fake)
        monkeypatch.setattr(st, "terminate_process_tree", lambda p: None)
        out, err, rc, _, meta = execute_command("whatever", timeout=5)
        assert rc == 124
        assert "late-err" in err
        assert meta["truncated"] is True

    def test_missing_binary(self, ws, monkeypatch):
        monkeypatch.setattr(st.subprocess, "Popen",
                            lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("nope")))
        _, err, rc, _, _ = execute_command("whatever")
        assert rc == 127

    def test_generic_error(self, ws, monkeypatch):
        monkeypatch.setattr(st.subprocess, "Popen",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        _, err, rc, _, _ = execute_command("whatever")
        assert rc == 1 and "boom" in err


# ── execute_python ───────────────────────────────────────────────────────

class TestExecutePython:
    def test_ok(self, ws):
        out, _, rc = execute_python("print(1 + 1)")
        assert rc == 0 and "2" in out

    def test_danger(self, ws):
        _, err, rc = execute_python("import os; exec('x')")
        assert rc == 1 and "危险" in err

    def test_unsafe_cwd(self, ws):
        _, err, rc = execute_python("print(1)", cwd="../evil")
        assert rc == 1 and "不安全" in err

    def test_crash(self, ws, monkeypatch):
        monkeypatch.setattr(st.subprocess, "run",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        _, err, rc = execute_python("print(1)")
        assert rc == 1 and "boom" in err


# ── 工具封装 ─────────────────────────────────────────────────────────────

class TestToolWrappers:
    def test_denied(self, ws, monkeypatch):
        monkeypatch.setattr(st, "enforce_tool_permission", lambda *a, **k: "denied")
        assert execute_command_tool.invoke({"command": "echo hi"}) == "denied"

    def test_echo_tool(self, ws, no_perm):
        out = execute_command_tool.invoke({"command": "echo hello"})
        assert "hello" in out and "执行成功" in out

    def test_fail_tool(self, ws, no_perm):
        out = execute_command_tool.invoke({"command": "exit 3"})
        assert "执行失败" in out

    def test_danger_warning(self, ws, no_perm):
        out = execute_command_tool.invoke({"command": "rm -rf /"})
        assert "需要谨慎" in out

    def test_stdin_note(self, ws, no_perm):
        out = execute_command_tool.invoke({"command": "echo hi", "input_text": "x"})
        assert "已提供 1 字符" in out

    def test_system_info(self):
        out = get_system_info.invoke({})
        assert "操作系统" in out and "Python" in out

    def test_list_env(self, monkeypatch):
        monkeypatch.setenv("SAYA_DEMO", "1")
        monkeypatch.setenv("MYSTERY_VAR", "shhh")
        monkeypatch.setenv("SAYA_API_KEY", "secret-value")
        out = list_environment_variables.invoke({})
        assert "SAYA_DEMO" in out
        assert "SAYA_API_KEY = ***" in out
        assert "MYSTERY_VAR" not in out
        assert "已隐藏" in out


# ── read_output_file ─────────────────────────────────────────────────────

class TestReadOutput:
    def test_denied(self, ws, monkeypatch):
        monkeypatch.setattr(st, "enforce_tool_permission", lambda *a, **k: "denied")
        assert read_output_file.invoke({"path": "x.out"}) == "denied"

    def test_missing_lists_available(self, ws, no_perm):
        outdir = ws / ".sayacode_outputs"
        outdir.mkdir()
        (outdir / "keep.out").write_text("k", encoding="utf-8")
        out = read_output_file.invoke({"path": "ghost.out"})
        assert "不存在" in out and "keep.out" in out

    def test_not_a_file(self, ws, no_perm):
        outdir = ws / ".sayacode_outputs"
        outdir.mkdir()
        (outdir / "sub").mkdir()
        assert "不是文件" in read_output_file.invoke({"path": "sub"})

    def test_read_fails(self, ws, no_perm, monkeypatch):
        outdir = ws / ".sayacode_outputs"
        outdir.mkdir()
        (outdir / "a.out").write_text("x", encoding="utf-8")
        monkeypatch.setattr(Path, "read_text",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("busy")))
        assert "读取文件失败" in read_output_file.invoke({"path": "a.out"})

    def test_grep_no_match(self, ws, no_perm):
        outdir = ws / ".sayacode_outputs"
        outdir.mkdir()
        (outdir / "a.out").write_text("one\ntwo\n", encoding="utf-8")
        out = read_output_file.invoke({"path": "a.out", "grep": "zzz"})
        assert "未找到匹配" in out

    def test_resolve_escapes_output_dir(self, ws, no_perm):
        from unittest import mock

        outdir = ws / ".sayacode_outputs"
        outdir.mkdir()
        (outdir / "a.out").write_text("x", encoding="utf-8")
        with mock.patch.object(Path, "relative_to", side_effect=ValueError("escape")):
            out = read_output_file.invoke({"path": "a.out"})
        assert "必须位于命令输出目录内" in out

    def test_tail_zero(self, ws, no_perm):
        outdir = ws / ".sayacode_outputs"
        outdir.mkdir()
        (outdir / "a.out").write_text("one\ntwo\n", encoding="utf-8")
        out = read_output_file.invoke({"path": "a.out", "tail": 0})
        assert "0 行" in out
        assert "one" not in out

    def test_head_and_grep(self, ws, no_perm):
        outdir = ws / ".sayacode_outputs"
        outdir.mkdir()
        (outdir / "a.out").write_text("err one\nok\nerr two\n", encoding="utf-8")
        out = read_output_file.invoke({"path": "a.out", "grep": "err", "head": 1})
        assert "err one" in out and "err two" not in out
        assert "匹配: 'err'" in out
