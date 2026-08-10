from pathlib import Path

from douyin_downloader.filenames import build_unique_output_path, sanitize_title


def test_sanitize_title_removes_windows_invalid_suffix_and_truncates() -> None:
    title = '  测试<>:"/\\|?*标题...  ' + "长" * 200

    result = sanitize_title(title, max_length=40)

    assert result == "测试_________标题... " + "长" * 23
    assert len(result) == 40


def test_empty_title_falls_back_to_video_id() -> None:
    assert sanitize_title(" . ", video_id="123456789") == "抖音视频_123456789"


def test_windows_reserved_device_name_is_made_safe() -> None:
    assert sanitize_title("CON") == "_CON"
    assert sanitize_title("aux.txt") == "_aux.txt"


def test_unique_output_path_never_overwrites_existing_file(tmp_path: Path) -> None:
    (tmp_path / "同名标题.mp4").write_bytes(b"first")
    (tmp_path / "同名标题 (2).mp4").write_bytes(b"second")

    result = build_unique_output_path(tmp_path, "同名标题", "mp4")

    assert result == tmp_path / "同名标题 (3).mp4"
