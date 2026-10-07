#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内置模拟认证服务器测试 —— 不用真校园网也能验证整条链路

跑法（在项目根目录）：

    .venv\\Scripts\\python.exe tests\\test_mock_eportal.py

它会在本机起一个假的锐捷 ePortal，然后让**真实代码**去连它，覆盖这些行为：

  A) 配置里没有设备参数（queryString 为空）→ 自动抓参数 + 自动过验证码 + 登录成功
  B) 设备参数过期（换了网络）→ 服务器回「设备未注册」→ 同一次调用里自愈重抓后成功
  C) 已经联网 → 重试模式立刻收工，不发多余请求
  D) 断网 → 用 logout_data，且空值字段照样发（跟登录一个道理）
  E) 未联网时要求断网 → 直接跳过，不发请求
  F) 一直连不上 → 到点退出（返回 1），不会永远重试

验证码图片是**用 src/captcha.py 自己的模板现画出来的**（纯标准库写 PNG），
所以这个测试同时验证了"取图 → 解码 → 切字 → 识别 → 带着识别结果登录"的闭环。
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'
sys.path.insert(0, str(SRC))

import captcha  # noqa: E402

spec = importlib.util.spec_from_file_location('eporta_main', str(SRC / '__main__.py'))
main_mod = importlib.util.module_from_spec(spec)
sys.modules['eporta_main'] = main_mod
spec.loader.exec_module(main_mod)

# 日志别写进项目目录，扔到临时目录里
main_mod._log_file['path'] = os.path.join(tempfile.gettempdir(), 'eporta_mock_test.log')

# --------------------------------------------------------------------------
# 用 src/captcha.py 的模板"画"一张验证码（纯标准库 PNG 编码，零依赖）
# --------------------------------------------------------------------------

_CODES = ['2469', '5381', '9072', '1468']
_BG = (255, 255, 255)
_FG = (230, 145, 30)  # 锐捷验证码那种橙黄色
_BROKEN_PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 40  # 有 PNG 头但内容坏，解码必失败


def _row_text(row) -> str:
    """位图的一行 → '#' / '.' 字符串（_segment 返回的是布尔元组，模板是字符串）"""
    if isinstance(row, str):
        return row
    return ''.join('#' if cell else '.' for cell in row)


def _png(width: int, height: int, pixels) -> bytes:
    """极简 PNG 编码：8bit 真彩、无滤波"""
    raw = b''.join(b'\x00' + bytes(v for px in row for v in px) for row in pixels)

    def chunk(tag: bytes, data: bytes) -> bytes:
        payload = tag + data
        return (struct.pack('>I', len(data)) + payload
                + struct.pack('>I', zlib.crc32(payload) & 0xFFFFFFFF))

    ihdr = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr)
            + chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b''))


def render_captcha(code: str) -> bytes:
    """按给定数字画一张验证码图片（字形直接取自 captcha 模块的模板）"""
    glyphs = dict(captcha._templates())
    arts = [_row_text(row) for row in glyphs[code[0]]]
    height = len(arts)
    width = 2
    for ch in code:
        width += len(_row_text(glyphs[ch][0])) + 2
    pixels = [[_BG for _ in range(width)] for _ in range(height)]
    x = 2
    for ch in code:
        rows = [_row_text(row) for row in glyphs[ch]]
        for y, line in enumerate(rows):
            for dx, cell in enumerate(line):
                if cell == '#':
                    pixels[y][x + dx] = _FG
        x += len(rows[0]) + 2
    return _png(width, height, pixels)


# --------------------------------------------------------------------------
# 假认证服务器
# --------------------------------------------------------------------------

