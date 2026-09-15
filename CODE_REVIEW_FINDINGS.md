# SAYACODE `lib/` 核心逻辑梳理报告

> **梳理方式**：逐模块读源码 + 运行时实证探测。清单中每条「已证实缺陷」都附有可复现的探测证据。
> **配套变更**：同期完成 `lib/` 全部英文 docstring 与注释的汉化（77 个文件）。两者分开验证：汉化只动注释/docstring（骨架比对证明），逻辑结论来自独立探测。
> **第一轮验证基线**（A1–A7 修复后）：`compileall` exit 0 · `ruff check .` All checks passed · `mypy` 48 modules clean · `pytest` **410 passed**
> **第二轮验证基线**（T1–T4 加固后）：`ruff check .` All checks passed · `mypy` Success: no issues found in 48 source files · `pytest` **574 passed** · 按包覆盖率 `lib` 59.1% / `lib/core` 72.2% / `lib/tools` 51.6% / `lib/models` 50.8% / `lib/cli` 40.2%

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
| B1/B3 | 切 `/mode build` 清空 session 授权 | 中 | ✅ 第二轮已修 |
| B2 | `save` 选项不可达（隐藏快捷键） | 低 | ✅ 第二轮已修 |
| B4 | 模式权限依赖「全局设置+快照拷贝」手工配对 | 中 | ✅ 第二轮已修 |
| **A8** | `PermissionRuleSet` 约九成是死代码（`set_rule`/`get_effective_action`/`to_dict` 全仓 0 调用） | 中 | ✅ 第二轮已修 |
| **A9** | `check_read_only()` / `check_destructive()` 是死方法（全仓含 tests 引用数 0） | 中 | ✅ 第二轮已修 |
| **A10** | `is_destructive` / `requires_confirmation` 是只写不读的装饰性 flag | 中 | ✅ 第二轮已修 |
| **A11** | 策略文件（含 path/command 规则）可给危险工具放行 | 高 | ✅ 第二轮已修 |
| **A12** | `prompt_too_long` 恢复路径静默吞掉压缩失败 | 中 | ✅ 第二轮已修 |
| **A13** | 死导入 `factory_models`、未知 provider 静默回退 ollama、`langchain_ollama` 顶层硬导入 | 中 | ✅ 第二轮已修 |
| **A14** | `is_enabled` / `interrupt_behavior` / `max_result_chars` 仍为描述性字段（无消费方） | 低 | 📝 已记录未修 |

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

---

# 第二轮加固（T1–T4）

第一轮修掉了 A1–A7，但排查中发现**同一类缺陷**（代码宣称做了 X、系统实际没做 X、
且没有任何地方报告这个落差）还有多处。本轮按四层收拾，并把「不静默降级」变成可执行的约束。

## T1 · 同源缺陷残留

| 编号 | 问题 | 证据 | 改法 |
|---|---|---|---|
| A8 | `PermissionRuleSet` 约九成是死代码 | `set_rule` / `get_effective_action` / `has_stripped_dangerous` / `get_stripped_summary` / `to_dict` 全仓 **0 调用**；三张 `always_*` 规则集从未被填充；`_decide()` 从不读 `rule_set` | 删掉整类，只保留真正在用的「危险规则剥离记录」并迁入新的 `SessionPermissionState`。剥离摘要接进 `/permissions`，从死代码变成用户可见信息 |
| A9 | `check_read_only()` / `check_destructive()` 是死方法 | 全仓（含 tests）引用数 **0**；`with_predicates` 同样 0 调用 | 删除，并一并删掉只服务于它们的 `_read_only_predicate` / `_destructive_predicate`。**保留**在用的 `check_concurrency_safe`（`batch_executor` 有 4 个调用点），并为其实测的异常回退补测试 |
| A10 | 两个 flag 只写不读 | `is_destructive` / `requires_confirmation` 唯一写入 `tools/__init__.py:422`（`delete_file`），唯一读取 `tool_search.py:174,176`（**仅用于展示**）；`test_tool_meta.py:16-17` 还把默认值锁死 | 改名 `destructive_hint` / `confirmation_hint`，并在模块 docstring 里明确区分「被生产代码消费」与「仅描述」两类字段。`delete_file` 声明 `confirmation_hint=True` 曾被误读成一道确认门，实际门控来自权限策略的默认 `ask` |
| A11 | 策略文件可给危险工具放行 | `PermissionPolicy` 只归一化取值、不检查危险集合；更严重的是 `_decide_path_rule()` 先于工具规则返回，因此 `paths: {"**": "allow"}` 能直接绕过工具级检查 | `decide()` 统一兜底：任何来源（tools / paths）解析出的 allow，只要工具在 `DANGEROUS_TOOLS` 就降级为 deny 并记入 `stripped_dangerous` |
| A12 | `prompt_too_long` 恢复路径静默吞错 | `agent.py:860` 与 `:1049` 两处 `except Exception: pass` 包住 `_force_compact_session()`；压缩失败后用**原样的超限消息**继续重试，必然同样报错，最终只给用户一个无信息量的失败 | 捕获后写入 `_recovery_state["compact_error"]` 并 `logger.warning`。压缩仍可能失败，但不再无声 |
| A13 | 模型层三处静默降级 | `registry.py:23` 导入不存在的 `factory_models`（被吞成 `None`，`GenericOpenAIModel or OpenAIModel` 永远走后者）；`provider_catalog_entry()` 对未知 provider 静默回退 ollama；`ollama_model.py:8` 顶层硬导入 `langchain_ollama`，而 `anthropic_model` 用 `find_spec` 懒加载 —— 干净环境实测 **28 个 collection error** | 删死导入并直接使用 `OpenAIModel`；未知 provider 抛 `ValueError`（空值仍回退，因为空值表示「未设置」而非「写错」）；ollama 改为与 anthropic 一致的 `find_spec` + 方法内懒导入 |

## T3 · 会话状态与模式权限（B1/B2/B3/B4）

**根因**：`session 授权`（用户在确认弹窗里逐项累加）与 `mode 规则`（`/mode` 注入的只读约束）
**共用同一个 dict**，而 `set_session_rules()` 是赋值语义。于是 `apply_agent_mode_permissions("build")`
用一个空 dict 把会话授权整体清空；`PermissionRuleSet` 那套文档化的「按来源分层」其实是装饰。

**改法**：把真实优先级写进代码与文档。

```
1. mode 规则 deny        ← 硬约束：plan/review 的只读要求不可被绕开
2. session 授权          ← 用户显式授权
3. mode 规则 allow/ask
4. user / project 策略文件（project 覆盖 user）
5. built-in 默认
```

