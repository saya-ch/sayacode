"""权限强制不变量与 mode/session 状态分离回归。

本文件对应 CODE_REVIEW_FINDINGS.md 的 A1（危险工具地板）与 B1/B3/B4（会话状态
与 mode 规则混用）修复。核心不变量：

    危险工具在任意「来源 × 动作」组合下，策略层都不得产出 allow。

允许的唯一例外是用户在确认弹窗中逐项确认（action == "ask" 且回调返回 True），
因此测试同时断言：一旦 allowed 为真，动作必须是 ask，而不是 allow。
"""

import pytest

from lib.core.modes import apply_agent_mode_permissions
from lib.core.permission_policy import DANGEROUS_TOOLS
from lib.core.permission_session import (
    PermissionRuntime,
    _active_runtime,
    permission_runtime_session,
)
from lib.core.permission_workspace import (
    configure_permission_workspace,
    create_permission_runtime,
    get_permission_policy_summary,
    reset_session_permission_rules,
    set_permission_confirm_callback,
)
from lib.core.private_io import write_private_json


# None 表示「该来源不设置规则」
ACTIONS = (None, "allow", "ask", "deny")


def _policy_workspace(tmp_path, monkeypatch, tool_name, policy_action):
    """构造一个把 tools/paths/default 全部设为 policy_action 的工作区。"""
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    if policy_action is not None:
        policy_dir = workspace / ".sayacode"
        policy_dir.mkdir()
        write_private_json(policy_dir / "permissions.json", {
            "default": policy_action,
            "tools": {tool_name: policy_action},
            "paths": {"**": policy_action},
        })
    return workspace


@pytest.mark.parametrize("tool_name", sorted(DANGEROUS_TOOLS))
@pytest.mark.parametrize("session_action", ACTIONS)
@pytest.mark.parametrize("mode_action", ACTIONS)
@pytest.mark.parametrize("policy_action", ACTIONS)
def test_dangerous_tool_never_policy_allowed(
    tool_name, session_action, mode_action, policy_action, tmp_path, monkeypatch
):
    """危险工具在 4×4×4 全组合下，策略层都不得产出 allow。"""
    workspace = _policy_workspace(tmp_path, monkeypatch, tool_name, policy_action)
    runtime = PermissionRuntime()
    runtime.configure_workspace(workspace)

    if session_action is not None:
        runtime.set_session_rules({tool_name: session_action}, source="session")
    if mode_action is not None:
        runtime.set_mode_rules({tool_name: mode_action}, source="mode:test")

    arguments = {"path": "docs/readme.md", "command": "git push origin main"}

    # 不变量 1：策略层绝不产出 allow（allow 只可能来自用户逐项确认）
    assert runtime._decide(tool_name, arguments).action != "allow", (
        f"{tool_name} 策略层产出 allow："
        f"session={session_action} mode={mode_action} policy={policy_action}"
    )

    # 不变量 2：无交互回调时必须 fail closed
    runtime.set_confirm_callback(None)
    decision = runtime.check(tool_name, arguments)
    assert decision.allowed is False, (
        f"{tool_name} 在无回调时被放行："
        f"session={session_action} mode={mode_action} policy={policy_action}"
    )

    # 不变量 3：即使回调一律放行，也只能通过 ask（逐项确认）放行
    runtime.set_confirm_callback(lambda request: True)
    confirmed = runtime.check(tool_name, arguments)
    if confirmed.allowed:
        assert confirmed.action == "ask", (
            f"{tool_name} 被静默放行（action={confirmed.action}）："
            f"session={session_action} mode={mode_action} policy={policy_action}"
        )


def test_policy_file_tools_allow_is_downgraded(tmp_path, monkeypatch):
    """手工编辑策略文件给危险工具写 allow，也必须被降级为 deny。

    这里必须断言 ``policy.tool_rules`` 与 ``policy.stripped_dangerous``（加载期
    降级），而不是只看 ``check()``：decide 出口的危险工具地板也会把 allow 降级
    并补记 stripped_dangerous，因此只断言 check() 的话，
    PermissionPolicy.__init__ 里的降级被删掉也测不出来。
    """
    workspace = _policy_workspace(tmp_path, monkeypatch, "delete_file", "allow")
    configure_permission_workspace(workspace)
    try:
        runtime = _active_runtime()

        # 加载期（__init__）就必须降级，且不依赖任何一次 check()
        assert runtime.policy.tool_rules["delete_file"] == "deny"
        assert runtime.policy.stripped_dangerous == ["delete_file"]

        assert runtime.check("delete_file", {"path": "a.txt"}).action == "deny"
        assert runtime.policy.stripped_dangerous == ["delete_file"]
        # 降级必须在摘要里可见，否则降级本身又是静默的
        assert "已强制降级为 deny" in get_permission_policy_summary()
    finally:
        configure_permission_workspace(tmp_path)


