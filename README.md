# 腾讯围棋 AI 陪练(GoAI)

本机自动下棋陪练系统:读取微信/QQ 小程序中「腾讯围棋」对局画面,
用 KataGo 提供着法并自动落子,支持 19/13/9 路与围棋精灵、友谊赛等模式。

> 仅用于 AI 房 / 友谊赛等允许自动落子的场景;真人匹配房间请勿启用自动下棋。

## 功能

- **棋盘读盘**:自动定位网格(19/13/9 路),像素级逐点判定棋子;
  容错最后一手标记、悬停预览方块、星位点、倒计时遮罩等干扰;
- **我方执色**:只认头像右上角角标棋子颜色(与文字/推断无关);
- **行棋判定**:落子算术为主(谁落子→轮到对方),绿框视觉信号
  (偏离背景占比 + 运行时自学习两态参考)作为交叉校验与兜底;
- **自动落子**:无光标点击(直接向渲染窗口发消息,不占用物理鼠标);
- **弹窗应答**:和棋拒绝 / 确认对方认输 / 数子同意 / 停一手;
- **自动续战**:`--wait-new` 终局自动等待新局、点击重新匹配并继续;
- **SGF 与趋势**:每手生成 SGF、胜率趋势 jsonl(UI 曲线);
- **UI 面板**:镜像棋盘、识别信息、按钮映射、日志、趋势图。

## 依赖(Windows)

- Python 3.9+(Pillow、numpy;Windows 自带 PowerShell,OCR 使用系统 Tesseract 需
  自行安装 Windows 版 tesseract 并配置,见 `ocr.ps1` / `ocr_daemon.ps1` 顶部注释);
- KataGo 引擎:需在 `katago_play.py` / `katago_client.py` 中配置
  `KATAGO`(katago.exe 路径)与 `CFG`(analysis 配置,建议
  `reportAnalysisWinratesAs = BLACK`)。GPU(如 RTX3050)+ OpenCL 版性能最佳;
- 微信/QQ 宿主:wechatappex / qqappex / qqmini / qq.exe 会自动发现;
  其他宿主可用环境变量 `ZCODE_GO_HOST` 追加进程名(逗号分隔)。

## 快速开始

```bat
cd E:\game\GoAI\tools

:: AI 房自动对局(默认自行识别执色与轮次)
python katago_play.py auto --visits 500

:: 终局自动续战,最多 3 盘
python katago_play.py auto --visits 500 --wait-new --max-games 3

:: 手动指定(执子/轮次/尺寸)——启动识别失败时的手动兜底
python katago_play.py 白 --turn 白 --size 19 --visits 500

:: 图形界面(镜像棋盘 + 日志 + 按钮映射 + 趋势)
python katago_ui.py

:: 对局帧录制 / 读盘回归(修改识别代码后必跑)
python capture_frames.py --out frames
python replay_read.py frames
```

首次运行请先在围棋对局画面(双方头像可见、有行棋徽章)启动;
若角标识别失败,按提示回 UI 手动指定执子/轮次。

## 主要文件

| 文件 | 作用 |
|---|---|
| `katago_play.py` | 主流程(启动/读盘/轮次/落子/终局/续战) |
| `board_reader.py` | 窗口定位、网格检测、逐点棋子判定、悬停直读 |
| `winclick.py` | OCR 助手(守护进程)、角标/行棋徽章检测、点击辅助 |
| `katago_client.py` | KataGo analysis 协议客户端(常驻连接) |
| `katago_ui.py` | Tk 界面:镜像棋盘、日志、趋势、按钮投影 |
| `katago_suggest.py` | 建议/辅助脚本 |
| `ocr.ps1` / `ocr_daemon.ps1` | Windows OCR 单次/常驻守护(PowerShell) |
| `capture_frames.py` / `replay_read.py` | 帧录制与读盘回放回归 |
| `cleanup_files.py` | 临时文件(截图/日志)自动清理 |
| `avatar_calib.py` | 头像角标校准(框选双方头像区域) |
| `flowchart.html` | 系统流程图(离线可看,依赖本地 mermaid.min.js) |

## 目录约定

- `frames/`、`frames_anomaly/`、`analysis_logs/`、`games/`、`*.log/*.jsonl`
  均为运行期产物,已在 `.gitignore` 中排除;`frames/` 为有意保留的回放语料,
  定期手动清理(约 260KB/帧)。
- `avatar_calib.json`、`grid*.json` 为个人环境标定,不入库,换机器后重跑
  `avatar_calib.py`(或用全自动检测)。

## 已知说明

- 行棋视觉信号(绿框)在两布局实测零重叠:我方≈0.10-0.15 / 对方≈0.16-0.20,
  绝对值随布局漂移,靠运行时自学习参考取中点分界;换环境后如出现误判,
  观察日志 `[视觉行棋] 绿框: ... 异0.xx` 两态值,可手动调整
  `katago_play.py` 顶部 `VIS_MINE_T` / `VIS_OPP_T`;
- 对局中对方长考时主循环会在对方回合做低算力预分析(350→800→2000→满配),
  轮到己方时直接用缓存,体感延迟明显降低;
- 若窗口移动/缩放,点击坐标自动跟随(每次点击重新解析渲染窗)。
