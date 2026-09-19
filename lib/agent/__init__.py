"""Agent 包入口：SAIAgent 门面与装配归属 lib.agent。

职责边界（拆分后）：
- 本模块：TOOL_PRIORITY、__init__ 装配、工具与 MCP 装配、提示词风格/模式切换、
  图可用性判断、公开门面方法（run/stream_run 为薄包装，统计/记忆/MCP 查询）。
- lib.agent_loop：执行循环（run/stream_run 主干 + turn 状态机 + 会话重置/计划）。
- lib.agent_recovery：恢复策略（分类/退避/续写/压缩重试/中断恢复）。
- lib.agent_assembly：对话装配（prompts/middleware 侧：turn 起止/历史/system 组装）。
- lib.agent_usage：用量记录（models 侧：结果/流 chunk 用量提取）。

调用链：SAIAgent.run/stream_run → agent_loop.run_turn/stream_turn
→ agent_recovery（恢复动作）+ agent_assembly（组装）+ agent_usage（用量）。
凡执行侧缝合点（_prepare_messages 等）本模块保留同名薄包装并走实例派发，
单测与调用方的 monkeypatch 继续生效。
"""

import logging
from typing import List, Optional, Dict, Any, Iterator, Union, Callable
from pathlib import Path
from langchain_core.tools import BaseTool

