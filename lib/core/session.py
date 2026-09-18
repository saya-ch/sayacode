"""
会话管理模块 — 商业级上下文压缩

采用三层压缩管道 + 可选 LLM 语义摘要：
- Tier 1: 最近 N 轮完整保留（默认 10 轮）
- Tier 2: 中间 N 轮压缩为逐轮摘要（默认 20 轮）
- Tier 3: 更早轮次合并为单条批量摘要

核心特性：
- Token 预算驱动触发（非消息数）
- LLM 结构化语义摘要（9 段式）
- Append-only 设计：保留原始消息并标记，不破坏上下文
- 手动 /compact 命令支持
- 边界标记（boundary marker）记录压缩事件
"""

from typing import List, Dict, Any, Optional, Tuple, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path

from .private_io import write_private_json
from ..i18n import tr


def normalize_truncate_target(target) -> int:
    """归一化截断目标为非负整数，非法输入返回 0（原 memory 模块内联，不再双写）。"""
    try:
        return max(0, int(target or 0))
    except (TypeError, ValueError):
        return 0

# ==============================================================================
# 分层压缩常量
# ==============================================================================

_KEEP_FULL_ROUNDS = 10       # 保留 Tier 1：最近 N 轮完整内容。
_SUMMARIZE_ROUNDS = 20       # 压缩 Tier 2：中间 N 轮逐轮摘要。

# 配置 Token 预算。
_PREVENTIVE_RATIO = 0.70           # 达到 70% 时触发预防性压缩
_CONTEXT_BUDGET_RATIO = 0.80       # 达到 80% 时触发标准压缩
_URGENT_RATIO = 0.90               # 达到 90% 时触发紧急压缩（更激进）
_OUTPUT_RESERVE_RATIO = 0.15       # 为模型输出保留 15%
_SYSTEM_OVERHEAD_ESTIMATE = 8700   # 系统提示词 + 工具定义 ≈ 8700 tokens

# 未知上下文窗口保持 0；不要用默认值冒充准确模型能力。
_DEFAULT_CONTEXT_LIMIT = 0
SESSION_SCHEMA_VERSION = 2


def _budgets_for_limit(limit: int) -> Tuple[int, int]:
    """由上下文窗口推导标准预算与输出保留量（未知窗口时均为 0）。"""
    limit = int(limit or 0)
    if limit <= 0:
        return 0, 0
    return int(limit * _CONTEXT_BUDGET_RATIO), int(limit * _OUTPUT_RESERVE_RATIO)

# 定义 LLM 语义摘要提示词模板。
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

# ==============================================================================
# 数据类型
# ==============================================================================


@dataclass
class Message:
    """单条消息数据结构"""
    role: str  # 限定取值为 "user" 或 "assistant"。
    content: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp,
            "metadata": self.metadata
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Message":
        """从字典创建"""
        return cls(
            role=data["role"],
            content=data["content"],
            timestamp=data.get("timestamp", ""),
            metadata=data.get("metadata", {})
        )


def load_legacy_memory_json(json_str: str) -> List[Dict[str, Any]]:
    """只读兼容已落盘的 MemoryManager JSON（不再写该格式）。

    返回可直接喂给 ``messages_from_interaction_dicts`` 的交互字典；
    解析失败或格式不对时返回空列表，不抛异常。
    """
    try:
        data = json.loads(json_str)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    items = data.get("interactions", [])
    if not isinstance(items, list):
        return []
    out: List[Dict[str, Any]] = []
    for entry in items:
        if isinstance(entry, dict):
            out.append(entry)
    return out


