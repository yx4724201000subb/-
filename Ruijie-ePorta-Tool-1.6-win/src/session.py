#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从认证服务器自动获取会话信息（cookie / queryString）

锐捷 ePortal 的登录请求里，``cookie``（JSESSIONID）和 ``login_data.queryString``
都来自"浏览器打开任意 http 网页 → 被跳到认证页"这一步。有两种跳法，都要处理：

1) 标准 HTTP 302：

       GET http://10.0.0.1/   →  302 Location: /eportal/index.jsp?wlanuserip=...

2) **JavaScript 跳转**（ePortal 未认证时实际用的就是这个，很容易漏掉）：

       HTTP/1.1 200 OK
       <script>top.self.location.href='http://10.0.0.1/eportal/index.jsp?wlanuserip=...'</script>

   这种情况下 urllib 认为"没有重定向"，最终地址会停在被劫持的原始网址
   （如 http://123.123.123.123/），queryString 就取不到了 —— 而 queryString 为空
   正是登录时服务器报"WEB认证设备未注册，请确认SAM+/portal/设备上的参数配置是否一致"
   的原因之一。所以这里必须把 body 里的 JS 跳转也跟下去。

注意参数是**服务器加密过的十六进制串**（如 ``nasip=0123456789abcdef0123456789abcdef``），
不要试图用明文 IP/MAC 自己拼，必须原样回传服务器给的那一串。

本模块把这一步自动化，省得用户去浏览器开发者工具里抄。零第三方依赖。

