"""主循环：平衡报价 -> 议价，反复执行。

性能（实测 3840x2160 / BG3 全屏）：
  - 稳态每轮 = ROI 抓帧(~8ms) + 局部单尺度匹配(~23ms) + 两次点击
  - 仅在丢失定位时回退全屏多尺度匹配（约 3s，偶发）

安全机制：
  - 鼠标甩到屏幕左上角急停
  - 仅在游戏窗口处于前台时动作
"""
from __future__ import annotations

import time

import numpy as np
import cv2

import core
import dialog
from core import (Screen, TemplateFinder, human_click, rand_delay,
                  panic_stop_pressed, is_game_foreground, foreground_title,
                  reset_click_throttle)

HAGGLE_TPL = "haggle_btn.png"
BALANCE_TPL = "balance_btn.png"


def _to_global(pos, region):
    """把 ROI 帧内的坐标换算回屏幕坐标。"""
    return (pos[0] + region["left"], pos[1] + region["top"], pos[2])


def locate_balance(finder: TemplateFinder, frame: np.ndarray, region: dict, anchor):
    """定位平衡报价按钮（位于议价按钮正上方）。

    稳态走 TemplateFinder 的位置缓存（单尺度局部匹配，~23ms）；
    首次/丢定位时以锚点为基准做窄条区域搜索。
    """
    hit = finder.locate(frame, BALANCE_TPL)
    if hit is not None:
        return _to_global(hit, region)

    tpl = finder.load(BALANCE_TPL)
    if tpl is None:
        return None
    th, tw = tpl.shape[:2]
    ax, ay, _ = anchor
    x0 = max(0, ax - 220 - region["left"])
    y0 = max(0, ay - 200 - region["top"])
    w = min(440, frame.shape[1] - x0)
    h = min(th + 80, frame.shape[0] - y0)
    if w <= tw or h <= th:
        return None
    hit = finder.locate(frame, BALANCE_TPL, use_hint=False,
                        search_region=(x0, y0, w, h))
    return _to_global(hit, region) if hit else None


def blueness(frame: np.ndarray, region: dict, pos, half: int = 30) -> float:
    """返回按钮区域的 (B通道 - R通道) 均值。

    这是判定按钮可用性的可靠特征（实测于 BG3 交易面板）：
      - 可点按钮（如「议价」）：蓝色高亮   B-R ≈ +44
      - 禁用按钮（如「平衡报价」无待平衡项时）：灰褐去饱和  B-R ≈ -26
    分离度约 70。
    """
    x = pos[0] - region["left"]
    y = pos[1] - region["top"]
    x0, y0 = max(0, x - half), max(0, y - half)
    x1 = min(frame.shape[1], x + half)
    y1 = min(frame.shape[0], y + half)
    patch = frame[y0:y1, x0:x1]
    if patch.size == 0:
        return 0.0
    return float(patch[:, :, 0].mean() - patch[:, :, 2].mean())


def gold_ratio(frame: np.ndarray, region: dict, pos, half: int = 30,
               sat_min: int = 60) -> float:
    """返回按钮区域内「高饱和金色像素」占比（0~1）。

    这是判定「平衡报价」可用性的**正确**特征。

    背景（BG3 交易面板）：
      - 可点态：金色描边 + 金色文字，S(饱和度)≈117，金色像素占比 **≈90%**
      - 禁用态：整体灰褐去饱和，S 接近 0，金色像素占比 **≈0%**
    分离度极大，判别非常稳。

    历史教训：曾用 B-R 蓝红差判定，方向完全搞反 —— 「平衡报价」是金色
    （B-R≈-26 为负），被判成禁用，导致脚本一直等待、毫无反应。
    """
    x = pos[0] - region["left"]
    y = pos[1] - region["top"]
    x0, y0 = max(0, x - half), max(0, y - half)
    x1 = min(frame.shape[1], x + half)
    y1 = min(frame.shape[0], y + half)
    patch = frame[y0:y1, x0:x1]
    if patch.size == 0:
        return 0.0
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
    h, s = hsv[:, :, 0], hsv[:, :, 1]
    # 金色/橙色色相区间（H: 0-40），且饱和度足够高
    gold = (s >= sat_min) & (h <= 40)
    return float(gold.mean())


def is_balance_enabled(finder: TemplateFinder, frame: np.ndarray,
                       region: dict, pos, cfg: dict) -> bool:
    """判断「平衡报价」此刻是否可点（是否为金色高亮态）。"""
    val = gold_ratio(frame, region, pos)
    return val >= cfg.get("enabled_gold_ratio", 0.25)


