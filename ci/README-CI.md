# ci/ — 快速引擎的 CI 与本地复现

上游每次发新 revision，客户端会**自己**从源码重编引擎（便携 GNU 链，慢约 3.4×）。
本目录负责补上"快速版"：**MSVC + PGO + mimalloc** 的预编译引擎，并在发布前用逐位闸门证明它与站点接受的值完全一致。

## 三个工作流（`.github/workflows/`）

| 工作流 | 触发 | 做什么 |
|---|---|---|
| `build-engine.yml` | 定时（每 6 小时）/ 手动 / `ci/patches/**` 变动 | **3 个 job**：`build`（拉最新上游源码 → 打补丁 → MSVC+PGO 构建 → 上传 exe+身份 json）、`gate-board`（100 board + 5 锚点）、`gate-riven`（矩阵 3 条紫卡各一个 job，并行） |
| `release.yml`（工作流名 `release`） | 上一个工作流**全部 job 成功后** / 手动 | 发**两条独立 Release**：`engine-<id>`（只放引擎 exe + 身份 json）与 `portable-<id>`（完整便携包 zip，标 latest）；便携包那条打包前跑 5 个 WASM 锚点冒烟 |
| `repack.yml`（工作流名 `repack`） | `src/**`、`ci/**`、说明文档 变动 / 手动 | **只重打包**，两个 job：`repack` 从已发布的 `engine-<id>` release 取回引擎（校验 sha256 与身份 json 一致）→ 重打包 zip → 锚点冒烟 → 覆盖同名 `portable-<id>` 资产；`selfbuild` 不打引擎，只发出 **`selfbuild-<id>`**（源码 + `ci/` + 补丁 + `自构建说明.md`，339 KB，几秒），给想自己编译的人。不编译引擎、不新建引擎标签、不 `git push` |

`release.yml` 靠 `workflow_run: completed + conclusion == success` 触发 ⇒ 任一闸门 job 失败就**不会发版**（这就是"闸门串在发布前面"的实现方式，而不是靠脚本里的顺序）。

`repack.yml` 存在的理由：改了客户端 `src/` 或打包脚本时引擎一个字节都没变，但需要一个新 zip。以前只能手填 `run_id` 去 dispatch `release`，或干脆重跑一遍编译；现在推送即重打包，且**永远不会**触发 `build-engine`（它的 path 过滤里没有这些路径）。

CI 用 `--target-cpu x86-64-v3`（通用 AVX2 基线）；本地自建建议 `native`。

## 本地复现（三条命令）

```powershell
python ci/fetch_sources.py                                   # 上游源码 + 补丁 → engine-src/work
python ci/build_engine.py --crt-static --rivens-gate 3        # MSVC+PGO 构建 + 闸门 → dist/
python ci/pack_portable.py --build-dir dist/staging --keep-build   # → dist/wfsim-portable-*.zip
```

要求：Windows、VS2022 BuildTools（`link.exe`）、rustup `1.97.1-x86_64-pc-windows-msvc`（含 `llvm-tools-preview`，PGO 的 `llvm-profdata` 来自它）、Python 3.11。

## 参数

`ci/fetch_sources.py`

| 参数 | 默认 | 说明 |
|---|---|---|
| `--root` | `.` | 仓库/包根（含 `src/` 与 `ci/`） |
| `--ref` | `main` | 上游 ref |

`ci/build_engine.py`

| 参数 | 默认 | 说明 |
|---|---|---|
| `--root` / `--work` / `--out` | `.` / `engine-src/work` / `dist` | 路径 |
| `--target-cpu` | `x86-64-v3` | 本地可传 `native` |
| `--crt-static` | 关 | 静态链接 CRT（目标机不需要 `vcruntime140*.dll`） |
| `--board` / `--riven` | `40` / `1` | PGO 训练量 |
| `--rivens-gate` | `0` | `all` 口径下：`0` 跳过；`3` 用 3 条快筛；`20` 跑满 20 条（约 7 分钟） |
| `--gates` | `all` | `all` 本机一把跑完（board + 锚点 + 紫卡）；`board` / `riven` 只跑其中一段；`none` 只构建（CI 的 `build` job 用它，闸门交给独立 job） |
| `--reuse-profdata` | 关 | 复用 `<out>/pgo/default.profdata`（CI 用缓存恢复它）；缺失或与源码不符时自动回到完整训练 |
| `--skip-gates` | 关 | `--gates none` 的别名；调试用；**发版绝不要加** |

`ci/gate_engine.py`（独立闸门 job 用，不需要 rust 工具链、不需要源码树）

| 参数 | 默认 | 说明 |
|---|---|---|
| `--exe` | `dist/wfsim-core.exe` | 待验引擎（由 `build` job 的 artifact 下载而来） |
| `--gate` | 必填 | `board`（100 board + 5 锚点，串行两段）或 `riven` |
| `--riven-only` | 空 | 只跑一条紫卡（id 或 1 起序号）；**选不中直接失败**，绝不静默跳过 |
| `--riven-corpus` | `ci/tools/riven_corpus.json` | 并行切片用 `ci/tools/riven3.json` |
| `--port` / `--log-dir` | `9501` / exe 所在目录 | 每段闸门各写 `gate-<label>.log`（失败时的唯一证据） |

