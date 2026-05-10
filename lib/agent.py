"""
Agent 主逻辑

使用 LangGraph 的 create_react_agent 构建智能 Agent。

功能：
- 基于 ReAct 模式的推理和行动
- 工具注册和调用
- 记忆管理
- 流式输出支持
- 安全检查集成
"""

from typing import List, Optional, Dict, Any, Iterator, Union, Callable
from pathlib import Path
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool

# 导入项目模块
from .core.agent_runtime import AgentRunner, ConversationManager, PromptBuilder, content_to_text, extract_tool_names, message_kind, message_to_chat_dict
from .core.memory import MemoryManager
from .core.safety import SafetyChecker
from .core.context import ProjectContext
from .core.context_packager import ContextPackager
from .core.session import SessionManager
from .core.modes import normalize_agent_mode
from .models import BaseModel
from .models.registry import get_model_provider_registry
from .runtime.context import RuntimeContext
from .tools import ToolFactory
from .tools.context import ToolAbortController, ToolExecutionContext, tool_execution_session
from .core.agent_runtime import TurnTransition, TurnState
from .core.hooks import create_hook_runtime
from .core.permissions import create_permission_runtime
from .prompts import normalize_prompt_style
from .i18n import tr
import time


# ==============================================================================
# 恢复路径常量
# ==============================================================================

_MAX_RETRIES = 3                  # 最大重试次数（可恢复错误）
_RETRY_BACKOFF_BASE = 1.5         # 指数退避基数（秒）
_RECOVERABLE_ERROR_PATTERNS = (
    "rate_limit",
    "rate limit",
    "too many requests",
    "server error",
    "internal server error",
    "service unavailable",
    "timeout",
    "connection",
    "overloaded",
)
_MAX_OUTPUT_TOKENS_PATTERNS = (
    "max_output_tokens",
    "max tokens",
    "output token limit",
    "maximum context length",
    "reduce the length",
)
_PROMPT_TOO_LONG_PATTERNS = (
    "prompt too long",
    "context length",
    "context window",
    "too many tokens",
    "input length",
)


def _classify_error(error_msg: str) -> str:
    """将错误消息归类为 recoverable / max_output_tokens / prompt_too_long / fatal。"""
    lowered = error_msg.lower()
    for pat in _MAX_OUTPUT_TOKENS_PATTERNS:
        if pat in lowered:
            return "max_output_tokens"
    for pat in _PROMPT_TOO_LONG_PATTERNS:
        if pat in lowered:
            return "prompt_too_long"
    for pat in _RECOVERABLE_ERROR_PATTERNS:
        if pat in lowered:
            return "recoverable"
    return "fatal"


def _retry_delay(attempt: int) -> float:
    """计算指数退避延迟（秒）。"""
    return _RETRY_BACKOFF_BASE ** attempt


TOOL_PRIORITY = {
    # Understand the project first.
    "analyze_project": 10,
    "get_project_summary": 11,
    "list_project_files": 12,
    "get_file_info": 13,
    # Search and read before editing.
    "glob_search": 20,
    "grep_search": 21,
    "list_directory": 22,
    "read_file": 23,
    # Narrow file edits before broader operations.
    "search_replace": 30,
    "write_file": 31,
    "create_directory": 32,
    "delete_file": 39,
    # Git inspection before mutation.
    "git_status": 40,
    "git_diff": 41,
    "git_log": 42,
    "git_branch": 43,
    "git_remote": 44,
    "git_add": 50,
    "git_commit": 51,
    "git_stash": 52,
    "git_checkout": 53,
    "git_pull": 54,
    "git_push": 55,
    # Shell diagnostics before command execution.
    "check_command_safety_tool": 60,
    "get_system_info": 61,
    "list_environment_variables": 62,
    "execute_command_tool": 69,
}


# ==============================================================================
# Agent 类
# ==============================================================================

