"""系统提示词的结构：每条规则只说一处，工具的参数细节留在工具描述里。

回归背景：提示词开头曾是三段 cache_history / search_replace_cache 的参数与返回字段说明，
排在身份和流程之前；试译规则、「GPT 字典优先」、问题过滤规则又各在流程和约束里重复了
几遍。重复的规则改一处漏一处，后来的版本就互相矛盾。这里守住"各留一处"。
"""

import unittest

from GalTransl.Agent.prompts import AGENT_SYSTEM_PROMPT
from GalTransl.Agent.tool_schemas import AGENT_TOOLS


def _description(name: str) -> str:
    return next(t["function"]["description"] for t in AGENT_TOOLS if t["function"]["name"] == name)


class PromptStructureTests(unittest.TestCase):
    def test_prompt_opens_with_identity_not_tool_parameters(self):
        head = AGENT_SYSTEM_PROMPT[:AGENT_SYSTEM_PROMPT.index("# 你的身份")]
        self.assertNotIn("cache_history", head)
        self.assertNotIn("search_replace_cache", head)

    def test_tool_parameter_details_live_in_the_tool_descriptions(self):
        for fragment in ("matched_entries", "would_update", "status=partial", "change_ids"):
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, AGENT_SYSTEM_PROMPT)
        replace = _description("search_replace_cache")
        for fragment in ("matched_entries", "would_update", "no_change 表示", "无匹配的文件正常跳过"):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, replace)
        self.assertIn("change_ids", _description("cache_history"))

    def test_cross_tool_write_conventions_stay_in_the_constraints(self):
        constraints = AGENT_SYSTEM_PROMPT[AGENT_SYSTEM_PROMPT.index("# 约束"):]
        self.assertIn("写入工具必须显式传 action", constraints)
        self.assertIn("统一记录在 cache_history 中", constraints)
        self.assertIn("mode、patch/add/remove 动作已经停用", constraints)

    def test_each_rule_is_stated_once(self):
        for fragment in (
            "第一优先",  # GPT 字典优先：只在「约束」
            "抄进",  # 不把字典译名重复抄进人名表
            '必须同时传 files 和 sentence_range',  # 试译：只在流程 3
            "禁止直接把整类问题加进过滤",  # 过滤规则：只在流程 7e
            'target="white_list"',
            "job_error",
        ):
            with self.subTest(fragment=fragment):
                self.assertEqual(AGENT_SYSTEM_PROMPT.count(fragment), 1)
        self.assertGreater(AGENT_SYSTEM_PROMPT.index("第一优先"), AGENT_SYSTEM_PROMPT.index("# 约束"))
        self.assertLess(AGENT_SYSTEM_PROMPT.index("必须同时传 files 和 sentence_range"),
                        AGENT_SYSTEM_PROMPT.index("4. **启动翻译（全量）**"))


if __name__ == "__main__":
    unittest.main()
