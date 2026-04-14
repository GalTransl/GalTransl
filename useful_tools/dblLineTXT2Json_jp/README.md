# dblLineTXT2Json_jp
通用双行文本与json_jp互转脚本

下载：https://github.com/XD2333/dblLineTXT2Json_jp

* 双行文本：是一种翻译常用文本格式

* json_jp：是GalTransl接受的输入格式

双行文本例：

```txt
◇00000A7C◇清史郎
◆00000A7C◆清史郎

◇00000A84◇「ついっったぁ！！」<
◆00000A84◆「ついっったぁ！！」<

◇00000B40◇電車に揺られ数時間……。
◆00000B40◆電車に揺られ数時間……。
```

使用例：

1. 修改settings.py，根据文本例参悟配置方法：

```python
原文标签头部特征 = "◇"
原文标签尾部特征 = "◇"

译文标签头部特征 = "◆"
译文标签尾部特征 = "◆"

人名长度阈值 = 12
人名黑名单字符 = "。『」…！、"

file_encoding = "utf-8"
```

2. 运行01_dump_nametable.py，获得nametable.txt

    nametable.txt格式为<名字>[TAB]<出现次数>   
    **凭感觉删掉不是人名的行**

3. 运行02_txt_jp2json_jp.py提取json_jp

4. 运行03_json_cn2txt_cn.py将json_cn导入，得到txt_cn