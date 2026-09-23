"""记忆能力通过原生 LangChain 工具暴露，作用域由运行上下文决定。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from langchain.tools import ToolRuntime, tool
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool

from .privacy import contains_secret
from .records import MemoryScope, MemorySource

if TYPE_CHECKING:
    from ..application import SayacodeApp


_MAX_LIST_RESULTS = 20
_MAX_LIST_BYTES = 12 * 1024
_MAX_DETAIL_BYTES = 8 * 1024
_MAX_QUERY_BYTES = 1024
_MAX_PROPOSAL_BYTES = 4 * 1024
def _text_chunk(value: str, *, offset: int = 0, max_bytes: int) -> tuple[str, int, bool]:
    """按 UTF-8 字节限定预览，同时返回下一段的字符位置。"""

    preview = value[offset:].encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
    next_offset = offset + len(preview)
    return preview, next_offset, next_offset < len(value)


def _json_size(value: dict[str, Any]) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))


def memory_tools(app: SayacodeApp) -> list[BaseTool]:
    """检索是只读工具；模型写入仅能产生可审查候选。"""

    @tool
    async def search_memory(
        runtime: ToolRuntime[Any],
        query: str = "",
        memory_id: str = "",
        limit: int = 8,
        text_offset: int = 0,
    ) -> dict[str, Any]:
        """检索当前用户和项目的长期记忆；用 memory_id 和 text_offset 分段查看详情。"""
        context = runtime.context
        if (
            not app.config.memory.enabled
            or not context.memory_use_enabled
            or not context.memory_owner_id
            or not context.memory_project_id
        ):
            return {"ok": False, "error": "当前会话未启用记忆读取"}
        if text_offset < 0 or (text_offset and not memory_id):
            return {"ok": False, "error": "文本位置无效；请先指定 memory_id"}
        if len(query.encode("utf-8")) > _MAX_QUERY_BYTES:
            return {"ok": False, "error": "检索词过长，请缩小范围"}
        if not 1 <= limit <= 50:
            return {"ok": False, "error": "结果数量须在 1 至 50 之间"}
        scopes = (
            MemoryScope("user", context.memory_owner_id),
            MemoryScope("project", context.memory_project_id),
        )
        if memory_id:
            for scope in scopes:
                record = await app.memory.repository.aget(scope, memory_id)
                if record is None:
                    continue
                match = await app.memory.retriever._classify(record, context.workspace)
                if not match.current:
                    return {
                        "ok": False,
                        "memory_id": memory_id,
                        "state": record.state,
                        "reason": match.reason,
                    }
                body, next_offset, has_more = _text_chunk(
                    record.text, offset=text_offset, max_bytes=_MAX_DETAIL_BYTES
                )
                subject, _, subject_more = _text_chunk(record.subject, max_bytes=256)
                return {
                    "ok": True,
                    "memory": {
                        "id": record.id,
                        "subject": subject,
                        "subject_truncated": subject_more,
                        "scope": record.scope.kind,
                        "text": body,
                        "next_text_offset": next_offset if has_more else None,
                        "source_refs": [
                            _text_chunk(source.ref, max_bytes=128)[0]
                            for source in record.sources[-3:]
                        ],
                    },
                    "current": match.current,
                    "reason": match.reason,
                }
            return {"ok": False, "error": "未找到此用户或项目的记忆"}
        requested = min(limit, _MAX_LIST_RESULTS)
        results = await app.memory.retriever.search(
            scopes, context.workspace, query=query, limit=requested + 1
        )
        payload: dict[str, Any] = {
            "ok": True,
            "retrieval": "basic",
            "results": [],
            "truncated": len(results) > requested,
            "limit_applied": requested,
        }
        for item in results[:requested]:
            subject, _, subject_more = _text_chunk(item.record.subject, max_bytes=256)
            body, _, body_more = _text_chunk(item.record.text, max_bytes=768)
            row = {
                "id": item.record.id,
                "subject": subject,
                "subject_truncated": subject_more,
                "scope": item.record.scope.kind,
                "text_preview": body,
                "text_truncated": body_more,
                "source_refs": [
                    _text_chunk(source.ref, max_bytes=128)[0]
                    for source in item.record.sources[-2:]
                ],
            }
            payload["results"].append(row)
            if _json_size(payload) > _MAX_LIST_BYTES:
                payload["results"].pop()
                payload["truncated"] = True
                break
        return payload

    @tool
    async def propose_memory(
        subject: str,
        text: str,
        evidence_quote: str,
        runtime: ToolRuntime[Any],
        scope: str = "project",
    ) -> dict[str, Any]:
        """引用当前用户原话提出长期记忆候选；仅显式学习档可调用。"""
        context = runtime.context
        if not app.config.memory.enabled or context.memory_learning_mode != "explicit":
            return {"accepted": False, "reason": "当前会话未启用记忆学习"}
        if context.trust_level == "read_only" or context.is_background:
            return {"accepted": False, "reason": "当前会话不允许持久记忆提案"}
        if context.task_id is not None:
            return {"accepted": False, "reason": "子 Agent 请向父 Agent 报告发现"}
        if not context.memory_owner_id or not context.memory_project_id:
            return {"accepted": False, "reason": "缺少当前用户或项目身份"}
        if not runtime.tool_call_id:
            return {"accepted": False, "reason": "缺少原生工具调用身份"}
        if scope not in {"user", "project"}:
            return {"accepted": False, "reason": "记忆范围须为 user 或 project"}
        if not subject.strip() or not text.strip() or not evidence_quote.strip():
            return {"accepted": False, "reason": "主题、正文和用户原话引用均不能为空"}
        if len(f"{subject}\n{text}".encode("utf-8")) > _MAX_PROPOSAL_BYTES:
            return {"accepted": False, "reason": "候选记忆过长，请只保留可复用的结论"}
        configured_keys = tuple(
            profile.api_key for profile in app.config.profiles.values() if profile.api_key
        )
        if app.profile_override is not None and app.profile_override.api_key:
            configured_keys = (*configured_keys, app.profile_override.api_key)
        if contains_secret(f"{subject}\n{text}", configured_keys):
            return {"accepted": False, "reason": "候选记忆包含疑似凭据"}
        state = runtime.state if isinstance(runtime.state, Mapping) else {}
        marker = state.get("memory_turn") if isinstance(state, Mapping) else None
        if not isinstance(marker, Mapping) or not marker.get("message_id"):
            return {"accepted": False, "reason": "缺少真实用户轮次来源"}
        if (
            marker.get("thread_id") != context.session_id
            or marker.get("owner_id") != context.memory_owner_id
            or marker.get("project_id") != context.memory_project_id
            or not marker.get("received_at")
        ):
            return {"accepted": False, "reason": "用户轮次身份与当前运行不一致"}
        human_id = str(marker["message_id"])
        human = next(
            (
                message
                for message in state.get("messages", ())
                if isinstance(message, HumanMessage)
                and str(message.id or "") == human_id
                and not message.additional_kwargs.get("sayacode_source")
            ),
            None,
        )
        if human is None:
            return {"accepted": False, "reason": "用户来源无法在当前图状态核对"}
        quote = evidence_quote.strip()
        if len(quote.encode("utf-8")) > _MAX_QUERY_BYTES:
            return {"accepted": False, "reason": "所引用户原话过长，请选取有关片段"}
        if quote.casefold() not in human.text.casefold():
            return {"accepted": False, "reason": "所引用户原话不在当前轮次中"}
        if contains_secret(quote, configured_keys):
            return {"accepted": False, "reason": "所引用户原话包含疑似凭据"}
        source = MemorySource(
            ref=f"proposal:{context.session_id}:{human_id}:{runtime.tool_call_id}",
            kind="user",
            order=f"{marker['received_at']}:{runtime.tool_call_id}",
            thread_id=context.session_id,
            user_message_id=human_id,
            project_id=context.memory_project_id,
            evidence_refs=(human_id,),
        )
        return await app.memory.propose(
            subject=subject, text=text, scope=scope, source=source
        )

    return [search_memory, propose_memory]


__all__ = ["memory_tools"]
