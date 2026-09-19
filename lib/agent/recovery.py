"""恢复策略归属 lib.agent 包，由 recovery 模块承载。

只留恢复策略。拆分后归属：
- 对话装配（start/finish_turn、history_messages、system 组装）
  → lib.agent_assembly（prompts/middleware 侧）；
- 用量记录（record/estimate、safe_token_count）
  → lib.agent_usage（models 侧，直调 vocabulary）；
- 流抽取包装（coerce_stream_delta、extract_stream_delta 等）
  → lib.agent_loop（执行循环侧，抽取逻辑在 AgentStreamExtractor）；
- run / stream_run 主干与 turn 状态机 → lib.agent_loop。

调用链：agent_loop.run_turn / stream_turn
→ classify_exception（本模块：结构化属性优先，文案表兜底）
→ recover_after_recoverable（退避）/ recover_after_max_output_tokens（续写）
→ recover_after_prompt_too_long（压缩重试）
→ resume_after_interrupt / drain_invoke_interrupts（中断恢复，fail-closed）。

外层整轮重试循环保留的结论（任务清单第 5 项评估）：
图内 ModelRetryMiddleware / ToolRetryMiddleware 只覆盖图内单步调用的
瞬时失败，保住图进度；以下三点它们看不到，必须由外层整轮兜底——

* recoverable 外层退避：覆盖 invoke 层以上的异常（图都进不去时中间件
  够不着），保留；
* max_output_tokens 续写注入：必须由外层发一条新的延续 HumanMessage
  再整轮重调，中间件注入不了新消息，保留最小形态；
* prompt_too_long 压缩重试：本库未挂载 SummarizationMiddleware，
  ContextEditingMiddleware（ClearToolUsesEdit）只剪工具结果、不压
  prompt，session.maybe_compact / force_compact 仍只能由外层触发，
  压缩分支保留（压缩失败记入恢复状态，由最终错误文案呈现）。
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from langchain_core.messages import AIMessage, HumanMessage

from .stream import AgentStreamExtractor


# ==============================================================================
# 恢复路径常量与错误分类（原 lib/agent.py 模块级定义，原样保留）
# ==============================================================================

MAX_RETRIES = 3                  # 最大重试次数（可恢复错误）
RETRY_BACKOFF_BASE = 1.5         # 指数退避基数（秒）
RECOVERABLE_ERROR_PATTERNS = (
    "rate_limit",
    "rate limit",
    "too many requests",
    "server error",
    "internal server error",
    "service unavailable",
    "timeout",
    "timed out",
    "connection",
    "overloaded",
)
# 输出 token 上限：要缩短的是「回复」。与上下文超限是两回事，不要混表。
MAX_OUTPUT_TOKENS_PATTERNS = (
    "max_output_tokens",
    "max tokens",
    "output token limit",
)
# 输出上限续写提示（中间件覆盖不到的恢复点：整轮续写必须由外层注入新消息）。
# run / stream_run 共用同一份文案；测试只断言 "Resume directly" 子串。
MAX_OUTPUT_CONTINUATION_TEXT = (
    "Output token limit hit. Resume directly — no apology, "
    "no recap of what you were doing. Pick up mid-thought "
    "if that is where the cut happened."
)
# 上下文超限：要压缩的是「输入」。
# 判定顺序必须先于 MAX_OUTPUT_TOKENS_PATTERNS：部分 provider 的超限文案同时含
# "max tokens" 之类字样，若先查输出上限表会误判成「输出超限」，从而走错恢复分支
# （给已超限的 prompt 再加消息）。
# 下划线形式 context_length_exceeded 需单列，空格形式覆盖不到它。
PROMPT_TOO_LONG_PATTERNS = (
    "maximum context length",
    "context_length",
    "context length",
    "prompt is too long",
    "prompt too long",
    "context window",
    "reduce the length",
    "too many tokens",
    "input length",
)

# 参数/请求校验类错误：重试不会成功，且会重放已执行的有副作用工具调用。
# 这些字样往往与 recoverable 关键字共存（如 connection_timeout 含 "timeout"），
# 因此需要先行拦截。
NON_RETRYABLE_ERROR_PATTERNS = (
    "invalid parameter",
    "invalid value",
    "invalid_request",
    "invalid request",
    "validation error",
    "must be",
)

# 分类规则序即判定序（判定顺序是语义的一部分）：
# prompt_too_long 先于 max_output_tokens（部分超限文案同时含 "max tokens" 字样，
# 先查输出上限表会误判成输出超限，从而给已超限的 prompt 再加消息）；
# fatal（参数校验）先于 recoverable（如 connection_timeout 含 "timeout"）。
# 四元组 (prompt_too_long / max_output_tokens / fatal / recoverable) 穷尽所有分支，
# 落空即 fatal。
_ERROR_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("prompt_too_long", PROMPT_TOO_LONG_PATTERNS),
    ("max_output_tokens", MAX_OUTPUT_TOKENS_PATTERNS),
    ("fatal", NON_RETRYABLE_ERROR_PATTERNS),
    ("recoverable", RECOVERABLE_ERROR_PATTERNS),
)

# 结构化异常上的显式分类属性（providers / LangChain 透出的 code / status / category）。
# 有就直接采信，不再做字符串匹配；没有才回落到文案表（兼容旧 provider）。
_ERROR_CATEGORY_ATTRS = ("error_category", "category", "retryable", "code", "error_code", "status_code")
RETRYABLE_STATUS_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


def classify_error_text(lowered: str) -> str:
    """已小写的文案走统一规则表（单循环，替代原来的四个重复循环）。"""
    for category, patterns in _ERROR_RULES:
        for pat in patterns:
            if pat in lowered:
                return category
    return "fatal"


def classify_error(error_msg: str) -> str:
    """将错误消息归类为 recoverable / max_output_tokens / prompt_too_long / fatal。"""
    return classify_error_text(str(error_msg or "").lower())


def classify_exception(exc: BaseException) -> str:
    """异常对象分类：结构化属性优先，文案表兜底。

    先看异常自带的显式信号（category / code / status_code 等），命中则直接返回，
    四语义与 classify_error 完全一致；拿不到才把 str(exc) 及常见属性拼起来
    走同一张规则表。纯字符串调用方继续用 classify_error，行为不变。
    """
    for attr in ("error_category", "category"):
        category = getattr(exc, attr, None)
        if isinstance(category, str) and category in (
            "recoverable", "max_output_tokens", "prompt_too_long", "fatal",
        ):
            return category
    retryable = getattr(exc, "retryable", None)
    if retryable is True:
        return "recoverable"
    if retryable is False:
        return "fatal"
    for attr in ("code", "error_code", "status_code", "status"):
        value = getattr(exc, attr, None)
        if value is None:
            continue
        if isinstance(value, int) and value in RETRYABLE_STATUS_CODES:
            return "recoverable"
        text = str(value).lower()
        if text:
            hit = classify_error_text(text)
            if hit != "fatal":
                return hit
    parts = [str(exc)]
    for attr in _ERROR_CATEGORY_ATTRS:
        try:
            value = getattr(exc, attr, None)
        except Exception:
            continue
        if value is not None and not isinstance(value, bool):
            parts.append(str(value))
    response = getattr(exc, "response", None)
    if response is not None:
        parts.append(str(response))
    return classify_error(" ".join(parts))


def retry_delay(attempt: int) -> float:
    """计算指数退避延迟（秒）。"""
    return RETRY_BACKOFF_BASE ** attempt


def format_execution_error(error_msg: str, recovery_state: Dict[str, Any]) -> str:
    """构造用户可见的最终错误文案。

    若本轮恢复中压缩失败过（`recovery_state["compact_error"]`），把原因一并附上：
    压缩失败会让 prompt_too_long 恢复路径失效（重试带的仍是原样超限的消息），
    只报模型错误会让用户看到一个没有信息量的失败。
    """
    compact_error = str(recovery_state.get("compact_error") or "")
    if compact_error:
        return f"执行出错: {error_msg}（上下文压缩失败: {compact_error}）"
    return f"执行出错: {error_msg}"


# ==============================================================================
# 图中断恢复（ask 权限走框架 interrupt，答案由调用方给；未知种类 fail-closed）
# ==============================================================================

def detect_interrupt(chunk: Any) -> Optional[list]:
    """从流事件里摘 __interrupt__（只在图+中断时出现，无持久化时永远 None）。"""
    _, payload = AgentStreamExtractor.split_mode_event(chunk)
    # 全模式检查：中断可能出现在 updates/messages/values 任一通道，不限 updates。
    target = payload
    if isinstance(target, dict) and "__interrupt__" in target:
        interrupts = target["__interrupt__"]
        return list(interrupts) if isinstance(interrupts, (list, tuple)) else [interrupts]
    return None


def resolve_interrupt(interrupts: list, interrupt_handler: Any) -> Any:
    """把中断载荷翻译成恢复答案。未知种类按拒绝恢复（fail-closed）。"""
    from ..core.middleware import INTERRUPT_TOOL_ASK

    import logging
    logger = logging.getLogger(__name__)

    answers: list = []
    for item in interrupts or []:
        value = getattr(item, "value", item)
        if isinstance(value, dict) and value.get("kind") == INTERRUPT_TOOL_ASK:
            if interrupt_handler is not None:
                answers.append(interrupt_handler(dict(value)))
            else:
                logger.warning(
                    "工具询问无中断处理器，已按拒绝处理: %s", value.get("tool")
                )
                answers.append({"approved": False})
        else:
            answers.append(None)
    if len(answers) == 1:
        return answers[0]
    return answers


def _unknown_interrupt_error(interrupts: Any) -> RuntimeError:
    """未知中断无法恢复：fail-closed，直接抛错交给外层恢复分类。"""
    kinds = []
    items = interrupts if isinstance(interrupts, (list, tuple)) else [interrupts]
    for item in items or []:
        value = getattr(item, "value", item)
        if isinstance(value, dict):
            kinds.append(str(value.get("kind") or value)[:200])
        else:
            kinds.append(str(value)[:200])
    return RuntimeError(f"未知中断无法恢复: kinds={kinds} payload={str(interrupts)[:1000]}")


def resume_after_interrupt(runner: Any, interrupts: list, interrupt_handler: Any) -> Optional[Any]:
    """interrupt_handler 拿答案 → Command(resume=…) 继续流。

    抛错就交给外层恢复分类（与流异常同一条路）。
    """
    if runner is None:
        raise RuntimeError("无法恢复中断：runner 不可用")
    answer = resolve_interrupt(interrupts, interrupt_handler)
    if answer is None or (isinstance(answer, list) and all(a is None for a in answer)):
        raise _unknown_interrupt_error(interrupts)
    return runner.resume(answer)


def drain_invoke_interrupts(runner: Any, result: Dict[str, Any], interrupt_handler: Any) -> Dict[str, Any]:
    """排空非流 invoke 里的中断：恢复→继续，直到跑完或无可恢复的中断。

    上限 8 轮：handler 若一直返回"再问一次"之类的答案，不能在这里死循环，
    外层恢复循环会接管（防御性，正常一次就排空）。
    """
    guard = 0
    while isinstance(result, dict) and result.get("__interrupt__") and guard < 8:
        raw = result["__interrupt__"]
        items = list(raw) if isinstance(raw, (list, tuple)) else [raw]
        answer = resolve_interrupt(items, interrupt_handler)
        if answer is None or (isinstance(answer, list) and all(a is None for a in answer)):
            raise _unknown_interrupt_error(raw)
        resumed = runner.invoke_command(answer) if runner else None
        if resumed is None:
            break
        result = resumed
        guard += 1
    return result


# ==============================================================================
# 整轮恢复动作（run / stream_run 外层共用；图内中间件覆盖不到的三点）
# ==============================================================================

def force_compact_session(session: Any, recovery_state: Dict[str, Any]) -> None:
    """上下文超限恢复：强制压缩会话。

    优先使用 force_compact（跳过阈值检查、更激进保留轮次）；compact() 在轮数不足
    时直接返回且谎报「已压缩」，「轮数少但单轮巨大」这一最常见超限形态下无效。
    自定义 session 实现可能没有 force_compact，此时降级到 compact，并把实际使用
    的路径记入 recovery_state 便于诊断（不静默）。
    """
    force_compact = getattr(session, "force_compact", None)
    if callable(force_compact):
        force_compact(reason="prompt_too_long")
        recovery_state["compact_api"] = "force_compact"
        return
    session.compact()
    recovery_state["compact_api"] = "compact_fallback"


def recover_after_recoverable(agent: Any, user_input: str, messages: list,
                              attempt: int, include_context: bool = True) -> list:
    """瞬时故障退避：图状态重置回镜像（丢半截消息，与重建全量同语义）后睡眠。"""
    agent._recovery_state["path"] = "retry_backoff"
    if agent._graph_mode():
        agent._reset_graph_state_for_retry(user_input, include_context)
        messages = []
    time.sleep(retry_delay(attempt))
    return messages


def recover_after_max_output_tokens(agent: Any, user_input: str, messages: list,
                                    include_context: bool = True) -> list:
    """输出上限续写：图模式只发延续消息（半截输出进不了重试），旧路径追加。"""
    agent._recovery_state["path"] = "max_output_tokens_recovery"
    continuation = HumanMessage(content=MAX_OUTPUT_CONTINUATION_TEXT)
    if agent._graph_mode():
        # 延续是新消息：干净基础上只追加它（半截输出今天同样进不了重试）。
        agent._reset_graph_state_for_retry(user_input, include_context)
        return [continuation]
    return [*messages, continuation]


def recover_after_prompt_too_long(agent: Any, user_input: str, messages: list,
                                  include_context: bool = True) -> list:
    """上下文超限压缩重试：压缩失败记入恢复状态，由最终错误文案呈现。"""
    import logging
    logger = logging.getLogger(__name__)

    agent._recovery_state["path"] = "compact_retry"
    try:
        # 必须用 force_compact：compact() 在轮数不足时直接返回且
        # 谎报「已压缩」，「轮数少但单轮巨大」这一最常见超限形态下无效。
        force_compact_session(agent.session, agent._recovery_state)
        if agent._graph_mode():
            # 镜像已被重写：同步进图后显式重发当前轮，避免空跑。
            agent._reset_graph_state_for_retry(user_input, include_context)
            return [HumanMessage(content=user_input)]
        return agent._build_messages(effective_input=user_input, include_context=include_context)
    except Exception as compact_error:
        # 压缩失败时不能静默吞掉：下一轮重试带的仍是原样的超限消息，
        # 必然再次失败。记入 _recovery_state 并由
        # format_execution_error 附在最终的用户可见错误里，
        # 否则用户只会看到一个没有信息量的模型错误。
        agent._recovery_state["compact_error"] = str(compact_error)
        logger.warning("上下文压缩失败，将以原消息重试", exc_info=True)
        return messages


def continue_after_stream_interrupt(agent: Any, messages: list, partial_response: str) -> str:
    """流式中断后，将已生成的部分回复追加到上下文中，用非流式方式续完。

    这确保模型不会丢失任务上下文，避免重新生成开场白。
    """
    from ..i18n import tr

    try:
        # 图模式下历史由 checkpointer 持有，只发增量，避免全量追加导致历史翻倍。
        if agent._graph_mode():
            continuation_messages = [
                AIMessage(content=partial_response),
                HumanMessage(content="请继续完成上面的回复，不要重复已输出的内容。"),
            ]
        else:
            # 构建延续消息：追加 assistant 的部分回复作为历史
            continuation_messages = list(messages)
            continuation_messages.append(AIMessage(content=partial_response))
            continuation_messages.append(
                HumanMessage(content="请继续完成上面的回复，不要重复已输出的内容。")
            )
        return agent._invoke_with_messages(continuation_messages)
    except Exception as e:
        print(tr("agent.stream_continue_failed", error=str(e)))
        return ""


__all__ = [
    "MAX_OUTPUT_CONTINUATION_TEXT",
    "MAX_OUTPUT_TOKENS_PATTERNS",
    "MAX_RETRIES",
    "NON_RETRYABLE_ERROR_PATTERNS",
    "PROMPT_TOO_LONG_PATTERNS",
    "RECOVERABLE_ERROR_PATTERNS",
    "RETRY_BACKOFF_BASE",
    "RETRYABLE_STATUS_CODES",
    "classify_error",
    "classify_error_text",
    "classify_exception",
    "continue_after_stream_interrupt",
    "detect_interrupt",
    "drain_invoke_interrupts",
    "force_compact_session",
    "format_execution_error",
    "recover_after_max_output_tokens",
    "recover_after_prompt_too_long",
    "recover_after_recoverable",
    "resolve_interrupt",
    "resume_after_interrupt",
    "retry_delay",
]
