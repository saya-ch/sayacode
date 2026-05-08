"""
增强的文件操作工具

提供完整的文件操作功能，使用 langchain_core.tools 的 @tool 装饰器。
所有路径都使用 pathlib.Path 进行处理，支持安全检查。

工具列表：
- read_file: 读取文件内容
- write_file: 写入文件内容
- search_replace: 搜索并替换文件内容
- glob_search: 使用 glob 模式搜索文件
- grep_search: 在文件中搜索内容
- create_directory: 创建目录
- delete_file: 删除文件（带危险标记）
- list_directory: 列出目录内容
"""

import difflib
from contextvars import ContextVar, Token
from pathlib import Path, PurePosixPath
from langchain_core.tools import tool
from typing import List, Optional, Dict, Any
import re

# 导入安全检查模块
from .safety import check_file_danger, sanitize_path, check_write_operation
from ..core.permissions import enforce_tool_permission


_DEFAULT_WORKSPACE = Path.cwd().resolve()
_WORKSPACE_CONTEXT: ContextVar[Path | None] = ContextVar("sayacode_file_tools_workspace", default=None)


def set_default_workspace(workspace: str | Path) -> Path:
    """设置文件工具默认工作区。"""
    global _DEFAULT_WORKSPACE
    _DEFAULT_WORKSPACE = Path(workspace).expanduser().resolve()
    return _DEFAULT_WORKSPACE


def get_default_workspace() -> Path:
    """返回文件工具默认工作区。"""
    return _WORKSPACE_CONTEXT.get() or _DEFAULT_WORKSPACE


def use_workspace(workspace: str | Path) -> Token[Path | None]:
    """Temporarily bind file tools to a workspace for the current context."""
    return _WORKSPACE_CONTEXT.set(Path(workspace).expanduser().resolve())


def reset_workspace(token: Token[Path | None]) -> None:
    """Restore the previous context-local file tools workspace."""
    _WORKSPACE_CONTEXT.reset(token)


# ==============================================================================
# 工具函数
# ==============================================================================

def _safe_resolve_path(
    filepath: str,
    base_dir: Optional[Path] = None
) -> Path:
    """
    安全地解析文件路径，防止目录遍历攻击
    
    Args:
        filepath: 文件路径（相对或绝对）
        base_dir: 基础目录，默认为当前目录
        
    Returns:
        解析后的绝对路径
        
    Raises:
        ValueError: 如果路径不安全
    """
    if base_dir is None:
        base_dir = get_default_workspace()

    return sanitize_path(filepath, base_dir=base_dir)


def _format_file_list(items: List[Path], show_details: bool = True) -> str:
    """
    格式化文件列表输出
    
    Args:
        items: 文件路径列表
        show_details: 是否显示详细信息
        
    Returns:
        格式化的字符串
    """
    if not items:
        return "目录为空"
    
    lines = []
    for item in sorted(items, key=lambda x: (x.is_file(), x.name.lower())):
        prefix = "📁 [目录]" if item.is_dir() else "📄 [文件]"
        name = item.name
        
        if show_details:
            try:
                stat = item.stat()
                size = _format_size(stat.st_size)
                lines.append(f"{prefix} {name} ({size})")
            except OSError:
                # 静默忽略：获取文件状态信息失败，仅回退显示名称
                lines.append(f"{prefix} {name}")
        else:
            lines.append(f"{prefix} {name}")
    
    return "\n".join(lines)


