"""Web 宿主的产品操作；复用现有配置、扩展和记忆服务。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from filelock import AsyncFileLock
from langchain.tools import tool

from ..agent.events import _message_text
from ..agent.models import _model_error_message, model_for
from ..agent.runtime import AgentRuntime
from ..application import SayacodeApp
from ..approvals import JevReviewer
from ..config import Config, ConfigRepository, JevConfig, Profile
from ..diagnostics import _doctor
from ..paths import AppPaths
from ..profiles import _new_profile_name


def _profile_view(profile: Profile) -> dict[str, Any]:
    return {
        "name": profile.name,
        "protocol": profile.protocol,
        "base_url": profile.base_url,
        "model_id": profile.model_id,
        "context_length": profile.context_length,
        "max_output_tokens": profile.max_output_tokens,
        "has_api_key": bool(profile.api_key),
        "summary_trigger_ratio": profile.summary_trigger_ratio,
    }


def _memory_view(value: dict[str, Any]) -> dict[str, Any]:
    scope = value.get("scope", {})
    return {
        "id": str(value["id"]),
        "scope": scope.get("kind", "") if isinstance(scope, dict) else getattr(scope, "kind", ""),
        "subject": str(value.get("subject") or ""),
        "text": str(value.get("text") or ""),
        "state": str(value.get("effective_state") or value.get("state") or "active"),
        "pinned": bool(value.get("pinned", False)),
        "updated_at": str(value.get("updated_at") or "") or None,
    }


def _write_project_server(path: Path, name: str, config: dict[str, Any] | None) -> None:
    """在现有 `.mcp.json` 格式内原子改一个服务，保留其他字段。"""
    original: dict[str, Any] = {}
    if path.exists():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("项目 .mcp.json 必须是对象")
        original = loaded
    nested = "mcpServers" in original or not original
    source = original.get("mcpServers", {}) if nested else original
    if not isinstance(source, dict):
        raise ValueError("项目 mcpServers 必须是对象")
    servers = dict(source)
    if config is None:
        if name not in servers:
            raise KeyError(name)
        del servers[name]
    else:
        servers[name] = config
    updated = {**original, "mcpServers": servers} if nested else servers
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".mcp-", suffix=".tmp", delete=False
        ) as handle:
            temporary = handle.name
            json.dump(updated, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


class ProductOperations:
    """供 WebHost 继承的领域操作；本类不负责 HTTP 和运行调度。"""

    config: Config
    repository: ConfigRepository
    runtime: AgentRuntime
    paths: AppPaths

    async def _app_for_workspace(self, workspace_id: str) -> SayacodeApp:
        raise NotImplementedError

    async def _app_for_thread(self, thread_id: str) -> SayacodeApp:
        raise NotImplementedError

    async def _workspace(self, workspace_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def _invalidate_handles(self) -> None:
        raise NotImplementedError

    async def _reload_all_mcp(self) -> None:
        raise NotImplementedError

    async def list_profiles(self) -> dict[str, Any]:
        return {
            "active_profile": self.config.default_profile,
            "profiles": [_profile_view(item) for item in self.config.profiles.values()],
        }

    async def create_profile(self, values: Mapping[str, Any]) -> dict[str, Any]:
        values = dict(values)
        name = values.pop("name", None) or _new_profile_name(
            str(values["model_id"]), self.config.profiles
        )
        if name in self.config.profiles:
            raise ValueError(f"模型配置已存在：{name}")
        profile = Profile(name=name, **values)
        self.config.profiles[name] = profile
        self.config.default_profile = self.config.default_profile or name
        await self.repository.save(self.config)
        self._invalidate_handles()
        return _profile_view(profile)

    async def update_profile(self, name: str, values: Mapping[str, Any]) -> dict[str, Any]:
        profile = self.config.profile(name)
        fields = asdict(profile)
        fields.update(values)
        replacement = Profile.from_dict(fields)
        self.config.profiles[name] = replacement
        await self.repository.save(self.config)
        self._invalidate_handles()
        return _profile_view(replacement)

    async def test_profile(self, name: str) -> dict[str, Any]:
        """使用官方模型构造器测试文本、工具调用和流式能力。"""
        profile = self.config.profile(name)
        model = model_for(profile)
        result: dict[str, Any] = {
            "profile": name,
            "ok": False,
            "text": False,
            "tool_calling": False,
            "stream": False,
            "errors": {},
        }
        try:
            response = await asyncio.wait_for(model.ainvoke("Reply with exactly: OK"), timeout=60)
            result["text"] = bool(_message_text(response).strip())
        except Exception as error:
            result["errors"]["text"] = _model_error_message(error, profile)
            return result

        @tool
        def sayacode_capability_probe(value: str) -> str:
            """原样返回输入，用来验证模型的原生工具调用。"""
            return value

        try:
            bound = model.bind_tools([sayacode_capability_probe])
            response = await asyncio.wait_for(
                bound.ainvoke("Call sayacode_capability_probe with value 'ping'."), timeout=60
            )
            result["tool_calling"] = any(
                call.get("name") == "sayacode_capability_probe"
                for call in getattr(response, "tool_calls", [])
            )
        except Exception as error:
            result["errors"]["tool_calling"] = _model_error_message(error, profile)
        try:
            chunks = []
            async for chunk in model.astream("Reply with exactly: OK"):
                chunks.append(_message_text(chunk))
            result["stream"] = bool("".join(chunks).strip())
        except Exception as error:
            result["errors"]["stream"] = _model_error_message(error, profile)
        result["ok"] = all(result[key] for key in ("text", "tool_calling", "stream"))
        return result

    async def select_profile(self, name: str) -> dict[str, Any]:
        self.config.profile(name)
        self.config.default_profile = name
        await self.repository.save(self.config)
        self._invalidate_handles()
        return {"active_profile": name}

    async def delete_profile(self, name: str) -> dict[str, Any]:
        if name not in self.config.profiles:
            raise KeyError(name)
        del self.config.profiles[name]
        if self.config.default_profile == name:
            self.config.default_profile = next(iter(self.config.profiles), None)
        await self.repository.save(self.config)
        self._invalidate_handles()
        return {"active_profile": self.config.default_profile}

    async def list_mcp(self, workspace_id: str) -> dict[str, Any]:
        app = await self._app_for_workspace(workspace_id)
        project = app.mcp._project_servers()
        trusted = app.mcp.trusted
        status = "error" if app.mcp.error else "ready"
        rows: list[dict[str, Any]] = [
            {"name": name, "scope": "user", "status": status, "error": app.mcp.error}
            for name in self.config.mcp_servers
        ]
        rows.extend(
            {
                "name": name,
                "scope": "project",
                "status": status if trusted else "untrusted",
                "error": app.mcp.error if trusted else None,
            }
            for name in project
        )
        return {
            "trusted_project": trusted,
            "servers": rows,
            "available_tools": [item.name for item in app.mcp.tools],
        }

    async def _update_project_mcp(
        self, app: SayacodeApp, name: str, config: dict[str, Any] | None
    ) -> None:
        path = app.workspace / ".mcp.json"
        digest = hashlib.sha256(str(path).encode()).hexdigest()[:24]
        lock = AsyncFileLock(str(self.paths.home / f"mcp-{digest}.lock"), timeout=15)
        async with lock:
            await asyncio.to_thread(_write_project_server, path, name, config)

    async def add_mcp_server(
        self, workspace_id: str, name: str, scope: str, config: Mapping[str, Any]
    ) -> dict[str, Any]:
        app = await self._app_for_workspace(workspace_id)
        if scope == "user":
            self.config.mcp_servers[name] = dict(config)
            await self.repository.save(self.config)
            await self._reload_all_mcp()
        elif scope == "project":
            await self._update_project_mcp(app, name, dict(config))
        else:
            raise ValueError("MCP 范围只能是 user 或 project")
        if scope == "project":
            await app.mcp.reload()
        self._invalidate_handles()
        return {"status": "error" if app.mcp.error else "ready", "name": name, "error": app.mcp.error}

    async def remove_mcp_server(self, workspace_id: str, name: str, scope: str) -> dict[str, Any]:
        app = await self._app_for_workspace(workspace_id)
        if scope == "user":
            if name not in self.config.mcp_servers:
                raise KeyError(name)
            del self.config.mcp_servers[name]
            await self.repository.save(self.config)
            await self._reload_all_mcp()
        elif scope == "project":
            await self._update_project_mcp(app, name, None)
        else:
            raise ValueError("MCP 范围只能是 user 或 project")
        if scope == "project":
            await app.mcp.reload()
        self._invalidate_handles()
        return {"status": "removed", "name": name}

    async def set_mcp_trust(self, workspace_id: str, trusted: bool) -> dict[str, Any]:
        app = await self._app_for_workspace(workspace_id)
        names = set(self.config.trusted_mcp_projects)
        if trusted:
            names.add(str(app.workspace))
        else:
            names.discard(str(app.workspace))
        self.config.trusted_mcp_projects = sorted(names)
        await self.repository.save(self.config)
        await app.mcp.reload()
        self._invalidate_handles()
        return {"status": "ready" if trusted and not app.mcp.error else "untrusted" if not trusted else "error", "trusted_project": trusted, "error": app.mcp.error}

    async def reload_mcp(self, workspace_id: str) -> dict[str, Any]:
        app = await self._app_for_workspace(workspace_id)
        await app.mcp.reload()
        self._invalidate_handles()
        return {"status": "error" if app.mcp.error else "ready", "error": app.mcp.error}

    async def list_skills(self, workspace_id: str) -> list[dict[str, Any]]:
        app = await self._app_for_workspace(workspace_id)
        return [asdict(info) for info in app.skills.list(app.workspace)]

    async def activate_skill(self, thread_id: str, name: str) -> dict[str, Any]:
        app = await self._app_for_thread(thread_id)
        activated = await app.activate_skill(name, thread_id=thread_id)
        return {"name": activated.name, "active": True}

    async def list_memory(
        self, workspace_id: str, scope: str | None, query: str | None
    ) -> list[dict[str, Any]]:
        app = await self._app_for_workspace(workspace_id)
        return [_memory_view(item) for item in await app.memory.list(scope, query)]

    async def memory_detail(self, workspace_id: str, memory_id: str) -> dict[str, Any]:
        app = await self._app_for_workspace(workspace_id)
        detail = await app.memory.get(memory_id)
        return {
            **_memory_view(detail),
            "validity_reason": detail.get("validity_reason"),
            "evidence": detail.get("evidence", []),
        }

    async def memory_settings(self) -> dict[str, Any]:
        return asdict(self.config.memory)

    async def update_memory_settings(
        self, workspace_id: str, patch: Mapping[str, Any]
    ) -> dict[str, Any]:
        # 记忆服务实现撤销与待处理来源重启，不能直接改 Config 字段。
        app = await self._app_for_workspace(workspace_id)
        result = await app.memory.settings(dict(patch))
        self._invalidate_handles()
        return result

    async def remember(self, workspace_id: str, scope: str, text: str) -> dict[str, Any]:
        app = await self._app_for_workspace(workspace_id)
        return _memory_view(await app.memory.remember(text, scope))

    async def correct_memory(
        self, workspace_id: str, memory_id: str, text: str
    ) -> dict[str, Any]:
        app = await self._app_for_workspace(workspace_id)
        return _memory_view(await app.memory.correct(memory_id, text))

    async def confirm_memory(self, workspace_id: str, memory_id: str) -> dict[str, Any]:
        app = await self._app_for_workspace(workspace_id)
        return _memory_view(await app.memory.confirm(memory_id))

    async def pin_memory(
        self, workspace_id: str, memory_id: str, pinned: bool
    ) -> dict[str, Any]:
        app = await self._app_for_workspace(workspace_id)
        return _memory_view(await app.memory.pin(memory_id, pinned=pinned))

    async def forget_memory(self, workspace_id: str, memory_id: str) -> dict[str, Any]:
        app = await self._app_for_workspace(workspace_id)
        prior = await app.memory.get(memory_id)
        await app.memory.forget(memory_id)
        return _memory_view({**prior, "state": "forgotten", "effective_state": "forgotten"})

    async def git_status(self, workspace_id: str) -> dict[str, Any]:
        app = await self._app_for_workspace(workspace_id)
        result = await app._invoke_native_tool("git", action="status")
        if not isinstance(result, dict):
            raise RuntimeError("Git 查询没有返回结构化结果")
        if result.get("exit_code") != 0:
            raise RuntimeError(str(result.get("stderr") or "Git status failed"))
        output = str(result.get("stdout") or "")
        staged: list[str] = []
        unstaged: list[str] = []
        untracked: list[str] = []
        branch: str | None = None
        for line in output.splitlines():
            if line.startswith("## "):
                branch = line[3:].split("...", 1)[0]
            elif line.startswith("?? "):
                untracked.append(line[3:])
            elif len(line) >= 3:
                if line[0] not in {" ", "?"}:
                    staged.append(line[3:])
                if line[1] not in {" ", "?"}:
                    unstaged.append(line[3:])
        return {
            "branch": branch,
            "clean": not (staged or unstaged or untracked),
            "staged": staged,
            "unstaged": unstaged,
            "untracked": untracked,
            "text": output,
        }

    async def symbols(self, workspace_id: str, query: str) -> list[dict[str, Any]]:
        app = await self._app_for_workspace(workspace_id)
        result = await app._invoke_native_tool("list_symbols", query=query)
        if not isinstance(result, dict):
            raise RuntimeError("符号查询没有返回结构化结果")
        rows = result.get("symbols", [])
        return [dict(item) for item in rows if isinstance(item, dict)]

    async def analyze(self, workspace_id: str) -> dict[str, Any]:
        app = await self._app_for_workspace(workspace_id)
        result = await app._invoke_native_tool("analyze_project")
        return {"text": json.dumps(result, ensure_ascii=False, indent=2, default=str)}

    async def doctor(self, workspace_id: str) -> dict[str, Any]:
        app = await self._app_for_workspace(workspace_id)
        result = await _doctor(app)
        checks = result.get("checks", {})
        return {
            "ok": bool(result.get("ok")),
            "checks": [
                {"name": str(name), "status": "ok" if value else "failed", "detail": str(value)}
                for name, value in checks.items()
            ],
            "summary": str(result.get("mcp_error") or "") or None,
        }

    async def reviewer_status(self) -> dict[str, Any]:
        reviewer = self.config.jev
        return {
            "configured": reviewer is not None,
            "base_url": reviewer.base_url if reviewer else None,
            "model_id": reviewer.model_id if reviewer else None,
            "has_api_key": bool(reviewer and reviewer.api_key),
        }

    async def configure_reviewer(self, values: Mapping[str, Any]) -> dict[str, Any]:
        selected = dict(values)
        if selected.get("api_key") is None and self.config.jev is not None:
            selected["api_key"] = self.config.jev.api_key
        self.config.jev = JevConfig(**selected)
        await self.repository.save(self.config)
        self._invalidate_handles()
        return await self.reviewer_status()

    async def test_reviewer(self) -> dict[str, Any]:
        if self.config.jev is None:
            raise ValueError("Jev 审理尚未配置")
        try:
            await JevReviewer(self.config.jev).test()
        except Exception as error:
            detail = str(error).replace(self.config.jev.api_key, "[已移除凭据]")
            return {"ok": False, "error": detail}
        return {"ok": True}

    async def _reviewer_in_use(self) -> bool:
        raise NotImplementedError

    async def remove_reviewer(self) -> dict[str, Any]:
        if await self._reviewer_in_use():
            raise ValueError("仍有默认设置或会话使用 Jev，请先切换信任档")
        self.config.jev = None
        await self.repository.save(self.config)
        self._invalidate_handles()
        return await self.reviewer_status()


__all__ = ["ProductOperations"]
