"""Unified project, memory, and conversation context packaging."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from .context import ProjectContext
from .project_memory import render_memory_for_prompt
from .symbols import SymbolIndex, render_symbols


@dataclass(frozen=True)
class ContextPackRequest:
    """Input for ContextPackager.pack."""

    workspace: str | Path
    project_context: Optional[ProjectContext] = None
    session: Optional[Any] = None
    include_project: bool = True
    include_memory: bool = True
    include_history: bool = False
    include_symbols: bool = True
    agent_mode: str = "build"
    max_files: int = 10
    max_chars: int = 32000
    token_estimator: Optional[Any] = None


@dataclass(frozen=True)
class ContextPackage:
    """Packaged context ready for prompt injection."""

    content: str
    included_sections: tuple[str, ...] = field(default_factory=tuple)
    truncated: bool = False
    estimated_tokens: int = 0
    token_estimate_is_exact: bool = False
    explain: tuple[dict[str, Any], ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class TokenEstimate:
    """Token estimate metadata."""

    tokens: int
    exact: bool = False


class TokenEstimator:
    """Conservative token estimator used when providers do not expose tokenizers."""

    def estimate(self, text: str) -> TokenEstimate:
        chars = len(str(text or ""))
        return TokenEstimate(tokens=max(1, (chars + 3) // 4) if chars else 0, exact=False)


class ContextPackager:
    """Package runtime context through one budgeted path."""

    def pack(self, request: ContextPackRequest) -> ContextPackage:
        workspace = Path(request.workspace).expanduser().resolve()
        sections: list[tuple[str, str]] = []
        mode = str(request.agent_mode or "build").lower()

        project_section: tuple[str, str] | None = None
        symbol_section: tuple[str, str] | None = None

        if request.include_project:
            project_context = request.project_context or ProjectContext(str(workspace))
            project_section = (
                "project",
                project_context.get_context_for_llm(max_files=request.max_files),
            )

        if request.include_symbols:
            symbol_text = self._render_symbols(workspace)
            if symbol_text:
                symbol_section = ("symbols", symbol_text)

        if mode in {"plan", "review"}:
            sections.extend(item for item in (symbol_section, project_section) if item is not None)
        else:
            sections.extend(item for item in (project_section, symbol_section) if item is not None)

        if request.include_memory:
            memory = render_memory_for_prompt(workspace)
            if memory:
                sections.append(("memory", memory))

        if request.include_history and request.session is not None:
            history = self._render_history(request.session)
            if history:
                sections.append(("history", history))

        return self._apply_budget(sections, request.max_chars, estimator=request.token_estimator)

    def explain(self, request: ContextPackRequest) -> dict[str, Any]:
        """Return a machine-readable explanation for context packing decisions."""
        package = self.pack(request)
        return {
            "included_sections": list(package.included_sections),
            "truncated": package.truncated,
            "estimated_tokens": package.estimated_tokens,
            "token_estimate_is_exact": package.token_estimate_is_exact,
            "sections": list(package.explain),
        }

    def _render_history(self, session: Any) -> str:
        messages = getattr(session, "messages", None)
        if messages is None and hasattr(session, "get_messages"):
            try:
                messages = session.get_messages(include_system=False)
            except TypeError:
                messages = session.get_messages()

        rendered: list[str] = []
        for item in list(messages or [])[-20:]:
            role = getattr(item, "role", None)
            content = getattr(item, "content", None)
            if isinstance(item, dict):
                role = item.get("role")
                content = item.get("content")
            if role in {"user", "assistant"} and content:
                rendered.append(f"{role}: {content}")

        if not rendered:
            return ""
        return "## Conversation History\n" + "\n".join(rendered)

    def _render_symbols(self, workspace: Path) -> str:
        try:
            symbols = SymbolIndex(workspace).search(limit=40)
        except Exception:
            return ""
        if not symbols:
            return ""
        return "## Code Symbols\n" + render_symbols(symbols)

    def _apply_budget(
        self,
        sections: Iterable[tuple[str, str]],
        max_chars: int,
        *,
        estimator: Optional[Any] = None,
    ) -> ContextPackage:
        budget = max(0, int(max_chars or 0))
        estimator = estimator or TokenEstimator()
        if budget == 0:
            return ContextPackage(
                content="",
                included_sections=(),
                truncated=True,
                estimated_tokens=0,
                token_estimate_is_exact=False,
                explain=({"section": "all", "status": "dropped", "reason": "zero_budget"},),
            )

        chunks: list[str] = []
        included: list[str] = []
        remaining = budget
        truncated = False
        explanation: list[dict[str, Any]] = []

        for name, content in sections:
            text = str(content or "").strip()
            if not text:
                explanation.append({"section": name, "status": "dropped", "reason": "empty"})
                continue
            separator = "\n\n" if chunks else ""
            needed = len(separator) + len(text)
            estimate = _estimate_tokens(estimator, text)
            if needed <= remaining:
                chunks.append(separator + text)
                included.append(name)
                remaining -= needed
                explanation.append({
                    "section": name,
                    "status": "included",
                    "chars": len(text),
                    "estimated_tokens": estimate.tokens,
                    "token_estimate_is_exact": estimate.exact,
                    "reason": "within_budget",
                })
                continue

            available = remaining - len(separator)
            if available > 80:
                truncated_text = text[:available].rstrip() + "\n[context truncated]"
                chunks.append(separator + truncated_text)
                included.append(name)
                truncated_estimate = _estimate_tokens(estimator, truncated_text)
                explanation.append({
                    "section": name,
                    "status": "truncated",
                    "chars": len(truncated_text),
                    "estimated_tokens": truncated_estimate.tokens,
                    "token_estimate_is_exact": truncated_estimate.exact,
                    "reason": "budget_exhausted",
                })
            else:
                explanation.append({
                    "section": name,
                    "status": "dropped",
                    "chars": len(text),
                    "estimated_tokens": estimate.tokens,
                    "token_estimate_is_exact": estimate.exact,
                    "reason": "insufficient_remaining_budget",
                })
            truncated = True
            break

        content = "".join(chunks).strip()
        total_estimate = _estimate_tokens(estimator, content)
        return ContextPackage(
            content=content,
            included_sections=tuple(included),
            truncated=truncated,
            estimated_tokens=total_estimate.tokens,
            token_estimate_is_exact=total_estimate.exact,
            explain=tuple(explanation),
        )


def _estimate_tokens(estimator: Any, text: str) -> TokenEstimate:
    try:
        value = estimator.estimate(text)
    except Exception:
        return TokenEstimator().estimate(text)
    if isinstance(value, TokenEstimate):
        return value
    if isinstance(value, tuple) and value:
        return TokenEstimate(tokens=int(value[0]), exact=bool(value[1]) if len(value) > 1 else False)
    return TokenEstimate(tokens=int(value or 0), exact=bool(getattr(estimator, "exact", False)))


__all__ = [
    "ContextPackRequest",
    "ContextPackage",
    "ContextPackager",
    "TokenEstimate",
    "TokenEstimator",
]