def price_changed(prev, frame, roi) -> bool:
    """报价数字区域帧差，检测数值是否更新。"""
    if prev is None or roi is None:
        return True
    x, y, w, h = roi
    x1, y1 = min(frame.shape[1], x + w), min(frame.shape[0], y + h)
    if x1 <= x or y1 <= y:
        return True
    cur = cv2.cvtColor(frame[y:y1, x:x1], cv2.COLOR_BGR2GRAY)
    old = cv2.cvtColor(prev[y:y1, x:x1], cv2.COLOR_BGR2GRAY)
    if cur.shape != old.shape:
        return True
    return float(np.abs(cur.astype(np.int16) - old.astype(np.int16)).mean()) > 4.0


def price_region_state(frame: np.ndarray, roi, bright_thr: int = 90) -> float:
    """返回报价区域的「亮像素占比」（0~1）。

    用亮像素占比而非平均亮度：金额数字和金币图标只占 ROI 很小比例，
    平均亮度会被大片暗背景稀释，判别窗口极窄（实测仅 5.5，不稳定）。
    亮像素占比对「内容占多少」不敏感，数字一填入就有明确跃升
    （实测 0% -> 3.3%）。
    注意：占比会随 ROI 变大而稀释，所以 price_roi 要框得紧凑。
    """
    x, y, w, h = roi
    x1, y1 = min(frame.shape[1], x + w), min(frame.shape[0], y + h)
    if x1 <= x or y1 <= y:
        return 0.0
    patch = frame[y:y1, x:x1]
    gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
    return float((gray > bright_thr).mean())


def price_filled(frame: np.ndarray, roi, threshold: float) -> bool:
    """报价栏是否已填入内容（亮像素占比超过阈值）。"""
    return price_region_state(frame, roi) >= threshold


def calibrate_price_threshold(scr: Screen, roi, label: str = "") -> float:
    """用真实画面自动标定阈值。

    做法：先在「已填入」状态抓一帧，算出占比，再取其一半作为阈值。
    这样阈值随实际画面自适应，不依赖硬编码数字。
    """
    frame = scr.grab()
    ratio = price_region_state(frame, roi)
    thr = max(0.004, round(ratio * 0.5, 4)) if ratio > 0 else 0.01
    print(f"  {label}当前亮像素占比 = {ratio * 100:.2f}%  ->  建议阈值 = {thr * 100:.2f}%")
    return thr


def wait_price_filled(scr: Screen, cfg: dict, roi, region: dict):
    """点完「平衡报价」后，轮询直到报价栏真的被填入金额。

    这是修复弹窗问题的**核心闸门**：点得太快时游戏会吞掉这次点击，
    报价栏保持空白；此时若继续点「议价」，游戏就会弹出「确定捐赠这些物品？」
    （因为交易里什么都没有 = 白送）。

    判据：`price_region_state()` 的亮像素占比 >= `price_filled_ratio`。

    返回 (frame, 状态)：
      status = 1  已填入，可以点议价
      status = 0  超时仍未填入 —— 判定本次点击失败，不许点议价
      status = -1 急停
    """
    if roi is None:
        # 未配置报价区域时无法验证，退化为「等一小段时间」。
        # 这是降级路径，弹窗仍可能出现 —— 务必跑 calibrate.py --price。
        time.sleep(cfg.get("verify_interval_ms", 120) / 1000.0)
        return None, 1

    threshold = cfg.get("price_filled_ratio", 0.01)
    interval = cfg.get("verify_interval_ms", 60) / 1000.0
    limit = cfg.get("max_verify_wait_ms", 1500) / 1000.0
    t0 = time.time()
    while time.time() - t0 < limit:
        if panic_stop_pressed():
            return None, -1
        cur = scr.grab(region)
        if price_filled(cur, roi, threshold):
            return cur, 1
        time.sleep(interval)
    return None, 0


def wait_price_settle(scr: Screen, cfg: dict, roi, region: dict):
    """（旧接口，保留兼容）报价数字是否发生变化。"""
    if roi is None:
        return None, 1
    prev = scr.grab(region)
    interval = cfg.get("verify_interval_ms", 120) / 1000.0
    limit = cfg.get("max_verify_wait_ms", 2500) / 1000.0
    t0 = time.time()
    while time.time() - t0 < limit:
        if panic_stop_pressed():
            return None, -1
        cur = scr.grab(region)
        if price_changed(prev, cur, roi):
            return cur, 1
        prev = cur
        time.sleep(interval)
    return prev, 0


def wait_for_balance_enabled(scr: Screen, finder: TemplateFinder, cfg: dict,
                             region: dict, anchor):
    """等待「平衡报价」由禁用变为可点。

    返回新的按钮坐标；超时返回 None；急停返回 -1。
    这是修复「议价→议价」连击的关键：禁用时不再跳过去点议价，而是等。
    """
    limit = cfg.get("balance_wait_ms", 5000) / 1000.0
    interval = cfg.get("balance_poll_ms", 80) / 1000.0
    t0 = time.time()
    while time.time() - t0 < limit:
        if panic_stop_pressed():
            return -1
        frame = scr.grab(region)
        bal = locate_balance(finder, frame, region, anchor)
        if bal is not None and is_balance_enabled(finder, frame, region, bal, cfg):
            return bal
        time.sleep(interval)
    return None


