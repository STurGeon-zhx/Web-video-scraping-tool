# 唯品会商品主视频适配实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让工具能够从唯品会商品详情链接中匿名提取并下载顶部商品主视频，完成真实链接验收、程序重启和单文件 EXE 更新。

**Architecture:** 新增独立 `VipshopIE`，仅匹配 `detail.vip.com/detail-<品牌ID>-<商品ID>.html`。解析器使用全新的无持久化 Edge 上下文监听唯品会公开商品详情响应，将 `shortVideoUrl` 转换为现有 yt-dlp 统一视频信息；平台注册、错误映射、界面标签和下载队列继续复用现有架构。

**Tech Stack:** Python 3.11/3.12、FastAPI、yt-dlp、Playwright + Microsoft Edge、Vue 3、Vitest、pytest、PyInstaller、FFmpeg。

## Global Constraints

- 仅提取商品详情顶部的商品主视频，不提取评论、推荐或广告视频。
- 仅支持无需登录即可访问的公开内容，不读取用户现有浏览器 Cookie、历史记录或个人资料。
- 唯品会输入链接必须是安全公网 HTTPS 地址，域名必须为 `detail.vip.com`。
- 匿名上下文触发验证码、登录或访问控制时直接失败，不尝试绕过。
- 不改变现有数据库结构、应用数据目录、临时目录和其他平台解析行为。
- 本次更新项目根目录的 `视频批量下载工具.exe`，不更新安装包。

---

### Task 1: 唯品会链接和公开主视频解析器

**Files:**
- Create: `backend/douyin_downloader/vipshop.py`
- Create: `tests/test_vipshop.py`

**Interfaces:**
- Produces: `VipshopExtractionError(ValueError)`。
- Produces: `VipshopVideoInfo`，字段为 `product_id: str`、`title: str`、`video_url: str`、`thumbnail: str | None`、`webpage_url: str`。
- Produces: `parse_vipshop_payload(payload: dict[str, Any], product_id: str, webpage_url: str) -> VipshopVideoInfo`。
- Produces: `resolve_vipshop_page(url: str, product_id: str) -> VipshopVideoInfo`。
- Produces: `VipshopIE(InfoExtractor)`，允许通过构造参数注入 `resolver: Callable[[str, str], VipshopVideoInfo]`。

- [ ] **Step 1: 写链接匹配和响应解析失败测试**

```python
def test_vipshop_extractor_matches_product_detail_only() -> None:
    assert VipshopIE.suitable(
        "https://detail.vip.com/detail-10007920-6921967044893585744.html?"
    )
    assert not VipshopIE.suitable("https://www.vip.com/")
    assert not VipshopIE.suitable("https://detail.vip.com/comment/123")


def test_parse_vipshop_payload_rejects_product_without_main_video() -> None:
    payload = {"code": 1, "data": {"productId": "6921967044893585744", "base": {}}}
    with pytest.raises(VipshopExtractionError, match="没有可下载的主视频"):
        parse_vipshop_payload(payload, "6921967044893585744", "https://detail.vip.com/")
```

- [ ] **Step 2: 运行测试并确认缺少模块而失败**

Run: `python -m pytest tests/test_vipshop.py -q`

Expected: FAIL，错误包含 `ModuleNotFoundError: No module named 'douyin_downloader.vipshop'`。

- [ ] **Step 3: 写成功解析和主视频唯一选择测试**

```python
def test_parse_vipshop_payload_uses_only_product_main_video() -> None:
    payload = {
        "code": 1,
        "data": {
            "productId": "6921967044893585744",
            "base": {
                "productName": "测试商品",
                "shortVideoUrl": "//a.vpimg4.com/upload/merchandise/video/main.mp4",
                "smallImage": "//a.vpimg4.com/upload/merchandise/cover.jpg",
            },
            "comments": [{"videoUrl": "https://review.example/review.mp4"}],
        },
    }
    info = parse_vipshop_payload(
        payload,
        "6921967044893585744",
        "https://detail.vip.com/detail-10007920-6921967044893585744.html",
    )
    assert info.title == "测试商品"
    assert info.video_url == "https://a.vpimg4.com/upload/merchandise/video/main.mp4"
```

