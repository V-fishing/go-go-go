"""腾讯围棋 人机/AI 对局 自动落子（配合 KataGo）— 状态机版。

仅用于: 人机对弈 / 双方知情同意的对局。真人匹配局请勿使用。

用法:
    python katago_play.py white --turn white   # 执白, 当前轮到白(立即下)
    python katago_play.py white --turn black   # 执白, 当前轮到黑(等AI下完再下)
    (不传 --turn 时按绿框行棋横幅判定, 不可判时停车请人工指定)

状态机:
    轮次只在两类事件翻转: ①确认到盘面变化(有人落子) ②自己成功落子。
    落子成功后立即"吸收"己方造成的盘面变化, 避免双重翻转;
    按落子前后总子数差判断对手是否已抢先应手。
"""
import json
import os
import subprocess
import sys
import threading
import time
import atexit
import ctypes
import ctypes.wintypes

# 强制 UTF-8 输出(避免 ✔ 等字符在 GBK 重定向下报 UnicodeEncodeError)
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import board_reader as br
import cleanup_files as _cf
from katago_client import KataClient
import winclick

def find_render_hwnd():
    """定位腾讯围棋(Chromium)的渲染子窗口 Chrome_RenderWidgetHostHWND。
    同进程可能还有别的(隐藏)小程序窗口, 按"与游戏窗口面积最接近"选择。"""
    user32 = ctypes.windll.user32
    pid = br.PID
    if pid is None:
        try:
            br.window_rect(None)     # 动态解析一次(并刷新 br.PID)
            pid = br.PID
        except Exception:
            return None
    try:
        grect = br.window_rect(pid)
    except SystemExit:
        return None
    garea = (grect[2] - grect[0]) * (grect[3] - grect[1])
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    best = None
    best_diff = None

    def on_top(h, _):
        nonlocal best, best_diff
        p = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(h, ctypes.byref(p))
        if p.value != pid:
            return True
        kids = []

        def on_child(ch, _2):
            cls = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(ch, cls, 128)
            kids.append((ch, cls.value))
            return True

        user32.EnumChildWindows(h, proto(on_child), 0)
        for ch, cls in kids:
            if 'Chrome_RenderWidgetHostHWND' not in cls:
                continue
            r = ctypes.wintypes.RECT()
            user32.GetWindowRect(ch, ctypes.byref(r))
            area = (r.right - r.left) * (r.bottom - r.top)
            diff = abs(area - garea)     # 面积差最小者胜
            if best_diff is None or diff < best_diff:
                best_diff = diff
                best = ch
        return True

    user32.EnumWindows(proto(on_top), 0)
    return best




def click_at(x, y):
    """无光标点击: 直接向 Chromium 渲染窗口 PostMessage 鼠标消息。
    不移动物理光标, 用户可同时自由使用鼠标。失败时退回传统点击。"""
    global _rend
    user32 = ctypes.windll.user32
    for _try in range(2):
        _rend = find_render_hwnd()   # 每次现找(微信窗口/进程重启后自动跟上)
        if _rend is not None:
            r = ctypes.wintypes.RECT()
            user32.GetWindowRect(_rend, ctypes.byref(r))
            cx, cy = int(x) - r.left, int(y) - r.top
            if 0 <= cx < r.right - r.left and 0 <= cy < r.bottom - r.top:
                lp = ((cy & 0xFFFF) << 16) | (cx & 0xFFFF)
                ok1 = user32.PostMessageW(_rend, 0x0200, 0, lp)
                time.sleep(0.05)
                ok2 = user32.PostMessageW(_rend, 0x0201, 0x0001, lp)
                time.sleep(0.08)
                ok3 = user32.PostMessageW(_rend, 0x0202, 0, lp)
                time.sleep(0.05)
                if ok1 and ok2 and ok3:
                    return True
        _rend = None
    return False




def ensure_foreground(hwnd):
    user32 = ctypes.windll.user32
    if hwnd and user32.GetForegroundWindow() != hwnd:
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.4)




def find_hwnd():
    user32 = ctypes.windll.user32
    found = []
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def cb(hwnd, _):
        p = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == br.PID and user32.IsWindowVisible(hwnd):
            r = ctypes.wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            if r.right - r.left > 200 and r.bottom - r.top > 200:
                found.append(hwnd)
        return True

    user32.EnumWindows(proto(cb), 0)
    return found[0] if found else None




def park_cursor():
    ctypes.windll.user32.SetCursorPos(2, 2)




def _hover_square(res, i, j):
    """光标格是否悬停实心方块(>=14px): 方块四角(±0.22*step)仍为子色,
    而圆棋子四角为木色。用于屏蔽被误读成棋子的悬停方块(旧布局 12px,
    当前布局 >=14px 时左半段 run>=8 会误判成子)。"""
    try:
        import numpy as _np
        im = res.get('img')
        if im is None:
            return False
        xs, ys = res['xs'], res['ys']
        if i >= len(ys) or j >= len(xs):
            return False
        a = _np.asarray(im).astype(_np.int16)
        lum = a.mean(axis=2)
        chm = a.max(axis=2) - a.min(axis=2)
        step = float(res.get('step', 27))
        o = max(5, int(round(step * 0.25)))
        xx, yy = int(round(xs[j])), int(round(ys[i]))
        ok = 0
        for (dy0, dx0) in ((-o, -o), (-o, o), (o, -o), (o, o)):
            L = lum[yy + dy0, xx + dx0]
            C = chm[yy + dy0, xx + dx0]
            if (L < 105 and C < 30) or (L > 165 and C < 60):
                ok += 1
        return ok >= 3
    except Exception:
        return False


def cursor_hover_cell(res=None):
    """返回鼠标压着的交叉点 (i, j); 不在棋盘上返回 None"""
    global LAST_RES
    import numpy as np
    base = res if res is not None else LAST_RES
    if base is None:
        return None
    rect = base['rect']
    xs = np.asarray(base['xs'])
    ys = np.asarray(base['ys'])
    if len(xs) < 2:
        return None
    cell = float(np.mean([xs[1] - xs[0], ys[1] - ys[0]]))
    p = ctypes.wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(p))
    wx, wy = p.x - rect[0], p.y - rect[1]
    if not (xs[0] - cell < wx < xs[-1] + cell
            and ys[0] - cell < wy < ys[-1] + cell):
        return None
    j = int(np.argmin(np.abs(xs - wx)))
    i = int(np.argmin(np.abs(ys - wy)))
    if abs(xs[j] - wx) <= cell * 0.55 and abs(ys[i] - wy) <= cell * 0.55:
        return (i, j)
    return None




def cursor_hovers_board():
    return cursor_hover_cell() is not None



def _other(c):
    """执色反转: 白->黑, 黑->白"""
    return 'black' if c == 'white' else 'white'



# ---------------- UI 状态投影 ----------------
# 主进程把关键状态(执子/轮次/手数/黑白/胜率/锚定来源/引擎状态)原子写入
# ui_state.json, katago_ui.py 轮询展示。节流 1s, 关键事件 force=True 立即。
_UI_ST = {'game': 0, 'assist': None, 'turn': None, 'turn_src': None,
          'n': 0, 'b': 0, 'w': 0, 'move_no': 0, 'wr': None, 'lead': None,
          'status': 'boot', 'mv': '', 'engine': ''}
_UI_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'ui_state.json')
_UI_LOCK = threading.Lock()
_UI_PUB_AT = [0.0]
_EVT = [None]      # 步进计时: 关键事件间隔诊断(定位"20s一手"花在哪)


def evt(msg):
    """关键步进日志: 打印距上一关键事件的间隔(诊断管道耗时)。"""
    try:
        _now = time.time()
        _d = (_now - _EVT[0]) if _EVT[0] else 0.0
        _EVT[0] = _now
        print(f'[步进+{_d:.1f}s] {msg}')
    except Exception:
        pass



def ui_pub(force=False):
    """写状态文件(临时文件+os.replace 原子替换, 防 UI 读到半截)。"""
    try:
        now = time.time()
        if not force and now - _UI_PUB_AT[0] < 1.0:
            return
        _UI_PUB_AT[0] = now
        _UI_ST['ts'] = now
        with _UI_LOCK:
            _tmp = _UI_PATH + '.tmp'
            with open(_tmp, 'w', encoding='utf-8') as _f:
                json.dump(_UI_ST, _f, ensure_ascii=False)
            os.replace(_tmp, _UI_PATH)
    except Exception:
        pass


def _set_st(force=False, **kw):
    _UI_ST.update(kw)
    ui_pub(force)


def visual_turn(res_cur, rect=None, assist=None):
    """视觉行棋判定(唯一机制 = 金色倒计时空心框; 徽章 geo 与 obox 兜底
    均已彻底删除)。
    判据: ROI(80,180)-(150,210) 内出现金黄色块 = 我方行棋, 否则对方行棋。
    需 assist(我执色)把 mine/opp 换算成颜色返回, 保持与旧 geo 相同的
    返回语义(颜色), 调用方无需改动。
    金框为确定性判据(只要有当前帧即可算), 不再保留任何兜底路径;
    采样失败一律返回不确定, 由调用方重试——不猜。
    返回 (src, val, 描述): ('gold', 行棋方颜色, 描述) 或 (None, None, '')。"""
    try:
        if res_cur is None or assist is None:
            return None, None, ''
        _im = res_cur.get('img') if res_cur else None
        _arr = None
        if _im is not None:
            import numpy as _np
            _arr = _np.asarray(_im).astype(_np.int16)
        _rect = rect or res_cur.get('rect')
        _gf = winclick.gold_frame_ratio(_rect, _arr)
        if _gf is None:
            return None, None, ''
        mine = _gf >= winclick.GOLD_THR
        val = assist if mine else other(assist)
        return 'gold', val, ('金框%.3f:%s方行棋' % (_gf, '我' if mine else '他'))
    except Exception as _e:
        print(f'  [行棋判定异常] {type(_e).__name__}: {_e}')
    return None, None, ''