| 项 | 改法 | 验证 |
|---|---|---|
| B1/B3 | 新增 `mode_rules` 与 `session_rules` 两个独立存储；`set_session_rules` 保留赋值语义但只服务于会话授权，mode 走新的 `set_mode_rules()` | `test_mode_switch_preserves_session_grants`、`test_mode_rules_and_session_rules_are_separate_stores` |
| B1/B3 加强 | 第 1 条优先级使 `update_session_permission_rules({"write_file": "allow"})` **也无法**绕开 plan 只读（旧结构下可以绕） | `test_session_allow_cannot_beat_mode_deny` |
| B4 | `SessionPermissionState` 由同进程内所有 runtime **共享引用**（原来是快照拷贝）。会话状态属于会话而非工作区，共享后「先设模式还是先建 runtime」不再影响结果 | `test_mode_application_is_order_independent`（两种顺序各测一次） |
| B2 | `save` 加回 `_CONFIRM_CHOICES`（i18n 文案 `permission.allow_permanent` 本来就存在，只是没接线）；快捷键补 `s` | `test_save_choice_is_visible_in_ui`、`test_every_choice_has_an_i18n_label` |
| B2 连带 | **让 save 可达后暴露出一个潜在崩溃**：危险工具选 save 时 `set_tool_permission` 抛 `ValueError`，而 project→user 的回退同样会抛，异常未被捕获。已显式拦截 | `test_dangerous_tool_save_does_not_crash_and_stays_gated` |

## T2 · 测试盲区（覆盖率实证）

`pytest` 从 410 → **574 passed**，新增 164 个用例。

| 新增文件 / 用例 | 覆盖的盲区 |
|---|---|
| `tests/test_permission_invariants.py` | **危险工具不变量全枚举**：2 个危险工具 × session/mode/policy 各 4 种状态 = 128 个组合，断言「策略层绝不产出 allow」；另加「无回调必须 fail closed」与「回调放行时动作必须仍是 `ask`」 |
| 同上 | B1/B3/B4 的回归用例（模式与授权隔离、顺序无关、会话 allow 压不过 mode deny） |
| `tests/test_cli_permission_choices.py` | `save` 可达性、i18n 文案齐备、快捷键映射、危险工具 save 不崩溃 |
| `tests/test_provider_optional_deps.py` | 未知 provider 抛错、空值回退、ollama 缺依赖时抛 `ImportError`、**AST 断言模块顶层没有厂商导入** |
| `tests/test_tool_meta.py`（扩充） | 并发谓词的异常回退路径（含「回退必须留日志」与「回退目标是自身静态值」），以及「内置工具全部使用静态值」的现状记录 |
| `tests/test_agent_turn_state.py`（扩充） | `max_output_tokens` 恢复（注入续写消息）、`prompt_too_long` 恢复（确实走 force_compact）、压缩失败必须被记录 |
| `tests/test_permissions.py`（修正） | 原 `test_path_rule_can_allow_specific_path` 用 `delete_file` 演示 path 规则，**等于把 A11 漏洞写成了测试**；改为用 `write_file`，并新增「path 规则不得放行危险工具」 |

### 覆盖率变化

| 包 | 第一轮后 | 第二轮后 |
|---|---|---|
| `lib` | 58.0% | 59.1% |
| `lib/core` | — | 72.2% |
| `lib/tools` | — | 51.6% |
| `lib/models` | — | 50.8% |
| `lib/cli` | — | 40.2% |

安全机制本身覆盖最好（`lib/core/permissions.py` 85%、`denial_tracker.py` 98%、
`runtime/startup.py` 99%）；**最大盲区是 `lib/tools/*` 的实现层**（`file_tools.py` 17%、`git_tools.py` 23%），
这也是后续投入回报最高的地方。

## T4 · 工程配置与覆盖率门禁

- **ruff**：新增 `[tool.ruff]`（`line-length = 88`、`target-version = "py311"`、`select = ["E4","E7","E9","F"]`）。
  声明的是与当前默认值**等价**的配置，行为不变，但意图被记录了 —— 此前 lint 行为完全由 pin 住的
  `ruff==0.15.15` 决定，升级 pin 会静默改变规则集。
- **pytest**：新增 `[tool.pytest.ini_options]`（`testpaths`、`timeout = 60`），把原先只写在 CI 命令里的约束落到配置。
- **覆盖率门禁**：新增 `scripts/check_coverage.py`，跑一次带覆盖率的 pytest 并**按包**校验门槛
  （`lib/core` ≥ 69%、`lib/tools` ≥ 48%、`lib/models` ≥ 47%、`lib/cli` ≥ 37%、`lib` ≥ 56%）。
  门槛取实测值减 3 个点：既阻断明显退化，又容忍 Windows/Linux 平台条件分支带来的波动。
  CI 用它替换原来的裸 `pytest -x` 步骤，因此 pytest 执行次数不变。
- **更正我先前的判断**：`[tool.mypy]` **本来就是声明过的**（`files` 白名单 + `follow_imports = "skip"`
  + `ignore_missing_imports` + `check_untyped_defs`），缺的只是 ruff 与 pytest 两节。
  `dev` 依赖新增 `pytest-cov`。

## A14 · 记录但未修：其余描述性字段

`ToolMeta` 上还有三个字段只有写入方、没有任何消费方：

| 字段 | 现状 |
|---|---|
| `is_enabled` | 注册时可传 False，但没有任何代码读取它来禁用工具 |
| `interrupt_behavior` | 声明 `"cancel" \| "block"`，无消费方 |
| `max_result_chars` | 注释称「结果超过此大小时写盘」，实际无消费方；`read_file` 注册时写 `float("inf")` 表示不限 |

**未修的理由**：它们不像 `requires_confirmation` 那样会被误读成安全护栏（A10 已处理），
而且实现它们属于**新增功能**（结果落盘、工具禁用）而非修缺陷。
已在 `tool_meta.py` 模块 docstring 中标注为「仅描述性」，避免下一个人误以为存在。
若要实现，`max_result_chars` 是三者中唯一有明确产品价值的。

---

# A4 为何仍未修（唯一残留的已证实缺陷）

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

# B. 待确认疑点（第二轮已全部处理，以下保留原始分析）

> B1/B3、B2、B4 均已在第二轮改掉，改法与验证见上文「T3 · 会话状态与模式权限」。
> 本节保留当时的分析过程，便于回溯判断依据。

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

以下三条已在第二轮作为 A13 修复，保留原始描述以便回溯：

- `lib/models/registry.py:23` 导入不存在的 `factory_models`（被 try/except 吞掉，`GenericOpenAIModel = None`）。功能未坏（`generic` 实际走 `OpenAIModel`），但属死导入。→ **已删除该导入与 `or OpenAIModel` 分叉，直接用 `OpenAIModel`。**
- `provider_catalog_entry()` 对未知 provider **静默回退到 ollama**，拼错 provider 名不报错。→ **已改为抛 `ValueError`（空值仍回退）。**
- `import lib` 会连带动到 `langchain_ollama`（`ollama_model.py` 顶层导入），缺该包时整个包导入失败；而 `anthropic` 有 try/except 保护 —— 保护策略不一致。→ **已改为与 anthropic 一致的 `find_spec` + 方法内懒导入，并加了 AST 断言防止回归。**

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

