"""Agent 的 write_project_guideline 工具：写完要能直接看到行级 diff。

前端「变更」卡靠结果里的 line_diff 渲染（与 save_dict 同一套结构），所以这里锁住它的
形状：add/del 行、增删计数、以及"内容没变就不产生变更行"（否则卡片会为一次空写弹出来）。
"""

import unittest
from unittest.mock import patch

from GalTransl.Agent.runtime import AgentRunner, AgentState, _tool_write_project_guideline


class WriteProjectGuidelineDiffTests(unittest.TestCase):
    """diff 来自"写前读一次、写后读一次"，不在本地重算编辑结果。"""

    def _run(self, before: str, after: str, args: dict) -> tuple[dict, list[dict]]:
        state = AgentState()
        state.project_dir = r"C:\proj"
        runner = AgentRunner(state)
        writes: list[dict] = []
        texts = [before, after]

        def fake_get(path: str) -> dict:
            # 第一次是写前、第二次是写后：按调用顺序取
            return {
                "filename": "translation_guideline.md",
                "exists": True,
                "content": texts.pop(0),
            }

        def fake_put(path: str, body: dict) -> dict:
            writes.append(body)
            return {"success": True, "filename": "translation_guideline.md", "mode": body["mode"]}

        with patch.object(
            AgentRunner, "_http_get", lambda self, path: fake_get(path)
        ), patch.object(AgentRunner, "_http_put", lambda self, path, body: fake_put(path, body)):
            return _tool_write_project_guideline(runner, args), writes

    def test_append_reports_added_lines_only(self) -> None:
        out, writes = self._run(
            "## 称呼\n- お兄ちゃん→哥哥",
            "## 称呼\n- お兄ちゃん→哥哥\n\n## 语气\n- 书面语",
            {"mode": "append", "content": "## 语气\n- 书面语"},
        )

        rows = out["line_diff"]["rows"]
        self.assertEqual([r["op"] for r in rows], ["add", "add", "add"])
        self.assertEqual(out["lines_added"], 3)
        self.assertEqual(out["lines_removed"], 0)
        self.assertTrue(out["changed"])
        self.assertEqual(writes[0]["mode"], "append")
        # 后端那份结果原样保留（前端与模型都还要看 success / filename / length）
        self.assertTrue(out["success"])

    def test_replace_reports_one_del_and_one_add(self) -> None:
        out, _ = self._run(
            "## 称呼\n- お兄ちゃん→哥哥\n## 语气\n- 书面语",
            "## 称呼\n- お兄ちゃん→兄长\n## 语气\n- 书面语",
            {"mode": "replace", "old_text": "→哥哥", "new_text": "→兄长"},
        )

        self.assertEqual([r["op"] for r in out["line_diff"]["rows"]], ["del", "add"])
        self.assertEqual((out["lines_added"], out["lines_removed"]), (1, 1))
        self.assertTrue(out["changed"])

    def test_unchanged_content_produces_no_rows(self) -> None:
        out, _ = self._run("同样的内容", "同样的内容", {"mode": "append", "content": "同样的内容"})

        self.assertEqual(out["line_diff"]["rows"], [])
        self.assertFalse(out["changed"])
        self.assertEqual((out["lines_added"], out["lines_removed"]), (0, 0))


if __name__ == "__main__":
    unittest.main()
