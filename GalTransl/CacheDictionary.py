"""Read-only dictionary inspection for the cache browser, using current rules."""

from os.path import exists

from GalTransl.ConfigHelper import CProjectConfig, initDictList
from GalTransl.CSentense import CSentense
from GalTransl.Dictionary import CNormalDic, CGptDict
from GalTransl.Frontend.LLMTranslate import preprocess_trans_list, postprocess_trans_list


MAX_BATCH = 100
DETAIL_PAGE_SIZE = 100


class _RecordingDictionary:
    def __init__(self, dictionary):
        self.dictionary = dictionary
        self.matches = {}

    def do_replace(self, text, tran):
        return self.dictionary.do_replace(
            text, tran, matches=self.matches.setdefault(id(tran), [])
        )


def _entry_detail(dic):
    return {
        "file": dic.source_file,
        "line": dic.line_number,
        "search": dic.search_word,
        "replacement": dic.replace_word,
        "note": dic.note,
        "rule": dic.raw_line,
    }


def _sentence(entry):
    if not isinstance(entry, dict):
        raise ValueError("each sentence must be an object")

    def text(*keys):
        value = next((entry[key] for key in keys if entry.get(key)), "")
        if not isinstance(value, str):
            raise ValueError("sentence text fields must be strings")
        return value

    speaker = entry.get("name", "") or ""
    if not isinstance(speaker, str) and not (
        isinstance(speaker, list) and all(isinstance(name, str) for name in speaker)
    ):
        raise ValueError("name must be a string or a list of strings")
    tran = CSentense(text("pre_src", "pre_jp", "post_src", "post_jp"), speaker, entry.get("index", 0))
    tran.pre_dst = text("pre_dst", "pre_zh")
    tran.post_dst = tran.proofread_zh = text("proofread_dst", "proofread_zh")
    tran.post_dst = tran.post_dst or tran.pre_dst
    return tran


def inspect_cache_dictionaries(project_dir, config_name, entries, *, details=False, offset=0):
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_BATCH:
        raise ValueError(f"entries must contain 1-{MAX_BATCH} sentences")
    if any(not isinstance(entry, dict) for entry in entries):
        raise ValueError("each entry must be an object")
    if not isinstance(details, bool) or (details and len(entries) != 1):
        raise ValueError("details must be boolean; details require exactly one sentence")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a non-negative integer")

    config = CProjectConfig(project_dir, config_name)
    settings = config.getDictCfgSection()
    dictionaries = []
    for key, cls in (("preDict", CNormalDic), ("postDict", CNormalDic), ("gpt.dict", CGptDict)):
        paths = initDictList(settings.get(key, []), settings.get("defaultDictFolder", ""), project_dir)
        # Browsing is repeated frequently: skip optional missing dictionaries
        # before loading, without muting warnings during actual translation.
        dictionary = cls([filename for filename in paths if exists(filename)])
        if settings.get("sortDict", True):
            dictionary.sort_dic()
        dictionaries.append(dictionary)
    pre, post = map(_RecordingDictionary, dictionaries[:2])
    gpt = dictionaries[2]
    results = []
    for entry in entries:
        before, after = entry.get("context_before", []), entry.get("context_after", [])
        if any(not isinstance(context, list) or len(context) > 2 for context in (before, after)):
            raise ValueError("sentence context must contain at most two neighbours per side")
        sentences = [_sentence(item) for item in [*before, entry, *after]]
        tran = sentences[len(before)]
        for i, item in enumerate(sentences):
            item.prev_tran = sentences[i - 1] if i else None
            item.next_tran = sentences[i + 1] if i + 1 < len(sentences) else None
        # Context keeps split dialogue handling consistent across batches/detail pages.
        # Do not execute arbitrary text plugins or write cache files during inspection.
        preprocess_trans_list(sentences, config, pre)
        # The cached post_src is the model input, including text plugin processing.
        cached_source = entry.get("post_src") or entry.get("post_jp")
        if cached_source and not isinstance(cached_source, str):
            raise ValueError("post_src must be a string")
        tran.post_src = cached_source or tran.post_src
        if tran.post_dst:
            postprocess_trans_list([tran], config, post)
        groups = {
            "pre": pre.matches.get(id(tran), []),
            "post": post.matches.get(id(tran), []),
            "gpt": gpt.matching_entries([tran]),
        }
        # A rule applied to both the speaker and the text counts as one entry.
        groups = {key: list({id(dic): dic for dic in values}.values()) for key, values in groups.items()}
        result = {"counts": {key: len(values) for key, values in groups.items()}}
        if details:
            flattened = [(kind, dic) for kind, values in groups.items() for dic in values]
            result.update({
                "entries": [{"kind": kind, **_entry_detail(dic)} for kind, dic in flattened[offset:offset + DETAIL_PAGE_SIZE]],
                "total": len(flattened),
                "offset": offset,
            })
        results.append(result)
        pre.matches.clear()
        post.matches.clear()
    return {"results": results}
