"""执行循环归属 lib.agent 包，由 loop 模块承载。

职责：turn 状态推进（TurnState / TurnTransition）、消息准备与发送、
流式事件抽取与去重、结果提取、会话重置、计划图执行。
原实现逐行搬自 lib/agent.py（SAIAgent 的执行侧方法）与
lib.agent_recovery 的流抽取包装（coerce_stream_delta /
stream_extractor_for / extract_stream_delta / extract_token_event），
只搬运，不重写语义。

调用链：
SAIAgent.run / stream_run（lib/agent.py 门面，薄包装）
→ run_turn / stream_turn（本模块：整轮重试循环 + 恢复语义）；
├─ agent._prepare_messages → prepare_messages（本模块）
│  → agent_assembly.start_turn（落 user 消息）
│  → sync_turn_state / build_messages（图增量 vs 全量）
│  → agent_assembly.history_messages + build_system_content；
├─ agent._invoke_with_messages → invoke_with_messages（本模块）
│  → runner.invoke → agent_recovery.drain_invoke_interrupts（排空中段）
│  → agent_usage.record_invoke_result（用量）→ extract_response；
├─ 异常 → agent_recovery.classify_exception
│  → recover_after_recoverable / max_output_tokens / prompt_too_long；
├─ 流中断 → agent_recovery.continue_after_stream_interrupt（非流续完）
│  或 resume_after_interrupt（工具询问 Command(resume=…) 继续）；
└─ 收尾 → agent_assembly.finish_turn（落 assistant 消息）。

恢复语义红线：重试上限、分类判定序、退避、续写文案、压缩失败呈现、
未知中断 fail-closed，均由 lib.agent_recovery 唯一定义，
本模块只负责在正确的位置调用它们。

实例派发约定：凡 SAIAgent 身上对外可见的缝合点
（_prepare_messages / _invoke_with_messages / _iter_agent_stream /
_build_messages / _sync_turn_state / _refresh_turn_prompt /
_reset_graph_state_for_retry / _extract_response / _graph_mode /
_require_runner / _tool_execution_context / run），本模块一律经由
agent. 实例派发调用，不直调模块函数——单测与调用方的
monkeypatch 才能继续生效。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterator, List, Optional, Union

from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

from . import recovery as _recovery
from . import assembly as _assembly
from . import usage as _usage
from .stream import AgentStreamExtractor
from ..core.agent_runtime import TurnTransition, TurnState, message_to_chat_dict


# ==============================================================================
# 流抽取（原 lib/agent_recovery 流节，逐行搬入；抽取逻辑在 AgentStreamExtractor）
# ==============================================================================

def coerce_stream_delta(delta: str, full_response: str) -> str:
    """归一化累计快照与真实增量，避免吞掉合法重复文本。"""
    if not delta:
        return ""
    if full_response and delta.startswith(full_response):
        return delta[len(full_response):]
    return delta


def stream_extractor_for(agent: Any) -> AgentStreamExtractor:
    """本轮流抽取器（agent 身上只保留去重标记，进出各同步一次）。

    _stream_tokens_seen 仍由 agent 持有（session_store 与旧测试直接读写它），
    抽取逻辑全部在 AgentStreamExtractor，避免两份实现各自演化。
    """
    extractor = getattr(agent, "_agent_stream_extractor", None)
    if extractor is None:
        extractor = AgentStreamExtractor()
        try:
            agent._agent_stream_extractor = extractor
        except Exception:
            pass
    try:
        extractor.tokens_seen = bool(getattr(agent, "_stream_tokens_seen", False))
    except Exception:
        pass
    return extractor


def _sync_stream_tokens_seen(agent: Any, extractor: AgentStreamExtractor) -> None:
    """把抽取器的去重状态写回本轮标记（供下一次 updates 去重）。"""
    try:
        agent._stream_tokens_seen = bool(getattr(extractor, "tokens_seen", False))
    except Exception:
        pass


def extract_stream_delta(agent: Any, chunk: Any) -> Any:
    """从 Agent 流式事件中提取结构化事件（多模式/字典/元组/单消息全覆盖）。

    messages 逐 token 通道置去重标记后，updates 里同一条 AI 消息的正文不再重发；
    工具调用标签仍从 updates 取。
    """
    extractor = stream_extractor_for(agent)
    try:
        return extractor.extract_stream_delta(chunk)
    finally:
        _sync_stream_tokens_seen(agent, extractor)


def extract_token_event(agent: Any, message: Any) -> Any:
    """messages 模式下的逐 token 增量 → 结构化事件（推理优先，其次正文）。"""
    extractor = stream_extractor_for(agent)
    try:
        return extractor.extract_token_event(message)
    finally:
        _sync_stream_tokens_seen(agent, extractor)


# ==============================================================================
# turn 消息准备（原 SAIAgent 执行侧方法，self 改 agent，缝合点走实例派发）
# ==============================================================================

def reminder_state(agent: Any) -> Dict[str, Any]:
    """构建系统提醒状态（纯数据，无 I/O）。原 SAIAgent._reminder_state，只搬运。"""
    from ..i18n import get_effective_language
    return {
        "agent_mode": agent.agent_mode,
        "context_usage": getattr(agent.session, "usage_ratio", 0.0),
        "language": get_effective_language(),
    }


def build_messages(
    agent: Any,
    effective_input: str,
    include_context: bool = True,
) -> List[Union[SystemMessage, HumanMessage, AIMessage]]:
    """构建发送给 Agent/模型的消息列表（无持久化时每次传全量）。

    原 SAIAgent._build_messages，只搬运。
    """
    # 在构建消息前触发上下文压缩检测
    agent.session.maybe_compact()

    return [
        SystemMessage(
            content=_assembly.build_system_content(
                agent.workspace,
                agent.context,
                agent.session,
                agent.system_prompt,
                None,
                include_context,
                reminder_state(agent),
            )
        ),
        *_assembly.history_messages(agent.session),
        HumanMessage(content=effective_input),
    ]


def refresh_turn_prompt(agent: Any, include_context: bool = True) -> str:
    """组装本轮 system 全文并刷进中间件（与今天"每轮拼一次"同成本）。

    原 SAIAgent._refresh_turn_prompt，只搬运。
    """
    system_text = _assembly.build_system_content(
        agent.workspace,
        agent.context,
        agent.session,
        agent.system_prompt,
        None,
        include_context,
        reminder_state(agent),
    )
    if agent.runner is not None:
        agent.runner.refresh_prompt(system_text)
    return system_text


def build_graph_import(
    agent: Any,
    effective_input: str,
    system_text: str,
) -> List[Union[SystemMessage, HumanMessage, AIMessage]]:
    """首轮/压缩同步用的全量消息：与 build_messages 同构，只是不触发压缩
    （调用方已做过），避免一次 turn 里压两次。

    原 SAIAgent._build_graph_import，只搬运。
    """
    return [
        SystemMessage(content=system_text),
        *_assembly.history_messages(agent.session),
        HumanMessage(content=effective_input),
    ]


def sync_turn_state(
    agent: Any,
    effective_input: str,
    include_context: bool = True,
) -> List[Union[SystemMessage, HumanMessage, AIMessage]]:
    """图路径的 turn 输入。

    * 空线程（首轮/新进程恢复）：全量导入，压缩产物标记一并进图；
    * 压缩刚发生：镜像被重写，必须用同一份转换覆盖图状态，否则图里还是
      压缩前的消息——压缩就退化成了"只改了镜像"；
    * 平时：只传本轮 HumanMessage，历史由 checkpointer 持有。

    原 SAIAgent._sync_turn_state，只搬运（缝合点走实例派发）。
    """
    compacted = agent.session.maybe_compact()
    system_text = agent._refresh_turn_prompt(include_context)
    runner = agent._require_runner()
    if runner.thread_message_count() == 0:
        return build_graph_import(agent, effective_input, system_text)
    if compacted:
        runner.sync_messages(
            build_graph_import(agent, effective_input, system_text)
        )
    return [HumanMessage(content=effective_input)]


def reset_graph_state_for_retry(
    agent: Any,
    user_input: str,
    include_context: bool = True,
) -> None:
    """重试前把图状态重置回镜像（与今天"从镜像重建后重试"同语义）。

    镜像永远是干净的 user/assistant 轮次；线程里的半截 AI/tool 消息被丢掉——
    今天重建全量消息重试同样丢掉它们（流 chunk 从不进 messages 列表）。

    原 SAIAgent._reset_graph_state_for_retry，只搬运（缝合点走实例派发）。
    """
    if not agent._graph_mode():
        return
    agent._require_runner().sync_messages(
        build_graph_import(
            agent, user_input, agent._refresh_turn_prompt(include_context)
        )
    )


def prepare_messages(
    agent: Any,
    user_input: str,
    include_context: bool = True,
) -> tuple[str, List[Union[SystemMessage, HumanMessage, AIMessage]]]:
    """记录本轮输入并构建统一消息列表。原 SAIAgent._prepare_messages，只搬运。"""
    from ..core.session_messages import SessionDerivedMemoryView

    # 会话切换后派生视图必须跟上新 session，否则记忆摘要停留在旧会话。
    if isinstance(agent.memory, SessionDerivedMemoryView) and agent.memory._session is not agent.session:
        agent.memory = SessionDerivedMemoryView(agent.session)
    original_input, effective_input = _assembly.start_turn(
        agent.session,
        agent.memory,
        user_input,
        enhancer=agent._enhance_user_input,
    )
    if agent._graph_mode():
        # 会话切换（/session）后 thread_id 必须跟上，否则串到别的会话里。
        agent._require_runner().thread_id = agent.session.session_id
        return original_input, agent._sync_turn_state(
            effective_input, include_context=include_context
        )
    return original_input, agent._build_messages(effective_input, include_context=include_context)


def invoke_with_messages(
    agent: Any,
    messages: List[Union[SystemMessage, HumanMessage, AIMessage]],
) -> str:
    """统一执行 Agent 或模型，并返回文本响应。原 SAIAgent._invoke_with_messages，只搬运。"""
    # ToolExecutionSession 守卫：如果不在执行上下文中，自动进入
    from ..tools.context import get_abort_controller
    if get_abort_controller() is not None and agent._abort_controller._aborted:
        return f"⚠️ 执行已中止（{agent._abort_controller.reason}）"

    if agent.runner and agent.runner.agent:
        result = agent.runner.invoke(messages)
        if result is None:
            response = agent.model.chat([message_to_chat_dict(message) for message in messages])
            return response
        if agent._graph_mode():
            # 非流 invoke 遇到中断是正常返回（result 带 __interrupt__），
            # 不是抛错：必须就地排空，否则本轮只拿到半截状态。
            result = _recovery.drain_invoke_interrupts(agent.runner, result, agent.interrupt_handler)
        _usage.record_invoke_result(agent.model, result)
        return agent._extract_response(result)

    response = agent.model.chat([message_to_chat_dict(message) for message in messages])
    return response


def iter_agent_stream(
    agent: Any,
    messages: List[Union[SystemMessage, HumanMessage, AIMessage]],
):
    """兼容不同图版本的流式接口，只做搬运。"""
    if not agent.runner:
        return None
    return agent.runner.stream(messages)


def extract_response(agent: Any, result: Dict) -> str:
    """从 Agent 结果中提取回复，同时保留 additional_kwargs 供多轮对话。

    原 SAIAgent._extract_response，只搬运。
    """
    if isinstance(result, dict) and 'messages' in result:
        messages = result['messages']
        for msg in reversed(messages):
            if isinstance(msg, AIMessage) or getattr(msg, "type", None) == "ai":
                # 保留 additional_kwargs（reasoning_content / thinking / tool_calls 等）
                extra = getattr(msg, "additional_kwargs", {}) or {}
                if extra:
                    agent._last_extra = dict(extra)
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


# ==============================================================================
# run / stream_run 主干（原 SAIAgent.run / stream_run，整轮重试循环逐行搬入）
# ==============================================================================

def run_turn(
    agent: Any,
    user_input: str,
    include_context: bool = True
) -> str:
    """执行一轮 Agent（非流式）— 含恢复路径。原 SAIAgent.run，只搬运。

    恢复路径（参考 Claude Code query.ts）：
    1. recoverable → 指数退避重试（最多 3 次；图模式下图内中间件先退避，外层是整轮兜底）
    2. max_output_tokens → 注入延续消息后重试
    3. prompt_too_long → 触发上下文压缩后重试
    """
    from ..tools.context import tool_execution_session

    agent._turn_count += 1
    turn_state = TurnState(
        transition=TurnTransition.NEXT_TURN,
        turn_count=agent._turn_count,
    )
    agent.last_turn_state = turn_state
    agent._abort_controller.reset()
    agent._recovery_state = {"attempt": 0, "path": ""}

    # 可观测走 LangSmith callbacks（ContextVar 调用树已删除）。
    with tool_execution_session(agent._tool_execution_context()):
        original_input, messages = agent._prepare_messages(
            user_input,
            include_context=include_context,
        )

        response = ""
        while agent._recovery_state["attempt"] <= _recovery.MAX_RETRIES:
            try:
                response = agent._invoke_with_messages(messages)
                turn_state.transition = TurnTransition.COMPLETED
                break
            except Exception as e:
                error_msg = str(e)
                category = _recovery.classify_exception(e)
                agent._recovery_state["attempt"] += 1
                attempt = agent._recovery_state["attempt"]

                if category == "fatal" or attempt > _recovery.MAX_RETRIES:
                    response = _recovery.format_execution_error(error_msg, agent._recovery_state)
                    turn_state.transition = TurnTransition.MODEL_ERROR
                    if attempt > _recovery.MAX_RETRIES:
                        turn_state.transition = TurnTransition.MAX_RETRIES
                    turn_state.error_message = error_msg
                    break

                if category == "recoverable":
                    messages = _recovery.recover_after_recoverable(
                        agent, user_input, messages, attempt, include_context
                    )
                    continue

                if category == "max_output_tokens":
                    messages = _recovery.recover_after_max_output_tokens(
                        agent, user_input, messages, include_context
                    )
                    continue

                if category == "prompt_too_long":
                    messages = _recovery.recover_after_prompt_too_long(
                        agent, user_input, messages, include_context
                    )
                    continue

        if not response:
            turn_state.error_message = "所有恢复路径均已耗尽"
            response = _recovery.format_execution_error(turn_state.error_message, agent._recovery_state)
            turn_state.transition = TurnTransition.MAX_RETRIES

    # 记录交互，保留 additional_kwargs 供下一轮透传
    metadata = {"additional_kwargs": dict(agent._last_extra)} if agent._last_extra else {}
    agent._last_extra.clear()
    agent.last_turn_state = turn_state
    _assembly.finish_turn(agent.session, agent.memory, original_input, response, metadata=metadata)
    if agent._graph_mode():
        agent._require_runner().remember_turn(agent._turn_count, original_input, response)

    return response


def stream_turn(
    agent: Any,
    user_input: str,
    include_context: bool = True,
    *,
    event_callback: Optional[Callable[[Any], None]] = None,
    emit_tool_status: bool = True,
) -> Iterator[Any]:
    """执行一轮 Agent（流式输出）— 含恢复路径。原 SAIAgent.stream_run，只搬运。

    产出正文字符串与状态事件混排，推理与工具事件是结构化对象。

    恢复路径：
    1. 流中断 → 用非流式续完
    2. recoverable → 指数退避重试（图模式下图内中间件先退避，外层是整轮兜底）
    3. max_output_tokens → 注入延续消息后重试
    4. prompt_too_long → 触发压缩后重试
    """
    from ..tools.context import tool_execution_session

    agent._turn_count += 1
    turn_state = TurnState(
        transition=TurnTransition.NEXT_TURN,
        turn_count=agent._turn_count,
    )
    agent.last_turn_state = turn_state
    agent._abort_controller.reset()
    agent._recovery_state = {"attempt": 0, "path": ""}
    agent._stream_tokens_seen = False

    with tool_execution_session(agent._tool_execution_context()):
        original_input, messages = agent._prepare_messages(
            user_input,
            include_context=include_context,
        )

        full_response = ""

        while agent._recovery_state["attempt"] <= _recovery.MAX_RETRIES:
            try:
                stream_iter = agent._iter_agent_stream(messages)

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
                                        agent.runner, interrupts, agent.interrupt_handler
                                    )
                                    interrupted = True
                                    break
                                last_chunk = chunk
                                event = extract_stream_delta(agent, chunk)
                                if event is None or not event.display_text:
                                    continue
                                delta = event.display_text
                                # reasoning 与工具事件走状态通道（受 emit_tool_status 门控）：
                                # 直接产出结构化事件，渲染层负责攒段落，不再把原文
                                # 片逐个拼成带标记的字符串往外吐。
                                if event.kind in {"tool_start", "tool_result", "tool_error", "reasoning"}:
                                    if not emit_tool_status:
                                        continue
                                    if agent.stream_callback:
                                        agent.stream_callback(event)
                                    else:
                                        yield event
                                    continue

                                actual_delta = coerce_stream_delta(delta, full_response)
                                full_response += actual_delta

                                if actual_delta:
                                    if agent.stream_callback:
                                        agent.stream_callback(actual_delta)
                                    else:
                                        yield actual_delta
                            if not interrupted:
                                pending = None

                        if last_chunk is not None:
                            _usage.record_stream_chunk(agent.model, last_chunk)

                    except Exception as stream_err:
                        error_msg = str(stream_err)
                        category = _recovery.classify_exception(stream_err)

                        if category == "recoverable":
                            agent._recovery_state["attempt"] += 1
                            if agent._recovery_state["attempt"] > _recovery.MAX_RETRIES:
                                turn_state.transition = TurnTransition.MAX_RETRIES
                                turn_state.error_message = error_msg
                                break
                            messages = _recovery.recover_after_recoverable(
                                agent,
                                user_input,
                                messages,
                                agent._recovery_state["attempt"],
                                include_context,
                            )
                            continue

                        if full_response:
                            continuation = _recovery.continue_after_stream_interrupt(agent, messages, full_response)
                            if continuation:
                                full_response += continuation
                                if agent.stream_callback:
                                    agent.stream_callback(continuation)
                                else:
                                    yield continuation
                                break
                            turn_state.transition = TurnTransition.STREAM_INTERRUPTED
                            turn_state.error_message = error_msg
                            break
                        else:
                            fallback = agent._invoke_with_messages(messages)
                            full_response = fallback
                            if agent.stream_callback:
                                agent.stream_callback(fallback)
                            else:
                                yield fallback
                            break

                    if not full_response:
                        fallback = agent._invoke_with_messages(messages)
                        full_response = fallback
                        if agent.stream_callback:
                            agent.stream_callback(fallback)
                        else:
                            yield fallback

                elif hasattr(agent.model, 'chat_stream'):
                    chat_messages = [message_to_chat_dict(message) for message in messages]
                    for chunk in agent.model.chat_stream(chat_messages):
                        full_response += chunk
                        if agent.stream_callback:
                            agent.stream_callback(chunk)
                        else:
                            yield chunk

                else:
                    fallback = agent._invoke_with_messages(messages)
                    full_response = fallback
                    if agent.stream_callback:
                        agent.stream_callback(fallback)
                    else:
                        yield fallback

                break  # 成功完成，退出重试循环

            except Exception as e:
                error_msg = str(e)
                category = _recovery.classify_exception(e)
                agent._recovery_state["attempt"] += 1
                attempt = agent._recovery_state["attempt"]

                if category == "fatal" or attempt > _recovery.MAX_RETRIES:
                    turn_state.transition = TurnTransition.MODEL_ERROR
                    if attempt > _recovery.MAX_RETRIES:
                        turn_state.transition = TurnTransition.MAX_RETRIES
                    turn_state.error_message = error_msg
                    if not full_response:
                        full_response = _recovery.format_execution_error(error_msg, agent._recovery_state)
                        yield full_response
                    metadata = {"additional_kwargs": dict(agent._last_extra)} if agent._last_extra else {}
                    agent._last_extra.clear()
                    agent.last_turn_state = turn_state
                    _assembly.finish_turn(agent.session, agent.memory, original_input, full_response, metadata=metadata)
                    if agent._graph_mode():
                        try:
                            agent._require_runner().remember_turn(agent._turn_count, original_input, full_response)
                        except Exception:
                            pass
                    return

                if category == "recoverable":
                    messages = _recovery.recover_after_recoverable(
                        agent, user_input, messages, attempt, include_context
                    )
                    continue

                if category == "max_output_tokens":
                    messages = _recovery.recover_after_max_output_tokens(
                        agent, user_input, messages, include_context
                    )
                    continue

                if category == "prompt_too_long":
                    messages = _recovery.recover_after_prompt_too_long(
                        agent, user_input, messages, include_context
                    )
                    continue

        if turn_state.transition == TurnTransition.NEXT_TURN:
            turn_state.transition = TurnTransition.COMPLETED
        if turn_state.transition == TurnTransition.MAX_RETRIES and not full_response:
            full_response = _recovery.format_execution_error(
                turn_state.error_message or "已达到最大重试次数", agent._recovery_state
            )
            yield full_response

        # STREAM_INTERRUPTED 为非终态：中断后不 finish_turn，标 needs_follow_up。
        if turn_state.transition == TurnTransition.STREAM_INTERRUPTED:
            turn_state.needs_follow_up = True
            agent.last_turn_state = turn_state
            return

        # 记录完整交互到记忆和会话
        metadata = {"additional_kwargs": dict(agent._last_extra)} if agent._last_extra else {}
        agent._last_extra.clear()
        agent.last_turn_state = turn_state
        _assembly.finish_turn(agent.session, agent.memory, original_input, full_response, metadata=metadata)
        if agent._graph_mode():
            agent._require_runner().remember_turn(agent._turn_count, original_input, full_response)


# ==============================================================================
# 会话重置与计划执行（原 SAIAgent.reset / run_with_plan，逐行搬入）
# ==============================================================================

def reset_turn_state(agent: Any, clear_memory: bool = True, clear_session: bool = True):
    """重置 turn 状态与会话。原 SAIAgent.reset，只搬运。

    /reset 后图线程必须同步清空，否则下轮增量仍带旧历史。
    """
    if clear_memory:
        agent.memory.clear()

    if clear_session:
        agent.session.clear()

    # /reset 后图线程必须同步清空，否则下轮增量仍带旧历史。
    agent._turn_count = 0
    agent._stream_tokens_seen = False
    agent._last_extra = {}
    try:
        runner = getattr(agent, "runner", None)
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
    agent.context.scan()


def run_with_plan_graph(agent: Any, goal: str, max_rounds: int = 6) -> str:
    """跑自主计划图后执行（README/CHANGELOG 宣称的入口）。

    原 SAIAgent.run_with_plan，只搬运。
    """
    from ..core.plan_graph import build_plan_graph, open_plan_checkpointer
    from ..core.plans import PlanStore

    store = PlanStore(agent.workspace, getattr(agent.session, "session_id", "default"))
    try:
        from ..tools.plan_tools import create_plan_tools

        plan_tools = create_plan_tools(lambda: store)
    except Exception:
        plan_tools = []
    try:
        from ..prompts.fragments.plan_execute import build_plan_execute_overlay

        overlay = build_plan_execute_overlay()
    except Exception:
        overlay = ""
    saver, conn = None, None
    try:
        try:
            saver, conn = open_plan_checkpointer(agent.workspace)
        except Exception:
            saver, conn = None, None
        graph = build_plan_graph(
            model=getattr(agent.runner, "model_with_tools", agent.model) if getattr(agent, "runner", None) else agent.model,
            plan_tools=plan_tools,
            run_turn=lambda prompt: agent.run(prompt),
            store=store,
            overlay=overlay,
            checkpointer=saver,
            permissions=getattr(agent, "_permissions_runtime", None),
        )
        result = graph.invoke(
            {"goal": str(goal or ""), "max_rounds": int(max_rounds or 6)},
            {"configurable": {"thread_id": getattr(agent.session, "session_id", "default")}},
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


__all__ = [
    "build_graph_import",
    "build_messages",
    "coerce_stream_delta",
    "extract_response",
    "extract_stream_delta",
    "extract_token_event",
    "invoke_with_messages",
    "iter_agent_stream",
    "prepare_messages",
    "refresh_turn_prompt",
    "reminder_state",
    "reset_graph_state_for_retry",
    "reset_turn_state",
    "run_turn",
    "run_with_plan_graph",
    "stream_extractor_for",
    "stream_turn",
    "sync_turn_state",
]
