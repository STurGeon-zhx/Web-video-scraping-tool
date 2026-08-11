# 抖音批量下载桌面安装版 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将现有本地浏览器工具改造成内置 QtWebEngine 独立窗口的 Windows 桌面客户端，并生成无需管理员权限、无需外部运行时的中文安装程序。

**Architecture:** Vue/FastAPI/SQLite/下载队列保持不变；新增可独立测试的本地后端运行器，在后台线程只监听 `127.0.0.1`，Qt 主线程负责窗口、目录选择和单实例通信。PyInstaller 以目录模式打包全部运行时，Inno Setup 再生成一个版本化 `Setup.exe`。

**Tech Stack:** Python 3.11、PySide6/QtWebEngine 6.x、FastAPI、Uvicorn、Vue 3、SQLite、yt-dlp、FFmpeg、PyInstaller 6.x、Inno Setup 6。

## Global Constraints

- 首版仅支持 Windows 10/11 64 位。
- 界面、历史记录、设置和本地文件管理断网可用；解析和下载抖音视频时需要联网。
- 软件不依赖接收方安装 Python、Node.js、浏览器、WebView2 或 FFmpeg。
- 内部服务只能监听 `127.0.0.1`，不能监听局域网地址。
- 应用数据保存在 `%LOCALAPPDATA%\DouyinBatchDownloader`，卸载不删除应用数据和已下载视频。
- 安装到 `%LOCALAPPDATA%\Programs\DouyinBatchDownloader`，不要求管理员权限。
- PyInstaller 必须使用目录模式；Inno Setup 输出单个安装程序。
- 只处理用户拥有版权或已获授权的公开内容。

---

### Task 1: 为桌面入口关闭浏览器会话自动退出

**Files:**
- Modify: `backend/douyin_downloader/api.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `create_app(...) -> FastAPI` 现有工厂。
- Produces: 新参数 `shutdown_on_page_disconnect: bool = True`；浏览器入口保持现有行为，桌面入口传 `False`。

- [ ] **Step 1: 写失败测试**

在 `tests/test_api.py` 新增：

```python
@pytest.mark.asyncio
async def test_desktop_app_ignores_page_session_disconnect(tmp_path: Path) -> None:
    shutdown_calls: list[bool] = []
    database = Database(tmp_path / "desktop.db")
    database.initialize()
    app = create_app(
        database,
        IdleQueue(database),
        default_download_dir=tmp_path,
        pick_directory=lambda: None,
        open_directory=lambda _path: None,
        shutdown_callback=lambda: shutdown_calls.append(True),
        shutdown_on_page_disconnect=False,
    )
    tracker = app.state.page_sessions

    await tracker.connect()
    await tracker.disconnect()
    await asyncio.sleep(0.02)

    assert shutdown_calls == []
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `python -m pytest tests/test_api.py::test_desktop_app_ignores_page_session_disconnect -q`

Expected: FAIL，`create_app()` 不接受 `shutdown_on_page_disconnect`。

- [ ] **Step 3: 实现桌面会话策略**

在 `create_app` 增加参数，并仅为页面会话选择回调：

```python
def create_app(..., shutdown_on_page_disconnect: bool = True) -> FastAPI:
    page_disconnect_callback = shutdown_callback if shutdown_on_page_disconnect else lambda: None
    page_sessions = PageSessionTracker(page_disconnect_callback, grace_seconds=30.0)
```

`POST /api/app/shutdown` 仍使用真实 `shutdown_callback`，不受此参数影响。

- [ ] **Step 4: 运行定向和 API 全量测试**

Run: `python -m pytest tests/test_api.py tests/test_page_sessions.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add backend/douyin_downloader/api.py tests/test_api.py
git commit -m "feat: support desktop-managed lifecycle"
```

---

### Task 2: 提取可测试的本地后端运行器

