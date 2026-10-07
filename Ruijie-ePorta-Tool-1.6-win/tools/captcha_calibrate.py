#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证码模板重新标定工具

``src/captcha.py`` 里的 10 个数字模板是针对**某一种**锐捷 ePortal 验证码
（80×30、4 位数字、固定字体、白底纯色字）标定的。如果你的学校验证码字体不一样，
用这个脚本重新标定一遍即可，整个流程只需要几分钟：

    .venv\\Scripts\\python.exe tools\\captcha_calibrate.py --count 40

它会：

1. 从认证服务器抓 ``--count`` 张验证码（**只取图，不登录、不断网**）；
2. 把每张图切成 4 个字形，再按"像素完全相同"聚类；
3. 把每个类的 ASCII 字形打印出来，逐个问你"这是哪个数字"；
4. 生成新的 ``_TEMPLATE_DATA``（默认写到 ``tools/templates_snippet.py``），
   你把它整段替换掉 ``src/captcha.py`` 里的同名变量即可；
5. 顺便报告类内 / 类间距离，用来判断安全边际有多大。

如果不想交互（比如只想先看看聚类效果）：

    .venv\\Scripts\\python.exe tools\\captcha_calibrate.py --count 40 --no-prompt
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import captcha  # noqa: E402  (需要先设置 sys.path)
import session  # noqa: E402


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='重新标定 src/captcha.py 的验证码模板',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--count', type=int, default=40, help='抓多少张验证码（默认 40，越多越稳）')
    parser.add_argument('--server', default=None, help='认证服务器地址，默认读 config.yml 里的 url.server')
    parser.add_argument('--config', default=None, help='config.yml 路径（默认项目根下的 config.yml）')
    parser.add_argument('--out', default=str(ROOT / 'tools' / 'templates_snippet.py'), help='生成的模板片段写到哪')
    parser.add_argument('--no-prompt', action='store_true', help='不交互，标签一律写成 ?')
    parser.add_argument('--threshold', type=int, default=0, help='聚类时允许的差异像素数（默认 0，即完全相同）')
    return parser.parse_args(argv)


def read_server(config_path: str | None) -> str:
    """从 config.yml 里取认证服务器地址（不依赖 PyYAML：只认 server: 那一行）"""
    path = Path(config_path) if config_path else ROOT / 'config.yml'
    if not path.exists():
        return ''
    text = path.read_text(encoding='utf-8', errors='replace')
    match = re.search(r'^\s*server\s*:\s*[\'"]?([^\s\'"#]+)', text, re.M)
    return match.group(1) if match else ''


def fetch(server: str, count: int) -> list[bytes]:
    images: list[bytes] = []
    for index in range(count):
        info = session.fetch_validcode(server)
        if info.get('error') or not info.get('image'):
            print(f'  [{index + 1}/{count}] 取图失败：{info.get("error") or "空响应"}')
            continue
        images.append(info['image'])
        print(f'  [{index + 1}/{count}] {len(info["image"])} 字节')
    return images


def glyphs_of(image: bytes) -> list[tuple[str, ...]]:
    """把一张验证码解码、二值化、按空白列切成字形"""
    width, height, rows = captcha._decode_png(image)
    grid = captcha._binarize(rows, width, height)
    return list(captcha._segment(grid, width, height))


def cluster(bitmaps: list[tuple[str, ...]], threshold: int = 0):
    """按差异像素数聚类，返回 [(代表位图, 样本数)]，按样本数从多到少排序"""
    clusters: list[dict] = []
    for bitmap in bitmaps:
        for item in clusters:
            if captcha._distance(bitmap, item['rep']) <= threshold:
                item['count'] += 1
                break
        else:
            clusters.append({'rep': bitmap, 'count': 1})
    clusters.sort(key=lambda item: -item['count'])
    return [(item['rep'], item['count']) for item in clusters]


def row_text(row) -> str:
    """位图的一行 → '#' / '.' 字符串

    ``captcha._segment()`` 返回的是"每行一个由 True/False 组成的元组"，
    而 ``_TEMPLATE_DATA`` 里存的是 '#' / '.' 字符串，这里统一成后者。
    """
    if isinstance(row, str):
        return row
    return ''.join('#' if cell else '.' for cell in row)


