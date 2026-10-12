"""核心模块：DPI 感知截图、模板匹配、拟人化点击。"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import math
import os
import random
import time

import numpy as np

try:
    import mss
except ImportError:
    mss = None

try:
    import cv2
except ImportError:
    cv2 = None

try:
    import pyautogui
except ImportError:
    pyautogui = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
TPL_DIR = os.path.join(BASE_DIR, "templates")

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
try:
    shcore = ctypes.windll.shcore
except AttributeError:
    shcore = None

# 急停只认「光标甩到屏幕左上角」。曾经还绑过 F8，但 F8 与 BG3 自身
# 快捷键冲突（按下去游戏里也会触发动作），已彻底移除 ——
# 教训：不要为急停绑定游戏也可能占用的功能键。
PANIC_CORNER_PX = 2


# --------------------------------------------------------------------------
# DPI 感知：必须在任何坐标计算之前设置，否则截图坐标与鼠标坐标错位
# --------------------------------------------------------------------------
def enable_dpi_awareness() -> None:
    if shcore is not None:
        try:
            # PER_MONITOR_AWARE_V2 = -4
            if shcore.SetProcessDpiAwareness(2) == 0:
                return
        except Exception:
            pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


enable_dpi_awareness()

if pyautogui is not None:
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------
# 截图
# --------------------------------------------------------------------------
class Screen:
    """mss 截图器，提供 numpy BGR 数组。"""

    def __init__(self):
        self._sct = None
        if mss is not None:
            self._sct = mss.mss()
        self._size = None

    @property
    def size(self):
        if self._size is None:
            if self._sct is not None:
                mon = self._sct.monitors[1]
                self._size = (mon["width"], mon["height"])
            else:
                self._size = pyautogui.size()
        return self._size

    def grab(self, region=None) -> np.ndarray:
        """抓帧。region 为 None 时抓全屏。

        传 region 只抓局部，实测 600x600 区域约 8ms，全屏约 75ms。
        """
        w, h = self.size
        if region is None:
            region = {"left": 0, "top": 0, "width": w, "height": h}
        else:
            region = self._clip(region)
        if self._sct is not None:
            raw = self._sct.grab(region)
            arr = np.frombuffer(raw.rgb, dtype=np.uint8)
            arr = arr.reshape(raw.height, raw.width, 3)
            return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR) if cv2 else arr
        img = pyautogui.screenshot(region=(region["left"], region["top"],
                                          region["width"], region["height"]))
        return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)

    def _clip(self, region: dict) -> dict:
        w, h = self.size
        x = max(0, min(int(region["left"]), w - 1))
        y = max(0, min(int(region["top"]), h - 1))
        cw = max(1, min(int(region["width"]), w - x))
        ch = max(1, min(int(region["height"]), h - y))
        return {"left": x, "top": y, "width": cw, "height": ch}

    def grab_around(self, cx: int, cy: int, pad: int = 320):
        """抓取以 (cx,cy) 为中心、四周留 pad 像素的画面。

        返回 (roi_dict, frame)。比全屏抓帧快约 10 倍。
        """
        region = self._clip({"left": int(cx) - pad, "top": int(cy) - pad,
                             "width": pad * 2, "height": pad * 2})
        return region, self.grab(region)

    def close(self):
        if self._sct is not None:
            self._sct.close()
            self._sct = None


# --------------------------------------------------------------------------
# 模板匹配
# --------------------------------------------------------------------------
class TemplateFinder:
    """模板匹配定位器。

    性能要点（实测于 3840x2160 全屏）：
      - 全屏多尺度匹配一次约 600ms，是主要瓶颈
      - 锚点锁定后，在上次位置附近做单尺度局部匹配约 23ms（快 25 倍）
    因此这里维护每个模板的「上次命中位置」，优先局部搜索，失败再回退全屏。
    """

    def __init__(self, threshold: float = 0.82,
                 scales=(1.0, 0.9, 1.1, 0.8, 1.25),
                 hint_radius: int = 220):
        self.threshold = threshold
        self.scales = tuple(scales)
        self.hint_radius = hint_radius
        self._cache: dict[str, np.ndarray] = {}
        self._last: dict[str, tuple[int, int]] = {}

    def load(self, name: str) -> np.ndarray | None:
        if name in self._cache:
            return self._cache[name]
        path = os.path.join(TPL_DIR, name)
        if not os.path.exists(path):
            return None
        img = cv2.imread(path, cv2.IMREAD_COLOR)
        if img is not None:
            self._cache[name] = img
        return img

    def hint(self, name: str) -> tuple[int, int] | None:
        return self._last.get(name)

    def set_hint(self, name: str, pos) -> None:
        if pos is not None:
            self._last[name] = (int(pos[0]), int(pos[1]))

    def forget(self, name: str) -> None:
        self._last.pop(name, None)

    def _match(self, frame, tpl, scales, ox, oy):
        th, tw = tpl.shape[:2]
        if frame.shape[0] < th or frame.shape[1] < tw:
            return None
        best = None
        for s in scales:
            if s == 1.0:
                t = tpl
            else:
                t = cv2.resize(tpl, (int(tw * s), int(th * s)),
                               interpolation=cv2.INTER_AREA)
            if t.shape[0] > frame.shape[0] or t.shape[1] > frame.shape[1]:
                continue
            res = cv2.matchTemplate(frame, t, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            if best is None or max_val > best[2]:
                best = (max_loc[0] + ox + t.shape[1] // 2,
                        max_loc[1] + oy + t.shape[0] // 2,
                        float(max_val))
            if max_val >= 0.95:
                break
        return best

    def locate(self, frame: np.ndarray, name: str,
               scales=None, search_region=None,
               use_hint: bool = True) -> tuple[int, int, float] | None:
        """返回 (center_x, center_y, score)，未找到返回 None。

        两段式：先在上次命中位置附近单尺度快搜，失败才做全量多尺度搜索。
        """
        tpl = self.load(name)
        if tpl is None:
            return None
        if scales is None:
            scales = self.scales
        fh, fw = frame.shape[:2]
        th, tw = tpl.shape[:2]

        # --- 第一段：上次位置附近局部快搜（单尺度，约 25ms）---
        # 注意：局部框必须完整容纳模板（tw x th），否则 matchTemplate 无法工作。
        if use_hint and not search_region and name in self._last:
            hx, hy = self._last[name]
            r = self.hint_radius
            x0 = max(0, hx - r)
            y0 = max(0, hy - r)
            x1 = min(fw, hx + r)
            y1 = min(fh, hy + r)
            # 向右/向下延伸，保证搜索框至少比模板大出 margin
            need_w = tw + 8
            need_h = th + 8
            if x1 - x0 < need_w:
                x1 = min(fw, x0 + need_w)
                x0 = max(0, x1 - need_w)
            if y1 - y0 < need_h:
                y1 = min(fh, y0 + need_h)
                y0 = max(0, y1 - need_h)
            if x1 - x0 > tw and y1 - y0 > th:
                sub = frame[y0:y1, x0:x1]
                hit = self._match(sub, tpl, (1.0,), x0, y0)
                if hit and hit[2] >= self.threshold:
                    self._last[name] = (hit[0], hit[1])
                    return hit

        # --- 第二段：全量搜索（多尺度，约 600ms/尺度）---
        ox, oy = 0, 0
        if search_region is not None:
            x, y, w, h = search_region
            x = max(0, min(int(x), fw - 1))
            y = max(0, min(int(y), fh - 1))
            w = min(int(w), fw - x)
            h = min(int(h), fh - y)
            if w > tw and h > th:
                frame = frame[y:y + h, x:x + w]
                ox, oy = x, y

        best = self._match(frame, tpl, scales, ox, oy)
        if best and best[2] >= self.threshold:
            self._last[name] = (best[0], best[1])
            return best
        return None

    @staticmethod
    def mean_luma(region: np.ndarray) -> float:
        if region.size == 0:
            return 0.0
        g = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY) if region.ndim == 3 else region
        return float(g.mean())


# --------------------------------------------------------------------------
# 前景窗口校验
# --------------------------------------------------------------------------
def foreground_title() -> str:
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return ""
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def is_game_foreground(keywords: list[str]) -> bool:
    if not keywords:
        return True
    t = foreground_title()
    if not t:
        return False
    t_low = t.lower()
    return any(k.lower() in t_low for k in keywords)


def active_window_pid_title() -> tuple[int, str]:
    hwnd = user32.GetForegroundWindow()
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value, foreground_title()


# --------------------------------------------------------------------------
# 急停
# --------------------------------------------------------------------------
def cursor_in_panic_corner(corner_px: int = PANIC_CORNER_PX) -> bool:
    """光标是否位于屏幕左上角的急停角落。"""
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x <= corner_px and pt.y <= corner_px


def panic_stop_pressed() -> bool:
    """急停：把光标甩到屏幕左上角（唯一触发方式）。

    注意：本函数只回答「现在是否处于急停状态」。它**不知道**会话刚开始
    这回事 —— 若光标本来就停在角落，「启动后第一轮」也会被判成急停。
    需要这种「重新武装」语义的调用方请自行处理
    （launcher._playback_loop 就是这么做的）。
    """
    return cursor_in_panic_corner()


# --------------------------------------------------------------------------
# 拟人化点击
# --------------------------------------------------------------------------
def _bezier(p0, p1, p2, p3, steps):
    pts = []
    for i in range(1, steps + 1):
        t = i / steps
        mt = 1 - t
        x = (mt ** 3 * p0[0] + 3 * mt ** 2 * t * p1[0]
             + 3 * mt * t ** 2 * p2[0] + t ** 3 * p3[0])
        y = (mt ** 3 * p0[1] + 3 * mt ** 2 * t * p1[1]
             + 3 * mt * t ** 2 * p2[1] + t ** 3 * p3[1])
        pts.append((x, y))
    return pts


def human_move(x: int, y: int, duration_ms: tuple[int, int],
               close_px: int = 0) -> None:
    """贝塞尔曲线移动到 (x, y)。

    close_px > 0 时，若当前鼠标与目标距离小于该值则直接瞬移过去 ——
    连续点击附近按钮时（如平衡报价 → 议价）可省掉整段移动时间。
    """
    cur = pyautogui.position()
    p0 = (cur.x, cur.y)
    p3 = (x, y)
    if close_px > 0 and max(abs(p3[0] - p0[0]), abs(p3[1] - p0[1])) <= close_px:
        user32.SetCursorPos(int(p3[0]), int(p3[1]))
        return
    d = max(abs(p3[0] - p0[0]), abs(p3[1] - p0[1])) + 1
    p1 = (p0[0] + (p3[0] - p0[0]) * random.uniform(0.15, 0.35) + random.uniform(-d, d) * 0.12,
          p0[1] + (p3[1] - p0[1]) * random.uniform(0.15, 0.35) + random.uniform(-d, d) * 0.12)
    p2 = (p0[0] + (p3[0] - p0[0]) * random.uniform(0.6, 0.85) + random.uniform(-d, d) * 0.08,
          p0[1] + (p3[1] - p0[1]) * random.uniform(0.6, 0.85) + random.uniform(-d, d) * 0.08)
    steps = max(8, min(40, d // 12))
    pts = _bezier(p0, p1, p2, p3, steps)
    total = random.uniform(duration_ms[0], duration_ms[1]) / 1000.0
    per = total / steps
    for px, py in pts:
        user32.SetCursorPos(int(round(px)), int(round(py)))
        time.sleep(per)


def human_click(cx: int, cy: int, cfg: dict, double: bool = False,
                throttle: bool = True) -> None:
    """拟人化点击。

    throttle=True 时遵守配置的最小点击间隔（min_click_gap_ms），
    无论流程如何调度，两次实际点击之间一定 >= 该值，
    避免游戏把快速连击合并成双击或误判为连续操作。

    throttle=False 时不额外阻塞（由调用方自行控制节奏，如 macro.py 回放）。
    """
    j = int(cfg.get("click_jitter_px", 3))
    x = cx + random.randint(-j, j)
    y = cy + random.randint(-j, j)
    human_move(x, y, cfg.get("move_duration_ms", [20, 45]),
               close_px=int(cfg.get("skip_move_below_px", 160)))
    pre = cfg.get("pre_click_delay_ms")
    if pre:
        time.sleep(random.uniform(pre[0], pre[1]) / 1000.0)
    if throttle:
        _throttle_click(cfg)
    pyautogui.click(x, y)
    if double:
        time.sleep(random.uniform(0.06, 0.13))
        j2 = random.randint(-j, j)
        if throttle:
            _throttle_click(cfg)
        pyautogui.click(x + j2, y + random.randint(-j, j))


# 全局点击节流：记录上一次点击时刻，保证任意两次点击的最小间隔
_last_click_at: float = 0.0


def _throttle_click(cfg: dict) -> None:
    """在真正点击前阻塞，直到距上次点击已超过 min_click_gap_ms。"""
    global _last_click_at
    gap = float(cfg.get("min_click_gap_ms", 150)) / 1000.0
    if gap <= 0:
        return
    now = time.monotonic()
    wait = gap - (now - _last_click_at)
    if _last_click_at > 0 and wait > 0:
        time.sleep(wait)
    _last_click_at = time.monotonic()


def reset_click_throttle() -> None:
    """重置节流状态（急停/重启时调用，避免第一击被上轮残留影响）。"""
    global _last_click_at
    _last_click_at = 0.0


def rand_delay(cfg: dict, key: str) -> None:
    lo, hi = cfg.get(key, [80, 180])
    time.sleep(random.uniform(lo, hi) / 1000.0)