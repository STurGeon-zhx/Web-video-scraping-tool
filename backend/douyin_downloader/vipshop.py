from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Callable

from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.utils import ExtractorError


VIPSHOP_DETAIL_PATTERN = re.compile(
    r"^https://detail\.vip\.com/detail-(?P<brand_id>\d+)-(?P<id>\d+)\.html(?:[?#].*)?$"
)


class VipshopExtractionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class VipshopVideoInfo:
    product_id: str
    title: str
    video_url: str
    thumbnail: str | None
    webpage_url: str


def _absolute_https_url(value: Any) -> str:
    url = str(value or "").strip()
    if url.startswith("//"):
        return f"https:{url}"
    return url


def parse_vipshop_payload(
    payload: dict[str, Any],
    product_id: str,
    webpage_url: str,
) -> VipshopVideoInfo:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise VipshopExtractionError("唯品会商品详情响应格式异常")
    returned_id = str(data.get("productId") or data.get("merchandiseId") or "")
    if returned_id and returned_id != product_id:
        raise VipshopExtractionError("唯品会商品详情与链接不匹配")
    base = data.get("base")
    if not isinstance(base, dict):
        raise VipshopExtractionError("唯品会商品详情响应格式异常")
    video_url = _absolute_https_url(base.get("shortVideoUrl"))
    if not video_url:
        raise VipshopExtractionError("该唯品会商品没有可下载的主视频")
    title = str(
        base.get("productName")
        or base.get("merchandiseName")
        or base.get("goodsName")
        or ""
    ).strip()
    thumbnail = _absolute_https_url(
        base.get("smallImage") or base.get("coverImage") or base.get("imageUrl")
    )
    return VipshopVideoInfo(
        product_id=product_id,
        title=title,
        video_url=video_url,
        thumbnail=thumbnail or None,
        webpage_url=webpage_url,
    )


def resolve_vipshop_page(
    url: str,
    product_id: str,
    *,
    playwright_factory: Callable[[], Any] | None = None,
    media_probe: Callable[[str], Any] | None = None,
) -> VipshopVideoInfo:
    if playwright_factory is None:
        from playwright.sync_api import sync_playwright

        playwright_factory = sync_playwright
    if media_probe is None:
        from .resolver import probe_public_video_url

        media_probe = probe_public_video_url

    browser: Any = None
    context: Any = None
    payloads: list[dict[str, Any]] = []
    try:
        with playwright_factory() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            context = browser.new_context()
            page = context.new_page()

            def capture(response: Any) -> None:
                if "/shopping/pc/detail/main/v6" not in str(response.url):
                    return
                try:
                    payload = response.json()
                except Exception:
                    return
                if isinstance(payload, dict):
                    payloads.append(payload)

            page.on("response", capture)
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            for _ in range(40):
                if payloads:
                    break
                page.wait_for_timeout(250)
            if not payloads:
                raise VipshopExtractionError("唯品会匿名访问失败，请稍后重试")

            last_error: VipshopExtractionError | None = None
            info: VipshopVideoInfo | None = None
            for payload in payloads:
                try:
                    info = parse_vipshop_payload(payload, product_id, url)
                    break
                except VipshopExtractionError as exc:
                    last_error = exc
            if info is None:
                raise last_error or VipshopExtractionError(
                    "唯品会匿名访问失败，请稍后重试"
                )
            probe = media_probe(info.video_url)
            return replace(info, video_url=str(probe.final_url))
    except VipshopExtractionError:
        raise
    except Exception as exc:
        raise VipshopExtractionError(
            f"唯品会匿名访问失败，请稍后重试: {exc}"
        ) from exc
    finally:
        if context is not None:
            context.close()
        if browser is not None:
            browser.close()


Resolver = Callable[[str, str], VipshopVideoInfo]


class VipshopIE(InfoExtractor):
    IE_NAME = "vipshop"
    _VALID_URL = VIPSHOP_DETAIL_PATTERN.pattern

    def __init__(
        self,
        downloader: Any = None,
        *,
        resolver: Resolver | None = None,
    ) -> None:
        super().__init__(downloader)
        self._resolver = resolver or resolve_vipshop_page

    def _real_extract(self, url: str) -> dict[str, Any]:
        product_id = self._match_id(url)
        try:
            item = self._resolver(url, product_id)
        except VipshopExtractionError as exc:
            raise ExtractorError(str(exc), expected=True) from exc
        return {
            "id": item.product_id,
            "title": item.title,
            "url": item.video_url,
            "ext": "mp4",
            "vcodec": "h264",
            "acodec": "aac",
            "thumbnail": item.thumbnail,
            "webpage_url": item.webpage_url,
            "http_headers": {"Referer": item.webpage_url},
            "extractor_key": "Vipshop",
        }
