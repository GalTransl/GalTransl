# AGENTS.md

GalTransl 是 Galgame/小说的 LLM 翻译工具：Python 后端（翻译引擎、HTTP API、翻译 Agent、提取 Agent）加 Tauri + React 桌面端。本文只记录读代码不容易看出来的约定；细节以文末索引的各目录文档为准。

## 仓库布局

- `GalTransl/`：Python 后端。
  - `server.py`：标准库 `ThreadingHTTPServer` 上的手写路由，所有 `/api/*` 都在这里分发；提取和应用更新的路由各自在 `Extraction/http.py`、`AppUpdate.py`。
  - `Frontend/LLMTranslate.py`：翻译主流程（切分、缓存命中、并发调度、前后处理）。`Backend/*Translate.py` 是各翻译模板，`Backend/GenDic.py` 是字典生成。
  - `Agent/`：翻译 Agent。`Extraction/`：提取 Agent，复用 `Agent.AgentRunner`。
  - `RuntimePaths.py`：资源目录与用户数据目录的唯一来源。
  - `yapsy/`：内置的第三方插件框架，不要改。
- `desktop/`：`src/` 是 React 18 + TypeScript + Vite 前端，`src-tauri/` 是 Rust 外壳（`backend.rs` 管后端进程生命周期，`update.rs` 管自动更新）。
- `plugins/`：文件、文本、问题三类插件，每个插件一个目录（`<名>.yaml` + `<名>.py`）。`billion-context/` 和 `agent-mnemon/` 只放说明与上游许可，实现在 `GalTransl/Agent/`。
- `tests/`：后端 pytest。`desktop/tests/`：前端 `node:test`。
- `Dict/`、`translation_guidelines/`、`res/`、`sampleProject/`：随程序分发的运行时资源。
- `skills/`：提取 Agent 的 Skill。`galtransl-galgamereverse-skill/` 由程序下载，不入库，有自己的 `AGENTS.md`；要改它去上游仓库改，不要在这里改。
- `build_release.py`、`build_linux_x64.py`、`build_macos.py`、`scripts/`：打包与发布检查。
- 不入库的本地目录：`build/`、`release/`、`docs/`、`plans/`、`agent_sessions/`、`.venv*/`、`.claude/`。不要把产物写进入库路径，也不要依赖这些目录里的内容。

## 命令

环境：Python 3.11（仓库 `.venv`），Node 22，Rust stable。Windows 下解释器是 `.venv/Scripts/python.exe`，Linux/macOS 是 `.venv/bin/python`。

```sh
# 后端测试（仓库根目录；pytest.ini 默认 -n auto，全量约 40 秒）
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m pytest tests/test_xxx.py -q -n 0   # 单文件，省掉 worker 启动

# 前端（在 desktop/ 下）
npm test              # node --test tests/*.test.mjs，约 15 秒
npm run build         # tsc + vite build，类型检查靠它
npm run i18n:check    # 语言资源与硬编码文案检查

# Rust（在 desktop/src-tauri/ 下；先在根目录跑 python scripts/prepare_extraction.py --local）
cargo check --locked
cargo test --locked backend::tests -- --include-ignored   # 会启动真实 Python 后端，可用 GALTRANSL_TEST_PYTHON 指定解释器

# 开发运行
python run_backend.py --host 127.0.0.1 --port 12333       # 后端
npm run dev            # 浏览器前端 http://127.0.0.1:1420
npm run tauri:dev      # 桌面外壳
```

`run_desktop_dev.bat` / `run_desktop_dev.sh` 一次启动后端和桌面端。仓库没有配置 linter 或 formatter，不要引入，也不要顺手重排无关代码。

改动后至少跑受影响的那一侧：改 Python 跑相关 pytest 文件，提交前跑全量；改前端跑 `npm test`、`npm run build`、`npm run i18n:check`；改 `src-tauri` 跑 cargo 两条。CI（`.github/workflows/build-release.yml`）在四个平台上依次跑这些再打包，本地只能覆盖当前平台，别声称其他平台已验证。

## 架构约定

