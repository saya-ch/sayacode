# SAYACODE 架构文档

> 终端 AI 编程 Agent，基于 LangChain / LangGraph 生态。27000+ 行 Python，306 个 pytest。

---

## 目录

1. [启动流程：从 `sayacode` 到交互界面](#1-启动流程从-sayacode-到交互界面)
2. [Agent 引擎：一次用户输入的生命周期](#2-agent-引擎一次用户输入的生命周期)
3. [模型层：多厂商兼容架构](#3-模型层多厂商兼容架构)
4. [提示词系统：行为-人格两层架构](#4-提示词系统行为-人格两层架构)
5. [工具系统：31 工具 + 扩展机制](#5-工具系统31-工具--扩展机制)
6. [会话与上下文压缩](#6-会话与上下文压缩)
7. [权限与安全](#7-权限与安全)
8. [Hook 生命周期](#8-hook-生命周期)
9. [MCP 扩展](#9-mcp-扩展)
10. [命令系统](#10-命令系统)
11. [多 Agent 协作](#11-多-agent-协作)
12. [项目结构速查](#12-项目结构速查)

---

## 1. 启动流程：从 `sayacode` 到交互界面

```
$ sayacode --workspace ./my-project --model-type openai
```

### 1.1 入口：`lib/cli/main.py` → `main()`

```python
def main(argv=None):
    user_config = load_user_config()           # 1. 加载 ~/.sayacode/user_config.json
    args = build_cli_parser().parse_args(argv) # 2. 解析 CLI 参数
    set_language(args.lang or user_config.language)  # 3. 设置语言

    # ---- Doctor 模式（诊断后退出） ----
    if args.doctor:
        checks = run_doctor_checks(workspace)
        print(render_doctor_report(checks))
        sys.exit(0)

    # ---- 六步启动流程 ----

    # 1. 选择工作区
    workspace = resolve_launch_workspace(args, user_config)

    # 2. 配置模型（profile / env / CLI 参数优先级合并）
    model_type, model_name, model_config, profile = resolve_launch_model_config(
        args, user_config, api_manager
    )

    # 3. 测试连接
    if not args.skip_connection_test:
        test_model_connection(model_type, model_name, model_config)

    # 4. 创建 Runtime
    startup = StartupService(api_manager, user_config)
    result = startup.bootstrap(StartupOptions(
        workspace=workspace,
        model_type=model_type, model_name=model_name,
        model_config=model_config, active_profile=profile,
        prompt_style=prompt_style, agent_mode=agent_mode,
        stream_output=not args.no_stream,
        confirm_dangerous=user_config.confirm_dangerous,
        requested_session_id=args.session,
        create_new_session=args.new_session,
    ))
    state, agent, mcp_manager = result.state, result.agent, result.mcp

    # 5. 运行对话
    InteractiveLoop(agent, state, user_config, mcp_manager, ...).run()

    # 6. 退出前清理
    persist_local_state(state, user_config)
    suggest_git_commit(workspace)  # 如果有未提交变更，提示 git commit
    agent.close()
```

### 1.2 `StartupService.bootstrap()` 详解

```python
class StartupService:
    def bootstrap(self, options: StartupOptions) -> StartupResult:
        # ① 创建模型实例
        model = create_runtime_model(
            options.model_type,   # "openai" | "anthropic" | "gemini" | "ollama"
            model_name=options.model_name,
            **options.model_config
        )

        # ② 恢复或创建 Session + Memory
        session, memory, was_restored = load_runtime_managers(
            workspace,
            requested_session_id=options.requested_session_id,
            create_new=options.create_new_session,
        )
        # ③ 同步上下文窗口到 session（用于压缩预算计算）
        sync_session_model_runtime(session, model)

        # ④ 创建 AppState（运行时状态的"数据库"）
        state = create_app_state(
            workspace=workspace, model_type=options.model_type,
            model_config=..., session_manager=session,
            memory_manager=memory, prompt_style=options.prompt_style,
            agent_mode=options.agent_mode,
        )
        state.stream_output = options.stream_output
        apply_agent_mode_permissions(state.agent_mode)  # plan/review → 限制工具

        # ⑤ 构建 RuntimeContext + 工具
        app = RuntimeApplication(api_manager, user_config, mcp_service)
        runtime = app.build_context(state, model=model)
        tools = app.build_tools(runtime)       # ToolFactory 创建 31 个工具

        # ⑥ 创建 Agent
        agent = SAIAgent(
            model=model, workspace=workspace,
            tools=tools, memory_manager=memory,
            session_manager=session, project_context=state.context,
            prompt_style=state.prompt_style, agent_mode=state.agent_mode,
            enable_mcp=True,
            permissions=runtime.permissions,   # 权限运行时
            hooks=runtime.hooks,               # Hook 运行时
        )

        return StartupResult(app, runtime, state, agent, model, mcp_service)
```

### 1.3 状态文件布局

```
~/.sayacode/                     # SAYACODE_HOME（可被环境变量覆盖）
├── user_config.json             # 用户偏好（语言/风格/默认模型）
├── api_configs.json             # 模型 profile 列表
├── permissions.json             # 用户级权限策略
├── hooks.json                   # 用户级 Hook
├── trusted_projects.json        # Hook 信任记录
├── mcp_trusted_projects.json    # MCP 信任记录
├── memory.md                    # 用户级长期记忆
├── history                      # 命令行输入历史
├── audit.jsonl                  # 审计日志
└── sessions/
    └── <workspace_hash>/
        ├── index.json           # 该工作区的会话索引
        ├── <session_id>.json    # 单个会话文件
        └── memory.json          # 该工作区的记忆

./my-project/                    # 项目工作区
├── SAYACODE.md                  # 项目记忆
├── CLAUDE.md                    # 兼容记忆
├── .mcp.json                    # 项目 MCP 配置
├── .claude/commands/*.md        # 自定义 slash 命令
├── .sayacode/
│   ├── permissions.json         # 项目级权限策略
│   └── hooks.json               # 项目级 Hook
└── .sayacode_outputs/           # 超长命令输出缓存
```

---

## 2. Agent 引擎：一次用户输入的生命周期

### 2.1 架构总览

```
用户输入 → InteractiveLoop → SAIAgent.run() / stream_run()
              │                      │
              │                      ├─ ConversationMgr.start_turn()
              │                      │    ├─ session.add_user_message()
              │                      │    └─ memory.start_interaction()
              │                      │
              │                      ├─ PromptBuilder.build_messages()
              │                      │    ├─ session.maybe_compact()    ← 70%/80%/90% 分层压缩
              │                      │    ├─ context_packager.pack()    ← 打包项目上下文
              │                      │    ├─ get_system_reminders()    ← 运行时提醒注入
              │                      │    └─ session.get_messages()    ← 恢复历史消息
              │                      │         └─ additional_kwargs 透传 ← reasoning_content
              │                      │
              │                      ├─ AgentRunner.stream(messages)
              │                      │    └─ LangGraph create_react_agent
              │                      │         ├─ model.bind_tools(tools)
              │                      │         ├─ agent.invoke({"messages": messages})
              │                      │         │    ├─ LLM 返回 AIMessage(tool_calls=...)
              │                      │         │    ├─ LangGraph 执行工具
              │                      │         │    ├─ LLM 继续生成
              │                      │         │    └─ ... 循环直到 stop_reason != tool_use
              │                      │         └─ 返回完整消息列表
              │                      │
              │                      └─ [恢复路径] 如果出错:
              │                           ├─ recoverable → 指数退避重试 (最多 3 次)
              │                           ├─ max_output_tokens → 注入续接消息重试
              │                           └─ prompt_too_long → 紧急压缩后重试
              │
              └─ ConversationMgr.finish_turn()
                   ├─ session.add_assistant_message()
                   └─ memory.add_interaction()
```

### 2.2 SAIAgent 核心数据结构

```python
class SAIAgent:
    # ── LangChain 层 ──
    model: BaseModel              # 语言模型实例 (OpenAI/Anthropic/Gemini/Ollama)
    tools: List[BaseTool]         # 可用工具列表
    runner: AgentRunner           # 管理 LangGraph agent 生命周期

    # ── 运行时管理器 ──
    workspace: Path               # 工作区根目录
    session: SessionManager       # 多轮对话历史 + Token 预算
    memory: MemoryManager         # 交互记忆 + 修改文件追踪
    safety: SafetyChecker         # 危险操作拦截
    context: ProjectContext       # 项目结构扫描

    # ── 提示词 ──
    prompt_builder: PromptBuilder # 组装完整系统提示词
    prompt_style: str             # standard / concise / tsundere / genki / ...
    agent_mode: str               # build / plan / review

    # ── Turn 追踪 ──
    _turn_count: int              # 当前会话总轮次
    _abort_controller: ToolAbortController  # 同级工具中止信号
    _last_extra: dict             # 上一轮 LLM 响应的 additional_kwargs
    _recovery_state: dict         # 恢复路径状态 (attempt, path)
```

### 2.3 Turn 状态机

```python
class TurnTransition(Enum):
    NEXT_TURN = "next_turn"             # 正常工具调用 → 继续循环
    COMPLETED = "completed"              # 无工具调用 → 结束
    STREAM_INTERRUPTED = "stream_interrupted"  # 流中断 → 尝试恢复
    MODEL_ERROR = "model_error"          # 模型调用失败
    MAX_RETRIES = "max_retries"          # 恢复路径耗尽
    ABORTED = "aborted"                 # 用户中断

@dataclass
class TurnState:
    transition: TurnTransition
    turn_count: int
    tool_use_count: int
    needs_follow_up: bool          # 是否需要继续（有工具调用）
    error_message: str

    @property
    def should_continue(self) -> bool:
        return (self.transition == TurnTransition.NEXT_TURN
                and self.needs_follow_up)
```

### 2.4 错误恢复路径

```
LLM 调用抛出异常
  │
  ├─ _classify_error(error_msg)
  │
  ├─ "recoverable" ──→ 指数退避重试 (1.5ⁿ 秒, n=1,2,3)
  │   (rate_limit / timeout / server_error / connection)
  │
  ├─ "max_output_tokens" ──→ 注入 "Resume directly" 消息后重试
  │   (最多 3 次)
  │
  ├─ "prompt_too_long" ──→ session.force_compact() 后重试
  │
  └─ "fatal" ──→ 返回错误信息，不重试
```

### 2.5 additional_kwargs 透传管道

这是为 DeepSeek `reasoning_content` / Claude `thinking_blocks` 设计的通用机制：

```
┌─ 第一轮 ─────────────────────────────────────────────┐
│ LLM 响应 → AIMessage.additional_kwargs               │
│                 .reasoning_content = "思考过程..."    │
│                         │                            │
│ _extract_response() ────┘                            │
│   self._last_extra = {"reasoning_content": "..."}    │
│                         │                            │
│ finish_turn(metadata={"additional_kwargs": ...})     │
│   → Message.metadata["additional_kwargs"] = {...}    │
└──────────────────────────────────────────────────────┘
                          │
┌─ 第二轮 ───────────────┐
│ build_messages()        │
│   history = session.get_messages()                   │
│   for msg in history:                               │
│     extra = msg["metadata"]["additional_kwargs"]     │
│     AIMessage(content=msg["content"],                │
│                additional_kwargs=extra)  ← 恢复!     │
└──────────────────────────────────────────────────────┘
```

---

## 3. 模型层：多厂商兼容架构

### 3.1 继承体系

```
BaseModel (抽象基类)                    ← lib/models/base.py
├─ OpenAIModel                          ← lib/models/openai_model.py
│   ├─ _is_deepseek() → True
│   │   └─ UniversalChatDeepSeek (ChatDeepSeek + 注入修复)
│   └─ _is_deepseek() → False
│       └─ UniversalChatOpenAI (ChatOpenAI + 通用透传)
│
├─ AnthropicModel                       ← lib/models/anthropic_model.py
│   └─ ChatAnthropic (原生 thinking 支持)
│
├─ GeminiModel                          ← lib/models/gemini_model.py
│   └─ 自研 BaseChatModel 子类（原生 Gemini API 映射）
│
└─ OllamaModel                          ← lib/models/ollama_model.py
    └─ ChatOllama
```

### 3.2 基础接口

```python
class BaseModel(ABC):
    model_name: str
    temperature: float
    _model: Any                # 底层 LangChain 模型实例（延迟初始化）

    @property
    def context_window(self) -> int:  # 上下文窗口（token）
        ...

    @abstractmethod
    def chat(self, messages: List[Dict], **kwargs) -> str: ...

    @abstractmethod
    def chat_stream(self, messages: List[Dict], **kwargs) -> Iterator[str]: ...

    def bind_tools(self, tools: List) -> Any:
        """返回已绑定工具的模型（供 LangGraph Agent 使用）"""
        self._initialize_model()
        return self._model.bind_tools(tools)

    def _record_usage(self, usage: TokenUsage):
        """记录 Token 用量（last_usage + session_usage 累加）"""
```

### 3.3 UniversalChatOpenAI：通用 non-standard-field 透传

`langchain-openai` 的 `ChatOpenAI` 明确拒绝处理非标准响应字段（`reasoning_content` 等）。`UniversalChatOpenAI` 覆盖两个关键方法：

```python
class UniversalChatOpenAI(ChatOpenAI):
    def _create_chat_result(self, response, ...):
        # 【提取】: 父类生成 ChatResult 后，从原始 API 响应中
        # 扫描 reasoning_content / reasoning_details / citations 等非标字段
        # 注入到 AIMessage.additional_kwargs
        result = super()._create_chat_result(response, ...)
        extras = _extract_nonstandard_fields(response.choices[0].message)
        result.generations[0].message.additional_kwargs.update(extras)
        return result

    def _get_request_payload(self, input_, ...):
        # 【注入】: 父类生成 payload 后，扫描 input 中的 AIMessage
        # 将其 additional_kwargs 的非标字段写入 payload["messages"]
        payload = super()._get_request_payload(input_, ...)
        _inject_nonstandard_fields(input_, payload)
        return payload
```

### 3.4 UniversalChatDeepSeek：修复 ChatDeepSeek 注入缺陷

`ChatDeepSeek`（langchain-deepseek 官方包）正确提取了 `reasoning_content`，但 `_get_request_payload` 只处理了 message content 格式转换，没有把 `reasoning_content` 写回 API 请求。`UniversalChatDeepSeek` 修复此问题：

```python
class UniversalChatDeepSeek(ChatDeepSeek):
    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        _inject_nonstandard_fields(input_, payload)
        return payload
```

### 3.5 ModelRegistry：按 model_type 路由

```python
class ModelProviderRegistry:
    def create_model(self, model_type: str, model_name: str, **kwargs) -> BaseModel:
        if model_type == "openai":
            return OpenAIModel(model_name=model_name, ...)
        elif model_type == "anthropic":
            return AnthropicModel(model_name=model_name, ...)
        elif model_type == "gemini":
            return GeminiModel(model_name=model_name, ...)
        elif model_type == "ollama":
            return OllamaModel(model_name=model_name, ...)
```

---

## 4. 提示词系统：行为-人格两层架构

### 4.1 设计原则

```
┌─────────────────────────────────┐
│  人格层 (personality overlay)   │  ← 可选叠加。7 种风格独立切换
├─────────────────────────────────┤
│  行为层 (behavior fragments)    │  ← 始终加载。定义安全/效率/代码规范
│  6 个 fragment：               │
│  base_profile                   │
│  task_playbook                  │
│  communication_style            │
│  tool_descriptions              │
│  security_rules                 │
│  code_generation                │
└─────────────────────────────────┘
```

### 4.2 get_system_prompt() 组合流程

```python
def get_system_prompt(agent_name, workspace, project_summary, agent_mode):
    sections = [
        build_base_profile(agent_name),        # "你是 SAYA，运行在终端里的 AI 编程 Agent..."
        build_task_playbook(),                 # "先识别用户任务类型..."
        build_communication_style(),           # "假设用户看不到工具调用..."
        build_tool_descriptions(),             # "文件工具: read_file..."
        build_security_rules(),                # "绝对禁止: rm -rf /..."
        build_code_generation_rules(),         # "默认不写注释..."
    ]

    # 模式条件加载
    if mode == "plan":
        sections.append(build_plan_mode_prompt())    # "你是架构师，不是实现者..."
    elif mode == "review":
        sections.append(build_review_mode_prompt())  # "P0 阻塞 → P1 重要 → P2 建议..."

    # 动态上下文
    if project_summary:
        sections.append(f"### 当前项目\n{project_summary}")

    return "\n\n".join(sections)
```

### 4.3 人格叠加

```python
def get_prompt_by_style(style: str, **kwargs) -> str:
    """根据风格获取系统提示词"""
    base = get_system_prompt(**kwargs)                    # 行为层
    overlay = build_personality_overlay(style, "SAYA")    # 人格层
    return base + "\n\n" + overlay if overlay else base
```

7 种风格：`tsundere` / `genki` / `mesugaki` / `onee-san` / `idol` / `catgirl` / `mukuchi`。
每种风格定义了 5 维数据：角色定位、语气规则、技术任务表现、示例语气、禁止事项。

### 4.4 系统提醒 (System Reminders)

运行时注入到系统消息末尾：

```python
def get_system_reminders(state: dict) -> str:
    reminders = []

    # 模式提醒
    if mode == "plan":
        reminders.append("**Plan 模式**：只读规划。不写文件、不执行 Shell。")

    # 上下文分级
    if usage > 0.85:
        reminders.append("上下文使用率 85%（紧急）。尽快压缩。")
    elif usage > 0.70:
        reminders.append("上下文使用率 70%（偏高）。优先精确编辑，避免大段重写。")

    # 语言一致性
    if language == "zh-CN":
        reminders.append("用户使用中文，用中文回复。")

    return "\n\n".join(f"- {r}" for r in reminders)
```

---

## 5. 工具系统：31 工具 + 扩展机制

### 5.1 工具分类

| 分组 | 数量 | 工具 |
|------|------|------|
| file | 9 | read_file, write_file, search_replace, batch_edit, glob_search, grep_search, create_directory, delete_file, list_directory |
| shell | 5 | execute_command_tool, check_command_safety_tool, get_system_info, list_environment_variables, read_output_file |
| git | 11 | git_status, git_diff, git_log, git_branch, git_remote, git_add, git_commit, git_stash, git_checkout, git_pull, git_push |
| project | 6 | analyze_project, get_project_summary, list_project_files, get_file_info, list_symbols, find_symbol |
| search | 1 | ToolSearch （按关键字搜索工具） |

### 5.2 工具创建流水线

```
ToolFactory(context: RuntimeContext) → List[StructuredTool]
  │
  ├─ 从 modules 导入 31 个原始函数
  ├─ _wrap_tool_with_hooks() 逐个包装
  │    ├─ 注入 PreToolUse / PostToolUse / ToolFailure Hook 事件
  │    ├─ 注入 ToolAbortController 检查
  │    ├─ 注入 audit_event 审计记录
  │    └─ Shell/Git 工具失败 → 触发同级中止 (Sibling Abort)
  │
  ├─ register_tool_meta() 注册元数据
  │    name, is_concurrency_safe, is_read_only, is_destructive,
  │    search_hint, tool_group, max_result_chars, ...
  │
  └─ 注入 MCP 工具（如果有）
```

### 5.3 ToolMeta 元数据

```python
@dataclass
class ToolMeta:
    name: str
    is_concurrency_safe: bool = False   # 可并发执行？
    is_read_only: bool = False          # 只读？
    is_destructive: bool = False        # 破坏性？
    requires_confirmation: bool = False  # 需要确认？
    interrupt_behavior: str = "cancel"   # 中断行为: cancel | block
    tool_group: str = "other"            # file | shell | git | project
    search_hint: str = ""                # ToolSearch 关键字
    should_defer: bool = False           # 延迟加载？
    always_load: bool = False            # 永远加载？
    max_result_chars: int = 50_000       # 结果大小上限
```

### 5.4 并发工具批处理

```python
class ToolBatchExecutor:
    def execute_batch(self, requests: List[ToolCallRequest]) -> BatchResult:
        safe, unsafe = partition_by_concurrency(requests)
        # 并发安全组 → ThreadPoolExecutor 并行执行
        safe_results = self._execute_concurrent(safe)
        # 非安全组 → 串行执行
        for req in unsafe:
            result = self._execute_one(req)
            if result.is_error and can_abort_siblings(req.tool_name):
                batch.abort_reason = f"sibling_error: {req.tool_name}"
        return batch
```

### 5.5 工具执行 Hook 包装

```python
def _wrap_tool_with_hooks(tool_obj):
    def wrapped_func(*args, **kwargs):
        # 1. 检查同级中止信号
        abort_ctrl = get_abort_controller()
        if abort_ctrl.is_aborted:
            return f"操作已中止（{abort_ctrl.reason}）"

        # 2. PreToolUse Hook
        block = trigger_hook_event("PreToolUse", {
            "tool_name": name, "arguments": arguments
        })
        if block:
            return f"被 Hook 阻止: {block}"

        # 3. 执行工具
        try:
            result = original_func(*args, **kwargs)
        except Exception as exc:
            trigger_hook_event("ToolFailure", {...})
            if tool_name in SIBLING_ABORT_TOOLS:  # Shell/Git 类
                abort_ctrl.abort("sibling_error")
            raise

        # 4. PostToolUse Hook + 审计
        trigger_hook_event("PostToolUse", {...})
        append_audit_event("tool", tool_name, ...)
        return result

    return StructuredTool.from_function(func=wrapped_func, ...)
```

---

## 6. 会话与上下文压缩

### 6.1 SessionManager 核心设计

```
SessionManager
│
├─ messages: List[Message]         # 消息历史
│   └─ Message(role, content, timestamp, metadata)
│
├─ Token 预算追踪
│   ├─ model_context_limit         # 模型上下文窗口（token）
│   ├─ _running_tokens             # 当前估算 token 数
│   ├─ usage_ratio                 # 使用比例 (0.0~1.0)
│   ├─ context_budget              # 预算 = limit * 0.80
│   └─ output_reserve              # 输出保留 = limit * 0.15
│
└─ 压缩状态
    ├─ _compact_fn                 # LLM 回调（用于语义摘要）
    ├─ _compact_count              # 已压缩次数
    └─ summary                     # 最近一次压缩摘要
```

### 6.2 三层分层压缩

```
┌─────────────────────────────────────────────────┐
│ 90% 紧急  │ force_compact()                     │
│           │ 保留 KEEP_FULL_ROUNDS // 2 = 5 轮   │
│           │ 最激进压缩，清出最多空间              │
├─────────────────────────────────────────────────┤
│ 80% 标准  │ needs_compact → _auto_compact()     │
│           │ 保留 KEEP_FULL_ROUNDS = 10 轮        │
├─────────────────────────────────────────────────┤
│ 70% 预防  │ needs_preventive_compact → gentle   │
│           │ 保留 KEEP_FULL_ROUNDS + 5 = 15 轮    │
│           │ 轻度压缩，提前清理                    │
└─────────────────────────────────────────────────┘
```

### 6.3 LLM 语义摘要

```
对话轮次 → _generate_semantic_summary()
  │
  ├─ 构建对话文本（每轮截断 1500 字符）
  ├─ 限制输入长度 15000 字符
  ├─ 调用 _compact_fn（model.chat）生成摘要
  │    └─ 9 段式结构化提示词：
  │       目标/文件/错误/尝试/指令/决策/待办/状态/下一步
  │
  └─ 失败时回退到静态截断 (_summarize_rounds_bulk)
```

---

## 7. 权限与安全

### 7.1 权限层次（优先级从高到低）

```
Session 级规则     ← /permissions allow write_file session
    │
Project 级规则     ← .sayacode/permissions.json
    │
User 级规则        ← ~/.sayacode/permissions.json
    │
Built-in 默认规则  ← 代码硬编码
```

### 7.2 默认权限映射

```python
# 只读工具 → allow
READ_ONLY_TOOLS = {read_file, glob_search, grep_search, list_directory,
                   git_status, git_diff, git_log, git_branch, git_remote,
                   analyze_project, get_project_summary, list_project_files,
                   get_file_info, list_symbols, find_symbol, ...}

# 安全写入 → allow
SAFE_WRITE_TOOLS = {write_file, search_replace, create_directory, batch_edit}

# 安全 Git → allow
SAFE_GIT_TOOLS = {git_add, git_commit}

# 直接变更 → allow
DIRECT_MUTATING_TOOLS = {execute_command_tool, git_checkout, git_pull, git_stash}

# 需确认 → ask
ASK_TOOLS = {delete_file, git_push}
```

### 7.3 拒绝追踪 (DenialTracker)

```python
class DenialTracker:
    consecutive_denials: int = 0    # 连续拒绝
    total_denials: int = 0          # 总计拒绝
    MAX_CONSECUTIVE = 3             # 连续 3 次 → 回退
    MAX_TOTAL = 20                  # 总计 20 次 → 回退

    def should_fallback_to_prompting(self) -> bool:
        """连续拒绝 3 次或总计 20 次 → 自动切换到询问模式"""
```

### 7.4 安全模块

```python
class SafetyChecker:
    def check_operation(self, operation: Operation) -> SafetyResult:
        """检查操作是否危险"""
        # 路径检查: 系统目录？含密钥？
        # 命令检查: rm -rf /? format? curl | sh?
        # 文件检查: .env? .ssh? .aws?
```

---

## 8. Hook 生命周期

### 8.1 支持的事件

| 事件 | 触发时机 | 用途 |
|------|---------|------|
| `SessionStart` | 会话开始 | 环境准备 |
| `UserPromptSubmit` | 用户提交输入 | 输入预处理 / 阻断 |
| `PreToolUse` | 工具执行前 | 权限检查 / 阻断 |
| `PostToolUse` | 工具执行后 | 日志 / 通知 |
| `ToolFailure` | 工具执行失败 | 告警 / 回滚 |
| `SessionEnd` | 会话结束 | 清理 / 通知 |

### 8.2 Hook 配置格式

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "name": "block-rm",
        "command": "python scripts/check_rm.py",
        "timeout": 5000
      }
    ]
  }
}
```

- Hook 命令返回非零退出码 = 阻断操作
- stdout 作为阻断原因展示给用户

---

## 9. MCP 扩展

### 9.1 工作流

```
项目 .mcp.json → /mcp trust → MCPRuntime.load_tools()
  │                              │
  │                              ├─ 启动 stdio MCP server 进程
  │                              ├─ 获取 tools/list
  │                              └─ 包装为 LangChain StructuredTool
  │
  └─ SAIAgent._load_mcp_tools() → self.tools += mcp_tools
```

### 9.2 信任机制

```
~/.sayacode/mcp_trusted_projects.json
{
  "<workspace_path_hash>": {
    "trusted_at": "2024-01-01T00:00:00Z",
    "servers": ["server1", "server2"]
  }
}
```

首次使用 `/mcp trust` 手动信任，之后自动加载。

---

## 10. 命令系统

### 10.1 路由机制

```python
class CommandRouter:
    def dispatch(self, raw_command: str, runtime: RuntimeContext) -> Optional[bool]:
        command = parse_command(raw_command)   # "/help abc" → CommandContext(name="help", args="abc")
        if command is None:                    # 不是斜杠命令 → 交给 Agent
            return None
        handler = self._routes.get(command.name)
        if handler is None:
            return None
        return handler.handle(command, runtime)  # True=已处理, False=退出
```

### 10.2 17 个斜杠命令

| 命令 | Handler | 功能 |
|------|---------|------|
| `/help` | HelpCommandHandler | 命令参考 |
| `/guide` | GuideCommandHandler | 引导流程 |
| `/start` | GuideCommandHandler | 快速开始 |
| `/status` | StatusCommandHandler | 运行时状态 |
| `/workspace` | WorkspaceCommandHandler | 工作区摘要 |
| `/context` | ContextCommandHandler | 项目上下文 |
| `/paths` | PathsCommandHandler | 状态文件路径 |
| `/sessions` | SessionCommandHandler | 会话管理 (list/new/use/rename) |
| `/history` | HistoryCommandHandler | 对话历史 |
| `/compact` | CompactCommandHandler | 手动压缩 |
| `/clear` | ClearCommandHandler | 清屏 |
| `/model` | ModelCommandHandler | 模型管理 (list/use/add/test) |
| `/config` | ConfigCommandHandler | 模型配置向导 |
| `/mode` | ModeCommandHandler | 切换 build/plan/review |
| `/style` | StyleCommandHandler | 切换人格风格 |
| `/lang` | LanguageCommandHandler | 切换语言 |
| `/settings` | SettingsCommandHandler | 运行时开关 |
| `/permissions` | PermissionsCommandHandler | 权限管理 |
| `/tools` | ToolsCommandHandler | 工具列表 |
| `/symbols` | SymbolsCommandHandler | 符号搜索 |
| `/mcp` | McpCommandHandler | MCP 管理 |
| `/hooks` | HooksCommandHandler | Hook 管理 |
| `/doctor` | DoctorCommandHandler | 系统诊断 |
| `/analyze` | AnalyzeCommandHandler | 项目分析 |
| `/git` | GitCommandHandler | Git 操作 |
| `/stats` | StatsCommandHandler | Agent 统计 |
| `/reset` | ResetCommandHandler | 重置 Agent |
| `/team` | TeamCommandHandler | 多 Agent 协作 |
| `/commands` | CustomCommandsCommandHandler | 自定义命令 |
| `/quit` | QuitCommandHandler | 退出 |

---

## 11. 多 Agent 协作

### 11.1 通信架构

```
┌──────────────┐   文件系统邮箱    ┌──────────────┐
│  Main Agent  │ ←──────────────→ │  Sub-Agent   │
│              │  JSON 消息文件    │              │
└──────────────┘                  └──────────────┘
```

### 11.2 核心组件

```python
# AgentMailbox: 文件系统消息邮箱
class AgentMailbox:
    def write(self, message: dict) -> Path    # 写入 .json
    def read_all(self) -> list[dict]           # 读取所有未读
    def mark_read(self, message_id: str)       # .json → .read
    def poll(self, timeout=1.0) -> dict | None # 阻塞等待

# WorkerManager: 子进程生命周期
class WorkerManager:
    def spawn(self, agent_config: dict) -> str  # 启动子进程
    def kill(self, worker_id: str)              # 终止
    def cleanup_all(self)                       # SIGINT 级联清理

# TeamManager: 统一入口
class TeamManager:
    def spawn(self, agent_type, task, workspace) -> str
    def get_mailbox(self, worker_id) -> AgentMailbox
    def get_status(self) -> str
    def cleanup(self) -> int
```

---

## 12. 项目结构速查

```
lib/
├── cli/                      # CLI 入口与配置
│   ├── main.py               # main() 入口
│   ├── parser.py             # argparse 参数定义
│   ├── configure.py          # 模型连接配置逻辑
│   ├── permissions.py        # 权限弹窗 UI
│   └── workspace.py          # 工作区选择
│
├── runtime/                  # 运行时启动与交互
│   ├── startup.py            # StartupService.bootstrap()
│   ├── interactive.py        # InteractiveLoop 主循环
│   ├── context.py            # RuntimeContext 数据容器
│   ├── app.py                # RuntimeApplication 工厂
│   ├── session_store.py      # 会话持久化 + manager 加载
│   ├── model_profiles.py     # 模型 profile 管理
│   └── launch_config.py      # 启动配置
│
├── agent.py                  # SAIAgent 主类（入口）
│
├── core/                     # 核心服务
│   ├── agent_runtime.py      # PromptBuilder / AgentRunner / TurnState
│   ├── session.py            # SessionManager (800+ 行)
│   ├── memory.py             # MemoryManager
│   ├── permissions.py        # PermissionPolicy / PermissionRuleSet
│   ├── hooks.py              # Hook 生命周期管理
│   ├── mcp_runtime.py        # MCP 工具加载
│   ├── context.py            # ProjectContext 项目扫描
│   ├── context_packager.py   # 上下文打包（截断 + 预算）
│   ├── safety.py             # SafetyChecker 危险操作拦截
│   ├── audit.py              # 审计日志
│   ├── doctor.py             # 诊断系统
│   ├── symbols.py            # 静态符号索引
│   ├── modes.py              # Agent 模式管理
│   ├── paths.py              # 状态文件路径管理
│   ├── denial_tracker.py     # 拒绝追踪器
│   ├── tool_meta.py          # 工具元数据
│   ├── process_env.py        # 进程环境检测
│   ├── private_io.py         # 私有文件 I/O
│   ├── project_memory.py     # 项目记忆加载
│   ├── team_manager.py       # 多 Agent 管理
│   ├── agent_mailbox.py      # 文件系统邮箱
│   └── worker_manager.py     # Worker 生命周期
│
├── models/                   # 模型适配层
│   ├── base.py               # BaseModel 抽象基类
│   ├── openai_model.py       # OpenAI 兼容（自动路由 DeepSeek）
│   ├── universal_chat_openai.py  # UniversalChatOpenAI + UniversalChatDeepSeek
│   ├── anthropic_model.py    # Anthropic Claude
│   ├── gemini_model.py       # Google Gemini
│   ├── ollama_model.py       # Ollama 本地模型
│   ├── registry.py           # ModelProviderRegistry
│   └── provider_catalog.py   # 提供商目录
│
├── prompts/                  # 提示词系统
│   ├── system_prompt.py      # get_system_prompt() / get_prompt_by_style()
│   ├── reminders.py          # get_system_reminders()
│   └── fragments/            # 微模块化片段
│       ├── base_profile.py
│       ├── task_playbook.py
│       ├── communication_style.py
│       ├── tool_descriptions.py
│       ├── security_rules.py
│       ├── code_generation.py
│       ├── mode_subagents.py
│       └── personality_overlay.py
│
├── tools/                    # 工具系统
│   ├── __init__.py           # 工具注册 + Hook 包装
│   ├── registry.py           # ToolFactory / ToolRegistry
│   ├── context.py            # ToolExecutionContext / ToolAbortController
│   ├── file_tools.py         # 文件操作
│   ├── shell_tools.py        # Shell 命令
│   ├── git_tools.py          # Git 操作
│   ├── project_tools.py      # 项目分析
│   ├── safety.py             # 安全检查工具
│   ├── batch_executor.py     # 并发批处理
│   └── tool_search.py        # ToolSearch 工具发现
│
├── commands/                 # 斜杠命令
│   ├── router.py             # CommandRouter
│   ├── base.py               # CommandContext / CommandHandler
│   ├── conversation.py       # /help /compact /history /clear /context /quit
│   ├── diagnostics.py        # /doctor
│   ├── hooks.py              # /hooks
│   ├── mcp.py                # /mcp
│   ├── mode.py               # /mode
│   ├── model.py              # /model /config
│   ├── permissions.py        # /permissions
│   ├── preferences.py        # /style /lang /settings /prefs
│   ├── runtime_handlers.py   # build_default_command_router()
│   ├── runtime_info.py       # /status /stats /analyze /git /reset
│   ├── session.py            # /sessions
│   ├── symbols.py            # /symbols
│   ├── team.py               # /team
│   ├── tools.py              # /tools
│   └── workspace.py          # /workspace /paths /commands
│
├── state.py                  # AppState / UserConfig / ConfigState
├── theme.py                  # Rich 终端渲染
├── i18n.py                   # 中英双语 (1400+ 行)
├── custom_commands.py        # .claude/commands/*.md 加载
├── api_config/               # API 配置管理
│   ├── api_config.py         # APIConfigManager
│   └── wizard.py             # API 配置向导
│
run.py                        # 开发启动脚本
pyproject.toml                # 项目配置（动态版本号）
requirements.txt              # 依赖
```

---

## 附录：关键技术决策

### A. 为什么用 LangGraph 而不是手写 Agent 循环？

LangGraph 的 `create_react_agent` 提供了成熟的 Think-Act-Observe 循环、工具调用管理、流式输出支持。代价是无法直接干预循环内部。SAYACODE 在 LangGraph **之上**做了错误恢复、additional_kwargs 透传、并发批处理等增强。

### B. 为什么每个厂商需要独立的模型适配？

`ChatOpenAI`（langchain-openai）明确只处理 OpenAI 标准字段。DeepSeek 的 `reasoning_content`、Claude 的 `thinking` 块等非标准字段会被丢弃。LangChain 的策略是"一个厂商一个包"。SAYACODE 的选择是：
- DeepSeek → `ChatDeepSeek` + 注入修复
- 其他 OpenAI 兼容厂商 → `UniversalChatOpenAI` 通用透传层
- Anthropic / Gemini → 各自官方适配包

### C. 为什么有两个 `maybe_compact()` 调用？

一个在 `agent._build_messages()` 中（Agent 层），一个在 `prompt_builder.build_messages()` 中（Prompt 层）。这是历史遗留，但因为压缩是幂等的（压缩后不会再次触发），实际不会有副作用。
