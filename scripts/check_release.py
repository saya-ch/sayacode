"""Run release-quality checks for SAYACODE."""

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
}


def run(command: list[str], timeout: int = 180) -> None:
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
    encoding = sys.stdout.encoding or "utf-8"
    return text.encode(encoding, errors="replace").decode(encoding, errors="replace")


def run_expect(command: list[str], expected: list[str], timeout: int = 60) -> None:
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
    env = os.environ.copy()
    env["SAYACODE_HOME"] = str(RELEASE_HOME)
    return env


def iter_project_files() -> list[Path]:
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


def assert_no_removed_package_manager_references() -> None:
    removed_name = "u" + "v"
    offenders = []
    for path in iter_project_files():
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if removed_name in content.lower():
            offenders.append(path.relative_to(ROOT).as_posix())

    if offenders:
        raise SystemExit("Found removed package-manager references:\n" + "\n".join(offenders))


def assert_no_secret_literals() -> None:
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
    for path in ROOT.rglob("*"):
        if path.is_dir() and (path.name in BUILD_ARTIFACT_NAMES or path.name.endswith(".egg-info")):
            shutil.rmtree(path, ignore_errors=True)
        elif path.is_file() and path.name in BUILD_ARTIFACT_FILE_NAMES:
            path.unlink(missing_ok=True)


def assert_clean_artifacts() -> None:
    offenders = []
    for path in ROOT.rglob("*"):
        if path.is_dir() and (path.name in BUILD_ARTIFACT_NAMES or path.name.endswith(".egg-info")):
            offenders.append(path.relative_to(ROOT).as_posix())
        elif path.is_file() and path.name in BUILD_ARTIFACT_FILE_NAMES:
            offenders.append(path.relative_to(ROOT).as_posix())

    if offenders:
        raise SystemExit("Build/cache artifacts remain:\n" + "\n".join(offenders))


def main() -> int:
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