def anchor_turn_visual(counts, assist, res_cur):
    """启动锚定: 唯一机制 = 金色倒计时空心框(徽章 geo / 子数奇偶算术 /
    绿框 obox 兜底 均已彻底删除)。
    判据: 有金框=我方行棋, 无金框=对方行棋(无需学习, 无滞后歧义, 实测两态
    区分度 ~5.7 倍)。
    空盘是铁律: 黑先手必轮到黑(与我方执色无关), 直接锚定无需视觉判定。
    取不到当前帧时重试; 连续重试仍失败返回 (None, 'unresolved')
    ——不猜, 交人工。
    返回 (turn, 'visual'/'empty'/'unresolved')。"""
    if counts[0] + counts[1] == 0:
        # 空盘: 必轮到黑(黑先手铁律), 无需视觉判定
        return 'black', 'empty'
    # 唯一判据 = 金色倒计时空心框: 有=我方行棋 / 无=对方行棋。
    # 徽章 geo / 子数奇偶算术 / 绿框 obox 均已彻底删除, 无任何兜底。
    _other = ('white' if assist == 'black' else 'black')
    try:
        _rw = br.window_rect(br.PID)
    except Exception:
        _rw = None
    # 多次采样取中位数: 抗开局"请落子"/倒计时提示等瞬时金色 UI 造成的假阳性
    # (单次采样易采到闪烁金框 -> 误判我方行棋)。金框在我方计时区, 出现=我方
    # 行棋, 无反转; 中位数可滤掉开局短暂闪烁, 读到真实状态。
    _gfs = []
    for _i in range(6):
        try:
            _arr = None
            _im = res_cur.get('img') if res_cur else None
            if _im is not None:
                import numpy as _np
                _arr = _np.asarray(_im).astype(_np.int16)
            _gf = winclick.gold_frame_ratio(_rw, _arr)
            if _gf is not None:
                _gfs.append(_gf)
            else:
                time.sleep(0.6)      # 未拿到当前帧: 重试取帧, 不猜
                continue
        except Exception as _e:
            # 不再静默吞异常(此前 NameError 被吞导致连判失败却无提示)
            print(f'  [锚定异常] {type(_e).__name__}: {_e}')
        time.sleep(0.5)
    if not _gfs:
        print('?? 视觉状态连续无法判定(读数失败), 不猜奇偶;')
        print('   请确认微信窗口在前台且处于对局页, 在 UI 里选[当前轮到]后'
              '点[启动]重试')
        return None, 'unresolved'
    _gfs.sort()
    _med = _gfs[len(_gfs) // 2]
    _turn = assist if _med >= winclick.GOLD_THR else _other
    print(f'  锚定: 金框采样{_gfs} 中位={_med:.3f} (黑{counts[0]}白{counts[1]})'
          f' -> 轮到{"黑" if _turn == "black" else "白"}')
    return _turn, 'visual'




def strip_hover_square(board, counts, res_cur, assist):
    """启动时悬停预览方块矫正: 我方回合光标压格会显示实心方块(我方色),
    空盘时会被读成"唯一一颗我方色子"。若盘面恰只有这一颗且光标在其上
    -> 判定为预览方块, 归零。返回 (board, counts)。"""
    try:
        if res_cur is None:
            return board, counts
        _hv = cursor_hover_cell(res_cur)
        if _hv is None or counts[0] + counts[1] != 1:
            return board, counts
        _hi, _hj = _hv
        _myc = 'X' if assist == 'black' else 'O'
        if board[_hi][_hj] != _myc:
            return board, counts
        _bb = [list(r) for r in board]
        _bb[_hi][_hj] = '.'
        _c2 = ((0, counts[1]) if _myc == 'X' else (counts[0], 0))
        print('(光标处为落子预览方块, 忽略, 盘面视为空)')
        return [''.join(r) for r in _bb], _c2
    except Exception:
        return board, counts




def color_observer_loop():
    """后台线程: 每 ~10s 独立采样我方执色(与主循环并行)。
    内部含 OCR 页面闸门, 守护进程已加锁, 并发安全。
    仅记录观察, 判定仍以启动/开局锚定为准。"""
    while True:
        time.sleep(10)
        if not _GAME_ACTIVE[0]:
            continue   # 非对局页不做无谓 OCR
        try:
            av = winclick.avatar_my_color()
        except Exception:
            av = None
        if av is None:
            continue
        _OBS_SIDE[:] = [av, time.time()]
        if _OBS_SIDE_LOG[0] != av:
            _OBS_SIDE_LOG[0] = av
            _OBS_SIDE_LOG[1] = time.time()
            print(f'[角标并行] 角标读为 {("黑" if av=="black" else "白")}')
        elif time.time() - _OBS_SIDE_LOG[1] >= 120:
            _OBS_SIDE_LOG[1] = time.time()
            print(f'[角标并行] 持续: 角标='
                  f'{("黑" if av=="black" else "白")}')




def analyze_position(n, stones, player, visits=None, banned=None,
                     prefix=None, prefix_player=None):
    visits = visits or VISITS
    moves = []
    initialPlayer = player
    analyzeTurns = [0]
    if prefix:
        # 接续分析: 在 initialStones 之上追加已落手(GTP 串列表), 颜色由
        # prefix_player 起依次交替; analyzeTurns 指向最后一手之后。用于
        # 对方思考期把"对方刚落的预测手"下进局面, 直接续算我方应手。
        moves = [str(m) for m in prefix]
        analyzeTurns = [len(moves)]
        initialPlayer = prefix_player or player
    req = {
        'id': 'play', 'moves': moves, 'initialStones': stones,
        'initialPlayer': initialPlayer, 'rules': 'chinese', 'komi': 7.5,
        'boardXSize': n, 'boardYSize': n, 'analyzeTurns': analyzeTurns,
        'maxVisits': visits,
    }
    _t0 = time.time()
    d = CLIENT.query(req)
    evt('引擎返回(%.1fs, %d点)' % (time.time() - _t0, visits))
    if d is None:
        return None, None, None
    mis = d.get('moveInfos', [])
    if not mis:
        return None, None, d.get('rootInfo')
    banned = banned or set()
    ranked = sorted(mis, key=lambda m: (m.get('visits', 0),
                                        m.get('winrate', 0)), reverse=True)
    best = None
    for m in ranked:
        if m.get('move') not in banned:
            best = m
            break
    if best is None:
        return None, None, d.get('rootInfo')
    return best['move'], best, d.get('rootInfo')




def root_view(root, want):
    """rootInfo/moveInfo 的 winrate 与 scoreLead 恒为黑方视角
    (cfg: reportAnalysisWinratesAs = BLACK; 目差>0 = 黑领先, 贴目计给白),
    与 currentPlayer 无关。want='black' 原样; want='white' 取镜像。
    注意: 查询的 initialPlayer 仍须传真实行棋方(问错行棋方会得到
    '对方刚虚着'式的失真评估)。"""
    if not root:
        return None, None
    wr = root.get('winrate', 0)
    lead = root.get('scoreLead', 0)
    if want == 'white':
        wr = 1.0 - wr
        lead = -lead
    return wr, lead




# 分相计时: 记录"对方落子检测 -> 我方落子确认"各阶段耗时
_MOVE_T = {}


def _phase_report():
    """打印上一手从检测到确认的分相耗时(检测/算手/点击/确认)。"""
    global _MOVE_T
    d = _MOVE_T
    t0 = d.get('detected')
    if t0 is None:
        return
    cs = d.get('compute_start')
    mr = d.get('mv_ready')
    ck = d.get('click')
    cf = d.get('confirm')
    parts = []
    if cs is not None:
        parts.append('检测->算手 %.0fms' % ((cs - t0) * 1000))
    if mr is not None and cs is not None:
        r = '复用' if d.get('reused') else '新算'
        parts.append('算手(%s) %.0fms' % (r, (mr - cs) * 1000))
    if ck is not None and mr is not None:
        parts.append('算手->点击 %.0fms' % ((ck - mr) * 1000))
    if cf is not None and ck is not None:
        parts.append('点击->确认 %.0fms' % ((cf - ck) * 1000))
    if cf is not None:
        parts.append('总计 %.0fms' % ((cf - t0) * 1000))
    if parts:
        print('[分相计时] ' + ' | '.join(parts))


def _gtp_to_ij(n, mv):
    """GTP 串(如 'J8') -> 棋盘 (行,列) 0-based; pass/非法返回 None。"""
    if mv is None:
        return None
    mv = str(mv).strip()
    if mv.lower().startswith('pass'):
        return None
    try:
        col = LETTERS.index(mv[0])
        row = n - int(mv[1:])
        return (row, col)
    except Exception:
        return None


def _prefill_after_opp(n, last_board, board, mover, assist):
    """对手落子后用其真实落点作 prefix 续算我方应手(异步, 不阻塞检测
    循环)。把 4-5s 冷算移到"对手落子 -> 我落子"的间隙; 我方落子时按
    对手落子后局面 KEY 复用缓存, 零延迟出招。"""
    try:
        _add = game_move_added(n, last_board, board)
        if not (_add and _add[1]):
            return
        _opp_gtp = _add[1]
        _stones_before = br.stones_legal(n, last_board)
        _mv, _info, _root = analyze_position(
            n, _stones_before, assist, visits=VISITS,
            prefix=[_opp_gtp], prefix_player=mover)
        PRE['key'] = tuple(tuple(s) for s in br.stones_legal(n, board))
        PRE['pred_move'] = _opp_gtp
        PRE['t'] = time.time()
        PRE['done'] = True
        if _mv and not str(_mv).lower().startswith('pass'):
            PRE['mv'], PRE['info'], PRE['root'] = str(_mv), _info, _root
            print(f'[预热] 对手落 {_opp_gtp} -> 续算我方应 {_mv} '
                  f'({VISITS}点, 缓存{len(PRE["key"])}子)')
        else:
            PRE['mv'] = None
            print(f'[预热] 对手落 {_opp_gtp}, 续算无应手')
    except Exception as e:
        print(f'[预热] 续算异常 {type(e).__name__}: {e}')


def pre_analyze(n, board, assist, wait):
    """对方回合内预测对方落点并续算我方应手(接续分析), 缓存"对方落子后
    局面"的应手; 轮到我们且盘面匹配时零延迟复用(强度=VISITS)。

    旧版失效根因: 用 assist 作 initialPlayer 分析"对方回合"局面(评估失真),
    且缓存 key 是"对方落子前"局面 -> 与我方真正要算的"对方落子后"永远不匹配。
    现改为:
      1) 用正确行棋方(对方)分析当前局面, 取对方 top 落点(预测, 低算力即可);
      2) 以"对方落该手"为前缀续算我方应手(强度=用户设定 VISITS);
      3) 缓存 key = 对方落该手后的局面(与我方 turn 真实局面匹配)。
    预测落点与真实不符时 key 不匹配 -> 主流程自动新算, 不退化。
    """
    if wait < 2:
        return
    global PRE
    now = time.time()
    opp = ('black' if assist == 'white' else 'white')
    stones = br.stones_legal(n, board)            # 对方落子前局面
    key0 = tuple(tuple(s) for s in stones)
    # 同一局面已用目标强度(VISITS)续算完成则跳过, 避免重复跑双查询
    if (PRE.get('pred_key') == key0 and PRE.get('done') is True):
        return
    # 同局面 1.5s 内不重跑预测(防止 0.25s 轮询下频繁触发双查询)
    if (PRE.get('pred_key') == key0 and now - PRE.get('t', 0) < 1.5):
        return
    try:
        PRE['t'] = now
        # 1) 正确行棋方(对方)分析当前局面 -> 预测对方落点(低算力够准)
        mv_opp, _, _ = analyze_position(n, stones, opp, visits=PRE_VISITS)
        if mv_opp is None or str(mv_opp).lower().startswith('pass'):
            PRE['pred_key'], PRE['mv'], PRE['done'] = key0, None, True
            print('[预热] 对方预测=pass/None, 不缓存')
            return
        # 2) 以"对方落该手"为前缀续算我方应手(强度=VISITS, 命中即复用)
        mv, info, root = analyze_position(
            n, stones, assist, visits=VISITS,
            prefix=[str(mv_opp)], prefix_player=opp)
        # 3) 缓存 key = 对方落该手后的局面
        ij = _gtp_to_ij(n, mv_opp)
        pred_board = [list(r) for r in board]
        if ij is not None:
            pred_board[ij[0]][ij[1]] = 'X' if opp == 'black' else 'O'
        PRE['key'] = tuple(tuple(s) for s in br.stones_legal(n, pred_board))
        PRE['pred_key'], PRE['pred_move'], PRE['done'] = key0, str(mv_opp), True
        if mv and not str(mv).lower().startswith('pass'):
            PRE['mv'], PRE['info'], PRE['root'] = str(mv), info, root
            print(f'[预热] 预测对方落 {mv_opp} -> 我方应 {mv} '
                  f'(续算{VISITS}点, 缓存{len(PRE["key"])}子)')
        else:
            PRE['mv'] = None
            print(f'[预热] 预测对方落 {mv_opp}, 我方续算无应手')
    except Exception as e:
        print(f'[预热] 异常 {type(e).__name__}: {e}')




def board_settled(n, board, mover, thr=0.5, max_unsettled=8):
    """用引擎 ownership 判断盘面是否已定型: 空点中 |ownership| < thr 的
    (低置信/争拗中)数量 <= max_unsettled 视为定型。mover = 实际行棋方。
    实测: 9路真实终局(引擎建议停一手时)该数为 0; 中盘 12 手时 47。
    查询失败时返回 True(保守放行, 不因该辅助功能卡住正常终局)。"""
    try:
        stones = br.stones_legal(n, board)
        d = CLIENT.query({'id': 'settle', 'moves': [], 'initialStones': stones,
                          'initialPlayer': mover, 'rules': 'chinese',
                          'komi': 7.5, 'boardXSize': n, 'boardYSize': n,
                          'analyzeTurns': [0], 'maxVisits': 400,
                          'includeOwnership': True})
        own = (d or {}).get('ownership') or []
        if len(own) != n * n:
            return True
        cnt = 0
        for i in range(n):
            row = board[i]
            base = i * n
            for j in range(n):
                if row[j] == '.' and abs(own[base + j]) < thr:
                    cnt += 1
                    if cnt > max_unsettled:
                        return False
        return True
    except Exception:
        return True




def board_to_stones(n, board):
    stones = []
    for i in range(n):
        for j in range(n):
            c = board[i][j]
            if c in 'XO':
                stones.append(['b' if c == 'X' else 'w',
                               LETTERS[j] + str(n - i)])
    return stones




def game_move_added(n, old, new):
    """old->new 中恰好新增一颗子 -> (颜色'X'/'O', gtp); 否则 None"""
    if old is None or len(old) != len(new):
        return None
    adds = []
    for i, ro in enumerate(old):
        rn = new[i]
        for j, (co, cn) in enumerate(zip(ro, rn)):
            if co == '.' and cn in 'XO':
                adds.append((cn, LETTERS[j] + str(n - i)))
    return adds[0] if len(adds) == 1 else None




def game_record(n, move_no, c, mv):
    GAME_MOVES.append({'no': move_no, 'c': c, 'mv': mv, 'n': n})




def save_game_sgf(final=False, reason=''):
    """写 SGF: 每手后滚动覆盖 last_game.sgf; 终局写带时间戳文件"""
    if not GAME_MOVES:
        return
    try:
        gdir = os.path.join(TOOLS, 'games')
        os.makedirs(gdir, exist_ok=True)
        ai = MY_SIDE or 'black'
        head = (f'(;GM[1]FF[4]CA[UTF-8]AP[ZCode-GoAI]'
                f'SZ[{GAME_MOVES[0]["n"]}]KM[7.5]'
                f'DT[{time.strftime("%Y-%m-%d %H:%M:%S")}]'
                f'PB[黑]PW[白]C[AI执'
                f'{"黑" if ai == "black" else "白"}'
                + (f'; {reason}' if reason else '') + ']')
        body = []
        for e in GAME_MOVES:
            c = 'B' if e['c'] == 'X' else 'W'
            ann = ''
            if 'bw' in e:
                ann = f'C[bw{e["bw"]:.1f}% lead{e["lead"]:+.1f}]'
            body.append(f';{c}[{e["mv"].lower()}]{ann}')
        if final:
            fname = os.path.join(gdir,
                                 time.strftime('game_%Y%m%d_%H%M%S.sgf'))
        else:
            fname = os.path.join(gdir, 'last_game.sgf')
        with open(fname, 'w', encoding='utf-8') as f:
            f.write(head + ''.join(body) + ')')
        if final:
            print(f'✔ 棋谱已存 {fname} ({len(GAME_MOVES)}手)')
    except Exception:
        pass




def trend_reset():
    try:
        open(TREND_FILE, 'w').close()
    except Exception:
        pass




def trend_record(board_n, board_cur, move_no, to_move=None):
    """记录第 move_no 手后黑方视角的胜率/目差(供 UI 画趋势图)。

    to_move = 该时刻实际行棋方; 问错行棋方会把'谁先走谁赢'的
    大龙对杀局面评估成镜像(例如我方大优被记成大劣)。"""
    try:
        legal = br.stones_legal(board_n, board_cur)
        if to_move is None:
            nb = sum(1 for s in legal if s[0] == 'b')
            to_move = 'white' if nb > len(legal) - nb else 'black'
        _, _inf, _rt = analyze_position(board_n, legal, to_move,
                                             visits=150)
        if _rt is None:
            return
        bw, lead = root_view(_rt, 'black')   # 统一换算成黑方视角存储
        rec = {'m': move_no,
               'bw': round(bw * 100, 2),
               'lead': round(lead, 2),
               'my': MY_SIDE or 'black'}
        with open(TREND_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + chr(10))
        # 给 SGF 本手注释(黑方视角胜率/目差), 并滚动落盘防崩溃丢失
        if GAME_MOVES and GAME_MOVES[-1].get('no') == move_no:
            GAME_MOVES[-1]['bw'] = round(bw * 100, 2)
            GAME_MOVES[-1]['lead'] = round(lead, 2)
        save_game_sgf(final=False)
    except Exception:
        # 趋势写失败不再静默: 同类只报一次, 避免图表悄悄停更
        import traceback as _tb
        _k = 'trend:' + str(sys.exc_info()[1])[:80]
        if _k != _trend_err.get('k'):
            _trend_err['k'] = _k
            _tb.print_exc()
            print(f'!! 趋势记录失败: {sys.exc_info()[1]}, 图表将不更新')


# ---- 视觉行棋(金框唯一判据)全局 ----
VIS_OVERRIDE_S = 15.0   # 视觉显示对方回合超过该时长则放行(防卡死)
# 回合锁定: 任何"确定的回合翻转"(board-change/落子/弹窗)之后进入锁窗,
# 锁窗内屏蔽"等待期视觉校正"翻回合。根因=对方落子后金框横幅滞后数秒仍显示
# 我方行棋, 旧逻辑(门槛12s)在滞后期内连续两次视觉=我方会把回合误翻回我方。
# 15s > 实测滞后窗(<8s)留余量; 锁窗覆盖滞后全程, 之后视觉校正才生效(真虚着
# 仍能救回, 仅延迟约 锁窗+连续两次采样)。
TURN_LOCK_SECS = 15.0
TURN_LOCK = {'until': 0.0}

# ---- 点击/悬停状态 ----
_rend = None
LAST_RES = None  # 最近一次成功读盘的网格(用于鼠标悬停检测)

# ---- 引擎/记录状态 ----
PRE_VISITS = 350      # 对方回合预热基础算力(等待越久阶梯加深)
PRE = {'key': None, 'mv': None, 'info': None, 'root': None,
       't': 0.0, 'lvl': 0}
TREND_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       'katago_trend.jsonl')
_trend_err = {}          # 趋势记录异常去抖

