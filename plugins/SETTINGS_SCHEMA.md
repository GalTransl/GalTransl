# 插件设置的展示描述

文件插件、文本插件和问题插件均可在 YAML 顶层添加可选的 `SettingsSchema`。它与 `Settings` 使用相同的键，描述界面展示与可选的旧配置回退路径。新建项目向导和项目配置页共用此描述。

```yaml
Settings:
  engine: ""
  timeout: 300

SettingsSchema:
  engine:
    label: 脚本引擎
    description: 通常使用自动识别，识别失败时再手动指定。
    options:
      - {value: "", label: 自动识别}
      - {value: bgi, label: BGI / Ethornell}
  timeout:
    label: 处理超时（秒）
    description: 单个文件的最长处理时间。
    advanced: true
    min: 1
    step: 1
```

- `label`、`description`：用户可见的名称和操作说明，建议使用中文。
- `options`：下拉菜单选项，每项包含 `value` 和 `label`。值类型应与 `Settings` 默认值一致；支持字符串、数字和布尔值，数字选项保存后仍是数字。
- `advanced: true`：放入默认折叠的“高级设置”，展开后可编辑。折叠不会清除已保存值。
- `placeholder`：文本输入提示。
- `secret: true`：字符串使用密码输入框遮挡显示；保存方式仍为项目配置，不代表加密存储。
- `multiline: true`：字符串使用多行编辑，适用于正则拼接格式和换行分隔符。
- `min`、`max`、`step`：数字输入控件约束；插件仍应执行运行时校验。
- `legacy_path`：问题插件可声明旧项目配置路径，例如 `problemAnalyze.avgSentenceLengthThreshold`。设置值优先级为插件默认值、旧配置值、项目插件覆盖值；桌面端和 Agent 使用相同顺序，新编辑仍写入 `plugin.<模块名>.<设置键>`。

未声明描述的旧插件仍按默认值类型生成原有控件，显示原始键名。顺序沿用 `Settings` 中的键顺序，常用设置和高级设置分别排列。旧项目中不在选项列表内的值仍会显示并保留，直到用户主动选择其他选项。新增设置描述不需要改前端组件。

数组设置可声明 `options`，界面会显示多选复选框，选项类型按默认数组的首项判断（空数组默认为字符串）；保存值仍为数组。对象设置使用 JSON 编辑器，有效对象才更新设置，格式错误会提示并保留上次有效值。
