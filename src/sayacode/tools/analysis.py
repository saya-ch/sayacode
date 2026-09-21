"""项目结构与符号查询，只读不写，不碰工作区外。

概览走文件漫步加清单截断，符号走语法树解析。
大结果会自动截断转文件，调用方要看输出文件字段。"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Literal

from langchain.tools import ToolRuntime, tool

from .files import _display_path, _files, _limited, _path, _root


def _project(root: Path) -> dict[str, Any]:
    """统计该目录下文件数扩展名和常见依赖清单。

    清单只读固定几个文件名，单个最多读两万字。
    忽略目录由文件漫步统一过滤，这里不再判。"""
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
    """做什么，概览项目文件语言扩展名与依赖清单。

    参数与返回，入参是运行上下文和目录，返回统计字典或截断指引。
    调用约束，目录会被约束在工作区规则内，大结果转文件。
    坑点是只看文件存在不解析内容，清单截断后要读输出文件。"""
    return _limited(_project(_path(runtime, root_dir)), runtime, "project")


def _symbols(path: Path) -> list[dict[str, Any]]:
    """解析单个文件的类和函数声明，返回名字种类和行号。

    分两路走，有语法解析器用它，无解析器只解析脚本并回退。
    非目标后缀直接返回空，调用方要先过滤后缀。"""
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
    """做什么，定位脚本声明及其文件和行号。

    参数与返回，入参是目录查询词种类和条数，返回符号加跳过列表。
    调用约束，条数只能在一到两千之间，超限直接报错。
    坑点是读失败的文件只记跳过不中断，大结果会转输出文件。"""
    return _list_symbols(runtime, root_dir, query, kind, limit)


def _list_symbols(
    runtime: ToolRuntime[Any], root_dir: str, query: str, kind: str, limit: int
) -> Any:
    """符号查询的真正实现，先定候选再逐文件过滤。

    分三步走，校验条数，定候选文件，逐个解析并按名和种类过滤。
    到达条数上限就地返回，读坏的文件记跳过继续跑。
    坑点是单文件既可是目录也可是文件，目录走漫步展开。"""
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
