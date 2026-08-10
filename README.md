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

## 开发运行

要求：Python 3.12、Node.js 20 或更高版本、系统已安装 Microsoft Edge。

```powershell
python -m pip install -e ".[dev,download,build]"
npm --prefix frontend install
npm --prefix frontend run build
python run_app.py
```

开发测试：

```powershell
python -m pytest -q
npm --prefix frontend test
```

## Windows 打包

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build.ps1
```

产物位于 `dist\抖音批量下载工具.exe`。前端、Python 后端、yt-dlp、Playwright 驱动和 FFmpeg 都会随 EXE 打包；匿名 Cookie 回退使用 Windows 自带的 Edge，不下载额外浏览器。

## 使用

1. 双击 EXE，等待默认浏览器自动打开。
2. 粘贴抖音链接，每行一条。
3. 选择下载目录并点击“开始批量下载”。
4. 浏览器页面可以安全刷新；关闭最后一个工具页面约 3 秒后，程序会自动退出。
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
