"""快照写入的临时文件：原子替换、启动清扫、不在列表里露面。

缓存整文件重写是先写 `<缓存>.json.tmp` 再替换（见 Cache._replace_cache_file 的两个调用点）。
这里钉住三件事：

1. 替换走 os.replace，不再用 shutil.move —— Windows 上 os.rename 覆盖已存在文件必抛
   FileExistsError，shutil.move 于是退化成 copy2：等于把正式缓存**原地截断重写**（写一半
   崩了就是半截 JSON），失败时还留下 .tmp；
2. 启动前的清扫只删 `*.json.tmp`，缓存本身与 .append.jsonl 一概不碰；
3. /files 与 /cache 的列表不再把 .json.tmp 当缓存文件露给界面与 Agent。

（这个文件要用 .venv 的 python 跑：GalTransl.server 依赖 yaml。）
"""

import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from GalTransl import Cache as cache_mod
from GalTransl.Cache import (
    CACHE_TEMP_SUFFIX,
    _compact_cache_from_append,
    cleanup_stale_cache_temp_files,
    save_transCache_to_json,
)
from GalTransl.CSentense import CSentense


def _tran(pre_src: str, pre_dst: str, index: int = 1) -> CSentense:
    tran = CSentense(pre_src=pre_src, index=index)
    tran.pre_dst = pre_dst
    return tran


class SnapshotReplaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="cache-tmp-")
        self.cache_path = os.path.join(self.dir, "sc_0_pr00.txt.json")

    def test_snapshot_replaces_with_os_replace(self) -> None:
        with open(self.cache_path, "w", encoding="utf8") as f:
            f.write("[]")  # 已有旧快照：替换必须覆盖它

        replaced: list[tuple[str, str]] = []
        real_replace = os.replace

        def spy(src, dst):
            replaced.append((src, dst))
            return real_replace(src, dst)

        with patch.object(cache_mod.os, "replace", spy), patch(
            "shutil.move", side_effect=AssertionError("快照不该再用 shutil.move")
        ):
            asyncio.run(
                save_transCache_to_json([_tran("原文", "译文")], self.cache_path, post_save=True)
            )

        # 走的是"临时文件 → 正式文件"的替换，而不是原地重写
        self.assertEqual(len(replaced), 1)
        self.assertEqual(replaced[0][1], self.cache_path)
        self.assertTrue(replaced[0][0].endswith(CACHE_TEMP_SUFFIX))
        with open(self.cache_path, "rb") as f:
            entries = json.loads(f.read())
        self.assertEqual(entries[0]["pre_dst"], "译文")
        self.assertFalse(os.path.exists(self.cache_path + ".tmp"))  # 中间文件不残留

    def test_compaction_replaces_with_os_replace(self) -> None:
        """另一条整文件重写的路径（停止翻译后合并 .append.jsonl）同样原子替换。"""
        append_path = self.cache_path + ".append.jsonl"
        with open(self.cache_path, "w", encoding="utf8") as f:
            f.write("[]")
        line = {
            "__cache_key": "NoneNoneNone",
            "name": "",
            "pre_src": "原文",
            "post_src": "原文",
            "pre_dst": "译文",
        }
        with open(append_path, "w", encoding="utf8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

        asyncio.run(_compact_cache_from_append(self.cache_path, append_path))

        with open(self.cache_path, "rb") as f:
            entries = json.loads(f.read())
        self.assertEqual(entries[0]["pre_dst"], "译文")
        self.assertFalse(os.path.exists(self.cache_path + ".tmp"))
        self.assertFalse(os.path.exists(append_path))  # 合并完把增量日志清掉


class CleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="cache-clean-")

    def _touch(self, name: str) -> str:
        path = os.path.join(self.dir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf8") as f:
            f.write("[]")
        return path

    def test_only_cache_temp_files_are_removed(self) -> None:
        keep = [
            self._touch("a.json"),
            self._touch("a.json.append.jsonl"),
            self._touch("note.tmp"),  # 不是 *.json.tmp：不认（别的工具留下的照旧留着）
        ]
        drop = [self._touch("a.json.tmp"), self._touch("b.json_0.json.tmp")]
        os.makedirs(os.path.join(self.dir, "sub.json.tmp"))  # 同名目录：不是文件，跳过

        removed = cleanup_stale_cache_temp_files(self.dir)

        self.assertEqual(removed, len(drop))
        for path in drop:
            self.assertFalse(os.path.exists(path), path)
        for path in keep:
            self.assertTrue(os.path.exists(path), path)
        self.assertTrue(os.path.isdir(os.path.join(self.dir, "sub.json.tmp")))

    def test_missing_dir_is_not_an_error(self) -> None:
        self.assertEqual(cleanup_stale_cache_temp_files(os.path.join(self.dir, "nope")), 0)
        self.assertEqual(cleanup_stale_cache_temp_files(""), 0)


class ListingFilterTests(unittest.TestCase):
    """缓存列表不露中间文件：界面与 Agent 拿到的都是有效缓存。"""

    def test_cache_listing_hides_temp_files(self) -> None:
        from GalTransl.server import _list_dir_entries

        with tempfile.TemporaryDirectory() as directory:
            for name in ("a.json", "a.json.tmp", "a.json.append.jsonl"):
                with open(os.path.join(directory, name), "w", encoding="utf8") as f:
                    f.write("[]")

            listed = {
                entry["name"]
                for entry in _list_dir_entries(directory, skip_suffixes=(CACHE_TEMP_SUFFIX,))
            }
            self.assertEqual(listed, {"a.json", "a.json.append.jsonl"})
            # 过滤是显式传的：不带它照旧全列（输入/输出目录不受影响）
            unfiltered = {entry["name"] for entry in _list_dir_entries(directory)}
            self.assertIn("a.json.tmp", unfiltered)


if __name__ == "__main__":
    unittest.main()