PORT = 0
STATE = {
    'params': 'wlanuserip=AAAAAAAA&nasip=BBBBBBBB&t=wireless-v2&url=CCCCCCCC',
    'online': False,
    'root_hits': 0,
    'index_hits': 0,
    'captcha_hits': 0,
    'broken_until': 0,
    'code': '',
    'captcha_cookie': '',
    'login_bodies': [],
    'login_cookies': [],
    'logout_bodies': [],
    'strict_cookie': True,
    'errors': [],
    'notices': [],
    'logs': [],
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):  # 别把访问日志打到测试输出里
        pass

    def _send(self, status, body=b'', cookie=None, ctype='text/html; charset=utf-8'):
        self.send_response(status)
        if cookie:
            self.send_header('Set-Cookie', cookie)
        if status in (301, 302):
            self.send_header('Location', body.decode())
            body = b''
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = self.path.split('?')[0]
        if path == '/':
            STATE['root_hits'] += 1
            if STATE['online']:
                # 已认证：真机上是 302 到 success.jsp，这里保持同样的形式
                self._send(302, b'/eportal/success.jsp?userIndex=deadbeef')
                return
            # 未认证：真机上是 HTTP 200 + JS 跳转（不是 302！）
            target = f'http://127.0.0.1:{PORT}/eportal/index.jsp?{STATE["params"]}'
            body = ("<html><script>top.self.location.href='%s';</script></html>"
                    % target).encode('utf-8')
            self._send(200, body)
            return
        if path == '/eportal/index.jsp':
            STATE['index_hits'] += 1
            self._send(200, '<html><body>校园网登录页</body></html>'.encode('utf-8'))
            return
        if path in ('/eportal/validcode', '/eportal/validCode'):
            STATE['captcha_hits'] += 1
            STATE['code'] = _CODES[(STATE['captcha_hits'] - 1) % len(_CODES)]
            STATE['captcha_cookie'] = f'JSESSIONID=CAPTCHA{STATE["captcha_hits"]}'
            body = (_BROKEN_PNG if STATE['captcha_hits'] <= STATE['broken_until']
                    else render_captcha(STATE['code']))
            self._send(200, body, cookie=STATE['captcha_cookie'] + '; Path=/eportal',
                       ctype='image/png')
            return
        self._send(404, b'not found', ctype='text/plain')

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(length).decode('utf-8')
        form = parse_qs(raw, keep_blank_values=True)
        full = self.path                      # 形如 /eportal/InterFace.do?method=login
        path = full.split('?')[0]
        method = parse_qs(full.split('?', 1)[1] if '?' in full else '').get('method', [''])[0]
        if method == 'logout' or path.endswith('/logout'):
            STATE['logout_bodies'].append(raw)
            body = json.dumps({'result': 'success', 'message': '下线成功！'},
                              ensure_ascii=False).encode('utf-8')
            self._send(200, body, ctype='application/json; charset=utf-8')
            return
        if method != 'login':
            self._send(404, b'not found', ctype='text/plain')
            return

        STATE['login_bodies'].append(raw)
        STATE['login_cookies'].append(self.headers.get('Cookie') or '')
        code = form.get('validcode', [''])[0]
        query = form.get('queryString', [''])[0]
        cookie = self.headers.get('Cookie') or ''
        if STATE['strict_cookie'] and cookie != STATE['captcha_cookie']:
            message = '验证码错误.'
        elif code != STATE['code']:
            message = '验证码错误.'
        elif query != STATE['params']:
            message = 'WEB认证设备未注册，请确认SAM+/portal/设备上的参数配置是否一致'
        else:
            STATE['online'] = True
            message = '登录成功'
        payload = {'result': 'fail' if message != '登录成功' else 'success',
                   'message': message, 'userIndex': None, 'validCodeUrl': ''}
        self._send(200, json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                   ctype='application/json; charset=utf-8')


server = HTTPServer(('127.0.0.1', 0), Handler)
PORT = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()

# 通知/弹窗全部拦截，顺便当断言素材
main_mod.notify = lambda title, msg, use_toast=True: STATE['notices'].append((title, msg))
main_mod.show_error = lambda title, msg, quiet=False, silent=False: STATE['errors'].append(msg)
main_mod.show_warning = lambda title, msg, quiet=False, silent=False: STATE['errors'].append(msg)

failures: list[str] = []


def check(label, ok, extra=''):
    print(('  OK   ' if ok else '  FAIL ') + label + (f'   {extra}' if extra else ''))
    if not ok:
        failures.append(label)


def reset():
    STATE.update({'online': False, 'root_hits': 0, 'index_hits': 0, 'captcha_hits': 0,
                  'broken_until': 0, 'code': '', 'captcha_cookie': '', 'login_bodies': [],
                  'login_cookies': [], 'logout_bodies': [], 'errors': [], 'notices': [],
                  'strict_cookie': True})
    main_mod._session_probe['last'] = 0.0
    main_mod._last_fail['reason'] = ''


