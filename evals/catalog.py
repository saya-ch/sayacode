"""读取冻结任务与实验配置；不接管 Agent 的运行。"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent


def digest(path: Path) -> str:
    """记录输入字节摘要，任务调整后不能沿用旧实验身份。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_cases(path: Path | None = None) -> list[dict[str, Any]]:
    """校验任务合同和目录边界，不加载任何用户安装配置。"""
    frozen = json.loads((ROOT / "cases" / "freeze.json").read_text(encoding="utf-8"))
    for name, expected in frozen["sha256"].items():
        target = (ROOT / name).resolve()
        if not target.is_relative_to(ROOT) or not target.is_file() or digest(target) != expected:
            raise ValueError(f"冻结任务被修改，必须新建数据集版本：{name}")
    paths = [path] if path else sorted((ROOT / "cases").glob("*.jsonl"))
    found: dict[str, dict[str, Any]] = {}
    for source in paths:
        for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            case = json.loads(line)
            identifier = case.get("id", "")
            if (
                not isinstance(identifier, str)
                or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,80}", identifier) is None
                or identifier in found
            ):
                raise ValueError(f"任务 ID 为空或重复：{source.name}:{number}")
            if case.get("split") not in {"dev", "holdout"}:
                raise ValueError(f"任务 {identifier} 的 split 无效")
            fixture = (ROOT / "fixtures" / case["fixture"]).resolve()
            if not fixture.is_relative_to((ROOT / "fixtures").resolve()) or not fixture.is_dir():
                raise ValueError(f"任务 {identifier} 的初始仓库不存在或越界")
            if not isinstance(case.get("prompt"), str) or not case["prompt"].strip():
                raise ValueError(f"任务 {identifier} 缺少用户输入")
            if not case.get("checks") or not isinstance(case["checks"], list):
                raise ValueError(f"任务 {identifier} 缺少评分合同")
            if any(
                not isinstance(turn, str) or not turn.strip() for turn in case.get("followups", [])
            ):
                raise ValueError(f"任务 {identifier} 的后续输入无效")
            if float(case.get("timeout_seconds", 300)) <= 0:
                raise ValueError(f"任务 {identifier} 的超时无效")
            case["case_sha256"] = hashlib.sha256(
                json.dumps(case, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
            found[identifier] = case
    if not found:
        raise ValueError("没有读取到评估任务")
    return list(found.values())


def source_digest() -> str:
    """补充 Git SHA 无法覆盖的未提交生产代码与评估代码身份。"""
    root = ROOT.parent
    paths = sorted((root / "src" / "sayacode").rglob("*.py"))
    paths += sorted(ROOT.glob("*.py"))
    paths += [root / "pyproject.toml", root / "uv.lock", ROOT / "Dockerfile"]
    checksum = hashlib.sha256()
    for path in paths:
        checksum.update(path.relative_to(root).as_posix().encode())
        checksum.update(path.read_bytes())
    return checksum.hexdigest()
