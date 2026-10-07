#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图形设置窗口（tkinter，零第三方依赖）

双击「设置.bat」或运行 ``python -m src --gui`` 会打开这个窗口，可以：

* 直接改服务器地址、学号、密码、Cookie，不用手编 YAML；
* 测试连接（等价于 ``--check``），看配置和网络哪里不通；
* 一键立即联网 / 立即断网；
* 勾选开机自启（写当前用户注册表 Run 项，不需要管理员权限）。

保存时会**保留 config.yml 里的注释**和用户自己加的额外参数（见 config.write_cfg）。

所有联网操作都丢到后台线程执行，界面不会卡死。
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import tkinter as tk
from os.path import abspath, dirname, join
from tkinter import messagebox, ttk

try:  # 作为包运行
    from . import autostart
    from .config import ConfigError, read_cfg, resolve_config_path, write_cfg, write_template
    from .notifier import notify as send_toast
except ImportError:  # 直接跑脚本
    import autostart  # type: ignore
    from config import (  # type: ignore
        ConfigError, read_cfg, resolve_config_path, write_cfg, write_template,
    )
    from notifier import notify as send_toast  # type: ignore

import yaml

APP_TITLE = '锐捷 ePorta 连接工具 - 设置'
ICON_PATH = join(dirname(abspath(__file__)), 'wangluo.ico')


