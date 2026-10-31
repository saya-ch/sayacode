"""独立验收代码与文件状态，不让 Agent 自己判断是否完成。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable

CommandRunner = Callable[[list[str], str | None, float], tuple[int, str]]


def file_fingerprints(workspace: Path) -> dict[str, str]:
    """忽略运行缓存与 Git 元数据，保留受保护文件的初始指纹。"""
    found: dict[str, str] = {}
    for path in workspace.rglob("*"):
        if {".git", "__pycache__"}.intersection(path.parts):
            continue
        if path.is_symlink():
            # 宿主不解引用 Agent 创建的链接，避免读取宿主同名绝对路径。
            payload = b"symlink:" + str(path.readlink()).encode("utf-8")
        elif path.is_file():
            if not path.resolve().is_relative_to(workspace.resolve()):
                raise ValueError("产物路径越出试验工作区")
            payload = path.read_bytes()
        else:
            continue
        found[path.relative_to(workspace).as_posix()] = hashlib.sha256(payload).hexdigest()
    return found


def grade_case(
    case: dict[str, Any],
    workspace: Path,
    response: str,
    execute: CommandRunner,
    initial: dict[str, str],
) -> list[dict[str, Any]]:
    """代码判分在容器中执行，评分合同留在宿主而不挂入 Agent 工作区。"""
    results: list[dict[str, Any]] = []
    for index, check in enumerate(case["checks"]):
        kind = check["type"]
        item: dict[str, Any] = {"index": index, "type": kind, "passed": False}
        try:
            if kind in {"python", "command"}:
                args = ["python", "-"] if kind == "python" else check["args"]
                code, output = execute(args, check.get("code"), float(check.get("timeout", 30)))
                item.update(
                    passed=code == check.get("expected_exit", 0),
                    exit_code=code,
                    output=output[-8000:],
                )
            elif kind in {"file_contains", "response_contains", "response_excludes"}:
                value = response
                if kind == "file_contains":
                    path = (workspace / check["path"]).resolve()
                    if not path.is_relative_to(workspace.resolve()):
                        raise ValueError("评分文件越界")
                    value = path.read_text(encoding="utf-8")
                missing = [
                    text for text in check["values"] if text.casefold() not in value.casefold()
                ]
                passed = (
                    not missing
                    if kind != "response_excludes"
                    else len(missing) == len(check["values"])
                )
                item.update(passed=passed, missing=missing)
            else:
                raise ValueError(f"未知评分类型：{kind}")
        except Exception as exc:
            item.update(error_type=type(exc).__name__)
        results.append(item)
    current = file_fingerprints(workspace)
    for name in case.get("protected_paths", []):
        results.append(
            {
                "type": "protected_file",
                "path": name,
                "passed": name in initial and current.get(name) == initial[name],
            }
        )
    return results
