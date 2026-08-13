# 视频批量下载工具 — Code Wiki

> **项目版本**: 0.1.0 (backend), 1.1.0 (desktop installer)
> **生成日期**: 2026-08-12
> **Python**: >=3.11, <3.13

---

## 目录

1. [项目概述](#1-项目概述)
2. [整体架构](#2-整体架构)
3. [目录结构](#3-目录结构)
4. [核心运行模式](#4-核心运行模式)
5. [后端模块详解](#5-后端模块详解)
   - [入口与编排层](#51-入口与编排层)
   - [API 层](#52-api-层)
   - [存储层](#53-存储层)
   - [任务队列层](#54-任务队列层)
   - [下载核心层](#55-下载核心层)
   - [媒体处理层](#56-媒体处理层)
   - [平台适配层](#57-平台适配层)
   - [页面采集层](#58-页面采集层)
   - [辅助模块](#59-辅助模块)
   - [桌面集成层](#510-桌面集成层)
6. [前端模块详解](#6-前端模块详解)
7. [数据流与关键交互序列](#7-数据流与关键交互序列)
8. [API 接口清单](#8-api-接口清单)
9. [数据库设计](#9-数据库设计)
10. [依赖关系](#10-依赖关系)
11. [构建与发布](#11-构建与发布)
12. [运行方式](#12-运行方式)
13. [测试体系](#13-测试体系)

---

## 1. 项目概述

"视频批量下载工具"是一套**仅在本机运行**的多平台公开视频批量下载工具，支持通过粘贴链接或页面采集的方式批量下载视频，并自动完成兼容性转码（统一转为 H.264/yuv420p/AAC MP4）。

### 核心能力

| 能力 | 说明 |
|------|------|
| 链接批量下载 | 粘贴多条链接，自动解析平台、扩展合集、加入队列下载 |
| 页面批量下载 | 粘贴一个页面 URL，工具自动滚动采集页内视频并下载 |
| 多平台支持 | 抖音、快手、B站、唯品会商品主视频、视频直链、YouTube 等 yt-dlp 支持平台 |
| 自动转码 | 下载完成后自动检测编码，非 H.264 视频通过 FFmpeg 转码 |
| 去重 & 跳过 | 同一批次内去重；跨批次检测历史已完成视频自动跳过 |
| 断点续传 | 支持 yt-dlp 内置的断点续传 |
| 单实例守护 | 启动时检测已有实例，避免重复运行 |
| 全本机隐私 | 所有数据仅存储在本机 SQLite，无后台上传 |

### 两种运行模式

| 模式 | 入口 | 前端 | 适用场景 |
|------|------|------|----------|
| 浏览器模式 | `run_app.py` / `launcher.py` | 系统浏览器打开 SPA | 便携版 / 开发调试 |
| 桌面模式 | `run_desktop.py` / `desktop.py` | PySide6 QWebEngine 内嵌窗口 | 安装版 / 最终用户 |

---

## 2. 整体架构

```
┌──────────────────────────────────────────────────────────────────────┐
│                        Frontend (Vue 3 SPA)                          │
│  ┌─────────────┐  ┌────────────┐  ┌──────────┐  ┌───────────────┐   │
│  │  App.vue    │  │ format.js  │  │history.js│  │ new-batch.js  │   │
│  │  (主组件)    │  │ (格式化)   │  │(分页逻辑) │  │ (状态重置)    │   │
│  └─────────────┘  └────────────┘  └──────────┘  └───────────────┘   │
├──────────────────────────────────────────────────────────────────────┤
│                         HTTP / SSE                                   │
├──────────────────────────────────────────────────────────────────────┤
│                     Backend (FastAPI)                                │
│  ┌──────────────────────────────────────────────────────────────┐    │
│  │  api.py          REST API + SSE endpoints                    │    │
│  ├──────────────────────────────────────────────────────────────┤    │
│  │  launcher.py     浏览器模式入口，组件装配与生命周期            │    │
│  │  desktop.py      桌面模式入口，PySide6 窗口管理               │    │
│  │  local_backend.py 在后台线程中运行 FastAPI                    │    │
│  ├──────────────────────────────────────────────────────────────┤    │
│  │  queue.py         TaskQueue (多线程下载队列)                  │    │
│  │  downloader.py    YtDlpDownloader (yt-dlp 封装)              │    │
│  │  media.py         FFmpegMediaProcessor (转码)                │    │
│  ├──────────────────────────────────────────────────────────────┤    │
│  │  platforms.py     BatchExpander (URL → 视频条目展开)          │    │
│  │  kuaishou.py      KuaishouIE (快手自定义提取器)               │    │
│  │  vipshop.py       VipshopIE (唯品会自定义提取器)              │    │
│  │  direct_media.py  DirectMediaIE (视频直链提取器)             │    │
│  ├──────────────────────────────────────────────────────────────┤    │
│  │  page_collector.py 页面视频采集 (Playwright 浏览器自动化)     │    │
│  │  cookies.py       匿名 Cookie 管理 (Netscape 格式)            │    │
│  ├──────────────────────────────────────────────────────────────┤    │
│  │  store.py         SQLite 数据库 (批次、任务、设置)             │    │
│  │  links.py         URL 解析与校验                              │    │
│  │  resolver.py      短链解析与直链探测                          │    │
│  │  filenames.py     文件名清洗与去重                            │    │
│  │  page_sessions.py 页面会话追踪 (自动关闭)                     │    │
│  └──────────────────────────────────────────────────────────────┘    │
├──────────────────────────────────────────────────────────────────────┤
│                      External Dependencies                           │
│  ┌──────────┐  ┌───────────┐  ┌──────────┐  ┌──────────────────┐    │
│  │ yt-dlp   │  │ Playwright│  │ FFmpeg   │  │ curl-cffi        │    │
│  │ (下载)   │  │ (浏览器)  │  │ (转码)   │  │ (快手指纹)       │    │
│  └──────────┘  └───────────┘  └──────────┘  └──────────────────┘    │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 3. 目录结构

```
网页自动拔取视频工具/
├── run_app.py                       # 浏览器模式入口
├── run_desktop.py                   # 桌面模式入口
├── pyproject.toml                   # 项目元数据与依赖声明
├── douyin_downloader.spec           # PyInstaller 便携版 spec
├── douyin_downloader_desktop.spec   # PyInstaller 桌面版 spec
│
├── backend/
│   └── douyin_downloader/           # 核心后端包
│       ├── __init__.py              # 包声明，版本号 0.1.0
│       ├── launcher.py              # 浏览器模式主入口，组件装配
│       ├── desktop.py               # 桌面模式主入口，PySide6 窗口
│       ├── desktop_bridge.py        # Qt 目录选择器桥接
│       ├── desktop_instance.py      # Qt 单实例管理
│       ├── local_backend.py         # 后台线程托管 FastAPI
│       ├── api.py                   # FastAPI 应用与路由
│       ├── store.py                 # SQLite 数据库操作
│       ├── queue.py                 # 多线程任务队列
│       ├── downloader.py            # yt-dlp 下载器封装
│       ├── media.py                 # FFmpeg 媒体分析与转码
│       ├── platforms.py            # 链接平台扩展器
│       ├── kuaishou.py             # 快手 InfoExtractor
│       ├── vipshop.py              # 唯品会 InfoExtractor
│       ├── direct_media.py         # 视频直链 InfoExtractor
│       ├── page_collector.py       # 浏览器页面采集
│       ├── page_sessions.py        # SSE 页面会话追踪
│       ├── cookies.py              # Cookie 持久化与刷新
│       ├── links.py                # URL 解析与验证
│       ├── resolver.py             # 短链/直链解析
│       └── filenames.py            # 文件名安全处理
│
├── frontend/
│   ├── src/
│   │   ├── main.js                 # Vue 应用入口
│   │   ├── App.vue                 # 单文件主组件
│   │   ├── format.js               # 格式化工具函数
│   │   ├── history.js              # 分页删除后处理
│   │   ├── new-batch.js            # 新建批次状态重置
│   │   └── styles.css              # 全局样式
│   ├── tests/                      # 前端单元测试 (vitest)
│   ├── index.html                  # HTML 入口
│   ├── package.json                # npm 依赖
│   └── vite.config.js              # Vite 构建配置
│
├── tests/                          # 后端单元测试 (pytest)
│   ├── conftest.py                 # pytest fixtures
│   ├── test_api.py                 # API 端点测试
│   ├── test_queue.py               # 下载队列测试
│   ├── test_downloader.py          # 下载器测试
│   ├── test_store.py               # 数据库测试
│   ├── test_platforms.py           # 平台扩展测试
│   ├── test_kuaishou.py            # 快手提取测试
│   ├── test_vipshop.py             # 唯品会提取测试
│   ├── test_direct_media.py        # 直链提取测试
│   ├── test_media.py               # 媒体处理测试
│   ├── test_links.py               # URL 解析测试
│   ├── test_resolver.py            # 链接解析测试
│   ├── test_filenames.py           # 文件名测试
│   ├── test_cookies.py             # Cookie 测试
│   ├── test_page_collector.py      # 页面采集测试
│   ├── test_page_sessions.py       # 页面会话测试
│   ├── test_launcher.py            # 启动器测试
│   ├── test_local_backend.py       # 本地后端测试
│   ├── test_desktop.py             # 桌面窗口测试
│   ├── test_desktop_bridge.py      # 目录选择桥测试
│   └── test_desktop_instance.py    # 单实例测试
│
├── scripts/
│   ├── build.ps1                   # 便携版构建脚本
│   ├── build-desktop-app.ps1       # 桌面版构建脚本
│   ├── build-installer.ps1         # 安装器构建脚本
│   └── smoke-test-installed-app.ps1 # 冒烟测试脚本
│
├── installer/
│   ├── douyin-downloader.iss       # Inno Setup 安装器配置
│   └── 使用说明.txt                 # 安装器附带说明
│
├── docs/superpowers/               # 设计文档与计划
│   ├── plans/                      # 功能计划
│   └── specs/                      # 设计规格
│
├── release/                        # 发布产物目录
└── dist/                           # PyInstaller 输出目录
```

---

## 4. 核心运行模式

### 4.1 浏览器模式 (`launcher.py`)

```
run_app.py
  └─> launcher.main()
        ├─> SingleInstance.acquire()           # Windows Mutex 单实例检测
        ├─> Database.initialize()              # 初始化 SQLite
        ├─> TaskQueue(database, downloader, 2) # 创建下载队列 (2 个工作线程)
        ├─> PageCollectionManager(...)         # 创建页面采集管理器
        ├─> create_app(...)                     # 创建 FastAPI 应用
        ├─> uvicorn.Server.run()               # 启动 HTTP 服务
        └─> webbrowser.open(...)               # 自动打开浏览器
```

关键设计：
- **单实例**：通过 Windows Named Mutex (`Local\DouyinBatchDownloader.SingleInstance`) 实现，新实例自动打开已有实例的页面
- **端口**：自动选择可用端口（`select_available_port()`），写入 `runtime.json`
- **数据目录**：`%LOCALAPPDATA%\DouyinBatchDownloader`（可通过 `DOUYIN_DOWNLOADER_DATA_DIR` 覆盖）
- **前端**：通过 `StaticFiles` 挂载预构建的 `frontend/dist`

### 4.2 桌面模式 (`desktop.py`)

```
run_desktop.py
  └─> desktop.main()
        ├─> QApplication 初始化
        ├─> DesktopInstance.acquire()          # Qt LocalServer 单实例
        ├─> LocalBackend.start()               # 后台线程运行 FastAPI
        ├─> DesktopWindow(QWebEngineView)       # 嵌入式浏览器窗口
        └─> application.exec()                  # Qt 事件循环
```

关键设计：
- **单实例**：通过 QLocalServer/QLocalSocket 实现，新实例通知已有窗口激活
- **内嵌浏览器**：QWebEngineView 直接加载本地 FastAPI 服务
- **目录选择**：`DirectoryPickerBridge` 通过 Qt 信号机制将 `QFileDialog` 桥接到同步的 `pick_directory` 回调
- **窗口配置**：1200×800 默认尺寸，最小 960×640

---

## 5. 后端模块详解

### 5.1 入口与编排层

#### `launcher.py` — 浏览器模式主入口

| 关键类/函数 | 职责 |
|-------------|------|
| `main()` | 装配所有组件，启动 uvicorn 服务器，打开浏览器 |
| `SingleInstance` | 基于 Windows Mutex 的单实例管理 |
| `application_data_directory()` | 确定应用数据目录（支持环境变量覆盖） |
| `select_available_port()` | 绑定随机端口，返回可用端口号 |
| `find_frontend_directory()` | PyInstaller 打包后定位 `frontend/dist` 目录 |
| `find_ffmpeg()` | 通过 `imageio-ffmpeg` 定位 FFmpeg 可执行文件 |
| `create_downloader()` | 创建 `YtDlpDownloader` 实例 |
| `create_server_config()` | 创建 `uvicorn.Config` |

核心组件装配链路：

```python
database = Database(...)
downloader = create_downloader(data_dir, ffmpeg_path)
queue = TaskQueue(database, downloader, worker_count=2)
page_manager = PageCollectionManager(database, queue, ...)
app = create_app(database, queue, ..., page_collection_manager=page_manager)
```

#### `desktop.py` — 桌面模式主入口

| 关键类/函数 | 职责 |
|-------------|------|
| `main()` | Qt 事件循环主入口，返回退出码 |
| `DesktopWindow(QMainWindow)` | PySide6 桌面窗口，内嵌 QWebEngineView |
| `activate_window()` | 激活已有窗口（恢复最小化、置顶） |

`DesktopWindow` 主要属性：
- 包含 `QWebEngineView` 加载本地后端 URL
- 关闭时触发 `on_close` 回调（`application.quit`）
- 页面加载失败时显示 QMessageBox 错误提示

#### `local_backend.py` — 后台服务托管

| 关键类/函数 | 职责 |
|-------------|------|
| `LocalBackend` | 在 daemon 线程中运行 FastAPI，管理生命周期 |

```python
class LocalBackend:
    def start(timeout=20) -> str    # 启动后台线程，返回 base_url
    def stop(timeout=10)  -> None   # 停止后台线程
    def is_running -> bool          # 检查运行状态
```

启动过程：创建 Database → 创建下载器 → 创建 TaskQueue → 创建 PageCollectionManager → 创建 FastAPI 应用 → 在 daemon 线程中启动 uvicorn → 等待端口可用 → 写入 runtime.json。

---

### 5.2 API 层

#### `api.py` — FastAPI 应用与路由

| 端点 | 方法 | 功能 |
|------|------|------|
| `/api/batches/preview` | POST | 预览链接，返回有效/重复/无效计数 |
| `/api/batches` | POST | **创建下载批次**（核心入口） |
| `/api/batches` | GET | 分页列出历史批次 |
| `/api/batches` | DELETE | 一键删除所有批次 |
| `/api/batches/{id}` | GET | 获取单个批次详情 |
| `/api/batches/{id}` | DELETE | 删除单个批次 |
| `/api/events` | GET | SSE 流，推送批次实时状态 |
| `/api/batches/{id}/retry-failed` | POST | 重试批次中所有失败任务 |
| `/api/batches/{id}/pause` | POST | 暂停批次 |
| `/api/batches/{id}/resume` | POST | 恢复批次 |
| `/api/settings` | GET | 获取设置 |
| `/api/settings/pick-directory` | POST | 打开目录选择对话框 |
| `/api/settings/open-directory` | POST | 在文件管理器中打开下载目录 |
| `/api/app/shutdown` | POST | 优雅关闭程序 |
| `/api/app/session` | GET | SSE 流，追踪前端连接状态 |

**`create_app()` 工厂函数**接收以下依赖注入参数：

| 参数 | 类型 | 说明 |
|------|------|------|
| `database` | `Database` | 数据库实例 |
| `queue` | `QueueController` | 任务队列控制器 |
| `default_download_dir` | `Path` | 默认下载目录 |
| `pick_directory` | `Callable` | 打开目录选择器的回调 |
| `open_directory` | `Callable` | 打开文件管理器的回调 |
| `shutdown_callback` | `Callable` | 关闭程序回调 |
| `static_dir` | `Path` or None | 前端静态资源目录 |
| `short_link_resolver` | `Callable` or None | 短链解析器 |
| `batch_expander` | `BatchExpander` or None | 平台扩展器 |
| `page_collection_manager` | `PageCollectionController` or None | 页面采集管理器 |
| `shutdown_on_page_disconnect` | `bool` | 前端断开时是否自动退出 |

**SSE 进度推送** (`stream_batch_events`)：
- 每 0.5 秒轮询数据库批次状态
- 仅在状态变化时推送 `event: batch` 事件
- 空闲时发送 `: keep-alive` 注释行保持连接

**创建批次流程** (`POST /api/batches`)：

```
1. preview_links(text) → 提取并验证 URL
2. 判断 source_mode:
   ├─ "page" → 短链解析 → 验证搜索模式 → create_page_batch → 提交采集
   └─ "links" → 逐条短链解析 → extractor_supports_url 检查
               → BatchExpander.expand() 平台扩展
               → create_expanded_batch → skip_existing_completed 去重
               → queue.wake() 唤醒工作线程
```

---

### 5.3 存储层

#### `store.py` — SQLite 数据库操作

**核心类**:

| 类/枚举 | 说明 |
|---------|------|
| `TaskStatus(StrEnum)` | 任务状态枚举：`QUEUED`, `RESOLVING`, `DOWNLOADING`, `MERGING`, `TRANSCODING`, `COMPLETED`, `FAILED`, `SKIPPED` |
| `TaskRecord` | dataclass：`id`, `batch_id`, `original_url`, `canonical_url`, `video_id`, `title`, `status`, `progress`, `output_dir`, `attempts`, `platform` |
| `Database` | SQLite 操作主类，线程安全（`RLock`） |

**Database 关键方法**:

| 方法 | 说明 |
|------|------|
| `initialize()` | 建表/迁移（WAL 模式，自动添加缺失列） |
| `create_batch(urls, output_dir)` | 创建链接批次（旧版） |
| `create_expanded_batch(videos, output_dir)` | 创建展开后的批次（新版，含平台信息） |
| `create_page_batch(url, count, dir)` | 创建页面采集批次 |
| `append_page_video(batch_id, video)` | 页面采集追加单条视频（含去重校验） |
| `get_batch(batch_id)` | 获取批次详情（含所有任务） |
| `list_batches(page, page_size)` | 分页列出批次 |
| `list_batch_ids()` | 列出所有批次 ID |
| `list_resumable_page_batches()` | 列出可恢复的页面采集批次 |
| `update_collection(batch_id, status, reason)` | 更新采集状态 |
| `delete_batch(batch_id)` | 级联删除批次和任务 |
| `set_batch_paused(batch_id, paused)` | 设置批次暂停状态 |
| `claim_next_task()` | **原子化领取任务**（CAS 更新状态为 RESOLVING） |
| `update_task(task_id, **fields)` | 更新任务字段（白名单校验） |
| `recover_interrupted()` | 启动时恢复中断中的任务为 QUEUED |
| `retry_failed(batch_id)` | 重置失败任务为 QUEUED |
| `skip_existing_completed(batch_id)` | 跨批次去重：标记历史已完成视频为 SKIPPED |
| `get_setting(key)` / `set_setting(key, value)` | 键值对设置管理 |

**数据库表结构**:

```sql
batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    output_dir TEXT NOT NULL,
    paused INTEGER NOT NULL DEFAULT 0,
    source_mode TEXT NOT NULL DEFAULT 'links',  -- 'links' | 'page'
    source_url TEXT,                             -- 页面采集源 URL
    requested_count INTEGER,                    -- 页面采集请求数量
    collected_count INTEGER NOT NULL DEFAULT 0, -- 已采集数量
    collection_status TEXT,                     -- pending | waiting_login | collecting | ...
    collection_stop_reason TEXT,
    created_at TEXT NOT NULL
)

tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    original_url TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    platform TEXT NOT NULL DEFAULT 'douyin',
    video_id TEXT,
    title TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    progress REAL NOT NULL DEFAULT 0,
    downloaded_bytes INTEGER NOT NULL DEFAULT 0,
    total_bytes INTEGER,
    speed REAL, eta REAL,
    output_path TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    error_code TEXT, error_message TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
)

settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
```

---

### 5.4 任务队列层

#### `queue.py` — 多线程任务队列

| 关键类/函数 | 职责 |
|-------------|------|
| `TaskQueue` | 管理 2 个 daemon 工作线程的核心队列 |

**`TaskQueue` 核心属性与行为**:

```
worker_count = 2            # 工作线程数
retry_delays = (2.0, 8.0)  # 重试延迟（秒）
minimum_free_bytes = 1 GB   # 最少剩余磁盘空间

_start()  → 启动 2 个 daemon 线程执行 _worker_loop
_stop()   → 设置停止事件，等待线程退出（超时 10s）
wake()    → 触发唤醒事件
```

**`_worker_loop` 主循环**（每个线程）:

```
while not stopped:
    task = database.claim_next_task()    # 原子领取任务
    if task is None:                     # 空闲
        sleep(0.2s), continue
    if batch is cancelled:               # 已取消
        cleanup_partial_files, continue
    if batch is paused:                  # 已暂停
        update_task(QUEUED), continue
    if disk_space < 1GB:                 # 磁盘空间不足
        set pause_reason, continue
    _process_task(task)
```

**`_process_task` 重试逻辑**:

```
for attempt in 1..3:
    if cancelled/paused/stopped → 退出
    update_task(status=RESOLVING)
    try:
        result = downloader.download(task, on_progress)
        → update_task(status=COMPLETED, ...)
        return
    except DownloadCancelled → 清理分片
    except DownloadPaused → 恢复为 QUEUED
    except DownloadError:
        if error is retryable and attempt < 3:
            等待延迟后重试
        else:
            update_task(status=FAILED)
            return
```

**可重试错误**：`NETWORK`, `RATE_LIMITED`, `FRESH_COOKIE`

**`_on_progress` 进度回调**：
- 检查取消/暂停状态，抛出 `DownloadCancelled` / `DownloadPaused`
- 将下载事件映射为任务状态更新

**批次管理**:
- `cancel_batch(batch_id)` → 标记取消 + 清理分片文件（`.douyin-part/task-{id}.*`）
- `pause_batch(batch_id)` → 数据库标记 paused + 内存标记
- `resume_batch(batch_id)` → 恢复暂停状态

---

### 5.5 下载核心层

#### `downloader.py` — yt-dlp 下载器封装

| 关键类/函数 | 职责 |
|-------------|------|
| `YtDlpDownloader` | 封装 yt-dlp 的下载主类 |
| `ErrorCode(StrEnum)` | 13 种错误码枚举 |
| `DownloadError` | 自定义异常（code + user_message） |
| `DownloadCancelled` | 取消异常 |
| `DownloadPaused` | 暂停异常 |
| `DownloadResult` | dataclass: `video_id`, `title`, `output_path` |

**`YtDlpDownloader` 关键设计**:

```
构造参数:
  - ydl_factory:                         工厂函数，默认 _default_ydl_factory
  - ffmpeg_location:                     用于合并与转码
  - cookie_file / cookie_provider:       Cookie 管理
  - media_processor:                     媒体转码处理器

默认 ydl_factory 注册的自定义提取器:
  - KuaishouIE()    # 快手
  - VipshopIE()     # 唯品会
  - DirectMediaIE() # 视频直链
```

**`download()` 方法流程**:

```
1. 创建临时目录 .douyin-part/
2. 构造 yt-dlp 选项（H.264 优先格式、断点续传、重试 3 次）
3. 加载 Cookie 文件
4. ydl.extract_info(url, download=True)
   ├─ 成功 → 获取下载路径
   ├─ FRESH_COOKIE 错误 → refresh_cookie → 重试
   └─ 其他错误 → classify_download_error → DownloadError
5. 媒体处理:
   ├─ 有 media_processor → ensure_compatible 转码
   └─ 无 → 直接使用原始文件
6. 移动到最终输出路径（build_unique_output_path）
7. 返回 DownloadResult
```

**`classify_download_error(message)` 错误分类逻辑**:
根据错误消息关键词，映射到 13 种 `ErrorCode`：
- `快手匿名访问失败` → `KUAISHOU_ACCESS`
- `唯品会匿名访问失败` → `VIPSHOP_ACCESS`
- `429/too many/rate limit` → `RATE_LIMITED`
- `fresh cookie` → `FRESH_COOKIE`
- `private/login/sign in` → `LOGIN_REQUIRED`
- `10204/ip blocked/access denied` → `ACCESS_RESTRICTED`
- `not available/404` → `UNAVAILABLE`
- `no space/permission/disk` → `DISK_ERROR`
- `ffmpeg/postprocessing/merge` → `MERGE_ERROR`
- `connection/timed out/timeout/network/dns` → `NETWORK`
- `受支持的平台专用解析器` → `UNSUPPORTED_PLATFORM`
- 其他 → `UNKNOWN`

**H.264 优先格式选择器**:

```python
H264_PREFERRED_FORMAT = "/".join((
    "bestvideo[vcodec^=avc1]+bestaudio[acodec=mp4a.40.2]",
    "bestvideo[vcodec^=h264]+bestaudio[acodec=mp4a.40.2]",
    "best[vcodec^=avc1][acodec=mp4a.40.2]",
    "best[vcodec^=h264][acodec=mp4a.40.2]",
    "bestvideo[vcodec^=avc1]+bestaudio",
    "bestvideo[vcodec^=h264]+bestaudio",
    "best[vcodec^=avc1]",
    "best[vcodec^=h264]",
    "bestvideo*+bestaudio/best",
))
```

**Cookie 刷新 (`_refresh_cookie`)**:
线程安全的 Cookie 刷新机制：如果首次下载因 Cookie 过期失败，调用 `cookie_provider.refresh(url)` 重新获取 Cookie，然后用新 Cookie 重试。

---

### 5.6 媒体处理层

#### `media.py` — FFmpeg 媒体分析与转码

| 关键类/函数 | 职责 |
|-------------|------|
| `MediaInfo` | dataclass: `video_codec`, `pixel_format`, `audio_codec`, `audio_profile`, `duration` |
| `probe_media(ffmpeg, source)` | 运行 FFmpeg 分析媒体信息 |
| `FFmpegMediaProcessor` | 转码处理主类 |

**`FFmpegMediaProcessor.ensure_compatible(source, output)`**:

```
1. probe_media(source) → 分析原始编码
2. _build_command(source, output, media) → 构建 FFmpeg 命令
   - 视频: 已是 H.264/yuv420p → copy; 否则 → libx264 重编码
   - 音频: 已是 AAC-LC → copy; 否则 → aac 重编码
   - 通用: -movflags +faststart（Web 渐进式播放优化）
3. 执行 FFmpeg，通过 out_time_us= 行解析进度
4. 转码后再次 probe 验证输出兼容性
5. 失败时清理部分文件
```

**兼容性判断标准**:
- 视频：codec 为 `h264` 或 `avc1` 且 pixel_format 为 `yuv420p`
- 音频：无音频流，或 codec 为 `aac` 且 profile 为 `LC`

#### `resolver.py` — 短链与直链解析

| 关键类/函数 | 职责 |
|-------------|------|
| `resolve_public_url(url, client)` | 异步 HTTP 重定向跟随（最多 5 跳），同时进行 DNS 安全性校验 |
| `probe_public_video_url(url)` | 同步 HEAD/GET 探测视频直链元信息 |
| `DirectMediaProbe` | dataclass: `final_url`, `content_type`, `content_length`, `filename` |
| `LinkResolutionError` | 链接解析异常 |

**安全性校验** (`_validate_public_target`):
- 必须是公网 HTTP/HTTPS
- DNS 解析结果必须是公网 IP（过滤内网地址）
- 防止 SSRF 攻击

---

### 5.7 平台适配层

#### `platforms.py` — 链接平台扩展器

| 关键类/函数 | 职责 |
|-------------|------|
| `BatchExpander` | 将 URL 列表展开为可下载的视频条目列表 |
| `ExpandedVideo` | dataclass: `platform`, `video_id`, `title`, `canonical_url`, `original_url` |
| `ExpansionResult` | dataclass: `videos`, `input_count`, `duplicate_count`, `platform_counts` |
| `extractor_supports_url(url)` | 检测是否有专用平台解析器支持该 URL |

**`BatchExpander.expand()` 处理流程**:

```
对每个 URL:
  1. 抖音特殊处理: 从 /video/{id} 直接提取 video_id，无需网络请求
  2. 其他: 通过 yt-dlp extract_info(url, download=False) 获取元信息
  3. 处理播放列表 (playlist/multi_video): 展开 entries
  4. 过滤: 跳过直播、图集
  5. 去重: 同一 (platform, video_id) 只保留一个
  6. 数量限制: 每批次最多 500 个视频
```

**平台识别 (`_platform_slug`)**:
```
kuaishou → "kuaishou"
vipshop  → "vipshop"
bilibili → "bilibili"
douyin/tiktok → "douyin"
direct-media → "direct"
其他 → extractor_key 或 hostname
```

#### `kuaishou.py` — 快手视频提取器

| 关键类/函数 | 职责 |
|-------------|------|
| `KuaishouIE(InfoExtractor)` | yt-dlp 自定义提取器，注册名 "kuaishou" |
| `parse_kuaishou_page(html, photo_id)` | 解析 PC 端页面（`window.__APOLLO_STATE__`） |
| `parse_kuaishou_mobile_page(html, photo_id)` | 解析移动端页面（`window.INIT_STATE`） |
| `cache_kuaishou_info()` | 内存缓存（TTL 600s） |

**快手提取的多层降级策略**:
1. 优先用 `curl-cffi`（模拟 Chrome Android）请求移动端
2. 移动端失败则回退 PC 端（模拟 Chrome）
3. PC 端支持通过 URL 参数 `ztDid` 传入设备 ID
4. 双重解析支持：`window.__APOLLO_STATE__` 和 `window.INIT_STATE`
5. 解析结果缓存 10 分钟

**视频格式提取**: 从 Apollo State 的 `videoResource.json` 或移动端 `manifest` 中提取 h264/h265 adaptionSet → representation。

#### `vipshop.py` — 唯品会商品视频提取器

| 关键类/函数 | 职责 |
|-------------|------|
| `VipshopIE(InfoExtractor)` | yt-dlp 自定义提取器，注册名 "vipshop" |
| `resolve_vipshop_page(url, product_id)` | Playwright 浏览器提取 |
| `parse_vipshop_payload(payload, product_id)` | 解析商品详情 API 响应 |
| `VipshopVideoInfo` | dataclass: `product_id`, `title`, `video_url`, `thumbnail`, `webpage_url` |

**提取流程**:
1. Playwright（headless Edge）打开商品详情页
2. 拦截 `/shopping/pc/detail/main/v6` API 响应
3. 从 `data.base.shortVideoUrl` 提取视频地址
4. 对视频地址做 HTTP 探测以获取最终直链

#### `direct_media.py` — 视频直链提取器

| 关键类/函数 | 职责 |
|-------------|------|
| `DirectMediaIE(InfoExtractor)` | yt-dlp 自定义提取器，注册名 "direct-media" |

**`suitable()` 匹配条件**: 任何公网 HTTP URL，且不被其他专用提取器匹配。

**提取逻辑**:
1. 通过 `probe_public_video_url()` 探测 URL
2. 根据 Content-Type 分三种处理：
   - `m3u8` → `_extract_m3u8_formats()`
   - `mpd` (DASH) → `_extract_mpd_formats()`
   - 其他视频类型 → 单一 direct format
3. 使用 SHA256 前 16 位生成 video_id

---

### 5.8 页面采集层

#### `page_collector.py` — 浏览器页面视频采集

| 关键类/函数 | 职责 |
|-------------|------|
| `BrowserPageCollector` | 页面采集核心逻辑（超时/滚轮/登录等待） |
| `PlaywrightBrowserSession` | Playwright 浏览器会话管理（Edge 持久化 profile） |
| `PageCollectionManager` | 页面采集任务管理器（多线程调度） |
| `CollectionControl` | 采集过程的取消/暂停控制 |
| `CollectionOutcome` | 采集结果: `status` + `reason` |

**`BrowserPageCollector.collect()` 采集循环**:

```
1. session.open(source_url)  → 打开页面
2. [抖音搜索页] 等待登录或风险控制
   - 检测登录提示 ("登录后即可搜索" 等)
   - 检测风控 ("安全验证" 等)
   - 自动 reload 重试
3. 进入采集循环:
   while not cancelled:
     ├─ 风控 → 等待验证
     ├─ 登录过期 → 等待重登
     ├─ 提取视频卡片:
     │   ├─ 抖音搜索: douyin_cards() + douyin_response_cards()
     │   └─ 通用页面: generic_candidates() (DOM + 网络拦截)
     ├─ 去重后逐个 on_video(video)
     ├─ 空闲轮次检查: 连续 ${max_idle_rounds} 轮无新视频 → 结束
     └─ scroll() → wait(1000ms)
4. session.close()
```

**`PlaywrightBrowserSession` 核心能力**:

| 方法 | 说明 |
|------|------|
| `open(url)` | 启动持久化 Edge Profile，拦截网络响应 |
| `login_required()` | 检测页面是否显示登录提示 |
| `risk_controlled()` | 检测页面是否触发风控验证 |
| `douyin_cards()` | 提取页面中所有 `/video/` 链接 |
| `douyin_response_cards()` | 从 XHR 响应中提取综合搜索结果 |
| `generic_candidates()` | 从 DOM video/src 标签 + 网络拦截中提取视频候选 |
| `sync_cookies()` | 导出当前 Cookie 为 Netscape 格式 |
| `scroll()` | 执行 JS 滚动 |
| `close()` | 关闭浏览器，同步 Cookie |

**`PageCollectionManager`**:
- 管理多个并发采集线程（每个批次一个线程）
- 启动时自动恢复中断的页面采集批次 (`list_resumable_page_batches()`)
- 通过 `_collection_lock` 确保同一时间只有一个采集器在执行（串行化以避免浏览器资源冲突）

**视频提取函数**:
- `extract_douyin_card_videos(cards, url)` — 从卡片列表提取 ExpandedVideo，过滤直播/广告/推广
- `extract_douyin_response_cards(payload)` — 递归遍历 API 响应 JSON，提取 `aweme_info` 中的视频
- `extract_generic_media_videos(candidates, url)` — 从通用候选列表中提取视频直链

#### `page_sessions.py` — 页面会话追踪

| 关键类/函数 | 职责 |
|-------------|------|
| `PageSessionTracker` | 追踪前端 SSE 连接状态，闲置超时后自动退出 |

**设计**:
- 前端通过 `GET /api/app/session` 维持 SSE 长连接
- 连接建立 → `active_sessions += 1`
- 连接断开 → `active_sessions -= 1`
- 如果曾经连接过且当前无活跃连接 → 等待 `grace_seconds` 后调用 `shutdown_callback`

此机制用于浏览器模式：用户关闭标签页后自动退出整个程序。

---

### 5.9 辅助模块

#### `links.py` — URL 解析与验证

| 关键类/函数 | 职责 |
|-------------|------|
| `LinkPreview` | dataclass: `valid_urls`, `duplicate_count`, `invalid_count` |
| `preview_links(text)` | 从多行文本中逐行提取和验证 URL |
| `extract_candidate_urls(text)` | 正则匹配 `https?://` 链接 |
| `is_public_http_url(url)` | 验证是否为安全公网 URL（过滤 localhost、内网 IP、非标准端口） |
| `normalize_url(url)` | 标准化 URL（去除无关参数如 B站 spm_id_from） |

#### `filenames.py` — 文件名安全处理

| 关键类/函数 | 职责 |
|-------------|------|
| `sanitize_title(title, video_id)` | 替换非法字符，截断到 120 字符，处理保留名 |
| `build_unique_output_path(dir, title, ext)` | 生成唯一文件路径（后缀递增 `(2)`, `(3)`...） |

#### `cookies.py` — Cookie 管理

| 关键类/函数 | 职责 |
|-------------|------|
| `write_netscape_cookie_file(cookies, path)` | 将 Cookie 列表写入 Netscape 格式文件 |
| `AnonymousCookieProvider` | 匿名 Cookie 提供者，需时自动通过 Playwright 获取 |

**`AnonymousCookieProvider.refresh(url)` 流程**:
1. 启动 headless Edge 持久化 profile
2. 访问目标 URL，等待 `s_v_web_id` 和 `ttwid` Cookie 出现
3. 额外等待 5 秒以获取完整 Cookie
4. 导出为 Netscape 格式文件

---

### 5.10 桌面集成层

#### `desktop_instance.py` — 单实例管理（Qt）

| 关键类/函数 | 职责 |
|-------------|------|
| `DesktopInstance` | 基于 QLocalServer/QLocalSocket 的单实例控制 |

```
acquire():
  1. 尝试连接已有实例 → 发送 "activate" → 返回 False
  2. 连接失败 → 创建 QLocalServer → 返回 True

已有实例收到 "activate" → activation_requested 信号 → 激活窗口
```

#### `desktop_bridge.py` — 目录选择桥接

| 关键类/函数 | 职责 |
|-------------|------|
| `DirectoryPickerBridge` | 将异步的 Qt 信号同步化为 `pick_directory()` 回调 |

```
pick_directory() 调用:
  ├─ 已在 Qt 线程 → 直接调用 QFileDialog
  └─ 非 Qt 线程 → 发射 _requested 信号 → Qt 主线程处理
       └─ 通过 threading.Event 等待结果（超时 60s）
```

---

## 6. 前端模块详解

前端是单文件 Vue 3 SPA，无路由库，通过 `window.location.hash` 手动控制视图。

### 文件说明

| 文件 | 职责 |
|------|------|
| `main.js` | 创建 Vue 应用，挂载到 `#app` |
| `App.vue` | 主组件（单文件，含 `<script setup>` + `<template>`） |
| `format.js` | 数据格式化：`formatBytes`, `formatSpeed`, `formatEta`, `statusLabel` |
| `history.js` | 删除后自动调整分页页码逻辑 |
| `new-batch.js` | 新建批次时重置所有相关响应式状态 |
| `styles.css` | 全局样式 |

### `App.vue` 核心逻辑

**响应式状态**:
| 变量 | 类型 | 说明 |
|------|------|------|
| `activeView` | ref | `"links"` 或 `"pages"` |
| `batch` | ref | 当前查看/监控的批次 |
| `preview` | ref | 链接预览结果 |
| `history` | ref | 批次历史（分页） |
| `historyOpen` | ref | 历史抽屉是否打开 |
| `eventSource` | var | 当前 SSE 连接 |

**SSE 连接管理**:
- `connectEvents(batchId)` — 创建 `EventSource` 连接 `/api/events`
- `closeCurrentEvents()` — 关闭当前 SSE
- 页面卸载时自动关闭 (`onBeforeUnmount`)

**视图切换**:
- 通过 `window.location.hash` 区分 `#/links` 和 `#/pages`
- `navigateTo(view)` 切换视图并关闭历史抽屉
- `hashchange` 事件同步 URL hash 与 `activeView`

**历史抽屉**:
- 右侧滑出面板，显示批次历史列表
- 支持分页、查看、单个删除、一键删除所有批次
- ESC 键关闭

**关键交互流程**:
- **链接下载**: 粘贴链接 → 失焦时自动预览 → 选择目录 → 开始下载 → SSE 实时进度
- **页面采集**: 粘贴页面 URL → 设定数量 → 开始采集 → SSE 实时采集进度 → 自动开始下载

---

## 7. 数据流与关键交互序列

### 7.1 链接批量下载完整流程

```
用户粘贴链接
    │
    ▼
previewInput()  →  POST /api/batches/preview
    │                └─ preview_links(text)
    │                   ├─ extract_candidate_urls()
    │                   ├─ is_public_http_url()
    │                   └─ normalize_url()
    │
    ▼
显示预览 (有效/重复/无效 计数)
    │
    ▼
startBatch()  →  POST /api/batches { text, output_dir }
    │              ├─ preview_links(text)
    │              ├─ [每条 URL]:
    │              │   ├─ resolve_short_link(url) → HTTP 重定向跟随
    │              │   └─ extractor_supports_url(resolved) → 平台检测
    │              ├─ BatchExpander.expand()
    │              │   ├─ [抖音] 直接提取 video_id
    │              │   └─ [其他] yt-dlp extract_info(flat)
    │              ├─ database.create_expanded_batch()
    │              ├─ database.skip_existing_completed() → 跨批次去重
    │              └─ queue.wake()
    │
    ▼
connectEvents(batchId) → SSE /api/events?batchId=xxx
    │
    ▼
TaskQueue worker 领取任务:
    database.claim_next_task() [CAS: queued → resolving]
    │
    ▼
YtDlpDownloader.download(task, on_progress):
    ├─ yt-dlp extract_info(url, download=True)
    │   ├─ progress_hook → on_progress("downloading", progress, speed, eta)
    │   └─ postprocessor_hook → on_progress("merging", 100%)
    ├─ [可选] FFmpegMediaProcessor.ensure_compatible()
    │   └─ on_progress("transcoding", progress)
    ├─ shutil.move → 最终路径
    └─ database.update_task(COMPLETED, output_path, ...)
    │
    ▼
SSE 推送更新 → 前端实时渲染进度
```

### 7.2 页面采集完整流程

```
用户粘贴页面 URL → startPageBatch()
    │
    ▼
POST /api/batches { text, source_mode: "page", max_items, output_dir }
    ├─ 抖音搜索页: is_douyin_search_url() + douyin_search_mode()
    ├─ database.create_page_batch()
    └─ page_collection_manager.submit(batch_id)
    │
    ▼
PageCollectionManager._run_batch():
    ├─ BrowserPageCollector.collect()
    │   ├─ PlaywrightBrowserSession.open(url)
    │   │   └─ 启动 Edge 持久化 Profile, 拦截网络响应
    │   ├─ [抖音搜索] 等待登录/风控
    │   │   ├─ 检测 login_required()
    │   │   ├─ 检测 risk_controlled()
    │   │   └─ 超时后 reload 重试
    │   └─ 采集循环:
    │       ├─ 提取卡片:
    │       │   ├─ douyin_cards() + douyin_response_cards()
    │       │   └─ extract_douyin_card_videos()
    │       ├─ 逐条 on_video(video)
    │       │   ├─ database.append_page_video()
    │       │   ├─ database.skip_existing_completed()
    │       │   └─ queue.wake()
    │       └─ scroll() + wait(1000ms)
    │
    ▼
TaskQueue 逐条下载采集到的视频 (同 7.1 下载流程)
```

---

## 8. API 接口清单

### 批次接口

| 方法 | 路径 | 请求体 | 响应 |
|------|------|--------|------|
| `POST` | `/api/batches/preview` | `{"text": str}` | `{valid_count, duplicate_count, invalid_count, valid_urls}` |
| `POST` | `/api/batches` | `{"text": str, "output_dir"?: str, "source_mode"?: "links"|"page", "max_items"?: int}` | 批次详情 JSON |
| `GET` | `/api/batches` | Query: `page`, `page_size` | `{items, page, page_size, total, total_pages}` |
| `DELETE` | `/api/batches` | — | `{deleted: true, count: int}` |
| `GET` | `/api/batches/{id}` | — | 批次详情 JSON（含 pause_reason） |
| `DELETE` | `/api/batches/{id}` | — | `{deleted: true, batch_id}` |
| `POST` | `/api/batches/{id}/retry-failed` | — | `{retried: int}` |
| `POST` | `/api/batches/{id}/pause` | — | 批次详情 JSON |
| `POST` | `/api/batches/{id}/resume` | — | 批次详情 JSON |

### 实时事件

| 方法 | 路径 | Query 参数 | 说明 |
|------|------|-----------|------|
| `GET` | `/api/events` | `batchId: int` | SSE 流，推送 `event: batch` 事件 |
| `GET` | `/api/app/session` | — | SSE 流，keep-alive 连接追踪 |

### 设置与工具

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/settings` | 获取 `download_directory` |
| `POST` | `/api/settings/pick-directory` | 打开目录选择对话框 |
| `POST` | `/api/settings/open-directory` | 在文件管理器中打开下载目录 |
| `POST` | `/api/app/shutdown` | 优雅关闭程序（BackgroundTasks） |

### 错误响应格式

所有错误返回统一格式：
```json
{"detail": "错误描述信息"}
```

HTTP 状态码：
- `201` — 创建成功
- `404` — 批次不存在
- `422` — 输入验证失败
- `500` — 服务端错误
- `503` — 功能不可用

---

## 9. 数据库设计

### 数据库引擎
- **SQLite** (WAL 模式)
- 存储路径：`{DATA_DIR}/tasks.db`
- 线程安全：所有 Database 方法使用 `threading.RLock`

### 表结构

#### batches 表

| 列名 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `id` | INTEGER | PK, AUTOINCREMENT | 批次 ID |
| `output_dir` | TEXT | NOT NULL | 下载目录 |
| `paused` | INTEGER | NOT NULL DEFAULT 0 | 暂停标志 |
| `source_mode` | TEXT | NOT NULL DEFAULT 'links' | 'links' 或 'page' |
| `source_url` | TEXT | — | 页面采集源 URL |
| `requested_count` | INTEGER | — | 请求采集数量 |
| `collected_count` | INTEGER | NOT NULL DEFAULT 0 | 已采集数量 |
| `collection_status` | TEXT | — | pending / waiting_login / collecting / ... |
| `collection_stop_reason` | TEXT | — | 结束原因 |
| `created_at` | TEXT | NOT NULL | ISO 8601 UTC |

#### tasks 表

| 列名 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `id` | INTEGER | PK, AUTOINCREMENT | 任务 ID |
| `batch_id` | INTEGER | FK → batches(id) CASCADE | 所属批次 |
| `original_url` | TEXT | NOT NULL | 用户输入的原始 URL |
| `canonical_url` | TEXT | NOT NULL | 标准化/展开后的 URL |
| `platform` | TEXT | NOT NULL DEFAULT 'douyin' | 平台标识 |
| `video_id` | TEXT | — | 平台视频 ID |
| `title` | TEXT | — | 视频标题 |
| `status` | TEXT | NOT NULL DEFAULT 'queued' | 任务状态 |
| `progress` | REAL | NOT NULL DEFAULT 0 | 下载/转码进度 0-100 |
| `downloaded_bytes` | INTEGER | NOT NULL DEFAULT 0 | 已下载字节数 |
| `total_bytes` | INTEGER | — | 总字节数 |
| `speed` | REAL | — | 下载速度 (bytes/s) |
| `eta` | REAL | — | 预估剩余秒数 |
| `output_path` | TEXT | — | 最终输出文件路径 |
| `attempts` | INTEGER | NOT NULL DEFAULT 0 | 重试次数 |
| `error_code` | TEXT | — | ErrorCode 枚举值 |
| `error_message` | TEXT | — | 错误详情 |
| `created_at` | TEXT | NOT NULL | ISO 8601 UTC |
| `updated_at` | TEXT | NOT NULL | ISO 8601 UTC |

#### settings 表

| 列名 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `key` | TEXT | PK | 设置键 |
| `value` | TEXT | NOT NULL | 设置值 |

### 索引

```
idx_tasks_status       ON tasks(status, id)
idx_tasks_batch        ON tasks(batch_id, id)
idx_tasks_video        ON tasks(video_id)
idx_tasks_platform_video ON tasks(platform, video_id)
```

### 迁移策略

`Database.initialize()` 使用 `PRAGMA table_info()` 检测列是否存在，按需执行 `ALTER TABLE ADD COLUMN`。当前迁移历史：
- `batches.paused` — 暂停支持
- `batches.source_mode / source_url / requested_count / collected_count / collection_status / collection_stop_reason` — 页面采集
- `tasks.platform` — 多平台支持

---

## 10. 依赖关系

### Python 依赖

#### 核心依赖 (install_requires)
| 包 | 版本 | 用途 |
|----|------|------|
| `fastapi` | >=0.115 | Web API 框架 |
| `uvicorn[standard]` | >=0.34 | ASGI 服务器 |
| `pydantic` | >=2.10 | 数据验证 |
| `platformdirs` | >=4.3 | 跨平台数据目录 |

#### 可选依赖：download
| 包 | 版本 | 用途 |
|----|------|------|
| `yt-dlp` | >=2026.3.17 | 核心下载引擎 |
| `curl-cffi` | >=0.13 | TLS 指纹模拟（快手） |
| `playwright` | >=1.52 | 浏览器自动化 |
| `imageio-ffmpeg` | >=0.6 | 内置 FFmpeg 二进制 |

#### 可选依赖：desktop
| 包 | 版本 | 用途 |
|----|------|------|
| `PySide6` | >=6.8 | Qt 桌面框架 |

#### 可选依赖：dev
| 包 | 版本 | 用途 |
|----|------|------|
| `httpx` | >=0.28 | HTTP 测试客户端 |
| `pytest` | >=8.3 | 测试框架 |
| `pytest-asyncio` | >=0.25 | 异步测试支持 |

#### 可选依赖：build
| 包 | 版本 | 用途 |
|----|------|------|
| `pyinstaller` | >=6.12 | 打包为可执行文件 |

### 前端依赖

| 包 | 用途 |
|----|------|
| `vue` 3.5.18 | UI 框架 |
| `vite` 7.3.6 | 构建工具 |
| `@vitejs/plugin-vue` | Vue SFC 编译 |
| `vitest` + `@vue/test-utils` | 单元测试 |
| `jsdom` | DOM 模拟环境 |

### 外部系统依赖

| 工具 | 用途 | 备注 |
|------|------|------|
| FFmpeg | 媒体分析与转码 | 通过 imageio-ffmpeg 捆绑 |
| Microsoft Edge | 浏览器自动化 | Playwright 使用 `channel="msedge"` |
| Inno Setup 6 | 安装器制作 | 构建时使用 |

### 模块间依赖关系图

```
launcher.py ───────────────┬── api.py ────────── store.py
                           │    │
                           │    ├── links.py
                           │    ├── resolver.py
                           │    ├── page_sessions.py
                           │    └── platforms.py
                           │
                           ├── queue.py ──────── store.py
                           │    └── downloader.py
                           │         ├── media.py
                           │         ├── filenames.py
                           │         ├── kuaishou.py
                           │         ├── vipshop.py
                           │         ├── direct_media.py
                           │         └── cookies.py
                           │
                           ├── page_collector.py
                           │    ├── store.py
                           │    ├── platforms.py
                           │    ├── links.py
                           │    └── cookies.py
                           │
                           └── cookies.py
                                └── store.py

desktop.py ────────────────┬── local_backend.py ── (同 launcher.py 的装配)
                           ├── desktop_bridge.py
                           └── desktop_instance.py
```

---

## 11. 构建与发布

### 11.1 开发模式运行

```powershell
# 前端开发
cd frontend
npm ci
npm run dev

# 后端开发
pip install -e ".[download]"
python run_app.py
```

### 11.2 便携版构建 (`scripts/build.ps1`)

```
1. npm ci + npm run build  (构建前端)
2. pip install -e ".[download,build]"
3. PyInstaller douyin_downloader.spec
   → dist/视频批量下载工具.exe (单文件 + _internal 目录)
```

使用 `douyin_downloader.spec`，入口为 `run_desktop.py`，生成包含前端静态文件的单文件 EXE。

### 11.3 桌面版构建 (`scripts/build-desktop-app.ps1`)

```
1. [可选] npm ci + npm test + npm run build + pytest
2. pip install -e ".[download,desktop,build]"
3. PyInstaller douyin_downloader_desktop.spec
   → release/desktop/app/视频批量下载工具/视频批量下载工具.exe (COLLECT 模式)
```

使用 `douyin_downloader_desktop.spec`，COLLECT 模式将二进制依赖分离到 `_internal` 目录。
构建后验证：检查 QtWebEngineProcess.exe、FFmpeg exe 是否被正确打包。

### 11.4 安装器构建 (`scripts/build-installer.ps1`)

```
1. build-desktop-app.ps1 (构建桌面版)
2. Inno Setup 6 编译 installer/douyin-downloader.iss
   → release/installer/视频批量下载工具-Setup-1.1.0-Windows-x64.exe
```

安装器特性：
- x64 only，最低 Windows 10 (17763)
- 安装到 `%LOCALAPPDATA%\Programs\DouyinBatchDownloader`
- 创建开始菜单和桌面快捷方式
- LZMA2 最大压缩

### 11.5 产物结构

```
便携版 (ZIP):
  视频批量下载工具.exe
  _internal/
  SHA256.txt
  使用说明.txt

安装版 (Setup EXE):
  安装到 %LOCALAPPDATA%\Programs\DouyinBatchDownloader\

数据目录:
  %LOCALAPPDATA%\DouyinBatchDownloader\
    ├── tasks.db
    ├── application.log
    ├── runtime.json         (运行时端口/PID)
    ├── browser/             (浏览器持久化数据)
    │   ├── anonymous-cookies.txt
    │   ├── anonymous-edge-profile/
    │   └── page-edge-profile/
    └── ...
```

---

## 12. 运行方式

### 开发环境

```powershell
# 1. 安装后端依赖
pip install -e ".[download,dev]"

# 2. 安装 Playwright 浏览器
python -m playwright install chromium

# 3. 构建前端
cd frontend
npm ci
npm run build

# 4. 启动（浏览器模式）
cd ..
python run_app.py
```

### 便携版使用

```powershell
# 解压 ZIP → 双击 视频批量下载工具.exe
# 或命令行启动:
.\视频批量下载工具.exe
```

### 桌面版使用

```powershell
# 安装 Setup EXE → 从开始菜单或桌面快捷方式启动
# 或命令行:
python run_desktop.py
```

### 环境变量

| 变量 | 说明 |
|------|------|
| `DOUYIN_DOWNLOADER_DATA_DIR` | 覆盖数据目录（默认 `%LOCALAPPDATA%\DouyinBatchDownloader`） |
| `DOUYIN_DOWNLOADER_NO_BROWSER` | 设为 `1` 时不自动打开浏览器 |

---

## 13. 测试体系

### 后端测试 (pytest)

```
tests/
├── conftest.py               # pytest fixtures (qt_app, wait_for_qt)
├── test_api.py               # API 端点功能测试
├── test_store.py             # 数据库 CRUD / 迁移测试
├── test_queue.py             # 任务队列逻辑测试
├── test_downloader.py        # 下载器测试
├── test_media.py             # FFmpeg 媒体处理测试
├── test_platforms.py         # 平台扩展器测试
├── test_kuaishou.py          # 快手解析器测试
├── test_vipshop.py           # 唯品会解析器测试
├── test_direct_media.py      # 直链提取器测试
├── test_links.py             # URL 解析 / 验证测试
├── test_resolver.py          # 短链/直链探测测试
├── test_filenames.py         # 文件名处理测试
├── test_cookies.py           # Cookie 管理测试
├── test_page_collector.py    # 页面采集逻辑测试
├── test_page_sessions.py     # 页面会话追踪测试
├── test_launcher.py          # 启动器组件测试
├── test_local_backend.py     # 本地后端测试
├── test_desktop.py           # 桌面窗口测试
├── test_desktop_bridge.py    # 目录选择桥测试
└── test_desktop_instance.py  # 单实例测试
```

**运行**:
```powershell
pytest -q
```

**pytest 配置** (来自 `pyproject.toml`):
- `pythonpath = ["backend"]` — 自动加入 Python 路径
- `asyncio_mode = "auto"` — 自动检测异步测试
- Qt 相关测试使用 `offscreen` 平台和 `--no-sandbox` 标志

### 前端测试 (vitest)

```
frontend/tests/
├── app.test.js
├── format.test.js
├── history.test.js
└── new-batch.test.js
```

**运行**:
```powershell
npm --prefix frontend test -- --run
```

### 冒烟测试

```powershell
# 运行安装后冒烟测试
.\scripts\smoke-test-installed-app.ps1
```

---

> **文档维护说明**: 本文档基于 `2026-08-12` 代码状态生成，记录了 `v0.1.0` (backend) / `v1.1.0` (desktop) 版本的完整架构。后续重大功能变更时请同步更新。
