#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""锐捷 ePortal Web 认证自动登录 / 断线工具

在原版 1.6（作者 Red_lnn，AGPL-3.0）基础上做本机适配，主要改动：

* 去掉了 tinyWinToast / pywin32 两个第三方依赖，只保留 pyyaml，
  通知改用系统自带 WinRT toast（详见 notifier.py 里的说明）；
* 修掉 Windows 11 上 "win32_ver()[0] == '10'" 导致通知退化成弹窗的问题；
* 修掉断网请求错误地使用 login_data 而不使用 logout_data 的问题；
* 配置文件路径、YAML 报错、非 JSON 响应等边界情况都做了处理；
* 新增 --check / --console / --config / --gen-config / --connect / --disconnect
  命令行参数，方便排查问题和挂计划任务；
* 新增 --gui 图形设置窗口（双击 设置.bat 即可），以及 --autostart on|off|status
  注册表开机自启；
* 新增 --retry 无人值守重试：连上就退出，超时（默认 300 秒）也退出，
  配合开机自启使用（系统刚启动时 WLAN 还没连上，一次失败就放弃没意义）。
* 新增图形验证码自动识别（captcha.py，纯标准库模板匹配）；
* 新增 --detect-server 自动探测认证服务器地址；
* 下载 exe 双击即用：没有配置文件时自动生成并直接打开设置窗口，
  不需要命令行、不需要抓包填 cookie。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import sys
import time
from datetime import datetime
from os.path import abspath, dirname, join
from platform import system as os_type
from urllib import parse, request
from urllib.error import HTTPError, URLError

try:  # 作为包运行（python -m src / python -m src.__main__）
    from .config import (  # type: ignore
        DEFAULT_RETRY_INTERVAL, DEFAULT_RETRY_TIMEOUT,
        KNOWN_LOGIN_KEYS,
        ConfigError, read_cfg, resolve_config_path, write_cfg, write_template,
    )
    from .notifier import notify as _notify
    from .notifier import toast_available
    from . import captcha as _captcha
    from . import session as _session
except ImportError:  # 直接运行脚本（python src\__main__.py）
    from config import (  # type: ignore
        DEFAULT_RETRY_INTERVAL, DEFAULT_RETRY_TIMEOUT,
        KNOWN_LOGIN_KEYS,
        ConfigError, read_cfg, resolve_config_path, write_cfg, write_template,
    )
    from notifier import notify as _notify  # type: ignore
    from notifier import toast_available
    import captcha as _captcha  # type: ignore
    import session as _session  # type: ignore

APP_NAME = '锐捷 ePorta 连接工具'
__version__ = '1.6.2'

# 用于判断"能不能上外网"的探测地址（返回 204 表示畅通）
INTERNET_PROBE = 'http://connect.rom.miui.com/generate_204'

# 没认证的时候，这些探测请求会被网关劫持并 302 到 portal 登录页，
# 拿 Location 就能反推出认证服务器地址（见 detect_portal）
PORTAL_PROBES = (
    INTERNET_PROBE,
    'http://www.msftconnecttest.com/connecttest.txt',
    'http://connectivitycheck.gstatic.com/generate_204',
    'http://detectportal.firefox.com/success.txt',
)

USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/99.0.4844.51 Safari/537.36 Edg/99.0.1150.39'
)

if getattr(sys, 'frozen', False):
    basedir = getattr(sys, '_MEIPASS', dirname(abspath(sys.executable)))
else:
    basedir = dirname(abspath(__file__))

ICON_PATH = join(basedir, 'wangluo.ico')