class SAIAgent:
    """
    SAYA Agent

    一个基于 ReAct 模式的智能编程助手，使用 LangGraph 实现。
    具备文件操作、代码编辑、Git 管理、项目分析等能力。
    扩展入口由 CLI 侧兼容层处理。
    """

    def __init__(
        self,
        model: Union[BaseModel, Any],
        workspace: Path,
        tools: Optional[List[BaseTool]] = None,
        memory_manager: Optional[MemoryManager] = None,
        safety_checker: Optional[SafetyChecker] = None,
        system_prompt: Optional[str] = None,
        prompt_style: str = "standard",
        project_context: Optional[ProjectContext] = None,
        session_manager: Optional[SessionManager] = None,
        stream_callback: Optional[Callable] = None,
        enable_mcp: bool = False,
        mcp_servers: Optional[List[str]] = None,
        agent_mode: str = "build",
        permissions: Optional[Any] = None,
        hooks: Optional[Any] = None,
    ):
        """
        初始化 Agent

        Args:
            model: 语言模型实例
            workspace: 工作区路径
            tools: 工具列表（默认为所有工具）
            memory_manager: 记忆管理器
            safety_checker: 安全检查器
            system_prompt: 系统提示词
            prompt_style: 系统提示词风格
            project_context: 项目上下文
            session_manager: 会话管理器
            stream_callback: 流式输出回调函数
        """
        self.model = model
        self.workspace = Path(workspace).expanduser().resolve()
        self._permissions_runtime = permissions
        self._hooks_runtime = hooks

        # 使用提供的工具或 runtime-bound 默认工具
        self._base_tools = self._normalize_tools(
            tools if tools is not None else self._build_default_tools(agent_mode)
        )

        # 初始化管理器
        self.memory = memory_manager or MemoryManager()
        self.safety = safety_checker or SafetyChecker(workspace_root=self.workspace)
        self.context = project_context or ProjectContext(str(self.workspace))
        self.session = session_manager or SessionManager()
        self.conversation_manager = ConversationManager(self.session, self.memory)

        # 从模型读取上下文窗口信息并同步到 SessionManager
        if hasattr(self.model, "context_window") and self.model.context_window > 0:
            self.session.set_context_limit(self.model.context_window)

        # 设置上下文压缩回调（使用模型生成语义摘要）
        if hasattr(self.model, "chat"):
            self.session.set_compact_fn(self.model.chat)

        self.prompt_style = normalize_prompt_style(prompt_style)
        self.agent_mode = normalize_agent_mode(agent_mode) or "build"
        self.context_packager = ContextPackager()
        self.prompt_builder = PromptBuilder(
            workspace=self.workspace,
            project_context=self.context,
            prompt_style=self.prompt_style,
            agent_mode=self.agent_mode,
            context_packager=self.context_packager,
        )

        # MCP stdio tools are loaded only after workspace safety/trust is configured.
        self._enable_mcp = bool(enable_mcp)
        self._mcp_servers = list(mcp_servers or [])
        self._mcp_registry: Optional[Any] = None
        self._mcp_runtime: Optional[Any] = None
        self._mcp_tools: List[BaseTool] = self._load_mcp_tools()

        # 合并所有工具
        self.tools = self._normalize_tools([*self._base_tools, *self._mcp_tools])

        # Turn 状态追踪
        self._turn_count = 0
        self._abort_controller = ToolAbortController()
        self._last_extra: dict = {}  # additional_kwargs 跨轮保留
        self._recovery_state: dict = {}  # 追踪恢复路径重试次数

        # 系统提示词
        self.system_prompt = system_prompt or self._build_system_prompt()

        # 流式输出回调
        self.stream_callback = stream_callback

        self.runner: Optional[AgentRunner] = None

        # 创建 LangGraph Agent
        self._create_agent()

    def _build_default_tools(self, agent_mode: str) -> List[BaseTool]:
        """Build runtime-bound default tools for the compatibility facade."""
        context_window = getattr(self.model, "context_window", 0)
        model_config = {"context_window": context_window} if context_window else {}
        context = RuntimeContext(
            workspace=self.workspace,
            model_type=str(getattr(self.model, "model_type", self.model.__class__.__name__)),
            model_name=str(getattr(self.model, "model_name", "")),
            model_config=model_config,
            model=self.model,
            memory=getattr(self, "memory", None),
            safety=getattr(self, "safety", None),
            project_context=getattr(self, "context", None),
            session=getattr(self, "session", None),
            agent_mode=agent_mode,
        )
        context.permissions = self._permissions_runtime or create_permission_runtime(context.workspace)
        context.hooks = self._hooks_runtime or create_hook_runtime(context.workspace)
        return ToolFactory(context)

    def _build_system_prompt(self) -> str:
        """根据当前 prompt style 构建系统提示词。"""
        self.prompt_builder.prompt_style = self.prompt_style
        self.prompt_builder.agent_mode = self.agent_mode
        self.prompt_builder.project_context = self.context
        return self.prompt_builder.build_system_prompt()

    def set_prompt_style(self, style: str) -> str:
        """切换系统提示词风格并重建 Agent。"""
        self.prompt_style = normalize_prompt_style(style)
        self.system_prompt = self._build_system_prompt()
        self._create_agent()
        return self.prompt_style

    def set_agent_mode(self, mode: str) -> str:
        """切换 Agent 工作模式并重建系统提示词。"""
        self.agent_mode = normalize_agent_mode(mode) or "build"
        self.system_prompt = self._build_system_prompt()
        self._create_agent()
        return self.agent_mode

    def _enhance_user_input(self, user_input: str) -> str:
        """旧版技能增强链路已停用，直接返回原始输入。"""
        return user_input

    def _build_messages(
        self,
        effective_input: str,
        include_context: bool = True,
    ) -> List[Union[SystemMessage, HumanMessage, AIMessage]]:
        """构建发送给 Agent/模型的消息列表。"""
        # 在构建消息前触发上下文压缩检测
        self.session.maybe_compact()

        if include_context:
            self.prompt_builder.project_context = self.context

        # 构建系统提醒状态（纯数据，无 I/O）
        from .i18n import get_effective_language
        reminder_state = {
            "agent_mode": self.agent_mode,
            "context_usage": getattr(self.session, "usage_ratio", 0.0),
            "language": get_effective_language(),
        }

        return self.prompt_builder.build_messages(
            effective_input=effective_input,
            session=self.session,
            system_prompt=self.system_prompt,
            include_context=include_context,
            reminder_state=reminder_state,
        )

    def _prepare_messages(
        self,
        user_input: str,
        include_context: bool = True,
    ) -> tuple[str, List[Union[SystemMessage, HumanMessage, AIMessage]]]:
        """记录本轮输入并构建统一消息列表。"""
        self.conversation_manager.session = self.session
        self.conversation_manager.memory = self.memory
        original_input, effective_input = self.conversation_manager.start_turn(
            user_input,
            enhancer=self._enhance_user_input,
        )
        return original_input, self._build_messages(effective_input, include_context=include_context)

    @staticmethod
    def _coerce_stream_delta(delta: str, full_response: str) -> str:
        """兼容累计快照和真实增量，避免吞掉合法重复文本。"""
        if not delta:
            return ""
        if full_response and delta.startswith(full_response):
            return delta[len(full_response):]
        return delta

    def _invoke_with_messages(
        self,
        messages: List[Union[SystemMessage, HumanMessage, AIMessage]],
    ) -> str:
        """统一执行 Agent 或模型，并返回文本响应。"""
        # ToolExecutionSession 守卫：如果不在执行上下文中，自动进入
        from .tools.context import get_abort_controller
        if get_abort_controller() is not None and self._abort_controller._aborted:
            return f"⚠️ 执行已中止（{self._abort_controller.reason}）"

        if self.runner and self.runner.agent:
            result = self.runner.invoke(messages)
            if result is None:
                response = self.model.chat([message_to_chat_dict(message) for message in messages])
                return response
            self._record_agent_usage(result)
            return self._extract_response(result)

        response = self.model.chat([message_to_chat_dict(message) for message in messages])
        return response

    def _record_agent_usage(self, result: Dict[str, Any]) -> None:
        """从 LangGraph Agent 的 invoke 结果中提取并记录 Token 用量。"""
        if not hasattr(self.model, "_record_usage"):
            return

        from .models.base import TokenUsage

        messages = result.get("messages", [])
        for msg in reversed(messages):
            usage_data = None

            # LangChain 标准 usage_metadata
            if hasattr(msg, "usage_metadata") and msg.usage_metadata:
                meta = msg.usage_metadata
                usage_data = meta
            # response_metadata 中的 token_usage
            elif hasattr(msg, "response_metadata") and msg.response_metadata:
                meta = msg.response_metadata
                usage_data = meta.get("token_usage") or meta.get("usage")

            if usage_data:
                if isinstance(usage_data, dict):
                    usage = TokenUsage(
                        prompt_tokens=int(usage_data.get("input_tokens", usage_data.get("prompt_tokens", 0))),
                        completion_tokens=int(usage_data.get("output_tokens", usage_data.get("completion_tokens", 0))),
                        total_tokens=int(usage_data.get("total_tokens", 0) or 0),
                    )
                else:
                    usage = TokenUsage(
                        prompt_tokens=int(getattr(usage_data, "input_tokens", getattr(usage_data, "prompt_tokens", 0))),
                        completion_tokens=int(getattr(usage_data, "output_tokens", getattr(usage_data, "completion_tokens", 0))),
                        total_tokens=int(getattr(usage_data, "total_tokens", 0) or 0),
                    )
                if usage.total_tokens > 0:
                    self.model._record_usage(usage)
                    return

            # 回退：尝试从 tool_calls 消息的 metadata 找
            if isinstance(msg, AIMessage):
                additional_kwargs = getattr(msg, "additional_kwargs", {}) or {}
                if isinstance(additional_kwargs, dict):
                    usage = additional_kwargs.get("usage")
                    if usage and isinstance(usage, dict):
                        token_usage = TokenUsage(
                            prompt_tokens=int(usage.get("input_tokens", usage.get("prompt_tokens", 0))),
                            completion_tokens=int(usage.get("output_tokens", usage.get("completion_tokens", 0))),
                            total_tokens=int(usage.get("total_tokens", 0) or 0),
                        )
                        if token_usage.total_tokens > 0:
                            self.model._record_usage(token_usage)
                            return

        # 一条都没找到 → 估算
        self._estimate_agent_usage(result)

    def _record_stream_usage(self, chunk: Any) -> None:
        """从 LangGraph 流式输出的最后 chunk 中提取 Token 用量。"""
        if not hasattr(self.model, "_record_usage"):
            return

        from .models.base import TokenUsage

        # 递归搜索 chunk 中的 AIMessage 以获取 usage_metadata
        def _find_usage(data: Any) -> Optional[TokenUsage]:
            if isinstance(data, dict):
                # 检查消息列表
                for key in ("messages", "agent", "tools"):
                    msgs = data.get(key)
                    if isinstance(msgs, dict) and "messages" in msgs:
                        msgs = msgs["messages"]
                    if isinstance(msgs, list):
                        for msg in reversed(msgs):
                            result = _extract_from_message(msg)
                            if result:
                                return result
                # 递归其他值
                for value in data.values():
                    result = _find_usage(value)
                    if result:
                        return result
            elif isinstance(data, (list, tuple)):
                for item in reversed(data):
                    result = _find_usage(item)
                    if result:
                        return result
            return None

        def _extract_from_message(msg: Any) -> Optional[TokenUsage]:
            usage_meta = None
            if hasattr(msg, "usage_metadata") and msg.usage_metadata:
                usage_meta = msg.usage_metadata
            elif hasattr(msg, "response_metadata") and msg.response_metadata:
                usage_meta = msg.response_metadata.get("token_usage") or msg.response_metadata.get("usage")

            if usage_meta:
                if isinstance(usage_meta, dict):
                    usage = TokenUsage(
                        prompt_tokens=int(usage_meta.get("input_tokens", usage_meta.get("prompt_tokens", 0))),
                        completion_tokens=int(usage_meta.get("output_tokens", usage_meta.get("completion_tokens", 0))),
                        total_tokens=int(usage_meta.get("total_tokens", 0) or 0),
                    )
                else:
                    usage = TokenUsage(
                        prompt_tokens=int(getattr(usage_meta, "input_tokens", getattr(usage_meta, "prompt_tokens", 0))),
                        completion_tokens=int(getattr(usage_meta, "output_tokens", getattr(usage_meta, "completion_tokens", 0))),
                        total_tokens=int(getattr(usage_meta, "total_tokens", 0) or 0),
                    )
                if usage.total_tokens > 0:
                    return usage
            return None

        usage = _find_usage(chunk)
        if usage:
            self.model._record_usage(usage)

    def _estimate_agent_usage(self, result: Dict[str, Any]) -> None:
        """当 API 未返回用量时的粗略估算。"""
        if not hasattr(self.model, "_record_usage"):
            return

        from .models.base import TokenUsage

        messages = result.get("messages", [])
        prompt_chars = 0
        completion_chars = 0
        for msg in messages:
            content = content_to_text(msg.content) if hasattr(msg, "content") else ""
            if isinstance(msg, (HumanMessage, SystemMessage)) or getattr(msg, "type", "") in ("human", "system"):
                prompt_chars += len(content)
            elif isinstance(msg, AIMessage) or getattr(msg, "type", "") in ("ai", "assistant"):
                completion_chars += len(content)

        prompt_tokens = max(1, prompt_chars // 3)
        completion_tokens = max(1, completion_chars // 3)
        self.model._record_usage(TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ))

    def _iter_agent_stream(
        self,
        messages: List[Union[SystemMessage, HumanMessage, AIMessage]],
    ):
        """兼容不同 LangGraph 版本的流式接口。"""
        if not self.runner:
            return None
        return self.runner.stream(messages)

    def _extract_stream_delta(self, chunk: Any) -> tuple[str, bool]:
        """
        从 Agent 流式事件中提取增量文本和元信息。

        Returns:
            (delta_text, is_tool_call): delta_text 为本次增量内容，is_tool_call 表示是否为工具调用
        """
        # 处理 dict 类型的流输出（LangGraph updates/values 模式）
        if isinstance(chunk, dict):
            # 检查是否有 agent 节点的消息更新
            if "agent" in chunk:
                agent_data = chunk["agent"]
                if isinstance(agent_data, dict) and "messages" in agent_data:
                    msgs = agent_data["messages"]
                    if msgs:
                        last_msg = msgs[-1]
                        return self._extract_message_delta(last_msg)
                if isinstance(agent_data, list) and agent_data:
                    return self._extract_message_delta(agent_data[-1])

            # 检查 tools 节点：提取工具执行反馈
            if "tools" in chunk:
                tools_data = chunk["tools"]
                if isinstance(tools_data, dict) and "messages" in tools_data:
                    msgs = tools_data["messages"]
                    if msgs:
                        return self._extract_tool_result(msgs[-1])
                if isinstance(tools_data, list) and tools_data:
                    return self._extract_tool_result(tools_data[-1])

            if "messages" in chunk:
                msgs = chunk["messages"]
                if msgs:
                    return self._extract_message_delta(msgs[-1])

            # 递归检查其他值，跳过已处理的 LangGraph 标准键
            _visited = {"agent", "tools", "messages"}
            for key, value in chunk.items():
                if key in _visited:
                    continue
                delta, is_tool = self._extract_stream_delta(value)
                if delta or is_tool:
                    return delta, is_tool
            return "", False

        if isinstance(chunk, tuple):
            for item in chunk:
                delta, is_tool = self._extract_stream_delta(item)
                if delta or is_tool:
                    return delta, is_tool
            return "", False

        return self._extract_message_delta(chunk)

    @staticmethod
    def _format_tool_call_label(tool_names: list[str]) -> str:
        if len(tool_names) == 1:
            return tool_names[0]
        from collections import Counter
        counts = Counter(tool_names)
        return ", ".join(f"{name} ×{n}" if n > 1 else name for name, n in counts.items())

    def _extract_message_delta(self, msg: Any) -> tuple[str, bool]:
        """从单条消息中提取增量文本和工具调用标记。"""
        kind = message_kind(msg)

        if isinstance(msg, ToolMessage) or kind == "tool":
            return self._extract_tool_result(msg)

        if isinstance(msg, (HumanMessage, SystemMessage)) or kind in {"human", "user", "system"}:
            return "", False

        is_ai_message = isinstance(msg, AIMessage) or kind in {"ai", "assistant", "aimessagechunk"}
        if is_ai_message:
            tool_calls = getattr(msg, "tool_calls", None) or []
            if tool_calls:
                tool_names = extract_tool_names(tool_calls)
                label = SAIAgent._format_tool_call_label(tool_names)
                return f"[调用工具: {label}]", True

            return content_to_text(getattr(msg, "content", "")), False

        if hasattr(msg, "content"):
            return "", False

        if isinstance(msg, str):
            return msg, False

        return "", False

    def _extract_tool_result(self, msg: Any) -> tuple[str, bool]:
        """从 ToolMessage 中提取工具执行结果反馈。"""
        if hasattr(msg, "content"):
            content = content_to_text(getattr(msg, "content", ""))
            tool_name = getattr(msg, "name", "") or ""
            if not tool_name:
                tool_name = getattr(msg, "tool_call_id", "") or "tool"
            # 如果工具返回错误，显式标记
            if content.startswith("工具执行失败") or content.startswith("❌") or content.startswith("⚠️"):
                return f"[工具执行出错: {tool_name} | {content}]", True
            # 截断过长的成功反馈
            if len(content) > 200:
                content = content[:200] + "..."
            return f"[工具结果: {tool_name} | {content}]", True
        return "", False

    def _extract_stream_text(self, chunk: Any) -> str:
        """从 Agent 流式事件中提取可显示文本（向后兼容）。"""
        delta, _ = self._extract_stream_delta(chunk)
        return delta

    def _load_mcp_tools(self) -> List[BaseTool]:
        """Load trusted MCP tools from the current workspace."""
        if self._mcp_runtime is not None:
            self._mcp_runtime.shutdown()
            self._mcp_runtime = None
        if not self._enable_mcp:
            return []
        try:
            from .core.mcp_runtime import MCPRuntime

            runtime = MCPRuntime(
                permissions=self._permissions_runtime,
                hooks=self._hooks_runtime,
            )
            runtime.configure_workspace(self.workspace)
            self._mcp_runtime = runtime
            return runtime.load_tools(self._mcp_servers or None)
        except Exception as exc:
            print(tr("agent.warn_mcp_load", error=str(exc)))
            return []

    def _normalize_tools(self, tools: List[Any]) -> List[Any]:
        """确保工具对象具备稳定 name，去重并按 Agent 使用优先级排序。"""
        normalized_tools = []
        seen_names = set()

        for index, tool in enumerate(tools):
            if tool is None:
                continue

            tool_name = getattr(tool, "name", None)

            if not isinstance(tool_name, str) or not tool_name.strip():
                fallback_name = getattr(tool, "_mock_name", None)
                if not fallback_name:
                    fallback_name = getattr(tool, "__name__", None)
                if not fallback_name:
                    fallback_name = tool.__class__.__name__ or f"tool_{index}"

                try:
                    setattr(tool, "name", str(fallback_name))
                except Exception:
                    # 静默忽略：某些工具对象不允许修改 name 属性
                    pass

                tool_name = str(fallback_name)

            normalized_name = str(tool_name).strip()
            if not normalized_name or normalized_name in seen_names:
                continue

            seen_names.add(normalized_name)
            normalized_tools.append((index, tool))

        normalized_tools.sort(
            key=lambda item: (
                TOOL_PRIORITY.get(getattr(item[1], "name", ""), 1000),
                item[0],
            )
        )
        return [tool for _, tool in normalized_tools]

    def _create_agent(self):
        """创建 LangGraph Agent"""
        self.runner = AgentRunner(
            model=self.model,
            tools=self.tools,
            system_prompt=self.system_prompt,
        )
        self.agent = self.runner.rebuild()
        self._model_with_tools = self.runner.model_with_tools

    def _tool_execution_context(self) -> ToolExecutionContext:
        """Return the runtime-bound tool execution context for this facade."""
        return ToolExecutionContext(
            workspace=self.workspace,
            permissions=self._permissions_runtime,
            hooks=self._hooks_runtime,
            mode=self.agent_mode,
        )

    def run(
        self,
        user_input: str,
        include_context: bool = True
    ) -> str:
        """
        执行 Agent（非流式）— 含恢复路径。

        恢复路径（参考 Claude Code query.ts）：
        1. recoverable → 指数退避重试（最多 3 次）
        2. max_output_tokens → 注入延续消息后重试
        3. prompt_too_long → 触发上下文压缩后重试
        """
        self._turn_count += 1
        turn_state = TurnState(
            transition=TurnTransition.NEXT_TURN,
            turn_count=self._turn_count,
        )
        self._abort_controller.reset()
        self._recovery_state = {"attempt": 0, "path": ""}

        with tool_execution_session(self._tool_execution_context()):
            original_input, messages = self._prepare_messages(
                user_input,
                include_context=include_context,
            )

            response = ""
            while self._recovery_state["attempt"] <= _MAX_RETRIES:
                try:
                    response = self._invoke_with_messages(messages)
                    turn_state.transition = TurnTransition.COMPLETED
                    break
                except Exception as e:
                    error_msg = str(e)
                    category = _classify_error(error_msg)
                    self._recovery_state["attempt"] += 1
                    attempt = self._recovery_state["attempt"]

                    if category == "fatal" or attempt > _MAX_RETRIES:
                        response = f"执行出错: {error_msg}"
                        turn_state.transition = TurnTransition.MODEL_ERROR
                        if attempt > _MAX_RETRIES:
                            turn_state.transition = TurnTransition.MAX_RETRIES
                        turn_state.error_message = error_msg
                        break

                    if category == "recoverable":
                        self._recovery_state["path"] = "retry_backoff"
                        delay = _retry_delay(attempt)
                        time.sleep(delay)
                        continue

                    if category == "max_output_tokens":
                        self._recovery_state["path"] = "max_output_tokens_recovery"
                        messages = list(messages)
                        messages.append(HumanMessage(
                            content="Output token limit hit. Resume directly — no apology, "
                                    "no recap of what you were doing. Pick up mid-thought "
                                    "if that is where the cut happened."
                        ))
                        continue

                    if category == "prompt_too_long":
                        self._recovery_state["path"] = "compact_retry"
                        try:
                            self.session.compact()
                            messages = self._build_messages(effective_input=user_input, include_context=include_context)
                        except Exception:
                            pass
                        continue

            if not response:
                response = "执行出错: 所有恢复路径均已耗尽"
                turn_state.transition = TurnTransition.MAX_RETRIES

        # 记录交互，保留 additional_kwargs 供下一轮透传
        metadata = {"additional_kwargs": dict(self._last_extra)} if self._last_extra else {}
        self._last_extra.clear()
        self.conversation_manager.finish_turn(original_input, response, metadata=metadata)

        return response

    def stream_run(
        self,
        user_input: str,
        include_context: bool = True
    ) -> Iterator[str]:
        """
        执行 Agent（流式输出）— 含恢复路径。

        恢复路径：
        1. 流中断 → 用非流式续完
        2. recoverable → 指数退避重试
        3. max_output_tokens → 注入延续消息后重试
        4. prompt_too_long → 触发压缩后重试
        """
        self._turn_count += 1
        self._abort_controller.reset()
        self._recovery_state = {"attempt": 0, "path": ""}

        with tool_execution_session(self._tool_execution_context()):
            original_input, messages = self._prepare_messages(
                user_input,
                include_context=include_context,
            )

            full_response = ""

            while self._recovery_state["attempt"] <= _MAX_RETRIES:
                try:
                    stream_iter = self._iter_agent_stream(messages)

                    if stream_iter is not None:
                        try:
                            last_chunk = None
                            for chunk in stream_iter:
                                last_chunk = chunk
                                delta, is_tool_call = self._extract_stream_delta(chunk)
                                if not delta:
                                    continue

                                if is_tool_call:
                                    if self.stream_callback:
                                        self.stream_callback(delta)
                                    else:
                                        yield delta
                                    continue

                                actual_delta = self._coerce_stream_delta(delta, full_response)
                                full_response += actual_delta

                                if actual_delta:
                                    if self.stream_callback:
                                        self.stream_callback(actual_delta)
                                    else:
                                        yield actual_delta

                            if last_chunk is not None:
                                self._record_stream_usage(last_chunk)

                        except Exception as stream_err:
                            error_msg = str(stream_err)
                            category = _classify_error(error_msg)

                            if category == "recoverable":
                                self._recovery_state["attempt"] += 1
                                self._recovery_state["path"] = "retry_backoff"
                                time.sleep(_retry_delay(self._recovery_state["attempt"]))
                                continue

                            if full_response:
                                continuation = self._continue_after_stream_interrupt(messages, full_response)
                                if continuation:
                                    full_response += continuation
                                    if self.stream_callback:
                                        self.stream_callback(continuation)
                                    else:
                                        yield continuation
                                    break
                            else:
                                fallback = self._invoke_with_messages(messages)
                                full_response = fallback
                                if self.stream_callback:
                                    self.stream_callback(fallback)
                                else:
                                    yield fallback
                                break

                        if not full_response:
                            fallback = self._invoke_with_messages(messages)
                            full_response = fallback
                            if self.stream_callback:
                                self.stream_callback(fallback)
                            else:
                                yield fallback

                    elif hasattr(self.model, 'chat_stream'):
                        chat_messages = [message_to_chat_dict(message) for message in messages]
                        for chunk in self.model.chat_stream(chat_messages):
                            full_response += chunk
                            if self.stream_callback:
                                self.stream_callback(chunk)
                            else:
                                yield chunk

                    else:
                        fallback = self._invoke_with_messages(messages)
                        full_response = fallback
                        if self.stream_callback:
                            self.stream_callback(fallback)
                        else:
                            yield fallback

                    break  # 成功完成，退出重试循环

                except Exception as e:
                    error_msg = str(e)
                    category = _classify_error(error_msg)
                    self._recovery_state["attempt"] += 1
                    attempt = self._recovery_state["attempt"]

                    if category == "fatal" or attempt > _MAX_RETRIES:
                        if not full_response:
                            full_response = f"执行出错: {error_msg}"
                        yield full_response
                        self._last_extra.clear()
                        self.conversation_manager.finish_turn(original_input, full_response)
                        return

                    if category == "recoverable":
                        self._recovery_state["path"] = "retry_backoff"
                        time.sleep(_retry_delay(attempt))
                        continue

                    if category == "max_output_tokens":
                        self._recovery_state["path"] = "max_output_tokens_recovery"
                        messages = list(messages)
                        messages.append(HumanMessage(
                            content="Output token limit hit. Resume directly — no apology. "
                                    "Pick up mid-thought if that is where the cut happened."
                        ))
                        continue

                    if category == "prompt_too_long":
                        self._recovery_state["path"] = "compact_retry"
                        try:
                            self.session.compact()
                            messages = self._build_messages(effective_input=user_input, include_context=include_context)
                        except Exception:
                            pass
                        continue

            # 记录完整交互到记忆和会话
            metadata = {"additional_kwargs": dict(self._last_extra)} if self._last_extra else {}
            self._last_extra.clear()
            self.conversation_manager.finish_turn(original_input, full_response, metadata=metadata)

    def _continue_after_stream_interrupt(
        self,
        messages: List[Union[SystemMessage, HumanMessage, AIMessage]],
        partial_response: str,
    ) -> str:
        """
        流式中断后，将已生成的部分回复追加到上下文中，用非流式方式续完。

        这确保模型不会丢失任务上下文，避免重新生成开场白。
        """
        try:
            # 构建延续消息：追加 assistant 的部分回复作为历史
            continuation_messages = list(messages)
            continuation_messages.append(AIMessage(content=partial_response))
            continuation_messages.append(
                HumanMessage(content="请继续完成上面的回复，不要重复已输出的内容。")
            )
            return self._invoke_with_messages(continuation_messages)
        except Exception as e:
            print(tr("agent.stream_continue_failed", error=str(e)))
            return ""

    def get_context_summary(self) -> str:
        """
        获取项目上下文摘要

        Returns:
            格式化的上下文摘要
        """
        return self.context.get_context_for_llm(max_files=20)

    def get_memory_summary(self) -> str:
        """
        获取记忆摘要

        Returns:
            记忆摘要文本
        """
        return self.memory.summarize()

    def get_recent_history(self, n: int = 5) -> str:
        """
        获取最近的对话历史

        Args:
            n: 获取最近 n 轮

        Returns:
            格式化的历史
        """
        return self.memory.get_recent_context(n)

    def _extract_response(self, result: Dict) -> str:
        """从 Agent 结果中提取回复，同时保留 additional_kwargs 供多轮对话。"""
        if isinstance(result, dict) and 'messages' in result:
            messages = result['messages']
            for msg in reversed(messages):
                if isinstance(msg, AIMessage) or getattr(msg, "type", None) == "ai":
                    # 保留 additional_kwargs（reasoning_content / thinking / tool_calls 等）
                    extra = getattr(msg, "additional_kwargs", {}) or {}
                    if extra:
                        self._last_extra = dict(extra)
                    content = msg.content
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
                        if text_parts:
                            return "\n".join(text_parts)
                    return str(content)
            return ""
        return str(result)

    def analyze_project(self) -> str:
        """分析当前项目"""
        self.context.scan()
        return self.context.get_context_for_llm()

    def get_tool_list(self) -> List[str]:
        """获取可用工具列表"""
        return [tool.name for tool in self.tools]

    def get_mcp_tool_list(self) -> List[Dict[str, Any]]:
        """
        获取 MCP 工具列表

        Returns:
            MCP 工具列表
        """
        try:
            if self._mcp_runtime is None:
                return []
            return self._mcp_runtime.status().get("tools", [])
        except Exception as e:
            print(tr("agent.mcp_tool_list_failed", error=str(e)))
            return []

    def get_stats(self) -> Dict[str, Any]:
        """获取 Agent 统计信息（含 Token 用量）"""
        stats = {
            "workspace": str(self.workspace),
            "model": str(self.model),
            "tools_count": len(self.tools),
            "base_tools_count": len(self._base_tools),
            "session_messages": self.session.get_message_count(),
            "memory_interactions": len(self.memory),
            "context_files": len(self.context.files),
            "modified_files": len(self.memory.get_modified_files()),
            "prompt_style": self.prompt_style,
            "agent_mode": self.agent_mode,
        }

        # 添加 Token 用量统计
        if hasattr(self.model, "last_usage") and self.model.last_usage is not None:
            last = self.model.last_usage
            stats["last_prompt_tokens"] = last.prompt_tokens
            stats["last_completion_tokens"] = last.completion_tokens
            stats["last_total_tokens"] = last.total_tokens

        if hasattr(self.model, "session_usage") and self.model.session_usage is not None:
            sess = self.model.session_usage
            stats["session_prompt_tokens"] = sess.prompt_tokens
            stats["session_completion_tokens"] = sess.completion_tokens
            stats["session_total_tokens"] = sess.total_tokens

        return stats

    def reset(self, clear_memory: bool = True, clear_session: bool = True):
        """
        重置 Agent

        Args:
            clear_memory: 是否清空记忆
            clear_session: 是否清空会话
        """
        if clear_memory:
            self.memory.clear()

        if clear_session:
            self.session.clear()

        # 重新分析项目
        self.context.scan()

    # =============================================================================
    # MCP 相关方法
    # =============================================================================

    def get_mcp_registry(self) -> Optional[Any]:
        """
        获取 MCP 注册表

        Returns:
            兼容旧接口，始终返回 None
        """
        try:
            if self._mcp_runtime is None:
                return None
            return self._mcp_runtime.status()
        except Exception as e:
            print(tr("agent.mcp_registry_failed", error=str(e)))
            return None

    def reload_mcp_tools(self):
        """Reload trusted MCP tools and rebuild the Agent."""
        self._mcp_tools = self._load_mcp_tools()
        self.tools = self._normalize_tools([*self._base_tools, *self._mcp_tools])
        self._create_agent()
        return self.get_mcp_tool_list()

    def close(self) -> None:
        """Release runtime-owned resources."""
        if self._mcp_runtime is not None:
            self._mcp_runtime.shutdown()
            self._mcp_runtime = None

    def shutdown(self) -> None:
        """Compatibility alias for close()."""
        self.close()

    async def execute_mcp_tool(self, tool_name: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """
        执行 MCP 工具

        Args:
            tool_name: 工具名称
            params: 工具参数

        Returns:
            工具执行结果
        """
        if self._mcp_runtime is None:
            return "❌ MCP runtime is not initialized"

        with tool_execution_session(self._tool_execution_context()):
            return self._mcp_runtime.call_tool(tool_name, params or {})

# ==============================================================================
# 便捷工厂函数
# ==============================================================================

def create_sai_agent(
    model_type: str = "ollama",
    model_name: str = "llama3.2",
    workspace: str = ".",
    prompt_style: str = "standard",
    agent_mode: str = "build",
    enable_mcp: bool = False,
    mcp_servers: Optional[List[str]] = None,
    **model_kwargs
) -> SAIAgent:
    """
    创建 SAYA Agent 的便捷函数

    Args:
        model_type: 模型类型 (ollama/openai/azure)
        model_name: 模型名称
        workspace: 工作区路径
        enable_mcp: 兼容旧接口，已忽略
        mcp_servers: 兼容旧接口，已忽略
        **model_kwargs: 其他模型参数

    Returns:
        SAIAgent 实例
    """
    # 创建模型
    model = get_model_provider_registry().create_model(
        model_type,
        model_name=model_name,
        **model_kwargs
    )

    # 创建 Agent
    return SAIAgent(
        model=model,
        workspace=Path(workspace),
        prompt_style=prompt_style,
        agent_mode=agent_mode,
        enable_mcp=enable_mcp,
        mcp_servers=mcp_servers,
    )


# ==============================================================================
# 导出
# ==============================================================================

__all__ = [
    'SAIAgent',
    'create_sai_agent',
]
