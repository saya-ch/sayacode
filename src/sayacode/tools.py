"""工作区工具直接表现为原生工具。

系统和仓库操作放在这里。审批调度和智能体状态归运行时管理。"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal

from langchain.tools import ToolRuntime, tool
from langchain_core.tools import BaseTool

from .policy import context_value, workspace_path

_IGNORED = {".git", ".venv", "venv", "node_modules", "__pycache__", ".sayacode_outputs"}
_EDIT_LOCK = threading.RLock()


def process_creation_options() -> dict[str, Any]:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def attach_process_tree(process: asyncio.subprocess.Process) -> Any:
    """给视窗进程绑定内核作业寿命。Unix 沿用自身会话。"""
    if sys.platform != "win32":
        return None
    import win32api
    import win32con
    import win32job

    job = win32job.CreateJobObject(None, f"Local\\SAYACODE-{uuid.uuid4().hex}")
    try:
        limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
        handle = win32api.OpenProcess(
            win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, process.pid
        )
        try:
            win32job.AssignProcessToJobObject(job, handle)
        finally:
            handle.Close()
        return job
    except BaseException:
        job.Close()
        raise


def close_process_tree(job: Any) -> None:
    if job is not None:
        job.Close()


async def stop_process_tree(process: asyncio.subprocess.Process, job: Any = None) -> None:
    """原进程已退出也要停掉派生子进程。"""
    if sys.platform == "win32":
        if job is not None:
            import win32job

            win32job.TerminateJobObject(job, 1)
        elif process.returncode is None:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **process_creation_options(),
            )
            await killer.wait()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    await process.wait()


def _root(runtime: ToolRuntime) -> Path:
    return workspace_path(runtime.context)


def _path(runtime: ToolRuntime, path: str) -> Path:
    return workspace_path(runtime.context, path)


def _output_dir(runtime: ToolRuntime) -> Path:
    value = context_value(runtime.context, "output_dir")
    result = Path(value) if value else _root(runtime) / ".sayacode_outputs"
    result = result.expanduser().resolve()
    result.mkdir(parents=True, exist_ok=True)
    return result


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        if path.exists():
            shutil.copymode(path, temporary)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_exact(path: Path) -> str:
    with path.open(encoding="utf-8", newline="") as handle:
        return handle.read()


def _files(root: Path) -> Iterator[Path]:
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(
            name
            for name in dirs
            if name not in _IGNORED and not (Path(directory) / name).is_symlink()
        )
        for name in sorted(names):
            path = Path(directory) / name
            yield path


def _display_path(path: Path, workspace: Path) -> str:
    return str(path.relative_to(workspace)) if path.is_relative_to(workspace) else str(path)


def _limited(value: Any, runtime: ToolRuntime, source: str) -> Any:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    limit = int(context_value(runtime.context, "output_limit_bytes", 64 * 1024))
    if limit <= 0:
        limit = 64 * 1024
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return value
    path = _output_dir(runtime) / f"{source}-{uuid.uuid4().hex}.txt"
    path.write_text(text, encoding="utf-8")
    return {
        "preview": encoded[:limit].decode("utf-8", "ignore"),
        "characters": len(text),
        "bytes": len(encoded),
        "output_file": path.name,
    }


@tool
def read_file(
    path: str, runtime: ToolRuntime[Any], offset: int = 1, limit: int = 250
) -> dict[str, Any]:
    """Read UTF-8 text with one-based line numbers; use offset and limit for large files."""
    if offset < 1 or not 1 <= limit <= 2000:
        raise ValueError("offset must be positive and limit must be 1..2000")
    lines = _path(runtime, path).read_text(encoding="utf-8-sig").splitlines()
    selected = lines[offset - 1 : offset - 1 + limit]
    return {
        "path": path,
        "total_lines": len(lines),
        "offset": offset,
        "content": _limited(
            "\n".join(f"{i}: {line}" for i, line in enumerate(selected, offset)), runtime, "read"
        ),
    }


@tool
def write_file(path: str, content: str, runtime: ToolRuntime[Any]) -> dict[str, Any]:
    """Create or replace a UTF-8 file, creating its parent directories."""
    target = _path(runtime, path)
    with _EDIT_LOCK:
        _atomic_write(target, content)
    return {"path": path, "bytes": len(content.encode("utf-8"))}


def _replace(content: str, old: str, new: str, replace_all: bool) -> tuple[str, int]:
    if not old:
        raise ValueError("old_text must not be empty")
    matches = content.count(old)
    if not matches:
        raise ValueError("old_text was not found; read the file again")
    if matches != 1 and not replace_all:
        raise ValueError(
            f"old_text matches {matches} locations; provide more context or replace_all"
        )
    return content.replace(old, new, -1 if replace_all else 1), matches


@tool
def search_replace(
    path: str, old_text: str, new_text: str, runtime: ToolRuntime[Any], replace_all: bool = False
) -> dict[str, Any]:
    """Replace an exact text match; ambiguous matches fail unless replace_all is true."""
    target = _path(runtime, path)
    with _EDIT_LOCK:
        content, count = _replace(_read_exact(target), old_text, new_text, replace_all)
        _atomic_write(target, content)
    return {"path": path, "replacements": count}


@tool
def delete_file(path: str, runtime: ToolRuntime[Any], recursive: bool = False) -> dict[str, Any]:
    """Delete a file or empty directory; recursive must be explicit for a directory tree."""
    target = _path(runtime, path)
    lexical = Path(path).expanduser()
    lexical = lexical if lexical.is_absolute() else _root(runtime) / lexical
    with _EDIT_LOCK:
        if lexical.is_symlink():
            lexical.unlink()
        elif target.is_dir():
            shutil.rmtree(target) if recursive else target.rmdir()
        else:
            target.unlink()
    return {"path": path, "deleted": True}


@tool
def list_directory(path: str, runtime: ToolRuntime[Any]) -> Any:
    """List immediate directory entries and sizes, including outside the workspace."""
    target = _path(runtime, path)
    rows = []
    for entry in sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
        rows.append(
            {"name": entry.name, "directory": entry.is_dir(), "bytes": entry.stat().st_size}
        )
    return _limited(rows, runtime, "directory")


async def _run_process(
    args: list[str], runtime: ToolRuntime, cwd: str, timeout: float, input_text: str | None = None
) -> dict[str, Any]:
    if not 0 < timeout <= 3600:
        raise ValueError("timeout must be between 0 and 3600 seconds")
    directory = _path(runtime, cwd)
    output_dir = _output_dir(runtime)
    identifier = uuid.uuid4().hex
    stdout_path, stderr_path = (
        output_dir / f"{identifier}.stdout.txt",
        output_dir / f"{identifier}.stderr.txt",
    )
    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "PAGER": "cat"}
    timed_out = False
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=directory,
            env=environment,
            stdout=stdout,
            stderr=stderr,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            **process_creation_options(),
        )
        job = None
        try:
            job = attach_process_tree(process)
            await asyncio.wait_for(
                process.communicate(input_text.encode("utf-8") if input_text is not None else None),
                timeout,
            )
        except TimeoutError:
            timed_out = True
            await stop_process_tree(process, job)
        except asyncio.CancelledError:
            await asyncio.shield(stop_process_tree(process, job))
            raise
        except BaseException:
            await stop_process_tree(process, job)
            raise
        finally:
            # 有限命令不能留下未跟踪的派生进程。
            if sys.platform == "win32":
                close_process_tree(job)
            else:
                await stop_process_tree(process)
    result: dict[str, Any] = {"exit_code": process.returncode, "timed_out": timed_out}
    preview_limit = int(context_value(runtime.context, "output_limit_bytes", 64 * 1024))
    if preview_limit <= 0:
        preview_limit = 64 * 1024
    for name, path in (("stdout", stdout_path), ("stderr", stderr_path)):
        with path.open("rb") as handle:
            result[name] = handle.read(preview_limit).decode("utf-8", "ignore")
        result[f"{name}_file"] = path.name
        result[f"{name}_bytes"] = path.stat().st_size
    return result


@tool
async def execute_command_tool(
    command: str,
    runtime: ToolRuntime[Any],
    cwd: str = ".",
    timeout: float = 120,
    input_text: str | None = None,
) -> dict[str, Any]:
    """Run a noninteractive PowerShell (Windows) or sh command; outputs remain available as files."""
    if sys.platform == "win32":
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if shell is None:
            raise RuntimeError("PowerShell is not installed")
        args = [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command]
    else:
        args = ["/bin/sh", "-c", command]
    return await _run_process(args, runtime, cwd, timeout, input_text)


@tool
def read_output_file(
    path: str,
    runtime: ToolRuntime[Any],
    mode: Literal["head", "tail", "grep"] = "tail",
    lines: int = 100,
    pattern: str = "",
) -> Any:
    """Read saved tool output by its returned filename using head, tail, or regex grep."""
    if not 1 <= lines <= 2000:
        raise ValueError("lines must be 1..2000")
    root = _output_dir(runtime)
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        raise PermissionError("Output path escapes the artifact directory")
    from collections import deque
    from itertools import islice

    with target.open(encoding="utf-8", errors="replace") as handle:
        if mode == "tail":
            selected = list(deque(handle, maxlen=lines))
        elif mode == "head":
            selected = list(islice(handle, lines))
        else:
            matcher = re.compile(pattern)
            selected = list(islice((line for line in handle if matcher.search(line)), lines))
    return _limited("".join(selected), runtime, "output")


def _validate_git_argument(value: str, label: str, *, required: bool = False) -> None:
    if required and not value.strip():
        raise ValueError(f"{label} must not be empty")
    if value.startswith("-") or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise ValueError(f"{label} cannot contain control characters or begin with '-'")


@tool
async def git(
    action: Literal["status", "diff", "log", "branch", "remote", "show"],
    runtime: ToolRuntime[Any],
    cwd: str = ".",
    ref: str = "",
    paths: list[str] | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Run read-only Git queries; use the approved Shell tool for Git changes."""
    _validate_git_argument(ref, "Git reference")
    directory = _path(runtime, cwd)
    safe_paths = []
    for value in paths or []:
        target = _path(runtime, value)
        if not target.is_relative_to(directory):
            raise ValueError("Git paths must be within cwd")
        safe_paths.append(str(target.relative_to(directory)))
    args = ["git", "--no-pager"]
    args += [
        "--no-optional-locks", "-c", "core.fsmonitor=false", "-c",
        f"core.hooksPath={os.devnull}", "-c", "log.showSignature=false", action,
    ]
    if action in {"diff", "show", "log"}:
        args += ["--no-ext-diff", "--no-textconv"]
    if action == "status":
        args += ["--short", "--branch"]
    elif action == "log":
        args += [f"-{max(1, min(limit, 200))}", "--oneline"]
    elif action == "branch":
        args += ["--list", "--all"]
    elif action == "remote":
        args += ["-v"]
    elif action in {"show", "diff"} and ref:
        args.append(ref)
    if safe_paths:
        if action not in {"diff", "show", "log"}:
            raise ValueError("paths are supported for diff, show, and log")
        args += ["--", *safe_paths]
    return await _run_process(args, runtime, cwd, 120)


