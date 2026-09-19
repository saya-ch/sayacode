"""Trace slash 命令：查看最近的运行追踪与单条 trace 的调用树。

/trace 列出最近若干 trace 的摘要；/trace <id> 展开该 trace 的
全部审计事件（含工具耗时与 span 嵌套），用于回答「这一轮到底做了什么、
慢在哪一步」。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

from ..core.audit import AuditLogService
from ..i18n import tr
from ..runtime import RuntimeContext
from ..cli.theme import console, print_error, print_info
from .base import CommandContext, CommandHandler


def _fmt_ms(value: str | int | float | None) -> str:
    """毫秒数转人类可读短串。"""
    if value is None:
        return "-"
    try:
        ms = float(value)
    except (TypeError, ValueError):
        return "-"
    if ms >= 1000:
        return f"{ms / 1000:.1f}s"
    return f"{ms:.0f}ms"


def _fmt_tokens(details: Dict[str, Any]) -> str:
    # 用 Any 承接审计明细动态值
    """渲染 token 用量；两个字段都缺时返回空串。"""
    prompt = details.get("input_tokens")
    completion = details.get("output_tokens")
    if prompt is None and completion is None:
        return ""
    return tr(
        "trace.tokens",
        prompt=prompt if prompt is not None else "?",
        completion=completion if completion is not None else "?",
    )


def _event_line(event: Dict[str, Any], depth: int) -> str:
    """把一条审计事件渲染成缩进一行的树节点。"""
    details = event.get("details") or {}
    kind = str(event.get("type") or "?")
    action = str(event.get("action") or "")
    indent = "  " * max(0, depth)
    parts: List[str] = [indent + ("- " if depth else "• ")]
    if kind == "span":
        parts.append(f"{tr('trace.kind_span')} {details.get('span') or action}")
    elif kind == "tool":
        parts.append(f"{tr('trace.kind_tool')} {action}")
    elif kind == "hook":
        parts.append(f"{tr('trace.kind_hook')} {action}")
    elif kind == "llm":
        parts.append(f"{tr('trace.kind_llm')} {action}")
        tokens = _fmt_tokens(details)
        if tokens:
            parts.append(f"  {tokens}")
    else:
        parts.append(f"{kind} {action}".strip())
    duration = details.get("duration_ms")
    if duration is not None:
        parts.append(f"  {_fmt_ms(duration)}")
    if event.get("allowed") is False:
        parts.append(f"  [{tr('trace.denied')}]")
    return "".join(parts)


def _build_tree(events: List[Dict[str, Any]]) -> List[str]:
    """按 span_id/parent_span 还原嵌套，输出缩进行。

    没有 span 信息的普通事件按发生顺序平铺在根层级；父 span 缺失时
    也退回平铺，保证任何形状的日志都能渲染出来。
    """
    by_span: Dict[str, Dict[str, Any]] = {}
    children: Dict[str, List[str]] = {}
    roots: List[str] = []
    loose: List[Dict[str, Any]] = []
    for index, event in enumerate(events):
        details = event.get("details") or {}
        span_id = str(details.get("span_id") or "")
        if not span_id:
            loose.append(event)
            continue
        key = f"{span_id}#{index}"
        by_span[key] = event
        parent = str(details.get("parent_span") or "")
        parent_key = next(
            (k for k in by_span if k.split("#")[0] == parent), ""
        ) if parent else ""
        if parent_key:
            children.setdefault(parent_key, []).append(key)
        else:
            roots.append(key)

    lines: List[str] = [_event_line(e, 0) for e in loose]

    def _render(key: str, depth: int) -> None:
        lines.append(_event_line(by_span[key], depth))
        for child in children.get(key, []):
            _render(child, depth + 1)

    for root in roots:
        _render(root, 0)
    return lines


@dataclass
class TraceCommandHandler(CommandHandler):
    """展示最近运行追踪或单条 trace 的调用树。"""

    name: str = "trace"
    aliases: tuple[str, ...] = ("traces",)

    def handle(self, command: CommandContext, runtime: RuntimeContext) -> bool:
        """处理 slash 命令，已被消费时返回 True。"""
        service = AuditLogService()
        target = str(command.args or "").strip()
        if target:
            return self._show(service, target)
        return self._list(service)

    def _list(self, service: AuditLogService) -> bool:
        """列出最近的 trace 摘要。"""
        traces = service.list_recent_traces(limit=10)
        if not traces:
            print_info(tr("trace.empty"))
            return True
        console.print()
        console.print(tr("trace.list_title"))
        for entry in traces:
            flag = tr("trace.denied") if entry.get("failed") else tr("trace.ok")
            tools = ", ".join(entry.get("tools") or []) or "-"
            console.print(
                f"  {entry['trace_id']}  {tr('trace.events', count=entry['events'])}  "
                f"{tools}  [{flag}]"
            )
        console.print()
        print_info(tr("trace.usage"))
        return True

    def _show(self, service: AuditLogService, trace_id: str) -> bool:
        """展开单条 trace 的调用树。"""
        events = service.read_by_trace(trace_id, limit=500)
        if not events:
            print_error(tr("trace.not_found", trace_id=trace_id))
            return True
        console.print()
        console.print(tr("trace.detail_title", trace_id=trace_id))
        for line in _build_tree(events):
            console.print(line)
        console.print()
        return True
