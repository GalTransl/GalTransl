from typing import List
from os import path
from GalTransl.CSentense import CSentense, CTransList
from GalTransl import LOGGER
from GalTransl.Utils import process_escape


class ifWord:
    __slots__ = ["without_flag", "startswith_flag", "endswith_flag", "word"]

    def __init__(self, if_word):
        if if_word.startswith(">"):
            startswith_flag, if_word = True, if_word[1:]
        else:
            startswith_flag = False

        if if_word.endswith("<"):
            endswith_flag, if_word = True, if_word[:-1]
        else:
            endswith_flag = False

        if if_word.startswith("!"):
            without_flag, if_word = True, if_word[1:]
        else:
            without_flag = False

        self.without_flag = without_flag
        self.startswith_flag = startswith_flag
        self.endswith_flag = endswith_flag
        self.word = if_word


class CBasicDicElement:
    """字典基本字元素"""

    conditionaDic_key = ["pre_jp", "post_jp", "pre_zh", "post_zh"]  # 条件字典
    situationsDic_key = ["mono", "diag"]  # 场景字典
    __slots__ = [
        "search_word",  # 搜索词
        "replace_word",  # 替换词
        "startswith_flag",  # 是否为startwith情况
        "onetime_flag",  # 是否为onetime情况
        "special_key",  # 区分是否为特殊词典的关键字
        "is_situationsDic",  # 是否为情景字典
        "is_conditionaDic",  # 是否为条件字典
        "if_word_list",  # 条件字典中的条件词列表
        "spl_word",  # if_word_list的连接关键字
        "note",  # For GPT
        "dic_name",  # 字典名
    ]

    def __init__(
        self,
        search_word: str = "",
        replace_word: str = "",
        special_key: str = "",
        dic_name: str = "",
    ) -> None:
        self.search_word: str = search_word
        self.replace_word: str = replace_word

        self.startswith_flag: bool = False
        if search_word.startswith("^^"):  # startswith情况
            self.startswith_flag = True
            self.search_word = search_word[2:]
        if search_word.startswith("1^"):  # onetime情况
            self.onetime_flag = True
            self.search_word = search_word[2:]
        else:
            self.onetime_flag = False

        self.special_key: str = special_key  # 区分是否为特殊词典的关键字

        self.is_situationsDic: bool = False

        self.is_conditionaDic: bool = False
        self.if_word_list: List[ifWord] = None  # 条件字典中的条件词列表
        self.spl_word: str = ""  # if_word_list的连接关键字
        self.note: str = ""  # For GPT
        self.dic_name: str = dic_name  # 字典名

    def __repr__(self) -> str:
        return f"{self.search_word} -> {self.replace_word}"

    def load_line(self, line: str, type="Normal"):
        """
        :line: 一行
        """
        if line.startswith("\n"):
            return
        elif line.startswith("\\\\") or line.startswith("//"):  # 注释行跳过
            return
        sp = line.rstrip("\r\n").split("\t")  # 去多余换行符，Tab分割
        len_sp = len(sp)
        if len_sp < 2:  # 至少是2个元素
            return None

        is_conditionaDic_line = True if sp[0] in self.conditionaDic_key else False
        is_situationsDic_line = True if sp[0] in self.situationsDic_key else False
        if is_conditionaDic_line:
            self.is_conditionaDic = True
            if_word = sp[1]
            spl_word = "[and]" if "[and]" in if_word else "[or]"  # 判断连接字符
            # 初始化ifWord的list
            self.special_key = sp[0]
            self.if_word_list = [ifWord(w.strip()) for w in if_word.split(spl_word)]
            self.spl_word = spl_word
            self.search_word = sp[2]

        if self.search_word.startswith("^^"):  # startswith情况
            self.startswith_flag = True
            self.search_word = self.search_word[2:]