def _format_size(size: int) -> str:
    """格式化文件大小"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _read_with_encoding(filepath: Path, encodings: List[str] = None) -> Optional[str]:
    """
    尝试使用多种编码读取文件
    
    Args:
        filepath: 文件路径
        encodings: 编码列表，按优先级排序
        
    Returns:
        文件内容，失败返回 None
    """
    if encodings is None:
        encodings = ['utf-8', 'gbk', 'gb2312', 'latin-1']
    
    for encoding in encodings:
        try:
            return filepath.read_text(encoding=encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    
    return None


def _validate_glob_pattern(pattern: str) -> Optional[str]:
    """校验 glob pattern，避免通过绝对路径或 .. 逃出工作区。"""
    if not pattern or not str(pattern).strip():
        return "glob 模式不能为空"

    normalized = str(pattern).replace("\\", "/")
    if Path(pattern).is_absolute() or normalized.startswith("/") or re.match(r"^[a-zA-Z]:", normalized):
        return "glob 模式不能使用绝对路径"

    if normalized.startswith("~"):
        return "glob 模式不能使用用户主目录展开"

    if ".." in PurePosixPath(normalized).parts:
        return "glob 模式不能包含 .. 路径段"

    return None


def _safe_relative_match(path: Path, root: Path) -> Optional[Path]:
    """返回位于 root 内的真实路径；不在 root 内则丢弃。"""
    try:
        resolved = path.resolve()
        resolved.relative_to(root.resolve())
        return resolved
    except (OSError, ValueError):
        return None


def _normalize_file_type_filter(file_type: str) -> tuple[Optional[str], Optional[str]]:
    """将文件类型参数规范化为扩展名。"""
    cleaned = str(file_type or "").strip().lstrip("*. ")
    if not cleaned:
        return None, "文件类型不能为空"
    if any(separator in cleaned for separator in ("/", "\\")) or ".." in cleaned:
        return None, "文件类型不能包含路径片段"
    if not re.fullmatch(r"[A-Za-z0-9_+-]+", cleaned):
        return None, "文件类型只能包含字母、数字、下划线、加号或连字符"
    return cleaned, None


# ==============================================================================
# LangChain Tools
# ==============================================================================

@tool
def read_file(path: str) -> str:
    """
    读取指定文件的内容。
    
    参数:
        path: 文件路径（相对路径或绝对路径）
    
    返回:
        文件内容，如果读取失败返回错误信息
    """
    try:
        file_path = _safe_resolve_path(path)
        
        # 安全检查
        is_safe, reason = check_file_danger(str(file_path))
        if not is_safe:
            return f"⚠️ 安全警告: {reason}"
        
        # 检查文件是否存在
        if not file_path.exists():
            return f"❌ 文件不存在: {path}"
        
        # 检查是否是目录
        if file_path.is_dir():
            return f"❌ {path} 是目录，不是文件"
        
        # 读取文件内容
        content = _read_with_encoding(file_path)
        if content is None:
            return f"❌ 无法读取文件 {path}，编码不支持"
        
        # 如果文件太大，返回摘要
        if len(content) > 50000:
            lines = content.split('\n')
            return (
                f"📄 文件: {path}\n"
                f"📊 总行数: {len(lines)}\n"
                f"⚠️ 文件较大，只显示前 100 行:\n\n"
                + '\n'.join(lines[:100])
            )
        
        return f"📄 文件: {path}\n\n{content}"
    
    except ValueError as e:
        return f"⚠️ 安全警告: {str(e)}"
    except Exception as e:
        return f"❌ 读取文件出错: {str(e)}"


@tool
def write_file(path: str, content: str) -> str:
    """
    将内容写入指定文件。如果目录不存在会自动创建。
    
    参数:
        path: 文件路径
        content: 要写入的内容
    
    返回:
        操作结果
    """
    permission_error = enforce_tool_permission(
        "write_file",
        {"path": path, "content_length": len(str(content))},
    )
    if permission_error:
        return permission_error

    try:
        file_path = _safe_resolve_path(path)
        
        # 安全检查 - 写入操作
        is_safe, reason = check_write_operation(str(file_path))
        if not is_safe:
            return f"⚠️ 安全警告: {reason}"
        
        # 检查是否覆盖危险文件
        if file_path.exists():
            is_safe, reason = check_file_danger(str(file_path))
            if not is_safe:
                return f"⚠️ 安全警告: 尝试覆盖受保护文件 - {reason}"
        
        # 确保父目录存在
        file_path.parent.mkdir(parents=True, exist_ok=True)
        
        # 写入文件
        file_path.write_text(content, encoding='utf-8')
        
        # 记录操作
        return f"✅ 成功写入文件: {path}\n📊 写入内容: {len(content)} 字符"
    
    except ValueError as e:
        return f"⚠️ 安全警告: {str(e)}"
    except Exception as e:
        return f"❌ 写入文件出错: {str(e)}"


@tool
def search_replace(
    file_path: str,
    old_content: str,
    new_content: str
) -> str:
    """
    在文件中搜索并替换内容。
    
    参数:
        file_path: 文件路径
        old_content: 要替换的旧内容
        new_content: 替换后的新内容
    
    返回:
        操作结果
    """
    permission_error = enforce_tool_permission(
        "search_replace",
        {
            "file_path": file_path,
            "old_content_length": len(str(old_content)),
            "new_content_length": len(str(new_content)),
        },
    )
    if permission_error:
        return permission_error

    try:
        path = _safe_resolve_path(file_path)
        
        # 安全检查
        is_safe, reason = check_file_danger(str(path))
        if not is_safe:
            return f"⚠️ 安全警告: {reason}"
        
        # 检查文件是否存在
        if not path.exists():
            return f"❌ 文件不存在: {file_path}"
        
        # 读取当前内容
        content = _read_with_encoding(path)
        if content is None:
            return f"❌ 无法读取文件 {file_path}"
        
        # 搜索内容
        if old_content not in content:
            return f"❌ 未找到要替换的内容:\n{old_content[:100]}..."
        
        # 执行替换
        new_file_content = content.replace(old_content, new_content)
        
        # 计算替换次数
        count = content.count(old_content)
        
        # 写入文件
        path.write_text(new_file_content, encoding='utf-8')
        
        return (
            f"✅ 成功替换文件: {file_path}\n"
            f"🔄 替换次数: {count}\n"
            f"📝 旧内容长度: {len(old_content)} 字符\n"
            f"📝 新内容长度: {len(new_content)} 字符"
        )
    
    except ValueError as e:
        return f"⚠️ 安全警告: {str(e)}"
    except Exception as e:
        return f"❌ 替换操作出错: {str(e)}"


@tool
def glob_search(pattern: str, root_dir: str = ".") -> str:
    """
    使用 glob 模式搜索文件。
    
    参数:
        pattern: glob 模式，例如 "*.py", "**/*.txt"
        root_dir: 搜索的根目录，默认为当前目录
    
    返回:
        匹配的文件列表
    """
    try:
        root = _safe_resolve_path(root_dir)
        pattern_error = _validate_glob_pattern(pattern)
        if pattern_error:
            return f"⚠️ 安全警告: {pattern_error}"
        
        # 安全检查
        if not root.exists():
            return f"❌ 目录不存在: {root_dir}"
        
        # 执行 glob 搜索
        safe_matches = []
        seen = set()
        for match in root.glob(pattern):
            safe_match = _safe_relative_match(match, root)
            if safe_match is None or safe_match in seen:
                continue
            seen.add(safe_match)
            safe_matches.append(safe_match)
        matches = safe_matches
        
        if not matches:
            return f"🔍 没有找到匹配 '{pattern}' 的文件"
        
        # 格式化输出
        lines = [f"🔍 找到 {len(matches)} 个匹配 '{pattern}' 的文件:\n"]
        
        for match in matches[:50]:  # 限制显示数量
            rel_path = match.relative_to(root) if match.is_relative_to(root) else match
            lines.append(f"  📄 {rel_path}")
        
        if len(matches) > 50:
            lines.append(f"\n... 还有 {len(matches) - 50} 个文件")
        
        return "\n".join(lines)
    
    except ValueError as e:
        return f"⚠️ 安全警告: {str(e)}"
    except Exception as e:
        return f"❌ glob 搜索出错: {str(e)}"


@tool
def grep_search(
    pattern: str,
    root_dir: str = ".",
    file_type: str = None,
    regex: bool = False,
    case_sensitive: bool = False,
    max_results: int = 50,
) -> str:
    """
    在文件中搜索内容（类似 grep）。
    
    参数:
        pattern: 要搜索的正则表达式或字符串
        root_dir: 搜索的根目录
        file_type: 限制搜索的文件类型，例如 "py", "js"
        regex: 是否将 pattern 作为正则表达式处理
        case_sensitive: 是否区分大小写
        max_results: 最大返回结果数
    
    返回:
        匹配的搜索结果
    """
    try:
        root = _safe_resolve_path(root_dir)
        
        # 安全检查
        if not root.exists():
            return f"❌ 目录不存在: {root_dir}"
        
        # 构建文件类型过滤器
        if file_type:
            normalized_type, type_error = _normalize_file_type_filter(file_type)
            if type_error:
                return f"⚠️ 安全警告: {type_error}"
            patterns = [f"**/*.{normalized_type}"]
        else:
            patterns = [
                "**/*.py", "**/*.js", "**/*.ts", "**/*.tsx", "**/*.jsx",
                "**/*.json", "**/*.toml", "**/*.yaml", "**/*.yml", "**/*.ini",
                "**/*.cfg", "**/*.txt", "**/*.md", "**/*.html", "**/*.css",
                "**/*.scss", "**/*.java", "**/*.go", "**/*.rs", "**/*.c",
                "**/*.cpp", "**/*.h", "**/*.hpp", "**/*.sh", "**/*.ps1",
            ]
        
        # 搜索文件
        matches = []
        for p in patterns:
            pattern_error = _validate_glob_pattern(p)
            if pattern_error:
                return f"⚠️ 安全警告: {pattern_error}"
            matches.extend(root.glob(p))

        unique_matches = []
        seen = set()
        for match in matches:
            safe_match = _safe_relative_match(match, root)
            if safe_match is None or safe_match in seen or not safe_match.is_file():
                continue
            seen.add(safe_match)
            unique_matches.append(safe_match)

        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            compiled = re.compile(pattern if regex else re.escape(pattern), flags)
        except re.error as e:
            return f"❌ 正则表达式无效: {e}"
        
        # 搜索内容
        results = []
        for file_path in unique_matches[:200]:  # 限制搜索文件数
            try:
                content = _read_with_encoding(file_path)
                if content is None:
                    continue
                
                lines = content.split('\n')
                for i, line in enumerate(lines, 1):
                    if compiled.search(line):
                        # 截断过长的行
                        display_line = line if len(line) <= 150 else line[:150] + "..."
                        results.append({
                            'file': str(file_path.relative_to(root)),
                            'line': i,
                            'content': display_line
                        })
                        if len(results) >= max_results:
                            break
                if len(results) >= max_results:
                    break
            except Exception:
                # 静默忽略：读取文件内容失败，跳过该文件
                continue

        if not results:
            return f"🔍 没有找到匹配 '{pattern}' 的内容"
        
        # 格式化输出
        lines = [f"🔍 找到 {len(results)} 处匹配:\n"]
        
        current_file = None
        for result in results[:max_results]:
            if result['file'] != current_file:
                lines.append(f"\n📁 {result['file']}:")
                current_file = result['file']
            
            lines.append(f"  {result['line']:4d}: {result['content']}")
        
        if len(results) >= max_results:
            lines.append(f"\n... 已达到最大返回条数 {max_results}")
        
        return "\n".join(lines)
    
    except ValueError as e:
        return f"⚠️ 安全警告: {str(e)}"
    except Exception as e:
        return f"❌ grep 搜索出错: {str(e)}"


@tool
def create_directory(path: str) -> str:
    """
    创建目录（可以创建多层嵌套目录）。
    
    参数:
        path: 目录路径
    
    返回:
        操作结果
    """
    permission_error = enforce_tool_permission("create_directory", {"path": path})
    if permission_error:
        return permission_error

    try:
        dir_path = _safe_resolve_path(path)
        
        # 安全检查
        is_safe, reason = check_file_danger(str(dir_path))
        if not is_safe:
            return f"⚠️ 安全警告: {reason}"
        
        # 检查是否已存在
        if dir_path.exists():
            return f"⚠️ 目录已存在: {path}"
        
        # 创建目录
        dir_path.mkdir(parents=True, exist_ok=True)
        
        return f"✅ 成功创建目录: {path}"
    
    except ValueError as e:
        return f"⚠️ 安全警告: {str(e)}"
    except Exception as e:
        return f"❌ 创建目录出错: {str(e)}"


@tool
def delete_file(path: str) -> str:
    """
    删除文件或目录。⚠️ 此操作需要谨慎使用。
    
    参数:
        path: 要删除的文件或目录路径
    
    返回:
        操作结果（危险操作会返回特殊标记）
    """
    permission_error = enforce_tool_permission("delete_file", {"path": path})
    if permission_error:
        return permission_error

    try:
        target = _safe_resolve_path(path)
        
        # 安全检查
        is_safe, reason = check_file_danger(str(target))
        if not is_safe:
            return f"⚠️ 🔴 危险操作已阻止: {reason}\n⚠️ 这可能是系统文件或受保护的文件。"
        
        # 检查是否存在
        if not target.exists():
            return f"❌ 文件/目录不存在: {path}"
        
        # 确认删除操作
        if target.is_dir():
            # 对于目录，检查是否为空
            try:
                contents = list(target.iterdir())
                if contents:
                    return (
                        f"⚠️ 目录不为空: {path}\n"
                        f"📊 包含 {len(contents)} 个项目\n"
                        f"⚠️ 使用通配符删除请小心，或先使用 list_directory 查看内容"
                    )
            except OSError:
                # 静默忽略：列出目录内容失败，跳过空目录检查
                pass

        # 执行删除
        if target.is_dir():
            target.rmdir()  # 只删除空目录
        else:
            target.unlink()
        
        return f"✅ 已删除: {path}"
    
    except ValueError as e:
        return f"⚠️ 安全警告: {str(e)}"
    except Exception as e:
        return f"❌ 删除操作出错: {str(e)}"


@tool
def list_directory(path: str = ".") -> str:
    """
    列出目录中的文件和子目录。
    
    参数:
        path: 目录路径，默认为当前目录
    
    返回:
        目录内容列表
    """
    try:
        dir_path = _safe_resolve_path(path)
        
        # 安全检查
        is_safe, reason = check_file_danger(str(dir_path))
        if not is_safe:
            return f"⚠️ 安全警告: {reason}"
        
        # 检查是否存在
        if not dir_path.exists():
            return f"❌ 目录不存在: {path}"
        
        # 检查是否是目录
        if not dir_path.is_dir():
            return f"❌ {path} 不是目录"
        
        # 列出内容
        items = list(dir_path.iterdir())
        
        if not items:
            return f"📁 目录为空: {path}"
        
        # 格式化输出
        lines = [f"📂 目录: {path}\n"]
        lines.append(f"📊 共 {len(items)} 个项目:\n")
        
        # 分类显示
        dirs = [item for item in items if item.is_dir()]
        files = [item for item in items if item.is_file()]
        
        if dirs:
            lines.append("\n📁 子目录:")
            for d in sorted(dirs, key=lambda x: x.name.lower()):
                try:
                    count = len(list(d.iterdir()))
                    lines.append(f"  ├── {d.name}/ ({count} 项)")
                except OSError:
                    # 静默忽略：列出子目录条目数失败，仅回退显示名称
                    lines.append(f"  ├── {d.name}/")

        if files:
            lines.append("\n📄 文件:")
            for f in sorted(files, key=lambda x: x.name.lower())[:20]:
                try:
                    size = _format_size(f.stat().st_size)
                    lines.append(f"  ├── {f.name} ({size})")
                except OSError:
                    # 静默忽略：获取文件大小失败，仅回退显示名称
                    lines.append(f"  ├── {f.name}")
            
            if len(files) > 20:
                lines.append(f"  └── ... 还有 {len(files) - 20} 个文件")
        
        return "\n".join(lines)
    
    except ValueError as e:
        return f"⚠️ 安全警告: {str(e)}"
    except Exception as e:
        return f"❌ 列出目录出错: {str(e)}"


# ==============================================================================
# 批量编辑工具
# ==============================================================================

@tool
def batch_edit(
    edits: List[Dict[str, Any]],
) -> str:
    """
    批量编辑多个文件，支持原子性（全做或全不做）。
    先验证所有操作，再统一执行，失败时自动回滚。

    支持的操作类型:
      - write: 完全覆盖文件内容
      - replace: 搜索并替换文件中的特定内容

    参数:
        edits: 编辑操作列表，每个操作是一个字典:
            - path (str): 必填，文件路径
            - operation (str): 必填，"write" 或 "replace"
            - content (str): write 操作必填，新文件内容
            - old_content (str): replace 操作必填，被替换的旧内容
            - new_content (str): replace 操作必填，替换后的新内容

    返回:
        操作的统一结果，包含每文件的 diff 预览

    示例:
        batch_edit(edits=[
            {"path": "src/main.py", "operation": "replace",
             "old_content": "old_func()", "new_content": "new_func()"},
            {"path": "src/utils.py", "operation": "write",
             "content": "# new file content"},
        ])
    """
    if not edits:
        return "⚠️ 未提供任何编辑操作"

    permission_error = enforce_tool_permission(
        "batch_edit",
        {
            "path": [
                edit.get("path", "")
                for edit in edits
                if isinstance(edit, dict)
            ],
            "edit_count": len(edits),
        },
    )
    if permission_error:
        return permission_error

    # ---------- 第一阶段：验证所有操作 ----------
    validated = []  # (path, action_description, old_content, new_content)
    errors = []
    backups = {}  # path -> original_content for rollback

    for idx, edit in enumerate(edits):
        if not isinstance(edit, dict):
            errors.append(f"[{idx}] 编辑项必须是字典")
            continue

        path_str = edit.get("path", "")
        operation = edit.get("operation", "")

        if not path_str or not operation:
            errors.append(f"[{idx}] 缺少 path 或 operation")
            continue

        if operation not in ("write", "replace"):
            errors.append(f"[{idx}] 不支持的操作类型: {operation}，仅支持 write/replace")
            continue

        try:
            file_path = _safe_resolve_path(path_str)
        except ValueError as e:
            errors.append(f"[{idx}] {path_str}: 路径无效 - {e}")
            continue

        # 检查文件存在性（write 可以创建新文件）
        if operation == "replace" and not file_path.exists():
            errors.append(f"[{idx}] {path_str}: 文件不存在，replace 操作需要目标文件")
            continue

        try:
            # 安全检查
            if operation == "write":
                is_safe, reason = check_write_operation(str(file_path))
                if not is_safe:
                    errors.append(f"[{idx}] {path_str}: 写入安全检查失败 - {reason}")
                    continue
            else:
                is_safe, reason = check_file_danger(str(file_path))
                if not is_safe:
                    errors.append(f"[{idx}] {path_str}: 文件安全检查失败 - {reason}")
                    continue
        except Exception as e:
            errors.append(f"[{idx}] {path_str}: 安全检查异常 - {e}")
            continue

        # 读取原内容（备份用 + replace 验证）
        original_content = None
        if file_path.exists():
            try:
                original_content = file_path.read_text(encoding="utf-8")
            except Exception as e:
                errors.append(f"[{idx}] {path_str}: 读取原文件失败 - {e}")
                continue

        if operation == "replace":
            old_content = edit.get("old_content", "")
            new_content = edit.get("new_content", "")
            if not old_content:
                errors.append(f"[{idx}] {path_str}: replace 操作缺少 old_content")
                continue

            if original_content and old_content not in original_content:
                # 尝试精确位置定位
                error_msg = f"[{idx}] {path_str}: 未找到匹配的 old_content"
                errors.append(error_msg)
                continue

            validated.append((file_path, operation, original_content, old_content, new_content))
        else:  # write
            content = edit.get("content", "")
            validated.append((file_path, operation, original_content, content, None))

        # 创建备份（只对已有文件）
        if file_path.exists():
            backups[str(file_path)] = original_content

    if errors:
        return (
            "❌ 批量编辑验证失败，未执行任何更改:\n"
            + "\n".join(f"  {e}" for e in errors)
        )

    # ---------- 第二阶段：执行所有操作 ----------
    results = []
    all_diff_lines = []

    try:
        for file_path, operation, original_content, arg_a, arg_b in validated:
            old_content_for_diff = original_content or ""

            if operation == "write":
                new_content = arg_a
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text(new_content, encoding="utf-8")
                action = "写入" if not original_content else "覆盖"
                results.append(f"  ✅ {file_path.relative_to(get_default_workspace())}: {action} ({len(new_content)} 字符)")
            else:  # replace
                old_content = arg_a
                new_content = arg_b
                file_content = file_path.read_text(encoding="utf-8")
                # 确保 old_content 唯一
                count = file_content.count(old_content)
                if count == 0:
                    raise RuntimeError("old_content 在文件中不存在（并发修改）")
                if count > 1:
                    raise RuntimeError(f"old_content 在文件中出现 {count} 次，不唯一")
                updated = file_content.replace(old_content, new_content, 1)
                file_path.write_text(updated, encoding="utf-8")
                results.append(f"  ✅ {file_path.relative_to(get_default_workspace())}: 替换 (旧: {len(old_content)} 字符 → 新: {len(new_content)} 字符)")

            # 生成 diff
            old_lines = old_content_for_diff.splitlines(keepends=True)
            new_lines = (new_content if operation == "write" else
                         (original_content or "").replace(old_content, new_content, 1)).splitlines(keepends=True)
            diff = list(difflib.unified_diff(
                old_lines, new_lines,
                fromfile=str(file_path),
                tofile=str(file_path),
                lineterm="",
            ))
            if diff:
                all_diff_lines.extend(diff)

    except Exception as e:
        # ---------- 回滚：恢复旧文件 + 删除新创建的文件 ----------
        rollback_msgs = []
        for rollback_path_str, rollback_content in backups.items():
            if rollback_content is not None:
                try:
                    Path(rollback_path_str).write_text(rollback_content, encoding="utf-8")
                    rollback_msgs.append(f"  ↩️ {Path(rollback_path_str).relative_to(get_default_workspace())}: 已回滚")
                except Exception as rollback_e:
                    rollback_msgs.append(f"  ❌ {rollback_path_str}: 回滚失败 - {rollback_e}")
        # 清理 batch_edit 中 write 操作创建的新文件（不在 backups 中的文件）
        for file_path, operation, _orig, _a, _b in validated:
            if operation == "write" and str(file_path) not in backups:
                try:
                    file_path.unlink(missing_ok=True)
                    rollback_msgs.append(f"  🗑️ {file_path.relative_to(get_default_workspace())}: 已清理新建文件")
                except Exception:
                    pass

        return (
            f"❌ 批量编辑执行失败，已回滚 {len(backups)} 个文件:\n"
            f"  错误: {e}\n"
            + "\n".join(rollback_msgs)
        )

    # ---------- 输出结果 ----------
    lines = []
    lines.append(f"📦 批量编辑完成: {len(validated)} 个操作")
    lines.append("")
    lines.extend(results)

    if all_diff_lines:
        lines.append("")
        lines.append("📋 变更预览 (unified diff):")
        lines.append("")
        # 限制 diff 输出长度
        diff_text = "\n".join(all_diff_lines)
        if len(diff_text) > 3000:
            diff_text = diff_text[:3000] + "\n... (diff 过长已截断)"
        lines.append(diff_text)

    return "\n".join(lines)


# ==============================================================================
# 导出
# ==============================================================================

__all__ = [
    'set_default_workspace',
    'get_default_workspace',
    'read_file',
    'batch_edit',
    'write_file',
    'search_replace',
    'glob_search',
    'grep_search',
    'create_directory',
    'delete_file',
    'list_directory',
]
