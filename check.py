"""开跑前自检：确认 DPI、截图、前台窗口、模板定位是否正常。

用法：
  python check.py

不点击任何东西，纯只读诊断。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt

import numpy as np
import cv2

import core
from core import Screen, TemplateFinder, foreground_title, is_game_foreground, load_config

OK = "[OK]"
BAD = "[!!]"


def main():
    u = ctypes.windll.user32
    cfg = load_config()
    scr = Screen()
    finder = TemplateFinder(cfg.get("template_threshold", 0.82))

    print("=" * 58)
    print("  环境自检")
    print("=" * 58)

    dpi = u.GetDpiForSystem()
    scale = dpi / 96
    print(f"{OK} DPI: {dpi} ({scale * 100:.0f}%)"
          + ("  <- 非 100%，坐标已自动换算" if abs(scale - 1) > 0.01 else ""))

    sw, sh = scr.size
    print(f"{OK} 截图尺寸: {sw}x{sh}")
    print(f"{OK} pyautogui 尺寸: {tuple(__import__('pyautogui').size())}")
    if (sw, sh) != (u.GetSystemMetrics(0), u.GetSystemMetrics(1)):
        print(f"{BAD} 截图尺寸与系统尺寸不一致，点击会偏移！")

    title = foreground_title()
    print(f"{OK} 前台窗口: {title or '(无)'}")
    kw = cfg.get("window_title_keywords", [])
    fg = is_game_foreground(kw)
    print(f"{'OK' if fg else '[!!]'} 前台校验: {'通过' if fg else '不通过（脚本会等待）'}"
          f"  关键词={kw}")

    hwnd = u.GetForegroundWindow()
    r = wt.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(r))
    print(f"{OK} 窗口矩形: L{r.left} T{r.top} W{r.right - r.left} H{r.bottom - r.top}")

    frame = scr.grab()
    print(f"{OK} 抓帧成功: {frame.shape}")

    mean = float(frame.mean())
    bright = "画面明亮，可能正常" if mean > 25 else f"画面偏暗(均值{mean:.1f})，可能是全屏独占模式"
    print(f"{OK} 画面亮度均值: {mean:.1f}  ({bright})")

    print("-" * 58)
    print("验证闸门（决定会不会弹「确定捐赠这些物品？」）")
    import auto_bid
    roi = cfg.get("price_roi")
    if not roi:
        print(f"{BAD} price_roi 未配置 —— 验证闸门失效！")
        print(f"    -> 必须运行:  python calibrate.py --price")
    else:
        ratio = auto_bid.price_region_state(frame, roi)
        thr = cfg.get("price_filled_ratio", 0.01)
        filled = auto_bid.price_filled(frame, roi, thr)
        print(f"{OK} price_roi = {roi}")
        print(f"{'   ' if filled else '   '}当前亮像素占比 = {ratio * 100:.2f}%  阈值 = {thr * 100:.2f}%"
              f"  -> {'已填入' if filled else '当前为空（正常，交易已完成时属此状态）'}")
    print(f"{OK} price_filled_ratio = {cfg.get('price_filled_ratio', 0.01)}")
    print(f"{OK} 失败退避 = {cfg.get('retry_backoff_ms', 250)}ms  "
          f"重试上限 = {cfg.get('max_balance_retries', 6)}")

    print("-" * 58)
    print("模板检查")
    for key, name in (("balance_btn_template", "balance_btn.png"),
                      ("haggle_btn_template", "haggle_btn.png")):
        tpl = finder.load(name)
        if tpl is None:
            print(f"{BAD} {name}: 缺失  -> 运行 python calibrate.py")
        else:
            print(f"{OK} {name}: {tpl.shape[1]}x{tpl.shape[0]}")

    hag = finder.load("haggle_btn.png")
    if hag is not None:
        hit = finder.locate(frame, "haggle_btn.png")
        if hit:
            print(f"{OK} 议价按钮定位: ({hit[0]}, {hit[1]}) 匹配度 {hit[2]:.3f}")
        else:
            print(f"{BAD} 议价按钮未定位 —— 确认交易面板已打开，或重新校准模板")

    bal = finder.load("balance_btn.png")
    if bal is not None:
        anchor = finder.locate(frame, "haggle_btn.png")
        if anchor:
            full_region = {"left": 0, "top": 0,
                           "width": frame.shape[1], "height": frame.shape[0]}
            import auto_bid
            hit = auto_bid.locate_balance(finder, frame, full_region, anchor)
            if hit:
                enabled = auto_bid.is_balance_enabled(finder, frame, full_region, hit, cfg)
                ratio = auto_bid.gold_ratio(frame, full_region, hit)
                thr = cfg.get("enabled_gold_ratio", 0.25)
                print(f"{OK} 平衡报价定位: ({hit[0]}, {hit[1]}) 匹配度 {hit[2]:.3f}")
                print(f"{OK}   状态 = {'可点' if enabled else '禁用'}"
                      f"   金色像素占比 = {ratio * 100:.1f}%  阈值 = {thr * 100:.0f}%")
                if not enabled and ratio < 0.05:
                    print(f"{BAD}   -> 按钮完全去饱和，确认游戏里该按钮是否为灰色禁用态")
            else:
                print(f"{BAD} 平衡报价未定位（请重新校准 balance_btn.png）")

    # 顺带报告一次稳态定位耗时（第二次调用会走位置缓存的快路径）
    if anchor:
        import time
        finder.set_hint("haggle_btn.png", anchor)
        t = time.perf_counter()
        again = finder.locate(frame, "haggle_btn.png")
        dt = (time.perf_counter() - t) * 1000
        print(f"{OK} 缓存复检耗时: {dt:.0f} ms"
              + ("  (稳态速度参考)" if again else "  (未命中，将回退全屏)"))

    print("=" * 58)
    print("全部 OK 即可运行:  python auto_bid.py")
    print("急停: 鼠标甩到屏幕左上角")
    print("=" * 58)
    scr.close()


if __name__ == "__main__":
    main()