注意：只有在校园网里（能连上认证服务器）才能取到，取不到不算错误，
原版工具也允许 cookie 留空直接登录。
"""

from __future__ import annotations

import re
from html import unescape
from http.cookiejar import CookieJar
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit
from urllib.request import HTTPCookieProcessor, build_opener

# 这些查询参数是每次都会变的时间戳，登录时不需要回传
# （注意只在值是纯数字时才跳过：ePortal 的 t=wireless-v2 是版本标记，必须保留）
_SKIP_QUERY_KEYS = {'t', 'timestamp', '_', 'callback'}

# 只有落在这类地址上，queryString 才是"登录页参数"（wlanuserip/wlanacname/nasip/mac）
_LOGIN_PAGE_MARKERS = ('index.jsp', 'wlanuserip')

# 未认证时 ePortal 用 JS 跳转（而不是 302）把我们送去登录页
_JS_REDIRECT_PATTERNS = (
    re.compile(r"""location\.href\s*=\s*['"]([^'"]+)['"]""", re.I),
    re.compile(r"""location\.replace\s*\(\s*['"]([^'"]+)['"]\s*\)""", re.I),
    re.compile(r"""location\s*=\s*['"]([^'"]+)['"]""", re.I),
    re.compile(r"""window\.open\s*\(\s*['"]([^'"]+)['"]""", re.I),
)

# 最多跟着 JS 跳转走几跳，防止页面互相跳造成死循环
_MAX_JS_HOPS = 5
# 响应体最多读这么多字节（登录/拦截页都很小，success.jsp 是 90KB 左右）
_MAX_BODY = 256 * 1024

# 走 SSO 的学校：ePortal 会先跳到统一身份认证页（带 response_type/client_id 的 OAuth 参数），
# 真正的设备参数被塞在它自己的 redirect_uri 里，要挖出来用那一份。
_SSO_HINTS = ('response_type', 'client_id', 'redirect_uri', 'login_from', 'code_challenge')

# 验证码图片的候选地址（锐捷 ePortal 常见的是 /eportal/validcode）
_VALIDCODE_PATHS = ('/eportal/validcode', '/eportal/validCode')


def _extract_nested_params(query: str) -> str:
    """从 SSO 的 redirect_uri 里挖出 ePortal 的设备参数

    形如::

        response_type=code&client_id=...&redirect_uri=http://<portal>/eportal/login_sso.jsp
            ?wlanuserip=<hex>&wlanacname=<hex>&nasip=<hex>&mac=<hex>&t=wireless-v2&...

    登录 ePortal 要的是 redirect_uri 里的那一份，而不是 OAuth 那一份，
    所以这里把它解出来（可能被编码了两次，多解几次），返回其中的 query 部分。
    """
    for key, value in parse_qsl(query, keep_blank_values=True):
        if key.lower() != 'redirect_uri':
            continue
        inner = value
        for _ in range(3):  # 解到不再是 %XX 为止
            decoded = unquote(inner)
            if decoded == inner:
                break
            inner = decoded
        inner_query = urlsplit(inner).query
        if inner_query and any(marker in inner_query for marker in _LOGIN_PAGE_MARKERS):
            return inner_query
    return ''


def _clean_query(query: str) -> str:
    """去掉每次都变的时间戳，其余原样保留

    注意 ePortal 的参数是服务器加密过的十六进制串，必须原样回传，
    不能自己用明文 IP/MAC 重新拼。
    """
    pairs = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True)
             if not (k.lower() in _SKIP_QUERY_KEYS and _is_timestamp(v))]
    return '&'.join(f'{k}={v}' for k, v in pairs)


def _decode_body(raw: bytes) -> str:
    """认证页可能是 UTF-8 也可能是 GBK，两种都试一下"""
    for encoding in ('utf-8', 'gbk'):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode('utf-8', 'replace')


def _extract_js_redirect(text: str, base_url: str) -> str:
    """从响应体里找出 JS 跳转目标

    ePortal 未认证时返回的不是 302，而是这样一段：

        <script>top.self.location.href='http://10.0.0.1/eportal/index.jsp?...'</script>

    urllib 不认识它，所以必须自己解析出来，否则最终地址会停在被劫持的原始网址上。
    """
    if not text or 'location' not in text:
        return ''
    for pattern in _JS_REDIRECT_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        target = unescape(match.group(1)).strip().replace('\\/', '/')
        if target.startswith(('http://', 'https://', '/')):
            return urljoin(base_url, target)
    return ''


def _is_timestamp(value: str) -> bool:
    """判断一个查询参数值是不是"每次都会变的时间戳"（只有这种才跳过）"""
    return value.isdigit() and len(value) >= 8


def _jar_cookie(jar: CookieJar) -> str:
    """把 cookie jar 里的 cookie 拼成 Cookie 请求头的样子

    优先保留 JSESSIONID，其余按 name=value 顺序拼接。
    """
    items = []
    for cookie in jar:
        items.append(f'{cookie.name}={cookie.value}')
    if not items:
        return ''
    items.sort(key=lambda s: 0 if s.upper().startswith('JSESSIONID') else 1)
    return '; '.join(items)


def fetch_validcode(server: str, timeout: float = 8.0) -> dict:
    """取登录验证码图片（锐捷 ePortal 的 ``/eportal/validcode`` 返回 PNG）

    有些学校（或多次登录失败后）会要求图形验证码：登录 POST 里 ``validcode``
    必须是这张图上的字符，而且**必须用取图那一次的 JSESSIONID** 去登录，
    否则会出现"验证码错误"。

    :return: ``{'image': bytes, 'cookie': str, 'url': str, 'error': str}``
    """
    result = {'image': b'', 'cookie': '', 'url': '', 'error': ''}
    server = (server or '').strip().rstrip('/')
    if not server:
        result['error'] = '认证服务器地址为空'
        return result
    if not server.startswith(('http://', 'https://')):
        server = 'http://' + server

    errors = []
    for path in _VALIDCODE_PATHS:
        url = server + path
        jar = CookieJar()
        opener = build_opener(HTTPCookieProcessor(jar))
        try:
            with opener.open(url, timeout=timeout) as resp:
                data = resp.read(_MAX_BODY)
                ctype = (resp.headers.get('Content-Type') or '').lower()
        except HTTPError as e:
            errors.append(f'{path} → HTTP {e.code}')
            continue
        except (URLError, OSError) as e:
            errors.append(f'{path} → {e}')
            continue
        # 要求是图片，且不是那种"404 error"占位页
        if data[:8] == b'\x89PNG\r\n\x1a\n' or 'image' in ctype:
            result['image'] = data
            result['cookie'] = _jar_cookie(jar)
            result['url'] = url
            return result
        errors.append(f'{path} → 返回的不是图片（{ctype or "未知类型"}）')
    result['error'] = '取不到验证码图片：' + '；'.join(errors)
    return result


def fetch_session(server: str, timeout: float = 8.0,
                  path: str = '/') -> dict:
    """访问认证服务器，取回 JSESSIONID 与 queryString

    :param server: 认证服务器地址，如 ``http://10.0.0.1``
    :param timeout: 超时秒数
    :param path: 访问的路径，默认根路径
    :return: 字典，含 ``cookie`` / ``queryString`` / ``url`` / ``online`` / ``error``
             （失败时 ``error`` 为中文说明，其余为空串）
    """
    result = {'cookie': '', 'queryString': '', 'url': '', 'online': False,
              'sso': False, 'error': ''}
    server = (server or '').strip().rstrip('/')
    if not server:
        result['error'] = '认证服务器地址为空'
        return result
    if not server.startswith(('http://', 'https://')):
        server = 'http://' + server

    jar = CookieJar()
    opener = build_opener(HTTPCookieProcessor(jar))
    target = server + path
    final_url = target
    for _hop in range(_MAX_JS_HOPS + 1):
        try:
            with opener.open(target, timeout=timeout) as res:
                final_url = res.geturl()
                raw = res.read(_MAX_BODY)  # 触发重定向链与 Set-Cookie 处理
        except HTTPError as e:
            # 有些认证页会返回 4xx/5xx，但 cookie 和跳转脚本已经下发了
            final_url = getattr(e, 'geturl', lambda: target)()
            try:
                raw = e.read(_MAX_BODY)
            except Exception:  # noqa: BLE001 - 读不到 body 不影响拿 cookie
                raw = b''
            result['error'] = f'服务器返回 HTTP {e.code} {e.reason}'
        except (URLError, OSError, ValueError) as e:
            result['error'] = (
                f'连不上认证服务器（{e}）\n'
                '请确认当前已连接校园网，且 url.server 填写正确。'
            )
            return result

        # HTTP 302 由 urllib 自己跟，这里补上 JS 跳转那种
        js_target = _extract_js_redirect(_decode_body(raw), final_url)
        if not js_target or js_target == final_url:
            break
        target = js_target

    result['url'] = final_url
    result['cookie'] = _jar_cookie(jar)

    path_part = urlsplit(final_url).path.lower()
    if 'success' in path_part:
        # 已经通过认证了，认证页会直接跳到"登录成功"页，
        # 这时的 queryString 是登录之后才有的（userIndex=...），回传上去反而会让
        # 服务器报"WEB认证设备未注册，请确认SAM+/portal/设备上的参数配置是否一致"。
        result['online'] = True
        result['error'] = (
            '当前已经在线（认证页直接跳到了登录成功页），拿不到登录页的参数。\n'
            '如果需要重新抓取，请先断网/下线，再点一次这个按钮。'
        )
        return result

    query = urlsplit(final_url).query
    if not query:
        return result

    # 情况一：走统一身份认证（SSO）的学校，设备参数被塞在 redirect_uri 里
    if any(hint in query for hint in _SSO_HINTS):
        nested = _extract_nested_params(query)
        if nested:
            result['queryString'] = _clean_query(nested)
            result['sso'] = True
            return result
        # 只有 OAuth 参数、挖不到设备参数：这份不能拿去登录
        result['sso'] = True
        result['error'] = (
            '认证页跳到了统一身份认证（SSO）页面，但没能从里面挖出设备参数。\n'
            '这份参数不能直接用于登录；请先断网/下线，从"打开网页被拦"的那一刻再抓一次。'
        )
        return result

    # 情况二：普通的 ePortal 登录页
    if any(marker in final_url for marker in _LOGIN_PAGE_MARKERS):
        result['queryString'] = _clean_query(query)
    else:
        result['error'] = (
            f'认证页地址不像登录页（{final_url}），不确定 queryString 是否能用于登录。'
        )
    return result


def describe(result: dict) -> str:
    """把 fetch_session 的结果整理成给人看的多行文本"""
    lines = []
    if result.get('error'):
        lines.append(f'[警告] {result["error"]}')
    if result.get('url'):
        lines.append(f'最终地址    : {result["url"]}')
    lines.append(f'Cookie      : {result.get("cookie") or "（未取到，可留空）"}')
    if result.get('sso'):
        lines.append('认证方式    : 统一身份认证（SSO），已从 redirect_uri 里取出设备参数')
    lines.append(f'queryString : {result.get("queryString") or "（未取到，可留空）"}')
    return '\n'.join(lines)
