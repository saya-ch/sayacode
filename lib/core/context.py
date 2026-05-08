"""
项目上下文模块

维护项目的结构信息、摘要和修改历史，用于为 LLM 提供项目级别的上下文。
"""

from fnmatch import fnmatch
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import json
import os

from .private_io import write_private_json
from ..i18n import tr


@dataclass
class FileInfo:
    """文件信息"""
    path: str
    name: str
    file_type: str  # py, js, json, md, txt, etc.
    size: int  # 字节
    modified_time: str
    line_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "name": self.name,
            "type": self.file_type,
            "size": self.size,
            "modified": self.modified_time,
            "lines": self.line_count
        }


@dataclass
class ChangeRecord:
    """修改记录"""
    timestamp: str
    action: str  # created, modified, deleted
    file_path: str
    description: str = ""
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "action": self.action,
            "file": self.file_path,
            "description": self.description,
            "details": self.details
        }


class ProjectContext:
    """
    项目上下文管理器

    维护项目的整体信息，包括：
    - 项目结构和文件列表
    - 依赖和配置信息
    - 修改历史
    - 代码统计信息
    """

    def __init__(self, root_dir: str):
        """
        初始化项目上下文

        Args:
            root_dir: 项目根目录
        """
        self.root_dir = Path(root_dir)
        self.name = self.root_dir.name

        # 项目信息
        self.language: Optional[str] = None
        self.dependencies: Dict[str, str] = {}
        self.project_type: Optional[str] = None

        # 文件结构
        self.files: List[FileInfo] = []
        self.excluded_patterns: List[str] = [
            "__pycache__",
            "*.pyc",
            ".git",
            ".venv",
            ".pytest_cache",
            "node_modules",
            ".idea",
            ".env",
            ".env.*",
            ".ssh",
            "*.pem",
            "*.key",
            "*.p12",
            "*.pfx",
            ".npmrc",
            ".pypirc",
            "AppData",
        ]

        # 修改历史
        self.change_history: List[ChangeRecord] = []

        # 初始扫描
        self.scan()

    def scan(self):
        """扫描项目结构"""
        self.files = []

        if not self.root_dir.exists():
            return

        try:
            shallow_scan = self.root_dir.resolve() == Path.home().resolve()
        except Exception:
            # 静默忽略：无法解析路径，默认为非 home 目录扫描
            shallow_scan = False

        max_depth = 3 if shallow_scan else None

        # 使用 os.walk 避免在遇到不可访问目录时中断整个扫描。
        def _ignore_walk_error(_: OSError) -> None:
            return None

        for current_root, dirnames, filenames in os.walk(
            self.root_dir,
            topdown=True,
            onerror=_ignore_walk_error,
        ):
            current_path = Path(current_root)
            try:
                relative_path = current_path.relative_to(self.root_dir)
                depth = 0 if str(relative_path) == "." else len(relative_path.parts)
            except ValueError:
                depth = 0

            dirnames[:] = [
                dirname
                for dirname in dirnames
                if not self._should_exclude(current_path / dirname)
            ]

            if max_depth is not None and depth >= max_depth:
                dirnames[:] = []

            for filename in filenames:
                file_path = current_path / filename
                if self._should_exclude(file_path):
                    continue
                self._add_file(file_path)

        # 检测项目类型和语言
        self._detect_project_type()

    def _should_exclude(self, path: Path) -> bool:
        """检查是否应该排除该路径"""
        try:
            match_path = path.relative_to(self.root_dir)
        except ValueError:
            match_path = path

        for pattern in self.excluded_patterns:
            if _matches_excluded_pattern(match_path, pattern):
                return True
        return False

    def _add_file(self, file_path: Path):
        """添加文件信息"""
        try:
            stat = file_path.stat()
            modified_time = datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')

            # 计算行数
            line_count = 0
            if file_path.suffix in ['.py', '.js', '.ts', '.java', '.cpp', '.c', '.h']:
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        line_count = sum(1 for _ in f)
                except Exception:
                    # 静默忽略：无法读取文件行数，保持行数为 0
                    pass

            file_info = FileInfo(
                path=str(file_path.relative_to(self.root_dir)),
                name=file_path.name,
                file_type=file_path.suffix.lstrip('.'),
                size=stat.st_size,
                modified_time=modified_time,
                line_count=line_count
            )

            self.files.append(file_info)

        except Exception as e:
            print(tr("core.scan_file_failed", path=file_path, error=str(e)))

    def _detect_project_type(self):
        """检测项目类型和语言"""
        # Python 项目
        if (self.root_dir / "requirements.txt").exists() or \
           (self.root_dir / "setup.py").exists() or \
           (self.root_dir / "pyproject.toml").exists():
            self.project_type = "python"
            self.language = "Python"
            self._load_python_dependencies()

        # JavaScript/TypeScript 项目
        elif (self.root_dir / "package.json").exists():
            self.project_type = "javascript"
            self.language = "JavaScript"
            self._load_js_dependencies()

        # Java 项目
        elif (self.root_dir / "pom.xml").exists() or \
             (self.root_dir / "build.gradle").exists():
            self.project_type = "java"
            self.language = "Java"

        # 其他类型
        else:
            # 根据文件扩展名猜测
            extensions = {}
            for file in self.files:
                ext = file.file_type
                extensions[ext] = extensions.get(ext, 0) + 1

            if extensions:
                most_common = max(extensions.items(), key=lambda x: x[1])
                self.language = most_common[0].upper()
                self.project_type = "generic"

    def _load_python_dependencies(self):
        """加载 Python 依赖"""
        # 尝试读取 requirements.txt
        req_file = self.root_dir / "requirements.txt"
        if req_file.exists():
            try:
                with open(req_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#'):
                            if '==' in line:
                                pkg, version = line.split('==', 1)
                                self.dependencies[pkg.strip()] = version.strip()
                            else:
                                self.dependencies[line] = "latest"
            except Exception as e:
                print(tr("core.parse_failed", error=str(e)))

    def _load_js_dependencies(self):
        """加载 JavaScript 依赖"""
        pkg_file = self.root_dir / "package.json"
        if pkg_file.exists():
            try:
                with open(pkg_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    deps = data.get('dependencies', {})
                    dev_deps = data.get('devDependencies', {})
                    self.dependencies = {**deps, **dev_deps}
            except Exception as e:
                print(tr("core.parse_failed", error=str(e)))

    def track_change(
        self,
        action: str,
        file_path: str,
        description: str = "",
        details: Optional[Dict[str, Any]] = None
    ):
        """
        记录修改历史

        Args:
            action: 操作类型（created/modified/deleted）
            file_path: 文件路径
            description: 描述
            details: 详细信息
        """
        record = ChangeRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            action=action,
            file_path=file_path,
            description=description,
            details=details or {}
        )
        self.change_history.append(record)

    def get_context_for_llm(
        self,
        max_files: int = 20,
        include_changes: bool = True,
        include_dependencies: bool = True
    ) -> str:
        """
        生成供 LLM 使用的上下文信息

        Args:
            max_files: 最大显示文件数
            include_changes: 是否包含修改历史
            include_dependencies: 是否包含依赖信息

        Returns:
            格式化的上下文文本
        """
        lines = []

        # 项目标题
        lines.append(f"# 项目: {self.name}")
        lines.append("")

        # 基本信息
        lines.append("## 项目信息")
        if self.language:
            lines.append(f"- 语言: {self.language}")
        if self.project_type:
            lines.append(f"- 类型: {self.project_type}")
        lines.append(f"- 根目录: {self.root_dir}")
        lines.append(f"- 总文件数: {len(self.files)}")
        lines.append("")

        # 依赖信息
        if include_dependencies and self.dependencies:
            lines.append("## 依赖")
            for pkg, version in list(self.dependencies.items())[:10]:
                lines.append(f"- {pkg}: {version}")
            if len(self.dependencies) > 10:
                lines.append(f"- ... 还有 {len(self.dependencies) - 10} 个依赖")
            lines.append("")

        # 文件结构
        lines.append("## 文件结构")
        lines.append(f"共 {len(self.files)} 个文件，显示前 {max_files} 个：")
        lines.append("")

        # 按类型分组显示
        by_type = {}
        for file in sorted(self.files, key=lambda x: x.path):
            file_type = file.file_type or "other"
            if file_type not in by_type:
                by_type[file_type] = []
            by_type[file_type].append(file)

        for file_type, files in sorted(by_type.items()):
            if len(files) > 5:
                lines.append(f"### {file_type.upper()} 文件 ({len(files)} 个)")
                for f in files[:5]:
                    lines.append(f"- {f.path} ({f.line_count} 行)")
                lines.append(f"- ... 还有 {len(files) - 5} 个 {file_type} 文件")
            else:
                lines.append(f"### {file_type.upper()} 文件")
                for f in files:
                    lines.append(f"- {f.path} ({f.line_count} 行)")
            lines.append("")

        # 修改历史
        if include_changes and self.change_history:
            lines.append("## 最近修改")
            for record in self.change_history[-10:]:
                lines.append(f"- [{record.timestamp}] {record.action}: {record.file_path}")
                if record.description:
                    lines.append(f"  {record.description}")
            lines.append("")

        return "\n".join(lines)

    def get_summary(self) -> str:
        """
        获取项目摘要

        Returns:
            简短的项目摘要
        """
        summary = f"项目: {self.name}"

        if self.language:
            summary += f", 语言: {self.language}"

        summary += f", {len(self.files)} 个文件"

        if self.dependencies:
            summary += f", {len(self.dependencies)} 个依赖"

        return summary

    def get_file_by_path(self, relative_path: str) -> Optional[FileInfo]:
        """
        根据路径获取文件信息

        Args:
            relative_path: 相对于项目根目录的路径

        Returns:
            文件信息，如果未找到返回 None
        """
        for file in self.files:
            if file.path == relative_path:
                return file
        return None

    def get_statistics(self) -> Dict[str, Any]:
        """
        获取项目统计信息

        Returns:
            统计信息字典
        """
        # 按类型统计
        by_type = {}
        total_lines = 0
        total_size = 0

        for file in self.files:
            file_type = file.file_type or "other"
            if file_type not in by_type:
                by_type[file_type] = {"count": 0, "lines": 0, "size": 0}
            by_type[file_type]["count"] += 1
            by_type[file_type]["lines"] += file.line_count
            by_type[file_type]["size"] += file.size
            total_lines += file.line_count
            total_size += file.size

        return {
            "total_files": len(self.files),
            "total_lines": total_lines,
            "total_size": total_size,
            "by_type": by_type,
            "total_dependencies": len(self.dependencies)
        }

    def save_context(self, file_path: str) -> bool:
        """
        保存项目上下文到文件

        Args:
            file_path: 文件路径

        Returns:
            是否保存成功
        """
        try:
            data = {
                "name": self.name,
                "root_dir": str(self.root_dir),
                "language": self.language,
                "project_type": self.project_type,
                "dependencies": self.dependencies,
                "files": [f.to_dict() for f in self.files],
                "changes": [c.to_dict() for c in self.change_history]
            }

            write_private_json(file_path, data)

            return True
        except Exception as e:
            print(tr("core.context_save_failed", error=str(e)))
            return False

    def __repr__(self) -> str:
        return f"ProjectContext(name={self.name}, files={len(self.files)}, changes={len(self.change_history)})"


def _matches_excluded_pattern(path: Path, pattern: str) -> bool:
    """按路径段或 glob 规则匹配排除模式。"""
    normalized_pattern = pattern.lower()

    if any(char in pattern for char in "*?[]"):
        for candidate in (path, *path.parents):
            candidate_name = candidate.name.lower()
            candidate_path = candidate.as_posix().lower()
            if fnmatch(candidate_name, normalized_pattern) or fnmatch(candidate_path, normalized_pattern):
                return True
        return False

    return normalized_pattern in {part.lower() for part in path.parts}