**Files:**
- Create: `backend/douyin_downloader/local_backend.py`
- Create: `tests/test_local_backend.py`
- Modify: `backend/douyin_downloader/launcher.py`

**Interfaces:**
- Consumes: `Database`、`TaskQueue`、`create_app`、`create_downloader`、`find_frontend_directory`、`find_ffmpeg`。
- Produces: `LocalBackend(data_dir, default_download_dir, pick_directory, open_directory)`，公开 `start(timeout=20) -> str`、`stop(timeout=10) -> None`、`base_url: str | None`、`port: int | None`。

- [ ] **Step 1: 写真实 HTTP 生命周期失败测试**

`tests/test_local_backend.py` 使用临时数据库和真实 Uvicorn：

```python
def test_local_backend_serves_frontend_and_stops(tmp_path: Path) -> None:
    backend = LocalBackend(
        data_dir=tmp_path / "data",
        default_download_dir=tmp_path / "downloads",
        pick_directory=lambda: None,
        open_directory=lambda _path: None,
    )

    base_url = backend.start(timeout=10)
    response = httpx.get(f"{base_url}/api/settings", timeout=5)
    backend.stop(timeout=10)

    assert response.status_code == 200
    assert backend.is_running is False
    assert not (tmp_path / "data" / "runtime.json").exists()
```

另加 `test_local_backend_only_binds_loopback`，使用 `urlsplit(base_url).hostname == "127.0.0.1"` 断言可观察边界。

- [ ] **Step 2: 运行测试并确认失败**

Run: `python -m pytest tests/test_local_backend.py -q`

Expected: ERROR，模块 `douyin_downloader.local_backend` 不存在。

- [ ] **Step 3: 实现 `LocalBackend`**

实现要点：

```python
class LocalBackend:
    def start(self, timeout: float = 20.0) -> str:
        # 初始化 Database/TaskQueue/FastAPI；shutdown_on_page_disconnect=False
        # 选择随机端口，在线程中 server.run()
        # 条件轮询 socket 直到端口可连接，不使用固定 sleep
        # 写 runtime.json: {"port": port, "pid": os.getpid()}
        return f"http://127.0.0.1:{port}"

    def stop(self, timeout: float = 10.0) -> None:
        self._server.should_exit = True
        self._thread.join(timeout)
        self._runtime_file.unlink(missing_ok=True)
        if self._thread.is_alive():
            raise TimeoutError("本地服务未能按时退出")
```

启动失败必须清理运行文件并重新抛出中文异常；`start()` 重复调用返回原地址；`stop()` 可重复调用。

- [ ] **Step 4: 让浏览器启动器复用通用配置函数**

仅移动无 Qt 依赖的组件构造逻辑，保持 `douyin_downloader.launcher:main` 现有行为和测试兼容，不把浏览器入口删除。

- [ ] **Step 5: 运行测试**

Run: `python -m pytest tests/test_local_backend.py tests/test_launcher.py tests/test_api.py -q`

Expected: PASS。

- [ ] **Step 6: 提交**

```powershell
git add backend/douyin_downloader/local_backend.py backend/douyin_downloader/launcher.py tests/test_local_backend.py
git commit -m "feat: add reusable local backend runtime"
```

---

### Task 3: 实现 Qt 原生目录选择桥接

**Files:**
- Create: `backend/douyin_downloader/desktop_bridge.py`
- Create: `tests/conftest.py`
- Create: `tests/test_desktop_bridge.py`

**Interfaces:**
- Consumes: Qt 主线程事件循环。
- Produces: `DirectoryPickerBridge.pick_directory() -> Path | None`，可从 Uvicorn 工作线程同步调用；内部 Qt 信号在主线程执行对话框。

- [ ] **Step 1: 添加 PySide6 开发依赖声明**

修改 `pyproject.toml`：

```toml
[project.optional-dependencies]
desktop = ["PySide6>=6.8,<7"]
```

安装：`python -m pip install -e ".[desktop]"`。

