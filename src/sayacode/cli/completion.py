"""基于帮助目录的斜杠命令补全与键盘选择。"""

from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import Callable, Iterator
from typing import Any

from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition, has_completions
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent

from .help import TOPICS


def _subsequence(needle: str, haystack: str) -> bool:
    """允许省略命令中的字符，如 /mdad 匹配 /model add。"""
    position = 0
    for character in needle:
        position = haystack.find(character, position)
        if position < 0:
            return False
        position += 1
    return True


class SlashCommandCompleter(Completer):
    """只补全斜杠命令；说明与常见操作直接复用帮助目录。"""

    def __init__(self, app: Any, *, language: Callable[[], str]) -> None:
        self.app = app
        self.language = language
        self._sessions: tuple[tuple[str, str, str], ...] | None = None
        self._tasks: tuple[tuple[str, str, str, str], ...] | None = None

    async def refresh_directory(self) -> None:
        """在主输入开始前读取会话和任务；按键补全只使用这份快照。"""
        workspace = getattr(self.app, "workspace", None)
        runtime = getattr(self.app, "runtime", None)
        tasks = getattr(self.app, "tasks", None)
        queries: list[tuple[str, Any]] = []
        for kind, owner, method in (
            ("sessions", runtime, "list_threads"),
            ("tasks", tasks, "list"),
        ):
            reader = getattr(owner, method, None)
            if callable(reader):
                queries.append((kind, reader))
        if not queries:
            return

        async def read(reader: Any) -> Any:
            result = reader(workspace=workspace)
            return await result if inspect.isawaitable(result) else result

        results = await asyncio.gather(
            *(read(reader) for _, reader in queries), return_exceptions=True
        )
        for (kind, _), result in zip(queries, results, strict=True):
            if isinstance(result, BaseException):
                if isinstance(result, Exception):
                    continue
                raise result
            if not isinstance(result, list):
                continue
            if kind == "sessions":
                sessions: list[tuple[str, str, str]] = []
                for item in result:
                    if not isinstance(item, dict) or item.get("is_background"):
                        continue
                    thread_id = item.get("thread_id")
                    if isinstance(thread_id, str) and thread_id:
                        sessions.append(
                            (
                                thread_id,
                                str(item.get("title") or ""),
                                str(item.get("status") or ""),
                            )
                        )
                self._sessions = tuple(sessions)
            else:
                saved_tasks: list[tuple[str, str, str, str]] = []
                for item in result:
                    task_id = getattr(item, "task_id", None)
                    if not isinstance(task_id, str) or not task_id:
                        continue
                    saved_tasks.append(
                        (
                            task_id,
                            str(getattr(item, "title", "") or ""),
                            str(getattr(item, "role", "") or ""),
                            str(getattr(item, "status", "") or ""),
                        )
                    )
                self._tasks = tuple(saved_tasks)

    def _commands(self, typed: str) -> Iterator[tuple[str, str, int]]:
        bare_slash = typed == "/"
        language = self.language()
        for topic in TOPICS:
            summary = topic.summary(language)
            usage = topic.shown_usage(language)
            yield f"/{topic.name}", f"{summary}  ·  {usage}", 0
            if bare_slash:
                continue
            for alias in topic.aliases:
                alias_label = "别名" if language == "zh" else "alias"
                yield f"/{alias}", f"{alias_label}  ·  {summary}", 2
            for action in topic.quick_actions:
                yield action, summary, 1

    def _arguments(self, typed: str) -> Iterator[tuple[str, str, int]]:
        match = re.fullmatch(
            r"/(model|config|skill|team|session)\s+(\w+)\s+([^\s]*)",
            typed,
            flags=re.IGNORECASE,
        )
        if match is None:
            return
        noun, action, _ = match.groups()
        noun, action = noun.casefold(), action.casefold()
        language = self.language()
        if noun in {"model", "config"} and action in {"use", "test", "show", "remove", "key"}:
            yield from self._profile_names(action, language)
        elif noun == "skill" and action in {"use", "show"}:
            yield from self._skill_names(action)
        elif noun == "team" and action in {
            "status",
            "pending",
            "approve",
            "reject",
            "wait",
            "stop",
            "resume",
            "followup",
            "diff",
            "apply",
            "cleanup",
        }:
            yield from self._task_ids(action, language)
        elif noun == "session" and action == "use":
            yield from self._session_ids(language)

    def _session_ids(self, language: str) -> Iterator[tuple[str, str, int]]:
        current = getattr(self.app, "session_id", None)
        if self._sessions is None:
            if isinstance(current, str) and current:
                label = "当前会话" if language == "zh" else "current session"
                yield f"/session use {current}", label, 0
            return
        for session_id, title, status in self._sessions:
            label = title[:60] or ("未命名会话" if language == "zh" else "Untitled session")
            current_label = "当前" if language == "zh" else "current"
            details = [label]
            if session_id == current:
                details.append(current_label)
            if status:
                details.append(status)
            yield f"/session use {session_id}", "  ·  ".join(details), 0

    def _profile_names(self, action: str, language: str) -> Iterator[tuple[str, str, int]]:
        profiles = getattr(getattr(self.app, "config", None), "profiles", {})
        if not isinstance(profiles, dict):
            return
        label = "已配置模型" if language == "zh" else "configured model"
        for name, profile in profiles.items():
            model_id = getattr(profile, "model_id", "")
            detail = f"{label}  ·  {model_id}" if model_id else label
            yield f"/model {action} {name}", detail, 0

    def _skill_names(self, action: str) -> Iterator[tuple[str, str, int]]:
        skills = getattr(self.app, "skills", None)
        list_skills = getattr(skills, "list", None)
        if not callable(list_skills):
            return
        workspace = getattr(self.app, "workspace", None)
        try:
            infos = list_skills(workspace)
        except (OSError, ValueError):
            return
        for info in infos:
            name = getattr(info, "name", None)
            if isinstance(name, str):
                yield f"/skill {action} {name}", str(getattr(info, "description", "")), 0

    def _task_ids(self, action: str, language: str) -> Iterator[tuple[str, str, int]]:
        if self._tasks is not None:
            for task_id, title, role, status in self._tasks:
                details = [role, title[:60], status]
                yield (
                    f"/team {action} {task_id}",
                    "  ·  ".join(detail for detail in details if detail),
                    0,
                )
            return
        tasks = getattr(self.app, "tasks", None)
        active_ids = getattr(tasks, "active_task_ids", None)
        ids = set(active_ids()) if callable(active_ids) else set()
        ids.update(getattr(self.app, "_spawned_task_ids", ()))
        label = "后台任务 ID" if language == "zh" else "background task ID"
        for task_id in sorted(ids):
            yield f"/team {action} {task_id}", label, 0

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterator[Completion]:
        """输入斜杠即列目录，输入更多字符后按前缀与模糊匹配排序。"""
        typed = document.text_before_cursor
        if not typed.startswith("/") or "\n" in typed or document.text_after_cursor:
            return
        query = typed.casefold()
        compact_query = "".join(query.split())
        matches: list[tuple[int, int, str, str]] = []
        seen: set[str] = set()
        for command, description, priority in (
            *self._arguments(typed),
            *self._commands(typed),
        ):
            if command in seen:
                continue
            seen.add(command)
            command_lower = command.casefold()
            if command_lower.startswith(query):
                rank = 0
            elif query != "/" and _subsequence(compact_query, "".join(command_lower.split())):
                rank = 1
            elif query.lstrip("/") in description.casefold() and query != "/":
                rank = 2
            else:
                continue
            matches.append((rank, priority, command, description))
        # 前缀优先，其次是完整命令和帮助目录顺序；保留原有目录结构。
        matches.sort(key=lambda item: (item[0], item[1]))
        for _, _, command, description in matches:
            yield Completion(
                command,
                start_position=-len(typed),
                display=command,
                display_meta=description,
            )


def slash_command_bindings() -> KeyBindings:
    """菜单打开时用方向键选项，回车先接收候选再执行命令。"""
    bindings = KeyBindings()

    @Condition
    def slash_menu() -> bool:
        from prompt_toolkit.application import get_app

        return get_app().current_buffer.text.startswith("/")

    active = has_completions & slash_menu

    @bindings.add("down", filter=active, eager=True)
    def select_next(event: KeyPressEvent) -> None:
        event.current_buffer.complete_next()

    @bindings.add("up", filter=active, eager=True)
    def select_previous(event: KeyPressEvent) -> None:
        event.current_buffer.complete_previous()

    @bindings.add("enter", filter=active, eager=True)
    def accept_candidate(event: KeyPressEvent) -> None:
        buffer = event.current_buffer
        state = buffer.complete_state
        if state is None or not state.completions:
            buffer.validate_and_handle()
            return
        candidate = state.current_completion or state.completions[0]
        original = state.original_document.text
        buffer.apply_completion(candidate)
        # 输入已经是完整命令时，一次回车即可执行。
        if original == candidate.text:
            buffer.validate_and_handle()

    return bindings
