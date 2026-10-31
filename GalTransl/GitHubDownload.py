"""Bounded archive downloads using the node list published by github.akams.cn."""
from __future__ import annotations

import hashlib
import io
import re
import time
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from typing import BinaryIO, Callable
from urllib.parse import urlsplit

import requests

# The site's current frontend ships this configuration rather than a node API.
NODE_LIST_URL = "https://cdn.akams.cn/jsd/gh/oopsunix/ghproxy-next@main/components/nodes.ts"
HEADERS = {"User-Agent": "GalTransl-GitHub-Download", "Accept": "application/octet-stream"}
ARCHIVE_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06", b"\x1f\x8b")


class GitHubDownloadError(requests.RequestException):
    pass


def download_archive(url: str, output: BinaryIO, maximum: int, *, sha256: str = "",
                     progress: Callable[[int], None] | None = None) -> str:
    failures = []
    for candidate in download_candidates(url):
        output.seek(0)
        output.truncate()
        if progress:
            progress(0)
        digest = hashlib.sha256()
        total = 0
        try:
            with requests.get(candidate, headers=HEADERS, stream=True, timeout=(5, 30)) as response:
                response.raise_for_status()
                for chunk in response.iter_content(256 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > maximum:
                        raise GitHubDownloadError("GitHub download exceeds its size budget")
                    output.write(chunk)
                    digest.update(chunk)
                    if progress:
                        progress(total)
            if not total:
                raise GitHubDownloadError("GitHub returned an empty archive")
            output.seek(0)
            if not output.read(4).startswith(ARCHIVE_SIGNATURES):
                raise GitHubDownloadError("GitHub download did not return an archive")
            if sha256 and (total != maximum or digest.hexdigest() != sha256):
                raise GitHubDownloadError("Release download failed SHA256 verification; the installed application was not changed")
            output.seek(0)
            return candidate
        except requests.RequestException as exc:
            failures.append(f"{urlsplit(candidate).netloc}: {exc}")
    raise GitHubDownloadError("Could not download GitHub archive: " + "; ".join(failures))


def download_archive_bytes(url: str, maximum: int) -> bytes:
    with io.BytesIO() as output:
        download_archive(url, output, maximum)
        return output.getvalue()


def download_candidates(url: str) -> list[str]:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in {"github.com", "codeload.github.com"} or parsed.username or parsed.password:
        return [url]
    try:
        nodes = _proxy_nodes(int(time.time() // 3600))
    except (requests.RequestException, UnicodeError, ValueError):
        return [url]
    # Most listed proxies accept github.com archive links, not codeload URLs.
    archive = re.sub(r"^https://codeload\.github\.com/([^/]+/[^/]+)/zip/(.+)$",
                     r"https://github.com/\1/archive/\2.zip", url)
    candidates = [node + "/" + archive for node in nodes[:6]]
    with ThreadPoolExecutor(max_workers=7) as workers:
        measured = list(workers.map(_probe_archive, [*candidates, url]))
    healthy = [candidate for _, candidate in sorted(result for result in measured if result is not None)]
    # A failed Range probe does not prevent the final full direct request.
    return list(dict.fromkeys([*healthy[:3], url]))


@lru_cache(maxsize=1)
def _proxy_nodes(hour: int) -> list[str]:
    with requests.get(NODE_LIST_URL, headers=HEADERS, stream=True, timeout=(3, 5)) as response:
        response.raise_for_status()
        content = bytearray()
        for chunk in response.iter_content(8192):
            content.extend(chunk)
            if len(content) > 128 * 1024:
                raise ValueError("GitHub proxy list exceeds its size budget")
    nodes = _parse_proxy_nodes(content.decode("utf-8"))
    if not nodes:
        raise ValueError("GitHub proxy list contains no valid nodes")
    return nodes


def _parse_proxy_nodes(source: str) -> list[str]:
    nodes = []
    for value in re.findall(r"\bvalue\s*:\s*[\"']([^\"']+)[\"']", source):
        try:
            host = value.encode("idna").decode("ascii").lower()
        except UnicodeError:
            continue
        if re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z][a-z0-9-]*", host):
            nodes.append("https://" + host)
    return list(dict.fromkeys(nodes))


def _probe_archive(url: str) -> tuple[float, str] | None:
    started = time.monotonic()
    try:
        with requests.get(url, headers={**HEADERS, "Range": "bytes=0-4095"}, stream=True, timeout=(3, 5)) as response:
            response.raise_for_status()
            beginning = bytearray()
            for chunk in response.iter_content(4):
                beginning.extend(chunk)
                if len(beginning) >= 4:
                    break
        return (time.monotonic() - started, url) if beginning.startswith(ARCHIVE_SIGNATURES) else None
    except requests.RequestException:
        return None