- [ ] **Step 2: 写失败测试**

先在 `tests/conftest.py` 定义所有 Qt 测试共用的真实事件循环夹具和条件等待函数：

```python
import os
import time

import pytest
from PySide6.QtWidgets import QApplication


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --no-sandbox")


@pytest.fixture(scope="session")
def qt_app() -> QApplication:
    app = QApplication.instance() or QApplication([])
    return app


def wait_for_qt(predicate, app: QApplication, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("等待 Qt 条件超时")
```

测试向桥接器注入对话框函数，避免模拟 Qt 本身：

```python
def test_directory_picker_returns_main_thread_selection(qt_app, tmp_path: Path) -> None:
    bridge = DirectoryPickerBridge(dialog=lambda: str(tmp_path))
    result: list[Path | None] = []
    worker = threading.Thread(target=lambda: result.append(bridge.pick_directory()))
    worker.start()
    wait_for_qt(lambda: not worker.is_alive(), qt_app)

    assert result == [tmp_path]
```

另测空字符串返回 `None`，桥接等待超过 60 秒抛 `TimeoutError`。

- [ ] **Step 3: 运行测试并确认失败**

Run: `python -m pytest tests/test_desktop_bridge.py -q`

Expected: ERROR，模块不存在。

- [ ] **Step 4: 实现桥接器**

使用 `QObject`、`Signal(object)` 和 `Qt.ConnectionType.QueuedConnection`。请求对象包含 `threading.Event` 与结果容器；Qt 槽调用 `QFileDialog.getExistingDirectory` 后写入结果并触发 Event。所有超时和 Qt 异常都要解除等待并返回明确异常。

- [ ] **Step 5: 运行测试并提交**

Run: `python -m pytest tests/test_desktop_bridge.py -q`

```powershell
git add pyproject.toml backend/douyin_downloader/desktop_bridge.py tests/test_desktop_bridge.py
git commit -m "feat: add Qt desktop bridge"
```

---

### Task 4: 实现不会残留锁的桌面单实例通信

**Files:**
- Create: `backend/douyin_downloader/desktop_instance.py`
- Create: `tests/test_desktop_instance.py`

**Interfaces:**
- Produces: `DesktopInstance(name: str)`，公开 `acquire() -> bool`、Qt 信号 `activation_requested`、`close() -> None`；第二实例 `acquire()` 返回 `False` 并通知第一实例。

- [ ] **Step 1: 写失败测试**

使用每个测试唯一的服务名：

```python
def test_second_instance_activates_first(qt_app) -> None:
    name = f"DouyinBatchDownloader.Test.{uuid.uuid4().hex}"
    primary = DesktopInstance(name)
    secondary = DesktopInstance(name)
    activated: list[bool] = []
    primary.activation_requested.connect(lambda: activated.append(True))

    assert primary.acquire() is True
    assert secondary.acquire() is False
    wait_for_qt(lambda: activated == [True], qt_app)

    primary.close()
    assert DesktopInstance(name).acquire() is True
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `python -m pytest tests/test_desktop_instance.py -q`

Expected: ERROR，模块不存在。

- [ ] **Step 3: 实现 `QLocalServer`/`QLocalSocket` 协调器**

首次 `listen(name)` 成功即为主实例。失败时先用 `QLocalSocket` 探测：连接成功则发送 `b"activate"` 并返回 `False`；连接失败才调用 `QLocalServer.removeServer(name)` 清理崩溃残留，再重试监听。第一实例收到消息后发射 `activation_requested`。

- [ ] **Step 4: 运行测试并提交**

Run: `python -m pytest tests/test_desktop_instance.py -q`

```powershell
git add backend/douyin_downloader/desktop_instance.py tests/test_desktop_instance.py
git commit -m "feat: add desktop single-instance activation"
```

---

### Task 5: 创建独立桌面窗口与入口

**Files:**
- Create: `backend/douyin_downloader/desktop.py`
- Create: `run_desktop.py`
- Create: `tests/test_desktop.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: `LocalBackend`、`DirectoryPickerBridge`、`DesktopInstance`。
- Produces: `DesktopWindow(QMainWindow)`、`activate_window(window) -> None`、`main() -> int`；脚本入口 `douyin-batch-downloader-desktop`。

