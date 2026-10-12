"""创建桌面快捷方式：一键启动交易助手。

生成：
  桌面「交易助手.lnk」  -> pythonw launcher.py（无黑窗口，托盘常驻）
  桌面「录制鼠标动作.lnk」-> python launcher.py --record（有控制台，需看 F9 提示）

同时在项目根目录放一个「交易助手.bat」供手动双击（带自检与错误提示）。

用法：python setup_shortcut.py
"""
from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(BASE_DIR, ".venv", "Scripts", "python.exe")
PYW = os.path.join(BASE_DIR, ".venv", "Scripts", "pythonw.exe")
ICON = os.path.join(BASE_DIR, "helper.ico")

BAT = """@echo off
chcp 65001 >nul
cd /d "{base}"
set PY="{py}"
if not exist "%PY%" (
  echo [错误] 找不到虚拟环境，请先安装依赖。
  pause
  exit /b 1
)
start "" /min "{pyw}" "{base}\\launcher.py"
"""


def desktop_dir() -> str:
    buf = ctypes.create_unicode_buffer(260)
    ctypes.windll.shell32.SHGetFolderPathW(None, 0, None, 0, buf)
    return buf.value


def make_icon(path: str) -> bool:
    """生成一个简单的 ICO 图标（金币色圆形 + 播放三角）。"""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return False
    try:
        sizes = [256, 128, 64, 48, 32, 16]
        img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((8, 8, 248, 248), fill=(46, 84, 122, 255),
                  outline=(240, 200, 90, 255), width=10)
        d.polygon([(104, 78), (104, 178), (188, 128)], fill=(250, 235, 190, 255))
        img.save(path, format="ICO", sizes=[(s, s) for s in sizes])
        return True
    except Exception as e:
        print(f"  [警告] 图标生成失败: {e}")
        return False


def create_shortcut(target: str, args: str, link_path: str,
                    workdir: str, desc: str) -> bool:
    """用 COM 接口创建 Windows 快捷方式。"""
    import pythoncom
    from win32com.client import Dispatch
    shell = Dispatch("WScript.Shell")
    lnk = shell.CreateShortCut(link_path)
    lnk.Targetpath = target
    lnk.Arguments = args
    lnk.WorkingDirectory = workdir
    lnk.Description = desc
    lnk.IconLocation = ICON if os.path.exists(ICON) else target
    lnk.WindowStyle = 7      # 最小化启动
    lnk.save()
    return os.path.exists(link_path)


def main() -> int:
    print("=" * 56)
    print("  创建桌面快捷方式")
    print("=" * 56)

    if not os.path.exists(PY):
        print(f"[错误] 找不到 {PY}")
        return 1

    print("1. 生成图标...")
    if make_icon(ICON):
        print(f"   [OK] {ICON}")
    else:
        print("   [跳过] 未生成图标（不影响快捷方式）")

    # 项目根目录的 bat
    bat_path = os.path.join(BASE_DIR, "交易助手.bat")
    with open(bat_path, "w", encoding="gbk", errors="replace") as f:
        f.write(BAT.format(base=BASE_DIR, py=PY, pyw=PYW))
    print(f"2. [OK] {bat_path}")

    dsk = desktop_dir()
    try:
        p1 = os.path.join(dsk, "交易助手.lnk")
        create_shortcut(PYW, f'"{os.path.join(BASE_DIR, "launcher.py")}"',
                        p1, BASE_DIR, "交易助手 - 托盘常驻，F6 开始/停止")
        print(f"3. [OK] {p1}")

        p2 = os.path.join(dsk, "录制鼠标动作.lnk")
        create_shortcut(PY, f'"{os.path.join(BASE_DIR, "launcher.py")}" --record',
                        p2, BASE_DIR, "录制鼠标动作（F9 开始/停止）")
        print(f"4. [OK] {p2}")
    except ImportError:
        print("3. [跳过] 需要 pywin32 才能创建快捷方式")
        print("   请手动右键 交易助手.bat → 发送到 → 桌面快捷方式")
    except Exception as e:
        print(f"3. [警告] 创建快捷方式失败: {e}")
        print("   可手动右键 交易助手.bat → 发送到 → 桌面快捷方式")

    print("=" * 56)
    print("完成。使用方式：")
    print("  · 双击桌面「交易助手」-> 托盘出现图标")
    print("  · 切到游戏，按 F6 开始/停止回放")
    print("  · 按 F10 退出启动器；急停 = 把光标甩到屏幕左上角")
    print("  · 右键托盘图标 -> 菜单里有录制/自检/退出")
    print("=" * 56)
    return 0


if __name__ == "__main__":
    sys.exit(main())
