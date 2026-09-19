"""压缩策略模块：预算阈值、三层压缩与语义摘要。

本模块以混入类提供能力，操作宿主的消息列表。
构造消息体时延迟导入，避免循环依赖。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from .session_state import _SessionState



# 最近若干轮完整保留，中间若干轮逐轮摘要
_KEEP_FULL_ROUNDS = 10
_SUMMARIZE_ROUNDS = 20

# 触发比例与输出保留
_PREVENTIVE_RATIO = 0.70
_CONTEXT_BUDGET_RATIO = 0.80
_URGENT_RATIO = 0.90
_OUTPUT_RESERVE_RATIO = 0.15
_SYSTEM_OVERHEAD_ESTIMATE = 8700

# 未知上下文窗口保持零，不用默认值冒充模型能力
_DEFAULT_CONTEXT_LIMIT = 0


def _budgets_for_limit(limit: int) -> Tuple[int, int]:
    """由上下文窗口推导标准预算与输出保留量，未知窗口均为零。"""
    limit = int(limit or 0)
    if limit <= 0:
        return 0, 0
    return int(limit * _CONTEXT_BUDGET_RATIO), int(limit * _OUTPUT_RESERVE_RATIO)


# 大模型语义摘要提示词模板
_COMPACT_SUMMARY_PROMPT = """You are a conversation compression engine. Compress the following conversation history into a structured summary that preserves ALL critical information for seamless continuation.

Preserve these elements explicitly:
1. **User's goals and intent** — what the user is trying to build or achieve
2. **Technologies and files involved** — specific file paths, frameworks, tools mentioned
3. **Errors encountered** — exact error messages, stack traces, problematic behaviors
4. **Solutions attempted and results** — what was tried and whether it worked
5. **User's explicit instructions** — verbatim commands, style preferences, constraints, requirements
6. **Decisions and rationale** — architectural choices and WHY they were made
7. **Pending or unresolved work** — what needs to be done next
8. **Current project state** — where things stand now (files modified, features working)
9. **Immediate next action** — the single most important next step

CRITICAL: Preserve exact file paths, error messages, and user commands verbatim when they are important.
Omit: social pleasantries, redundant explanations, resolved trivial issues.

Conversation history:
{conversation_text}

