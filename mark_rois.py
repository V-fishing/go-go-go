# -*- coding: utf-8 -*-
"""截取当前腾讯围棋窗口, 把当前脚本用到的识别区域全部画出来并保存。

用法: python mark_rois.py
输出: docs/_mark_rois.png  (同时打印各区域坐标)
"""
import os
import sys
import numpy as np
from PIL import Image, ImageDraw, ImageGrab

TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS)
import board_reader as br
import winclick

OUT = os.path.join(TOOLS, 'docs', '_mark_rois.png')


def main():
    # 1) 定位窗口
    try:
        rect = br.window_rect(br.PID)
    except SystemExit:
        rect = None
    if not rect:
        print('!! 未找到腾讯围棋窗口, 请先打开对局画面再运行')
        sys.exit(1)
    l, t, r, b = rect
    w, h = r - l, b - t
    print(f'窗口 rect=({l},{t},{r},{b})  尺寸 {w}x{h}')

    # 2) 截图 (窗口左上角为原点)
    img = ImageGrab.grab(bbox=(l, t, r, b)).convert('RGB')
    a = np.asarray(img)
    draw = ImageDraw.Draw(img)

    # ---- 顶栏 OCR 带 (段位/手数/名字行) ----
    ocr_h = int(h * 0.35)
    draw.rectangle([0, 0, w, ocr_h], outline='#ffe000', width=2)
    draw.text((4, 4), '顶栏OCR带(段位/手数/名字)', fill='#ffe000')

    # ---- 金框 ROI (行棋判定) ----
    gx0, gy0, gx1, gy1 = winclick.GOLD_ROI
    draw.rectangle([gx0, gy0, gx1, gy1], outline='red', width=2)
    draw.text((gx0, max(0, gy0 - 14)),
              f'金框ROI({gx0},{gy0},{gx1},{gy1})', fill='red')

    # ---- 棋盘网格 (读盘) ----
    res = br.locate_board(a)
    if res:
        xs, ys, meta = res
        for x in xs:
            draw.line([(int(x), int(ys[0]) - 4),
                       (int(x), int(ys[-1]) + 4)], fill='#00ff66', width=1)
        for y in ys:
            draw.line([(int(xs[0]) - 4, int(y)),
                       (int(xs[-1]) + 4, int(y))], fill='#00ff66', width=1)
        draw.text((int(xs[0]), max(0, int(ys[0]) - 28)),
                  f'棋盘网格 {len(xs)}x{len(ys)}', fill='#00ff66')
        print(f'棋盘: {len(xs)}x{len(ys)}  首线=({xs[0]:.0f},{ys[0]:.0f}) '
              f'步长~{xs[1]-xs[0]:.1f}px')
    else:
        print('!! locate_board 未检出棋盘 (可能非对局画面)')

    # ---- 执色角标 (屏幕坐标 -> 窗口相对) ----
    mb = winclick.avatar_my_box()
    if mb:
        mx0, my0, mx1, my1 = [int(v) for v in mb]
        draw.rectangle([mx0 - l, my0 - t, mx1 - l, my1 - t],
                       outline='#3c78ff', width=2)
        draw.text((mx0 - l, max(0, my0 - t - 14)), '执色角标', fill='#3c78ff')
        print(f'执色角标: 屏幕({mx0},{my0},{mx1},{my1})')
    else:
        print('!! avatar_my_box 为空 (未校准或未进对局页)')

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    img.save(OUT)
    print(f'已保存标注图: {OUT}')


if __name__ == '__main__':
    main()
