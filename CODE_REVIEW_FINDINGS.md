# SAYACODE `lib/` 核心逻辑梳理报告

> **梳理方式**：逐模块读源码 + 运行时实证探测。清单中每条「已证实缺陷」都附有可复现的探测证据。
> **配套变更**：同期完成 `lib/` 全部英文 docstring 与注释的汉化（77 个文件）。两者分开验证：汉化只动注释/docstring（骨架比对证明），逻辑结论来自独立探测。
> **验证基线**（修复后）：`compileall` exit 0 · `ruff check .` All checks passed · `mypy` 48 modules clean · `pytest` **410 passed**

## 缺陷总览

| 编号 | 缺陷 | 严重度 | 状态 |
|---|---|---|---|
| **A1** | 危险工具 force-deny 是死代码，安全承诺不成立 | 高 | ✅ 已修 |
| **A2** | 「连续拒绝自动回退询问」是空操作 | 高 | ✅ 已修 |
| **A5** | 错误分类表失准（实测 8 例错 5 例） | 高 | ✅ 已修 |
| **A6** | 压缩摘要永不进 prompt，压缩=静默丢弃历史 | 高 | ✅ 已修 |
| **A3** | `get_effective_action()` 冗余死循环 | 低 | ✅ 已修 |
| **A7** | `force_compact()` 是死代码，恢复路径错用 `compact()` | 中 | ✅ 已修 |
| **A4** | Hook 串行执行，最坏 N×30s 且无进度提示 | 中 | ⏸ 待定 |
| B1/B3 | 切 `/mode build` 清空 session 授权 | — | ⏸ 待定 |
| B2 | `save` 选项不可达（隐藏快捷键） | — | ⏸ 待定 |
| B4 | 模式权限依赖「全局设置+快照拷贝」手工配对 | — | ⏸ 待定 |

## 修复摘要（已落地）

| 缺陷 | 改法 | 验证 |
|---|---|---|
| A5 | `"maximum context length"`/`"reduce the length"` 移入 `prompt_too_long`；补 `context_length`（覆盖下划线形式）与 `prompt is too long`；补 `"timed out"`；新增 `_NON_RETRYABLE_ERROR_PATTERNS` 拦截参数校验错误；判定顺序改为 prompt_too_long → max_output_tokens | 8 用例 **8/8** 正确 |
| A6 | `get_messages()` 新增 `include_compaction_summaries`；`PromptBuilder` 传 `True`，只保留 `metadata["compressed"]` 的系统消息，不重复注入原始系统提示词 | prompt 含「早期对话摘要」；系统提示词仅出现 1 次 |
| A7 | 新增 `SAIAgent._force_compact_session()`：优先 `force_compact`，缺失时降级 `compact` 并记录 `compact_api` | 3 个测试覆盖两条路径 + 异常传播 |
| A3 | 外层循环去掉重复三次的来源与未使用的 `action_type`，改为 3×3 | 优先级语义保持（3 例验证） |
| A1 | `set_session_rules()` 把危险工具 `allow` 降级为 `deny` 并记入 `stripped_dangerous`；`set_tool_permission()` 拒绝写入危险工具 `allow`；CLI 弹窗不为危险工具提供会话级放行 | 4 个回归测试 |
| A2 | `PermissionRuntime.is_in_fallback` + `_apply_fallback()`（只把 allow 升级为 ask，deny 不变）；CLI 进入/退出回退时同步标志位 | 4 个回归测试（含无回调时 fail closed） |

**新增回归测试 37 个**，全量 **410 passed**（原 373 + 37）。

### A4 为何未修（需你决定）

Hook 串行是**当前唯一未改的已证实缺陷**，因为它不是逻辑错误而是执行模型：

- 串行可能是**有意**的 —— hook 之间可能有顺序依赖（例如「先格式化，再检查」），并发执行会静默改变语义。我无法从代码判断原作者意图。
- 改成并发会引入新的复杂度（工作区/权限 ContextVar 在子线程中的传播、失败时的顺序语义），风险高于收益。
- 折中方案：保持串行，但**加进度提示**（"正在执行 PreToolUse hook 2/5…"）。这能消除「以为程序卡死」的体验问题，且不改语义。

