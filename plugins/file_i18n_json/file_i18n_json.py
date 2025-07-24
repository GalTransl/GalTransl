import orjson, re
from collections import OrderedDict
from GalTransl import LOGGER
from GalTransl.GTPlugin import GFilePlugin


class file_plugin(GFilePlugin):
    def gtp_init(self, plugin_conf: dict, project_conf: dict):
        """
        This method is called when the plugin is loaded.在插件加载时被调用。
        :param plugin_conf: The settings for the plugin.插件yaml中所有设置的dict。
        :param project_conf: The settings for the project.项目yaml中common下设置的dict。
        """
        pass

    def load_file(self, file_path: str) -> list:
        """
        This method is called to load a file.
        加载文件时被调用。
        :param file_path: The path of the file to load.文件路径。
        :return: A list of objects with message and name(optional).返回一个包含message和name(可空)的对象列表。
        """
        # 检查不支持的文件类型并抛出TypeError
        if not file_path.endswith(".json"):
            raise TypeError("File type not supported.")
        with open(file_path, "r", encoding="utf-8") as f:
            raw_dict = orjson.loads(f.read())
        # 检查文件内容是否为字典并抛出TypeError
        if type(raw_dict) is not dict:
            raise TypeError("File content is not a dictionary.")

        flat_dict = flatten(raw_dict)
        result_list = []
        for key, value in flat_dict.items():
            if type(value) is str:
                result_list.append({"key": key,"value":value, "message": value})
            else:
                result_list.append({"key": key,"value":value, "message": ""})

        return result_list

    def save_file(self, file_path: str, transl_json: list):
        """
        This method is called to save a file.
        保存文件时被调用。
        :param file_path: The path of the file to save.保存文件路径
        :param transl_json: A list of objects same as the return of load_file().load_file提供的json在翻译message和name后的结果。
        :return: None.
        """
        i = 0
        result_dict= {}
        for item in transl_json:
            if type(item["value"]) is str:
                result_dict[item["key"]] = item["message"]
            else:
                result_dict[item["key"]] = item["value"]
        result_dict = unflatten(result_dict)
        with open(file_path, "wb") as f:
            f.write(orjson.dumps(result_dict, option=orjson.OPT_INDENT_2))

    def gtp_final(self):
        """
        This method is called after all translations are done.
        在所有文件翻译完成之后的动作，例如输出提示信息。
        """
        pass


def _object_to_rows(obj, prefix=None):
    rows = []
    dot_prefix = prefix and (prefix + "🅖") or ""
    if isinstance(obj, dict):
        if not obj:
            rows.append(((prefix or "") + "🅣empty", "{}"))
        else:
            for key, item in obj.items():
                rows.extend(_object_to_rows(item, prefix=dot_prefix + key))
    elif isinstance(obj, (list, tuple)):
        if len(obj) == 0:
            rows.append(((prefix or "") + "🅣emptylist", "[]"))
        for i, item in enumerate(obj):
            rows.extend(_object_to_rows(item, prefix=dot_prefix + "[{}]".format(i)))
    elif obj is None:
        rows.append(((prefix or "") + "🅣none", "None"))
    elif isinstance(obj, bool):
        rows.append(((prefix or "") + "🅣bool", str(obj)))
    elif isinstance(obj, int):
        rows.append(((prefix or "") + "🅣int", str(obj)))
    elif isinstance(obj, float):
        rows.append(((prefix or "") + "🅣float", str(obj)))
    else:
        rows.append((prefix, str(obj)))
    return rows


def flatten(obj):
    if not isinstance(obj, dict):
        raise TypeError("Expected dict, got {}".format(type(obj)))
    return OrderedDict(_object_to_rows(obj))


_types_re = re.compile(r".*🅣(none|bool|int|float|empty|emptylist)$")
_int_key_re = re.compile(r"\[(\d+)\]")


def unflatten(data):
    obj = {}
    for key, value in data.items():
        current = obj
        bits = key.split("🅖")
        path, lastkey = bits[:-1], bits[-1]
        for bit in path:
            current[bit] = current.get(bit) or {}
            current = current[bit]
        # Now deal with $type suffixes:
        if _types_re.match(lastkey):
            lastkey, lasttype = lastkey.rsplit("🅣", 2)
            value = {
                "int": int,
                "float": float,
                "empty": lambda v: {},
                "emptylist": lambda v: [],
                "bool": lambda v: v.lower() == "true",
                "none": lambda v: None,
            }.get(lasttype, lambda v: v)(value)
        current[lastkey] = value

    # We handle foo.[0].one, foo.[1].two syntax in a second pass,
    # by iterating through our structure looking for dictionaries
    # where all of the keys are stringified integers
    def replace_integer_keyed_dicts_with_lists(obj):
        if isinstance(obj, dict):
            if obj and all(_int_key_re.match(k) for k in obj):
                return [
                    i[1]
                    for i in sorted(
                        [
                            (
                                int(_int_key_re.match(k).group(1)),
                                replace_integer_keyed_dicts_with_lists(v),
                            )
                            for k, v in obj.items()
                        ]
                    )
                ]
            else:
                return dict(
                    (k, replace_integer_keyed_dicts_with_lists(v))
                    for k, v in obj.items()
                )
        elif isinstance(obj, list):
            return [replace_integer_keyed_dicts_with_lists(v) for v in obj]
        else:
            return obj

    obj = replace_integer_keyed_dicts_with_lists(obj)
    # Handle root units only, e.g. {'$empty': '{}'}
    if list(obj.keys()) == [""]:
        return list(obj.values())[0]
    return obj