- [ ] **Step 1: 写窗口行为失败测试**

测试真实 `QMainWindow` 可观察行为：

```python
def test_desktop_window_loads_local_app_and_closes_backend(qt_app) -> None:
    stopped: list[bool] = []
    window = DesktopWindow("http://127.0.0.1:8765", on_close=lambda: stopped.append(True))

    assert window.windowTitle() == "抖音批量下载工具"
    assert window.minimumWidth() >= 960
    window.close()
    qt_app.processEvents()

    assert stopped == [True]
```

另测 `activate_window` 会从最小化恢复并调用 `raise_()`/`activateWindow()` 后处于可见状态。

- [ ] **Step 2: 运行测试并确认失败**

Run: `python -m pytest tests/test_desktop.py -q`

Expected: ERROR，桌面模块不存在。

- [ ] **Step 3: 实现窗口**

`DesktopWindow` 内嵌 `QWebEngineView`，加载本地 URL，默认 `1200×800`、最小 `960×640`。重写 `closeEvent`，保证关闭回调只执行一次；启动加载失败显示 `QMessageBox` 和日志，不静默退出。

- [ ] **Step 4: 实现桌面 `main()`**

顺序严格为：创建 `QApplication` → 获取单实例 → 创建目录桥接 → 启动 `LocalBackend` → 创建并显示窗口 → 运行 Qt 事件循环 → `finally` 中停止后端和关闭实例服务。

第二实例只发送激活消息，不启动后端。主实例响应激活时恢复并置前。设置 `QCoreApplication` 的组织名、应用名和版本 `1.0.0`。

在创建后端前调用 `logging.basicConfig`，日志固定写入 `application_data_directory() / "application.log"`。捕获初始化、QtWebEngine 加载和退出异常，写入完整堆栈并以 `QMessageBox.critical` 显示中文摘要，禁止无窗口静默退出。

在 `pyproject.toml` 增加：

```toml
[project.scripts]
douyin-batch-downloader-desktop = "douyin_downloader.desktop:main"
```

`run_desktop.py` 只调用该 `main()`。

- [ ] **Step 5: 运行桌面相关及全量后端测试**

Run: `python -m pytest tests/test_desktop.py tests/test_desktop_bridge.py tests/test_desktop_instance.py tests/test_local_backend.py -q`

Run: `python -m pytest -q`

Expected: PASS。

- [ ] **Step 6: 提交**

```powershell
git add backend/douyin_downloader/desktop.py run_desktop.py tests/test_desktop.py pyproject.toml
git commit -m "feat: add standalone desktop window"
```

---

### Task 6: 增加 PyInstaller 目录模式桌面构建

**Files:**
- Create: `douyin_downloader_desktop.spec`
- Create: `scripts/build-desktop-app.ps1`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: `frontend/dist`、`run_desktop.py`、Python `desktop/download/build` 依赖。
- Produces: `release/desktop/app/抖音批量下载工具/抖音批量下载工具.exe` 及同目录全部依赖。

- [ ] **Step 1: 写构建验收脚本的失败条件**

`scripts/build-desktop-app.ps1` 必须执行真实命令并对退出码和产物做断言，不以源码文本作为测试：

```powershell
npm --prefix frontend ci
npm --prefix frontend test
npm --prefix frontend run build
python -m pytest -q
python -m pip install -e ".[download,desktop,build]"
python -m PyInstaller --noconfirm --clean douyin_downloader_desktop.spec
if (-not (Test-Path $desktopExe)) { throw "桌面 EXE 未生成" }
```