def _project(root: Path) -> dict[str, Any]:
    file_count = 0
    extensions: dict[str, int] = {}
    for path in _files(root):
        file_count += 1
        extensions[path.suffix or "(none)"] = extensions.get(path.suffix or "(none)", 0) + 1
    manifests = {}
    for name in (
        "package.json",
        "pyproject.toml",
        "Cargo.toml",
        "go.mod",
        "pom.xml",
        "requirements.txt",
    ):
        path = root / name
        if path.is_file():
            manifests[name] = path.read_text(encoding="utf-8", errors="replace")[:20000]
    return {
        "root": str(root),
        "file_count": file_count,
        "extensions": extensions,
        "manifests": manifests,
    }


@tool
def analyze_project(runtime: ToolRuntime[Any], root_dir: str = ".") -> Any:
    """Describe project files, language extensions, and dependency manifests."""
    return _limited(_project(_path(runtime, root_dir)), runtime, "project")


def _symbols(path: Path) -> list[dict[str, Any]]:
    content = path.read_bytes()
    languages: dict[str, Literal["python", "javascript", "typescript", "tsx"]] = {
        ".py": "python",
        ".js": "javascript",
        ".jsx": "javascript",
        ".ts": "typescript",
        ".tsx": "tsx",
    }
    language = languages.get(path.suffix)
    if language is None:
        return []
    try:
        from tree_sitter_language_pack import get_parser
    except ImportError:
        if language != "python":
            raise RuntimeError(
                "Install tree-sitter-language-pack for JavaScript/TypeScript symbols"
            )
        python_tree = ast.parse(content)
        return [
            {
                "name": node.name,
                "kind": "class" if isinstance(node, ast.ClassDef) else "function",
                "line": node.lineno,
            }
            for node in ast.walk(python_tree)
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        ]
    tree = get_parser(language).parse(content)
    rows = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        kind = None
        if node.type in {"class_definition", "class_declaration", "interface_declaration"}:
            kind = "class"
        elif node.type in {
            "function_definition",
            "function_declaration",
            "method_definition",
            "method_signature",
        }:
            kind = "function"
        elif node.type == "variable_declarator":
            value = node.child_by_field_name("value")
            if value and value.type in {"arrow_function", "function_expression"}:
                kind = "function"
        name = node.child_by_field_name("name") if kind else None
        if name is not None:
            rows.append(
                {
                    "name": content[name.start_byte : name.end_byte].decode("utf-8", "replace"),
                    "kind": kind,
                    "line": node.start_point[0] + 1,
                }
            )
        stack.extend(reversed(node.children))
    return rows


