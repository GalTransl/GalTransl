from GalTransl import LOGGER
from GalTransl.CSentense import *
from os import path
from json import loads
from typing import Union, Tuple, List

def load_transList(json_path_or_list: Union[str, list]) -> Tuple[CTransList, list]:
    """
    从json文件路径、json字符串、json list中载入待翻译列表
    json格式为[{"name":xx/"names":[],"message/pre_jp":"xx"},...]
    """
    trans_list: CTransList = []

    if isinstance(json_path_or_list, str):
        if path.exists(json_path_or_list):
            # 如果是文件路径
            with open(json_path_or_list, "r", encoding="utf-8") as f:
                try:
                    json_list = loads(f.read())
                except Exception as e:
                    raise ValueError(f"无法解析JSON文件 {json_path_or_list}: {str(e)}")
        else:
            # 如果是JSON字符串
            try:
                json_list = loads(json_path_or_list)
            except Exception as e:
                raise ValueError(f"无法解析JSON字符串: {str(e)}")
    elif isinstance(json_path_or_list, list):
        json_list = json_path_or_list
    else:
        raise TypeError("输入必须是字符串（文件路径或JSON字符串）或列表")

    if not isinstance(json_list, list):
        raise ValueError("解析后的JSON不是列表格式")

    for i, item in enumerate(json_list):
        if not isinstance(item, dict):
            raise ValueError(f"JSON列表中的第{i+1}项不是字典格式")
        
        if "message" not in item:
            raise ValueError(f"JSON格式不正确，第{i+1}个item缺少message字段：{item}")

        name = item.get("name", item.get("names", ""))
        pre_jp = item["message"]
        index = item.get("index", i + 1)
        tmp_tran = CSentense(pre_jp, name, index)
        
        # 链接上下文
        if trans_list:
            tmp_tran.prev_tran = trans_list[-1]
            trans_list[-1].next_tran = tmp_tran
        trans_list.append(tmp_tran)

    return trans_list, json_list