需要我按折中方案做，还是保持现状？

---

# A. 已证实缺陷

## A1. `PermissionRuleSet` 的危险工具防护是纯装饰性的 —— 安全机制未接线

- **位置**：`lib/core/permissions.py:40`（`DANGEROUS_TOOLS`）、`:157-236`（`PermissionRuleSet`）、`:487-504`（`PermissionRuntime._decide`）
- **文档承诺**：`:40` 的注释写「特别危险工具集合（可以 allow → force-deny）」；`PermissionRuleSet` docstring 写「stripped_dangerous 记录哪些危险工具的 allow 被强制降级为 deny」；README 亦宣称危险操作拦截。
- **实际行为**：
  - 降级逻辑本身**实现正确**。实测 `set_rule("user", "delete_file", "allow")` 后：
    ```
    always_allow = {'user': {}}
    always_deny  = {'user': {'delete_file': 'deny'}}
    stripped_dangerous = {'user': ['delete_file']}
    get_effective_action("delete_file") -> ('deny', 'user')
    ```
  - **但 `set_rule()` 在整个代码库中从未被调用**（全仓 grep 仅命中定义处）。
  - **且 `_decide()` 完全不读 `rule_set`** —— 只看 `session_rules` 与 `self.policy`。
- **实证绕过**（两条路径都能把 `delete_file` 变成 `allow`）：
  - session 规则：`set_session_permission_rules({"delete_file": "allow"})` → `allowed=True action=allow`
  - 用户策略文件：`set_tool_permission("delete_file", "allow", scope="user")` → 落盘 `{"default":"ask","tools":{"delete_file":"allow"}}` → 判定 `allowed=True`
- **影响**：`delete_file` / `git_push` 除默认值为 `ask` 外**无任何强制保护**。任何能写 `~/.sayacode/permissions.json` 或项目 `.sayacode/permissions.json` 的东西都能静默提权。宣称的 force-deny 是不可达代码。
- **性质**：有实现、没接线 —— 比完全没实现更危险，因为注释让人以为边界存在。

## A2. 「连续拒绝自动回退询问」实际是空操作

- **位置**：`lib/cli/permissions.py:180`（`_denial_tracker`）、`:241-246`（deny 分支）；`lib/core/denial_tracker.py:86-93`；`lib/core/permissions.py:442-485`（`check`）
- **文档承诺**：README「连续拒绝自动回退询问模式」；`denial_tracker.py` docstring「当连续拒绝达到阈值时自动回退到询问模式」；回退时提示「后续操作将逐项询问」。
- **实测**：
  - 计数与阈值判定本身**正确**：3 次 deny 后 `should_fallback=True`，`enter_fallback_mode()` 后 `is_in_fallback=True`。
  - **但 `is_in_fallback` 从未被任何判定代码读取**：`lib/cli/permissions.py` 中该标识出现 **0** 次；`PermissionRuntime.check()` 不知道回退模式存在。
  - 回退模式下实测 `read_file` / `write_file` / `execute_command_tool` 全部 `allowed=True`，**一次询问都未触发**。
- **影响**：回退模式的唯一可见效果是打印一行警告。用户连点 3 次拒绝后，系统行为与之前完全一致。
- **附带问题**：`"once"`（允许一次）也走 `record_success()` 重置连续计数，所以必须恰好连续 3 次拒绝才触发；若回退将来能生效，这个阈值语义需重新评估。

## A5. 错误分类表大面积失准（实测 8 例错 5 例）

