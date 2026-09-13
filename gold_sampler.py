# -*- coding: utf-8 -*-
"""金框实时采样器(对局中跑, 复现"金框/非金框"两态真实数值)。

用法:
    python gold_sampler.py            每 0.5s 采样, Ctrl+C 停止
    python gold_sampler.py --every 0.3 --out docs/_gold_samples.csv

采样内容: 每次抓当前游戏窗口, 分别计算两个区域的"金色像素占比":
  A = 现有 GOLD_ROI (winclick.GOLD_ROI, 默认 80,180,150,210)
  B = 用户标注匹配区 (72,162,282,267, 含黄框+印章)
判定: 占比 >= GOLD_THR(0.094) => 我方行棋(金框); 否则对方行棋。

实时打印: 仅当数值明显变化(>0.05)或跨越阈值时打印, 避免刷屏。
结束(Ctrl+C): 打印两态统计(样本数/均值/最小/最大), 并保存 CSV。
"""
import argparse
import csv
import os
import sys
import time

import numpy as np
from PIL import ImageGrab

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import board_reader as br
import winclick

# 用户标注的匹配区(roi_mark_turn 模板匹配得到), 作对照
ROI_B = (72, 162, 282, 267)


def gold_ratio(arr, roi):
    """计算给定 ROI 内金色像素占比(与 winclick.gold_frame_ratio 同判据)。"""
    h, w = arr.shape[:2]
    x0, y0, x1, y1 = roi
    x1, y1 = min(x1, w), min(y1, h)
    if x1 <= x0 or y1 <= y0:
        return None
    s = arr[y0:y1, x0:x1]
    R, G, B = s[:, :, 0], s[:, :, 1], s[:, :, 2]
    return float(((R - B > 90) & (R - G < 90) & (G - B > 40)).mean())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--every', type=float, default=0.5, help='采样间隔(秒)')
    ap.add_argument('--out', default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        'docs', '_gold_samples.csv'))
    args = ap.parse_args()

    try:
        rw = br.window_rect(br.PID)
    except Exception as e:
        print('cannot get window rect:', e)
        return
    print('window rect =', rw)
    print('ROI_A (GOLD_ROI) =', winclick.GOLD_ROI)
    print('ROI_B (marked)   =', ROI_B)
    print('GOLD_THR =', winclick.GOLD_THR)
    print('--- sampling every %.2fs, Ctrl+C to stop ---' % args.every)

    rows = []
    last_a = None
    last_state = None
    t0 = time.time()
    try:
        while True:
            try:
                img = ImageGrab.grab(bbox=rw).convert('RGB')
                a = np.asarray(img).astype(int)
                ga = winclick.gold_frame_ratio(rw, None)   # ROI_A(内置)
                gb = gold_ratio(a, ROI_B)                  # ROI_B
            except Exception as e:
                print('sample fail:', type(e).__name__, e)
                time.sleep(args.every)
                continue

            if ga is None:
                time.sleep(args.every)
                continue

            t = time.time() - t0
            state = 'MINE(gold)' if ga >= winclick.GOLD_THR else 'OPP(no-gold)'
            rows.append([round(t, 2), round(ga, 4),
                         None if gb is None else round(gb, 4), state])

            # 变化显著 或 状态切换 才打印
            changed = (last_a is None or abs(ga - last_a) > 0.05
                       or state != last_state)
            if changed:
                gb_s = '%.4f' % gb if gb is not None else 'n/a'
                print('[%6.1fs] A=%.4f  B=%s  -> %s' % (t, ga, gb_s, state))
                last_a = ga
                last_state = state
            time.sleep(args.every)
    except KeyboardInterrupt:
        print('\n--- stopped ---')

    # 统计两态
    mine = [r[1] for r in rows if r[3].startswith('MINE')]
    opp = [r[1] for r in rows if r[3].startswith('OPP')]
    print()
    print('=== ROI_A (GOLD_ROI) 两态统计 ===')
    for name, vals in (('MINE(gold)', mine), ('OPP(no-gold)', opp)):
        if vals:
            print('  %-12s n=%3d  mean=%.4f  min=%.4f  max=%.4f'
                  % (name, len(vals), sum(vals) / len(vals),
                     min(vals), max(vals)))
        else:
            print('  %-12s n=0' % name)
    if mine and opp:
        print('  分离度 = %.1fx  (mine_mean / opp_mean)'
              % ((sum(mine) / len(mine)) / max(1e-9, sum(opp) / len(opp))))

    # 保存 CSV
    try:
        os.makedirs(os.path.dirname(args.out), exist_ok=True)
        with open(args.out, 'w', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            w.writerow(['t_sec', 'roi_a', 'roi_b', 'state'])
            w.writerows(rows)
        print('\nsaved %d samples -> %s' % (len(rows), args.out))
    except Exception as e:
        print('save fail:', e)


if __name__ == '__main__':
    main()