_tk_root = None


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog='Ruijie-ePorta-Tool',
        description=f'{APP_NAME}（本机适配版 {__version__}）',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            '示例：\n'
            '  run.bat                     常规运行（联网/断网，带通知）\n'
            '  run.bat --check             只检查配置和网络状态，不做任何登录\n'
            '  run.bat --connect           网络不通时直接联网，不弹确认框\n'
            '  run.bat --disconnect        已联网时直接断网，不弹确认框\n'
            '  设置.bat                     打开图形设置窗口\n'
            '  run.bat --retry             连不上就反复重试，5 分钟后自动退出\n'
            '  run.bat --autostart on      设置开机自启（--retry 模式）\n'
            '  run.bat --autostart status  查看开机自启状态\n'
        ),
    )
    parser.add_argument(
        '--debug', action='store_true',
        help='把发出去的请求体和服务器原始回复写进日志（排查登录失败时用）',
    )
    parser.add_argument('--config', metavar='PATH', help='指定 config.yml 路径')
    parser.add_argument('--gen-config', action='store_true', help='生成默认配置模板后退出')
    parser.add_argument('--check', action='store_true', help='检查配置/校园网/外网状态后退出')
    parser.add_argument('--console', action='store_true', help='把头信息打到控制台')
    parser.add_argument('--no-toast', action='store_true', help='不使用系统通知，改为弹窗')
    parser.add_argument('--gui', action='store_true', help='打开图形设置窗口')
    parser.add_argument(
        '--fetch-cookie', action='store_true',
        help='从认证服务器自动获取 cookie 与 queryString 并写回配置，然后退出',
    )
    parser.add_argument(
        '--open-login', action='store_true',
        help='用默认浏览器打开认证网页，手动输账号密码登录，然后退出（自动登录失败时的兜底）',
    )
    parser.add_argument(
        '--autostart', choices=('on', 'off', 'status'), metavar='on|off|status',
        help='设置/取消/查看开机自启，然后退出',
    )
    parser.add_argument(
        '--retry', action='store_true',
        help='连不上就反复重试，直到连上或超时（开机自启用这个）',
    )
    parser.add_argument(
        '--timeout', type=float, metavar='SEC',
        help=f'重试总时长，默认取配置里的 auto.retry_timeout（{DEFAULT_RETRY_TIMEOUT} 秒）',
    )
    parser.add_argument(
        '--interval', type=float, metavar='SEC',
        help=f'重试间隔，默认取配置里的 auto.retry_interval（{DEFAULT_RETRY_INTERVAL} 秒）',
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--connect', action='store_true', help='强制尝试联网')
    action.add_argument('--disconnect', action='store_true', help='强制断网')
    parser.add_argument(
        '--test-captcha', action='store_true',
        help='取一张验证码图片做自动识别并报结果（不登录，用来确认识别是否正常）',
    )
    parser.add_argument(
        '--detect-server', action='store_true',
        help='自动探测校园网认证服务器地址并打印，然后退出（第一次配置时用）',
    )
    return parser.parse_args(argv)


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #
# 开机自启是用 pythonw 跑的，**没有控制台**：print 出去的东西全丢。
# 所以所有日志同时追加到程序目录下的 eporta.log，出问题直接看这个文件。
LOG_NAME = 'eporta.log'
_log_file = {'path': '', 'failed': False}
_debug = {'on': False}


def log_path() -> str:
    """日志文件路径

    优先放在 config.yml 旁边（用户最容易找到，exe 打包后也在同一层），
    程序目录不可写时退回 %LOCALAPPDATA%\\RuijieEPorta。
    """
    if _log_file['path']:
        return _log_file['path']
    candidates = []
    try:
        candidates.append(dirname(abspath(resolve_config_path(None))))
    except Exception:
        pass
    if getattr(sys, 'frozen', False):
        candidates.append(dirname(abspath(sys.executable)))
    candidates.append(dirname(abspath(__file__)))
    candidates.append(os.path.join(os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'),
                                   'RuijieEPorta'))
    for folder in candidates:
        try:
            os.makedirs(folder, exist_ok=True)
            probe = join(folder, LOG_NAME)
            with open(probe, 'a', encoding='utf-8'):
                pass
            _log_file['path'] = probe
            return probe
        except OSError:
            continue
    _log_file['failed'] = True
    return ''


def log(msg: str, quiet: bool = False) -> None:
    """写一行日志：控制台 + 日志文件（任何模式下都写文件）"""
    # pythonw / PyInstaller 的 windowed exe 里 sys.stdout 是 None，print 会崩
    if not quiet and sys.stdout is not None:
        try:
            print(msg, flush=True)
        except OSError:
            pass
    path = log_path()
    if not path:
        return
    try:
        with open(path, 'a', encoding='utf-8') as fp:
            fp.write(f'{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}\n')
    except OSError:
        _log_file['failed'] = True


def log_debug(msg: str, quiet: bool = False) -> None:
    """只在 --debug 下写的详细日志（请求体、原始响应等）"""
    if _debug['on']:
        log(f'[调试] {msg}', quiet)


def console_attached() -> bool:
    """当前进程有没有能给人看的控制台

    ``pythonw.exe`` 和 PyInstaller 的 windowed exe 里 ``sys.stdout`` 是 ``None``：
    这时候打印等于没打，出了错只能弹窗告诉用户。
    """
    return sys.stdout is not None and sys.stderr is not None


# 当前是不是"没人看着控制台"的模式（GUI / 双击 exe / 开机自启），由 main() 设置
_ui = {'windowed': False}


def windowed() -> bool:
    """该弹模态框吗

    --console 跑的时候不弹：脚本/批处理里弹一个没人点的对话框会直接卡死。
    没控制台可看（pythonw、双击 exe）的时候必须弹，否则错误就石沉大海了。
    """
    return bool(_ui['windowed']) or not console_attached()


def get_tk_root():
    """惰性创建隐藏的 tkinter 主窗口（仅用于对话框图标）"""
    global _tk_root
    if _tk_root is None:
        try:
            from tkinter import Tk

            _tk_root = Tk()
            _tk_root.withdraw()
            if os.path.exists(ICON_PATH):
                try:
                    _tk_root.iconbitmap(ICON_PATH)
                except Exception:
                    pass
        except Exception:
            _tk_root = False  # 没有图形环境
    return _tk_root or None


def show_error(title: str, message: str, quiet: bool = False,
               silent: bool = False) -> None:
    """报错

    :param quiet: 调用方已经知道自己跑在什么环境下（一般直接传 ``quiet``，
        也就是"没要求 --console"）：此时不打印，改为弹窗。
    :param silent: 重试模式（开机自启）下必须静默：pythonw 无人值守，
        弹个模态框就会一直挂着没人点。
    """
    log(f'[错误] {title}: {message}', quiet=quiet)
    if silent or not (quiet or windowed()):
        return
    get_tk_root()
    try:
        from tkinter.messagebox import showerror

        showerror(title=title, message=message)
    except Exception:
        log(f'[错误] {title}: {message}', quiet=True)


def show_warning(title: str, message: str, quiet: bool = False,
                 silent: bool = False) -> None:
    """警告，参数含义同 show_error"""
    log(f'[警告] {title}: {message}', quiet=quiet)
    if silent or not (quiet or windowed()):
        return
    get_tk_root()
    try:
        from tkinter.messagebox import showwarning

        showwarning(title=title, message=message)
    except Exception:
        log(f'[警告] {title}: {message}', quiet=True)


def ask_yes_no(title: str, message: str) -> bool:
    get_tk_root()
    try:
        from tkinter.messagebox import askyesno

        return bool(askyesno(title=title, message=message, icon='info'))
    except Exception:
        return False


def notify(title: str, msg: str, use_toast: bool = True) -> None:
    """发送通知（可用 --no-toast 强制走弹窗）"""
    if use_toast:
        _notify(title=title, msg=msg, icon=ICON_PATH)
    else:
        show_warning(title, msg)


# --------------------------------------------------------------------------- #
# 网络检测与 ePortal 交互
# --------------------------------------------------------------------------- #
def test_internet(host: str = INTERNET_PROBE, timeout: float = 2.0, allow_status: bool = True) -> bool:
    """测试某个地址是否可达

    :param host: 探测地址
    :param timeout: 超时时间（秒）
    :param allow_status: 非 generate_204 探测时，2xx 是否也算通
    """
    try:
        resp = request.urlopen(host, timeout=timeout)
    except HTTPError as e:
        # 认证服务器返回 4xx/5xx 也说明它活着，能连上
        return allow_status and 200 <= getattr(e, 'code', 0) < 500
    except (URLError, OSError, ValueError):
        return False
    except Exception:
        return False
    else:
        try:
            status = resp.status
        finally:
            resp.close()
        if host.endswith('generate_204'):
            return status == 204
        return allow_status and (200 <= status <= 208 or status == 226)


class _NoRedirect(request.HTTPRedirectHandler):
    """不自动跟随 302：跳转目标本身就是我们要找的认证服务器。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def detect_portal(timeout: float = 4.0, quiet: bool = False) -> str:
    """自动探测校园网认证服务器地址

    原理：没认证的时候，网关会把"访问外网"的请求劫持并 302 到 portal 登录页，
    所以只要故意去访问一个正常的联网探测地址、抓住这次跳转的 Location，
    里面的主机就是认证服务器。已经联网（不需要认证）时探测不出来，返回空字符串。

    :return: 形如 ``http://10.0.0.1`` 的地址；探测不到返回 ``''``
    """
    for probe in PORTAL_PROBES:
        location = ''
        try:
            opener = request.build_opener(_NoRedirect)
            resp = opener.open(request.Request(probe, headers={'User-Agent': USER_AGENT}),
                               timeout=timeout)
            resp.close()
            continue  # 正常返回 200/204：没被劫持，换下一个探测点
        except HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                location = e.headers.get('Location') or ''
            else:
                continue
        except (URLError, OSError, ValueError):
            continue
        except Exception:  # noqa: BLE001 - 探测失败不该影响主流程
            continue
        netloc = parse.urlsplit(location).netloc
        if netloc:
            server = f'http://{netloc}'
            log(f'探测到认证服务器：{server}（{probe} 被重定向到 {location}）', quiet)
            return server
    log('没能探测到认证服务器（可能当前已经联网，或不在校园网里）', quiet)
    return ''


def build_headers(cfg: dict) -> dict:
    server = cfg['url']['server']
    host_only = parse.urlsplit(server).netloc or server
    headers = {
        'Connection': 'keep-alive',
        'User-Agent': USER_AGENT,
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'Accept': '*/*',
        'Accept-Encoding': 'gzip, deflate',
        'Accept-Language': 'zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7',
        'Host': host_only,
        'Origin': server,
    }
    if cfg.get('cookie'):
        headers['Cookie'] = cfg['cookie']
    headers.update(cfg.get('headers') or {})
    return headers


def _form_body(data: dict) -> str:
    """把参数拼成表单串

    **空值参数也要发**（对齐浏览器）：页面里的 ``AuthInterFace.js`` 永远是
    ``userId=..&password=..&service=&queryString=..&operatorPwd=&operatorUserId=&validcode=&passwordEncrypt=false``，
    空参数也是"带上了"的。早期版本为了"别发空值"把 ``service`` / ``validcode``
    过滤掉了，结果服务器可能把它当成"这次请求没带验证码"而回「验证码错误」——
    浏览器一次就过、自动登录却被拒，差别就在这儿。
    """
    pairs = []
    for key, value in data.items():
        text = '' if value is None else str(value)
        pairs.append((str(key), text))
    return parse.urlencode(pairs)


def _mask_form(form: str) -> str:
    """写日志时把密码/验证码打码（日志可能被发出来求助）"""
    return re.sub(r'(password|operatorPwd|validcode)=[^&]*', r'\1=***', form)


def set_debug(on: bool) -> None:
    """打开/关闭调试日志（GUI 默认打开：出问题只发 eporta.log 就够了）"""
    _debug['on'] = bool(on)


def _post_json(url: str, headers: dict, data: dict) -> tuple[str, dict]:
    """POST 表单并解析 JSON 响应

    :return: (result, payload)；result 取值 success / fail / unknown / error
    """
    form = _form_body(data)
    log_debug(f'POST {url}')
    log_debug(f'请求头: {headers}')
    log_debug(f'请求体: {_mask_form(form)}')
    body = form.encode('utf-8') if data else None
    req = request.Request(url, data=body, headers=headers, method='POST')
    with request.urlopen(req, timeout=15) as res:
        raw = res.read()
        log_debug(f'HTTP {res.status}，原始回复: {raw.decode("utf-8", errors="replace")[:800]}')
    text = raw.decode('utf-8', errors='replace').strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return 'error', {'raw': text[:500]}
    if not isinstance(payload, dict):
        return 'error', {'raw': text[:500]}
    result = str(payload.get('result', '')).lower()
    if result not in ('success', 'fail'):
        result = 'unknown'
    return result, payload


# 自动抓会话的节流：queryString 为空时才去抓，避免每次重试都白等
_session_probe = {'last': 0.0, 'cooldown': 30.0}

# 验证码最多让用户重试几次（输错是常事，但不能无限问）
_MAX_CAPTCHA_TRIES = 3

# 上一次 do_connect 失败的原因（重试循环用它判断"再试也没意义"）
_last_fail = {'reason': ''}


def open_login_page(url: str) -> bool:
    """用系统默认浏览器打开认证网页，让人手动输账号密码

    为什么这条一定能成：认证网页里的 JavaScript 自己会带上服务器当时下发的
    加密设备参数（wlanuserip/nasip/mac…），所以只要页面能打开，手动登录就有效。
    自动登录失败时（比如参数抓不到、学校临时改成统一身份认证）可以走这条路。
    """
    try:
        import webbrowser

        return bool(webbrowser.open(url))
    except Exception:
        return False


def run_test_captcha(cfg: dict, quiet: bool = False) -> int:
    """自检验证码自动识别：取一张图 → 识别 → 把图和结果都留下，成功返回 0

    这条命令**只发一次取图请求**，不登录、不碰在线状态，所以可以随时运行。
    取到的图片会存成 config.yml 旁边的 validcode_last.png，好让人肉眼核对识别对不对。
    """
    server = (cfg.get('url') or {}).get('server', '')
    if not server:
        show_error('验证码自检', '配置里没有 url.server，无法取验证码。', quiet, False)
        return 2
    log(f'正在取验证码图片：{server}', quiet)
    info = ({} if _session is None else _session.fetch_validcode(server))
    image = info.get('image') or b''
    if not image:
        show_error('验证码自检',
                   f'没能取到验证码图片：\n{info.get("error") or "未知原因"}\n'
                   f'（取图地址：{info.get("url") or server}）', quiet, False)
        return 1

    save_path = join(dirname(abspath(log_path())), 'validcode_last.png')
    try:
        with open(save_path, 'wb') as fp:
            fp.write(image)
    except OSError:
        save_path = ''

    code, detail = ('', '验证码模块不可用')
    if _captcha is not None:
        code, detail = _captcha.recognize(image)

    lines = [f'取图地址：{info.get("url")}',
             f'图片大小：{len(image)} 字节' + (f'，已存到 {save_path}' if save_path else '（存盘失败）'),
             f'识别结果：{code or "没认出来"}',
             f'说明    ：{detail}']
    for line in lines:
        log(line, quiet)
    if not code:
        log('自动识别失败时，程序会退化成弹窗让人输入（有人在场时），'
            '或在无人值守时直接报错退出。', quiet)
        return 1
    return 0


def auto_fill_session(cfg: dict, headers: dict, quiet: bool = False,
                      force: bool = False) -> bool:
    """queryString 为空时，自动去认证服务器抓一次登录页参数

    锐捷 ePortal 的设备参数（wlanuserip / wlanacname / nasip / mac）只在"当前未认证"
    时才由服务器下发，而且是服务器加密过的十六进制串，自己用明文 IP/MAC 拼出来的
    一定会被拒（报"WEB认证设备未注册"）。所以这里在登录前自动抓一次并直接用于本次登录。

    返回 True 表示 cfg / headers 被更新过。
    """
    login_data = cfg.get('login_data')
    if not isinstance(login_data, dict) or _session is None:
        return False
    if str(login_data.get('queryString') or '').strip() and not force:
        return False
    now = time.monotonic()
    if not force and now - _session_probe['last'] < _session_probe['cooldown']:
        return False
    _session_probe['last'] = now

    info = _session.fetch_session(cfg['url']['server'], timeout=6.0)
    if info.get('online'):
        log('自动获取会话：当前已经在线，不需要登录参数。', quiet)
        return False
    if not info.get('queryString'):
        reason = (info.get('error') or '认证服务器没有下发登录页参数').replace('\n', ' ')
        log(f'自动获取会话：没取到（{reason}）', quiet)
        return False

    login_data['queryString'] = info['queryString']
    if info.get('cookie'):
        cfg['cookie'] = info['cookie']
    headers.clear()
    headers.update(build_headers(cfg))
    log('已自动获取登录页参数（wlanuserip/nasip/mac 等），用本次抓到的参数登录…', quiet)
    return True


def _needs_captcha(payload, msg: str) -> bool:
    """判断服务器是不是在要图形验证码"""
    if isinstance(payload, dict):
        if str(payload.get('validCodeUrl') or '').strip():
            return True
        if str(payload.get('validcode') or '').strip():
            return True
    text = str(msg)
    return ('验证码' in text) or ('validcode' in text.lower())


def ask_captcha(image_bytes: bytes, quiet: bool = False) -> str:
    """弹出验证码图片让人输入；没有图形环境就存成文件 + 终端输入

    返回用户输入的验证码（空串表示放弃）。
    """
    import base64
    import tempfile

    root = get_tk_root()
    if root is None:
        path = os.path.join(tempfile.gettempdir(), 'eporta_validcode.png')
        try:
            with open(path, 'wb') as f:
                f.write(image_bytes)
        except OSError:
            return ''
        log(f'验证码图片已保存：{path}', quiet)
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            return input('请输入图片里的验证码：').strip()
        except Exception:
            return ''

    from tkinter import Button, Entry, Label, StringVar, Toplevel

    result = {'code': ''}
    try:
        photo = tk_photoimage(root, image_bytes)
    except Exception:
        photo = None
    try:
        win = Toplevel(root)
        win.title('请输入验证码')
        win.resizable(False, False)
        win.attributes('-topmost', True)
        Label(win, text='校园网认证需要验证码，请输入图片里的字符：').pack(padx=14, pady=(12, 6))
        if photo is not None:
            lbl = Label(win, image=photo)
            lbl.image = photo  # type: ignore[attr-defined]
            lbl.pack(padx=14, pady=4)
        var = StringVar()
        entry = Entry(win, textvariable=var, width=14, justify='center',
                      font=('Consolas', 14))
        entry.pack(padx=14, pady=6)
        entry.focus_set()

        def ok(_event=None):
            result['code'] = var.get().strip()
            win.destroy()

        def cancel():
            result['code'] = ''
            win.destroy()

        row = ttk_or_tk_frame(win)
        row.pack(pady=(0, 12))
        Button(row, text='确定', width=10, command=ok).pack(side='left', padx=6)
        Button(row, text='取消', width=10, command=cancel).pack(side='left', padx=6)
        win.bind('<Return>', ok)
        win.bind('<Escape>', lambda _e: cancel())
        center_window(win)
        win.grab_set()
        root.wait_window(win)
    except Exception as e:
        log(f'验证码窗口打不开（{e}），改用终端输入', quiet)
        return ''
    return result['code']


def tk_photoimage(root, image_bytes: bytes):
    """把 PNG 字节变成 tkinter 能显示的图片对象"""
    import base64
    from tkinter import PhotoImage

    try:
        return PhotoImage(master=root, data=base64.b64encode(image_bytes).decode('ascii'))
    except Exception:
        # 老版本 Tk 不支持直接读 PNG，转成 GIF/PNG 文件再读
        import tempfile
        path = os.path.join(tempfile.gettempdir(), 'eporta_validcode.png')
        with open(path, 'wb') as f:
            f.write(image_bytes)
        return PhotoImage(master=root, file=path)


def ttk_or_tk_frame(parent):
    from tkinter import Frame

    return Frame(parent)


def center_window(win) -> None:
    """把窗口摆到屏幕中间（弹验证码时更醒目）"""
    try:
        win.update_idletasks()
        w, h = win.winfo_width(), win.winfo_height()
        x = (win.winfo_screenwidth() - w) // 2
        y = (win.winfo_screenheight() - h) // 2
        win.geometry(f'+{max(x, 0)}+{max(y, 0)}')
    except Exception:
        pass


def _login_form(cfg: dict) -> dict:
    """拼出登录表单：**顺序和浏览器一致，缺的键补空串**

    为什么要补：``AuthInterFace.js`` 里写死了
    ``"userId=" + userId + "&password=" + password + "&service=" + service +
    "&queryString=" + queryString + "&operatorPwd=" + operatorPwd +
    "&operatorUserId=" + operatorUserId + "&validcode=" + validcode``，
    即使值为空，这个字段也是"出现过"的。服务器有可能按"参数在不在"来判断，
    而不是按"值空不空"。所以这里一个都不许少。
    """
    login_data = cfg.get('login_data') or {}
    form: dict = {}
    for key in KNOWN_LOGIN_KEYS:
        value = login_data.get(key, '')
        form[key] = '' if value is None else value
    for key, value in login_data.items():
        if key not in form:
            form[key] = '' if value is None else value
    return form


def do_connect(cfg: dict, headers: dict, quiet: bool = False,
               silent: bool = False, _retried: bool = False,
               captcha_prompt=None) -> bool:
    """登录联网，成功返回 True

    :param silent: 不弹模态框（重试模式使用）
    :param _retried: 内部用，标记"参数失效后已经重抓重试过一次"，避免死循环
    :param captcha_prompt: 需要验证码时用来要输入的回调，签名 ``(png_bytes) -> str``。
        默认弹 tkinter 窗口/终端输入；GUI 传入自己的实现（把弹窗调度回主线程）。
    """
    # 配置里 queryString 为空时先自动抓一次，用户不必手动点「获取会话」
    auto_fill_session(cfg, headers, quiet)
    url = cfg['url']['server'] + cfg['url']['login']
    tries = 0
    _last_fail['reason'] = ''

    while True:
        data = _login_form(cfg)
        log(f'正在登录：{url}', quiet)

        try:
            result, payload = _post_json(url, headers, data)
        except HTTPError as e:
            show_error('联网失败', f'服务器返回错误：HTTP {e.code}\n{e.reason}\n{url}',
                       quiet, silent)
            return False
        except (URLError, OSError) as e:
            show_error('联网失败', f'无法连接认证服务器：\n{e}\n{url}\n\n请检查 url.server 是否正确。',
                       quiet, silent)
            return False
        except Exception as e:
            show_error('未知错误', f'出现错误：\n{e}', quiet, silent)
            return False

        if result == 'success':
            message = str(payload.get('message') or '')
            notify('联网成功', message if message else '网络已连接！')
            return True

        if result != 'fail':
            show_error('错误', f'认证服务器返回了无法识别的内容：\n{payload}', quiet, silent)
            return False

        msg = payload.get('message') or payload

        # 1) 设备参数失效（换了网络/网卡）：清掉、解除节流、立刻重抓重试一次
        if '设备未注册' in str(msg) and not _retried:
            cfg.get('login_data', {}).pop('queryString', None)
            cfg['cookie'] = ''
            _session_probe['last'] = 0.0
            headers.clear()
            headers.update(build_headers(cfg))
            if auto_fill_session(cfg, headers, quiet, force=True):
                log('之前的设备参数已失效，已重新抓取，再登录一次…', quiet)
                _retried = True
                continue
            show_error(
                '联网失败',
                f'{msg}\n\n说明：这条报错表示 login_data.queryString（设备参数）为空或已失效。\n'
                '设备参数由服务器在"本机尚未认证"时下发，而且和当时的 IP/网卡绑定，\n'
                '换了网络（比如从网线换成 WiFi）就会失效。\n'
                '本工具会自动重抓；如果一直失败，请确认当前确实连在校园网里。\n'
                '还不行就用「打开认证网页」在浏览器里手动登录（命令行 --open-login）。',
                quiet, silent)
            return False

        # 2) 要验证码：取图 → 先自动识别（开机自启全靠它）→ 认不出才问人
        #    然后用"取图那一轮的 JSESSIONID"重新登录
        if _needs_captcha(payload, str(msg)) and tries < _MAX_CAPTCHA_TRIES:
            info = ({} if _session is None
                    else _session.fetch_validcode(cfg['url']['server']))
            image = info.get('image') or b''
            if not image:
                show_error(
                    '联网失败',
                    f'{msg}\n\n需要验证码，但没能取到验证码图片：\n'
                    f'{info.get("error") or "未知原因"}',
                    quiet, silent)
                return False

            code = ''
            detail = ''
            if _captcha is not None:
                code, detail = _captcha.recognize(image)
                log(f'验证码自动识别：{code or "没认出来"}（{detail}）', quiet)

            if not code:
                # 自动识别失败：有人在（GUI/命令行）就问人，这也是一条兜底路
                if captcha_prompt is not None or not silent:
                    prompt = captcha_prompt or (lambda png: ask_captcha(png, quiet))
                    code = (prompt(image) or '').strip()
                    if not code:
                        show_error('联网失败', f'{msg}\n\n（没有输入验证码，已放弃）',
                                   quiet, silent)
                        return False

            if not code:
                # 无人值守 + 识别失败：换一张图再认一次（图片偶尔异常是可能的），
                # 试满 _MAX_CAPTCHA_TRIES 次还不行才认输，别一次失手就放弃开机自启
                tries += 1
                if tries < _MAX_CAPTCHA_TRIES:
                    log(f'验证码没认出来（{detail}），换一张再试（第 {tries} 次）…', quiet)
                    continue
                _last_fail['reason'] = 'captcha'
                show_error(
                    '联网失败',
                    f'{msg}\n\n这台认证服务器要求输入图形验证码，'
                    f'自动识别连续 {tries} 张都没认出来（最后一次：{detail}），'
                    '而自动重试时没有人能输入。\n'
                    '请先手动登录一次（设置窗口里的「打开认证网页」，'
                    '或点「立即联网」并在弹窗里输入验证码）。',
                    quiet, silent)
                return False

            # 关键：验证码和取图时的 JSESSIONID 绑定，必须换成这一轮的 cookie
            if info.get('cookie'):
                cfg['cookie'] = info['cookie']
            cfg.setdefault('login_data', {})['validcode'] = code
            headers.clear()
            headers.update(build_headers(cfg))
            tries += 1
            log(f'带着验证码 {code} 重新登录（第 {tries} 次）…', quiet)
            continue

        extra = ''
        if 'password' in str(msg).lower() or '密码' in str(msg):
            extra = '\n\n提示：请检查 login_data.password 与 passwordEncrypt 是否与抓包一致。'
        if _needs_captcha(payload, str(msg)):
            _last_fail['reason'] = 'captcha'
            extra = ('\n\n验证码试了 %d 次都没过。可以点「打开认证网页」在浏览器里手动登录。'
                     % tries) if tries else extra
        show_error('联网失败', f'{msg}{extra}', quiet, silent)
        return False


def do_disconnect(cfg: dict, headers: dict, quiet: bool = False,
                  silent: bool = False) -> bool:
    """断网，成功返回 True"""
    url = cfg['url']['server'] + cfg['url']['logout']
    # 同样不丢空值：跟登录一个道理
    data = {k: ('' if v is None else v)
            for k, v in (cfg.get('logout_data') or {}).items()}
    log(f'正在断网：{url}', quiet)

    try:
        result, payload = _post_json(url, headers, data)
    except HTTPError as e:
        show_error('断网失败', f'服务器返回错误：HTTP {e.code}\n{e.reason}\n{url}',
                   quiet, silent)
        return False
    except (URLError, OSError) as e:
        show_error('断网失败', f'无法连接认证服务器：\n{e}\n{url}', quiet, silent)
        return False
    except Exception as e:
        show_error('未知错误', f'出现错误：\n{e}', quiet, silent)
        return False

    if result == 'success':
        notify('已断网', '断网成功！')
        return True
    if result == 'fail':
        show_error('断网失败', f'断网失败：\n{payload.get("message") or payload}',
                   quiet, silent)
        return False
    show_error('错误', f'认证服务器返回了无法识别的内容：\n{payload}', quiet, silent)
    return False


# --------------------------------------------------------------------------- #
# 单实例检测（原来依赖 pywin32，这里用 ctypes 直接调 kernel32）
# --------------------------------------------------------------------------- #
def acquire_single_instance_lock(name: str = 'ruijie_eporta_tool') -> bool:
    """获取命名互斥体，True 表示本程序是唯一实例"""
    if os_type() != 'Windows':
        return True
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW(None, False, name)
        ERROR_ALREADY_EXISTS = 183
        return kernel32.GetLastError() != ERROR_ALREADY_EXISTS
    except Exception:
        return True


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def run_with_retry(cfg: dict, headers: dict, quiet: bool = False,
                   timeout: float | None = None, interval: float | None = None,
                   disconnect: bool = False) -> int:
    """--retry：无人值守地反复尝试，连上就收工，到点无条件退出

    这是给"开机自启"用的：系统刚起来时 WLAN 往往还没连上，
    一次失败就退出没意义，所以 5 分钟内每隔几秒重试一次；
    一旦成功立刻退出，超过时限（默认 300 秒）不论成败都关掉程序。

    注意全程 silent，不弹任何模态框 —— pythonw 下没人去点确定。
    """
    auto = cfg.get('auto') or {}
    try:
        timeout = float(timeout if timeout is not None
                        else auto.get('retry_timeout', DEFAULT_RETRY_TIMEOUT))
    except (TypeError, ValueError):
        timeout = float(DEFAULT_RETRY_TIMEOUT)
    try:
        interval = float(interval if interval is not None
                         else auto.get('retry_interval', DEFAULT_RETRY_INTERVAL))
    except (TypeError, ValueError):
        interval = float(DEFAULT_RETRY_INTERVAL)
    if timeout <= 0:
        timeout = float(DEFAULT_RETRY_TIMEOUT)
    if interval <= 0:
        interval = float(DEFAULT_RETRY_INTERVAL)

    what = '断网' if disconnect else '联网'
    log(f'重试模式：最长 {timeout:g} 秒，每隔约 {interval:g} 秒尝试一次{what}。', quiet)

    deadline = time.monotonic() + timeout
    attempt = 0
    announced = False

    while True:
        attempt += 1
        if disconnect:
            if not test_internet(timeout=3):
                log(f'第 {attempt} 次检查：当前并未联网，无需断网。', quiet)
                notify('未联网', '当前设备并未联网，无需断网')
                return 0
            if do_disconnect(cfg, headers, quiet, silent=True):
                log(f'第 {attempt} 次尝试：断网成功。', quiet)
                return 0
        else:
            if test_internet(timeout=3):
                if attempt == 1:
                    notify('设备已联网', '网络本来就是通的噢~')
                log(f'第 {attempt} 次检查：已联网，任务完成。', quiet)
                return 0
            if do_connect(cfg, headers, quiet, silent=True):
                log(f'第 {attempt} 次尝试：联网成功。', quiet)
                return 0
            if _last_fail['reason'] == 'captcha':
                # 要输验证码的学校，后台重试再多也没用（没人能输），立刻收工别刷屏
                log(f'{what}需要输入图形验证码，后台重试无法完成，程序退出。', quiet)
                notify(f'{what}需要验证码',
                       '这台认证服务器要输入验证码，自动重试输不了。\n'
                       '请打开工具点「立即联网」手动输入一次。')
                return 1

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if not announced:
            # 只提醒一次，免得每 10 秒弹一条通知刷屏
            announced = True
            notify(f'{what}尚未成功', f'正在后台重试，最长再等 {timeout:g} 秒…')
        sleep_for = min(interval, remaining)
        log(f'第 {attempt} 次{what}失败，{sleep_for:g} 秒后重试（剩余 {remaining:.1f} 秒）。', quiet)
        time.sleep(sleep_for)

    log(f'{timeout:g} 秒内未能{what}，程序退出。', quiet)
    notify(f'{what}失败', f'{timeout:g} 秒内没能{what}，程序已退出。')
    return 1


def run_check(cfg: dict, config_path: str) -> int:
    """--check：只体检，不登录/断网"""
    server = cfg['url']['server']
    lines: list[str] = [f'{APP_NAME} v{__version__} 自检']
    add = lines.append
    add(f'  Python      : {sys.version.split()[0]} ({sys.executable})')
    add(f'  配置文件    : {config_path}')
    add(f'  认证服务器  : {server}')
    add(f'  登录接口    : {cfg["url"]["login"]}')
    add(f'  下线接口    : {cfg["url"]["logout"]}')
    add(f'  学号        : {cfg["login_data"].get("userId", "")}')
    add(f'  密码已填写  : {"是" if str(cfg["login_data"].get("password", "")) else "否"}')
    add(f'  Cookie 已填 : {"是" if cfg.get("cookie") else "否（联网时会自动抓）"}')
    add(f'  queryString : {"已填" if str(cfg["login_data"].get("queryString") or "").strip() else "否（联网时会自动抓）"}')
    add(f'  登录参数    : {", ".join(cfg.get("login_data") or {}) or "（空）"}')
    add(f'  下线参数    : {", ".join(cfg.get("logout_data") or {}) or "（空，部分学校无需）"}')
    add(f'  检查校园网  : {cfg["funtion"].get("check_school_network")}')
    add(f'  断网询问    : {cfg["funtion"].get("disconnect_network")}')
    add('  --- 系统通知 ---')
    if toast_available(verbose=True):
        add('  系统通知    : 可用（Windows 通知中心）')
    else:
        add('  系统通知    : 不可用，将使用弹窗（原因见上）')
    add('  --- 连通性 ---')
    school_ok = test_internet(server, timeout=3)
    add(f'  校园网认证服务器 : {"可达" if school_ok else "不可达"}')
    internet_ok = test_internet(timeout=3)
    add(f'  外网(generate_204): {"已联网" if internet_ok else "未联网"}')
    add('自检完成：配置可正常读取。')
    if not school_ok:
        add('提示：认证服务器不可达，通常说明当前不在校园网内，或者服务器地址填错了。')

    text = '\n'.join(lines)
    print(text, flush=True)
    # 自检结果也写进 eporta.log：exe 没有控制台，报错窗口一关就什么都不剩了
    for line in lines:
        log(line, quiet=True)

    # 打包成 exe 后没有控制台，把自检结果弹窗显示，否则用户什么都看不到
    if getattr(sys, 'frozen', False) and (sys.stdout is None or not sys.stdout.isatty()):
        _show_report_window(f'{APP_NAME} - 自检结果', text)
    return 0


def _show_report_window(title: str, text: str) -> None:
    """把一段文本显示在可滚动的只读窗口里（给没有控制台的 exe 用）"""
    try:
        import tkinter as tk
        from tkinter import ttk

        root = tk.Tk()
        root.title(title)
        root.geometry('820x600')
        if os.path.exists(ICON_PATH):
            try:
                root.iconbitmap(ICON_PATH)
            except Exception:
                pass
        frame = ttk.Frame(root, padding=10)
        frame.pack(fill='both', expand=True)
        box = tk.Text(frame, wrap='word', font=('Consolas', 10))
        bar = ttk.Scrollbar(frame, command=box.yview)
        box.configure(yscrollcommand=bar.set)
        bar.pack(side='right', fill='y')
        box.pack(fill='both', expand=True)
        box.insert('1.0', text)
        box.configure(state='disabled')
        ttk.Button(root, text='关闭', command=root.destroy).pack(pady=(0, 10))
        root.mainloop()
    except Exception as e:  # noqa: BLE001 - 弹不出来也不该让程序崩
        print(f'（无法显示窗口：{e}）', file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    quiet = not args.console
    _ui['windowed'] = quiet
    _debug['on'] = bool(getattr(args, 'debug', False))

    # 每次运行都留一行抬头，方便事后翻 eporta.log 对齐时间线
    log(f'===== {APP_NAME} {__version__} 启动：{" ".join(sys.argv[1:]) or "(无参数)"} =====',
        quiet)
    log(f'日志文件：{log_path() or "(写不了，日志只在控制台)"}', quiet)

    # 图形设置窗口：不依赖配置文件是否存在，进去再建
    if args.gui:
        try:
            from . import gui
        except ImportError:
            import gui  # type: ignore
        return gui.main(args.config)

    # 开机自启开关：写注册表后直接退出
    if args.autostart:
        try:
            from . import autostart
        except ImportError:
            import autostart  # type: ignore
        if not autostart.is_windows():
            print('开机自启只支持 Windows 系统。')
            return 1
        value = args.autostart
        if value == 'status':
            print(f'开机自启：{"已开启" if autostart.is_enabled() else "未开启"}')
            cmd = autostart.get_command()
            if cmd:
                print(f'注册表命令：{cmd}')
            return 0
        ok, info = autostart.enable() if value == 'on' else autostart.disable()
        print(info)
        return 0 if ok else 1

    # 自动探测认证服务器：第一次配置时不用自己去翻 IP
    if args.detect_server:
        server = detect_portal(quiet=quiet)
        if not server:
            print('没探测到认证服务器。\n'
                  '可能原因：当前已经联网（不需要认证）、不在校园网里，或被网关放行了探测请求。\n'
                  '可以在浏览器里随便打开一个 http 网站，看它跳转到哪个地址，那就是认证服务器。')
            return 1
        print(f'认证服务器：{server}\n'
              f'把这个地址填进 config.yml 的 url.server（或设置窗口里的「认证服务器」）即可。')
        return 0

    config_path = resolve_config_path(args.config)

    if args.gen_config:
        if os.path.exists(config_path):
            print(f'配置文件已存在，未覆盖：{config_path}')
            return 1
        write_template(config_path)
        print(f'已生成配置文件：{config_path}\n请填写 login_data 等信息后重新运行。')
        return 0

    # 这些参数表示"我知道自己在干什么"，出问题就直接报错退出，不要弹设置窗口
    explicit_action = (args.check or args.fetch_cookie or args.test_captcha or args.open_login
                       or args.connect or args.disconnect or args.retry)

    try:
        cfg = read_cfg(config_path)
    except ConfigError as e:
        created = False
        if not os.path.exists(config_path):
            # 首次运行：自动生成模板（放在 exe 旁边），不用用户自己去建文件
            try:
                write_template(config_path)
                created = True
            except OSError as werr:
                show_error('配置错误', f'配置文件不存在且无法创建：\n{config_path}\n{werr}')
                return 2
        # 下载下来直接双击的情况：与其丢一个错误框让人一脸懵，不如把设置窗口开出来，
        # 让人在里面把学号密码填了就完事（这是"下载就能用"的关键一步）
        if not explicit_action:
            hint = ('第一次使用：已经生成好配置文件了，把「学号」和「密码」填上，'
                    '点「保存配置」就完事。认证服务器地址不确定的话点右边的「自动探测」。'
                    if created else
                    f'配置还没填好（{e}）。'
                    '请在下面改完，点「保存配置」。')
            log(f'[提示] {hint}', quiet)
            try:
                from . import gui
            except ImportError:
                import gui  # type: ignore
            return gui.main(config_path, first_run=hint)
        if created:
            msg = (f'配置文件不存在，已生成模板：\n{config_path}\n\n'
                   '请填写学号、密码等信息后再运行。')
        else:
            msg = f'{e}\n\n配置文件：{config_path}'
        show_error('配置错误', msg, quiet)
        return 2

    if args.check:
        return run_check(cfg, config_path)

    # 自动抓会话：访问认证服务器拿 JSESSIONID 与 queryString
    if args.fetch_cookie:
        try:
            from . import session
        except ImportError:
            import session  # type: ignore
        info = session.fetch_session(cfg['url']['server'])
        print(session.describe(info))
        if info.get('online'):
            print('当前已经在线，配置未改动。想重新抓取请先断网/下线。')
            return 0
        if not info['cookie'] and not info['queryString']:
            print('未能取到任何会话信息，配置未改动。')
            return 1
        if info['cookie']:
            cfg['cookie'] = info['cookie']
        if info['queryString']:
            cfg['login_data']['queryString'] = info['queryString']
        write_cfg(cfg, config_path)
        print(f'已写回配置：{config_path}')
        return 0

    # 验证码识别自检：取一张图、识别、把图存下来给人对照（不登录，不影响在线状态）
    if args.test_captcha:
        return run_test_captcha(cfg, quiet)

    # 手动登录兜底：直接打开认证网页，自己输账号密码
    if args.open_login:
        url = cfg['url']['server']
        log(f'正在用默认浏览器打开认证网页：{url}', quiet)
        if open_login_page(url):
            print(f'已打开 {url}\n'
                  f'在网页里输入学号 {cfg["login_data"].get("userId", "")} 和密码即可登录。\n'
                  f'（网页里的 JS 会自己带上服务器下发的设备参数，所以这条路一定能成）')
            return 0
        show_error('打不开浏览器', f'请手动在浏览器里打开：\n{url}', quiet, False)
        return 1

    headers = build_headers(cfg)

    # 重试模式（开机自启）：不弹框、不询问，反复尝试到成功或超时
    if args.retry:
        return run_with_retry(cfg, headers, quiet, args.timeout, args.interval,
                              disconnect=args.disconnect)

    school_check = cfg['funtion'].get('check_school_network', True)
    # 配置文件里 true/false 已被转成字符串 'true'/'false'
    school_check = str(school_check).lower() in ('true', '1', 'yes')
    disconnect_ask = str(cfg['funtion'].get('disconnect_network', True)).lower() in ('true', '1', 'yes')

    log(f'{APP_NAME} v{__version__}｜配置：{config_path}', quiet)

    # 1) 先确认自己在校园网里（否则连认证服务器都找不到，直接重试一次再退出）
    if school_check and not (args.connect or args.disconnect):
        if not test_internet(cfg['url']['server'], timeout=3):
            notify(
                '非校园网环境',
                '当前不在校园网环境，10 秒后重新检测\n'
                '（若系统刚启动，还没连上任何网络，属正常现象）',
            )
            log('校园网认证服务器不可达，10 秒后重试…', quiet)
            time.sleep(10)
            if not test_internet(cfg['url']['server'], timeout=2):
                notify('非校园网环境', '当前不在校园网环境，不自动尝试联网，程序退出')
                log('仍不可达，退出（未做任何登录操作）。', quiet)
                return 0

    # 2) 判断外网是否已通
    online = test_internet(timeout=3)

    if args.disconnect:
        if not online:
            log('当前并未联网，无需断网。', quiet)
            notify('未联网', '当前设备并未联网，无需断网')
            return 0
        return 0 if do_disconnect(cfg, headers, quiet) else 1

    if args.connect:
        if online:
            log('当前已联网，无需登录。', quiet)
            notify('设备已联网', '网络本来就是通的噢~')
            return 0
        return 0 if do_connect(cfg, headers, quiet) else 1

    if online:
        if disconnect_ask:
            if ask_yes_no('网络已连接', '设备目前已联网，是否需要断网？'):
                return 0 if do_disconnect(cfg, headers, quiet) else 1
            return 0
        notify('设备已联网', '网络本来就是通的噢~')
        log('已联网，按配置不询问断网，退出。', quiet)
        return 0

    # 3) 没联网 → 登录
    return 0 if do_connect(cfg, headers, quiet) else 1


def entry_point() -> int:
    """脚本入口：做单实例检测，然后跑 main()"""
    args = parse_args()
    quiet = not args.console
    _ui['windowed'] = quiet

    # 这些是一次性操作，跟"唯一实例"没关系，允许多开
    if (args.gui or args.autostart or args.gen_config or args.check
            or args.fetch_cookie or args.open_login or args.test_captcha
            or args.detect_server):
        return main()

    if not acquire_single_instance_lock():
        msg = '本程序不可多开。\n若右下角仍有本程序通知，请关掉通知后重试！'
        show_warning('警告', msg, quiet)
        return 0

    try:
        return main()
    except KeyboardInterrupt:
        log('\n已取消。', quiet)
        return 130


if __name__ == '__main__':
    sys.exit(entry_point())
