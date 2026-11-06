# C 阶段 musl 接线评估（2026-10-29）

## 结论先行

**不实施**。理由：影响面 4/24 题（17%），预计 1.5–2.5 天工程且存在技术不确定性；当前性价比低于 token 控制与机制保护。以下是实测依据，供将来需要时直接启动。

## 问题

musl 基础镜像的任务（`gravitational/teleport`×2、`protonmail/webclients`×2）在安装产品时失败：

```
sqlite-vec==0.1.9 has no wheels with a matching platform tag (e.g., musllinux_1_2_x86_64)
```

产品依赖 `sqlite-vec==0.1.9`（`requirements.txt` hash 锁定），而该版本**只发布预编译 wheel**，无 musllinux 变体。

## 实测证据

| 检查项 | 结果 |
|---|---|
| PyPI 0.1.9 文件清单 | 5 个 bdist_wheel：macosx×2、manylinux×2、win_amd64 —— **无 sdist** |
| alpine:3.20 编译链 | 可装（build-base/python3-dev/sqlite-dev，50 包 392 MB，清华 alpine 源） |
| `pip wheel --no-build-isolation sqlite-vec==0.1.9` | **失败**：无可用分发（无 sdist 可编译） |
| GitHub 源码结构 | 仓库无 `python/` 目录；`bindings/python` 仅 `extra_init.py`；C 源码 `sqlite-vec.c` 在根目录 |

**关键发现**：不能靠 `pip` 触发源码构建——必须自行编译 `vec0.so` 并手工组装 musllinux wheel。

## 可行路径（若将来要做）

1. 取源码 `v0.1.9`，用 musl 工具链编译 loadable 扩展（`make loadable` → `vec0.so`）。
2. 组装 wheel：`sqlite_vec/` 包 + 平台 tag `musllinux_1_2_x86_64`，sha256 记录。
3. adapter 分支：检测 musl → 上传该 wheel → `uv pip install --no-index --find-links <dir> sqlite-vec==0.1.9`（需绕过或补充 hash 校验）。
4. 验证：sqlite3 `load_extension` 在 musl 上可用、`python -c "import sqlite_vec"` 通过，然后跑 4 道题。

## 工作量与风险

| 项 | 估计 |
|---|---|
| 编译 + 组装 wheel | 0.5–1 天 |
| adapter 分支 + hash 处理 | 0.5 天 |
| 4 题 × 多次验证 | 0.5 天 |
| **合计** | **1.5–2.5 天** |

风险：
- sqlite-vec 0.1.9 的 Python 包结构需逆向确认（无 sdist，只能从 wheel 反推布局）；
- musl 下 `load_extension` 行为与 glibc 可能不同；
- 绕过 hash 校验会偏离"官方条件"，需要在报告中显式标注。

## 替代方案（已排除）

- **改基础镜像**：官方任务镜像固定，不可改。
- **产品把 sqlite-vec 变可选依赖**：会改变被评测的产品条件，与"以发布版为基线"冲突。
- **换用 manylinux wheel**：glibc .so 无法在 musl 上加载。

## 建议

维持现状：在报告中把 4 题标注为"结构性不可评测"，并在覆盖率计算中单列。若未来需要完整 24/24 覆盖，再按上述路径投入 1.5–2.5 天。

## 复现命令

```powershell
# PyPI 清单
curl.exe -sS https://pypi.org/pypi/sqlite-vec/0.1.9/json
# alpine 编译 spike
docker run --rm -v C:\sb\tmp\musl_spike.sh:/spike.sh:ro -v C:\sb\tmp\musl-wheel:/out --entrypoint /bin/sh alpine:3.20 /spike.sh
```
