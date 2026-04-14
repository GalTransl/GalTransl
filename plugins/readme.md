# 插件规范

## 插件介绍

插件系统是GalTransl v4的一项升级。插件可以分为两种类型：文本插件和文件插件。
- 文本插件可以对文本进行自定义的处理，例如清洁文本、修复文本、修复人名位置等。
- 文件插件可以使GalTransl支持更多格式的文件，例如字幕文件、文本文件等。

如果你愿意贡献插件，或者只是希望编写自己的插件，都可以继续阅读本文档获得更多提示。

## 1. 插件命名规则

插件的命名应该清晰明了，能够准确地反映出插件的功能。命名应遵循以下规则：

- 统一格式为文本插件：`Text_(引擎名/文件类型名)_(用途)`，文件插件`File_(引擎名/文件类型名)_(后缀/noext)`
- 插件的文件名应以`Text`或`File`开头。
- 引擎名/文件类型名，例如`krkr（引擎名）`，`subtitle（字幕类文件）`等。通用文本插件可使用`common`。
- 最后是用途或后缀，例如文本插件：`clean（清洁文本）`、`fix（修复文本）`，文件插件：`ast`、`srt`、`noext`等。
- 最终名称的样例：`Text_krkr_rubyClean`、`Text_common_linebreakFix`、`File_subtitle_srt`

## 2. 插件结构

插件应至少包含`插件名.yaml`、`插件名.py`，并位于插件名目录下。目录树示例：

```
plugins
├── Text_krkr_rubyClean
│   ├── Text_krkr_rubyClean.yaml
│   └── Text_krkr_rubyClean.py
├── File_subtitle_srt
│   ├── File_subtitle_srt.yaml
│   └── File_subtitle_srt.py
```

其中.yaml配置文件的样例如下：

```yaml
Core:
  Name: 样例文本插件
  Type: text
  Module: text_example_nouse

Documentation:
  Author: cx2333
  Version: 1.0
  Description: 这是个样例文本插件，任何项目都不应该使用本插件

Settings: # 这里存放插件的设置
  set_int: 123
  set_string: aaa
  set_bool: True
  设置样例4: False
```
   
其中Core和Documentation必填，可以没有Settings。

- Core：插件的基本信息，包括插件名、类型（text/file）、模块名（与文件同名即可）。
- Documentation：插件的作者、版本、描述。
- Settings：插件的设置，可以根据需要自定义。

## 3. 插件编写指引

插件应继承自`GTextPlugin`类、`GFilePlugin`类，新的类名没有要求。

以下方法用于初始化，`GTextPlugin`类、`GFilePlugin`类都有：

- `gtp_init(self, plugin_conf: dict, project_conf: dict)`: 在插件加载时被调用。 plugin_conf为插件yaml的设置。 project_conf为项目yaml中common下的设置。
- `gtp_final(self)`: 在所有文件翻译完成之后的动作，例如输出提示信息。

对于处理文本的GTextPlugin插件，还需要实现以下方法，可以按需实现：

- `before_src_processed(self, tran: CSentense) -> CSentense`: 在源句子处理之前被调用。返回修改后的`CSentense`。
- `after_src_processed(self, tran: CSentense) -> CSentense`: 在源句子处理之后被调用。返回修改后的`CSentense`。
- `before_dst_processed(self, tran: CSentense) -> CSentense`: 在目标句子处理之前被调用。返回修改后的`CSentense`。
- `after_dst_processed(self, tran: CSentense) -> CSentense`: 在目标句子处理之后被调用。返回修改后的`CSentense`。

* 这里的"处理"，是指GalTransl一定会对日文做去掉日文对话框、字典替换，并在译后恢复对话框、译后字典替换。   
* 所以当引擎的文本在日文对话框左右有多余的字符，可以在`before_src_processed`中进行隐藏处理（挪到tran的left_symbol和right_symbol属性中），恢复对话框时会自动还原。   
* 当引擎的文本在日文对话框左边写了人名，可以在`before_src_processed`中进行处理，把人名挪到tran的speaker属性里，再在`after_dst_processed`中把人名还原到对话框左边（先让程序恢复对话框，所以是after）。   

对于处理文件的GFilePlugin插件，**必须**实现以下方法：
- `load_file(self, file_path: str) -> list`: 加载文件，返回一个包含message和name(可空)的dict list。
- `save_file(self, file_path: str, transl_json: list)`: 保存文件，transl_json为load_file提供的json在翻译message和name后的结果。

* 在load_file中，可以通过以下方式把与message无关的内容带到结果里
1. 直接在每个dict里插其他的信息，save_file里会原样将其他内容送回，然后还原成原文件并保存文件（例如file_subtitle_srt插件）
## 3. 注意事项

- 所有的插件方法都应该有适当的错误处理。
- 插件应避免使用全局变量，以防止插件之间的相互影响。
- 插件应尽可能地减少对外部资源的依赖，只使用python官方库和GalTransl提供的接口。
- Complex is better than complicated.如果你的插件可以合并到某个插件中，优先合并，并在Author里加上你的名字。

以上就是插件的基本规范，希望对你有所帮助。
