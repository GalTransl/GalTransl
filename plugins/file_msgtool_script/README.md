# Galgame 脚本文件插件

通过随程序分发的 `res/msg_tool.exe` 提取和回填脚本。

## 回填编码检查

“原文编码”（`source_encoding`）控制读取；“输出编码”（`patched_encoding`）控制回填。输出编码留空时使用 msg-tool 的引擎默认值，不会自动跟随原文编码。例如 CatSystem2 的 CST 默认以 CP932 / Shift-JIS 回填，部分中文字符无法直接写入。

插件会在调用 msg-tool 回填前检查已确认编码规则的 CST 和 KAG 文本，发现无法编码的字符后列出文件、数据条目、人名/正文及字符码位，并保留原脚本和已有输出。Unicode BOM、封装 KAG、Windows 代码页参数及工具的替换表/人名表交由工具处理，避免错误拦截；其他格式或预检未覆盖的编码问题，也会在工具返回错误或警告时给出处理提示并保留原始诊断。msg-tool 的 `gb2312` 实际使用 GBK，预检也按 GBK 处理。

遇到 `Failed to encode Shift-JIS` 或编码丢失提示时，可按游戏支持情况选择：

- 游戏需要日文编码：开启下述 **JIS 替换**，并部署 UIF 配置或对应替换字体。
- 游戏支持其他编码：将 **输出编码** 改成 `gb2312` 或 `utf8`。仅修改原文编码无效；如果 `extra_args` 指定了 `-p` / `-P`，需同步修改或移除，这些额外参数优先于输出编码设置。
- 修正提示中的字符后重新构建结果；调整输出配置不需要重新翻译。

## JIS 替换

在插件设置中开启 **JIS 替换**（`jis_substitution: true`），回填时会将正文和人名中 CP932 无法编码的字符按内置字典替换成日文字符。可直接编码的字符保持原样；保留原文模式下，追加的原文也会经过检查。翻译缓存和传入的数据不变。

- 开启后固定以 CP932 输出，忽略 `patched_encoding`；`source_encoding` 仍控制源脚本的读取编码。请移除 `extra_args` 中的输出编码参数。
- 每次成功回填都会在项目输出根目录生成或更新 `uif_config.json`（通常为 `gt_output/uif_config.json`，旧项目可能为 `json_cn/uif_config.json`）。子目录内的脚本共用此配置，无需等全部翻译完成。
- UIF 的 `character_substitution.source_characters` 为替换后的日文字符，`target_characters` 为对应中文。映射汇总当前插件实例成功写出的所有脚本；重复保存同一文件时更新该文件的映射。续跑时保留此前配置中的映射，以兼容未重新回填的脚本；需要清除历史映射时，可删除输出目录的配置后完整重建输出。
- 已有配置中的字体、注入等其他设置会保留。复制脚本到游戏后，需要配合 [UniversalInjectorFramework](https://github.com/AtomCrafty/UniversalInjectorFramework) 和此配置，或使用与字典对应的替换字体，才能还原中文显示。
- **JIS 未匹配字符处理**（`jis_unmapped`）默认 `error`：字典未覆盖时停止该文件的写出。设为 `space` 时与 SExtractor 一致，替换成全角空格，并在配置的 `character_substitution.remain` 中列出字符，同时输出警告。
- 文本本身包含替换字典的目标字符时，会在 `character_substitution.repeat` 中列出并提示检查显示。双语文本或使用替换字体时尤其需要检查。

关闭 JIS 替换后恢复原有回填流程，不生成或更新 UIF 配置。

## Agent 调整设置

Agent 可调用 `get_plugin_settings(plugin_name="file_msgtool_script")` 查看默认值、项目生效值、中文选项说明和完整配置路径。即使项目 YAML 尚未包含某项，也可通过 `update_project_config` 新增插件声明的设置，例如 `plugin.file_msgtool_script.jis_substitution: true`；未知键、错误类型和无效选项会被拒绝，写入仍遵循当前 Agent 权限模式及审批预览。

回填编码失败时，`wait` 和 `get_runtime` 会返回完整错误及结构化修复指引：先查询设置，再依据游戏兼容性选择 JIS 替换或调整 `plugin.file_msgtool_script.patched_encoding`。只改回填配置后可调用 `start_translation(translator="rebuildr")` 从缓存重新构建，并等待任务成功；使用 JIS 时仍须部署 UIF/对应字体。

## 字典来源

`subs_cn_jp.json`（2,999 组映射）与 `uif_config.json` 模板复制自 [SExtractor](https://github.com/satan53x/SExtractor)，版本 `8d8d976fd04ae54e7c677705af937273d04a376a` 的 `src/subs_cn_jp.json`、`src/uif_config.json`。替换规则参考 `src/helper_text.py` 的 `generateSubsJis` / `generateSubsConfig`，字典由 SExtractor 注明最初来自 GalTransl_DumpInjector。SExtractor 和本项目均使用 GPL-3.0 许可证，详见仓库根目录的 `LICENSE`。

字典及模板随插件打包，运行时无需安装 SExtractor。