- **位置**：`lib/agent.py:45-84`（三张模式表 + `_classify_error` 判定顺序）
- **独立实测**（直接导入 `_classify_error` 构造输入调用）：

  | 场景 | 实测分类 | 期望 | |
  |---|---|---|---|
  | OpenAI 上下文超限（最主流） | `max_output_tokens` | `prompt_too_long` | ❌ |
  | Anthropic `prompt is too long:` | `fatal` | `prompt_too_long` | ❌ |
  | `context_length_exceeded` | `fatal` | `prompt_too_long` | ❌ |
  | `Request timed out` | `fatal` | `recoverable` | ❌ |
  | `Invalid parameter: connection_timeout` | `recoverable` | `fatal` | ❌ |
  | `max_output_tokens exceeded` | `max_output_tokens` | 同 | ✅ |
  | `Input length ... exceeds maximum` | `prompt_too_long` | 同 | ✅ |
  | `Rate limit reached` | `recoverable` | 同 | ✅ |

- **根因**：
  1. `"maximum context length"` 与 `"reduce the length"` 被放进 `_MAX_OUTPUT_TOKENS_PATTERNS`，而 `_classify_error` **先检查该表**（75-77 行先于 78-80 行）。OpenAI 超限错误串同时含 `"maximum context length"` 与 `"context length"`，于是被误判为「输出 token 超限」。
  2. `"prompt too long"`（无 is）匹配不到 `"prompt is too long: ..."`。
  3. `"context length"`（空格）匹配不到 `context_length_exceeded`（下划线）。
  4. `"timeout"` 子串匹配不到 `"timed out"`（空格分隔）。
  5. `"connection"` 子串过宽，把参数校验错误也判为可重试。
- **影响**：真实上下文超限会走「注入续写消息后重试」分支 —— 给**已超限**的 prompt **再加一条 HumanMessage**，方向完全相反；而最主流的两种超限串直接 `fatal`，一次都不重试、不压缩。
- **修复成本**：极低（调整 5 个字符串 + 判定顺序）。

## A6. 压缩生成的摘要永远不会进 prompt —— 压缩等于静默丢弃历史

- **位置**：`lib/core/session.py:474-498`（摘要与边界标记存为 `role="system"`）、`lib/core/agent_runtime.py:133`（`get_messages(include_system=False)`）、`:137-138`（`role == "system"` 分支）
- **独立实测**（12 轮 × 8KB）：

  ```
  压缩前消息对象数: 24
  compact() 返回: '上下文已压缩 (第 1 次): 保留最近 10 轮完整对话 + 2 轮要点 | 使用率 0%'
  压缩后 role=='system' 的对象数: 2
     - '── 上下文压缩 (1) @ 2026-09-14T05:24:26 ──'
     - '--- 早期对话摘要 (2 轮) ---\n1. USER-TURN-0 xxx...'
  ```

  但构建 prompt 用的 `get_messages(include_system=False)` → 20 条，**其中 `role=='system'` 为 0 条**：
  - `PromptBuilder:137` 的 system 分支**不可达**
  - prompt 历史含 `'上下文压缩'` → **False**
  - prompt 历史含 `'摘要'` → **False**
  - prompt 历史含被压缩掉的 `'USER-TURN-0'` → **False**

- **影响**：触发压缩后（70% 阈值，长会话几乎必然触发），Agent 静默忘记用户目标、文件路径、报错历史 —— 正是摘要想保住的东西。**既无原文、也无摘要、且无任何报错**。这是清单里最隐蔽的一条。
- **修复成本**：极低（`include_system=False` 改 `True`，或让摘要用独立 role/字段承载）。

## A3. `PermissionRuleSet.get_effective_action()` 有冗余死循环

- **位置**：`lib/core/permissions.py:197-213`
- **问题**：外层循环把每个来源重复三次（`(SOURCE_SESSION,"deny")`、`(SOURCE_SESSION,"allow")`、`(SOURCE_SESSION,"ask")`），内层又遍历 `always_deny/always_allow/always_ask`。外层携带的 `action_type` 变量**从未被使用**。实际产生 3×3=9 次检查，语义上只需 3 次。
- **影响**：功能正确（返回首个命中），但明显是写错或残留。配合 A1，整个 `PermissionRuleSet` 未接线。

## A4. Hook 串行执行，交互式延迟可达 N×30s 且无进度提示

