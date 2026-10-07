import unittest
from GalTransl.CSentense import CSentense
from plugins.problem_common.problem_common import _check_problems
from test_problem_control_symbols import FakeProblemConfig


class Config(FakeProblemConfig):
    def getProblemAnalyzeConfig(self, key):
        return ['残留日文'] if key == 'problemList' else []


class OriginalOutputTests(unittest.TestCase):
    def check_text(self, src, dst):
        row = CSentense(src, index=1)
        row.pre_dst = row.post_dst = dst
        return _check_problems(row, Config())

    def test_identical_japanese_is_original_output_only(self):
        self.assertEqual(self.check_text('こんにちは', 'こんにちは'), ['原文输出'])

    def test_partial_translation_remains_residual_japanese(self):
        problems = self.check_text('こんにちは世界', 'こんにちは世界你好')
        self.assertTrue(any(p.startswith('残留日文：') for p in problems))
        self.assertNotIn('原文输出', problems)

    def test_identical_punctuation_or_chinese_is_not_flagged(self):
        for text in ('……', '你好', ''):
            self.assertEqual(self.check_text(text, text), [])

    def test_compares_final_text_with_original_including_dialogue_symbols(self):
        row = CSentense('「こんにちは」', index=1)
        row.post_src = row.pre_dst = 'こんにちは'
        row.post_dst = '「こんにちは」'
        self.assertEqual(_check_problems(row, Config()), ['原文输出'])
        row.post_dst = '「你好こんにちは」'
        self.assertTrue(_check_problems(row, Config())[0].startswith('残留日文：'))
