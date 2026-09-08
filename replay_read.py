# -*- coding: utf-8 -*-
"""离线回放读盘回归: 对录制帧重跑当前读盘代码并核对。

用法:
    python replay_read.py frames/                 # 回放 + 与录制时结果对照
    python replay_read.py frames/ --stats         # 只看统计(逐帧差异明细另存)
    python replay_read.py frames/ --save-baseline out.json   # 存基线
    python replay_read.py frames/ --baseline out.json        # 与基线对照

对照模式:
  * 帧内录制了 rec_n/rec_board(需 capture_frames.py --keep-during-read)
    -> 报告当前代码与录制代码的差异;
  * 给了 --baseline -> 报告当前代码与基线输出的差异。
差异以逐帧报告 + 汇总计数输出; 退出码 0=全一致, 1=存在差异。

每帧独立处理(清网格缓存), 保证确定性; 顺带输出耗时统计。
"""
import argparse
import glob
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import board_reader as br


def read_one(rec):
    """对单帧执行完整读盘(独立于其他帧)"""
    br.clear_grid_cache()
    a = rec['img'].astype(np.int16)
    rect = rec['rect']
    vis_h = min(int(rect[3] - rect[1]), br.SCREEN_H - int(rect[1]))
    return br.read_img(a, rect, vis_h, use_calib=False)


def summarize(res):
    if res is None:
        return None
    return {'n': int(res['n']), 'board': list(res['board']),
            'src': res['src'], 'drift': float(res.get('drift', 0))}


def diff_cells(n, b1, b2):
    if b1 is None or b2 is None:
        return None
    if len(b1) != n or len(b2) != n:
        return [(-1, -1)]
    return [(i, j) for i in range(min(n, len(b1)))
            for j in range(min(n, len(b1[i]), len(b2[i])))
            if b1[i][j] != b2[i][j]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('dir')
    ap.add_argument('--stats', action='store_true')
    ap.add_argument('--save-baseline', default=None)
    ap.add_argument('--baseline', default=None)
    ap.add_argument('--limit', type=int, default=0, help='只回放前 N 帧')
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.dir, 'f*.npz')))
    if args.limit:
        files = files[:args.limit]
    if not files:
        print('目录中没有 f*.npz 帧')
        sys.exit(2)

    base = None
    if args.baseline and os.path.exists(args.baseline):
        base = json.load(open(args.baseline, encoding='utf-8'))

    times, diffs = [], 0
    rows = []
    t0 = time.time()
    for k, f in enumerate(files):
        rec = np.load(f, allow_pickle=True)
        t1 = time.time()
        res = read_one(rec)
        times.append(time.time() - t1)
        cur = summarize(res)
        ref = None
        if base is not None and str(k) in base:
            ref = base[str(k)]
        elif int(rec['rec_n']) > 0:
            ref = {'n': int(rec['rec_n']),
                   'board': [str(x) for x in rec['rec_board']]}
        if ref is None:
            rows.append({'i': k, 'file': os.path.basename(f),
                         'cur': cur, 'ref': None})
            continue
        dc = diff_cells((cur or {}).get('n', 0), (cur or {}).get('board'),
                        ref['board'])
        same = (cur is not None and cur['n'] == ref['n'] and dc == [])
        if not same:
            diffs += 1
        rows.append({'i': k, 'file': os.path.basename(f), 'cur': cur,
                     'ref': ref, 'same': same, 'diff': dc})
    dt = time.time() - t0

    if args.save_baseline:
        out = {str(r['i']): r['cur'] for r in rows if r['cur']}
        json.dump(out, open(args.save_baseline, 'w', encoding='utf-8'),
                  ensure_ascii=False, indent=0)
        print(f'基线已存: {args.save_baseline} ({len(out)} 帧有读盘结果)')

    ok = sum(1 for r in rows if r['ref'] is None
             or (r.get('same') and r['same']))
    total_ref = sum(1 for r in rows if r['ref'] is not None)
    print(f'帧数 {len(rows)} | 可对照 {total_ref} | 一致 {ok} | 差异 {diffs}')
    if times:
        print(f'读盘耗时: 中位 {np.median(times)*1000:.0f}ms, '
              f'均值 {np.mean(times)*1000:.0f}ms, '
              f'最大 {np.max(times)*1000:.0f}ms')
    for r in rows:
        if r['ref'] is None:
            if not args.stats:
                print(f"[{r['i']}] {r['file']}: 无对照(录制未存结果) cur="
                      f"{None if r['cur'] is None else r['cur']['n']}")
            continue
        if r.get('same'):
            continue
        c = r['cur']
        print(f"[{r['i']}] {r['file']}: 差异! "
              f"cur={'无' if c is None else str(c['n']) + '路/' + c['src']} "
              f"ref={r['ref']['n']}路")
        if c and r['diff']:
            for (i, j) in r['diff'][:6]:
                rr = c['board'][i] if 0 <= i < len(c['board']) else '?'
                print(f"    r{i} c{j}: 现={rr[j] if j < len(rr) else '?'} "
                      f"录={r['ref']['board'][i][j]}")
    print('结论: ' + ('全部一致' if diffs == 0 else f'{diffs} 帧存在差异'))
    sys.exit(1 if diffs else 0)


if __name__ == '__main__':
    main()