---

# 第三轮：模型层重写（声明式通用接入）

`lib/models` 从 **3,196 行 / 9 文件**重写为 **1,915 行 / 9 文件**（**-40%**），
删除 5 个旧实现文件共 2,146 行。

## 结果

| 指标 | 重写前 | 重写后 |
|---|---|---|
| `lib/models` 行数 | 3,196 | **1,915** |
| `lib/models` 覆盖率 | 50.8% | **64.8%** |
| `lib` 覆盖率 | 59.1% | **60.3%** |
| `pytest` | 574 passed | **625 passed** |
| 新增一个 provider 的成本 | 写一个类（约 100 行） | **目录里加一条数据** |
| 新增一个 wire 协议的成本 | 写一份完整实现 | **`PROTOCOL_SPECS` 加一行 + 两行类** |

覆盖率门禁同步上调（`lib/models` 47% → 61%，`lib` 56% → 57%），
否则这次提升不会被保护住。

## 新结构

| 模块 | 行数 | 职责 |
|---|---|---|
| `provider_catalog.py` | 269 | **声明式纯数据目录**：7 个 provider 的 protocol / 端点 / 凭据环境变量 / 模型表 / 兼容开关 |
| `registry.py` | 336 | 目录驱动的注册表（默认 spec 是对目录的一次遍历） |
| `extras.py` | 324 | `ModelExtras`：上下文窗口 / 用量 / 消息转换 / `chat` / `check_connection` |
| `providers.py` | 264 | 6 个协议薄类 + `PROTOCOL_SPECS` 表（每类只声明一个 `WIRE_PROTOCOL`） |
| `probing.py` | 239 | 协议键控的上下文窗口探测表 |
| `compat.py` | 206 | 非标准字段双向透传 + 声明式兼容开关 |
| `vocabulary.py` | 129 | 共享词汇表（打断 `extras` ↔ `base` 循环导入） |
| `base.py` | **96**（原 470） | 自有传输基类 + 词汇表再导出 |
| `__init__.py` | 52 | 包导出面 |

**删除**：`openai_model.py`(494)、`gemini_model.py`(653)、`ollama_model.py`(397)、
`anthropic_model.py`(369)、`universal_chat_openai.py`(233)。

## 去重与死代码清理（重写顺带完成）

| 逻辑 | 旧状态 | 新状态 |
|---|---|---|
| `_estimate_usage_from_text` | **4 份**重复 | 1 份 |
| `chat` / `chat_stream` | **4 份**各自实现 | 1 份 |
| `check_connection` | **4 份**各自实现 | 1 份 |
| `chat_with_tools` | **4 份**重复，生产代码**零调用** | 已删 |
| `StreamingMixin` | 全仓**零引用** | 已删 |
| DeepSeek `base_url` 嗅探 | 靠 URL 猜测 | 已删（升为目录一等条目） |
| Gemini 手写 REST | 653 行 | 已删（改用 `langchain-google-genai`） |

## 记录在案的行为差异（三处，均有实测依据）

1. **`validate_temperature` → `clamp_temperature`。**
   实测 `langchain_openai` 自己在 `BaseChatOpenAI` 上就定义了 `validate_temperature`，
   并由 pydantic 的 `validate_<字段名>` 约定注册为 `temperature` 字段的校验器。
   本层 mixin 在 MRO 中位于厂商类之前，同名方法会**覆盖**该校验器，导致签名不匹配
   （校验器收到 `ValidationInfo` 而非 float），**模型构造直接失败**。实测实例方法 /
   staticmethod / property 三种写法都会覆盖它，故只能改名。
   依据：该成员生产代码**零调用**，能力完整保留在 `clamp_temperature`；
   旧名保留在 `BaseModel`（纯 Python，无厂商校验器冲突）。

2. **构造期即要求凭据。** 官方集成（尤其 Gemini）在**构造期**校验 API key，
   旧手写实现是延迟到调用期。属「早失败优于晚失败」，有意保留。
   连带影响：`OllamaModel(model_name=...)` 直接构造时 `base_url` 为 `None`
   （由上游内部解析），默认端点的唯一来源是**目录**。

3. **`generic` 的 `model_type`。** 旧实现复用 `OpenAIModel` 故返回 `"openai"`；
   现在按协议声明返回 `"generic"`——两者都是实现细节，冻结测试特意不锁这条。

## 验证方法：契约冻结

```
1. 先写 tests/test_model_contract.py（66 项），对着【旧实现】跑绿  ← 安全网
2. 替换实现
3. 同一套测试必须继续全绿；该文件只使用稳定公共面（lib.models / provider_catalog），
   不导入任何实现模块，因此实现可被整体替换
4. 调整过的断言逐条说明理由（见上「行为差异」）
```

**测试迁移分两类处理**：

* **行为断言** → 只改导入路径（`test_model_registry.py`、`test_provider_optional_deps.py`）。
* **实现断言** → 重写或删除并说明理由。删除的集中在本轮开头：整个 `TestGeminiModel`
  （18 项，断言手写 REST 的 `_build_url` / `_build_payload` / `_convert_tools_to_gemini`
  等私有方法）、DeepSeek 嗅探测试、以及 `test_import_failure_prints_hint`
  （可选依赖保护已移到 `test_provider_optional_deps.py`，并加了 AST 门禁断言厂商导入
  必须在 `try/except` 内）。

## 三个技术坑（实测，不写下来就会再踩）

1. **pydantic 私有属性与普通 mixin 不兼容。** 把 `PrivateAttr` 声明在普通 mixin 上，
   初始读取返回**描述符对象本身**；声明在具体 pydantic 类上，setter 赋值后读回仍是默认值。
   → 全部状态改用 `self.__dict__` 存取（写入用 `__dict__[...]`，读取用 `.get()`）。

2. **类级声明必须 `ClassVar` + 无下划线前缀。** 无下划线不注解 → pydantic 直接报错要求
   `ClassVar`；下划线 → 被当私有属性**静默接管**，子类覆盖失效（第一版 `model_type`
   读出来是空字符串）。

3. **约束催生更好的设计。** 既然每类都得声明，就把它收敛成「每类一个 `WIRE_PROTOCOL`
   + 一张 `PROTOCOL_SPECS` 表」——比逐类重复 6 行声明更声明式。

## 未做的相邻清理（有意留出，避免混淆变更集）