- [ ] **Step 4: 实现纯数据解析和 yt-dlp 信息转换**

在 `vipshop.py` 中：

```python
VIPSHOP_DETAIL_PATTERN = re.compile(
    r"^https://detail\.vip\.com/detail-(?P<brand_id>\d+)-(?P<id>\d+)\.html(?:[?#].*)?$"
)

@dataclass(frozen=True, slots=True)
class VipshopVideoInfo:
    product_id: str
    title: str
    video_url: str
    thumbnail: str | None
    webpage_url: str

class VipshopIE(InfoExtractor):
    IE_NAME = "vipshop"
    _VALID_URL = VIPSHOP_DETAIL_PATTERN.pattern

    def __init__(self, resolver=resolve_vipshop_page) -> None:
        super().__init__()
        self._resolver = resolver

    def _real_extract(self, url: str) -> dict[str, Any]:
        product_id = self._match_id(url)
        item = self._resolver(url, product_id)
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
```

`parse_vipshop_payload` 只读取 `data.base.shortVideoUrl`，不得递归搜索评论或推荐结构；协议相对地址统一补为 `https:`。商品 ID 不匹配、响应格式异常和主视频为空时抛出明确的 `VipshopExtractionError`。

- [ ] **Step 5: 运行纯解析测试**

Run: `python -m pytest tests/test_vipshop.py -q`

Expected: PASS。

- [ ] **Step 6: 提交纯解析单元**

```bash
git add backend/douyin_downloader/vipshop.py tests/test_vipshop.py
git commit -m "feat: add Vipshop main video extractor"
```

### Task 2: 匿名 Edge 动态响应捕获与媒体安全校验

**Files:**
- Modify: `backend/douyin_downloader/vipshop.py`
- Modify: `tests/test_vipshop.py`

**Interfaces:**
- Consumes: `parse_vipshop_payload(...) -> VipshopVideoInfo`。
- Consumes: `probe_public_video_url(url: str) -> DirectMediaProbe` from `resolver.py`。
- Produces: `resolve_vipshop_page(url: str, product_id: str) -> VipshopVideoInfo`，默认通过 `playwright.sync_api.sync_playwright` 启动系统 Edge。

- [ ] **Step 1: 写匿名上下文和安全媒体校验测试**

测试使用伪造的 Playwright 工厂，断言：

```python
info = resolve_vipshop_page(
    PRODUCT_URL,
    PRODUCT_ID,
    playwright_factory=fake_playwright,
    media_probe=lambda url: DirectMediaProbe(
        final_url="https://a.vpimg4.com/main.mp4",
        content_type="video/mp4",
        content_length=1024,
        filename=None,
    ),
)
assert fake_browser.launch_kwargs == {"channel": "msedge", "headless": True}
assert fake_context.storage_state_path is None
assert info.video_url == "https://a.vpimg4.com/main.mp4"
assert fake_context.closed is True
```

另加超时、验证码页面、详情接口无响应和媒体探测失败用例，均断言抛出 `VipshopExtractionError`。

- [ ] **Step 2: 运行测试并确认动态解析尚未实现**

Run: `python -m pytest tests/test_vipshop.py -q`

Expected: FAIL，缺少 `playwright_factory` 或未捕获响应。

- [ ] **Step 3: 实现临时浏览器上下文**

实现要点：

```python
with playwright_factory() as playwright:
    browser = playwright.chromium.launch(channel="msedge", headless=True)
    context = browser.new_context()
    page = context.new_page()
    payloads: list[dict[str, Any]] = []

    def capture(response: Any) -> None:
        if "/shopping/pc/detail/main/v6" not in response.url:
            return
        body = response.json()
        if isinstance(body, dict):
            payloads.append(body)

    page.on("response", capture)
    page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    for _ in range(40):
        if payloads:
            break
        page.wait_for_timeout(250)
```

