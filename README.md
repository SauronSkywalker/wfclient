# wfclient

**WFSim 的第三方客户端分支 —— 基于 [WFSim](https://github.com/magenie33/wfsim)，自带「快一倍以上」的预编译引擎与一套可复现的 CI。**

> ⚠️ 这是 **非官方** 分支，与 WFSim 官方、Digital Extremes 均无关联。
> *Warframe* 是 Digital Extremes Ltd. 的商标。本仓库按上游 `TRADEMARK.md` 的要求使用了**自己的名字**，并在此声明它**基于 WFSim**。

---

## 这是什么

上游 WFSim 是一个志愿计算项目：客户端向站点申请一批小计算任务，算完把结果交回去。上游官方客户端是 Python + 便携 Rust(GNU) 工具链，能在**任何 Windows 机器上从源码自建**引擎——这是它的设计目标，也是它的速度上限。

本分支做的事只有一件：**在完全不改变计算结果的前提下，把那台引擎做快。**

| | |
|---|---|
| 引擎链 | MSVC 1.97.1 + **PGO** + **mimalloc** + `-C target-cpu=native` + 静态 CRT |
| 实测加速 | **3.43×**（同一份上游源码、同一台机器、同一个 revision：100 条 board 从 102.7 s → 29.8 s） |
| 逐位一致 | 100 条 board + 5 个站点 WASM 锚点 + 20 条紫卡，`score / metric / work / counts` **全部逐位相同**，0 条不一致 |
| 运行时依赖 | **零**（静态 CRT，只导入系统 DLL，不需要 VC++ 运行库） |
| 客户端改动 | 协议 8 单一通路；引擎侧批量投递 + 流水线；空闲引擎回收 |

加速来自四处叠加，都可以单独复现：

- **编译器链** MSVC vs 便携 GNU：**2.19×**（最大的一项）
- **PGO**（用 40 board + 1 紫卡训练）：**1.53×**（+34.8%）
- **mimalloc** 全局分配器：board **6.3%** / 紫卡 **6.1%**
- **批量投递 + 流水线**（`POST /api/board/rows`）：单发动机侧 3.9%，客户端侧 B/S 0.948–0.964

> 逐位一致性不是"应该一样"，是**每次交付前跑闸门验出来的**（见下文）。上游 `docs/DISTRIBUTION.md` 说 "The channel may not fork"——站点公布的答案才是唯一定论；本分支的引擎必须与站点接受的值逐位相同，否则不许发版。

---

## 怎么用

### 方式 A：下载现成的便携包（推荐）

到 [Releases](../../releases) 下载 `wfsim-portable-*.zip`，解压，双击 **`run-portable.bat`**。

包里已经有：打好补丁并编译好的引擎、客户端 `src`、便携版 Python 与全部依赖、启动脚本、使用说明（`README-便携包.md`）。**无需安装任何东西**，不需要 Rust、不需要 VS、不需要联网构建。

现场旋钮（`data/config.json`）：

| 键 | 值 | 作用 |
|---|---|---|
| `board_batch_rows` | `8` | 一次向引擎投 8 行，摊薄每行固定的串行段 |
| `idle_recycle_s` | `60` | 空闲 60 秒回收闲置引擎（保底留 1 个），醒来前先把引擎拉起来所以不吃首次卡顿 |
| `task_logging` | `"verbose"` | 验收期建议 `verbose`；日常可用 `"off"`（`full` 会吃掉约 4.5 个百分点算力） |

### 方式 B：让客户端自己从上游源码重建

上游发布新 revision 时，客户端会**自动**发现（`wire engine` vs `served engine` 不一致）、自动拉源码、自动打补丁、自动编译、自动安装并重启——这条链是上游自带的，本分支没有动它。

代价：它只能用便携 GNU 链编（包里没有 C 编译器），产物是**交付件的 1/3.4 速度**。所以本仓库提供 CI 来补上"快速版"。

### 方式 C：自己编快速版

```powershell
# 需要 VS2022 BuildTools（提供 link.exe）与 rustup 的 1.97.1-x86_64-pc-windows-msvc
python ci/fetch_sources.py                 # 拉上游源码 + 打补丁
python ci/build_engine.py --crt-static     # MSVC + PGO，跑完闸门才落盘
```

`ci/build_engine.py` 会自己完成 PGO 三轮（generate → 训练 → use），然后在 100 board + 5 锚点 + 20 紫卡三道逐位闸门全绿后，才把引擎写进 `dist/`。任一道闸门失败即**拒绝产出**。

---

## 仓库结构

```
src/                    客户端（上游源码 + 本分支改动，AGPL-3.0-or-later）
data/config.json        现场配置（只含设置，不含游戏数据）
run-portable.bat        便携包启动脚本（包内路径）
ci/patches/             本分支对上游的 5 个补丁（唯一真源）
ci/core-host-template/  客户端自建引擎用的 core-host 模板
ci/tools/               逐位闸门与语料（run_corpus / verify_batch / verify_pipeline / pgo_train / corpus.json / riven3.json）
ci/fetch_sources.py     按清单拉上游源码并应用补丁
ci/build_engine.py      MSVC+PGO 构建 + 三道逐位闸门
ci/pack_portable.py     打便携包（引擎 + src + 便携 Python + 依赖 + 文档 + 启动脚本）
ci/requirements.txt     依赖清单（textual 等）
.github/workflows/      build-engine.yml（补丁变→最新引擎）、release-portable.yml（引擎→zip Release）
```

---

## 两个 CI 工作流

1. **`build-engine.yml`** —— 定时（每 6 小时）+ 手动 + `ci/patches/**` 变动时：拉**最新**上游源码 → 应用 `ci/patches/*.patch` → MSVC+PGO 构建 → 三道逐位闸门 → 上传 `wfsim-core.exe` 与 `engine-identity.json`（engine id / sha256 / 闸门结果）。
2. **`release-portable.yml`** —— 上一个工作流成功后（或手动）：下载引擎产物 → `ci/pack_portable.py` 打出便携包 → 跑 5 个 WASM 锚点冒烟 → 发布 GitHub Release。

CI 里引擎用 `-C target-cpu=x86-64-v3`（一档 AVX2 基线）而不是 `native`：那边是通用托管机，`native` 只能在构建机上跑。本地自建仍建议 `native`（本机 Zen 4 下比 v3 快 12.5%）。

私网环境下想给 CI 一个 token（提高 GitHub API 配额），设环境变量 `WFSIM_GITHUB_PAT`（CI 里自动用 `GITHUB_TOKEN`）即可；不设也能跑（匿名 60 次/小时）。

---

## 逐位一致性：怎么验的

三道闸门都跑在**同一份语料 + 站点冻结值**上：

- **100 条 board**（7 种武器 / 16 组 ruler，单条 0.47–2.44 s）
- **5 个 WASM 锚点**（上游公布的跨平台逐位锚定值：`burston_prime` / `ocucor` / `dual_toxocyst` / `torid` / `boar_prime`）
- **20 条紫卡**（整个优化器报告冻结：`work` / `final_work` / `builds` / `results`）

比对口径：`score`（浮点精确相等）、`metric`、`work`（整数）、`counts`（字典全等）。任何一项不等即 FAIL，构建产物直接丢弃。

---

## 开源与合规

- **许可**：AGPL-3.0-or-later（与上游一致）。`src/`、`ci/` 的修改同样以 AGPL-3.0-or-later 发布；上游版权与许可头保留。
- **数据**：游戏数据不在本仓库内，按上游 `LICENSE-DATA.md` 处理。
- **命名**：按上游 `TRADEMARK.md` —— 派生产物用**自己的名字**、自己的图标、自己的域名，并说明"基于 WFSim"。本仓库名为 **wfclient**，README（本文件）、`NOTICE.md`、发布页说明均已注明。
- **不做的事**：不冒充官方、不使用 WFSim 名称与图标发布、不把站点答案当成"我们自己的答案"。本站答案与站点一致是**验出来的**，不是承诺出来的。
- **上游**：`https://github.com/magenie33/wfsim`，本分支对齐的上游提交：`8cd2ec649fc5845b13ae560b793c714fa29f415a`。

详见 [`NOTICE.md`](NOTICE.md) 与 [`SECOND-DEVELOPMENT.md`](SECOND-DEVELOPMENT.md)。

---

## English (short)

**wfclient** is an unofficial third-party fork of [WFSim](https://github.com/magenie33/wfsim). It ships a prebuilt MSVC+PGO+mimalloc engine that is **3.43× faster** than the portable GNU build of the identical source, with **zero runtime dependencies** (static CRT) — and it is **bit-identical** to the site's accepted values (verified against 100 board rows, 5 cross-platform WASM anchors and 20 riven reports before every release).

Download `wfsim-portable-*.zip` from [Releases](../../releases), unzip, run `run-portable.bat`.
Requires Windows x86-64. Licensed AGPL-3.0-or-later, same as upstream. Not affiliated with WFSim or Digital Extremes; *Warframe* is a trademark of Digital Extremes Ltd. Naming follows upstream `TRADEMARK.md`.