* `langchain-community` 与 `httpx` 声明了但全仓**零引用**，可删。
* LangChain 系依赖只写了过时的下限（声明 `langchain-core>=0.3.0`，实测跑在 `1.4.8` 上），
  且无上限约束。新增的 `langchain-google-genai>=4.0.0` 按实测版本写下限，未沿用这种写法。

---

# 第四轮：真实链路测试（并因此修掉 3 个缺陷）

重写完成时 625 个测试全绿，但**全部是 mock**。用本机已保存的真实 profile 与一个
本地假厂商（真说 OpenAI 协议、真实 socket、真实 LangChain 客户端）做端到端测试后，
暴露出 **3 个 mock 与契约测试都覆盖不到的缺陷**——其中 2 个是重写引入的回归，
1 个是继承自旧实现的既有缺陷。

## E1 · `create_from_config` 把 harness 配置键透传进请求体（重写引入，致命）

保存的 profile 里带着 `azure_api_version: None` / `azure_deployment: None` /
`metadata: {}`。新实现在去掉各家的过滤 skip-list 后把它们**原样**传给 LangChain 类，
LangChain 把它们收进 `model_kwargs`，最终**作为请求体参数发给厂商**：

```
TypeError: Completions.create() got an unexpected keyword argument 'azure_api_version'
```

影响：**每一次真实调用都失败**，即模型层对本机标准配置完全不可用。旧实现每家都有自己的
过滤名单，重写时丢掉了。

**修法**：`create_model` 丢弃取值为 `None` 的额外参数（`None` 表示「未设置」）；
非 Azure 协议显式剥掉 `azure_*` / `api_version`；`create_from_config` 用
`_CONFIG_ONLY_KEYS` 排除属于 harness 配置层、而非模型构造参数的键（`metadata` 等）。

## E2 · `chat_stream` 把空 content 的 chunk 对象 repr 当文本产出（重写引入）

旧代码的判据是 `if hasattr(chunk, 'content'):`（空 content 就什么都不做）；
重写时改成 `content = getattr(...)` + `if content:`，导致空 content 落进
`elif chunk:` 分支，把整个 chunk 对象的 `repr` 当成回复文本 yield 出去。

**修法**：判据改回「有没有 `content` 属性」，并把原因写进注释。

## E3 · 流式用量取自最后一个块（继承自旧实现的既有缺陷）

用量块**不是**最后一个块——其后通常还有一个只带 `chunk_position='last'` 的空收尾块。
只看最后一块会拿不到 usage，**静默退化成按字符数估算**：实测真实用量 `7/3/10`
被估成 `3/2/5`（少报一半以上）。

**修法**：记住最后一个**带用量**的块；只有上游确实不报用量时才估算。

## 新增的永久保护

| 文件 | 内容 |
|---|---|
| `tests/test_model_wire.py` | 8 项**真实 HTTP 集成测试**：本地假厂商 + 真实客户端，断言请求体形状、SSE 解析、工具调用解析、非标准字段双向透传、用量提取、探测、以及 E1 的回归。只依赖回环地址 |
| `tests/test_model_streaming.py` | 3 项流式回归：不产出 chunk repr、用量取自用量块、无用量时才估算 |
| `tests/test_model_contract.py` | 新增 `create_from_config` 不污染请求体的断言（E1） |

覆盖率因此从 `lib/models` 64.8% 升到 **79.0%**，门槛同步上调到 76%。

## 真实端点的结论

本机 profile 的调用现在能**正确抵达厂商**（请求体干净、认证通过、路由正确），
唯一阻塞是账户余额：

```
openai.APIStatusError: Error code: 402 - {'error': {'message': 'Insufficient Balance', ...}}
```

402 而非 400/401，说明**请求格式与认证都是对的**，被拒的原因是账单。
**因此「能否拿到真实模型回复」仍未验证**——这需要账户充值后再跑一次。

## 教训（写给下一次）

> mock 测试全绿**不等于**能用。这次 625 个测试通过的情况下，模型层对本机标准
> profile 是**完全不可用**的。原因是 mock 绕过了唯一会出问题的环节：请求体的
> 实际形状。凡是「跨进程序列化边界」的代码，都应有至少一条走真实序列化的测试。

---

# 第五轮：独立对抗性审查 + 全修

用户要求 review。这一轮不是再写代码，而是**先审自己的代码**——并把 T1–T4 那批
未提交改动拆给两个并行子代理做独立审查（各自用「实际 revert 后测试是否失败」来
验证每条测试是不是真的回归测试）。

## 0. 最重要的元教训：我的「真实链路测试」复现了它本该消除的盲区

第四轮我声称 `tests/test_model_wire.py` 用真实 HTTP 抓住了 mock 抓不到的问题。
它确实抓到了 E2/E3，**但它自己的假厂商从不校验 `Authorization` 头**（对任何请求
无条件回 200）。于是它验证了「URL 形状」，没验证「凭据有没有正确传下去」——
正是它存在的理由。结果就是一个让上下文窗口自动探测在所有 provider 上彻底失效
的回归，在这套测试下**全绿通过**。

> **真实 HTTP ≠ 真实契约。** 假服务器至少要校验真服务器校验的输入（最低限度：认证），
> 否则它只是一个绕了远路的 mock。

这一轮把「假服务器校验 Bearer」固化进 `test_model_wire.py`，并加了一条
`test_probe_fails_with_a_rejected_key` **专门证明假服务器真的在校验**——
否则那条探测断言本身是空的。

## 1. 模型层（M1–M8）

### M1 · 严重 · 上下文窗口探测在所有 provider 上失效（重写引入的回归）

`providers.py` 的 `_resolved_api_key()` 直接 `str()` 了 pydantic 的 `SecretStr`
字段，而 `str(SecretStr("k")) == '**********'`。探测请求于是带着**掩码**出去：

| 证据 | 结果 |
|---|---|
| 强制校验 Bearer 的本地端点（暴露 `max_model_len`） | 修复前 `detect_context_window() -> None`；修复后 `-> 200000` |
| **真实** DeepSeek 端点，真 Key | `GET /models/{id}` → **200** |
| **真实** DeepSeek 端点，掩码 | → **401** `Your api key: ******** is invalid` |

旧实现把密钥存成普通 `str` 直接拼进请求头，因此是正确的 —— 这是纯回归。

**影响面 4 处**：`api_config/wizard.py:185`、`cli/configure.py:94` 与 `:408`、
`extras.py:280`（`check_connection`）。**最严重的是非交互模式**：
`configure.py:149` 在探测失败且配置无窗口值时 `raise RuntimeError`，而
`cli/headless.py:135` 正是 `interactive_input=False` —— 对本地 vLLM/TGI 用户
（`_OPENAI_COMPATIBLE_FIELDS` 就是为他们写的）从「能自动探测」变成「直接崩」。

**修法**：调用 `get_secret_value()` 解包。

### M2 · 中 · 直接构造时 `context_window=` 既被丢弃、又污染请求体

