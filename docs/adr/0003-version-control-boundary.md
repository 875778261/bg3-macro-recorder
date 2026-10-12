# 入库边界：仓库是「作者本机工作状态」的记录，不是可分发的发行包

**背景**：首次把项目纳入 git 并推送到 GitHub（public）时，必须划清哪些文件算
「工程的一部分」、哪些算「本机产物」。工作树里混着三类东西：

1. 源码与文档（`*.py` / `*.md` / `docs/`）
2. **本机专用资产**：`config.json` 的 `price_roi = [1718,367,403,247]`、
   `macro.json` 的录制坐标 `[2025,1947] / [2029,2069]`、`templates/*.png`
   —— 全部绑定 3840x2160 + DPI 175% 这一块屏幕
3. **agent 工作记忆**：`.workbuddy/memory/`（逐日记录，含「血泪教训」的原始版本）

**决定**：

1. **忽略**「可由工具重建」的产物：`.venv/`、`__pycache__/`、`*.py[cod]`、
   `build/`、`*.lnk`、OS 与编辑器杂物。虚拟环境 200MB+，重建即可。
2. **入库本机专用资产**（`config.json` / `macro.json` / `templates/*.png`）。
   理由：仓库的定位是**作者本机可运行工程的版本记录**。入库后 clone 下来
   立刻能跑 `check.py`，且「当时可用的参数快照」有历史可查 —— 出问题时能
   对照「上次好用的是哪组阈值」。
3. **入库 `.workbuddy/memory/`**。它与 `HANDOFF.md` 的精炼版互为详略：
   `HANDOFF.md §7` 是结论，memory 是推导过程。用户明确要求不忽略。
4. `*.bat` 声明为 `-text`（见 `.gitattributes`）—— `交易助手.bat` 是 GBK 编码，
   不能让 git 的 autocrlf 改写它的行尾。

**考虑过的替代方案**：把仓库做成「可分发工具」—— 忽略 `config.json` /
`macro.json` / `templates/`，改为提交 `*.example.json`，并在 README 写明
「首次先跑 `calibrate.py`」。**拒绝的原因**：本项目是单人自用，坐标与阈值就是
它的核心资产；拆成 example 只是平白多一步「先标定」的门槛，而标定又依赖同一块
屏幕 —— 对唯一下一个使用者（未来的自己）没有任何收益。

**后果**：

- clone 到不同分辨率 / DPI 的机器上，`price_roi` 与 `macro.json` 坐标**直接失效**，
  必须重跑 `calibrate.py` 与 `macro.py record`（见 `HANDOFF.md` §9）。
- **若将来要对外开放**，本决定需要重新审视：`templates/*.png` 是 BG3 按钮 UI 的
  裁切图，放在 public 仓库里有版权灰区（当前体量 45KB、风险极低，故暂不处理）。
- 入库边界是**很难回头**的决定：一旦这些文件进了历史，再想移除就得重写历史。
  所以它值得被记下来，而不是留在「当初随手设的 .gitignore」里。
