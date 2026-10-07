# -*- mode: python ; coding: utf-8 -*-
import os

# 路径一律用 SPECPATH（本文件所在目录）拼出来。
# 以前这里写的是 'src/__main__.py' / pathex=['src'] 这种相对路径：
# 只有"cd 到项目根目录再执行"才是对的，换个目录执行 PyInstaller，
# pathex 就会指到别处，打出来的 exe 里**少了 config.py / captcha.py / session.py**，
# 双击只会弹一个 "Unhandled exception in script"（而且 exe 是无控制台的，很难查）。
ROOT = os.path.abspath(SPECPATH)
SRC = os.path.join(ROOT, 'src')
ICON = os.path.join(SRC, 'wangluo.ico')

block_cipher = None


a = Analysis([os.path.join(SRC, '__main__.py')],
             pathex=[SRC, ROOT],
             binaries=[],
             datas=[(ICON, '.')],
             hiddenimports=['queue'],
             hookspath=[],
             hooksconfig={},
             runtime_hooks=[],
             excludes=[],
             win_no_prefer_redirects=False,
             win_private_assemblies=False,
             cipher=block_cipher,
             noarchive=False)
pyz = PYZ(a.pure, a.zipped_data,
             cipher=block_cipher)

exe = EXE(pyz,
          a.scripts,
          a.binaries,
          a.zipfiles,
          a.datas,
          name='锐捷 ePorta 连接工具',
          debug=False,
          bootloader_ignore_signals=False,
          strip=False,
          upx=True,
          upx_exclude=['ucrtbase.dll','vcruntime140.dll'],
          runtime_tmpdir=None,
          console=False,
          disable_windowed_traceback=False,
          target_arch=None,
          codesign_identity=None,
          entitlements_file=None,
          version=os.path.join(ROOT, 'version.txt'),
          icon=ICON)
