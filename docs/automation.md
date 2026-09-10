# 自动化实现

本文说明工具如何「无感」操控腾讯围棋客户端：无光标点击、坐标映射、自动续战、弹窗应答、长考预分析缓存。

## 1. 无光标点击（Win32 消息注入）

`winclick.post_click(x, y)`（`winclick.py:181`）用 `user32.PostMessageW` 向**渲染窗口**（`_rend`，`find_render_hwnd` `:146` 取得）发送鼠标消息，**不移动真实光标**：

```python
# winclick.py:181
def post_click(x, y):
    user32 = ctypes.windll.user32
    r = ctypes.wintypes.RECT()
    user32.GetWindowRect(_rend, ctypes.byref(r))
    cx, cy = int(x) - r.left, int(y) - r.top           # 屏幕→窗口相对
    lp = ((cy & 0xFFFF) << 16) | (cx & 0xFFFF)         # 坐标打包为 lParam
    user32.PostMessageW(_rend, 0x0200, 0, lp)          # WM_MOUSEMOVE
    user32.PostMessageW(_rend, 0x0201, 0x0001, lp)     # WM_LBUTTONDOWN
    user32.PostMessageW(_rend, 0x0202, 0, lp)          # WM_LBUTTONUP
```

特点：
- 坐标为**屏幕绝对坐标**（由调用方用 `window_rect` 偏移换算）。
- 渲染窗口可能在对局重开后重建 → 失败重试一次 `find_render_hwnd`；仍失败返回 `False`（**点不到就不点**，避免误触其它窗口）。
- 游戏内棋盘交点 (i,j) → 屏幕坐标：`grid_calib` 交点 + `win_x0/win_y0`。

## 2. 按钮 / 弹窗文字点击

- `ocr_buttons(keywords)`（`:206`）：`ImageGrab` 截全窗口 → 存盘 → `ocr_items`（调 `ocr_daemon.ps1`）→ 返回 `[(label, 屏幕中心x, 屏幕中心y, w, h)]`，含关键词过滤。
- `click_label(keyword, extra)`（`:231`）：按文字找到按钮即 `post_click` 中心。
- `click_confirm()`（`:240`）：`click_label('确认', ('确定',))`，用于结算/弹窗确认。
- 主循环弹窗应答 `scan_popups()`：3s 节流识别「求和/数子/认输/和棋」等，自动确认续局。

## 3. 自动续战（--wait-new）

见 [call-flow.md](call-flow.md) §6。`end_or_wait()` 在 `--wait-new` 下：
1. 检测是否已在下一盘（小盘面 `counts<=4`）→ 清 `grid` 缓存与 SGF 记录，局数 +1；
2. 否则每 ~6s OCR 找 `[重新匹配]`，点中后 `click_confirm()`；
3. 连续 10 次未找到改找 `[续战]`；仍找不到则停（`:883`–`:905`）。

> 关键：续战由**主程序内部** `ocr_buttons`/`click_label` 完成，**不依赖 UI 的"游戏按钮投影条"**（该 UI 栏已移除）。

## 4. 对手长考预分析缓存（_prefill_after_opp）

对方落子后、轮到我方之前的等待期，主循环调用 `_prefill_after_opp`（`:1600` 附近）：把「对方刚落的预测手」下进局面，直接让 KataGo 续算我方应手，命中即零延迟落子；否则正常 `analyze_position`。

- 算力阶梯：`PRE_VISITS=350`（`:750`）为基础，对方等待越久算力越深。
- `analyze_position()`（`:398`）封装 KataGo 请求（`id/moves/initialStones/initialPlayer/rules=chinese/komi=7.5`）。

## 5. 落子验证与重试

我方回合落子流程（`:1947` 起）：
1. 提子动画稳定后重读盘面（`:1950`）。
2. **视觉闸**：金框=对方时拦截 ≤1.5s（`:1961`），确认确实轮到我方。
3. 劫检测 `br.detect_ko` → `banned`（`:1999`）。
4. 预热缓存命中或 `analyze_position`（`:2018`/`:2032`）。
5. 引擎建议 `pass` → 终局流程（智能裁判/停一手）（`:2041`）。
6. 点击前重读 + `click_at`（PostMessage）（`:2117`/`:2270`）。
7. **阶梯轮询验证**：0.35/0.45/0.8/1.4s 确认盘面吸收（`:2275`）：
   - 成功 → 翻转轮次、记 SGF/趋势（`:2363`）；
   - 失败 → 重试 / 加入黑名单 / 清缓存 / 终局出口（`:2411`）。

## 6. 落子坐标换算示意

```
GTP 落点 "G3"  →  (列 letter=G→索引j, 行号=3→ i=n-3)
                →  屏幕坐标 = grid 交点(xs[j], ys[i]) + (win_x0, win_y0)
                →  post_click(屏幕x, 屏幕y)
```

网格交点 `xs/ys` 由 `board_reader.read_img` 的 `locate_board`/`refine_grid` 给出（窗口相对），叠加 `grid_calib.json` 的 `win_x0/win_y0` 即得屏幕坐标。