```
OpenAIModel(model="m", api_key="k", context_window=12345)
# .context_window == 0                        ← 属性静默忽略
# model_kwargs == {'context_window': 12345}
# _get_request_payload(...) 多出 {'context_window': 12345}  ← 会发给厂商
```

这是 E1 的失效模式在 `providers.py` 文档明确推荐的直接构造路径上**原样复活**。

**修法**：在 `_ProtocolModel.__init__` 里于交给 pydantic **之前**消化掉
`context_window`，并在同时给出 `model` 与 `model_name` 时丢弃后者。
守卫从此落在协议类自己身上，而不是「注册表顺风路径」上。

### M3 · 中 · 「声明式兼容开关」整体是装饰性的，而且正好反了

| 开关 | 谁读它 |
|---|---|
| `passthrough_nonstandard` | **只有** `tests/test_model_providers.py`（断言目录常量等于它自己） |
| `thinking_format` | **只有**同一条同义反复断言 |
| `system_role` / `max_tokens_field` / `supports_max_output_tokens` | `apply_compat_to_payload` 真读，但目录 7 个条目**全部取默认值** |

即：**打开的两个开关没人读，有人读的三个开关没人打开**。`provider_catalog.py`
还写着「加 `compat=CompatSwitches(passthrough_nonstandard=True)` 即可生效」——
这句话是错的，它本来就一直开着。

**这就是同一轮刚被记录为缺陷的 A14 模式在新代码里原样重现。**

**修法**：让开关真的驱动行为 ——

* `passthrough_nonstandard` 成为**总闸**：关闭时 mixin 两个钩子都不动作；
* 删除 `thinking_format`（没有任何可诚实定义的行为），改为
  **`extra_passthrough_fields: Tuple[str, ...]`**：声明式补充厂商特有字段名，
  提取与回填两个方向都生效。这终于让 `provider_catalog.py` 那句「网关差异用
  数据表达、无需新代码」变成真的；
* 字段集合由声明决定，**声明之外的一律不搬**（`model_extra` 里计划外的键
  也不再被顺手带走）；
* 新增 `tests/test_model_compat.py`：每个开关一条「打开它 → 行为改变」的用例，
  外加一条 **AST 元测试**断言每个 `CompatSwitches` 字段都在 `compat.py` 里有
  读取点。任何装饰性开关都会立刻红灯。

### M4–M8 · 低

| # | 问题 | 修法 |
|---|---|---|
| M4 | `context_window = 0/None` 无法清空，一旦设过就回不到「未知」 | `None` 显式清空；非法值仍忽略 |
| M5 | `create_model(model_name="a", model="b")` → `TypeError: got multiple values for keyword argument 'model'` | 无条件摘掉 `model`；显式参数优先 |
| M6 | `compat` 是 pydantic 字段，可被构造参数覆盖 | 保留为字段（声明式的一部分），文档说明「工厂注入总是覆盖构造默认值」 |
| M7 | `registry.py` 对 `model_name` 的处理是死代码 | 随 M5 一并删除 |
| M8 | AST 门禁只断言「模块名出现在文件里某个 try 内」 | 改为检查保护**形状**：模块顶层 try + 捕获 `ImportError` + 设置降级值 |

## 2. 权限层（F1–F10）

审查结论：A1 与 A11 **确实修好了**，但危险底线**按规则 key 判定而非解析后的工具名**，
因此留下两个绕过口子。

| # | 问题 | 修法 |
|---|---|---|
| F1 | `set_session_rules({"delete_*":"allow"})` → `delete_file` 被放行 | 危险底线**下移到唯一决策出口**（`_decide` → `_apply_dangerous_floor`），按**解析后的工具名**判定 |
| F2 | `session.session_rules["delete_file"]="allow"` 等裸写绕过全部检查 | 同一处出口兜住（F1 与 F2 同根因）；setter 也走归一化 |
| F3 | 路径 `allow` 压过显式 `tools: deny`（既有缺陷） | 策略层顺序改为 **显式 tools deny > commands > paths > tools > default** |
| F4 | `PermissionRuntime()` 默认构造**私有** session（fail-open），而模块文档声称「所有 runtime 共享一个实例」 | 默认共享进程级 `SessionPermissionState`，文档变成真的 |
| F5 | 会话授权**进程内无法撤销**（无 CLI 入口） | 新增 `clear_session_rules()` + `/permissions reset`（别名 `clear`），保留 mode 规则 |
| F8 | `stripped_dangerous` 不随 reset 清空 → `/permissions` 打印过期提示 | reset 时一并清除 |
| F9 | 策略文件 `tools` 的 glob 不生效，但 `/permissions` 显示为生效 | 策略层 glob 匹配；策略 key 压过内置精确默认值（`git_*: deny` 不再被内置 `git_push: ask` 遮蔽） |
| F10 | `PermissionRuleSet` 等公开面被删无 shim | 确认无仓库内消费者，保持删除；一致性由测试守卫 |

**独立复核**：上述每一条都由主 Agent 用独立探针重跑验证（不依赖子代理自述），
包括「非危险工具的 `allow` 仍然被尊重」这条**反向**断言——避免修 F1/F2 时过度封锁。

复核过程中我自己踩了一个命名坑：`reset_session_permission_rules()` 与
`PermissionRuntime.clear_session_rules()` 名字太近、契约却不同（前者连 mode 规则
一起清，供测试隔离；后者保留 mode 规则，是面向用户的撤销入口）。已在两处
docstring 互相交叉引用说明，避免下次误用。

## 3. tool_meta / agent（F1–F9）

审查确认 **A14 属实**（`is_enabled` / `interrupt_behavior` / `max_result_chars`
确实零消费者，含动态逃逸检查）与 **A4 属实**。

| # | 问题 | 修法 |
|---|---|---|
| F1 | `agent.py` 的 `_recovery_state["compact_error"]` 是**只写不读**的死状态，而注释声称用户不会只看到一个没有信息量的错误 | 抽出 `_format_execution_error()`，把压缩失败原因附进最终错误；注释改成描述真实行为 |
| F2 | `destructive_hint`/`confirmation_hint` 的「供 ToolSearch 展示」是假的（唯一读取者 `_get_tool_detail` 零调用方） | 改为如实文档化（与 A14 的决定一致）；不往模型可见的搜索输出里注入新文本 |
| F3 | 新增的 `logger.debug` 在本应用完全不可见（全仓库无 logging 配置） | 提升到 `warning`（谓词损坏是真实异常）；文档说明只有 WARNING+ 能经 `lastResort` 到达 stderr |
| F4 | `max_result_chars` 注释说「0 表示无限制」，实际用 `float("inf")` | 注释与实现对齐 |
| F5 | `tests/test_mcp_runtime.py` 的改动在修复前也通过（拒绝串逐字节相同） | 改为断言规则落在 `mode_rules` 而非 `session_rules`，并验证 `clear_mode_rules()` 后恢复 |
| F6 | 3 个新 agent 测试里 2 个在 revert 后仍通过 | 明确标注为**覆盖率测试**并指向真正的回归测试 |
| F8 | ToolMeta 新测试多数只锁现状（且断言私有字段） | 保留唯一由新代码引起的用例；删除锁现状的 `test_no_builtin_tool_uses_functional_predicate` |
| F9 | 公开面删除未记录 | 在模块 docstring 记录 `is_destructive`/`requires_confirmation`/`check_read_only`/`check_destructive`/`with_predicates` 的移除 |

