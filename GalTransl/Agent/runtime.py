"""GalTransl Agent runtime.

一个 Agent 推理循环：读取选定的后端配置（OpenAI-Compatible）与项目，
用 OpenAI 官方 function-calling 接口驱动"先写字典后启动翻译"的标准流程。
工具通过本机 HTTP 调回现有 server.py 的 API，走和 UI 一样的代码路径。
不直接接触文件系统，所有写入经现有 API 的路径校验。

Agent 是一个持久的多轮会话：用户的第一条消息启动会话，之后 Agent 在后台
跑一个回合（可以调用任意多次工具直到自然收尾）；用户随时可以打断，或等
回合结束后继续发消息，Agent 在同一条对话历史上接着干。reset 才会清空。

这里是兼容入口：实现按职责拆在同目录的各模块里，下面把它们的全部名字（含下划线开头的内部名）重新导出，from GalTransl.Agent.runtime import X 的老写法照常可用。注意：要 patch 某个名字，得 patch 它被使用的那个模块，patch 这里不生效。模块一览：

- core: 常量、日志、异常与 LLM 请求的错误分类/重试退避。
- tools.common: 各工具共用的小函数：变更记录、diff、index 区间解析、上下文行标记等。
- tools.cache_fields: 翻译缓存条目的字段定义（读取投影与可 patch 字段）。
- prompts: Agent 的提示词：系统提示、上下文压缩提示，以及系统提示的拼装。
- handlers: 工具名到实现函数的注册表，以及工具调用理由（reason）的附加规则。
- permissions: 权限：工具执行前的审批（风险分级、权限模式、拒绝原因）。
- models: 会话状态与事件的数据结构。
- runner: AgentRunner：一个会话的回合主循环（LLM 流式请求、工具调度、审批、压缩）。
- context: 上下文管理：token 估算、压缩切点与归档、残缺历史修补、prompt cache。
- backend_http: 调用本机 server.py HTTP API 的辅助函数。
- tool_schemas: Agent 工具的 OpenAI function schema（AGENT_TOOLS）。
- tools.project: 项目类工具：项目概览、配置读写、项目翻译规范。
- tools.listing: 清单类工具的公共入参（grep + limit + order）与挑选逻辑。
- tools.input: 输入文件类工具：列出与读取待翻译原文。
- tools.dicts: 字典类工具：列出、读取、保存与新建字典文件。
- tools.problems: 问题类工具：列出问题（带上下文）、问题过滤与白名单管理。
- tools.names: 人名表工具（含用 GPT 字典回填 name 字段）。
- tools.jobs: 翻译任务类工具：启动/停止翻译、等待、运行时状态与错误汇总。
- tools.preview: 审批卡的「将要变更」预览：写类工具执行前先算出 diff。
- tools.render_md: 工具结果的 Markdown 表格渲染（给模型看的输出）。
- tools.cache: 翻译缓存类工具：列出、读取、修改、删除缓存条目，以及读取输出文件。
- tools.search: 搜索类工具：在翻译缓存与待翻译原文里搜索（分页）。
- tools.ask: 询问用户（ask_user）与压缩归档回查工具。
- subagent: 子代理（subagent）：把大批量的复核/探索工作拆给并行的子代理。
- registry: AgentRuntime：进程内的会话注册表（启动/消息/停止/恢复/事件读取）。
"""

from __future__ import annotations

import importlib as _importlib

_MODULES = (
    "GalTransl.Agent.core",
    "GalTransl.Agent.tools.common",
    "GalTransl.Agent.tools.cache_fields",
    "GalTransl.Agent.prompts",
    "GalTransl.Agent.handlers",
    "GalTransl.Agent.permissions",
    "GalTransl.Agent.models",
    "GalTransl.Agent.runner",
    "GalTransl.Agent.context",
    "GalTransl.Agent.backend_http",
    "GalTransl.Agent.tool_schemas",
    "GalTransl.Agent.tools.project",
    "GalTransl.Agent.tools.listing",
    "GalTransl.Agent.tools.input",
    "GalTransl.Agent.tools.dicts",
    "GalTransl.Agent.tools.problems",
    "GalTransl.Agent.tools.names",
    "GalTransl.Agent.tools.jobs",
    "GalTransl.Agent.tools.preview",
    "GalTransl.Agent.tools.render_md",
    "GalTransl.Agent.tools.cache",
    "GalTransl.Agent.tools.search",
    "GalTransl.Agent.tools.ask",
    "GalTransl.Agent.subagent",
    "GalTransl.Agent.registry",
)

for _name in _MODULES:
    _mod = _importlib.import_module(_name)
    globals().update({k: v for k, v in vars(_mod).items() if not k.startswith("__")})
del _name, _mod
