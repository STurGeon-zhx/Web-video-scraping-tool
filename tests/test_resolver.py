import httpx
import pytest

from douyin_downloader.resolver import (
    DirectMediaProbe,
    LinkResolutionError,
    probe_public_video_url,
    resolve_public_url,
)


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
        result = await resolve_public_url(
            "https://v.douyin.com/ABC123/",
            client,
            dns_resolver=lambda _host: ["8.8.8.8"],
        )

    assert result == "https://www.douyin.com/video/1234567890123456789"


@pytest.mark.asyncio
async def test_rejects_redirect_to_private_network_before_requesting_it() -> None:
    requested_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_hosts.append(request.url.host)
        return httpx.Response(302, headers={"location": "https://127.0.0.1/steal"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(LinkResolutionError, match="公网"):
            await resolve_public_url(
                "https://v.douyin.com/ABC123/",
                client,
                dns_resolver=lambda _host: ["8.8.8.8"],
            )

    assert requested_hosts == ["v.douyin.com"]


@pytest.mark.asyncio
async def test_rejects_redirect_loops_after_five_hops() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://v.douyin.com/again"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(LinkResolutionError, match="次数过多"):
            await resolve_public_url(
                "https://v.douyin.com/ABC123/",
                client,
                dns_resolver=lambda _host: ["8.8.8.8"],
            )


@pytest.mark.asyncio
async def test_resolves_public_http_url_without_upgrading_scheme() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200))
    ) as client:
        result = await resolve_public_url(
            "http://cdn.example/video.mp4",
            client,
            dns_resolver=lambda _host: ["8.8.8.8"],
        )

    assert result == "http://cdn.example/video.mp4"


def test_probe_accepts_public_video_head_response() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-type": "video/mp4",
                "content-length": "3338956",
                "content-disposition": "attachment; filename*=UTF-8''clip%20name.mp4",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = probe_public_video_url(
            "http://cdn.example/video.mp4",
            client=client,
            dns_resolver=lambda _host: ["8.8.8.8"],
        )

    assert result == DirectMediaProbe(
        final_url="http://cdn.example/video.mp4",
        content_type="video/mp4",
        content_length=3338956,
        filename="clip name.mp4",
    )


def test_probe_falls_back_to_ranged_get_when_head_is_not_supported() -> None:
    requests: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.headers.get("range")))
        if request.method == "HEAD":
            return httpx.Response(405)
        return httpx.Response(206, headers={"content-type": "video/webm"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = probe_public_video_url(
            "https://cdn.example/video",
            client=client,
            dns_resolver=lambda _host: ["8.8.8.8"],
        )

    assert result.content_type == "video/webm"
    assert requests == [("HEAD", None), ("GET", "bytes=0-4095")]


def test_probe_rejects_private_redirect_before_requesting_target() -> None:
    requested_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_hosts.append(request.url.host)
        return httpx.Response(302, headers={"location": "http://127.0.0.1/video.mp4"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(LinkResolutionError, match="公网"):
            probe_public_video_url(
                "http://cdn.example/video.mp4",
                client=client,
                dns_resolver=lambda _host: ["8.8.8.8"],
            )

    assert requested_hosts == ["cdn.example"]


def test_probe_rejects_non_video_response() -> None:
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, headers={"content-type": "text/html"})
        )
    ) as client:
        with pytest.raises(LinkResolutionError, match="不是可下载的视频直链"):
            probe_public_video_url(
                "https://example.com/page",
                client=client,
                dns_resolver=lambda _host: ["8.8.8.8"],
            )


def test_probe_accepts_public_hls_manifest() -> None:
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                headers={"content-type": "application/vnd.apple.mpegurl"},
            )
        )
    ) as client:
        result = probe_public_video_url(
            "https://cdn.example/master.m3u8",
            client=client,
            dns_resolver=lambda _host: ["8.8.8.8"],
        )

    assert result.content_type == "application/vnd.apple.mpegurl"
