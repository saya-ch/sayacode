# Coding Agent 记忆系统调研

核对日期：2026-09-23。SAYACODE 基线：`a938844`，版本 2.1.0。

本文记录一手资料、当前源码核对结果和设计依据。目标方案见 [记忆系统设计](../design/memory-system.md)。产品文档会变化；下文的“当前”均指核对日期。官方产品说明、厂商实验、论文结果和本项目提案分别标识，不把设计推断写成既有功能。

## 1. 可借鉴的产品经验

| 对象 | 已核验做法 | SAYACODE 可吸收的部分 | 适用边界 |
|---|---|---|---|
| Claude Code | 人工项目说明与自动记忆分开；简短索引在会话开始时加载，详情按需读取；同一仓库的 worktree 共享自动记忆目录 | 短入口、按需详情、仓库身份不随 worktree 改变 | 文件组织方式不必照搬；主会话和子 Agent 的记忆可见性是其产品选择 |
| GitHub Copilot | 仓库事实带代码引用，使用前核对当前分支；用户偏好独立作用域；未使用的条目有保留期限 | 当前代码证据优先、读取时核验、偏好与项目事实分域 | 28 天是该产品的保留策略，不能当作所有记忆的通用有效期 |
| Codex 本地记忆 | 后台处理符合条件的历史会话；区分使用已有记忆和为未来生成记忆；等待会话空闲，避免持续整理进行中的工作 | 读写开关独立、延迟整理、可观察的后台费用 | 文档描述本地 Codex；不由此推断云端或其他客户端行为完全相同 |
| OpenAI Sandbox Agents | 先形成单次运行材料，再整理共享记忆；短摘要、索引、详细材料逐级读取 | 提取与整理分阶段，输出按需展开 | 这是 Agents SDK 的明确能力，不能直接等同于所有 Codex 客户端内部实现 |
| Cursor 当前 Rules | 按路径、显式选择或任务相关性加载指令，鼓励引用权威文件而非复制内容 | 作用域、按需激活、减少可从代码直接读出的重复知识 | 旧 Memories 页面现重定向，旧版后台记忆说明不足以证明当前产品仍如此运行 |
| Cascade 历史记忆 | 自动生成的记忆按工作区隔离；人工规则与 Skill 是其他入口 | 自动经验不直接升级成人工项目规则 | 当前旧 Windsurf URL 转向 Devin Desktop 文档，页面明确标注该记忆机制适用于旧 Cascade，不能泛称新默认 Agent 的能力 |
| Aider repository map | 从当前代码提取结构，按上下文预算选择相关部分 | 能低成本从当前仓库取得的事实，优先读代码 | repository map 是当前代码检索，不等同于聊天长期记忆 |

### Claude Code

官方页面区分人工说明和自动经验。自动记忆的 `MEMORY.md` 是目录入口，启动时只读有界部分，详细主题文件按需读取；同一 Git 仓库的 worktree 和子目录共享自动记忆位置。文档同时说明记忆可以编辑、删除，并给出查看入口。

设计启示：检索入口保持短小，把大块经验留给按需读取。无需复制其具体行数限制，也不假定文件被读取就代表其中事实重新得到验证。