始终在 `finally` 中关闭 `context` 和 `browser`。从捕获结果中选择商品 ID 匹配的数据，用 `probe_public_video_url` 校验并替换为最终公网媒体 URL。不得使用 `launch_persistent_context`，不得传入用户数据目录。

- [ ] **Step 4: 运行唯品会测试**

Run: `python -m pytest tests/test_vipshop.py -q`

Expected: PASS。

- [ ] **Step 5: 提交动态解析单元**

```bash
git add backend/douyin_downloader/vipshop.py tests/test_vipshop.py
git commit -m "feat: resolve Vipshop videos in anonymous Edge"
```

### Task 3: 注册平台、错误分类和界面标签

**Files:**
- Modify: `backend/douyin_downloader/downloader.py`
- Modify: `backend/douyin_downloader/platforms.py`
- Modify: `backend/douyin_downloader/direct_media.py`
- Modify: `frontend/src/App.vue`
- Modify: `tests/test_downloader.py`
- Modify: `tests/test_platforms.py`
- Modify: `tests/test_direct_media.py`
- Modify: `frontend/tests/app.test.js`

**Interfaces:**
- Consumes: `VipshopIE` from Task 1。
- Produces: 平台字符串 `vipshop` 和界面标签“唯品会”。
- Produces: `ErrorCode.VIPSHOP_ACCESS = "vipshop_access"`。

- [ ] **Step 1: 写平台注册和防止直链解析器抢占的失败测试**

```python
def test_supports_vipshop_product_detail() -> None:
    url = "https://detail.vip.com/detail-10007920-6921967044893585744.html"
    assert extractor_supports_url(url) is True
    assert DirectMediaIE.suitable(url) is False


def test_expands_vipshop_task_with_platform_slug() -> None:
    info = {
        "id": "6921967044893585744",
        "title": "测试商品",
        "url": "https://a.vpimg4.com/main.mp4",
        "webpage_url": VIPSHOP_URL,
        "formats": [{"url": "https://a.vpimg4.com/main.mp4", "vcodec": "h264"}],
        "extractor_key": "Vipshop",
    }
    assert expanded.platform == "vipshop"
```

- [ ] **Step 2: 写错误和前端标签失败测试**

在 `tests/test_downloader.py` 添加唯品会匿名访问失败映射和空标题回退测试；在 `frontend/tests/app.test.js` 断言存在 `vipshop: '唯品会'` 且导入提示包含唯品会。

- [ ] **Step 3: 运行相关测试并确认失败**

Run: `python -m pytest tests/test_platforms.py tests/test_direct_media.py tests/test_downloader.py -q`

Run: `npm --prefix frontend test -- --run tests/app.test.js`

Expected: FAIL，平台未注册、错误未映射或标签不存在。

- [ ] **Step 4: 最小实现平台注册和错误映射**

- `_default_ydl_factory` 按 `KuaishouIE()`、`VipshopIE()`、`DirectMediaIE()`、默认解析器的顺序注册。
- `extractor_supports_url` 对合法唯品会商品页返回 `True`。
- `_platform_slug` 将 `Vipshop` 映射为 `vipshop`。
- `DirectMediaIE.suitable` 明确排除 `detail.vip.com` 商品页。
- 新增 `ErrorCode.VIPSHOP_ACCESS`，用户提示为“唯品会匿名访问失败，请稍后重试”。
- 空标题文件名前缀新增 `vipshop: "唯品会视频"`。
- 前端平台标签新增 `vipshop: '唯品会'`，输入帮助增加唯品会商品详情链接。

- [ ] **Step 5: 运行相关测试并确认通过**

Run: `python -m pytest tests/test_platforms.py tests/test_direct_media.py tests/test_downloader.py tests/test_vipshop.py -q`

Run: `npm --prefix frontend test -- --run tests/app.test.js`

Expected: PASS。

- [ ] **Step 6: 提交集成单元**