### 后端

- 路径一律走 `RuntimePaths`（`get_resource_path`、`get_user_data_dir` 等），不要依赖进程工作目录或 `__file__` 相对位置；打包后的程序从任意目录启动。用户数据在 `~/.galtransl`，可用 `GALTRANSL_DATA_DIR`、`GALTRANSL_APP_SETTINGS_PATH`、`GALTRANSL_RESOURCE_DIR` 覆盖。
- 打包版每个桌面窗口用 `--port 0` 启动自己的后端，实际地址通过 `--ready-file` 交给 Tauri；开发模式固定用 `127.0.0.1:12333`。不要在任何地方写死端口。
- 版本号的唯一来源是 `GalTransl/__init__.py` 的 `GALTRANSL_VERSION`，翻译模板清单是同文件的 `TRANSLATOR_SUPPORTED`。新增翻译模板要同时改这张表和 `Frontend/LLMTranslate.py` 的 `init_gptapi`。
- 启发式规则（GenDic、问题检测、对话框分析等）必须对各种游戏通用，不为某一个游戏的文本格式写特例。人名字段里的脚本变量（如 `$str20`）是有意保留给模型的占位符，不要过滤。
- 新增第三方依赖写进 `requirements.txt`。`build_release.py` 会自动扫描 `plugins/*/*.py` 的 import 和 `GalTransl/Agent` 的模块生成 hidden-import；其他位置的动态导入要自己确认 PyInstaller 能收集到。新增随程序分发的资源目录要同时加进 `desktop/src-tauri/tauri.conf.json` 的 `bundle.resources` 和 `build_release.py`（Windows 便携包由它自己拷贝资源）。

### 翻译 Agent（`GalTransl/Agent/`）

- 工具不直接读写文件系统，而是通过本机 HTTP 调 `server.py` 的 API，和界面走同一条代码路径、同一套路径校验。给 Agent 加能力时先有 API，再包成工具。
- 工具的三张表必须一起改：`tool_schemas.py` 的 `AGENT_TOOLS`（给模型看的 schema，多数工具用 `action` 合并入口）、`tool_operations.py` 的 `TOOL_OPERATIONS`（`action` → 具体操作）、`handlers.py` 的 `_TOOL_HANDLERS`（操作 → 实现）。写类操作还要登记到 `_TOOLS_WITH_REASON` 和 `permissions.py` 的风险分级，并在 `tools/preview.py` 提供审批预览。
- 每个后端工具在前端 `desktop/src/pages/agent/toolMeta.ts` 的 `TOOL_META` 里要有条目，`tests/test_agent_tool_meta_labels.py` 会对账（包括提取工具、billion-context 和 mnemon 的工具）。
- 改名或合并工具时，旧名字加进 `handlers.py` 的 `_RETIRED_TOOLS` 并给出明确的新调用方式：旧会话历史里还有旧调用，模型会照着再调。
- `Agent/runtime.py` 只是重新导出各模块名字的兼容入口。测试里要 patch 某个名字，必须 patch 它实际被使用的模块，patch `runtime` 不生效。
- 会话以 JSONL 落盘并在重启后懒加载恢复。改事件或历史的结构时要保证旧会话仍能回放。
- 提示词和工具描述是产品行为的一部分：工作流规则写在 `prompts.py`，工具描述只讲这个工具本身。不少测试直接断言提示词和工具 schema 的内容，改措辞后跑一遍 `tests/test_agent_*.py`。

### 提取 Agent（`GalTransl/Extraction/`）

- `read`、`bash`、`edit`、`write` 四个工具的描述、参数说明和系统提示词片段逐字取自 Pi 1.4.0 源码，行为契约由 `tests/test_extraction_native_tools.py` 固定；保留的自定义工具由 `tests/test_extraction_tool_parity.py` 和 `tests/fixtures/extraction_legacy_tools.json` 固定。不要润色这些文字，也不要改参数形状。
- 读取范围是游戏、Skill、工作区和当前会话目录，写入仅限工作区。新增工具时保持这条边界。
- Windows 的 `bash` 是随程序分发的 `res/busybox64u.exe`（POSIX sh 语义），不要替换成 PowerShell 或 Git Bash。