@tool
def list_symbols(
    runtime: ToolRuntime[Any],
    root_dir: str = ".",
    query: str = "",
    kind: str = "",
    limit: int = 100,
) -> Any:
    """Find Python/JS/TS/JSX/TSX declarations with names, file paths and one-based lines."""
    return _list_symbols(runtime, root_dir, query, kind, limit)


def _list_symbols(
    runtime: ToolRuntime[Any], root_dir: str, query: str, kind: str, limit: int
) -> Any:
    if not 1 <= limit <= 2000:
        raise ValueError("limit must be 1..2000")
    root = _path(runtime, root_dir)
    candidates = [root] if root.is_file() else _files(root)
    rows: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for path in candidates:
        if path.suffix not in {".py", ".js", ".jsx", ".ts", ".tsx"}:
            continue
        try:
            for item in _symbols(path):
                if query.lower() not in item["name"].lower() or (kind and kind != item["kind"]):
                    continue
                rows.append({**item, "path": _display_path(path, _root(runtime))})
                if len(rows) >= limit:
                    return _limited({"symbols": rows, "skipped": skipped}, runtime, "symbols")
        except (OSError, SyntaxError, UnicodeError) as exc:
            skipped.append({"path": str(path), "reason": str(exc)})
    return _limited({"symbols": rows, "skipped": skipped}, runtime, "symbols")


