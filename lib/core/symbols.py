"""Static source symbol indexing."""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Dict, Iterable, Optional
import ast
import re
import warnings

from ..i18n import tr

SUPPORTED_SYMBOL_EXTENSIONS = {".py", ".js", ".jsx", ".ts", ".tsx"}
MAX_SYMBOL_FILE_SIZE = 1_000_000
MAX_SYMBOL_FILES = 1000

IGNORED_SYMBOL_PATTERNS = {
    ".git",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".env",
    ".env.*",
    ".ssh",
}


@dataclass(frozen=True)
class CodeSymbol:
    """One code symbol."""

    name: str
    kind: str
    path: str
    line: int
    column: int = 0
    parent: str = ""
    signature: str = ""

    @property
    def qualified_name(self) -> str:
        return f"{self.parent}.{self.name}" if self.parent else self.name

    def to_dict(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "qualified_name": self.qualified_name,
            "kind": self.kind,
            "path": self.path,
            "line": self.line,
            "column": self.column,
            "parent": self.parent,
            "signature": self.signature,
        }


class SymbolIndex:
    """Static symbol index for one workspace."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.symbols: list[CodeSymbol] = []

    def scan(self) -> list[CodeSymbol]:
        """Scan supported source files."""
        self.symbols = []
        if not self.root.exists() or not self.root.is_dir():
            return []

        scanned = 0
        for path in self.root.rglob("*"):
            if scanned >= MAX_SYMBOL_FILES:
                break
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_SYMBOL_EXTENSIONS:
                continue
            if _should_ignore(path, self.root):
                continue
            try:
                if path.stat().st_size > MAX_SYMBOL_FILE_SIZE:
                    continue
            except OSError:
                continue

            scanned += 1
            self.symbols.extend(_extract_symbols(path, self.root))

        self.symbols.sort(key=lambda item: (item.path.lower(), item.line, item.kind, item.name.lower()))
        return list(self.symbols)

    def search(
        self,
        query: Optional[str] = None,
        kind: Optional[str] = None,
        limit: int = 80,
    ) -> list[CodeSymbol]:
        """Search indexed symbols."""
        if not self.symbols:
            self.scan()

        query_text = str(query or "").strip().lower()
        kind_text = str(kind or "").strip().lower()
        results: list[CodeSymbol] = []

        for symbol in self.symbols:
            if kind_text and symbol.kind.lower() != kind_text:
                continue
            if query_text and query_text not in symbol.name.lower() and query_text not in symbol.qualified_name.lower():
                continue
            results.append(symbol)
            if len(results) >= limit:
                break

        return results

    def find(self, name: str, limit: int = 20) -> list[CodeSymbol]:
        """Find best matches for a symbol name."""
        target = str(name or "").strip().lower()
        if not target:
            return []
        if not self.symbols:
            self.scan()

        exact = [
            symbol
            for symbol in self.symbols
            if symbol.name.lower() == target or symbol.qualified_name.lower() == target
        ]
        if exact:
            return exact[:limit]

        suffix = [
            symbol
            for symbol in self.symbols
            if symbol.qualified_name.lower().endswith("." + target)
        ]
        if suffix:
            return suffix[:limit]

        return self.search(query=target, limit=limit)


def index_project_symbols(root: str | Path) -> list[CodeSymbol]:
    """Return symbols for a workspace."""
    return SymbolIndex(root).scan()


def render_symbols(symbols: Iterable[CodeSymbol], title: str | None = None) -> str:
    """Render symbols as compact text."""
    title = title or tr("symbols.top")
    symbol_list = list(symbols)
    if not symbol_list:
        return f"{title}\n{tr('symbols.none')}"

    lines = [title, ""]
    for symbol in symbol_list:
        location = f"{symbol.path}:{symbol.line}"
        signature = f" {symbol.signature}" if symbol.signature else ""
        lines.append(f"- {symbol.kind} {symbol.qualified_name}{signature} ({location})")
    return "\n".join(lines)


def summarize_symbol_index(root: str | Path) -> str:
    """Render symbol count summary."""
    symbols = index_project_symbols(root)
    counts: Dict[str, int] = {}
    for symbol in symbols:
        counts[symbol.kind] = counts.get(symbol.kind, 0) + 1

    lines = ["Symbol Index", f"Root: {Path(root).expanduser().resolve()}", f"Total: {len(symbols)}"]
    for kind, count in sorted(counts.items()):
        lines.append(f"{kind}: {count}")
    return "\n".join(lines)


def _extract_symbols(path: Path, root: Path) -> list[CodeSymbol]:
    suffix = path.suffix.lower()
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []

    if suffix == ".py":
        return _extract_python_symbols(path, root, text)
    return _extract_js_like_symbols(path, root, text)


def _extract_python_symbols(path: Path, root: Path, text: str) -> list[CodeSymbol]:
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=SyntaxWarning)
            warnings.filterwarnings("ignore", category=DeprecationWarning)
            tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return []

    relative = _relative_path(path, root)
    symbols: list[CodeSymbol] = []

    def visit(node: ast.AST, parents: list[str]) -> None:
        if isinstance(node, ast.ClassDef):
            symbols.append(CodeSymbol(
                name=node.name,
                kind="class",
                path=relative,
                line=node.lineno,
                column=node.col_offset,
                parent=".".join(parents),
                signature=node.name,
            ))
            for child in node.body:
                visit(child, [*parents, node.name])
            return

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = "method" if parents else "function"
            symbols.append(CodeSymbol(
                name=node.name,
                kind=kind,
                path=relative,
                line=node.lineno,
                column=node.col_offset,
                parent=".".join(parents),
                signature=_python_signature(node),
            ))
            for child in node.body:
                if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    visit(child, [*parents, node.name])
            return

        for child in ast.iter_child_nodes(node):
            visit(child, parents)

    visit(tree, [])
    return symbols


def _python_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args: list[str] = []
    for arg in [*node.args.posonlyargs, *node.args.args]:
        args.append(arg.arg)
    if node.args.vararg:
        args.append("*" + node.args.vararg.arg)
    for arg in node.args.kwonlyargs:
        args.append(arg.arg)
    if node.args.kwarg:
        args.append("**" + node.args.kwarg.arg)
    prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
    return f"{prefix}{node.name}({', '.join(args)})"


def _extract_js_like_symbols(path: Path, root: Path, text: str) -> list[CodeSymbol]:
    relative = _relative_path(path, root)
    symbols: list[CodeSymbol] = []
    lines = text.splitlines()

    patterns = [
        ("class", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)")),
        ("function", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)\)")),
        ("function", re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\(([^)]*)\)|([A-Za-z_$][\w$]*))\s*=>")),
    ]

    for index, line in enumerate(lines, 1):
        for kind, pattern in patterns:
            match = pattern.search(line)
            if not match:
                continue
            name = match.group(1)
            raw_args = match.group(2) if match.lastindex and match.lastindex >= 2 else ""
            if not raw_args and match.lastindex and match.lastindex >= 3:
                raw_args = match.group(3) or ""
            signature = name if kind == "class" else f"{name}({_compact_args(raw_args)})"
            symbols.append(CodeSymbol(
                name=name,
                kind=kind,
                path=relative,
                line=index,
                column=max(0, line.find(name)),
                signature=signature,
            ))
            break

    return symbols


def _compact_args(raw_args: str) -> str:
    return ", ".join(part.strip() for part in str(raw_args or "").split(",") if part.strip())[:160]


def _should_ignore(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = path

    parts = {part.lower() for part in relative.parts}
    for pattern in IGNORED_SYMBOL_PATTERNS:
        normalized = pattern.lower()
        if any(char in normalized for char in "*?[]"):
            if fnmatch(relative.as_posix().lower(), normalized) or any(fnmatch(part, normalized) for part in parts):
                return True
        elif normalized in parts:
            return True
    return False


def _relative_path(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


__all__ = [
    "CodeSymbol",
    "SymbolIndex",
    "index_project_symbols",
    "render_symbols",
    "summarize_symbol_index",
]
