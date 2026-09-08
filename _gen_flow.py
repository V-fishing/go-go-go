# -*- coding: utf-8 -*-
"""生成离线 HTML 流程图(纯 CSS, 无 JS/无外网依赖)"""
import html as H

# 流程数据: (类型, 文本, 子分支[(条件, [步骤...]), ...])
# 类型: start/step/decision/end/note

F = [
    ("start", "启动 katago_play", []),
    ("step", "参数解析: 执色 黑/白/auto · --turn · --visits · --size · --wait-new · --beep", []),
    ("step", "引擎后台预热", []),
    ("decision", "等待游戏窗口 ≤30s (动态 PID 解析)", [
        ("超时", [("end", "提示: 请打开对局画面后重试", [])]),
        ("找到", [
            ("step", "读取初始盘面 (连续两次一致, ≤15 次)", []),
            ("decision", "执色识别 (auto?)", [
                ("是", [("step", "头像角标颜色 · 2帧投票 × 8次重试", []),
                        ("decision", "识别成功?", [
                            ("成功", [("step", "我方 = 黑/白", [])]),
                            ("失败", [("end", "停止: 页面诊断 + UI 人工兜底对话框", [])]),
                        ])]),
                ("否(手动指定)", [("step", "我方 = 黑/白", [])]),
            ]),
            ("decision", "场景安检: 复盘/棋谱/结算特征词?", [
                ("是", [("end", "拒绝启动 (--force-any 可绕过)", [])]),
                ("否", [
                    ("decision", "行棋方锚定", [
                        ("已指定 --turn", [("step", "采用指定轮次", [])]),
                        ("未指定", [("step", "读横幅 黑方/白方行棋 (分级放大 ×14 重试)", []),
                                    ("decision", "读到了?", [
                                        ("成功", [("step", "轮到 = 横幅", [])]),
                                        ("失败", [("end", "停止: 诊断提示 + 人工兜底", [])]),
                                    ])]),
                    ]),
                    ("step", "清趋势文件, 记录第 0 点", []),
                    ("step", "▼ 进入主循环 ▼", []),
                ]),
            ]),
        ]),
    ]),
    # ---------------- 主循环 ----------------
    ("step", "主循环每轮: 读盘 (尺寸OCR缓存 + 网格 + 悬停屏蔽)", []),
    ("decision", "棋盘尺寸变化?", [
        ("是", [("decision", "连续两轮同尺寸?", [
            ("否", [("step", "跳过本轮 (防过渡垃圾帧)", [])]),
            ("是", [("step", "清网格缓存 + 重置基准", [])]),
        ])]),
        ("否", [
            ("decision", "盘面变化? (双读确认, 开局三读)", [
                ("无新增子/跳变>3", [("step", "忽略 (悔棋/撤销/垃圾帧)", [])]),
                ("接受落子", [
                    ("step", "记 SGF · 提子动画窗口 · 按落子方算术翻转轮次 · 横幅交叉校验 · 趋势记录", []),
                    ("decision", "轮到谁?", [
                        ("轮到对方", [("step", "等待: 每4s横幅轮询 (双读切换); 横幅不可读→继续等", []),
                                      ("step", "心跳每15s: 对方已思考 N 秒", [])]),
                        ("轮到我方", [
                            ("step", "提子动画稳定重读", []),
                            ("decision", "行动前横幅闸门", [
                                ("横幅确认", [("step", "继续落子流程", [])]),
                                ("不可读 <20s", [("step", "等下一轮再读", [])]),
                                ("不可读 ≥20s", [("step", "按落子推算谨慎落子 (点错会被校验拦截)", [])]),
                                ("横幅≠推算 且刚落子<3s", [("step", "信推算 (动画旧值)", [])]),
                            ]),
                            ("step", "引擎分析: 预分析缓存命中→零等待; 否则按等待时长阶梯算力", []),
                            ("decision", "着法在网格内且目标为空?", [
                                ("否", [("step", "清缓存重识别 / 改选他处", [])]),
                                ("是", [
                                    ("step", "点击前重读 (失败快速重试3次)", []),
                                    ("step", "PostMessage 点击 (每次现找渲染窗口)", []),
                                    ("decision", "落子验证 (阶梯轮询 ≤4次)", [
                                        ("成功", [("step", "更新状态 · 趋势记录 · SGF 滚动保存", [])]),
                                        ("失败", [
                                            ("decision", "劫争/连续失败?", [
                                                ("否", [("step", "回到校验/换点", [])]),
                                                ("是", [("step", "加入黑名单改选 · 8s轮 · 清缓存", []),
                                                        ("decision", "超过失败上限?", [
                                                            ("否", [("step", "重新分析", [])]),
                                                            ("是", [("step", "→ 终局出口", [])]),
                                                        ])]),
                                            ]),
                                        ]),
                                    ]),
                                ]),
                            ]),
                        ]),
                    ]),
                ]),
            ]),
        ]),
    ]),
    # ---------------- 终局 ----------------
    ("decision", "引擎建议停一手?", [
        ("胜率≥98% 或盘面下满", [("step", "点[智能裁判] + 确认", [])]),
        ("胜率90-98%", [("decision", "ownership 盘面定型检查", [
            ("定型", [("step", "点[智能裁判] + 确认", [])]),
            ("未定型", [("step", "不采纳, 继续下", [])]),
        ])]),
        ("胜率不足", [("step", "不采纳, 继续下", [])]),
    ]),
    ("step", "结算出现 → 终局出口 end_or_wait: 存时间戳 SGF", []),
    ("decision", "--wait-new?", [
        ("否", [("end", "停止", [])]),
        ("是", [("step", "等结算页 → 点[重新匹配] (≤10分钟)", []),
                ("decision", "检测到新局?", [
                    ("超时", [("end", "停止", [])]),
                    ("是", [("step", "清 SGF/趋势状态", []),
                            ("step", "新局: 角标核对执色 (分先换色自动切换); 黑先观察/白等待", []),
                            ("step", "▲ 回到主循环 ▲", [])]),
                ])]),
    ]),
    ("end", "异常兜底: 窗口消失等30s · 连续失败走出口 · Ctrl+C/UI停止存棋谱 · 每10分钟自动清理临时文件", []),
]


