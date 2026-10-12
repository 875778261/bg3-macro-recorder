# 2026-10-12 · 托盘状态不同步（粘滞状态）+ 全局热键全程失效

## 用户报告

脚本开始播放后，把鼠标甩到左上角点击 → 脚本停了；
但托盘仍显示「运行中」，再点「开始回放」无反应；
必须先右键托盘 → 停止，才能重新开始。

## 实测确认：两个真 bug

### Bug A — 回放线程退出后不复位状态（粘滞状态）

`launcher.py::_playback_loop` 有三条退出路径，**没有一条**会把 `_running` 置回
False 或刷新托盘图标：

- `while` 条件不再成立（第 105 行）
- `if panic_stop_pressed(): break`（第 106–107 行）← 左上角/F8 急停走的正是这条
- `except Exception` / `finally`（第 148–152 行）

全文件 `_running = False` 只出现在 `stop_playback`（第 180 行）。
线程死了但没人调用 `stop_playback` → `_running` 永为 True
→ 托盘菜单「开始回放」在 `start_playback` 第 158 行被静默 `return`
→ 用户看到「怎么点都不动」。这与用户描述完全吻合。

### Bug B — `ctypes` 未导入，全局热键线程启动即崩溃

`launcher.py:201` 用 `ctypes.windll.user32`，但文件里**没有** `import ctypes`
（只有 json/os/sys/threading/time）。用 venv Python 实测探针：

```
[A] 热键线程存活: False | 线程内异常: NameError: name 'ctypes' is not defined
```

→ F9/F8 全局热键**从一开始就从来没生效过**，用户只能靠托盘菜单操作。
这解释了用户为什么说「必须先右键交易助手停止」而不是「必须先按 F9」。

## 附带发现

急停的角落判据是 `pt.x <= 2 and pt.y <= 2`（`core.py::panic_stop_pressed`）。
用户甩到角落停止后光标仍留在 (0,0)，此时若重启回放，第一轮循环立刻又命中急停
→ 秒停（即使状态修好了，也会表现为「一起来就停」）。需要在重启语义上做处理。

## 待用户裁决（grill-with-docs 第 1 轮，未动手改代码）

1. 状态权威归谁（会话自持生命周期 / 看门狗 / 统一收口）
2. Bug B 是否一并修
3. 角落急停的重启宽限语义
4. 退出原因是否区分并通知

## 已产出

- `CONTEXT.md`（项目统一语言：回放会话 / 运行状态 / 退出原因 / 急停 / 粘滞状态）

---

## 用户裁决结果（同日）

- Q1 状态权威 → 收口函数（推荐 C）
- Q2 热键 → **F8 与游戏冲突，改 F6 单键切换**（用户指定）
- Q3 角落重启语义 → 重新武装（推荐 A）
- Q4 退出原因 → 区分并通知（推荐 A）

## 已修复

| 改动 | 文件 |
|---|---|
| `_begin_session` / `_end_session` 成为运行状态唯一改写点，回放线程 `finally` 必经收口 | launcher.py |
| `start_playback` 自愈粘滞状态；托盘菜单合成单个会变文字的切换项 | launcher.py |
| 模块级 `import ctypes`；热键线程加异常兜底（不再静默死亡） | launcher.py |
| F6 单键切换 / F10 退出；角落急停重新武装 | launcher.py、core.py |
| `panic_stop_pressed` 去掉 F8 判断，新增 `cursor_in_panic_corner`；删 `PANIC_KEY_VK` | core.py |
| F8 相关文案清理 | macro.py、auto_bid.py、check.py、setup_shortcut.py、README.md、使用说明.md |
| 交接文档 §7 新增教训 ⑩⑪、新增 §14 修复记录 | HANDOFF.md |
| ADR 0001 / 0002 | docs/adr/ |
| 无头回归测试（15 项） | test_state_sync.py |

## 验证

`.venv\Scripts\python.exe test_state_sync.py` → **15/15 通过**（用假 core/dialog，
不碰真实鼠标与屏幕）。另做了全部 .py 的编译 + 导入冒烟测试，均 OK。
用户侧的实际手感（按 F6、甩角落）仍需用户自己在游戏里试一次。

