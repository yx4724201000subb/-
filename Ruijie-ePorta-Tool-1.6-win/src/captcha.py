# -*- coding: utf-8 -*-
"""验证码自动识别（纯标准库，不依赖 Pillow / numpy / 任何 OCR 库）

为什么需要它
------------
这台学校的 portal 在 ``InterFace.do?method=pageInfo`` 里把 ``validCodeUrl`` 设成了
``/eportal/validcode?rnd=…``，也就是**每次登录都必须带图形验证码**。
但本工具的核心用法是"开机自启 → 后台重试 → 连上就收工"，那时候没有人能输验证码。
所以：先自动识别，识别不出来才退化成弹窗问人（有人在场时）。

为什么模板匹配就够
------------------
这个验证码是固定字体渲染的位图，特征很干净：
``80×30``、8bit 真彩 PNG（无隔行、无调色板）、白底纯橙字、无噪点、无旋转、无粘连、每个数字一种字形。
实测 40 张样本切出 160 个字形，聚成 10 类，**类内像素完全相同（距离 0）**，
而**类间最小距离 61**（最像的一对是 3 和 5）。所以"精确匹配 + 容忍 1 像素平移"已经足够；
匹配不上或前两名太接近时返回空串，交给人工 —— 宁可不猜，也不要用错误的验证码去撞服务器。
"""

import struct
import zlib

# 笔画判定：不是接近白色就算笔画（与采集样本时用的阈值保持一致）
_BG_THRESHOLD = 190

# 匹配安全边界（类内 0、类间 61，这里留了很大余量）
_MAX_DISTANCE = 8        # 与最像的模板差这么多像素以上就不认
_MIN_MARGIN = 6          # 第一名与第二名至少差这么多像素，否则算"分不清"

_EXPECTED_GLYPHS = 4     # 这个 portal 的验证码固定 4 位

# 由 build_templates.py 生成：10 个数字的位图模板（行用 / 分隔，# 为笔画）
# 这个学校的验证码字体完全确定性：40 张样本、160 个字形聚成 10 类，类内最大距离 0、类间最小距离 61
_TEMPLATE_DATA = [
    ('0', '.....####..../...##...##.../..##.....##../..##.....###./.###......##./.##.......##./###.......###/###.......###/###.......###/###.......###/###.......###/###.......###/###.......###/###.......###/.##.......##./.###.....###./.###.....###./..##.....##../...##...##.../....####.....'),   # 17 个样本，13x20
    ('2', '...######..../..########.../.##....####../#.......####./#........###./.........###./.........###./.........##../.........##../........##.../........##.../.......##..../......##...../......#....../.....#......./....#......../...##.......#/..##.......##/.###########./############.'),   # 19 个样本，13x20
    ('6', '.........####/.......###.../.....###...../....###....../...###......./..###......../..##........./.###........./.##..#####.../#####...###../###......###./###......####/###.......###/###.......###/###.......###/.##.......###/.###......##./..##.....###./...##...###../....#####....'),   # 12 个样本，13x20
    ('9', '....#####..../..###...##.../.###.....##../.##......###./###.......##./###.......###/###.......###/###.......###/####......###/.###......###/..###....####/...######.##./.........###./.........##../........###../.......###.../......###..../.....###...../...###......./####.........'),   # 20 个样本，13x20
    ('7', '..###########/.############/.###########./.#........##./#........##../.........##../.........##../........##.../........##.../........##.../.......##..../.......##..../.......##..../......##...../......##...../......##...../.....##....../.....##....../.....##....../....##.......'),   # 13 个样本，13x20
    ('3', '...######.../..########../.#.....####./#.......###./........###./........###./........##../.......##.../.....####.../...#######../......#####./........####/........####/.........###/.........###/.........###/.........##./##......##../####...##.../.######.....'),   # 17 个样本，12x20
    ('5', '....#######/...#######./...#######./..#......../..#......../.####....../.######..../#########../....######./......####./.......####/........###/.........##/.........##/.........##/.........#./........##./.#.....##../########.../.#####.....'),   # 15 个样本，11x20
    ('4', '........##.../.......###.../......####.../......####.../.....#.###.../.....#.###.../....#..###.../...#...###.../...#...###.../..#....###.../..#....###.../.#.....###.../##.....###.../#############/#############/.......###.../.......###.../.......###.../.......###.../.......###...'),   # 14 个样本，13x20
    ('8', '...#####.../.###...###./.##.....###/###.....###/###.....###/###.....###/####...###./.####..##../..#####..../...####..../...#####.../..##.####../.##...####./###....####/###.....###/###.....###/###.....###/.##.....##./.###...###./...#####...'),   # 19 个样本，11x20
    ('1', '....##.../.#####.../######.../...###.../...###.../...###.../...###.../...###.../...###.../...###.../...###.../...###.../...###.../...###.../...###.../...###.../...###.../...###.../...###.../#########'),   # 14 个样本，9x20
]


