"""容器内调用现有应用；不实现新的模型或工具执行循环。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from time import perf_counter
from typing import Any

from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from sayacode.agent import AgentRuntime
from sayacode.application import SayacodeApp
from sayacode.cli.events import _redact
from sayacode.config import Config, ConfigRepository, Profile
from sayacode.paths import AppPaths


def workspace_digest() -> str:
    """只观察试验文件，用于发现要求等待实施时的提前写入。"""
    checksum = hashlib.sha256()
    for path in sorted(Path("/workspace").rglob("*")):
        if {".git", "__pycache__"}.intersection(path.parts):
            continue
        if path.is_symlink():
            payload = str(path.readlink()).encode()
        elif path.is_file():
            payload = path.read_bytes()
        else:
            continue
        checksum.update(path.relative_to("/workspace").as_posix().encode())
        checksum.update(payload)
    return checksum.hexdigest()


async def recover_context(path: Path) -> dict[str, Any]:
    """只读取已保存的原生检查点，强制停止后也不伪造摘要是否发生。"""
    if not path.is_file():
        return {"context_recovered": False}
    identifiers: set[str] = set()
    count = 0
    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        config: RunnableConfig = {"configurable": {"thread_id": "eval-main"}}
        latest = await saver.aget_tuple(config)
        if latest is None:
            return {"context_recovered": False}
        async for item in saver.alist(config):
            count += 1
            for message in item.checkpoint.get("channel_values", {}).get("messages", []):
                if message.additional_kwargs.get("lc_source") == "summarization":
                    identifiers.add(
                        message.id or hashlib.sha256(str(message.content).encode()).hexdigest()
                    )
        messages = latest.checkpoint.get("channel_values", {}).get("messages", [])
        return {
            "context_recovered": True,
            "summary_count": len(identifiers),
            "history_checkpoints": count,
            "last_graph_tokens_approx": count_tokens_approximately(
                messages, use_usage_metadata_scaling=False
            ),
        }


def usage_from_audit(path: Path) -> dict[str, int]:
    """统计所有线程的原生调用，缺失用量不能补造为完整的零。"""
    observed: set[str] = set()
    reported: set[str] = set()
    usages: dict[str, dict[str, int]] = {}
    caches: dict[str, int] = {}
    damaged = False
    fields = ("input_tokens", "output_tokens", "total_tokens")
    if path.is_file():
        with path.open("rb") as stream:
            for line in stream:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line.decode("utf-8-sig"))
                except (UnicodeError, ValueError):
                    # 强制停止可能留下半行；保留此前已落盘的计量并标注不完整。
                    damaged = True
                    continue
                if not isinstance(event, dict):
                    damaged = True
                    continue
                kind = event.get("event")
                if kind not in {"model.started", "model.completed", "model.failed"}:
                    continue
                identifier = event.get("run_id")
                if not isinstance(identifier, str) or not identifier:
                    damaged = True
                    continue
                observed.add(identifier)
                details = event.get("details")
                usage = details.get("usage") if isinstance(details, dict) else None
                if kind != "model.completed" or not isinstance(usage, dict):
                    continue
                numeric = {
                    key: value
                    for key in fields
                    if isinstance(value := usage.get(key), int)
                    and not isinstance(value, bool)
                    and value >= 0
                }
                # 重复回调不累计两次；完整记录不被后续的不完整记录覆盖。
                if identifier not in reported:
                    usages[identifier] = numeric
                if len(numeric) == len(fields):
                    usages[identifier] = numeric
                    reported.add(identifier)
                cache = usage.get("cache_read_tokens")
                if isinstance(cache, int) and not isinstance(cache, bool) and cache >= 0:
                    caches[identifier] = cache
    return {
        **{key: sum(row.get(key, 0) for row in usages.values()) for key in fields},
        # 缓存命中只作为成本解释的附加观测量，不参与完整性判定。
        "cache_read_tokens": sum(caches.values()),
        "cache_reported_calls": len(caches),
        "model_calls": len(observed),
        "reported_calls": len(reported),
        "missing_usage_calls": max(len(observed - reported), int(damaged)),
    }


def turn_status(final: dict[str, Any], tasks: list[dict[str, Any]]) -> str:
    """父线程的显式轮次和结果唤醒共同决定本轮是否完成。"""
    outcomes = {
        "run.completed": "completed",
        "run.paused": "paused",
        "agent.wake.completed": "completed",
        "agent.wake.paused": "paused",
    }
    statuses = [outcomes.get(final.get("type", ""), "failed")]
    for task in tasks:
        wake = task.get("parent_wake")
        if isinstance(wake, dict):
            statuses.append(outcomes.get(wake.get("type", ""), "failed"))
    if "failed" in statuses:
        return "failed"
    return "paused" if "paused" in statuses else "completed"


async def run(input_path: Path = Path("/state/input.json")) -> dict[str, Any]:
    """独立状态目录运行脚本用户轮次，并等待整个任务族收尾。"""
    spec = json.loads(input_path.read_text(encoding="utf-8"))
    paths = AppPaths.resolve(input_path.parent / "agent")
    repository = ConfigRepository(paths.home)
    profile = Profile.from_dict(spec["profile"])
    config = Config(
        default_profile=profile.name,
        default_trust="full",
        profiles={profile.name: profile},
        preferences={"language": "zh", "shutdown_grace_seconds": "5"},
    )
    await repository.save(config)
    runtime = await AgentRuntime.open(paths.home)
    app = SayacodeApp(
        paths=paths,
        repository=repository,
        config=config,
        runtime=runtime,
        workspace=Path("/workspace"),
        session_id="eval-main",
        trust_level="full",
        profile_name=profile.name,
        headless=True,
        team_enabled=spec["team_enabled"],
    )
    start = perf_counter()
    responses: list[str] = []
    turns: list[dict[str, Any]] = []
    context_history: list[dict[str, Any]] = []
    summaries: set[str] = set()
    initial_workspace = workspace_digest()
    events_path = input_path.parent / "events.jsonl"
    try:
        await app.initialize()
        for turn_index, prompt in enumerate([spec["prompt"], *spec.get("followups", [])]):
            final: dict[str, Any] = {}
            with events_path.open("a", encoding="utf-8") as stream:
                async for event in app.stream(prompt, input_format="headless"):
                    stream.write(json.dumps(_redact(event), ensure_ascii=False, default=str) + "\n")
                    stream.flush()
                    if event.get("type") in {
                        "run.completed",
                        "run.failed",
                        "run.paused",
                        "run.stopped",
                    }:
                        final = event
            tasks = await app.wait_for_tasks()
            handle, _ = await app._context_for_thread(app.session_id)
            snapshot = await app.runtime.get_state(handle, app.session_id)
            messages = snapshot.values.get("messages", [])
            for message in messages:
                if message.additional_kwargs.get("lc_source") == "summarization":
                    summaries.add(
                        message.id or hashlib.sha256(str(message.content).encode()).hexdigest()
                    )
            context_history.append(
                {
                    "turn": turn_index + 1,
                    "messages": len(messages),
                    "graph_tokens_approx": count_tokens_approximately(messages),
                    "summary_count": len(summaries),
                    "workspace_changed": workspace_digest() != initial_workspace,
                }
            )
            status = turn_status(final, tasks)
            turns.append({"final": final, "tasks": tasks, "status": status})
            responses.append(str(final.get("response") or ""))
            for task in tasks:
                if isinstance(task.get("parent_wake"), dict):
                    responses.append(str(task["parent_wake"].get("response") or ""))
            if status != "completed":
                break
        records = [
            record for record in await app.tasks.list() if record.parent_thread_id == app.session_id
        ]
        snapshots: dict[str, Any] = {}
        for tid in [app.session_id, *(record.thread_id for record in records)]:
            handle, _ = await app._context_for_thread(tid)
            state = await app.runtime.get_state(handle, tid)
            snapshots[tid] = {
                "next": list(state.next),
                "messages": [
                    message.model_dump(mode="json") for message in state.values.get("messages", [])
                ],
                "todos": state.values.get("todos", []),
            }
        (input_path.parent / "snapshots.json").write_text(
            json.dumps(_redact(snapshots), ensure_ascii=False, default=str, indent=2),
            encoding="utf-8",
        )
        status = turns[-1]["status"] if turns else "failed"
        if status == "completed" and any(
            record.status in {"paused", "running", "stopping", "pending"} for record in records
        ):
            status = "paused" if any(record.status == "paused" for record in records) else "failed"
        # 子任务终态失败可被父线程纠正，最终能力仍由外部验收判断。
        result = {
            "status": status,
            "response": "\n\n".join(responses),
            "turns": turns,
            "task_count": len(records),
            "child_roles": [record.role for record in records],
            "context_history": context_history,
            "summary_count": len(summaries),
            "failed_child_tasks": sum(record.status == "failed" for record in records),
        }
    except Exception as exc:
        result = {
            "status": "failed",
            "error_type": type(exc).__name__,
            "response": "\n\n".join(responses),
            "turns": turns,
            "task_count": 0,
        }
    finally:
        await app.aclose()
    result.update(
        duration_seconds=round(perf_counter() - start, 3), usage=usage_from_audit(paths.audit)
    )
    (input_path.parent / "worker-result.json").write_text(
        json.dumps(_redact(result), ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return result


if __name__ == "__main__":
    asyncio.run(run())
