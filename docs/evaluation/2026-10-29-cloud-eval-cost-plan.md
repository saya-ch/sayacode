# 云评测落地与成本方案（预算 ≤ ¥50）

## 一、结论

**¥50 完全够，甚至能做 40 题 × 5 重复。** 主要时间花在拉镜像，不是算力。

| 方案 | 规模 | 墙钟 | 成本 | 备注 |
|---|---|---|---|---|
| 本地（现状） | HARD-51 40 题 ×1 | ~16.7 h 串行 | **¥0** | 只耗时间，可过夜 |
| 云 8 并行 | 40 题 ×1 | ~2.5–3 h | **¥2–5** | 推荐 |
| 云 8 并行 | 40 题 ×5 重复 | ~12–15 h | **¥15–25** | 统计可辩护 |
| 云 8 并行 | 全量 642 ×1 | — | 磁盘需 3TB，**超预算** | 不做 |

按每次 ¥2–5 计，**¥50 可跑 10–25 个批次**。

## 二、选型（国内按量付费最省心）

### 推荐配置
- **厂商**：阿里云 ECS 或腾讯云 CVM（有账号即可，按量付费）
- **规格**：4 核 8 GB（ecs.c7.xlarge / S5.MEDIUM4 等）
- **系统盘**：40 GB
- **数据盘**：100 GB ESSD（逐题删镜像，峰值 ~70 GB 够用）
- **带宽**：**按使用流量**（不要选固定带宽！固定 100 Mbps 约 ¥6/h）；拉镜像是入站，基本免费
- **镜像**：Ubuntu 22.04 + Docker

### 成本构成（估算，以控制台为准）
| 项 | 单价 | 3 小时 | 15 小时 |
|---|---|---|---|
| 4核8G 按量 | ~¥0.9/h | ¥2.7 | ¥13.5 |
| 100G ESSD | ~¥0.02/h | ¥0.06 | ¥0.3 |
| 流量（入站免费） | — | ~0 | ~0 |
| **合计** | | **~¥3** | **~¥14** |

### 海外更便宜但有门槛
- **Hetzner CX41**（4 vCPU/16 GB/160 GB SSD）约 €0.036/h ≈ ¥0.28/h，¥50 可跑 ~170 h；但需海外支付 + 国内访问 ghcr 需代理，不推荐首选。

## 三、关键前置条件（决定能否跑通）

1. **Magpie 搬配置**：官方 Docker 镜像 `ghcr.io/yetone/magpie`，`/config` 卷 = 本地 `~/.config/magpie`。已按白名单导出到 `C:\sb\magpie-export\config`（含 providers/logins/settings/install-id，排除大日志）。
   - 风险①：登录凭证可能绑定 install-id（已一并导出）。
   - 风险②：同账号多设备可能互踢——评测期间暂停本地 Magpie，或用另一账号（`zgq051018lazt`）。
2. **云 VM 访问 Magpie 上游**：Magpie 会连其云端上游（commandcode-plan）。国内云访问境外模型 API 可能慢/不稳，需实测；必要时给 Magpie 配代理。
3. **镜像加速**：国内云访问 `ghcr.io` 慢，用 `ghcr.nju.edu.cn` 镜像（流水线已内置）。
4. **代码上传**：`sayacode-bench` + `v2/tasks` 元数据（不含镜像，约几十 MB）+ 产品 wheel，打包上传。
5. **结果回收**：跑完 rsync 回本地或传 OSS。

## 四、一键落地步骤（确认账号后我准备脚本）

```bash
# 1) 云 VM 上装 Docker
curl -fsSL https://get.docker.com | sh

# 2) 起 Magpie（挂载导出的配置）
mkdir -p /opt/magpie-config && # 上传 C:\sb\magpie-export\config\* 到此
chown -R 65532:65532 /opt/magpie-config
docker run -d --name magpie --restart unless-stopped \
  -p 127.0.0.1:3425:3425 \
  -v /opt/magpie-config:/config \
  ghcr.io/yetone/magpie:latest serve

# 3) 验证模型可用
curl -s http://127.0.0.1:3425/v1/models -H "Authorization: Bearer magpie" | head

# 4) 上传评测包并跑 HARD-51（8 并行）
python run_pipeline.py --tasks-file hard51_ids.txt --repeats 1 --concurrency 8 \
  --out /data/eval-hard51
```

## 五、需要你确认

1. **用哪家云**？（阿里云 / 腾讯云 / 其他；有账号吗）
2. **Magpie 账号策略**：评测期间停用本地、用第二账号，还是接受互踢风险？
3. 是否要我把**部署脚本 + 上传包**（含 magpie 配置、评测代码、产品 wheel）整理成一个压缩包，你传上去即可跑？

> 说明：`C:\sb\magpie-export\config` 含登录凭证，请仅在加密通道传输，云端限制 3425 端口仅本机（或 Tailscale/VPN），不要公网暴露。
