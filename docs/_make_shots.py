# -*- coding: utf-8 -*-
"""GoAI 文档截图标注脚本（可复跑）—— 现场采样版。

直接抓取当前腾讯围棋对局窗口(ImageGrab), 用 Pillow 叠加关键 ROI 框,
输出到 docs/images/。所有图均来自实时画面, 不再依赖 _eval_out 旧截图。

运行前提: 腾讯围棋对局页处于打开状态。
依赖: Pillow / numpy / 本项目 board_reader / winclick / capture_frames。
"""
import json
import os
import sys
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # tools/
sys.path.insert(0, BASE)
import board_reader as br
import winclick
import capture_frames as cf

OUT = os.path.join(BASE, 'docs', 'images')
os.makedirs(OUT, exist_ok=True)

calib_a = json.load(open(os.path.join(BASE, 'avatar_calib.json'), encoding='utf-8'))
calib_g = json.load(open(os.path.join(BASE, 'grid_calib.json'), encoding='utf-8'))

# ---- 窗口相对坐标(与 545x992 截图 1:1) ----
GOLD = winclick.GOLD_ROI                       # (80, 180, 150, 210)
MY_BOX = tuple(calib_a['my'])                 # 我方头像角标框
wx0, wy0 = calib_g['win_x0'], calib_g['win_y0']
GX0 = calib_g['x0'] - wx0
GY0 = calib_g['y0'] - wy0
GX1 = calib_g['x1'] - wx0
GY1 = calib_g['y1'] - wy0
GRID = (GX0, GY0, GX1, GY1)

CYAN = (0, 255, 255)     # 头像角标
GOLD_C = (255, 215, 0)   # 金框
GREEN = (0, 200, 0)      # 棋盘
BLUE = (30, 120, 255)    # 空点
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)


def grab():
    """抓一帧实时窗口; 返回 (PIL 窗口图, arr int16, rect) 或 (None, None, None)。"""
    a, rect = cf.grab_frame()
    if a is None:
        return None, None, None
    img = Image.fromarray(a.astype('uint8')).convert('RGB')
    return img, a, rect


def annotate(img, boxes):
    d = ImageDraw.Draw(img)
    for box, color in boxes:
        d.rectangle(box, outline=color, width=3)
    return img


def label_box(img, box, color, text):
    """裁剪并放大 ROI 区域, 便于展示细节。"""
    crop = img.crop(box)
    crop = crop.resize((crop.width * 3, crop.height * 3), Image.NEAREST)
    d = ImageDraw.Draw(crop)
    d.rectangle([0, 0, crop.width - 1, crop.height - 1], outline=color, width=4)
    try:
        d.text((6, 6), text, fill=color)
    except Exception:
        pass
    return crop


def capture_turn_frames(timeout=30.0):
    """轮询抓帧, 按 gold_frame_ratio 收集「我方行棋帧 / 对方行棋帧」各一帧。"""
    my_img = opp_img = None
    t0 = time.time()
    while time.time() - t0 < timeout:
        img, arr, rect = grab()
        if img is None:
            print('[warn] 未找到游戏窗口, 1s 后重试...')
            time.sleep(1)
            continue
        r = winclick.gold_frame_ratio(arr=arr)
        if r is None:
            time.sleep(0.5)
            continue
        if r >= winclick.GOLD_THR:
            if my_img is None:
                my_img = img
                print(f'[info] 抓到「我方行棋」帧 (gold={r:.3f})')
        else:
            if opp_img is None:
                opp_img = img
                print(f'[info] 抓到「对方行棋」帧 (gold={r:.3f})')
        if my_img is not None and opp_img is not None:
            break
        time.sleep(0.5)
    return my_img, opp_img