class SessionDerivedMemoryView:
    """会话派生的只读记忆视图（替代已删除的 MemoryManager 双写）。

    历史唯一真相源为 session.messages；本视图仅按需派生
    (user, assistant) 轮，供旧调用方只读展示用，不另存第二份历史。
    """

    def __init__(self, session: "SessionManager") -> None:
        """绑定会话实例（不拷贝消息，实时派生）。"""
        self._session = session

    def _pairs(self) -> List[Tuple[str, str]]:
        """由会话派生交互轮（复用 session 唯一真相源）。"""
        session = self._session
        if session is None or not hasattr(session, "derived_interaction_pairs"):
            return []
        try:
            return list(session.derived_interaction_pairs())
        except Exception:
            return []

    def summarize(self) -> str:
        """生成记忆摘要（只读派生，不触碰会话）。"""
        pairs = self._pairs()
        if not pairs:
            return "空记忆 - 暂无对话历史"
        lines = ["## 记忆摘要", "", f"**总交互数**: {len(pairs)}", ""]
        last_user, last_ai = pairs[-1]
        lines.append("**最近活动**:")
        lines.append(f"  用户: {last_user[:50]}{'...' if len(last_user) > 50 else ''}")
        lines.append(f"  助手: {last_ai[:50]}{'...' if len(last_ai) > 50 else ''}")
        return "\n".join(lines)

    def get_recent_context(self, n: int = 10) -> str:
        """获取最近 N 轮交互摘要（只读派生）。"""
        pairs = self._pairs()
        if not pairs:
            return "暂无对话历史"
        recent = pairs[-n:] if len(pairs) >= n else pairs
        lines = [f"## 最近 {len(recent)} 轮对话\n"]
        for i, (user_text, ai_text) in enumerate(recent, 1):
            lines.append(f"### 第 {i} 轮")
            lines.append(f"**用户**: {user_text[:100]}{'...' if len(user_text) > 100 else ''}")
            lines.append(f"**助手**: {ai_text[:100]}{'...' if len(ai_text) > 100 else ''}")
            lines.append("")
        return "\n".join(lines)

    def get_modified_files(self) -> List[str]:
        """派生视图不跟踪文件修改，恒返回空列表（权限语义不动）。"""
        return []

    def get_stats(self) -> Dict[str, Any]:
        """返回派生统计（字段名与旧格式对齐，只读）。"""
        pairs = self._pairs()
        session_id = getattr(self._session, "session_id", "")
        return {
            "session_id": session_id,
            "total_interactions": len(pairs),
            "total_file_modifications": 0,
            "total_tool_uses": 0,
            "unique_tools_used": 0,
            "unique_files_modified": 0,
        }

    def clear(self) -> None:
        """视图无状态可清，保持空操作兼容旧调用。"""
        return None

    def __len__(self) -> int:
        """返回派生交互轮数。"""
        return len(self._pairs())

    def __repr__(self) -> str:
        session_id = getattr(self._session, "session_id", "")
        return f"SessionDerivedMemoryView(session={session_id}, interactions={len(self)})"


