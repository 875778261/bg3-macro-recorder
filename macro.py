"""录制 / 回放你的鼠标动作。

为什么需要它：图像识别方案每轮都要全屏截图 + 模板匹配（约 100ms），
再加上为避免「点太快被吞」而设的长点击间隔（400ms），单轮约 1.1 秒 ——
比手动点击还慢。

但这两个按钮的位置是**固定的**，视觉识别只在首次定位时有价值。
所以正确做法是：录一次，之后直接回放坐标，用小区域采样做轻量自检。

用法：
  1. 录制：  python macro.py record
     开始后你会看到控制台提示，按 F9 切换录制状态。
     切到游戏，正常点几轮「平衡报价 → 议价」，再按 F9 结束。
     动作会存到 macro.json

  2. 回放：  python macro.py play
     把光标甩到屏幕左上角即停（脚本会把光标移来移去，用别的键会跟游戏冲突）。

特点：
  - 回放时仍会检查「平衡报价」是否可点（金色判据），禁用就跳过等待
  - 仍会检查报价区是否已填入，未填入则退避重试（避免白送弹窗）
  - 仍会兜底处理「确定捐赠这些物品？」弹窗
  - 点击间隔可调（--gap 毫秒），默认 150
"""
from __future__ import annotations

import json
import os
import sys
import time

import numpy as np
import cv2

import core
import dialog
from core import (Screen, TemplateFinder, load_config, panic_stop_pressed,
                  is_game_foreground, foreground_title, user32)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MACRO_FILE = os.path.join(BASE_DIR, "macro.json")
RECORD_KEY_VK = 0x78   # F9

F9_DOWN = 0x78


def key_down(vk: int) -> bool:
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


class ClickListener:
    """监听鼠标点击（F9 开关录制）。"""

    def __init__(self, recorder: bool = False):
        self.recording = recorder
        self.events: list[dict] = []
        self._left_down = False
        self._f9_down = False
        self._t0 = time.monotonic()

    def poll(self):
        """调用一次处理输入事件。非阻塞。"""
        now = time.monotonic()

        # F9 切换录制
        f9 = key_down(F9_DOWN)
        if f9 and not self._f9_down:
            self._f9_down = True
            self.recording = not self.recording
            state = "开始录制" if self.recording else "停止录制"
            print(f"  [F9] {state}"
                  + (f"（已录 {len(self.events)} 个动作）" if not self.recording else ""))
            if not self.recording:
                self._t0 = now
        elif not f9:
            self._f9_down = False

        # 左键按下沿
        left = key_down(0x01)
        if left and not self._left_down:
            self._left_down = True
            if self.recording:
                pt = self._cursor()
                self.events.append({
                    "t": round(now - self._t0, 3),
                    "x": pt[0], "y": pt[1],
                })
                print(f"    录: #{len(self.events):3d}  ({pt[0]}, {pt[1]})  t={now - self._t0:.3f}s")
        elif not left:
            self._left_down = False

    @staticmethod
    def _cursor():
        import ctypes
        import ctypes.wintypes as wt
        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        return (pt.x, pt.y)