Structured Summary:"""


class SessionCompactMixin(_SessionState):
    """压缩能力混入，宿主状态见 _SessionState。"""

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """估算文本 token 数，约三字符折一。"""
        if not text:
            return 0
        return max(1, len(text) // 3)

    def _count_message_tokens(self, message) -> int:
        """估算单条消息 token 数，含结构开销。"""
        return self.estimate_tokens(message.content) + 4

    def _estimate_overhead_tokens(self) -> int:
        """估算系统固定开销，另加实际系统消息贡献。"""
        overhead = _SYSTEM_OVERHEAD_ESTIMATE
        for msg in self.messages:
            if msg.role == "system":
                overhead += self._count_message_tokens(msg)
        return overhead

    def _rebuild_token_count(self):
        """从当前消息列表重算运行中 token 数。"""
        self._running_tokens = self._estimate_overhead_tokens()
        for msg in self.messages:
            if msg.role != "system":
                self._running_tokens += self._count_message_tokens(msg)

    def _over_budget(self, ratio: float) -> bool:
        """运行中 token 含输出保留是否超过按比例折算的预算。"""
        if self.model_context_limit <= 0:
            return False
        return (self._running_tokens + self.output_reserve) > int(self.model_context_limit * ratio)

    @property
    def usage_ratio(self) -> float:
        """当前上下文使用比例，零到一之间。"""
        if self.model_context_limit <= 0:
            return 0.0
        return min(1.0, self._running_tokens / self.model_context_limit)

    @property
    def needs_compact(self) -> bool:
        """是否达到标准压缩阈值。"""
        return self._over_budget(_CONTEXT_BUDGET_RATIO)

    @property
    def needs_preventive_compact(self) -> bool:
        """是否达到预防性压缩阈值，触发轻度压缩。"""
        return self._over_budget(_PREVENTIVE_RATIO)

    @property
    def needs_urgent_compact(self) -> bool:
        """是否达到紧急压缩阈值，触发激进压缩。"""
        return self._over_budget(_URGENT_RATIO)

    def set_compact_fn(self, fn: Optional[Callable[[List[Dict[str, str]]], str]], strategy: Optional[str] = None):
        """设置语义压缩模型回调，可覆盖压缩策略。"""
        self._compact_fn = fn
        if strategy:
            self._compact_strategy = strategy

    def set_context_limit(self, limit: int):
        """动态调整模型上下文窗口并重算预算。"""
        limit = int(limit or 0)
        self.model_context_limit = limit
        self.context_budget, self.output_reserve = _budgets_for_limit(limit)
        self._rebuild_token_count()

    def maybe_compact(self) -> bool:
        """按分层阈值触发压缩，返回是否执行。"""
        if not self.enable_summary:
            return False

        if self.needs_urgent_compact:
            self._auto_compact(urgent=True)
            return True
        if self.needs_compact:
            self._auto_compact()
            return True
        if self.needs_preventive_compact:
            self._auto_compact(gentle=True)
            return True
        return False

    def compact(self, focus: Optional[str] = None) -> str:
        """手动压缩，供压缩命令调用。"""
        if len(self.messages) <= 1:
            return "会话太短，无需压缩"

        self._auto_compact(focus=focus)
        return self.summary or "上下文已压缩"

    def force_compact(self, reason: str = "prompt_too_long") -> str:
        """紧急压缩，跳过阈值更激进，开关关闭也执行。"""
        if len(self.messages) <= 1:
            return "会话太短，无法紧急压缩"

        self._auto_compact(urgent=True, focus=reason)
        return (
            f"紧急压缩完成 (原因: {reason}): "
            f"保留最近 {_KEEP_FULL_ROUNDS // 2} 轮, "
            f"使用率 {self.usage_ratio:.0%}"
        )

    def _auto_compact(self, focus: Optional[str] = None, gentle: bool = False, urgent: bool = False):
        """三层压缩核心，旧轮摘要加最近轮保留加计数重算。"""
        from .session_messages import Message

        if not self.enable_summary and not urgent:
            return

        if urgent:
            keep_rounds = max(2, _KEEP_FULL_ROUNDS // 2)
        elif gentle:
            keep_rounds = _KEEP_FULL_ROUNDS + 5
        else:
            keep_rounds = _KEEP_FULL_ROUNDS

        # 压缩前存档完整历史
        archive_path = self._archive_history()

        system_msgs, rounds = self._identify_rounds(self.messages)

        # 可压缩轮次排除纯压缩与系统轮
        compressible = [r for r in rounds if r["type"] == "round"]
        total_compressible = len(compressible)

        if total_compressible <= keep_rounds:
            return

        keep_rounds_count = min(keep_rounds, total_compressible)
        compress_count = total_compressible - keep_rounds_count

        new_messages: List[Message] = list(system_msgs)

        # 边界标记记录压缩事件，附存档路径
        boundary_parts = [f"── 上下文压缩 ({self._compact_count + 1}) @ {datetime.now(timezone.utc).isoformat()} ──"]
        if archive_path:
            boundary_parts.append(f"完整历史存档: {archive_path}")
        boundary = Message(
            role="system",
            content=" | ".join(boundary_parts),
            metadata={
                "compressed": True, "type": "boundary", "tier": 0,
                "archive": archive_path or "",
            },
        )
        new_messages.append(boundary)

        old_rounds = compressible[:compress_count]

        if (self._compact_fn and self._compact_strategy == "semantic"):
            summary = self._generate_semantic_summary(old_rounds, focus)
        else:
            summary = self._summarize_rounds_bulk(old_rounds)

        if archive_path:
            summary += f"\n\n完整历史存档: {archive_path}"

        new_messages.append(Message(
            role="system",
            content=summary,
            metadata={
                "compressed": True,
                "tier": "semantic" if (self._compact_fn and self._compact_strategy == "semantic") else 3,
                "focus": focus or "",
                "num_rounds": len(old_rounds),
                "archive": archive_path or "",
            },
        ))

        # 最近若干轮完整保留
        for r in compressible[-keep_rounds_count:]:
            if r.get("user"):
                new_messages.append(r["user"])
            if r.get("assistant"):
                new_messages.append(r["assistant"])

        # 之前已压缩消息一并保留
        for r in rounds:
            if r["type"] == "compressed":
                if r["message"].metadata.get("type") != "boundary":
                    new_messages.append(r["message"])

        self.messages = new_messages
        self._compact_count += 1
        self._last_compact_time = datetime.now(timezone.utc).isoformat()

        self._rebuild_token_count()

        self.summary = (
            f"上下文已压缩 (第 {self._compact_count} 次): "
            f"保留最近 {keep_rounds_count} 轮完整对话 + "
            f"{len(old_rounds)} 轮{'语义摘要' if self._compact_fn and self._compact_strategy == 'semantic' else '要点'} | "
            f"使用率 {self.usage_ratio:.0%}"
        )

    def _generate_semantic_summary(self, rounds: list, focus: Optional[str] = None) -> str:
        """用模型生成结构化语义摘要，失败回退静态截断。"""
        lines = []
        for r in rounds:
            user_msg = r.get("user")
            asst_msg = r.get("assistant")
            if user_msg:
                content = user_msg.content
                if len(content) > 1500:
                    content = content[:1500] + f"\n... [truncated, original {len(content)} chars]"
                lines.append(f"User: {content}")
            if asst_msg:
                content = asst_msg.content
                if len(content) > 1500:
                    content = content[:1500] + f"\n... [truncated, original {len(content)} chars]"
                lines.append(f"Assistant: {content}")

        conversation_text = "\n\n".join(lines)

        # 限制输入长度，防止摘要调用本身溢出
        max_chars = 15000
        if len(conversation_text) > max_chars:
            conversation_text = conversation_text[-max_chars:] + (
                "\n\n[earlier parts truncated]"
            )

        prompt_text = _COMPACT_SUMMARY_PROMPT.format(
            conversation_text=conversation_text
        )
        if focus:
            prompt_text += f"\n\nFocus area: {focus}"

        if not self._compact_fn:
            return self._summarize_rounds_bulk(rounds)

        try:
            summary = self._compact_fn([{"role": "user", "content": prompt_text}])
            if not summary or not summary.strip():
                return self._summarize_rounds_bulk(rounds)
            return summary.strip()
        except Exception as e:
            return f"[Semantic summary fallback: {e}]\n\n" + self._summarize_rounds_bulk(rounds)

    @staticmethod
    def _summarize_round(round_data: dict) -> str:
        """单轮对话压缩为一两行摘要。"""
        user_msg = round_data.get("user")
        assistant_msg = round_data.get("assistant")

        user_content = user_msg.content if user_msg else "[未知用户消息]"
        user_summary = user_content[:200].replace("\n", " ").strip()
        if len(user_content) > 200:
            user_summary += "..."

        if not assistant_msg:
            return f"[用户] {user_summary} → [待回复]"

        assistant_content = assistant_msg.content
        assistant_summary = assistant_content[:200].replace("\n", " ").strip()
        if len(assistant_content) > 200:
            assistant_summary += "..."

        return f"[用户] {user_summary}\n[助手] {assistant_summary}"

    @staticmethod
    def _summarize_rounds_bulk(rounds_list: list) -> str:
        """多轮早期对话合并为要点摘要。"""
        parts = [f"--- 早期对话摘要 ({len(rounds_list)} 轮) ---"]
        for i, r in enumerate(rounds_list, 1):
            if r["type"] == "compressed":
                parts.append(r["message"].content)
                continue
            user_msg = r.get("user")
            if user_msg:
                preview = user_msg.content[:120].replace("\n", " ").strip()
                if len(user_msg.content) > 120:
                    preview += "..."
                parts.append(f"{i}. {preview}")
            assistant_msg = r.get("assistant")
            if assistant_msg:
                preview = assistant_msg.content[:120].replace("\n", " ").strip()
                if len(assistant_msg.content) > 120:
                    preview += "..."
                parts.append(f"   回应: {preview}")
        return "\n".join(parts)

    @staticmethod
    def _identify_rounds(messages: list) -> tuple:
        """消息列表分离为系统消息与问答轮次。"""
        system_msgs: list = []
        rounds: list = []
        current_user = None

        for msg in messages:
            if msg.metadata.get("compressed"):
                rounds.append({"type": "compressed", "message": msg})
            elif msg.role == "system":
                system_msgs.append(msg)
            elif msg.role == "user":
                if current_user is not None:
                    rounds.append({"type": "round", "user": current_user, "assistant": None})
                current_user = msg
            elif msg.role == "assistant":
                if current_user is not None:
                    rounds.append({"type": "round", "user": current_user, "assistant": msg})
                    current_user = None
                else:
                    rounds.append({"type": "round", "user": None, "assistant": msg})

        if current_user is not None:
            rounds.append({"type": "round", "user": current_user, "assistant": None})

        return system_msgs, rounds

    def get_compact_info(self) -> Dict[str, Any]:
        """获取压缩状态详情。"""
        return {
            "compact_count": self._compact_count,
            "last_compact_time": self._last_compact_time,
            "running_tokens": self._running_tokens,
            "context_budget": self.context_budget,
            "model_context_limit": self.model_context_limit,
            "context_limit_known": self.model_context_limit > 0,
            "usage_ratio": self.usage_ratio,
            "strategy": self._compact_strategy,
            "needs_compact": self.needs_compact,
        }