### 上游移植

billion-context（上下文压缩）、agent-mnemon（记忆）和 Pi 原生工具都是从上游移植的 Python 实现。同步上游时：只移植适用于 Python 核心的部分，在提交说明里写明对应的上游版本或提交，更新 `plugins/<名>/README.md`，保留上游许可文件。

### 桌面前端（`desktop/src/`）

- 所有后端请求集中在 `lib/api.ts`（提取页在 `pages/extraction/api.ts`）。后端接口的增删改要同步这里的类型。
- 前端保持浏览器可运行：Tauri 专有能力要有浏览器下的降级路径，不能让 `npm run dev` 的纯浏览器模式坏掉。
- 界面文案全部走 i18n：`zh-CN` 是源语言和回退，`en` 必须填满（`npm run test:i18n` 不允许空值）。新增文案在两种语言的同一位置各加一条；key 是固定标识，不因文案调整而改名；不拼接句子。JSX 里不许出现硬编码文字，确实不该翻译的中文登记到 `i18n/audit-exclusions.json` 并说明原因。模型输入（Agent 建议指令、规范示例）、后端原始错误和用户内容不翻译。完整规则见 `desktop/src/i18n/README.md`。
- 插件设置的界面由插件 YAML 的 `SettingsSchema` 驱动，新增设置不需要改前端组件；内置插件的英文文案通过 `i18n/plugin-map.json` 映射。
- 样式是 `styles/` 下的普通 CSS（`components/`、`pages/` 分文件），没有 UI 组件库或 CSS 框架。优先复用 `components/` 里已有的 `Button`、`Panel`、`CustomSelect`、`AnimatedDialog`、`Icon` 等；动效走 `lib/motion.ts` 和 `UiMotion`，并尊重“关闭动画”设置。
- 翻译 Agent 页和提取 Agent 页共用队列、审批、提问卡片、运行指示器等展示逻辑。改其中一边时检查另一边是否应保持一致。

### 插件（`plugins/`）

- 命名和结构见 `plugins/readme.md`；基类在 `GalTransl/GTPlugin.py`（`GTextPlugin`、`GFilePlugin`、`GProblemPlugin`）。
- 问题插件的 `check` 只返回问题文字，不修改句子或它的 `problem` 字段；合并去重由系统做。
- 给已有设置改名或换位置时用 `SettingsSchema` 的 `legacy_path` 兼容旧项目配置，不要让旧项目的值丢失。

## 兼容性

用户手里有大量既有项目和会话。项目 `config.yaml`、`transl_cache` 缓存文件、字典文件、应用设置和会话存档的格式变更都必须向后兼容：新增字段给默认值，改名保留旧值读取路径，不写一次性迁移脚本让用户手动跑。自动更新会替换程序文件但保留项目、缓存、用户设置和（默认）全局字典，改动更新逻辑时守住这一点。

## 代码风格

- 跟随所在文件的既有写法。Python 新模块用 `from __future__ import annotations` 和类型标注，内部名以下划线开头；老模块（`CSentense.py`、`ConfigHelper.py` 等）保持原有命名，不做顺手重构。
- 注释写“为什么”和不显然的约束，尤其是修过的坑；不写复述代码的注释。注释中英文都有，跟随所在文件。
- 不为假想的需求加抽象、配置项或防御代码。`server.py`、`AgentPage.tsx` 这类大文件按现有组织方式追加，除非任务就是拆分。
- 给用户和模型看的文字说清楚发生了什么、下一步怎么做。给模型的错误信息要可操作（参考 `tool_operations.py` 里的写法）。
- TypeScript 开 `strict`，不用 `any` 绕过类型；模块级不要在导入时计算译文。

## 测试