`ci/pack_portable.py`：`--engine` / `--identity` / `--out` / `--build-dir` / `--python-version` / `--stamp` / `--keep-build`。

## CI 缓存（每次省掉大半轮）

`build-engine.yml` 用 `actions/cache@v6` 恢复三类产物，key 里带补丁/语料的哈希，`restore-keys` 前缀兜底：

| 缓存路径 | 内容 | 说明 |
|---|---|---|
| `engine-src/work` + `~/.cargo/{registry,git}` | 上游源码树 + crate 索引 | 命中时 `fetch_sources.py` 按 `.synced-commit` 标记**整段跳过**下载 |
| `dist/target` | MSVC 编译缓存（约 540 MB） | 冷启动编 mimalloc/serde/optimizer/webapi 两遍；命中后只编变了的 crate |
| `dist/pgo` | 训练好的 `default.profdata`（约 3 MB） | 配 `--reuse-profdata` 命中时**跳过仪器化构建 + 训练**（约 6 分钟） |

profile 只有在控制流真的移动时才会失效，而失效也只是 rustc 的警告；发不发版始终由三道闸门决定。
`--reuse-profdata` 缺档、或最终构建真的拒绝该 profile 时，脚本会自动回到完整训练（`train()`）再编一次。

缓存体积（API 实测，压缩后）：`dist/target` 95.4 MB、`dist/pgo` 6.0 MB、源码树+cargo 索引 7.8 MB ⇒ 每轮新增约 **109 MB**；
仓库缓存配额 10 GB ⇒ 约 90 轮才轮到 LRU 回收（6 小时一轮 ≈ 三周），且 `restore-keys` 会兜住稍旧的条目。

PGO 的 profile 目录必须是**纯 ASCII 路径**（`-Cprofile-generate=<dir>` 会被编进二进制，运行时用窄字符 `fopen` 静默失败）：
`--out` 含非 ASCII 时脚本自动改用 `%TEMP%\wfsim-pgo`，CI 的 `D:\a\wfclient\wfclient\dist` 天然满足。

## 凭据

- 引擎源码走 GitHub：`GITHUB_TOKEN`（CI 自带）→ `WFSIM_GITHUB_PAT`（本地可选）→ 匿名 60 次/小时。
- **不要把 PAT 写进任何提交文件**；`pack_portable.py` 打包时会把 `data/config.json` 的 `github_pat` 清空。

## 闸门（发布前必须全绿）

| 闸门 | 内容 | CI runner 实测 | 跑在哪个 job |
|---|---|---|---|
| `board-100` | 100 条 board 语料（`ci/tools/corpus.json`） | 63 s（缓存前 30 s 是本机） / 114 s | `gate-board` |
| `wasm-anchors-105` | 100 条 + 5 个站点 WASM 锚点 | 63 s / 135 s | `gate-board`（同一 runner 串行） |
| `riven-3` | 3 条紫卡优化器报告（`ci/tools/riven3.json`，各一条） | 130 s / 295 s | `gate-riven` × 3（**并行，各占一个 runner**） |
| `riven-20` | 20 条紫卡全量 | ~7–8 分钟 | `--rivens-gate 20`（发版前可手工跑） |

口径：`score`（浮点精确）、`metric`、`work`（整数）、`counts`（字典全等）。任一道 FAIL ⇒ 整个 `build-engine` 结论失败 ⇒ `release.yml` 不发版。

### 为什么拆成 4 个 job

闸门彼此独立，串行只是历史包袱：拆开后墙钟从"相加"变成 `max(board+anchors, 最慢那条紫卡)`。
实测两轮：run `38970844958` 闸门合计 256 s、run `38973837158` 合计 544 s（那轮紫卡 295 s）⇒ 现在约 `max(≈250, ≈300)`，整轮省 1.5–3 分钟。
成本可忽略：artifact `wfsim-core-and-identity` 只有 3.92 MB（14.9 MB 的 exe 压缩后），多三次下载 = 几秒；并发上限 Free 档也有 20 个 job，public 仓库的 Actions 分钟数免费。

## 补丁政策

- `ci/patches/*.patch` 是引擎侧改动的唯一真源；`fetch_sources.py` 每次覆盖式拉上游源码后再应用它们。
- 补丁打不上（上游改了同一处）⇒ 脚本**直接失败**，不会静默产出与上游不一致的引擎；人工 rebase 后重跑。
- 上游已吸收过的改动不要再写回补丁（会重复应用报错）：mimalloc 可选 feature、`/api/board/rows`、`/api/quit` + `pgo-exit`、`INNER` 分配器 cfg 门控。