```bash
git add backend/douyin_downloader/downloader.py backend/douyin_downloader/platforms.py backend/douyin_downloader/direct_media.py frontend/src/App.vue tests/test_downloader.py tests/test_platforms.py tests/test_direct_media.py frontend/tests/app.test.js
git commit -m "feat: integrate Vipshop platform support"
```

### Task 4: 完整回归与真实链接下载验收

**Files:**
- Modify only if a failing test exposes a Vipshop-scoped defect.

**Interfaces:**
- Consumes: 用户提供的唯品会商品链接。
- Produces: 一条完成状态的 `vipshop` 任务和可播放 MP4。

- [ ] **Step 1: 运行完整自动化测试和前端构建**

Run: `python -m pytest -q`

Run: `npm --prefix frontend test -- --run`

Run: `npm --prefix frontend run build`

Expected: 后端、前端测试全部 PASS，Vite 构建退出码为 0。

- [ ] **Step 2: 在隔离数据目录启动源码服务**

使用工作区 `build/vipshop-smoke` 作为临时应用数据和下载目录，启动 `python run_desktop.py` 或等价的 `LocalBackend` 测试实例。不得读写用户现有 `tasks.db`。

- [ ] **Step 3: 用真实唯品会链接创建批次并等待完成**

POST `/api/batches`：

```json
{
  "text": "https://detail.vip.com/detail-10007920-6921967044893585744.html?",
  "output_dir": "<隔离下载目录>"
}
```

Expected: `platform_counts.vipshop == 1`，任务最终状态为 `completed`，输出文件存在。

- [ ] **Step 4: 用 FFmpeg 验证输出媒体**

Run: `ffmpeg -v error -i <output.mp4> -map 0:v:0 -frames:v 1 -f null NUL`

Run: `ffmpeg -v error -i <output.mp4> -map 0:a:0 -frames:a 1 -f null NUL`

Expected: 两条命令退出码均为 0。

- [ ] **Step 5: 清理隔离测试数据**

先解析并验证绝对路径位于工作区 `build/vipshop-smoke` 内，再删除临时数据库、下载文件和临时目录；不得删除用户下载目录或应用数据。

### Task 5: 重启源码程序并更新单文件 EXE

**Files:**
- Build output: `dist/视频批量下载工具.exe`
- Replace: `视频批量下载工具.exe`

**Interfaces:**
- Produces: 项目根目录可双击启动的最新单文件 EXE。

- [ ] **Step 1: 安全关闭当前工具实例**

从 `%LOCALAPPDATA%/DouyinBatchDownloader/runtime.json` 读取精确 PID 和端口，先请求安全退出；只在进程仍存活时停止该精确 PID，不按模糊名称结束其他程序。

- [ ] **Step 2: 启动最新源码程序并验证**

Run: `python run_desktop.py`

Expected: 新 `runtime.json` 中 PID 存活，页面返回 HTTP 200，`/api/settings` 正常，唯品会链接预览为有效。

- [ ] **Step 3: 重新构建单文件 EXE**

Run: `npm --prefix frontend run build`

Run: `python -m PyInstaller --noconfirm --clean douyin_downloader.spec`

Expected: `dist/视频批量下载工具.exe` 存在且 PyInstaller 退出码为 0。

- [ ] **Step 4: 替换根目录同名文件并校验哈希**

复制 `dist/视频批量下载工具.exe` 到项目根目录，计算两个文件 SHA-256，断言哈希相同。

- [ ] **Step 5: 关闭源码实例并实际启动新 EXE**

启动项目根目录 `视频批量下载工具.exe`，读取新 `runtime.json`，验证：

- 进程存活。
- 页面 HTTP 200。
- `/api/settings` 返回成功。
- `/api/batches/preview` 将唯品会链接识别为 1 条有效候选。

- [ ] **Step 6: 最终验证和交付**

Run: `python -m pytest -q`

Run: `npm --prefix frontend test -- --run`

Run: `git diff --check`

记录 EXE 的绝对路径、文件大小、更新时间和 SHA-256。不得在没有上述新鲜验证结果时宣称完成。
