"""腾讯围棋 AI 陪练 - 启动配置界面 (线程化, 不卡顿)

左侧: 日志; 右侧: 实时识别棋盘(与腾讯围棋画面核对)。
顶部: 操作按钮(启动自动落子 / 连续观战 / 单次分析 / 停止)。
配置: 执子(自动识别) / 当前轮到 / 棋盘尺寸(0=OCR自动) / AI算力 / 自动续战 / 提示音。
仅用于: 人机/AI 对局、双方知情的对局。
"""
import json
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import scrolledtext

TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS)
import board_reader as br
import cleanup_files as cf
import winclick

PY = sys.executable
LOG = os.path.join(TOOLS, 'katago_ui.log')
STATE = os.path.join(TOOLS, 'ui_state.json')   # 主进程状态投影文件
# 引擎耗时按实测 ~3000 visits/s 标注(RTX3050, 未含读盘/OCR)
VISIT_OPTIONS = [('轻快 (100, 引擎≈0.03s/手)', '100'),
                 ('标准 (500, 引擎≈0.2s/手)', '500'),
                 ('强力 (2000, 引擎≈0.7s/手)', '2000'),
                 ('极强 (8000, 引擎≈2.7s/手)', '8000')]
DEFAULT_VISITS = '8000'
WOOD = '#ecd9a2'
LINE = '#6b4f2a'
# 对局模式: 下拉显示中文, 底层值仍为 board_reader 所需的英文键
MODE_CN2EN = {'匹配': 'match', 'AI对战': 'ai', '好友友谊赛': 'friend',
              '挑战赛': 'challenge'}