## 4. 测试质量的元结论

三个审查各自用「实际 revert 后测试是否失败」验证，结果至少 **6 条**被标为
「回归测试」的用例在 revert 后**仍然通过**：

| 测试 | revert 后 |
|---|---|
| `test_model_wire.py::test_detect_context_window_...` | 仍通过（假厂商不校验 auth） |
| `test_agent_turn_state.py` 3 个新测试中的 2 个 | 仍通过 |
| `test_mcp_runtime.py` 的改动 | 仍通过（拒绝串逐字节相同） |
| `test_permission_invariants.py:90-102` | 仍通过（不守卫它命名的那个修复） |
| `test_permission_invariants.py:163-181` | 仍通过 |
| `test_runtime_context_tools.py` 策略改动 | 仍通过（拿掉危险底线也能过） |

全部已改造成 revert 敏感，并逐条用「临时回退 → 必须失败 → 恢复 → 必须通过」
验证过（两个子代理各自做了 10 组与多组变异实验，文件按 sha256 校验恢复）。
这说明：**「加了测试」和「加了回归保护」是两件事**，只有后者能被 revert 实验证明。

## 5. 最终验证

| 检查 | 结果 |
|---|---|
| `pytest` | **707 passed**（第五轮前 636） |
| `ruff check lib tests scripts run.py` | All checks passed |
| `mypy`（项目配置门禁，48 文件） | Success: no issues found |
| 覆盖率门禁 | 全过，且门槛已同步上调 |
| `scripts/check_release.py` | Release checks passed |
| 覆盖率 | `lib` **61.9%** · `lib/core` **72.6%** · `lib/models` **80.1%** · `lib/tools` **52.3%** · `lib/cli` **40.2%** |
| 真实端点 | 明文密钥 → `GET /models/{id}` **200**（修复前掩码 → 401） |

门槛上调对照（实测值 − 约 1 点，保留平台分支波动余量）：

| 包 | 旧门槛 | 新门槛 | 实测 |
|---|---|---|---|
| `lib` | 58.0 | **60.0** | 61.9 |
| `lib/core` | 69.0 | **71.0** | 72.6 |
| `lib/models` | 76.0 | **79.0** | 80.1 |
| `lib/tools` | 48.0 | **51.0** | 52.3 |
| `lib/cli` | 37.0 | **39.0** | 40.2 |

## 6. 仍然开放（记录，未修）

* **`lib/models` 不在 `[tool.mypy] files` 里**，因此该包不受类型门禁保护 ——
  这也是 `_ProtocolModel.__init__` 覆盖未被类型检查发现的原因之一。
  要么把 `lib/models` 纳入门禁，要么在文档里写明「本包不做类型检查」。
* **`lib/agent.py` 的轮级错误文案是硬编码中文**（`执行出错:`、
  `所有恢复路径均已耗尽`），而应用有 `tr()` 双语层 —— 英文环境下用户会看到中文。
  这是跨模块的既有缺口；修它需要连带改测试里的中文断言，作为独立改动处理。
* **`~/.sayacode/api_configs.json` 明文存密钥** —— 向导已明确警告用户
  （`wizard.api_key_storage_warning`），属已知设计而非缺陷。
* **真实模型回复仍未验证**：本机唯一可用账户余额不足（402）。修复后请求能正确
  抵达厂商（402 而非 400/401），但「能否拿到真实补全」需要充值后再跑一次。
* `web_search` 在本机因 402 不可用，LangChain provider 数量的外部核对仍未做。
* **自定义 OpenAI 兼容端点在向导里选不到**（`generic` 的 `visible=False`，
  且 `--model-type` 用 `choices=USER_VISIBLE_MODEL_TYPES` 直接拒绝它）——
  详见下文 7.1。

---

# 第五轮补充：真实链路接入（Command Code）

第五轮结束时「真实模型回复」仍是未验证项（唯一账户 402）。随后用本机已有的
Command Code 凭据完成了**第一次真正的端到端验证**。

## 7.1 供应商定义属于用户配置，不属于仓库

第一版我把 `commandcode` 作为一等条目**硬编码进 `lib/models/provider_catalog.py`**
（连 `APIType` 成员与 i18n 键一起加）。这是错的，已被指出并**全部撤回**：

> 一个具体的第三方供应商是**用户的**配置事实，不是项目的源码事实。
> 把它写进仓库等于替所有用户做一个他们没做的选择。

正确的落点是既有机制，**零代码改动**：

| 机制 | 位置 |
|---|---|
| `generic` 供应商条目 | 「自定义 OpenAI 兼容端点」，`protocol=openai` + `requires_base_url=True` |
| 用户 profile | `~/.sayacode/api_configs.json`（`api_type: "generic"` + 真实 base_url） |
| 命令行覆盖 | `--model-type / --model-name / --base-url / --api-key / --context-window` |

**由此暴露一个真实缺口（记录，未修）**：`generic` 的 `visible=False`，
而 `--model-type` 的 `choices=USER_VISIBLE_MODEL_TYPES`，因此「自定义 OpenAI 兼容
端点」在**交互式向导里选不到**，`--model-type generic` 也会被 argparse 拒绝。
用户只能手写 profile 或退而用 `--model-type openai --base-url ...`。
这是一个值得单独决策的产品问题 —— 但答案绝不是「给每个供应商加一条源码条目」。

## 7.2 接入结果：**真实模型回复拿到了**

profile：`commandcode-goat` → `api_type: generic`，
`base_url=https://api.commandcode.ai/provider/v1`，
`model_name=deepseek/deepseek-v4.1-flash`。全部走 saycode 自己的模型层：

| 能力 | 真实结果 |
|---|---|
| `chat()` | `'PONG'`，`usage=TokenUsage(36/11/47)` |
| `chat_stream()` | `'1, 2, 3, 4, 5.'`，14 个 chunk，**真实用量 74/64/138**（E3 的修法在真实 SSE 上成立） |
| `bind_tools()` | 真实工具调用 `get_weather(city="Paris")` 解析成功 |
| `model_kwargs` | `{}`（E1 的修法在真实 profile 上成立） |
| `detect_context_window()` | `1000000`，`source='api'`（**M1 的修法在真实端点上的验收**） |

