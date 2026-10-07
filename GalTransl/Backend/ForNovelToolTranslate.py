"""小说工具翻译：无 name 字段的原文，通过补丁提交译文。"""

from GalTransl.Backend.ForGalToolTranslate import ForGalToolTranslate
from GalTransl.Backend.Prompts import FORNOVEL_TOOL_SYSTEM_PROMPT, FORNOVEL_TOOL_TRANS_PROMPT


class ForNovelToolTranslate(ForGalToolTranslate):
    include_speaker = False
    default_system_prompt = FORNOVEL_TOOL_SYSTEM_PROMPT
    default_trans_prompt = FORNOVEL_TOOL_TRANS_PROMPT

    def _encode_sig_jsonline(self, sig, obj):
        return super()._encode_sig_jsonline(sig, {key: value for key, value in obj.items() if key != "name"})
