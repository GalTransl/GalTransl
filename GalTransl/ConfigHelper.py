"""
读取 / 处理配置
"""

from GalTransl import (
    LOGGER,
    CONFIG_FILENAME,
    INPUT_FOLDERNAME,
    OUTPUT_FOLDERNAME,
    CACHE_FOLDERNAME,
)
from GalTransl.Dictionary import CGptDict, CNormalDic
from asyncio import gather
from tenacity import retry, stop_after_attempt, wait_fixed
from httpx import AsyncClient, TimeoutException
from time import time
from typing import Optional
from random import choice
from yaml import safe_load
from os import path, sep
from enum import Enum
from importlib.metadata import version


class CProxy:
    def __init__(
        self,
        address: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ) -> None:
        self.addr = address
        self.username = username
        self.pw = password
        pass


class CProblemType(Enum):
    """
    问题类型
    """

    词频过高 = 1
    标点错漏 = 2
    本无括号 = 2
    本无引号 = 2
    残留日文 = 3
    丢失换行 = 4
    多加换行 = 5
    比日文长 = 6
    字典使用 = 7
    引入英文 = 8
    比日文长严格 = 9
    语言不通 = 10


class CProjectConfig:
    def __init__(self, projectPath: str, config_name=CONFIG_FILENAME) -> None:
        self.projectConfig = loadConfigFile(path.join(projectPath, config_name))
        self.projectDir: str = projectPath
        input_dir = path.abspath(path.join(projectPath, INPUT_FOLDERNAME))
        path_json_jp = path.abspath(path.join(projectPath, "json_jp"))
        if not path.exists(input_dir) and path.exists(path_json_jp):
            input_dir = path_json_jp  # 兼容旧版本
        self.inputPath: str = str(input_dir)
        output_dir = path.abspath(path.join(projectPath, OUTPUT_FOLDERNAME))
        path_json_cn = path.abspath(path.join(projectPath, "json_cn"))
        if not path.exists(output_dir) and path.exists(path_json_cn):
            output_dir = path_json_cn  # 兼容旧版本
        self.outputPath: str = str(output_dir)
        self.cachePath: str = str(
            path.abspath(path.join(projectPath, CACHE_FOLDERNAME))
        )
        self.keyValues = dict()
        for k, v in self.projectConfig["common"].items():
            self.keyValues[k] = v
        self.keyValues["internals.enableProxy"] = self.projectConfig["proxy"][
            "enableProxy"
        ]
        LOGGER.debug(
            "inputPath: %s, outputPath: %s, cachePath: %s,keyValues: %s",
            self.inputPath,
            self.outputPath,
            self.cachePath,
            self.keyValues,
        )

        self.select_translator = ""  # 本次选择的翻译器
        self.pre_dic: CNormalDic = None  # 预处理字典
        self.post_dic: CNormalDic = None  # 后处理字典
        self.gpt_dic: CGptDict = None  # gpt字典
        self.file_save_funcs = {}  # 文件保存函数
        self.name_replaceDict = {}  # 名字替换字典
        self.tPlugins = []  # 文本插件列表
        self.fPlugins = []  # 文件插件列表
        self.tokenPool = None  # 令牌池
        self.proxyPool = None  # 代理池
        self.endpointQueue = None  # 端点队列
        self.input_splitter = None  # 输入分割器

    def getProjectConfig(self) -> dict:
        """
        获取解析的 YAML 配置文件
        """
        return self.projectConfig

    def getProjectDir(self) -> str:
        return self.projectDir

    def getTextPluginList(self) -> list:
        if "plugin" not in self.projectConfig:
            return []
        else:
            return self.projectConfig["plugin"]["textPlugins"]

    def getFilePlugin(self) -> str:
        if "plugin" not in self.projectConfig:
            return "file_galtransl_json"
        else:
            return self.projectConfig["plugin"]["filePlugin"]

    def getInputPath(self) -> str:
        return self.inputPath

    def getOutputPath(self) -> str:
        return self.outputPath

    def getCachePath(self) -> str:
        return self.cachePath

    def getCommonConfigSection(self) -> dict:
        return self.projectConfig["common"]

    def getPluginConfigSection(self) -> dict:
        return self.projectConfig["plugin"]

    def getlbSymbol(self) -> str:
        lbSymbol = self.projectConfig["common"].get("linebreakSymbol", "auto")
        return lbSymbol

    def getProxyConfigSection(self) -> dict:
        return self.projectConfig["proxy"]["proxies"]

    def getBackendConfigSection(self, backendName: str) -> dict:
        """
        backendName: GPT35 / GPT4 / ChatGPT / bingGPT4
        """
        if backendName=="OpenAI-Compatible":
            if "OpenAI-Compatible" not in self.projectConfig["backendSpecific"]:
                backendName="GPT4"
        elif backendName=="SakuraLLM":
            if "SakuraLLM" not in self.projectConfig["backendSpecific"]:
                backendName="Sakura"
        return self.projectConfig["backendSpecific"][backendName]

    def getDictCfgSection(self, key: str = "") -> dict:
        if key == "":
            return self.projectConfig["dictionary"]
        elif key in self.projectConfig["dictionary"]:
            return self.projectConfig["dictionary"][key]
        else:
            return None

    def getKey(self, key: str, default: None = None) -> str | bool | int | None:
        return self.keyValues.get(key, default)

    def getProblemAnalyzeConfig(self, backendName: str) -> list[CProblemType]:
        if backendName not in self.projectConfig["problemAnalyze"]:
            return []
        result: list[CProblemType] = []
        for i in self.projectConfig["problemAnalyze"][backendName]:
            result.append(CProblemType[i])

        return result

    def getProblemAnalyzeArinashiDict(self) -> dict:
        if "arinashiDict" not in self.projectConfig["problemAnalyze"]:
            return {}
        elif not self.projectConfig["problemAnalyze"]["arinashiDict"]:
            return {}
        return self.projectConfig["problemAnalyze"]["arinashiDict"]


