"""冻结输入并在独立容器中评估现有 SAYACODE 产品链路。"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import itertools
import json
import random
import re
import shutil
import subprocess
import threading
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .catalog import ROOT, digest, load_cases, source_digest
from .docker_backend import DockerBackend
from .graders import file_fingerprints, grade_case
from .report import generate_report


class ActiveContainers:
    """只跟踪本次实验创建的容器，Ctrl+C 不枚举或终止其他进程。"""

    def __init__(self) -> None:
        self.stopping = threading.Event()
        self.reason: str | None = None
        self.cleanup_errors: list[str] = []
        self._lock = threading.Lock()
        self._names: set[str] = set()

    def add(self, name: str) -> None:
        with self._lock:
            self._names.add(name)

    def discard(self, name: str) -> None:
        with self._lock:
            self._names.discard(name)

    def stop(self, reason: str = "operator_interrupt") -> None:
        with self._lock:
            if self.reason is not None:
                return
            self.reason = reason
            self.stopping.set()
            names = sorted(self._names)
        if names:
            try:
                ok = docker_backend().stop_many(names, timeout=2)
                if not ok:
                    self.cleanup_errors.append("ContainerStopFailed")
            except Exception as exc:
                # 清理失败不能吞掉中止原因，后续 finally 仍保存报告。
                self.cleanup_errors.append(type(exc).__name__)


def terminal_http_status(path: Path) -> int | None:
    """检查任务族各线程最后一次模型终态，仅返回认证或限流状态码。"""
    if not path.is_file():
        return None
    seen: set[str] = set()
    for line in reversed(path.read_bytes().splitlines()):
        try:
            row = json.loads(line.decode("utf-8-sig"))
        except (ValueError, UnicodeError):
            continue
        thread_id = row.get("thread_id")
        if (
            not isinstance(thread_id, str)
            or thread_id in seen
            or row.get("event")
            not in {
                "model.completed",
                "model.failed",
            }
        ):
            continue
        seen.add(thread_id)
        if row["event"] == "model.completed":
            continue
        code = (row.get("details") or {}).get("status_code")
        if type(code) is int and code in {401, 403, 429}:
            return code
    return None


def write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def archive_source(output: Path) -> str:
    """归档本次运行器、生产代码与题面，排除凭据、用户状态和构建缓存。"""
    project = ROOT.parent
    files = [
        *sorted((project / "src" / "sayacode").rglob("*.py")),
        *sorted(ROOT.glob("*.py")),
        ROOT / "Dockerfile",
        project / "pyproject.toml",
        project / "uv.lock",
    ]
    for folder in ("cases", "fixtures", "scenarios"):
        files.extend(
            path
            for path in (ROOT / folder).rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        )
    path = output / "source.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file in files:
            archive.write(file, file.relative_to(project).as_posix())
    return digest(path)


def command(
    args: list[str], *, input_text: str | None = None, timeout: float = 60, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """只使用参数列表启动命令，不拼接 Shell。"""
    result = subprocess.run(
        args,
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if check and result.returncode:
        raise RuntimeError(
            f"命令失败：{args[0]}，退出码 {result.returncode}：{result.stderr[-2000:]}"
        )
    return result


_BACKEND: DockerBackend | None = None


def _backend_runner(
    args: list[str], *, input_text: str | None = None, timeout: float = 60, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """CLI 回退复用本模块 command，测试仍可 monkeypatch run.command。"""
    return command(args, input_text=input_text, timeout=timeout, check=check)


def docker_backend() -> DockerBackend:
    """惰性创建 Docker 后端；首次 docker 操作时才尝试 SDK 连接。"""
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = DockerBackend(runner=_backend_runner)
    return _BACKEND


def build_image() -> tuple[str, str]:
    """固定镜像身份，镜像只包含生产运行代码和薄的实验入口。"""
    checksum = source_digest()
    tag = f"sayacode-eval:{checksum[:12]}"
    backend = docker_backend()
    backend.info()
    backend.build(
        ROOT.parent,
        ROOT / "Dockerfile",
        tag,
        {"sayacode.eval.source": checksum},
    )
    return tag, backend.inspect_image(tag)


def _container(trial: Path, image: str, identifier: str) -> None:
    backend = docker_backend()
    backend.run_detached(
        name=identifier,
        image=image,
        mounts=[
            (str(trial / "workspace"), "/workspace"),
            (str(trial / "state"), "/state"),
        ],
        env={"HOME": "/state/home"},
        cpus=2,
        memory="2g",
        pids_limit=256,
        cap_drop=["ALL"],
        security_opt=["no-new-privileges"],
        tmpfs={"/tmp": "rw,nosuid,size=256m"},
        read_only=True,
    )
    backend.exec(identifier, ["mkdir", "-p", "/state/home"])
    # Windows bind mount 在 Linux 中可能显示为 root 所有；此信任只存在于一次性容器。
    backend.exec(identifier, ["git", "config", "--global", "--add", "safe.directory", "*"])
    for args in (
        ["git", "init", "-q"],
        ["git", "config", "user.name", "Evaluation"],
        ["git", "config", "user.email", "eval@example.invalid"],
        ["git", "add", "."],
        ["git", "commit", "-qm", "initial fixture"],
    ):
        backend.exec(identifier, args)


def execute_trial(
    case: dict[str, Any],
    variant: str,
    repeat: int,
    experiment: Path,
    profile: dict[str, Any],
    image: str,
    *,
    fixture_only: bool = False,
    active: ActiveContainers | None = None,
    team_enabled: bool | None = None,
) -> dict[str, Any] | None:
    """一次试验一个容器、工作区和状态目录；评分发生在任务族收尾后。"""
    if active is not None and active.stopping.is_set():
        return None
    trial = experiment / "trials" / f"{case['id']}--{variant}--{repeat}"
    trial.mkdir(parents=True, exist_ok=False)
    shutil.copytree(
        ROOT / "fixtures" / case["fixture"],
        trial / "workspace",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"),
    )
    (trial / "state").mkdir()
    write_json(trial / "case.json", case)
    initial = file_fingerprints(trial / "workspace")
    identifier = f"saya-eval-{uuid4().hex[:16]}"
    base = {
        "experiment_id": experiment.name,
        "case_id": case["id"],
        "split": case["split"],
        "category": case["category"],
        "variant": variant,
        "repeat": repeat,
        "case_sha256": case["case_sha256"],
        "human_review_required": bool(case.get("human_review", False)),
        "started_at": datetime.now(UTC).isoformat(),
    }
    outcome: dict[str, Any] = {"status": "infrastructure_error", "task_count": 0}
    checks: list[dict[str, Any]] = []
    start = time.perf_counter()
    try:
        if active is not None:
            active.add(identifier)
        _container(trial, image, identifier)
        if active is not None and active.stopping.is_set():
            raise InterruptedError("实验已请求停止")
        write_json(
            trial / "state" / "input.json",
            {
                "prompt": case["prompt"],
                "followups": case.get("followups", []),
                "profile": profile,
                "variant": variant,
                "team_enabled": variant == "team" if team_enabled is None else team_enabled,
            },
        )
        model_start = time.perf_counter()
        if fixture_only:
            outcome = {"status": "completed", "task_count": 0, "response": ""}
        else:
            try:
                exit_code, worker_output = docker_backend().exec(
                    identifier,
                    ["python", "/opt/evals/worker.py"],
                    timeout=float(case.get("timeout_seconds", 300)),
                )
                (trial / "worker.log").write_text(worker_output, encoding="utf-8")
                output = trial / "state" / "worker-result.json"
                if output.is_file():
                    outcome = json.loads(output.read_text(encoding="utf-8"))
                else:
                    outcome = {
                        "status": "infrastructure_error",
                        "task_count": 0,
                        "error_type": "WorkerResultMissing",
                        "exit_code": exit_code,
                    }
            except subprocess.TimeoutExpired:
                outcome = {"status": "timeout", "task_count": 0}
                # 客户端超时不代表容器进程停止；先终止整容器，不能边写边判分。
                docker_backend().stop(identifier, timeout=2)
        elapsed = time.perf_counter() - model_start

        def execute(args: list[str], code: str | None, timeout: float) -> tuple[int, str]:
            return docker_backend().exec(identifier, args, timeout=timeout, input_text=code)

        if outcome["status"] == "completed":
            checks = grade_case(
                case, trial / "workspace", str(outcome.get("response", "")), execute, initial
            )
            if case.get("read_only_until_turn"):
                early = [
                    row
                    for row in outcome.get("context_history", [])
                    if row["turn"] <= case["read_only_until_turn"] and row["workspace_changed"]
                ]
                checks.append(
                    {
                        "type": "deferred_implementation",
                        "passed": not early,
                        "early_write_turns": [row["turn"] for row in early],
                    }
                )
            if case.get("min_child_tasks") is not None:
                checks.append(
                    {
                        "type": "explicit_collaboration",
                        "passed": outcome.get("task_count", 0) >= case["min_child_tasks"],
                        "observed": outcome.get("task_count", 0),
                    }
                )
            if case.get("max_failed_child_tasks") is not None:
                checks.append(
                    {
                        "type": "child_completion",
                        "passed": outcome.get("failed_child_tasks", 0)
                        <= case["max_failed_child_tasks"],
                    }
                )
            if case.get("required_child_roles"):
                checks.append(
                    {
                        "type": "child_roles",
                        "passed": all(
                            role in outcome.get("child_roles", [])
                            for role in case["required_child_roles"]
                        ),
                    }
                )
        base["duration_seconds"] = round(elapsed, 3)
    except Exception as exc:
        outcome = {
            "status": "infrastructure_error",
            "task_count": 0,
            "error_type": type(exc).__name__,
            "diagnostic": str(exc)[-2000:],
        }
        base["duration_seconds"] = round(time.perf_counter() - start, 3)
    finally:
        try:
            if not docker_backend().remove(identifier, force=True):
                base["cleanup_error"] = "ContainerRemovalFailed"
        except Exception as exc:
            base["cleanup_error"] = type(exc).__name__
        if active is not None:
            active.discard(identifier)
    if active is not None and active.stopping.is_set():
        outcome["experiment_stop_reason"] = active.reason
    automatic_passed = bool(checks) and all(check["passed"] for check in checks)
    if outcome["status"] != "completed":
        automatic_passed = False
    passed = None if base["human_review_required"] and automatic_passed else automatic_passed
    usage = outcome.get("usage")
    if not usage:
        # 杀容器或 worker 失败时仍从已持久化审计收集部分用量。
        from .worker import usage_from_audit

        usage = usage_from_audit(trial / "state" / "agent" / "audit.jsonl")
        if outcome["status"] != "completed":
            usage["missing_usage_calls"] = max(1, usage["missing_usage_calls"])
    if case.get("category") == "context_summary":
        from .worker import recover_context

        try:
            outcome.update(
                asyncio.run(recover_context(trial / "state" / "agent" / "checkpoints.sqlite3"))
            )
        except Exception as exc:
            outcome["context_recovery_error"] = type(exc).__name__
    failure = None if passed is not False else "acceptance_failure"
    if outcome["status"] != "completed":
        failure = outcome["status"]
    result = {
        **base,
        **outcome,
        "usage": usage,
        "checks": checks,
        "automatic_passed": automatic_passed,
        "passed": passed,
        "failure_category": failure,
        "cost": {"amount": None, "currency": "USD", "coverage": "unknown"},
    }
    write_json(trial / "result.json", result)
    if active is not None:
        status = terminal_http_status(trial / "state" / "agent" / "audit.jsonl")
        reason = (
            {401: "model_authentication", 403: "model_forbidden", 429: "model_rate_limit"}.get(
                status
            )
            if status is not None
            else None
        )
        if reason is not None:
            active.stop(reason)
    print(
        f"{case['id']} {variant} #{repeat}: {result['status']} / "
        f"{'待复核' if passed is None else '通过' if passed else '未通过'} "
        f"{base['duration_seconds']:.1f}s",
        flush=True,
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("validate", "build", "run", "check-fixtures"))
    parser.add_argument("--split", choices=("dev", "holdout"), default="dev")
    parser.add_argument("--cases", nargs="*")
    parser.add_argument("--catalog", type=Path, help="独立专题任务清单；默认使用冻结32题")
    parser.add_argument("--profile", type=Path, default=ROOT / "profiles" / "commandcode.json")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--variants", nargs="+", default=["single", "team"])
    parser.add_argument("--variant-profile", action="append", default=[], metavar="组名=配置路径")
    parser.add_argument("--team-variants", nargs="*", default=["team"], help="开放团队工具的组名")
    parser.add_argument("--seed", type=int, default=20261021)
    args = parser.parse_args()
    cases = load_cases(args.catalog)
    if args.action == "validate":
        print(
            json.dumps(
                {
                    "cases": len(cases),
                    "dev": sum(c["split"] == "dev" for c in cases),
                    "holdout": sum(c["split"] == "holdout" for c in cases),
                },
                ensure_ascii=False,
            )
        )
        return
    if args.repeats < 1 or not 1 <= args.concurrency <= 8:
        parser.error("repeats 必须为正；concurrency 必须在 1..8")
    if len(set(args.variants)) != len(args.variants) or any(
        re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,40}", name) is None for name in args.variants
    ):
        parser.error("实验组名必须唯一，且只能含小写字母、数字、下划线和连字符")
    image, image_id = build_image()
    if args.action == "build":
        print(image)
        return
    if args.output is None:
        parser.error("执行实验必须指定仓库外的 --output")
    output = args.output.resolve()
    if output.is_relative_to(ROOT.parent):
        parser.error("原始轨迹与工作区必须存放在仓库外")
    chosen = [
        case
        for case in cases
        if case["split"] == args.split and (not args.cases or case["id"] in args.cases)
    ]
    if not chosen or args.cases and set(args.cases) - {case["id"] for case in chosen}:
        parser.error("任务选择为空，或任务不属于指定 split")
    output.mkdir(parents=True, exist_ok=False)
    profile = json.loads(args.profile.read_text(encoding="utf-8"))
    profiles = {name: dict(profile) for name in args.variants}
    for entry in args.variant_profile:
        name, separator, value = entry.partition("=")
        if not separator or name not in profiles:
            parser.error("--variant-profile 必须是已选择的组名=配置路径")
        profiles[name] = json.loads(Path(value).read_text(encoding="utf-8"))
    from sayacode.config import Profile

    for item in profiles.values():
        Profile.from_dict(item)
    public_profile = {key: value for key, value in profile.items() if key != "api_key"}
    source = source_digest()
    manifest = {
        "created_at": datetime.now(UTC).isoformat(),
        "action": args.action,
        "case_ids": [case["id"] for case in chosen],
        "source_sha256": source,
        "source_archive_sha256": archive_source(output),
        "git_sha": command(["git", "rev-parse", "HEAD"]).stdout.strip(),
        "dirty_paths": command(["git", "status", "--porcelain"]).stdout.splitlines(),
        "image": image,
        "image_id": image_id,
        "model": public_profile,
        "models": {
            name: {key: value for key, value in item.items() if key != "api_key"}
            for name, item in profiles.items()
        },
        "config": {
            "repeats": args.repeats,
            "concurrency": args.concurrency,
            "variants": args.variants,
            "team_variants": args.team_variants,
            "seed": args.seed,
        },
        "case_sha256": {case["id"]: case["case_sha256"] for case in chosen},
        "dataset_freeze_sha256": digest(ROOT / "cases" / "freeze.json"),
        "catalog_sha256": digest(args.catalog) if args.catalog else None,
        "notes": [
            "自构造任务集，非官方基准成绩",
            "模型标识由反代返回，非独立验证上游权重版本",
            "使用声明的 64K 实验上下文预算，并非测量模型最大窗口",
            "未获得可靠计价快照，费用未知；不以 token 数推算订阅扣费",
        ],
    }
    write_json(output / "manifest.json", manifest)
    fixture_only = args.action == "check-fixtures"
    jobs = list(itertools.product(chosen, args.variants, range(1, args.repeats + 1)))
    random.Random(args.seed).shuffle(jobs)
    active = ActiveContainers()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = [
                pool.submit(
                    execute_trial,
                    case,
                    variant,
                    repeat,
                    output,
                    profiles[variant],
                    image,
                    fixture_only=fixture_only,
                    active=active,
                    team_enabled=variant in args.team_variants,
                )
                for case, variant, repeat in jobs
            ]
            try:
                for future in concurrent.futures.as_completed(futures):
                    future.result()
            except KeyboardInterrupt:
                active.stop()
                for future in futures:
                    future.cancel()
            except Exception:
                active.stop("evaluation_error")
                for future in futures:
                    future.cancel()
                raise
    finally:
        if active.reason is not None:
            write_json(
                output / "abort.json",
                {
                    "reason": active.reason,
                    "planned_trials": len(jobs),
                    "completed_records": len(list(output.rglob("result.json"))),
                    "cleanup_errors": active.cleanup_errors,
                },
            )
        generate_report(output, expected_repeats=args.repeats)
    print(f"报告：{output / 'report.md'}")


if __name__ == "__main__":
    main()
