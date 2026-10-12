"""一键启动器：系统托盘常驻 + 全局热键。

解决「每次都要开终端敲命令」的摩擦。启动后缩到托盘，
不显示控制台窗口，游戏全屏也不会被打扰。

全局热键（任何时候生效，无需切窗口）：
    F6   开始 / 停止回放（单键切换）
    F10  退出启动器

为什么是 F6：F8 与 BG3 自身快捷键冲突（按下会在游戏里也触发动作），
所以急停不再占用任何功能键 —— **把光标甩到屏幕左上角**即为急停
（见 core.cursor_in_panic_corner）。

状态与托盘的一致性（重要）：
    回放会话的每一次结束 —— 用户停止 / 角落急停 / 异常停机 —— 都必须
    经过 _end_session()。它是「运行状态」唯一的改写点，保证托盘不会
    出现「显示运行中、实际已停」的粘滞状态（见 CONTEXT.md）。

托盘图标右键菜单也提供全部功能。

用法：
    python launcher.py            # 启动托盘（推荐，桌面放个快捷方式指向它）
    python launcher.py --record   # 直接进入录制模式
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MACRO_FILE = os.path.join(BASE_DIR, "macro.json")

# 热键虚拟键码
VK_F6 = 0x75
VK_F10 = 0x79

# 退出原因（见 CONTEXT.md「退出原因」）
R_USER = "用户停止"        # F6 / 托盘菜单
R_CORNER = "急停·左上角"   # 光标甩到左上角
R_ERROR = "异常停机"       # 回放过程中抛错

_state_lock = threading.Lock()
_running = False
_thread: threading.Thread | None = None
_stop_evt: threading.Event | None = None      # 当前回放线程的停止信号
_stats = {"rounds": 0, "ok": 0, "fail": 0, "dlg": 0, "t0": 0.0}
_exit_reason = ""                              # 最近一次结束的原因
_toggle_item = None                            # 托盘里的「开始 / 停止」项


def _make_icon(color: str = "#2f6f4f", symbol: str = "▶"):
    """生成托盘图标（无需外部文件）。"""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=color)
    # 画个播放三角
    d.polygon([(26, 20), (26, 44), (45, 32)], fill=(255, 255, 255, 255))
    return img


def _make_icon_stop(color: str = "#a33"):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=color)
    d.rectangle((24, 24, 40, 40), fill=(255, 255, 255, 255))
    return img


def _notify(icon, title, message):
    if not icon:
        return
    try:
        icon.notify(message, title)
    except Exception:
        pass


# --------------------------------------------------------------------------
# 运行状态：唯一的两个改写点 _begin_session / _end_session
# --------------------------------------------------------------------------
def _session_alive() -> bool:
    """回放会话是否真的还活着（不只看标志位，还要看线程）。"""
    t = _thread
    return bool(_running and t is not None and t.is_alive())


def _summary() -> str:
    el = time.monotonic() - _stats["t0"] if _stats["t0"] else 0.0
    r = _stats["rounds"]
    rate = r / el if el > 0 else 0.0
    tot = _stats["ok"] + _stats["fail"]
    sr = (_stats["ok"] / tot * 100) if tot else 0.0
    return f"{r} 轮 / {el:.1f}s = {rate:.1f} 轮/秒，成功率 {sr:.0f}%"


def _sync_menu(icon) -> None:
    """让托盘菜单文字跟随运行状态（失败不影响主流程）。"""
    if icon is None or _toggle_item is None:
        return
    try:
        _toggle_item.text = "停止回放 (F6)" if _running else "开始回放 (F6)"
        icon.update_menu()
    except Exception:
        pass


def _set_tray(icon, running: bool, reason: str = "") -> None:
    """把运行状态画到托盘上（图标 + 标题 + 菜单文字）。"""
    if icon is None:
        return
    if running:
        icon.icon = _make_icon_stop("#a33")
        icon.title = "交易助手 - 运行中 (F6 停止)"
    else:
        icon.icon = _make_icon()
        icon.title = (f"交易助手 - 已停止 · {reason} (F6 开始)" if reason
                      else "交易助手 - 已停止 (F6 开始)")
    _sync_menu(icon)


def _end_session(reason: str, icon=None, detail: str = "",
                 notify: bool = True) -> bool:
    """【唯一收口】运行中 → 已停止。

    回放线程的所有退出路径（用户停止 / 角落急停 / 异常 / 循环自然结束）
    都要走到这里，这样托盘状态与回放会话不会各说各话。
    幂等：不在运行中就什么都不做。
    """
    global _running, _exit_reason
    with _state_lock:
        if not _running:
            return False
        _running = False
        _exit_reason = reason

    text = f"  [停止] {reason}"
    if detail:
        text += f" · {detail}"
    print(text + " · " + _summary())

    _set_tray(icon, running=False, reason=reason)
    if notify:
        _notify(icon, "交易助手 · 已停止",
                f"{reason}{(' · ' + detail) if detail else ''}\n{_summary()}")
    return True


def _begin_session(icon, gap_ms: int) -> bool:
    """【唯一入口】已停止 → 运行中。"""
    global _running, _thread, _stop_evt
    if not os.path.exists(MACRO_FILE):
        _notify(icon, "交易助手", "还没录制！\n请右键菜单 → 录制动作")
        return False
    with _state_lock:
        if _running:
            return False
        _running = True
        _stop_evt = threading.Event()
        evt = _stop_evt

    # 先把托盘画成「运行中」，再启动线程：否则线程若瞬间结束，
    # 它的收口会被这行覆盖回「运行中」，又变成粘滞状态。
    _set_tray(icon, running=True)
    _thread = threading.Thread(target=_playback_loop, args=(evt, gap_ms, icon),
                               daemon=True)
    _thread.start()
    print("  [启动] 回放已开始（F6 停止 / 光标甩到左上角急停）")
    return True


def _playback_loop(stop_evt: threading.Event, gap_ms: int, icon=None):
    """回放主循环（在独立线程里跑）。

    无论以何种方式退出，最后一定经过 _end_session —— 这是
    「托盘状态跟随回放会话」的唯一保证。
    """
    import numpy as np
    import cv2
    import core
    import dialog
    from core import Screen, load_config, is_game_foreground

    reason = R_USER          # 默认：用户按 F6 / 托盘菜单停止
    detail = ""
    corner_armed = False     # 角落急停是否已武装（见下）
    scr = None

    try:
        cfg = load_config()
        with open(MACRO_FILE, "r", encoding="utf-8") as f:
            macro = json.load(f)
        bal_xy = tuple(macro["balance_click"])
        hag_xy = tuple(macro["haggle_click"])

        scr = Screen()
        play_cfg = dict(cfg)
        play_cfg["move_duration_ms"] = [8, 18]
        play_cfg["pre_click_delay_ms"] = [2, 5]
        play_cfg["skip_move_below_px"] = 0
        play_cfg["click_jitter_px"] = 1

        roi = cfg.get("price_roi")
        ratio_thr = cfg.get("price_filled_ratio", 0.01)
        gold_thr = cfg.get("enabled_gold_ratio", 0.25)

        _stats.update(rounds=0, ok=0, fail=0, dlg=0, t0=time.monotonic())

        def probe_gold(xy):
            x, y = int(xy[0]), int(xy[1])
            sw, sh = scr.size
            reg = {"left": max(0, min(x - 30, sw - 60)),
                   "top": max(0, min(y - 30, sh - 60)),
                   "width": 60, "height": 60}
            img = scr.grab(reg)
            hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
            return float(((hsv[:, :, 1] >= 60) & (hsv[:, :, 0] <= 40)).mean())

        # 角落急停的「重新武装」：启动时若光标已经在左上角（用户上一次正是
        # 用甩角落停的），先不武装 —— 否则第一轮循环立刻又命中急停，表现为
        # 「一启动就停」。等光标离开角落一次后再武装。
        corner_armed = not core.cursor_in_panic_corner()

        while not stop_evt.is_set():
            if corner_armed:
                if core.cursor_in_panic_corner():
                    reason = R_CORNER
                    break
            elif not core.cursor_in_panic_corner():
                corner_armed = True

            if cfg.get("require_foreground", True):
                if not is_game_foreground(cfg.get("window_title_keywords", [])):
                    stop_evt.wait(0.2)      # 用 wait 而非 sleep，才能立即响应停止
                    continue

            if probe_gold(bal_xy) < gold_thr:
                stop_evt.wait(0.15)
                continue

            core.human_click(bal_xy[0], bal_xy[1], play_cfg, throttle=False)
            stop_evt.wait(gap_ms / 1000.0)
            if stop_evt.is_set():
                break

            if roi:
                x, y, w, h = (int(v) for v in roi)
                sw, sh = scr.size
                rx = max(0, min(x, sw - 1))
                ry = max(0, min(y, sh - 1))
                img = scr.grab({"left": rx, "top": ry,
                                "width": max(1, min(w, sw - rx)),
                                "height": max(1, min(h, sh - ry))})
                filled = float((cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) > 90).mean()) >= ratio_thr
                if not filled:
                    _stats["fail"] += 1
                    stop_evt.wait(cfg.get("retry_backoff_ms", 250) / 1000.0)
                    continue

            core.human_click(hag_xy[0], hag_xy[1], play_cfg, throttle=False)

            if cfg.get("handle_dialog", True):
                if dialog.dismiss_confirm_dialog(scr, cfg, log=lambda m: None):
                    _stats["dlg"] += 1
                    _stats["fail"] += 1

            _stats["ok"] += 1
            _stats["rounds"] += 1
            if _stats["rounds"] % 50 == 0 and icon:
                el = time.monotonic() - _stats["t0"]
                icon.title = (f"交易助手 - 运行中 {_stats['rounds'] / el:.1f} 轮/秒"
                              f" (F6 停止)")
    except Exception as e:
        reason = R_ERROR
        detail = f"{type(e).__name__}: {e}"
        _notify(icon, "交易助手", f"出错了：{e}")
    finally:
        if scr is not None:
            scr.close()
        # 收口：托盘状态在这里回到「已停止」，无论上面是怎么退出的
        _end_session(reason, icon, detail)


# --------------------------------------------------------------------------
# 对外动作
# --------------------------------------------------------------------------
def start_playback(icon=None, gap_ms: int = 150) -> bool:
    """开始回放。

    若状态卡在「运行中」而线程其实早已消失（历史遗留的粘滞状态），
    先自愈收口再启动 —— 这样按 F6 永远能重新跑起来。
    """
    if _running and not _session_alive():
        _end_session(R_ERROR, icon, "回放线程已消失（状态自愈）", notify=False)
    return _begin_session(icon, gap_ms)


def stop_playback(icon=None, reason: str = R_USER, quiet: bool = False) -> bool:
    """请求停止：只发信号 + 等线程收尾。

    真正的状态收口由回放线程的 finally 完成；线程若已不在，这里兜底收口，
    保证任何情况下都不留下粘滞状态。
    """
    evt = _stop_evt
    t = _thread
    with _state_lock:
        was_running = _running
    if not was_running:
        return False
    if evt is not None:
        evt.set()                    # 唤醒线程，使其尽快退出
    if t is not None and t.is_alive():
        t.join(timeout=1.0)          # 等它收尾（释放截图器 + 收口状态）
    _end_session(reason, icon, notify=not quiet)   # 幂等：线程已收口则空转
    return True


def toggle_playback(icon=None) -> bool:
    """单键切换：运行中 → 停止；已停止 → 开始。（F6 与托盘菜单共用）"""
    if _session_alive():
        return stop_playback(icon, R_USER)
    return start_playback(icon)


def _quit(icon):
    """停止回放并退出启动器。"""
    try:
        stop_playback(icon, R_USER, quiet=True)
    finally:
        icon.stop()


def _keyboard_loop(stop_evt, icon):
    """监听全局热键：F6 启停，F10 退出。

    F6 要求「上一次是抬起」才触发，避免长按反复切换。
    任何异常都不允许本线程静默死亡 —— 那会让用户以为热键「没反应」。
    """
    try:
        user32 = ctypes.windll.user32
        f6_prev = f10_prev = False
        while not stop_evt.is_set():
            f6 = bool(user32.GetAsyncKeyState(VK_F6) & 0x8000)
            f10 = bool(user32.GetAsyncKeyState(VK_F10) & 0x8000)

            # F6：开始 / 停止（单键切换）
            if f6 and not f6_prev:
                toggle_playback(icon)
            f6_prev = f6

            # F10：退出启动器
            if f10 and not f10_prev:
                _quit(icon)
                return
            f10_prev = f10

            time.sleep(0.01)
    except Exception as e:
        print(f"  [警告] 热键监听异常退出：{e}")
        _notify(icon, "交易助手", f"热键已失效：{e}\n（托盘右键菜单仍可用）")


def cmd_record() -> int:
    import macro
    return macro.cmd_record()


def _run_subprocess(script: str) -> None:
    import subprocess
    py = os.path.join(BASE_DIR, ".venv", "Scripts", "python.exe")
    subprocess.Popen([py, os.path.join(BASE_DIR, script)], cwd=BASE_DIR)


def main():
    global _toggle_item

    if "--record" in sys.argv:
        return cmd_record()

    if not os.path.exists(MACRO_FILE):
        print("=" * 58)
        print("  交易助手启动器")
        print("=" * 58)
        print("  还没录过动作。先录制一次：")
        print("    python launcher.py --record")
        print("  （按 F9 开始/停止录制，期间正常点几轮）")
        print("=" * 58)
        time.sleep(3)
        return 1

    import pystray

    _toggle_item = pystray.MenuItem(
        lambda item: "停止回放 (F6)" if _running else "开始回放 (F6)",
        lambda: toggle_playback(icon),
        default=True,
    )
    icon = pystray.Icon(
        "trade_helper",
        _make_icon(),
        "交易助手 - 已停止 (F6 开始)",
        menu=pystray.Menu(
            _toggle_item,
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("录制动作...", lambda: _run_subprocess("macro.py record")),
            pystray.MenuItem("环境自检...", lambda: _run_subprocess("check.py")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出 (F10)", lambda: _quit(icon)),
        ),
    )

    stop_evt = threading.Event()
    th = threading.Thread(target=_keyboard_loop, args=(stop_evt, icon), daemon=True)
    th.start()

    print("=" * 58)
    print("  交易助手已启动（托盘常驻）")
    print("=" * 58)
    print("  F6   开始 / 停止回放（单键切换）")
    print("  F10  退出启动器")
    print("  急停 把光标甩到屏幕左上角")
    print("  右键托盘图标 → 菜单")
    print()
    print("  提示：启动器在后台跑，不占屏幕。切到游戏直接按 F6 即可。")
    print("=" * 58)
    icon.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
