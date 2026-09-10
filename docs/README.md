# GoAI 技术文档

> 项目：**腾讯围棋 AI 陪练（GoAI）** —— 基于 KataGo 引擎 + 屏幕识别的自动对弈工具。
> 当前版本行棋判定机制为 **金框唯一判据**（腾讯围棋倒计时区金黄色空心框：有框=我方行棋，无框=对方行棋）。

## 文档导航

| 文档 | 内容 | 面向 |
|---|---|---|
| [使用指南 usage.md](usage.md) | 环境依赖、启动命令、UI 操作、头像/网格标定、常见问题 | 上手用户 |
| [架构总览 architecture.md](architecture.md) | 模块划分、数据流管线、外部依赖、文件职责表（含 mermaid 图） | 整体理解 |
| [调用逻辑 call-flow.md](call-flow.md) | UI 拉起 play 进程、主循环状态机、线程/队列、回合翻转、终局/续战 | 二次开发 |
| [识别逻辑 recognition.md](recognition.md) | 网格检测、逐点棋子判定、执色（头像角标）、金框回合、劫检测、悬停/末手噪声 | 算法/调参 |
| [自动化实现 automation.md](automation.md) | 无光标点击（Win32 消息）、坐标映射、自动续战、弹窗应答、长考预分析缓存 | 二次开发 |
| [截图与坐标标定 screenshots-calibration.md](screenshots-calibration.md) | 带框选标注的截图集 + 如何重标定 `GOLD_ROI` / `avatar_calib.json` | 标定/排错 |
| [调优与排错 tuning.md](tuning.md) | `VISITS` 算力取舍、识别失败排查、回合锁窗、已知问题 | 运维 |

## 关键事实速查

- **行棋判定（唯一）**：`winclick.gold_frame_ratio()`，`GOLD_ROI=(80,180,150,210)`（窗口相对像素），阈值 `GOLD_THR=0.094`。有金框占比 ≥ 阈值 = 我方行棋。
- **执色判定（唯一）**：`winclick.avatar_my_color()`，只读我方头像角标框（`avatar_calib.json` 的 `my`）。
- **回合锁窗**：对方落子后锁 15s（`TURN_LOCK_SECS=15.0`），屏蔽视觉误翻，防金框横幅滞后。
- **引擎**：KataGo（常驻 JSON 协议，`katago_client.py`），`--visits` 控制算力（默认 8000，实测 RTX3050 ≈ visits/3000 秒/手）。
- **OCR**：Windows 自带 `Windows.Media.Ocr`（WinRT），由 `ocr_daemon.ps1` 常驻守护；**非 Tesseract**。
- **点击**：`winclick.post_click()` 用 `PostMessageW` 向渲染窗口发鼠标消息，无需移动真实光标。

## 文档与代码一致性

本文档以当前代码为准。**已删除、不再存在的概念**（请勿再使用）：绿框（obox）、徽章（geo）、子数奇偶算术、自学习参考（`_VIS_REF`/`VIS_MINE_T`/`VIS_OPP_T`）、顶部"游戏按钮投影条"。旧 `README.md` / `CLAUDE.md` 中的相关描述已同步清理。
