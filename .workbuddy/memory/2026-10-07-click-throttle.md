# 2026-10-07（补充）

## mouse-script：修复「平衡报价→议价→议价」连击，加 150ms 点击节流

### 用户反馈
第二轮优化（提速 25 倍）后出现异常：点击序列变成
「平衡报价 → 议价 → **议价**」，导致触发其他操作。
用户要求：**确保每次点击之间相隔 150ms**。

### 根因（两个叠加）
1. `skip_move_below_px=160` —— 议价按钮在平衡报价下方约 110px，
   小于阈值，第二次点击直接鼠标瞬移，几乎无移动时间。
2. `step_delay_ms` 被调到 `[15,40]`，加上 `pre_click_delay_ms [5,15]`，
   两次实际点击间隔仅约 15~40ms。游戏把快速连击判定为双击/长按，
   于是「再点一次议价」被当成双击操作，触发非预期行为。

### 设计决策：为什么废弃 step_delay_ms 改为全局节流
原 `step_delay_ms` 只作用于循环内**一处**，「平衡报价被跳过只点议价」
等其它路径不受约束，时序不可控。
改为在 `core.py` 加**全局点击节流器**，与调用路径无关：

```python
_last_click_at = 0.0
def _throttle_click(cfg):
    global _last_click_at
    gap = float(cfg.get("min_click_gap_ms", 150)) / 1000.0
    if gap <= 0: return
    now = time.monotonic()
    wait = gap - (now - _last_click_at)
    if _last_click_at > 0 and wait > 0: time.sleep(wait)
    _last_click_at = time.monotonic()
```

`human_click` 在每次 `pyautogui.click()` 前调用它（double 分支也调用）。
新增 `reset_click_throttle()`，在 `auto_bid.py` 的 finally 中调用。
`step_delay_ms` 从代码和 config 中彻底移除（文档标注废弃）。

### 验证方法（可复用）
**不要只测配置值，要测真实点击时刻**。用 monkeypatch 拦截实际点击：

```python
stamps=[]
orig=pyautogui.click
pyautogui.click=lambda x,y,**k: stamps.append(time.monotonic()*1000)
... 跑流程 ...
gaps=[stamps[i+1]-stamps[i] for i in range(len(stamps)-1)]
print(min(gaps))  # 必须 >= 150
```

实测结果：8 次点击，最小 150.1 / 最大 151.1 / 平均 150.7ms，**违例 0 次**。
另测 4 场景全 PASS：极快连续调用、跨轮次只点议价、double=True 双击、
节流重置后首击不残留阻塞(6ms)。
配置可调性：设 150/200/300/500 实测 150.2/200.2/300.7/500.2ms，精确生效。

### 代价
每轮固定 2 击，单轮最少 `2 × min_click_gap_ms`。
150ms → ~2.6 轮/秒；300ms → ~1.6 轮/秒。

### 教训
提速时不能只看「单步耗时」，必须检查**动作间的时序关系**。
把某个 delay 从 60 调到 15 虽省了 45ms，但破坏了游戏对点击的语义判断。
这类交互安全约束应该做成**机制级强制**（节流器），而不是散落在各处的配置项。
