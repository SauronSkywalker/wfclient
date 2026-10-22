# WFSim 算力共享 — wfclient 便携版

> 本包是 [WFSim](https://github.com/magenie33/wfsim) 的第三方分支（wfclient）产物。非官方，与 WFSim 官方、Digital Extremes 无关联。详见包内/仓库的 `NOTICE.md`。

## 怎么用

1. 解压整个文件夹到目标电脑（Win10/11 x64）。**无需安装 Python，无需安装 VC++ 运行库**（引擎是静态 CRT，只依赖系统 DLL）。
2. 双击包里那个 `run-portable-<时间戳>.bat`：**首次启动会按本机探测生成 `data\config.json` 后自动退出**（包里刻意不带 `data/`，所以不会继承打包机的 CPU 列表、端口，也不会继承别人手写的引擎号）。
3. 打开生成好的 `data\config.json`，填写：
   - `account.email` / `account.password`：WFSim 网站账号（必填，邮箱为空会拒绝启动）。
   - `github_pat`：可选。GitHub fine-grained PAT，只给公开仓库读权限即可，用来提高自动检查更新时的 API 限额。
4. 再次双击**同一个**启动器即可开始计算（文件名带打包时间戳，每次发布是新的一个；
   `current.txt` 第 4 行写着当前该用哪一个）。

首次启动会自动生成设备 ID（`verifiers`），之后按 `active_verifiers` 启动若干计算槽位。
**不要**从旧版包里直接拷 `data/` 过来（那会把旧机器的 `engine_cores`/`work_lanes`/端口和手写引擎号一起带进来）。

## 这一包与上游便携版的差别

| | 上游便携版 | 本包 |
|---|---|---|
| 引擎 | 便携 GNU 工具链本地自建 | **随包预编译**：MSVC + PGO + mimalloc，静态 CRT |
| 速度 | 1× | **3.4×**（同一份源码、同一台机器：100 条 board 102.7 s → 29.8 s） |
| 结果 | 站点接受 | **逐位相同**（100 board + 5 WASM 锚点 + 20 紫卡全 PASS） |
| 运行时依赖 | 无 | 无（比上游更少：连 `msvcp`/`vcruntime` 都不需要） |
| 包体积 | 大（含工具链，可自建） | 小（不含工具链，**不能**自建引擎，靠发布包更新） |

引擎身份记录在 `engine\engine-identity.json`：engine id、release、sha256、三道闸门结果。可用它核对"我这个包里的引擎是哪一版"。

## 现场旋钮（`data\config.json`）

| 键 | 本包默认 | 说明 |
|---|---|---|
| `board_batch_rows` | `8` | 一次向引擎投 8 行。摊薄每行固定的串行尾巴；设为 `1` 即回到逐行投递 |
| `idle_recycle_s` | `60` | 空闲 60 秒回收闲置引擎（保底留 `idle_keep`=1 个）。下次有活时**先拉起引擎再算**，所以不吃首次卡顿；设为 `0` 关闭 |
| `task_logging` | `off` | 两档：`off` / `full`（关 / 完整）。**日常建议 `off`**（一个字节都不写）；排查/验收切到 `full`（每行大 body 也写进日志，代价约 4.5 个百分点算力）。旧配置里的 `standard`/`verbose`/`summary` 一律按 `full` 读 |

其余键与上游一致，没把握就不要改。

## 上游更新了怎么办

本包是**预编译包**（`engine-src/state.json` 里写着 `core_source: prebuilt`），所以只有一条更新线：

1. 客户端在同步应答里看到服务器下发了新 engine id（`wire engine` 与站点 `served engine` 不一致）时会提示"服务器已下发新引擎；后台下载中（计算不中断）"，从本仓库 `engine-<id>` Release 下载对应引擎、校验 sha256，**下载期间照常计算**，只在安装前短暂排空；
2. 想马上拿到整包也可以：到本仓库 [Releases](../../releases) 下载最新的 `wfsim-portable-*.zip`，解压覆盖（`data\config.json` 可以保留，把里面的 `verifiers` / `claimed` 一并保留就能延续本机身份）。

本包**不显示**"重建引擎 / 编译引擎"两个设置项：包里既没有 rust 工具链也没有源码树，本地编译这条路不存在。

想要本机最快的引擎有两条路：

- **推荐（不用装 rustup、不用拉源码）**：下载同一 engine id 的 `wfclient-deps-<id>.zip`，解压到本目录（与本文件同层，覆盖合并），然后双击 `build-engine.bat` 或在设置页点「编译本机最快引擎」。除 **VS** 以外全是自包含的（rust 工具链、离线 crate 缓存、PGO 剖面、补丁、已打补丁源码都在包里），编译**离线**完成，产物是 `engine-local/wfsim-core.native.exe`，**不覆盖**通用引擎；详见 [`ci/deps/README-自编译依赖.md`](ci/deps/README-自编译依赖.md)。
- **从源码编译**：下载源码包 `selfbuild-<id>`，依赖清单与官方安装渠道见 [`自构建说明.md`](自构建说明.md)。

两条路都装最新的 **VS 2026** 即可（2022 起都行）；native 一般比通用引擎快 **10%~15%**（通用引擎也是 PGO 构建，差别只在 CPU 目标指令集）。

## 文件夹里有什么

```
current.txt                 指针：python / src / engine / 启动器 四行
python-<hash>/              便携版 Python 3.11 + 依赖（textual 等）
src-<hash>/                 客户端源码（协议 8 单一通路 + 批量投递 + 空闲回收）
engine-<hash>/              预编译引擎 + engine-identity.json
engine-local/               本机自己的核心（自编译或下载替换），可整目录删掉
run-portable-<时间>.bat     本次发布的启动器（每次发布是新文件名）
data/config.json            现场配置（不含账号、不含 PAT、不含设备身份）
engine-src/state.json       引擎 revision 记录（客户端据此离线启动）
logs/                       运行日志（`task_logging` 为 `off` 时不生成）
```

三个带哈希的目录是**代目录**：新版本落在另一个名字下，`current.txt` 一行改名就切过去，
所以正在运行的 `python.exe` 不参与替换，也不会出现"新 app.py 配老 fleet.py"。旧的代在
切换后自动删掉（只留当前与上一代）。

## 自检

```powershell
# 引擎 sha256 是否与包内记录一致（核心在 engine-<hash>/ 里，名字随内容变）
python -c "import glob,hashlib,json,pathlib;p=pathlib.Path(sorted(glob.glob('engine-*/wfsim-core.exe'))[-1]);print(p);print(hashlib.sha256(p.read_bytes()).hexdigest());print(json.loads(pathlib.Path('engine-src/state.json').read_text())['sha256'])"
```

两行一致即包未被改动。