def wait_for_state_change(scr: Screen, finder: TemplateFinder, cfg: dict,
                          region: dict, anchor, settle_ms: int = 120):
    """点完议价后，等画面状态稳定（游戏结算动画）再进入下一轮。

    避免在对方回应/面板重绘尚未完成时就发起下一轮动作。
    """
    interval = cfg.get("settle_poll_ms", 40) / 1000.0
    prev = scr.grab(region)
    g0 = cv2.cvtColor(prev, cv2.COLOR_BGR2GRAY)
    quiet = 0
    t0 = time.time()
    while time.time() - t0 < settle_ms / 1000.0:
        cur = scr.grab(region)
        g1 = cv2.cvtColor(cur, cv2.COLOR_BGR2GRAY)
        d = float(np.abs(g1.astype(np.int16) - g0.astype(np.int16)).mean())
        if d < 0.35:
            quiet += 1
            if quiet >= 2:
                return True
        else:
            quiet = 0
        g0 = g1
        time.sleep(interval)
    return False


def acquire_anchor(scr: Screen, finder: TemplateFinder, pad: int):
    """定位议价按钮。

    有位置缓存 -> 只抓 ROI 做单尺度匹配（快）；
    无缓存或失败 -> 全屏多尺度匹配（慢但可自愈）。
    返回 (anchor_global, frame, region, used_fullscreen)
    """
    hint = finder.hint(HAGGLE_TPL)
    if hint is not None:
        region, frame = scr.grab_around(hint[0], hint[1], pad)
        hit = finder.locate(frame, HAGGLE_TPL, use_hint=False,
                            search_region=(0, 0, region["width"], region["height"]))
        if hit:
            return _to_global(hit, region), frame, region, False

    region = {"left": 0, "top": 0, "width": scr.size[0], "height": scr.size[1]}
    frame = scr.grab()
    hit = finder.locate(frame, HAGGLE_TPL)
    return hit, frame, region, True


