"""Agent 工具的 OpenAI function schema（AGENT_TOOLS）。"""

from __future__ import annotations

from typing import Any

from GalTransl.Agent.core import RUNTIME_ERRORS_PER_QUERY, SUBAGENT_AGENTS, SUBAGENT_MAX_TASKS


# ---- Agent 工具的 OpenAI function schema ----

# 带 reason 入参的工具（写类：改配置/规范/字典/缓存，加上启动翻译）共用的可选参数：
# 让模型自己交代"为什么这么做"。
# **怎么填、填了显示在哪里，只在 system prompt 的约束里写一份**（见 AGENT_SYSTEM_PROMPT），
# 这里只说明"这是什么"，这些工具引用同一个 dict，既不重复解释也不各写一遍。
# 哪些工具带这个参数见 _TOOLS_WITH_REASON（_attach_reason 按它把 reason 挂回结果）。
_REASON_PROPERTY: dict[str, Any] = {
    "type": "string",
    "description": "可选。这次操作的原因（怎么填、显示在哪见系统提示词里那条约束）。",
}

AGENT_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_input_files",
            "description": "列出待翻译的输入文件（原文）与每个文件解析出的条数，供估工作量与挑选代表性文件（不必再逐个 read_input_file 数句子）。条数是原文解析出的条数（文本插件如「跳过无日文句」还没跑，可能偏大）；**只用于估工作量，不代表进度**（不管这个文件有没有缓存）——进度看 get_project_overview 的 files_translated/files_total。文件很多时默认只返回 100 个（order=even：**均匀采样**，含首尾、等距摊满整个清单，不是前 100 个；sentences_total 仍是整份清单的合计），要缩小范围用 grep（文件名子串），换挑选方式用 order。返回 Markdown 表格 + 文字说明（格式见系统提示）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "grep": {"type": "string", "description": "可选。按文件名过滤（子串、大小写不敏感），如 \"sc_2\"、\"pr00\"。"},
                    "limit": {"type": "integer", "description": "可选。最多返回多少个文件（默认 100，上限 500）；超出时按 order 挑选。"},
                    "order": {
                        "type": "string",
                        "enum": ["even", "name", "random", "size_desc", "size_asc"],
                        "description": "可选。清单的排列与采样方式（默认 even）：even=按文件名顺序均匀采样（含首尾、等距摊满整个清单）；name=按文件名顺序取前 limit 个；random=随机采样 limit 个（每次调用可能不同）；size_desc=按文件大小从大到小取前 limit 个；size_asc=按文件大小从小到大取前 limit 个。",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_input_file",
            "description": "读取待翻译原文内容（文件插件解析后的条目：说话人+原文）。index 统一从 1 开始；留空 index 返回前 30 条；指定 index 支持区间，如 \"1-100\"。试译前用它了解原文文风、角色、专有名词。返回 Markdown 表格 + 文字说明（格式见系统提示）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "输入文件名，来自 list_input_files。"},
                    "index": {
                        "type": "string",
                    "description": "可选。要读取的条目 index（从 1 开始），支持逗号和区间，如 \"1-100\"。留空返回前 30 条。",
                    },
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_input",
            "description": "在**待翻译原文**里搜关键词或说话人（read_transl_cache 搜索（action=search）的原文侧对应工具：那边搜缓存=原文+译文+问题，这边只搜还没翻译的原文全文）。query 为关键词，field 取 all/src（原文正文）/name（说话人）；传 context=N 让每条命中再带上 N 句上文（默认只给上文，要前后都给传 only_preceding=false；上下文行的 index 带 *）。field=all 时顶层 matched_in 汇总命中在原文还是说话人。典型用途：定译法/收字典前先查某个称呼或专有名词在全篇出现过几次、都出现在哪些上下文（出现次数与说话人是「该不该收、收哪个写法」的依据），以及比 read_input_file 逐段读更省 token 地定位语境；命中的 filename+index 可直接交给 read_input_file 精读。传 filename 只搜某个输入文件（来自 list_input_files），留空搜全部输入文件——**每次搜索都要把涉及的输入文件过一遍文件插件（比搜缓存慢），要缩小范围就传 filename**。注意译文侧的问题（漏译/残留日文/译名是否统一）不在原文里，那些用 read_transl_cache 的 action=search。命中多时分页看：整页最多 200 行，limit 是本页最多几条命中（默认 100、最大 200；带 context 时命中上限按行数换算，如 context=3 → 最多 28 条），offset 是跳过前几条命中；结果里的 returned 是本页命中数、has_more 表示还有下一批，还有就把 offset 加上 returned 再查一次。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "field": {"type": "string", "enum": ["all", "src", "name"]},
                    "filename": {"type": "string", "description": "可选。只在这个输入文件里搜（来自 list_input_files）。留空搜全部输入文件。"},
                    "context": {
                        "type": "integer",
                        "description": "可选，0-20（默认 0）。每条命中再带上文（见 only_preceding），用于判断语意与称呼用法。带上下文时整页最多 200 行，命中上限按行数换算、会明显收紧（如 context=3 → 最多 28 条命中），命中很多时可配合 limit/offset 翻页或 filename 缩小范围。上下文行的 index 带 *（如 12*），那不是命中行。",
                    },
                    "only_preceding": {
                        "type": "boolean",
                        "description": "可选，默认 true：带 context 时只返回上文（判断这句为什么这么翻通常看上文就够，还省 token）；要前后两边都给传 false。",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "可选。本页最多返回几条命中，默认 100，最大 200（带 context 时还会按行数预算再收紧，整页最多 200 行）。total 始终是全部命中数。",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "可选。分页偏移：跳过前 N 条命中（默认 0，前后文行不算数）。配合 has_more 翻页。",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_guideline",
            "description": "读取翻译规范（决定文风与措辞）。scope=global（默认）读全局规范库：不带 name 列出可选文件名，传 name（如 \"日译中_增强v2.md\"）返回全文。scope=project 读**项目规范**——项目目录里的 translation_guideline.md，是这个项目专属的规则，翻译时拼在全局规范之后、冲突时以它为准。试译定稿前必读；要改文风/术语/称呼前，先看项目规范里已经写了什么。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "可选。scope=global 时的规范文件名，来自不带参数调用返回的列表。"},
                    "scope": {
                        "type": "string",
                        "enum": ["global", "project"],
                        "description": "可选。global（默认）读全局规范库；project 读本项目的项目规范。",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_project_guideline",
            "description": "写**项目规范**（项目目录里的 translation_guideline.md）。三种模式：overwrite=整份覆写；append=末尾增写；replace=把 old_text 换成 new_text（old_text 要原样来自规范全文、且只出现一次，否则会报错让你带上更多前后文）。规范是写给翻译模型的，要具体可执行（术语对照、称呼、语气、标点习惯、禁忌），别写「要地道」这类空话。返回里带这一次改动的行级 diff（新增/删除的行、增删计数），不用再读一遍文件确认。",
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["overwrite", "append", "replace"],
                        "description": "写入方式：overwrite 覆写整份 / append 末尾增写 / replace 替换某段。",
                    },
                    "content": {"type": "string", "description": "mode=overwrite / append 时的规范文本（markdown）。"},
                    "old_text": {"type": "string", "description": "mode=replace 时要被替换的原文，连同前后文一起给，确保在规范里唯一。"},
                    "new_text": {"type": "string", "description": "mode=replace 时替换成的内容；传空串表示删掉这一段。"},
                    "reason": _REASON_PROPERTY,
                },
                "required": ["mode"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_project_overview",
            "description": "了解项目：查看翻译进度、实际生效的后端与项目配置。进度含句数 total/translated/problems/failed 和文件级 files_total/files_translated/files_untranslated；total/translated 只统计已生成缓存的文件，未翻译的文件不计入分母，translated==total 不代表整个项目翻完，整体进度看 files_translated/files_total。backend 里是两份实际生效的后端（各含 name 配置名 / type 后端类型 / model 模型名，不含地址与密钥）：agent 是本会话在用的，translator 是翻译任务会用的。流程第一步调用它确认项目可用；配置与配置键说明基本不变，之后再查进度只传 include=[\"progress\"] 即可，别重复拉。输入文件清单本身用 list_input_files / read_transl_cache（action=list）单独查询。",
            "parameters": {
                "type": "object",
                "properties": {
                    "include": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["progress", "backend", "config", "config_field_descriptions"],
                        },
                        "description": (
                            "可选。只返回这几部分（名字即返回体的键），用于避免重复拉取基本不变的内容："
                            "progress=进度；backend=实际生效的两份后端；config=项目配置；"
                            "config_field_descriptions=每个配置键的作用与取值说明（约 40 条，基本不变，"
                            "看过一次就不用再取）。留空返回全部。"
                        ),
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dict_files",
            "description": "列出项目配置的译前字典(preDict)、GPT字典(gpt.dict)、译后字典(postDict)文件与各文件行数（不含内容，读内容用 read_dict）。准备字典阶段使用。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_dict",
            "description": "读取某个项目字典文件的完整内容（按 file_key，来自 list_dict_files 返回的 pre_dict_files / gpt_dict_files / post_dict_files）。",
            "parameters": {
                "type": "object",
                "properties": {"file_key": {"type": "string", "description": "字典文件 key，形如 (project_dir)项目GPT字典.txt"}},
                "required": ["file_key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_dict",
            "description": "写入/维护某个项目字典文件。file_key 必须来自 list_dict_files；content 为 tab 分隔文本（格式：日文<Tab>中文[<Tab>解释]）。action 决定操作：overwrite（默认，整文件覆盖）、replace（按 key 替换已有词条，未匹配的 key 不新增）、append（追加到末尾，重复 key 跳过）、delete（按 key 删除词条）。补充新词条优先用 append，避免重发整份字典；delete 的 content 可整行粘贴，也可只写 key。新建字典文件也用它：带上 category（pre=译前 / gpt=GPT / post=译后），file_key 写新文件名（如 项目GPT字典2.txt），文件不存在时会先新建并登记到对应的字典清单再写入（新建时 action 用 overwrite 或 append；content 可为空，只建空文件）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_key": {"type": "string", "description": "字典文件 key（来自 list_dict_files，形如 (project_dir)项目GPT字典.txt）；新建时写新文件名即可"},
                    "content": {"type": "string", "description": "要写入的字典内容（tab 分隔文本）；delete 时传要删除的词条（每行一个，可整行或只写 key）"},
                    "category": {
                        "type": "string",
                        "enum": ["pre", "gpt", "post"],
                        "description": "可选。文件还不存在时新建到哪一类：pre=译前，gpt=GPT，post=译后。已存在的文件忽略它。",
                    },
                    "action": {
                        "type": "string",
                        "enum": ["overwrite", "replace", "append", "delete"],
                        "description": "overwrite=全量覆盖（默认）；replace=按 key 部分替换已有词条；append=追加到末尾（重复 key 跳过）；delete=按 key 删除词条。",
                    },
                    "reason": _REASON_PROPERTY,
                },
                "required": ["file_key", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_name_table",
            "description": "读取 name替换表（人名表），返回 src_name/dst_name/count 列表。为空说明尚未生成。配置 dictionary.useGPTDictInName 开着时（默认开），译名为空而 GPT 字典已收录的行会按字典译名补上（带 dst_name_source=gpt_dict），并额外返回 filled_from_gpt_dict 与 still_empty 两份清单——**还缺哪些名字看 still_empty**。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_name_table",
            "description": "保存人名表（写入 name替换表.csv）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "names": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"src_name": {"type": "string"}, "dst_name": {"type": "string"}, "count": {"type": "integer"}},
                        },
                    },
                    "reason": _REASON_PROPERTY,
                },
                "required": ["names"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_translation",
            "description": "提交一个翻译任务。translator 取值：ForGal-json/ForGal-tsv/ForNovel（主翻译）；GenDic（生成GPT字典）；dump-name（导出人名表）；rebuilda（用字典重建缓存+结果，跳过翻译，复核时用这个才能在 list_problems 看到变化）；rebuildr（只重建结果 json，不更新缓存，一般不用）。任务用的是「翻译任务会用」的那份后端（项目选择 → 否则全局「翻译器默认」），不是本 Agent 会话自己那份；返回里的 backend 会写明实际用的模型。传 files 只翻译指定的输入文件（试译时用：只翻一两个文件验证文风）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "translator": {"type": "string"},
                    "files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可选。只翻译这些输入文件（文件名来自 list_input_files），如试译只翻第一个文件。留空翻译全部。",
                    },
                    "reason": _REASON_PROPERTY,
                },
                "required": ["translator"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_translation",
            "description": "停止当前项目正在运行的翻译任务。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait",
            "description": (
                "等待一段时间后继续。用于翻译/GenDic 等后台任务还在跑、需要隔一会儿再看进度的场景。"
                "两种用法：① 只给时长——纯等这么久；② 时长 + job_id——**盯着这个任务等：它先结束就立刻返回，"
                "时长先到就照常返回**（例：「等这个任务完成，或最多等 5 分钟再看看」= job_id 给任务 id、minutes=5）。"
                "用法：先 get_runtime 确认任务在跑 → wait → wait 结束后再 get_runtime 查状态（completed / 仍在跑看 eta_seconds 决定下一轮等多久）。"
                "注意：没给 job_id 时 wait 结束只代表计时到了，不代表后台任务完成，必须查任务状态确认。"
                "等待期间界面会显示倒计时；若用户期间点了停止，会立即中断等待。"
                "单次最多等待 1800 秒（30 分钟）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "seconds": {
                        "type": "number",
                        "description": "等待的秒数。与 minutes 二选一；两个都传时以二者之和为准。",
                    },
                    "minutes": {
                        "type": "number",
                        "description": "等待的分钟数。适合等待较久的翻译任务。",
                    },
                    "job_id": {
                        "type": "string",
                        "description": "可选。要等哪个任务（start_translation 返回的 job_id）。给了它就盯着这个任务：它先跑完（completed / failed / cancelled）就立刻返回，不必等满时长；时长先到而它还在跑，则照常返回并带上它当前的状态 + 一份运行时快照（等同 get_runtime，含 summary.eta_seconds，不必再单独查一次）。仍然必须给一个时长（那是兜底上限）。",
                    },
                    "reason": {
                        "type": "string",
                        "description": "可选。等待原因，会显示在界面上，如 '等待翻译任务完成'。",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_runtime",
            "description": (
                "查询运行时状态：当前任务状态(running/completed/failed)、阶段、本轮任务计数与 ETA、本轮新出现的错误。"
                "summary.total/percent 是**本轮任务**的口径（按任务计划统计，含正在翻译、缓存尚未落盘的文件）；"
                "已落盘缓存的口径与文件级完成度看 get_project_overview 的 progress——两个 total 分母不同，"
                "数字不一致是正常的，不要为了对齐它们多查一轮。"
                "recent_errors 是**上次查询之后新出现**的错误，同类（同 kind/同原因）已合并为一条："
                "count 是本次新增次数、text 是可直接读的一行、files 是涉及的缓存文件（最多列 5 个），"
                f"单次最多 {RUNTIME_ERRORS_PER_QUERY} 类；已发过的不再重复，另有 recent_errors_pending 表示还没发完的新错误数。"
                "列表为空只代表没有新错误，不代表之前的问题已消失——整体问题情况用 list_problems 查。"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_project_config",
            "description": "修改项目配置（与桌面端「项目配置」页同一通道）。键名与 get_project_overview 返回的 config/config_field_descriptions 一致（如 \"common.gpt.contextNum\"、\"common.language\"、\"common.gpt.translation_guideline\"），只允许改已存在的键。适合调整翻译参数、切换翻译规范文件、启停问题检测项等；改完对新启动的翻译任务生效。",
            "parameters": {
                "type": "object",
                "properties": {
                    "updates": {
                        "type": "array",
                        "description": "要修改的键值对列表，一次可改多个。",
                        "items": {
                            "type": "object",
                            "properties": {
                                "key": {"type": "string", "description": "配置键的点号路径，如 \"common.gpt.contextNum\"。说明见 get_project_overview 的 config_field_descriptions。"},
                                "value": {"description": "新值，类型跟随配置原值（数字/布尔/字符串/列表）。"},
                            },
                            "required": ["key", "value"],
                        },
                    },
                    "reason": _REASON_PROPERTY,
                },
                "required": ["updates"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_problems",
            "description": "查询自动检测到的翻译问题（残留日文、字典使用、过长等）。默认返回类型统计（各类型问题数）；传 problem_type 查看该类型的具体条目，支持分页。传 context=N 让每条问题在表里并上 N 句上文（默认只给上文，要前后都给传 only_preceding=false；上下文行的 index 带 *）——判断\"这句到底哪里有问题、该怎么改\"通常直接看这张表就够了，不必再逐条 read_transl_cache。trans_by 与 read_transl_cache 同一套：逐行只给少数派（本会话改过的、手工改的），多数派记在顶层 majority_trans_by。返回 Markdown 表格 + 文字说明（格式见系统提示）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "problem_type": {
                        "type": "string",
                        "description": "可选。要查看的问题类型（来自默认返回的统计列表，如 \"残留日文\"），支持逗号分隔多个；传 \"*\" 返回所有类型的具体条目。留空只返回类型统计。",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "可选。单次返回条目数，默认 10，最大 20。",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "可选。分页偏移，默认 0。配合 has_more 翻页。",
                    },
                    "context": {
                        "type": "integer",
                        "description": "可选，0-5（默认 0）。每条问题在表里并上 N 句上文（见 only_preceding）；相邻问题的窗口会合并、重复行只给一份。需要判断语意与改法时建议 2-3。上下文行的 index 带 *（如 12*），那不是本页的问题行。",
                    },
                    "only_preceding": {
                        "type": "boolean",
                        "description": "可选，默认 true：带 context 时只并上文（看这句为什么出问题通常看上文就够，还省 token）；要前后都并传 false。",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_problem_filter",
            "description": "管理问题过滤关键字（项目配置 common.problemFilterKey，与「缓存与问题」页同一套配置）。**正则匹配**：keyword 是一条正则，按 re.search 命中问题项的那一项会被 list_problems 与进度统计过滤掉（如 `缺失.*标点` 按样式、`比日文长：1\\.5倍` 精确到某条；正则里的特殊字符要转义，写坏的正则会被拒）。**原则上只过滤小类，不要过滤大类**：像 `残留日文`、`^残留日文：` 这种把整个问题大类藏起来的写法不要用——大类里通常混着真问题，整类过滤等于不再复核；确实个别条目不用再处理时用 manage_problem_white_list 按条目豁免。list 会给出每条过滤项当前各挡住了多少条问题（problems 为 0 说明它已经一条也挡不到，可考虑 remove）。keyword 可传字符串或数组，一次增删多个。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "add", "remove"],
                        "description": "list 查看当前关键字；add 添加；remove 移除。",
                    },
                    "keyword": {
                        "anyOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}},
                        ],
                        "description": "add/remove 必填。要操作的过滤项（**正则**，如 \"缺失.*标点\"、\"^残留日文：♪\"）；命中问题项的任意位置即过滤，特殊字符需转义（\\. \\( \\[ \\*）。原则上只过滤小类：整类写法（如 \"残留日文\"）禁止使用。可传单个字符串，也可传数组一次操作多个。",
                    },
                    "reason": _REASON_PROPERTY,
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_problem_white_list",
            "description": "管理问题白名单（项目配置 common.problemWhiteList）。白名单是「缓存文件 + 条目 index」的名单，命中的条目等价于勾选了 skip_check：不再检测/展示问题，也不计入问题统计。适合确认某几条译文无需再处理时按位置精确豁免（如个别专有名词、语气词导致的反复误报）。entry 传 \"文件名:index\"（如 \"01.json:12\"，区间写 \"01.json:12-15\"），可传字符串或数组一次增删多个。与 manage_problem_filter 的区别：filter 按问题文本子串整类过滤，白名单按具体条目豁免。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "add", "remove"],
                        "description": "list 查看当前白名单；add 添加；remove 移除。",
                    },
                    "entry": {
                        "anyOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}},
                        ],
                        "description": "add/remove 必填。要操作的条目，格式 \"<缓存文件名>:<index>\"（如 \"01.json:12\"；闭区间写 \"01.json:12-15\"）。可传单个字符串，也可传数组一次操作多条。",
                    },
                    "reason": _REASON_PROPERTY,
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_transl_cache",
            "description": (
                "读翻译缓存（译文）的唯一入口，action 分三种："
                "①list = 列出缓存文件与各文件条目数（文件很多时默认只给 100 个，order=even 是**均匀采样**、含首尾，"
                "不是前 100 个；grep 按文件名过滤，limit 调数量，上限 500）；"
                "②read = 读某个缓存文件（filename）的条目：留空 index 返回前 30 条，指定 index 只返回那些条目；"
                "默认只返回必要字段（index/说话人/原文/译文/问题，以及确实非空或与原文不同的附加字段），要看别的传 fields；"
                "grep 只留命中的条目（字符串 = 在所选字段里搜文本，数组 = 这些字段都非空）；"
                "③search = 在缓存里搜 query（field 选 all/src/dst/problem；filename 可选，只搜某个文件），"
                "命中多时分页：整页最多 200 行，limit 是本页最多几条命中（默认 100、最大 200），offset 跳过前几条，"
                "has_more 表示还有，下一页 offset 加上 returned；field=all 时 matched_in 汇总命中在哪一侧。"
                "read 和 search 都可以传 context=N 给目标条目/每条命中带 N 句上文（默认只给上文，前后都要传 only_preceding=false；"
                "上下文行的 index 带 *，那不是点名/命中的条目）；trans_by 逐条只给少数派，多数派记在 majority_trans_by。"
                "不传 action 时按参数推断：有 query 是 search，有 filename 是 read，否则是 list。"
                "返回 Markdown 表格 + 文字说明（格式见系统提示）。"
                "要把某条缓存展示给用户时，在回复里单独一行写 $transl_cache(\"<缓存文件名>\", <行号>)"
                "（行号 = 条目 index，区间 12-15 / 列表 12,20 均可），界面会把它渲染成那几行缓存的卡片。"
                "例：read_transl_cache(action=\"list\", grep=\"sc_2\")；"
                "read_transl_cache(action=\"read\", filename=\"sc_2_st01.txt.json\", index=\"33-40\", context=3)；"
                "read_transl_cache(action=\"search\", query=\"ドルード\", field=\"src\", context=3)。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "read", "search"],
                        "description": "list=列缓存文件；read=读某个文件的条目；search=在缓存里搜。不传则按参数推断（有 query→search，有 filename→read，否则 list）。",
                    },
                    "filename": {
                        "type": "string",
                        "description": "缓存文件名（来自 action=list 的清单）。read 必填；search 可选，只在这个文件里搜（留空搜全项目）；list 不用。",
                    },
                    "index": {
                        "type": "string",
                        "description": "read 用，可选。要读取的条目 index 列表，支持逗号和区间，如 \"33-40,50-60\"、\"5,9,12\"。留空返回前 30 条。",
                    },
                    "query": {"type": "string", "description": "search 必填：要搜的关键词。"},
                    "field": {
                        "type": "string",
                        "enum": ["all", "src", "dst", "problem"],
                        "description": "search 用：在哪一侧搜（默认 all）。src=原文，dst=译文，problem=问题描述。",
                    },
                    "context": {
                        "type": "integer",
                        "description": (
                            "read/search 用，可选，0-20。目标条目（read）/每条命中（search）向上多返回 N 句（见 only_preceding），"
                            "如 read 的 index=\"205-206\" context=3 返回 202~206。修问题、判断译名/语意时建议 2-4。"
                            "search 带 context 时整页最多 200 行，命中上限按行数换算（如 context=3 → 最多 28 条命中）。"
                        ),
                    },
                    "only_preceding": {
                        "type": "boolean",
                        "description": "read/search 用，可选，默认 true：带 context 时只返回上文（通常就够，还省 token）；要前后都给传 false。",
                    },
                    "grep": {
                        "anyOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}},
                        ],
                        "description": (
                            "可选。list：按文件名过滤（子串、大小写不敏感），如 \"sc_2\"。"
                            "read：只返回命中的条目——①字符串 = 在 fields 选中的字段（不传 fields 则默认精简集）内容里"
                            "做大小写不敏感的子串搜索，如 grep=\"残留日文\"；②字符串数组 = 每个元素当字段名，只保留这些字段都不为空的条目，"
                            "如 grep=[\"problem\",\"proofread_comment\"]。与 index 同用时先按 grep 过滤，再按 index 取。"
                        ),
                    },
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "read 用，可选。每条要返回哪些字段：index、name（说话人）、pre_src（原句）、pre_dst（译文）、"
                            "post_src、post_dst_preview（译后字典替换后的预览）、proofread_dst、proofread_by、"
                            "trans_by、problem。"
                            "不传 = 默认精简集（index/name/post_src/pre_dst/problem；post_dst_preview 仅在译后处理真的改了内容时给，"
                            "空值省略；要看 pre_src/trans_by 等列得显式传 fields）；传 [\"pre_dst\",\"problem\"] 这类只要某几列。"
                            "trans_by 逐条只给少数派（多数派 = 这批里出现最多的那个模型，通常就是引擎翻的；它记在返回的 majority_trans_by 里）。"
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": "可选。list：最多返回多少个文件（默认 100，上限 500）；search：本页最多几条命中（默认 100，最大 200，带 context 时还会按行数收紧）。",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "search 用，可选。分页偏移：跳过前 N 条命中（默认 0，前后文行不算数）。配合 has_more 翻页。",
                    },
                    "order": {
                        "type": "string",
                        "enum": ["even", "name", "random", "size_desc", "size_asc"],
                        "description": "list 用，可选（默认 even）：even=按文件名顺序均匀采样（含首尾）；name=按文件名顺序取前 limit 个；random=随机采样；size_desc/size_asc=按文件大小从大到小/从小到大取前 limit 个。",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_output",
            "description": "读取最终输出文件（gt_output，交付物）。输出是缓存经译后字典替换、控制符处理后的最终形态，与缓存可能不完全一致——验收交付物、确认 postDict 替换效果用这个，而不是 read_transl_cache。文件名通常与输入文件同名。留空 index 返回前 30 条。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "输出文件名，通常与输入文件同名（如 sc_0_pr00.txt.json）"},
                    "index": {
                        "type": "string",
                    "description": "可选。要读取的条目 index（从 1 开始），支持逗号和区间（如 \"1-100\"）。留空返回前 30 条。",
                    },
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_transl_cache",
            "description": "删除缓存（条目或整个文件）。物理删除后，重启翻译时被删除的句子会因缓存未命中而重新翻译——这是触发部分重翻的手段。注意：删除不可撤销；rebuilda/rebuildr 依赖缓存，删除后不要再跑重建。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "缓存文件名（来自 get_project_overview 的 cache_files）。传 \"*\" 删除全部缓存文件。",
                    },
                    "indexes": {
                        "type": "string",
                        "description": "可选。要删除的条目 index 列表，支持逗号和区间（如 \"33-40,50-60\"，index 来自 read_transl_cache/list_problems）。留空则删除整个文件。",
                    },
                    "reason": _REASON_PROPERTY,
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "patch_transl_cache",
            "description": "批量修改缓存条目的译文（pre_dst / proofread_dst 两列）。**一次调用可以跨多个缓存文件**（统一译名/术语这类活一次就交完）：patches 里每条自带 file；只改一个文件时用顶层 filename、patches 不带 file。只更新 patches 里点名的条目与字段，其它条目原样保留。**按校对批注改完一批译文后，顶层带 clear_comment=true**：点名的条目的 proofread_comment 一并清空（表示这些意见已处理），不必在每条 patch 里各写一遍空串。返回是一篇**按文件分组的 Markdown**：每个文件一节，先列「改了什么」（每条 before→after），再列没落地的条目（index 不存在 / 字段不许改及原因）与「改完仍存在的问题」（只列被改过的条目——没列到的就是消掉了）；改了什么一目了然、有没有引入新问题当场可验，不必再 read_transl_cache。适合发现问题后改译文、再配合 rebuilda 重建的复核循环。trans_by 由工具自动标记，不用手动指定。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "可选。默认缓存文件名（来自 read_transl_cache 的 list 清单）：patches 里没写 file 的都改它。**只改一个文件就写它**；要一次改多个文件，就每条 patch 都写 file，这里可以不写"},
                    "patches": {
                        "type": "array",
                        "description": "要改的条目，按顺序应用；跨文件时每条带上 file",
                        "items": {
                            "type": "object",
                            "properties": {
                                "file": {"type": "string", "description": "可选。这条改哪个缓存文件；不写就用顶层 filename。一次要改多个文件就每条都写它"},
                                "index": {"type": "integer", "description": "要修改的条目 index"},
                                "pre_dst": {"type": "string", "description": "可选。新译文（机翻结果）"},
                                "proofread_dst": {"type": "string", "description": "可选。新校对译文（校对/润色结果，优先于 pre_dst）"},
                                "proofread_comment": {"type": "string", "description": "可选。校对批注（校对子代理写下的意见：校对建议或润色建议，见 run_subagents）。按它改完译文后传空串清掉，表示这条已处理；一批都要清就用顶层 clear_comment=true，不必每条各写一遍"},
                            },
                            "required": ["index"],
                        },
                    },
                    "clear_comment": {
                        "type": "boolean",
                        "description": "可选（只写一次，对所有 patch 生效）。true = 把这次点名条目的校对批注（proofread_comment）一并清空——按批注改完译文、这条已处理时用它，省得在每条 patch 里各写一遍空串。某条 patch 自己写了 proofread_comment 的以它为准；本来就没有批注的条目不会产生变更。",
                    },
                    "reason": _REASON_PROPERTY,
                },
                "required": ["patches"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_subagents",
            "description": (
                "派一批子代理并行干活，等它们全部跑完，把每份报告收回来。子代理有自己的上下文与"
                "受限工具集，干活过程不进你的上下文，返回给你的只有每份报告。两种角色："
                "**proofread（校对）**——只能读缓存/人名表/规范/问题清单，加写缓存条目的「校对批注」"
                "（proofread_comment，校对建议与润色建议都写这里），改不了译文；适合翻译完成后逐文件校对，"
                "返回它写了哪些 index 的疑问，你用 read_transl_cache 读那些 proofread_comment、改完译文再清空它。"
                "**explore（原文探索）**——只读原文与 GPT 字典、不写任何文件；用来补 GenDic 覆盖不到的"
                "昵称/专有名词/称呼，以及给翻译规范提建议，结论在你的报告里由你汇总落地"
                "（save_dict / write_project_guideline）。explore 要通读原文、**很费 token**，"
                "属于可选项：派之前先用 ask_user 征得用户同意。"
                f"一次最多 {SUBAGENT_MAX_TASKS} 个，要它们重点看什么就写进 brief。"
                '要并行多个又不想写多条任务：一条任务写 file:"*" + count:N 就展开成 N 个'
                "（brief 只写一遍）。"
                "file / count / indexes 三个维度互相独立、任意组合：file 选谁（支持选择器）、"
                "count 切几份（对任何 file 都生效）、indexes 取哪段。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "description": f"要派的任务，1-{SUBAGENT_MAX_TASKS} 个（并行跑）。",
                        "items": {
                            "type": "object",
                            "properties": {
                                "agent": {
                                    "type": "string",
                                    "enum": list(SUBAGENT_AGENTS),
                                    "description": "子代理角色：proofread（校对缓存译文）/ explore（通读原文，提字典候选与规范建议）",
                                },
                                "file": {
                                    "type": "string",
                                    "description": '要负责的文件（"选谁"）。proofread 必填；explore 可留空（自己按 list_input_files 挑）。支持选择器：具体文件名（"01.json"）；"*"=该角色全部文件（自动均分）；"list:a.json,b.json"=清单；"glob:SW_01_*"=通配；"regex:^0[12]_"=正则；"select:has_problem"=有问题的文件，"select:problem_type=残留日文"=含某类问题的文件（直接吃 list_problems 的结果集）；"random:N"=从候选里随机挑 N 个（前期探索用）。本批里已具体选中的文件不会再分给 "*"（点名优先）。锁定是工具层强制的：子代理只能读写派给它的那些文件，范围外会被拒',
                                },
                                "count": {
                                    "type": "integer",
                                    "description": '可选。把这一条任务"切几份"（默认 1，上限同批任务数），对任何 file 都生效：选中的文件数 >= count 就按文件均分；文件数 < count 就把大文件按 index 切成 count 段并行。例：{file:"*", count:16} 全项目均分给 16 个；{file:"select:has_problem", count:16} 把有问题的文件均分给 16 个；{file:"03_RE13.json", count:4} 把一个大文件切 4 段。brief 只写一遍、由展开出的子代理共用',
                                },
                                "indexes": {
                                    "type": "string",
                                    "description": '可选。"取哪段"：只处理这个区间（写法同 read_transl_cache 的 index，如 "1-200" 或 "1-200,300-400"）；留空=整个文件。与 count 组合时在区间内再切段（只对单个文件有意义）：{file:"03_RE13.json", indexes:"1-200", count:4} = 只在前 200 条里切 4 段并行',
                                },
                                "brief": {
                                    "type": "string",
                                    "description": "可选。给这个子代理的额外要求：重点核对什么、注意哪些角色/术语；**校对子代理还要在这里写明这一遍写哪一类意见**（只写校对建议 / 只写润色建议 / 两者都要，先 ask_user 问用户，见流程 6.5），没写就默认只写校对建议。count > 1 时这一份 brief 由展开出来的每个子代理共用（不用重复写）",
                                },
                            },
                            "required": ["agent"],
                        },
                    },
                },
                "required": ["tasks"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_history_archive",
            "description": (
                "读本次会话被上下文压缩归档下来的早期对话（压缩摘要消息里列出的 chunk 文件）。"
                "不带参数：列出所有归档及主题，供你判断该查哪一份。带 chunk：返回该归档全文"
                "（超长会截断）。带 query：在所有归档里检索关键词，返回命中行及上下文，用来"
                "找回具体细节（早前定下的术语、某个文件的处理结论等）。只在摘要信息不够时"
                "才查，不要为了「确认一下」逐个通读归档。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "chunk": {
                        "type": "string",
                        "description": "归档文件名（如 chunk-0001.md）或序号（如 1）。留空 = 列出全部归档。",
                    },
                    "query": {
                        "type": "string",
                        "description": "可选。关键词，在各归档里检索并返回命中行及上下文。",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "可选。最多返回多少条命中/多少行，默认 30。",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": (
                "当你不确定该不该做（要不要动这个文件、要不要重翻）、或不确定该怎么翻译"
                "（用词、称谓、语气、风格取舍）时，向用户提问并等待回答，不要自己猜。每题给出"
                "2-6 个候选选项，用户还可以自己填；一次最多 4 题。每题都要填 recommended——"
                "你推荐的那个选项：「全自动-零打断」档位下后端会直接采用它替你作答、不打扰用户，"
                "其余档位只把它标成卡片上的「推荐」。用户跳过某题会以空答案返回（不算失败），"
                "你按自己的最佳判断继续即可。能从项目配置、字典或原文里判断出来的不要问——"
                "只有真的需要人来定夺时才用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "questions": {
                        "type": "array",
                        "description": "要问的问题（1-4 个）",
                        "items": {
                            "type": "object",
                            "properties": {
                                "question": {
                                    "type": "string",
                                    "description": "问题本身。写清背景与各选项的差别，让用户不必再看别处就能决定。",
                                },
                                "options": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "候选答案（2-6 个）；用户在卡片里还可以自己填。",
                                },
                                "multiSelect": {
                                    "type": "boolean",
                                    "description": "可选。true 表示可以多选，默认单选。",
                                },
                                "recommended": {
                                    "type": "string",
                                    "description": (
                                        "你推荐的那个选项，必须与 options 里的某一项一字不差。"
                                        "建议每题都填：「全自动-零打断」档位下后端直接采用它代答，"
                                        "不填就退而取第一个选项。"
                                    ),
                                },
                            },
                            "required": ["question", "options"],
                        },
                    },
                },
                "required": ["questions"],
            },
        },
    },
]
