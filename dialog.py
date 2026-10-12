"""确认对话框处理。

BG3 点「议价」后会弹出确认框：
    「确定捐赠这些物品？」—「你不会从这次交易中获得任何金币。是否确定继续？」  [是] [否]

如果不处理，脚本下一轮会照旧点原位置的「议价」，落点正好在对话框区域，
就会触发非预期操作（观感上表现为「议价 → 议价」的连击）。

处理方式：用蓝色高亮特征定位对话框的「是」按钮（该按钮是纯蓝填充，
B-R 差值很大，特征稳定），再按对称关系算出「否」按钮位置并点击它，取消操作。
"""
from __future__ import annotations

import time

import numpy as np
import cv2

import core

# 对话框的蓝色「是」按钮特征（B - R 通道差）
DIALOG_BLUE_THRESHOLD = 25
DIALOG_MIN_AREA = 5000
DIALOG_MIN_W = 200
DIALOG_MIN_H = 40
DIALOG_MAX_ASPECT = 3.0          # 按钮扁平：宽/高 <= 3，竖长条一律排除
DIALOG_ASPECT_MIN = 2.5          # 宽/高 >= 2.5，过窄的色块也排除


def _blue_yes_button(frame_i16):
    """在屏幕中部找对话框的蓝色「是」按钮，返回 (x,y,w,h) 或 None。"""
    b_minus_r = frame_i16[:, :, 0] - frame_i16[:, :, 2]
    mask = (b_minus_r > DIALOG_BLUE_THRESHOLD).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))

    h, w = mask.shape[:2]
    n, _lab, stats, _cent = cv2.connectedComponentsWithStats(mask)
    best = None
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        if area < DIALOG_MIN_AREA or bw < DIALOG_MIN_W or bh < DIALOG_MIN_H:
            continue
        # 必须是扁平按钮：对话框的「是」宽 411 高 62（比值 ~6.6）
        # 这一条同时排除了左侧圆形图标（竖长条）、物品格高亮等误检
        aspect = bw / max(bh, 1)
        if aspect < DIALOG_ASPECT_MIN or aspect > DIALOG_MAX_ASPECT * 2.5:
            continue
        # 对话框位于屏幕中部；「议价」按钮贴屏幕底部，用 y 上界排除
        if not (int(h * 0.30) < y < int(h * 0.80)):
            continue
        if best is None or area > best[4]:
            best = (x, y, bw, bh, area)
    return best


def _no_button_right(frame_i16, yes_x, yes_y, yes_w, yes_h):
    """在「是」按钮右侧找金色描边的「否」按钮，返回 (cx, cy) 或 None。

    「否」按钮是深棕底 + 金色描边，用 R-B > 10 且亮度不高的特征定位，
    比按比例推算可靠（实测两按钮宽度比约 1.67，不是固定比例）。
    """
    h, w = frame_i16.shape[:2]
    y0 = max(0, yes_y - 40)
    y1 = min(h, yes_y + yes_h + 40)
    x0 = min(w - 2, yes_x + yes_w + 10)
    x1 = min(w, yes_x + yes_w + 900)
    if x1 - x0 < 150 or y1 - y0 < yes_h:
        return None

    sub = frame_i16[y0:y1, x0:x1]
    # 金色描边：R 明显高于 B，但整体亮度不高（深棕底）
    gold = ((sub[:, :, 2] - sub[:, :, 0] > 10) &
            (sub[:, :, 2].astype(np.int16) < 160)).astype(np.uint8) * 255
    gold = cv2.morphologyEx(gold, cv2.MORPH_CLOSE, np.ones((11, 11), np.uint8))
    n, _lab, stats, _cent = cv2.connectedComponentsWithStats(gold)
    best = None
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        if area < 2000 or bw < 150 or bh < yes_h * 0.6:
            continue
        if best is None or area > best[4]:
            best = (x, y, bw, bh, area)
    if best is None:
        return None
    x, y, bw, bh, _ = best
    return (x0 + x + bw // 2, y0 + y + bh // 2)


def find_confirm_dialog(frame: np.ndarray):
    """检测确认对话框。

    返回 None，或 (yes_cx, yes_cy, yes_w, yes_h, no_cx, no_cy)。
    判定依据：屏幕中部出现一条宽而扁的蓝色高亮块（对话框的「是」按钮）。
    """
    f = frame.astype(np.int16)
    yes = _blue_yes_button(f)
    if yes is None:
        return None
    x, y, bw, bh, _area = yes
    no = _no_button_right(f, x, y, bw, bh)
    if no is None:
        return None
    return (x + bw // 2, y + bh // 2, bw, bh, no[0], no[1])


def dismiss_confirm_dialog(scr, cfg: dict, log=print) -> bool:
    """若确认对话框存在，点击「否」关闭它。返回是否处理过。"""
    frame = scr.grab()
    hit = find_confirm_dialog(frame)
    if hit is None:
        return False
    yes_cx, yes_cy, _bw, _bh, no_cx, no_cy = hit
    log(f"[对话框] 检测到确认框，点击「否」({no_cx},{no_cy}) 取消")
    core.human_click(no_cx, no_cy, cfg)
    return True


def wait_and_dismiss_dialog(scr, cfg: dict, timeout_ms: int = 1200,
                            interval_ms: int = 60, log=print) -> bool:
    """点完议价后，等对话框出现并关闭它。

    这是修复「议价→议价」的关键一步：议价后必定弹确认框，
    若不关闭，下一轮的点击会落到对话框上。
    """
    limit = timeout_ms / 1000.0
    step = interval_ms / 1000.0
    t0 = time.time()
    handled = False
    while time.time() - t0 < limit:
        if core.panic_stop_pressed():
            return handled
        frame = scr.grab()
        hit = find_confirm_dialog(frame)
        if hit is not None:
            yes_cx, yes_cy, _bw, _bh, no_cx, no_cy = hit
            log(f"[对话框] 出现确认框 ({int((time.time()-t0)*1000)}ms)，点击「否」")
            core.human_click(no_cx, no_cy, cfg)
            handled = True
            # 关闭后再等一帧确认
            time.sleep(0.25)
            if find_confirm_dialog(scr.grab()) is None:
                return True
            return handled
        time.sleep(step)
    return handled
