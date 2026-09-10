"""读盘 + KataGo 局面分析（只读，不落子）。

用法:
    python katago_suggest.py                 # 单次分析当前盘面
    python katago_suggest.py --watch         # 观战分析: 盘面变化自动刷新推荐
    python katago_suggest.py --watch --interval 8
规则: 中国规则贴 7.5 目。
"""
import json
import os
import subprocess
import sys
import time
import atexit

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import board_reader as br
from katago_client import KataClient

CLIENT = KataClient()
atexit.register(CLIENT.close)

KATAGO = r'E:\game\GoAI\katago\katago.exe'
MODEL19 = r'E:\game\GoAI\networks\kata1-b18c384nbt-s9996604416-d4316597426.bin.gz'
MODEL9 = r'E:\game\GoAI\networks\kata9x9-b18c384nbt-20231025.bin.gz'
CFG = r'E:\game\GoAI\katago\analysis_example.cfg'
LETTERS = 'ABCDEFGHJKLMNOPQRST'
VISITS = 300
SIZE_FIX = 0


def analyze(n, stones, player, visits=None):
    """调用 katago analysis(常驻进程, JSON 模式), 返回 (err, root, moves)"""
    visits = visits or VISITS
    req = {
        'id': 'sug',
        'moves': [],
        'initialStones': stones,
        'initialPlayer': player,
        'rules': 'chinese',
        'komi': 7.5,
        'boardXSize': n,
        'boardYSize': n,
        'analyzeTurns': [0],
        'maxVisits': visits,
    }
    d = CLIENT.query(req)
    if d is None:
        return 'engine error', None, None
    return None, d.get('rootInfo'), d.get('moveInfos', [])


def analyze_show(n, board):
    nb = sum(r.count('X') for r in board)
    nw = sum(r.count('O') for r in board)
    print(f'棋盘 {n}路  黑{nb} 白{nw}')
    try:
        import winclick
        _bt = winclick.ocr_turn()
        if _bt:
            print(f'[官方横幅] 当前行棋方: '
                  f'{("黑" if _bt=="black" else "白")}'
                  f'(只有该行有效)')
    except Exception:
        pass
    if nb == 0 and nw == 0:
        print('空棋盘, 无需分析')
        return
    stones = br.stones_legal(n, board)
    # 静态盘面无法断定轮到谁(有提子时子数差可>1), 两种情况都算并标注
    for player in ('black', 'white'):
        err, root, moves = analyze(n, stones, player)
        print('=' * 56)
        if err is not None:
            print(f'[{player} 先] KataGo 调用失败: {err}')
            continue
        side = '黑' if player == 'black' else '白'
        # 引擎 cfg reportAnalysisWinratesAs=BLACK: 胜率/目差恒为黑方基准,
        # 白行棋方一行换算成白方视角显示
        is_w = player == 'white'
        wr = root.get('winrate', 0)
        sl = root.get('scoreLead', 0)
        if is_w:
            wr, sl = 1.0 - wr, -sl
        line = f'[{side}先] {side}方胜率 {wr * 100:.1f}%  目差 {sl:+.1f}'
        if nb != nw and nb - nw not in (0, 1):
            line += f' (静态盘面子数差{nb - nw:+d}, 此为假定)'
        print(line)
        top = sorted(moves, key=lambda m: -m.get('visits', 0))[:3]
        for k, m in enumerate(top, 1):
            mv = m.get('move')
            pv = ' '.join(m.get('pv', [])[:8])
            mwr = m.get('winrate', 0)
            msl = m.get('scoreLead', 0)
            if is_w:
                mwr, msl = 1.0 - mwr, -msl
            print(f'  {k}. {side}{mv}  胜率 {mwr * 100:.1f}%'
                  f'  目差 {msl:+.1f}  '
                  f'({m.get("visits", 0)}次访问)')
            if pv:
                print(f'     后续: {pv}')


def main():
    watch = '--watch' in sys.argv
    interval = 5
    if '--interval' in sys.argv:
        interval = int(sys.argv[sys.argv.index('--interval') + 1])
    if '--visits' in sys.argv:
        global VISITS
        VISITS = int(sys.argv[sys.argv.index('--visits') + 1])
    if '--size' in sys.argv:
        global SIZE_FIX
        SIZE_FIX = int(sys.argv[sys.argv.index('--size') + 1])
    if '--mode' in sys.argv:
        br.set_game_mode(sys.argv[sys.argv.index('--mode') + 1])

    def size_ok(res):
        if SIZE_FIX and res['n'] != SIZE_FIX:
            print(f'!! 指定 {SIZE_FIX} 路, 检测到 {res["n"]} 路, 跳过')
            return False
        return True

    if watch:
        print(f'观战分析模式: 每 {interval}s 读盘一次, 盘面变化时自动分析, '
              f'Ctrl+C 停止')
        last = None
        while True:
            try:
                res = br.read_current(force_size=SIZE_FIX, use_calib=False)
                if res is None:
                    print('读盘失败, 等待...')
                    time.sleep(3)
                    continue
                if not size_ok(res):
                    time.sleep(interval)
                    continue
                n, board = res['n'], res['board']
                key = ''.join(''.join(r) for r in board)
                if key == last:
                    time.sleep(interval)
                    continue
                last = key
                if sum(r.count('X') + r.count('O') for r in board) == 0:
                    print('空棋盘, 等待落子...')
                    time.sleep(interval)
                    continue
                print(f'\n--- 盘面变化 (来源 {res["src"]}) ---')
                analyze_show(n, board)
            except KeyboardInterrupt:
                print('\n已停止')
                return
            except Exception as e:
                print(f'异常: {e}, 继续...')
                time.sleep(3)
        return

    res = br.read_current(force_size=SIZE_FIX, use_calib=False)
    if res is None:
        print('读盘失败: 未找到棋盘')
        return
    if not size_ok(res):
        return
    analyze_show(res['n'], res['board'])


if __name__ == '__main__':
    main()
