from __future__ import annotations

import re
import ipaddress
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
TRAILING_PUNCTUATION = ".,!?;:，。！？；：、)]}》】」』"


@dataclass(slots=True)
class LinkPreview:
    valid_urls: list[str]
    duplicate_count: int
    invalid_count: int


def extract_candidate_urls(text: str) -> list[str]:
    return [match.group(0).rstrip(TRAILING_PUNCTUATION) for match in URL_PATTERN.finditer(text)]


def is_public_http_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if (
        scheme not in {"http", "https"}
        or port not in (None, 80 if scheme == "http" else 443)
        or parsed.username is not None
        or parsed.password is not None
        or not hostname
        or hostname == "localhost"
        or hostname.endswith(".localhost")
        or hostname.endswith(".local")
    ):
        return False
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return True
    return address.is_global


def is_public_https_url(url: str) -> bool:
    try:
        return urlsplit(url).scheme.lower() == "https" and is_public_http_url(url)
    except ValueError:
        return False


def is_allowed_douyin_url(url: str) -> bool:
    """兼容旧调用方；新的输入校验允许安全公网 HTTP/HTTPS 地址。"""
    return is_public_http_url(url)


def normalize_url(url: str) -> str:
    from .youtube import classify_youtube_url

    youtube = classify_youtube_url(url)
    if youtube is not None and youtube.kind != "unsupported":
        return youtube.canonical_url
    parsed = urlsplit(url.strip())
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower().rstrip(".")
    netloc = hostname
    default_port = 80 if scheme == "http" else 443
    if parsed.port and parsed.port != default_port:
        netloc = f"{hostname}:{parsed.port}"
    path = parsed.path or "/"
    query_items = parse_qsl(parsed.query, keep_blank_values=True)
    if hostname == "bilibili.com" or hostname.endswith(".bilibili.com"):
        query_items = [(key, value) for key, value in query_items if key != "spm_id_from"]
    return urlunsplit((scheme, netloc, path, urlencode(query_items), ""))


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
            if not is_public_http_url(candidate):
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