def login_body(index=-1):
    """取第 index 个登录请求体；没有就返回空串（免得断言直接抛 IndexError）"""
    bodies = STATE['login_bodies']
    if not bodies:
        return ''
    try:
        return bodies[index]
    except IndexError:
        return ''


def make_cfg(query='', cookie='JSESSIONID=OLDONE'):
    return {
        'main': {'version': 3},
        'funtion': {'check_school_network': True, 'disconnect_network': False},
        'url': {'server': f'http://127.0.0.1:{PORT}',
                'login': '/eportal/InterFace.do?method=login',
                'logout': '/eportal/InterFace.do?method=logout'},
        'cookie': cookie,
        'login_data': {'userId': '00000000000', 'password': 'secret',
                       'service': '', 'queryString': query, 'operatorPwd': '',
                       'operatorUserId': '', 'validcode': '', 'passwordEncrypt': False},
        'logout_data': {'userId': '00000000000', 'service': ''},
        'headers': {'Referer': f'http://127.0.0.1:{PORT}/'},
        'auto': {'retry_timeout': 300, 'retry_interval': 10},
    }


print('--- 0) 模板渲染出来的验证码，识别器能原样认回来（OCR 闭环基线）---')
_baseline = []
for _code in _CODES:
    _got, _detail = captcha.recognize(render_captcha(_code))
    _baseline.append(_got == _code)
check(f'4 张自制验证码全部识别正确（{"".join(_CODES)}）', all(_baseline), f'{_baseline}')

print('--- A) 配置里没有设备参数：自动抓参数 + 自动过验证码 → 登录成功 ---')
reset()
cfg = make_cfg()
headers = main_mod.build_headers(cfg)
ok = main_mod.do_connect(cfg, headers, silent=True)
check('do_connect 返回 True（全程无人参与）', ok is True)
check('自动抓到了设备参数', 'wlanuserip=' in str(cfg['login_data'].get('queryString', '')),
      str(cfg['login_data'].get('queryString'))[:60])
check('取了 1 张验证码', STATE['captcha_hits'] == 1, f'{STATE["captcha_hits"]} 张')
check('先不带验证码试了一次，最后带的是识别结果',
      len(STATE['login_bodies']) >= 2 and 'validcode=&' in login_body(0)
      and f'validcode={STATE["code"]}' in login_body(),
      str([b.split('validcode=')[1][:4] for b in STATE['login_bodies']]))
check('登录请求带了取图那一轮的 cookie（验证码和 JSESSIONID 绑定）',
      STATE['login_cookies'] and STATE['login_cookies'][-1] == 'JSESSIONID=CAPTCHA1',
      repr(STATE['login_cookies'][-1] if STATE['login_cookies'] else ''))
_EIGHT = ['userId=', 'password=', 'service=', 'queryString=', 'operatorPwd=',
          'operatorUserId=', 'validcode=', 'passwordEncrypt=']
_missing = [key for key in _EIGHT if key not in login_body()]
check('表单八个字段一个不少（含空值字段）', not _missing,
      f'缺 {_missing}  /  {login_body()[-150:]}')
check('发出了「联网成功」通知', any(t == '联网成功' for t, _ in STATE['notices']))
check('没有弹错误框', not STATE['errors'], str(STATE['errors'])[:120])

print('--- B) 设备参数过期（换了网络）→ 自愈重抓后成功 ---')
reset()
STATE['params'] = 'wlanuserip=NEWNEWNEW&nasip=NEWNEWNEW&t=wireless-v2'
cfg = make_cfg(query='wlanuserip=OLDOLDOLD&nasip=OLDOLDOLD&t=wireless-v2')
headers = main_mod.build_headers(cfg)
ok = main_mod.do_connect(cfg, headers, silent=True)
check('do_connect 返回 True（自己重抓了参数）', ok is True)
check('重抓后用的是新参数', 'NEWNEWNEW' in str(cfg['login_data'].get('queryString', '')),
      str(cfg['login_data'].get('queryString'))[:60])
