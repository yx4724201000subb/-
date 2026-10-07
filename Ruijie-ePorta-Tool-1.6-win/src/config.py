#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配置文件的定位、生成与读取

相对原版的改动：

1. **配置文件位置可预测**。原版用 ``dirname(sys.argv[0])`` 定位 config.yml，
   用 ``python -m``、从别处调用、或双击不同目录的脚本时会找错地方。现在按
   ``--config`` → ``环境变量 EPORTA_CONFIG`` → 程序目录（含打包后的 exe 目录）
   → 项目根目录 的顺序查找。
2. **报错友好**。原版缺 key 会直接 KeyError 崩掉且没有提示，现在所有缺失/类型
   错误都会给出中文提示并指出是哪个字段。
3. **版本号提示修正**：原版提示用户不要改 ``config_version``，实际字段名是 ``version``。
"""

from __future__ import annotations

import os
import sys
from os.path import abspath, dirname, isfile, join

import yaml

CURRENT_CONFIG_VERSION = 3
CONFIG_FILENAME = 'config.yml'

# 默认重试参数（开机自启场景：最多尝试 5 分钟，每 10 秒一次）
DEFAULT_RETRY_TIMEOUT = 300
DEFAULT_RETRY_INTERVAL = 10

# login_data / logout_data 里已知的键，GUI 会单独列出，渲染时按这个顺序
KNOWN_LOGIN_KEYS = (
    'userId', 'password', 'service', 'queryString',
    'operatorPwd', 'operatorUserId', 'validcode', 'passwordEncrypt',
)

CONFIG_TEMPLATE = '''\
# 本配置文件内容需要根据学校服务器设置动态调整
# 详细抓包与填写方法见 README.md（或原作者的 B 站视频 BV1TZ4y167b6）
# 也可以双击「设置.bat」用图形界面修改，改完点保存即可。
main:
  version: 3 # 配置文件版本号，请勿更改

funtion:
  check_school_network: true # 是否检查校园网环境
  disconnect_network: true # 设备已联网时是否询问断网（改为 false 则联网后直接退出）

url:
  server: http://127.0.0.1 # 校园网登录服务器的地址，不知道填什么就用 --detect-server 自动探测
  login: /eportal/InterFace.do?method=login # 校园网登录地址，无需服务器地址
  logout: /eportal/InterFace.do?method=logout # 校园网断线地址，无需服务器地址

# 请根据抓包结果调整条目与内容
cookie: ''

# 请根据抓包结果调整条目与内容（?key1=value1&key2=value2）
# 明文密码把 passwordEncrypt 设为 false；抓包里是加密串则设为 true
login_data:
  userId: '00000000000'
  password:
  service:
  queryString:
  operatorPwd:
  operatorUserId:
  validcode:
  passwordEncrypt: false

# 下线请求用到的参数，一般来说不填也能用（?key1=value1&key2=value2）
logout_data:

# 开机自启后最多尝试多久、每隔多久试一次（秒）
auto:
  retry_timeout: 300
  retry_interval: 10

headers:
  Referer:
'''


class ConfigError(Exception):
    """配置文件无法使用（内容或版本有问题）"""


def _candidate_dirs() -> list[str]:
    """按优先级返回可能存放 config.yml 的目录"""
    dirs: list[str] = []

    if getattr(sys, 'frozen', False):
        exe_dir = dirname(abspath(sys.executable))
        # PyInstaller 打包后：exe 所在目录（用户可见、可写）
        dirs.append(exe_dir)
        # 再往上一级看一眼：exe 放在项目的 dist\ 里时，配置其实在项目根目录
        dirs.append(dirname(exe_dir))
        dirs.append(getattr(sys, '_MEIPASS', ''))

    dirs.append(dirname(abspath(__file__)))          # ...\src
    dirs.append(dirname(dirname(abspath(__file__))))  # 项目根目录
    dirs.append(os.getcwd())
    return [d for d in dirs if d]


def resolve_config_path(cli_path: str | None = None) -> str:
    """决定使用哪个 config.yml 路径"""
    if cli_path:
        return abspath(cli_path)

    env_path = os.environ.get('EPORTA_CONFIG')
    if env_path:
        return abspath(env_path)

    for folder in _candidate_dirs():
        candidate = join(folder, CONFIG_FILENAME)
        if isfile(candidate):
            return candidate

    # 一个都没有：把新配置写到项目根目录（打包后写到 exe 旁边）
    if getattr(sys, 'frozen', False):
        return join(dirname(abspath(sys.executable)), CONFIG_FILENAME)
    return join(dirname(dirname(abspath(__file__))), CONFIG_FILENAME)


def write_template(config_path: str) -> None:
    """生成默认配置文件"""
    with open(config_path, 'w', encoding='utf-8') as fp:
        fp.write(CONFIG_TEMPLATE)


# --------------------------------------------------------------------------- #
# 回写配置（GUI 保存用）
#
# 直接用 yaml.safe_dump() 会把注释和键顺序全部丢掉，配置文件就变得没法手改了，
# 所以这里自己渲染：模板里的注释原样保留，只把值换掉；用户额外加的
# login_data / logout_data / headers 条目也会一并写回去。
# --------------------------------------------------------------------------- #
def _scalar(value) -> str:
    """把一个值渲染成 YAML 标量

    * 空值渲染成空（``key:``）
    * ``'true'``/``'false'`` 渲染成裸布尔，跟模板保持一致
    * 其余字符串一律加单引号，避免学号这类纯数字被 YAML 当成 int
    """
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'true' if value else 'false'
    text = str(value)
    if text == '':
        return ''
    if text.lower() in ('true', 'false'):
        return text.lower()
    if isinstance(value, int):
        return text
    return "'" + text.replace("'", "''") + "'"


def _line(key: str, value, comment: str = '', indent: int = 2) -> str:
    rendered = _scalar(value)
    body = f'{" " * indent}{key}: {rendered}'.rstrip()
    return f'{body} {comment}'.rstrip() + '\n'


def _extra_lines(mapping: dict, skip: tuple[str, ...], indent: int = 2) -> str:
    out = ''
    for k, v in (mapping or {}).items():
        if k in skip:
            continue
        out += _line(str(k), v, indent=indent)
    return out


def render_cfg(cfg: dict) -> str:
    """把配置字典渲染成带注释的 YAML 文本"""
    url = cfg.get('url') or {}
    funtion = cfg.get('funtion') or {}
    login_data = cfg.get('login_data') or {}
    logout_data = cfg.get('logout_data') or {}
    headers = cfg.get('headers') or {}
    auto = cfg.get('auto') or {}

    text = '# 本配置文件内容需要根据学校服务器设置动态调整\n'
    text += '# 详细抓包与填写方法见 README.md（或原作者的 B 站视频 BV1TZ4y167b6）\n'
    text += '# 也可以双击「设置.bat」用图形界面修改，改完点保存即可。\n'
    text += 'main:\n'
    text += _line('version', 3, '# 配置文件版本号，请勿更改')
    text += '\nfuntion:\n'
    text += _line('check_school_network', funtion.get('check_school_network', True),
                  '# 是否检查校园网环境')
    text += _line('disconnect_network', funtion.get('disconnect_network', True),
                  '# 设备已联网时是否询问断网（改为 false 则联网后直接退出）')
    text += '\nurl:\n'
    text += _line('server', url.get('server', ''),
                  '# 校园网登录服务器的地址（就是浏览器登录页的地址，如 http://10.0.0.1）\n'
                  '# 不知道填什么：运行 --detect-server 自动探测，'
                  '或在设置窗口里点「自动探测」')
    text += _line('login', url.get('login', '/eportal/InterFace.do?method=login'),
                  '# 校园网登录地址，无需服务器地址')
    text += _line('logout', url.get('logout', '/eportal/InterFace.do?method=logout'),
                  '# 校园网断线地址，无需服务器地址')
    text += '\n# 会话 Cookie。留空即可：工具在联网前会自己去认证服务器抓一次\n'
    text += '# （服务器只在"本机尚未认证"的时候才下发正确的值）\n'
    text += _line('cookie', cfg.get('cookie', ''), indent=0)
    text += '\n# 请根据抓包结果调整条目与内容（?key1=value1&key2=value2）\n'
    text += '# 明文密码把 passwordEncrypt 设为 false；抓包里是加密串则设为 true\n'
    text += '# queryString 也可以留空，同样会自动抓。\n'
    text += '# 千万不要用明文 IP/MAC 自己拼：真值是服务器加密过的十六进制串，\n'
    text += '# 拼错了会被拒，报"WEB认证设备未注册，请确认SAM+/portal/设备上的参数配置是否一致"。\n'
    text += '# 走统一身份认证（SSO）的学校，工具会自动从 redirect_uri 里把设备参数挖出来。\n'
    text += '# validcode 是图形验证码：留空即可，工具每次会自动取一张新图并**自动识别**\n'
    text += '# （纯标准库实现，不需要额外装东西），识别不出来时才弹窗让你输入。\n'
    text += '# 想确认识别功能正常，可以运行 --test-captcha：取一张图、报识别结果，\n'
    text += '# 同时把图存成 validcode_last.png，方便你肉眼核对。\n'
    text += 'login_data:\n'
    for key in KNOWN_LOGIN_KEYS:
        text += _line(key, login_data.get(key, ''))
    text += _extra_lines(login_data, KNOWN_LOGIN_KEYS)
    text += '\n# 下线请求用到的参数，一般来说不填也能用（?key1=value1&key2=value2）\n'
    if logout_data:
        text += 'logout_data:\n'
        for k, v in logout_data.items():
            text += _line(str(k), v)
    else:
        text += 'logout_data:\n'
    text += '\n# 开机自启后最多尝试多久、每隔多久试一次（秒）\n'
    text += 'auto:\n'
    text += _line('retry_timeout', auto.get('retry_timeout', DEFAULT_RETRY_TIMEOUT))
    text += _line('retry_interval', auto.get('retry_interval', DEFAULT_RETRY_INTERVAL))
    text += '\nheaders:\n'
    if headers:
        for k, v in headers.items():
            text += _line(str(k), v)
    else:
        text += _line('Referer', '')
    return text


def write_cfg(cfg: dict, config_path: str) -> None:
    """把配置写回文件（保留注释与键顺序）"""
    with open(config_path, 'w', encoding='utf-8') as fp:
        fp.write(render_cfg(cfg))


def _normalize(target: dict) -> dict:
    """把 dict 里的 int/bool/None 统一转成小写字符串（True -> 'true'）"""
    for k, v in target.items():
        if isinstance(v, dict):
            _normalize(v)
        elif isinstance(v, int):  # bool 是 int 的子类，会一起处理
            target[k] = str(v).lower()
        elif v is None:
            target[k] = ''
    return target


def _require(cfg: dict, key: str, kind: type, where: str = '顶层'):
    if key not in cfg or cfg[key] is None:
        raise ConfigError(f'{where} 缺少必需的配置项：{key}')
    if not isinstance(cfg[key], kind):
        raise ConfigError(
            f'配置项 {where}.{key} 的类型不对，应为 {kind.__name__}，实际是 {type(cfg[key]).__name__}'
        )
    return cfg[key]


def read_cfg(config_path: str) -> dict:
    """读取并校验配置文件

    :raises ConfigError: 文件缺失 / YAML 语法错误 / 版本不符 / 必填项缺失
    """
    try:
        with open(config_path, 'r', encoding='utf-8') as fp:
            cfg = yaml.safe_load(fp)
    except FileNotFoundError:
        raise ConfigError(f'找不到配置文件：{config_path}')
    except yaml.YAMLError as e:
        raise ConfigError(f'config.yml 不是合法的 YAML：\n{e}')
    except OSError as e:
        raise ConfigError(f'读取配置文件失败：\n{e}')

    if not isinstance(cfg, dict):
        raise ConfigError('config.yml 内容为空或格式不正确，请删除后重新生成')

    main = _require(cfg, 'main', dict)
    version = main.get('version', 0)
    if not isinstance(version, int):
        raise ConfigError('main.version 必须是数字，且不要改成字符串')
    if version < CURRENT_CONFIG_VERSION:
        raise ConfigError(
            '配置文件格式过期，请备份并删除原 config.yml 后重新运行本程序生成'
        )
    if version > CURRENT_CONFIG_VERSION:
        raise ConfigError(
            f'配置文件版本过高（{version} > {CURRENT_CONFIG_VERSION}），请更新本程序；'
            '如程序已是最新版，请勿修改 main.version 的值'
        )

    url = _require(cfg, 'url', dict)
    for key in ('server', 'login', 'logout'):
        _require(url, key, str, 'url')
    if not url['server'].strip():
        raise ConfigError('url.server 不能为空，请填入校园网认证服务器地址')

    funtion = _require(cfg, 'funtion', dict)  # 原版拼写就是 funtion，保持兼容
    for key in ('check_school_network', 'disconnect_network'):
        if key not in funtion:
            funtion[key] = True

    login_data = cfg.get('login_data')
    if not isinstance(login_data, dict):
        raise ConfigError('login_data 必须是键值对列表，请参照 config.yml 模板填写')
    _normalize(login_data)

    if login_data.get('userId', '') in ('', '00000000000'):
        raise ConfigError('配置文件未正确填写：login_data.userId 还是模板里的默认值')

    # logout_data 允许为空
    if not isinstance(cfg.get('logout_data'), dict):
        cfg['logout_data'] = {}
    _normalize(cfg['logout_data'])

    # auto 段允许整个缺失（老配置文件），这里补默认值并保证是正整数
    auto = cfg.get('auto')
    if not isinstance(auto, dict):
        auto = {}
    for key, default in (('retry_timeout', DEFAULT_RETRY_TIMEOUT),
                         ('retry_interval', DEFAULT_RETRY_INTERVAL)):
        try:
            value = int(auto.get(key, default))
        except (TypeError, ValueError):
            value = default
        auto[key] = value if value > 0 else default
    cfg['auto'] = auto

    headers = cfg.get('headers')
    if not isinstance(headers, dict):
        cfg['headers'] = {}
    # 原版会把 headers 里为 None 的值变成空字符串，这里直接丢掉空值，
    # 避免出现 Referer: '' 这种会让部分 ePortal 直接 403 的请求头
    cfg['headers'] = {k: v for k, v in cfg['headers'].items() if v not in (None, '')}

    cfg['cookie'] = '' if cfg.get('cookie') is None else str(cfg['cookie']).strip()
    _normalize(url)  # server/login/logout 保证是字符串

    if url['server'].endswith('/'):
        url['server'] = url['server'].rstrip('/')
    if not url['server'].startswith(('http://', 'https://')):
        raise ConfigError('url.server 必须以 http:// 或 https:// 开头')
    if not url['login'].startswith('/'):
        url['login'] = '/' + url['login']
    if not url['logout'].startswith('/'):
        url['logout'] = '/' + url['logout']

    return cfg
