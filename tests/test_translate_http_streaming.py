"""The translation engine's HTTP client must deliver streamed chunks as they arrive.

Regression: pyreqwest's httpx adapter reads the body with ``read()``, which waits for a full default-size
buffer. A streamed reply (SSE) therefore arrived in one lump at the end of the response, so the file
progress light jumped from "waiting" straight to "writing" and never showed "thinking", and the stream
speed and streamed previews were wrong too.
"""

from __future__ import annotations

import asyncio
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from GalTransl.Backend.BaseTranslate import BaseTranslate, HttpxTransport

_GAP_SECONDS = 0.4


class _SlowStream(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for index in range(3):
            self.wfile.write(f"data: {index}\n\n".encode())
            self.wfile.flush()
            time.sleep(_GAP_SECONDS)


@unittest.skipIf(HttpxTransport is None, "pyreqwest is not installed")
class EngineHttpClientStreamingTests(unittest.TestCase):
    def setUp(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _SlowStream)
        thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def test_streamed_chunks_arrive_before_the_response_ends(self):
        async def first_chunk_delay():
            client = BaseTranslate._build_http_client(None)
            self.assertIsInstance(client._transport, HttpxTransport)
            start = time.monotonic()
            async with client:
                async with client.stream("GET", f"http://127.0.0.1:{self.httpd.server_port}/") as response:
                    async for _chunk in response.aiter_raw():
                        return time.monotonic() - start

        # The whole response takes 3 gaps; the first chunk must not wait for any of them.
        self.assertLess(asyncio.run(first_chunk_delay()), _GAP_SECONDS)


if __name__ == "__main__":
    unittest.main()
