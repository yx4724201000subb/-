#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Windows 通知模块（零第三方依赖）

原版依赖 tinyWinToast + pywin32，那套东西在本机（Windows 11 + Python 3.14）
上有两个坑：

1. ``tinyWinToast.Toast.show()`` 首次调用时走 ``ToastInit()``，而 ``ToastInit()``
   会**从 GitHub 下载 PoshWinRT.dll 并执行**，还会往 site-packages 目录写
   toast.ps1。既要联网，又属于执行来路不明的代码。
2. 原版 ``__main__.py`` 用 ``win32_ver()[0] == '10'`` 判断是否走 toast 分支，
   但 Windows 11 上 ``platform.win32_ver()`` 返回 ``('11', '10.0.26200', ...)``，
   判断为 False，通知全部退化成必须手动点确定的 tkinter 弹窗。

本模块改用纯标准库实现，并且**只调用 Windows PowerShell 5.1**
（``System32\\WindowsPowerShell\\v1.0\\powershell.exe``）：
PowerShell 7（pwsh）已经不支持 ``ContentType=WindowsRuntime`` 的 WinRT
类型投影，用它调 toast 必然失败（实测报"找不到类型
[Windows.UI.Notifications.ToastNotificationManager]"）。所以这里不去 PATH 里
瞎找 pwsh，而是直接用 5.1 的固定路径。

XML 通过 UTF-8 + Base64 传给 PowerShell 再解码，彻底避开中文/引号转义问题。

失败时回退到 tkinter 弹窗，保证任何情况下用户都能看到结果。
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
from xml.sax.saxutils import escape, quoteattr

# 用 Windows 自己注册过的 AppUserModelID，免去注册表注册的麻烦；
# 否则 Windows 11 会静默丢弃 toast（不报错也不显示）。
_POWERSHELL_AUMID = (
    '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe'
)

_PS_TIMEOUT = 25
_PS_SCRIPT_TEMPLATE = (
    "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
    "ContentType=WindowsRuntime] | Out-Null;"
    "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, "
    "ContentType=WindowsRuntime] | Out-Null;"
    "[Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, "
    "ContentType=WindowsRuntime] | Out-Null;"
    "$ErrorActionPreference='Stop';"
    "$xml = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__PAYLOAD__'));"
    "$doc = New-Object Windows.Data.Xml.Dom.XmlDocument;"
    "$doc.LoadXml($xml);"
    "$toast = [Windows.UI.Notifications.ToastNotification]::new($doc);"
    "[Windows.UI.Notifications.ToastNotificationManager]::"
    "CreateToastNotifier('__AUMID__').Show($toast);"
    "Write-Output 'TOAST-OK'"
)
# 关掉子进程黑窗（pythonw 下也安全）
_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def is_windows() -> bool:
    return os.name == 'nt'


def _powershell_path() -> str | None:
    """返回可用于 WinRT 的 Windows PowerShell 5.1 路径（pwsh 不可用）"""
    if not is_windows():
        return None
    system_root = os.environ.get('SystemRoot') or os.environ.get('WINDIR') or r'C:\Windows'
    candidate = os.path.join(
        system_root, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe'
    )
    if os.path.exists(candidate):
        return candidate
    # 兜底：PATH 里如果明确是 powershell.exe 也可以用
    import shutil

    found = shutil.which('powershell.exe') or shutil.which('powershell')
    return found


def _run_powershell(script: str) -> tuple[int, str, str]:
    exe = _powershell_path()
    if not exe:
        return 1, '', '找不到 Windows PowerShell'
    try:
        proc = subprocess.run(
            [exe, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
             '-Command', script],
            capture_output=True,
            timeout=_PS_TIMEOUT,
            creationflags=_NO_WINDOW,
            encoding='utf-8',
            errors='replace',
        )
    except Exception as e:  # 超时、权限等一律当成失败
        return 1, '', f'{type(e).__name__}: {e}'
    return proc.returncode, proc.stdout or '', proc.stderr or ''


def _build_toast_xml(title: str, msg: str, icon: str | None = None) -> str:
    """生成 ToastGeneric XML（内容全部转义）"""
    image = ''
    if icon and os.path.exists(icon):
        src = 'file:///' + os.path.abspath(icon).replace('\\', '/')
        image = f'<image placement="appLogoOverride" hint-crop="circle" src={quoteattr(src)}/>'
    return (
        '<toast duration="short">'
        '<visual><binding template="ToastGeneric">'
        f'<text>{escape(title)}</text>'
        f'<text>{escape(msg)}</text>'
        f'{image}'
        '</binding></visual>'
        '<audio silent="true"/>'
        '</toast>'
    )


def _show_toast(title: str, msg: str, icon: str | None = None) -> bool:
    """通过 WinRT 弹出系统通知，成功返回 True"""
    if not is_windows():
        return False
    payload = base64.b64encode(
        _build_toast_xml(title, msg, icon).encode('utf-8')
    ).decode('ascii')
    script = (
        _PS_SCRIPT_TEMPLATE
        .replace('__PAYLOAD__', payload)
        .replace('__AUMID__', _POWERSHELL_AUMID)
    )
    rc, out, _err = _run_powershell(script)
    return rc == 0 and 'TOAST-OK' in out


def _show_dialog(title: str, msg: str) -> None:
    """兜底：tkinter 弹窗"""
    try:
        from tkinter.messagebox import showinfo

        showinfo(title=title, message=msg)
    except Exception:
        # 连图形界面都没有（例如被计划任务以无桌面方式拉起）就写到 stderr
        print(f'[{title}] {msg}', file=sys.stderr)


def notify(title: str = '锐捷 ePorta 连接工具', msg: str = '', icon: str | None = None) -> bool:
    """发送通知，返回是否走的是系统 toast

    :param title: 通知标题
    :param msg: 通知内容
    :param icon: 可选图标路径（.ico/.png）
    """
    if _show_toast(title, msg, icon):
        return True
    _show_dialog(title, msg)
    return False


def toast_available(verbose: bool = False) -> bool:
    """探测当前环境能否弹出系统 toast（给 --check / 排错用）"""
    if not _powershell_path():
        if verbose:
            print('  未找到 Windows PowerShell 5.1')
        return False
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
        "ContentType=WindowsRuntime] | Out-Null;"
        "Write-Output 'TOAST-READY'"
    )
    rc, out, err = _run_powershell(script)
    if verbose and err:
        print(f'  PowerShell 报错：{err.strip()[:400]}')
    return rc == 0 and 'TOAST-READY' in out
