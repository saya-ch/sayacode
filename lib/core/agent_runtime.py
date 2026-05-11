"""Agent runtime components used by SAIAgent."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Union
import inspect

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool
from langgraph.prebuilt import create_react_agent

from .context import ProjectContext
from .context_packager import ContextPackager, ContextPackRequest
from .memory import MemoryManager
from .modes import get_agent_mode_prompt_overlay, normalize_agent_mode
from .session import SessionManager
from ..i18n import tr
from ..prompts import get_prompt_by_style, normalize_prompt_style
from ..prompts.reminders import get_system_reminders
from ..theme import print_warning

try:
    from langchain.agents import create_agent as create_langchain_agent
except ImportError:
    create_langchain_agent = None


MessageLike = Union[SystemMessage, HumanMessage, AIMessage]


# ==============================================================================
# Turn 状态机
# ==============================================================================


class TurnTransition(Enum):
    """每次 Agent turn 的转换原因 — 参考 Claude Code queryLoop transition。"""
    NEXT_TURN = "next_turn"
    COMPLETED = "completed"
    STREAM_INTERRUPTED = "stream_interrupted"
    MODEL_ERROR = "model_error"
    MAX_RETRIES = "max_retries"
    ABORTED = "aborted"


@dataclass
class TurnState:
    """追踪单次 Agent turn 的执行状态。"""
    transition: TurnTransition = TurnTransition.COMPLETED
    turn_count: int = 0
    tool_use_count: int = 0
    needs_follow_up: bool = False
    error_message: str = ""

    @property
    def is_terminal(self) -> bool:
        """Turn 是否为终止状态（不需要继续）。"""
        return self.transition in (
            TurnTransition.COMPLETED,
            TurnTransition.MODEL_ERROR,
            TurnTransition.MAX_RETRIES,
            TurnTransition.ABORTED,
        )

    @property
    def should_continue(self) -> bool:
        """Turn 是否需要继续（有工具调用需要执行）。"""
        return self.transition == TurnTransition.NEXT_TURN and self.needs_follow_up


@dataclass
class PromptBuilder:
    """Build system prompts and per-turn model messages."""

    workspace: Path
    project_context: ProjectContext
    prompt_style: str = "standard"
    agent_mode: str = "build"
    context_packager: ContextPackager = field(default_factory=ContextPackager)

    def __post_init__(self) -> None:
        self.workspace = Path(self.workspace).expanduser().resolve()
        self.prompt_style = normalize_prompt_style(self.prompt_style)
        self.agent_mode = normalize_agent_mode(self.agent_mode) or "build"

    def build_system_prompt(self) -> str:
        base_prompt = get_prompt_by_style(
            style=self.prompt_style,
            agent_name="SAYA",
            workspace=str(self.workspace),
            project_summary=self.project_context.get_summary(),
            agent_mode=self.agent_mode,
        )
        # get_system_prompt() 已根据 agent_mode 条件加载模式提示词，
        # 此处的 mode overlay 作为补充（向后兼容）
        return base_prompt + "\n\n" + get_agent_mode_prompt_overlay(self.agent_mode)

    def build_messages(
        self,
        effective_input: str,
        session: SessionManager,
        system_prompt: str,
        include_context: bool = True,
        reminder_state: Optional[Dict[str, Any]] = None,
    ) -> List[MessageLike]:
        session.maybe_compact()

        messages: List[MessageLike] = []
        if include_context:
            context_package = self.context_packager.pack(ContextPackRequest(
                workspace=self.workspace,
                project_context=self.project_context,
                session=session,
                include_project=True,
                include_memory=True,
                include_history=False,
                max_files=10,
            ))
            system_content = f"{system_prompt}\n\n## 项目上下文\n{context_package.content}"
        else:
            system_content = system_prompt

        # 注入系统提醒（纯文本拼接，无 I/O）
        reminders = get_system_reminders(reminder_state or {})
        if reminders:
            system_content += f"\n\n## 系统提醒\n{reminders}"

        messages.append(SystemMessage(content=system_content))

        history = session.get_messages(include_system=False)
        for msg in history[:-1]:
            if msg["role"] == "user":
                messages.append(HumanMessage(content=msg["content"]))
            elif msg.get("role") == "system":
                messages.append(SystemMessage(content=msg["content"]))
            else:
                # 恢复 additional_kwargs（reasoning_content / thinking 等跨轮透传）
                extra = (msg.get("metadata") or {}).get("additional_kwargs", {})
                if extra:
                    messages.append(AIMessage(content=msg["content"], additional_kwargs=extra))
                else:
                    messages.append(AIMessage(content=msg["content"]))

        messages.append(HumanMessage(content=effective_input))
        return messages


@dataclass
class ConversationManager:
    """Coordinate session and memory updates for one conversation."""

    session: SessionManager
    memory: MemoryManager

    def start_turn(
        self,
        user_input: str,
        enhancer: Optional[Callable[[str], str]] = None,
    ) -> tuple[str, str]:
        self.memory.start_interaction()
        self.session.add_user_message(user_input)
        effective_input = enhancer(user_input) if enhancer else user_input
        return user_input, effective_input

    def finish_turn(self, original_input: str, response: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        self.memory.add_interaction(original_input, response)
        self.session.add_assistant_message(response, metadata=metadata)


@dataclass
class AgentRunner:
    """Own model/tool binding and LangGraph agent lifecycle."""

    model: Any
    tools: List[BaseTool]
    system_prompt: str
    agent: Optional[Any] = None
    model_with_tools: Optional[Any] = None

    def rebuild(self) -> Optional[Any]:
        self.model_with_tools = self._bind_tools()
        self.agent = self._create_agent()
        return self.agent

    def invoke(self, messages: List[MessageLike]) -> Optional[Dict[str, Any]]:
        if not self.agent:
            return None
        return self.agent.invoke({"messages": messages})

    def stream(self, messages: List[MessageLike]) -> Optional[Iterator[Any]]:
        if not self.agent or not hasattr(self.agent, "stream"):
            return None

        try:
            return self.agent.stream({"messages": messages}, stream_mode="updates")
        except TypeError:
            try:
                return self.agent.stream({"messages": messages}, stream_mode="values")
            except TypeError:
                return self.agent.stream({"messages": messages})

    def _bind_tools(self) -> Any:
        try:
            if hasattr(self.model, "bind_tools"):
                return self.model.bind_tools(self.tools)
            return self.model
        except Exception as exc:
            print_warning(tr("agent.warn_bind_tools", error=str(exc)))
            return self.model

    def _create_agent(self) -> Optional[Any]:
        try:
            if not (
                hasattr(self.model_with_tools, "invoke")
                or callable(self.model_with_tools)
            ):
                print_warning(tr("agent.warn_create_agent", error="model does not implement LangChain invoke"))
                return None

            agent_factory = create_langchain_agent or create_react_agent
            factory_signature = inspect.signature(agent_factory)
            agent_kwargs: Dict[str, Any] = {}

            if "system_prompt" in factory_signature.parameters:
                agent_kwargs["system_prompt"] = self.system_prompt
            elif "prompt" in factory_signature.parameters:
                agent_kwargs["prompt"] = self.system_prompt
            elif "messages_modifier" in factory_signature.parameters:
                agent_kwargs["messages_modifier"] = self.system_prompt
            elif "state_modifier" in factory_signature.parameters:
                agent_kwargs["state_modifier"] = self.system_prompt

            return agent_factory(
                self.model_with_tools,
                self.tools,
                **agent_kwargs,
            )
        except Exception as exc:
            print_warning(tr("agent.warn_create_agent", error=str(exc)))
            return None


def message_to_chat_dict(message: MessageLike) -> Dict[str, str]:
    """Convert a LangChain message to the local chat dict format."""
    if isinstance(message, SystemMessage):
        role = "system"
    elif isinstance(message, AIMessage):
        role = "assistant"
    else:
        role = "user"

    return {"role": role, "content": content_to_text(message.content)}


def content_to_text(content: Any) -> str:
    """Extract text from common LangChain message content shapes."""
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        text_parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                text_parts.append(str(block["text"]))
            elif hasattr(block, "type") and getattr(block, "type", None) == "text":
                text_value = getattr(block, "text", None)
                if text_value:
                    text_parts.append(str(text_value))
        return "\n".join(text_parts)

    if content is None:
        return ""

    return str(content)


def message_kind(msg: Any) -> str:
    """尽量稳定地识别 LangChain 消息类型。"""
    message_type = getattr(msg, "type", None)
    if message_type:
        return str(message_type).lower()
    return msg.__class__.__name__.lower()


def extract_tool_names(tool_calls: Any) -> List[str]:
    """从不同供应商的 tool_calls 结构中提取工具名。"""
    tool_names: list[str] = []
    for tool_call in tool_calls or []:
        name = None
        if isinstance(tool_call, dict):
            name = tool_call.get("name")
            if not name and isinstance(tool_call.get("function"), dict):
                name = tool_call["function"].get("name")
        else:
            name = getattr(tool_call, "name", None)
            function = getattr(tool_call, "function", None)
            if not name and isinstance(function, dict):
                name = function.get("name")
            elif not name and function is not None:
                name = getattr(function, "name", None)
        tool_names.append(str(name or "unknown"))
    return tool_names


__all__ = [
    "AgentRunner",
    "ConversationManager",
    "PromptBuilder",
    "TurnTransition",
    "TurnState",
    "content_to_text",
    "extract_tool_names",
    "message_kind",
    "message_to_chat_dict",
]
