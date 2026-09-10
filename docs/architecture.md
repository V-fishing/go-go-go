# 架构总览

## 1. 模块职责

| 文件 | 职责 | 关键对外接口 |
|---|---|---|
| `katago_ui.py` | tkinter GUI：启动/停止、日志、实时棋盘镜像、模式同步 | `start_play()`、`poll_log/poll_board/poll_state`、`_drain_q` |
| `katago_play.py` | **主控**：参数解析、主循环、回合判定、落子、续战、弹窗应答 | `main()`、`visual_turn()`、`anchor_turn_visual()`、`end_or_wait()`、`read_board_counts()` |
| `winclick.py` | 窗口几何、无光标点击、金框回合判定、头像执色判定、OCR 按钮 | `gold_frame_ratio()`、`avatar_my_color()`、`post_click()`、`ocr_buttons()`、`click_label()` |
| `board_reader.py` | 棋盘网格检测、逐点棋子判定、模式偏移、劫检测、网格标定 | `read_current()`、`stones_legal()`、`detect_ko()`、`set_game_mode()`、`window_rect()` |
| `katago_client.py` | KataGo 引擎常驻通信（JSON 协议） | `KataClient.query()` |
| `katago_suggest.py` | 单次建议/分析调用（UI 观战用，只读） | 分析入口 |
| `avatar_calib.py` | 头像标定：写入 `avatar_calib.json` | 标定交互 |
| `ocr.ps1` / `ocr_daemon.ps1` | Windows OCR 守护（WinRT `Windows.Media.Ocr`） | stdin 图片路径 → stdout JSON 行 |

## 2. 数据流管线（mermaid）

```mermaid
flowchart TD
    A[katago_ui.py<br/>GUI/日志/镜像] -->|spawn 子进程| B[katago_play.py<br/>main]
    B -->|Popen 常驻| C[katago.exe<br/>KataGo 引擎]
    B -->|import| D[board_reader.py<br/>网格/棋子/劫]
    B -->|import| E[winclick.py<br/>金框/头像/点击/OCR]
    B -->|import| F[katago_client.py<br/>JSON 协议]
    E -->|subprocess| G[ocr_daemon.ps1<br/>Windows.Media.Ocr]
    E -->|FindWindow/PostMessage| H[(腾讯围棋渲染窗口)]
    D -->|window_rect| E
    E -->|PostMessage 落子| H
    H -->|屏幕像素| I[PIL ImageGrab 截图]
    I --> D
    I --> E
    F -->|moves/initialStones| C
    C -->|policy/winrate| B
    B -->|stdout Tee| J[katago_ui.log / UI 日志区]
    B -->|每局结束| K[save_game_sgf]
    B -. wait-new .->|ocr_buttons 找[重新匹配]| H
```

## 3. 进程与线程模型

- **UI 进程**（`katago_ui.py`）：独立 tkinter 进程。点「启动」时用 `subprocess` 拉起 `katago_play.py auto ...`；UI 周期性 `poll_log/poll_board/poll_state` 读取子进程 stdout 与 `ui_state.json` 投影，经 `_drain_q` 队列刷新界面。
- **主控进程**（`katago_play.py`）：
  - 主线程跑 `while True` 主循环（读盘 → 判定 → 落子 → 校验）。
  - 后台线程 `_warm` 预热引擎；`color_observer_loop` 每 ~10s 并行采样执色（仅观察，不改判定）。
  - 引擎 `KataClient` 用常驻子进程 + 轻量协议通信。
- **OCR 守护**：`ocr_daemon.ps1` 常驻 PowerShell，被 `winclick` 通过 stdin/stdout 反复复用（协议：`READY` 后逐行 `图片路径 → JSON`）。

## 4. 外部依赖

| 依赖 | 作用 | 接口位置 |
|---|---|---|
| KataGo | 落子分析 | `katago_client.py::KataClient.query` → Popen `katago.exe` |
| Windows.Media.Ocr | 按钮/弹窗/标题文字识别 | `ocr_daemon.ps1` + `winclick.ocr_items` |
| Win32 `user32` | 找窗口、发鼠标消息（无光标点击） | `winclick.find_render_hwnd` / `winclick.post_click` |
| PIL / numpy / cv2 | 截图、ROI 像素分析、模板/网格 | `board_reader.py` / `winclick.py` |

## 5. 关键常量（定义位置）

| 常量 | 位置 | 含义 |
|---|---|---|
| `GOLD_ROI=(80,180,150,210)` | `winclick.py:466` | 金框 ROI（窗口相对像素 x0,y0,x1,y1） |
| `GOLD_THR=0.094` | `winclick.py:467` | 金框判定阈值 |
| `TURN_LOCK_SECS=15.0` | `katago_play.py:742` | 对方落子后回合锁窗 |
| `VISITS=8000`（默认） | `katago_play.py:775` | 每手算力（`--visits` 覆盖） |
| `LETTERS='ABCDEFGHJKLMNOPQRST'` | `katago_play.py:770` | GTP 列名（跳过 I） |
| `MODE_GRID_OFFSET` | `board_reader.py:44` | 各模式网格偏移 |
| `PRE_VISITS=350` | `katago_play.py:750` | 对方回合预热基础算力 |

## 6. 设计要点（当前版本）

- **金框唯一判据**：行棋方只由金框（`gold_frame_ratio`）决定，已删除徽章(geo)/绿框(obox)/子数奇偶算术/自学习（`_VIS_REF`）等全部其它判据，避免多判据矛盾的误翻。
- **执色只读我方角标**：`avatar_my_color()` 只认我方头像角标，对方不参与，降低干扰。
- **锁窗防滞后**：对方落子 → 锁窗 15s → 期间屏蔽视觉翻回合，规避金框横幅滞后导致的重复点击。
- **零光标点击**：全程 `PostMessageW` 注入，不移动真实鼠标，不影响用户其它操作。