- 测真实实现：本地 HTTP 测试起真实服务器（`serve_forever(poll_interval=0.01)`，结束时 join 或关闭），文件操作用临时目录。只在边界上打桩（模型客户端、外网、时钟）。
- 测试不访问外网，不调用付费模型。`tests/conftest.py` 会清掉代理环境变量。
- 测试不能受开发机设置影响：需要应用设置或用户数据目录时用 `GALTRANSL_APP_SETTINGS_PATH`、`GALTRANSL_DATA_DIR` 指到临时目录；测普通 Agent 行为时显式关闭 billion-context 和 agent-mnemon。
- 默认多进程并行，测试之间不共享文件、端口或全局状态。体量大的夹具会让慢的 CI 机器超时，只造够触发条件的数据。
- 前端测试没有 DOM 环境：用 `tests/helpers/load-typescript.mjs` 直接转译并加载源码模块，按模块路径注入替身，组件用 `react-test-renderer`。新逻辑尽量写成 `lib/` 或页面目录下可单独加载的纯函数模块，方便这样测试。
- 前后端共享口径的地方（工具标签、Markdown 输出、搜索条件等）用跨语言的契约测试对账，不要各写一份期望值。
- 修 bug 先写能复现的测试。测试名和文档字符串说明它守护的行为及回归背景。

## 提交与发布

- 默认分支是 `main`，日常直接在 `main` 上提交。需要分支时用简短的连字符名字，如 `fix-extraction-output`。
- 提交信息用英文，格式 `type(scope): summary`，小写开头、不加句号。type 用 `feat`、`fix`、`refactor`、`perf`、`test`、`docs`、`chore`、`ci`、`style`；scope 用受影响的区域，如 `desktop`、`agent`、`extraction`、`cache`、`translate`、`runtime`、`plugins`、`i18n`、`release`。正文说明原因和用户可见的变化，而不是罗列改了哪些文件。
- 只提交自己改动的文件，按路径 `git add`，不用 `git add -A` 或 `git commit -a`：这个工作区可能同时有其他会话在改动和提交。报告测试失败前先确认它不是 `HEAD` 上已有的失败。
- 提交和推送都要用户明确要求才做。
- 发布：`chore(release): bump version to X.Y.Z` 一次改齐六处版本号（`GalTransl/__init__.py`、`desktop/package.json`、`desktop/package-lock.json`、`desktop/src-tauri/Cargo.toml`、`Cargo.lock`、`tauri.conf.json`），然后推送与版本号一致的 tag（`X.Y.Z` 或 `vX.Y.Z`）。CI 只创建草稿 release，由维护者手动发布；不要移动或复用已有 tag，也不要改动已发布的 release。

## 工作区注意事项

- 仓库没有 `.gitattributes`，文件换行符不统一：`GenDic.py`、`CSentense.py`、`ConfigHelper.py`、`GTPlugin.py`、`Utils.py`、`i18n.py`、大部分插件、两份 README、`desktop/package.json` 等以 CRLF 入库，其余多为 LF。编辑时保持每个文件原有的换行符，提交前用 `git diff --stat` 确认没有整文件的换行符改动。用脚本批量改文件时按二进制读写。
- 不要未经询问就对真实项目跑会消耗付费 API 的翻译或 GenDic；验证用单元测试、本机模拟模型或已保存日志的离线回放。在真实项目上做实验时先复制到临时目录。
- `README.md` 和 `README_EN.md` 是一对，改用户可见的说明时两份一起改。

## 文档索引

- `README.md`：用户文档，末尾“开发与构建”一节有各平台的环境搭建和发布流程。
- `tests/README.md`：后端测试的运行方式和夹具约定。
- `desktop/README.md`：桌面端开发、后端进程握手、自动更新、Linux 构建。
- `desktop/src/i18n/README.md`：界面文案规则。
- `GalTransl/Extraction/README.md`：提取 Agent 的完整行为说明。
- `plugins/readme.md`、`plugins/SETTINGS_SCHEMA.md`：插件规范与设置描述。
- `plugins/billion-context/README.md`、`plugins/agent-mnemon/README.md`：两个上游移植模块的范围与行为。

改了这些文档描述的行为时同步更新对应文档；本文件也一样，发现与实际不符就改。
