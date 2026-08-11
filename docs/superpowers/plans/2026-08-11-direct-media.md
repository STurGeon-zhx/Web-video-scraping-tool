# Public Direct Media Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow the local desktop tool to accept and download any safely validated public HTTP/HTTPS direct video URL.

**Architecture:** Add a focused `DirectMediaIE` custom yt-dlp extractor backed by a redirect-aware public-network probe. Keep `GenericIE` disabled and route only unmatched HTTP/HTTPS candidates through this extractor. Reuse the existing batch model, queue, progress hooks, retry behavior, media compatibility processing, and storage.

**Tech Stack:** Python 3.11+, FastAPI, httpx, yt-dlp custom `InfoExtractor`, Vue 3, pytest, Vitest, FFmpeg.

## Global Constraints

- Accept only anonymous public HTTP/HTTPS video resources.
- Reject loopback, private, link-local, reserved, and non-global targets before every request.
- Require a final `Content-Type` under `video/*`; URL extensions alone are insufficient.
- Keep yt-dlp `GenericIE` disabled.
- Do not add WeChat Channels support.
- Do not rebuild the installer or EXE in this task.
- Restart the source desktop program after all verification passes.

---

### Task 1: Public HTTP URL normalization and media probing

**Files:**
- Modify: `backend/douyin_downloader/links.py`
- Modify: `backend/douyin_downloader/resolver.py`
- Test: `tests/test_links.py`
- Test: `tests/test_resolver.py`

**Interfaces:**
- Produces: `is_public_http_url(url: str) -> bool`.
- Produces: `normalize_url(url: str) -> str` that preserves valid HTTP while retaining existing Bilibili tracking cleanup.
- Produces: synchronous `probe_public_video_url(url, client: httpx.Client | None = None, max_redirects=5, dns_resolver=_resolve_host) -> DirectMediaProbe` for use inside yt-dlp extraction threads.
- Produces: immutable `DirectMediaProbe(final_url: str, content_type: str, content_length: int | None, filename: str | None)`.

- [ ] **Step 1: Write failing tests**

Add literal behavior tests proving that a public HTTP MP4 candidate is accepted and normalized without upgrading it to HTTPS; private HTTP targets remain invalid. Add resolver tests where HEAD returns `video/mp4`, where HEAD 405 falls back to `GET` with `Range: bytes=0-4095`, where a redirect to `127.0.0.1` is rejected before the second request, and where `text/html` is rejected.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_links.py tests/test_resolver.py -q`

Expected: failures because HTTP input is currently rejected/upgraded and `probe_public_video_url` does not exist.

- [ ] **Step 3: Implement the minimal probe**

Update URL validation to accept default ports 80 and 443 according to scheme. Generalize host resolution to use the parsed port. Make the existing async URL resolver accept safe HTTP and stream its GET response so direct media is not buffered during redirect resolution. Implement the synchronous HEAD/Range probe for the extractor. Parse `Content-Disposition` using the standard library, return content length when numeric, and close Range responses without consuming the full body.

- [ ] **Step 4: Run tests and verify GREEN**

Run: `python -m pytest tests/test_links.py tests/test_resolver.py -q`

Expected: all selected tests pass.

### Task 2: Direct media extractor and batch expansion

**Files:**
- Create: `backend/douyin_downloader/direct_media.py`
- Modify: `backend/douyin_downloader/platforms.py`
- Modify: `backend/douyin_downloader/downloader.py`
- Test: `tests/test_direct_media.py`
- Test: `tests/test_platforms.py`

**Interfaces:**
- Produces: `DirectMediaIE(InfoExtractor)` with `IE_NAME = "direct-media"`.
- Produces: `direct_media_supports_url(url: str) -> bool` for HTTP/HTTPS candidates not claimed by dedicated extractors.
- Consumes: `probe_public_video_url` and `DirectMediaProbe` from Task 1.
- Produces yt-dlp info with `id`, `title`, `webpage_url`, `url`, `ext`, `vcodec`, and optional `filesize`.

- [ ] **Step 1: Write failing extractor tests**

Use a fake probe returning a hand-written MP4 response. Assert ID equals the first 16 hex characters of SHA-256 over the normalized final URL, `Content-Disposition` filename wins over the path, path filename is the fallback, and non-video probe errors surface as a user-readable extraction error.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_direct_media.py tests/test_platforms.py -q`

