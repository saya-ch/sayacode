"""
项目分析工具

提供项目结构分析和摘要功能，帮助 LLM 理解项目的整体情况。

分析内容：
- 项目类型（Python/JavaScript/Java/Go 等）
- 框架（Django/React/FastAPI 等）
- 依赖文件（requirements.txt, package.json, go.mod 等）
- 项目结构
- 代码统计
"""

from fnmatch import fnmatch
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from langchain_core.tools import tool
import json

# 导入上下文管理
from ..core.context import ProjectContext
from ..core.symbols import SymbolIndex, render_symbols
from ..i18n import tr
from .safety import sanitize_path


_DEFAULT_WORKSPACE = Path.cwd().resolve()
_WORKSPACE_CONTEXT: ContextVar[Path | None] = ContextVar("sayacode_project_tools_workspace", default=None)


def set_default_workspace(workspace: str | Path) -> Path:
    """设置项目分析工具默认工作区。"""
    global _DEFAULT_WORKSPACE
    _DEFAULT_WORKSPACE = Path(workspace).expanduser().resolve()
    return _DEFAULT_WORKSPACE


def get_default_workspace() -> Path:
    """返回项目分析工具默认工作区。"""
    return _WORKSPACE_CONTEXT.get() or _DEFAULT_WORKSPACE


def use_workspace(workspace: str | Path) -> Token[Path | None]:
    """Temporarily bind project tools to a workspace for the current context."""
    return _WORKSPACE_CONTEXT.set(Path(workspace).expanduser().resolve())


def reset_workspace(token: Token[Path | None]) -> None:
    """Restore the previous context-local project tools workspace."""
    _WORKSPACE_CONTEXT.reset(token)


def _safe_resolve_root(root_dir: str | Path = ".") -> Path:
    """将项目分析目标限制在默认工作区内。"""
    return sanitize_path(str(root_dir), base_dir=get_default_workspace())


# ==============================================================================
# 项目类型检测
# ==============================================================================

