import base64
import sys
from types import SimpleNamespace

import lib.tools.shell_tools as shell_tools
from lib.tools.shell_tools import (
    _mask_env_value,
    check_command_safety,
    execute_command,
    execute_python,
    read_output_file,
    reset_workspace,
    use_workspace,
)


def _python_command(code: str) -> str:
    payload = base64.b64encode(code.encode("utf-8")).decode("ascii")
    return (
        f'"{sys.executable}" -c '
        f'"import base64; exec(base64.b64decode(\'{payload}\').decode(\'utf-8\'))"'
    )


def test_execute_command_accepts_bounded_stdin_payload():
    command = _python_command("name = input(); print('hello ' + name)")

    stdout, stderr, returncode, _, _ = execute_command(
        command,
        timeout=5,
        input_text="Saya\n",
        check_safety=False,
        shell=True,
    )

    assert returncode == 0, stderr
    assert stdout.strip() == "hello Saya"


def test_execute_command_without_stdin_does_not_block_on_input():
    command = _python_command("input('name: ')")

    stdout, stderr, returncode, _, _ = execute_command(
        command,
        timeout=5,
        check_safety=False,
        shell=True,
    )

    assert returncode not in (0, 124)
    assert "EOF" in stderr
    assert stdout == "name: "


def test_execute_command_timeout_returns_124():
    command = _python_command("import time; time.sleep(10)")

    stdout, stderr, returncode, _, _ = execute_command(
        command,
        timeout=1,
        check_safety=False,
        shell=True,
    )

    assert returncode == 124
    assert "超时" in stderr
    assert stdout == ""


def test_environment_value_masking_hides_credentials():
    assert _mask_env_value("OPENAI_API_KEY", "sk-secret") == "***"
    assert (
        _mask_env_value("PIP_INDEX_URL", "https://user:token@example.com/simple")
        == "https://example.com/simple"
    )


def test_execute_command_filters_sensitive_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    command = _python_command("import os; print(os.environ.get('OPENAI_API_KEY', ''))")

    stdout, stderr, returncode, _, _ = execute_command(
        command,
        timeout=5,
        check_safety=False,
        shell=True,
    )

    assert returncode == 0, stderr
    assert "sk-secret" not in stdout
    assert stdout.strip() == ""


def test_command_safety_rejects_recursive_delete_and_encoded_powershell():
    assert check_command_safety("rm -r .")["is_safe"] is False
    assert check_command_safety("powershell -EncodedCommand AAAA")["is_safe"] is False


def test_read_output_file_rejects_path_traversal(tmp_path):
    output_dir = tmp_path / ".sayacode_outputs"
    output_dir.mkdir()
    (tmp_path / ".env").write_text("SECRET=leaked", encoding="utf-8")
    token = use_workspace(tmp_path)
    try:
        result = read_output_file.invoke({"path": "../.env"})
    finally:
        reset_workspace(token)

    assert "安全警告" in result
    assert "SECRET=leaked" not in result


def test_read_output_file_reads_saved_output_only(tmp_path):
    output_dir = tmp_path / ".sayacode_outputs"
    output_dir.mkdir()
    (output_dir / "stdout_1.out").write_text("hello\n", encoding="utf-8")
    token = use_workspace(tmp_path)
    try:
        result = read_output_file.invoke({"path": "stdout_1.out"})
    finally:
        reset_workspace(token)

    assert "hello" in result


def test_read_output_file_rejects_negative_line_limits(tmp_path):
    output_dir = tmp_path / ".sayacode_outputs"
    output_dir.mkdir()
    (output_dir / "stdout_1.out").write_text("one\ntwo\n", encoding="utf-8")
    token = use_workspace(tmp_path)
    try:
        result = read_output_file.invoke({"path": "stdout_1.out", "tail": -1})
    finally:
        reset_workspace(token)

    assert "安全警告" in result
    assert "two" not in result


def test_read_output_file_zero_head_returns_no_body_lines(tmp_path):
    output_dir = tmp_path / ".sayacode_outputs"
    output_dir.mkdir()
    (output_dir / "stdout_1.out").write_text("one\ntwo\n", encoding="utf-8")
    token = use_workspace(tmp_path)
    try:
        result = read_output_file.invoke({"path": "stdout_1.out", "head": 0})
    finally:
        reset_workspace(token)

    assert "0 行" in result
    assert "one" not in result
    assert "two" not in result


def test_execute_python_uses_current_interpreter(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return SimpleNamespace(stdout="ok\n", stderr="", returncode=0)

    monkeypatch.setattr(shell_tools.subprocess, "run", fake_run)

    stdout, stderr, returncode = execute_python("print('ok')")

    assert stdout == "ok\n"
    assert stderr == ""
    assert returncode == 0
    assert captured["cmd"][0] == sys.executable