Expected: import failure for the missing direct media module and platform support assertions fail.

- [ ] **Step 3: Implement and register the extractor**

Create the custom extractor, inject its probe for unit tests, and register it before default extractors in the shared `_default_ydl_factory`. Its `suitable` method must decline URLs matched by a dedicated yt-dlp extractor or the existing Kuaishou adapter, so it cannot shadow platform extractors. Update `extractor_supports_url` so dedicated extractors remain preferred and unmatched HTTP/HTTPS links are eligible for direct probing. Update `_platform_slug` to return `direct` for extractor key `DirectMedia`.

- [ ] **Step 4: Preserve HTTP canonical URLs**

Change `BatchExpander` so valid direct-media canonical HTTP URLs are retained rather than replaced or upgraded. Keep non-direct platform canonical URLs under the prior HTTPS rule.

- [ ] **Step 5: Run tests and verify GREEN**

Run: `python -m pytest tests/test_direct_media.py tests/test_platforms.py tests/test_downloader.py -q`

Expected: all selected tests pass.

### Task 3: API behavior, errors, naming, and UI labels

**Files:**
- Modify: `backend/douyin_downloader/api.py`
- Modify: `backend/douyin_downloader/downloader.py`
- Modify: `frontend/src/App.vue`
- Test: `tests/test_api.py`
- Test: `tests/test_downloader.py`
- Test: `frontend/tests/app.test.js`

**Interfaces:**
- API preview accepts safe public HTTP/HTTPS candidates.
- API batch creation reports `platform_counts.direct`.
- UI renders platform `direct` as `视频直链` and includes direct-video URLs in the import help.

- [ ] **Step 1: Write failing API and UI tests**

Add a batch API test using an injected expander that returns `ExpandedVideo(platform="direct", ...)`. Add a downloader fallback-name test expecting `视频直链_<id>.mp4`. Add a Vue test whose batch task platform is `direct` and expects the visible badge `视频直链`.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_api.py tests/test_downloader.py -q`

Run from `frontend`: `npm test -- --run tests/app.test.js`

Expected: HTTP input/API wording or direct platform labels fail.

- [ ] **Step 3: Implement minimal API and UI changes**

Change invalid-input text from “公网 HTTPS” to “公网 HTTP/HTTPS”. Add `direct: "视频直链"` to backend fallback naming and frontend `platformLabel`. Update the import description and placeholder with one direct MP4 example.

- [ ] **Step 4: Run tests and verify GREEN**

Run: `python -m pytest tests/test_api.py tests/test_downloader.py -q`

Run from `frontend`: `npm test -- --run tests/app.test.js`

Expected: selected backend and frontend tests pass.

### Task 4: Full regression and real-link acceptance

**Files:**
- No production files expected.
- Runtime output: a temporary directory outside the repository.

**Interfaces:**
- Consumes the public link supplied by the user.
- Produces one completed direct-media task and one playable local MP4.

- [ ] **Step 1: Run full automated tests**

Run: `python -m pytest -q`

Run: `npm test -- --run`

Run: `npm run build`

Expected: every test passes and Vite completes successfully.

- [ ] **Step 2: Run real download smoke test**

Use a temporary application data directory and output directory. Submit the user URL through `POST /api/batches`, wait for completion, and assert platform `direct`, non-empty output path, and completed status.

- [ ] **Step 3: Verify the downloaded media**

Run bundled FFmpeg/ffprobe against the output and assert at least one readable video stream. Remove only the temporary smoke-test directory after recording the result.

- [ ] **Step 4: Restart the source desktop program**

Gracefully close the current `视频批量下载工具` instance, launch `python run_desktop.py`, read `runtime.json`, and verify `GET /api/settings` plus the static page both return successfully.

- [ ] **Step 5: Review repository state**

Run: `git diff --check` and `git status --short`.

Expected: only intentional source, test, design, and plan changes remain.
