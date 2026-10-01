"""Exercise real socket conflicts, including Windows address reuse."""

import errno
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from GalTransl.Agent import AgentRuntime
from GalTransl.server import BackendHTTPServer, serve


class ProbeHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"original listener"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class ServerPortBindingTests(unittest.TestCase):
    def test_dynamic_port_is_published_and_used_by_agent(self):
        runtime = AgentRuntime()
        with tempfile.TemporaryDirectory(prefix="galtransl startup ") as directory:
            ready_file = Path(directory) / "ready.json"

            def inspect_started(server):
                info = json.loads(ready_file.read_text(encoding="utf-8"))
                self.assertGreater(info["port"], 0)
                self.assertEqual(info["port"], server.server_port)
                self.assertEqual(info["pid"], os.getpid())
                self.assertEqual((runtime.host, runtime.port), ("127.0.0.1", server.server_port))
                self.assertFalse(ready_file.with_name("ready.json.tmp").exists())
                raise KeyboardInterrupt

            with patch("GalTransl.server.AGENT_REGISTRY", runtime):
                with patch.object(BackendHTTPServer, "serve_forever", autospec=True, side_effect=inspect_started):
                    with redirect_stdout(io.StringIO()) as output:
                        serve(port=0, ready_file=str(ready_file))
            self.assertIn(f"http://127.0.0.1:{runtime.port}", output.getvalue())
            self.assertFalse(ready_file.exists())

    def test_ready_file_failure_closes_listener_without_claiming_success(self):
        created = []

        def create_server(*args):
            server = BackendHTTPServer(*args)
            created.append(server)
            return server

        with tempfile.TemporaryDirectory() as directory:
            missing = str(Path(directory) / "missing" / "ready.json")
            with patch("GalTransl.server.AGENT_REGISTRY", AgentRuntime()):
                with patch("GalTransl.server.BackendHTTPServer", side_effect=create_server):
                    with redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit) as caught:
                        serve(port=0, ready_file=missing)
        self.assertEqual(caught.exception.code, 1)
        self.assertNotIn("listening at", output.getvalue())
        self.assertEqual(created[0].socket.fileno(), -1)

    def test_duplicate_backend_exits_without_claiming_to_listen(self):
        # Reject a second backend, including an exclusive wildcard listener
        # and a legacy backend that reused the same loopback address.
        cases = (
            (BackendHTTPServer, "127.0.0.1"),
            (ThreadingHTTPServer, "127.0.0.1"),
            (BackendHTTPServer, "0.0.0.0"),
        )
        for server_type, host in cases:
            with self.subTest(server=server_type.__name__, host=host):
                with server_type((host, 0), ProbeHandler) as first:
                    worker = threading.Thread(target=first.serve_forever, daemon=True)
                    worker.start()
                    try:
                        port = first.server_port
                        result = subprocess.run(
                            [sys.executable, "run_backend.py", "--host", "127.0.0.1", "--port", str(port)],
                            cwd=Path(__file__).resolve().parents[1],
                            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                            capture_output=True,
                            text=True,
                            encoding="utf-8",
                            timeout=20,
                        )
                        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                        self.assertIn(str(port), result.stdout)
                        self.assertIn("[错误]", result.stdout)
                        self.assertTrue("已被占用" in result.stdout or "独占" in result.stdout, result.stdout)
                        self.assertNotIn("listening at", result.stdout + result.stderr)
                        self.assertNotIn("Traceback", result.stderr)

                        # Rejected startup must leave the existing server usable.
                        connection = HTTPConnection("127.0.0.1", port, timeout=5)
                        try:
                            connection.request("GET", "/")
                            response = connection.getresponse()
                            self.assertEqual(response.status, 200)
                            self.assertEqual(response.read(), b"original listener")
                        finally:
                            connection.close()
                    finally:
                        first.shutdown()
                        worker.join(timeout=5)

    def test_existing_backend_cannot_be_shared_by_legacy_reuse_listener(self):
        with BackendHTTPServer(("127.0.0.1", 0), ProbeHandler) as first:
            with self.assertRaises(OSError):
                with ThreadingHTTPServer(first.server_address, ProbeHandler):
                    pass

    def test_port_can_be_rebound_after_listener_closes(self):
        with BackendHTTPServer(("127.0.0.1", 0), ProbeHandler) as first:
            address = first.server_address
            worker = threading.Thread(target=first.serve_forever, daemon=True)
            worker.start()
            connection = HTTPConnection(*address, timeout=5)
            try:
                connection.request("GET", "/")
                self.assertEqual(connection.getresponse().read(), b"original listener")
            finally:
                connection.close()
                first.shutdown()
                worker.join(timeout=5)
        with BackendHTTPServer(address, ProbeHandler) as restarted:
            self.assertEqual(restarted.server_address, address)

    def test_bind_errors_are_reported_without_success_message(self):
        for code, expected in (
            (errno.EADDRINUSE, "已被占用"),
            (10048, "已被占用"),
            (errno.EACCES, "系统限制访问"),
            (10013, "系统限制访问"),
            (errno.EADDRNOTAVAIL, "无法绑定"),
        ):
            with self.subTest(code=code):
                output = io.StringIO()
                with patch("GalTransl.server.BackendHTTPServer", side_effect=OSError(code, "bind failed")):
                    with redirect_stdout(output), self.assertRaises(SystemExit) as caught:
                        serve()
                self.assertEqual(caught.exception.code, 1)
                self.assertIn(expected, output.getvalue())
                self.assertNotIn("listening at", output.getvalue())


if __name__ == "__main__":
    unittest.main()