来源：[How Claude remembers your project](https://code.claude.com/docs/en/memory)。

### GitHub Copilot

当前产品文档说明：仓库事实带代码引用，使用时对照当前分支；用户偏好带来源并归属于用户。未使用条目在 28 天后删除，成功验证并使用可刷新期限。2026 年 1 月的工程文章说明其最初采用最近记忆加即时核验，减少离线整理服务的成本。

设计启示：保留“为什么可信、在哪个代码版本成立”，检索后做验证。厂商报告的效果来自其自身实验，不能当作 SAYACODE 的收益承诺。默认开关等现行行为以当前文档为准，不沿用早期文章的发布状态。

来源：[产品文档](https://docs.github.com/en/copilot/concepts/agents/copilot-memory)、[工程文章](https://github.blog/ai-and-ml/github-copilot/building-an-agentic-memory-system-for-github-copilot/)。

### Codex 与 OpenAI Sandbox Agents

Codex 当前本地记忆文档公开了后台整理、空闲等待，以及每个会话分别控制使用和生成的方式。Sandbox Agents 文档另外公开了分阶段生成和渐进读取布局。两份资料共同支持“短上下文入口、后台整理、可独立关闭学习”的设计，但其具体实现不能相互混用。

OpenAI 的近期提示词工程文章还建议删减过度普遍化、已经无益的项目指令。记忆价值应以对任务的帮助衡量，不能以生成的规则数量衡量。

来源：[Codex 本地记忆](https://learn.chatgpt.com/docs/customization/memories)、[Sandbox Agents 记忆](https://developers.openai.com/api/docs/guides/agents/sandboxes#persist-memory-across-runs)、[提示词与 Skill 的维护经验](https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra)。

### Cursor、Cascade 与 Aider

Cursor 当前 Rules 文档可验证按任务和路径加载的机制，并建议直接引用现有代码。旧自动 Memories 地址已经失去原页面，因此不采用旧搜索摘要中的审批行为作为当前设计依据。

Cascade 页面现明确区分历史 Agent 与新默认 Agent；这里只借鉴其工作区隔离和记忆可管理性。Aider 的当前代码映射说明了另一个边界：目录、定义和调用关系可以从当前代码重建，长期记忆更应该保存不易直接推导的原因、纠正和实际踩坑。

来源：[Cursor Rules](https://cursor.com/docs/rules)、[Cascade Memories & Rules](https://docs.devin.ai/desktop/cascade/memories)、[Aider repository map](https://aider.chat/docs/repomap.html)。

## 2. 研究对验收的启示

[Evaluating AGENTS.md](https://arxiv.org/abs/2602.11988) 在其任务、模型和上下文文件设置下，观察到额外指令可能增加成本并降低完成率。它不能证明所有项目规则无用，但支持用无记忆基线检查“新增上下文是否真的帮助了任务”。

[VibeMemBench](https://arxiv.org/abs/2609.23570) 是 2026-09-20 的新预印本，把记忆评估放到可执行仓库任务上。它区分“历史里存在有用经验”和“记忆系统实际找对并交给 Agent”两件事。其目标选择带有已验证经验有效的条件，结果不应外推成任意仓库的普遍提升比例。

SAYACODE 因此需要同时测量：实际任务完成、错误记忆采用率、用户纠正次数、检索有效率，以及主任务和后台整理的总费用。只测事实问答召回率不足以决定上线。

## 3. LangChain 生态的可用职责

| 职责 | 可复用组件 | 本项目仍需负责 |
|---|---|---|
| Agent 执行与短期状态 | `create_agent`、LangGraph checkpoint | 标记真实用户轮次和记忆来源 |
| 内容提取、对照旧记忆生成修订 | LangMem `create_memory_manager` | 提供作用域、证据和产品提取要求 |
| 跨会话持久化 | `AsyncSqliteStore` | 记录有效性、遗忘状态和提交一致性 |
| 请求时带入相关记忆 | `AgentMiddleware.awrap_model_call` | 筛选、限量、标明来源与适用条件 |
| 可选语义检索 | SQLite Store 的官方 embedding index | 显式 embedding 配置、版本与删除验收 |
| 跨进程短临界区 | 成熟的 `filelock.AsyncFileLock` | 按本地记忆作用域选择锁，不持锁等待模型 |

来源：[LangMem API](https://langchain-ai.github.io/langmem/reference/memory/)、[LangMem 提取源码](https://raw.githubusercontent.com/langchain-ai/langmem/main/src/langmem/knowledge/extraction.py)、[LangChain 中间件](https://docs.langchain.com/oss/python/langchain/middleware/custom)、[SQLite Store 源码](https://github.com/langchain-ai/langgraph/blob/main/libs/checkpoint-sqlite/langgraph/store/sqlite/aio.py)、[filelock API](https://py-filelock.readthedocs.io/en/latest/api.html)。

需要修正前期讨论中的一个选择：完整系统采用**无存储副作用的 `create_memory_manager`**。`create_memory_store_manager` 会直接进行多次 `aput/adelete`，不便在落盘前统一检查遗忘、修订冲突和证据范围。选择更底层的官方提取能力，仍然复用了内容理解和修订算法，并非重写提取器。

### 已知兼容边界

- LangMem 当前已核验发布版为 `0.0.30`。前期对 SAYACODE 固定依赖的解析已成功，但这不能替代实际模型工具调用测试。
- 上述源码链接指向公开主分支，实施时必须对照锁定发布版再次检查行为，不能假定主分支与安装包逐字相同。
- LangMem 使用工具调用进行结构化提取；聊天协议名称不能保证服务端支持这种用法。
- 本地 `ReflectionExecutor` 使用同步工作线程和 `invoke`。SAYACODE 应在现有异步循环中调用 `ainvoke`，将生命周期纳入 CLI 退出处理。
- `create_manage_memory_tool` 与 `create_memory_store_manager` 有同命名空间格式不兼容的公开报告。正式记忆只能有一条提交路径。
- 当前 Store 没有配置 `index`。本机核验：此时带 `query` 的搜索不进行语义匹配，普通分支按更新时间返回。
- SQLite 向量删除有公开的残留 embedding 报告。可选语义索引必须通过删除与重建测试，不能把 API 删除成功等同于数据完全移除。

来源：[LangMem 发布](https://pypi.org/project/langmem/)、[ReflectionExecutor 源码](https://github.com/langchain-ai/langmem/blob/main/src/langmem/reflection.py)、[格式不兼容报告 #138](https://github.com/langchain-ai/langmem/issues/138)、[SQLite 向量删除报告 #8757](https://github.com/langchain-ai/langgraph/issues/8757)。这些报告说明需要验证的边界，不代表已在 SAYACODE 复现全部问题。

## 4. 本机源码与故障注入核验

本次未修改生产代码、配置或已有数据库。使用项目当前安装的 `langgraph-checkpoint-sqlite==3.1.1`、`langgraph==1.2.11`，在临时数据库进行验证。

### 当前可确认的接口

- `AsyncSqliteStore.from_conn_string` 接受 `index` 和 `ttl`。
- `AsyncSqliteSaver` 提供 `adelete_thread`。
- `BaseStore` 没有公开的 compare-and-swap 接口。
- `AsyncSqliteStore.abatch` 虽使用事务游标，但当前 `_cursor` 在 `finally` 中执行 `COMMIT`，不能据方法名推断失败时整批回滚。

### 批量提交异常时的实际结果

实验先保存 `existing`，再创建一个仅对 `boom` 插入报错的 SQLite trigger，然后调用：

```python
await store.abatch([
    PutOp(("probe",), "existing", None, index=False),
    PutOp(("probe",), "boom", {"text": "new"}, index=False),
])
```

观测到 `sqlite3.IntegrityError` 后，`existing` 已被删除，`boom` 没有写入。即：**当前版本的失败批次可能留下部分效果**。此结论来自本机实测，不只是接口推断。

第二个实验对同一键 `state` 写入版本 1，再用 trigger 使版本 2 的替换失败。通过 `aput(index=False)` 写入失败后，读取结果仍为完整的版本 1。该场景支持单文档提交方案，但还不等于已经验证进程强杀、磁盘故障和跨进程竞争。

设计影响：首版权威记忆按作用域保存在一个 Store 文档中，正文修订、删除标记和已处理来源一起以单次 `aput(index=False)` 提交；避免把多个键之间的原子性假设成既有能力。实现前还需对该单文档方案做故障注入；若未来改用逐条记录，必须先验证官方后端真实事务契约。

## 5. 调研结论对应的设计选择

1. 学习用户纠正、非显然约定和已验证的经验；尽量少复制当前代码能够轻易说明的内容。
2. 记忆有来源和适用条件，相关性检索不能越过有效性检查。
3. 人工规则、对话状态、学习记忆、Skill 各有一个权威来源，避免同一内容双写。
4. 低延迟读取与后台学习分开，后台任务受 CLI 生命周期管理。
5. 自动生成不自动提升指令权限，也不自动变成跨项目规则或 Skill。
6. 将“临时例外、永久修订、条件过期、用户遗忘”作为首版行为合同。
7. 用同任务、同模型的记忆开关对照验证收益；不预先承诺百分比提升。

以上形成 [SAYACODE 记忆系统设计](../design/memory-system.md)；本文本身不启用任何自动学习功能。
