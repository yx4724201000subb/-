#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开机自启（写当前用户的注册表 Run 项，零第三方依赖）

用 ``HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Run``：

* 只影响当前用户，不需要管理员权限；
* 在任务管理器的「启动」列表里能看到，用户可以自己关掉；
* 比往「启动」文件夹丢快捷方式更好控制（命令行参数明确，路径含空格也不会出问题）。

写入的命令分两种情况：

* 源码运行：``"<venv>\\pythonw.exe" "<项目>\\src\\__main__.py" --connect --retry``
  用 pythonw 才不会每次开机弹一个黑色控制台窗口。
* 打包成 exe：``"<exe 路径>" --connect --retry``
"""

from __future__ import annotations

import os
import sys
from os.path import abspath, dirname, exists, join

RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
VALUE_NAME = 'RuijieEPortaTool'

# 自启时使用的参数：静默登录 + 重试，不做任何弹窗询问
AUTOSTART_ARGS = ['--connect', '--retry']


def is_windows() -> bool:
    return os.name == 'nt'


def _project_root() -> str:
    return dirname(dirname(abspath(__file__)))


def _pythonw() -> str:
    """优先用当前解释器同目录的 pythonw.exe（venv 里就有）"""
    exe = abspath(sys.executable)
    folder = dirname(exe)
    for name in ('pythonw.exe', 'pythonw'):
        candidate = join(folder, name)
        if exists(candidate):
            return candidate
    return exe


def _quote(path: str) -> str:
    return f'"{path}"'


def entry_script() -> str:
    return join(dirname(abspath(__file__)), '__main__.py')


def build_command(args: list[str] | None = None) -> str:
    """生成写进注册表的命令行"""
    args = AUTOSTART_ARGS if args is None else args
    tail = ' '.join(args)
    if getattr(sys, 'frozen', False):
        # 打包后 sys.executable 就是 exe 自己
        return f'{_quote(abspath(sys.executable))} {tail}'.strip()
    return (
        f'{_quote(_pythonw())} {_quote(entry_script())} {tail}'
    ).strip()


def _open_key(write: bool = False):
    import winreg

    access = winreg.KEY_SET_VALUE if write else winreg.KEY_READ
    return winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, access)


def get_command() -> str | None:
    """读取当前登记的自启命令，没登记返回 None"""
    if not is_windows():
        return None
    import winreg

    try:
        with _open_key() as key:
            value, _kind = winreg.QueryValueEx(key, VALUE_NAME)
            return str(value)
    except FileNotFoundError:
        return None
    except OSError:
        return None


def is_enabled() -> bool:
    return get_command() is not None


def enable(args: list[str] | None = None) -> tuple[bool, str]:
    """开启开机自启

    :return: (是否成功, 写进去的命令或错误信息)
    """
    if not is_windows():
        return False, '仅支持 Windows'
    import winreg

    command = build_command(args)
    try:
        with _open_key(write=True) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command)
    except OSError as e:
        return False, f'写入注册表失败：{e}'
    return True, command


def disable() -> tuple[bool, str]:
    """关闭开机自启（本来就没开也算成功）"""
    if not is_windows():
        return False, '仅支持 Windows'
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:
        return True, '本来就没有设置开机自启'
    except OSError as e:
        return False, f'删除注册表项失败：{e}'
    return True, '已关闭开机自启'