class SessionManager:
    """
    会话管理器 — 商业级上下文压缩

    管理多轮对话历史，支持：
    - Token 预算驱动触发压缩
    - 三层压缩管道（完整保留 → 逐轮摘要 → 批量摘要）
    - 可选 LLM 结构化语义摘要
    - Append-only 设计（保留原始消息的元数据，不破坏上下文）
    - 手动 /compact 命令支持
    """

    def __init__(
        self,
        max_messages: int = 100,
        enable_summary: bool = True,
        session_id: Optional[str] = None,
        model_context_limit: int = _DEFAULT_CONTEXT_LIMIT,
        compact_strategy: str = "semantic",
        archive_dir: Optional[str] = None,
    ):
        """
        初始化会话管理器

        Args:
            max_messages: 最大消息数（安全上限，防止极端情况溢出）
            enable_summary: 是否启用上下文摘要
            session_id: 会话 ID
            model_context_limit: 模型上下文窗口大小（token），用于预算计算
            compact_strategy: 压缩策略 — "semantic"（LLM语义摘要）或 "simple"（静态截断）
            archive_dir: 压缩时保存完整历史存档的目录（可选），用于事后回溯
        """
        self.session_id = session_id or self._generate_session_id()
        self.max_messages = max_messages
        self.enable_summary = enable_summary

        # 配置 Token 预算。
        self.model_context_limit = model_context_limit
        self._compact_strategy = compact_strategy
        self._compact_fn: Optional[Callable] = None  # 声明摘要回调签名。

        # 计算预算值。未知上下文长度时不启用预算压缩，避免伪造 200K 一类默认值。
        self.context_budget, self.output_reserve = _budgets_for_limit(model_context_limit)

        # 运行中的 token 计数
        self._running_tokens: int = 0
        self._last_compact_time: Optional[str] = None
        self._compact_count: int = 0

        # 完整历史存档目录（可选，用于压缩时保存原始消息供回溯）
        self.archive_dir: Optional[Path] = Path(archive_dir) if archive_dir else None
        if self.archive_dir:
            self.archive_dir.mkdir(parents=True, exist_ok=True)
        self._last_archive_path: Optional[str] = None

        # 消息历史
        self.messages: List[Message] = []

        # 元数据
        self.created_at = datetime.now(timezone.utc)
        self.last_updated = datetime.now(timezone.utc)

        # 摘要信息
        self.summary: Optional[str] = None

    # ==========================================================================
    # 估算 Token 预算。
    # ==========================================================================

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """估算文本的 token 数（1 token ≈ 3 字符的近似）。"""
        if not text:
            return 0
        return max(1, len(text) // 3)

    def _count_message_tokens(self, message: Message) -> int:
        """估算单条消息的 token 数。"""
        return self.estimate_tokens(message.content) + 4  # 消息结构开销

    def _estimate_overhead_tokens(self) -> int:
        """估算系统固定开销（系统提示词 + 工具定义）。"""
        overhead = _SYSTEM_OVERHEAD_ESTIMATE
        # 加上实际系统消息贡献
        for msg in self.messages:
            if msg.role == "system":
                overhead += self._count_message_tokens(msg)
        return overhead

    def _rebuild_token_count(self):
        """从当前消息列表重新计算运行中的 token 数。"""
        self._running_tokens = self._estimate_overhead_tokens()
        for msg in self.messages:
            if msg.role != "system":
                self._running_tokens += self._count_message_tokens(msg)

    def _over_budget(self, ratio: float) -> bool:
        """运行中 token（含输出保留）是否超过按比例折算的预算。"""
        if self.model_context_limit <= 0:
            return False
        return (self._running_tokens + self.output_reserve) > int(self.model_context_limit * ratio)

    @property
    def usage_ratio(self) -> float:
        """当前上下文使用比例（0.0 ~ 1.0）。"""
        if self.model_context_limit <= 0:
            return 0.0
        return min(1.0, self._running_tokens / self.model_context_limit)

    @property
    def needs_compact(self) -> bool:
        """检查是否达到标准压缩触发阈值（80%）。"""
        return self._over_budget(_CONTEXT_BUDGET_RATIO)

    @property
    def needs_preventive_compact(self) -> bool:
        """检查是否达到预防性压缩阈值（70%）。
        在此阈值时触发轻度压缩：保留更多轮次，减少压缩激进程度。
        """
        return self._over_budget(_PREVENTIVE_RATIO)

    @property
    def needs_urgent_compact(self) -> bool:
        """检查是否达到紧急压缩阈值（90%）。
        在此阈值时触发激进压缩：保留更少轮次，尽量压缩旧内容。
        """
        return self._over_budget(_URGENT_RATIO)

    # ==========================================================================
    # 压缩函数接口
    # ==========================================================================

    def set_compact_fn(self, fn: Optional[Callable[[List[Dict[str, str]]], str]], strategy: Optional[str] = None):
        """
        设置用于语义压缩的模型回调函数。

        Args:
            fn: 接收消息列表并返回文本的模型回调（如 model.chat）
            strategy: 覆盖压缩策略
        """
        self._compact_fn = fn
        if strategy:
            self._compact_strategy = strategy

    def set_context_limit(self, limit: int):
        """动态调整模型上下文窗口大小。"""
        limit = int(limit or 0)
        self.model_context_limit = limit
        self.context_budget, self.output_reserve = _budgets_for_limit(limit)
        self._rebuild_token_count()

    # ==========================================================================
    # 消息管理
    # ==========================================================================

    def add_message(
        self,
        role: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Message:
        """
        添加消息到会话，自动触发 Token 预算检测与压缩。

        Args:
            role: 角色（user/assistant/system）
            content: 消息内容
            metadata: 附加元数据

        Returns:
            创建的消息对象
        """
        message = Message(
            role=role,
            content=content,
            metadata=metadata or {}
        )
        self.messages.append(message)
        self.last_updated = datetime.now(timezone.utc)

        # 更新运行中 token 计数
        self._running_tokens += self._count_message_tokens(message)

        # 安全上限检查（fallback：防止极端消息撑爆）
        if len(self.messages) > self.max_messages * 2:
            self._auto_compact()

        return message

    def add_user_message(self, content: str) -> Message:
        """添加用户消息"""
        return self.add_message("user", content)

    def add_assistant_message(self, content: str, metadata: Optional[Dict[str, Any]] = None) -> Message:
        """添加助手消息"""
        return self.add_message("assistant", content, metadata)

    # ==========================================================================
    # 三层压缩核心
    # ==========================================================================

    def maybe_compact(self) -> bool:
        """
        在构建消息前调用：按分层阈值触发压缩。

        分层策略：
        - 70% 预防性：保留更多轮次（_KEEP_FULL_ROUNDS + 5）
        - 80% 标准：标准保留轮次
        - 90% 紧急：激进压缩，减少保留轮次

        Returns:
            True 如果执行了压缩
        """
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
        """
        手动压缩（供 /compact 命令调用）。

        Args:
            focus: 压缩重点，提示模型保留特定方面的信息

        Returns:
            压缩结果描述
        """
        if len(self.messages) <= 1:
            return "会话太短，无需压缩"

        self._auto_compact(focus=focus)
        return self.summary or "上下文已压缩"

    def force_compact(self, reason: str = "prompt_too_long") -> str:
        """
        紧急压缩 — 当 API 返回 prompt_too_long / context_length_exceeded 时调用。

        与常规压缩的区别：
        - 跳过阈值检查，总是执行
        - 更激进地减少保留轮次（_KEEP_FULL_ROUNDS // 2）
        - 即使 enable_summary=False 也会执行

        Args:
            reason: 触发紧急压缩的原因

        Returns:
            压缩结果描述
        """
        if len(self.messages) <= 1:
            return "会话太短，无法紧急压缩"

        self._auto_compact(urgent=True, focus=reason)
        return (
            f"紧急压缩完成 (原因: {reason}): "
            f"保留最近 {_KEEP_FULL_ROUNDS // 2} 轮, "
            f"使用率 {self.usage_ratio:.0%}"
        )

    def _archive_history(self) -> Optional[str]:
        """
        将当前完整消息历史保存到存档文件（用于事后回溯）。

        Returns:
            存档文件路径，若未配置 archive_dir 则返回 None
        """
        if not self.archive_dir:
            return None

        try:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            filename = f"compact_{timestamp}_{self._compact_count}.json"
            path = self.archive_dir / filename

            data = {
                "schema_version": SESSION_SCHEMA_VERSION,
                "session_id": self.session_id,
                "compact_count": self._compact_count,
                "archived_at": datetime.now(timezone.utc).isoformat(),
                "messages": [msg.to_dict() for msg in self.messages],
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

            self._last_archive_path = str(path)
            return str(path)
        except Exception as e:
            print(tr("core.archive_save_failed", error=str(e)))
            return None

    def _auto_compact(self, focus: Optional[str] = None, gentle: bool = False, urgent: bool = False):
        """
        三层压缩核心逻辑。

        分层策略：
        - gentle=True: 预防性 — 保留更多轮次（_KEEP_FULL_ROUNDS + 5）
        - urgent=True: 紧急 — 更激进压缩（保留 _KEEP_FULL_ROUNDS // 2）
        - 默认: 标准压缩

        1. 压缩前将当前完整历史保存到存档（可选）
        2. 插入边界标记（boundary marker）记录压缩事件
        3. 旧轮次按策略压缩（LLM 语义摘要 或 静态截断）
        4. 最近 N 轮完整保留
        5. 重新计算 token 计数
        """
        if not self.enable_summary and not urgent:
            return

        # 根据压缩级别决定保留轮次
        if urgent:
            keep_rounds = max(2, _KEEP_FULL_ROUNDS // 2)
        elif gentle:
            keep_rounds = _KEEP_FULL_ROUNDS + 5
        else:
            keep_rounds = _KEEP_FULL_ROUNDS

        # 压缩前存档完整历史
        archive_path = self._archive_history()

        system_msgs, rounds = self._identify_rounds(self.messages)

        # 排除纯压缩/系统类轮次后的可压缩轮次
        compressible = [r for r in rounds if r["type"] == "round"]
        total_compressible = len(compressible)

        if total_compressible <= keep_rounds:
            return  # 不够一轮，不压缩

        # 需要压缩的轮次 = 所有可压缩轮次 - 保留的轮次
        keep_rounds_count = min(keep_rounds, total_compressible)
        compress_count = total_compressible - keep_rounds_count

        # 构建新消息列表
        new_messages: List[Message] = list(system_msgs)

        # 边界标记：记录压缩事件，附上存档路径
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

        # 要压缩的轮次
        old_rounds = compressible[:compress_count]

        # 选择压缩策略
        if (self._compact_fn and self._compact_strategy == "semantic"):
            summary = self._generate_semantic_summary(old_rounds, focus)
        else:
            summary = self._summarize_rounds_bulk(old_rounds)

        # 在摘要末尾附上存档路径（如有）
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

        # 保留 Tier 1：最近 N 轮完整内容。
        for r in compressible[-keep_rounds_count:]:
            if r.get("user"):
                new_messages.append(r["user"])
            if r.get("assistant"):
                new_messages.append(r["assistant"])

        # 将之前已压缩的消息也保留
        for r in rounds:
            if r["type"] == "compressed":
                # 避免重复添加已处理的压缩消息
                if r["message"].metadata.get("type") != "boundary":
                    new_messages.append(r["message"])

        self.messages = new_messages
        self._compact_count += 1
        self._last_compact_time = datetime.now(timezone.utc).isoformat()

        # 重新计算 token 计数
        self._rebuild_token_count()

        # 更新摘要文本
        self.summary = (
            f"上下文已压缩 (第 {self._compact_count} 次): "
            f"保留最近 {keep_rounds_count} 轮完整对话 + "
            f"{len(old_rounds)} 轮{'语义摘要' if self._compact_fn and self._compact_strategy == 'semantic' else '要点'} | "
            f"使用率 {self.usage_ratio:.0%}"
        )

    def _generate_semantic_summary(self, rounds: list, focus: Optional[str] = None) -> str:
        """
        使用 LLM 生成结构化语义摘要（9 段式）。

        Args:
            rounds: 需要压缩的轮次列表
            focus: 可选的压缩重点

        Returns:
            语义摘要文本
        """
        # 构建对话文本
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
            # 没有模型回调，回退到静态截断
            return self._summarize_rounds_bulk(rounds)

        try:
            summary = self._compact_fn([{"role": "user", "content": prompt_text}])
            if not summary or not summary.strip():
                return self._summarize_rounds_bulk(rounds)
            return summary.strip()
        except Exception as e:
            # 捕获 LLM 调用失败，静默回退。
            return f"[Semantic summary fallback: {e}]\n\n" + self._summarize_rounds_bulk(rounds)

    def truncate_to_user_turns(self, target: int) -> int:
        """按 user 轮截断并重算 token，返回丢弃的消息数。"""
        want = normalize_truncate_target(target)
        original = len(self.messages)
        if want <= 0:
            if not self.messages:
                return 0
            self.messages = []
            self._rebuild_token_count()
            return original
        user_indices = [i for i, m in enumerate(self.messages) if m.role == "user"]
        if want >= len(user_indices):
            return 0
        cutoff = user_indices[want]
        self.messages = self.messages[:cutoff]
        self._rebuild_token_count()
        return original - len(self.messages)

    def get_messages(
        self,
        include_system: bool = True,
        max_turns: Optional[int] = None,
        include_compaction_summaries: bool = False
    ) -> List[Dict[str, Any]]:
        """
        获取消息列表（用于模型输入）。

        注意：调用此方法前建议先调用 maybe_compact()。

        Args:
            include_system: 是否包含系统消息
            max_turns: 最大对话轮数（每轮包含 user 和 assistant）
            include_compaction_summaries: include_system=False 时，是否仍保留压缩产物
                （摘要与边界标记，其 metadata["compressed"] 为 True）。
                原始系统提示词由每轮重建，不进历史；但压缩摘要只存在于历史，
                若一并过滤则压缩等价于静默丢弃历史。

        Returns:
            消息字典列表
        """
        messages: List[Dict[str, Any]] = []

        for msg in self.messages:
            if not include_system and msg.role == "system":
                is_compaction_artifact = bool(msg.metadata.get("compressed"))
                if not (include_compaction_summaries and is_compaction_artifact):
                    continue
            item: Dict[str, Any] = {
                "role": msg.role,
                "content": msg.content
            }
            if msg.metadata:
                item["metadata"] = dict(msg.metadata)
            messages.append(item)

        if max_turns:
            system_messages = [m for m in messages if m["role"] == "system"]
            non_system = [m for m in messages if m["role"] != "system"]
            recent = non_system[-max_turns * 2:]
            messages = system_messages + recent

        return messages

    # ==========================================================================
    # 辅助方法
    # ==========================================================================

    @staticmethod
    def _generate_session_id() -> str:
        from uuid import uuid4
        return str(uuid4())[:8]

    @staticmethod
    def _summarize_round(round_data: dict) -> str:
        """将单轮对话压缩为 1-2 行摘要。"""
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
        """将多轮早期对话合并为要点摘要。"""
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
        """将消息列表分离为系统消息和 (user, assistant) 轮次。"""
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

    def get_history(self) -> List[Tuple[str, str]]:
        """获取对话历史（简化格式）。"""
        return [(msg.role, msg.content) for msg in self.messages]

    def derived_interaction_pairs(self) -> List[Tuple[str, str]]:
        """由消息派生的 (user, assistant) 轮视图（单写 messages，不另存第二份历史）。

        system 与压缩产物不计入轮次；落单 user 配空回应，落单 assistant 配空提问。
        """
        pairs: List[Tuple[str, str]] = []
        pending_user: Optional[str] = None
        for msg in self.messages:
            if msg.metadata.get("compressed") or msg.role == "system":
                continue
            if msg.role == "user":
                if pending_user is not None:
                    pairs.append((pending_user, ""))
                pending_user = msg.content
            elif msg.role == "assistant":
                pairs.append((pending_user or "", msg.content))
                pending_user = None
        if pending_user is not None:
            pairs.append((pending_user, ""))
        return pairs

    @staticmethod
    def messages_from_interaction_dicts(items: List[Dict[str, Any]]) -> List["Message"]:
        """由记忆形态的交互字典派生消息列表（单写 interactions，会话按需派生）。"""
        out: List["Message"] = []
        for item in items:
            user_text = item.get("user_input", "") or item.get("user", "")
            ai_text = item.get("ai_response", "") or item.get("assistant", "")
            if user_text:
                out.append(Message(role="user", content=user_text))
            if ai_text:
                out.append(Message(role="assistant", content=ai_text))
        return out

    def clear(self):
        """清空会话历史。"""
        self.messages = []
        self.summary = None
        self._running_tokens = 0
        self._compact_count = 0
        self._last_compact_time = None
        self._last_archive_path = None
        self.last_updated = datetime.now(timezone.utc)

    def is_empty(self) -> bool:
        """检查会话是否为空。"""
        return len(self.messages) == 0

    def get_message_count(self) -> int:
        """获取消息数量。"""
        return len(self.messages)

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

    # ==========================================================================
    # 持久化
    # ==========================================================================

    def _messages_payload(self) -> List[Dict[str, Any]]:
        """消息列表的落盘形态（save 与 json 导出共用，保持同一格式）。"""
        return [msg.to_dict() for msg in self.messages]

    def to_state_dict(self) -> Dict[str, Any]:
        """会话全量的落盘字典（与 Store 原子写语义对齐：单字典一次落盘）。"""
        return {
            "schema_version": SESSION_SCHEMA_VERSION,
            "session_id": self.session_id,
            "created_at": self.created_at.isoformat(),
            "last_updated": self.last_updated.isoformat(),
            "max_messages": self.max_messages,
            "enable_summary": self.enable_summary,
            "summary": self.summary,
            "messages": self._messages_payload(),
            "_compact_count": self._compact_count,
            "_last_compact_time": self._last_compact_time,
            "_last_archive_path": self._last_archive_path,
            "_model_context_limit": self.model_context_limit,
            "_compact_strategy": self._compact_strategy,
            "_archive_dir": str(self.archive_dir) if self.archive_dir else None,
        }

    def _apply_state_dict(self, data: Dict[str, Any]) -> None:
        """由落盘字典恢复消息与压缩元数据（不含 archive_dir 沙箱校验）。"""
        self.messages = [
            Message.from_dict(msg) for msg in data.get("messages", [])
        ]
        self.summary = data.get("summary")
        self.created_at = datetime.fromisoformat(data.get("created_at", datetime.now(timezone.utc).isoformat()))
        self.last_updated = datetime.fromisoformat(data.get("last_updated", datetime.now(timezone.utc).isoformat()))
        self._compact_count = data.get("_compact_count", 0)
        self._last_compact_time = data.get("_last_compact_time")
        self._last_archive_path = data.get("_last_archive_path")

    def save(self, file_path: str) -> bool:
        """保存会话到文件。"""
        try:
            write_private_json(file_path, self.to_state_dict())
            return True
        except Exception as e:
            print(tr("core.session_save_failed", error=str(e)))
            return False

    @classmethod
    def load(cls, file_path: str) -> Optional["SessionManager"]:
        """从文件加载会话。"""
        try:
            path = Path(file_path)
            if not path.exists():
                return None

            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            if data.get("schema_version") != SESSION_SCHEMA_VERSION:
                return None

            raw_archive = data.get("_archive_dir")
            archive_dir = None
            if raw_archive:
                try:
                    arch_path = Path(str(raw_archive)).expanduser().resolve()
                    base = path.expanduser().resolve().parent
                    arch_path.relative_to(base)
                    archive_dir = str(arch_path)
                except (OSError, ValueError):
                    archive_dir = None

            session = cls(
                max_messages=data.get("max_messages", 100),
                enable_summary=data.get("enable_summary", True),
                session_id=data.get("session_id"),
                model_context_limit=data.get("_model_context_limit", _DEFAULT_CONTEXT_LIMIT),
                compact_strategy=data.get("_compact_strategy", "semantic"),
                archive_dir=archive_dir,
            )

            session._apply_state_dict(data)

            # 加载后重建 token 计数
            session._rebuild_token_count()

            return session
        except Exception as e:
            print(tr("core.session_load_failed", error=str(e)))
            return None

    # ==========================================================================
    # 导出与统计
    # ==========================================================================

    def get_context_summary(self) -> str:
        """获取上下文摘要。"""
        if self.summary:
            return self.summary
        if not self.messages:
            return "空对话"
        user_count = sum(1 for m in self.messages if m.role == "user")
        limit_display = self.model_context_limit if self.model_context_limit > 0 else "unknown"
        return (
            f"对话轮数: {user_count} 轮, "
            f"消息: {len(self.messages)} 条, "
            f"Token: {self._running_tokens}/{limit_display} "
            f"({self.usage_ratio:.0%})"
        )

    def export_conversation(self, format: str = "text") -> str:
        """导出会话内容。"""
        if format == "json":
            return json.dumps(
                self._messages_payload(),
                indent=2,
                ensure_ascii=False
            )
        elif format == "markdown":
            lines = [f"# 会话 {self.session_id}\n"]
            lines.append(f"**创建时间**: {self.created_at.strftime('%Y-%m-%d %H:%M:%S')}\n")
            lines.append(f"**最后更新**: {self.last_updated.strftime('%Y-%m-%d %H:%M:%S')}\n")
            if self._compact_count > 0:
                limit_display = self.model_context_limit if self.model_context_limit > 0 else "unknown"
                lines.append(f"**压缩次数**: {self._compact_count}\n")
                lines.append(f"**Token 使用**: {self._running_tokens}/{limit_display} ({self.usage_ratio:.0%})\n\n")
            for msg in self.messages:
                role_label = "用户" if msg.role == "user" else "助手"
                lines.append(f"### {role_label}\n")
                lines.append(f"{msg.content}\n\n")
            return "".join(lines)
        else:
            lines = [f"=== 会话 {self.session_id} ===\n"]
            for msg in self.messages:
                role_label = "用户" if msg.role == "user" else "助手"
                lines.append(f"{role_label}: {msg.content}\n\n")
            return "".join(lines)

    def __repr__(self) -> str:
        limit_display = self.model_context_limit if self.model_context_limit > 0 else "unknown"
        return (
            f"SessionManager("
            f"id={self.session_id}, "
            f"messages={len(self.messages)}, "
            f"tokens={self._running_tokens}/{limit_display} "
            f"({self.usage_ratio:.0%}), "
            f"compacts={self._compact_count}"
            f")"
        )