首次运行预期在规格文件不存在时失败。

- [ ] **Step 2: 创建目录模式规格文件**

沿用现有 yt-dlp、imageio_ffmpeg、playwright、certifi 收集逻辑，并加入 PySide6/QtWebEngine。关键结构：

```python
a = Analysis(["run_desktop.py"], pathex=["backend"], datas=datas, ...)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="抖音批量下载工具", console=False)
coll = COLLECT(exe, a.binaries, a.datas, name="抖音批量下载工具")
```

禁止使用 one-file CArchive。构建日志中的缺失模块警告逐项审核；QtWebEngineProcess、resources、translations 和 locales 必须存在于产物目录。

- [ ] **Step 3: 更新忽略规则**

确保 `/release/` 和 PyInstaller 构建目录保持忽略，不提交二进制产物。

- [ ] **Step 4: 执行真实桌面构建并检查资源**

Run: `powershell -ExecutionPolicy Bypass -File scripts/build-desktop-app.ps1`

检查：

```powershell
Test-Path release/desktop/app/抖音批量下载工具/抖音批量下载工具.exe
Get-ChildItem release/desktop/app/抖音批量下载工具 -Recurse -Filter QtWebEngineProcess.exe
Get-ChildItem release/desktop/app/抖音批量下载工具 -Recurse -Filter ffmpeg*.exe
```

Expected: 三项均找到，进程退出码 0。

- [ ] **Step 5: 提交构建定义**

```powershell
git add douyin_downloader_desktop.spec scripts/build-desktop-app.ps1 .gitignore
git commit -m "build: package standalone desktop app"
```

---

### Task 7: 创建无需管理员权限的 Inno Setup 安装程序

**Files:**
- Create: `installer/douyin-downloader.iss`
- Create: `scripts/build-installer.ps1`
- Create: `installer/使用说明.txt`

**Interfaces:**
- Consumes: Task 6 的目录模式产物和本机 Inno Setup 6 `ISCC.exe`。
- Produces: `release/installer/抖音批量下载工具-Setup-1.0.0-Windows-x64.exe`。

- [ ] **Step 1: 安装并定位 Inno Setup 6**

优先使用已安装 `ISCC.exe`；不存在时通过 `winget install --id JRSoftware.InnoSetup -e --accept-source-agreements --accept-package-agreements` 安装。构建脚本依次检查 `PATH`、`Program Files (x86)` 和 `Program Files`，仍找不到则明确失败。

- [ ] **Step 2: 创建安装脚本**

必须包含以下可执行配置：

```ini
[Setup]
AppId={{7F381B63-8EE8-4E79-B4F2-68F4C5926D32}
AppName=抖音批量下载工具
AppVersion=1.0.0
DefaultDirName={localappdata}\Programs\DouyinBatchDownloader
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\抖音批量下载工具.exe
OutputDir=..\release\installer
OutputBaseFilename=抖音批量下载工具-Setup-1.0.0-Windows-x64
Compression=lzma2/max
SolidCompression=yes

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Files]
Source: "..\release\desktop\app\抖音批量下载工具\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\抖音批量下载工具"; Filename: "{app}\抖音批量下载工具.exe"
Name: "{autodesktop}\抖音批量下载工具"; Filename: "{app}\抖音批量下载工具.exe"

[Run]
Filename: "{app}\抖音批量下载工具.exe"; Description: "启动抖音批量下载工具"; Flags: nowait postinstall skipifsilent
```

卸载段不删除 `%LOCALAPPDATA%\DouyinBatchDownloader`，也不触碰用户下载目录。

- [ ] **Step 3: 创建安装构建脚本并真实编译**

`scripts/build-installer.ps1` 先调用 Task 6 构建，再执行：

```powershell
& $isccPath /Qp installer/douyin-downloader.iss
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $setupExe)) {
    throw "安装程序构建失败"
}
Get-FileHash -Algorithm SHA256 $setupExe
```

