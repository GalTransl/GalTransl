from GalTransl.CSentense import *
from GalTransl.ConfigHelper import CProjectConfig
from GalTransl.Dictionary import CGptDict
from GalTransl.Backend.BaseTranslate import BaseTranslate
from GalTransl import LOGGER
from GalTransl.i18n import get_text,GT_LANG


class CRebuildTranslate(BaseTranslate):
    def __init__(
        self,
        config: CProjectConfig,
        eng_type: str,
    ):
        pass

    def init(self) -> bool:
        """
        call it before jobs
        """
        pass

    async def asyncTranslate(self, content: CTransList, gptdict="") -> CTransList:
        """
        translate with async requests
        """
        pass

    async def batch_translate(
        self,
        filename,
        cache_path,
        trans_list: CTransList,
        num_pre_req: int,
        retry_failed: bool = False,
        gpt_dic: CGptDict = None,
        proofread: bool = False,
        retran_key: str = "",
        translist_hit: CTransList = [],
        translist_unhit: CTransList = [],
    ) -> CTransList:

        if len(translist_hit) != len(trans_list):  # 不Build
            error_msg = get_text("cache_incomplete", GT_LANG, filename)
            LOGGER.error(error_msg)
            raise Exception(error_msg)

        return translist_hit