class CNormalDic:
    """
    :由多个BasicDic字典元素构成的大字典List（这个Dic不Normal但是懒得改名）
    :dic_list:字典文件的list，可以只有文件名，然后提供dir参数，也可以是完整的，混搭也可以
    :dic_base_dir:字典目录的path，会自动进行拼接
    """

    conditionaDic_key = ["pre_jp", "post_jp", "pre_zh", "post_zh"]  # 条件字典
    situationsDic_key = ["mono", "diag"]  # 场景字典

    def __init__(self, dic_list: list) -> None:
        self.dic_list: List[CBasicDicElement] = []
        for dic_path in dic_list:
            self.load_dic(dic_path)  # 加载字典

    def sort_dic(self):
        """
        按字典search_word的长度重排序
        """
        self.dic_list.sort(key=lambda x: len(x.search_word), reverse=True)

    def get_dst(self,word):
        for dic in self.dic_list:
            if dic.search_word == word:
                return dic.replace_word
        return ""

    def load_dic(self, dic_path: str):
        """加载一个字典txt到这个对象的内存"""
        if not path.exists(dic_path):
            LOGGER.warning(f"{dic_path}不存在，请检查路径。")
            return
        with open(dic_path, encoding="utf8") as f:
            dic_lines = f.readlines()
        if len(dic_lines) == 0:
            return

        normalDic_count = 0
        conditionaDic_count = 0
        situationsDic_count = 0
        dic_name = path.basename(dic_path)
        dic_name = path.splitext(dic_name)[0]

        for line in dic_lines:
            if line.startswith("\n"):
                continue
            # elif line.startswith("\\\\") or line.startswith("//"):  # 注释行跳过
            #     continue

            # 四个空格换成Tab
            line = line.replace("    ", "\t")

            sp = line.rstrip("\r\n").split("\t")  # 去多余换行符，Tab分割
            len_sp = len(sp)
            if len_sp < 2:  # 至少是2个元素
                continue
            # 处理转义字符
            for i in range(len_sp):
                sp[i] = process_escape(sp[i])

            is_conditionaDic_line = True if sp[0] in self.conditionaDic_key else False
            is_situationsDic_line = True if sp[0] in self.situationsDic_key else False
            if (is_conditionaDic_line and len_sp < 4) or (
                is_situationsDic_line and len_sp < 3
            ):
                continue

            if is_conditionaDic_line:
                if_word = sp[1]
                spl_word = "[and]" if "[and]" in if_word else "[or]"  # 判断连接字符
                # 初始化ifWord的list
                if_word_list = [ifWord(w.strip()) for w in if_word.split(spl_word)]
                con_dic = CBasicDicElement(sp[2], sp[3], sp[0], dic_name)
                con_dic.is_conditionaDic = True
                con_dic.if_word_list = if_word_list
                con_dic.spl_word = spl_word
                self.dic_list.append(con_dic)
                conditionaDic_count += 1
            elif is_situationsDic_line:
                sit_dic = CBasicDicElement(sp[1], sp[2], sp[0], dic_name)
                sit_dic.is_situationsDic = True
                self.dic_list.append(sit_dic)
                situationsDic_count += 1
            else:
                self.dic_list.append(CBasicDicElement(sp[0], sp[1], dic_name=dic_name))
                normalDic_count += 1
        LOGGER.info(
            "载入 普通字典："
            + path.basename(dic_path)
            + "  "
            + (str(normalDic_count) + "普通词条 " if normalDic_count != 0 else "")
            + (
                str(conditionaDic_count) + "条件词条 "
                if conditionaDic_count != 0
                else ""
            )
            + (
                str(situationsDic_count) + "场景词条 "
                if situationsDic_count != 0
                else ""
            )
        )

    def do_replace(
        self, input_text: str, input_tran: CSentense, full_match: bool = False
    ) -> str:
        """
        通过这个dic字典来优化一个句子。
        input_text：要被润色的句子
        input_translate：这个句子所在的Translate对象
        full_match：是否全匹配，默认False，开启后查找词完全等于input_text才替换
        """
        # 遍历每个BasicDicElement做替换
        for dic in self.dic_list:
            # 场景字典判断
            if dic.is_situationsDic:
                if ("diag" == dic.special_key and input_tran.is_dialogue == False) or (
                    "mono" == dic.special_key and input_tran.is_dialogue == True
                ):
                    continue
            # 条件字典屎山
            if dic.is_conditionaDic:
                can_replace = False  # True代表本轮满足替换条件
                # 取对应的查找关键字的句子
                match dic.special_key:
                    case "pre_jp":
                        find_ifword_text = input_tran.pre_jp
                    case "post_jp":
                        find_ifword_text = input_tran.post_jp
                    case "pre_zh":
                        find_ifword_text = input_tran.pre_zh
                    case "post_zh":
                        find_ifword_text = input_tran.post_zh
                    case _:
                        raise ValueError(f"不支持的条件字典关键字{dic.special_key}")
                # 遍历if_word_list
                for if_word in dic.if_word_list:
                    # 因为如果有stratwith的话需要修改word，所以要新建一份副本
                    if_word_now = if_word.word
                    if if_word_now == "":
                        continue
                    if if_word.startswith_flag:
                        if dic.special_key == "pre_jp":
                            # 把left_symbol先拼接回去
                            if_word_now = input_tran.left_symbol + if_word_now
                        elif dic.special_key == "post_jp":
                            # 需要给判断词加上对应的format
                            if input_tran.is_dialogue:
                                if_word_now = (
                                    input_tran.dia_format.split("#句子")[0]
                                    + if_word_now
                                )
                            else:
                                if_word_now = (
                                    input_tran.mono_format.split("#句子")[0]
                                    + if_word_now
                                )

                    if if_word_now in ["~", "(同上)", "（同上）"]:  # 同上flag判断
                        can_replace = True if last_one_success == True else False
                    elif if_word.startswith_flag:  # startswith
                        can_replace = find_ifword_text.startswith(if_word_now)
                    else:  # 默认为find
                        can_replace = if_word_now in find_ifword_text

                    if if_word.without_flag:  # 有without_flag时则取反
                        can_replace = not can_replace

                    # and中有一个是False就跳出
                    if dic.spl_word == "[and]" and can_replace == False:
                        break
                    elif (
                        dic.spl_word == "[or]" and can_replace == True
                    ):  # or中有一个是True就跳出
                        break

                if not can_replace:  # 条件不满足，跳到字典下一条
                    last_one_success = False
                    continue
                else:
                    last_one_success = True

            # 不管是不是特殊字典，替换部分都是一样的，跑到这里就是条件满足了：

            search_word = dic.search_word
            replace_word = dic.replace_word

            # startwith情况，只替换开头的
            if dic.startswith_flag:
                len_search_word = len(search_word)
                len_input_text = len(input_text)
                if len_search_word > len_input_text:
                    continue  # 肯定不满足
                elif input_text[:len_search_word] == search_word:
                    input_text = input_text.replace(search_word, replace_word, 1)
            elif dic.onetime_flag:  # onetime情况，只替换一次
                input_text = input_text.replace(search_word, replace_word, 1)
            else:  # 普通情况
                if not full_match:
                    input_text = input_text.replace(search_word, replace_word)
                elif search_word == input_text:
                    input_text = replace_word

        return input_text