# 导入项目模块
from . import loop as _loop
from . import assembly as _assembly
from ..core.agent_runtime import AgentRunner
from ..core.safety import SafetyChecker
from ..core.context import ProjectContext
from ..core.session_messages import (
    SessionManager,
    SessionDerivedMemoryView,
)
from ..core.modes import normalize_agent_mode
from ..models import BaseModel
from ..models.registry import get_model_provider_registry
from ..runtime.context import RuntimeContext
from ..tools.context import ToolAbortController, ToolExecutionContext, tool_execution_session
from ..core.hooks import create_hook_runtime
from ..core.permission_workspace import create_permission_runtime
from ..prompts import normalize_prompt_style
from ..i18n import tr

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
        """初始化 Agent（装配：模型/工具/会话/提示词/graph runner）。

        历史唯一真相源为 session（+ checkpointer 持久化），
        记忆为会话派生只读视图。MCP stdio 工具只在 workspace
        安全/信任配置完成后才加载；中断恢复默认 fail-closed。
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
        self.last_turn_state = _loop.TurnState(turn_count=0)

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
            from ..tools import ToolRegistry

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
        """根据当前 prompt style 构建系统提示词（组装语义见 agent_assembly）。"""
        return _assembly.build_system_prompt_text(
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

    def _graph_mode(self) -> bool:
        """是否走图路径（中间件 + checkpointer 就绪）。"""
        runner = getattr(self, "runner", None)
        return bool(runner is not None and runner.graph_enabled)

    def _require_runner(self) -> AgentRunner:
        """拿 runner（图路径专用）：没了就 loud fail，不静默降级——调用方都已判过 _graph_mode()。"""
        if self.runner is None:
            raise RuntimeError("runner 不可用")
        return self.runner

    def _load_mcp_tools(self) -> List[BaseTool]:
        """从当前 workspace 加载受信任的 MCP 工具。"""
        if self._mcp_runtime is not None:
            self._mcp_runtime.shutdown()
            self._mcp_runtime = None
        if not self._enable_mcp:
            return []
        try:
            from ..core.mcp_runtime import MCPRuntime

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
            from ..core.permission_workspace import create_permission_runtime

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

    # ==========================================================================
    # 执行侧薄包装：实现见 lib.agent_loop（实例派发，monkeypatch 继续生效）
    # ==========================================================================

    def _reminder_state(self) -> Dict[str, Any]:
        """系统提醒状态（纯数据）：实现见 agent_loop.reminder_state。"""
        return _loop.reminder_state(self)

    def _build_messages(self, effective_input: str, include_context: bool = True):
        """构建消息列表：实现见 agent_loop.build_messages。"""
        return _loop.build_messages(self, effective_input, include_context=include_context)

    def _refresh_turn_prompt(self, include_context: bool = True) -> str:
        """组装本轮 system 全文并刷进中间件：实现见 agent_loop.refresh_turn_prompt。"""
        return _loop.refresh_turn_prompt(self, include_context)

    def _build_graph_import(self, effective_input: str, system_text: str):
        """首轮/压缩同步全量消息：实现见 agent_loop.build_graph_import。"""
        return _loop.build_graph_import(self, effective_input, system_text)

    def _sync_turn_state(self, effective_input: str, include_context: bool = True):
        """图路径 turn 输入：实现见 agent_loop.sync_turn_state。"""
        return _loop.sync_turn_state(self, effective_input, include_context=include_context)

    def _reset_graph_state_for_retry(self, user_input: str, include_context: bool = True) -> None:
        """重试前重置图状态回镜像：实现见 agent_loop.reset_graph_state_for_retry。"""
        return _loop.reset_graph_state_for_retry(self, user_input, include_context)

    def _prepare_messages(self, user_input: str, include_context: bool = True):
        """记录本轮输入并构建消息列表：实现见 agent_loop.prepare_messages。"""
        return _loop.prepare_messages(self, user_input, include_context=include_context)

    def _invoke_with_messages(self, messages) -> str:
        """统一执行 Agent 或模型：实现见 agent_loop.invoke_with_messages。"""
        return _loop.invoke_with_messages(self, messages)

    def _iter_agent_stream(self, messages):
        """兼容不同 LangGraph 版本的流式接口：实现见 agent_loop.iter_agent_stream。"""
        return _loop.iter_agent_stream(self, messages)

    def _extract_response(self, result: Dict) -> str:
        """从结果提取回复并保留 additional_kwargs：实现见 agent_loop.extract_response。"""
        return _loop.extract_response(self, result)

    def run(
        self,
        user_input: str,
        include_context: bool = True
    ) -> str:
        """执行 Agent（非流式）— 含恢复路径，实现见 agent_loop.run_turn。"""
        return _loop.run_turn(self, user_input, include_context=include_context)

    def stream_run(
        self,
        user_input: str,
        include_context: bool = True,
        *,
        event_callback: Optional[Callable[[Any], None]] = None,
        emit_tool_status: bool = True,
    ) -> Iterator[str]:
        """执行 Agent（流式输出）— 含恢复路径，实现见 agent_loop.stream_turn。"""
        yield from _loop.stream_turn(
            self, user_input, include_context=include_context,
            event_callback=event_callback, emit_tool_status=emit_tool_status,
        )

    def reset(self, clear_memory: bool = True, clear_session: bool = True):
        """重置 Agent（含 turn 状态与图线程）：实现见 agent_loop.reset_turn_state。"""
        return _loop.reset_turn_state(self, clear_memory=clear_memory, clear_session=clear_session)

    def run_with_plan(self, goal: str, max_rounds: int = 6) -> str:
        """跑自主计划图后执行：实现见 agent_loop.run_with_plan_graph。"""
        return _loop.run_with_plan_graph(self, goal, max_rounds)

    # ==========================================================================
    # 门面查询与资源管理
    # ==========================================================================

    def get_context_summary(self) -> str:
        """获取项目上下文摘要。"""
        return self.context.get_context_for_llm(max_files=20)

    def get_memory_summary(self) -> str:
        """获取记忆摘要。"""
        return self.memory.summarize()

    def get_recent_history(self, n: int = 5) -> str:
        """获取最近 n 轮对话历史。"""
        return self.memory.get_recent_context(n)

    def analyze_project(self) -> str:
        """分析当前项目"""
        self.context.scan()
        return self.context.get_context_for_llm()

    def get_tool_list(self) -> List[str]:
        """获取可用工具列表"""
        return [tool.name for tool in self.tools]

    def get_mcp_tool_list(self) -> List[Dict[str, Any]]:
        """获取 MCP 工具列表。"""
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

    # =============================================================================
    # MCP 相关方法
    # =============================================================================

    def get_mcp_registry(self) -> Optional[Any]:
        """获取 MCP 注册表（兼容旧接口，始终返回 None 或状态）。"""
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
        """执行 MCP 工具。"""
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
    """创建 SAYA Agent 的便捷函数。

    interrupt_handler 为图中断恢复的答案来源（None = 一律拒绝）；
    checkpoint_path 为图持久化位置（None = 无持久化，每次传全量）。
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