`reasoning_details` 也被真实返回，内容是真实推理文本：

```json
[{"type":"reasoning.text","text":"We need answer. ... 17*23 = 391. ...","format":"unknown","index":0}]
```

并且实测该网关**接受**回填（透传 ON / OFF 两轮都 200），即
`passthrough_nonstandard` 的两个位置在真实链路上都成立 —— 这是 M3 把开关做成
真开关之后第一次拿到真实世界的证据。

## 7.3 真实链路又逼出一个缺陷：探测只认 per-model 路由

`probe_openai_compatible` 原本只请求 `GET {base}/models/{model}`。
实测该网关：

| 请求 | 结果 |
|---|---|
| `GET /provider/v1/models/deepseek/deepseek-v4.1-flash` | **404**（无 per-model 路由） |
| `GET /provider/v1/models` | **200**，且**每个条目都带 `context_length`** |

即：对「只在列表里给窗口」的网关，自动探测永远失效；而「探测失败 = 未知」是
设计行为，用户只会看到「请手动输入」而不知原因 —— 又一个**静默失败**。

**修法**（`lib/models/probing.py`）：

1. 仍先试 per-model 路由；模型名按**单一路径段**百分号编码
   （`deepseek/deepseek-v4.1-flash` 含 `/`，不编码会被当成多级路径）；
2. 拿不到结果时退化到 `GET {base}/models`，按 `id`/`model`/`name` 找到条目再搜字段。

**回归保护**（`tests/test_model_wire.py`，假厂商新增两种网关形状）：

| 测试 | 断言 |
|---|---|
| `test_probe_prefers_the_per_model_route` | per-model 可用时**不得**请求列表（顺序是契约） |
| `test_probe_falls_back_to_the_models_list` | per-model 404 → 走列表并找到条目 |
| `test_probe_falls_back_when_per_model_has_no_window_field` | per-model 返回 200 但无窗口字段 → 仍走列表 |

三条都用「临时关掉回退 → 2 条必须失败 → 恢复 → 全过」验证过。

## 7.4 补充后的最终验证

| 检查 | 结果 |
|---|---|
| `pytest` | **710 passed** |
| `ruff check lib tests scripts run.py` | All checks passed |
| `mypy`（项目门禁） | Success: no issues found |
| `scripts/check_release.py` | Release checks passed |
| 真实模型回复 | **已拿到**（非流式 / 流式 / 工具调用 / 窗口探测全部通过） |

仓库内**没有**任何供应商专属的硬编码：`git grep -i commandcode` 在 `lib`/`tests`/
`pyproject.toml` 里为空。凭据只存在于用户配置 `~/.sayacode/api_configs.json`
（写入前已备份为 `api_configs.json.bak-*`），cc-switch 的数据库只被**只读**访问。

---

# 第六轮：真实 TUI 验证暴露的缺陷（T1–T4）

用 `pywinpty` 起真 PTY 驱动了交互 TUI（管道不行：`lib/cli/main.py:231` 在
`stdin` 非 TTY 时**刻意**跳过对话循环）。TUI 全流程跑通：横幅 → 工作区握手 →
工作区面板 → 已保存模型卡片 → 连接测试 → Agent 就绪 → 流式渲染（含工具调用）
→ `/model list` → `/quit` → 退出确认。**真实模型、真实工具。**

## T1 · 中高 · 不可见的协议被静默改写为 Ollama（**已修**）

`lib/cli/configure.py:_get_protocol_option` 只在 `USER_VISIBLE_PROVIDER_TYPES`
里查表，查不到就 `return dict(defaults["ollama"])`。而 `generic` 与 `azure_openai`
都**不在**该集合里：

| 输入 | 回退结果 |
|---|---|
| `generic` | label `Ollama` · model `qwen3.5:9b` · url `http://localhost:11434` |
| `azure_openai` | 同上 |

后果不只是显示错误：`configure.py:272` 是 `model_type = selected_protocol["value"]`，
回退值会被**写回用户配置** —— 「配置写错」被伪装成「莫名其妙跑在本地 ollama 上」。

这正是此前已修过的「拼错 provider 静默跑在 ollama 上」的**同类缺陷**，
但那条路径漏掉了。修法：改为按**目录**解析（`normalize_provider_type` +
`provider_defaults`），未指定仍回退 ollama 保持兼容，**拼错则抛 ValueError**
（与 `provider_catalog_entry` 的既有约定一致）。顺带删掉因此变成死代码的
`_protocol_defaults`。

**TUI 上的肉眼验收**：卡片从 `协议 Ollama` 变成 `协议 Generic OpenAI Compatible`。

## T2 · 低 · 非交互模式仍弹 git 提交确认（**已修**）

`lib/cli/workspace.py:suggest_git_commit` 位于退出路径最后且会 `input()`。
实测 `echo "我的问题" | sayacode` 时管道里剩下的内容被这一问当成 y/n 吃掉并误判
（打印 `Please enter Y or N`），**用户真正的 prompt 永远没机会执行**。

修法：把判据放在**提问点自己身上**（`if not _supports_interactive_input(): return`），
任何调用方都受保护且可直接测。

## T3 · 低 · `user_config.workspace` 被持久化但从不读取（**已修**）

`persist_local_state`（`session_store.py:287`）一直在写 `user_config.workspace`，
但 `resolve_launch_workspace(args, user_config)` 的 `user_config` 参数**完全没被使用**，
`get_workspace_path` 在交互模式下**无条件**提问工作区路径。于是：

* 用户配置里存了 `workspace`，却每次启动都要重问一次；
* 这一问很容易被误答 —— 用 PTY 驱动 TUI 时，第一版驱动把 `/model list` 喂给了它，
  整个会话被带偏。

**修法**：按优先级解析 —— ① 显式 `--workspace`；② 配置里记住的工作区
**仅当它等于当前目录时**直接采用（此时没有任何可选分支，再问一次纯粹是摩擦）；
③ 否则照旧询问（默认当前目录）。

刻意**不**在「记住的目录 ≠ 当前目录」时静默切过去：用户刚 `cd` 到某个目录是强意图
信号，静默换目录比多问一句更糟。

## T4 · 高 · `list_directory` 在任何真实仓库上都失败（**已修**）

实测 `list_directory(".")` 在本仓库返回：

```
⚠️ 安全警告: 目录包含 656 个文件，批量删除存在风险
```

根因：`lib/tools/safety.py:check_file_danger(path)` **只有路径参数、没有操作参数**，
却把「目录（递归）文件数 > 100」当作删除风险。所有调用方都继承了这个删除专用启发式。

**权威对照**：`lib/core/safety.py` 在同一件事上**本来就做对了** ——
`if operation == 'delete':` 才做这项检查。工具层缺这道闸。