def _decode_png(data):
    """解 8bit 真彩 PNG（就是这个验证码的格式），返回 ``(w, h, rows)``

    只用手写 PNG 解码是为了让整个工具保持"零额外依赖"：验证码只是 80×30 的小图，
    用不着为它引入 Pillow。遇到别的格式就抛 ValueError，调用方会退化成问人。
    """
    if not data or data[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError('不是 PNG 数据')
    pos = 8
    width = height = depth = color = interlace = 0
    idat = bytearray()
    while pos + 8 <= len(data):
        (length,) = struct.unpack('>I', data[pos:pos + 4])
        ctype = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if ctype == b'IHDR':
            width, height, depth, color, _comp, _filt, interlace = struct.unpack('>IIBBBBB', body[:13])
        elif ctype == b'IDAT':
            idat += body
        elif ctype == b'IEND':
            break
        pos += 12 + length
    if depth != 8 or color != 2 or interlace != 0:
        raise ValueError(f'不支持的 PNG 格式：depth={depth} color={color} interlace={interlace}')
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as e:
        raise ValueError(f'PNG 数据损坏：{e}')
    stride = width * 3
    rows = []
    prev = bytearray(stride)
    off = 0
    for _ in range(height):
        if off + 1 + stride > len(raw):
            raise ValueError('PNG 数据不完整')
        ftype = raw[off]
        line = bytearray(raw[off + 1:off + 1 + stride])
        off += 1 + stride
        for i in range(stride):
            a = line[i - 3] if i >= 3 else 0
            b = prev[i]
            c = prev[i - 3] if i >= 3 else 0
            x = line[i]
            if ftype == 1:
                line[i] = (x + a) & 0xFF
            elif ftype == 2:
                line[i] = (x + b) & 0xFF
            elif ftype == 3:
                line[i] = (x + (a + b) // 2) & 0xFF
            elif ftype == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (x + pr) & 0xFF
            elif ftype != 0:
                raise ValueError(f'未知的 PNG 滤波类型 {ftype}')
        rows.append(bytes(line))
        prev = line
    return width, height, rows


def _binarize(rows, width, height):
    grid = []
    for y in range(height):
        line = rows[y]
        grid.append([0 if (line[x * 3] > _BG_THRESHOLD
                           and line[x * 3 + 1] > _BG_THRESHOLD
                           and line[x * 3 + 2] > _BG_THRESHOLD) else 1
                     for x in range(width)])
    return grid


def _crop(grid, x0, x1, y0, y1):
    return tuple(tuple(grid[y][x0:x1]) for y in range(y0, y1))


def _segment(grid, width, height):
    """按空白列切字，返回裁好边的位图列表"""
    cols = [any(grid[y][x] for y in range(height)) for x in range(width)]
    spans = []
    start = None
    for x in range(width):
        if cols[x] and start is None:
            start = x
        elif not cols[x] and start is not None:
            spans.append((start, x))
            start = None
    if start is not None:
        spans.append((start, width))
    glyphs = []
    for x0, x1 in spans:
        ys = [y for y in range(height) if any(grid[y][x] for x in range(x0, x1))]
        if not ys:
            continue
        glyphs.append(_crop(grid, x0, x1, ys[0], ys[-1] + 1))
    return glyphs


def _distance(a, b):
    """两个位图的差异像素数，允许上下左右各 1 像素的平移（取最小）"""
    if not a or not b:
        return 10 ** 6
    ha, wa = len(a), len(a[0])
    hb, wb = len(b), len(b[0])
    best = None
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            hh = max(ha, hb + dy)
            ww = max(wa, wb + dx)
            diff = 0
            for y in range(hh):
                row_a = a[y] if 0 <= y < ha else None
                yb = y - dy
                row_b = b[yb] if 0 <= yb < hb else None
                for x in range(ww):
                    va = row_a[x] if (row_a is not None and 0 <= x < wa) else 0
                    vb = row_b[x] if (row_b is not None and 0 <= x < wb) else 0
                    if va != vb:
                        diff += 1
            if best is None or diff < best:
                best = diff
    return best


_TEMPLATES = None


def _templates():
    """把 ``_TEMPLATE_DATA`` 里的字符串模板解析成位图，只做一次"""
    global _TEMPLATES
    if _TEMPLATES is None:
        out = []
        for digit, rows in _TEMPLATE_DATA:
            bitmap = tuple(tuple(1 if ch == '#' else 0 for ch in row) for row in rows.split('/'))
            out.append((digit, bitmap))
        _TEMPLATES = out
    return _TEMPLATES


def recognize(image_bytes):
    """识别验证码，返回 ``(验证码字符串, 说明)``；认不出来时字符串为空"""
    templates = _templates()
    if not templates:
        return '', '模板为空（安装不完整？）'
    try:
        width, height, rows = _decode_png(image_bytes)
    except ValueError as e:
        return '', f'解码失败：{e}'
    grid = _binarize(rows, width, height)
    glyphs = _segment(grid, width, height)
    if len(glyphs) != _EXPECTED_GLYPHS:
        return '', f'切出 {len(glyphs)} 个字形（预期 {_EXPECTED_GLYPHS} 个）'

    digits = []
    notes = []
    for idx, glyph in enumerate(glyphs, 1):
        scored = sorted((_distance(tpl, glyph), digit) for digit, tpl in templates)
        best, best_digit = scored[0]
        second = scored[1][0]
        if best > _MAX_DISTANCE:
            return '', f'第 {idx} 位匹配不上（最像的是 {best_digit}，差 {best} 像素）'
        if second - best < _MIN_MARGIN:
            return '', (f'第 {idx} 位分不清（{best_digit} 与 {scored[1][1]} 分别差 '
                        f'{best}/{second} 像素）')
        digits.append(best_digit)
        notes.append(f'{best_digit}:{best}')
    code = ''.join(digits)
    return code, '每位差异 ' + ' '.join(notes)


def solve(image_bytes):
    """只要结果：识别出来就返回 4 位字符串，否则返回空串"""
    code, _detail = recognize(image_bytes)
    return code
