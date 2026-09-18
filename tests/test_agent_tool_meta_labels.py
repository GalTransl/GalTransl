"""前后端工具名对照：每个后端工具在前端都要有中文名（TOOL_META）。

回归背景：read_output / read_history_archive 漏登记，转录里那几行直接显示英文工具名
（旁边别的工具都是「搜索缓存」「读取缓存」这种中文动作），看着就像"没翻译"。
缺条目时 toolMeta() 会退回显示原始工具名——那是给"还没收录的新工具"留的兜底，不该成为常态。

前端没有测试框架，所以这里从 AgentPage.tsx 里读出 TOOL_META 的键与 action 来对账
（跨语言的契约测试，和 test_agent_markdown_output 那类"口径一致性"测试同一路数）。
"""

import os
import re
import unittest

from GalTransl.Agent.runtime import AGENT_TOOLS

_PAGE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "desktop",
    "src",
    "pages",
    "AgentPage.tsx",
)
# TOOL_META 的对象字面量：每条键缩进两个空格
_ENTRY = re.compile(r"^\s{2}([a-z_][a-z0-9_]*):\s*\{", re.M)
_ACTION = re.compile(r"action:\s*'([^']*)'")
_CJK = re.compile(r"[\u4e00-\u9fff]")


def _tool_meta_block() -> str:
    with open(_PAGE, encoding="utf-8") as handle:
        source = handle.read()
    return source.split("const TOOL_META", 1)[1].split("\n};", 1)[0]


def _entries() -> list[tuple[str, str]]:
    """[(工具名, 该条目的源码片段)]，片段用来找它的 action。"""
    block = _tool_meta_block()
    starts = [(match.group(1), match.start()) for match in _ENTRY.finditer(block)]
    return [
        (name, block[start : starts[index + 1][1] if index + 1 < len(starts) else len(block)])
        for index, (name, start) in enumerate(starts)
    ]


class ToolMetaLabelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not os.path.isfile(_PAGE):
            raise unittest.SkipTest("没有前端源码（纯后端检出）")

    def test_every_backend_tool_has_a_frontend_entry(self) -> None:
        have = {name for name, _ in _entries()}
        missing = [
            tool["function"]["name"]
            for tool in AGENT_TOOLS
            if tool["function"]["name"] not in have
        ]
        self.assertEqual(
            missing,
            [],
            f"这些工具在前端 TOOL_META 里没有条目，转录里会显示英文工具名：{missing}",
        )

    def test_no_stale_frontend_entries(self) -> None:
        """反向也不能漂：留着后端已经删掉的工具，标签迟早张冠李戴（get_progress 就是这么赖着的）。"""
        names = {tool["function"]["name"] for tool in AGENT_TOOLS}
        stale = sorted(name for name, _ in _entries() if name not in names)
        self.assertEqual(stale, [], f"这些条目在后端已经没有对应工具：{stale}")

    def test_every_entry_has_a_chinese_action(self) -> None:
        """每条都要有中文 action（工具行显示的就是它），别只写 icon/summary 忘了名字。"""
        for name, entry in _entries():
            match = _ACTION.search(entry)
            self.assertIsNotNone(match, f"{name} 没有 action")
            action = match.group(1)
            self.assertTrue(_CJK.search(action), f"{name} 的 action 不是中文：{action!r}")


if __name__ == "__main__":
    unittest.main()
