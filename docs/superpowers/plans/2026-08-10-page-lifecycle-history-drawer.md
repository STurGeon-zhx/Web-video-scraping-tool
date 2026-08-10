# 页面关闭退出与历史抽屉 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 最后一个工具页面关闭 3 秒后安全结束程序，同时把页面底部批次历史改为右上角按钮打开的右侧抽屉。

**Architecture:** 后端用独立的页面生命周期 SSE 统计活动标签页，最后一条连接断开后启动可取消的 3 秒退出任务；前端始终维护该连接。批次历史继续使用现有 REST 数据和操作，只改变为顶栏入口与抽屉布局。

**Tech Stack:** Python 3.11/3.12、FastAPI、asyncio、Vue 3、Vitest、Vue Test Utils、jsdom、PyInstaller

## Global Constraints

- 仅当至少一个页面成功连接过、且最后一个页面断开超过 3 秒时退出。
- 页面刷新和 3 秒内重新打开页面必须取消退出。
- 多标签页只关闭其中一个时不得退出。
- 程序退出后，未完成任务沿用 SQLite 恢复机制在下次启动时回到等待状态。
- 历史抽屉保留新建批次、查看、删除、统计和分页。
- 不修改下载解析、文件命名、本地数据隔离和数据库结构。

---

### Task 1: 页面会话计数与延迟退出

**Files:**
- Create: `backend/douyin_downloader/page_sessions.py`
- Create: `tests/test_page_sessions.py`

**Interfaces:**
- Consumes: 同步回调 `shutdown_callback: Callable[[], None]`。
- Produces: `PageSessionTracker(shutdown_callback, grace_seconds=3.0)`，以及异步方法 `connect() -> None`、`disconnect() -> None`、`close() -> None`。

- [ ] **Step 1: Write the failing tests**

```python
import asyncio

import pytest

from douyin_downloader.page_sessions import PageSessionTracker


@pytest.mark.asyncio
async def test_last_page_disconnects_then_shutdown_runs_after_grace() -> None:
    shutdowns = 0

    def shutdown() -> None:
        nonlocal shutdowns
        shutdowns += 1

    tracker = PageSessionTracker(shutdown, grace_seconds=0.01)
    await tracker.connect()
    await tracker.disconnect()
    await asyncio.sleep(0.03)

    assert shutdowns == 1


@pytest.mark.asyncio
async def test_reconnect_during_grace_cancels_shutdown() -> None:
    shutdowns = 0

    def shutdown() -> None:
        nonlocal shutdowns
        shutdowns += 1

    tracker = PageSessionTracker(shutdown, grace_seconds=0.03)
    await tracker.connect()
    await tracker.disconnect()
    await tracker.connect()
    await asyncio.sleep(0.05)

    assert shutdowns == 0
    await tracker.close()


@pytest.mark.asyncio
async def test_one_of_multiple_pages_disconnects_without_shutdown() -> None:
    shutdowns = 0

    def shutdown() -> None:
        nonlocal shutdowns
        shutdowns += 1

    tracker = PageSessionTracker(shutdown, grace_seconds=0.01)
    await tracker.connect()
    await tracker.connect()
    await tracker.disconnect()
    await asyncio.sleep(0.03)

    assert tracker.active_sessions == 1
    assert shutdowns == 0
    await tracker.close()
```

- [ ] **Step 2: Run tests to verify RED**

Run: `python -m pytest tests/test_page_sessions.py -v`

Expected: collection fails because `douyin_downloader.page_sessions` does not exist.

- [ ] **Step 3: Implement the minimal tracker**

```python
class PageSessionTracker:
    def __init__(self, shutdown_callback, grace_seconds: float = 3.0) -> None:
        self.shutdown_callback = shutdown_callback
        self.grace_seconds = grace_seconds
        self.active_sessions = 0
        self._ever_connected = False
        self._lock = asyncio.Lock()
        self._shutdown_task: asyncio.Task[None] | None = None

    async def connect(self) -> None:
        async with self._lock:
            self.active_sessions += 1
            self._ever_connected = True
            if self._shutdown_task:
                self._shutdown_task.cancel()
                self._shutdown_task = None

    async def disconnect(self) -> None:
        async with self._lock:
            if self.active_sessions == 0:
                return
            self.active_sessions -= 1
            if self._ever_connected and self.active_sessions == 0:
                if self._shutdown_task:
                    self._shutdown_task.cancel()
                self._shutdown_task = asyncio.create_task(self._shutdown_after_grace())

    async def _shutdown_after_grace(self) -> None:
        try:
            await asyncio.sleep(self.grace_seconds)
            async with self._lock:
                if self.active_sessions != 0:
                    return
                self._shutdown_task = None
            self.shutdown_callback()
        except asyncio.CancelledError:
            return

    async def close(self) -> None:
        async with self._lock:
            if self._shutdown_task:
                self._shutdown_task.cancel()
                self._shutdown_task = None
```

