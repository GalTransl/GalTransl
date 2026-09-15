"""项目翻译规范（ProjectGuideline）的单元测试。

覆盖三种写入模式、与全局规范的拼接、以及各种参数错误——Agent 的
write_project_guideline 工具与前端编辑页都走这一份实现，口径只有一处。
"""

import os
import tempfile
import unittest

from GalTransl.ProjectGuideline import (
    MAX_PROJECT_GUIDELINE_CHARS,
    PROJECT_GUIDELINE_FILENAME,
    apply_project_guideline_edit,
    combine_guidelines,
    project_guideline_path,
    read_project_guideline,
)


class ProjectGuidelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project = tempfile.mkdtemp(prefix="guideline-proj-")

    # ---- 读 ----

    def test_read_missing_or_empty_is_empty(self) -> None:
        self.assertEqual(read_project_guideline(self.project), "")
        self.assertEqual(read_project_guideline(""), "")
        # 只有空白也当作没有（不然翻译时会出现一个空标题段）
        with open(project_guideline_path(self.project), "w", encoding="utf-8") as f:
            f.write("   \n\n")
        self.assertEqual(read_project_guideline(self.project), "")

    def test_file_lives_in_project_dir(self) -> None:
        self.assertEqual(
            project_guideline_path(self.project),
            os.path.join(os.path.abspath(self.project), PROJECT_GUIDELINE_FILENAME),
        )

    # ---- 写：三种模式 ----

    def test_overwrite_creates_then_replaces_whole_file(self) -> None:
        res = apply_project_guideline_edit(self.project, mode="overwrite", content="第一条")
        self.assertTrue(res["created"])
        self.assertTrue(os.path.isfile(project_guideline_path(self.project)))
        self.assertEqual(read_project_guideline(self.project), "第一条")

        res = apply_project_guideline_edit(self.project, mode="overwrite", content="整份换掉")
        self.assertFalse(res["created"])
        self.assertTrue(res["existed"])
        self.assertEqual(read_project_guideline(self.project), "整份换掉")

    def test_append_keeps_existing_and_creates_when_missing(self) -> None:
        res = apply_project_guideline_edit(self.project, mode="append", content="只有这条")
        self.assertTrue(res["created"])
        self.assertEqual(read_project_guideline(self.project).strip(), "只有这条")

        apply_project_guideline_edit(self.project, mode="append", content="再加一条")
        text = read_project_guideline(self.project)
        self.assertIn("只有这条", text)
        self.assertIn("再加一条", text)
        self.assertLess(text.index("只有这条"), text.index("再加一条"))

    def test_replace_touches_only_that_span(self) -> None:
        apply_project_guideline_edit(
            self.project,
            mode="overwrite",
            content="## 称呼\n- お兄ちゃん→哥哥\n## 语气\n- 书面语",
        )
        apply_project_guideline_edit(
            self.project, mode="replace", old_text="お兄ちゃん→哥哥", new_text="お兄ちゃん→兄长"
        )
        text = read_project_guideline(self.project)
        self.assertIn("兄长", text)
        self.assertNotIn("哥哥", text)
        self.assertIn("书面语", text)

    def test_replace_can_delete_a_span(self) -> None:
        apply_project_guideline_edit(self.project, mode="overwrite", content="保留\n删掉这行\n保留2")
        apply_project_guideline_edit(self.project, mode="replace", old_text="删掉这行\n", new_text="")
        self.assertEqual(read_project_guideline(self.project), "保留\n保留2")

    # ---- 写：参数错误一律报错，不替调用方猜 ----

    def test_replace_miss_or_ambiguous_raises(self) -> None:
        apply_project_guideline_edit(self.project, mode="overwrite", content="哥哥")
        with self.assertRaises(ValueError) as ctx:
            apply_project_guideline_edit(self.project, mode="replace", old_text="没有这段", new_text="x")
        self.assertIn("没有找到", str(ctx.exception))
        # 改动后仍是原样
        self.assertEqual(read_project_guideline(self.project), "哥哥")

        apply_project_guideline_edit(self.project, mode="overwrite", content="哥哥 … 哥哥")
        with self.assertRaises(ValueError) as ctx:
            apply_project_guideline_edit(self.project, mode="replace", old_text="哥哥", new_text="兄长")
        self.assertIn("2 次", str(ctx.exception))
        self.assertEqual(read_project_guideline(self.project), "哥哥 … 哥哥")

    def test_replace_without_file_raises(self) -> None:
        with self.assertRaises(ValueError):
            apply_project_guideline_edit(self.project, mode="replace", old_text="a", new_text="b")
        self.assertFalse(os.path.isfile(project_guideline_path(self.project)))

    def test_bad_mode_and_empty_append_and_size_guard(self) -> None:
        with self.assertRaises(ValueError):
            apply_project_guideline_edit(self.project, mode="prepend", content="x")
        with self.assertRaises(ValueError):
            apply_project_guideline_edit(self.project, mode="append", content="   ")
        with self.assertRaises(ValueError):
            apply_project_guideline_edit(
                self.project, mode="overwrite", content="x" * (MAX_PROJECT_GUIDELINE_CHARS + 1)
            )
        # 不合法时不该留下文件
        self.assertFalse(os.path.isfile(project_guideline_path(self.project)))

    # ---- 拼接 ----

    def test_combine_guidelines(self) -> None:
        self.assertEqual(combine_guidelines("全局规则", ""), "全局规则")
        self.assertEqual(combine_guidelines("全局规则", "   \n"), "全局规则")

        only_project = combine_guidelines("", "项目规则")
        self.assertIn("项目规则", only_project)
        self.assertNotIn("全局规则", only_project)

        both = combine_guidelines("全局规则", "项目规则")
        self.assertIn("全局规则", both)
        self.assertIn("项目规则", both)
        # 项目规范在后（冲突时以它为准）
        self.assertLess(both.index("全局规则"), both.index("项目规则"))


if __name__ == "__main__":
    unittest.main()