check('自愈时重抓了 1 次会话（配置里的旧参数不重抓）', STATE['root_hits'] == 1,
      f'{STATE["root_hits"]} 次')
check('没有弹错误框', not STATE['errors'], str(STATE['errors'])[:80])
reset()

print('--- C) 已经联网：重试模式立刻收工，不发登录请求 ---')
main_mod.test_internet = lambda **kw: True
cfg = make_cfg()
headers = main_mod.build_headers(cfg)
rc = main_mod.run_with_retry(cfg, headers, timeout=3, interval=0.5)
check('返回 0', rc == 0, f'rc={rc}')
check('一次登录请求都没发', not STATE['login_bodies'], str(STATE['login_bodies'])[:60])

print('--- D) 断网：用 logout_data，空值字段照样发 ---')
reset()
STATE['online'] = True
cfg = make_cfg()
headers = main_mod.build_headers(cfg)
ok = main_mod.do_disconnect(cfg, headers, silent=True)
check('do_disconnect 返回 True', ok is True)
check('断网体是 logout_data（含空值字段 service=）',
      STATE['logout_bodies'] and 'service=' in STATE['logout_bodies'][-1]
      and 'userId=00000000000' in STATE['logout_bodies'][-1],
      str(STATE['logout_bodies'])[:80])

print('--- E) 未联网时要求断网：直接跳过，不发请求 ---')
main_mod.test_internet = lambda **kw: False
STATE['logout_bodies'] = []
rc = main_mod.run_with_retry(cfg, headers, timeout=3, interval=0.5, disconnect=True)
check('返回 0（无需断网）', rc == 0, f'rc={rc}')
check('没有发断网请求', not STATE['logout_bodies'], str(STATE['logout_bodies'])[:60])

print('--- F) 一直连不上：到点退出，不会永远重试 ---')
STATE['online'] = False
main_mod.test_internet = lambda **kw: False
cfg = make_cfg()
cfg['url']['server'] = 'http://127.0.0.1:9'  # 打不通的端口
headers = main_mod.build_headers(cfg)
started = time.monotonic()
rc = main_mod.run_with_retry(cfg, headers, timeout=1.5, interval=0.4)
elapsed = time.monotonic() - started
check('返回 1（超时退出）', rc == 1, f'rc={rc}')
check('确实等到了时限才退出', elapsed >= 1.4, f'{elapsed:.2f} 秒')
check('发了「失败」通知', any('失败' in t for t, _ in STATE['notices']),
      str([t for t, _ in STATE['notices']]))

print('--- G) 验证码图片坏掉：换一张再认，一次失手不放弃 ---')
reset()
STATE['broken_until'] = 1
cfg = make_cfg()
headers = main_mod.build_headers(cfg)
ok = main_mod.do_connect(cfg, headers, silent=True)
check('do_connect 返回 True（第二张认出来了）', ok is True)
check('一共取了 2 张验证码', STATE['captcha_hits'] == 2, f'{STATE["captcha_hits"]} 张')
check('最后带着第二张的验证码登录', f'validcode={STATE["code"]}' in login_body(),
      str(STATE['code']))

print('--- H) 验证码一直认不出来：试满次数后放弃，并标记原因 ---')
reset()
STATE['broken_until'] = 99
cfg = make_cfg()
headers = main_mod.build_headers(cfg)
ok = main_mod.do_connect(cfg, headers, silent=True)
check('do_connect 返回 False', ok is False)
check(f'取图次数 = {main_mod._MAX_CAPTCHA_TRIES}', STATE['captcha_hits'] == main_mod._MAX_CAPTCHA_TRIES,
      f'{STATE["captcha_hits"]} 张')
check('标记失败原因是验证码（重试循环据此提前收工）',
      main_mod._last_fail['reason'] == 'captcha', repr(main_mod._last_fail['reason']))
check('报错里说明是自动识别没认出来',
      any('自动识别' in e for e in STATE['errors']), str(STATE['errors'])[:100])

