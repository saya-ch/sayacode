"""绘制分层、时序与治理三张架构图，供文档展示用。
用法：
    python scripts/draw_arch.py
输出：docs/arch1_layers.png 等三图；恒为 0，用后可删。
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

# 指定中文字体并关闭负号乱码，保证中文正常渲染。
plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "Noto Sans SC"]
plt.rcParams["axes.unicode_minus"] = False

# 定义统一配色，保持三图视觉一致。

PINK, DEEP, HOT = "#FFB7C5", "#FF69B4", "#FF1493"
BG, SF, SF2 = "#0A0A0A", "#161016", "#211521"
TXT, DIM, BDR = "#E0E0E0", "#9A8693", "#FFDDE8"
GREEN, YEL, RED, BLUE = "#7CE7A2", "#FFB703", "#FF6B6B", "#7CC7E7"


def box(ax, xy, w, h, face, edge, text, fs=11, tc=TXT, style="round,pad=0.02"):
    """绘制带文本的圆角矩形框。"""
    ax.add_patch(FancyBboxPatch(xy, w, h, boxstyle=style, facecolor=face, edgecolor=edge, lw=1.5))
    ax.text(xy[0] + w / 2, xy[1] + h / 2, text, ha="center", va="center", fontsize=fs, color=tc, linespacing=1.5)


def arrow(ax, a, b, color=DEEP):
    """绘制带箭头的流向连线。"""
    ax.add_patch(FancyArrowPatch(a, b, arrowstyle="-|>", mutation_scale=16, color=color, lw=2))


# 绘制图 1：分层架构。
fig, ax = plt.subplots(figsize=(12, 13))
fig.patch.set_facecolor(BG)
ax.set_facecolor(BG)
ax.axis("off")
ax.set_xlim(0, 12)
ax.set_ylim(0, 13.6)
ax.text(6, 13.1, "SAYACODE 分层架构", ha="center", fontsize=22, color=PINK, weight="bold")
ax.text(6, 12.7, "调用自上而下 · 数据同向流动", ha="center", fontsize=11, color=DIM)
layers = [
    ("CLI 交互层", [("main.py\n主入口",), ("headless\n-p 非交互",), ("commands\n20+ slash",), ("theme\nrich 渲染",), ("i18n\n中英双语",)]),
    ("启动装配层 runtime/", [("StartupService\n装配流水线",), ("RuntimeApplication\nbuild/sync",), ("RuntimeContext\n显式容器",), ("session_store\n会话索引",)]),
    ("Agent 执行核", [("SAIAgent\nturn 状态机+恢复",), ("AgentRunner\n建图+持久化",), ("middleware×4\nHook 洋葱",)]),
    ("能力层", [("tools 32+3\n文件/Shell/Git",), ("ToolSearch\n延迟发现",), ("batch_execute\n≤8 并发",), ("models 6 协议\n薄继承集成",)]),
    ("治理层", [("permissions\n危险地板→默认",), ("hooks/audit\n6 事件+审计",), ("session/memory\n三层压缩",), ("prompts\n6 段+9 人格",)]),
    ("扩展层", [("mcp_runtime\n.mcp.json",), ("team\nsupervisor",), ("custom_cmd\n.claude 兼容",), ("doctor\n自检",)]),
]
y = 11.9
for title, mods in layers:
    box(ax, (0.4, y - 1.42), 11.2, 1.62, SF, BDR, "", 10)
    ax.text(0.7, y - 0.12, title, fontsize=12, color=PINK, weight="bold", va="center")
    n = len(mods)
    w = min(2.35, 10.4 / n - 0.12)
    x0 = 0.7
    for m in mods:
        box(ax, (x0, y - 1.28), w, 0.95, SF2, BDR, m[0], 9.5)
        x0 += w + 0.12
    if y > 2.0:
        arrow(ax, (6, y - 1.5), (6, y - 1.78))
    y -= 1.86
fig.savefig("docs/arch1_layers.png", dpi=150, facecolor=BG, bbox_inches="tight")
print("arch1 ok")

# 绘制图 2：单轮时序。
fig2, ax2 = plt.subplots(figsize=(11, 13))
fig2.patch.set_facecolor(BG)
ax2.set_facecolor(BG)
ax2.axis("off")
ax2.set_xlim(0, 11)
ax2.set_ylim(0, 13.6)
ax2.text(5.5, 13.1, "单轮执行时序", ha="center", fontsize=22, color=PINK, weight="bold")
steps = [
    ("输入\nInteractiveLoop / headless -p", SF, BDR),
    ("开 turn\nTurnState(NEXT_TURN) + tool 会话", SF, BDR),
    ("_prepare_messages\n压缩检测 → 刷 system → 增量输入", SF, BDR),
    ("图执行 runner.invoke / stream\n中断走 interrupt→handler→resume", SF2, DEEP),
    ("异常？ _classify_error", None, None),
    ("四档恢复（≤3 次）\n退避重试 / 续写 / 强制压缩 / 致命错", SF2, YEL),
    ("finish_turn\n记记忆+会话+用量 → COMPLETED", SF, GREEN),
]
y = 12.0
for i, (t, face, edge) in enumerate(steps):
    if t.startswith("异常"):
        ax2.add_patch(plt.Polygon([[5.5, y + 0.1], [7.6, y - 0.45], [5.5, y - 1.0], [3.4, y - 0.45]], closed=True, facecolor=SF2, edgecolor=YEL, lw=2))
        ax2.text(5.5, y - 0.45, "异常？", ha="center", va="center", fontsize=12, color=TXT, weight="bold")
        ax2.text(8.3, y - 0.45, "否 ↓", fontsize=11, color=GREEN, va="center")
        ax2.text(3.0, y - 0.45, "是 →", fontsize=11, color=YEL, va="center", ha="right")
        y -= 1.35
        continue
    box(ax2, (2.2, y - 0.9), 6.6, 1.0, face, edge, t, 10.5)
    y -= 1.3
    if i < len(steps) - 1:
        arrow(ax2, (5.5, y + 0.42), (5.5, y + 0.08))
# 标注恢复分支的回跳说明。
ax2.text(0.4, 3.4, "恢复 loop 回到\n「图执行」", fontsize=10, color=YEL)
fig2.savefig("docs/arch2_turn.png", dpi=150, facecolor=BG, bbox_inches="tight")
print("arch2 ok")

# 绘制图 3：治理视图。
fig3, (a1, a2) = plt.subplots(1, 2, figsize=(13, 7))
fig3.patch.set_facecolor(BG)
for a in (a1, a2):
    a.set_facecolor(BG)
    a.axis("off")
a1.set_xlim(0, 10)
a1.set_ylim(0, 10)
a1.set_title("中间件洋葱（外层在前）", color=PINK, fontsize=15, pad=12)
rings = [("1 Hook\n唯一发射方", 1.0), ("2 Permission\npeek+deny/interrupt", 1.9), ("3 Safety\n只否决不升级", 2.8), ("4 Prompt\n每轮注入 system", 3.7)]
for t, inset in rings:
    box(a1, (inset, 1.2 + inset * 0.55), 10 - 2 * inset, 8 - inset * 1.1, SF if inset < 3 else SF2, DEEP, t, 11)
box(a1, (4.15, 3.6), 1.7, 1.1, DEEP, DEEP, "执行", 12, "#0A0A0A")
a2.set_xlim(0, 10)
a2.set_ylim(0, 10)
a2.set_title("权限判定优先级（高→低）", color=PINK, fontsize=15, pad=12)
levels = [("0 地板\n危险 allow→deny", HOT), ("1 mode deny", RED), ("2 session 授权", PINK), ("3 mode allow/ask", BLUE), ("4 user/project 文件", DIM), ("5 built-in 默认", GREEN)]
y = 8.8
for t, c in levels:
    box(a2, (1.0, y - 0.75), 8.0, 0.95, SF, c, t, 11)
    y -= 1.22
fig3.savefig("docs/arch3_govern.png", dpi=150, facecolor=BG, bbox_inches="tight")
print("arch3 ok")
