# 提取 Agent

提取 Agent 复用 `GalTransl.Agent.AgentRunner` 的 OpenAI 兼容请求、流式输出、重试、停止、审批、会话持久化和上下文压缩。提取专用提示词、工具集和输出工作区与翻译任务分开；不再启动 Pi sidecar，也不打包 Node.js、Pi SDK 或 npm 运行依赖。

工具集为 `read`、`bash`、`edit`、`write`、`run_python`、`ask_user` 和共用的 `read_history_archive`。启用 billion-context 时隐藏 `read_history_archive`，改用插件的 `compress`、`decompress`、`search_context` 和 `acp_status`。四个原生名称工具依据本地归档的 Pi 1.4.0 实现其参数及行为契约，替换原 `read_file/run_shell/edit_file/write_file`，不再注册旧名称。四个工具的 description、参数 `description`，以及系统提示词里的 `Available tools` / `Guidelines`（各工具的 promptSnippet 与 promptGuidelines）均逐字取自 Pi 源码，由 `tool_schemas.py` 的 `tool_prompt_section()` 生成；bash 的 `PI_*` 环境变量提示不适用，已省略。目录浏览使用 `bash` 或 `run_python`。读取范围为游戏、Skill 和工作区，文件编辑仅限工作区；Python/bash 以当前用户权限执行，遵循审批模式，可停止，不构成操作系统沙箱。

- `read(path, offset?, limit?)`：UTF-8 文本保留 BOM/CRLF，按 2,000 行或 50 KiB 完整行截断，附续读提示；不再限制文本文件为 2 MiB。按文件头识别 JPG/PNG/GIF/WebP/BMP，BMP 转 PNG；图片最大边 2,000 像素、base64 小于 4.5 MiB，缩放附坐标映射。模型请求中的 `tool` 回复仅含文本，图片作为该批全部工具回复之后的 `user` 附件传递并标明调用 ID。历史和 UI 日志仅保存会话资产引用；旧会话重放采用同一转换，重启后恢复。
- `bash(command, timeout?)`：实际 Bash；Windows 优先 Git Bash，缺少 Bash 时明确报错，不替代为 PowerShell。默认无超时，支持正数小数秒、最大 2,147,483.647 秒。继承环境并清理冻结程序的私有加载路径；保留末尾 2,000 行或 50 KiB，长输出完整落在工作区日志，不设旧的 10 MiB 输出终止限制。非零退出标为失败，超时/停止回收进程树。与 Pi 一致，命令正常结束后留下的后台进程继续运行，bash 在输出管道静默 100 ms 后返回，不会因后代持有管道而永久挂起；Windows 上结束时释放作业对象而不杀后代。`read`/`write`/`edit` 的路径沿用 Pi 的 Windows 转换：`/c/x`、`/mnt/c/x`、`/cygdrive/c/x` 视为 `C:\x`。
- `edit(path, edits)`：一次多个替换，全部匹配原始文件；唯一性、重叠、缺失和无变化均校验，整批成功才保存。兼容 Pi 的单对象/JSON 字符串 edits 与旧 camelCase 单替换参数。精确优先，支持 NFKC、引号、连字符、特殊空格和行尾空白模糊匹配；仅重写涉及的行，保留其他行。保留 BOM 和原换行风格，返回编号 diff、四行上下文的 unified patch、`firstChangedLine`。diff 使用与 Pi 相同的 Myers 路径选择。
- `write(path, content)`：创建父目录，UTF-8 创建或整文件覆盖；无旧的字符数或 2 MiB 限制。与 edit 共用按文件串行的修改队列。

集成边界：保留目录/链接限制、审批、原子替换及审批期间外部修改检测；原生参数校验允许额外字段，其余自定义工具维持严格校验。UI/审批参数仅显示有限预览，执行仍使用完整参数；超过 50 KiB 的 diff/patch 预览有明确提示，完整结果在 `fullDiffPath` JSON 中。图片处理使用 Pillow，缩放编码不保证与 Pi 的 Photon 逐字节相同；损坏或过大的图片返回明确的省略说明。未新增 grep/find/ls/powershell，也未改用 Pi 会话框架。旧集成证据与三组 Pi 原生编辑实测保留在 `tests/fixtures/extraction_legacy_tools.json`，测试与运行均无需 Pi SDK/Node。

行差分移植的第三方许可随 [THIRD_PARTY_LICENSES.txt](THIRD_PARTY_LICENSES.txt) 保留，并由现有 `--collect-data GalTransl` 发布配置收集。

默认 Python 使用已有后端可执行文件的 `--extraction-python` 入口，源码开发使用当前 Python。没有第二份 Python 安装；需要额外依赖时，可显式选择 `runtime="system"`，不会自动回退或安装包。旧设置中的 Node 路径会被忽略。

提取 Agent 左下角的配置窗口将上下文管理、提取工具包和 Python 运行环境分区展示。打开窗口会自动探测 PATH、Windows Python 启动器和注册表、常见安装位置及虚拟环境中的 Python，按版本、位数和路径提供下拉选择；支持重新探测或手动指定解释器文件。留空时在执行本机 Python 的工具调用时自动选择，应用 Python 仍是默认运行环境。目录和解释器选择需点击保存，billion-context 开关自动保存，翻译与提取各自独立。

配置窗口立即打开，先显示已加载的环境信息，再后台检查环境和探测解释器；首次尚无数据时显示加载提示。检查失败时窗口内显示错误，并支持重试。

