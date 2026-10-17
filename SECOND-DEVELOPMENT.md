# 二次开发说明（SECOND-DEVELOPMENT）

本文件说明 **wfclient 相对上游 WFSim 改了什么、怎么复现、怎么再同步**。
许可与命名问题见 [`NOTICE.md`](NOTICE.md)。

- 上游：`https://github.com/magenie33/wfsim`（AGPL-3.0-or-later）
- 本分支对齐的上游提交：`8cd2ec649fc5845b13ae560b793c714fa29f415a`（`main`，2026-10-16）
- 本分支的改动**全部公开**：`src/`（客户端）、`ci/patches/`（引擎补丁）、`ci/`（构建与闸门工具）

---

## 1. 组成与来源

| 目录 | 来源 | 许可 |
|---|---|---|
| `src/` | WFSim 便携客户端源码（上游分发的便携包内源码）+ 本分支修改 | AGPL-3.0-or-later |
| `ci/patches/*.patch` | **本分支自研**，对上游引擎源码的补丁 | AGPL-3.0-or-later |
| `ci/core-host-template/` | 上游 `core-host` 模板 + 本分支所需的最小改动 | AGPL-3.0-or-later |
| `ci/*.py`、`ci/tools/*` | **本分支自研**（构建、打补丁、闸门、打便携包） | AGPL-3.0-or-later |
| `data/config.json` | 现场配置（**只含设置**：无账号、无 PAT、无设备身份） | 属于本项目 |
| 游戏数据 | **不在本仓库**；引擎在构建时从上游拉取 | 见上游 `LICENSE-DATA.md` |

上游 `main` 当前不再包含 Python 客户端源码路径（桌面包已迁到 Rust/Tauri）。因此 `src/` 的来源是**上游分发的便携客户端**，本分支只在其上做修改；我们**不声称**能把它逐文件对应到某个上游提交，能对应到提交的是**引擎树**（`ci/fetch_sources.py` 会记录实际拉取的提交与全部文件 sha）。

---

## 2. 引擎补丁（`ci/patches/`）

补丁是引擎侧改动的**唯一真源**；上游源码由 `ci/fetch_sources.py` 按清单拉取后再打补丁。

| 补丁 | 大小 | 作用文件 | 作用 |
|---|---|---|---|
| `wfsim-build-profile.patch` | 466 B | `Cargo.toml` | 增加 `maxperf` / `devfast` 构建 profile（`codegen-units=1`、fat LTO 等）；由客户端 `ensure_build_profiles()` 使用，不经 `patch.exe` |
| `wfsim-compute-optimization.patch` | 14,197 B | `engine/src/fight/monte_carlo.rs`、`webapi/src/simulate.rs`、`webapi/src/board_rows.rs` | 战斗折叠循环的并行切分与合流优化 |
| `wfsim-fleet-drive.patch` | 11,234 B | `core-host/src/main.rs` | 引擎本地 HTTP 面：`/api/board/row`、`/api/optimize` 等驱动接口 |
| `wfsim-fold-pipeline.patch` | 7,967 B | `engine/src/fight/monte_carlo.rs` | 折叠流水线（把逐行的串行尾巴藏进下一行的计算里） |
| `wfsim-work-statistics.patch` | 2,716 B | `webapi/src/optimize.rs` | 紫卡优化器报告里的工作统计字段（`work` / `builds` / `fights`） |

补丁都带**幂等标记**：客户端每次同步后按标记判断"已应用 / 未应用"，重复同步不会二次打补丁。

### 引擎侧的构建加速（不改算法）

- **MSVC 工具链**而不是便携 GNU：**2.19×**（同一份源码、同一台机器、逐位相同）
- **PGO**：用 40 条 board + 1 条紫卡训练，再以 `-Cprofile-use` 重编：**1.53×**
- **mimalloc** 全局分配器（可选 feature `fast-alloc`，没有它自动退回系统分配器）：约 **6%**
- **`-C target-cpu`**：本地用 `native`（本机 Zen 4 比 `x86-64-v3` 快 12.5%）；CI 用 `x86-64-v3` 以适配通用托管机

---

## 3. 客户端改动（`src/`）