- [ ] **Step 4: 提交安装定义**

```powershell
git add installer/douyin-downloader.iss installer/使用说明.txt scripts/build-installer.ps1
git commit -m "build: add Windows desktop installer"
```

---

### Task 8: 安装版端到端验收与文档

**Files:**
- Create: `scripts/smoke-test-installed-app.ps1`
- Modify: `README.md`

**Interfaces:**
- Consumes: Task 7 安装程序。
- Produces: 安装、窗口、HTTP、下载、退出、重启和卸载的机器可读验收结果，以及最终 SHA256。

- [ ] **Step 1: 编写端到端验收脚本**

脚本使用隔离数据目录环境变量并执行以下真实行为：

1. 静默安装到临时当前用户目录：`Setup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /DIR=<temp>`。
2. 启动安装后的 EXE，条件轮询 `runtime.json`，确认 `MainWindowHandle -ne 0`，且没有启动默认浏览器进程。
3. `GET /` 返回 200；`GET /api/settings` 可用。
4. 断开网络不作为脚本的系统级破坏操作；用不可达代理或模拟下载器的自动化测试覆盖网络错误，成品验收只验证离线界面可启动。
5. 使用用户授权的公开链接创建批次，测试暂停、继续并等待 `completed`；枚举输出目录确认一个非空 MP4。
6. 再次启动 EXE，确认仍只有一个主窗口且已有窗口被激活。
7. 向主窗口发送关闭消息，10 秒内内外进程全部结束；再次启动成功。
8. 运行 `{app}\unins000.exe /VERYSILENT /NORESTART`；安装目录消失，应用数据目录和下载文件仍存在。

任何一步失败都以非零退出码结束，并打印失败阶段、日志路径和相关进程信息。

- [ ] **Step 2: 更新 README**

明确桌面版安装方法、联网边界、Windows 支持范围、SmartScreen 提示、日志路径、卸载数据保留和构建命令。删除“会打开外部浏览器”的成品说明；保留开发模式说明。

- [ ] **Step 3: 运行最终完整验证**

Run:

```powershell
python -m pytest -q
npm --prefix frontend test
powershell -ExecutionPolicy Bypass -File scripts/build-installer.ps1
powershell -ExecutionPolicy Bypass -File scripts/smoke-test-installed-app.ps1
git diff --check
```

Expected: 所有命令退出码 0。记录安装包绝对路径、字节大小和 SHA256。

- [ ] **Step 4: 在干净 Windows 环境复核**

优先使用 Windows Sandbox 或全新 Windows 10/11 64 位虚拟机，不挂载开发 Python/Node 环境。执行安装、桌面快捷方式启动、真实下载、暂停继续、重复启动、关闭、重启和卸载。若当前机器不提供 Sandbox/VM，必须在交付说明中明确本机隔离验收范围，不能声称完成了不存在的跨机器测试。

- [ ] **Step 5: 提交验收脚本和文档**

```powershell
git add scripts/smoke-test-installed-app.ps1 README.md
git commit -m "test: verify installed desktop application"
```

---

## 最终交付检查

- [ ] `git status --short` 为空，二进制产物未进入 Git。
- [ ] 安装包名称为 `抖音批量下载工具-Setup-1.0.0-Windows-x64.exe`。
- [ ] 安装包包含 QtWebEngine、Vue、yt-dlp、FFmpeg、Playwright 和 Python 运行时。
- [ ] 安装后独立桌面窗口启动，不打开外部浏览器。
- [ ] 不联网时界面、设置和历史记录可用；联网后真实下载完成。
- [ ] 重复启动激活已有窗口；关闭后无残留进程。
- [ ] 卸载不删除应用数据和下载文件。
- [ ] 最终回复提供安装包可点击路径、文件大小、SHA256、自动化测试计数和干净系统验收范围。