- [ ] **Step 4: Run tests to verify GREEN**

Run: `python -m pytest tests/test_page_sessions.py -v`

Expected: 3 tests pass.

- [ ] **Step 5: Commit**

```powershell
git add backend/douyin_downloader/page_sessions.py tests/test_page_sessions.py
git commit -m "feat: track active browser pages"
```

### Task 2: 生命周期 SSE 接口集成

**Files:**
- Modify: `backend/douyin_downloader/api.py`
- Modify: `tests/test_page_sessions.py`

**Interfaces:**
- Consumes: Task 1 的 `PageSessionTracker`。
- Produces: `stream_page_session(tracker, keepalive_interval=15.0, max_events=None)` 和 `GET /api/app/session`。

- [ ] **Step 1: Add failing stream lifecycle test**

```python
from douyin_downloader.api import stream_page_session


@pytest.mark.asyncio
async def test_page_session_stream_registers_until_closed() -> None:
    shutdowns = 0

    def shutdown() -> None:
        nonlocal shutdowns
        shutdowns += 1

    tracker = PageSessionTracker(shutdown, grace_seconds=0.01)
    events = stream_page_session(tracker, keepalive_interval=0, max_events=1)

    assert await anext(events) == ": keep-alive\n\n"
    assert tracker.active_sessions == 1
    await events.aclose()
    await asyncio.sleep(0.03)

    assert tracker.active_sessions == 0
    assert shutdowns == 1
```

- [ ] **Step 2: Run test to verify RED**

Run: `python -m pytest tests/test_page_sessions.py::test_page_session_stream_registers_until_closed -v`

Expected: FAIL because `stream_page_session` is not exported.

- [ ] **Step 3: Add stream, route, and lifespan cleanup**

Create one tracker per app inside `create_app`, store it as `app.state.page_sessions`, and add:

```python
async def stream_page_session(tracker, keepalive_interval=15.0, max_events=None):
    emitted = 0
    await tracker.connect()
    try:
        while max_events is None or emitted < max_events:
            emitted += 1
            yield ": keep-alive\n\n"
            await asyncio.sleep(keepalive_interval)
    finally:
        await tracker.disconnect()


@app.get("/api/app/session")
async def page_session() -> StreamingResponse:
    return StreamingResponse(
        stream_page_session(page_sessions),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
```

In FastAPI lifespan `finally`, call `await page_sessions.close()` before `queue.stop()` so shutdown cannot be scheduled twice while the server is already exiting.

- [ ] **Step 4: Run focused and full backend tests**

Run: `python -m pytest tests/test_page_sessions.py tests/test_api.py -v`

Expected: all focused tests pass.

Run: `python -m pytest`

Expected: full backend suite passes.

- [ ] **Step 5: Commit**

```powershell
git add backend/douyin_downloader/api.py tests/test_page_sessions.py
git commit -m "feat: exit after last page closes"
```

### Task 3: 建立 Vue 组件交互测试

**Files:**
- Modify: `frontend/package.json`
- Modify: `frontend/package-lock.json`
- Create: `frontend/tests/app.test.js`

**Interfaces:**
- Consumes: Vue `App.vue` 与浏览器 API `EventSource`。
- Produces: 可挂载 Vue 单文件组件的 jsdom 测试环境，以及完整 API/EventSource 测试替身。

- [ ] **Step 1: Install component test dependencies**

Run: `npm install --save-dev @vue/test-utils jsdom`

Expected: `package.json` and `package-lock.json` add compatible locked versions.

- [ ] **Step 2: Write failing drawer and lifecycle tests**

Create `frontend/tests/app.test.js` with `// @vitest-environment jsdom` and a complete history fixture. Mock only network boundaries: `fetch` returns settings and paged history payloads; `EventSource` records URLs and exposes `close()`.

Required assertions:

```javascript
expect(wrapper.find(".history-panel").exists()).toBe(false);
expect(wrapper.get(".history-trigger").text()).toContain("历史批次 1");
await wrapper.get(".history-trigger").trigger("click");
expect(wrapper.get(".history-drawer").attributes("aria-modal")).toBe("true");
expect(wrapper.get(".history-drawer").text()).toContain("批次 #5");
window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
await nextTick();
expect(wrapper.find(".history-drawer").exists()).toBe(false);
expect(FakeEventSource.urls).toContain("/api/app/session");
```

- [ ] **Step 3: Run test to verify RED**

Run: `npm test -- tests/app.test.js`

Expected: FAIL because the topbar trigger, drawer, and lifecycle EventSource do not exist.

