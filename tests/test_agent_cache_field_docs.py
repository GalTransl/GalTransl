"""system prompt 里要讲清 transl_cache 每个字段的含义。

Agent 判断"这句译文为什么长这样、该改哪个字段、能不能改"全靠这些字段，所以说明必须与
字段清单同步：说明块由 CACHE_ENTRY_FIELDS + CACHE_ENTRY_FIELD_DESCRIPTIONS 生成，
默认精简列与可改字段列表也分别从 CACHE_ENTRY_FIELDS_DEFAULT / _PATCHABLE_FIELDS 生成，
这里把这些一致性锁死（加字段忘了写说明、或文档与代码漂移，测试都会红）。
"""

import unittest

from GalTransl.Agent.runtime import (
    CACHE_ENTRY_FIELDS,
    CACHE_ENTRY_FIELDS_DEFAULT,
    CACHE_ENTRY_FIELD_DESCRIPTIONS,
    AgentState,
    AgentToolError,
    _build_system_prompt,
    _cache_fields_section,
    _normalize_cache_fields,
    _patchable_fields_text,
)


def _state() -> AgentState:
    state = AgentState()
    state.project_dir = r"C:\proj\MyGame"
    state.config_file_name = "config.yaml"
    return state


class CacheFieldCoverageTests(unittest.TestCase):
    def test_every_field_is_documented(self) -> None:
        self.assertEqual(set(CACHE_ENTRY_FIELDS), set(CACHE_ENTRY_FIELD_DESCRIPTIONS))
        for name, description in CACHE_ENTRY_FIELD_DESCRIPTIONS.items():
            self.assertTrue(description.strip(), f"{name} 的说明为空")

    def test_field_names_are_the_real_cache_keys(self) -> None:
        """字段名就是缓存 JSON 的键（含兼容旧名的 pre_jp/post_jp 那套新名）。"""
        self.assertIn("pre_src", CACHE_ENTRY_FIELDS)
        self.assertIn("post_dst_preview", CACHE_ENTRY_FIELDS)
        self.assertNotIn("post_zh_preview", CACHE_ENTRY_FIELDS)  # 旧名由后端兼容，不在 Agent 口径里

    def test_removed_fields_are_gone_from_the_agent_surface(self) -> None:
        """trans_conf / doub_content / unknown_proper_noun：先不暴露给 Agent
        （管道仍会写进缓存，只是 Agent 读不到、也改不了）。"""
        removed = ("trans_conf", "doub_content", "unknown_proper_noun")
        for name in removed:
            self.assertNotIn(name, CACHE_ENTRY_FIELDS)
            self.assertNotIn(name, CACHE_ENTRY_FIELDS_DEFAULT)
            self.assertNotIn(name, CACHE_ENTRY_FIELD_DESCRIPTIONS)
            # 读：fields 里带它会被当成未知字段拒掉
            with self.assertRaises(AgentToolError):
                _normalize_cache_fields({"fields": [name]})
        # 写：可改字段只剩这三个
        self.assertEqual(_patchable_fields_text(), "pre_dst / proofread_dst / trans_by")

    def test_removed_fields_are_not_in_the_prompt(self) -> None:
        prompt = _build_system_prompt(_state())
        for name in ("trans_conf", "doub_content", "unknown_proper_noun"):
            self.assertNotIn(name, prompt)


class CacheFieldsSectionTests(unittest.TestCase):
    def test_section_lists_every_field_with_its_meaning(self) -> None:
        section = _cache_fields_section()
        for name, description in CACHE_ENTRY_FIELD_DESCRIPTIONS.items():
            self.assertIn(f"- {name}：{description}", section)

    def test_default_columns_and_patchable_list_are_generated(self) -> None:
        section = _cache_fields_section()
        self.assertIn(
            " / ".join(CACHE_ENTRY_FIELDS_DEFAULT),
            section,  # 默认精简列与常量一致
        )
        self.assertIn(_patchable_fields_text(), section)  # 可改字段列表与白名单一致
        # problem / post_* 是派生字段，不能出现在"只能改"的那句里
        patchable_sentence = section.split("只能改 ")[1].split("；")[0]
        for derived in (
            "problem",
            "pre_src",
            "post_src",
            "post_dst_preview",
            "index",
            "name",
            "trans_conf",
            "doub_content",
            "unknown_proper_noun",
        ):
            self.assertNotIn(derived, patchable_sentence)

    def test_key_semantics_are_explained(self) -> None:
        section = _cache_fields_section()
        self.assertIn("前润", section)  # post_src 与 pre_src 的区别
        self.assertIn("译前字典", section)
        self.assertIn("译后字典", section)
        self.assertIn("proofread_dst ＞ pre_dst", section)  # 哪个才是最终译文
        self.assertIn("派生字段", section)  # 哪些改不动
        self.assertIn("缓存 ≠ 交付物", section)  # 别拿缓存当输出
        self.assertIn("read_output", section)


class SystemPromptInclusionTests(unittest.TestCase):
    def test_section_is_in_the_system_prompt(self) -> None:
        prompt = _build_system_prompt(_state())
        self.assertIn("# 缓存（transl_cache）字段说明", prompt)
        for name in CACHE_ENTRY_FIELDS:
            self.assertIn(f"- {name}：", prompt)
        # 基础约束与环境信息都还在（说明块是追加，不是替换）
        self.assertIn("# 当前项目环境", prompt)
        self.assertIn("标准翻译流程", prompt)

    def test_section_survives_compaction(self) -> None:
        """压缩后 system prompt 会重建：字段说明必须跟着回来（不能被摘要吃掉）。"""
        prompt = _build_system_prompt(_state(), summary="早前干了很多事……")
        self.assertIn("# 缓存（transl_cache）字段说明", prompt)
        self.assertIn("# 会话摘要（早前对话已压缩）", prompt)
        self.assertIn("早前干了很多事……", prompt)


if __name__ == "__main__":
    unittest.main()
