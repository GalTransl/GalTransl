"""The translation engine's HTTP client must deliver streamed chunks as they arrive.

Regression: pyreqwest's httpx adapter reads the body with ``read()``, which waits for a full default-size
buffer. A streamed reply (SSE) therefore arrived in one lump at the end of the response, so the file
progress light jumped from "waiting" straight to "writing" and never showed "thinking", and the stream
speed and streamed previews were wrong too.

Since replies are streamed live (8.6.0), the body is read while the model is still generating, so errors
raised mid-body have to look like ordinary network errors to the retry loop:
- pyreqwest raises ``asyncio.CancelledError("Read was cancelled")`` when a pending body read is
  interrupted. When the reading task was not actually cancelled, that escaped ``except Exception`` in
  the retry loop, took down every worker through ``gather`` and failed the whole job (reported from
  8.6.1 with only "Read was cancelled" and no traceback). A real cancel must still cancel.
- pyreqwest's own adapter maps errors only while sending, so a dropped connection mid-body surfaced as
  a raw pyreqwest exception that ``_is_transport_error`` did not recognise.
- A stream aborted early (bad line) was never closed: the cleanup looked for ``aclose``, but openai's
  ``AsyncStream`` only has ``close()``, so the connection stayed open until garbage collection.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx

from GalTransl.Backend.BaseTranslate import BaseTranslate, HttpxTransport

if HttpxTransport is not None:
    from GalTransl.Backend.BaseTranslate import _LiveByteStream

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


class _RawSseServer:
    """Raw-socket SSE server so a test can drop the connection mid-body (FIN or RST).

    ``mode``: "slow" keeps sending a chunk every 50ms for ~5s; "fin"/"rst" send a few chunks of a
    chunked body and then close without the terminating chunk. ``disconnected`` is set when a write
    fails, i.e. the client has closed its end.
    """

    def __init__(self, mode: str):
        self.mode = mode
        self.disconnected = threading.Event()
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self._sock.settimeout(0.05)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.port = self._sock.getsockname()[1]
        accept = threading.Thread(target=self._accept, daemon=True)
        accept.start()
        self._threads.append(accept)

    def _accept(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                continue
            worker = threading.Thread(target=self._serve, args=(conn,), daemon=True)
            worker.start()
            self._threads.append(worker)

    def _serve(self, conn: socket.socket):
        try:
            conn.settimeout(5)
            request = b""
            while b"\r\n\r\n" not in request:
                data = conn.recv(65536)
                if not data:
                    return
                request += data
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n"
            )
            for index in range(100):
                if self._stop.is_set():
                    return
                payload = b'data: {"choices":[],"n":%d}\n\n' % index
                try:
                    conn.sendall(b"%x\r\n%s\r\n" % (len(payload), payload))
                except OSError:
                    self.disconnected.set()
                    return
                time.sleep(0.05)
                if self.mode in ("fin", "rst") and index == 2:
                    if self.mode == "rst":
                        conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
                    return
        finally:
            conn.close()

    def close(self):
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=5)
        self._sock.close()


async def _read_body(client: httpx.AsyncClient, url: str):
    async with client.stream("GET", url, timeout=httpx.Timeout(10.0)) as response:
        async for _chunk in response.aiter_raw():
            pass


@unittest.skipIf(HttpxTransport is None, "pyreqwest is not installed")
class LiveStreamErrorTests(unittest.IsolatedAsyncioTestCase):
    def _server(self, mode: str) -> _RawSseServer:
        server = _RawSseServer(mode)
        self.addCleanup(server.close)
        return server

    async def test_interrupted_read_in_a_task_that_was_not_cancelled_is_a_retryable_read_error(self):
        # pyreqwest's own wording when a pending read is interrupted; the task doing the read was not
        # cancelled, so this must not escape the retry loop as a CancelledError
        class InterruptedReader:
            async def read_chunk(self):
                raise asyncio.CancelledError("Read was cancelled")

        async def close():
            return None

        request = httpx.Request("POST", "http://127.0.0.1/v1/chat/completions")
        stream = _LiveByteStream(SimpleNamespace(_body_reader=InterruptedReader(), aclose=close), request)

        with self.assertRaises(httpx.ReadError) as caught:
            async for _chunk in stream:
                pass

        self.assertIn("Read was cancelled", str(caught.exception))
        self.assertTrue(BaseTranslate._is_transport_error(caught.exception))

    async def test_cancelling_the_reading_task_still_cancels_it(self):
        server = self._server("slow")
        client = BaseTranslate._build_http_client(None)
        self.addAsyncCleanup(client.aclose)

        task = asyncio.ensure_future(_read_body(client, f"http://127.0.0.1:{server.port}/"))
        await asyncio.sleep(0.3)  # the task is now waiting for the next chunk
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(task.cancelled())

    async def test_connection_dropped_mid_body_is_an_httpx_transport_error(self):
        for mode in ("fin", "rst"):
            with self.subTest(mode=mode):
                server = self._server(mode)
                client = BaseTranslate._build_http_client(None)
                self.addAsyncCleanup(client.aclose)

                with self.assertRaises(httpx.TransportError) as caught:
                    await _read_body(client, f"http://127.0.0.1:{server.port}/")

                self.assertTrue(BaseTranslate._is_transport_error(caught.exception))

    async def test_aborted_openai_stream_is_closed(self):
        from openai import AsyncOpenAI

        server = self._server("slow")
        openai_client = AsyncOpenAI(
            api_key="sk-test",
            base_url=f"http://127.0.0.1:{server.port}/v1",
            max_retries=0,
            http_client=BaseTranslate._build_http_client(None),
        )
        self.addAsyncCleanup(openai_client.close)

        response = await openai_client.chat.completions.create(
            model="demo", messages=[{"role": "user", "content": "hi"}], stream=True, timeout=10.0
        )
        async for _chunk in response:
            break  # like stream_line_callback returning False on a bad line

        await BaseTranslate._close_stream_quietly(response)

        self.assertTrue(response.response.is_closed)
        # the connection is really released: the server's next write fails instead of streaming on
        self.assertTrue(await asyncio.to_thread(server.disconnected.wait, 3))

    async def test_closing_tolerates_responses_without_close(self):
        await BaseTranslate._close_stream_quietly(object())

        class SyncClose:
            closed = False

            def close(self):
                self.closed = True

        response = SyncClose()
        await BaseTranslate._close_stream_quietly(response)
        self.assertTrue(response.closed)


if __name__ == "__main__":
    unittest.main()
