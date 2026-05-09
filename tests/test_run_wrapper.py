import subprocess
import sys
import json
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "run.py", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        stdin=subprocess.DEVNULL,
    )


def test_cli_version_matches_package_metadata():
    from lib._version import __version__
    from lib.cli.parser import CLI_VERSION

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    # pyproject.toml 使用 setuptools dynamic version: attr 指向 lib._version
    version_cfg = pyproject.get("tool", {}).get("setuptools", {}).get("dynamic", {})
    attr_path = version_cfg.get("version", {}).get("attr", "")
    # 验证 attr 路径正确
    assert attr_path == "lib._version.__version__", (
        f"pyproject.toml version attr should be 'lib._version.__version__', got '{attr_path}'"
    )
    assert CLI_VERSION == f"SAYACODE v{__version__}"


def test_run_py_help_forwards_to_real_cli():
    result = run_cli("--help")

    assert result.returncode == 0
    assert "--doctor" in result.stdout
    assert "--workspace" in result.stdout
    assert "--context-window" in result.stdout
    assert "--bundle" in result.stdout


def test_run_py_help_respects_lang_override():
    english = run_cli("--lang", "en", "--help")
    chinese = run_cli("--lang", "zh", "--help")

    assert english.returncode == 0
    assert "Workspace path" in english.stdout
    assert "指定工作区路径" not in english.stdout
    assert chinese.returncode == 0
    assert "指定工作区路径" in chinese.stdout
    assert "用法:" in chinese.stdout
    assert "选项:" in chinese.stdout


def test_run_py_doctor_report_respects_lang_override():
    result = run_cli("--lang", "zh", "--doctor", "--no-clear")

    assert result.returncode == 0
    assert "SAYACODE 诊断" in result.stdout
    assert "工作区" in result.stdout
    assert "SAYACODE Doctor" not in result.stdout


def test_run_py_doctor_json_stays_stable_with_lang_override():
    result = run_cli("--lang", "zh", "--doctor", "--json", "--no-clear")
    payload = json.loads(result.stdout)

    assert result.returncode == 0
    assert any(check["name"] == "Workspace" for check in payload["checks"])


def test_run_py_doctor_bundle_writes_redacted_support_payload(tmp_path):
    bundle_path = tmp_path / "support.json"
    result = run_cli("--doctor", "--bundle", str(bundle_path), "--no-clear")
    payload = json.loads(bundle_path.read_text(encoding="utf-8"))

    assert result.returncode == 0
    assert str(bundle_path) in result.stdout
    assert payload["schema_version"] == 1
    assert any(check["name"] == "Workspace" for check in payload["checks"])