class KatagoUI:
    def __init__(self):
        self.proc = None
        self.log_pos = 0
        self._sess_log = ''        # 本次子进程输出(失败检测用)
        self._fallback_shown = False
        self.board_key = None
        self._pending_key = None   # 显示去抖: 连续两轮同 key 才重绘
        self._ticks = 0
        self._redraw_pending = False
        self._board_busy = False
        self._fast_poll = False      # 盘面变化后快速复读(降镜像延迟)
        self._q = __import__('queue').Queue()
        self._trend = []
        self._trend_my = True   # True=我方视角 False=黑方视角
        self.root = tk.Tk()
        self.root.title('腾讯围棋 AI 陪练配置')
        self.root.geometry('980x660')
        self.root.minsize(780, 540)
        self.root.resizable(True, True)
        self.mode_var = tk.StringVar(value='挑战赛')  # 对局模式(中文显示, 底层英文)

        frm = tk.Frame(self.root, padx=14, pady=8)
        frm.pack(fill='x')

        tk.Label(frm, text='我执子:', font=('Microsoft YaHei', 10)).grid(
            row=0, column=0, sticky='w')
        self.color_var = tk.StringVar(value='auto')
        for i, (txt, val) in enumerate([('自动识别', 'auto'), ('黑棋', 'black'),
                                        ('白棋', 'white')]):
            tk.Radiobutton(frm, text=txt, value=val, variable=self.color_var,
                           font=('Microsoft YaHei', 10)).grid(
                row=0, column=1 + i, sticky='w', padx=(6, 0))

        tk.Label(frm, text='当前轮到:', font=('Microsoft YaHei', 10)).grid(
            row=1, column=0, sticky='w', pady=(6, 0))
        self.turn_var = tk.StringVar(value='auto')
        for i, (txt, val) in enumerate([('自动(横幅)', 'auto'),
                                        ('黑方', 'black'), ('白方', 'white')]):
            tk.Radiobutton(frm, text=txt, value=val, variable=self.turn_var,
                           font=('Microsoft YaHei', 10)).grid(
                row=1, column=1 + i, sticky='w', padx=(6, 0))
        tk.Label(frm, text='自动识别失败时的兜底: 手动指定执子+轮到即可启动',
                 fg='#888888', font=('Microsoft YaHei', 8)).grid(
            row=1, column=4, sticky='w', padx=(10, 0))

        tk.Label(frm, text='棋盘尺寸:', font=('Microsoft YaHei', 10)).grid(
            row=2, column=0, sticky='w', pady=(6, 0))
        self.size_var = tk.StringVar(value='0')
        size_menu = tk.OptionMenu(frm, self.size_var, '0', '9', '13', '19')
        size_menu.config(font=('Microsoft YaHei', 9))
        size_menu.grid(row=2, column=1, columnspan=3, sticky='w', pady=(6, 0))
        tk.Label(frm, text='(0=OCR标题自动读取路数; 指定则强制)',
                 fg='#888888', font=('Microsoft YaHei', 8)).grid(
            row=3, column=1, columnspan=3, sticky='w')

        tk.Label(frm, text='对局模式:', font=('Microsoft YaHei', 10)).grid(
            row=4, column=0, sticky='w', pady=(6, 0))

        def _on_mode_change(cn):
            # 实时同步到主进程识别: 切换即生效(右侧实时面板用全局 GAME_MODE)
            br.set_game_mode(MODE_CN2EN.get(cn, 'challenge'))

        mode_menu = tk.OptionMenu(
            frm, self.mode_var, '匹配', 'AI对战', '好友友谊赛', '挑战赛',
            command=_on_mode_change)
        mode_menu.config(font=('Microsoft YaHei', 9))
        mode_menu.grid(row=4, column=1, columnspan=3, sticky='w', pady=(6, 0))
        # 初始值同步(默认 challenge)
        br.set_game_mode(MODE_CN2EN.get(self.mode_var.get(), 'challenge'))

        tk.Label(frm, text='AI 算力:', font=('Microsoft YaHei', 10)).grid(
            row=5, column=0, sticky='w', pady=(6, 0))
        self.visits_var = tk.StringVar(value=DEFAULT_VISITS)
        menu = tk.OptionMenu(frm, self.visits_var,
                             *[v for _, v in VISIT_OPTIONS])
        menu.config(font=('Microsoft YaHei', 9))
        menu.grid(row=5, column=1, columnspan=3, sticky='w', pady=(6, 0))

        self._board_blank = False   # 当前画布是否已显示"无棋盘"占位
        self._cv_size = (0, 0)      # 画布最近一次绘制尺寸
        self.wait_var = tk.BooleanVar(value=False)
        self.beep_var = tk.BooleanVar(value=False)
        tk.Checkbutton(frm, text='自动续战: 终局后自动点[重新匹配]并继续下一盘',
                       variable=self.wait_var,
                       font=('Microsoft YaHei', 9)).grid(
            row=6, column=0, columnspan=4, sticky='w', pady=(8, 0))

        self.maxgames_var = tk.StringVar(value='')
        tk.Label(frm, text='续战上限(盘):', font=('Microsoft YaHei', 10)).grid(
            row=7, column=0, sticky='w', pady=(4, 0))
        tk.Entry(frm, textvariable=self.maxgames_var, width=6,
                 font=('Microsoft YaHei', 10)).grid(
            row=7, column=1, sticky='w', pady=(4, 0))
        tk.Label(frm, text='(留空=无限续战)', fg='#888888',
                 font=('Microsoft YaHei', 8)).grid(
            row=7, column=2, columnspan=2, sticky='w', pady=(4, 0))

        tk.Checkbutton(frm, text='终局/停止时响提示音',
                       variable=self.beep_var,
                       font=('Microsoft YaHei', 9)).grid(
            row=8, column=0, columnspan=4, sticky='w')

        btns = tk.Frame(self.root, pady=4)
        btns.pack(fill='x', padx=10)
        self.btn_start = tk.Button(btns, text='▶ 启动自动落子', width=14,
                                   command=self.start_play, bg='#cdeccd')
        self.btn_watch = tk.Button(btns, text='连续观战分析', width=12,
                                   command=self.start_watch)
        self.btn_suggest = tk.Button(btns, text='单次分析', width=9,
                                     command=self.start_suggest)
        self.btn_stop = tk.Button(btns, text='■ 停止', width=8,
                                  command=self.stop, state='disabled',
                                  bg='#ffd9d9')
        for b in (self.btn_start, self.btn_watch, self.btn_suggest,
                  self.btn_stop):
            b.pack(side='left', padx=4)

        main = tk.Frame(self.root)
        main.pack(fill='both', expand=True, padx=10, pady=4)
        left = tk.Frame(main)
        left.pack(side='left', fill='both', expand=True)
        self.log = scrolledtext.ScrolledText(left, height=12, width=54,
                                             font=('Consolas', 9),
                                             state='disabled')
        self.log.pack(fill='both', expand=True)
        # 日志滚动策略: 贴底时自动跟随新行; 用户上翻查看时不强行拉回底部
        self._log_pin = True
        self.log.vbar.config(command=self._on_log_scroll)
        self.log.bind('<MouseWheel>', self._on_log_wheel)
        self.log.bind('<Button-4>', self._on_log_wheel)   # Linux 上滚
        self.log.bind('<Button-5>', self._on_log_wheel)   # Linux 下滚
        th = tk.Frame(left)
        th.pack(fill='x')
        self._trend_lab = tk.Label(th, text='胜率趋势(我方视角%):',
                                   font=('Microsoft YaHei', 9))
        self._trend_lab.pack(side='left')
        tk.Button(th, text='切换视角', command=self.toggle_trend,
                  font=('Microsoft YaHei', 8)).pack(side='right')
        self.trend_cv = tk.Canvas(left, height=170, bg='#ffffff')
        self.trend_cv.pack(fill='x')
        right = tk.Frame(main)
        right.pack(side='right', fill='both', expand=True, padx=(10, 0))
        self.state_lab = tk.Label(
            right, justify='left', anchor='w', wraplength=470,
            font=('Microsoft YaHei', 10), fg='#0a3d62',
            text='状态投影: 启动后显示 执子/轮次/手数/黑白/胜率')
        self.state_lab.pack(fill='x', pady=(0, 4))
        self.board_cv = tk.Canvas(right, width=440, height=440,
                                  bg='#f0ead6')
        self.board_cv.pack(fill='both', expand=True)
        self.board_cv.bind('<Configure>', self._on_cv_resize)
        self.board_info = tk.Label(right, text='棋盘面板: 等待读取...',
                                   font=('Microsoft YaHei', 9),
                                   justify='left', anchor='w')
        self.board_info.pack(fill='x', pady=(4, 0))
        tk.Label(right, text='(与腾讯围棋实际画面并排核对识别是否正确)',
                 fg='#999999', font=('Microsoft YaHei', 8)).pack(anchor='w')

        self.root.protocol('WM_DELETE_WINDOW', self.on_close)
        self.poll_log()
        self.poll_board()
        self.root.after(300, self._drain_q)
        self.poll_trend()

    # ---------- 棋盘对账(后台线程读盘) ----------
    def _mask_hover(self, res):
        """鼠标停在游戏棋盘某格上时屏蔽该格(悬停假子会导致镜像棋盘抖动)"""
        try:
            if res is None:
                return res
            import ctypes
            pt = ctypes.wintypes.POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            xs, ys = res['xs'], res['ys']
            rect, n = res['rect'], res['n']
            step = float(res.get('step', 30))
            best, bd = None, 1e18
            for i in range(n):
                for j in range(n):
                    dx = pt.x - (rect[0] + float(xs[j]))
                    dy = pt.y - (rect[1] + float(ys[i]))
                    d = dx * dx + dy * dy
                    if d < bd:
                        bd, best = d, (i, j)
            if best is not None and bd < (step * 0.42) ** 2:
                i, j = best
                b = [list(r) for r in res['board']]
                if b[i][j] in 'XO':
                    b[i][j] = '?'
                    res = dict(res)
                    res['board'] = [''.join(r) for r in b]
            return res
        except Exception:
            return res

    def poll_state(self):
        """轮询主进程状态投影文件 ui_state.json -> ('state', dict|None)"""
        import threading as _th

        def worker():
            try:
                with open(STATE, 'r', encoding='utf-8') as f:
                    d = json.load(f)
                self._q.put(('state', d))
            except Exception:
                self._q.put(('state', None))
        _th.Thread(target=worker, daemon=True).start()

    def _fmt_state(self, d):
        """状态投影一行文案: 局/执子/轮次/路数/手数/黑白/胜率/引擎"""
        p = []
        game = d.get('game') or 0
        assist = d.get('assist')
        turn = d.get('turn')
        n = d.get('n') or 0
        b = d.get('b', 0)
        w = d.get('w', 0)
        p.append('第%d盘' % game if game else '对局')
        p.append('执%s' % ('黑' if assist == 'black'
                           else ('白' if assist == 'white' else '?')))
        if turn == 'black':
            t = '轮到黑'
        elif turn == 'white':
            t = '轮到白'
        else:
            t = '轮次?'
        if assist:
            t += '(我方)' if turn == assist else '(对方)'
        p.append(t)
        if n:
            p.append('%d路' % n)
        p.append('黑%d 白%d' % (b, w))
        if d.get('move_no'):
            p.append('第%d手' % d['move_no'])
        wr = d.get('wr')
        if wr is not None:
            lead = d.get('lead')
            ls = (' 目差%+.1f' % lead) if lead is not None else ''
            p.append('胜率%.1f%%%s' % (wr * 100, ls))
        if d.get('mv'):
            p.append('推荐%s' % d['mv'])
        src = d.get('turn_src')
        if src:
            mp = {'visual': '视觉', 'empty': '空盘', 'parity': '奇偶',
                  'manual': '手动', 'unresolved': '未定'}
            p.append('锚定:%s' % mp.get(src, src))
        st = d.get('status')
        if st:
            sm = {'boot': '启动中', 'wait': '等待', 'analyze': '分析中',
                  'clicked': '已落子', 'fail': '落子失败',
                  'gameover': '终局', 'new': '新局'}
            p.append(sm.get(st, st))
        return ' | '.join(p)

    def _state_result(self, d):
        try:
            if d is None:
                self.state_lab.config(
                    text='状态投影: 未启动(启动自动落子后显示)',
                    fg='#999999')
                return
            self.state_lab.config(text=self._fmt_state(d), fg='#0a3d62')
        except Exception:
            pass

    def poll_board(self):
        if self._board_busy:
            self.root.after(800, self.poll_board)
            return
        self._board_busy = True
        self._ticks += 1

        def worker():
            try:
                sv = self.size_var.get()
                if sv != '0':
                    size_fix = int(sv)
                else:
                    # 自动模式: 不用 OCR 标题强制路数(OCR 偶发误读会在缓存
                    # 期内整盘按错尺寸重绘=周期性闪动); 网格自动检测已修复
                    # (多余线刷分问题), 直接信任 auto
                    size_fix = 0
                res = br.read_current(do_align=(self._ticks % 15 == 1),
                                      force_size=size_fix, use_calib=False)
                res = self._mask_hover(res)
                self._q.put(('board', res))
            except Exception:
                self._q.put(('board', None))
            finally:
                self._board_busy = False

        threading.Thread(target=worker, daemon=True).start()

    def _drain_q(self):
        try:
            while True:
                kind, payload = self._q.get_nowait()
                if kind == 'board':
                    self._board_result(payload)
                elif kind == 'state':
                    self._state_result(payload)
        except Exception:
            pass
        if not self._board_busy:
            delay = 300 if self._fast_poll else 1000
            self._fast_poll = False
            self.root.after(delay, self.poll_board)
        self.root.after(600, self.poll_state)
        self.root.after(300, self._drain_q)

    def _board_result(self, res):
        try:
            if res is not None:
                self._board_blank = False
                key = (res['n'], ''.join(''.join(r) for r in res['board']))
                if key != self.board_key:
                    if key == self._pending_key:
                        # 连续两轮读数一致才更新显示(抗动画帧/瞬时误读闪动;
                        # 读盘已容错最后一手标记, 确认延迟典型 ~1s)
                        self._pending_key = None
                        self.board_key = key
                        self.draw_board(res)
                    else:
                        self._pending_key = key
                        self._fast_poll = True   # 待确认: 下轮快速复读
                else:
                    self._pending_key = None
                cnt = (sum(r.count('X') for r in res['board']),
                       sum(r.count('O') for r in res['board']))
                self.board_info.configure(
                    text='识别: {}路  黑{} 白{}  来源:{}  {}'.format(
                        res['n'], cnt[0], cnt[1], res['src'],
                        time.strftime('%H:%M:%S')))
            else:
                self.board_key = None
                self._pending_key = None
                # 占位只在状态切换时画一次, 否则每轮重画会造成画布闪动
                if not self._board_blank:
                    self._board_blank = True
                    self.board_cv.delete('all')
                    self.board_cv.create_text(
                        220, 220, text='未检测到棋盘\n(非对局画面/被遮挡)',
                        font=('Microsoft YaHei', 12), fill='#999999',
                        justify='center')
                self.board_info.configure(
                    text='识别: 无棋盘  {}'.format(time.strftime('%H:%M:%S')))
        except Exception:
            pass

    def _on_cv_resize(self, _e=None):
        if self.board_key is None or self._redraw_pending:
            return
        try:
            w, h = self.board_cv.winfo_width(), self.board_cv.winfo_height()
            if (w, h) == self._cv_size:
                return   # 尺寸没变(事件噪音), 不重画
        except Exception:
            return
        self._redraw_pending = True
        self.root.after(120, self._do_redraw)

    def _do_redraw(self):
        self._redraw_pending = False
        res = getattr(self, '_last_res', None)
        if res is not None:
            try:
                self._cv_size = (self.board_cv.winfo_width(),
                                 self.board_cv.winfo_height())
            except Exception:
                pass
            self.draw_board(res)

    def draw_board(self, res):
        self._last_res = res
        cv = self.board_cv
        cv.delete('all')
        n = res['n']
        board = res['board']
        W = max(200, cv.winfo_width())
        H = max(200, cv.winfo_height())
        S = min(W, H)
        pad = max(14, int(S * 0.055))
        step = (S - 2 * pad) / (n - 1)
        ox = (W - S) / 2
        oy = (H - S) / 2
        cv.create_rectangle(0, 0, W, H, fill=WOOD, outline='')
        for i in range(n):
            y = oy + pad + i * step
            cv.create_line(ox + pad, y, ox + pad + step * (n - 1), y,
                           fill=LINE)
        for j in range(n):
            x = ox + pad + j * step
            cv.create_line(x, oy + pad, x, oy + pad + step * (n - 1),
                           fill=LINE)
        for (i, j) in br.STAR_POINTS.get(n, []):
            r = max(2.0, step * 0.09)
            cv.create_oval(ox + pad + j * step - r,
                           oy + pad + i * step - r,
                           ox + pad + j * step + r,
                           oy + pad + i * step + r, fill=LINE, outline='')
        r = step * 0.44
        for i in range(n):
            for j in range(n):
                c = board[i][j]
                if c == '.':
                    continue
                x = ox + pad + j * step
                y = oy + pad + i * step
                if c == 'X':
                    cv.create_oval(x - r, y - r, x + r, y + r,
                                   fill='#1a1a1a', outline='')
                else:
                    cv.create_oval(x - r, y - r, x + r, y + r,
                                   fill='#fafafa', outline='#888888')

    # ---------- 胜率趋势图 ----------
    def toggle_trend(self):
        self._trend_my = not self._trend_my
        self._trend_lab.configure(
            text='胜率趋势({}%):'.format('我方视角' if self._trend_my
                                       else '黑方视角'))
        if self._trend:
            self.draw_trend()

    def poll_trend(self):
        try:
            path = os.path.join(TOOLS, 'katago_trend.jsonl')
            data = []
            if os.path.exists(path):
                with open(path, encoding='utf-8') as f:
                    for ln in f:
                        ln = ln.strip()
                        if ln:
                            try:
                                data.append(json.loads(ln))
                            except Exception:
                                pass
            if data != self._trend:
                self._trend = data
                self.draw_trend()
        except Exception:
            pass
        self.root.after(1000, self.poll_trend)

    def draw_trend(self):
        cv = self.trend_cv
        cv.delete('all')
        W = max(200, cv.winfo_width())
        H = max(100, cv.winfo_height())
        if not self._trend:
            cv.create_text(W // 2, H // 2,
                           text='等待对局数据...', fill='#aaaaaa')
            return
        self._trend_lab.configure(
            text='胜率趋势({}%):'.format('我方视角' if self._trend_my
                                       else '黑方视角'))
        padl, padr, padt, padb = 34, 10, 8, 18
        y0, y1 = padt, H - padb
        cv.create_line(padl, y0, padl, y1, fill='#888')
        cv.create_line(padl, y1, W - padr, y1, fill='#888')
        cv.create_line(padl, (y0 + y1) / 2, W - padr, (y0 + y1) / 2,
                       fill='#ddd', dash=(2, 2))
        for val in (0, 50, 100):
            yy = y1 - (y1 - y0) * val / 100.0
            cv.create_text(padl - 4, yy, text=str(val), anchor='e',
                           font=('Consolas', 8), fill='#888')
        my = self._trend[-1].get('my', 'black') or 'black'
        ms = [r['m'] for r in self._trend]
        # 视角: 黑方视角直接黑胜率; 我方视角按执色换算。
        # 关键: 每条记录按**它自己**的 my 换算! 跨局(分先轮换)时各段执色不同
        # (文件里同时存在 my=black/white), 若统一用最后一条的 my 换算全部历史
        # 点, 会把执色不同的前段数据镜像翻转(我方胜率显示反)。
        vals = []
        for r in self._trend:
            bw = max(0.0, min(100.0, r['bw']))
            r_my = r.get('my', 'black') or 'black'
            if self._trend_my:
                vals.append(bw if r_my == 'black' else 100.0 - bw)
            else:
                vals.append(bw)
        m0, m1 = ms[0], ms[-1]
        span = max(1, m1 - m0)
        pts = []
        for m, mw in zip(ms, vals):
            x = padl + (W - padl - padr) * (m - m0) / span
            y = y1 - (y1 - y0) * mw / 100.0
            pts.append((x, y))
        for i in range(1, len(pts)):
            cv.create_line(pts[i - 1][0], pts[i - 1][1],
                           pts[i][0], pts[i][1], fill='#d33', width=2)
        if pts:
            lx, ly = pts[-1]
            cv.create_oval(lx - 3, ly - 3, lx + 3, ly + 3, fill='#d33',
                           outline='')
            last = self._trend[-1]
            if self._trend_my:
                dsp = last['bw'] if my == 'black' else 100 - last['bw']
                lead_d = last['lead'] if my == 'black' else -last['lead']
                lab_s = '我方胜率{:.0f}% 目差{:+.1f} (执{})'.format(
                    dsp, lead_d, ('黑' if my == 'black' else '白'))
            else:
                dsp = last['bw']
                lead_d = last['lead']
                lab_s = '黑方胜率{:.0f}% 目差{:+.1f}'.format(dsp, lead_d)
            cv.create_text(W - padr, padt, anchor='ne',
                           text='第{}手 {}'.format(last['m'], lab_s),
                           font=('Consolas', 9), fill='#333')

    # ---------- 临时文件定时清理 ----------
    def _auto_cleanup(self):
        try:
            st = cf.cleanup_now()
            if st['removed'] > 0:
                self.write_log('[清理] 已删除 %d 个临时文件'
                               ' (释放 %.0fKB)' + chr(10)
                               % (st['removed'], st['freed_kb']))
        except Exception:
            pass
        try:
            self.root.after(600000, self._auto_cleanup)  # 每 10 分钟
        except Exception:
            pass

    def _log_update_pin(self):
        """视口是否贴底: 贴底才允许新日志自动滚动"""
        try:
            self._log_pin = self.log.yview()[1] >= 0.999
        except Exception:
            self._log_pin = True

    def _on_log_scroll(self, *args):
        """用户拖动/点击滚动条(接管 ScrolledText 默认 command)"""
        try:
            self.log.yview(*args)
        except Exception:
            pass
        self._log_update_pin()

    def _on_log_wheel(self, _e=None):
        """鼠标滚轮滚动日志后刷新贴底状态(滚动发生在默认绑定之后)"""
        self.root.after_idle(self._log_update_pin)

    def write_log(self, text):
        self.log.configure(state='normal')
        self.log.insert('end', text)
        if self._log_pin:
            self.log.see('end')   # 贴底才跟随; 上翻查看时保持视口不动
        self.log.configure(state='disabled')
        try:
            # 另存一份(play 子进程占用了 katago_ui.log), 供外部排查;
            # 超 2MB 轮转: 旧文件转 .1(覆盖), 新文件从头写
            _p = os.path.join(TOOLS, 'katago_ui_self.log')
            try:
                if os.path.exists(_p) and os.path.getsize(_p) > 2 * 1024 * 1024:
                    _p1 = _p + '.1'
                    if os.path.exists(_p1):
                        os.remove(_p1)
                    os.replace(_p, _p1)
            except Exception:
                pass
            with open(_p, 'a', encoding='utf-8') as f:
                f.write(text)
        except Exception:
            pass

    def poll_log(self):
        try:
            if os.path.exists(LOG):
                with open(LOG, 'rb') as f:
                    f.seek(self.log_pos)
                    data = f.read()
                    self.log_pos += len(data)
                if data:
                    try:
                        txt = data.decode('utf-8')
                    except UnicodeDecodeError:
                        txt = data.decode('gbk', errors='replace')
                    self._sess_log += txt
                    self.write_log(txt)
        except Exception:
            pass
        if self.proc is not None and self.proc.poll() is not None:
            self.write_log('\n[进程已结束]\n')
            self.set_running(False)
            self.proc = None
            self._offer_fallback()
        self.root.after(500, self.poll_log)

    # ---------- 启动识别失败 -> 人工兜底对话框 ----------
    def _offer_fallback(self):
        """自动识别失败(启动即退出)时弹出按钮对话框, 手动指定执子/轮到并重试"""
        if self._fallback_shown or self.proc is not None:
            return
        txt = self._sess_log
        # 仅当"启动失败"特征出现且没真正下过棋时才弹
        if not any(k in txt for k in ('无法', '失败', '拒绝')):
            return
        if '✔ 已落子' in txt or '检测到落子' in txt:
            return
        self._fallback_shown = True
        dlg = tk.Toplevel(self.root)
        dlg.title('识别失败 - 人工指定后重试')
        dlg.transient(self.root)
        dlg.grab_set()
        tk.Label(dlg, text='自动识别执色/行棋方失败, 请手动指定后重试:',
                 font=('Microsoft YaHei', 10), padx=14, pady=(10, 2)).pack()
        frm = tk.Frame(dlg, padx=14, pady=4)
        frm.pack()
        cv = tk.StringVar(value='auto')
        tv = tk.StringVar(value='auto')
        tk.Label(frm, text='我执子:', font=('Microsoft YaHei', 10)).grid(
            row=0, column=0, sticky='w')
        for i, (txt2, val) in enumerate([('自动', 'auto'), ('黑', 'black'),
                                         ('白', 'white')]):
            tk.Radiobutton(frm, text=txt2, value=val, variable=cv,
                           font=('Microsoft YaHei', 10)).grid(
                row=0, column=1 + i, sticky='w', padx=(4, 0))
        tk.Label(frm, text='当前轮到:', font=('Microsoft YaHei', 10)).grid(
            row=1, column=0, sticky='w', pady=(4, 0))
        for i, (txt2, val) in enumerate([('自动', 'auto'), ('黑方', 'black'),
                                         ('白方', 'white')]):
            tk.Radiobutton(frm, text=txt2, value=val, variable=tv,
                           font=('Microsoft YaHei', 10)).grid(
                row=1, column=1 + i, sticky='w', padx=(4, 0))
        bar = tk.Frame(dlg, padx=14, pady=(4, 12))
        bar.pack()

        def retry():
            dlg.destroy()
            self.color_var.set(cv.get())
            self.turn_var.set(tv.get())
            self._fallback_shown = False
            self.write_log('人工指定: 我执子=%s 轮到=%s, 重新启动\n'
                           % (cv.get(), tv.get()))
            self.start_play()

        tk.Button(bar, text='按此设置并重新启动', bg='#ccffcc',
                  font=('Microsoft YaHei', 10), command=retry).pack(
            side='left', padx=6)
        tk.Button(bar, text='取消', font=('Microsoft YaHei', 10),
                  command=dlg.destroy).pack(side='left', padx=6)

    # ---------- 子进程 ----------
    def _common_args(self):
        args = []
        sv = self.size_var.get()
        if sv != '0':
            args += ['--size', sv]
        args += ['--visits', self.visits_var.get()]
        if self.wait_var.get():
            args.append('--wait-new')
        mg = self.maxgames_var.get().strip()
        if mg.isdigit() and int(mg) > 0:
            args += ['--max-games', mg]
        if self.beep_var.get():
            args.append('--beep')
        return args

    def _spawn(self, args):
        if self.proc is not None:
            self.write_log('[已有任务在运行, 请先停止]\n')
            return
        try:
            with open(LOG, 'wb') as f:
                f.truncate(0)
        except Exception:
            pass
        self.log_pos = 0
        self._sess_log = ''
        self._fallback_shown = False
        self.write_log('$ ' + ' '.join(args) + '\n')
        self.proc = subprocess.Popen(
            [PY, '-u'] + args, cwd=TOOLS,
            stdout=open(LOG, 'ab'), stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.set_running(True)

    def set_running(self, running):
        st = 'normal' if not running else 'disabled'
        for b in (self.btn_start, self.btn_watch, self.btn_suggest):
            b.configure(state=st)
        self.btn_stop.configure(state='normal' if running else 'disabled')

    def _mode_arg(self):
        """对局模式 -> --mode 启动参数(各模式棋盘位置/缩放有细微偏差)。"""
        cn = self.mode_var.get()
        m = MODE_CN2EN.get(cn, 'challenge')  # 中文显示, 转回 board_reader 英文键
        return ['--mode', m]

    def start_play(self):
        color = self.color_var.get()
        args = ['katago_play.py', color]
        tv = self.turn_var.get()
        if tv in ('black', 'white'):
            args += ['--turn', tv]     # 人工兜底: 手动指定当前行棋方
        self._spawn(args + self._mode_arg() + self._common_args())

    def start_watch(self):
        self._spawn(['katago_suggest.py', '--watch'] + self._mode_arg()
                    + self._common_args())

    def start_suggest(self):
        args = ['katago_suggest.py']
        sv = self.size_var.get()
        if sv != '0':
            args += ['--size', sv]
        args += ['--visits', self.visits_var.get()]
        args += self._mode_arg()
        self._spawn(args)

    def stop(self):
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except Exception:
                self.proc.kill()
            self.proc = None
            self.write_log('\n[已手动停止]\n')
            self.set_running(False)
            self.state_lab.config(
                text='状态投影: 已停止', fg='#999999')
        try:
            subprocess.run(['taskkill', '/IM', 'katago.exe', '/F'],
                           capture_output=True,
                           creationflags=getattr(subprocess,
                                                 'CREATE_NO_WINDOW', 0))
        except Exception:
            pass

    def on_close(self):
        self.stop()
        self.root.destroy()


if __name__ == '__main__':
    app = KatagoUI()
    app.root.after(3000, app._auto_cleanup)   # 启动 3s 后先清一轮
    app.root.mainloop()