def test_dangerous_allow_is_floored_at_the_decision_exit(tmp_path, monkeypatch):
    """危险工具地板必须收敛在决策出口，而不是各来源的写入路径。

    只要 allow 是从**决策出口**返回的，策略里的通配 allow、session/mode 的
    通配规则、以及被直接改写的 session dict 都必须被拦下 —— 这三种来源分别
    对应 F1（通配键绕过）与 F2（绕过归一化的裸写入）。
    """
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
    workspace = _policy_workspace(tmp_path, monkeypatch, "delete_file", "allow")
    configure_permission_workspace(workspace)
    try:
        runtime = PermissionRuntime()
        runtime.configure_workspace(workspace)
        runtime.set_confirm_callback(None)

        # F1：session / mode 规则的通配键
        runtime.set_session_rules({"delete_*": "allow"}, source="session")
        assert runtime._decide("delete_file", {"path": "a.txt"}).action == "deny"
        runtime.set_session_rules({}, source="session")

        runtime.set_mode_rules({"*": "allow"}, source="mode:test")
        assert runtime._decide("delete_file", {"path": "a.txt"}).action == "deny"
        runtime.clear_mode_rules()

        # F2：绕开归一化的裸写入（属性 setter 与直接改 dict）
        runtime.session_rules = {"delete_file": "allow"}
        # setter 必须自己走归一化：存进来的就已经是 deny（只看 _decide 的话，
        # 出口地板会顶罪，setter 退化成裸赋值也测不出来）
        assert runtime.session.session_rules == {"delete_file": "deny"}
        assert runtime._decide("delete_file", {"path": "a.txt"}).action == "deny"
        runtime.set_session_rules({}, source="session")

        # 直接改 dict 没有归一化钩子，只能靠出口地板
        runtime.session.session_rules["delete_file"] = "allow"
        assert runtime._decide("delete_file", {"path": "a.txt"}).action == "deny"
        runtime.set_session_rules({}, source="session")
        runtime.session.mode_rules = {"delete_file": "allow"}
        assert runtime._decide("delete_file", {"path": "a.txt"}).action == "deny"
        runtime.clear_mode_rules()

        # 普通工具不受地板影响，否则地板会变成「一切都不许」
        runtime.set_session_rules({"write_file": "allow"}, source="session")
        assert runtime._decide("write_file", {"path": "a.txt"}).action == "allow"
        runtime.set_session_rules({}, source="session")
    finally:
        reset_session_permission_rules()
        configure_permission_workspace(tmp_path)


def test_mode_switch_preserves_session_grants(tmp_path, monkeypatch):
    """切到 build 模式不得清空用户已授予的会话授权（历史 B1/B3）。"""
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    reset_session_permission_rules()
    try:
        runtime = create_permission_runtime(workspace)
        runtime.set_confirm_callback(None)
        # 等价于用户在确认弹窗里选了「本次会话始终允许」
        runtime.update_session_rules({"write_file": "allow"}, source="session")

        apply_agent_mode_permissions("plan", runtime=runtime)
        # plan 的 deny 是硬约束，压过会话授权
        assert runtime.check("write_file", {"path": "a.txt"}).action == "deny"

        apply_agent_mode_permissions("build", runtime=runtime)
        # 关键回归：会话授权必须还在
        assert runtime.session.session_rules == {"write_file": "allow"}
        assert runtime.check("write_file", {"path": "a.txt"}).allowed is True
    finally:
        reset_session_permission_rules()


def test_session_allow_cannot_beat_mode_deny(tmp_path, monkeypatch):
    """会话级 allow 不能绕开 plan 模式的只读要求。"""
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    reset_session_permission_rules()
    try:
        runtime = create_permission_runtime(workspace)
        runtime.set_confirm_callback(None)
        apply_agent_mode_permissions("plan", runtime=runtime)

        runtime.update_session_rules({"write_file": "allow"}, source="session")

        assert runtime.check("write_file", {"path": "a.txt"}).action == "deny"
    finally:
        reset_session_permission_rules()


