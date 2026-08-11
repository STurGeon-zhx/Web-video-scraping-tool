import json

import pytest

from douyin_downloader.kuaishou import (
    KuaishouExtractionError,
    KuaishouIE,
    anonymous_device_id_from_url,
    cache_kuaishou_info,
    parse_kuaishou_page,
    parse_kuaishou_mobile_page,
)


def test_parses_public_video_from_apollo_state() -> None:
    state = {
        "defaultClient": {
            "VisionVideoDetailPhoto:3abc": {
                "id": "3abc",
                "caption": "公开快手视频",
                "duration": 12000,
                "coverUrl": "https://cdn.example/cover.jpg",
                "photoUrl": "https://cdn.example/default.mp4",
                "videoResource": {
                    "type": "json",
                    "json": {
                        "h264": {
                            "adaptationSet": [
                                {
                                    "representation": [
                                        {
                                            "id": 2,
                                            "url": "https://cdn.example/1080.mp4",
                                            "width": 1920,
                                            "height": 1080,
                                            "avgBitrate": 3200000,
                                            "frameRate": 30,
                                        }
                                    ]
                                }
                            ]
                        }
                    },
                },
            }
        }
    }
    html = f"<script>window.__APOLLO_STATE__={json.dumps(state)};window.next=1</script>"

    info = parse_kuaishou_page(
        html,
        "3abc",
        "https://www.kuaishou.com/short-video/3abc",
    )

    assert info["id"] == "3abc"
    assert info["title"] == "公开快手视频"
    assert info["duration"] == 12.0
    assert info["formats"] == [
        {
            "format_id": "h264-2",
            "url": "https://cdn.example/1080.mp4",
            "ext": "mp4",
            "vcodec": "h264",
            "acodec": "aac",
            "width": 1920,
            "height": 1080,
            "tbr": 3200.0,
            "fps": 30,
        }
    ]


def test_rejects_kuaishou_page_without_public_video_state() -> None:
    with pytest.raises(KuaishouExtractionError, match="匿名访问"):
        parse_kuaishou_page("<html></html>", "3abc", "https://www.kuaishou.com/short-video/3abc")


def test_kuaishou_extractor_accepts_single_video_and_share_links_only() -> None:
    assert KuaishouIE.suitable("https://www.kuaishou.com/f/X109BY66mOaj1nh")
    assert KuaishouIE.suitable("https://www.kuaishou.com/short-video/3abc")
    assert not KuaishouIE.suitable("https://www.kuaishou.com/profile/author-id")


def test_uses_only_valid_public_device_id_from_share_redirect() -> None:
    assert anonymous_device_id_from_url(
        "https://www.kuaishou.com/short-video/3abc?ztDid=web_34b6228eb614433eab0a8ccd9050f2c5"
    ) == "web_34b6228eb614433eab0a8ccd9050f2c5"
    assert anonymous_device_id_from_url(
        "https://www.kuaishou.com/short-video/3abc?ztDid=not-valid"
    ) is None


def test_downloader_reuses_recent_public_metadata_without_second_request() -> None:
    info = {
        "id": "cached-photo",
        "title": "缓存视频",
        "formats": [{"url": "https://cdn.example/video.mp4", "vcodec": "h264"}],
    }
    cache_kuaishou_info("cached-photo", info)

    result = KuaishouIE()._real_extract(
        "https://www.kuaishou.com/short-video/cached-photo"
    )

    assert result == info
    assert result is not info


def test_parses_mobile_public_state_when_desktop_page_is_rate_limited() -> None:
    state = {
        "opaque-cache-key": {
            "result": 1,
            "photo": {
                "photoId": "3mobile",
                "caption": "移动公开视频",
                "duration": 57516,
                "coverUrls": [{"url": "https://cdn.example/cover.jpg"}],
                "manifest": {
                    "adaptationSet": [
                        {
                            "representation": [
                                {
                                    "id": 1,
                                    "url": "https://cdn.example/mobile.mp4",
                                    "videoCodec": "avc",
                                    "width": 720,
                                    "height": 1280,
                                    "avgBitrate": 4457,
                                    "frameRate": 60,
                                }
                            ]
                        }
                    ]
                },
            },
        }
    }
    html = f"<script>window.INIT_STATE = {json.dumps(state)};</script>"

    info = parse_kuaishou_mobile_page(
        html,
        "3mobile",
        "https://www.kuaishou.com/short-video/3mobile",
    )

    assert info["id"] == "3mobile"
    assert info["title"] == "移动公开视频"
    assert info["formats"][0]["url"] == "https://cdn.example/mobile.mp4"
    assert info["formats"][0]["vcodec"] == "h264"
