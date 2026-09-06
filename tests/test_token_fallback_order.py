import asyncio
import unittest
from types import SimpleNamespace

from GalTransl.COpenAI import COpenAIToken, COpenAITokenPool


class TokenFallbackOrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_availability_checks_keep_configured_order(self) -> None:
        pool = COpenAITokenPool.__new__(COpenAITokenPool)
        first = COpenAIToken("first", "https://first.example", "model-a")
        second = COpenAIToken("second", "https://second.example", "model-b")
        pool.tokens = [(True, first), (True, second)]
        pool.pj_config = SimpleNamespace(
            non_interactive=True,
            stop_event=None,
            getBackendConfigSection=lambda _name: {"checkAvailableConcurrency": 2},
        )
        pool._raise_if_stop_requested = lambda: None

        async def check_token(token, proxy=None):
            # The second endpoint finishes first; result order must not change.
            await asyncio.sleep(0.03 if token is first else 0.0)
            return True, token

        pool._check_token_availability_with_retry = check_token

        await pool.checkTokenAvailablity()

        self.assertEqual(
            [token.model_name for _, token in pool.tokens], ["model-a", "model-b"]
        )


if __name__ == "__main__":
    unittest.main()
