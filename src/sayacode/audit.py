"""终端产品层的本地追加式审计记录。"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

from langchain_core.callbacks import BaseCallbackHandler

_SECRET = re.compile(r"(?:api[_-]?key|secret|password|credential|authorization|token)$", re.I)


def _redact(value: Any, key: str = "") -> Any:
    if _SECRET.search(key):
        return "***"
    if isinstance(value, dict):
        return {str(name): _redact(item, str(name)) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


class AuditLog:
    """提供可查询的审计投影；真实执行状态仍以 LangGraph 为准。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()

    async def append(
        self,
        event: str,
        *,
        thread_id: str | None = None,
        task_id: str | None = None,
        details: Any = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        row = self._row(event, thread_id=thread_id, task_id=task_id, details=details, run_id=run_id)

        def write() -> None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

        await asyncio.to_thread(write)
        return row

    def append_sync(
        self,
        event: str,
        *,
        thread_id: str | None = None,
        task_id: str | None = None,
        details: Any = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """写入 LangChain 同步回调产生的事件。"""
        row = self._row(event, thread_id=thread_id, task_id=task_id, details=details, run_id=run_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        return row

    @staticmethod
    def _row(
        event: str,
        *,
        thread_id: str | None,
        task_id: str | None,
        details: Any,
        run_id: str | None,
    ) -> dict[str, Any]:
        return {
            "id": uuid4().hex,
            "at": datetime.now(UTC).isoformat(),
            "event": event,
            "thread_id": thread_id,
            "task_id": task_id,
            "run_id": run_id,
            "details": _redact(details),
        }

    async def list(self, *, thread_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if limit <= 0 or not self.path.is_file():
            return []

        def read() -> list[dict[str, Any]]:
            rows: list[dict[str, Any]] = []
            with self.path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if thread_id is None or row.get("thread_id") == thread_id:
                        rows.append(row)
            return rows[-limit:]

        return await asyncio.to_thread(read)


class LangChainAuditCallback(BaseCallbackHandler):
    """将 LangChain 模型及工具生命周期元数据写入本地审计。"""

    raise_error = False

    def __init__(self, audit: AuditLog, *, thread_id: str, task_id: str | None = None) -> None:
        self.audit = audit
        self.thread_id = thread_id
        self.task_id = task_id
        self._started: dict[str, float] = {}

    def _start(
        self, kind: str, run_id: Any, details: dict[str, Any], parent_run_id: Any = None
    ) -> None:
        identifier = str(run_id)
        self._started[identifier] = perf_counter()
        if parent_run_id is not None:
            details = {**details, "parent_run_id": str(parent_run_id)}
        self.audit.append_sync(
            kind + ".started",
            thread_id=self.thread_id,
            task_id=self.task_id,
            run_id=identifier,
            details=details,
        )

    def _finish(self, kind: str, run_id: Any, details: dict[str, Any]) -> None:
        identifier = str(run_id)
        started = self._started.pop(identifier, None)
        if started is not None:
            details = {**details, "duration_ms": round((perf_counter() - started) * 1000)}
        self.audit.append_sync(
            kind + ".completed",
            thread_id=self.thread_id,
            task_id=self.task_id,
            run_id=identifier,
            details=details,
        )

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: Any,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **_: Any,
    ) -> None:
        self._start(
            "model",
            run_id,
            {"name": serialized.get("name") or serialized.get("id")},
            parent_run_id,
        )

    def on_llm_end(self, response: Any, *, run_id: Any, **_: Any) -> None:
        usage: Any = None
        try:
            usage = response.generations[0][0].message.usage_metadata
        except (AttributeError, IndexError, TypeError):
            pass
        self._finish("model", run_id, {"usage": usage})

    def on_llm_error(self, error: BaseException, *, run_id: Any, **_: Any) -> None:
        self._failed("model", run_id, error)

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **_: Any,
    ) -> None:
        self._start(
            "tool",
            run_id,
            {"name": serialized.get("name"), "input_characters": len(input_str)},
            parent_run_id,
        )

    def on_tool_end(self, output: Any, *, run_id: Any, **_: Any) -> None:
        self._finish("tool", run_id, {"output_characters": len(str(output))})

    def on_tool_error(self, error: BaseException, *, run_id: Any, **_: Any) -> None:
        self._failed("tool", run_id, error)

    def _failed(self, kind: str, run_id: Any, error: BaseException) -> None:
        identifier = str(run_id)
        started = self._started.pop(identifier, None)
        details: dict[str, Any] = {"error_type": type(error).__name__, "error": str(error)[:1000]}
        if started is not None:
            details["duration_ms"] = round((perf_counter() - started) * 1000)
        self.audit.append_sync(
            kind + ".failed",
            thread_id=self.thread_id,
            task_id=self.task_id,
            run_id=identifier,
            details=details,
        )


__all__ = ["AuditLog", "LangChainAuditCallback"]
