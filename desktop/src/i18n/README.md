# 前端语言资源

默认语言为 `zh-CN`，仅支持 `zh-CN` 和 `en`。界面语言存储在独立的
`galtransl.ui.language` 偏好中，不修改项目的源语言、目标语言或模型提示词。
存储不可用时仍可在当前会话切换，切换不会重挂载应用或清空表单。

## 翻译交接

填写 `locales/en/*.json` 中的空字符串即可。对应的 `locales/zh-CN/*.json`
保留现有界面文案，包括之前就使用英文的标签。英文缺失或为空时逐条回退中文。
英文文案已全部填写，没有把中文复制进英文资源；新增 key 时仍按上面的方式补空字符串。

资源按 `common`、`settings`、`projects`、`config`、`agent`、`plugins` 和 `errors`
分组。key 是固定标识，不要因文案调整、页面行号变化或翻译内容改变而重命名。
`resources.ts` 从中文资源推导 `TranslationKey`，未知 key 会触发 TypeScript 错误。

## 文案规则

- 命名插值如 `{{name}}`、`{{count}}` 必须保留名称，可以调整位置。
- 含 `count` 的数量消息提供 `_one` 和 `_other`。分别填写英文单数与复数；
  中文对应项保持相同文案。不需要数量变化的消息使用普通 key。
- 富文本用 `<0>...</0>` 等组件占位符，可调整位置，但保留成对标记及编号。
  组件由 `UiTrans` 的 `components` 提供，不在资源中添加 HTML 属性或脚本。
- 不拼接不同语言的句子；优先把整句和参数放进一个资源。
- 技术标识、配置实际值、用户内容、后端原始错误和日志、模型输出不翻译。
  Agent 建议指令和翻译规范示例也属于模型输入，保留原文。

## 新增文案

1. 在中文资源中新增语义 key，在英文资源同位置新增空字符串。
2. React 组件调用 `useUiLanguage()` 订阅切换，再用 `t(key, values)` 渲染。
   非 React 模块从 `i18n/core` 使用同一个 `t`。模块级展示定义使用 key 或延迟 getter，
   不在模块导入时计算译文。包含展示结果的 `useMemo` 依赖必须包含当前语言。
3. 持续显示的提示保存 `message(key, values)`，使用 `useMessageState` 或
   `useFeedbackState` 在渲染时解析。后端原始文字作为普通字符串保留。
4. 配置字段用 `labelKey`、`descriptionKey`、`placeholderKey`；选项分别定义
   `value` 与 `labelKey`，只把 `value` 写回配置。
5. 内置插件资源位于 `plugins:builtin`。`plugin-map.json` 用原始插件标识、设置键
   和选项实际值显式映射资源 key。选项值可以是数字、布尔或空字符串；不要把
   任意选项值拼进点分 key。未知插件、字段与选项回退后端元数据。

## 检查

在 `desktop` 目录运行：

```sh
npm run i18n:check
npm run i18n:check -- --pending
npm run test:i18n
npm test
npm run build
```

资源检查验证值类型、未知 key、已填写英文的插值与富文本标记，报告未填写或缺失项。
英文空值不会导致检查或构建失败，但 `--pending` 会列出未填写项，`npm run test:i18n`
要求英文资源没有空值。源文件中的遗留中文必须记录在
`audit-exclusions.json`，每项说明为什么属于排除范围；新增界面文案应进入语言资源。

离线 UI 冒烟使用模拟 API，检查设置切换与刷新、导航、项目配置、插件、Agent 和
编辑草稿，不发起真实翻译、模型请求或付费 API 调用。
