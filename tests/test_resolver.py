import httpx
import pytest

from douyin_downloader.resolver import LinkResolutionError, resolve_douyin_url


@pytest.mark.asyncio
async def test_resolves_allowed_short_link_chain() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "v.douyin.com":
            return httpx.Response(
                302,
                headers={"location": "https://www.douyin.com/video/1234567890123456789"},
            )
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await resolve_douyin_url("https://v.douyin.com/ABC123/", client)

    assert result == "https://www.douyin.com/video/1234567890123456789"


@pytest.mark.asyncio
async def test_rejects_redirect_to_non_douyin_domain_before_requesting_it() -> None:
    requested_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_hosts.append(request.url.host)
        return httpx.Response(302, headers={"location": "https://evil.example/steal"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(LinkResolutionError, match="白名单"):
            await resolve_douyin_url("https://v.douyin.com/ABC123/", client)

    assert requested_hosts == ["v.douyin.com"]


@pytest.mark.asyncio
async def test_rejects_redirect_loops_after_five_hops() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://v.douyin.com/again"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(LinkResolutionError, match="次数过多"):
            await resolve_douyin_url("https://v.douyin.com/ABC123/", client)