| 位置 | 改动 |
|---|---|
| `fleet.py` / `api.py` | **协议 8 单一通路**：协议 7 的取活/交活/对账路径全部删除，只剩 `POST /api/board/sync` 一条线；服务器若拒绝协议 8，客户端**休息**而不是回退到 7 |
| `api.py` | 新增 `Engine.board_rows_batch()`：一次 `POST /api/board/rows` 投递多行 |
| `fleet.py` | 批量投递接线（`_take_slot_batch` / `_run_slot_batch` / `_run_one_reserved`），配置键 `board_batch_rows`（默认 8） |
| `fleet.py` | 空闲引擎回收：`_idle_release` / `_idle_wake`，配置键 `idle_recycle_s`（默认 60，保底 `idle_keep`=1） |
| `fleet.py` | `states` 只询问**已交过**的答案，修掉把服务器 `gone` 误读成"自己的答案丢了"导致白扣任务的问题 |
| `fleet.py` | 备胎/退役 verifier 不再压锚点（不再谎报"等待队列"） |
| `app.py` | 状态列显示"计算中 / 等待队列 Ns"，队列空时不再显示发呆 |
| `config.py` | 新增 `board_batch_rows` / `idle_recycle_s` / `idle_keep` 默认值 |
| 全部 `src/*.py` | 加了本分支声明头（AGPL 要求标注修改、保留声明） |

客户端的**自愈链（发现上游更新 → 拉源码 → 打补丁 → 编译 → 安装 → 重启）是上游自带能力**，本分支没有改；改动只在于它现在拿到的是一个已经过闸门的预编译引擎（见 `ci/`），而不是每次都必须现场用便携 GNU 链编。

---

## 4. 怎么复现

```powershell
# 0) 依赖：VS 生成工具（link.exe；装最新的 VS 2026，2022 起都能用，工具集自动发现）、
#    rustup 1.97.1-x86_64-pc-windows-msvc（含 llvm-tools-preview）、Python 3.11
# 1) 拉上游源码 + 打补丁（写入 build/ci-src/）
python ci/fetch_sources.py
# 2) 构建 MSVC + PGO 引擎，跑完三道逐位闸门才落盘
python ci/build_engine.py --crt-static --target-cpu x86-64-v3 --rivens-gate 3
# 3) 打便携包（引擎 + src + 便携 Python + 依赖 + 文档 + 启动脚本）
python ci/pack_portable.py --build-dir dist/staging --keep-build
```

`ci/build_engine.py --gates all` 跑完三道闸门（任一道 FAIL 即**不产出**）：

1. 100 条 board（`ci/tools/corpus.json`）
2. 100 条 + 5 个站点 WASM 锚点（`--anchors`）
3. 紫卡优化器报告（`--rivens-gate 20` 全量；`3` 用 3 条快筛，`0` 跳过）

CI 里这四路闸门是**四个并行 job**（`ci/gate_engine.py`：`gate-board` 跑前两道、`gate-riven` 矩阵各跑一条紫卡），
对同一个 `build` job 上传的 exe 验；任一路失败 ⇒ 整个工作流失败 ⇒ `release.yml` 不发版。

口径：`score`（浮点精确）、`metric`、`work`（整数）、`counts`（字典全等）。

---

## 5. 与上游再同步的政策

- 上游一有新 revision，本站点会给出新的 engine id；客户端的自愈链会自己拉源码重编（慢 3.4×），**本仓库的 CI 负责补上快速版**。
- 补丁若因上游改动而**打不上**：`ci/fetch_sources.py` / `ci/build_engine.py` **直接失败**，不会产出"看起来能跑但和上游不一致"的引擎。此时需要人工把补丁 rebase 到新上游。
- 本分支上游已吸收过的改动：mimalloc 可选 feature、`/api/board/rows` 批量路由、`/api/quit` 与 `pgo-exit` 构建开关、`INNER` 分配器 cfg 门控（见上游 `core-host/`）。因此**引擎侧手改已全部归零**，只剩 `ci/patches/` 里的补丁——这是故意的：手改会被上游同步覆盖，补丁不会。
- 上游的 `AGENTS.md` / `CONTRIBUTING.md` / `CLA.md` 是本分支贡献回上游时的规则，本仓库不改变它们。

---

## 6. 已知边界

- **平台**：仅 Windows x86-64。便携包里的 Python 是 Windows 嵌入版，`run-portable.bat` 也是 Windows 批处理。
- **CPU 指令集**：预编译引擎按构建机的 `target-cpu` 编译；换机后如果 CPU 不同，客户端会提示重建（`_cpu_rebuild_prompt`），按 `y` 即用便携 GNU 链重建（慢但正确），或由 CI 的 `x86-64-v3` 产物替代。
- **PGO 训练集**：当前是 40 条 board + 1 条紫卡；训练集换了，提速比例会变（实测 27%–35% 区间）。
- **不改变结果**：所有优化都只影响速度；每次交付都以逐位闸门为准。