@tool
async def web_search(
    query: str,
    runtime: ToolRuntime[Any],
    max_results: int = 5,
    region: str = "wt-wt",
    time_range: Literal["", "day", "week", "month", "year"] = "",
) -> Any:
    """Search the web through DDGS or a configured SearXNG, returning titles, URLs and snippets."""
    if not query.strip() or not 1 <= max_results <= 20:
        raise ValueError("query is required and max_results must be 1..20")
    searxng = os.environ.get("SAYACODE_SEARXNG_URL", "").rstrip("/")
    if os.environ.get("SAYACODE_SEARCH_PROVIDER", "ddgs") == "searxng":
        if not searxng:
            raise ValueError("SAYACODE_SEARXNG_URL is required")
        import httpx

        params = {"q": query, "format": "json", "language": region}
        if time_range:
            params["time_range"] = time_range
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            response = await client.get(f"{searxng}/search", params=params)
            response.raise_for_status()
        results = [
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("content", ""),
            }
            for item in response.json().get("results", [])[:max_results]
        ]
    else:
        from ddgs import DDGS

        def search() -> list[dict[str, str]]:
            values = DDGS(timeout=20).text(
                query,
                region=region,
                timelimit={"day": "d", "week": "w", "month": "m", "year": "y"}.get(time_range),
                max_results=max_results,
            )
            return [
                {
                    "title": item.get("title", ""),
                    "url": item.get("href", ""),
                    "snippet": item.get("body", ""),
                }
                for item in values
            ]

        results = await asyncio.to_thread(search)
    return _limited(results, runtime, "web")


_TOOLS = [
    read_file,
    write_file,
    search_replace,
    delete_file,
    list_directory,
    execute_command_tool,
    read_output_file,
    git,
    analyze_project,
    list_symbols,
    web_search,
]


def build_tools(context: Any = None) -> list[BaseTool]:
    """返回原生工具。每次调用由运行时注入执行上下文。"""
    return list(_TOOLS)


def tool_catalog(tools: list[BaseTool] | None = None) -> list[dict[str, Any]]:
    """面向界面描述已注册工具。不引入第二套调用协议。"""
    return [
        {
            "name": item.name,
            "description": item.description,
            "schema": item.tool_call_schema.model_json_schema()
            if hasattr(item.tool_call_schema, "model_json_schema")
            else item.tool_call_schema,
        }
        for item in tools or _TOOLS
    ]


def namespace_mcp_tools(tools: list[BaseTool]) -> list[BaseTool]:
    """保留官方结构和调用方式。给远端工具预留命名空间。"""
    result = []
    names: set[str] = set()
    for item in tools:
        original = item.name
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", original)
        if safe != original or len(safe) > 59:
            safe = safe[:46] + "_" + hashlib.sha256(original.encode()).hexdigest()[:12]
        name = f"mcp__{safe}"
        if name in names:
            raise ValueError(f"Duplicate MCP tool name: {original}")
        names.add(name)
        metadata = {
            **(item.metadata or {}),
            "sayacode_origin": "mcp",
            "mcp_original_name": original,
        }
        result.append(item.model_copy(update={"name": name, "metadata": metadata}))
    return result


__all__ = [
    "build_tools",
    "tool_catalog",
    "namespace_mcp_tools",
    "process_creation_options",
    "attach_process_tree",
    "stop_process_tree",
    "close_process_tree",
]
