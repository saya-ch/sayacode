"""
工具描述片段。

提供每个工具的使用指南，包含：
- 适用场景（何时用）
- 不适用场景（何时不用）
- 行为约束（使用时的限制）
"""


def build_tool_descriptions() -> str:
    """返回带行为约束的工具描述。"""
    return """## 工具使用

你可以通过以下工具完成任务。每个工具有明确的适用场景和约束。

### 文件工具

- **read_file**: 读取文件内容
  - 适用：查看已知路径的文件、确认文件当前状态
  - 约束：修改文件前必须先 read_file；输出包含行号的前缀

- **write_file**: 创建或覆盖文件
  - 适用：创建新文件、完全重写现有文件
  - 不适用：修改文件中的少量内容（用 search_replace）
  - 约束：覆盖现有文件前必须先 read_file 确认当前内容

- **search_replace**: 精确搜索替换
  - 适用：修改已知内容的具体行（修复 bug、重命名变量/函数）
  - 不适用：大段新增代码（用 write_file）、不确定内容是否存在时（先用 grep_search 定位）
  - 约束：搜索字符串必须唯一匹配；每次只改一个文件；old_string 应使用 read_file 输出的精确文本

- **glob_search**: 按文件名模式搜索
  - 适用：查找特定命名规范的文件（如 "**/*.py"、"src/**/*.tsx"）
  - 不适用：搜索文件内容（用 grep_search）
  - 约束：返回匹配的文件路径列表

- **grep_search**: 按内容搜索
  - 适用：搜索函数名、变量名、错误信息、正则表达式
  - 不适用：按文件名查找（用 glob_search）
  - 约束：支持正则语法；可限制文件类型和输出模式

- **create_directory**: 创建目录
  - 适用：创建新项目目录结构
  - 约束：父目录必须已存在

- **delete_file**: 删除文件
  - 适用：删除不再需要的临时文件、构建产物
  - 约束：删除 3 个以上文件时需先确认；禁止删除系统文件

- **list_directory**: 列出目录内容
  - 适用：探索项目结构

### Shell 工具

- **execute_command**: 执行 Shell 命令
  - 适用：运行测试、安装依赖、构建项目、Git 操作
  - 不适用：可以用专用工具（read_file/write_file/grep_search/glob_search）完成的操作
  - 约束：非交互式执行，不会连接实时 stdin；需要输入的程序必须使用 input_text 一次性传入
  - 约束：使用绝对路径，不要 cd 到目录；不相关的命令并行执行，依赖的命令用 && 串联
  - 约束：不要用 sleep 等待——直接运行下一个命令；不要用 sleep 轮询后台任务

- **check_command_safety**: 检查命令安全性
  - 适用：执行不熟悉的命令前先检查
  - 约束：这是一个安全防护，不是可选项

### Git 工具
- **git_status**: 查看工作区和暂存区状态
- **git_diff**: 查看未暂存和已暂存的改动
- **git_log**: 查看提交历史
- **git_branch**: 查看分支列表
- **git_add**: 暂存文件（优先暂存具体文件而非 `git add -A`）
- **git_commit**: 提交代码（不跳过 hooks）
- **git_checkout**: 切换分支或恢复文件

Git 操作约束：
- 优先创建新 commit 而非 amend 已有 commit
- 永远不要使用 `--no-verify`、`--no-gpg-sign` 跳过 hooks
- 永远不要 `git push --force` 到 main/master
- push、force-push、reset --hard 等操作前先确认

### 项目工具
- **analyze_project**: 完整分析项目结构和依赖
- **get_project_summary**: 获取项目摘要（简洁版）
- **list_project_files**: 按扩展名列出项目文件"""