`read`、`write`、`edit`、`bash` 和 `run_python` 增加可选 `reason` 参数（最多 200 字符），系统提示词要求每次调用用用户语言填写一句简短的操作意图。调用行和审批区直接显示这句说明；没有该参数的旧会话仍显示文件路径或命令摘要。原始执行参数、工具结果和 Pi 参数契约保留，`reason` 是 GalTransl 额外提供的展示参数。billion-context 的四个工具共用翻译和提取界面的中文标签。

首条用户消息在模型侧注入提取专用的意图判断段落：问候只作简短回复；实际执行任务时，先在助手正文说明主要步骤与验证方式，再开始调用工具。执行过程中在工具调用之间穿插当前状况、实际进展、问题和下一步，关键阶段、阻碍和方案调整及时反馈；思考内容或工具 `reason` 不代替正文反馈。界面及用户消息事件仍展示原始输入，后续用户消息不重复注入该段落。

新会话默认「自动执行」：工具直接执行，`ask_user` 仍等待用户回答。「全自动-零打断」复用翻译 Agent 的问题参数和推荐项代答逻辑：自动采用 `recommended`，未指定时取第一个选项，不修改系统提示词。普通提问共用翻译 Agent 的多题、选项和自填答案卡片。权限模式可在运行中切换并保存到当前会话：放宽权限会放行当前符合条件的审批，切到零打断会按推荐项回答正在等待的问题；收紧权限对后续工具调用生效。模型列表及已选项共用「配置名/实际模型」展示格式。

开发启动只准备本地资源：Tauri 的开发配置在 `desktop` 运行 `npm run extraction:prepare -- --local`，不拉取或更新 Skill；缺少 Skill 时仍准备 Python 适配器，随后可在提取 Agent 的「运行环境」中点击「下载 Skill」。该页面显示当前版本，支持手动检查更新和下载最新版；下载通过 GitHub 的 `main` 提交和对应源码 ZIP 完成，无需本机 Git，成功后保存到用户数据目录 `extraction/skills/` 并自动启用。每个提交独立保存，原有内置、自定义和已下载目录不覆盖；下载失败时保留原选择。仅点击检查或下载时联网。

打包仍运行 `python scripts/prepare_extraction.py`（或 `desktop` 中的 `npm run extraction:prepare`），拉取并快进同步 Skill 上游 `main` 分支的最新提交，将完整 Skill 和小型 Python 适配器导出到 `build/extraction-resources`，排除 Git 元数据和本地输出；缺少 Skill 时初始化子模块，有本地修改或分叉时停止同步，网络失败时明确报错。源码清单只指定来源分支，导出清单记录实际 commit 和文件数。Tauri 及各平台发布脚本使用相同资源；发布后的程序默认使用包内快照，也可在「运行环境」中手动下载更新。前端开发仍使用常规 Node 工具链，发布后的提取 Agent 无需 Node。

新会话默认复用游戏目录内的 `<游戏名>_extract`，不追加数字；自定义输出目录也可复用，创建会话仅补齐缺少的子目录并保留已有文件。多个会话保留独立历史，同一游戏目录（或同一输出工作区）同时只能运行一个提取 Agent，等待审批、等待回答和停止收尾期间仍占用目录；不同游戏仍受全局最多两个提取会话并行的限制。

侧栏会话的 × 删除该会话历史并保留输出文件；文件夹的 × 仅隐藏该游戏分组，保留所有会话历史，为该游戏新建会话时重新显示。运行中的会话以及包含运行中会话的游戏分组都禁止关闭，由后端在同一运行锁内校验。

UI 会话日志位于用户数据目录 `extraction/sessions/<id>/`，模型历史和压缩归档由共用的 Agent SessionStore 存在其 `execution/` 子目录，不包含 API 密钥。切换到其他页面时，运行中的当前会话继续接收事件，并复用翻译 Agent 的后台提问/审批系统通知。停止按钮终止当前回合。有效 `gt_input/*.json` 可直接打开为翻译项目。

运行指示器、压缩结果、流式段落结束和工具/思考耗时共用翻译 Agent 的展示；压缩结果及耗时写入日志，刷新后可回放。审批支持填写可选拒绝原因，并将原因传回模型。事件流断开时显示重连次数，按 1/2/4/8/10 秒退避，最多重试五次；恢复后撤掉提示，从最新快照和游标继续接收。

运行期间发送的文字和图片先进入消息队列，面板共用翻译 Agent 的立即发送、编辑和删除操作。本轮正常完成或失败后自动消费队列并续跑；手动停止则保留队列供用户处理。「立即」打断本轮，先发送选中的消息，其余消息继续等待新一轮完成。队列仅保存在内存中，重启后清空；消费前不进入模型历史。自动续跑和立即发送期间持续占用游戏目录及输出工作区。

验证：`python -m pytest tests/test_extraction_agent.py tests/test_extraction_runtime.py tests/test_extraction_shared_runner.py tests/test_extraction_tool_parity.py tests/test_extraction_native_tools.py`；前端检查从 `desktop` 运行 `npm test`、`npm run build`、`npm run i18n:check`。测试使用隔离的合成文件和本机模拟模型，不调用付费模型或执行真实游戏提取。原生进程与图片测试需安装后端 requirements（含 Pillow）和本机 Bash；平台验证仅覆盖实际执行的环境。
