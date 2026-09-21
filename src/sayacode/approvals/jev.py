"""用 Jev 对待审批工具调用做异步预审，并把结果写入图状态。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from importlib import import_module
from typing import Any, Literal, Protocol

from langchain_core.messages import HumanMessage

from ..config import JevConfig

# Jev 是独立审理能力。旧 editable 安装尚未同步依赖时，普通 CLI 仍应启动；
# 真正启用 Jev 时再给出可执行的修复指引。
_typesafe_sdk: Any = None
try:
    _typesafe_sdk = import_module("typesafe_sdk")
except ModuleNotFoundError:
    pass

AsyncTypeSafeClient: Any = getattr(_typesafe_sdk, "AsyncTypeSafeClient", None)
Choice: Any = getattr(_typesafe_sdk, "Choice", None)
RetryPolicy: Any = getattr(_typesafe_sdk, "RetryPolicy", None)

ReviewAction = Literal["allow", "ask", "deny"]
ToolReview = dict[str, Any]

_ALLOW_CONFIDENCE = 0.85
_DENY_CONFIDENCE = 0.90
_BATCH_SIZE = 8
_SECRET_FIELDS = ("api_key", "authorization", "credential", "password", "secret", "token")


def _require_typesafe_sdk() -> None:
    """启用 Jev 前确认官方 SDK 可用，并给旧安装明确修复方式。"""
    if AsyncTypeSafeClient is None or Choice is None or RetryPolicy is None:
        raise RuntimeError(
            "Jev 审理依赖 typesafe-sdk 未安装。请在 SAYACODE 仓库执行 "
            "`uv sync --locked`，或重新安装当前项目。"
        )


class ToolReviewer(Protocol):
    """工具审理后端的最小异步接口，测试可注入本地实现。"""

    async def review(
        self, tool_calls: Sequence[Mapping[str, Any]], state: Mapping[str, Any]
    ) -> dict[str, ToolReview]: ...


def _bounded(value: Any, *, key: str = "", depth: int = 0) -> Any:
    """压缩并脱敏发给外部审理服务的工具参数。"""
    lowered = key.casefold()
    if any(marker in lowered for marker in _SECRET_FIELDS):
        return "<redacted>"
    if depth >= 4:
        return "<truncated>"
    if isinstance(value, Mapping):
        return {
            str(item_key): _bounded(item_value, key=str(item_key), depth=depth + 1)
            for item_key, item_value in list(value.items())[:30]
        }
    if isinstance(value, (list, tuple)):
        return [_bounded(item, depth=depth + 1) for item in value[:20]]
    if isinstance(value, str):
        return value[:2_000] + ("…" if len(value) > 2_000 else "")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:2_000]


def _arguments_complete(value: Any, *, key: str = "", depth: int = 0) -> bool:
    """判断送审投影是否完整；任何截断都会强制转人工。"""
    if any(marker in key.casefold() for marker in _SECRET_FIELDS):
        return False
    if depth >= 4:
        return value is None or isinstance(value, (bool, int, float))
    if isinstance(value, Mapping):
        return len(value) <= 30 and all(
            _arguments_complete(item, key=str(item_key), depth=depth + 1)
            for item_key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return len(value) <= 20 and all(
            _arguments_complete(item, depth=depth + 1) for item in value
        )
    if isinstance(value, str):
        return len(value) <= 2_000
    return True


def _message_text(message: Any) -> str:
    """把消息内容压成供审理用的短文本。"""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(item.get("text") or "") if isinstance(item, Mapping) else str(item)
            for item in content
        )
    return str(content or "")


def _is_user_request(message: Any) -> bool:
    """识别真实用户消息，并排除运行时自动续写指令。"""
    if isinstance(message, HumanMessage):
        metadata = getattr(message, "additional_kwargs", {})
        return not bool(metadata.get("sayacode_continuation")) and metadata.get(
            "sayacode_source"
        ) != "agent_inbox"
    if isinstance(message, Mapping):
        role = str(message.get("role") or message.get("type") or "")
        metadata = message.get("additional_kwargs", {})
        return role in {"user", "human"} and not (
            isinstance(metadata, Mapping)
            and (
                metadata.get("sayacode_continuation")
                or metadata.get("sayacode_source") == "agent_inbox"
            )
        )
    return False


def _review_context(state: Mapping[str, Any]) -> dict[str, Any]:
    """从图状态提取当前用户目标和计划，不复制完整消息历史。"""
    messages = list(state.get("messages", []))
    request = next(
        (
            _message_text(message)
            if not isinstance(message, Mapping)
            else str(message.get("content") or "")
            for message in reversed(messages)
            if _is_user_request(message)
        ),
        "",
    )
    return {
        "user_request": request[:8_000],
        "current_plan": _bounded(state.get("todos", [])),
    }


class JevReviewer:
    """通过 TypeSafe 官方异步 SDK 批量审理工具调用。"""

    def __init__(self, config: JevConfig) -> None:
        self.config = config

    async def review(
        self, tool_calls: Sequence[Mapping[str, Any]], state: Mapping[str, Any]
    ) -> dict[str, ToolReview]:
        """按批次请求 Jev，并把概率分布收敛到允许、询问或拒绝。"""
        _require_typesafe_sdk()
        reviews: dict[str, ToolReview] = {}
        context = _review_context(state)
        for start in range(0, len(tool_calls), _BATCH_SIZE):
            batch = list(tool_calls[start : start + _BATCH_SIZE])
            actions = [
                {
                    "id": str(call.get("id") or f"call-{start + index}"),
                    "tool": str(call.get("name") or "tool"),
                    "arguments": _bounded(call.get("args", {})),
                    "arguments_complete": _arguments_complete(call.get("args", {})),
                }
                for index, call in enumerate(batch)
            ]
            questions = {
                f"action_{index}": Choice(
                    instructions=(
                        f"Which review decision should apply to `actions[{index}]`? "
                        "Judge only the proposed action against `user_request`, `current_plan`, "
                        "and the supplied policy."
                    ),
                    criteria={
                        "allow": (
                            "The action is clearly authorized, scoped to the task, and routine "
                            "or readily reversible."
                        ),
                        "ask": (
                            "Human confirmation is appropriate because authorization, scope, "
                            "sensitivity, external impact, or reversibility is uncertain."
                        ),
                        "deny": (
                            "The action clearly conflicts with the user's request, attempts "
                            "credential exfiltration or security weakening, or is destructive "
                            "beyond the authorized scope."
                        ),
                    },
                )
                for index in range(len(actions))
            }
            client = AsyncTypeSafeClient(
                api_key=self.config.api_key,
                base_url=self.config.base_url,
                model=self.config.model_id,
                timeout=self.config.timeout_seconds,
                retry=RetryPolicy(max_retries=self.config.max_retries),
            )
            async with client:
                response = await client.system_one(
                    state={
                        **context,
                        "policy": (
                            "Approve only clearly authorized low-risk actions. Route uncertainty "
                            "to a human. Deny only clear conflicts or severe abuse."
                        ),
                        "actions": actions,
                    },
                    questions=questions,
                )
            for index, action in enumerate(actions):
                answer = response.choices[f"action_{index}"]
                selected = str(answer.choice)
                confidence = float(answer.confidence)
                if not action["arguments_complete"]:
                    decision: ReviewAction = "ask"
                elif selected == "allow" and confidence >= _ALLOW_CONFIDENCE:
                    decision = "allow"
                elif selected == "deny" and confidence >= _DENY_CONFIDENCE:
                    decision = "deny"
                else:
                    decision = "ask"
                reviews[action["id"]] = {
                    "tool_call_id": action["id"],
                    "tool_name": action["tool"],
                    "arguments_sha256": hashlib.sha256(
                        json.dumps(
                            batch[index].get("args", {}),
                            sort_keys=True,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            default=str,
                        ).encode("utf-8")
                    ).hexdigest(),
                    "action": decision,
                    "suggested_action": selected,
                    "confidence": confidence,
                    "probabilities": dict(answer.probabilities),
                    "model": str(getattr(response, "model", self.config.model_id)),
                    "request_id": getattr(response, "request_id", None),
                    "reason": (
                        "Jev 高置信度判定为可自动执行"
                        if decision == "allow"
                        else "Jev 高置信度判定为拒绝"
                        if decision == "deny"
                        else "Jev 判断需要人工确认、置信度不足或送审参数被截断"
                    ),
                }
        return reviews

    async def test(self) -> dict[str, Any]:
        """发送一次无副作用的审理请求，验证端点、密钥和模型。"""
        result = await self.review(
            [{"id": "connectivity-test", "name": "read_file", "args": {"path": "README.md"}}],
            {
                "messages": [HumanMessage(content="Test the reviewer connection only.")],
                "todos": [],
            },
        )
        return result["connectivity-test"]


__all__ = [
    "JevReviewer",
    "ReviewAction",
    "ToolReview",
    "ToolReviewer",
]
