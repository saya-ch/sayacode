"""对话装配归属 lib.agent 包，由 assembly 模块承载。

归属：prompts / middleware 侧 helper（只搬运，不重写语义）。
原实现逐行搬自 lib.agent_recovery（start_turn / finish_turn /
history_messages / build_system_prompt_text / build_system_content），
分类与行为保持完全一致。

调用链：
SAIAgent.__init__ / _build_system_prompt（lib/agent.py 装配）
→ build_system_prompt_text（风格模板 + 模式补充，一次性）；
agent_loop.prepare_messages / build_messages / refresh_turn_prompt /
sync_turn_state（lib/agent_loop.py 执行循环，每轮）
→ start_turn（落 user 消息）→ history_messages（镜像转 LangChain 消息）
→ build_system_content（system 全文组装）；
agent_loop.run_turn / stream_turn 收尾
→ finish_turn（落 assistant 消息）；
commands/conversation.py 的 /compact 同步图状态
→ history_messages（与 build_system_content 同构的全量覆盖）。

历史唯一真相源为 session.messages（+ checkpointer 持久化），
记忆为会话派生只读视图；压缩摘要与边界标记随历史一并转换，
否则压缩退化为静默丢历史。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage


def start_turn(session: Any, memory: Any, user_input: str,
               enhancer: Optional[Callable[[str], str]] = None) -> tuple[str, str]:
    """开始一轮对话并返回原始与增强输入（只写 session）。

    历史唯一真相源为 session.messages（+ checkpointer 持久化）；
    memory 参数仅为兼容旧调用方保留，不再写入。
    """
    session.add_user_message(user_input)
    effective_input = enhancer(user_input) if enhancer else user_input
    return user_input, effective_input


def finish_turn(session: Any, memory: Any, original_input: str, response: str,
                metadata: Optional[Dict[str, Any]] = None) -> None:
    """收尾一轮对话并落盘会话（只写 session，记忆按需派生）。"""
    session.add_assistant_message(response, metadata=metadata)


def history_messages(session: Any) -> list:
    """把镜像历史转成 LangChain 消息（压缩摘要与边界标记一并保留）。

    原 PromptBuilder.history_messages：压缩后镜像被重写，图状态必须用同一份
    转换结果覆盖，否则压缩退化为丢历史。历史只取对话轮次；但压缩摘要/边界标记
    仅存在于历史中，必须一并保留，否则压缩会退化为静默丢弃历史
    （原始系统提示词每轮重建，无需从历史恢复）。
    """
    converted: list = []
    history = session.get_messages(
        include_system=False,
        include_compaction_summaries=True,
    )
    for msg in history[:-1]:
        if msg["role"] == "user":
            converted.append(HumanMessage(content=msg["content"]))
        elif msg.get("role") == "system":
            converted.append(SystemMessage(content=msg["content"]))
        else:
            # 恢复 additional_kwargs（reasoning_content / thinking 等跨轮透传）
            extra = (msg.get("metadata") or {}).get("additional_kwargs", {})
            if extra:
                converted.append(AIMessage(content=msg["content"], additional_kwargs=extra))
            else:
                converted.append(AIMessage(content=msg["content"]))
    return converted


def build_system_prompt_text(workspace: Any, project_context: Any,
                             prompt_style: str, agent_mode: str) -> str:
    """组装基础 system prompt（含模式补充）。原 PromptBuilder.build_system_prompt。"""
    from pathlib import Path

    from ..core.modes import get_agent_mode_prompt_overlay
    from ..prompts import get_prompt_by_style

    base_prompt = get_prompt_by_style(
        style=prompt_style,
        agent_name="SAYA",
        workspace=str(Path(workspace).expanduser().resolve()),
        project_summary=project_context.get_summary(),
        agent_mode=agent_mode,
    )
    # 补充说明 get_system_prompt() 已加载模式提示词，
    # 此处的 mode overlay 作为补充（向后兼容）。
    return base_prompt + "\n\n" + get_agent_mode_prompt_overlay(agent_mode)


def build_system_content(workspace: Any, project_context: Any, session: Any,
                         system_prompt: str, context_packager: Any,
                         include_context: bool = True,
                         reminder_state: Optional[Dict[str, Any]] = None) -> str:
    """只拼 system 文本（不碰压缩、不读历史）：给图中间件每轮 refresh 用。

    原 PromptBuilder.build_system_content：与 build_messages 共用同一套
    组装语义；压缩（maybe_compact）由调用方在外层先做——中间件路径下压缩后
    还要同步图状态，顺序必须由外层掌控。项目上下文与项目记忆全量注入
    （dynamic_prompt 条件扩展），剪枝由官方 ContextEditingMiddleware 负责，
    这里不做字符截断。context_packager 参数仅为兼容旧签名保留，不再使用。
    """
    from ..core.project_memory import build_memory_system_section
    from ..prompts import build_conditional_system_extras

    if include_context:
        parts = [system_prompt]
        try:
            project_text = project_context.get_context_for_llm(max_files=10)
        except Exception:
            project_text = ""
        if project_text and project_text.strip():
            parts.append(f"## 项目上下文\n{project_text.strip()}")
        try:
            memory_text = build_memory_system_section(workspace)
        except Exception:
            memory_text = ""
        if memory_text and memory_text.strip():
            parts.append(memory_text.strip())
        system_content = "\n\n".join(parts)
    else:
        system_content = system_prompt

    # 条件 system 扩展（原 reminders 字符串注入）：按运行时状态推导，
    # 经 SayaPromptMiddleware（dynamic_prompt）挂载，无提醒时跳过。
    extras = build_conditional_system_extras(reminder_state or {})
    if extras:
        system_content += f"\n\n## 系统提醒\n{extras}"
    return system_content


__all__ = [
    "build_system_content",
    "build_system_prompt_text",
    "finish_turn",
    "history_messages",
    "start_turn",
]