class ProjectAnalyzer:
    """
    项目分析器
    
    分析项目结构、类型、依赖等信息，生成供 LLM 理解的摘要。
    """
    
    # 语言和框架映射
    LANGUAGE_PATTERNS = {
        'python': {
            'files': ['.py'],
            'config': ['requirements.txt', 'setup.py', 'pyproject.toml', 'Pipfile', 'poetry.lock'],
            'framework_indicators': {
                'Django': ['django', 'django.conf', 'django.db'],
                'Flask': ['flask', 'from flask import'],
                'FastAPI': ['fastapi', 'from fastapi import'],
                'Pyramid': ['pyramid', 'from pyramid import'],
                'Tornado': ['tornado', 'from tornado import'],
                'Sanic': ['sanic', 'from sanic import'],
                'Bottle': ['bottle', 'from bottle import'],
                'Celery': ['celery', 'from celery import'],
            }
        },
        'javascript': {
            'files': ['.js', '.mjs', '.cjs'],
            'config': ['package.json', 'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml'],
            'framework_indicators': {
                'React': ['react', 'React.Component', 'useState', 'useEffect'],
                'Vue': ['vue', 'Vue.createApp', 'setup()'],
                'Angular': ['@angular/core', 'ngModule', '@Component'],
                'Next.js': ['next', 'next.config', 'getServerSideProps'],
                'Express': ['express', 'app.use', 'app.get', 'app.post'],
                'NestJS': ['@nestjs', 'NestFactory'],
                'Svelte': ['svelte', '<script>', 'export let'],
            }
        },
        'typescript': {
            'files': ['.ts', '.tsx'],
            'config': ['tsconfig.json', 'package.json'],
            'framework_indicators': {
                'React-TS': ['react', '@types/react'],
                'Vue-TS': ['vue', 'defineComponent'],
                'Angular-TS': ['@angular/core'],
                'NestJS': ['@nestjs', 'NestFactory'],
            }
        },
        'java': {
            'files': ['.java'],
            'config': ['pom.xml', 'build.gradle', 'gradlew', 'mvnw'],
            'framework_indicators': {
                'Spring': ['org.springframework', '@SpringBootApplication'],
                'Maven': ['<dependency>', '<groupId>'],
            }
        },
        'go': {
            'files': ['.go'],
            'config': ['go.mod', 'go.sum'],
            'framework_indicators': {
                'Gin': ['github.com/gin-gonic/gin'],
                'Echo': ['github.com/labstack/echo'],
                'Fiber': ['github.com/gofiber/fiber'],
                'Beego': ['github.com/beego/beego'],
            }
        },
        'rust': {
            'files': ['.rs'],
            'config': ['Cargo.toml', 'Cargo.lock'],
            'framework_indicators': {
                'Actix': ['actix', 'actix-web'],
                'Rocket': ['rocket', '#[rocket::'],
                'Tokio': ['tokio', '#[tokio::'],
            }
        },
        'cpp': {
            'files': ['.cpp', '.hpp', '.h', '.cc'],
            'config': ['CMakeLists.txt', 'Makefile', '.vcxproj'],
            'framework_indicators': {
                'Qt': ['Q_OBJECT', '#include <Q'],
                'Boost': ['#include <boost'],
            }
        },
    }
    
    # 忽略的目录和文件
    IGNORED_PATTERNS = [
        '__pycache__',
        '.git',
        '.svn',
        'node_modules',
        '.venv',
        'venv',
        'env',
        '.env',
        '.idea',
        '.vscode',
        'dist',
        'build',
        'target',
        'bin',
        'obj',
        '.cache',
        '.pytest_cache',
        '.tox',
        '.mypy_cache',
        'coverage',
        '.coverage',
    ]
    
    def __init__(self, root_dir: str):
        """
        初始化项目分析器
        
        Args:
            root_dir: 项目根目录
        """
        self.root_dir = _safe_resolve_root(root_dir)
        self.name = self.root_dir.name
        self.context = ProjectContext(str(self.root_dir))
        
        # 分析结果
        self.language: Optional[str] = None
        self.project_type: Optional[str] = None
        self.frameworks: List[str] = []
        self.dependencies: Dict[str, str] = {}
        self.structure: Dict[str, List[str]] = {}
        self.stats: Dict[str, any] = {}
        self.config_files: List[str] = []
        
        # 执行分析
        self.analyze()
    
    def analyze(self):
        """执行完整的项目分析"""
        self._detect_language_and_type()
        self._load_dependencies()
        self._build_structure()
        self._collect_stats()

    def _iter_context_paths(self, suffixes: Optional[Tuple[str, ...]] = None) -> List[Path]:
        """返回上下文中匹配后缀的文件绝对路径列表。"""
        results: List[Path] = []
        for file_info in self.context.files:
            if suffixes and Path(file_info.path).suffix.lower() not in suffixes:
                continue
            results.append(self.root_dir / file_info.path)
        return results
    
    def _detect_language_and_type(self):
        """检测项目语言和类型"""
        config_files: List[str] = []
        file_counts: Dict[str, int] = {}

        tracked_configs = {
            'requirements.txt', 'setup.py', 'pyproject.toml',
            'package.json', 'tsconfig.json', 'pom.xml',
            'build.gradle', 'go.mod', 'Cargo.toml',
        }
        for file_info in self.context.files:
            if file_info.name in tracked_configs:
                config_files.append(file_info.path)
            ext = Path(file_info.path).suffix.lower()
            if ext:
                file_counts[ext] = file_counts.get(ext, 0) + 1
        
        self.config_files = config_files
        
        # 根据配置文件确定语言
        if any('requirements.txt' in f or 'setup.py' in f or 'pyproject.toml' in f for f in config_files):
            self.language = 'Python'
            self.project_type = 'python'
            self._detect_python_framework()
        
        elif any('package.json' in f for f in config_files):
            if any('tsconfig.json' in f for f in config_files):
                self.language = 'TypeScript'
                self.project_type = 'typescript'
                self._detect_js_framework()
            else:
                self.language = 'JavaScript'
                self.project_type = 'javascript'
                self._detect_js_framework()
        
        elif any('pom.xml' in f or 'build.gradle' in f for f in config_files):
            self.language = 'Java'
            self.project_type = 'java'
        
        elif any('go.mod' in f for f in config_files):
            self.language = 'Go'
            self.project_type = 'go'
        
        elif any('Cargo.toml' in f for f in config_files):
            self.language = 'Rust'
            self.project_type = 'rust'
        
        elif any('CMakeLists.txt' in f or '.vcxproj' in f for f in config_files):
            self.language = 'C++'
            self.project_type = 'cpp'
        
        else:
            # 根据文件扩展名猜测
            self.language = self._guess_language_from_ext(file_counts)
            self.project_type = 'generic'

        if self.context.project_type and self.project_type == 'generic':
            self.project_type = self.context.project_type
        if self.context.language and self.language == 'Unknown':
            self.language = self.context.language
    
    def _detect_python_framework(self):
        """检测 Python 框架"""
        for file_path in self._iter_context_paths(('.py',)):
            try:
                content = file_path.read_text(encoding='utf-8', errors='ignore')
                for framework, indicators in self.LANGUAGE_PATTERNS['python']['framework_indicators'].items():
                    for indicator in indicators:
                        if indicator in content:
                            if framework not in self.frameworks:
                                self.frameworks.append(framework)
            except Exception:
                # 静默忽略：读取文件进行 Python 框架检测失败，跳过该文件
                continue

    def _detect_js_framework(self):
        """检测 JavaScript/TypeScript 框架"""
        for file_path in self._iter_context_paths(('.json', '.js', '.ts', '.tsx', '.jsx')):
            try:
                content = file_path.read_text(encoding='utf-8', errors='ignore')
                for framework, indicators in self.LANGUAGE_PATTERNS['javascript']['framework_indicators'].items():
                    for indicator in indicators:
                        if indicator in content and framework not in self.frameworks:
                            self.frameworks.append(framework)
            except Exception:
                # 静默忽略：读取文件进行 JS 框架检测失败，跳过该文件
                continue
    
    def _guess_language_from_ext(self, file_counts: Dict[str, int]) -> str:
        """根据文件扩展名猜测语言"""
        ext_mapping = {
            '.py': 'Python',
            '.js': 'JavaScript',
            '.ts': 'TypeScript',
            '.java': 'Java',
            '.go': 'Go',
            '.rs': 'Rust',
            '.cpp': 'C++',
            '.c': 'C',
            '.rb': 'Ruby',
            '.php': 'PHP',
            '.swift': 'Swift',
            '.kt': 'Kotlin',
        }
        
        max_count = 0
        guessed_lang = 'Unknown'
        
        for ext, count in file_counts.items():
            if ext in ext_mapping and count > max_count:
                max_count = count
                guessed_lang = ext_mapping[ext]
        
        return guessed_lang
    
    def _load_dependencies(self):
        """加载项目依赖"""
        self.dependencies = dict(self.context.dependencies)

        # Python
        req_file = self.root_dir / 'requirements.txt'
        if req_file.exists() and not self.dependencies:
            self._parse_python_requirements(req_file)
        
        # JavaScript
        pkg_file = self.root_dir / 'package.json'
        if pkg_file.exists() and not self.dependencies:
            self._parse_package_json(pkg_file)
        
        # Go
        go_mod = self.root_dir / 'go.mod'
        if go_mod.exists():
            self._parse_go_mod(go_mod)
    
    def _parse_python_requirements(self, req_file: Path):
        """解析 requirements.txt"""
        try:
            with open(req_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        if '==' in line:
                            pkg, version = line.split('==', 1)
                            self.dependencies[pkg.strip()] = version.strip()
                        elif '>=' in line:
                            pkg, version = line.split('>=', 1)
                            self.dependencies[pkg.strip()] = f'>={version.strip()}'
                        else:
                            self.dependencies[line] = 'any'
        except Exception as e:
            print(tr("core.parse_failed", error=str(e)))

    def _parse_package_json(self, pkg_file: Path):
        """解析 package.json"""
        try:
            with open(pkg_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                deps = data.get('dependencies', {})
                dev_deps = data.get('devDependencies', {})

                for pkg, version in {**deps, **dev_deps}.items():
                    self.dependencies[pkg] = version
        except Exception as e:
            print(tr("core.parse_failed", error=str(e)))

    def _parse_go_mod(self, go_mod: Path):
        """解析 go.mod"""
        try:
            in_require = False
            with open(go_mod, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line == 'require (':
                        in_require = True
                        continue
                    elif line == ')':
                        in_require = False
                    elif line.startswith('require '):
                        parts = line.split()[1:]
                        if len(parts) >= 2:
                            self.dependencies[parts[0]] = parts[1]
                    elif in_require:
                        parts = line.split()
                        if parts:
                            self.dependencies[parts[0]] = parts[1] if len(parts) > 1 else 'any'
        except Exception as e:
            print(tr("core.parse_failed", error=str(e)))
    
    def _build_structure(self):
        """构建项目结构"""
        self.structure = {
            'source_dirs': [],
            'test_dirs': [],
            'config_dirs': [],
            'docs_dirs': [],
            'other_dirs': []
        }
        
        for item in self.root_dir.iterdir():
            if self._should_ignore(item):
                continue
            
            if item.is_dir():
                name_lower = item.name.lower()
                
                if any(keyword in name_lower for keyword in ['src', 'lib', 'app']):
                    self.structure['source_dirs'].append(item.name)
                elif any(keyword in name_lower for keyword in ['test', 'spec']):
                    self.structure['test_dirs'].append(item.name)
                elif any(keyword in name_lower for keyword in ['config', '.']):
                    self.structure['config_dirs'].append(item.name)
                elif any(keyword in name_lower for keyword in ['doc', 'docs']):
                    self.structure['docs_dirs'].append(item.name)
                else:
                    self.structure['other_dirs'].append(item.name)
    
    def _collect_stats(self):
        """收集项目统计信息"""
        total_files = len(self.context.files)
        total_lines = 0
        by_extension: Dict[str, Dict] = {}

        for file_info in self.context.files:
            ext = Path(file_info.path).suffix.lower() or 'no_ext'
            if ext not in by_extension:
                by_extension[ext] = {'count': 0, 'lines': 0, 'size': 0}

            by_extension[ext]['count'] += 1
            by_extension[ext]['lines'] += file_info.line_count
            by_extension[ext]['size'] += file_info.size
            total_lines += file_info.line_count
        
        self.stats = {
            'total_files': total_files,
            'total_lines': total_lines,
            'by_extension': by_extension
        }
    
    def _should_ignore(self, path: Path) -> bool:
        """检查是否应该忽略"""
        for pattern in self.IGNORED_PATTERNS:
            if _matches_path_pattern(path, pattern):
                return True
        return False
    
    def get_summary(self) -> str:
        """
        获取项目摘要（供 LLM 使用）
        
        Returns:
            格式化的项目摘要
        """
        lines = []
        
        # 项目标题
        lines.append(f"# 项目: {self.name}")
        lines.append("")
        
        # 基本信息
        lines.append("## 基本信息")
        lines.append(f"- 语言: {self.language}")
        lines.append(f"- 类型: {self.project_type}")
        if self.frameworks:
            lines.append(f"- 框架: {', '.join(self.frameworks)}")
        lines.append(f"- 根目录: {self.root_dir}")
        lines.append("")
        
        # 统计信息
        lines.append("## 统计信息")
        lines.append(f"- 总文件数: {self.stats.get('total_files', 0)}")
        lines.append(f"- 总代码行数: {self.stats.get('total_lines', 0)}")
        
        by_ext = self.stats.get('by_extension', {})
        if by_ext:
            lines.append("\n### 文件类型分布:")
            for ext, info in sorted(by_ext.items(), key=lambda x: x[1]['count'], reverse=True)[:10]:
                lines.append(f"- {ext}: {info['count']} 个文件, {info['lines']} 行")
        lines.append("")
        
        # 依赖信息
        if self.dependencies:
            lines.append(f"## 依赖 ({len(self.dependencies)} 个)")
            for pkg, version in list(self.dependencies.items())[:15]:
                lines.append(f"- {pkg}: {version}")
            if len(self.dependencies) > 15:
                lines.append(f"- ... 还有 {len(self.dependencies) - 15} 个依赖")
            lines.append("")
        
        # 项目结构
        lines.append("## 项目结构")
        if self.structure['source_dirs']:
            lines.append(f"- 源代码目录: {', '.join(self.structure['source_dirs'])}")
        if self.structure['test_dirs']:
            lines.append(f"- 测试目录: {', '.join(self.structure['test_dirs'])}")
        if self.structure['config_dirs']:
            lines.append(f"- 配置目录: {', '.join(self.structure['config_dirs'])}")
        if self.structure['other_dirs']:
            lines.append(f"- 其他目录: {', '.join(self.structure['other_dirs'])}")
        lines.append("")
        
        return "\n".join(lines)


# ==============================================================================
# LangChain Tools
# ==============================================================================

@tool
def analyze_project(root_dir: str = ".") -> str:
    """
    分析项目结构和依赖。
    
    参数:
        root_dir: 项目根目录（默认为当前目录）
    
    返回:
        项目分析报告
    """
    try:
        analyzer = ProjectAnalyzer(root_dir)
        return analyzer.get_summary()
    except ValueError as e:
        return f"⚠️ 安全警告: {str(e)}"
    except Exception as e:
        return f"❌ 分析项目失败: {str(e)}"


@tool
def get_project_summary(root_dir: str = ".") -> str:
    """
    获取项目摘要（简洁版本）。
    
    参数:
        root_dir: 项目根目录（默认为当前目录）
    
    返回:
        项目摘要
    """
    try:
        analyzer = ProjectAnalyzer(root_dir)
        
        parts = [
            f"项目: {analyzer.name}",
            f"语言: {analyzer.language}",
        ]
        
        if analyzer.frameworks:
            parts.append(f"框架: {', '.join(analyzer.frameworks)}")
        
        parts.append(f"文件: {analyzer.stats.get('total_files', 0)}")
        parts.append(f"代码行: {analyzer.stats.get('total_lines', 0)}")
        
        if analyzer.dependencies:
            parts.append(f"依赖: {len(analyzer.dependencies)} 个")
        
        return " | ".join(parts)
    except ValueError as e:
        return f"⚠️ 安全警告: {str(e)}"
    except Exception as e:
        return f"❌ 获取项目摘要失败: {str(e)}"


@tool
def list_project_files(
    root_dir: str = ".",
    extension: str = None,
    max_count: int = 50
) -> str:
    """
    列出项目中的文件。
    
    参数:
        root_dir: 项目根目录
        extension: 文件扩展名过滤（如 'py', 'js'）
        max_count: 最大显示数量
    
    返回:
        文件列表
    """
    try:
        root = _safe_resolve_root(root_dir)
        context = ProjectContext(str(root))
        wanted_extension = None
        if extension:
            wanted_extension = extension.lower().lstrip("*. ")

        files = []
        for file_info in context.files:
            file_ext = Path(file_info.path).suffix.lower().lstrip(".")
            if wanted_extension and file_ext != wanted_extension:
                continue
            files.append(root / file_info.path)

        files.sort(key=lambda x: (str(x.relative_to(root).parent).lower(), x.name.lower()))
        
        # 截断
        if len(files) > max_count:
            display_files = files[:max_count]
            has_more = True
        else:
            display_files = files
            has_more = False
        
        # 格式化输出
        lines = [f"📁 项目文件 (共 {len(files)} 个，显示前 {len(display_files)} 个):\n"]
        
        current_dir = None
        for file_path in display_files:
            relative_dir = file_path.relative_to(root).parent.as_posix()
            if relative_dir != current_dir:
                lines.append(f"\n📂 {relative_dir}:")
                current_dir = relative_dir
            
            rel_path = file_path.relative_to(root)
            lines.append(f"  📄 {rel_path}")
        
        if has_more:
            lines.append(f"\n... 还有 {len(files) - max_count} 个文件")
        
        return "\n".join(lines)
    except ValueError as e:
        return f"⚠️ 安全警告: {str(e)}"
    except Exception as e:
        return f"❌ 列出文件失败: {str(e)}"


@tool
def get_file_info(file_path: str) -> str:
    """
    获取文件信息（大小、修改时间、行数等）。
    
    参数:
        file_path: 文件路径
    
    返回:
        文件信息
    """
    try:
        path = sanitize_path(file_path, base_dir=get_default_workspace())
        
        if not path.exists():
            return f"❌ 文件不存在: {file_path}"
        
        stat = path.stat()
        
        # 格式化大小
        size = stat.st_size
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024:
                size_str = f"{size:.1f} {unit}"
                break
            size /= 1024
        else:
            size_str = f"{size:.1f} TB"
        
        # 修改时间
        from datetime import datetime
        mtime = datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')
        
        # 行数
        line_count = 0
        if path.suffix.lower() in ['.py', '.js', '.ts', '.java', '.go', '.rs', '.cpp', '.c', '.h', '.md', '.txt']:
            try:
                line_count = len(path.read_text(encoding='utf-8', errors='ignore').split('\n'))
            except Exception:
                # 静默忽略：计算行数失败，保持行数为 0
                pass
        
        lines = [
            f"📄 文件: {path.name}",
            f"📍 路径: {path}",
            f"📊 大小: {size_str}",
            f"🕐 修改: {mtime}",
            f"📝 行数: {line_count}",
        ]
        
        return "\n".join(lines)
    except ValueError as e:
        return f"⚠️ 安全警告: {str(e)}"
    except Exception as e:
        return f"❌ 获取文件信息失败: {str(e)}"


@tool
def list_symbols(
    root_dir: str = ".",
    query: Optional[str] = None,
    kind: Optional[str] = None,
    max_results: int = 80,
) -> str:
    """
    列出或搜索项目中的静态代码符号。

    参数:
        root_dir: 项目根目录
        query: 可选的符号名称片段
        kind: 可选的符号类型过滤，如 class/function/method
        max_results: 最大显示数量

    返回:
        匹配的类、函数、方法等符号位置
    """
    try:
        root = _safe_resolve_root(root_dir)
        limit = max(1, min(int(max_results or 80), 200))
        index = SymbolIndex(root)
        symbols = index.search(query=query, kind=kind, limit=limit)
        title = tr("symbols.top")
        if query:
            title = tr("symbols.matching", query=query)
        if kind:
            title = f"{title} ({kind})"
        return render_symbols(symbols, title=title)
    except ValueError as e:
        return f"⚠️ 安全警告: {str(e)}"
    except Exception as e:
        return f"❌ 列出符号失败: {str(e)}"


@tool
def find_symbol(name: str, root_dir: str = ".", max_results: int = 20) -> str:
    """
    按名称定位项目中的静态代码符号。

    参数:
        name: 要查找的类名、函数名或限定名
        root_dir: 项目根目录
        max_results: 最大显示数量

    返回:
        最相关的符号位置
    """
    try:
        root = _safe_resolve_root(root_dir)
        limit = max(1, min(int(max_results or 20), 100))
        index = SymbolIndex(root)
        symbols = index.find(name, limit=limit)
        return render_symbols(symbols, title=tr("symbols.lookup", name=name))
    except ValueError as e:
        return f"⚠️ 安全警告: {str(e)}"
    except Exception as e:
        return f"❌ 查找符号失败: {str(e)}"


# ==============================================================================
# 导出
# ==============================================================================

__all__ = [
    'ProjectAnalyzer',
    'set_default_workspace',
    'get_default_workspace',
    'analyze_project',
    'get_project_summary',
    'list_project_files',
    'get_file_info',
    'list_symbols',
    'find_symbol',
]


def _matches_path_pattern(path: Path, pattern: str) -> bool:
    """按路径段或 glob 规则匹配忽略模式，避免误伤包含同名子串的目录。"""
    normalized_pattern = pattern.lower()

    if any(char in pattern for char in "*?[]"):
        for candidate in (path, *path.parents):
            candidate_name = candidate.name.lower()
            candidate_path = candidate.as_posix().lower()
            if fnmatch(candidate_name, normalized_pattern) or fnmatch(candidate_path, normalized_pattern):
                return True
        return False

    return normalized_pattern in {part.lower() for part in path.parts}
