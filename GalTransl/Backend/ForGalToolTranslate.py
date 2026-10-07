"""每批独立的工具翻译：通过译文补丁分组写入结果。"""

import json

from GalTransl import LOGGER
from GalTransl.Backend.ForGalJsonTranslate import ForGalJsonTranslate
from GalTransl.Backend.Prompts import FORGAL_TOOL_SYSTEM_PROMPT, FORGAL_TOOL_TRANS_PROMPT


TRANSLATION_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "write_translation_result",
            "description": "Apply a translation patch containing 1 to N sentences, where N is the number of sentences in the current input batch: *** Begin Patch, @@ anchor|id, +translation, *** End Patch. All changes are applied atomically. Use only anchors from the current input. Follow the Next patch anchors in each tool response. Do not repeat completed groups. Previously written sentences may be patched only to make actual corrections. Correct and retry failed patches.",
            "parameters": {
                "type": "object",
                "properties": {"patch": {"type": "string", "description": "Complete translation patch text, starting with *** Begin Patch and ending with *** End Patch."}},
                "required": ["patch"],
                "additionalProperties": False,
            },
        },
    },
]


def apply_translation_patch(patch, expected, results):
    """Validate the whole patch before updating this batch's in-memory results."""
    lines = patch.strip().splitlines()
    if len(lines) < 4 or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        raise ValueError("需要完整的 *** Begin Patch / *** End Patch")
    pending = {}
    key = None
    text = []

    def finish():
        if key is None:
            return
        value = "\n".join(text)
        if not text or (expected[key] and not value.strip()):
            raise ValueError(f"{key} 译文为空")
        if "�" in value:
            raise ValueError(f"{key} 译文包含乱码")
        pending[key] = value

    for line in lines[1:-1]:
        if line.startswith("@@ "):
            finish()
            key = line.removeprefix("@@ ")
            if key not in expected:
                raise ValueError(f"未知的 anchor|id：{key}")
            if key in pending:
                raise ValueError(f"重复的 anchor|id：{key}")
            text = []
        elif key is not None and line.startswith("+"):
            text.append(line[1:])
        else:
            raise ValueError("译文行必须以 + 开头，并位于 @@ anchor|id 后")
    finish()
    if not pending:
        raise ValueError("补丁没有译文")
    # 条目必须来自本批且不重复，因此最多可写入本批的全部句子。
    results.update(pending)
    return len(pending)