class CProxyPool:
    def __init__(self, config: CProjectConfig) -> None:
        self.proxies: list[tuple[bool, CProxy]] = []
        for i in config.getProxyConfigSection():
            self.proxies.append(
                (False, CProxy(i["address"], i.get("username", i.get("password"))))
            )

    @retry(stop=stop_after_attempt(3), wait=wait_fixed(1))
    async def _availablityChecker(
        self, proxy: CProxy, test_address="http://www.gstatic.com/generate_204"
    ) -> tuple[bool, CProxy]:
        try:
            st = time()
            LOGGER.debug("start testing proxy %s", proxy.addr)
            async with AsyncClient(proxy=proxy.addr) as client:
                response = await client.get(test_address)
                if response.status_code != 204:
                    LOGGER.debug(
                        "tested proxy %s failed (%s)", proxy.addr, response
                    )
                    return False, proxy
                else:
                    return True, proxy
        except TimeoutException:
            LOGGER.debug("we got exception in testing proxy %s", proxy.addr)
            return False, proxy
        except:
            LOGGER.error("代理 %s 无法连接", proxy.addr)
            return False, proxy
        finally:
            et = time()
            LOGGER.debug("tested proxy %s in %s", proxy.addr, et - st)
            pass

    async def checkAvailablity(self) -> None:
        fs = []
        for _, proxy in self.proxies:
            fs.append(self._availablityChecker(proxy))
        result: list[tuple[bool, CProxy]] = await gather(*fs)
        newList: list[tuple[bool, CProxy]] = []
        for proxyStatus, proxy in result:
            if proxyStatus != True:
                LOGGER.info("removed proxy %s, because it's not available", proxy.addr)
            else:
                newList.append((True, proxy))
        self.proxies = newList

    def getProxy(self) -> CProxy:
        rounds: int = 0
        while True:
            if rounds > 10:
                raise RuntimeError("CProxyPool::getProxy: 没有可用的代理！")
            available, proxy = choice(self.proxies)
            if not available:
                rounds += 1
                continue
            else:
                return proxy


def initProxyList(config: CProjectConfig) -> Optional[list[dict]]:
    """
    处理代理设置项
    """
    result: list = []
    for i in config.getProxyConfigSection():
        result.append(
            {
                "addr": i["address"],
                "username": i.get("username"),
                "password": i.get("password"),
            }
        )

    return result


def initDictList(config: dict, dictDir: str, projectDir: str) -> Optional[list[str]]:
    """
    处理字典设置项
    """
    if not config:
        return []
    result: list[str] = []
    for entry in config:
        if entry.startswith("(project_dir)"):
            entry = entry.replace("(project_dir)", "")
            result.append(str(path.abspath(projectDir) + sep + entry))
        else:
            result.append(str(path.abspath(dictDir) + sep + entry))
    return result


def loadConfigFile(path: str) -> dict:
    """
    加载项目配置文件（YAML）
    """
    with open(path, "rb") as cfgfile:
        cfg: dict = {}
        try:
            cfg = safe_load(cfgfile.read())
        except Exception as err:
            LOGGER.error(f"error parsing config file: {err}")
            return False
        """
        try:
            validate(cfg)
        except ValidationError as err:
            LOGGER.error(f"config file is invaild: {err}")
            return False
        """
        return cfg