# ---- 角标观察线程状态 ----
_OBS_SIDE = [None, 0.0]      # 后台角标持续采样结果 [颜色, 时间戳]
_OBS_SIDE_LOG = [None, 0.0]  # [上次记录的颜色, 时间] (日志去抖)
_GAME_ACTIVE = [False]       # 主循环最近是否成功读到棋盘(后台采样节流)


CLIENT = KataClient()  # 常驻引擎: 每步省 ~4s 启动
atexit.register(CLIENT.close)

KATAGO = r'E:\game\GoAI\katago\katago.exe'
MODEL19 = r'E:\game\GoAI\networks\kata1-b18c384nbt-s9996604416-d4316597426.bin.gz'
MODEL9 = r'E:\game\GoAI\networks\kata9x9-b18c384nbt-20231025.bin.gz'
CFG = r'E:\game\GoAI\katago\analysis_example.cfg'
LETTERS = 'ABCDEFGHJKLMNOPQRST'
VISITS = 500
STALL_S = 90      # 干等看门狗阈值
FAIL_CAP = 4      # 连续落子失败上限(轮)
ERR_CAP = 60      # 连续读盘异常上限(次)
VISITS = 8000     # 默认算力(--visits 覆盖)。实测本机(RTX3050, GPU热身后):
                    # 19/9路均 ~3000-4500 visits/s(500≈0.15s 2000≈0.6s 8000≈2.5s 引擎时间)
WAIT_NEW = False  # --wait-new: 终局后等待新局自动继续
BEEP_ON = False   # --beep: 终局/停止时提示音
SIZE_FIX = 0      # --size 9/13/19: 固定棋盘尺寸(0=自动检测)


_plast = {}  # 降噪: 消息节流


# 视觉行棋判定: 特征 = 绿框整块"异占比"(偏离框背景色的像素比例)。
# 注: 旧的"绿框整框异占比(obox)"判据及其自学习参考(_VIS_REF)已彻底删除,
# 行棋判定统一由"金色倒计时空心框"(winclick.gold_frame_ratio)独家负责。


def plog(key, interval, msg):
    import time as _t
    now = _t.time()
    if now - _plast.get(key, 0) >= interval:
        _plast[key] = now
        print(msg)
        return True
    return False


def beep():
    if BEEP_ON:
        try:
            import winsound
            winsound.MessageBeep()
        except Exception:
            pass






# 整窗 OCR 文本缓存(3s 缓存供失败时判断当前页面)
_page_sniff_cache = [None, 0.0]   # [文本, 时间戳]


def page_text_sniff():
    """整窗 OCR 文本(缓存 3s), 用于失败时判断当前是什么页面"""
    now = time.time()
    if _page_sniff_cache[1] and now - _page_sniff_cache[1] < 3:
        return _page_sniff_cache[0]
    try:
        texts = ''.join(t for t, _, _ in ocr_window_text())
    except Exception:
        texts = ''
    _page_sniff_cache[:] = [texts, now]
    return texts


# 仅保留"绝不会出现在对局页"的词(复盘/聊天/胜率曲线等是
# 绝艺房间侧栏菜单词, 不能当非对局特征)
NON_GAME_WORDS = ('我的棋谱', '职业棋谱', '最近打开', '对局结束',
                  '终局方式', '全部路数', '协商和局', '中盘胜',
                  '目胜', '和局', '黑胜', '白胜')


def page_no_game_reason():
    """当前页面是否非对局页: 命中返回原因文本, 否则 None"""
    texts = page_text_sniff()
    hits = [w for w in NON_GAME_WORDS if w in texts]
    if hits and '行棋' not in texts:
        return ('检测到非对局页面特征 ' + str(hits[:3])
                + ' (复盘/棋谱/结算等页面无行棋指示)')
    return None


