from __future__ import annotations

import pytest

from douyin_downloader.vipshop import (
    VipshopExtractionError,
    VipshopIE,
    VipshopVideoInfo,
    parse_vipshop_payload,
    resolve_vipshop_page,
)
from douyin_downloader.resolver import DirectMediaProbe


PRODUCT_ID = "6921967044893585744"
PRODUCT_URL = f"https://detail.vip.com/detail-10007920-{PRODUCT_ID}.html"


def test_vipshop_extractor_matches_product_detail_only() -> None:
    assert VipshopIE.suitable(f"{PRODUCT_URL}?")
    assert not VipshopIE.suitable("https://www.vip.com/")
    assert not VipshopIE.suitable("https://detail.vip.com/comment/123")


def test_parse_vipshop_payload_rejects_product_without_main_video() -> None:
    payload = {"code": 1, "data": {"productId": PRODUCT_ID, "base": {}}}

    with pytest.raises(VipshopExtractionError, match="没有可下载的主视频"):
        parse_vipshop_payload(payload, PRODUCT_ID, PRODUCT_URL)


def test_parse_vipshop_payload_uses_only_product_main_video() -> None:
    payload = {
        "code": 1,
        "data": {
            "productId": PRODUCT_ID,
            "base": {
                "productName": "测试商品",
                "shortVideoUrl": "//a.vpimg4.com/upload/merchandise/video/main.mp4",
                "smallImage": "//a.vpimg4.com/upload/merchandise/cover.jpg",
            },
            "comments": [{"videoUrl": "https://review.example/review.mp4"}],
        },
    }

    info = parse_vipshop_payload(payload, PRODUCT_ID, PRODUCT_URL)

    assert info == VipshopVideoInfo(
        product_id=PRODUCT_ID,
        title="测试商品",
        video_url="https://a.vpimg4.com/upload/merchandise/video/main.mp4",
        thumbnail="https://a.vpimg4.com/upload/merchandise/cover.jpg",
        webpage_url=PRODUCT_URL,
    )


def test_vipshop_extractor_returns_downloadable_video_info() -> None:
    resolved = VipshopVideoInfo(
        product_id=PRODUCT_ID,
        title="测试商品",
        video_url="https://a.vpimg4.com/upload/merchandise/video/main.mp4",
        thumbnail="https://a.vpimg4.com/upload/merchandise/cover.jpg",
        webpage_url=PRODUCT_URL,
    )
    extractor = VipshopIE(resolver=lambda _url, _product_id: resolved)

    info = extractor._real_extract(PRODUCT_URL)

    assert info["id"] == PRODUCT_ID
    assert info["title"] == "测试商品"
    assert info["url"] == resolved.video_url
    assert info["webpage_url"] == PRODUCT_URL
    assert info["http_headers"] == {"Referer": PRODUCT_URL}
    assert info["extractor_key"] == "Vipshop"


class FakeResponse:
    url = "https://mapi-pc.vip.com/vips-mobile/rest/shopping/pc/detail/main/v6"

    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def json(self) -> dict[str, object]:
        return self._payload


class FakePage:
    def __init__(self, payload: dict[str, object] | None) -> None:
        self.payload = payload
        self.response_handler = None
        self.goto_args: tuple[str, str, int] | None = None

    def on(self, event: str, handler: object) -> None:
        assert event == "response"
        self.response_handler = handler

    def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        self.goto_args = (url, wait_until, timeout)
        if self.payload is not None and self.response_handler is not None:
            self.response_handler(FakeResponse(self.payload))

    def wait_for_timeout(self, _milliseconds: int) -> None:
        return None

    def content(self) -> str:
        return "<html><body>商品详情</body></html>"


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.closed = False

    def new_page(self) -> FakePage:
        return self.page

    def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self, context: FakeContext) -> None:
        self.context = context
        self.new_context_kwargs: dict[str, object] | None = None
        self.closed = False

    def new_context(self, **kwargs: object) -> FakeContext:
        self.new_context_kwargs = kwargs
        return self.context

    def close(self) -> None:
        self.closed = True


class FakeChromium:
    def __init__(self, browser: FakeBrowser) -> None:
        self.browser = browser
        self.launch_kwargs: dict[str, object] | None = None

    def launch(self, **kwargs: object) -> FakeBrowser:
        self.launch_kwargs = kwargs
        return self.browser


class FakePlaywright:
    def __init__(self, chromium: FakeChromium) -> None:
        self.chromium = chromium

    def __enter__(self) -> "FakePlaywright":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def make_fake_playwright(
    payload: dict[str, object] | None,
) -> tuple[object, FakeChromium, FakeBrowser, FakeContext, FakePage]:
    page = FakePage(payload)
    context = FakeContext(page)
    browser = FakeBrowser(context)
    chromium = FakeChromium(browser)
    playwright = FakePlaywright(chromium)
    return lambda: playwright, chromium, browser, context, page


def test_resolve_vipshop_page_uses_clean_edge_and_validates_media() -> None:
    payload = {
        "code": 1,
        "data": {
            "productId": PRODUCT_ID,
            "base": {
                "productName": "测试商品",
                "shortVideoUrl": "//a.vpimg4.com/original.mp4",
            },
        },
    }
    factory, chromium, browser, context, page = make_fake_playwright(payload)

    info = resolve_vipshop_page(
        PRODUCT_URL,
        PRODUCT_ID,
        playwright_factory=factory,
        media_probe=lambda _url: DirectMediaProbe(
            final_url="https://a.vpimg4.com/final.mp4",
            content_type="video/mp4",
            content_length=1024,
            filename=None,
        ),
    )

    assert chromium.launch_kwargs == {"channel": "msedge", "headless": True}
    assert browser.new_context_kwargs == {}
    assert page.goto_args == (PRODUCT_URL, "domcontentloaded", 30_000)
    assert info.video_url == "https://a.vpimg4.com/final.mp4"
    assert context.closed is True
    assert browser.closed is True


def test_resolve_vipshop_page_closes_browser_when_api_has_no_response() -> None:
    factory, _chromium, browser, context, _page = make_fake_playwright(None)

    with pytest.raises(VipshopExtractionError, match="匿名访问失败"):
        resolve_vipshop_page(
            PRODUCT_URL,
            PRODUCT_ID,
            playwright_factory=factory,
            media_probe=lambda _url: pytest.fail("不应探测媒体"),
        )

    assert context.closed is True
    assert browser.closed is True