- **位置**：`lib/core/hooks.py:118-124`（`trigger` 的 for 循环）、`:39-40`（超时常量）、`:291-302`（`subprocess.run`）
- **实测**：注册 5 个 `UserPromptSubmit` 阻塞型 hook（各 3 秒），`trigger()` 耗时 **15.7 秒**，完全串行。
- **边界**：`_extract_hook_timeout()` 把 timeout 钳制在 `[1, MAX_HOOK_TIMEOUT=30]`，最坏 **N × 30s**。
- **影响**：阻塞发生在用户按下回车之后、Agent 开始响应之前；交互式 CLI 全程无进度提示，用户会误以为程序卡死。`PreToolUse` 同理，会让每次工具调用最多等待 N×30s。
- **备注**：hook 是本地脚本，串行可能是为了保持确定性顺序（有依赖的 hook 需要顺序）。属「正确但有延迟风险」，需确认是否加进度提示或并发。

## A7. `force_compact()` 是死代码，恢复路径错用 `compact()`

- **位置**：`lib/core/session.py:346`（定义）；`lib/agent.py:813-820` 与 `:1000-1007`（都调 `self.session.compact()`）
- **证据**：`git grep force_compact -- lib` 只命中定义行 `session.py:346`，**生产代码 0 个调用点**。
- **影响**：`force_compact` 的 docstring 明确写着「当 API 返回 prompt_too_long / context_length_exceeded 时调用」「跳过阈值检查」「更激进减少保留轮次」，但真正处理超限的路径用的是 `compact()`；后者在轮数 ≤ `keep_rounds`（10）时直接 return，且**无条件返回「上下文已压缩」**—— 空操作却谎报成功，重试因此注定同错。

---

# B. 待确认疑点

## B1 / B3. 切 `/mode build` 会清空 session 授权

- **位置**：`lib/core/modes.py:66-78`（build 的 `permission_rules={}`）、`:136-140`；`lib/core/permissions.py:429-440`
- **链条**：`build.permission_rules` 是空 dict → `set_session_permission_rules({})` → `PermissionRuntime.set_session_rules({})` → `self.session_rules = {}`（**赋值，非合并**）。
- **影响**：确认弹窗里选「本次会话始终允许」写入的授权（`lib/cli/permissions.py:230` 调 `update_session_permission_rules`），执行一次 `/mode build` 后全部消失。`plan`/`review` 注入的 `MUTATION_DENY_RULES` 同样被清掉 —— 这是设计意图，但与 session 授权共用同一个 dict，无法区分来源。
- **待确认**：是否有意让模式切换重置所有 session 授权。若是，应显式清除并提示；若否，需按来源分离存储。
- **注意**：`update_session_permission_rules()` 是合并语义，两个函数并存容易误用。

## B2. `save` 选项不可达（隐藏快捷键）

- **位置**：`lib/cli/permissions.py:62-66`（`_CONFIRM_CHOICES` 只有 once/session/deny）、`:117-127`（`"p"` → `"save"`）、`:233-240`（处理 `"save"`）
- **观察**：`"save"` 不在 `_CONFIRM_CHOICES`，UI 从不显示、上下键选不到；但按 `p` 键可触发。提示文案「↑/↓ 切换，Enter 确认；y/a/n 可快速选择」也未提及 `p`。
- **待确认**：有意设计的隐藏快捷键，还是残留。

## B4. 模式权限依赖「全局设置 + 快照拷贝」的手工配对（脆弱耦合，当前无活跃 bug）

- **位置**：`lib/core/permissions.py:615-623`（`create_permission_runtime` 快照拷贝）、`lib/core/modes.py:136-140`（写全局 `_RUNTIME`）、`lib/commands/mode.py:49-54`、`lib/runtime/startup.py:127`
- **机制**：`create_permission_runtime()` 把 `base.session_rules` **快照拷贝**到新 runtime；`apply_agent_mode_permissions()` 只写全局 `_RUNTIME.session_rules`。因此模式规则能否生效，取决于**建 runtime 的时机是否晚于设模式**。
- **实测三种顺序**：

  | 顺序 | 会话 `session_rules` | `write_file` 判定 |
  |---|---|---|
  | 先建 runtime，后设模式 | `{}` | **allow**（模式失效） |
  | 先设模式，后建 runtime | 13 条 deny | deny ✅ |
  | 在 `permission_runtime_session` 内部设模式 | 13 条 deny | deny ✅ |

