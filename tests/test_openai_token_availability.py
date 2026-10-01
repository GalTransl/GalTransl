"""可用性检测不能因为一个可选参数（max_tokens）把能用的后端判死。

真实报错（用户的主翻译引擎）：
    检查模型可用性请求失败 [sk-7Bj...c6bP]: BadRequestError: Error code: 400 -
    {'error': {'message': 'max_tokens must be greater than 2', ...}}
起因：检测请求为了"别让模型瞎生成"写了 max_tokens=1，而有的中转对 max_tokens 有下限。

现在的约定：
- 上限用一个各家都能接受的较小值（AVAILABILITY_CHECK_MAX_TOKENS，不能是 1）；
- provider 若嫌这个参数不合规（创建时或在流式迭代时抱怨），摘掉它再试一次——
  检测只要"能拿到响应"，这个可选参数没必要坚持；
- 与该参数无关的报错照旧算不可用，并且只多试一次。
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

try:  # COpenAI → ConfigHelper → yaml；精简环境（系统 python）里没有 yaml
    from GalTransl.COpenAI import (
        AVAILABILITY_CHECK_MAX_TOKENS,
        COpenAIToken,
        COpenAITokenPool,
        _rejects_max_tokens,
    )

    _IMPORT_ERROR: Exception | None = None
except ModuleNotFoundError as exc:  # pragma: no cover
    _IMPORT_ERROR = exc

MAX_TOKENS_400 = (
    "Error code: 400 - {'error': {'message': 'max_tokens must be greater than 2', "
    "'type': 'invalid_request_error', 'code': 'invalid_request'}}"
)


def _chunk(choices=True):
    return SimpleNamespace(choices=[SimpleNamespace()] if choices else [])


class _FakeClient:
    """假 OpenAI 客户端：按脚本在 create 时抛错 / 返回（流式则逐个迭代）。"""

    def __init__(self, behaviors):
        self.behaviors = behaviors
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        behavior = self.behaviors[min(len(self.calls) - 1, len(self.behaviors) - 1)]
        if isinstance(behavior, BaseException):
            raise behavior
        if callable(behavior):  # 迭代时抛错的情形
            return behavior()
        return behavior

    @property
    def chat(self):
        return SimpleNamespace(completions=self)


def _pool_state() -> SimpleNamespace:
    """只借 _isTokenAvailable_sync 需要的两样东西（timeout 与错误记录）。"""
    errors: list[dict] = []

    def _record(**kwargs):
        errors.append(kwargs)

    state = SimpleNamespace(timeout=5, _record_runtime_error=_record)
    state.errors = errors  # type: ignore[attr-defined]
    return state


class MaxTokensToleranceTests(unittest.TestCase):
    def setUp(self):
        if _IMPORT_ERROR is not None:
            self.skipTest(f"COpenAI 依赖不可用: {_IMPORT_ERROR}")

    def _patch_client(self, fake: _FakeClient):
        return patch("GalTransl.COpenAI.OpenAI", lambda **kwargs: fake)

    def test_check_does_not_use_max_tokens_of_one(self):
        """上限不能是 1（有的家要求 >2）。"""
        self.assertGreater(AVAILABILITY_CHECK_MAX_TOKENS, 2)
        fake = _FakeClient([[ _chunk() ]])
        token = COpenAIToken(token="sk-x", domain="https://relay.example.com/v1", model_name="m", stream=True)
        with self._patch_client(fake):
            ok, _ = COpenAITokenPool._isTokenAvailable_sync(_pool_state(), token)

        self.assertTrue(ok)
        self.assertEqual(fake.calls[0]["max_tokens"], AVAILABILITY_CHECK_MAX_TOKENS)

    def test_max_tokens_rejection_is_retried_without_it(self):
        """「max_tokens must be greater than 2」→ 摘掉该参数重试，后端照样算可用。"""
        fake = _FakeClient([RuntimeError(MAX_TOKENS_400), [_chunk()]])
        token = COpenAIToken(token="sk-7Bj", domain="https://relay.example.com/v1", model_name="m", stream=True)
        with self._patch_client(fake):
            ok, _ = COpenAITokenPool._isTokenAvailable_sync(_pool_state(), token)

        self.assertTrue(ok)
        self.assertEqual(len(fake.calls), 2)
        self.assertIn("max_tokens", fake.calls[0])
        self.assertNotIn("max_tokens", fake.calls[1])

    def test_rejection_during_stream_iteration_is_also_retried(self):
        """有的实现到迭代流时才报这个错——同样要兜住。"""
        def _raising_stream():
            raise RuntimeError(MAX_TOKENS_400)

        fake = _FakeClient([_raising_stream, [_chunk()]])
        token = COpenAIToken(token="sk-x", domain="https://relay.example.com/v1", model_name="m", stream=True)
        with self._patch_client(fake):
            ok, _ = COpenAITokenPool._isTokenAvailable_sync(_pool_state(), token)

        self.assertTrue(ok)
        self.assertEqual(len(fake.calls), 2)
        self.assertNotIn("max_tokens", fake.calls[1])

    def test_unrelated_error_is_still_unavailable(self):
        fake = _FakeClient([RuntimeError("502 bad gateway")])
        state = _pool_state()
        token = COpenAIToken(token="sk-x", domain="https://relay.example.com/v1", model_name="m", stream=True)
        with self._patch_client(fake):
            ok, _ = COpenAITokenPool._isTokenAvailable_sync(state, token)

        self.assertFalse(ok)
        self.assertEqual(len(fake.calls), 1)  # 只发一次，不乱重试
        self.assertTrue(state.errors)  # 仍上报运行时错误
        self.assertIn("502", str(state.errors[0]["message"]))

    def test_type_error_fallback_is_kept(self):
        """老兜底（SDK 层面不认 max_tokens 参数）没被新分支挤掉。"""
        fake = _FakeClient([TypeError("unexpected keyword argument 'max_tokens'"), [_chunk()]])
        token = COpenAIToken(token="sk-x", domain="https://relay.example.com/v1", model_name="m", stream=True)
        with self._patch_client(fake):
            ok, _ = COpenAITokenPool._isTokenAvailable_sync(_pool_state(), token)

        self.assertTrue(ok)
        self.assertNotIn("max_tokens", fake.calls[1])

    def test_non_stream_uses_choices(self):
        fake = _FakeClient([SimpleNamespace(choices=[])])
        token = COpenAIToken(token="sk-x", domain="https://relay.example.com/v1", model_name="m", stream=False)
        with self._patch_client(fake):
            ok, _ = COpenAITokenPool._isTokenAvailable_sync(_pool_state(), token)

        self.assertFalse(ok)

    def test_empty_stream_is_unavailable(self):
        fake = _FakeClient([[]])
        token = COpenAIToken(token="sk-x", domain="https://relay.example.com/v1", model_name="m", stream=True)
        with self._patch_client(fake):
            ok, _ = COpenAITokenPool._isTokenAvailable_sync(_pool_state(), token)

        self.assertFalse(ok)


class RejectsMaxTokensTests(unittest.TestCase):
    def setUp(self):
        if _IMPORT_ERROR is not None:
            self.skipTest(f"COpenAI 依赖不可用: {_IMPORT_ERROR}")

    def test_recognises_both_spellings(self):
        self.assertTrue(_rejects_max_tokens(RuntimeError(MAX_TOKENS_400)))
        self.assertTrue(_rejects_max_tokens(RuntimeError("Unsupported parameter: max_tokens")))
        self.assertFalse(_rejects_max_tokens(RuntimeError("rate limit exceeded")))


if __name__ == "__main__":
    unittest.main()
