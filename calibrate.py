"""校准工具：采集「平衡报价」「议价」按钮模板，以及报价数字区域。

用法：
  python calibrate.py            # 采集两个按钮模板
  python calibrate.py --price    # 采集报价数字区域（写入 config.json）

步骤：
  1. 打开游戏交易面板，确认「平衡报价」处于可点（亮态）
  2. 回本窗口按 Enter 抓取全屏
  3. 用鼠标左键框选按钮，拖拽出一个矩形后按 Enter 确认
  4. 框选完议价按钮后自动结束，模板写入 templates/
"""
from __future__ import annotations

import json
import os
import sys

import cv2
import numpy as np

import core
import auto_bid
from core import BASE_DIR, TPL_DIR, Screen, load_config

try:
    import pyautogui
except ImportError:
    pyautogui = None


def save_png(name: str, img: np.ndarray) -> None:
    os.makedirs(TPL_DIR, exist_ok=True)
    path = os.path.join(TPL_DIR, name)
    cv2.imwrite(path, img)
    print(f"[已保存] {path}  尺寸 {img.shape[1]}x{img.shape[0]}")


def capture_roi(scr: Screen, prompt: str) -> tuple[np.ndarray, tuple[int, int, int, int]] | None:
    print(prompt)
    input("按 Enter 抓取画面... ")
    frame = scr.grab()
    bgr = frame.copy()

    h, w = bgr.shape[:2]
    win = "Calibrate - drag a box, Enter=confirm, C=cancel"
    bgr_small = bgr if (w <= 1600) else cv2.resize(
        bgr, (1600, int(h * 1600 / w)), interpolation=cv2.INTER_AREA)
    scale = bgr_small.shape[1] / w

    try:
        cv2.namedWindow(win, cv2.WINDOW_NORMAL)
        cv2.imshow(win, bgr_small)
        k = cv2.selectROI(win, bgr_small, showCrosshair=True, fromCenter=False)
        cv2.destroyWindow(win)
        cv2.waitKey(200)
    except cv2.error as e:
        print(f"[错误] OpenCV 窗口异常: {e}")
        print("如果 OpenCV 无 GUI 支持，请手动截图并放到 templates/ 目录。")
        return None

    if k == (0, 0, 0, 0):
        print("[取消] 本次跳过")
        return None

    x, y, rw, rh = [int(v / scale) for v in k]
    x = max(0, min(x, w - 2))
    y = max(0, min(y, h - 2))
    rw = max(2, min(rw, w - x))
    rh = max(2, min(rh, h - y))
    return bgr[y:y + rh, x:x + rw].copy(), (x, y, rw, rh)


def do_price_roi(scr: Screen) -> None:
    """采集「出价 / 要价」数字区域。

    这是脚本的**核心验证区**：点完「平衡报价」后，脚本靠判断这块区域
    是否出现内容（金额/物品图标）来决定「平衡报价」是否真的生效。
    没生效就退避重试，绝不会去点「议价」—— 那会触发
    「确定捐赠这些物品？」白送弹窗。
    """
    print("\n" + "=" * 60)
    print("  采集报价验证区（关键步骤，建议必做）")
    print("=" * 60)
    print("""
在这个交易面板截图里，「出价」和「要价」是面板中间并排的两列数字，
它们的正下方各有一排物品格（你放进去要卖的东西）。

请框选范围：**包含「出价/要价」这两列数字 + 它们下方那排物品格**的矩形。
  - 左边界：贴住「出价」列的左侧
  - 右边界：贴住「要价」列的右侧
  - 上边界：略高于「出价/要价」文字
  - 下边界：包含物品格那一行

为什么要这块：点太快时游戏会吞掉「平衡报价」这次点击，物品格不会被填上，
此时若继续点「议价」，游戏就会弹出「确定捐赠这些物品？」白送确认框。
脚本靠这块区域判断是否填入成功。

框不准也没关系，之后可以重跑本命令。
""")
    r = capture_roi(scr, "请框选报价验证区（出价/要价 两列 + 下方物品格）")
    if r is None:
        print("\n[取消] 未设置 price_roi")
        print("[重要] 不设置时脚本无法验证「平衡报价」是否生效，")
        print("       点击过快时可能出现「确定捐赠这些物品？」弹窗。")
        return
    roi = list(r[1])
    cfg = load_config()
    cfg["price_roi"] = roi
    with open(os.path.join(BASE_DIR, "config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print(f"\n[已写入 config.json] price_roi = {roi}")
    print("现在脚本会：点平衡报价 -> 等这块区域出现内容 -> 确认后才点议价。")

    # 用真实画面自动标定阈值（亮像素占比），避免手填数字不准确
    thr = auto_bid.calibrate_price_threshold(scr, roi, "已填入状态：")
    cfg = load_config()
    cfg["price_filled_ratio"] = thr
    with open(os.path.join(BASE_DIR, "config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print(f"\n[已写入] price_filled_ratio = {thr}")
    print("\n[验证方法] 回到游戏跑 auto_bid.py，看进度行：")
    print("  「平衡报价成功 N/M (xx%)」")
    print("  - 成功率接近 100%  -> 阈值合适")
    print("  - 总是 0% 或很低  -> ROI 框偏了，或阈值偏高，重跑本命令")
    print("  - 总是 100% 但仍弹窗 -> ROI 没包含到真正的报价数字，重框")


def main():
    price_only = "--price" in sys.argv
    scr = Screen()
    print(f"屏幕分辨率: {scr.size[0]}x{scr.size[1]}")
    print("=" * 52)
    print("  模板校准工具")
    print("=" * 52)

    try:
        if price_only:
            do_price_roi(scr)
        else:
            print("\n前提：游戏处于交易/议价面板，窗口无边框或全屏。")
            print("建议分辨率固定，不要在运行脚本时改窗口大小。\n")
            r = capture_roi(scr, "① 框选「平衡报价」按钮（中间偏下那个，亮态可点的）")
            if r:
                save_png("balance_btn.png", r[0])
            else:
                print("跳过 balance_btn.png")
            print()
            r2 = capture_roi(scr, "② 框选「议价」按钮（最下方那个宽按钮）")
            if r2:
                save_png("haggle_btn.png", r2[0])
            print("\n完成。运行  python auto_bid.py  开始循环。")
            print("可选：再运行  python calibrate.py --price  采集报价数字区域（更稳）。")
    except KeyboardInterrupt:
        print("\n已取消")
    finally:
        scr.close()
        if pyautogui is not None:
            pyautogui.FAILSAFE = True


if __name__ == "__main__":
    main()