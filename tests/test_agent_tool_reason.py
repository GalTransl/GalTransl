"""写类工具的可选入参 reason：模型交代"为什么改这次"，随结果回传给界面。

schema 声明 + 分发时搬到结果上（_attach_reason）。这里锁住三件事：写类工具填了要能
带出来、没填不能出现空键（否则界面会画一行空的「原因」）、读类工具一律不动。
"""

import unittest

from GalTransl.Agent.runtime import (
    AGENT_TOOLS,
    _REASON_PROPERTY,
    _WRITE_TOOLS_WITH_REASON,
    _attach_reason,
)


def _tool_schema(name: str) -> dict:
    for tool in AGENT_TOOLS:
        if tool["function"]["name"] == name:
            return tool["function"]
    raise AssertionError(f"{name} 不在工具表里")


class ReasonSchemaTests(unittest.TestCase):
    def test_write_tools_declare_reason_as_optional(self) -> None:
        for name in sorted(_WRITE_TOOLS_WITH_REASON):
            schema = _tool_schema(name)
            # 同一份描述对象，八个工具不各写一遍
            self.assertIs(schema["parameters"]["properties"].get("reason"), _REASON_PROPERTY, name)
            self.assertNotIn("reason", schema["parameters"].get("required") or [], name)

    def test_read_tools_do_not_declare_reason(self) -> None:
        for name in ("get_project_overview", "read_transl_cache", "read_guideline", "list_problems"):
            properties = _tool_schema(name)["parameters"]["properties"]
            self.assertNotIn("reason", properties, name)

    def test_declared_reason_tools_match_the_constant(self) -> None:
        """工具表里声明了 reason 的集合必须与常量一致：加了写类工具忘了登记，这里会红。

        wait 除外——它的 reason 是"为什么等"（显示在那一行的倒计时后面），跟写类工具的
        "为什么改"无关，也不在 _WRITE_TOOLS_WITH_REASON 里。
        """
        declared = {
            tool["function"]["name"]
            for tool in AGENT_TOOLS
            if "reason" in tool["function"]["parameters"].get("properties", {})
        }
        self.assertEqual(declared - {"wait"}, set(_WRITE_TOOLS_WITH_REASON))


class AttachReasonTests(unittest.TestCase):
    def test_reason_is_copied_to_result_and_trimmed(self) -> None:
        out = _attach_reason(
            "patch_transl_cache", {"reason": "  第 33 句残留日文：统一为「多鲁德」  "}, {"updated": 1}
        )
        self.assertEqual(out["reason"], "第 33 句残留日文：统一为「多鲁德」")
        self.assertEqual(out["updated"], 1)  # 原有字段不动

    def test_missing_or_blank_reason_is_not_added(self) -> None:
        for args in ({}, {"reason": ""}, {"reason": "   "}, {"reason": None}):
            self.assertNotIn("reason", _attach_reason("save_dict", args, {"file_key": "x"}))

    def test_read_tools_are_untouched(self) -> None:
        result = {"entries": []}
        self.assertIs(_attach_reason("read_transl_cache", {"reason": "为什么"}, result), result)

    def test_non_dict_result_is_untouched(self) -> None:
        result = ["a", "b"]
        self.assertEqual(_attach_reason("save_dict", {"reason": "为什么"}, result), result)

    def test_tool_own_reason_wins(self) -> None:
        out = _attach_reason("save_dict", {"reason": "模型填的"}, {"reason": "工具自己写的"})
        self.assertEqual(out["reason"], "工具自己写的")


if __name__ == "__main__":
    unittest.main()
