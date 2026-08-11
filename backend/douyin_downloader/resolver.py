from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import urljoin, urlsplit

import httpx

from .links import is_public_https_url, normalize_url


class LinkResolutionError(ValueError):
    pass


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
    if not is_public_https_url(url):
        raise LinkResolutionError("链接必须是公网 HTTPS 地址")
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
            response = await client.get(current, follow_redirects=False)
        except httpx.HTTPError as exc:
            raise LinkResolutionError(f"短链解析失败: {exc}") from exc
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
    raise LinkResolutionError("短链重定向次数过多")


resolve_douyin_url = resolve_public_url
