# 自编译依赖包 —— 除 Visual Studio 以外全是自包含的

把这个包**解压到便携包目录里**（和 `run-portable.bat` 同一层，覆盖合并），客户端下次启动就会自动发现它：
设置页会多出「编译本机最快引擎」按钮，`build-engine.bat` 也能双击直接编译。

> 只想用通用引擎、不想自己编译的用户**不需要**这个包：便携包自带 GitHub Release 下发的 x86-64-v3（AVX2）通用引擎，下载引擎由客户端自动完成。

## 里面有什么（解压后约 400 MB）

| 路径 | 内容 | 为什么 |
|---|---|---|
| `toolchain-msvc/` | 裁剪过的 rust `1.97.1-x86_64-pc-windows-msvc`（含 `llvm-profdata.exe`） | MSVC 目标的编译器；PGO 需要 `llvm-profdata` |
| `build-deps/cargo/registry/` | 离线 crate 缓存（25 MB） | 编译时当 `CARGO_HOME`，不依赖 crates.io 网络 |
| `build-deps/bin/patch.exe` | msys patch + `msys-2.0.dll` | 给上游源码打 `ci/patches/*.patch` |
| `build-deps/pgo/default.profdata` | 本次 revision 的 PGO 剖面 | 省掉约 2 分钟的采样训练，直接出最快引擎 |
| `engine-src/work/` | 已打好补丁的上游源码 | 不需要再联网拉源码 |
| `ci/` | 补丁、构建脚本、三份冻结语料 | 构建与逐位闸门 |
| `build-engine.bat` | 双击即编译并安装 | 不想开 TUI 也能编 |

**唯一不在包里的依赖是 Visual Studio**：`cl.exe`/`link.exe` 和 CRT、Windows SDK 的导入库不允许再分发（微软只授权再分发运行时 DLL），所以需要你本机装一个：

```powershell
winget install --id Microsoft.VisualStudio.BuildTools -e --override "--quiet --wait --add Microsoft.VisualStudio.Component.VC.Tools.x86.x64 --add Microsoft.VisualStudio.Component.Windows11SDK.26100"
```

装最新的 **VS 2026** 即可（**2022 及以后都行**）。装完不用配任何环境变量：构建脚本用 `vswhere` 自动找最新工具集，也可以 `WFSIM_VS_BIN` / `--vs-toolset` 指定。

## 三种编译方式（效果一样）

1. **双击 `build-engine.bat`** —— 一条命令走完：MSVC + PGO 编译 → 五个站点锚点逐位校验 → 安装。
2. **TUI 设置页 →「编译本机最快引擎」** —— 客户端后台编译，期间照常计算，只在装引擎前排空一次；设置页同一行会显示编译结果（`已启用 PGO` / `未编译`）。
3. 手动：

```powershell
python\python.exe src\sync_engine.py --build-native    :: 编译 + 锚点校验 + 启用
python\python.exe src\sync_engine.py --native-status   :: 现在实际会跑哪个引擎
```

`--build-native` 是**离线**的：源码、crate 缓存、PGO 剖面都在包里，不访问 crates.io 也不访问 GitHub。编译产物是**本机专属**的（`-C target-cpu=native` + PGO），一般比通用引擎快 **10%~15%**（通用引擎也是 PGO 构建，差别只在 CPU 目标指令集）。

## 兜底：通用引擎永远都在

- 自动更新照旧从 GitHub Release 下载**通用引擎**（`engine-<id>`），写进 `engine/wfsim-core.exe`；
- 本机编译出的引擎单独放 `engine/wfsim-core.native.exe`，并把当时的源码 revision 记在 `engine-src/state.json`；
- 客户端**只在 `engine` id + `patches_sha` + 自身 sha256 三者都一致时**才跑 native，否则自动回落到通用引擎（设置页会显示「native 已过期」）；
- 用户自己设过 `engine_exe` 的机器**不受影响**：指向别处就永远跑那一个；
- 想撤销：删掉 `engine/wfsim-core.native.exe`，或直接删掉依赖包目录（见下）。

## 卸载依赖包

删掉 `toolchain-msvc/`、`build-deps/`、`build-deps.json`、`ci/`、`engine-src/work/`，再把 `engine/wfsim-core.native.exe` 删掉即可恢复成纯通用便携包。`build-cache/`（编译中间产物，可能上百 MB）也可以随时删。

## 已知坑（脚本已经处理）

- **路径里有中文也能编译**：`cl.exe` 用 ANSI 解析参数，`CARGO_HOME` 落在中文路径下会报 `C1083 无法打开包括文件`。构建脚本会把 `CARGO_HOME`/`CARGO_TARGET_DIR` 自动落到 ASCII 目录（`%PUBLIC%\wfclient-build`）。
- **首次编译要写几百 MB 中间产物**，`build-cache/` 建议留在磁盘空间充足的分区。
- **PGO 剖面与 revision 绑定**：如果剖面对不上，删掉 `build-deps/pgo/default.profdata` 就会自动改成"先采样训练再编译"（多约 2 分钟）。
