"""棋盘区域框选校准工具（截图式）。

用法:  python select_board.py
流程:
  1. 截取当前屏幕, 弹出窗口
  2. 在棋盘上按住鼠标左键拖出一个矩形(四边大致压在棋盘最外圈线上即可)
  3. 按 1/2/3 切换 9/13/19 路, 红色网格预览会实时显示
  4. 方向键可微调整个选区(1px, Ctrl=5px), 不满意可重新拖
  5. 按 S 保存校准(grid_calib.json, 屏幕坐标), Q 退出

之后 board_reader.py 会整屏截图并按保存的网格采样识别。
"""
import json
import os
import tkinter as tk

from PIL import Image, ImageGrab, ImageTk

TOOLS = os.path.dirname(os.path.abspath(__file__))
CALIB = os.path.join(TOOLS, 'grid_calib.json')
GRID_C = '#ff4040'
HUD_C = '#00cc00'


class Selector:
    def __init__(self):
        self.img = ImageGrab.grab().convert('RGB')
        self.img.save(os.path.join(TOOLS, 'screen_base.png'))
        self.iw, self.ih = self.img.size
        # 缩放以适应屏幕
        scr_w = self.img.size[0]
        scr_h = self.img.size[1]
        self.scale = min((scr_w - 60) / scr_w, (scr_h - 220) / scr_h)
        if self.scale > 1:
            self.scale = 1
        self.cw = int(self.iw * self.scale)
        self.ch = int(self.ih * self.scale)
        self.size = 19
        self.region = None  # (x0,y0,x1,y1) 屏幕像素
        self._drag = None

        self.root = tk.Tk()
        self.root.title('框选棋盘区域')
        # 底部控制条(鼠标操作, 不依赖键盘焦点)
        bar = tk.Frame(self.root)
        bar.pack(side='bottom', fill='x', pady=4)
        tk.Label(bar, text='棋盘路数:').pack(side='left')
        for sz in (9, 13, 19):
            tk.Button(bar, text=f'{sz}路', width=4,
                      command=lambda s=sz: self.set_size(s)).pack(side='left', padx=2)
        tk.Button(bar, text='重新截图(R)', command=self.recapture).pack(side='left', padx=8)
        tk.Button(bar, text='保存校准(S)', command=self.save,
                  bg='#ccffcc').pack(side='left', padx=8)
        tk.Button(bar, text='退出(Q)', command=self.root.destroy).pack(side='left', padx=8)
        tk.Label(bar, text='  操作: 在棋盘上拖出矩形(四边压最外圈线)',
                 fg='#666666').pack(side='left', padx=8)
        self.photo = ImageTk.PhotoImage(self.img.resize((self.cw, self.ch)))
        self.cv = tk.Canvas(self.root, width=self.cw, height=self.ch,
                            cursor='crosshair')
        self.cv.pack()
        self.cv.create_image(0, 0, anchor='nw', image=self.photo)
        self.cv.bind('<Button-1>', self.on_down)
        self.cv.bind('<B1-Motion>', self.on_drag)
        self.cv.bind('<ButtonRelease-1>', self.on_up)
        self.root.bind('<Key>', self.on_key)
        self.cv.focus_set()
        self.rect_item = None
        self.grid_items = []
        self.hud = self.cv.create_text(
            8, 8, anchor='nw', text='', fill=HUD_C,
            font=('Consolas', 13), tags='hud')

    def set_size(self, sz):
        self.size = sz
        self.draw()

    def set_region(self, r):
        self.region = tuple(int(v) for v in r)
        self.draw()

    def draw(self):
        self.cv.delete('selrect')
        self.cv.delete('grid')
        if self.region is None:
            self.cv.itemconfig(self.hud, text='拖出矩形框住棋盘')
            return
        x0, y0, x1, y1 = self.region
        sx0, sy0 = x0 * self.scale, y0 * self.scale
        sx1, sy1 = x1 * self.scale, y1 * self.scale
        self.cv.create_rectangle(sx0, sy0, sx1, sy1, outline=GRID_C,
                                 width=2, tags='selrect')
        n = self.size
        for i in range(n):
            fx = (x0 + (x1 - x0) * i / (n - 1)) * self.scale
            fy = (y0 + (y1 - y0) * i / (n - 1)) * self.scale
            self.cv.create_line(fx, sy0, fx, sy1, fill=GRID_C, tags='grid')
            self.cv.create_line(sx0, fy, sx1, fy, fill=GRID_C, tags='grid')
        cell = ((x1 - x0) / (n - 1) + (y1 - y0) / (n - 1)) / 2
        self.cv.itemconfig(
            self.hud,
            text=f'{n}路  格距≈{cell:.1f}px  选区=({x0},{y0})-({x1},{y1})  '
                 f'[拖=重选 方向=微调 S=保存 Q=退出]')

    def on_down(self, e):
        self._drag = (e.x / self.scale, e.y / self.scale)
        self.cv.delete('selrect')

    def on_drag(self, e):
        if self._drag:
            x0, y0 = self._drag
            x1, y1 = e.x / self.scale, e.y / self.scale
            self.cv.delete('selrect')
            self.cv.create_rectangle(x0 * self.scale, y0 * self.scale,
                                     x1 * self.scale, y1 * self.scale,
                                     outline=GRID_C, width=2, tags='selrect')

    def on_up(self, e):
        if self._drag:
            x0, y0 = self._drag
            x1, y1 = e.x / self.scale, e.y / self.scale
            self._drag = None
            x0, x1 = sorted((int(x0), int(x1)))
            y0, y1 = sorted((int(y0), int(y1)))
            if x1 - x0 > 20 and y1 - y0 > 20:
                self.set_region((x0, y0, x1, y1))

    def on_key(self, e):
        k = e.keysym.lower()
        ctrl = bool(e.state & 0x0004)
        if k == 'q':
            self.root.destroy()
            return
        if k == 'r':
            self.recapture()
            return
        if k == 's' or k == 'return':
            self.save()
            return
        if k in ('1', '2', '3'):
            self.size = {1: 9, 2: 13, 3: 19}[int(k)]
            self.draw()
            return
        if self.region is None:
            return
        x0, y0, x1, y1 = self.region
        d = 5 if ctrl else 1
        if k == 'left':
            self.set_region((x0 - d, y0, x1 - d, y1))
        elif k == 'right':
            self.set_region((x0 + d, y0, x1 + d, y1))
        elif k == 'up':
            self.set_region((x0, y0 - d, x1, y1 - d))
        elif k == 'down':
            self.set_region((x0, y0 + d, x1, y1 + d))

    def recapture(self):
        """R: 重新截取当前屏幕（窗口移动/切换后重新框选）"""
        self.img = ImageGrab.grab().convert('RGB')
        self.img.save(os.path.join(TOOLS, 'screen_base.png'))
        self.photo = ImageTk.PhotoImage(self.img.resize((self.cw, self.ch)))
        self.cv.delete('all')
        self.cv.create_image(0, 0, anchor='nw', image=self.photo)
        self.region = None
        self._drag = None
        self.hud = self.cv.create_text(8, 8, anchor='nw', text='', fill=HUD_C,
                                       font=('Consolas', 13), tags='hud')

    def save(self):
        if self.region is None:
            return
        x0, y0, x1, y1 = self.region
        data = {'mode': 'screen', 'x0': x0, 'y0': y0, 'x1': x1, 'y1': y1,
                'size': self.size, 'screen_w': self.iw, 'screen_h': self.ih}
        # 记录游戏窗口位置, 之后窗口移动时读取器可自动跟随
        try:
            import sys
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import board_reader as br
            wr = br.window_rect(br.PID)
            data.update(win_x0=wr[0], win_y0=wr[1],
                        win_w=wr[2] - wr[0], win_h=wr[3] - wr[1])
        except Exception:
            pass
        with open(CALIB, 'w') as f:
            json.dump(data, f, indent=2)
        print('已保存校准 ->', CALIB)
        self.cv.itemconfig(
            self.hud,
            text=f'已保存 {self.size}路 选区=({x0},{y0})-({x1},{y1})  (Q 退出)',
            fill='#ffff00')


if __name__ == '__main__':
    Selector().root.mainloop()
