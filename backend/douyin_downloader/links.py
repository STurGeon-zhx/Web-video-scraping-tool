from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit


URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
TRAILING_PUNCTUATION = ".,!?;:，。！？；：、)]}》】」』"


@dataclass(slots=True)
class LinkPreview:
    valid_urls: list[str]
    duplicate_count: int
    invalid_count: int


def extract_candidate_urls(text: str) -> list[str]:
    return [match.group(0).rstrip(TRAILING_PUNCTUATION) for match in URL_PATTERN.finditer(text)]


def is_allowed_douyin_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    hostname = (parsed.hostname or "").lower().rstrip(".")
    return (
        parsed.scheme == "https"
        and port in (None, 443)
        and parsed.username is None
        and parsed.password is None
        and (hostname == "douyin.com" or hostname.endswith(".douyin.com"))
    )


def normalize_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    hostname = (parsed.hostname or "").lower().rstrip(".")
    netloc = hostname
    if parsed.port and parsed.port != 443:
        netloc = f"{hostname}:{parsed.port}"
    path = parsed.path or "/"
    return urlunsplit(("https", netloc, path, parsed.query, ""))


def preview_links(text: str) -> LinkPreview:
    valid_urls: list[str] = []
    seen: set[str] = set()
    duplicate_count = 0
    invalid_count = 0

    for line in (line.strip() for line in text.splitlines()):
        if not line:
            continue
        candidates = extract_candidate_urls(line)
        if not candidates:
            invalid_count += 1
            continue
        for candidate in candidates:
            if not is_allowed_douyin_url(candidate):
                invalid_count += 1
                continue
            try:
                normalized = normalize_url(candidate)
            except ValueError:
                invalid_count += 1
                continue
            if normalized in seen:
                duplicate_count += 1
                continue
            seen.add(normalized)
            valid_urls.append(normalized)

    return LinkPreview(valid_urls, duplicate_count, invalid_count)
