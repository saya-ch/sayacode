"""工作区文件检索工具，路径参数与结果都统一成工作区内真实路径。

官方检索中间件把工作区根映射成虚拟根 `/`：命中结果返回 `/lib/...` 这类路径，
模型再交给 read_file 会落到文件系统根目录而找不到文件；显式传真实绝对路径
`/app` 时又会被拼成 `<root>/app` 直接查空。
这里保留官方检索实现，只覆写路径解析与结果展示，避免复制整套检索逻辑。"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from langchain.agents.middleware import FilesystemFileSearchMiddleware
from langchain_core.tools import tool


def _real_path_line(line: str, root: Path) -> str:
    """把一行虚拟路径还原成工作区内真实绝对路径，非路径行原样返回。

    命中行形如 `/src/a.py`、`/src/a.py:12:content` 或 `/src/a.py:3`，
    只替换行首到第一个冒号之间的路径部分。"""
    if not line.startswith("/"):
        return line
    head, separator, tail = line.partition(":")
    return f"{root / head.lstrip('/')}{separator}{tail}"


def _real_paths(text: str, root: Path) -> str:
    """逐行还原检索结果里的虚拟路径，保持原有排序与分隔。"""
    if not text:
        return text
    return "\n".join(_real_path_line(line, root) for line in text.splitlines())


class WorkspaceFileSearchMiddleware(FilesystemFileSearchMiddleware):
    """工作区文件检索，参数与结果都用工作区内真实路径。

    参数与返回，同官方中间件，root_path 是工作区根。
    调用约束，结果只列工作区内文件；越界的绝对路径按虚拟路径回落到工作区，
    因此不会因模型传 `/etc` 这类路径而越出工作区。
    坑点是官方实现返回 `/x` 虚拟路径，直接交给文件工具会找不到文件。"""

    def __init__(
        self,
        *,
        root_path: str,
        use_ripgrep: bool = True,
        max_file_size_mb: int = 10,
    ) -> None:
        super().__init__(
            root_path=root_path,
            use_ripgrep=use_ripgrep,
            max_file_size_mb=max_file_size_mb,
        )
        root = self.root_path
        glob_inner = self.glob_search
        grep_inner = self.grep_search

        @tool
        def glob_search(pattern: str, path: str = "/") -> str:
            """Fast file pattern matching tool that works with any codebase size.

            Supports glob patterns like `**/*.js` or `src/**/*.ts`.

            Returns matching file paths sorted by modification time.

            Use this tool when you need to find files by name patterns.

            Args:
                pattern: The glob pattern to match files against.
                path: The directory to search in. Accepts a workspace absolute
                    path, a workspace relative path, or `/` for the workspace root.

            Returns:
                Newline-separated list of matching file paths as workspace
                absolute paths, sorted by modification time (most recently
                modified first). Returns `'No files found'` if no matches.
            """
            return _real_paths(glob_inner.invoke({"pattern": pattern, "path": path}), root)

        @tool
        def grep_search(
            pattern: str,
            path: str = "/",
            include: str | None = None,
            output_mode: Literal["files_with_matches", "content", "count"] = (
                "files_with_matches"
            ),
        ) -> str:
            """Fast content search tool that works with any codebase size.

            Searches file contents using regular expressions. Supports full regex
            syntax and filters files by pattern with the include parameter.

            Args:
                pattern: The regular expression pattern to search for in file contents.
                path: The directory to search in. Accepts a workspace absolute
                    path, a workspace relative path, or `/` for the workspace root.
                include: File pattern to filter (e.g., `'*.js'`, `'*.{ts,tsx}'`).
                output_mode: Output format: `'files_with_matches'`, `'content'`,
                    or `'count'`.

            Returns:
                Search results formatted according to `output_mode`, with file
                paths as workspace absolute paths. Returns `'No matches found'`
                if no results.
            """
            return _real_paths(
                grep_inner.invoke(
                    {
                        "pattern": pattern,
                        "path": path,
                        "include": include,
                        "output_mode": output_mode,
                    }
                ),
                root,
            )

        self.glob_search = glob_search
        self.grep_search = grep_search
        self.tools = [glob_search, grep_search]

    def _validate_and_resolve_path(self, path: str) -> Path:
        """解析工作区内真实绝对路径、虚拟路径或相对路径。

        参数是模型给的路径，返回工作区内的绝对路径。
        调用约束是含 `..` 或 `~` 的路径直接拒绝；越界绝对路径按虚拟路径
        回落到工作区，与官方实现的虚拟根语义保持兼容。"""
        text = str(path or "/")
        if ".." in text or "~" in text:
            msg = "Path traversal not allowed"
            raise ValueError(msg)
        candidate = Path(text)
        if candidate.is_absolute():
            resolved = candidate.resolve()
            if resolved.is_relative_to(self.root_path):
                return resolved
        relative = text.lstrip("/")
        full = (self.root_path / relative).resolve()
        if not full.is_relative_to(self.root_path):
            msg = f"Path outside root directory: {text}"
            raise ValueError(msg)
        return full


__all__ = ["WorkspaceFileSearchMiddleware"]
