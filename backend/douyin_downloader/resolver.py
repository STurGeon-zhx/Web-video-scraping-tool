from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass
from email.message import Message
from urllib.parse import urljoin, urlsplit

import httpx

from .links import is_public_http_url, normalize_url


class LinkResolutionError(ValueError):
    pass


@dataclass(slots=True, frozen=True)
class DirectMediaProbe:
    final_url: str
    content_type: str
    content_length: int | None
    filename: str | None


def _resolve_host(hostname: str) -> list[str]:
    return list({item[4][0] for item in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)})


def _is_public_address(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False


def _validate_public_target(
    url: str,
    dns_resolver: Callable[[str], list[str]],
) -> None:
    if not is_public_http_url(url):
        raise LinkResolutionError("链接必须是公网 HTTP/HTTPS 地址")
    hostname = urlsplit(url).hostname or ""
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        try:
            addresses = dns_resolver(hostname)
        except OSError as exc:
            raise LinkResolutionError(f"域名解析失败: {exc}") from exc
        if not addresses or not all(_is_public_address(address) for address in addresses):
            raise LinkResolutionError("链接域名未解析到安全的公网地址")


async def resolve_public_url(
    url: str,
    client: httpx.AsyncClient,
    max_redirects: int = 5,
    dns_resolver: Callable[[str], list[str]] = _resolve_host,
) -> str:
    current = url
    for hop in range(max_redirects + 1):
        _validate_public_target(current, dns_resolver)
        try:
            request = client.build_request("GET", current)
            response = await client.send(request, follow_redirects=False, stream=True)
        except httpx.HTTPError as exc:
            raise LinkResolutionError(f"短链解析失败: {exc}") from exc
        try:
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise LinkResolutionError("重定向响应缺少目标地址")
                if hop >= max_redirects:
                    raise LinkResolutionError("短链重定向次数过多")
                next_url = urljoin(current, location)
                _validate_public_target(next_url, dns_resolver)
                current = next_url
                continue
            if response.status_code >= 400:
                raise LinkResolutionError(f"链接访问失败: HTTP {response.status_code}")
            return normalize_url(current)
        finally:
            await response.aclose()
    raise LinkResolutionError("短链重定向次数过多")


def _content_disposition_filename(value: str | None) -> str | None:
    if not value:
        return None
    message = Message()
    message["content-disposition"] = value
    filename = message.get_filename()
    return str(filename).strip() if filename else None


def _content_length(headers: httpx.Headers) -> int | None:
    value = headers.get("content-length")
    if value and value.isdigit():
        return int(value)
    content_range = headers.get("content-range") or ""
    total = content_range.rpartition("/")[2]
    return int(total) if total.isdigit() else None


def probe_public_video_url(
    url: str,
    client: httpx.Client | None = None,
    max_redirects: int = 5,
    dns_resolver: Callable[[str], list[str]] = _resolve_host,
) -> DirectMediaProbe:
    owned_client = client is None
    active_client = client or httpx.Client(
        timeout=15,
        headers={"User-Agent": "Mozilla/5.0 VideoBatchDownloader/0.2"},
    )
    current = url
    redirects = 0
    method = "HEAD"
    try:
        while True:
            _validate_public_target(current, dns_resolver)
            headers = {"Range": "bytes=0-4095"} if method == "GET" else None
            try:
                with active_client.stream(
                    method,
                    current,
                    headers=headers,
                    follow_redirects=False,
                ) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise LinkResolutionError("重定向响应缺少目标地址")
                        if redirects >= max_redirects:
                            raise LinkResolutionError("短链重定向次数过多")
                        next_url = urljoin(current, location)
                        _validate_public_target(next_url, dns_resolver)
                        current = next_url
                        redirects += 1
                        continue

                    content_type = response.headers.get("content-type", "").partition(";")[0].strip().lower()
                    if method == "HEAD" and (
                        response.status_code >= 400 or not content_type.startswith("video/")
                    ):
                        method = "GET"
                        continue
                    if response.status_code >= 400:
                        raise LinkResolutionError(f"视频直链访问失败: HTTP {response.status_code}")
                    if not content_type.startswith("video/"):
                        raise LinkResolutionError("链接响应不是可下载的视频直链")
                    return DirectMediaProbe(
                        final_url=normalize_url(current),
                        content_type=content_type,
                        content_length=_content_length(response.headers),
                        filename=_content_disposition_filename(
                            response.headers.get("content-disposition")
                        ),
                    )
            except LinkResolutionError:
                raise
            except httpx.HTTPError as exc:
                raise LinkResolutionError(f"视频直链访问失败: {exc}") from exc
    finally:
        if owned_client:
            active_client.close()


resolve_douyin_url = resolve_public_url
