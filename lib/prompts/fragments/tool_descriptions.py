"""
工具描述片段 v2。

提供每个工具的使用指南，包含：
- 适用场景（何时用）
- 不适用场景（何时不用）
- 行为约束（使用时的限制）
- ToolSearch 工具发现机制
"""


def build_tool_descriptions() -> str:
    """返回带行为约束的工具描述。"""
    return """## 工具使用

你可以通过以下工具完成任务。每个工具有明确的适用场景和约束。

### 工具发现 (ToolSearch)
- 如果你不确定用哪个工具，或怀疑某个功能可能存在但没出现在下面的列表中，使用 **ToolSearch** 按关键字搜索。
- ToolSearch 接受中文或英文关键词，返回匹配的工具名称和简要说明。

### 文件工具

- **read_file**: 读取文件内容
  - 适用：查看已知路径的文件、确认文件当前状态
  - 约束：修改文件前必须先 read_file；输出包含行号前缀

- **write_file**: 创建或覆盖文件
  - 适用：创建新文件、完全重写现有文件
  - 不适用：修改文件中的少量内容（用 search_replace）
  - 约束：覆盖现有文件前必须先 read_file 确认当前内容

- **search_replace**: 精确搜索替换
  - 适用：修改已知内容的具体行（修复 bug、重命名变量/函数）
  - 不适用：大段新增代码（用 write_file）、不确定内容是否存在时（先用 grep_search 定位）
  - 约束：搜索字符串必须唯一匹配；每次只改一个文件；old_string 应使用 read_file 输出的精确文本
  - 每次 edit 只改一个逻辑变更。不相关的改动分成多次 edit。

- **glob_search**: 按文件名模式搜索
  - 适用：查找特定命名规范的文件（如 "**/*.py"、"src/**/*.tsx"）
  - 不适用：搜索文件内容（用 grep_search）
  - 约束：返回匹配的文件路径列表

- **grep_search**: 按内容搜索
  - 适用：搜索函数名、变量名、错误信息、正则表达式
  - 不适用：按文件名查找（用 glob_search）
  - 约束：支持正则语法；可限制文件类型和输出模式

- **batch_edit**: 批量编辑多个文件
  - 适用：跨多个文件的统一修改（重命名、格式调整）
  - 约束：每次编辑指定文件路径和要替换的内容

- **create_directory**: 创建目录
  - 适用：创建新项目目录结构
  - 约束：父目录必须已存在

- **delete_file**: 删除文件
  - 适用：删除不再需要的临时文件、构建产物
  - 约束：删除前确认；禁止删除系统文件；批量删除需逐项确认

- **list_directory**: 列出目录内容
  - 适用：探索项目结构、确认文件存在

### Shell 工具

- **execute_command_tool**: 执行 Shell 命令
  - 适用：运行测试、安装依赖、构建项目、执行无法用现有专用工具替代的操作
  - 不适用：可以用专用工具（read_file/write_file/grep_search/glob_search）完成的操作
  - 约束：非交互式执行，不会连接实时 stdin；需要输入的程序使用 input_text 一次性传入
  - 约束：使用绝对路径，不要 cd 到目录；不相关的命令并行执行，依赖的命令用 && 串联
  - 约束：不要 sleep —— 直接执行后续命令；不要 sleep 轮询后台任务

- **check_command_safety_tool**: 检查命令安全性
  - 适用：执行不熟悉的命令前先检查
  - 约束：安全防护，不是可选项

- **get_system_info**: 获取系统信息
- **list_environment_variables**: 列出环境变量
- **read_output_file**: 读取超长命令输出文件

### Git 工具
- **git_status**: 查看工作区和暂存区状态
- **git_diff**: 查看未暂存和已暂存的改动
- **git_log**: 查看提交历史
- **git_branch**: 查看分支列表
- **git_remote**: 管理远程仓库
- **git_add**: 暂存文件（优先暂存具体文件而非 `git add -A`）
- **git_commit**: 提交代码
- **git_checkout**: 切换分支或恢复文件
- **git_stash**: 暂存工作区修改
- **git_pull**: 拉取远程更新
- **git_push**: 推送到远程仓库

Git 操作约束：
- 优先创建新 commit 而非 amend 已有 commit
- 永远不要使用 `--no-verify`、`--no-gpg-sign` 跳过 hooks
- 永远不要 `git push --force` 到 main/master
- 永远不要跳过 hooks（--no-verify）。如果 hook 失败，调查并修复根因。
- 如果需要提交，先检查 git status 和 git diff，理解变更内容后再写 commit message。
- Commit message 用一句话说清"为什么改"，不要堆砌"改了哪些文件"。

### 项目工具
- **analyze_project**: 完整分析项目结构和依赖
- **get_project_summary**: 获取项目摘要（简洁版）
- **list_project_files**: 按扩展名列出项目文件
- **get_file_info**: 获取文件的详细信息
- **list_symbols**: 列出代码符号（函数、类、方法）
- **find_symbol**: 按名称查找特定符号"""