**修法**：把该启发式拆成独立的 `check_delete_danger(path)`：

| 位置 | 改动 |
|---|---|
| `check_file_danger` | 只保留「敏感文件 / 受保护目录 / 危险扩展名」三项**路径**检查，与操作类型无关 |
| `check_delete_danger` | 新增，承载「目录递归条目 > 100 则拒绝」 |
| `core/safety.py` delete 分支 | 两项都调用 —— 它自己的 `len(contents) > 20` 只看直接子项，覆盖不到「5 个子目录各 1000 个文件」 |
| `check_batch_operation` delete 分支 | 调用 `check_delete_danger` |
| `file_tools.delete_file` | **刻意不调用** —— 该工具只删空目录（下方「目录不为空」分支已拒绝一切非空目录），删除判据在这里是死代码；已加注释说明 |
| `list_directory` / `create_directory` / `batch_edit` / `check_write_operation` | 代码未改，自动不再继承删除启发式 |

**效果**：`check_file_danger('.')` → 安全；`check_delete_danger('.')` → 拒绝；
`list_directory('.')` → 正常工作。**判据没有消失，只是换了位置。**

## T5 · 观察 · 仓库里的 `.commandcode/` 不是本项目产生的

`git status` 里多出 `.commandcode/taste/taste.md`（0 字节，本次会话期间创建）。
仓库代码中**没有任何** `taste` / `.commandcode` 引用；本机装有 npm 包
`commandcode`（`%APPDATA%\npm\commandcode.ps1`），这是**它的**工作区元数据。
未删除，也未加进 `.gitignore` —— 是否忽略由项目决定。

## 6.x 第六轮验证

| 检查 | 结果 |
|---|---|
| `pytest` | **734 passed**（第六轮开始前 710） |
| `ruff` / `mypy`（项目门禁） | 全过 |
| 覆盖率门禁 | 全过 |
| `scripts/check_release.py` | Release checks passed |

revert 敏感性（每条都用「临时回退 → 必须失败 → 恢复 → 必须通过」验证）：

| 修复 | 回退后 | 恢复后 |
|---|---|---|
| T1 静默回退到 ollama | 4 failed | 全过 |
| T2 非交互仍弹 git 确认 | 1 failed | 全过 |
| T3 不读取记住的工作区 | 2 failed | 全过 |
| T4 删除判据污染只读工具 | 3 failed | 全过 |

## TUI 产品形态的判断（记录）

结论：**保留现在的 rich 逐行 REPL，不做全屏 TUI（textual 那类）**。理由：

1. 它已经够用且便宜 —— 225 行的 `interactive.py` 覆盖完整工作流，PTY 实测全通过；
2. 全屏 TUI 会**主动放弃**三样真优势：`echo | sayacode` 可组合、`-p` 无头模式、
   JSONL 事件流的 CI 可解析性；
3. 这六轮找到的真缺陷全在**语义层**（静默回退、请求体污染、装饰性开关、只读工具误拦、
   重复提问），没有一个是「界面不够漂亮」—— 全屏 TUI 一个都不会修好；
4. 当前痛点是可廉价修掉的（T3 已修；权限确认做成结构化块、编辑结果 inline diff
   是值得投入的方向）。

优先级：修语义缺陷 > 结构化权限确认 / inline diff > 全屏 TUI。

---

# 第七轮：1.4.0 发布——以及「本地全绿 ≠ CI 全绿」

## 7.1 发布流程

版本唯一来源是 `lib/_version.py`（`pyproject.toml` 用 `dynamic` + `attr` 读它）。
`.github/workflows/ci.yml` 在 `branches: [main]` 与 `tags: ['v*']` 上都跑，其中
`publish` job 仅在 tag 上触发，用 `secrets.PYPI_TOKEN` 发到 PyPI。

1.4.0 的发布提交只改 `lib/_version.py` 一行（沿用 1.3.18 的惯例），
消息里列明变更与**破坏性变更清单**。

## 7.2 踩到的坑：CI 全红，而本地怎么跑都是绿的

推送后 **三个 Windows job 全部失败**，三个 Ubuntu job 全过。失败点不在测试：

```
File "scripts/check_coverage.py", line 106, in main
UnicodeEncodeError: 'charmap' codec can't encode characters in position 2-7
```

原因：**Windows 运行器的控制台是 cp1252**，而 `check_coverage.py` 会打印中文
（「门槛」等）。覆盖率校验**本身是通过的**（测试跑完、覆盖率 JSON 已写出），
崩溃只发生在最后一个 `print`。Ubuntu 与本地控制台都是 UTF-8，所以两边都看不见。

**复现方式**（关键：不靠猜）：`PYTHONIOENCODING=cp1252 python scripts/check_coverage.py --report`
—— 修复前抛 `UnicodeEncodeError`，修复后正常跑完全量并输出中文。

**修法**：
* `check_coverage.py` 启动时把 stdout/stderr 重配为 UTF-8 且 `errors="replace"`
  —— 宁可少数几个字变占位符，也不能让纯展示问题把 CI 判红；
* `ci.yml` 全局声明 `PYTHONIOENCODING: utf-8`，覆盖 pytest 等其余输出。

## 7.3 教训：本地全绿是最弱的一种证据

本轮先后出现两次「本地全绿但 CI 红」：

| 场景 | 本地 | CI |
|---|---|---|
| 中文输出编码 | UTF-8 控制台，正常 | Windows cp1252，`UnicodeEncodeError` |
| 平台矩阵 | 仅 Windows / py3.13 | Ubuntu + Windows × py3.11/3.12/3.13 |

> **在本地只跑一次，等于只验了 1/6 的组合。** 任何面向多平台发布的改动，
> 都应当把「CI 矩阵里的差异」当作一等公民：输出编码、路径分隔符、平台条件分支、
> Python 小版本差异。本地通过的结论必须附带「在哪个解释器 / 哪个平台」。

## 7.4 发布结果

| 项 | 结果 |
|---|---|
| `v1.4.0` CI | **success**（6 个矩阵 job + publish） |
| PyPI | **1.4.0**：wheel 340,123 B + sdist 375,338 B |
| 全新 venv 安装 | `pip install --index-url https://pypi.org/simple sayacode==1.4.0` → 成功 |
| `sayacode --version` | `SAYACODE v1.4.0` |
| 破坏性变更生效 | `lib.models.openai_model` 已不存在；`OpenAIModel`、`passthrough_fields`、`check_delete_danger` 可用 |
| `main` HEAD CI | success（此前两次红均为同一个 cp1252 问题，已由 `67bc8a7` 修复） |

注意：本机 pip 默认走清华镜像，镜像同步滞后会导致 `pip install sayacode==1.4.0`
暂时找不到 —— 验证发布时应显式指定 `--index-url https://pypi.org/simple`。
