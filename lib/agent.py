"""
Agent 主逻辑

使用 langchain.agents.create_agent 构建智能 Agent（单工厂，中间件+可选持久化）。

功能：
- 基于 ReAct 模式的推理和行动
- 工具注册和调用
- 记忆管理
- 流式输出支持
- 安全检查集成
"""

import logging
from typing import List, Optional, Dict, Any, Iterator, Union, Callable
from pathlib import Path
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langchain_core.tools import BaseTool

# 导入项目模块
from .core.agent_runtime import AgentRunner, message_to_chat_dict
from . import agent_recovery as _recovery
from .core.safety import SafetyChecker
from .core.context import ProjectContext
from .core.session import SessionManager, SessionDerivedMemoryView
from .core.modes import normalize_agent_mode
from .models import BaseModel
from .models.registry import get_model_provider_registry
from .runtime.context import RuntimeContext
from .tools.context import ToolAbortController, ToolExecutionContext, tool_execution_session
from .core.agent_runtime import TurnTransition, TurnState
from .core.hooks import create_hook_runtime
from .core.permissions import create_permission_runtime
from .prompts import normalize_prompt_style
from .i18n import tr

logger = logging.getLogger(__name__)


TOOL_PRIORITY = {
    # 先发现或编排工具，再调用专用操作。
    "ToolSearch": 1,
    "invoke_tool": 2,
    "batch_execute": 3,
    # 先理解项目。
    "analyze_project": 10,
    "get_project_summary": 11,
    "list_project_files": 12,
    "get_file_info": 13,
    # 先搜索和读取，再编辑。
    "glob_search": 20,
    "grep_search": 21,
    "web_search": 22,
    "list_directory": 23,
    "read_file": 24,
    # 先做窄范围文件编辑，再做范围更大的操作。
    "search_replace": 30,
    "write_file": 31,
    "create_directory": 32,
    "delete_file": 39,
    # 先做 Git 检查，再做变更。
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
    # 先做 Shell 诊断，再执行命令。
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
        memory_manager: Optional[Any] = None,
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
        tool_registry: Optional[Any] = None,
        interrupt_handler: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
        checkpoint_path: Optional[str] = None,
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
        self._tool_registry = tool_registry

        # 使用提供的工具或 runtime-bound 默认工具
        self._base_tools = self._normalize_tools(
            tools if tools is not None else self._build_default_tools(agent_mode)
        )

        # 初始化管理器：历史唯一真相源为 session（+ checkpointer 持久化），
        # 记忆为会话派生只读视图（不再双写 MemoryManager 镜像）。
        self.session = session_manager or SessionManager()
        self.memory = memory_manager if memory_manager is not None else SessionDerivedMemoryView(self.session)
        self.safety = safety_checker or SafetyChecker(workspace_root=self.workspace)
        self.context = project_context or ProjectContext(str(self.workspace))

        # 从模型读取上下文窗口信息并同步到 SessionManager
        if hasattr(self.model, "context_window") and self.model.context_window > 0:
            self.session.set_context_limit(self.model.context_window)

        # 设置上下文压缩回调（使用模型生成语义摘要）
        if hasattr(self.model, "chat"):
            self.session.set_compact_fn(self.model.chat)

        self.prompt_style = normalize_prompt_style(prompt_style)
        self.agent_mode = normalize_agent_mode(agent_mode) or "build"

        # MCP stdio 工具只在 workspace 安全/信任配置完成后才加载。
        self._enable_mcp = bool(enable_mcp)
        self._mcp_servers = list(mcp_servers or [])
        self._mcp_registry: Optional[Any] = None
        self._mcp_runtime: Optional[Any] = None
        self._mcp_tools: List[BaseTool] = self._load_mcp_tools()

        # 合并所有工具
        self.tools = self._compose_runtime_tools()

        # Turn 状态追踪
        self._turn_count = 0
        self._abort_controller = ToolAbortController()
        self._last_extra: dict = {}  # additional_kwargs 跨轮保留
        self._recovery_state: dict = {}  # 追踪恢复路径重试次数
        # 本轮是否已从 LangGraph 的 messages 模式拿到逐 token 增量。
        # 拿到之后，updates 模式里同一条 AI 消息的正文就不要再发一次（否则整段回答会出现两遍）。
        self._stream_tokens_seen = False
        self.last_turn_state = TurnState(turn_count=0)

        # 系统提示词
        self.system_prompt = system_prompt or self._build_system_prompt()

        # 流式输出回调
        self.stream_callback = stream_callback

        # 图中断恢复：ask 权限走框架 interrupt() 停住，答案由调用方给。
        # 交互层传确认窗，headless 传自动拒绝；None = 一律拒绝（fail-closed）。
        self.interrupt_handler = interrupt_handler
        # 图持久化位置（workspace state 目录下的 sqlite）。None = 无持久化
        # （每次传全量，中间件仍在），单测与旧调用方不受影响。
        self.checkpoint_path = checkpoint_path

        self.runner: Optional[AgentRunner] = None

        # 创建 LangGraph Agent
        self._create_agent()

    def _build_default_tools(self, agent_mode: str) -> List[BaseTool]:
        """为兼容 facade 构建 runtime-bound 默认工具。"""
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
        if self._tool_registry is None:
            from .tools import ToolRegistry

            self._tool_registry = ToolRegistry(context)
        return self._tool_registry.build_tools()

    def _compose_runtime_tools(self) -> List[BaseTool]:
        """基于完整实时 catalog 重建编排工具。"""
        if self._tool_registry is not None and hasattr(self._tool_registry, "compose_tools"):
            tools = self._normalize_tools(self._tool_registry.compose_tools(self._mcp_tools))
            runtime_context = getattr(self._tool_registry, "context", None)
            if runtime_context is not None and hasattr(runtime_context, "attach_tools"):
                runtime_context.attach_tools(tools, registry=self._tool_registry)
            return tools
        return self._normalize_tools([*self._base_tools, *self._mcp_tools])

    def _build_system_prompt(self) -> str:
        """根据当前 prompt style 构建系统提示词（组装语义见 agent_recovery）。"""
        return _recovery.build_system_prompt_text(
            self.workspace, self.context, self.prompt_style, self.agent_mode
        )

    def set_prompt_style(self, style: str) -> str:
        """切换系统提示词风格并重建 Agent。"""
        self.prompt_style = normalize_prompt_style(style)
        self.system_prompt = self._build_system_prompt()
        self._create_agent()
        return self.prompt_style

    def set_agent_mode(self, mode: str) -> str:
        """切换 Agent 工作模式并重建系统提示词。"""
        try:
            self.agent_mode = normalize_agent_mode(mode) or "build"
        except ValueError:
            self.agent_mode = "build"
        self.system_prompt = self._build_system_prompt()
        self._create_agent()
        return self.agent_mode

    def _enhance_user_input(self, user_input: str) -> str:
        """旧版技能增强链路已停用，直接返回原始输入。"""
        return user_input

    def _reminder_state(self) -> Dict[str, Any]:
        """构建系统提醒状态（纯数据，无 I/O）。"""
        from .i18n import get_effective_language
        return {
            "agent_mode": self.agent_mode,
            "context_usage": getattr(self.session, "usage_ratio", 0.0),
            "language": get_effective_language(),
        }

    def _build_messages(
        self,
        effective_input: str,
        include_context: bool = True,
    ) -> List[Union[SystemMessage, HumanMessage, AIMessage]]:
        """构建发送给 Agent/模型的消息列表（无持久化时每次传全量）。"""
        # 在构建消息前触发上下文压缩检测
        self.session.maybe_compact()

        return [
            SystemMessage(
                content=_recovery.build_system_content(
                    self.workspace,
                    self.context,
                    self.session,
                    self.system_prompt,
                    None,
                    include_context,
                    self._reminder_state(),
                )
            ),
            *_recovery.history_messages(self.session),
            HumanMessage(content=effective_input),
        ]

    def _graph_mode(self) -> bool:
        """是否走图路径（中间件 + checkpointer 就绪）。"""
        runner = getattr(self, "runner", None)
        return bool(runner is not None and runner.graph_enabled)

    def _require_runner(self) -> AgentRunner:
        """拿 runner（图路径专用）：没了就 loud fail，不静默降级——调用方都已判过 _graph_mode()。"""
        if self.runner is None:
            raise RuntimeError("runner 不可用")
        return self.runner

    def _refresh_turn_prompt(self, include_context: bool = True) -> str:
        """组装本轮 system 全文并刷进中间件（与今天"每轮拼一次"同成本）。"""
        system_text = _recovery.build_system_content(
            self.workspace,
            self.context,
            self.session,
            self.system_prompt,
            None,
            include_context,
            self._reminder_state(),
        )
        if self.runner is not None:
            self.runner.refresh_prompt(system_text)
        return system_text

    def _build_graph_import(
        self,
        effective_input: str,
        system_text: str,
    ) -> List[Union[SystemMessage, HumanMessage, AIMessage]]:
        """首轮/压缩同步用的全量消息：与 build_messages 同构，只是不触发压缩
        （调用方已做过），避免一次 turn 里压两次。"""
        return [
            SystemMessage(content=system_text),
            *_recovery.history_messages(self.session),
            HumanMessage(content=effective_input),
        ]

    def _sync_turn_state(
        self,
        effective_input: str,
        include_context: bool = True,
    ) -> List[Union[SystemMessage, HumanMessage, AIMessage]]:
        """图路径的 turn 输入。

        * 空线程（首轮/新进程恢复）：全量导入，压缩产物标记一并进图；
        * 压缩刚发生：镜像被重写，必须用同一份转换覆盖图状态，否则图里还是
          压缩前的消息——压缩就退化成了"只改了镜像"；
        * 平时：只传本轮 HumanMessage，历史由 checkpointer 持有。
        """
        compacted = self.session.maybe_compact()
        system_text = self._refresh_turn_prompt(include_context)
        runner = self._require_runner()
        if runner.thread_message_count() == 0:
            return self._build_graph_import(effective_input, system_text)
        if compacted:
            runner.sync_messages(
                self._build_graph_import(effective_input, system_text)
            )
        return [HumanMessage(content=effective_input)]

    def _reset_graph_state_for_retry(
        self,
        user_input: str,
        include_context: bool = True,
    ) -> None:
        """重试前把图状态重置回镜像（与今天"从镜像重建后重试"同语义）。

        镜像永远是干净的 user/assistant 轮次；线程里的半截 AI/tool 消息被丢掉——
        今天重建全量消息重试同样丢掉它们（流 chunk 从不进 messages 列表）。
        """
        if not self._graph_mode():
            return
        self._require_runner().sync_messages(
            self._build_graph_import(
                user_input, self._refresh_turn_prompt(include_context)
            )
        )

    def _prepare_messages(
        self,
        user_input: str,
        include_context: bool = True,
    ) -> tuple[str, List[Union[SystemMessage, HumanMessage, AIMessage]]]:
        """记录本轮输入并构建统一消息列表。"""
        # 会话切换后派生视图必须跟上新 session，否则记忆摘要停留在旧会话。
        if isinstance(self.memory, SessionDerivedMemoryView) and self.memory._session is not self.session:
            self.memory = SessionDerivedMemoryView(self.session)
        original_input, effective_input = _recovery.start_turn(
            self.session,
            self.memory,
            user_input,
            enhancer=self._enhance_user_input,
        )
        if self._graph_mode():
            # 会话切换（/session）后 thread_id 必须跟上，否则串到别的会话里。
            self._require_runner().thread_id = self.session.session_id
            return original_input, self._sync_turn_state(
                effective_input, include_context=include_context
            )
        return original_input, self._build_messages(effective_input, include_context=include_context)

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
            if self._graph_mode():
                # 非流 invoke 遇到中断是正常返回（result 带 __interrupt__），
                # 不是抛错：必须就地排空，否则本轮只拿到半截状态。
                result = _recovery.drain_invoke_interrupts(self.runner, result, self.interrupt_handler)
            _recovery.record_invoke_result(self.model, result)
            return self._extract_response(result)

        response = self.model.chat([message_to_chat_dict(message) for message in messages])
        return response

    def _iter_agent_stream(
        self,
        messages: List[Union[SystemMessage, HumanMessage, AIMessage]],
    ):
        """兼容不同 LangGraph 版本的流式接口。"""
        if not self.runner:
            return None
        return self.runner.stream(messages)

    def _load_mcp_tools(self) -> List[BaseTool]:
        """从当前 workspace 加载受信任的 MCP 工具。"""
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
        permissions = self._permissions_runtime
        if permissions is None:
            # 与 _build_default_tools 的回退一致：共享会话状态，工作区只影响策略文件。
            from .core.permissions import create_permission_runtime

            permissions = create_permission_runtime(self.workspace)
        self.runner = AgentRunner(
            model=self.model,
            tools=self.tools,
            system_prompt=self.system_prompt,
            permissions=permissions,
            safety_checker=self.safety,
            checkpoint_path=self.checkpoint_path,
            thread_id=self.session.session_id,
        )
        self.agent = self.runner.rebuild()
        self._model_with_tools = self.runner.model_with_tools

    def _tool_execution_context(self) -> ToolExecutionContext:
        """返回此 facade 的 runtime-bound 工具执行上下文。"""
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
        1. recoverable → 指数退避重试（最多 3 次；图模式下图内中间件先退避，外层是整轮兜底）
        2. max_output_tokens → 注入延续消息后重试
        3. prompt_too_long → 触发上下文压缩后重试
        """
        self._turn_count += 1
        turn_state = TurnState(
            transition=TurnTransition.NEXT_TURN,
            turn_count=self._turn_count,
        )
        self.last_turn_state = turn_state
        self._abort_controller.reset()
        self._recovery_state = {"attempt": 0, "path": ""}

        # 可观测走 LangSmith callbacks（ContextVar 调用树已删除）。
        with tool_execution_session(self._tool_execution_context()):
            original_input, messages = self._prepare_messages(
                user_input,
                include_context=include_context,
            )

            response = ""
            while self._recovery_state["attempt"] <= _recovery.MAX_RETRIES:
                try:
                    response = self._invoke_with_messages(messages)
                    turn_state.transition = TurnTransition.COMPLETED
                    break
                except Exception as e:
                    error_msg = str(e)
                    category = _recovery.classify_exception(e)
                    self._recovery_state["attempt"] += 1
                    attempt = self._recovery_state["attempt"]

                    if category == "fatal" or attempt > _recovery.MAX_RETRIES:
                        response = _recovery.format_execution_error(error_msg, self._recovery_state)
                        turn_state.transition = TurnTransition.MODEL_ERROR
                        if attempt > _recovery.MAX_RETRIES:
                            turn_state.transition = TurnTransition.MAX_RETRIES
                        turn_state.error_message = error_msg
                        break

                    if category == "recoverable":
                        messages = _recovery.recover_after_recoverable(
                            self, user_input, messages, attempt, include_context
                        )
                        continue

                    if category == "max_output_tokens":
                        messages = _recovery.recover_after_max_output_tokens(
                            self, user_input, messages, include_context
                        )
                        continue

                    if category == "prompt_too_long":
                        messages = _recovery.recover_after_prompt_too_long(
                            self, user_input, messages, include_context
                        )
                        continue

            if not response:
                turn_state.error_message = "所有恢复路径均已耗尽"
                response = _recovery.format_execution_error(turn_state.error_message, self._recovery_state)
                turn_state.transition = TurnTransition.MAX_RETRIES

        # 记录交互，保留 additional_kwargs 供下一轮透传
        metadata = {"additional_kwargs": dict(self._last_extra)} if self._last_extra else {}
        self._last_extra.clear()
        self.last_turn_state = turn_state
        _recovery.finish_turn(self.session, self.memory, original_input, response, metadata=metadata)
        if self._graph_mode():
            self._require_runner().remember_turn(self._turn_count, original_input, response)

        return response

    def stream_run(
        self,
        user_input: str,
        include_context: bool = True,
        *,
        event_callback: Optional[Callable[[Any], None]] = None,
        emit_tool_status: bool = True,
    ) -> Iterator[str]:
        """
        执行 Agent（流式输出）— 含恢复路径。

        恢复路径：
        1. 流中断 → 用非流式续完
        2. recoverable → 指数退避重试（图模式下图内中间件先退避，外层是整轮兜底）
        3. max_output_tokens → 注入延续消息后重试
        4. prompt_too_long → 触发压缩后重试
        """
        self._turn_count += 1
        turn_state = TurnState(
            transition=TurnTransition.NEXT_TURN,
            turn_count=self._turn_count,
        )
        self.last_turn_state = turn_state
        self._abort_controller.reset()
        self._recovery_state = {"attempt": 0, "path": ""}
        self._stream_tokens_seen = False

        with tool_execution_session(self._tool_execution_context()):
            original_input, messages = self._prepare_messages(
                user_input,
                include_context=include_context,
            )

            full_response = ""

            while self._recovery_state["attempt"] <= _recovery.MAX_RETRIES:
                try:
                    stream_iter = self._iter_agent_stream(messages)

                    if stream_iter is not None:
                        try:
                            last_chunk = None
                            pending = stream_iter
                            while pending is not None:
                                interrupted = False
                                for chunk in pending:
                                    if event_callback is not None:
                                        event_callback(chunk)
                                    interrupts = _recovery.detect_interrupt(chunk)
                                    if interrupts is not None:
                                        # 工具询问：handler 拿答案后 Command(resume=…) 继续
                                        # 同一个 while 循环——新迭代器，无缝接上。
                                        pending = _recovery.resume_after_interrupt(
                                            self.runner, interrupts, self.interrupt_handler
                                        )
                                        interrupted = True
                                        break
                                    last_chunk = chunk
                                    event = _recovery.extract_stream_delta(self, chunk)
                                    if event is None or not event.display_text:
                                        continue
                                    delta = event.display_text
                                    # reasoning 与工具事件都走状态通道（受 emit_tool_call 门控），
                                    # 沿用旧协议：reasoning 走状态通道，不计入正文。
                                    if event.kind in {"tool_start", "tool_result", "tool_error", "reasoning"}:
                                        if not emit_tool_status:
                                            continue
                                        if self.stream_callback:
                                            self.stream_callback(delta)
                                        else:
                                            yield delta
                                        continue

                                    actual_delta = _recovery.coerce_stream_delta(delta, full_response)
                                    full_response += actual_delta

                                    if actual_delta:
                                        if self.stream_callback:
                                            self.stream_callback(actual_delta)
                                        else:
                                            yield actual_delta
                                if not interrupted:
                                    pending = None

                            if last_chunk is not None:
                                _recovery.record_stream_chunk(self.model, last_chunk)

                        except Exception as stream_err:
                            error_msg = str(stream_err)
                            category = _recovery.classify_exception(stream_err)

                            if category == "recoverable":
                                self._recovery_state["attempt"] += 1
                                if self._recovery_state["attempt"] > _recovery.MAX_RETRIES:
                                    turn_state.transition = TurnTransition.MAX_RETRIES
                                    turn_state.error_message = error_msg
                                    break
                                messages = _recovery.recover_after_recoverable(
                                    self,
                                    user_input,
                                    messages,
                                    self._recovery_state["attempt"],
                                    include_context,
                                )
                                continue

                            if full_response:
                                continuation = _recovery.continue_after_stream_interrupt(self, messages, full_response)
                                if continuation:
                                    full_response += continuation
                                    if self.stream_callback:
                                        self.stream_callback(continuation)
                                    else:
                                        yield continuation
                                    break
                                turn_state.transition = TurnTransition.STREAM_INTERRUPTED
                                turn_state.error_message = error_msg
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
                    category = _recovery.classify_exception(e)
                    self._recovery_state["attempt"] += 1
                    attempt = self._recovery_state["attempt"]

                    if category == "fatal" or attempt > _recovery.MAX_RETRIES:
                        turn_state.transition = TurnTransition.MODEL_ERROR
                        if attempt > _recovery.MAX_RETRIES:
                            turn_state.transition = TurnTransition.MAX_RETRIES
                        turn_state.error_message = error_msg
                        if not full_response:
                            full_response = _recovery.format_execution_error(error_msg, self._recovery_state)
                            yield full_response
                        metadata = {"additional_kwargs": dict(self._last_extra)} if self._last_extra else {}
                        self._last_extra.clear()
                        self.last_turn_state = turn_state
                        _recovery.finish_turn(self.session, self.memory, original_input, full_response, metadata=metadata)
                        if self._graph_mode():
                            try:
                                self._require_runner().remember_turn(self._turn_count, original_input, full_response)
                            except Exception:
                                pass
                        return

                    if category == "recoverable":
                        messages = _recovery.recover_after_recoverable(
                            self, user_input, messages, attempt, include_context
                        )
                        continue

                    if category == "max_output_tokens":
                        messages = _recovery.recover_after_max_output_tokens(
                            self, user_input, messages, include_context
                        )
                        continue

                    if category == "prompt_too_long":
                        messages = _recovery.recover_after_prompt_too_long(
                            self, user_input, messages, include_context
                        )
                        continue

            if turn_state.transition == TurnTransition.NEXT_TURN:
                turn_state.transition = TurnTransition.COMPLETED
            if turn_state.transition == TurnTransition.MAX_RETRIES and not full_response:
                full_response = _recovery.format_execution_error(
                    turn_state.error_message or "已达到最大重试次数", self._recovery_state
                )
                yield full_response

            # STREAM_INTERRUPTED 为非终态：中断后不 finish_turn，标 needs_follow_up。
            if turn_state.transition == TurnTransition.STREAM_INTERRUPTED:
                turn_state.needs_follow_up = True
                self.last_turn_state = turn_state
                return

            # 记录完整交互到记忆和会话
            metadata = {"additional_kwargs": dict(self._last_extra)} if self._last_extra else {}
            self._last_extra.clear()
            self.last_turn_state = turn_state
            _recovery.finish_turn(self.session, self.memory, original_input, full_response, metadata=metadata)
            if self._graph_mode():
                self._require_runner().remember_turn(self._turn_count, original_input, full_response)

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

        # /reset 后图线程必须同步清空，否则下轮增量仍带旧历史。
        self._turn_count = 0
        self._stream_tokens_seen = False
        self._last_extra = {}
        try:
            runner = getattr(self, "runner", None)
            if runner is not None and getattr(runner, "graph_enabled", False):
                try:
                    current = runner._current_thread_messages() if hasattr(runner, "_current_thread_messages") else []
                    if current:
                        from langchain_core.messages import RemoveMessage

                        ids = [getattr(m, "id", None) for m in current if getattr(m, "id", None)]
                        if ids:
                            runner.agent.update_state(
                                runner._thread_config(),
                                {"messages": [RemoveMessage(id=i) for i in ids]},
                            )
                except Exception:
                    try:
                        runner.sync_messages([])
                    except Exception:
                        pass
        except Exception:
            pass

        # 重新分析项目
        self.context.scan()

    def run_with_plan(self, goal: str, max_rounds: int = 6) -> str:
        """跑自主计划图后执行（README/CHANGELOG 宣称的入口）。"""
        from .core.plan_graph import build_plan_graph, open_plan_checkpointer
        from .core.plans import PlanStore

        store = PlanStore(self.workspace, getattr(self.session, "session_id", "default"))
        try:
            from .tools.plan_tools import create_plan_tools

            plan_tools = create_plan_tools(lambda: store)
        except Exception:
            plan_tools = []
        try:
            from .prompts.fragments.plan_execute import build_plan_execute_overlay

            overlay = build_plan_execute_overlay()
        except Exception:
            overlay = ""
        saver, conn = None, None
        try:
            try:
                saver, conn = open_plan_checkpointer(self.workspace)
            except Exception:
                saver, conn = None, None
            graph = build_plan_graph(
                model=getattr(self.runner, "model_with_tools", self.model) if getattr(self, "runner", None) else self.model,
                plan_tools=plan_tools,
                run_turn=lambda prompt: self.run(prompt),
                store=store,
                overlay=overlay,
                checkpointer=saver,
                permissions=getattr(self, "_permissions_runtime", None),
            )
            result = graph.invoke(
                {"goal": str(goal or ""), "max_rounds": int(max_rounds or 6)},
                {"configurable": {"thread_id": getattr(self.session, "session_id", "default")}},
            )
            if isinstance(result, dict):
                return str(result.get("final") or result.get("last_response") or "")
            return str(result or "")
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass

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
        """重新加载受信任的 MCP 工具并重建 Agent。"""
        self._mcp_tools = self._load_mcp_tools()
        self.tools = self._compose_runtime_tools()
        self._create_agent()
        return self.get_mcp_tool_list()

    def close(self) -> None:
        """释放 runtime 持有的资源。"""
        if self._mcp_runtime is not None:
            self._mcp_runtime.shutdown()
            self._mcp_runtime = None

    def shutdown(self) -> None:
        """close() 的兼容别名。"""
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
    interrupt_handler: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    checkpoint_path: Optional[str] = None,
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
        interrupt_handler: 图中断恢复（工具询问的答案来源），None = 一律拒绝
        checkpoint_path: 图持久化位置，None = 无持久化（每次传全量）
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
        interrupt_handler=interrupt_handler,
        checkpoint_path=checkpoint_path,
    )


# ==============================================================================
# 导出
# ==============================================================================

__all__ = [
    'SAIAgent',
    'create_sai_agent',
]
