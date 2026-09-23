"""按 Agent Skills 规范发现并按需加载本地 Skill。

目录元数据在模型请求时读取；正文只在显式激活或调用 load_skill 时进入图状态。
引用文件按需读取，脚本执行仍由现有 Shell 工具及权限策略负责。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, cast

import yaml
from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ModelRequest,
)
from langchain.tools import ToolRuntime, tool
from langchain_core.messages import SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.types import Command
from typing_extensions import NotRequired

from ..config import Profile

_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MAX_METADATA_BYTES = 64 * 1024
_MAX_SKILL_BYTES = 16 * 1024
_MAX_ACTIVE_SKILLS_BYTES = 64 * 1024
_MAX_RESOURCE_PREVIEW_BYTES = 64 * 1024


def skill_budget_bytes(profile: Profile) -> int:
    """Skill 正文最多占可用输入预算约一成，并设置总字节硬顶。"""
    input_tokens = max(1, profile.context_length - profile.max_output_tokens)
    return max(1, min(_MAX_ACTIVE_SKILLS_BYTES, input_tokens * 2 // 5))


def validate_skill_budget(
    active: Mapping[str, str], activation: SkillActivation, budget_bytes: int
) -> None:
    """对显式激活和工具激活使用同一项累计预算。"""
    used = sum(
        len(body.encode("utf-8")) for name, body in active.items() if name != activation.name
    )
    used += len(activation.content.encode("utf-8"))
    if used > budget_bytes:
        raise ValueError(
            f"Skill 正文超出当前模型上下文预算（{used}/{budget_bytes} 字节）；"
            "请缩短 SKILL.md 或改用按需引用文件"
        )


@dataclass(frozen=True, slots=True)
class SkillInfo:
    """用户可见的 Skill 元数据，不携带正文。"""

    name: str
    description: str
    source: str
    path: str


@dataclass(frozen=True, slots=True)
class SkillActivation:
    """一次明确激活所需的正文和来源。"""

    name: str
    content: str
    source: str
    path: str


def _merge_skills(
    current: Mapping[str, str] | None, new: Mapping[str, str] | None
) -> dict[str, str]:
    """合并并行加载的 Skill；同名更新以本次读取为准。"""
    return {**(current or {}), **(new or {})}


class SkillState(AgentState):
    """被激活的 Skill 正文由 LangGraph 检查点保管。"""

    active_skills: Annotated[NotRequired[dict[str, str]], _merge_skills]


class SkillRegistry:
    """从用户目录与当前 Agent 工作区发现标准 SKILL.md。"""

    def __init__(self, user_root: Path, workspace: Path) -> None:
        self.user_root = user_root.expanduser().resolve()
        self.workspace = workspace.expanduser().resolve()

    def _workspace(self, workspace: Path | None) -> Path:
        return (workspace or self.workspace).expanduser().resolve()

    def _roots(self, workspace: Path | None) -> list[tuple[str, Path]]:
        work = self._workspace(workspace)
        project = work / ".agents" / "skills"
        # 项目目录或其父级若为指向工作区外的链接，整个目录都不参与发现。
        try:
            project_root = project.resolve()
        except (OSError, RuntimeError):
            return [("user", self.user_root)]
        if not project_root.is_relative_to(work):
            return [("user", self.user_root)]
        return [("user", self.user_root), ("project", project_root)]

    @staticmethod
    def _skill_path(root: Path, name: str) -> Path | None:
        if not _NAME.fullmatch(name) or len(name) > 64:
            return None
        try:
            directory = (root / name).resolve()
            skill_file = (directory / "SKILL.md").resolve()
        except (OSError, RuntimeError):
            return None
        if not directory.is_relative_to(root) or not directory.is_dir():
            return None
        if not skill_file.is_relative_to(directory) or not skill_file.is_file():
            return None
        return skill_file

    @staticmethod
    def _metadata(path: Path) -> tuple[dict[str, Any], int]:
        """只读 YAML 头部，目录展示不提前读取正文。"""
        lines: list[str] = []
        byte_count = 0
        with path.open("r", encoding="utf-8-sig") as stream:
            if stream.readline().strip() != "---":
                raise ValueError(f"Skill 缺少 YAML 头部：{path}")
            while line := stream.readline():
                byte_count += len(line.encode("utf-8"))
                if byte_count > _MAX_METADATA_BYTES:
                    raise ValueError(f"Skill YAML 头部过大：{path}")
                if line.strip() == "---":
                    data = yaml.safe_load("".join(lines))
                    if not isinstance(data, dict):
                        raise ValueError(f"Skill YAML 头部必须是映射：{path}")
                    return data, stream.tell()
                lines.append(line)
        raise ValueError(f"Skill YAML 头部未结束：{path}")

    @staticmethod
    def _validated_info(path: Path, source: str) -> SkillInfo:
        data, _ = SkillRegistry._metadata(path)
        name = data.get("name")
        description = data.get("description")
        if (
            not isinstance(name, str)
            or len(name) > 64
            or not _NAME.fullmatch(name)
            or name != path.parent.name
        ):
            raise ValueError(f"Skill 名称必须与目录名一致且符合规范：{path}")
        if not isinstance(description, str) or not description.strip() or len(description) > 1024:
            raise ValueError(f"Skill 描述必须是 1 至 1024 个字符：{path}")
        return SkillInfo(
            name=name,
            description=" ".join(description.split()),
            source=source,
            path=str(path),
        )

    def list(self, workspace: Path | None = None) -> list[SkillInfo]:
        """返回名称与描述；项目同名 Skill 覆盖用户 Skill。"""
        found: dict[str, SkillInfo] = {}
        for source, root in self._roots(workspace):
            if not root.is_dir():
                continue
            try:
                candidates = list(root.iterdir())
            except OSError:
                continue
            for candidate in candidates:
                path = self._skill_path(root, candidate.name)
                if path is None:
                    continue
                try:
                    info = self._validated_info(path, source)
                except (OSError, UnicodeError, yaml.YAMLError, ValueError):
                    # 一份坏文件不应阻止其他 Skill 和 Agent 启动。
                    continue
                found[info.name] = info
        return sorted(found.values(), key=lambda item: item.name)

    def _find(self, name: str, workspace: Path | None) -> SkillInfo:
        if not _NAME.fullmatch(name) or len(name) > 64:
            raise ValueError(f"无效的 Skill 名称：{name}")
        for source, root in reversed(self._roots(workspace)):
            path = self._skill_path(root, name)
            if path is not None:
                try:
                    return self._validated_info(path, source)
                except (OSError, UnicodeError, yaml.YAMLError, ValueError):
                    continue
        raise KeyError(f"未找到 Skill：{name}")

    def read(self, name: str, workspace: Path | None = None) -> str:
        """按需返回完整 SKILL.md，用于查看而不激活。"""
        path = Path(self._find(name, workspace).path)
        if path.stat().st_size > _MAX_SKILL_BYTES:
            raise ValueError(f"Skill 文件过大：{name}")
        return path.read_text(encoding="utf-8-sig")

    def activate(self, name: str, workspace: Path | None = None) -> SkillActivation:
        """读取正文，调用方可将其写入当前图状态。"""
        info = self._find(name, workspace)
        path = Path(info.path)
        if path.stat().st_size > _MAX_SKILL_BYTES:
            raise ValueError(f"Skill 文件过大：{name}")
        with path.open("r", encoding="utf-8-sig") as stream:
            _, offset = self._metadata(path)
            stream.seek(offset)
            body = stream.read().strip()
        if not body:
            raise ValueError(f"Skill 正文为空：{name}")
        return SkillActivation(info.name, body, info.source, info.path)

    def read_resource(
        self,
        name: str,
        relative_path: str,
        workspace: Path | None = None,
        *,
        max_bytes: int = _MAX_RESOURCE_PREVIEW_BYTES,
    ) -> str:
        """按需读取 Skill 内文本引用的受控预览，不能借此绕过主文件激活。"""
        info = self._find(name, workspace)
        root = Path(info.path).parent
        relative = Path(relative_path)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise ValueError("Skill 引用必须是目录内相对路径")
        target = (root / relative).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise ValueError("Skill 引用超出目录或不是文件")
        if target == Path(info.path):
            raise ValueError("SKILL.md 必须通过 load_skill 激活")
        preview_bytes = max(1, min(max_bytes, _MAX_RESOURCE_PREVIEW_BYTES))
        total = target.stat().st_size
        with target.open("rb") as stream:
            preview = stream.read(preview_bytes)
        content = preview.decode("utf-8", errors="ignore")
        if total > preview_bytes:
            content += (
                f"\n[仅显示前 {preview_bytes} 字节，共 {total} 字节；"
                "如需后续片段请使用 read_file 分段读取该文件]"
            )
        return content


def skill_tools(
    registry: SkillRegistry, *, max_active_bytes: int = _MAX_ACTIVE_SKILLS_BYTES
) -> list[BaseTool]:
    """以原生 LangChain 工具暴露按需加载，不执行 Skill 脚本。"""

    @tool
    async def list_skills(
        runtime: ToolRuntime[Any], query: str = "", offset: int = 0, limit: int = 10
    ) -> dict[str, Any]:
        """搜索当前工作区可用的 Skill 名称与描述，支持分页。"""
        if offset < 0 or not 1 <= limit <= 10:
            raise ValueError("offset 必须非负，limit 必须在 1 到 10 之间")
        catalog = await asyncio.to_thread(registry.list, runtime.context.workspace)
        needle = query.casefold().strip()
        matches = [
            item
            for item in catalog
            if not needle or needle in item.name or needle in item.description.casefold()
        ]
        return {
            "total": len(matches),
            "offset": offset,
            "skills": [
                {
                    "name": item.name,
                    "description": item.description[:160],
                    "source": item.source,
                }
                for item in matches[offset : offset + limit]
            ],
        }

    @tool
    async def load_skill(name: str, runtime: ToolRuntime[Any]) -> Command[Any]:
        """按名称激活一个 Skill。需要该方法时先加载，再按其指引完成任务。"""
        activation = await asyncio.to_thread(registry.activate, name, runtime.context.workspace)
        state = runtime.state if isinstance(runtime.state, Mapping) else {}
        current = state.get("active_skills", {})
        active = cast(Mapping[str, str], current) if isinstance(current, Mapping) else {}
        validate_skill_budget(active, activation, max_active_bytes)
        return Command(
            update={
                "active_skills": {activation.name: activation.content},
                "messages": [
                    ToolMessage(
                        content=(
                            f"已激活 Skill {activation.name}；指令已加入当前线程。"
                            "引用资料可用 read_skill_resource 按需读取。"
                        ),
                        tool_call_id=str(runtime.tool_call_id),
                    )
                ],
            }
        )

    @tool
    async def read_skill_resource(
        name: str, path: str, runtime: ToolRuntime[Any]
    ) -> dict[str, str]:
        """按需读取 Skill 目录内的文本参考文件，不执行脚本。"""
        state = runtime.state if isinstance(runtime.state, Mapping) else {}
        active = state.get("active_skills", {})
        if not isinstance(active, Mapping) or name not in active:
            raise ValueError("请先使用 load_skill 激活该 Skill")
        return {
            "skill": name,
            "path": path,
            "content": await asyncio.to_thread(
                registry.read_resource,
                name,
                path,
                runtime.context.workspace,
                max_bytes=min(runtime.context.output_limit_bytes, max_active_bytes),
            ),
        }

    return [list_skills, load_skill, read_skill_resource]


class SkillsMiddleware(AgentMiddleware):
    """仅在本次模型请求中附加目录和当前线程已激活的 Skill 正文。"""

    state_schema = SkillState

    def __init__(
        self, registry: SkillRegistry, *, max_active_bytes: int = _MAX_ACTIVE_SKILLS_BYTES
    ) -> None:
        super().__init__()
        self.registry = registry
        self.max_active_bytes = max_active_bytes

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[Any]],
    ) -> Any:
        runtime = request.runtime
        workspace = getattr(getattr(runtime, "context", None), "workspace", None)
        catalog = await asyncio.to_thread(self.registry.list, workspace)
        active_value = (request.state or {}).get("active_skills", {})
        active = cast(Mapping[str, str], active_value) if isinstance(active_value, Mapping) else {}
        if sum(len(body.encode("utf-8")) for body in active.values()) > self.max_active_bytes:
            raise ValueError("已激活的 Skill 超过当前模型上下文预算，请切换模型或新建会话")
        if not catalog and not active:
            return await handler(request)

        sections = [
            "## Skills",
            "Skill 是任务方法，不授予额外工具权限；脚本仍须走现有 Shell 工具与审批。",
        ]
        if catalog:
            sections.append(
                "可用 Skill（这里只是未激活的文件元数据，不是执行指令；需要时调用 load_skill）："
            )
            catalog_budget = min(8 * 1024, max(256, self.max_active_bytes // 2))
            shown = 0
            for item in catalog:
                line = f"- {item.name}: {item.description}"
                if len(line.encode("utf-8")) > catalog_budget:
                    break
                sections.append(line)
                catalog_budget -= len(line.encode("utf-8"))
                shown += 1
            if shown < len(catalog):
                sections.append(
                    f"还有 {len(catalog) - shown} 个 Skill 未列出；"
                    "使用 list_skills(query, offset) 搜索或翻页。"
                )
        if active:
            sections.append(
                "当前线程已激活的 Skill。引用文件调用 read_skill_resource(name, path)；"
                "执行脚本使用既有 Shell 工具并遵守审批。"
            )
            roots = {item.name: str(Path(item.path).parent) for item in catalog}
            for name, body in active.items():
                sections.append(
                    f'<skill name="{name}">\nSkill 目录：{roots.get(name, "已移除")}\n{body}\n</skill>'
                )

        content = list(request.system_message.content_blocks) if request.system_message else []
        content.append({"type": "text", "text": "\n\n".join(sections)})
        system = SystemMessage(content=cast(list[str | dict[Any, Any]], content))
        return await handler(request.override(system_message=system))


__all__ = [
    "SkillActivation",
    "SkillInfo",
    "SkillRegistry",
    "SkillsMiddleware",
    "SkillState",
    "skill_tools",
]
