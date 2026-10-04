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
from GalTransl.RuntimePaths import resolve_dict_dir
from GalTransl.PluginSettings import get_plugin_config_section
from asyncio import gather
from tenacity import retry, stop_after_attempt, wait_fixed
import httpx
import inspect
from httpx import AsyncClient, TimeoutException
from time import time
from typing import Optional
from random import choice
from yaml import safe_load
from os import path
from pathlib import Path
from importlib.metadata import version



def build_httpx_proxy_kwargs(proxy_addr: Optional[str]) -> dict:
    """根据当前安装的 httpx 版本，返回与 `httpx.AsyncClient` 兼容的代理参数。

    - httpx < 0.26: 仅支持 `proxies=`
    - 0.26 <= httpx < 0.28: 同时支持 `proxy=` 与 `proxies=`
    - httpx >= 0.28: 仅支持 `proxy=`
    """
    if not proxy_addr:
        return {}
    try:
        params = inspect.signature(httpx.AsyncClient.__init__).parameters
    except (TypeError, ValueError):
        params = {}
    if "proxy" in params:
        return {"proxy": proxy_addr}
    if "proxies" in params:
        return {"proxies": proxy_addr}
    # 兜底：通过 mounts 指定代理传输层
    return {"mounts": {"all://": httpx.AsyncHTTPTransport(proxy=proxy_addr)}}


def build_httpx_sync_proxy_kwargs(proxy_addr: Optional[str]) -> dict:
    """同 `build_httpx_proxy_kwargs`，但用于 `httpx.Client`。"""
    if not proxy_addr:
        return {}
    try:
        params = inspect.signature(httpx.Client.__init__).parameters
    except (TypeError, ValueError):
        params = {}
    if "proxy" in params:
        return {"proxy": proxy_addr}
    if "proxies" in params:
        return {"proxies": proxy_addr}
    return {"mounts": {"all://": httpx.HTTPTransport(proxy=proxy_addr)}}


def has_usable_proxy_config(proxy_cfg: Optional[dict]) -> bool:
    if not isinstance(proxy_cfg, dict):
        return False
    if not bool(proxy_cfg.get("enableProxy", False)):
        return False
    proxies = proxy_cfg.get("proxies", [])
    if not isinstance(proxies, list):
        return False
    for item in proxies:
        if not isinstance(item, dict):
            continue
        address = str(item.get("address", "")).strip()
        if address:
            return True
    return False


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
        self.refreshProxyEnabledFlag()
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
        self.pPlugins = None  # None means problem plugins have not been loaded yet.
        self._problem_type_catalog = None
        self.fPluginAuto = False  # filePlugin: auto 时按文件逐个识别插件
        self.tokenPool = None  # 令牌池
        self.proxyPool = None  # 代理池
        self.endpointQueue = None  # 端点队列
        self.input_splitter = None  # 输入分割器
        self.active_workers: int=0
        self.target_lang=""
        self.translation_guideline=""
        self.non_interactive: bool = False  # 非交互模式（前端启动时为True）
        self.runtime_project_dir: str = projectPath
        # 只翻译这些输入文件（文件名匹配）；空 = 全部。试译/部分重翻场景由
        # JobSpec.input_files 注入，run_job 阶段设置，翻译流程读取。
        self.runtime_input_files: list = []
        

    def getProjectConfig(self) -> dict:
        """
        获取解析的 YAML 配置文件
        """
        return self.projectConfig

    def getProjectDir(self) -> str:
        return self.projectDir

    def getTextPluginList(self) -> list:
        return self.getPluginConfigSection().get("textPlugins") or []

    def getFilePlugin(self) -> str:
        return self.getPluginConfigSection().get("filePlugin", "file_galtransl_json")

    def getProblemPluginList(self) -> list:
        # Existing projects keep the built-in checks; an explicit [] disables them.
        return self.getPluginConfigSection().get(
            "problemPlugins", ["problem_common"]
        ) or []

    def getInputPath(self) -> str:
        return self.inputPath

    def getOutputPath(self) -> str:
        return self.outputPath

    def getCachePath(self) -> str:
        return self.cachePath

    def getCommonConfigSection(self) -> dict:
        return self.projectConfig["common"]

    def getPluginConfigSection(self) -> dict:
        return get_plugin_config_section(self.projectConfig)

    def getlbSymbol(self) -> str:
        lbSymbol = self.projectConfig["common"].get("linebreakSymbol", "auto")
        return lbSymbol

    def getProxyConfigSection(self) -> dict:
        return self.projectConfig.get("proxy", {}).get("proxies", [])

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

    def getProblemAnalyzeConfig(self, backendName: str) -> list[str]:
        """Return configured names; plugins own and interpret their problem types."""
        analyze = self.projectConfig.get("problemAnalyze")
        if analyze is None:
            analyze = {}
        if not isinstance(analyze, dict):
            raise ValueError("problemAnalyze configuration must be an object")
        configured = analyze.get(backendName)
        if configured is None and backendName == "problemList":
            configured = analyze.get("GPT35")
        if configured is not None:
            if isinstance(configured, str):
                configured = configured.splitlines()
            if not isinstance(configured, list) or any(not isinstance(item, str) for item in configured):
                raise ValueError(f"problemAnalyze.{backendName} must be a list of names or a newline-separated string")
            return [item.strip() for item in configured if item.strip()]
        if backendName != "problemList":
            return []
        if self._problem_type_catalog is None:
            from GalTransl.Problem import list_problem_types

            self._problem_type_catalog = list_problem_types(self.getProjectDir())
        return [item["name"] for item in self._problem_type_catalog if item["default_enabled"]]

    def getProblemAnalyzeArinashiDict(self) -> dict:
        return (self.projectConfig.get("problemAnalyze") or {}).get("arinashiDict") or {}

    def refreshProxyEnabledFlag(self) -> None:
        self.keyValues["internals.enableProxy"] = has_usable_proxy_config(
            self.projectConfig.get("proxy", {})
        )


class CProxyPool:
    def __init__(self, config: CProjectConfig) -> None:
        self.proxies: list[tuple[bool, CProxy]] = []
        for i in config.getProxyConfigSection():
            if not isinstance(i, dict):
                continue
            address = str(i.get("address", "")).strip()
            if not address:
                continue
            self.proxies.append(
                (False, CProxy(address, i.get("username"), i.get("password")))
            )

    @retry(stop=stop_after_attempt(3), wait=wait_fixed(1))
    async def _availablityChecker(
        self, proxy: CProxy, test_address="http://www.gstatic.com/generate_204"
    ) -> tuple[bool, CProxy]:
        try:
            st = time()
            LOGGER.debug("start testing proxy %s", proxy.addr)
            async with AsyncClient(**build_httpx_proxy_kwargs(proxy.addr)) as client:
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
    project_root = Path(projectDir).expanduser().resolve()
    configured_dict_dir = Path(dictDir or ".").expanduser()
    resource_dict_dir = resolve_dict_dir(configured_dict_dir)
    for entry in config:
        entry = str(entry)
        if entry.startswith("(project_dir)"):
            entry = entry.removeprefix("(project_dir)").lstrip("/\\")
            result.append(str((project_root / entry).resolve()))
        else:
            result.append(str((resource_dict_dir / entry).resolve()))
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