- **核对全部调用点（仅 2 处），当前都安全**：
  - `lib/runtime/startup.py:127` —— 在第 134 行 `build_context()` 建 runtime **之前**设模式 ✅
  - `lib/commands/mode.py:49-54` —— 设模式后**显式**对会话 runtime 调 `set_session_rules()` ✅（这一行是必需的，不是冗余）
- **结论**：不是活跃 bug，但属**易碎的隐式约定**。建议模式规则改为读取时动态解析（而非快照），或让 `create_permission_runtime` 持有对 base 的引用而非拷贝。

---

# C. 已核对无问题（记录以免重复排查）

- `PermissionPolicy.load()` 分层合并顺序正确：`_policy_paths()` 返回 `[user, project]`，project 后写入覆盖 user，符合「项目级优先于用户级」。
- `decide()` 判定顺序正确：path 规则 → command 规则 → tool 规则 → default。
- `_path_pattern_matches()` 同时比较原样与小写，规避了 `fnmatch` 的大小写行为差异。
- `_normalize_action()` 对非法值返回 fallback，不会把脏值写进策略。
- `summarize_arguments()` 会脱敏敏感 key 并截断到 160 字符，审计日志不会泄露完整参数。
- `permission_workspace_session()` 共享 `audit_log` 列表引用，会话内审计记录不丢失。
- `DenialTracker` 的不可变模式（子 Agent 场景）实现正确，`from_snapshot` 保留父级计数。
- **`hooks.py`**：`blocking` 对 `UserPromptSubmit`/`PreToolUse` 默认生效；超时的 hook **不会**阻塞（`returncode=124` 但 `blocked=False`）；project hook 需显式 `/hooks trust` 才加载，用户 hook 无需信任。
- **`mcp_runtime.py` 的 JSON-RPC 请求匹配是健壮的**：用换行分隔 JSON（非 LSP 的 Content-Length 分帧，符合 MCP stdio 规范）；`_request` 按 id 匹配响应。**实测 8 线程并发 + 乱序响应 → 8/8 正确匹配、零串话**。
- **`lib/models` 导入链**：`anthropic` 的可选依赖用 try/except 保护正确；`registry.create_model` 的 azure 分支参数处理正确。
- **`lib/models/provider_catalog.py`** 的 `PROVIDER_CATALOG` 有 6 家、`registry.list_types()` 有 6 家，两侧一致。

## 已确认的已知问题（前序排查，非本轮新增）

- `lib/models/registry.py:23` 导入不存在的 `factory_models`（被 try/except 吞掉，`GenericOpenAIModel = None`）。功能未坏（`generic` 实际走 `OpenAIModel`），但属死导入。
- `provider_catalog_entry()` 对未知 provider **静默回退到 ollama**，拼错 provider 名不报错。
- `import lib` 会连带动到 `langchain_ollama`（`ollama_model.py` 顶层导入），缺该包时整个包导入失败；而 `anthropic` 有 try/except 保护 —— 保护策略不一致。

---

# D. 汉化范围判定规则

扫描器把「无中文的注释」一律标为待翻，但以下类别**不应翻译**，需人工排除。最终保留 10 处：

| 类别 | 示例 | 理由 |
|---|---|---|
| 纯端点标识 | `# Anthropic API: GET /v1/models/{model_name}` | 是 API 路径 |
| JSON/响应示例片段 | `# "max_input_tokens": 200000}` | 从真实响应摘录 |
| 类型检查器指令 | `# type: ignore[assignment,misc]` | 翻译会破坏语义 |
| 厂商/产品专名 | `# Perplexity`、`# DeepSeek`、`# TGI` | 专有名词 |
| 类型签名 | `# Callable[[List[Dict]], str]` | 类型标注 |
| 取值枚举 | `# "cancel" \| "block"` | 代码字面量取值 |

判断原则：**注释承载的是「标识」还是「说明」**——标识保留，说明汉化。
