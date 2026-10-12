"""launcher 运行状态机回归测试（无头，不碰真实屏幕 / 鼠标）。

为什么需要它：2026-10-12 的故障是「回放线程悄悄退出、托盘状态没跟上」，
现象是「停了之后再也起不来」。这类问题在真实游戏里很难复现（要先甩鼠标、
再按热键、再看托盘），但用假的 core / dialog 替换真实实现后，可以在
一秒内跑完所有分支，而且**不会移动鼠标、不会点击、不会截图**。

跑法：
    .venv/Scripts/python.exe test_state_sync.py

覆盖：
    1. 角落急停 → 状态同步 + 能再次启动（用户报告的原症状）
    2. 角落急停的「重新武装」（启动时光标已在角落不应秒停）
    3. 粘滞状态自愈（状态说运行中、线程已死）
    4. _end_session 幂等
    5. 热键监听线程不会因未导入 ctypes 而静默死亡
"""
import importlib.util
import os
import sys
import threading
import time
import types

import numpy as np

LCH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "launcher.py")

FAILS = []
PASSES = []


def check(name, cond, extra=""):
    (PASSES if cond else FAILS).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' — ' + extra) if extra else ''}")


# ---------------------------------------------------------------- 假 core
class Script:
    """脚本化的「光标是否在左上角」。"""

    def __init__(self, value=False):
        self.value = value
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.value


CORNER = Script(False)
CLICKS = []


class FakeScreen:
    def __init__(self):
        self.closed = False

    @property
    def size(self):
        return (3840, 2160)

    def grab(self, region=None):
        # 高饱和金色 + 高亮度：让 probe_gold 判为「可点」
        return np.full((60, 60, 3), (0, 200, 255), dtype=np.uint8)

    def close(self):
        self.closed = True


def fake_human_click(x, y, cfg, double=False, throttle=True):
    CLICKS.append((x, y))          # 只记录，绝不真的动鼠标


fake_core = types.ModuleType("core")
fake_core.Screen = FakeScreen
fake_core.load_config = lambda: {
    "require_foreground": False,
    "price_roi": None,           # 跳过报价验证，循环更快
    "handle_dialog": False,
    "enabled_gold_ratio": 0.25,
    "window_title_keywords": [],
}
fake_core.is_game_foreground = lambda kw: True
fake_core.cursor_in_panic_corner = CORNER
fake_core.human_click = fake_human_click
fake_core.panic_stop_pressed = CORNER

fake_dialog = types.ModuleType("dialog")
fake_dialog.dismiss_confirm_dialog = lambda scr, cfg, log=None: False

sys.modules["core"] = fake_core
sys.modules["dialog"] = fake_dialog

spec = importlib.util.spec_from_file_location("lch", LCH)
L = importlib.util.module_from_spec(spec)
spec.loader.exec_module(L)
print(f"launcher 已加载（ctypes 绑定: {'ctypes' in vars(L)}）\n")


def wait_for(pred, timeout=3.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------- 1. 用户报告的场景
print("1) 核心回归：角落急停后，状态必须同步且能重新启动")
CORNER.value = False
L._end_session(L.R_USER)                       # 确保从「已停止」开始
started = L.start_playback(None, gap_ms=10)
check("start_playback 返回 True", started)
check("会话存活", L._session_alive())

# 等回放真的跑起来（线程里 import cv2 要花点时间）再甩鼠标，
# 否则「启动时就已在角落」会被重新武装逻辑正确拦下，测不到急停本身。
check("回放已进入循环", wait_for(lambda: L._stats["rounds"] > 0))

CORNER.value = True                            # 用户把光标甩到左上角
check("角落急停后 _running 归位", wait_for(lambda: not L._running),
      f"reason={L._exit_reason!r}")
check("停因 = 急停·左上角", L._exit_reason == L.R_CORNER, repr(L._exit_reason))
check("点击确实发生过（回放真的在跑）", len(CLICKS) > 0, f"{len(CLICKS)} 次")

# 关键：这就是以前「无法再次开始」的地方
CORNER.value = False
restarted = L.start_playback(None, gap_ms=10)
check("急停后能再次启动（不再粘滞）", restarted and L._session_alive())
L.toggle_playback(None)                        # F6 第二次按下 = 停止
check("toggle 停止生效", wait_for(lambda: not L._running))
check("停因 = 用户停止", L._exit_reason == L.R_USER, repr(L._exit_reason))


# -------------------------------------------- 2. 角落急停的「重新武装」
print("\n2) 重新武装：启动时光标就在角落，不能被自己立刻停掉")
CORNER.value = True
L.start_playback(None, gap_ms=10)
time.sleep(0.5)
check("光标一直在角落时仍保持运行（不秒停）", L._running and L._session_alive())

CORNER.value = False                           # 光标离开角落一次 → 武装
time.sleep(0.12)
CORNER.value = True                            # 再甩回去 → 应当急停
check("武装后再甩角落，急停生效", wait_for(lambda: not L._running),
      f"reason={L._exit_reason!r}")
check("停因 = 急停·左上角", L._exit_reason == L.R_CORNER, repr(L._exit_reason))


# ------------------------------------------------- 3. 粘滞状态自愈
print("\n3) 自愈：状态说「运行中」但线程已死，仍能重新开始")
dead = threading.Thread(target=lambda: None)
dead.start()
dead.join()
L._running = True                              # 人为制造历史遗留的粘滞状态
L._thread = dead
L._exit_reason = ""
ok = L.start_playback(None, gap_ms=10)
check("自愈并成功启动", ok and L._session_alive())
L.toggle_playback(None)


# ------------------------------------------------- 4. _end_session 幂等
print("\n4) 收口函数幂等（线程收口 + 调用方兜底可共存）")
check("已停止时 _end_session 返回 False", L._end_session(L.R_USER) is False)


# ------------------------------------------------- 5. 热键线程不再秒死
print("\n5) 热键：ctypes 已导入，监听线程不得静默死亡")


class FakeIcon:
    def notify(self, *a, **k):
        pass

    def stop(self):
        pass


errs = []
threading.excepthook = lambda a: errs.append(f"{a.exc_value}")


def run_kb():
    ev = threading.Event()
    t = threading.Thread(target=L._keyboard_loop, args=(ev, FakeIcon()), daemon=True)
    t.start()
    time.sleep(0.35)
    alive = t.is_alive()
    ev.set()
    return alive


check("热键监听线程存活", run_kb(), f"异常: {errs or '无'}")

print("\n" + "=" * 56)
print(f"通过 {len(PASSES)} / {len(PASSES) + len(FAILS)}")
if FAILS:
    print("失败项：" + "; ".join(FAILS))
print("=" * 56)
sys.exit(1 if FAILS else 0)
