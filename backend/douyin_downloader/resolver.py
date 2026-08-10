from __future__ import annotations

from urllib.parse import urljoin

import httpx

from .links import is_allowed_douyin_url, normalize_url


class LinkResolutionError(ValueError):
    pass


async def resolve_douyin_url(
    url: str,
    client: httpx.AsyncClient,
    max_redirects: int = 5,
) -> str:
    current = url
    for hop in range(max_redirects + 1):
        if not is_allowed_douyin_url(current):
            raise LinkResolutionError("重定向目标不在抖音域名白名单内")
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
            if not is_allowed_douyin_url(next_url):
                raise LinkResolutionError("重定向目标不在抖音域名白名单内")
            current = next_url
            continue
        if response.status_code >= 400:
            raise LinkResolutionError(f"链接访问失败: HTTP {response.status_code}")
        return normalize_url(current)
    raise LinkResolutionError("短链重定向次数过多")
