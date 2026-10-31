"""Exercise proxy discovery and archive failover with real local HTTP requests."""
import hashlib
import io
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from GalTransl import GitHubDownload


@pytest.fixture
def archive_server(monkeypatch):
    responses = {}
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append((self.path, self.headers.get("Range")))
            status, content, mode = responses.get(self.path, (404, b"", ""))
            self.send_response(status)
            if mode == "chunked":
                self.send_header("Transfer-Encoding", "chunked")
            else:
                self.send_header("Content-Length", str(len(content) + (10 if mode == "truncated" else 0)))
            self.end_headers()
            try:
                if mode == "chunked":
                    for value in content:
                        self.wfile.write(b"1\r\n" + bytes([value]) + b"\r\n")
                    self.wfile.write(b"0\r\n\r\n")
                    return
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    worker.start()
    cached_nodes = GitHubDownload._proxy_nodes
    cached_nodes.cache_clear()
    url = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setattr(GitHubDownload, "NODE_LIST_URL", url + "/nodes")
    yield url, responses, calls
    cached_nodes.cache_clear()
    server.shutdown()
    server.server_close()
    worker.join(timeout=2)


@pytest.fixture
def archive():
    with io.BytesIO() as output:
        with zipfile.ZipFile(output, "w") as bundle:
            bundle.writestr("package/data.txt", b"archive payload" * 22000)
        return output.getvalue()


def test_node_source_is_bounded_filtered_and_cached_for_one_hour(archive_server):
    _, responses, calls = archive_server
    source = """export const PROXY_NODES = [
        { value: 'GH-Proxy.com' }, { value: "gh-proxy.com" },
        { value: 'another.example' }, { value: '中文.example' },
        { value: 'https://bad.example' }, { value: 'bad.example/path' },
        { value: 'user@bad.example' }, { value: 'bad.example:443' },
        { value: '127.0.0.1' }, { value: 'localhost' },
        { value: '-bad.example' }, { value: 'bad_.example' }
    ];"""
    responses["/nodes"] = (200, source.encode(), "")
    assert GitHubDownload._proxy_nodes(1) == [
        "https://gh-proxy.com", "https://another.example", "https://xn--fiq228c.example"]
    assert GitHubDownload._proxy_nodes(1)[0] == "https://gh-proxy.com"
    assert len(calls) == 1
    responses["/nodes"] = (200, b"{value: 'new.example'}", "")
    assert GitHubDownload._proxy_nodes(2) == ["https://new.example"]
    assert len(calls) == 2


@pytest.mark.parametrize("status,content", [
    (503, b"unavailable"), (200, b"<html>error</html>"),
    (200, b"x" * (128 * 1024 + 1)), (200, b"\xff")],
    ids=["http-error", "invalid-list", "size-budget", "encoding"])
def test_failed_node_discovery_falls_back_to_direct_and_can_retry(archive_server, status, content):
    _, responses, calls = archive_server
    url = "https://github.com/owner/repo/releases/download/v1/archive.zip"
    responses["/nodes"] = (status, content, "")
    assert GitHubDownload.download_candidates(url) == [url]
    responses["/nodes"] = (200, b"{value: 'working.example'}", "")
    assert GitHubDownload._proxy_nodes(int(GitHubDownload.time.time() // 3600)) == ["https://working.example"]
    assert len(calls) == 2


@pytest.mark.parametrize("status,content,mode,valid", [
    (200, b"PK\x03\x04content", "", True),
    (206, b"PK\x03\x04content", "chunked", True),
    (206, b"\x1f\x8bcontent", "", True),
    (200, b"<html>error</html>", "", False),
    (503, b"PK\x03\x04content", "", False),
    (200, b"", "", False)],
    ids=["zip", "fragmented-range", "gzip", "html", "http-error", "empty"])
def test_probe_reads_archive_signature_and_requests_only_a_range(archive_server, status, content, mode, valid):
    url, responses, calls = archive_server
    responses["/archive"] = (status, content, mode)
    result = GitHubDownload._probe_archive(url + "/archive")
    assert (result is not None) == valid
    assert calls == [("/archive", "bytes=0-4095")]


def test_selection_probes_proxies_with_github_archive_links_and_retains_direct_fallback(archive_server, monkeypatch):
    server_url, responses, calls = archive_server
    original = "https://codeload.github.com/owner/repo/zip/" + "a" * 40
    converted = "https://github.com/owner/repo/archive/" + "a" * 40 + ".zip"
    nodes = ["https://first.example", "https://second.example", "https://broken.example"]
    monkeypatch.setattr(GitHubDownload, "_proxy_nodes", lambda hour: nodes)
    probe = GitHubDownload._probe_archive
    monkeypatch.setattr(GitHubDownload, "_probe_archive", lambda url: (
        (result[0], url) if (result := probe(server_url + "/" + url)) else None))
    responses["/" + nodes[0] + "/" + converted] = (200, b"PK\x03\x04", "")
    responses["/" + nodes[1] + "/" + converted] = (200, b"PK\x03\x04", "")
    candidates = GitHubDownload.download_candidates(original)
    assert set(candidates[:-1]) == {nodes[0] + "/" + converted, nodes[1] + "/" + converted}
    assert candidates[-1] == original
    assert len(calls) == 4 and all(value == "bytes=0-4095" for _, value in calls)


@pytest.mark.parametrize("url", [
    "https://api.github.com/repos/owner/repo/releases/latest",
    "https://downloads.example/archive.zip", "http://github.com/archive.zip",
    "https://user:password@github.com/archive.zip", "https://github.com.evil.example/archive.zip"])
def test_other_urls_bypass_node_discovery(archive_server, url):
    assert GitHubDownload.download_candidates(url) == [url]
    assert archive_server[2] == []


@pytest.mark.parametrize("failure", ["http", "html", "empty", "truncated", "oversize", "checksum", "undersize"])
def test_failed_routes_reset_output_and_progress_before_verified_fallback(archive_server, archive, monkeypatch, failure):
    url, responses, calls = archive_server
    failed = {
        "http": (503, b"unavailable", ""), "html": (200, b"<html>error</html>", ""),
        "empty": (200, b"", ""), "truncated": (200, archive, "truncated"),
        "oversize": (200, archive + b"extra", ""),
        "checksum": (200, archive[:-1] + bytes([archive[-1] ^ 1]), ""),
        "undersize": (200, archive[:-1], ""),
    }
    responses["/proxy"] = failed[failure]
    responses["/direct"] = (200, archive, "")
    monkeypatch.setattr(GitHubDownload, "download_candidates", lambda original: [url + "/proxy", url + "/direct"])
    progress = []
    with io.BytesIO(b"leftover file data") as output:
        selected = GitHubDownload.download_archive(url, output, len(archive),
                     sha256=hashlib.sha256(archive).hexdigest(), progress=progress.append)
        assert selected == url + "/direct"
        assert output.tell() == 0 and output.read() == archive
    assert calls == [("/proxy", None), ("/direct", None)]
    assert progress.count(0) == 2 and progress[-1] == len(archive)


def test_all_failed_routes_report_error_and_no_archive(archive_server, monkeypatch):
    url, responses, calls = archive_server
    responses["/proxy"] = (200, b"<html>error</html>", "")
    responses["/direct"] = (503, b"unavailable", "")
    monkeypatch.setattr(GitHubDownload, "download_candidates", lambda original: [url + "/proxy", url + "/direct"])
    with pytest.raises(GitHubDownload.GitHubDownloadError, match="Could not download GitHub archive"):
        GitHubDownload.download_archive_bytes(url, 1024)
    assert len(calls) == 2