def cmd_record() -> int:
    scr = Screen()
    cfg = load_config()
    print("=" * 62)
    print("  鼠标动作录制")
    print("=" * 62)
    print(f"屏幕: {scr.size[0]}x{scr.size[1]}")
    print()
    print("操作步骤：")
    print("  1. 切到游戏，打开交易面板")
    print("  2. 回到本窗口按 F9 开始录制")
    print("  3. 切回游戏，正常点几轮「平衡报价 → 议价」")
    print("  4. 按 F9 停止录制，动作自动保存")
    print()
    print("提示：录 3~5 轮即可，脚本会自动取「平衡报价 → 议价」这一对。")
    print("      录制期间不要按 F8 之类游戏也占用的功能键。")
    print()

    listener = ClickListener(recorder=False)
    print("按 F9 随时开始/停止...（Ctrl+C 退出）")
    try:
        while True:
            listener.poll()
            time.sleep(0.008)      # ~125Hz 轮询，足够捕捉点击
    except KeyboardInterrupt:
        pass

    if not listener.events:
        print("\n[未录到任何动作] 请确认按过 F9 开始录制。")
        return 1

    # 从录制序列里提取「平衡报价 → 议价」这一对
    pair = extract_pair(listener.events)
    if pair is None:
        print(f"\n[失败] 录到 {len(listener.events)} 个动作，但没能识别出「平衡报价→议价」")
        print("       请重录，确保每轮都点两个按钮。")
        return 1

    bal, hag = pair
    data = {
        "balance_click": [bal["x"], bal["y"]],
        "haggle_click": [hag["x"], hag["y"]],
        "recorded_intervals": [round((listener.events[i + 1]["t"]
                                       - listener.events[i]["t"]) * 1000)
                               for i in range(len(listener.events) - 1)],
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(MACRO_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"\n[已保存] {MACRO_FILE}")
    print(f"  平衡报价坐标: {data['balance_click']}")
    print(f"  议价坐标    : {data['haggle_click']}")
    if data["recorded_intervals"]:
        iv = data["recorded_intervals"]
        print(f"  你的点击间隔: {min(iv)}~{max(iv)} ms（平均 {sum(iv) // len(iv)} ms）")
    print("\n现在运行:  python macro.py play")
    scr.close()
    return 0


def extract_pair(events: list[dict]):
    """从点击序列中提取「平衡报价 → 议价」这一对。

    判据：议价按钮在屏幕更下方（y 更大），且两击时间间隔在合理范围。
    """
    if len(events) < 2:
        return None
    # 找出所有相邻对中「前一个在上、后一个在下」且间隔 < 5s 的组合
    for i in range(len(events) - 1):
        a, b = events[i], events[i + 1]
        gap = (b["t"] - a["t"]) * 1000
        if 100 <= gap <= 5000 and abs(b["x"] - a["x"]) < 400 and 0 < (b["y"] - a["y"]) < 600:
            return a, b
    # 退化：取前两击
    return events[0], events[1]


def cmd_play(gap_ms: int = 150) -> int:
    cfg = load_config()
    if not os.path.exists(MACRO_FILE):
        print(f"[错误] 找不到 {MACRO_FILE}")
        print("请先运行:  python macro.py record")
        return 1
    with open(MACRO_FILE, "r", encoding="utf-8") as f:
        macro = json.load(f)

    bal_xy = tuple(macro["balance_click"])
    hag_xy = tuple(macro["haggle_click"])
    scr = Screen()
    finder = TemplateFinder(cfg.get("template_threshold", 0.82),
                            scales=tuple(cfg.get("match_scales", [1.0])))

    # 回放专用轻量参数：坐标已固定，无需长贝塞尔移动和长节流
    play_cfg = dict(cfg)
    play_cfg["move_duration_ms"] = [8, 18]
    play_cfg["pre_click_delay_ms"] = [2, 5]
    play_cfg["skip_move_below_px"] = 0
    play_cfg["click_jitter_px"] = 1

    print("=" * 62)
    print("  鼠标动作回放（录制坐标 + 轻量自检）")
    print("=" * 62)
    print(f"平衡报价: {bal_xy}    议价: {hag_xy}")
    print(f"点击间隔: {gap_ms} ms    急停: 光标甩到屏幕左上角")
    if roi_hint := cfg.get("price_roi"):
        print(f"报价验证区: {roi_hint}（会检查平衡报价是否真的生效）")
    else:
        print("报价验证区: 未配置（建议跑 calibrate.py --price 以获得更稳的防弹窗保护）")
    print("启动: 切回游戏后延迟 3 秒")
    print("=" * 62)

    # 标定价报区（若有）用于验证平衡报价是否生效
    roi = cfg.get("price_roi")
    ratio_thr = cfg.get("price_filled_ratio", 0.01)
    gold_thr = cfg.get("enabled_gold_ratio", 0.25)

    time.sleep(3)
    rounds = 0
    ok_cnt = 0
    fail_cnt = 0
    dlg_cnt = 0
    t0 = time.monotonic()

    def probe_gold(xy):
        """只采 60x60 小块，判按钮是否可点（约 8ms，比全屏快 8 倍）。"""
        x, y = xy
        sw, sh = scr.size          # Screen.size 是 property
        region = {
            "left": max(0, min(int(x) - 30, sw - 60)),
            "top": max(0, min(int(y) - 30, sh - 60)),
            "width": 60, "height": 60,
        }
        img = scr.grab(region)
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        gold = (hsv[:, :, 1] >= 60) & (hsv[:, :, 0] <= 40)
        return float(gold.mean())

    try:
        while True:
            if panic_stop_pressed():
                print("\n[急停] 已停止")
                break
            if cfg.get("require_foreground", True):
                if not is_game_foreground(cfg.get("window_title_keywords", [])):
                    time.sleep(0.2)
                    continue

            # 轻量自检：平衡报价是否可点（约 8ms）
            g = probe_gold(bal_xy)
            if g < gold_thr:
                # 禁用 -> 短暂等待再试，不盲点
                time.sleep(0.15)
                continue

            core.human_click(bal_xy[0], bal_xy[1], play_cfg, throttle=False)

            # 等一小会儿让游戏处理（不抓帧，固定短等待）
            time.sleep(gap_ms / 1000.0)

            # 若配置了报价区，验证是否真的填入
            filled = True
            if roi:
                x, y, w, h = (int(v) for v in roi)
                sw, sh = scr.size
                rx = max(0, min(x, sw - 1))
                ry = max(0, min(y, sh - 1))
                rw = max(1, min(w, sw - rx))
                rh = max(1, min(h, sh - ry))
                img = scr.grab({"left": rx, "top": ry, "width": rw, "height": rh})
                filled = float((cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) > 90).mean()) >= ratio_thr
                if not filled:
                    fail_cnt += 1
                    time.sleep(cfg.get("retry_backoff_ms", 250) / 1000.0)
                    continue

            core.human_click(hag_xy[0], hag_xy[1], play_cfg, throttle=False)

            # 兜底：万一仍弹白送框
            if cfg.get("handle_dialog", True):
                if dialog.dismiss_confirm_dialog(scr, cfg, log=lambda m: None):
                    dlg_cnt += 1
                    fail_cnt += 1

            ok_cnt += 1
            rounds += 1
            if rounds % 50 == 0:
                el = time.monotonic() - t0
                rate = ok_cnt / (ok_cnt + fail_cnt) * 100 if (ok_cnt + fail_cnt) else 0
                print(f"[进度] {rounds} 轮 | {rounds / el:.2f} 轮/秒"
                      f" | 成功率 {rate:.0f}%  | 异常弹窗 {dlg_cnt}")
    except KeyboardInterrupt:
        print("\n[中断] Ctrl+C 停止")
    finally:
        el = time.monotonic() - t0
        if rounds:
            rate = ok_cnt / (ok_cnt + fail_cnt) * 100 if (ok_cnt + fail_cnt) else 0
            print(f"[统计] {rounds} 轮 / {el:.1f}s = {rounds / el:.2f} 轮/秒"
                  f"（{1000 * el / rounds:.0f} ms/轮）  成功率 {rate:.0f}%")
        scr.close()
    return 0


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        print("用法: python macro.py record | play [--gap 150]")
        return 1
    cmd = sys.argv[1].lower()
    if cmd == "record":
        return cmd_record()
    if cmd == "play":
        gap = 150
        if "--gap" in sys.argv:
            try:
                gap = int(sys.argv[sys.argv.index("--gap") + 1])
            except (IndexError, ValueError):
                pass
        return cmd_play(gap)
    print(f"未知命令: {cmd}")
    print("用法: python macro.py record | play [--gap 150]")
    return 1


if __name__ == "__main__":
    sys.exit(main())
