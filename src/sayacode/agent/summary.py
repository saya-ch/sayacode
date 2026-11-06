"""摘要降级：摘要不可用时保留原任务与最近消息，不写入占位摘要。

官方 ``SummarizationMiddleware`` 在待摘要消息被裁剪为空时返回
"Previous conversation was too long to summarize."，并把它当成摘要写回上下文，
真实历史因此被一句占位替换。摘要模型返回空文本时也会写入空摘要。
这里在生成摘要处拦截这两种结果，改为保留首条用户任务与最近 N 条消息，
删除中间消息，并插入一条带 ``sayacode_summary_failed`` 标记的说明消息。

**已知限制（2026-11-07 复核，未修）**：降级只保留 ``messages[0]``，但**首次成功
摘要之后**，``messages[0]`` 已经是摘要消息（``lc_source=summarization``）而不是
原始任务。此时降级只能保住那段摘要，无法恢复原任务 —— 实测 ``summary-pressure``
配置下模型明确回复「The original task is not recoverable from the retained context」，
该题 reward 从 1.0 掉到 0.0（``docs/SUMMARY-FALLBACK-CHECK.md`` §4.1）。
根治需要在首次摘要时把原始任务文本固定存进 state，或让它不参与摘要；这两条都要
改官方中间件的消息构造，风险高于收益，因此暂缓。

当前评测配置（context 192k / ratio 0.8 / keep 12 → 触发线约 **150,400** tokens）
下摘要**从未触发**（40 题单次输入峰值 102k–127k），该限制不影响现有结果；
它只会在长轨迹任务上显现。"""

from __future__ import annotations

from typing import Any, cast

from langchain.agents.middleware import SummarizationMiddleware
from langchain.agents.middleware.types import AgentState
from langchain_core.messages import AnyMessage, HumanMessage, RemoveMessage
from langgraph.runtime import Runtime

_FAILURE_MARKERS = ("too long to summarize",)
_SUMMARY_FAILED_KEY = "sayacode_summary_failed"
_FALLBACK_KEEP_MESSAGES = 12
_FALLBACK_NOTICE = (
    "Earlier conversation was not summarized because no usable summary was produced. "
    "The original task message and the most recent messages are kept."
)


class SummaryUnavailable(RuntimeError):
    """摘要调用没有产出可用摘要，调用方需要降级而不是写入占位。"""


class ResilientSummarizationMiddleware(SummarizationMiddleware[Any, Any]):
    """摘要失败时降级保留原文并标记，不把占位或空文本当摘要。

    参数与返回同官方中间件。
    调用约束是只在摘要结果为空或含 "too long to summarize" 时降级；
    真正的模型调用异常仍按官方语义向上抛出。
    坑点是降级消息用 HumanMessage 追加，避免在 Anthropic 协议下插入 system 角色。"""

    def before_model(
        self, state: AgentState[Any], runtime: Runtime[Any]
    ) -> dict[str, Any] | None:
        """同步路径的摘要降级包装。"""
        try:
            return super().before_model(state, runtime)
        except SummaryUnavailable as exc:
            return self._degrade(state, exc)

    async def abefore_model(
        self, state: AgentState[Any], runtime: Runtime[Any]
    ) -> dict[str, Any] | None:
        """异步路径的摘要降级包装。"""
        try:
            return await super().abefore_model(state, runtime)
        except SummaryUnavailable as exc:
            return self._degrade(state, exc)

    def _create_summary(self, messages_to_summarize: list[AnyMessage]) -> str:
        return self._require_usable(super()._create_summary(messages_to_summarize))

    async def _acreate_summary(self, messages_to_summarize: list[AnyMessage]) -> str:
        return self._require_usable(await super()._acreate_summary(messages_to_summarize))

    @staticmethod
    def _require_usable(text: str) -> str:
        """拒绝空摘要和官方占位摘要，其余原样返回。"""
        value = (text or "").strip()
        if not value:
            raise SummaryUnavailable("empty_summary")
        if any(marker in value.lower() for marker in _FAILURE_MARKERS):
            raise SummaryUnavailable("too_long_to_summarize")
        return text

    def _keep_message_count(self) -> int:
        """读取保留策略里的消息条数，非消息策略回落默认值。"""
        kind, value = self.keep
        if kind == "messages":
            return max(1, int(value))
        return _FALLBACK_KEEP_MESSAGES

    def _degrade(self, state: AgentState[Any], exc: Exception) -> dict[str, Any] | None:
        """保留原任务与最近消息，删除中间消息并插入失败标记。"""
        messages = list(state.get("messages", []))
        if any(
            isinstance(message, HumanMessage)
            and message.additional_kwargs.get(_SUMMARY_FAILED_KEY)
            for message in messages
        ):
            return None
        cutoff = self._safe_degrade_cutoff(messages, self._keep_message_count())
        updates: list[Any] = []
        if cutoff is not None:
            removable = messages[1:cutoff]
            identifiers = [getattr(message, "id", None) for message in removable]
            if all(identifiers):
                updates.extend(RemoveMessage(id=cast("str", item)) for item in identifiers)
        updates.append(
            HumanMessage(
                content=_FALLBACK_NOTICE,
                additional_kwargs={_SUMMARY_FAILED_KEY: True, "reason": str(exc)},
            )
        )
        return {"messages": updates}

    @staticmethod
    def _safe_degrade_cutoff(messages: list[AnyMessage], keep: int) -> int | None:
        """从目标裁剪点向前找最近的用户消息，保证删除区间以完整回合结束。"""
        if len(messages) <= keep + 1:
            return None
        target = len(messages) - keep
        for index in range(min(target, len(messages) - 1), 0, -1):
            if isinstance(messages[index], HumanMessage):
                return index
        return None


__all__ = ["ResilientSummarizationMiddleware", "SummaryUnavailable"]
