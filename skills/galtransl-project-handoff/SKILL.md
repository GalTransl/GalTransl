---
name: galtransl-project-handoff
description: 提取完成并验证 gt_input 后，将导出的游戏文本交接给 GalTransl 翻译项目，显示创建翻译项目按钮并说明译文回填位置。
---

# GalTransl 项目交接

仅在提取完成且 `gt_input` 文件已通过验证后，在最终回复单独一行输出：

```text
@new_project_wizard("absolute project directory")
```

参数替换为包含 `gt_input` 的绝对项目目录，通常是当前输出工作区，不能传 `gt_input` 本身。
使用双引号 JSON 字符串，转义 Windows 反斜杠和引号；实际输出不要放进代码块或行内代码。
界面会将这行显示为“创建翻译项目”按钮，点击后预填项目名和父目录，并在第二步载入已有输入。
不要自行创建 `config.yaml`，也不要声称翻译项目已经创建，由用户完成向导。

用用户的语言报告真实产物路径、数量、失败和验证结果。说明同名译文放入 `gt_output`，之后可继续请求校验、回填和重封包。
