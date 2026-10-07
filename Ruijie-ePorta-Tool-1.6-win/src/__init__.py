#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""包入口，只为了支持 `python -m src` 这种运行方式。

真正的代码在 src/__main__.py。
"""

import sys

from .__main__ import entry_point

if __name__ == '__main__':
    sys.exit(entry_point())
