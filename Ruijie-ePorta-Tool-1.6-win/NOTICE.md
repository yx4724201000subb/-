# 声明与第三方说明

## 上游

本仓库是 [Redlnn/Ruijie-ePorta-Tool](https://github.com/Redlnn/Ruijie-ePorta-Tool)（`1.6`）的
非官方修改版，针对 Windows 11 + 新版 Python 做了大幅重写与修补，**不是上游发布**。

```
Copyright (C) 2022 Red_lnn            （原项目）
Copyright (C) 2026 本仓库贡献者        （Windows 增强分支的改动）
```

按上游的授权，本仓库同样以 **AGPL-3.0** 发布，完整条款见 [LICENSE](LICENSE)。
转载、二次修改请一并保留 `LICENSE`、`version.txt` 与本节署名。

## 运行时依赖

| 依赖 | 用途 | 协议 |
| --- | --- | --- |
| [PyYAML](https://pyyaml.org/) | 读写 `config.yml` | MIT |

除此之外只使用 Python 标准库。打包出来的 exe 里包含 Python 运行时与 PyYAML。

## 与原版相比特意去掉的东西

- **`tinyWinToast`**：它的 `Toast.show()` 首次调用会去 GitHub 下载 `PoshWinRT.dll` 并调用
  `LoadLibrary` 执行，同时往 `site-packages` 写 `toast.ps1`、常驻一个 PowerShell 进程。
  本仓库改用系统自带的 Windows PowerShell 5.1 发通知（见 `src/notifier.py`），
  不下载、不落地任何可执行文件。
- **`pywin32`**：原本只为系统版本判断和单实例互斥体，现在分别用标准库和 `ctypes` 实现。

## 与本仓库无关的内容

- **本仓库不包含任何校园网账号、密码、Cookie 或认证服务器地址**，
  仓库里的 `config.example.yml`、截图、示例全部是占位数据（`http://127.0.0.1`、`00000000000`）。
  使用前请填写**你自己**的账号。
- `src/captcha.py` 里的验证码模板是从**开发者的校园网认证页验证码样本**中标定出来的
  （80×30、4 位数字的常见锐捷验证码）。如果你的学校字体不同，
  请用 `tools/captcha_calibrate.py` 重新标定，不要直接套用。
- 本工具只是把浏览器上手动点的那次登录自动化了，请遵守你所在学校的网络使用规定。
