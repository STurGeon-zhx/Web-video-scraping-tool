from douyin_downloader.links import extract_candidate_urls, preview_links


def test_extracts_direct_short_and_share_text_urls() -> None:
    text = """
    https://www.douyin.com/video/7667362613860371754
    复制此链接，打开抖音看看 https://v.douyin.com/AbC123/ 朋友分享
    """

    assert extract_candidate_urls(text) == [
        "https://www.douyin.com/video/7667362613860371754",
        "https://v.douyin.com/AbC123/",
    ]


def test_preview_rejects_non_douyin_and_insecure_urls_and_deduplicates() -> None:
    text = "\n".join(
        [
            "https://www.douyin.com/video/1234567890123456789",
            "https://www.douyin.com/video/1234567890123456789",
            "http://www.douyin.com/video/2222222222222222222",
            "https://example.com/video/123",
            "不是链接",
        ]
    )

    preview = preview_links(text)

    assert preview.valid_urls == [
        "https://www.douyin.com/video/1234567890123456789"
    ]
    assert preview.duplicate_count == 1
    assert preview.invalid_count == 3


def test_does_not_accept_douyin_lookalike_domain() -> None:
    preview = preview_links("https://douyin.com.evil.example/video/123")

    assert preview.valid_urls == []
    assert preview.invalid_count == 1


def test_invalid_port_is_counted_as_invalid_instead_of_crashing() -> None:
    preview = preview_links("https://www.douyin.com:bad/video/1234567890123456789")

    assert preview.valid_urls == []
    assert preview.invalid_count == 1
