from douyin_downloader.links import extract_candidate_urls, normalize_url, preview_links


def test_extracts_direct_short_and_share_text_urls() -> None:
    text = """
    https://www.douyin.com/video/7667362613860371754
    复制此链接，打开抖音看看 https://v.douyin.com/AbC123/ 朋友分享
    """

    assert extract_candidate_urls(text) == [
        "https://www.douyin.com/video/7667362613860371754",
        "https://v.douyin.com/AbC123/",
    ]


def test_preview_accepts_public_http_and_https_candidates_and_deduplicates() -> None:
    text = "\n".join(
        [
            "https://www.douyin.com/video/1234567890123456789",
            "https://www.douyin.com/video/1234567890123456789",
            "http://www.douyin.com/video/2222222222222222222",
            "https://www.bilibili.com/video/BV1kdKr6qEMF/?spm_id_from=333.1007",
            "不是链接",
        ]
    )

    preview = preview_links(text)

    assert preview.valid_urls == [
        "https://www.douyin.com/video/1234567890123456789",
        "http://www.douyin.com/video/2222222222222222222",
        "https://www.bilibili.com/video/BV1kdKr6qEMF/",
    ]
    assert preview.duplicate_count == 1
    assert preview.invalid_count == 1


def test_normalize_url_preserves_public_http_scheme_and_default_port() -> None:
    assert normalize_url("http://VD3.BDSTATIC.COM:80/path/video.mp4") == (
        "http://vd3.bdstatic.com/path/video.mp4"
    )


def test_preview_rejects_local_and_private_ip_targets() -> None:
    preview = preview_links(
        "http://localhost/video.mp4\nhttps://127.0.0.1/video/123\nhttp://192.168.1.2/v.mp4"
    )

    assert preview.valid_urls == []
    assert preview.invalid_count == 3


def test_invalid_port_is_counted_as_invalid_instead_of_crashing() -> None:
    preview = preview_links("https://www.douyin.com:bad/video/1234567890123456789")

    assert preview.valid_urls == []
    assert preview.invalid_count == 1


def test_normalizes_youtube_single_urls_and_removes_tracking_parameters() -> None:
    assert normalize_url("https://youtu.be/BaW_jenozKc?si=tracking") == (
        "https://www.youtube.com/watch?v=BaW_jenozKc"
    )
    assert normalize_url(
        "https://www.youtube.com/watch?v=BaW_jenozKc&list=PL123&t=30"
    ) == "https://www.youtube.com/watch?v=BaW_jenozKc"


def test_normalizes_youtube_channel_root_to_videos_page() -> None:
    assert normalize_url("https://youtube.com/@example") == (
        "https://www.youtube.com/@example/videos"
    )
