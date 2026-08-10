from __future__ import annotations

import re
from pathlib import Path


INVALID_WINDOWS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def sanitize_title(title: str, video_id: str | None = None, max_length: int = 120) -> str:
    cleaned = INVALID_WINDOWS_CHARS.sub("_", title.strip())
    cleaned = re.sub(r"\s+", " ", cleaned).rstrip(" .")
    cleaned = cleaned[:max_length].rstrip(" .")
    if cleaned:
        if cleaned.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
            cleaned = f"_{cleaned}"
        return cleaned
    return f"抖音视频_{video_id or '未知'}"


def build_unique_output_path(directory: Path, title: str, extension: str) -> Path:
    safe_title = sanitize_title(title)
    safe_extension = extension.lstrip(".") or "mp4"
    candidate = directory / f"{safe_title}.{safe_extension}"
    suffix = 2
    while candidate.exists():
        candidate = directory / f"{safe_title} ({suffix}).{safe_extension}"
        suffix += 1
    return candidate