class ForGalToolTranslate(ForGalJsonTranslate):
    default_trans_prompt = FORGAL_TOOL_TRANS_PROMPT
    default_system_prompt = FORGAL_TOOL_SYSTEM_PROMPT

    def __init__(self, config, eng_type, proxy_pool, token_pool):
        super().__init__(config, eng_type, proxy_pool, token_pool)
        self.multi_turn = False
        self.enhance_jailbreak = False

    def _get_effective_num_per_request(self, configured_value, proofread=False):
        return self._coerce_positive_int(configured_value, 1)

    def _update_dynamic_num_per_request(self, **kwargs):
        # 严格使用用户设置的单次句数，不自动扩缩输入批次。
        pass

    def _prepare_translation_messages(self, *args, **kwargs):
        # 父类解析重试可能启用 JSON prefill；工具模板始终禁用。
        kwargs["assistant_prompt"] = ""
        return super()._prepare_translation_messages(*args, **kwargs)

    def _get_chatbot_state(self):
        # 工具响应在本批完成后统一适配为记录，不走父类的逐行流式解析。
        return False, super()._get_chatbot_state()[1]

    async def _ask_translation_batch(self, session, *, trans_list, sig_list, proofread, n_symbol="", **kwargs):
        messages = list(kwargs.pop("messages"))
        kwargs.pop("stream_line_callback", None)
        kwargs.pop("reasoning_holder", None)
        expected = {f"{sig}|{tran.index}": tran.post_src for sig, tran in zip(sig_list, trans_list)}
        sentences_by_key = {f"{sig}|{tran.index}": tran for sig, tran in zip(sig_list, trans_list)}
        results = {}
        reasoning_field = ""
        token = None
        missing_tool_retries = 0
        no_progress_calls = 0
        failure_reason = "工具调用轮数达到上限"

        def progress_instruction():
            remaining = [key for key in expected if key not in results]
            if not remaining:
                return "All sentences are written."
            next_keys = ", ".join(remaining)
            return (
                f"Progress: {len(results)}/{len(expected)} sentences written. "
                f"Next patch anchors (in order): {next_keys}. "
                f"Each patch may contain 1 to {len(expected)} sentences from this batch. "
                "Translate these unwritten sentences next; do not repeat completed groups. "
                "Only revisit a written sentence to make an actual correction."
            )

        def formatted_results():
            # 与 ForGal-json 的部分解析一致：只推进连续的已完成前缀，不能跳过缺句。
            key = "newdst" if proofread else "dst"
            lines = []
            for sig, tran in zip(sig_list, trans_list):
                anchor = f"{sig}|{tran.index}"
                if anchor not in results:
                    break
                lines.append(self._encode_sig_jsonline(sig, {"id": tran.index, key: results[anchor]}))
            return "\n".join(lines)

        # 工具往返仅属于当前批次，不保留跨批次对话。设置上限防止模型空转。
        for _ in range(2 * len(trans_list) + 6):
            self._check_stop_requested()
            response, reasoning = {}, {}
            next_tool = "write_translation_result"
            tool_choice = {"type": "function", "function": {"name": next_tool}}
            content, token = await self.ask_chatbot(
                messages=list(messages), tools=TRANSLATION_TOOLS,
                tool_choice=tool_choice, tool_response_holder=response, reasoning_holder=reasoning, **kwargs,
            )
            if response.get("finish_reason") in ("length", "content_filter"):
                failure_reason = f"响应结束原因为 {response['finish_reason']}，未执行本次工具调用"
                LOGGER.warning("[ForGal-tool][%s] %s；正文=%r", kwargs.get("file_name", ""), failure_reason, content)
                break  # 截断的工具调用绝不能应用
            calls = response.get("tool_calls", [])
            # 部分兼容接口在指定 function 的请求里省略返回的 function.name。
            # 本模板只开放一个工具，因此可依据请求补齐；必须在保存 assistant
            # 历史前修正，避免下一轮把空名称再次发给接口。不同调用 ID 不合并。
            normalized_calls = []
            for call in calls:
                function = call.get("function")
                if (
                    call.get("type") == "function"
                    and isinstance(function, dict)
                    and function.get("name") in (None, "")
                ):
                    LOGGER.warning(
                        "[ForGal-tool][%s][%s] 接口返回的工具名为空，按本轮指定工具补齐为 %s",
                        kwargs.get("file_name", ""), call.get("id", ""), next_tool,
                    )
                    call = {**call, "function": {**function, "name": next_tool}}
                normalized_calls.append(call)
            calls = normalized_calls
            if not calls:
                missing_tool_retries += 1
                failure_reason = f"缺少工具调用，等待 {next_tool}；已写入 {len(results)}/{len(expected)} 句"
                LOGGER.warning(
                    "[ForGal-tool][%s] %s；finish_reason=%s；正文=%r；保留上下文重试 %s/3",
                    kwargs.get("file_name", ""), failure_reason, response.get("finish_reason"), content, missing_tool_retries,
                )
                if missing_tool_retries >= 3:
                    break
                assistant = {"role": "assistant", "content": content or ""}
                if reasoning.get("text"):
                    reasoning_field = reasoning["field"]
                if reasoning_field:
                    assistant[reasoning_field] = reasoning.get("text", "")
                messages.append(assistant)
                messages.append({
                    "role": "user",
                    "content": f"Call {next_tool} now. Do not describe the tool call in text. {progress_instruction()}",
                })
                continue
            missing_tool_retries = 0
            assistant = {"role": "assistant", "content": content or "", "tool_calls": calls}
            if reasoning.get("text"):
                reasoning_field = reasoning["field"]
            if reasoning_field:
                assistant[reasoning_field] = reasoning.get("text", "")
            messages.append(assistant)
            for call in calls:
                function = call.get("function", {})
                name, arguments = function.get("name"), function.get("arguments", "")
                log_label = f"[ForGal-tool][{kwargs.get('file_name', '')}][{getattr(token, 'model_name', '')}][{call['id']}][{name}]"
                print(f"{log_label} LLM工具调用：\n{arguments}", flush=True)
                output = ""
                previous_count = len(results)
                try:
                    # 与 Agent runner 一样，在拼接完 function.arguments 后解析参数，
                    # 参数错误通过配对的 tool 消息返回模型修正。
                    try:
                        args = json.loads(arguments or "{}")
                    except (json.JSONDecodeError, TypeError) as exc:
                        raise ValueError(f"参数 JSON 解析失败：{exc}") from exc
                    if not isinstance(args, dict):
                        raise ValueError("工具参数必须为对象")
                    if name != "write_translation_result":
                        raise ValueError(f"未知工具：{name}")
                    payload = args.get("patch")
                    if not isinstance(payload, str):
                        raise ValueError("工具参数 patch 必须为字符串")
                    print(f"{log_label} 工具输入：\n{payload}", flush=True)
                    pending = {}
                    count = apply_translation_patch(payload, expected, pending)
                    new_count = sum(key not in results for key in pending)
                    corrected_count = sum(key in results and results[key] != text for key, text in pending.items())
                    if not new_count and not corrected_count:
                        raise ValueError("补丁重复提交已完成译文，没有新增或修改；请继续下一组")
                    # 整份 patch 校验、规范化后再发布，失败的 patch 不产生最近译文。
                    normalized = {
                        key: self._normalize_parsed_translation_text(text, sentences_by_key[key], n_symbol)
                        for key, text in pending.items()
                    }
                    results.update(pending)
                    if not proofread:
                        model_name = getattr(token, "model_name", "")
                        for key, text in normalized.items():
                            tran = sentences_by_key[key]
                            already_reported = (
                                getattr(tran, "_runtime_success_recorded", False)
                                and tran.pre_dst == text and tran.trans_by == model_name
                            )
                            tran.pre_dst = tran.post_dst = text
                            tran.trans_by = model_name
                            if not already_reported:
                                self._record_runtime_success(kwargs.get("progress_file", ""), tran)
                            # 整批收尾时复用现有标记，避免再次上报同一句。
                            tran._runtime_success_recorded = True
                    output = f"补丁应用成功：{count} 句（新增 {new_count}，修正 {corrected_count}）；本批已完成 {len(results)}/{len(expected)} 句。"
                except ValueError as exc:
                    output = f"补丁应用失败：{exc}"
                no_progress_calls = 0 if len(results) > previous_count else no_progress_calls + 1
                output += "\n" + progress_instruction()
                print(f"{log_label} 工具返回：\n{output}", flush=True)
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": output})
                if len(results) == len(expected):
                    # 最后一份 patch 成功即完成本批，不再请求模型生成结尾。
                    self._clear_chatbot_state()
                    return formatted_results(), token
            if no_progress_calls >= 3 and len(results) < len(expected):
                failure_reason = "连续 3 次工具调用未新增译文"
                break
        partial = formatted_results()
        LOGGER.warning(
            "[ForGal-tool][%s] 未完成本批工具翻译：%s；%s",
            kwargs.get("file_name", ""), failure_reason,
            "保留已完成的连续译文，后续从未完成位置继续" if partial else "无连续译文，按解析失败策略重试",
        )
        self._clear_chatbot_state()
        return partial, token