class CGptDict:
    def __init__(self, dic_list: list) -> None:
        self._dic_list: List[CBasicDicElement] = []
        for dic_path in dic_list:
            self.load_dic(dic_path)  # 加载字典
    
    def get_dst(self,word):
        for dic in self._dic_list:
            if dic.search_word == word:
                return dic.replace_word
        return ""

    def sort_dic(self):
        """
        按字典search_word的长度重排序
        """
        self._dic_list.sort(key=lambda x: len(x.search_word), reverse=True)

    def load_dic(self, dic_path: str):
        """加载一个字典txt到这个对象的内存"""
        if not path.exists(dic_path):
            LOGGER.warning(f"{dic_path}不存在，请检查路径。")
            return
        with open(dic_path, encoding="utf8") as f:
            dic_lines = f.readlines()
        if len(dic_lines) == 0:
            return

        dic_name = path.basename(dic_path)
        dic_name = path.splitext(dic_name)[0]
        normalDic_count = 0

        for line in dic_lines:
            if line.startswith("\n"):
                continue

            # 兼容四个空格
            line = line.replace("    ", "\t")
            # 兼容src->dst #note
            if "->" in line:
                line = line.replace("->", "\t").replace("#", "\t")

            sp = line.rstrip("\r\n").split("\t")  # 去多余换行符，Tab分割
            len_sp = len(sp)

            if len_sp < 2:  # 至少是2个元素
                continue

            search_word = sp[0]
            replace_word = sp[1]
            if len_sp > 2 and sp[2]:
                note = sp[2]
            else:
                note = ""

            redundant_flag = False
            for d in self._dic_list:
                if d.search_word == search_word and d.replace_word == replace_word:
                    if d.note and d.note == note:
                        LOGGER.warning(f"重复的GPT字典词条 {search_word} -> {replace_word} 已忽略")
                        redundant_flag = True
                        break
            if redundant_flag:
                continue

            dic = CBasicDicElement(search_word, replace_word, dic_name=dic_name)
            dic.note = note
            self._dic_list.append(dic)
            normalDic_count += 1
        LOGGER.info(
            f"载入 GPT字典: {path.basename(dic_path)} {normalDic_count}普通词条"
        )

    def gen_prompt(self, trans_list: CTransList, type="gpt"):
        promt = ""
        input_text = "\n".join(
            [f"{tran.speaker}:{tran.post_jp}" for tran in trans_list]
        )
        if type == "gpt":
            for i, dic in enumerate(self._dic_list):
                prev_dic = self._dic_list[i - 1] if i > 0 else None
                if prev_dic and dic.search_word in prev_dic.search_word:
                    input_text = input_text.replace(prev_dic.search_word, "")
                if dic.startswith_flag or dic.search_word in input_text:
                    promt += f"| {dic.search_word} | {dic.replace_word} |"
                    if dic.note != "":
                        promt += f" {dic.note}"
                    promt += " |\n"

            if promt != "":
                promt = (
                    "# Glossary\n| Src | Dst(/Dst2/..) | Note |\n| --- | --- | --- |\n"
                    + promt
                )
        elif type == "sakura":
            for i, dic in enumerate(self._dic_list):
                prev_dic = self._dic_list[i - 1] if i > 0 else None
                if prev_dic and dic.search_word in prev_dic.search_word:
                    input_text = input_text.replace(prev_dic.search_word, "")
                if dic.startswith_flag or dic.search_word in input_text:
                    promt += f"{dic.search_word}->{dic.replace_word}"
                    if dic.note != "":
                        promt += f" #{dic.note}"
                    promt += "\n"
        elif type == "tsv":
            for i, dic in enumerate(self._dic_list):
                if dic.startswith_flag or dic.search_word in input_text:
                    promt += f"{dic.search_word}\t{dic.replace_word}"
                    input_text = input_text.replace(dic.search_word, "")
                    if dic.note!= "":
                        promt += f"\t{dic.note}"
                    promt += "\n"
            if promt!= "":
                promt = f"SRC\tDST\tNOTE\n{promt}"
        return promt

    def check_dic_use(self, find_from_str: str, tran: CSentense):
        problem_list = []
        for dic in self._dic_list:
            if dic.search_word not in tran.post_jp:
                continue

            replace_word_list = (
                dic.replace_word.split("/")
                if "/" in dic.replace_word
                else [dic.replace_word]
            )

            flag = False
            for replace_word in replace_word_list:
                if replace_word in find_from_str:
                    flag = True
                    break

            if not flag:
                problem_list.append(
                    f"{dic.dic_name} {dic.search_word} -> {dic.replace_word} 未使用"
                )

        return ", ".join(problem_list)