def test_mode_rules_and_session_rules_are_separate_stores(tmp_path, monkeypatch):
    """mode 规则与 session 授权必须是两个独立的存储（历史 B1/B3 根因）。"""
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    reset_session_permission_rules()
    try:
        runtime = create_permission_runtime(workspace)
        apply_agent_mode_permissions("plan", runtime=runtime)

        assert runtime.session.mode_rules, "plan 模式应写入 mode_rules"
        assert runtime.session.session_rules == {}, "plan 模式不应写 session_rules"
    finally:
        reset_session_permission_rules()


@pytest.mark.parametrize("mode_first", [True, False])
def test_mode_application_is_order_independent(tmp_path, monkeypatch, mode_first):
    """无论先设模式还是先建 runtime，模式都必须生效（历史 B4 脆弱耦合）。

    顺序无关的唯一成立理由是「会话状态是共享引用」：断言 ``runtime.session``
    与进程级 runtime 是**同一个对象**。若 create_permission_runtime 退回
    「建 runtime 时拷一份会话快照」，mode_first=True 会拿到空快照，两条断言
    （身份 + mode_rules 可见）都会失败。
    """
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    reset_session_permission_rules()
    try:
        if mode_first:
            apply_agent_mode_permissions("plan")
            runtime = create_permission_runtime(workspace)
        else:
            runtime = create_permission_runtime(workspace)
            apply_agent_mode_permissions("plan", runtime=runtime)

        assert runtime.session is _active_runtime().session, "会话状态必须是共享引用"
        assert runtime.session.mode_rules, "新建的 runtime 必须能看到已写入的 mode 规则"

        runtime.set_confirm_callback(None)
        assert runtime.check("write_file", {"path": "a.txt"}).action == "deny"
    finally:
        reset_session_permission_rules()


# ── F3：策略层优先级 ──────────────────────────────────────────────────────────


def test_explicit_tool_deny_outranks_paths_allow(tmp_path, monkeypatch):
    """显式 ``tools: deny`` 必须压过 ``paths: allow``。

    shell 工具会把 ``cwd`` 传进权限检查（PATH_ARGUMENT_KEYS），因此一条
    ``paths: {"**": "allow"}`` 足以让 ``tools: {"execute_command_tool": "deny"}``
    形同虚设 —— path 规则先返回 allow，工具级 deny 永远轮不到。
    """
    workspace = _policy_workspace(tmp_path, monkeypatch, "execute_command_tool", "deny")
    policy_file = workspace / ".sayacode" / "permissions.json"
    write_private_json(policy_file, {
        "default": "ask",
        "tools": {"execute_command_tool": "deny"},
        "paths": {"**": "allow"},
        "commands": {"rm *": "ask"},
    })
    configure_permission_workspace(workspace)
    set_permission_confirm_callback(lambda request: True)
    try:
        runtime = _active_runtime()
        decision = runtime.check(
            "execute_command_tool",
            {"command": "rm -rf /", "cwd": "src"},
        )
        assert decision.action == "deny"
        assert decision.allowed is False
        assert decision.source == "project"
    finally:
        set_permission_confirm_callback(None)
        reset_session_permission_rules()
        configure_permission_workspace(tmp_path)


def test_paths_allow_does_not_preempt_command_rules(tmp_path, monkeypatch):
    """``paths: {"**": "allow"}`` 不得吞掉针对命令内容的 commands 规则。

    commands 规则比宽泛的 path 规则更具体，必须先判定；否则任何带 ``cwd``
    的调用都能让 ``rm *: ask`` 静默失效。
    """
    workspace = _policy_workspace(tmp_path, monkeypatch, "execute_command_tool", None)
    policy_file = workspace / ".sayacode" / "permissions.json"
    write_private_json(policy_file, {
        "default": "ask",
        "paths": {"**": "allow"},
        "commands": {"rm *": "ask"},
    })
    configure_permission_workspace(workspace)
    set_permission_confirm_callback(None)
    try:
        runtime = _active_runtime()
        risky = runtime.check("execute_command_tool", {"command": "rm -rf /", "cwd": "src"})
        safe = runtime.check("execute_command_tool", {"command": "python --version", "cwd": "src"})

        assert risky.action == "ask"
        assert risky.allowed is False
        # 未被 commands 规则覆盖的命令仍然吃 path allow
        assert safe.action == "allow"
        assert safe.source == "project"
    finally:
        reset_session_permission_rules()
        configure_permission_workspace(tmp_path)


