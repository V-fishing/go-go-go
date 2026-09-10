# 使用指南

本工具自动在「腾讯围棋」客户端（微信小游戏/PC 客户端）中对弈：识别棋盘 → 调用 KataGo 求解 → 模拟点击落子。无需修改游戏，纯屏幕识别 + 消息注入。

## 1. 环境依赖

| 依赖 | 说明 | 备注 |
|---|---|---|
| Windows 10/11 | 屏幕坐标与 Win32 消息依赖 Windows | 需在桌面端运行 |
| Python 3.9+ | 运行脚本 | 需 `Pillow`、`numpy`、`opencv-python`、`pywin32` |
| KataGo 引擎 | 落子分析 | 常驻子进程，走 JSON 协议（`katago_client.py` 封装） |
| Windows OCR | 文字识别（按钮/弹窗/标题） | 系统自带 `Windows.Media.Ocr`，**无需安装 Tesseract** |
| 腾讯围棋 | 对局客户端 | 需在对局画面，窗口处于前台 |

> OCR 通过 `ocr_daemon.ps1` 启动一个常驻 PowerShell 进程，从 stdin 逐行读图片路径、输出 JSON 行（含 `text/x/y/w/h`），复用引擎提升速度。

## 2. 安装与校验

```bash
cd e:/game/GoAI/tools
pip install pillow numpy opencv-python pywin32

# 校验 OCR 守护是否可用（应输出 READY 或 ERR_NO_ENGINE）
powershell -NoProfile -File ocr_daemon.ps1   # 输入一个图片路径回车测试，Ctrl+C 退出

# 校验 KataGo 可执行（由 katago_client.py 决定启动参数，确认 katago.exe 路径）
```

## 3. 启动命令

主程序为 `katago_play.py`，由 UI（`katago_ui.py`）一键拉起，也可命令行直跑。

```bash
# 最基本：自动识别执色 + 19 路 + 默认算力，手动起手
python katago_play.py auto --mode friend --visits 500

# 指定执色 / 先手
python katago_play.py black --mode friend --visits 2000
python katago_play.py white --turn black --mode challenge --visits 2000

# 终局后自动点[重新匹配]续下一盘（挂机用）
python katago_play.py auto --mode friend --visits 8000 --wait-new

# 仅观战/单次分析（由 UI 按钮触发，不自动落子）
```

### 命令行参数

| 参数 | 取值 | 说明 |
|---|---|---|
| 位置参数 | `auto` / `black` / `white` | 我方执色；`auto` 用头像角标自动识别 |
| `--turn` | `black` / `white` | 强制指定先手（覆盖 detect）；不传则自动 |
| `--visits` | 整数 | 每手算力（默认 8000）；越小越快越弱 |
| `--size` | 9 / 13 / 19 | 棋盘路数（默认 19） |
| `--mode` | `match`/`challenge`/`ai`/`friend` 等 | 对局模式，影响网格偏移（`MODE_GRID_OFFSET`） |
| `--wait-new` | 开关 | 终局后自动检测结算页并点[重新匹配]续战；可配合 `--max-games` |
| `--max-games` | 整数 | 自动续战上限局数 |
| `--beep` | 开关 | 终局/异常蜂鸣提示 |
| `--detect-first` | 开关 | 起手先检测已落子（残局接入用） |

> 算力与耗时：引擎实测约 `VISITS/3000` 秒/手（RTX3050，GPU 热身后），不含读盘/OCR。2000 以下基本无感。

## 4. UI 操作（katago_ui.py）

UI 顶部一排操作按钮（启动 / 连续观战 / 单次分析 / 停止），左侧日志、右侧实时识别棋盘镜像。

- **模式选择**：下拉框中文显示（好友/匹配/挑战/人机…），选择即实时调用 `br.set_game_mode()` 切换网格偏移。
- **执色**：默认「自动识别」，UI 启动后主程序用头像角标判定；手动指定执色可跳过识别。
- **启动**：点「启动自动落子」→ UI 拉起 `katago_play.py auto ...` 子进程，日志实时回流到左侧。
- **停止**：点「停止」终止子进程。

> 注：早期版本 UI 顶部有「游戏按钮投影条」（OCR 后台识别游戏内按钮并映射点击），该方式已移除；自动续战/重开改由主程序内部 `winclick.ocr_buttons`/`click_label` 完成，不依赖 UI。

## 5. 首次标定（重要）

识别精度取决于两份标定文件，**窗口尺寸或分辨率变化后必须重新标定**：

1. **头像角标** `avatar_calib.json`：记录我方头像角标框（窗口相对像素 `my:[x0,y0,x1,y1]`），用于执色判定。运行 `avatar_calib.py` 跟随提示框选（详见 [screenshots-calibration.md](screenshots-calibration.md)）。
2. **棋盘网格** `grid_calib.json`：记录棋盘在屏幕/窗口中的矩形与路数，用于所有棋子坐标换算。同样由标定脚本写入。

标定偏差会造成「执色判反」「落子点偏」——出现此类现象先重做标定。

## 6. 常见场景

- **挂机连打**：`--mode friend --visits 8000 --wait-new --max-games 50`，摆好对局页让窗口在前台即可。
- **只观战不落子**：UI 选「连续观战」，仅分析不注入点击。
- **残局接入**：`--detect-first` 让起手先读已有棋子。

## 7. 排错

| 现象 | 可能原因 | 处理 |
|---|---|---|
| 启动即 `未找到腾讯围棋窗口` | 客户端未打开 / 未在对局页 | 打开腾讯围棋并进入对局画面，重跑 |
| 执色判反 | `avatar_calib.json` 过期 | 重新跑 `avatar_calib.py` |
| 落子位置偏移 | 分辨率变 / `grid_calib.json` 过期 | 重新标定棋盘 |
| 一直误判对方行棋 | 金框 ROI `GOLD_ROI` 与当前窗口错位 | 见[screenshots-calibration.md](screenshots-calibration.md) 重标定 |
| 续战不点 | 结算页按钮文字不匹配 | 检查 OCR 是否输出 `重新匹配`/`续战`；可临时放宽 `REMATCH_KEYS` |
| OCR 无输出 | 系统缺少 OCR 语言包 | 在系统设置安装「Windows 显示语言」离线包（OCR 用用户语言引擎） |

更细的调参与机制见 [tuning.md](tuning.md) 与 [recognition.md](recognition.md)。