def make_snap_overlay():
    """读当前盘, 叠加网格线 / A–T 列字母 / 黑白子与空点色块。"""
    res = br.read_current()
    if res is None:
        print('[warn] read_current 失败, 跳过 snap_overlay')
        return
    img = res['img'].convert('RGB')
    d = ImageDraw.Draw(img)
    xs = res['xs']
    ys = res['ys']
    n = res['n']
    board = res['board']
    stone_r = res['stone_r']
    f = ImageFont.load_default()

    # 青色网格线
    for x in xs:
        d.line([(x, ys[0]), (x, ys[-1])], fill=CYAN, width=1)
    for y in ys:
        d.line([(xs[0], y), (xs[-1], y)], fill=CYAN, width=1)

    # 逐点棋子标记
    for i in range(n):
        for j in range(n):
            c = board[i][j]
            x = xs[j]
            y = ys[i]
            if c == 'X':
                d.ellipse([x - stone_r, y - stone_r, x + stone_r, y + stone_r],
                          fill=BLACK, outline=WHITE, width=1)
            elif c == 'O':
                d.ellipse([x - stone_r, y - stone_r, x + stone_r, y + stone_r],
                          outline=WHITE, width=2)
            else:
                d.ellipse([x - 2, y - 2, x + 2, y + 2], fill=BLUE)

    # 顶部列字母(A–T 跳过 I) + 左侧行号(n-i)
    letters = 'ABCDEFGHJKLMNOPQRST'[:n]
    for j, L in enumerate(letters):
        d.text((xs[j] - 3, ys[0] - 14), L, fill=GOLD_C, font=f)
    for i in range(n):
        d.text((xs[0] - 16, ys[i] - 6), str(n - i), fill=GOLD_C, font=f)

    # 标题栏读数
    wb = sum(r.count('O') for r in board)
    xb = sum(r.count('X') for r in board)
    empty = n * n - wb - xb
    txt = (f"board {n}x{n} step={res['step']:.1f} | white={wb} "
           f"black={xb} empty={empty} | white=white circle "
           f"black=black dot blue=empty")
    d.text((4, 4), txt, fill=WHITE, font=f)

    save(img, 'snap_overlay.png')


def save(img, name):
    img.save(os.path.join(OUT, name))
    print('wrote', name)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--timeout', type=float, default=30.0,
                    help='抓帧轮询总超时(秒), 需覆盖一次「我方→对方」回合切换')
    args = ap.parse_args()
    my_img, opp_img = capture_turn_frames(timeout=args.timeout)
    if my_img is None and opp_img is None:
        print('[FATAL] 未找到腾讯围棋窗口, 请确认对局页已打开后重跑。')
        sys.exit(1)

    base = my_img or opp_img

    # 1) 窗口总览: 三框叠加(我方头像 / 金框 / 棋盘)
    if base is not None:
        save(annotate(base.copy(), [(MY_BOX, CYAN), (GOLD, GOLD_C),
                                    (GRID, GREEN)]), 'window_overview.png')

    # 2) 我方行棋: 金框在位
    if my_img is not None:
        save(annotate(my_img.copy(), [(GOLD, GOLD_C)]), 'turn_gold.png')
        save(label_box(my_img, GOLD, GOLD_C, 'GOLD'), 'gold_roi_turn.png')
    else:
        print('[warn] 未抓到「我方行棋」帧, 跳过 turn_gold/gold_roi_turn '
              '(请在我方行棋时重跑)')

    # 3) 对方行棋: 金框缺失
    if opp_img is not None:
        save(annotate(opp_img.copy(), [(GOLD, GOLD_C)]), 'mine_gold.png')
    else:
        print('[warn] 未抓到「对方行棋」帧, 跳过 mine_gold '
              '(请在对方行棋时重跑)')

    # 4) 棋盘网格框
    if base is not None:
        save(annotate(base.copy(), [(GRID, GREEN)]), 'grid_box.png')
        # 5) 头像角标放大裁剪
        save(label_box(base, MY_BOX, CYAN, 'MY'), 'avatar_my_box.png')

    # 6) 坐标 / 棋子色块诊断视图(实时读盘叠加)
    make_snap_overlay()

    print('DONE ->', OUT)


if __name__ == '__main__':
    main()