- [ ] **Step 4: Commit the red test setup with the implementation in Task 4**

Do not commit a permanently failing test. Proceed directly to Task 4 after recording the expected failures.

### Task 4: 右上角历史抽屉与前端生命周期连接

**Files:**
- Modify: `frontend/src/App.vue`
- Modify: `frontend/src/styles.css`
- Modify: `frontend/tests/app.test.js`
- Modify: `README.md`

**Interfaces:**
- Consumes: `GET /api/app/session`、现有 `loadHistory`、`viewBatch`、`deleteBatch`、`newBatch`。
- Produces: `.history-trigger`、`.history-overlay`、`.history-drawer` 和页面级生命周期 EventSource。

- [ ] **Step 1: Add frontend state and lifecycle hooks**

Add `historyOpen = ref(false)` and `pageSession = null`. On mount, create `new EventSource("/api/app/session")`, register the `keydown` listener, and load settings/history. On unmount, close both EventSources, remove the listener, and remove `history-drawer-open` from `document.body`.

Implement `openHistory`, `closeHistory`, and an Escape handler. Toggle `document.body.classList` only through these two functions.

- [ ] **Step 2: Move history markup into the drawer**

Add a topbar button after the privacy badge:

```vue
<button class="history-trigger" type="button" @click="openHistory">
  历史批次 <strong>{{ history.total }}</strong>
</button>
```

Replace the bottom `<section class="panel history-panel">` with conditional overlay and drawer markup at app-shell level. The drawer uses `role="dialog"`, `aria-modal="true"`, an accessible close button, the existing history list/actions/pagination, and a compact “新建批次” button.

- [ ] **Step 3: Close drawer after view/new batch**

Call `closeHistory()` at the start of `newBatch()` and after `viewBatch()` successfully loads. Do not close after delete or page navigation so the user can continue managing history.

- [ ] **Step 4: Add responsive drawer styles**

Remove `.history-panel` layout rules. Add a fixed full-screen overlay, right-aligned drawer with desktop width `min(440px, 92vw)`, scrollable content, stacked compact history cards, and mobile width `min(92vw, 440px)`. Add `body.history-drawer-open { overflow: hidden; }`.

- [ ] **Step 5: Update README behavior**

Replace “浏览器页面可以刷新或关闭” with: refresh is safe; closing the final page exits after about 3 seconds; unfinished tasks recover on next launch. Explain history access through the top-right button.

- [ ] **Step 6: Run frontend tests and build**

Run: `npm test`

Expected: component tests plus existing helper tests pass.

Run: `npm run build`

Expected: Vite production build succeeds without template errors.

- [ ] **Step 7: Commit**

```powershell
git add frontend/package.json frontend/package-lock.json frontend/tests/app.test.js frontend/src/App.vue frontend/src/styles.css README.md
git commit -m "feat: move batch history into drawer"
```

### Task 5: 集成验收与 Windows 打包

**Files:**
- Verify: `backend/douyin_downloader/launcher.py`
- Build: `dist-page-lifecycle/抖音批量下载工具.exe`

**Interfaces:**
- Consumes: Tasks 1-4 的后端、前端和现有 PyInstaller spec。
- Produces: 可在 Windows 10/11 双击运行的新版 EXE。

- [ ] **Step 1: Run fresh full verification**

Run: `python -m pytest`

Expected: all backend tests pass.

Run: `npm --prefix frontend test`

Expected: all frontend tests pass.

Run: `npm --prefix frontend run build`

Expected: production assets build successfully.

- [ ] **Step 2: Browser acceptance test**

With a development server and test API, verify the topbar history trigger, drawer open/close, Escape behavior, view/new/delete controls, absence of the old bottom history panel, and one `/api/app/session` connection per page.

- [ ] **Step 3: Build a non-conflicting EXE artifact**

Run:

```powershell
python -m PyInstaller --noconfirm --clean --distpath dist-page-lifecycle --workpath build-page-lifecycle douyin_downloader.spec
```

Expected: `dist-page-lifecycle/抖音批量下载工具.exe` exists and contains the latest hashed frontend JS/CSS assets.

- [ ] **Step 4: Packaged lifecycle smoke test**

After ensuring no older instance holds the single-instance mutex, start the EXE with a unique `DOUYIN_DOWNLOADER_DATA_DIR`. Verify refresh keeps the PID alive, opening two pages and closing one keeps it alive, and closing the last page ends the PID after approximately 3 seconds. Restart and confirm interrupted task state recovery remains covered by the backend suite.

- [ ] **Step 5: Inspect scope and status**

Run: `git status --short`

Expected: only planned source/test/docs changes plus generated build artifacts are present; unrelated existing untracked files remain untouched.
