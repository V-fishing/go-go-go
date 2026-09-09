# -*- coding: utf-8 -*-
"""头像角标(执子标记)框选校准工具。

用法: 1) 把腾讯围棋切到对局画面(能看到双方头像与角标棋子)
      2) python avatar_calib.py
      3) 点 [框选我方角标] 在我方头像右上角的棋子外拖一个小框
      4) 点 [实时检测] 验证识别结果(显示 我方执X)
      5) 点 [保存校准] 写入 avatar_calib.json

之后 winclick/katago_play 只用官方"我方角标"判定执色(不识别对方角标)。
"""
import json
import os
import sys
import time
import tkinter as tk

import numpy as np
from PIL import Image, ImageTk

TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS)
import board_reader as br
import winclick

CALIB = os.path.join(TOOLS, 'avatar_calib.json')
C_MY = '#ff4040'      # 我方框: 红
HUD_C = '#0000cc'


class AvatarCalib:
    def __init__(self):
        self.mode = None          # 'my'
        self.boxes = {'my': None}   # 窗口像素坐标
        self.last_key = None          # 最近编辑的框(my/opp)
        self._drag = None
        self.win_rect = None
        self.zoom = None
        self.root = tk.Tk()
        self.root.title('框选头像角标(执子标记)')
        self.recapture()
        bar = tk.Frame(self.root)
        bar.pack(side='bottom', fill='x', pady=4)
        tk.Button(bar, text='框选我方角标', bg='#ffcccc',
                  command=lambda: self.set_mode('my')).pack(side='left', padx=2)
        tk.Button(bar, text='重截(R)', command=self.recapture).pack(side='left', padx=6)
        tk.Button(bar, text='实时检测(T)', command=self.live_test,
                  bg='#ffffcc').pack(side='left', padx=6)
        tk.Button(bar, text='保存校准(S)', command=self.save,
                  bg='#ccffcc').pack(side='left', padx=6)
        tk.Button(bar, text='退出(Q)', command=self.root.destroy).pack(side='left', padx=6)
        self.status = tk.Label(bar, text='', fg='#cc0000', anchor='w')
        self.status.pack(side='left', fill='x', padx=8)
        self.photo = ImageTk.PhotoImage(
            self.img.resize((self.cw, self.ch)))
        self.cv = tk.Canvas(self.root, width=self.cw, height=self.ch,
                            cursor='crosshair')
        self.cv.pack()
        self.cv.create_image(0, 0, anchor='nw', image=self.photo)
        self.cv.bind('<Button-1>', self.on_down)
        self.cv.bind('<B1-Motion>', self.on_drag)
        self.cv.bind('<ButtonRelease-1>', self.on_up)
        self.root.bind('<Key>', self.on_key)
        self.rect_items = {}
        self._clear_zoom()

    # ---------- 截图与缩放 ----------
    def recapture(self):
        try:
            rect = br.window_rect(br.PID)
        except SystemExit:
            print('未找到腾讯围棋窗口, 请先打开对局画面')
            sys.exit(1)
        self.win_rect = rect
        self.img = ImageGrab.grab(bbox=rect).convert('RGB')
        self.iw, self.ih = self.img.size
        scr_w = self.root.winfo_screenwidth()
        scr_h = self.root.winfo_screenheight()
        self.scale = min((scr_w - 80) / self.iw, (scr_h - 260) / self.ih)
        if self.scale > 2.0:
            self.scale = 2.0
        self.cw = int(self.iw * self.scale)
        self.ch = int(self.ih * self.scale)
        if hasattr(self, 'cv'):
            self.photo = ImageTk.PhotoImage(self.img.resize((self.cw, self.ch)))
            self.cv.delete('all')
            self.cv.create_image(0, 0, anchor='nw', image=self.photo)
            self.cv.config(width=self.cw, height=self.ch)
            self.redraw()

    def set_mode(self, m):
        self.mode = m
        self.status.config(
            text='请在我方头像的角标棋子上拖一个小框')

    def on_down(self, e):
        self._drag = (e.x / self.scale, e.y / self.scale)
        if self.mode:
            self.cv.delete('drag')

    def on_drag(self, e):
        if self._drag:
            x0, y0 = self._drag
            x1, y1 = e.x / self.scale, e.y / self.scale
            self.cv.delete('drag')
            self.cv.create_rectangle(x0 * self.scale, y0 * self.scale,
                                     x1 * self.scale, y1 * self.scale,
                                     outline=(C_MY if self.mode == 'my'
                                              else C_OPP), width=2,
                                     tags='drag')

    def on_up(self, e):
        if self._drag and self.mode:
            x0, y0 = self._drag
            x1, y1 = e.x / self.scale, e.y / self.scale
            self._drag = None
            x0, x1 = sorted((int(x0), int(x1)))
            y0, y1 = sorted((int(y0), int(y1)))
            if x1 - x0 < 8 or y1 - y0 < 8:
                self.status.config(text='框太小, 请重新拖(框住整个角标棋子)')
                return
            self.boxes[self.mode] = (x0, y0, x1, y1)
            self.last_key = self.mode
            self.mode = None
            self.redraw()
            self._zoom_box((x0, y0, x1, y1))
            self.check_live()
        self._drag = None

    def redraw(self):
        for t in ('box_my', 'box_opp'):
            self.cv.delete(t)
        b = self.boxes['my']
        if b:
            x0, y0, x1, y1 = b
            self.cv.create_rectangle(x0 * self.scale, y0 * self.scale,
                                     x1 * self.scale, y1 * self.scale,
                                     outline=C_MY, width=2, tags='box_my')
            self.cv.create_text(
                x0 * self.scale, max(2, y0 * self.scale - 6), anchor='sw',
                text='我方',
                fill=C_MY, font=('Microsoft YaHei', 10, 'bold'),
                tags='box_my')

    # ---------- 放大预览 ----------
    def _clear_zoom(self):
        if self.zoom:
            try:
                self.zoom.destroy()
            except Exception:
                pass
            self.zoom = None

    def _zoom_box(self, b):
        self._clear_zoom()
        x0, y0, x1, y1 = b
        pad = 8
        x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
        x1, y1 = min(self.iw, x1 + pad), min(self.ih, y1 + pad)
        crop = self.img.crop((x0, y0, x1, y1)).resize(
            ((x1 - x0) * 8, (y1 - y0) * 8), Image.LANCZOS)
        self.zoom = tk.Toplevel(self.root)
        self.zoom.title('放大预览(8x) - 应只框住一颗黑/白小棋子')
        p = ImageTk.PhotoImage(crop)
        tk.Label(self.zoom, image=p).pack()
        self.zoom._img = p

    # ---------- 实时识别反馈 ----------
    def _classify_now(self, key):
        if not self.boxes[key]:
            return None
        try:
            img = ImageGrab.grab(bbox=self.win_rect).convert('RGB')
            return winclick.classify_avatar_box(img.crop(self.boxes[key]))
        except Exception:
            return None

    def check_live(self):
        if not self.boxes['my']:
            return
        m = self._classify_now('my')
        sm = '黑' if m == 'black' else ('白' if m == 'white' else '?')
        ok = m in ('black', 'white')
        txt = f'实时识别: 我方角标={sm}'
        if ok:
            txt += f'   => 我方执{("黑" if m=="black" else "白")}  ✔ 可保存'
            self.status.config(text=txt, fg='#006600')
        else:
            txt += '  (未识别到黑/白, 请调整框的位置/大小)'
            self.status.config(text=txt, fg='#cc0000')

    # ---------- 实时检测 ----------
    def live_test(self):
        if not self.boxes['my']:
            self.status.config(text='请先框选我方角标')
            return
        self.save(quiet=True)
        my = winclick.avatar_my_color()
        if my:
            self.status.config(
                text=f'✔ 实时检测: 我方执{("黑" if my=="black" else "白")}')
        else:
            self.status.config(text='✘ 检测失败(框没对准棋子?), 调整后重试')

    # ---------- 保存 ----------
    def save(self, quiet=False):
        if not self.boxes['my']:
            self.status.config(text='请先框选我方角标')
            return
        x0, y0, x1, y1 = self.win_rect
        data = {'win_w': self.iw, 'win_h': self.ih,
                'my': list(self.boxes['my'])}
        json.dump(data, open(CALIB, 'w'))
        if not quiet:
            self.status.config(
                text=f'✔ 已保存 {CALIB}  我方={data["my"]}')
            print('已保存:', CALIB, data)

    def on_key(self, e):
        k = e.keysym.lower()
        step = 5 if e.state & 0x0001 else 1   # Shift=5px
        if k in ('up', 'down', 'left', 'right'):
            if not self.last_key or not self.boxes[self.last_key]:
                self.status.config(text='先框选一个角标再微调')
                return
            x0, y0, x1, y1 = self.boxes[self.last_key]
            if k == 'up':
                y0, y1 = y0 - step, y1 - step
            elif k == 'down':
                y0, y1 = y0 + step, y1 + step
            elif k == 'left':
                x0, x1 = x0 - step, x1 - step
            else:
                x0, x1 = x0 + step, x1 + step
            self.boxes[self.last_key] = (x0, y0, x1, y1)
            self.redraw()
            self.check_live()
        elif k in ('r',):
            self.recapture()
            self.check_live()
        elif k in ('t',):
            self.live_test()
        elif k in ('s',):
            self.save()
        elif k in ('q', 'escape'):
            self.root.destroy()


from PIL import ImageGrab  # noqa: E402


def main():
    app = AvatarCalib()
    app.status.config(text='先点 [框选我方角标], 在我方头像右上角的棋子上拖框; '
                           '只识别我方角标即可判定执色')
    app.root.mainloop()


if __name__ == '__main__':
    main()
