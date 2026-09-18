"""单个非交互团队 worker 的 mailbox 驱动入口。

负责消费 mailbox 任务并以 headless 方式运行回写结果。
核心函数：execute_mailbox_task、main、build_parser。
调用链：WorkerManager→team_worker→mailbox。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any

from .agent_mailbox import AgentMailbox


_COPIED_CONFIG_FILES = (
    "api_configs.json",
    "user_config.json",
    "permissions.json",
    "hooks.json",
    "trusted_projects.json",
    "mcp_trusted_projects.json",
)
_WORKER_ID_RE = re.compile(r"^w[0-9a-f]{8}$")


def execute_mailbox_task(
    *,
    base_dir: Path,
    worker_id: str,
    workspace: Path,
    mode: str,
) -> dict[str, Any]:
    """消费一个任务，以 headless 方式运行 SAYACODE，并发布其结果。"""
    if not _WORKER_ID_RE.fullmatch(worker_id):
        raise ValueError("invalid worker_id")
    if not workspace.is_dir():
        raise NotADirectoryError(f"workspace is not a directory: {workspace}")
    inbox = AgentMailbox(base_dir, worker_id)
    messages = inbox.read_unread()
    task_message = next(
        (message for message in messages if message.content.get("type") == "task"),
        None,
    )
    if task_message is None:
        return _publish_result(base_dir, worker_id, {
            "ok": False,
            "worker_id": worker_id,
            "error": "worker mailbox contains no task",
            "error_type": "MissingTask",
        })

    task = str(task_message.content.get("task") or "").strip()
    agent_type = str(task_message.content.get("agent_type") or "builder")
    inbox.mark_read(task_message.message_id)
    if not task:
        return _publish_result(base_dir, worker_id, {
            "ok": False,
            "worker_id": worker_id,
            "error": "team task must not be empty",
            "error_type": "ValueError",
        })

    role_prompt = (
        f"你是团队中的 {agent_type} 子 Agent。独立完成以下任务；需要时使用工具验证，"
        "最终只汇报结论、证据和未完成边界。\n\n"
        f"任务：{task}"
    )
    with tempfile.TemporaryDirectory(prefix=f"sayacode-{worker_id}-") as temp_name:
        isolated_home = Path(temp_name)
        for filename in _COPIED_CONFIG_FILES:
            source = base_dir / filename
            if source.is_file():
                shutil.copy2(source, isolated_home / filename)

        from .process_env import build_process_env

        env = build_process_env()
        env["SAYACODE_HOME"] = str(isolated_home)
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "lib",
                "--workspace",
                str(workspace),
                "--mode",
                mode,
                "--new-session",
                "--output-format",
                "json",
                "-p",
                "-",
            ],
            input=role_prompt,
            capture_output=True,
            text=True,
            cwd=str(workspace),
            env=env,
        )

    try:
        payload = json.loads(completed.stdout)
        if not isinstance(payload, dict):
            raise TypeError("headless payload is not an object")
    except (json.JSONDecodeError, TypeError):
        payload = {
            "ok": False,
            "error": "child Agent returned invalid JSON",
            "error_type": "InvalidWorkerOutput",
        }

    result = {
        "ok": bool(completed.returncode == 0 and payload.get("ok")),
        "worker_id": worker_id,
        "response": str(payload.get("response") or ""),
        "error": str(payload.get("error") or completed.stderr.strip()[-4000:]),
        "error_type": str(payload.get("error_type") or ""),
        "model_type": payload.get("model_type"),
        "model_name": payload.get("model_name"),
        "session_id": payload.get("session_id"),
    }
    return _publish_result(base_dir, worker_id, result)


def _publish_result(base_dir: Path, worker_id: str, result: dict[str, Any]) -> dict[str, Any]:
    AgentMailbox(base_dir, "leader").write(
        {"type": "result", **result},
        sender=worker_id,
    )
    return result


def build_parser() -> argparse.ArgumentParser:
    """构建 worker 命令行参数解析器。"""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--base-dir", required=True)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--mode", choices=("build", "plan", "review"), default="build")
    return parser


def main(argv: list[str] | None = None) -> int:
    """解析参数并执行 mailbox 任务。"""
    args = build_parser().parse_args(argv)
    result = execute_mailbox_task(
        base_dir=Path(args.base_dir).expanduser().resolve(),
        worker_id=args.worker_id,
        workspace=Path(args.workspace).expanduser().resolve(),
        mode=args.mode,
    )
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "execute_mailbox_task", "main"]
