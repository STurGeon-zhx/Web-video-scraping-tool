# 抖音批量下载工具

一个只在 Windows 本机运行的批量下载工具。粘贴抖音公开视频链接后，程序会进行安全校验、去重、排队、下载并展示进度。

> 仅可下载你拥有版权或已经获得授权的公开内容。请遵守内容版权、抖音平台规则及所在地法律。工具不会尝试访问私密、登录后可见或无权访问的视频。

## 功能

- 每行一条链接，也支持直接粘贴含链接的分享文案
- 支持 `www.douyin.com/video/...` 和 `v.douyin.com` 分享短链
- 两条任务并发、网络重试、限流冷却和 `.part` 续传
- SQLite 保存批次、任务和设置，重启后恢复未完成任务
- 自动选择公开可获得的最高画质，必要时使用内置 FFmpeg 合并音视频
- 标题文件名自动清理 Windows 非法字符，同名文件绝不覆盖
- 不读取现有浏览器 Cookie；必要时仅创建无账号的临时 Edge 会话

## 安装版使用

安装版支持 64 位 Windows 10/11。运行安装程序后，可从桌面或开始菜单打开“抖音批量下载工具”。程序使用独立桌面窗口，不会打开外部浏览器，也不要求目标电脑安装 Python、Node.js 或 FFmpeg。

页面、设置和历史批次均保存在当前电脑的 `%LOCALAPPDATA%\DouyinBatchDownloader`。查看界面和历史记录可离线完成；解析与下载公开抖音视频时需要联网。每台电脑只读取自己的本地数据。

关闭桌面窗口会安全停止本地后台。卸载程序不会删除任务历史、设置或已下载的视频。安装包未购买商业代码签名证书，Windows SmartScreen 可能显示“未知发布者”；分发时请同时提供发布页公布的 SHA256。

发生启动错误时，请查看 `%LOCALAPPDATA%\DouyinBatchDownloader\application.log`。

## 开发运行

要求：Python 3.11/3.12、Node.js 20 或更高版本、系统已安装 Microsoft Edge。

```powershell
python -m pip install -e ".[dev,download,desktop,build]"
npm --prefix frontend install
npm --prefix frontend run build
python run_desktop.py
```

开发测试：

```powershell
python -m pytest -q
npm --prefix frontend test
```

## Windows 安装包构建

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build-installer.ps1
```

最终产物位于 `release\installer\抖音批量下载工具-Setup-1.0.0-Windows-x64.exe`。安装程序为当前用户安装，无需管理员权限；前端、Python 后端、QtWebEngine、yt-dlp、Playwright 驱动和 FFmpeg 都包含在安装包内。匿名 Cookie 回退使用 Windows 自带的 Edge，不读取用户现有浏览器 Cookie。

## 使用

1. 双击桌面或开始菜单中的“抖音批量下载工具”，等待独立窗口打开。
2. 粘贴抖音链接，每行一条。
3. 选择下载目录并点击“开始批量下载”。
4. 页面可以安全刷新；关闭桌面窗口后，程序会自动退出。
5. 未完成任务会保存在本机，下次启动时自动恢复为等待状态。
6. 点击页面右上角“历史批次”打开历史抽屉；需要导入另一组链接时，点击抽屉中的“新建批次”。

应用数据默认保存在 Windows 当前用户的本地应用数据目录，包括 `tasks.db`、匿名浏览器临时资料和 `application.log`。视频保存在页面选择的目录。

## 常见问题

- **视频需要登录或无权访问**：首版不会登录账号，也不会绕过访问控制。
- **访问过于频繁**：队列会自动冷却并重试；持续失败时请稍后再试。
- **磁盘空间不足**：剩余空间低于 1 GB 时队列暂停，清理磁盘或更换目录后重试。
- **匿名 Cookie 获取失败**：确认 Microsoft Edge 可正常启动且没有被安全软件阻止。
- **抖音改版后无法解析**：解析内核不会静默在线更新，需要安装项目发布的新版本。

## 隐私与安全

- 服务只监听 `127.0.0.1`，不向局域网或互联网开放管理接口。
- 短链的每一跳重定向都必须属于抖音域名白名单。
- Cookie、媒体临时签名和视频文件不会上传到第三方服务。
- 日志只记录程序状态和错误，不主动记录 Cookie 内容。