# ── F4：默认 runtime 必须复用共享会话状态 ─────────────────────────────────────


def test_bare_runtime_shares_process_session_state():
    """``PermissionRuntime()`` 不得新建私有会话状态（fail-open 陷阱）。

    模块 docstring 承诺「同一进程的所有 runtime 共享一个实例」。若不成立，
    ``with permission_runtime_session(PermissionRuntime())`` 会丢掉全部 mode
    deny，plan 模式下的写操作被静默放行。
    """
    reset_session_permission_rules()
    try:
        apply_agent_mode_permissions("plan")
        bare = PermissionRuntime()

        assert bare.session is _active_runtime().session
        bare.set_confirm_callback(None)
        with permission_runtime_session(bare):
            assert bare.check("write_file", {"path": "a.txt"}).action == "deny"
    finally:
        reset_session_permission_rules()


# ── F8：降级记录必须随会话授权一起清除 ───────────────────────────────────────


def test_reset_clears_stripped_dangerous(tmp_path, monkeypatch):
    """reset 必须清掉 stripped_dangerous，否则 /permissions 打印过期降级行。"""
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    reset_session_permission_rules()
    try:
        runtime = create_permission_runtime(workspace)
        runtime.set_confirm_callback(None)
        runtime.set_session_rules({"delete_file": "allow"}, source="session")
        assert runtime.session.stripped_dangerous.get("session"), "应记录一次降级"

        with permission_runtime_session(runtime):
            reset_session_permission_rules()
            assert runtime.session.session_rules == {}
            assert runtime.session.stripped_dangerous == {}
            assert "已强制降级为 deny" not in get_permission_policy_summary()
    finally:
        reset_session_permission_rules()


# ── F9：策略文件 tools 键必须真的通配匹配 ─────────────────────────────────────


def test_policy_tools_keys_glob_match(tmp_path, monkeypatch):
    """策略里的 ``tools`` 键支持 ``prefix*`` 通配，且与 /permissions 摘要一致。

    此前 ``/permissions`` 会把 ``mcp_*: deny (project)`` 列为生效规则，
    但 decide 用的是精确匹配 —— 显示与行为不一致（用户会以为已经禁用了）。
    """
    workspace = _policy_workspace(tmp_path, monkeypatch, "mcp_foo", None)
    policy_file = workspace / ".sayacode" / "permissions.json"
    write_private_json(policy_file, {"default": "allow", "tools": {"mcp_*": "deny"}})
    configure_permission_workspace(workspace)
    try:
        runtime = _active_runtime()
        decision = runtime.check("mcp_foo", {})
        assert decision.action == "deny"
        assert decision.source == "project"

        # 摘要里的 tools 行本身不经过 i18n，断言与语言无关
        assert "mcp_*: deny (project)" in get_permission_policy_summary()
    finally:
        reset_session_permission_rules()
        configure_permission_workspace(tmp_path)


def test_policy_glob_beats_builtin_exact_default(tmp_path, monkeypatch):
    """策略文件的通配键必须压过 built-in 默认表里的精确键。

    built-in 默认表里有 ``git_push: ask`` 这类精确键；若匹配时先命中它，
    策略文件里的 ``git_*: deny`` 就永远不生效 —— 摘要照样打印
    ``git_*: deny (project)``，行为却是 ask，显示与行为再次不一致（F9）。
    """
    workspace = _policy_workspace(tmp_path, monkeypatch, "git_push", None)
    policy_file = workspace / ".sayacode" / "permissions.json"
    write_private_json(policy_file, {"default": "ask", "tools": {"git_*": "deny"}})
    configure_permission_workspace(workspace)
    set_permission_confirm_callback(lambda request: True)
    try:
        runtime = _active_runtime()
        decision = runtime.check("git_push", {})
        assert decision.action == "deny"
        assert decision.allowed is False
        assert decision.source == "project"
    finally:
        set_permission_confirm_callback(None)
        reset_session_permission_rules()
        configure_permission_workspace(tmp_path)