def size_of(bitmap) -> tuple[int, int]:
    return (len(row_text(bitmap[0])) if bitmap else 0), len(bitmap)


def show(bitmap) -> str:
    return '\n'.join('    ' + row_text(row) for row in bitmap)


def distance_matrix(reps: list[tuple[str, ...]]):
    """类间最小距离 / 类内最大距离（类内来自聚类阈值，这里只报类间）"""
    best = None
    for i in range(len(reps)):
        for j in range(i + 1, len(reps)):
            value = captcha._distance(reps[i], reps[j])
            if best is None or value < best[0]:
                best = (value, i, j)
    return best


def build_snippet(pairs: list[tuple[str, tuple[str, ...]]]) -> str:
    lines = ['_TEMPLATE_DATA: list[tuple[str, str]] = [']
    for label, bitmap in pairs:
        art = '/'.join(row_text(row) for row in bitmap)
        lines.append(f"    ('{label}',")
        lines.append(f"     '{art}'),")
    lines.append(']')
    return '\n'.join(lines) + '\n'


def main(argv=None) -> int:
    args = parse_args(argv)
    server = args.server or read_server(args.config)
    if not server:
        print('没有拿到认证服务器地址。请用 --server http://x.x.x.x 指定，')
        print(f'或者先跑 run.bat --gen-config 生成 {ROOT / "config.yml"} 并填好 url.server。')
        return 2

    print(f'认证服务器：{server}')
    print(f'开始抓 {args.count} 张验证码（只取图，不登录、不断网）…')
    images = fetch(server, args.count)
    if not images:
        print('一张都没抓到，检查一下服务器地址和网络。')
        return 1

    bitmaps: list[tuple[str, ...]] = []
    per_image: list[list[tuple[str, ...]]] = []
    for image in images:
        try:
            got = glyphs_of(image)
        except Exception as exc:  # 解码失败就当这张坏了
            print(f'  跳过一张：{exc}')
            continue
        bitmaps.extend(got)
        per_image.append(got)

    print()
    print(f'解析成功 {len(per_image)} 张 → 切出 {len(bitmaps)} 个字形')
    clusters = cluster(bitmaps, args.threshold)
    print(f'聚类结果：{len(clusters)} 类（期望 10 类；差太多就加大 --count 或调 --threshold）')
    print()

    pairs: list[tuple[str, tuple[str, ...]]] = []
    used: dict[str, int] = {}
    for index, (rep, count) in enumerate(clusters):
        width, height = size_of(rep)
        print(f'--- 类 {index}：{count} 个样本，{width}x{height} ---')
        print(show(rep))
        label = '?'
        if not args.no_prompt:
            while True:
                answer = input(f'    这是哪个数字？(0-9，回车跳过) ').strip()
                if not answer:
                    break
                if len(answer) == 1 and answer.isdigit() and answer not in used:
                    label = answer
                    used[answer] = index
                    break
                print('    请输入一个还没用过的数字（0-9）')
        pairs.append((label, rep))
        print()

    if len(clusters) != 10:
        print(f'注意：聚出 {len(clusters)} 类，而数字只有 10 个，说明样本可能不够或有噪声。')

    matrix = distance_matrix([rep for rep, _ in clusters])
    if matrix:
        value, i, j = matrix
        print(f'类间最小距离：{value}（类 {i} 与类 {j} 之间）')
        print(f'当前判定阈值：best ≤ {captcha._MAX_DISTANCE} 且与次优相差 ≥ {captcha._MIN_MARGIN}')

    out = Path(args.out)
    out.write_text(build_snippet(pairs), encoding='utf-8')
    print()
    print(f'模板片段已写入：{out}')
    print('把它整段替换掉 src/captcha.py 里的 _TEMPLATE_DATA 即可（注意每行结尾的逗号）。')
    if '?' in dict(pairs):
        print('提示：里面还有 ? 占位标签，替换前记得改成真正的数字。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
