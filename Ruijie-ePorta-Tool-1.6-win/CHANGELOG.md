# 更新日志

本文件记录这个 Windows 增强分支相对上游的改动。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号沿用上游的 `1.6.x`。

## [1.6.2] - 2026-10-04

第一个"能直接跑起来"的 Windows 增强版。

### 新增

- **下载就能用**：没有 `config.yml` 时直接双击程序（exe 或 `设置.bat`），
  会**自动生成配置模板 + 弹出设置窗口**，并在日志区写清"填学号和密码就行"。
  只有在你明确指定了动作（`--connect` / `--check` 等）时才保持原来的报错退出，不会干扰脚本。
- **自动探测认证服务器**（`--detect-server`，图形窗口里是「自动探测」按钮）：
  请求几个公共连通性检测地址，从网关 302 劫持的 `Location` 里读出 portal 地址，
  不用抓包也知道 `url.server` 该填什么。已联网（网关不再劫持）时会给出手动查找的办法。
- **图形设置窗口**（`src/gui.py`，tkinter 零依赖）：账号、接口、运行方式分三段，
  带日志区和状态栏；「测试连接 / 立即联网 / 立即断网 / 获取会话 / 验证码自检 /
  打开认证网页 / 重新载入」全部在后台线程执行，界面不卡。
- **开机自启**（`src/autostart.py`）：写 `HKCU\...\CurrentVersion\Run`，不需要管理员；
  `--autostart on|off|status` 可在命令行开关。
- **无人值守重试**（`--retry`）：默认 5 分钟内每隔 10 秒重试一次，连上就收工，
  到点不管成没成都退出；全程不弹模态框（`pythonw` 下没人点确定）。
- **验证码自动识别**（`src/captcha.py`）：纯标准库的模板匹配，
  开机自启遇到验证码也能自己过；识别不出来才弹窗问人。附带 `--test-captcha` 自检。
- **自动抓设备参数**（`src/session.py`）：支持真机上那种 **HTTP 200 + JS 跳转**
  （不是 302）的登录页，也支持从 SSO 的 `redirect_uri` 里挖参数；
  参数失效时会清空并在同一次调用里重抓重试。
- **系统通知**（`src/notifier.py`）：直接调 Windows PowerShell 5.1 发 WinRT toast，
  失败退回 tkinter 弹窗；不再依赖 `tinyWinToast`。
- **日志文件 `eporta.log`**：任何模式下都写，带时间戳；`--debug` 额外记录请求体与原始响应
  （密码、验证码自动打码）。
- 新增命令行开关：`--gui`、`--autostart`、`--retry`、`--timeout`、`--interval`、
  `--fetch-cookie`、`--open-login`、`--test-captcha`、`--detect-server`、`--debug`、`--gen-config`。
- 新增 `run.bat` / `设置.bat`，以及 `--gen-config` 生成带注释的配置模板；
  GUI 保存时会**保留配置里的注释**。

### 修复

- **断网用错了参数**：上游把 `login_data` 当成断网请求体发给 `method=logout`
  （`logout_data` 从未被使用），现在改用 `logout_data`。
- **登录请求体与浏览器不一致**：上游会把值为空的字段整条丢掉，
  而浏览器（`AuthInterFace.js`）是 `service=` / `operatorPwd=` / `operatorUserId=` /
  `validcode=` 都照样发。现在八个字段一个不少，顺序也一致。
- **Windows 11 上永远弹不出系统通知**：上游用 `win32_ver()[0] == '10'` 判断系统版本，
  Win11 上是 `'11'`，于是通知功能静默失效。现在改为直接尝试 toast。
- **多个裸 `except:`**，以及缺配置项时直接 `KeyError`：现在全部换成明确的报错文案，
  缺少哪个键、该去哪个文件补，都会说清楚。
- 配置定位不再只看 `sys.argv[0]`：`--config` → 环境变量 `EPORTA_CONFIG` →
  程序目录 → 项目根目录 → 当前目录，exe 放进 `dist\` 也能找到项目根里的配置。
- 通知卡片里的中文/引号不会再因 XML 转义出问题（改用 Base64 传参）。
- **`--console` 下不再弹模态对话框**：以前 `--console` 只是"额外打印一遍"，
  出错时照样弹窗 —— 写在批处理里就会**卡在一个没人点的窗口上**
  （`run.bat --check --console` 遇到配置没填就中招）。
  现在 `--console` 只打印、不弹窗；双击运行（`pythonw` / 打包 exe）时照样弹窗，
  不会让错误石沉大海。
- `pythonw` / windowed exe 下 `sys.stdout` 是 `None`，以前偶然会因 `print` 崩掉，现在会先判断。
- **`Windows-Ruijie.spec` 不再依赖"当前目录"**：原来写的是 `pathex=['src']` 这类相对路径，
  只有在项目根目录执行 `pyinstaller Windows-Ruijie.spec` 才是对的；
  换个目录执行就会打出一个**缺了 `config.py` / `captcha.py` / `session.py` 的 exe**，
  双击只弹 `Unhandled exception in script`。现在一律按 spec 文件自身的位置解析路径。

### 变更

- **Python 版本要求从 `>=3.9,<3.11` 放宽到 `>=3.9`**（上游的 pin 会让 3.11+ 装不上）。
- **依赖从 3 个减到 1 个**：只留 `PyYAML`。
  移除 `tinyWinToast`（它的 `Toast.show()` 首次调用会**从 GitHub 下载 `PoshWinRT.dll` 并执行**、
  往 site-packages 写 `toast.ps1`、还常驻一个 PowerShell，见 README 的说明）
  和 `pywin32`（只为几行代码，现在用 `ctypes` 调 `CreateMutexW` 实现单实例）。
- 单实例用命名互斥体实现，替代 `pywin32` 方案。

### 移除

- `poetry.lock`（改用 `requirements.txt`）、不再依赖 Poetry 构建。

### 验证状态

已实测：依赖安装、配置生成与校验、模拟服务器全流程（登录/断网/自愈/验证码）、
通知、开机自启注册表读写、exe 打包与启动、验证码识别（真机 6/6 被服务器接受）。

未实测：**「未认证 → 自动登录成功」这条完整链路**。开发期间能复现的真实状态是
"机器已经在线"，而在线时服务器会对登录请求短路，参数并没有被真正校验。
第一次使用建议手动跑 `run.bat --connect --console`，看 `eporta.log` 确认。

## [1.6] - 上游版本

原始的 Poetry 工程版本，依赖 `tinyWinToast` + `pywin32` + `PyYAML`，只有命令行，
配置需要自己抓包填写。见 [Redlnn/Ruijie-ePorta-Tool](https://github.com/Redlnn/Ruijie-ePorta-Tool)。
