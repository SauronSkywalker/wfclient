# NOTICE

## 这个仓库是什么

`wfclient` 是 [WFSim](https://github.com/magenie33/wfsim) 的**第三方分支**（fork）。
它基于上游源码做修改后重新分发，因此在此按上游 `TRADEMARK.md` 的要求明确声明：

- 本仓库及其产物 **基于 WFSim**（英文：*based on WFSim*）。
- 本仓库 **不是** WFSim 官方项目，未获官方背书，与 WFSim 官方无隶属关系。
- 本仓库使用了 **自己的名字（wfclient）**、自己的发布页，不冒充上游。
- 首次对齐的上游提交：`8cd2ec649fc5845b13ae560b793c714fa29f415a`（`magenie33/wfsim`，`main`，AGPL-3.0-or-later）。

## 版权与维护

- 本分支由 **司空祎宸（@SauronSkywalker）** 维护；本仓库中新写的部分（`ci/`、文档、工作流）著作权归其所有。
- 上游派生部分（`src/`、以及补丁所作用的上游源码）著作权归 WFSim 项目及其贡献者所有。
- 整仓库以 **AGPL-3.0-or-later** 发布，与上游一致。这一条是**强制**的：本仓库包含上游 AGPL 代码的派生作品，不能改用 MIT 等宽松许可。

## 商标

- *Warframe* 是 Digital Extremes Ltd. 的商标。
- WFSim 是非官方粉丝项目，与 Digital Extremes 无关联、未获其背书。
- 本仓库对上述商标不主张任何权利；本声明不覆盖 Digital Extremes 的商标。
- 本仓库不是 WFSim，也未使用 WFSim 的名称、图标或域名来发布产物。

## 许可

| 内容 | 许可 |
|---|---|
| `src/`（客户端源码，含本分支修改） | AGPL-3.0-or-later（与上游一致，见 `LICENSE`） |
| `ci/`（补丁、构建工具、闸门工具） | AGPL-3.0-or-later |
| 游戏数据 | **不在本仓库内**；使用与再分发边界见上游 `LICENSE-DATA.md` |

AGPL-3.0-or-later 的核心义务照旧：**谁分发本仓库的修改版二进制，谁就要一并提供对应的完整源码。**
本仓库的便携包（Release 里的 `wfsim-portable-*.zip`）内**已经包含** `src/`、`ci/patches/` 与全部构建脚本，因此满足这一义务；如果你基于本仓库再次修改并分发，请同样保留源码、版权与许可声明。

## 计算结果的权威性

上游 `docs/DISTRIBUTION.md` 的原则是 **"The channel may not fork"**：站点公布的答案才是唯一定论，任何一个客户端都不应给出"第二个答案"。

本分支遵守这一点，并且是**验证**而非承诺：每次交付引擎前跑三道逐位闸门

- 100 条 board（`ci/tools/corpus.json`）
- 5 个站点 WASM 锚点（`ANCHOR-burston_prime` / `ocucor` / `dual_toxocyst` / `torid` / `boar_prime`）
- 20 条紫卡优化器报告（`ci/tools/riven3.json` 为快筛子集）

比对 `score`（浮点精确）、`metric`、`work`（整数）、`counts`（字典全等）。任何一项不等即拒绝产出。

## 第三方组件

- Rust 工具链 1.97.1（`x86_64-pc-windows-msvc`）与 `llvm-profdata`（CI 中由 rustup 安装，不随本仓库分发）。
- [mimalloc](https://github.com/microsoft/mimalloc)（MIT），经 `libmimalloc-sys` 以可选 feature `fast-alloc` 接入；没有它也能编（退回系统分配器）。
- 便携包内的 Python 与依赖（`textual`、`rich`、`markdown-it-py` 等）各自保留其原有许可，清单见 `ci/requirements.txt`。

## 联系

发现问题请在本仓库开 issue。**关于上游 WFSim 的规则、名称、站点答案的问题，请去上游仓库**；本仓库没有资格代表上游作答。
