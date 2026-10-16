# ci/ — 快速引擎的 CI 与本地复现

上游每次发新 revision，客户端会**自己**从源码重编引擎（便携 GNU 链，慢约 3.4×）。
本目录负责补上"快速版"：**MSVC + PGO + mimalloc** 的预编译引擎，并在发布前用逐位闸门证明它与站点接受的值完全一致。

## 两个工作流（`.github/workflows/`）

| 工作流 | 触发 | 做什么 |
|---|---|---|
| `build-engine.yml` | 定时（每 6 小时）/ 手动 / `ci/patches/**` 变动 | 拉最新上游源码 → 打补丁 → MSVC+PGO 构建 → 三道闸门 → 上传 `wfsim-core.exe` + `engine-identity.json` + 闸门日志 |
| `release-portable.yml` | 上一个工作流成功后 / 手动 | 下载引擎 → `pack_portable.py` 打便携包 → 跑 5 个 WASM 锚点冒烟 → 发 GitHub Release |

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
| `--rivens-gate` | `0` | `0` 跳过；`3` 用 3 条快筛；`20` 跑满 20 条（约 7 分钟） |
| `--skip-gates` | 关 | 调试用；**发版绝不要加** |

`ci/pack_portable.py`：`--engine` / `--identity` / `--out` / `--build-dir` / `--python-version` / `--stamp` / `--keep-build`。

## 凭据

- 引擎源码走 GitHub：`GITHUB_TOKEN`（CI 自带）→ `WFSIM_GITHUB_PAT`（本地可选）→ 匿名 60 次/小时。
- **不要把 PAT 写进任何提交文件**；`pack_portable.py` 打包时会把 `data/config.json` 的 `github_pat` 清空。

## 闸门（发布前必须全绿）

| 闸门 | 内容 | 参考耗时 |
|---|---|---|
| `board-100` | 100 条 board 语料（`ci/tools/corpus.json`） | ~30 s |
| `wasm-anchors-105` | 100 条 + 5 个站点 WASM 锚点 | ~36 s |
| `riven-20` | 20 条紫卡优化器报告（`ci/tools/riven_corpus.json`） | ~7–8 分钟 |

口径：`score`（浮点精确）、`metric`、`work`（整数）、`counts`（字典全等）。任一道 FAIL ⇒ **不产出 `dist/wfsim-core.exe`**。

## 补丁政策

- `ci/patches/*.patch` 是引擎侧改动的唯一真源；`fetch_sources.py` 每次覆盖式拉上游源码后再应用它们。
- 补丁打不上（上游改了同一处）⇒ 脚本**直接失败**，不会静默产出与上游不一致的引擎；人工 rebase 后重跑。
- 上游已吸收过的改动不要再写回补丁（会重复应用报错）：mimalloc 可选 feature、`/api/board/rows`、`/api/quit` + `pgo-exit`、`INNER` 分配器 cfg 门控。