def main():
    cfg = core.load_config()
    scr = Screen()
    finder = TemplateFinder(cfg.get("template_threshold", 0.82),
                            scales=tuple(cfg.get("match_scales", [1.0])),
                            hint_radius=int(cfg.get("hint_radius_px", 220)))
    roi = cfg.get("price_roi")
    pad = int(cfg.get("roi_pad_px", 320))

    print("=" * 58)
    print("  交易自动化  |  balance + haggle loop")
    print("=" * 58)
    print(f"屏幕分辨率 : {scr.size[0]}x{scr.size[1]}")
    print(f"前台窗口   : {foreground_title() or '(无)'}")
    print(f"匹配尺度   : {finder.scales}   ROI边距: {pad}px")
    print("急停       : 鼠标甩到屏幕左上角")
    print("启动       : 切回游戏后延迟 3 秒自动开始")
    print("=" * 58)

    for name in (BALANCE_TPL, HAGGLE_TPL):
        if finder.load(name) is None:
            print(f"[错误] 缺少模板 {name}")
            print("请先运行:  python calibrate.py")
            scr.close()
            input("按回车退出...")
            return

    time.sleep(3)
    rounds = 0
    balance_hits = 0
    balance_clicks = 0
    balance_fail = 0
    balance_retries = 0
    refinds = 0
    dlg_closed = 0
    idle_rounds = 0
    max_retries = int(cfg.get("max_balance_retries", 6))
    max_rounds = int(cfg.get("max_rounds", 0))
    t_start = time.time()

    try:
        while True:
            if panic_stop_pressed():
                print("\n[急停] 已停止")
                break

            if cfg.get("require_foreground", True):
                if not is_game_foreground(cfg.get("window_title_keywords", [])):
                    time.sleep(0.25)
                    continue

            # --- 清场：若出现「确定捐赠这些物品？」弹窗，说明上一轮
            #     平衡报价没生效（报价栏是空的）。关掉它，然后重做这一轮。
            if cfg.get("handle_dialog", True):
                if dialog.dismiss_confirm_dialog(scr, cfg, log=lambda m: None):
                    dlg_closed += 1
                    balance_retries = 0        # 已回到干净状态，允许重试

            anchor, frame, region, full = acquire_anchor(scr, finder, pad)
            if anchor is None:
                if not full:
                    # ROI 丢失 -> 清缓存，下一轮走全屏自愈
                    finder.forget(HAGGLE_TPL)
                    finder.forget(BALANCE_TPL)
                    refinds += 1
                else:
                    time.sleep(0.4)
                continue
            if full:
                refinds += 1
                # 平衡报价大致在锚点上方，给它一个初始位置提示
                finder.set_hint(BALANCE_TPL, (anchor[0], anchor[1] - 60))

            # --- 平衡报价 ---
            bal = locate_balance(finder, frame, region, anchor)
            if bal is None:
                # 找不到平衡报价：面板状态可能已变，重试本轮（不盲点议价）
                continue

            if not is_balance_enabled(finder, frame, region, bal, cfg):
                # 平衡报价当前禁用 —— 等它变为可点，而不是立刻去点议价。
                waited = wait_for_balance_enabled(scr, finder, cfg, region, anchor)
                if waited == -1:
                    print("\n[急停] 已停止")
                    break
                if waited is None:
                    idle_rounds += 1
                    if idle_rounds == 1 or idle_rounds % 20 == 0:
                        print(f"[等待平衡报价] 已等待 {cfg.get('balance_wait_ms', 5000)}ms "
                              f"仍不可点（本轮跳过）。报价已平衡时游戏会禁用此按钮。")
                    continue
                bal = waited
            idle_rounds = 0

            # === 关键：点完平衡报价必须验证报价栏已填入，未填入说明点击被吞 ===
            balance_retries += 1
            if balance_retries > max_retries:
                print(f"[重试超限] 连续 {max_retries} 次「平衡报价」点击未生效"
                      f"（报价栏始终为空），暂停 1 秒后重试。")
                reset_click_throttle()
                time.sleep(1.0)
                balance_retries = 0
                continue

            human_click(bal[0], bal[1], cfg)
            balance_clicks += 1
            _, filled = wait_price_filled(scr, cfg, roi, region)
            if filled == -1:
                print("\n[急停] 已停止")
                break
            if filled == 0:
                # 报价栏没变化 -> 这次点击被游戏吞掉了。退避后重试，
                # 绝不能点议价（那会弹出「确定捐赠这些物品？」白送确认）。
                balance_fail += 1
                time.sleep(cfg.get("retry_backoff_ms", 250) / 1000.0)
                continue
            balance_hits += 1
            balance_retries = 0

            # --- 报价栏已有金额，此时点议价才会正常完成交易 ---
            human_click(anchor[0], anchor[1], cfg)

            # 保险：若仍弹出白送确认框（说明平衡报价其实还是没生效），
            # 关掉它并把这一轮记为失败，下次会退避重来。
            if cfg.get("handle_dialog", True):
                if dialog.wait_and_dismiss_dialog(
                        scr, cfg,
                        timeout_ms=cfg.get("dialog_wait_ms", 800),
                        interval_ms=cfg.get("dialog_poll_ms", 40)):
                    dlg_closed += 1
                    balance_fail += 1
                    balance_retries = max(1, balance_retries)

            rounds += 1
            if not cfg.get("handle_dialog", True):
                wait_for_state_change(scr, finder, cfg, region, anchor)

            if rounds % 50 == 0:
                el = time.time() - t_start
                total_clicks = balance_clicks + rounds
                rate = (balance_hits / total_clicks * 100) if total_clicks else 0
                print(f"[进度] {rounds} 轮 | {rounds / el:.2f} 轮/秒"
                      f" | 平衡报价成功 {balance_hits}/{balance_clicks} ({rate:.0f}%)"
                      f" | 异常弹窗 {dlg_closed} 次"
                      f" | 全屏定位 {refinds} 次"
                      f" | {1000 * el / rounds:.0f} ms/轮")
            if max_rounds and rounds >= max_rounds:
                print(f"[完成] 达到设定轮数 {max_rounds}")
                break
            rand_delay(cfg, "round_delay_ms")
    except KeyboardInterrupt:
        print("\n[中断] Ctrl+C 停止")
    finally:
        reset_click_throttle()
        el = time.time() - t_start
        if el > 0 and rounds:
            total = balance_clicks + rounds
            rate = (balance_hits / total * 100) if total else 0
            print(f"[统计] 共 {rounds} 轮，用时 {el:.1f}s，"
                  f"平均 {rounds / el:.2f} 轮/秒 ({1000 * el / rounds:.0f} ms/轮)")
            print(f"[统计] 平衡报价点击 {balance_clicks} 次，成功 {balance_hits} 次 "
                  f"({rate:.0f}%)，被吞 {balance_fail} 次")
            if dlg_closed:
                print(f"[统计] 出现白送确认框 {dlg_closed} 次 —— 说明平衡报价点击太快被吞，"
                      f"建议调大 min_click_gap_ms")
        scr.close()


if __name__ == "__main__":
    main()