def render(items, depth=0):
    out = []
    for typ, text, branches in items:
        cls = {'start': 'start', 'end': 'end', 'decision': 'decision',
               'step': 'step'}.get(typ, 'step')
        out.append(f'<div class="node {cls}">{H.escape(text)}</div>')
        if branches:
            out.append('<div class="branch">')
            for cond, sub in branches:
                out.append(f'<div class="cond">{H.escape(cond)}</div>')
                out.append('<div class="sub">')
                out.extend(render(sub, depth + 1))
                out.append('</div>')
            out.append('</div>')
        else:
            out.append('<div class="arrow">&#9660;</div>')
    return out


html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<title>腾讯围棋 AI 陪练 - 完整流程图(离线版)</title>
<style>
 body {{ background:#f5f4ee; margin:20px; font-family:"Microsoft YaHei",sans-serif; }}
 h1 {{ font-size:20px; color:#333; }}
 .flow {{ max-width:860px; margin:0 auto; background:#fff; padding:22px;
         border-radius:12px; box-shadow:0 1px 8px rgba(0,0,0,.12); }}
 .node {{ border:2px solid #8a8a8a; border-radius:10px; padding:10px 14px;
         margin:6px 0; font-size:14px; line-height:1.7; background:#fdfdfd; }}
 .node.start {{ background:#e8f5e9; border-color:#2e7d32; font-weight:bold; }}
 .node.end   {{ background:#fdecea; border-color:#c62828; }}
 .node.decision {{ background:#fff8e1; border-color:#f9a825; border-radius:14px; }}
 .arrow {{ text-align:center; color:#888; font-size:16px; margin:2px 0; }}
 .branch {{ margin-left:26px; border-left:3px dashed #bbb; padding-left:16px; }}
 .cond {{ display:inline-block; margin:10px 0 2px 6px; padding:2px 12px;
         border-radius:20px; background:#e3f2fd; border:1px solid #90caf9;
         font-size:12px; color:#1565c0; }}
 .sub {{ margin-left:10px; }}
 .note {{ margin-top:18px; color:#555; font-size:13px; line-height:1.9; }}
</style></head><body>
<h1>腾讯围棋 AI 陪练 — 启动到终局完整流程(离线版)</h1>
<div class="flow">
{''.join(render(F))}
</div>
<div class="note">
<b>四个数据源分工:</b><br>
· <b>执色</b> = 头像角标棋子颜色(唯一依据, 读不到就停/人工指定)<br>
· <b>轮次</b> = 横幅为真值; 落子瞬间靠"谁刚落子→轮到对方"算术即时翻转, 横幅只做校准<br>
· <b>网格/尺寸</b> = 自动检测, 变化需双读确认<br>
· <b>人工介入点</b> = 启动识别失败(UI 对话框) / 行动前横幅长期不可读(按推算谨慎落子) / 连续失败出口
</div>
</body></html>"""

open('flowchart.html', 'w', encoding='utf-8').write(html)
print('flowchart.html 已生成(离线 CSS 版)')
