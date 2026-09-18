"""发布前质量门禁。

按顺序执行：字节编译→全量 pytest→中英文 `--help`/`--doctor` 冒烟→
pip dry-run/wheel 打包→包管理器残留与密钥字面量扫描→构建产物清理。
任一步失败即非零退出；全部通过打印 `Release checks passed.`。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RELEASE_HOME = ROOT / ".tmp_release_home"
BUILD_ARTIFACT_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".tmp_release_home",
    ".tmp_wheel",
    "build",
    "dist",
}
BUILD_ARTIFACT_FILE_NAMES = {
    ".tmp_support_bundle.json",
}
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*=\s*['\"][^'\"\s]{8,}['\"]"),
]
TEXT_SUFFIXES = {
    ".bat",
    ".cfg",
    ".css",
    ".html",
    ".ini",
    ".json",
    ".md",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
IGNORED_FILE_NAMES = {
    "ARCHITECTURE.md",
    "CHANGELOG.md",
}
# 已移除包管理器检查的白名单目录：tests/ 中的依赖名（如 uvicorn）是测试夹具，
# 整词匹配后本不应误杀，目录级白名单是第二道保险，避免测试数据中断发布。
REMOVED_PM_WHITELIST_DIRS = {"tests"}
# 包管理器名拆开书写：本文件也在门禁的扫描范围内，避免自匹配。
_REMOVED_PM_TOKEN = "u" + "v"
REMOVED_PM_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])" + _REMOVED_PM_TOKEN + r"(?![A-Za-z0-9_])",
    re.IGNORECASE,
)


def run(command: list[str], timeout: int = 180) -> None:
    """执行一条发布检查命令，失败直接抛异常中断门禁。"""
    print(f"\n> {' '.join(command)}", flush=True)
    subprocess.run(
        command,
        cwd=ROOT,
        env=release_env(),
        check=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )


def stdout_safe(text: str) -> str:
    """按当前终端编码清洗输出，避免 Windows 下编码报错。"""
    encoding = sys.stdout.encoding or "utf-8"
    return text.encode(encoding, errors="replace").decode(encoding, errors="replace")


def run_expect(command: list[str], expected: list[str], timeout: int = 60) -> None:
    """执行命令并断言输出包含预期片段，缺失则非零退出。"""
    print(f"\n> {' '.join(command)}", flush=True)
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=release_env(),
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = result.stdout or ""
    print(stdout_safe(output), end="" if output.endswith("\n") else "\n")
    if result.returncode != 0:
        raise SystemExit(f"Command failed with exit code {result.returncode}: {' '.join(command)}")

    missing = [item for item in expected if item not in output]
    if missing:
        raise SystemExit(
            "Expected output was not found:\n"
            + "\n".join(missing)
            + f"\nCommand: {' '.join(command)}"
        )


def release_env() -> dict[str, str]:
    """构造隔离的门禁环境（`SAYACODE_HOME` 指向临时目录）。"""
    env = os.environ.copy()
    env["SAYACODE_HOME"] = str(RELEASE_HOME)
    return env


def iter_project_files() -> list[Path]:
    """枚举需扫描的项目文件，跳过缓存、构建与版本控制目录。"""
    ignored_dirs = {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tmp_wheel",
        ".venv",
        ".vscode",
        "__pycache__",
        "build",
        "dist",
        "htmlcov",
        "node_modules",
    }
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if any(part in ignored_dirs for part in path.relative_to(ROOT).parts):
            continue
        if path.name in IGNORED_FILE_NAMES:
            continue
        if path.is_file():
            files.append(path)
    return files


def has_removed_package_manager_reference(text: str) -> bool:
    """整词匹配已下线包管理器引用，避免 uvicorn/fluvio 这类子串误杀。"""
    return bool(REMOVED_PM_PATTERN.search(text))


def assert_no_removed_package_manager_references() -> None:
    """断言仓库无已下线包管理器的残留引用，有则列出并退出。"""
    offenders = []
    for path in iter_project_files():
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        rel = path.relative_to(ROOT)
        if any(part in REMOVED_PM_WHITELIST_DIRS for part in rel.parts):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if has_removed_package_manager_reference(content):
            offenders.append(rel.as_posix())

    if offenders:
        raise SystemExit("Found removed package-manager references:\n" + "\n".join(offenders))


def assert_no_secret_literals() -> None:
    """断言文本文件中无密钥字面量，有则列出并退出。"""
    offenders = []
    for path in iter_project_files():
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for pattern in SECRET_PATTERNS:
            if pattern.search(content):
                offenders.append(path.relative_to(ROOT).as_posix())
                break

    if offenders:
        raise SystemExit("Potential secret literals found:\n" + "\n".join(offenders))


def cleanup_artifacts() -> None:
    """清理构建与缓存产物（build/dist/__pycache__ 等）。"""
    for path in ROOT.rglob("*"):
        if path.is_dir() and (path.name in BUILD_ARTIFACT_NAMES or path.name.endswith(".egg-info")):
            shutil.rmtree(path, ignore_errors=True)
        elif path.is_file() and path.name in BUILD_ARTIFACT_FILE_NAMES:
            path.unlink(missing_ok=True)


def assert_clean_artifacts() -> None:
    """断言构建与缓存产物已清干净，有残留则列出并退出。"""
    offenders = []
    for path in ROOT.rglob("*"):
        if path.is_dir() and (path.name in BUILD_ARTIFACT_NAMES or path.name.endswith(".egg-info")):
            offenders.append(path.relative_to(ROOT).as_posix())
        elif path.is_file() and path.name in BUILD_ARTIFACT_FILE_NAMES:
            offenders.append(path.relative_to(ROOT).as_posix())

    if offenders:
        raise SystemExit("Build/cache artifacts remain:\n" + "\n".join(offenders))


def main() -> int:
    """发布检查主流程，返回进程退出码（0 表示通过）。"""
    cleanup_artifacts()
    run([sys.executable, "-m", "compileall", "-q", "lib", "run.py", "tests", "scripts"])
    run([sys.executable, "-m", "pytest", "-q"])
    run([sys.executable, "run.py", "--version"])
    run_expect([sys.executable, "run.py", "--lang", "en", "--help"], ["Workspace path", "options:"], timeout=60)
    run_expect([sys.executable, "run.py", "--lang", "zh", "--help"], ["指定工作区路径", "用法:", "选项:"], timeout=60)
    run_expect([sys.executable, "run.py", "--lang", "en", "--doctor", "--no-clear"], ["SAYACODE Doctor", "Workspace"], timeout=60)
    run_expect([sys.executable, "run.py", "--lang", "zh", "--doctor", "--no-clear"], ["SAYACODE 诊断", "工作区"], timeout=60)
    run_expect([sys.executable, "run.py", "--doctor", "--json", "--no-clear"], ['"name": "Workspace"', '"status":'], timeout=60)
    run_expect([sys.executable, "run.py", "--lang", "zh", "--doctor", "--json", "--no-clear"], ['"name": "Workspace"', '"status":'], timeout=60)
    support_bundle = ROOT / ".tmp_support_bundle.json"
    run_expect(
        [sys.executable, "run.py", "--doctor", "--bundle", str(support_bundle), "--no-clear"],
        [str(support_bundle)],
        timeout=60,
    )
    run([sys.executable, "-m", "pip", "install", "-e", ".", "--dry-run", "--no-deps"])
    run([sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "--wheel-dir", ".tmp_wheel"])
    assert_no_removed_package_manager_references()
    assert_no_secret_literals()
    cleanup_artifacts()
    assert_clean_artifacts()
    print("\nRelease checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