def _resolve_new_game_turn(assist, total, samples=6, gap=0.5):
    """新局重置时确定 turn。

    盘面为空(0子): 黑先手, turn='black'(原逻辑)。
    盘面非空: 说明对方可能已落首手(该盘面会被设为 last_counts 基线, 此后
    counts 恒等于基线而永不触发落子检测), 此时绝不能用"空盘黑先"假设, 否则
    turn 会永久停在 black —— 表现为"轮到我方(金框/角标均正确)却不落子"的
    静默卡死。改用金框(谁行棋)判定: 金框=我方 -> turn=assist, 否则对方。
    多次采样取中位数, 抗开局瞬时金色 UI 假阳性。
    """
    if total <= 0:
        return 'black'
    try:
        import winclick as _wc
        import board_reader as _br
        _rw = _br.window_rect(_br.PID)
    except Exception:
        return 'black'
    _gfs = []
    for _i in range(samples):
        try:
            _gf = _wc.gold_frame_ratio(_rw, None)
            if _gf is not None:
                _gfs.append(_gf)
        except Exception:
            pass
        time.sleep(gap)
    if not _gfs:
        print('  [新局轮次] 金框采样失败, 退回黑先')
        return 'black'
    _gfs.sort()
    _med = _gfs[len(_gfs) // 2]
    _t = assist if _med >= _wc.GOLD_THR else other(assist)
    print(f'  [新局轮次] 金框中位={_med:.3f} (盘面{total}子) -> '
          f'轮到{"黑" if _t == "black" else "白"}')
    return _t


def end_or_wait(msg):
    """终局/异常出口: 打印原因; wait_new 模式下自动点[重新匹配/续战]进入下一盘,
    返回新局读数 (n,board,counts,res); 否则返回 None(停止)。"""
    global AUTO_NEXT, GAMES_DONE, GAME_MOVES, GAME_LAST_KEY, CAP_HOLD
    _set_st(force=True, status='gameover', mv=msg[:40])
    print(msg)
    beep()
    save_game_sgf(final=True, reason=msg[:50])
    if not WAIT_NEW:
        return None
    AUTO_NEXT = True
    print('--wait-new: 自动检测结算页并点击 [重新匹配] (10分钟内)...')
    deadline = time.time() + 600
    clicked = False
    last_ocr = 0.0
    ocr_tries = 0
    xuzhan = False   # 找不到重新匹配时允许点[续战]
    while time.time() < deadline:
        # 1) 已在下一盘(小盘面)?
        try:
            g = read_board_counts()
        except Exception:
            g = None
        if g is not None and g[2][0] + g[2][1] <= 4:
            br.clear_grid_cache()   # 新局棋盘位置可能不同, 清旧缓存
            GAME_MOVES.clear()      # 新局另起 SGF 记录
            GAME_LAST_KEY = None
            CAP_HOLD = 0.0
            KO_BAN['point'] = None  # 新局清劫禁(防跨局残留)
            KO_BAN['seq'] = -1
            # 新局清空趋势数据: 否则旧局记录残留, 且新局手数从0重来会让
            # 趋势折线横跳(旧局尾 -> 新局头), 图上出现无意义的大跳变。
            try:
                open(TREND_FILE, 'w', encoding='utf-8').close()
            except Exception:
                pass
            GAMES_DONE += 1
            if MAX_GAMES and GAMES_DONE >= MAX_GAMES:
                print(f'!! 已达设定局数上限({MAX_GAMES}盘), 停止')
                return None
            print(f'检测到新局(第{GAMES_DONE}盘), 继续')
            _set_st(force=True, game=GAMES_DONE, status='new',
                    move_no=0, mv='', wr=None, lead=None)
            return g
        # 2) OCR 找 [重新匹配] 按钮并点击(每 ~6s 一次, 最多试 10 次)
        now = time.time()
        if not clicked and now - last_ocr > 6:
            last_ocr = now
            ocr_tries += 1
            if ocr_tries >= 10 and not xuzhan:
                # 找不到[重新匹配]: 后半程允许点[续战](对方认输后常只有续战)
                xuzhan = True
                ocr_tries = 0
                print('未找到[重新匹配], 接下来尝试[续战]...')
            if ocr_tries >= 10 and xuzhan:
                print('!! 未找到 [重新匹配/续战] 按钮, 停止自动续战。')
                return None
            keys = (REMATCH_KEYS + ('续战',)) if xuzhan else REMATCH_KEYS
            for text, cx, cy in ocr_window_text():
                if any(k in text for k in keys):
                    print(f'检测到按钮 [{text}] @({cx},{cy}), 点击')
                    click_at(cx, cy)
                    clicked = True
                    time.sleep(1.5)
                    if winclick.click_confirm():
                        print('✔ 已自动确认弹窗')
                    break
        time.sleep(3)
    print('等待新局超时, 停止')
    return None


_pop_hist = {}   # 弹窗应答去抖: {事件: 上次点击时间}


def scan_popups():
    """对局中弹窗智能应答(整窗 OCR 一次):
    和棋 -> 点[拒] ; 对方认输 -> 点[确定] ; 数子请求 -> 点[同意];
    停一手/虚着 -> 返回 'pass' 由主循环按弹窗语义切轮次。
    返回事件名或 None; 8s 内同事件不重复点。"""
    try:
        items = winclick.ocr_buttons()
    except Exception:
        return None
    J = ''.join(l for l, *_ in items)
    now = time.time()

    def click(part, sig, why):
        for l, cx, cy, _w, _h in items:
            if part in l:
                if now - _pop_hist.get(sig, 0) < 8:
                    return False
                click_at(cx, cy)
                _pop_hist[sig] = now
                print(f'[弹窗应答] {why}: 点击[{l}]')
                return True
        return False

    if '和棋' in J:
        if click('拒', 'draw', '对方申请和棋(一律拒绝)'):
            return 'draw_reject'
    if '投子认输' in J or '对方认输' in J:
        if click('确定', 'resign', '对方投子认输'):
            return 'resign_ok'
    if '请求数子' in J or ('数子' in J and '同意' in J):
        if click('同意', 'count', '对方请求数子(同意)'):
            return 'count_ok'
    if '停一手' in J or '虚着' in J or '过一手' in J:
        return 'pass'
    if '重连' in J and '对局已结束' in J:
        if click('确定', 'reconnect_end', '重连成功-对局已结束'):
            return 'reconnect_end'
    if '匹配超时' in J and '重新匹配' in J:
        # 匹配超时提示: 一律点[确定](重新匹配), 继续等待新局。
        # 实测该弹窗按钮文案为"确定"(非"确认"), 且另有"取消"按钮, 须精确匹配
        # "确定"以免误点取消。
        if click('确定', 'match_timeout', '匹配超时-重新匹配'):
            return 'match_timeout'
    return None



def click_target_profile(res, i, col):
    """点击目标点邻域剖面: 返回 (暗占比, 彩占比, 亮占比, 均亮) 或 None。
    剖面按 res 的网格坐标采样(与读盘同一坐标系)。
    引擎读空但暗占比>=0.35 且亮占比低 => 该点视觉上有子,
    疑似读盘漏子/网格错位(点已有子的点会被客户端静默忽略)。"""
    try:
        import numpy as _np
        if res is None:
            return None
        im = res.get('img')
        if im is not None:
            a = _np.asarray(im).astype(int)
        else:
            from PIL import ImageGrab
            a = _np.asarray(
                ImageGrab.grab(bbox=res['rect']).convert('RGB')).astype(int)
        xs, ys = res['xs'], res['ys']
        if i >= len(ys) or col >= len(xs):
            return None
        gx, gy = int(xs[col]), int(ys[i])
        _h = max(6, int(res.get('step', 27) * 0.55))
        c = a[gy - _h:gy + _h + 1, gx - _h:gx + _h + 1]
        if c.size == 0:
            return None
        chm = c.max(axis=2) - c.min(axis=2)
        lum = c.mean(axis=2)
        df = float(((lum < 92) & (chm < 60)).mean())
        bf = float(((lum > 238) & (chm < 40)).mean())
        cf = float((chm > 60).mean())
        return df, cf, bf, float(lum.mean())
    except Exception:
        return None




def read_board_counts():
    """读盘(悬停防护): 路数由网格自动检测决定(OCR 标题偶发误读会在缓存
    期内整盘按错尺寸读=周期性错乱); 用户 --size 指定时仍强制。
    返回 (n, board, counts, res) 或 None"""
    global _read_cnt, SIZE_FIX
    if '--size' not in sys.argv:
        SIZE_FIX = 0   # 信任自动检测(多余线刷分问题已修)
    for _ in range(4):           # 悬停预算 ~1s, 之后用屏蔽而非干等
        if not cursor_hovers_board():
            break
        time.sleep(0.25)
    _read_cnt += 1
    do_align = (_read_cnt % 10 == 1)
    res = br.read_current(do_align=do_align, force_size=SIZE_FIX,
                          use_calib=False)
    if res is None:
        return None
    # 悬停屏蔽: 仅当光标压着的是空点时标记 '?'(游戏会在空点画悬停假子,
    # 屏蔽它防假子; 光标压在真棋子上时绝不屏蔽——真子被藏会导致"无新增
    # 消失"反复触发, 误判成结算)
    hov = cursor_hover_cell(res)
    if hov is not None:
        hi, hj = hov
        if res['board'][hi][hj] == '.':
            board = [list(r) for r in res['board']]
            board[hi] = ''.join(c if k != hj else '?'
                                for k, c in enumerate(board[hi]))
            board = [''.join(r) for r in board]
            res['board'] = board
        elif _hover_square(res, hi, hj):
            # 光标下方块被读成我方子(布局方块>=14px 时): 屏蔽为 '?'。
            # 圆棋子四角为木色不会命中, 我方刚落子的真子不受影响。
            board = [list(r) for r in res['board']]
            board[hi] = ''.join(c if k != hj else '?'
                                for k, c in enumerate(board[hi]))
            res['board'] = [''.join(r) for r in board]
    LAST_RES = res
    n, board = res['n'], res['board']
    counts = (sum(r.count('X') for r in board),
              sum(r.count('O') for r in board))
    return n, board, counts, res


_read_cnt = 0


def other(side):
    return 'white' if side == 'black' else 'black'


# 终局后只点'重新匹配'(匹配新对手); 不点'续战'(与原对手重赛)
REMATCH_KEYS = ('重新匹配',)
AUTO_NEXT = False  # 已发生过终局->续战(触发新局先手识别)
GAMES_DONE = 0
MAX_GAMES = 0     # --max-games N: 自动续战最多 N 盘
DETECT_FIRST = False  # --detect-first: 空盘启动也做先手/执子识别
MY_SIDE = None           # 我方执色(启动时由 main 设置, 记录用)

GAME_MOVES = []       # 本局着法(终局写 SGF): {'no','c'(X/O),'mv','n',+bw/lead}
GAME_LAST_KEY = None  # 已记录着法后的盘面键(防同手重复记录)
# 劫禁(手顺法): 'point'=对方上一手恰好提我方1子的被提点(GTP),
# 'seq'=该禁令生效的我方落子回合序号(仅该回合禁, 下回合自动解禁)。
# 模块级(跨局/主循环与 end_or_wait 共享); 改动字典内容无需 global。
KO_BAN = {'point': None, 'seq': -1}
LAST_BOARD = None     # 上一已确认盘面(推算对方落点)
CAP_HOLD = 0.0        # 最近一次含提子的变化接受时刻(动画稳定窗口)
SETTLE_S = 0.6        # 稳定窗口: 该时段内提交分析前须重读一次盘核对
                      # (重读确认稳定即继续, 不再卡满整窗)


def ocr_window_text():
    """OCR 当前窗口, 返回 [(text, 屏幕中心x, 屏幕中心y), ...]"""
    import json
    from PIL import ImageGrab
    try:
        rect = br.window_rect(br.PID)
    except SystemExit:
        return []
    img = ImageGrab.grab(bbox=rect).convert('RGB')
    png = winclick.save_tmp(img, 'ocr')
    items = winclick.ocr_items(png)
    out = []
    for it in items:
        out.append((it.get('text', ''), rect[0] + it.get('x', 0)
                    + it.get('w', 0) // 2,
                    rect[1] + it.get('y', 0) + it.get('h', 0) // 2))
    return out


def main():
    global VISITS, WAIT_NEW, BEEP_ON, SIZE_FIX, DETECT_FIRST, MAX_GAMES
    global MY_SIDE, LAST_BOARD, GAME_LAST_KEY, CAP_HOLD
    _tee_out = None
    if getattr(sys.stdout, 'isatty', lambda: False)():
        # 命令行启动: 输出同时镜像到 katago_ui.log, 打开的 UI 日志区可见
        try:
            _logf = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      'katago_ui.log'), 'a', encoding='utf-8',
                         buffering=1)
            _real_out = sys.stdout

            class _Tee:
                def write(self, s):
                    _real_out.write(s)
                    try:
                        _logf.write(s)
                    except Exception:
                        pass
                    return len(s)

                def flush(self):
                    _real_out.flush()
                    try:
                        _logf.flush()
                    except Exception:
                        pass

            sys.stdout = _Tee()
            _tee_out = _logf
        except Exception:
            pass
    try:
        _cf.cleanup_now()   # 启动清理上次残留临时文件
    except Exception:
        pass
    assist = 'auto'   # auto=自动识别执子(黑/白可显式指定)
    args = [a for a in sys.argv[1:] if not a.startswith('-')]
    if args:
        assist = args[0]
    assert assist in ('auto', 'black', 'white')
    turn = None
    if '--turn' in sys.argv:
        i = sys.argv.index('--turn')
        turn = sys.argv[i + 1]
        assert turn in ('black', 'white')
    if '--visits' in sys.argv:
        i = sys.argv.index('--visits')
        VISITS = int(sys.argv[i + 1])
    print(f'算力: {VISITS} visits/手'
          + (f' (引擎实测约{VISITS/3000:.1f}s/手, 未含读盘/OCR, '
             f'2000以下基本无感)' if VISITS >= 3000 else ''))
    if '--size' in sys.argv:
        i = sys.argv.index('--size')
        SIZE_FIX = int(sys.argv[i + 1])
        assert SIZE_FIX in (9, 13, 19)
    if '--mode' in sys.argv:
        i = sys.argv.index('--mode')
        br.set_game_mode(sys.argv[i + 1])
    WAIT_NEW = '--wait-new' in sys.argv
    BEEP_ON = '--beep' in sys.argv
    DETECT_FIRST = '--detect-first' in sys.argv
    if '--max-games' in sys.argv:
        MAX_GAMES = int(sys.argv[sys.argv.index('--max-games') + 1])

    def _warm():
        try:
            CLIENT.query({'id': 'warm', 'moves': [], 'initialPlayer': 'black',
                          'initialStones': [], 'rules': 'chinese',
                          'komi': 7.5, 'boardXSize': 19, 'boardYSize': 19,
                          'analyzeTurns': [0], 'maxVisits': 8})
        except Exception:
            pass
    threading.Thread(target=_warm, daemon=True).start()
    side = ('自动' if assist == 'auto'
            else ('黑' if assist == 'black' else '白'))
    if assist != 'auto':
        print(f'注: 手动指定执色={side}, 将跳过角标自动识别'
              '(UI 选回"自动识别"即可恢复)')

    # 等待游戏窗口出现(用户可能还没打开腾讯围棋/没在对局画面)
    win_found = False
    for _w in range(30):
        try:
            br.window_rect(br.PID)
            win_found = True
            break
        except SystemExit:
            if _w == 0:
                print('未找到腾讯围棋窗口: 请打开腾讯围棋并对局画面, '
                      '最长等待 30 秒...')
            time.sleep(1)
    if not win_found:
        print('!! 未找到腾讯围棋窗口(需对局画面), 请打开后重试')
        return

    print('读取初始盘面(稳定化中)...')
    # 弹窗自动应答(3s 节流): 求和/数子/认输弹窗在启动阶段同样会盖住
    # 棋盘, 主循环有应答、稳定化/wait-new 阶段此前没有, 会被干等 30s
    _pop_last = [0.0]

    def _pop_scan():
        if time.time() - _pop_last[0] > 3.0:
            _pop_last[0] = time.time()
            try:
                scan_popups()
            except Exception:
                pass

    init = None
    for _try in range(15):
        try:
            g1 = read_board_counts()
            _d1 = ('%d路 黑%d白%d src=%s' % (g1[0], g1[2][0], g1[2][1],
                                             g1[3].get('src'))
                   if g1 else None)
        except Exception as _e1:
            g1 = None          # 读盘瞬时异常(网格细化退化等): 当 None 重试
            _d1 = '异常:%s' % _e1
        if g1 is None:
            time.sleep(0.4)
            _pop_scan()   # 弹窗可能正盖住棋盘
            continue
        time.sleep(0.25)
        try:
            g2 = read_board_counts()
            _d2 = ('%d路 黑%d白%d src=%s' % (g2[0], g2[2][0], g2[2][1],
                                             g2[3].get('src'))
                   if g2 else None)
        except Exception as _e2:
            g2 = None
            _d2 = '异常:%s' % _e2
        if g2 is None:
            time.sleep(0.4)
            _pop_scan()
            print('  稳定化#%d: 第%d读=None (%s) 第%d读=None (%s), 重试' %
                  (_try + 1, 1, _d1, 2, _d2))
            continue
        if g2[2] != g1[2]:
            time.sleep(0.4)
            _pop_scan()
            print('  稳定化#%d: 两次计数不一致 %s vs %s, 重试' %
                  (_try + 1, _d1, _d2))
            continue
        # OCR 标题路数纠错: 校准/自动检测的路数与标题不符时切换
        if SIZE_FIX == 0:
            osz = winclick.ocr_board_size()
            if osz and osz != g2[0]:
                SIZE_FIX = osz
                print(f'OCR标题指示 {osz} 路(检测到 {g2[0]} 路), '
                      f'切换为按 {osz} 路自动检测, 重新稳定化...')
                continue   # 用新尺寸重新读盘稳定化
        init = g2
        break
    if init is None:
        if WAIT_NEW:
            print('当前无对局画面, --wait-new: 等待新对局出现(10分钟)...')
            deadline = time.time() + 600
            while time.time() < deadline:
                time.sleep(2)
                _pop_scan()   # 待机时弹窗(求和/数子/结算提示)自动应答
                g1 = read_board_counts()
                if g1 is None:
                    continue
                time.sleep(0.4)
                g2 = read_board_counts()
                if g2 is not None and g2[2] == g1[2]:
                    init = g2
                    break
            if init is None:
                print('等待对局超时, 停止')
                return
        else:
            print('初始盘面不稳定或不可见(可能被遮挡/非对局界面), '
                  '请确认棋盘画面后重启')
            return
    n, board, counts, res_cur = init
    last_n = n
    pending_n = None      # 尺寸变化待确认(过渡垃圾帧防抖)
    if assist == 'auto':
        # 执色判定只信官方头像角标(颜色识别), 不做任何文本/观察/试探推断
        av = None
        # 角标读不到常见(动画/掉线通知遮挡), 重试 ~15s 捕捉可见帧
        for _r in range(14):
            try:
                av = winclick.avatar_my_color()
            except Exception:
                av = None
            if av is not None:
                break
            time.sleep(0.8)
        if av is not None:
            assist = av
            print(f'执子识别(头像角标颜色): 我方执'
                  f'{("黑" if assist=="black" else "白")}')
            turn = None     # 启动锚定: 绿框视觉判定(唯一依据)
        else:
            # 执色只认头像角标(名字行的 黑方/白方 是行棋指示文字, 非持子标记,
            # 轮到对方时它会显示对方颜色, 不能用于判我方执色)
            pass
        if av is None:
            print('!! 无法从头像角标确认执色(执色只以角标颜色为准)。')
            if not os.path.exists(winclick.CALIB_AVATAR):
                print('   请先运行 avatar_calib.py 框选双方头像角标棋子')
            else:
                reason = page_no_game_reason()
                if reason:
                    print('   ' + reason)
                print('   角标可能被遮挡或页面不对(需对局页且双方头像可见), '
                      '请检查后重启')
            if any(k in page_text_sniff() for k in
                   ('协商和局', '中盘胜', '目胜', '和局', '对局结束',
                    '终局方式')):
                print('   (页面显示对局已结束/结算, 请开新局后再启动)')
            elif '掉线' in page_text_sniff():
                print('   (对方掉线中: 掉线通知期间角标不可读属正常, '
                      '等判负结算后开新局再启动; 若对方回来会恢复)')
            print('   人工兜底: 回到界面选[我执子/当前轮到]后点[启动]重试;')
            print('   或命令行: python katago_play.py 白 --turn 白 '
                  '--size 19 --visits 500 --wait-new')
            return
        # 重新读盘建立基准(颜色识别不落子, 盘面应未变)
        rb = read_board_counts()
        if rb is not None:
            n, board, counts, res_cur = rb
        else:
            print('!! 识别后重读盘失败, 退出')
            return
        if turn is None:
            board, counts = strip_hover_square(board, counts, res_cur,
                                               assist)
            # 启动锚定: 绿框视觉为唯一依据, 不可判则人工兜底
            turn, _asrc = anchor_turn_visual(counts, assist, res_cur)
            if turn is None:
                print('!! 无法锚定当前轮次(绿框不可判), 已停止。')
                return
            print(f'锚定({_asrc}): 轮到{("黑" if turn=="black" else "白")}')
        MY_SIDE = assist
        side = '黑' if assist == 'black' else '白'
        print(f'锚定: 轮到{("黑" if turn=="black" else "白")} | '
              f'盘面黑{counts[0]}白{counts[1]}')
        _set_st(force=True, assist=assist, turn=turn, turn_src=_asrc,
                n=n, b=counts[0], w=counts[1], move_no=0, wr=None,
                lead=None, mv='', status='wait',
                engine='')
    MY_SIDE = assist            # 无论 auto/手动, 记录我方执色
    LAST_BOARD = board          # SGF 盘面基线
    trend_reset()
    trend_record(n, board, 0,
                  (turn or ('white' if counts[0] > counts[1]
                            else 'black')))  # 初始盘面第0点
    # 场景安检: 复盘/棋谱/结算等页面禁止启动(会对着假棋盘乱下),
    # 手动指定执色/轮次也不能绕过(除非 --force-any 强制)
    if '--force-any' not in sys.argv:
        reason = page_no_game_reason()
        if reason:
            print('!! ' + reason + ', 拒绝启动。')
            print('   请在真正的对局画面下启动; '
                  '复盘/棋谱/结算/大厅不可自动下棋')
            return
    print('网格来源:', res_cur['src'])
    if SIZE_FIX and n != SIZE_FIX:
        print(f'!! 指定下 {SIZE_FIX} 路, 但检测到 {n} 路棋盘, 请确认对局后重试')
        return
    if turn is None:
        # 兜底锚定: 绿框视觉为唯一依据(手动执色路径同理), 不可判则停止
        _rc2 = locals().get('res_cur')
        board, counts = strip_hover_square(board, counts, _rc2, assist)
        turn, _asrc = anchor_turn_visual(counts, assist, _rc2)
        if turn is None:
            print('!! 无法锚定当前轮次(绿框不可判), 已停止。')
            return
        print(f'锚定({_asrc}): 轮到{("黑" if turn=="black" else "白")}')
    print(f'执{side} | 锚定: 轮到{("黑" if turn=="black" else "白")} '
          f'| 盘面黑{counts[0]}白{counts[1]} | Ctrl+C 停止')

    last_counts = counts        # 已确认的盘面基准
    acted_counts = (-1, -1)     # 已行动时的盘面(占位=立即行动)
    cand = None                 # 变化确认: (counts, 连续次数)
    last_activity = time.time()
    failed_cycles = 0
    err_streak = 0
    last_beat = 0.0
    _chip_samp_at = 0.0      # 视觉观察节拍: 色块 2s 采样
    _sig_last = None         # 绿框状态(变化时记录)
    _sig_stalled_at = 0.0     # 画面停滞提示节流
    _vis_gate_t = 0.0        # 行动门视觉采样节流
    _vis_opp_since = 0.0     # 视觉连续显示"对方回合"的起始时刻(0=无)
    _gold_latest = [None]    # 最近一次金框读数(供"金框纠正闸"判定)
    _gold_my_since = [0.0]   # 金框连续显示"我方"的起始时刻(0=无)
    last_pop = 0.0           # 弹窗应答扫描节流
    last_change = time.time()  # 最近一次盘面变化(显示对方思考时长)
    opening_since = None   # 自动续战新局(空盘)的开始时刻
    opening_probed = False
    opening_av_at = 0.0   # 新局角标颜色重试时刻
    opening_blk_n = 0     # 开局连续确认执黑次数
    opening_garb_at = 0.0 # 最近一次开局垃圾帧(倒计时提示遮罩)时刻
    consec_bad = 0          # 连续方向校验丢弃次数
    vanish_sum = 0          # 无新增消失累计子数
    vanish_max = 0          # 单次无新增消失最大子数(区分噪声/真清盘)
    last_wr = None          # 上一手我方胜率(骤降自查用)
    move_no = 0             # 记录总手数(趋势图 x 轴)
    extreme_count = 0       # 双视角极端分化连续次数
    last_flip = 0.0         # 上次自动翻转执色时间
    bad_points = set()      # 被拒落点黑名单(劫争等), 盘面变化时清空
    ko_state = {'point': None, 'turn': -1}  # 劫点及被禁的我方回合序号
    my_turn_seq = 0         # 每进入我方落子分支自增, 约束劫禁只限当回合
    # 劫禁(手顺法, 主防线): 记录"对方上一手恰好提我方1子"的被提点; 该点本回合
    # 禁落(提回会重现局面=劫规禁止)。比几何 detect_ko 可靠——不依赖劫形识别,
    # 直接反映真实手顺, 可覆盖边线/非标准劫形(几何法易漏判)。
    # 注意: 只重置字段(共享模块级 KO_BAN, 勿重新绑定为局部)
    KO_BAN['point'] = None
    KO_BAN['seq'] = -1
    _last_err = {}          # 异常去抖

    # 角标持续采样后台线程(并行观察, 仅记录; 真值确认后再并入判定)
    threading.Thread(target=color_observer_loop, daemon=True).start()

    while True:
        try:
            got = read_board_counts()
            if got is None:
                err_streak += 1
                # 弹窗应答: 读不到棋盘常因弹窗遮挡(求和/数子/认输会被自动
                # 应答; 20s 倒计时结束默认拒绝并回棋盘)。静置等待期间持续扫
                if time.time() - last_pop > 3.0:
                    last_pop = time.time()
                    try:
                        scan_popups()
                    except Exception:
                        pass
                if err_streak >= 15:
                    # 静置 ~25-30s 仍未回棋盘(求和/数子倒计时仅20s,
                    # 会自动回棋盘)才判定: 真结算页/持续遮挡
                    _GAME_ACTIVE[0] = False
                    g2 = end_or_wait('!! 棋盘持续不可见约30s'
                                     '(已排除求和/数子弹窗, 疑似终局结算)')
                    if g2 is None:
                        return
                    n, board, counts, res_cur = g2
                    last_counts = counts
                    acted_counts = (-1, -1)
                    turn = 'black'
                    cand = None
                    last_activity = time.time()
                    failed_cycles = 0
                    err_streak = 0
                    continue
                time.sleep(1.5)
                continue
            err_streak = 0
            n, board, counts, res_cur = got
            _GAME_ACTIVE[0] = True
            total = counts[0] + counts[1]

            # 棋盘尺寸变化(如 19->9 路新局): 连续两轮同尺寸才确认,
            # 防提示框/转场动画的垃圾帧把尺寸读花
            if n == last_n:
                pending_n = None
            elif pending_n is None:
                pending_n = n
                time.sleep(0.4)
                continue        # 第一轮见到新尺寸: 等下一轮确认
            elif n == pending_n:
                # 连续两轮同尺寸: 确认真实换路数
                pending_n = None
                br.clear_grid_cache()
                last_n = n
                last_counts = counts
                acted_counts = (-1, -1)
                turn = 'black' if counts[0] <= counts[1] else 'white'
                cand = None
                last_activity = time.time()
                failed_cycles = 0
                print(f'!! 棋盘尺寸确认为 {n} 路, 已重置基准 '
                      f'(轮到{("黑" if turn=="black" else "白")}, '
                      f'如颜色/轮次不对请重启并指定 --turn)')
            else:
                # 尺寸又变(过渡垃圾帧): 忽略本轮, 维持原基准
                pending_n = None
                continue

            # 尺寸校验
            if SIZE_FIX and n != SIZE_FIX:
                if WAIT_NEW:
                    print(f'[等待] 指定 {SIZE_FIX} 路, 当前读到 {n} 路, '
                          f'等待正确对局...')
                    time.sleep(5)
                    continue
                print(f'!! 指定下 {SIZE_FIX} 路, 但检测到 {n} 路棋盘, 停止')
                return

            # 终局保护
            if total > 0.80 * n * n:
                g = end_or_wait(f'!! 盘面几乎下满({counts[0]}:{counts[1]}), '
                                f'判定终局')
                if g is None:
                    return
                n, board, counts, res_cur = g
                last_counts = counts
                acted_counts = (-1, -1)
                turn = 'black'
                cand = None
                last_activity = time.time()
                failed_cycles = 0
                err_streak = 0
                print(f'新局: 黑{counts[0]}白{counts[1]}, '
                      f'轮到{("黑" if turn=="black" else "白")}, 继续')
                continue

            # 持续脏读数计数(方向校验连续丢弃过多时做场景自检)
            if counts == last_counts:
                consec_bad = 0
                vanish_sum = 0
                vanish_max = 0
            # 变化确认(连续两次一致才认定, 抗悬停/脏帧)
            if counts != last_counts:
                if cand is None:
                    cand = (counts, 1)
                elif cand[0] == counts:
                    cand = (counts, cand[1] + 1)
                    # 开局头几手(总子数<=2)需三读一致才接受(防提示框/转场垃圾帧)
                    need = 3 if total <= 2 else 2
                    if cand[1] >= need:
                        # 只接受"有新增子"的变化(落子必新增一颗);
                        # 行棋方由新增子颜色算术推断
                        db = counts[0] - last_counts[0]
                        dw = counts[1] - last_counts[1]
                        if db < 1 and dw < 1:
                            # 无新增子: 可能是悬停/消息框噪声, 也可能是劫争提子
                            # 中间态(对方提我方1子但落子方那1子尚未读出 => 差分呈
                            # 单色±1、另一色0)。劫争提子单次恰少1子, 直接刷新盘面
                            # 基准等稳定态(黑+1,白-1)出现再正常接受, 不按噪声丢弃
                            # 累加 consec_bad(否则劫争期盘面停滞且旧版会误触结算)
                            if db + dw == -1:
                                last_counts = counts
                                last_change = time.time()
                                consec_bad = 0
                                vanish_sum = 0
                                vanish_max = 0
                                print(f'(单子提中间态, 刷新基准: 黑{counts[0]}'
                                      f'白{counts[1]})')
                                cand = None
                                continue
                            # 其余无新增子(撤销/悔棋/消息框闪烁/悬停误读): 忽略。
                            # 悬停与闪烁消息框都是"单次消失1-3颗"的小噪声(容忍);
                            # 仅当"大片消失"(vanish_max>=8, 即数子遮挡/换局)才判
                            # 疑似结算。注意: 对方长考期的纯小噪声绝不判结算
                            _van = -(db + dw)
                            vanish_sum += _van
                            if _van > vanish_max:
                                vanish_max = _van
                            consec_bad += 1
                            if vanish_max >= 8 and consec_bad >= 14:
                                g3 = end_or_wait(
                                    f'!! 读数持续异常(连续{consec_bad}次'
                                    f'无新增消失, 单次最大{vanish_max}子, '
                                    f'疑似结算/换局)')
                                if g3 is None:
                                    return
                                n, board, counts, res_cur = g3
                                last_counts = counts
                                acted_counts = (-1, -1)
                                turn = 'black'
                                cand = None
                                last_activity = time.time()
                                failed_cycles = 0
                                err_streak = 0
                                consec_bad = 0
                                vanish_sum = 0
                                vanish_max = 0
                                continue
                            plog('dir', 5,
                                 f'!! 无新增子变化丢弃(黑{db:+d}白{dw:+d})')
                            cand = None
                            continue
                        # 幅度闸门: 单步净增 >3 只可能是提子动画错读,
                        # 除非距上次提交已很久(漏掉多手)
                        dbg = counts[0] - last_counts[0]
                        dwg = counts[1] - last_counts[1]
                        elapsed_c = time.time() - last_activity
                        big_jump = ((dbg > 3) or (dwg > 3))
                        if big_jump and elapsed_c < 20:
                            plog('jump', 5,
                                 f'!! 跳变丢弃(黑{dbg:+d}白{dwg:+d}) 等动画稳定')
                            cand = None
                            continue
                        # 落子预览方块防护: 我方回合光标压格会出现实心方块
                        # (我方执棋色), 会被误读成新增子。若"新增子"恰在
                        # 光标格, 判为预览方块: 只吸收基准, 不记落子/翻轮次
                        # (对方回合无方块, 光标格出现真落子仍正常接受)
                        if turn == assist:
                            _hv0 = cursor_hover_cell(res_cur)
                            if _hv0 is not None:
                                _hi0, _hj0 = _hv0
                                _add0 = None
                                if len(LAST_BOARD) == n:
                                    for _i0 in range(n):
                                        for _j0 in range(n):
                                            if LAST_BOARD[_i0][_j0] == '.' \
                                                    and board[_i0][_j0] in 'XO':
                                                _add0 = (_i0, _j0)
                                                break
                                        if _add0:
                                            break
                                if _add0 == (_hi0, _hj0):
                                    last_counts = counts
                                    LAST_BOARD = board
                                    last_change = time.time()
                                    consec_bad = 0
                                    vanish_sum = 0
                                    vanish_max = 0
                                    print('(悬停预览方块被忽略: 非真实落子)')
                                    continue
                        last_counts = counts
                        last_change = time.time()
                        consec_bad = 0      # 真实落子被接受 = 读盘正常
                        vanish_sum = 0
                        vanish_max = 0
                        # 落子后下一手是算术事实: 谁刚落子(有 +1 的一方),
                        # 下一手必是对方(横幅判定已取消, 不依赖 OCR)
                        mover = 'black' if db >= 1 else 'white'
                        turn = other(mover)
                        # 对方落子后轮到我方: 我方本回合尚未行动, 重置"已行动"
                        # 标记, 确保落子门(turn==assist and counts!=acted_counts)
                        # 一定放行。否则若 acted_counts 因故残留为当前盘面值,
                        # 会出现"轮到我却不落子"的静默卡死。
                        if turn == assist:
                            acted_counts = (-1, -1)
                        TURN_LOCK['until'] = time.time() + TURN_LOCK_SECS
                        print(f'[锁帧] 回合锁定{TURN_LOCK_SECS:.0f}s(对方落子) '
                              f'期间忽略视觉翻回合, 防金框滞后误翻')
                        cand = None
                        last_activity = time.time()
                        failed_cycles = 0
                        bad_points.clear()
                        # 提子动画稳定窗口: 任一方子数减少 => 本手含提子
                        if db < 0 or dw < 0:
                            CAP_HOLD = time.time()
                            # 劫禁(手顺): 对方恰好提我方1子 -> 该被提点本回合禁
                            # (提回会重现局面)。提>=2子不是劫(提回不重现), 不禁。
                            if mover != assist:
                                _mc = 'X' if assist == 'black' else 'O'
                                _cap = []
                                if len(LAST_BOARD) == n:
                                    for _y in range(n):
                                        for _x in range(n):
                                            if (LAST_BOARD[_y][_x] == _mc
                                                    and board[_y][_x] == '.'):
                                                _cap.append((_y, _x))
                                if len(_cap) == 1:
                                    _cy, _cx = _cap[0]
                                    KO_BAN['point'] = (LETTERS[_cx]
                                                       + str(n - _cy))
                                    # 下一次进入我方落子分支时(序号+1)生效
                                    KO_BAN['seq'] = my_turn_seq + 1
                                    print(f'[劫禁] 对方提我方1子 '
                                          f'@{KO_BAN["point"]}, 本回合禁提回')
                        print(f'检测到落子 -> 盘面黑{counts[0]}白{counts[1]}, '
                              f'轮到{("黑" if turn=="black" else "白")}')
                        evt('检测到落子')
                        global _MOVE_T
                        _MOVE_T = {'detected': time.time()}
                        if mover != assist:
                            # 对手落子后, 立即用其真实落点作 prefix 续算我方
                            # 应手(异步线程, 不阻塞检测循环)。把 4-5s 冷算移到
                            # "对手落子 -> 我落子"的间隙完成; 我方落子时按对手
                            # 落子后局面 KEY 复用缓存, 零延迟出招。
                            import threading as _th
                            _th.Thread(
                                target=_prefill_after_opp,
                                args=(n, list(map(list, LAST_BOARD)),
                                      list(map(list, board)), mover, assist),
                                daemon=True).start()
                        move_no += 1
                        _set_st(turn=turn, b=counts[0], w=counts[1],
                                move_no=move_no, status='wait', mv='')
                        # 记录本手(推算盘面新增点)供终局落 SGF
                        _key = tuple(board)
                        if _key != GAME_LAST_KEY:
                            _add = game_move_added(n, LAST_BOARD, board)
                            if _add:
                                game_record(n, move_no, _add[0], _add[1])
                            GAME_LAST_KEY = _key
                        LAST_BOARD = board
                        # 对方落子后轮到我们: 按真实行棋方记录
                        trend_record(n, board, move_no, turn)
                else:
                    cand = (counts, 1)
            else:
                cand = None

            # 心跳(诊断用); 行棋方由落子推算维护, 无横幅轮询
            now = time.time()
            if now - last_beat > 15:
                last_beat = now
                _set_st(turn=turn, b=counts[0], w=counts[1],
                        move_no=move_no, status='wait')
                print(f'[心跳] 等待中 | 轮到{("黑" if turn=="black" else "白")} '
                      f'| 盘面黑{counts[0]}白{counts[1]} | '
                      f'我方={("黑" if assist=="black" else "白")}'
                      + (f' | 对方已思考{int(now-last_change)}s'
                         if turn != assist else '')
                      + f' | acted={acted_counts}'
                      + (' (可落子)' if (turn == assist
                                         and counts != acted_counts) else ''))
                # 画面停滞提示: 等待超 90s 且绿框信号 60s 未变 =>
                # 微信窗口大概率后台冻结(不重绘), 提醒用户检查
                if turn != assist and now - last_change > 90:
                    if now - _sig_stalled_at > 60:
                        _sig_stalled_at = now
                        print('?? 等待>90s 且画面信号未变: 窗口可能已切后台'
                              '(微信不重绘), 请把游戏窗口点到前台')


            # ---- 视觉观察节拍(仅记录): 绿框竖线带 RGB 变化 ----
            # 每 2s 采样绿框内 RGB 概要, 概要变化 = 色块出现/消失/变色;
            # 与当前轮次对照收集真值。其余识别一律不做(按用户规格)。
            if now - _chip_samp_at >= 2.0:
                _chip_samp_at = now
                try:
                    _rw2 = br.window_rect(br.PID)
                    # 复用主循环刚截的帧(免二次截屏, 边际成本 ~5ms)
                    _arr2 = None
                    _im2 = res_cur.get('img') if res_cur else None
                    if _im2 is not None:
                        try:
                            import numpy as _np
                            _arr2 = _np.asarray(_im2).astype(_np.int16)
                        except Exception:
                            _arr2 = None
                    _gf2 = winclick.gold_frame_ratio(_rw2, _arr2)
                    _gold_latest[0] = _gf2    # 保存供"金框纠正闸"使用
                    _ws = ('' if _gf2 is None
                           else ' 金框%.3f(%s)' % (_gf2, '我方' if _gf2 >= winclick.GOLD_THR
                                                  else '对方'))
                    if _ws != _sig_last:
                        _sig_last = _ws
                        _turn_s = '黑' if turn == 'black' else '白'
                        print(f'[视觉行棋] 金框: {_ws} | 轮到{_turn_s}')
                except Exception:
                    pass

            # 对局中弹窗智能应答(每 ~3s): 和棋拒绝/认输确定/数子同意/停一手
            if time.time() - last_pop > 3.0:
                last_pop = time.time()
                try:
                    ev = scan_popups()
                    if ev == 'pass' and turn != assist:
                        # 对方停一手(虚着): 盘面无变化, 轮到我们继续
                        print('[停一手] 对方虚着, 轮到我们')
                        turn = assist
                        acted_counts = (-1, -1)
                        TURN_LOCK['until'] = time.time() + TURN_LOCK_SECS
                    elif ev in ('resign_ok', 'reconnect_end'):
                        _reason = '重连成功-对局已结束' if ev == 'reconnect_end' \
                            else '对方认输'
                        print(f'[{_reason}] 本局结束, 进入结算/下一局流程')
                        # 对局结束(认输/重连断开): 本局立即结束, 此前只点了确认
                        # 却未收尾, 主循环继续按正常对局读盘 -> 空盘/轮次反复
                        # 横跳(无新增子丢弃)直到 30s 才由 err_streak 兜底。
                        # 这里直接走终局流程: 结算页 -> 点[重新匹配/续战]进下一盘
                        _GAME_ACTIVE[0] = False
                        g2 = end_or_wait('对局结束(' + _reason + ')')
                        if g2 is None:
                            return
                        n, board, counts, res_cur = g2
                        last_counts = counts
                        last_n = n
                        acted_counts = (-1, -1)
                        # 新局盘面非空时不能用"空盘黑先"(对方可能已落首手且
                        # 被设为基线), 用金框判定, 防 turn 停错导致不落子
                        turn = _resolve_new_game_turn(
                            assist, counts[0] + counts[1])
                        cand = None
                        last_activity = time.time()
                        failed_cycles = 0
                        err_streak = 0
                        consec_bad = 0
                        vanish_sum = 0
                        bad_points.clear()
                        _vis_opp_since = 0.0
                        continue
                except Exception:
                    pass

            # ---- 自动续战/空盘启动: 执色只认头像角标颜色(分先可能轮换) ----
            if total == 0 and (AUTO_NEXT or DETECT_FIRST):
                if opening_since is None:
                    opening_since = time.time()
                    opening_probed = False
                    opening_av_at = 0.0
                    opening_blk_n = 0
                    opening_garb_at = 0.0
                    # 结算->新局过渡: 等新画面渲染稳定再读角标, 避免读到旧局帧
                    time.sleep(0.8)
                    # 分先换色: 新局用「金框(谁行棋)+空盘黑先」定执色, 比角标
                    # 更稳(开盘即渲染, 不受角标过渡帧误读影响); 角标仅作兜底。
                    try:
                        _rgf0 = br.window_rect(br.PID)
                        _gf0 = winclick.gold_frame_ratio(_rgf0, None)
                    except Exception:
                        _gf0 = None
                    if _gf0 is not None:
                        # 空盘黑先: 金框=我方行棋 唯一解释为我方执黑
                        _col0 = 'black' if _gf0 >= winclick.GOLD_THR else 'white'
                        if _col0 != assist:
                            print(f'[开局执色] 金框判定我方执'
                                  f'{("黑" if _col0=="black" else "白")}'
                                  f'(空盘黑先), 已切换')
                            assist = _col0
                            MY_SIDE = assist
                            side = '黑' if assist == 'black' else '白'
                    else:
                        # 金框不可用(渲染未就绪)时退回角标
                        try:
                            av = winclick.avatar_my_color()
                        except Exception:
                            av = None
                        if av is not None and av != assist:
                            print(f'[头像角标] 新局我方实际执'
                                  f'{("黑" if av=="black" else "白")}'
                                  f'(分先轮换), 已切换')
                            assist = av
                            MY_SIDE = assist
                            side = '黑' if assist == 'black' else '白'
                    print(f'新局(空盘): 我方执'
                          f'{("黑" if assist=="black" else "白")}, '
                          f'等{("我方" if assist=="black" else "黑方")}开局')
                elapsed = time.time() - opening_since
                if assist == 'black':
                    if elapsed < 6:
                        # 观察期持续以角标复核(防沿用上一局颜色/分先换色);
                        # 连续两次确认执黑即开局(空盘执黑=我方第一手无歧义),
                        # 不必等满 6s 观察窗
                        if (not opening_probed
                                and time.time() - opening_av_at > 1.5):
                            opening_av_at = time.time()
                            try:
                                avb = winclick.avatar_my_color()
                            except Exception:
                                avb = None
                            if avb == 'white':
                                # 金框守卫: 若金框=我方行棋(空盘黑先), 说明是
                                # 角标过渡误读白, 忽略之, 保持我方执黑开局
                                try:
                                    _rgf2 = br.window_rect(br.PID)
                                    _gf2 = winclick.gold_frame_ratio(_rgf2, None)
                                except Exception:
                                    _gf2 = None
                                if _gf2 is not None and _gf2 >= winclick.GOLD_THR:
                                    opening_blk_n += 1
                                    if opening_blk_n >= 2:
                                        print('[金框守卫] 角标误读白, 金框确认'
                                              '我方执黑, 开局')
                                        opening_probed = True
                                        opening_since = None
                                        acted_counts = (-1, -1)
                                        # 不 continue: 落到行动门直接落子
                                    else:
                                        print('[金框守卫] 角标读白但金框=我方'
                                              '行棋, 保持执黑')
                                else:
                                    print('[头像角标] 新局我方实际执白, 已切换'
                                          '(等待黑方开局)')
                                    assist = 'white'
                                    MY_SIDE = assist
                                    side = '黑' if assist == 'black' else '白'
                                    acted_counts = (-1, -1)
                                    opening_probed = False
                                    opening_since = None
                                    opening_blk_n = 0
                                    continue
                            if avb == 'black':
                                opening_blk_n += 1
                                if opening_blk_n >= 2:
                                    print('角标确认我方执黑, 开局')
                                    opening_probed = True
                                    opening_since = None
                                    acted_counts = (-1, -1)
                                    # 不 continue: 落到行动门直接落子
                                else:
                                    print('[头像角标] 新局确认我方执黑')
                            else:
                                opening_blk_n = 0
                        if not opening_probed:
                            time.sleep(0.3)
                            continue
                    # 6s 后仍未由他人开黑子: 再复核一次, 角标不可读则不开局,
                    # 继续等(新局转场角标可能稍后才渲染), 最迟 20s
                    if not opening_probed:
                        if time.time() - opening_av_at > 1.5:
                            opening_av_at = time.time()
                            try:
                                avb = winclick.avatar_my_color()
                            except Exception:
                                avb = None
                            if avb == 'white':
                                print('[头像角标] 我方实际执白, 已切换')
                                assist = 'white'
                                MY_SIDE = assist
                                side = '黑' if assist == 'black' else '白'
                                acted_counts = (-1, -1)
                                opening_probed = False
                                opening_since = None
                                continue
                            if avb == 'black':
                                print('角标确认我方执黑, 开局')
                                opening_probed = True
                                opening_since = None
                                acted_counts = (-1, -1)
                                # 不 continue: 落到下方行动门, 本轮回合直接落子
                        if not opening_probed:
                            # 倒计时提示遮罩(全屏变暗)会让角标/棋盘持续不可读
                            # 至多 ~30s; 若近期有遮罩垃圾帧则放宽停止时限, 防
                            # 误把"等遮罩消失"判成卡死停掉整个会话
                            _ov = time.time() - opening_garb_at < 12
                            if elapsed >= (45 if _ov else 20):
                                print('!! 新局 角标长时间不可读'
                                      + ('(倒计时提示遮罩中)' if _ov else '')
                                      + ', 无法确认执色(不沿用上一局), 停止; '
                                        '请人工指定执色/轮次后重启')
                                return
                            time.sleep(0.5)
                            continue
                else:
                    # 我方执白: 等黑方开局
                    if not opening_probed and elapsed >= 6:
                        if time.time() - opening_av_at > 2.5:
                            opening_av_at = time.time()
                            try:
                                av2 = winclick.avatar_my_color()
                            except Exception:
                                av2 = None
                            if av2 == 'black':
                                print('[头像角标] 我方实际执黑'
                                      '(先前误判), 已切换开局')
                                assist = 'black'
                                MY_SIDE = assist
                                side = '黑' if assist == 'black' else '白'
                                acted_counts = (-1, -1)
                                opening_probed = False
                                opening_since = None
                                continue
                            if av2 == 'white':
                                print('(角标确认我方执白, 继续等黑方开局)')
                                opening_probed = True
                            elif elapsed >= 25:
                                print('!! 黑方长时间未开局且角标颜色不可读, '
                                      '无法确认执色(只以颜色为准), 停止')
                                return
                            # av2 为 None 且未超时: 稍后自动重试
            else:
                if (opening_since is not None and not opening_probed
                        and total > 0):
                    # 开局态见到非空盘: 只认"恰好(黑1,白0)"为对方真实开局;
                    # 其余(上一局残影/新局渲染中的转场帧)一律忽略跳过,
                    # 不重置开局时钟也不进行动门(防在垃圾盘面上分析落子)
                    if counts == (1, 0):
                        if assist == 'black':
                            # 黑先手已落而自认执黑: 我方从未点击, 那颗黑子
                            # 只可能是 (a) 我方实际执白(分先已换, 角标终会
                            # 显示白) -> 翻白应手; (b) 转场残影 -> 忽略。
                            # 角标不可读/仍显示黑时不猜, 节流重读, 由角标定夺
                            if time.time() - opening_since >= 25:
                                print('!! (黑1,白0)持续 25s 且角标始终非白, '
                                      '无法确认执色, 停止; '
                                      '请人工指定执色/轮次后重启')
                                return
                            if time.time() - opening_av_at > 1.5:
                                opening_av_at = time.time()
                                try:
                                    avf = winclick.avatar_my_color()
                                except Exception:
                                    avf = None
                                if avf == 'white':
                                    print('!! 黑先手由他人落下且角标显示白 '
                                          '-> 我方实际执白, 自动切换')
                                    assist = 'white'
                                    acted_counts = (-1, -1)
                                    opening_since = None
                                    opening_probed = False
                                    opening_blk_n = 0
                                    # 不 continue: 交行动门按白方应手
                                elif avf == 'black':
                                    print('(黑1由他人落而角标仍黑'
                                          '=转场残影, 忽略等待)')
                                    time.sleep(0.4)
                                    continue
                                else:
                                    # 角标未渲染: 不猜, 稍后重读
                                    time.sleep(0.4)
                                    continue
                            else:
                                time.sleep(0.4)
                                continue
                        else:
                            # 白方等待黑开局: 黑第一手已落, 解除开局态应手
                            opening_since = None
                            opening_probed = False
                            opening_blk_n = 0
                    else:
                        print('(开局盘面读数异常(黑%d白%d), 忽略, '
                              '等待稳定)' % counts)
                        opening_garb_at = time.time()   # 倒计时提示遮罩
                        # 遮罩只盖棋盘中央(~18子), 头像角标仍可读:
                        # 遮罩期持续确认执色, 遮罩一消失即可立即开局
                        if time.time() - opening_av_at > 1.5:
                            opening_av_at = time.time()
                            try:
                                avm = winclick.avatar_my_color()
                            except Exception:
                                avm = None
                            if avm == 'white' and assist == 'black':
                                print('[头像角标] 新局我方实际执白, 已切换'
                                      '(等待黑方开局)')
                                assist = 'white'
                                MY_SIDE = assist
                                side = '黑' if assist == 'black' else '白'
                                acted_counts = (-1, -1)
                                opening_probed = False
                                opening_since = None
                                opening_blk_n = 0
                                continue
                            if avm == 'black':
                                opening_blk_n += 1   # 预累积, 遮罩消失即开局
                            else:
                                opening_blk_n = 0
                        time.sleep(0.4)
                        continue

            # ---- 金框纠正闸(用户规格: 金框=我方 即我方行棋, 不受其他干扰) ----
            # turn 由"落子算术"维护, 偶尔会因漏读/提子/时序而停在错误的一方,
            # 表现为"金框明明=我方却显示轮到对方、干等不落子"。金框是唯一判据,
            # 故当金框连续稳定显示我方、而 turn 仍是对方时, 直接纠正 turn 并
            # 放行落子门(同时清 acted_counts, 防其等于当前盘面而卡住)。
            if turn != assist and _gold_latest[0] is not None:
                if _gold_latest[0] >= winclick.GOLD_THR:
                    _gold_my_since[0] = (_gold_my_since[0] or now)
                    if now - _gold_my_since[0] >= 3.0:
                        print('[金框纠正] 金框持续=我方(%.3f) 但轮到%s, '
                              '按金框纠正为我方(%s)'
                              % (_gold_latest[0],
                                 '黑' if turn == 'black' else '白',
                                 '黑' if assist == 'black' else '白'))
                        turn = assist
                        acted_counts = (-1, -1)
                        _gold_my_since[0] = 0.0
                else:
                    _gold_my_since[0] = 0.0
            else:
                _gold_my_since[0] = 0.0

            # 我方回合: 分析并落子
            if turn == assist and counts != acted_counts:
                # 提子动画稳定窗口: 刚接受过含提子的变化时, 提交分析前
                # 再读一次盘核对, 防止把提子半程的过渡盘面送进引擎
                if time.time() - CAP_HOLD < SETTLE_S:
                    CAP_HOLD = 0.0
                    g = read_board_counts()
                    if g is not None and (g[2] != counts or g[1] != board):
                        print('(提子动画未完全结束, 等盘面稳定后重读)')
                        continue
                    if g is not None:
                        n, board, counts, res_cur = g
                # 视觉行棋确认: 唯一判据=金框(gold=白/黑行棋)。
                # 视觉明确=对方回合且盘面稳定时暂不落子(防锚定错/翻转中);
                # 超时则按当前盘面放行。
                _now_v = time.time()
                if _now_v - _vis_gate_t > 1.0:
                    _vis_gate_t = _now_v
                    try:
                        _srcv, _resv, _infov = visual_turn(res_cur, assist=assist)
                        # gold: 行棋方颜色 -> mine/opp; 其余不可判
                        _vcls = ('mine' if _resv == assist else 'opp') \
                            if _srcv == 'gold' else None
                    except Exception:
                        _vcls = None
                    if _vcls == 'opp':
                        if _vis_opp_since == 0.0:
                            _vis_opp_since = _now_v
                            print(f'[视觉闸] 判定对方回合({_infov} '
                                  f'{_srcv}), 启动 1.5s 拦截计时')
                    else:
                        if _vis_opp_since:
                            print(f'[视觉闸] 视觉恢复非对方回合, 取消拦截')
                        _vis_opp_since = 0.0
                if (_vis_opp_since and time.time() - _vis_opp_since > 1.5
                        and time.time() - last_change > 1.5):
                    if time.time() - _vis_opp_since > VIS_OVERRIDE_S:
                        print('!! 视觉连续显示对方回合超时, 按算术放行')
                        _vis_opp_since = 0.0
                    else:
                        print(f'[视觉闸] 拦截中 '
                              f'{int(time.time() - _vis_opp_since)}s')
                        continue   # 视觉=对方回合: 回主循环等待
                rect = res_cur['rect']
                xs, ys = res_cur['xs'], res_cur['ys']
                ok = False
                mid_change = False
                last_mv = None
                same_fails = 0
                forced = []  # 读盘漏子时手动标记的占位点
                # 劫争检测: 我方即将提劫但该点本回合被劫规禁止(须先找劫材),
                # 主动把劫点加入黑名单, 让引擎改选他处; 下一回合(对手已应)解禁。
                my_turn_seq += 1
                # 劫禁(手顺法, 主防线): 对方刚提我方1子 -> 该点本回合禁, 让引擎
                # 直接跳过劫点, 不必等"点了被拒再拉黑"(省掉整轮重试+重算耗时)。
                if KO_BAN.get('point') and KO_BAN.get('seq') == my_turn_seq:
                    bad_points.add(KO_BAN['point'])
                    print(f'[劫禁] 本回合禁 {KO_BAN["point"]}'
                          f'(对方刚提子, 提回会重现局面)')
                _kko = br.detect_ko(n, board, 'X' if assist == 'black' else 'O')
                if _kko is not None:
                    if ko_state['point'] == _kko and ko_state['turn'] == my_turn_seq:
                        bad_points.add(_kko)        # 仍为本回合: 继续禁
                    elif ko_state['point'] != _kko:
                        ko_state = {'point': _kko, 'turn': my_turn_seq}
                        bad_points.add(_kko)        # 新劫: 本回合禁
                    else:
                        ko_state = {'point': None, 'turn': -1}  # 已到下一回合: 解禁
                else:
                    ko_state = {'point': None, 'turn': -1}
                for attempt in range(3):
                    stones = br.stones_legal(n, board) + forced
                    if attempt == 0 and 'compute_start' not in _MOVE_T:
                        _MOVE_T['compute_start'] = time.time()
                    # 优先采用对方回合预分析结果(同局面且未被禁), 免等引擎
                    mv = info = root = None
                    if not forced:
                        _k = tuple(tuple(s) for s in stones)
                        _hit = (PRE.get('key') == _k and PRE.get('mv')
                                and PRE['mv'] not in bad_points
                                and time.time() - PRE.get('t', 0) < 90)
                        if _hit:
                            mv = PRE['mv']
                            info = PRE['info']
                            root = PRE['root']
                            _MOVE_T['reused'] = True
                        elif PRE.get('pred_move') is not None:
                            print(f'[预热] 未命中: 预测对方落 '
                                  f'{PRE.get("pred_move")} 与真实不符 '
                                  f'(key差{1 if PRE.get("key")!=_k else 0})')
                            PRE['pred_move'] = None  # 避免重复打印
                    if mv is None:
                        mv, info, root = analyze_position(n, stones, assist,
                                        banned=bad_points)
                        _MOVE_T['reused'] = False
                    if mv is None:
                        print('KataGo 无返回, 稍后重试')
                        time.sleep(3)
                        break
                    if 'mv_ready' not in _MOVE_T:
                        _MOVE_T['mv_ready'] = time.time()
                    if str(mv).lower().startswith('pass'):
                        # 仅在"我方大优收局"或盘面接近下满时才终局流程
                        # 引擎胜率为黑方基准(cfg), 先换算成我方视角再门控
                        pw = info.get('winrate', 0) if info else 0
                        if assist == 'white':
                            pw = 1.0 - pw
                        occ = (counts[0] + counts[1]) / (n * n)
                        accept = pw >= 0.90 or occ >= 0.75
                        if accept and pw < 0.98 and occ < 0.75:
                            # 胜率在 90-98% 且未下满: 用 ownership 确认盘面
                            # 已定型(空点仍有大量低置信 = 大龙/官子未定, 不裁判)
                            accept = board_settled(n, board, assist)
                            if not accept:
                                print('!! ownership 显示盘面未定型(仍有争拗点), '
                                      '暂不裁判, 继续下')
                        if not accept:
                            print(f'!! 引擎建议停一手但我方胜率仅{pw*100:.0f}%'
                                  f'(占盘{occ*100:.0f}%), 不采纳, 继续下')
                            bad_points.add('pass')
                            same_fails = 0
                            last_mv = None
                            continue
                        print('!! 引擎判定终局, 尝试[智能裁判]...')
                        done = False
                        if winclick.click_label('智能裁判', ('裁判',)):
                            print('✔ 已点击智能裁判')
                            time.sleep(1.5)
                            if winclick.click_confirm():
                                print('✔ 已确认裁判弹窗')
                            done = True
                            # 裁判后等结算/对方应手, 不再重复分析
                            acted_counts = counts
                            turn = other(assist)
                            last_activity = time.time()
                            time.sleep(2)
                            continue
                        if not done:
                            print('! 无智能裁判按钮, 尝试停一手...')
                            clicked = winclick.click_label(
                                '停一手', ('虚着', '过一手', '停一手'))
                            if clicked:
                                print('✔ 已点击停一手, 等待对方回应/终局')
                                time.sleep(1.5)
                                if winclick.click_confirm():
                                    print('✔ 已确认弹窗(停一手)')
                                last_activity = time.time()
                                acted_counts = (-1, -1)
                                time.sleep(2)
                                continue
                        print('! 未找到收尾按钮, 按终局处理')
                        g2 = end_or_wait('引擎判定终局(pass)')
                        if g2 is None:
                            return
                        n, board, counts, res_cur = g2
                        last_counts = counts
                        acted_counts = (-1, -1)
                        # 同上: 新局盘面非空用金框判定 turn
                        turn = _resolve_new_game_turn(
                            assist, counts[0] + counts[1])
                        cand = None
                        last_activity = time.time()
                        failed_cycles = 0
                        err_streak = 0
                        continue
                    if mv == last_mv:
                        same_fails += 1
                        if same_fails >= 2:
                            bad_points.add(mv)
                            print(f'!! {mv} 连续失败, 加入黑名单'
                                  f'(劫争/禁入点), 改选他处')
                            same_fails = 0
                            last_mv = None
                            continue
                    else:
                        same_fails = 0
                        last_mv = mv
                    # 点击前瞬间重读(窗口可能刚移动/对手刚落子)
                    # 瞬时读盘失败很常见(截图抖动/窗口一闪), 快速重试后再放弃
                    fresh = None
                    for _rr in range(3):
                        fresh = read_board_counts()
                        if fresh is not None:
                            break
                        time.sleep(0.4)
                    if fresh is None:
                        print('点击前重读失败, 跳过本手(下轮重试)')
                        break
                    n2, board2, counts2, res2 = fresh
                    if counts2 != counts:
                        print('盘面在分析期间变化, 重新判断')
                        n, board, counts, res_cur = n2, board2, counts2, res2
                        # 同步刷新坐标数组(网格尺寸可能已变, 防旧数组错位)
                        rect = res_cur['rect']
                        xs, ys = res_cur['xs'], res_cur['ys']
                        last_counts = counts
                        cand = None
                        mid_change = True
                        break
                    n, board, counts, res_cur = n2, board2, counts2, res2
                    rect = res_cur['rect']
                    xs, ys = res_cur['xs'], res_cur['ys']
                    col = LETTERS.index(mv[0])
                    i = n - int(mv[1:])
                    if not (0 <= i < n and 0 <= col < n
                            and col < len(xs) and i < len(ys)):
                        print(f'!! 引擎着法 {mv} 超出当前 {n} 路网格, '
                              '清缓存重新识别尺寸')
                        br.clear_grid_cache()
                        time.sleep(1)
                        continue
                    if i < 0 or i >= n:
                        break
                    # 目标点须为空
                    if board[i][col] != '.':
                        print(f'目标 {mv} 读盘非空, 重选...')
                        forced.append(['w' if assist == 'black' else 'b', mv])
                        continue
                    rx = rect[0] + round(float(xs[col]))
                    ry = rect[1] + round(float(ys[i]))
                    if not (rect[0] <= rx <= rect[2]
                            and rect[1] <= ry <= rect[3]):
                        print(f'!! 落点({rx},{ry})不在窗口内, 放弃本手')
                        break
                    # 我方视角: 引擎胜率/目差恒为黑方基准(cfg), 由
                    # root_view 换算; 查询的 initialPlayer 已按实际行棋方传
                    wr = lead = None
                    try:
                        if root is not None:
                            wr, lead = root_view(root, assist)
                        if wr is None and info is not None:
                            wr, lead = root_view(info, assist)
                    except Exception:
                        wr, lead = root_view(info, assist)
                        if wr is None:
                            wr, lead = 0.0, 0.0
                    wrp = (wr or 0) * 100
                    # 骤降自查: 上一手还说我们大优, 这手突然极低
                    if last_wr is not None and last_wr > 0.65 and wr < 0.2:
                        print('!! 胜率骤降(疑似读盘漏子), 重新读盘核对...')
                        chk0 = read_board_counts()
                        if chk0 is not None and chk0[2] != counts:
                            print('盘面已变, 重新分析')
                            n, board, counts, res_cur = chk0
                            last_counts = counts
                            cand = None
                            mid_change = True
                            break
                    # 对方视角 = 我方胜率的镜像(同一局面同一引擎评估)
                    o_wr2 = None if wr is None else (1.0 - wr)
                    if o_wr2 is not None:
                        o_s = '黑' if assist == 'white' else '白'
                        print(f'推荐 {side}{mv} @({rx},{ry}) '
                              f'我方({side})胜率 {wrp:.1f}% | '
                              f'{o_s}方视角 {o_wr2*100:.1f}% '
                              f'目差{lead:+.1f} '
                              f'(第{attempt+1}次)')
                        evt('引擎出招')
                        if wr < 0.05:
                            extreme_count += 1
                            print(f'!! 胜率异常[对方大优], '
                                  f'连续第{extreme_count}次')
                            if extreme_count == 3:
                                print('--- 当前识别盘面(排查漏子) ---')
                                nb2 = sum(r.count('X') for r in board)
                                nw2 = sum(r.count('O') for r in board)
                                try:
                                    legal = br.stones_legal(n, board)
                                    lb = sum(1 for x in legal if x[0]=='b')
                                    lw = sum(1 for x in legal if x[0]=='w')
                                    print(f'读盘: 黑{nb2} 白{nw2} | '
                                          f'剔除零气组后 黑{lb} 白{lw} | '
                                          f'来源 {res_cur.get("src")}')
                                except Exception:
                                    print(f'读盘: 黑{nb2} 白{nw2} | '
                                          f'来源 {res_cur.get("src")}')
                                bd = res_cur.get('board')
                                if bd:
                                    nn2 = len(bd)
                                    let = 'ABCDEFGHJKLMNOPQRST'[:nn2]
                                    print('    ' + ' '.join(let))
                                    for ii in range(nn2):
                                        print(f'{nn2-ii:2d}  '
                                              + ' '.join(bd[ii]))
                                print('--- 盘面结束 ---')
                            # 执色翻转只认头像角标颜色(不做胜率/子数推断):
                            # 角标与我方当前执色一致 -> 确系大劣, 不翻转;
                            # 角标不可读 -> 宁可不翻, 也不猜
                            av = None
                            try:
                                av = winclick.avatar_my_color()
                            except Exception:
                                av = None
                            if av is not None:
                                if av != assist:
                                    print(f'!! 头像角标显示我方实际执'
                                          f'{("黑" if av=="black" else "白")}, '
                                          f'翻转执色')
                                    assist = av
                                    MY_SIDE = assist
                                    side = '黑' if assist == 'black' else '白'
                                    acted_counts = (-1, -1)
                                    turn = assist
                                    last_flip = time.time()
                                    extreme_count = 0
                                    continue
                                print('(角标确认执色无误, 我方确处大劣, 不翻转)')
                                extreme_count = 0
                        else:
                            extreme_count = 0
                    else:
                        print(f'推荐 {side}{mv} @({rx},{ry}) '
                              f'我方胜率 {wrp:.1f}% '
                              f'目差{lead:+.1f} '
                              f'(第{attempt+1}次)')
                        evt('引擎出招')
                    _set_st(force=True, wr=wr, lead=lead, mv=mv,
                            status='analyze')
                    # 点击前预检: 引擎读空但视觉上有子样(暗占比高且亮
                    # 占比低) -> 疑似漏子/网格错位; 点已有子的点会被客户端
                    # 静默忽略(反馈红圈=彩占比抬高), 形成"失败-重试-黑名单"
                    # 循环。跳过本手, 强制重对齐后由主循环重新分析。
                    _pc = click_target_profile(res2, i, col)
                    if (_pc is not None and board[i][col] == '.'
                            and _pc[0] >= 0.35 and _pc[2] <= 0.30):
                        print(f'?? 目标 {mv} 读盘为空, 但邻域暗{_pc[0]:.2f} '
                              f'彩{_pc[1]:.2f} 亮{_pc[2]:.2f}, '
                              '疑似漏子/网格错位, 强制重对齐, 本手跳过重分析')
                        br.align_reset()
                        time.sleep(0.3)
                        break
                    _MOVE_T['click'] = time.time()
                    click_at(rx, ry)
                    my_char = 'X' if assist == 'black' else 'O'
                    ok_move = False
                    print(f'  [点击] ({rx:.0f},{ry:.0f}) 窗口{res_cur["rect"]}')
                    evt('点击落子')
                    for _v, _w in enumerate((0.35, 0.45, 0.8, 1.4)):
                        # 阶梯轮询: 绝大多数点击即时生效, 首个验证点 0.35s
                        time.sleep(_w)
                        chk = read_board_counts()
                        if chk is None:
                            continue
                        n3, board3, counts3, res3 = chk
                        placed = (i < n3 and col < n3
                                  and board3[i][col] == my_char)
                        # 只认"我方颜色子数增加"(对方变化/脏帧不算)
                        my_inc = ((counts3[0] - counts[0]) if
                                  assist == 'black'
                                  else (counts3[1] - counts[1]))
                        # 标记信号: 点击前该点(board[i][col])为空, 落子成功后
                        # 腾讯显示"最后一手"彩色标记覆盖该点; 读盘可能因标记
                        # 遮挡读不到 my_char。判成功须**同时**满足:
                        #   1) 高彩(cf>=0.25)
                        #   2) 有棋子(暗>=0.30 黑子 或 亮>=0.30 白子)
                        # 关键: 不能只看高彩! 棋盘木色底本身 RGB 差 >60, 空点
                        # 天然高彩(N3 空点实测 彩0.94 暗0.06 亮0.00)。若仅凭
                        # cf>=0.25 判成功, 任何空点/被劫禁拒绝的点都会被误判
                        # 成"落子成功"(没落上也当落了), 后果远重于漏判。
                        # 故必须有"有子"硬条件: 空点/拒绝(无子) => mark=False。
                        _mark = False
                        if board[i][col] == '.':
                            try:
                                from PIL import ImageGrab
                                import numpy as _np
                                _im = ImageGrab.grab(
                                    bbox=res_cur['rect']).convert('RGB')
                                _a = _np.asarray(_im).astype(int)
                                _gx, _gy = int(xs[col]), int(ys[i])
                                _h = max(6, int(res_cur.get('step', 27)
                                                * 0.55))
                                _c = _a[_gy-_h:_gy+_h+1, _gx-_h:_gx+_h+1]
                                _chm = (_c.max(axis=2) - _c.min(axis=2))
                                _lum = _c.mean(axis=2)
                                _cf = float((_chm > 60).mean())
                                _df = float(((_lum < 92) & (_chm < 60)).mean())
                                _bf = float(((_lum > 238) & (_chm < 40)).mean())
                                if _cf >= 0.25 and (_df >= 0.30 or _bf >= 0.30):
                                    _mark = True
                            except Exception:
                                pass
                        if placed or my_inc >= 1 or _mark:
                            ok_move = True
                            _MOVE_T['confirm'] = time.time()
                            _phase_report()
                            break
                    if not ok_move:
                        # 重试前快查: 盘面若已变化(我方子其实已出现/对方刚
                        # 落子), 交回主循环吸收, 不再盲目重试同一位置
                        _chk3 = read_board_counts()
                        if _chk3 is not None and _chk3[2] != counts:
                            mid_change = True
                            break
                        # 级联自愈: 点击失败且盘面未变——常见于我方上一手
                        # 已落但被漏读/回合失步(实际轮到对方, 点空点被服务
                        # 端忽略)。金框视觉若明确=对方行棋, 翻回合停止点击。
                        try:
                            _vtx = visual_turn(res_cur, assist=assist)
                            _vcy = ('opp' if _vtx[1] == _other(assist)
                                    else 'mine') if _vtx[0] == 'gold' else None
                            if _vcy == 'opp':
                                print(f'[视觉闸] 点击失败+金框'
                                      f'({_vtx[2]}) -> 实际轮到对方, '
                                      f'翻回合停止点击')
                                turn = _other(assist)
                                acted_counts = (-1, -1)
                                cand = None
                                break
                        except Exception:
                            pass
                        if attempt == 0:
                            # 诊断: 点击点邻域像素剖面, 区分
                            # 没点上 / 棋子被最后一手标记遮挡 / 读盘网格错位
                            try:
                                from PIL import ImageGrab
                                import numpy as _np
                                _im = ImageGrab.grab(
                                    bbox=res_cur['rect']).convert('RGB')
                                _a = _np.asarray(_im).astype(int)
                                _gx, _gy = int(xs[col]), int(ys[i])
                                _h = max(6, int(res_cur.get('step', 27)
                                                * 0.55))
                                _c = _a[_gy-_h:_gy+_h+1, _gx-_h:_gx+_h+1]
                                _chm = (_c.max(axis=2) - _c.min(axis=2))
                                _df = float(((_c.mean(axis=2) < 92)
                                             & (_chm < 60)).mean())
                                _bf = float(((_c.mean(axis=2) > 238)
                                             & (_chm < 40)).mean())
                                _cf = float((_chm > 60).mean())
                                print(f'  落点邻域剖面: 暗{_df:.2f} '
                                      f'亮{_bf:.2f} 彩{_cf:.2f} '
                                      f'均亮{_c.mean():.0f} '
                                      f'(彩>0.1=有彩色标记覆盖; '
                                      f'暗0.4-0.7且彩低=普通棋子)')
                                # 存档失败帧(圈出目标点), 供事后确认
                                # 是漏子/网格错位还是彩色标记遮挡
                                try:
                                    from PIL import ImageDraw
                                    _dr = ImageDraw.Draw(_im)
                                    _rr = max(10, int(res_cur.get(
                                        'step', 27) * 0.9))
                                    _dr.ellipse((_gx - _rr, _gy - _rr,
                                                 _gx + _rr, _gy + _rr),
                                                outline=(255, 0, 0), width=2)
                                    _fp = (r'D:\Temp\goai_clickfail_%d.png'
                                           % int(time.time()))
                                    _im.save(_fp)
                                    print(f'  已存点击失败帧 {_fp}')
                                except Exception:
                                    pass
                            except Exception:
                                pass
                        # 落子被拒兜底: 我方回合、盘面无变化、金框非对方 ->
                        # 极可能是劫禁(本回合不可提回)或客户端禁入点。立即把
                        # 该点加入黑名单并失效预热缓存, 强制重新分析(banned)
                        # 改选他处, 杜绝反复点同一被禁点造成的死循环/异常。
                        # 注: detect_ko 因 board_reader 内一处死条件(XO in
                        # ('.X','X.') 单字符永不等于双字符)永远返回 None, 劫禁
                        # 预检从未生效; 故用落子被拒兜底作为主防线更稳妥。
                        # 仅当连续2次(最终)均失败才拉黑, 单次失败先重试,
                        # 避免落子成功标记遮挡造成的单次假阴性误伤该点。
                        if attempt == 1:
                            bad_points.add(mv)
                            PRE['done'] = False
                            PRE['mv'] = None
                            print(f'!! 落子未确认, 把 {mv} 加入黑名单并重算'
                                  f'(疑似劫禁/禁入点)')
                            time.sleep(0.5)
                        continue
                    # 成功: 吸收己方落子造成的盘面变化
                    delta = (counts3[0] + counts3[1]) - total
                    if (counts3[0] < counts[0]) or (counts3[1] < counts[1]):
                        CAP_HOLD = time.time()   # 我方提子, 同样等动画稳定
                    n, board, counts, res_cur = n3, board3, counts3, res3
                    last_counts = counts
                    last_change = time.time()
                    acted_counts = counts
                    last_activity = time.time()
                    failed_cycles = 0
                    ok = True
                    if delta >= 2:
                        # 对手已抢先应手 -> 轮到我们, 欠一手
                        turn = assist
                        acted_counts = (-1, -1)
                    else:
                        # 只有我们落了子 -> 等对手应
                        turn = other(assist)
                        acted_counts = counts
                    TURN_LOCK['until'] = time.time() + TURN_LOCK_SECS
                    print('✔ 已落子 ' + mv +
                          ('(对手已应)' if not placed else ''))
                    evt('落子确认')
                    last_wr = wr
                    bad_points.clear()
                    move_no += 1
                    _set_st(force=True, move_no=move_no, mv=mv,
                            b=counts[0], w=counts[1], turn=turn,
                            status='wait')
                    # 记录我方着法供终局落 SGF(同盘面只记一次)
                    _key = tuple(board)
                    if _key != GAME_LAST_KEY:
                        game_record(n, move_no,
                                    'X' if assist == 'black' else 'O', mv)
                        GAME_LAST_KEY = _key
                    LAST_BOARD = board
                    # 我方落子后轮到对方: 按真实行棋方记录
                    trend_record(n, board, move_no, turn)
                    break
                    print(f'? 第{attempt+1}次未确认落子, 稍后重试')
                    time.sleep(2)
                if ok:
                    pass
                elif mid_change:
                    failed_cycles = 0
                    pass  # 盘面已变, 主循环重新判断
                else:
                    failed_cycles += 1
                    _set_st(force=True, status='fail', mv=mv,
                            b=counts[0], w=counts[1])
                    print(f'!! 本轮落子失败(第{failed_cycles}次)')
                    if failed_cycles == 1:
                        print('   若棋子其实已落(界面标记遮挡致读不到), '
                              '对方落子后即自动恢复; 点击已降频')
                    if failed_cycles >= 2:
                        br.clear_grid_cache()
                        print('    已清网格缓存, 重新定位棋盘')
                    time.sleep(8 if failed_cycles < 2 else 20)
                    if failed_cycles >= FAIL_CAP:
                        g2 = end_or_wait('!! 连续多轮落子失败, 棋局可能已结束或界面异常')
                        if g2 is None:
                            return
                        n, board, counts, res_cur = g2
                        last_counts = counts
                        acted_counts = (-1, -1)
                        turn = 'black'
                        cand = None
                        last_activity = time.time()
                        failed_cycles = 0
                        err_streak = 0
                        continue
                    acted_counts = (-1, -1)  # 保持我方回合, 稍后重试
            else:
                # 对方回合: 低算力预分析我方应手(等>2s 再算, 不与快应手抢引擎)
                try:
                    if (turn != assist and counts == last_counts
                            and time.time() - last_activity > 2):
                        pre_analyze(n, board, assist,
                                    time.time() - last_activity)
                except Exception:
                    pass
                time.sleep(0.25)  # 快速轮询(读盘已很快; P4 提速)
        except KeyboardInterrupt:
            save_game_sgf(final=True, reason='手动停止')
            print('\n已停止')
            return
        except SystemExit:
            err_streak += 1
            if err_streak > ERR_CAP:
                g2 = end_or_wait('!! 长时间找不到游戏窗口')
                if g2 is None:
                    return
                n, board, counts, res_cur = g2
                last_counts = counts
                acted_counts = (-1, -1)
                turn = 'black'
                cand = None
                last_activity = time.time()
                failed_cycles = 0
                err_streak = 0
                continue
            time.sleep(3)
        except Exception as e:
            import traceback
            _nowx = time.time()
            _key = type(e).__name__ + ':' + str(e)[:80]
            if (_key != _last_err.get('k')
                    or _nowx - _last_err.get('t', 0) > 10):
                _last_err = {'k': _key, 't': _nowx}
                traceback.print_exc()
                print('异常: {}: {}, 2s后继续...'.format(
                    type(e).__name__, e))
            time.sleep(2)


if __name__ == '__main__':
    main()
