# -*- coding: utf-8 -*-
"""对局帧录制(供离线回放回归测试)。

用法:
    python capture_frames.py --out frames --every 2 --max 900

在对局(含开局/终局转场)期间后台运行; 每 --every 秒存一帧窗口图。
帧格式: <out>/f<序号>.npz, 含 img(窗口相对 RGB 数组)与 rect(屏幕框)。
正常用完即删; 与临时 png 不同, 这些是有意保留的回归语料, 请放到
tools/frames/ 下并定期清理不需要的旧帧。

--keep-during-read: 每帧同时跑一次当前读盘并存结果(n/board),
  供回放时对照(默认不回放期另算)。
"""
import argparse
import os
import sys
import time

import numpy as np
from PIL import ImageGrab

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import board_reader as br


def grab_frame():
    """窗口图 + 屏幕框; 找不到窗口返回 (None, None)"""
    g = br._find_game_window()
    if g is None:
        return None, None
    pid, rect = g
    br.PID = pid
    vis_h = min(rect[3] - rect[1], br.SCREEN_H - rect[1])
    img = ImageGrab.grab(bbox=(rect[0], rect[1],
                               rect[2], rect[1] + vis_h)).convert('RGB')
    return np.asarray(img), np.array(rect)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'frames'))
    ap.add_argument('--every', type=float, default=2.0)
    ap.add_argument('--max', type=int, default=900)
    ap.add_argument('--keep-during-read', action='store_true')
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    print(f'录制开始 -> {args.out} (每 {args.every}s, Ctrl+C 停止)')
    idx = 0
    t_next = time.time()
    while idx < args.max:
        try:
            now = time.time()
            if now < t_next:
                time.sleep(min(0.5, t_next - now))
                continue
            t_next = now + args.every
            a, rect = grab_frame()
            if a is None:
                print('.', end='', flush=True)
                time.sleep(2)
                continue
            rec_n, rec_board = 0, []
            if args.keep_during_read:
                res = br.read_img(a, rect, min(rect[3] - rect[1],
                                               br.SCREEN_H - rect[1]),
                                  use_calib=False)
                if res is not None:
                    rec_n, rec_board = res['n'], res['board']
            p = os.path.join(args.out, f'f{idx:06d}.npz')
            np.savez_compressed(p, img=a, rect=rect,
                                rec_n=rec_n,
                                rec_board=np.array(rec_board, dtype=object))
            idx += 1
            if idx % 10 == 0:
                print(f'\n已存 {idx} 帧', flush=True)
            else:
                print('.', end='', flush=True)
        except KeyboardInterrupt:
            print(f'\n停止, 共 {idx} 帧')
            return
    print(f'\n完成, 共 {idx} 帧')


if __name__ == '__main__':
    main()
