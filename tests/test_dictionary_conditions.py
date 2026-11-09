import tempfile
import unittest
from pathlib import Path

from GalTransl.CSentense import CSentense
from GalTransl.Dictionary import CNormalDic


class ConditionDictionaryTests(unittest.TestCase):
    def load(self, text: str) -> CNormalDic:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dict.txt"
            path.write_text(text, encoding="utf-8")
            return CNormalDic([str(path)])

    def test_same_as_previous_on_first_condition_entry_does_not_replace(self):
        dic = self.load("pre_src\t~\t猫\t犬\n")
        sentence = CSentense("猫")
        self.assertEqual(dic.do_replace("猫", sentence), "猫")

    def test_same_as_previous_follows_preceding_condition(self):
        dic = self.load("pre_src\t猫\t猫\t犬\npre_src\t~\t鳥\t魚\n")
        sentence = CSentense("猫と鳥")
        self.assertEqual(dic.do_replace("猫と鳥", sentence), "犬と魚")


if __name__ == "__main__":
    unittest.main()
