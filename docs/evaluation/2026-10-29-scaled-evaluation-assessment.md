# 规模化评测评估：测试集大小与云设施（2026-10-29）

## 一、测试集确实过小——量化证据

### 1. 置信区间过宽
| 样本 | 通过率 | 95% 置信区间（Wilson） | 半宽 |
|---|---|---|---|
| 14/20（当前 holdout 可评测） | 70.0% | [48.1%, 85.5%] | **±18.7pp** |
| 12/17（早期） | 70.6% | [46.9%, 86.7%] | ±19.9pp |
| 4/6（token A/B 子集） | 66.7% | [30.0%, 90.3%] | ±30.2pp |

**含义**：14/20 无法区分「真实 55%」与「真实 85%」。要压到 ±5pp 需 n≈400 题。

### 2. 重复实验的实证（本轮直接发现）
navidrome-0130 基线（无任何控制）3 次重复：**FAIL / FAIL / PASS（r3）** ——基线自���就有 1/3 通过。

**这直接推翻了我此前"keep3/40k 翻盘 navidrome"的单次结论**：既然基线本身就会偶尔 PASS，单次翻盘不足以证明优化有效。这是"样本过小"的活例证，已记入台账。

对照：flipt-756 基线 3/3 PASS（917k/1.66M/3.71M token），"token 控制伤害它"的结论相对可靠。

## 二、官方正式评测的构成（已核实）

来源：[SWE-bench Pro V2 README](https://github.com/scaleapi/SWE-bench_Pro-os/blob/main/v2/README.md)、[Scale Labs 博客](https://labs.scale.com/blog/swe-bench-pro-v2)、[Leaderboard](https://labs.scale.com/leaderboard/swe_bench_pro_public_v2)

| 项 | 内容 |
|---|---|
| 全量 | **642 题 / 11 仓库**，Harbor 格式，含 verifier、reference solution、公开镜像 |
| **HARD-51** | **51 道高难度子集**（来自顶级模型失败模式），官方专为快速、高区分度评测设计 |
| 锁定协议 | `v2/tooling/`：locked agents（mini-swe / codex / claude-code）+ **fresh-sandbox re-grader**（在干净镜像上重放 diff 评分）+ 反作弊 probe |
| 模型接入 | `--ae OPENAI_API_KEY=... --ae OPENAI_API_BASE=...` → **支持任意 OpenAI 兼容端点**（我们的 Magpie 网关可直接用） |
| 托管服务 | **无**。官方只提供任务 + 工具 + leaderboard，评测需自行执行后提交结果 |

## 三、HARD-51 子集分析（本地已具备）

本地 `v2/` 已有完整 `hard51_ids.txt`、`tooling/`、全部 51 个 task 目录（0 缺失）。

| 指标 | 数值 |
|---|---|
| HARD-51 总题 | 51 |
| 与已跑 holdout 24 题重叠 | **仅 1 题**（qutebrowser-c09e）→ 正交扩展 |
| musl 仓库不可评测 | 11（teleport 6 + protonmail 5）|
| **可评测** | **40** |

仓库分布：ansible 8、internetarchive 8、flipt-io 7、gravitational 6、protonmail 5、element 4、vuls 4、qutebrowser 4、navidrome 3、NodeBB 1、tutao 1。

**意义**：HARD-51 可评测 40 题 + 现有 holdout 20 题 = **60 题样本**，且是官方精选的高区分度题——正好解决"过小"问题，无需换数据集。

## 四、云设施方案（可行，关键约束是模型端点）

### 核心约束
本项目的模型是**本机 Magpie 网关 `auto/deepseek-v4.1-flash`**。云 VM 无法直接访问 `127.0.0.1:3425`。可行路径：
1. **隧道**（推荐）：cloudflared / frp / ngrok 把本地 Magpie 暴露为 HTTPS，云 VM 用 `--ae OPENAI_API_BASE=<隧道地址>`。零额外模型成本。
2. 云上部署同等模型：需权重/额度，且受"仅可 deepseek-v4.1-flash"约束。
3. 云厂商托管同名模型 API（若可用）。

### 架构
```
云 VM (16 vCPU / 64 GB / 500 GB SSD, Docker)
  ├─ Harbor + v2/tasks（40 或 642 题）
  ├─ 容器执行 + 官方 verifier（fresh-sandbox re-grade）
  └─ 出站 → 隧道 → 本地 Magpie（模型）
```

### 成本/时间估算
| 目标 | 规模 | 并行 | 预计 |
|---|---|---|---|
| HARD-51 可评测 40 题 ×1 | 40 trial | 12 | ~6–8 h |
| 40 题 ×5 重复 | 200 trial | 12 | ~1–1.5 天 |
| 全量 642 题 ×1（含 musl 处理后） | 642 trial | 16 | ~3–5 天（磁盘需 ~3 TB）|

云成本（Hetzner/AWS 按需，16 核 + 500 GB SSD）：约 **$0.3–1/小时**，上面最大规模也就 **$50–200**；真正的瓶颈是模型调用与时间，而非云费用。

## 五、建议路径（按性价比）

1. **立即可做（无需云）**：本地跑 HARD-51 可评测 40 题 × 1 次（逐题流水线，过夜 7–13 h）→ 样本 20→60 题，CI 从 ±19pp 收窄到约 ±12pp。
2. **需要正经结论时（推荐云）**：云 VM + cloudflared 隧道到本地 Magpie，并行 12 路，跑 **40 题 ×5 重复**（约 1–1.5 天），得到统计可辩护的结果。
3. **对外宣称/提交 leaderboard**：用官方 `v2/tooling` 的 fresh-sandbox re-grader 对 captured diff 重评（我们现有 `run_official_trial` 已符合官方 verifier 协议，可直接复用）。
4. **musl 11 题**：仍不可评测，先按 40/51 报告并单列。

## 六、结论

- **测试集过小是真的**（±19pp），且已用 navidrome 1/3 PASS 实证。
- **不必换数据集**：官方 HARD-51（本地已有、40 可评测、与现集仅重叠 1 题）就是为此设计的。
- **云设施可行且便宜**，唯一关键约束是模型端点需经隧道；否则本地逐题流水线也能跑 40 题。
- 推荐顺序：本地跑 HARD-51（今晚）→ 需要强结论时上云做 5 重复。