print('--- I) 自动探测认证服务器：抓住网关 302 跳转的 Location ---')
# 假服务器把自己扮演成"劫持了外网请求的网关"：任何请求都 302 到 portal
class _Hijack(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(302)
        self.send_header('Location', f'http://127.0.0.1:{PORT}/eportal/index.jsp?wlanuserip=x')
        self.end_headers()

    def log_message(self, *a):
        pass


hijack = HTTPServer(('127.0.0.1', 0), _Hijack)
HIJACK_PORT = hijack.server_address[1]
threading.Thread(target=hijack.serve_forever, daemon=True).start()
_old_probes = main_mod.PORTAL_PROBES
main_mod.PORTAL_PROBES = (f'http://127.0.0.1:{HIJACK_PORT}/generate_204',)
found = main_mod.detect_portal(timeout=2.0, quiet=True)
# 网关把我们 302 到了 portal 的地址，探测结果应当是那个地址（不带路径）
check('探测到认证服务器地址', found == f'http://127.0.0.1:{PORT}', repr(found))
# 探测不出来时（没有任何跳转）应当老实返回空串，而不是瞎猜
main_mod.PORTAL_PROBES = (f'http://127.0.0.1:{PORT}/generate_204',)
STATE['online'] = True
check('没被劫持时返回空串', main_mod.detect_portal(timeout=2.0, quiet=True) == '')
main_mod.PORTAL_PROBES = _old_probes
hijack.shutdown()

print('--- J) 第一次运行（没有配置文件）：自动建模板并直接开设置窗口 ---')
fresh = tempfile.mkdtemp(prefix='eporta_first_run_')
fresh_cfg = os.path.join(fresh, 'config.yml')
opened = {}


class _FakeGui:
    @staticmethod
    def main(config_path=None, first_run=''):
        opened['path'] = config_path
        opened['hint'] = first_run
        return 0


real_gui = sys.modules.get('gui')
sys.modules['gui'] = _FakeGui  # type: ignore[assignment]
try:
    rc = main_mod.main(['--config', fresh_cfg])
finally:
    if real_gui is None:
        sys.modules.pop('gui', None)
    else:
        sys.modules['gui'] = real_gui
check('返回 0 而不是报错', rc == 0, f'rc={rc}')
check('自动生成了配置文件', os.path.isfile(fresh_cfg), fresh_cfg)
check('开的是设置窗口（并带上了第一次使用的提示）',
      opened.get('path') == fresh_cfg and '第一次使用' in str(opened.get('hint')), str(opened)[:120])
with open(fresh_cfg, encoding='utf-8') as fp:
    first_text = fp.read()
check('模板里没有真实服务器地址', '172.16' not in first_text)
# 明确知道自己要干什么时（比如 --connect），不该弹窗口
opened.clear()
rc = main_mod.main(['--config', fresh_cfg, '--connect', '--console'])
check('带 --connect 时不弹设置窗口', not opened, str(opened)[:60])
shutil.rmtree(fresh, ignore_errors=True)

print('--- K) 打包成 exe 后（没有 .py 源文件）界面也能找到干活的后端 ---')
sys.modules.pop('gui', None)  # 上面 J 组塞进去的是假货，这里要真的 src/gui.py
import gui as gui_mod  # noqa: E402  （src 已经在 sys.path 上）

saved_main = sys.modules.pop('eporta_main', None)
fake_inst = object.__new__(gui_mod.SettingsWindow)  # 不建真窗口，只测找后端的逻辑
fake_main = sys.modules['__main__']
saved_frozen = getattr(sys, 'frozen', None)
fake_main.do_connect = main_mod.do_connect  # 扮演"打包后的主模块"
sys.frozen = True  # type: ignore[attr-defined]
try:
    got = fake_inst._import_main()
    check('frozen 模式下借用进程自己的 __main__', got is fake_main, repr(got)[:60])
    check('借到的后端确实能干活（有 do_connect）', hasattr(got, 'do_connect'))
    check('缓存进了 eporta_main，不会重复加载', sys.modules.get('eporta_main') is fake_main)
finally:
    if saved_frozen is None:
        del sys.frozen  # type: ignore[attr-defined]
    else:
        sys.frozen = saved_frozen  # type: ignore[attr-defined]
    del fake_main.do_connect
    sys.modules.pop('eporta_main', None)
    if saved_main is not None:
        sys.modules['eporta_main'] = saved_main

server.shutdown()
print()
print('全部通过' if not failures else f'失败 {len(failures)} 项: {failures}')
sys.exit(1 if failures else 0)
