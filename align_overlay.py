"""腾讯围棋 棋盘网格人工校准覆盖层（透明置顶）。

用法:  python align_overlay.py
启动后网格会叠在腾讯围棋窗口上。若自动检测失败,网格出现在窗口中央,
拖到棋盘上对齐即可。

操作:
  鼠标拖动           移动整个网格
  滚轮               整体缩放(格距)
  方向键             微移 1px   (Ctrl+方向键 = 5px)
  1 / 2 / 3          切换 9 / 13 / 19 路
  [  ]               横格距 -/+ 0.5px
  ;  '               纵格距 -/+ 0.5px
  C                  重新吸附到腾讯围棋窗口(窗口移动后)
  S                  保存校准到 tools/grid_calib.json
  Q                  退出(不保存)

对齐标准: 让网格线的交叉点与棋盘线的交叉点完全重合即可,
          不需要管棋子。
"""
import ctypes
import ctypes.wintypes
import json
import os
import sys

import tkinter as tk

TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS)
PID = 26768
MAGIC = '#010203'
COLOR = '#00ff88'
CALIB = os.path.join(TOOLS, 'grid_calib.json')


def window_rect(pid):
    user32 = ctypes.windll.user32
    found = []
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def cb(hwnd, _):
        p = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid and user32.IsWindowVisible(hwnd):
            r = ctypes.wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            if r.right - r.left > 200 and r.bottom - r.top > 200:
                found.append((r.left, r.top, r.right, r.bottom))
        return True

    user32.EnumWindows(proto(cb), 0)
    if not found:
        return None
    return max(found, key=lambda r: (r[2] - r[0]) * (r[3] - r[1]))


def try_auto_grid(win_rect):
    """尝试用自动检测给出初始网格 (size, origin_x, origin_y, step)"""
    try:
        from PIL import ImageGrab
        import numpy as np
        from board_reader import detect_grid
        img = ImageGrab.grab(bbox=win_rect).convert('RGB')
        a = np.asarray(img).astype(np.int16)
        det = detect_grid(a.mean(axis=2), img=a)
        if det is not None:
            xs, ys = det
            sx = float(np.median(np.diff(xs)))
            sy = float(np.median(np.diff(ys)))
            return len(xs), float(xs[0]), float(ys[0]), (sx + sy) / 2
    except Exception:
        pass
    return None


class Overlay:
    def __init__(self):
        rect = window_rect(PID)
        if rect is None:
            print('找不到腾讯围棋窗口')
            sys.exit(1)
        self.win = rect
        auto = try_auto_grid(rect)
        self.size = 19
        self.ox = 0.0
        self.oy = 0.0
        self.sx = self.sy = 31.0
        if auto:
            self.size, self.ox, self.oy, st = auto
            self.sx = self.sy = st
            print(f'自动检测到 {self.size} 路棋盘, 可作为起点微调')
        else:
            w, h = rect[2] - rect[0], rect[3] - rect[1]
            self.ox = (w - self.sx * (self.size - 1)) / 2
            self.oy = (h - self.sy * (self.size - 1)) / 2
            print('自动检测失败, 网格已居中, 请手动拖到棋盘上对齐')

        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes('-topmost', True)
        self.root.attributes('-transparentcolor', MAGIC)
        self.root.geometry(f'{rect[2]-rect[0]}x{rect[3]-rect[1]}+{rect[0]}+{rect[1]}')
        self.cv = tk.Canvas(self.root, bg=MAGIC, highlightthickness=0)
        self.cv.pack(fill='both', expand=True)
        self.cv.bind('<Button-1>', self.on_down)
        self.cv.bind('<B1-Motion>', self.on_drag)
        self.cv.bind('<MouseWheel>', self.on_wheel)
        self.root.bind('<Key>', self.on_key)
        self._drag = None
        self.redraw()

    def redraw(self):
        self.cv.delete('all')
        n = self.size
        W = self.cv.winfo_width()
        H = self.cv.winfo_height()
        if W <= 1:
            W = self.win[2] - self.win[0]
            H = self.win[3] - self.win[1]
        for i in range(n):
            x = self.ox + i * self.sx
            y = self.oy + i * self.sy
            if -5 <= x <= W + 5:
                self.cv.create_line(x, 0, x, H, fill=COLOR)
            if -5 <= y <= H + 5:
                self.cv.create_line(0, y, W, y, fill=COLOR)
        txt = (f'{n}路  stepX={self.sx:.1f} stepY={self.sy:.1f}  '
               f'origin=({self.ox:.0f},{self.oy:.0f})')
        self.cv.create_text(8, 8, anchor='nw', text=txt, fill=COLOR, font=('Consolas', 11))
        self.cv.create_text(8, 24, anchor='nw',
                            text='拖=移动 滚轮=缩放 方向=微调 []=横距 ;=纵距 1/2/3=路数 S=保存 Q=退出',
                            fill=COLOR, font=('Consolas', 10))

    def on_down(self, e):
        self._drag = (e.x, e.y)

    def on_drag(self, e):
        if self._drag:
            dx, dy = e.x - self._drag[0], e.y - self._drag[1]
            self._drag = (e.x, e.y)
            self.ox += dx
            self.oy += dy
            self.redraw()

    def on_wheel(self, e):
        f = 1.03 if e.delta > 0 else 0.97
        self.sx = min(80, max(10, self.sx * f))
        self.sy = min(80, max(10, self.sy * f))
        self.redraw()

    def on_key(self, e):
        k = e.keysym.lower()
        ctrl = bool(e.state & 0x0004)
        if k in ('left', 'right', 'up', 'down'):
            d = 5 if ctrl else 1
            if k == 'left':
                self.ox -= d
            elif k == 'right':
                self.ox += d
            elif k == 'up':
                self.oy -= d
            else:
                self.oy += d
        elif k == '1':
            self.size = 9
        elif k == '2':
            self.size = 13
        elif k == '3':
            self.size = 19
        elif k == 'bracketleft':
            self.sx = max(8, self.sx - 0.5)
        elif k == 'bracketright':
            self.sx = min(90, self.sx + 0.5)
        elif k == 'semicolon':
            self.sy = max(8, self.sy - 0.5)
        elif k == 'apostrophe':
            self.sy = min(90, self.sy + 0.5)
        elif k == 'c':
            rect = window_rect(PID)
            if rect:
                dx, dy = rect[0] - self.win[0], rect[1] - self.win[1]
                self.win = rect
                self.ox += dx
                self.oy += dy
                self.root.geometry(
                    f'{rect[2]-rect[0]}x{rect[3]-rect[1]}+{rect[0]}+{rect[1]}')
        elif k == 's':
            self.save()
            return
        elif k == 'q':
            self.root.destroy()
            return
        self.redraw()

    def save(self):
        data = {'size': self.size, 'origin_x': self.ox, 'origin_y': self.oy,
                'step_x': self.sx, 'step_y': self.sy,
                'win_x0': self.win[0], 'win_y0': self.win[1]}
        with open(CALIB, 'w') as f:
            json.dump(data, f, indent=2)
        print('校准已保存 ->', CALIB)
        self.cv.create_text(8, 42, anchor='nw', text='SAVED 已保存 (Q 退出)',
                            fill='#ffff00', font=('Consolas', 11))


if __name__ == '__main__':
    Overlay().root.mainloop()
