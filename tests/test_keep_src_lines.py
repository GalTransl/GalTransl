"""Resource paths in key-value JSON must come out byte-for-byte unchanged (issue #221).

MTool replaces strings at runtime by exact original text, so a translated
`res://.../昼の台詞/p00.wav` or a post-dictionary `ー→—` on a path breaks the game's
assets. File plugins mark such lines with `keep_src`; the core then skips the model,
dictionaries, text plugins and problem checks for them.
"""
import asyncio
import tempfile
import unittest
from pathlib import Path

import orjson
import yaml

from GalTransl.Cache import get_transCache_from_json, save_transCache_to_json
from GalTransl.ConfigHelper import CProjectConfig
from GalTransl.CSerialize import update_json_with_transList
from GalTransl.Dictionary import CNormalDic
from GalTransl.Frontend.LLMTranslate import postprocess_trans_list, preprocess_trans_list
from GalTransl.Loader import load_transList
from GalTransl.Problem import find_problems
from GalTransl.server import _cache_entries_to_trans_list
from plugins.file_i18n_json.file_i18n_json import file_plugin, is_resource_reference

VOICE = "res://asset/audio/voice/イリス/昼の台詞/p00.wav"
BGM = "res://asset/audio/bgm/フリーBGM/鬱蒼.mp3"
TEXT = "フリーの時間・ループする日々"


class ResourceReferenceTests(unittest.TestCase):
    def test_recognises_paths_urls_and_asset_files(self):
        for text in (VOICE, BGM, "https://example.com/a?b=1",
                     "ancient_stone_hall/ancient_stone_hall_floor_a_76x43.png",
                     "data\\イベント\\001.json", "p00.wav", "ループ.ogg", "立ち絵.PNG"):
            self.assertTrue(is_resource_reference(text), text)

    def test_keeps_ordinary_text_translatable(self):
        for text in ("はい/いいえ", "1/2.5", "Mr.Bin", "Ver.1.0", "Save", "autoplayBgm",
                     "フリーBGMを再生.wav を開く", "res: 防御力", TEXT, ""):
            self.assertFalse(is_resource_reference(text), text)


class KeepSourcePipelineTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "post.txt").write_text("ー\t—\n・\t·\n", encoding="utf-8")
        (self.root / "config.yaml").write_text(yaml.safe_dump({
            "common": {"language": "zh-cn"},
            "plugin": {"filePlugin": "file_i18n_json"},
            "dictionary": {"preDict": [], "postDict": [], "gpt.dict": []},
            "problemAnalyze": {"problemList": ["残留日文"]},
        }, allow_unicode=True), encoding="utf-8")
        self.config = CProjectConfig(str(self.root), "config.yaml")
        self.post_dic = CNormalDic([str(self.root / "post.txt")])

    def test_marked_lines_skip_model_dictionary_and_problems_and_save_unchanged(self):
        source = self.root / "ManualTransFile.json"
        source.write_bytes(orjson.dumps({VOICE: VOICE, TEXT: TEXT, BGM: BGM}))
        plugin = file_plugin()
        items = plugin.load_file(str(source))
        self.assertEqual([item.get("keep_src", False) for item in items], [True, False, True])
        trans_list, json_list = load_transList(items)

        # An older run translated the paths; that stale cache must not come back.
        stale = self.root / "transl_cache" / "ManualTransFile.json"
        stale.parent.mkdir()
        old = [dict(index=t.index, name="", pre_src=t.pre_src, post_src=t.pre_src,
                    pre_dst=t.pre_src.replace("台詞", "台词")) for t in trans_list]
        stale.write_bytes(orjson.dumps(old))

        preprocess_trans_list(trans_list, self.config, CNormalDic([]))
        hit, miss = asyncio.run(get_transCache_from_json(trans_list, str(stale)))
        self.assertEqual([t.pre_src for t in miss], [])
        self.assertEqual(trans_list[1].pre_dst, TEXT)  # 普通行仍然走缓存
        postprocess_trans_list(trans_list, self.config, self.post_dic)
        find_problems(trans_list, self.config)

        self.assertEqual(trans_list[0].post_dst, VOICE)
        self.assertEqual(trans_list[2].post_dst, BGM)
        self.assertEqual(trans_list[1].post_dst, "フリ—の時間·ル—プする日々")
        self.assertEqual([t.problem for t in (trans_list[0], trans_list[2])], ["", ""])

        out = self.root / "out.json"
        plugin.save_file(str(out), update_json_with_transList(trans_list, json_list))
        result = orjson.loads(out.read_bytes())
        self.assertEqual(result[VOICE], VOICE)
        self.assertEqual(result[BGM], BGM)

    def test_flag_survives_cache_round_trip_for_problem_rebuild(self):
        trans_list, _ = load_transList([{"message": VOICE, "keep_src": True}, {"message": TEXT}])
        preprocess_trans_list(trans_list, self.config, CNormalDic([]))
        trans_list[1].pre_dst = trans_list[1].post_dst = "自由时间"
        cache = self.root / "cache.json"
        asyncio.run(save_transCache_to_json(trans_list, str(cache), post_save=True))
        entries = orjson.loads(cache.read_bytes())
        self.assertTrue(entries[0]["keep_src"])
        self.assertNotIn("keep_src", entries[1])

        rebuilt = _cache_entries_to_trans_list(entries, "cache.json", {})
        find_problems(rebuilt, self.config)
        self.assertTrue(rebuilt[0].keep_src)
        self.assertEqual(rebuilt[0].problem, "")  # 否则会报「原文输出」


if __name__ == "__main__":
    unittest.main()