class SettingsWindow:
    def __init__(self, config_path: str | None = None) -> None:
        self.config_path = resolve_config_path(config_path)
        self.raw: dict = {}
        self.log_queue: queue.Queue[str] = queue.Queue()
        # 后台线程 → 主线程的"请弹验证码窗口"请求：(png字节, 结果字典, 完成事件)
        self.captcha_requests: queue.Queue = queue.Queue()
        self.busy = False

        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.minsize(660, 620)
        if os.path.exists(ICON_PATH):
            try:
                self.root.iconbitmap(ICON_PATH)
            except Exception:
                pass

        self._build_widgets()
        self.load_config(quiet=True)
        self.root.after(120, self._drain_log)
        self.root.protocol('WM_DELETE_WINDOW', self.on_close)

    # ------------------------------------------------------------------ #
    # 界面
    # ------------------------------------------------------------------ #
    def _build_widgets(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use('vista')
        except tk.TclError:
            pass

        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill='both', expand=True)

        # ---------- 账号 ----------
        account = ttk.LabelFrame(outer, text=' 账号信息 ', padding=10)
        account.pack(fill='x')
        account.columnconfigure(1, weight=1)

        self.var_server = tk.StringVar()
        self.var_user = tk.StringVar()
        self.var_password = tk.StringVar()
        self.var_encrypt = tk.BooleanVar()
        self.var_cookie = tk.StringVar()

        ttk.Label(account, text='认证服务器').grid(row=0, column=0, sticky='w', pady=3)
        ttk.Entry(account, textvariable=self.var_server).grid(
            row=0, column=1, sticky='ew', padx=(8, 0), pady=3)
        ttk.Label(account, text='如 http://10.0.0.1').grid(
            row=0, column=2, sticky='w', padx=(8, 0))
        ttk.Button(account, text='自动探测',
                   command=lambda: self.run_job('自动探测服务器', self._job_detect_server)).grid(
            row=0, column=3, sticky='w', padx=(8, 0), pady=3)

        ttk.Label(account, text='学号').grid(row=1, column=0, sticky='w', pady=3)
        self.entry_user = ttk.Entry(account, textvariable=self.var_user)
        self.entry_user.grid(row=1, column=1, sticky='ew', padx=(8, 0), pady=3)

        ttk.Label(account, text='密码').grid(row=2, column=0, sticky='w', pady=3)
        self.entry_password = ttk.Entry(account, textvariable=self.var_password, show='*')
        self.entry_password.grid(row=2, column=1, sticky='ew', padx=(8, 0), pady=3)
        self.var_show_pwd = tk.BooleanVar(value=False)
        ttk.Checkbutton(account, text='显示', variable=self.var_show_pwd,
                        command=self._toggle_password).grid(
            row=2, column=2, sticky='w', padx=(8, 0))

        ttk.Checkbutton(
            account, text='密码是抓包里的加密串（passwordEncrypt）',
            variable=self.var_encrypt,
        ).grid(row=3, column=1, columnspan=2, sticky='w', padx=(8, 0), pady=(0, 3))

        ttk.Label(account, text='Cookie').grid(row=4, column=0, sticky='w', pady=3)
        ttk.Entry(account, textvariable=self.var_cookie).grid(
            row=4, column=1, sticky='ew', padx=(8, 0), pady=3)
        ttk.Button(account, text='从服务器获取',
                   command=lambda: self.run_job('获取会话', self._job_fetch_cookie)).grid(
            row=4, column=2, sticky='w', padx=(8, 0), pady=3)

        # ---------- 高级 ----------
        advanced = ttk.LabelFrame(outer, text=' 接口（一般不用改） ', padding=10)
        advanced.pack(fill='x', pady=(10, 0))
        advanced.columnconfigure(1, weight=1)

        self.var_login = tk.StringVar()
        self.var_logout = tk.StringVar()
        self.var_referer = tk.StringVar()
        self.var_querystring = tk.StringVar()

        for row, (label, var) in enumerate((
            ('登录接口', self.var_login),
            ('下线接口', self.var_logout),
            ('queryString', self.var_querystring),
            ('Referer', self.var_referer),
        )):
            ttk.Label(advanced, text=label).grid(row=row, column=0, sticky='w', pady=3)
            ttk.Entry(advanced, textvariable=var).grid(
                row=row, column=1, sticky='ew', padx=(8, 0), pady=3)
        ttk.Label(advanced, text='queryString 一般是重定向地址里 ? 后面的内容，'
                                 '点上面的「从服务器获取」可自动填',
                  foreground='#666').grid(row=4, column=0, columnspan=2, sticky='w')

        # ---------- 行为 ----------
        behaviour = ttk.LabelFrame(outer, text=' 运行方式 ', padding=10)
        behaviour.pack(fill='x', pady=(10, 0))
        behaviour.columnconfigure(1, weight=1)

        self.var_school = tk.BooleanVar()
        self.var_ask_disconnect = tk.BooleanVar()
        self.var_timeout = tk.StringVar()
        self.var_interval = tk.StringVar()
        self.var_autostart = tk.BooleanVar()

        ttk.Checkbutton(behaviour, text='启动前先确认是否在校园网环境',
                        variable=self.var_school).grid(
            row=0, column=0, columnspan=3, sticky='w', pady=2)
        ttk.Checkbutton(behaviour, text='已联网时弹窗询问是否断网',
                        variable=self.var_ask_disconnect).grid(
            row=1, column=0, columnspan=3, sticky='w', pady=2)
        ttk.Checkbutton(behaviour, text='开机自动运行（联网后自动退出）',
                        variable=self.var_autostart).grid(
            row=2, column=0, columnspan=3, sticky='w', pady=2)

        retry = ttk.Frame(behaviour)
        retry.grid(row=3, column=0, columnspan=3, sticky='w', pady=2)
        ttk.Label(retry, text='最长尝试').pack(side='left')
        ttk.Entry(retry, textvariable=self.var_timeout, width=6).pack(side='left', padx=4)
        ttk.Label(retry, text='秒，每隔').pack(side='left')
        ttk.Entry(retry, textvariable=self.var_interval, width=6).pack(side='left', padx=4)
        ttk.Label(retry, text='秒重试一次（连上就结束；超时也结束）').pack(side='left')

        # ---------- 按钮 ----------
        buttons = ttk.Frame(outer)
        buttons.pack(fill='x', pady=(12, 0))
        self.buttons: list[ttk.Button] = []
        specs = (
            ('保存配置', self.on_save),
            ('测试连接', lambda: self.run_job('测试连接', self._job_check)),
            ('立即联网', lambda: self.run_job('联网', self._job_connect)),
            ('立即断网', lambda: self.run_job('断网', self._job_disconnect)),
            ('获取会话', lambda: self.run_job('获取会话', self._job_fetch_cookie)),
            ('验证码自检', lambda: self.run_job('验证码自检', self._job_test_captcha)),
            ('打开认证网页', lambda: self.run_job('打开认证网页', self._job_open_login)),
            ('重新载入', lambda: self.load_config()),
        )
        for text, command in specs:
            btn = ttk.Button(buttons, text=text, command=command)
            btn.pack(side='left', padx=(0, 6))
            self.buttons.append(btn)
        ttk.Button(buttons, text='关闭', command=self.on_close).pack(side='right')

        # ---------- 日志 ----------
        log_frame = ttk.LabelFrame(outer, text=' 运行记录 ', padding=6)
        log_frame.pack(fill='both', expand=True, pady=(10, 0))
        self.log_text = tk.Text(log_frame, height=10, wrap='word', state='disabled',
                               font=('Consolas', 9))
        scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.log_text.pack(fill='both', expand=True)

        self.status = tk.StringVar(value='')
        ttk.Label(outer, textvariable=self.status, foreground='#555').pack(
            fill='x', pady=(6, 0))

    def _toggle_password(self) -> None:
        self.entry_password.configure(show='' if self.var_show_pwd.get() else '*')

    def focus_account(self) -> None:
        """把光标放到学号框里：第一次用的人一眼就知道该填哪儿"""
        try:
            self.entry_user.focus_set()
        except Exception:
            pass

    def auto_detect_server(self, delay: int = 300) -> None:
        """先让窗口显示出来，再去后台探测认证服务器（探测最长要十几秒，不能卡启动）"""
        self.root.after(delay, lambda: self.run_job('自动探测服务器', self._job_detect_server))

    def _job_detect_server(self) -> None:
        """靠网关劫持时的 302 跳转反推认证服务器地址"""
        main_mod = self._import_main()
        self.log('正在自动探测认证服务器地址…')
        server = main_mod.detect_portal(timeout=3.0, quiet=True)
        if not server:
            self.log('  没探测到。可能当前已经联网（不需要认证），或者不在这张校园网里。')
            self.log('  手动找的办法：用浏览器打开任意一个 http 网站，'
                     '它自动跳到哪个地址，那个地址就是认证服务器。')
            return
        self.var_server.set(server)
        self.log(f'  探测到：{server}（已经填进上面的输入框）')
        self.status.set('已探测到服务器地址，记得点「保存配置」')

    # ------------------------------------------------------------------ #
    # 日志
    # ------------------------------------------------------------------ #
    def log(self, message: str = '') -> None:
        self.log_queue.put(message)

    def _write_log_file(self, message: str) -> None:
        """界面上显示的日志同样写进 eporta.log（出问题时只发这个文件就够了）"""
        try:
            self._import_main().log(message, quiet=True)
        except Exception:
            pass

    def _drain_log(self) -> None:
        try:
            while True:
                message = self.log_queue.get_nowait()
                self.log_text.configure(state='normal')
                self.log_text.insert('end', message + '\n')
                self.log_text.see('end')
                self.log_text.configure(state='disabled')
                self._write_log_file(message)
        except queue.Empty:
            pass
        # 顺手处理"后台线程想要验证码"的请求（**必须**在主线程建窗口：
        # 从别的线程调 root.after 会抛 RuntimeError: main thread is not in main loop）
        self._poll_captcha_request()
        self.root.after(120, self._drain_log)

    def _poll_captcha_request(self) -> None:
        try:
            request = self.captcha_requests.get_nowait()
        except queue.Empty:
            return
        image, box, done = request
        try:
            box['code'] = self._captcha_dialog(image)
        except Exception as e:  # 弹窗失败也不能把后台线程卡死
            self.log(f'[错误] 验证码窗口打不开：{e}')
            box['code'] = ''
        finally:
            done.set()

    def _set_busy(self, busy: bool, text: str = '') -> None:
        self.busy = busy
        state = 'disabled' if busy else 'normal'
        for btn in self.buttons:
            btn.configure(state=state)
        self.status.set(text)

    # ------------------------------------------------------------------ #
    # 读写配置
    # ------------------------------------------------------------------ #
    def _load_raw(self) -> dict:
        if not os.path.exists(self.config_path):
            write_template(self.config_path)
            self.log(f'配置文件不存在，已生成模板：{self.config_path}')
        try:
            with open(self.config_path, 'r', encoding='utf-8') as fp:
                data = yaml.safe_load(fp)
        except Exception as e:
            messagebox.showerror(APP_TITLE, f'读取配置失败：\n{e}')
            data = None
        if not isinstance(data, dict):
            # 内容为空或坏掉：给一份最小可编辑的结构，保存时会重新写全
            data = {
                'main': {'version': 3},
                'url': {
                    'server': '',
                    'login': '/eportal/InterFace.do?method=login',
                    'logout': '/eportal/InterFace.do?method=logout',
                },
                'login_data': {},
            }
        return data

    def load_config(self, quiet: bool = False) -> None:
        self.raw = self._load_raw()
        url = self.raw.get('url') or {}
        funtion = self.raw.get('funtion') or {}
        login_data = self.raw.get('login_data') or {}
        auto = self.raw.get('auto') or {}
        headers = self.raw.get('headers') or {}

        self.var_server.set(str(url.get('server') or ''))
        self.var_login.set(str(url.get('login') or '/eportal/InterFace.do?method=login'))
        self.var_logout.set(str(url.get('logout') or '/eportal/InterFace.do?method=logout'))
        self.var_user.set(str(login_data.get('userId') or ''))
        password = login_data.get('password')
        self.var_password.set('' if password is None else str(password))
        self.var_encrypt.set(
            str(login_data.get('passwordEncrypt', False)).lower() in ('true', '1', 'yes')
        )
        self.var_cookie.set(str(self.raw.get('cookie') or ''))
        qs = login_data.get('queryString')
        self.var_querystring.set('' if qs is None else str(qs))
        self.var_referer.set(str(headers.get('Referer') or ''))
        self.var_school.set(
            str(funtion.get('check_school_network', True)).lower() in ('true', '1', 'yes')
        )
        self.var_ask_disconnect.set(
            str(funtion.get('disconnect_network', True)).lower() in ('true', '1', 'yes')
        )
        self.var_timeout.set(str(auto.get('retry_timeout', 300)))
        self.var_interval.set(str(auto.get('retry_interval', 10)))
        self.var_autostart.set(autostart.is_enabled())

        self.status.set(f'配置文件：{self.config_path}')
        if not quiet:
            self.log(f'已重新载入：{self.config_path}')

    def collect(self) -> dict:
        """把界面上的值合并进原始配置（保留用户自己加的额外参数）"""
        cfg = self.raw if isinstance(self.raw, dict) else {}
        cfg.setdefault('main', {'version': 3})
        cfg['main']['version'] = 3

        url = cfg.setdefault('url', {})
        url['server'] = self.var_server.get().strip()
        url['login'] = self.var_login.get().strip()
        url['logout'] = self.var_logout.get().strip()

        funtion = cfg.setdefault('funtion', {})
        funtion['check_school_network'] = bool(self.var_school.get())
        funtion['disconnect_network'] = bool(self.var_ask_disconnect.get())

        login_data = cfg.setdefault('login_data', {})
        login_data['userId'] = self.var_user.get().strip()
        login_data['password'] = self.var_password.get()
        login_data['passwordEncrypt'] = bool(self.var_encrypt.get())
        querystring = self.var_querystring.get().strip()
        if querystring:
            login_data['queryString'] = querystring
        else:
            login_data.pop('queryString', None)

        cfg['cookie'] = self.var_cookie.get().strip()

        headers = cfg.setdefault('headers', {})
        referer = self.var_referer.get().strip()
        if referer:
            headers['Referer'] = referer
        else:
            headers.pop('Referer', None)
        if not headers:
            cfg.pop('headers', None)

        auto = cfg.setdefault('auto', {})
        auto['retry_timeout'] = self._positive_int(self.var_timeout.get(), 300)
        auto['retry_interval'] = self._positive_int(self.var_interval.get(), 10)
        return cfg

    @staticmethod
    def _positive_int(text: str, default: int) -> int:
        try:
            value = int(str(text).strip())
        except (TypeError, ValueError):
            return default
        return value if value > 0 else default

    # ------------------------------------------------------------------ #
    # 动作
    # ------------------------------------------------------------------ #
    def on_save(self) -> bool:
        cfg = self.collect()
        server = str(cfg['url'].get('server') or '')
        if not server:
            messagebox.showwarning(APP_TITLE, '请先填认证服务器地址，例如 http://10.0.0.1')
            return False
        if not server.startswith(('http://', 'https://')):
            server = 'http://' + server
            cfg['url']['server'] = server
            self.var_server.set(server)
        try:
            write_cfg(cfg, self.config_path)
        except OSError as e:
            messagebox.showerror(APP_TITLE, f'保存失败：\n{e}')
            return False

        # 开机自启跟随勾选框
        want = bool(self.var_autostart.get())
        if want and not autostart.is_enabled():
            ok, info = autostart.enable()
            self.log(f'开启开机自启：{"成功" if ok else "失败"} - {info}')
            if not ok:
                messagebox.showwarning(APP_TITLE, f'开机自启设置失败：\n{info}')
                self.var_autostart.set(False)
        elif not want and autostart.is_enabled():
            ok, info = autostart.disable()
            self.log(f'关闭开机自启：{"成功" if ok else "失败"} - {info}')
            if not ok:
                messagebox.showwarning(APP_TITLE, f'关闭开机自启失败：\n{info}')
                self.var_autostart.set(True)

        self.log(f'已保存配置：{self.config_path}')
        self.status.set('已保存')
        messagebox.showinfo(APP_TITLE, f'已保存到\n{self.config_path}')
        return True

    def run_job(self, name: str, job) -> None:
        if self.busy:
            return
        self._set_busy(True, f'正在{name}…')
        threading.Thread(target=self._worker, args=(name, job), daemon=True).start()

    def _worker(self, name: str, job) -> None:
        try:
            job()
        except Exception as e:  # 后台线程里出错也要让用户看到
            self.log(f'[错误] {type(e).__name__}: {e}')
        finally:
            self.root.after(0, lambda: self._set_busy(False, '就绪'))

    def _validate(self) -> bool:
        """保存后按 read_cfg 的规则校验一遍"""
        try:
            self.cfg = read_cfg(self.config_path)
            return True
        except ConfigError as e:
            self.log(f'[配置错误] {e}')
            messagebox.showwarning(APP_TITLE, f'配置还没填对：\n\n{e}')
            return False

    def _job_check(self) -> None:
        # 先把界面上的值落盘，再按落盘结果体检
        if not self.on_save_silent():
            return
        main_mod = self._import_main()
        cfg = self.cfg
        self.log('--- 配置 ---')
        self.log(f'  配置文件  : {self.config_path}')
        self.log(f'  认证服务器: {cfg["url"]["server"]}')
        self.log(f'  学号      : {cfg["login_data"].get("userId", "")}')
        self.log(f'  密码已填  : {"是" if str(cfg["login_data"].get("password", "")) else "否"}')
        self.log(f'  密码类型  : {"加密串" if str(cfg["login_data"].get("passwordEncrypt")).lower() == "true" else "明文"}')
        self.log(f'  Cookie    : {"已填" if cfg.get("cookie") else "未填（部分学校需要）"}')
        self.log(f'  额外参数  : {", ".join(k for k in (cfg.get("login_data") or {}) if k not in ("userId", "password", "passwordEncrypt")) or "无"}')
        self.log('--- 连通性 ---')
        school_ok = main_mod.test_internet(cfg['url']['server'], timeout=3)
        self.log(f'  认证服务器: {"可达" if school_ok else "不可达"}')
        online = main_mod.test_internet(timeout=3)
        self.log(f'  外网      : {"已联网" if online else "未联网"}')
        self.log('--- 系统通知 ---')
        self.log(f'  通知可用  : {"是" if main_mod.toast_available() else "否，会回退成弹窗"}')
        self.log('体检完成。')
        if not school_ok:
            self.log('提示：认证服务器不可达，通常说明当前不在校园网内，或者服务器地址填错了。')

    def _job_fetch_cookie(self) -> None:
        """访问认证服务器，自动填 Cookie 与 queryString"""
        try:
            from . import session as session_mod
        except ImportError:
            import session as session_mod  # type: ignore

        server = self.var_server.get().strip()
        if not server:
            self.log('[错误] 请先填认证服务器地址，例如 http://10.0.0.1')
            return
        self.log(f'正在访问 {server} 获取会话信息…')
        info = session_mod.fetch_session(server)
        for line in session_mod.describe(info).splitlines():
            self.log('  ' + line)
        got = False
        if info['cookie']:
            self.var_cookie.set(info['cookie'])
            got = True
        if info['queryString']:
            self.var_querystring.set(info['queryString'])
            got = True
        if info.get('online'):
            self.log('当前已经在线，只需要在重新联网前先断网再点一次「获取会话」。')
            return
        if got:
            self.log('已填入界面，记得点「保存配置」写入文件。')
        else:
            self.log('没能取到会话信息。当前不在校园网时这是正常的，'
                     'Cookie 也可以留空直接登录。')

    def _job_open_login(self) -> None:
        """用默认浏览器打开认证网页，手动输账号密码（自动登录失败时的兜底）"""
        main_mod = self._import_main()
        server = self.var_server.get().strip()
        if not server:
            self.log('[错误] 请先填认证服务器地址，例如 http://10.0.0.1')
            return
        self.log(f'正在用默认浏览器打开认证网页：{server}')
        self.log(f'  在网页里输入学号 {self.var_user.get().strip()} 和密码即可登录。')
        self.log('  说明：网页里的 JS 会自己带上服务器下发的加密设备参数，'
                 '所以手动登录一定能成功（自动登录失败时就走这条路）。')
        if not main_mod.open_login_page(server):
            self.log('[错误] 打不开浏览器，请手动复制上面的地址到浏览器里打开。')

    def _job_test_captcha(self) -> None:
        """验证码自检：取一张验证码图、自动识别、把图存下来给人核对（不登录）"""
        main_mod = self._import_main()
        server = self.var_server.get().strip()
        if not server:
            self.log('[错误] 请先填认证服务器地址，例如 http://10.0.0.1')
            return
        self.log('正在取一张验证码图片做自动识别…')
        rc = main_mod.run_test_captcha({'url': {'server': server}}, quiet=True)
        if rc == 0:
            self.log('  识别成功。图也存成了 validcode_last.png（和 eporta.log 在同一个目录），'
                     '可以直接打开对照一眼。')
        else:
            self.log('  识别失败。这不影响使用：真正登录时识别不出来会弹窗让你自己输。')

    def _captcha_prompt(self, image: bytes) -> str:
        """在**主线程**弹出验证码输入框，等用户输完再返回（do_connect 在后台线程里调它）

        不能直接 ``root.after``：那是从非主线程碰 Tcl，会抛
        ``RuntimeError: main thread is not in main loop``。
        所以走队列，交给主线程的 ``_drain_log`` 轮询处理（它每 120ms 跑一次）。
        """
        box = {'code': ''}
        done = threading.Event()
        self.captcha_requests.put((image, box, done))
        # 最多等 5 分钟（跟重试总时长一致），期间用户想输多久都行
        if not done.wait(timeout=300):
            self.log('[提示] 等验证码超时了，本次登录放弃。')
            return ''
        return box['code']

    def _captcha_dialog(self, image: bytes) -> str:
        """真正建窗口的代码，只在主线程跑"""
        from tkinter import Button, Entry, Label, StringVar, Toplevel

        result = {'code': ''}
        main_mod = self._import_main()
        win = Toplevel(self.root)
        win.title('请输入验证码')
        win.resizable(False, False)
        win.attributes('-topmost', True)
        Label(win, text='校园网认证需要验证码，请输入图片里的字符：').pack(padx=16, pady=(12, 4))
        photo = None
        try:
            photo = main_mod.tk_photoimage(self.root, image)
        except Exception as e:
            self.log(f'[提示] 验证码图片显示不出来（{e}），请到浏览器里手动登录。')
        if photo is not None:
            lbl = Label(win, image=photo)
            lbl.image = photo  # 防止被 GC 回收
            lbl.pack(padx=16, pady=4)
        var = StringVar()
        entry = Entry(win, textvariable=var, width=14, justify='center',
                      font=('Consolas', 16))
        entry.pack(padx=16, pady=6)
        entry.focus_set()

        def ok(_event=None):
            result['code'] = var.get().strip()
            win.destroy()

        def cancel():
            result['code'] = ''
            win.destroy()

        row = tk.Frame(win)
        row.pack(pady=(0, 12))
        Button(row, text='确定', width=10, command=ok).pack(side='left', padx=6)
        Button(row, text='取消', width=10, command=cancel).pack(side='left', padx=6)
        win.bind('<Return>', ok)
        win.bind('<Escape>', lambda _e: cancel())
        try:
            main_mod.center_window(win)
            win.grab_set()
        except Exception:
            pass
        self.root.wait_window(win)
        return result['code']

    def _job_connect(self) -> None:
        if not self.on_save_silent():
            return
        main_mod = self._import_main()
        self.log('开始登录…')
        old_qs = (self.cfg.get('login_data') or {}).get('queryString') or ''
        ok = main_mod.do_connect(self.cfg, main_mod.build_headers(self.cfg), quiet=True,
                                 captcha_prompt=self._captcha_prompt)
        new_qs = (self.cfg.get('login_data') or {}).get('queryString') or ''
        if new_qs and new_qs != old_qs:
            # 自动抓到的参数同步回界面并落盘，下次直接用，不必再抓一次
            self.var_querystring.set(new_qs)
            self.var_cookie.set(self.cfg.get('cookie') or '')
            if self.on_save_silent():
                self.log('已把本次抓到的会话参数写进 config.yml。')
        self.log('登录成功。' if ok else '登录失败，看上面的提示。')

    def _job_disconnect(self) -> None:
        if not self.on_save_silent():
            return
        main_mod = self._import_main()
        self.log('开始断网…')
        ok = main_mod.do_disconnect(self.cfg, main_mod.build_headers(self.cfg), quiet=True)
        self.log('断网成功。' if ok else '断网失败，看上面的提示。')

    def on_save_silent(self) -> bool:
        try:
            write_cfg(self.collect(), self.config_path)
        except OSError as e:
            self.log(f'[错误] 保存失败：{e}')
            return False
        return self._validate()

    def _import_main(self):
        """拿到 `__main__.py` 那个模块（里面是真正干活的联网逻辑）"""
        if 'eporta_main' in sys.modules:
            return sys.modules['eporta_main']

        # 打包成 exe 之后磁盘上没有 .py 源文件可读（都进了 exe），
        # 而"主模块"本来就是本进程的 __main__，直接借用它即可。
        if getattr(sys, 'frozen', False):
            bundled = sys.modules.get('__main__')
            if bundled is not None and hasattr(bundled, 'do_connect'):
                sys.modules['eporta_main'] = bundled
                return bundled

        import importlib.util

        src_dir = dirname(abspath(__file__))
        # __main__.py 被当成顶层模块加载，里面的 `from config import ...`
        # 需要 src 目录在 sys.path 上
        if src_dir not in sys.path:
            sys.path.insert(0, src_dir)
        path = join(src_dir, '__main__.py')
        spec = importlib.util.spec_from_file_location('eporta_main', path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules['eporta_main'] = mod
        spec.loader.exec_module(mod)
        return mod

    def on_close(self) -> None:
        if self.busy and not messagebox.askyesno(APP_TITLE, '还有操作在进行，确定要关闭吗？'):
            return
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main(config_path: str | None = None, first_run: str = '') -> int:
    if os.name != 'nt':
        # 非 Windows 也允许开界面，只是通知和自启不可用
        pass
    try:
        window = SettingsWindow(config_path)
    except tk.TclError as e:
        print(f'无法创建图形界面：{e}', file=sys.stderr)
        return 1
    # 图形界面下默认记详细日志：出问题时让你直接把 eporta.log 发出来就行
    try:
        window._import_main().set_debug(True)
        window.log(f'详细日志已开启，日志文件：{window._import_main().log_path()}')
    except Exception as e:  # noqa: BLE001 - 记不了详细日志也得让人能用界面
        window.log(f'（详细日志没开起来：{e}）')
    if first_run:
        # 下载下来直接双击的情况：把"接下来该干什么"直接写在窗口里，别让人猜
        window.log('')
        window.log('=' * 46)
        window.log(first_run)
        window.log('=' * 46)
        window.log('')
        window.status.set('第一次使用：填好学号和密码 → 保存配置')
        try:
            window.focus_account()
            window.auto_detect_server()
        except Exception as e:  # noqa: BLE001 - 探测失败不能挡着人填配置
            window.log(f'（自动探测认证服务器失败：{e}）')
    window.run()
    return 0


if __name__ == '__main__':
    sys.exit(main())
