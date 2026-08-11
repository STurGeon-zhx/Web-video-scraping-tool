# 打包运行稳定性修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 修复接收方电脑上页面会话短暂断开导致服务退出和旧进程卡死的问题，并产出经过独立 EXE 端到端验收的 Windows 文件包。

**Architecture:** 保留“关闭页面后结束程序”的现有行为，但把页面会话断开宽限提高到足以覆盖浏览器 SSE 自动重连；同时限制 Uvicorn 优雅关闭等待时间，避免单实例锁长期残留。验收脚本使用全新数据目录启动成品 EXE，通过真实 HTTP 接口创建批次并验证进程生命周期。

**Tech Stack:** Python 3.11、FastAPI、Uvicorn、Vue 3、PyInstaller、PowerShell。

## Global Constraints

- 仅监听 `127.0.0.1`。
- 不读取用户已有浏览器 Cookie。
- 不影响现有下载状态数据库，验收使用隔离数据目录。
- 保留页面关闭后自动退出和单实例行为。

---

### Task 1: 修复页面重连与关闭竞态

**Files:**
- Modify: `backend/douyin_downloader/api.py`
- Test: `tests/test_api.py`

- [ ] 写失败测试，验证应用页面会话关闭宽限不少于 30 秒。
- [ ] 运行定向测试并确认因当前 3 秒默认值失败。
- [ ] 在 `create_app` 中显式使用 30 秒宽限。
- [ ] 运行定向测试并确认通过。

### Task 2: 限制服务关闭等待时间

**Files:**
- Modify: `backend/douyin_downloader/launcher.py`
- Test: `tests/test_launcher.py`

- [ ] 写失败测试，验证 Uvicorn 只监听本机且优雅关闭最多等待 5 秒。
- [ ] 运行定向测试并确认当前配置失败。
- [ ] 设置 `timeout_graceful_shutdown=5`。
- [ ] 运行定向测试并确认通过。

### Task 3: 全量验证与成品验收

**Files:**
- Modify: `release/app/使用说明.txt`
- Create: `release/抖音批量下载工具-v0.1.1-Windows-x64.zip`

- [ ] 运行后端与前端全量测试。
- [ ] 构建前端和 PyInstaller 单文件 EXE。
- [ ] 在隔离数据目录启动 EXE，验证首页、设置、批次创建、暂停、继续、关闭接口。
- [ ] 验证进程在 5 秒关闭期限后释放单实例锁。
- [ ] 压缩 EXE 和使用说明，检查 ZIP 内容并生成 SHA256。
