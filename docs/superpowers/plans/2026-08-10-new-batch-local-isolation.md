# 新建批次与本地隔离 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 删除页面中的“退出程序”按钮，在批次历史标题栏增加“新建批次”按钮，并确保新建操作只重置当前页面、不停止本机后台下载。

**Architecture:** 保持现有 FastAPI 仅监听 `127.0.0.1`、SQLite 数据库存放于当前 Windows 用户数据目录的架构，因此各台电脑只读取自己的本地批次。前端新增一个可单元测试的状态重置函数，由 Vue 页面负责关闭当前批次 SSE、滚动到顶部并聚焦链接输入框。

**Tech Stack:** Vue 3、Vite、Vitest、FastAPI、SQLite、PyInstaller

## 全局约束

- 不修改后端接口、数据库表结构或下载队列。
- “新建批次”不删除历史记录，不停止等待或下载中的任务。
- 只移除前端“退出程序”入口，保留后端安全关闭接口供打包程序和测试使用。
- 仓库尚无初始提交，本次不创建包含未知既有文件的提交。

---

### Task 1: 用测试固定新建批次的状态重置行为

**Files:**
- Create: `frontend/tests/new-batch.test.js`
- Create: `frontend/src/new-batch.js`

**Step 1: Write the failing test**

测试输入文本、预览结果、当前批次、通知与错误均被清空，同时关闭当前 SSE；历史批次数据不在重置参数中，避免被误清空。

**Step 2: Run test to verify it fails**

Run: `npm test -- --run frontend/tests/new-batch.test.js`

Expected: FAIL，因为 `frontend/src/new-batch.js` 尚不存在。

**Step 3: Write minimal implementation**

实现 `resetNewBatchState`，只重置当前导入表单和当前批次视图，并调用传入的 SSE 关闭回调。

**Step 4: Run test to verify it passes**

Run: `npm test -- --run frontend/tests/new-batch.test.js`

Expected: PASS。

### Task 2: 修改页面按钮与交互

**Files:**
- Modify: `frontend/src/App.vue`
- Modify: `frontend/src/styles.css`

**Step 1: Wire the tested state reset into Vue**

- 删除顶栏“退出程序”按钮及不再使用的前端 `shutdown` 方法。
- 给链接文本框增加模板引用。
- 新建批次时调用 `resetNewBatchState`，待 DOM 更新后滚动到页面顶部并聚焦文本框。

**Step 2: Add the history-header button**

在“批次历史”标题栏右侧、批次数量之前增加“新建批次”按钮，并补充桌面端和窄屏样式。

**Step 3: Build and run frontend tests**

Run: `npm test -- --run`

Expected: 全部通过。

Run: `npm run build`

Expected: Vue 生产构建成功。

### Task 3: 验证本地隔离与后端回归

**Files:**
- Verify: `backend/douyin_downloader/launcher.py`
- Verify: `backend/tests/`

**Step 1: Verify isolation invariants**

确认服务仍只监听 `127.0.0.1`，数据库目录仍来自当前 Windows 用户的本地应用数据目录。

**Step 2: Run backend regression tests**

Run: `python -m pytest`

Expected: 全部通过。

### Task 4: 更新说明并重新打包验证

**Files:**
- Modify if needed: `README.md`
- Rebuild: `dist-updated/抖音批量下载.exe`

**Step 1: Remove stale usage wording**

若文档仍要求点击“退出程序”，改为直接关闭浏览器页面，并说明后台程序由打包启动器管理。

**Step 2: Rebuild the Windows executable**

使用现有 PyInstaller 配置重新构建前后端资源和可执行文件。

**Step 3: Smoke-test the packaged application**

使用独立临时数据目录启动 EXE，确认：

- 首页和静态资源可访问；
- 初始批次历史为空，未读取其他测试目录的数据；
- 页面包含“新建批次”且不再显示“退出程序”；
- 安全关闭接口仍能终止测试实例。

**Step 4: Final verification**

再次运行前端测试、前端构建和后端测试，并检查工作区差异只涉及本次需求。
