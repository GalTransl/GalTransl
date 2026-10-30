# billion-context · Python 核心版

GalTransl 使用了 [billion-context](https://github.com/ranxianglei/billion-context) 和
[acp-kernel](https://github.com/ranxianglei/acp-kernel) 的设计与压缩规则。
本目录保留上游许可；Python 实现在 `GalTransl/Agent/billion_context.py`，规则在
`GalTransl/Agent/billion_context_prompts.py`。

## 开启与运行环境

桌面端在翻译或提取 Agent **左下角的齿轮配置入口**开启 billion-context，默认关闭；提取 Agent 的入口同时显示运行环境状态。

启用时，翻译和提取 Agent 隐藏普通上下文压缩的 `read_history_archive` 工具，
使用 `search_context` / `decompress` 回查插件历史；关闭后恢复普通归档工具。
翻译开关保存到后端应用设置 `billionContextEnabled`，提取开关保存到
`extractionBillionContextEnabled`，独立控制各自类型的 Agent，从下一次回合起生效。
升级时两个开关继承原统一开关的状态；正在执行的回合保持开始时的状态。

全部核心逻辑使用 Python 标准库（包括 `sqlite3`），复用现有模型客户端与后端解释器。
无需 Node.js、npm、Pi SDK、代理服务或额外 Python 包。源码前端的开发、构建仍使用原有 Node 工具链。

## 保留的核心功能

- 消息使用稳定递增的 `m00001` 引用；压缩块使用 `b1` 引用，重启后恢复。
- `compress`：模型提供忠实摘要，批量压缩历史消息，或将摘要逐层精炼为 tier 2、tier 3。
  只接受节省 token 的摘要，批量操作在一个 SQLite 事务中提交。
- `decompress`：分页读取子摘要或完整原始消息；读取不会自动展开请求上下文。
- `search_context`：按关键词检索归档与摘要，支持中文、日文；返回引用、片段和所在块。
- `acp_status`：查看压缩块、可压缩范围、上下文压力、分层摘要规则。
- 请求达到估算窗口的 45% / 75% / 95% 时附加分级提示；达到 90% 或模型报告超限时，
  复用当前模型生成有大小限制的紧急摘要，目标降到 80% 以下。
- 保留最近至少三条消息与工作尾部，压缩范围扩展到完整工具交换；系统消息不压缩，
  assistant 的思考、签名与工具参数不因引用标记而改变。
- 压缩只改变发给模型的请求视图，保留主会话原文；失败、取消不会用残缺摘要覆盖历史。

摘要由当前配置的模型生成，会产生正常 API 用量。token 数为中日韩文字感知的估算，
可用服务商返回的实际用量校准，并非精确 tokenizer。不可压缩的系统提示、近期消息或
单次工具交换本身过大时会明确报错，保留历史，需增大窗口或减少单次输出。

## 本地数据

每个会话在 JSONL 日志旁保存 `<会话日志>.billion-context.sqlite3`，包含原始请求消息、
摘要、引用与工具调用回执。提取与翻译会话分别隔离。关闭开关会保留归档，重新开启可继续使用；
删除或重置对应会话时清理归档。归档可能包含工具输出和图片数据，与会话原文同样应妥善保管。

## 移植范围与出处

参考本地 billion-context **0.1.207**，提交
`bb7c24972c98e8f62d63726d7a3777c90a0610e1`，及其 acp-kernel **0.0.163**。
分层压缩提示词由上游 `compression-rules.ts` 适配，作为普通 Python 常量随源码发布。

这是内置、由开关控制的 Python 核心移植。没有迁移通用 JS 扩展加载器、Pi/其他宿主插件协议、
上游命令面板、可视化管理、外部检索服务或上游缓存锚点保留策略。压缩范围在请求中替换为
历史摘要消息，可能影响前缀缓存命中。专用校对与探索子 Agent 仍使用原有上下文管理。
本实现不承诺无限上下文；原文归档增长与检索仍受本机磁盘、内存和处理时间限制。

Copyright (c) 2026 ranxianglei. 许可见 [LICENSE](LICENSE) 和 [LICENSE.acp-kernel](LICENSE.acp-kernel)，
包括产品可见出处标注条款。
