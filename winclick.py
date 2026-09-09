"""腾讯围棋窗口: 无光标点击 + 页面按钮 OCR 识别 (共享模块)。

- post_click(x, y): 屏幕坐标 -> PostMessage 到 Chromium 渲染窗口(不移动光标)
- ocr_buttons(): 识别窗口内文字按钮 -> [(label, cx, cy), ...]
- click_label(keyword): 按文字点击按钮(如 '停一手'), 成功返回 True
"""
import ctypes
import ctypes.wintypes
import json
import os
import subprocess
import threading
import time

PID = 26768  # WeChatAppEx.exe
OCR_PS1 = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ocr.ps1')
OCR_DAEMON_PS1 = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              'ocr_daemon.ps1')
_tmp_n = [0]


def tmp_png(tag):
    """临时截图路径: 系统 temp + pid + 序号, 避免项目目录文件锁/堆积"""
    import tempfile
    _tmp_n[0] += 1
    return os.path.join(tempfile.gettempdir(),
                        f'goai_{os.getpid()}_{_tmp_n[0]}_{tag}.png')


def save_tmp(img, tag, tries=3):
    """保存临时截图(瞬时文件锁自动重试)"""
    png = tmp_png(tag)
    for i in range(tries):
        try:
            img.save(png)
            return png
        except OSError:
            if i == tries - 1:
                raise
            time.sleep(0.15)
    return png
_OCR_DAEMON = {'proc': None}
_OCR_LOCK = threading.Lock()   # 守护进程串行 stdin; 多线程并发 OCR 时排队


def _ocr_spawn(png):
    """单次 powershell OCR(守护进程不可用时的回退) -> items 列表"""
    p = subprocess.run(
        ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
         '-File', OCR_PS1, png], capture_output=True, timeout=60,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        return json.loads(p.stdout.decode('utf-8'))
    except Exception:
        return []


def _ocr_daemon():
    """常驻 OCR 进程(首次启动含引擎加载); 崩溃自动重启一次"""
    proc = _OCR_DAEMON.get('proc')
    if proc is not None and proc.poll() is None:
        return proc
    try:
        proc = subprocess.Popen(
            ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
             '-File', OCR_DAEMON_PS1],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        line = proc.stdout.readline()   # 等 READY(首次含引擎加载)
        if b'READY' not in line:
            try:
                proc.kill()
            except Exception:
                pass
            return None
        _OCR_DAEMON['proc'] = proc
        return proc
    except Exception:
        return None


def ocr_items(png):
    """OCR 图片 -> items 列表。优先常驻守护(省 ~0.5s/次进程启动),
    守护不可用/挂起(8s 超时)时回退单次 powershell。
    读完后删除临时图片(防累积)。"""
    import threading as _th
    try:
        try:
            proc = _ocr_daemon()
            if proc is not None:
                box = {}

                def work():
                    try:
                        with _OCR_LOCK:
                            proc.stdin.write((png + chr(10)).encode('utf-8'))
                            proc.stdin.flush()
                            line = proc.stdout.readline()
                        if line:
                            box['v'] = json.loads(line.decode('utf-8'))
                    except Exception as e:
                        box['e'] = e

                th = _th.Thread(target=work, daemon=True)
                th.start()
                th.join(8)
                if th.is_alive():
                    # 守护挂起: 杀掉并回退单次(防主循环卡死)
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    _OCR_DAEMON['proc'] = None
                    return _ocr_spawn(png)
                if 'e' in box or 'v' not in box:
                    _OCR_DAEMON['proc'] = None   # 守护异常, 下次重启
                    return _ocr_spawn(png)
                return box['v']
        except Exception:
            _OCR_DAEMON['proc'] = None
        return _ocr_spawn(png)
    finally:
        try:
            os.remove(png)     # 用完即删, 防 temp 目录堆积
        except Exception:
            pass


def _ocr_daemon_close():
    p = _OCR_DAEMON.get('proc')
    if p is not None:
        try:
            p.stdin.close()
            p.kill()
        except Exception:
            pass
        _OCR_DAEMON['proc'] = None


import atexit
atexit.register(_ocr_daemon_close)
_rend = None


def find_render_hwnd():
    global _rend
    user32 = ctypes.windll.user32
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    best = None
    best_area = 0

    def on_top(h, _):
        nonlocal best, best_area
        p = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(h, ctypes.byref(p))
        if p.value != PID:
            return True
        kids = []
        def on_child(ch, _2):
            cls = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(ch, cls, 128)
            kids.append((ch, cls.value))
            return True
        user32.EnumChildWindows(h, proto(on_child), 0)
        for ch, cls in kids:
            if 'Chrome_RenderWidgetHostHWND' in cls:
                r = ctypes.wintypes.RECT()
                user32.GetWindowRect(ch, ctypes.byref(r))
                area = (r.right - r.left) * (r.bottom - r.top)
                if area > best_area:
                    best_area = area
                    best = ch
        return True

    user32.EnumWindows(proto(on_top), 0)
    _rend = best
    return best


def post_click(x, y):
    """屏幕坐标无光标点击(PostMessage); 失败返回 False"""
    global _rend
    user32 = ctypes.windll.user32
    if _rend is None:
        find_render_hwnd()
    for _try in range(2):
        if _rend is not None:
            r = ctypes.wintypes.RECT()
            user32.GetWindowRect(_rend, ctypes.byref(r))
            cx, cy = int(x) - r.left, int(y) - r.top
            if 0 <= cx < r.right - r.left and 0 <= cy < r.bottom - r.top:
                lp = ((cy & 0xFFFF) << 16) | (cx & 0xFFFF)
                user32.PostMessageW(_rend, 0x0200, 0, lp)
                time.sleep(0.05)
                user32.PostMessageW(_rend, 0x0201, 0x0001, lp)
                time.sleep(0.08)
                user32.PostMessageW(_rend, 0x0202, 0, lp)
                return True
        # 渲染窗口可能已重建(重开游戏): 重找一次再试
        _rend = None
        find_render_hwnd()
    return False   # 点不到就不点(避免误点其他窗口)


def ocr_buttons(keywords=None):
    """OCR 窗口文字, 返回 [(label, 屏幕中心x, 屏幕中心y, w, h), ...]
    keywords: 文字过滤(子串匹配, 不传则全部返回)"""
    import board_reader as br
    from PIL import ImageGrab
    try:
        rect = br.window_rect(br.PID)
    except SystemExit:
        return []
    img = ImageGrab.grab(bbox=rect).convert('RGB')
    png = save_tmp(img, 'btn')
    items = ocr_items(png)
    out = []
    for it in items:
        label = it.get('text', '').strip()
        if not label:
            continue
        if keywords and not any(k in label for k in keywords):
            continue
        out.append((label, rect[0] + it.get('x', 0) + it.get('w', 0) // 2,
                    rect[1] + it.get('y', 0) + it.get('h', 0) // 2,
                    it.get('w', 0), it.get('h', 0)))
    return out


def click_label(keyword, extra=()):
    """按文字点击按钮; 找到即点, 返回 True"""
    keys = (keyword,) + tuple(extra)
    for label, cx, cy, _w, _h in ocr_buttons(keys):
        if any(k in label for k in keys):
            return post_click(cx, cy)
    return False


def click_confirm():
    """点击弹窗的 确认/确定 按钮(若有), 返回是否点中"""
    return click_label('确认', ('确定',))


AI_WORDS = ('精灵', '绝艺', '机器人', '电脑', '人机', 'AI')
PAGE_NOGO = ('复盘', '聊天', '成员', '积分', '胜率曲线', '棋谱', '大厅')


def _small_discs(a, x0, y0, x1, y1):
    """在区域里找小实心圆点(头像棋子颜色标记): 返回 [(color, cx, cy)]"""
    import numpy as np
    h, w = a.shape[:2]
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(w - 1, x1), min(h - 1, y1)
    found = []
    for y in range(y0 + 3, y1 - 3, 2):
        for x in range(x0 + 3, x1 - 3, 2):
            patch = a[y - 2:y + 3, x - 2:x + 3].reshape(-1, 3)
            lum = patch.mean()
            chroma = (patch.max(axis=1) - patch.min(axis=1)).mean()
            if lum < 95 and chroma < 60:
                c = 'black'
            elif lum > 215 and chroma < 40:
                c = 'white'
            else:
                continue
            if all((x - fx) ** 2 + (y - fy) ** 2 > 36
                   for _, fx, fy in found):
                found.append((c, x, y))
    return found


CALIB_AVATAR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               'avatar_calib.json')


def avatar_calib_boxes():
    """读 avatar_calib.json -> ((my_x0,my_y0,my_x1,my_y1),(opp...)) 屏幕坐标。
    窗口尺寸变化时按比例缩放; 无校准返回 None。"""
    if not os.path.exists(CALIB_AVATAR):
        return None
    try:
        import board_reader as br
        d = json.load(open(CALIB_AVATAR))
        rect = br.window_rect(br.PID)
        rw = rect[2] - rect[0]
        rh = rect[3] - rect[1]
        kx = rw / float(d['win_w'])
        ky = rh / float(d['win_h'])
        out = []
        for key in ('my', 'opp'):
            x0, y0, x1, y1 = d[key]
            out.append((rect[0] + int(x0 * kx), rect[1] + int(y0 * ky),
                        rect[0] + int(x1 * kx), rect[1] + int(y1 * ky)))
        return tuple(out)
    except Exception:
        return None


def classify_avatar_box(img):
    """分类角标框内的棋子: 'black'/'white'/None。
    以框边缘像素为背景基准做自适应判断(框可能压在灰面板/头像上):
    明显暗于背景的实心块=黑子; 明显亮于背景的浅色块=白子。"""
    import numpy as np
    a = np.asarray(img.convert('RGB')).astype(np.int16)
    lum = a.mean(axis=2)
    chroma = a.max(axis=2) - a.min(axis=2)
    h, w = lum.shape
    if h < 6 or w < 6:
        return None
    # 背景估计 = 框边缘 2px 的亮度中位数
    border = np.concatenate([lum[:2, :].ravel(), lum[-2:, :].ravel(),
                             lum[:, :2].ravel(), lum[:, -2:].ravel()])
    bg = float(np.median(border))
    dark = int(((lum < bg - 55) & (chroma < 50)).sum())
    bright = int((lum > bg + 18).sum())
    area = lum.size
    dark_f, bright_f = dark / area, bright / area
    if dark_f >= 0.10 and dark > bright * 2:
        return 'black'
    if bright_f >= 0.08 and bright > dark * 2:
        return 'white'
    return None


def _name_row_y(rect):
    """顶部名字行 y(窗口坐标): OCR 找含 段/级 的文本行; None=找不到"""
    try:
        from PIL import ImageGrab
        img = ImageGrab.grab(bbox=(rect[0], rect[1], rect[2],
                                   rect[1] + int((rect[3]-rect[1])*0.5)))
        img2 = img.resize((img.width*2, img.height*2), 1).convert('RGB')
        png = save_tmp(img2, 'nrow')
        ys = []
        for it in ocr_items(png):
            t = it.get('text', '')
            if ('段' in t or '级' in t) and len(t) >= 4:
                ys.append(it.get('y', 0) // 2)
        if not ys:
            return None
        ys.sort()
        return ys[len(ys)//2]
    except Exception:
        return None


def _blob_compact(crop, color):
    """校验候选色像素成紧凑圆斑(拒绝文字横条/边框伪影)"""
    import numpy as np
    a = np.asarray(crop.convert('RGB')).astype(np.int16)
    lum = a.mean(axis=2)
    chroma = a.max(axis=2) - a.min(axis=2)
    if color == 'black':
        m = (lum < 110) & (chroma < 70)
    else:
        m = lum > 230
    ys, xs = np.where(m)
    n = len(xs)
    if n < 24:
        return False
    w = int(xs.max()) - int(xs.min()) + 1
    h = int(ys.max()) - int(ys.min()) + 1
    if not (7 <= w <= 21 and 7 <= h <= 21):
        return False
    if w / h < 0.55 or w / h > 1.8:
        return False
    return n / (w * h) >= 0.4


def _scan_side_disc(img, x0, x1, y0, y1, step=5, win=28):
    """区域内滑动找棋子: 返回 (颜色, 分数, cx, cy) 最优或 None"""
    W, H = img.size
    x0, x1 = max(14, x0), min(W - win - 14, x1)
    y0, y1 = max(2, y0), min(H - win - 2, y1)
    best = None
    for y in range(y0, y1, step):
        for x in range(x0, x1, step):
            crop = img.crop((x, y, x + win, y + win))
            c = classify_avatar_box(crop)
            if c is None or not _blob_compact(crop, c):
                continue
            sc = _disc_score(crop)
            if best is None or sc > best[1]:
                best = (c, sc, x + win // 2, y + win // 2)
    return best


def avatar_indicators_dynamic(rect):
    """布局自适应: 顶带(标题下~棋盘上)全局扫描紧凑圆斑, 按左右半区分侧,
    组合选出一黑一白的一对(头像/文字伪影用紧凑校验+配对排除)。
    角标棋子随对局上下浮动较大, 本方法不依赖固定框。"""
    from PIL import ImageGrab
    import board_reader as _br
    try:
        g = _br.read_current(do_align=False, use_calib=False)
        board_top = int(g['ys'][0] - g.get('step', 27) * 0.6) if g else 250
    except Exception:
        board_top = 250
    W = rect[2] - rect[0]
    img = ImageGrab.grab(bbox=rect).convert('RGB')
    y_bot = min(img.size[1] - 16, max(120, board_top - 8))
    left, right = [], []
    for y in range(75, y_bot, 5):
        for x in range(12, W - 40, 5):
            crop = img.crop((x, y, x + 28, y + 28))
            c = classify_avatar_box(crop)
            if c is None or not _blob_compact(crop, c):
                continue
            sc = _disc_score(crop)
            hit = (c, sc, x + 14, y + 14)
            (left if x + 14 < W // 2 else right).append(hit)
    # 聚类 + 按分数排序
    def dedup(lst):
        out = []
        for h in sorted(lst, key=lambda h: -h[1]):
            if all(abs(h[2]-o[2]) > 22 or abs(h[3]-o[3]) > 22 for o in out):
                out.append(h)
            if len(out) >= 6:
                break
        return out
    L, R = dedup(left), dedup(right)
    # 头像圆: 取带内面积最大的两个(左右各一)
    import cv2
    import numpy as np
    gray = cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray[70:y_bot, :], 40, 120)
    circ = cv2.HoughCircles(edges, cv2.HOUGH_GRADIENT, dp=1.1, minDist=110,
                            param1=80, param2=24, minRadius=16, maxRadius=52)
    avs = []
    if circ is not None:
        for cx, cy, r in np.round(circ[0]).astype(int):
            avs.append((cx, cy + 70, r))
        avs.sort(key=lambda a: -(a[2] ** 2))
        avs = avs[:4]
    if len(avs) >= 2:
        # 我方角标贴我方头像右上角; 对方贴其头像左上角(官方几何)
        av_l = min(avs, key=lambda a: a[0])
        av_r = max(avs, key=lambda a: a[0])
        picks = []
        for av, corner_x, zone in ((av_l, 1, L), (av_r, -1, R)):
            ax, ay, ar = av
            cxp = ax + corner_x * int(ar * 0.72)
            cyp = ay - int(ar * 0.72)
            best = None
            for c, sc, x, y in zone:
                d = (x - cxp) ** 2 + (y - cyp) ** 2
                if d < (ar * 0.62) ** 2 and (best is None or d < best[0]):
                    best = (d, c)
            if best:
                picks.append(best[1])
        if len(picks) == 2 and picks[0] != picks[1]:
            return picks[0], picks[1]
    # 兜底: 左右各取一、颜色不同即命中(分数高者优先)
    for l in L:
        for r in R:
            if l[0] != r[0]:
                return l[0], r[0]
    return None


def _strip_my_color(rect, img=None, a=None):
    """竖条法读我方角标颜色(用户规格):
    角标只沿 y 漂移 => 在我方校准框中心 x 附近(±16, 步4)取窄竖条(宽18),
    逐行统计'近纯黑/近纯白'占比, 找最长连续段(棋子高 10-40px)。
    头像色块/文字/亮带因阈值(黑<90低彩度, 白>246低彩度, 占比>45%)
    与连续段要求而排除。返回 'black'/'white'/None。"""
    import numpy as np
    if img is None:
        from PIL import ImageGrab
        img = ImageGrab.grab(bbox=rect).convert('RGB')
    if a is None:
        a = np.asarray(img).astype(np.int16)
    try:
        myb = avatar_calib_boxes()[0]
    except Exception:
        return None
    xc = (myb[0] + myb[2]) // 2 - rect[0]
    lum = a.mean(axis=2)
    chroma = a.max(axis=2) - a.min(axis=2)
    ya = max(30, (myb[1] - rect[1]) - 60)
    yb = min(a.shape[0] - 2, (myb[3] - rect[1]) + 60)

    def col_run(x_center):
        x0 = max(2, x_center - 9)
        x1 = min(a.shape[1] - 2, x_center + 9)
        if x1 - x0 < 10:
            return None
        best = None
        cur = None
        for y in range(ya, yb):
            wl = lum[y, x0:x1]
            c2 = chroma[y, x0:x1]
            if float(((wl < 90) & (c2 < 60)).mean()) > 0.45:
                col = 'black'
            elif float(((wl > 246) & (c2 < 40)).mean()) > 0.45:
                col = 'white'
            else:
                cur = None
                continue
            cur = (col, cur[1] + 1) if cur and cur[0] == col else (col, 1)
            if 8 <= cur[1] <= 40 and (best is None or cur[1] > best[1]):
                best = cur
        return best

    # 1) 校准 x 附近 ±16(优先)
    cand = []
    for xoff in range(-16, 17, 4):
        r = col_run(xc + xoff)
        if r:
            cand.append((abs(xoff), r))
    if cand:
        cand.sort(key=lambda t: (t[0], -t[1][1]))
        return cand[0][1][0]
    # 2) 换局 x 可能整体平移: 扫左半区, 取离校准 x 最近的棋子列
    best = None
    for x in range(15, int(a.shape[1] * 0.42), 3):
        r = col_run(x)
        if r:
            d = abs(x - xc)
            if best is None or d < best[0]:
                best = (d, r)
    return best[1][0] if best else None


def avatar_indicators_vstrip(rect):
    """竖条法(我方必读; 对方读到则做异色校验):
    返回我方颜色; 对方也能读到且同色时视为冲突返回 None。"""
    my = _strip_my_color(rect)
    if my is None:
        return None
    # 对方侧同样竖条(仅用于校验)
    try:
        boxes = avatar_calib_boxes()
        oppb = boxes[1]
        from PIL import ImageGrab
        img = ImageGrab.grab(bbox=rect).convert('RGB')
        a = np.asarray(img).astype(np.int16)
        xc = (oppb[0] + oppb[2]) // 2 - rect[0]
        lum = a.mean(axis=2)
        chroma = a.max(axis=2) - a.min(axis=2)
        ya = max(30, (oppb[1] - rect[1]) - 55)
        yb = min(a.shape[0] - 2, (oppb[3] - rect[1]) + 55)
        opp = None
        for xoff in range(-16, 17, 4):
            x0 = max(2, xc + xoff - 9)
            x1 = min(a.shape[1] - 2, xc + xoff + 9)
            rows = []
            for y in range(ya, yb):
                wl = lum[y, x0:x1]
                c2 = chroma[y, x0:x1]
                if float(((wl < 90) & (c2 < 60)).mean()) > 0.45:
                    rows.append('black')
                elif float(((wl > 246) & (c2 < 40)).mean()) > 0.45:
                    rows.append('white')
            cur = None
            for col in rows:
                if cur and cur[0] == col:
                    cur = (col, cur[1] + 1)
                else:
                    cur = (col, 1)
                if 10 <= cur[1] <= 40:
                    opp = col
        if opp is not None and opp == my:
            return None      # 两侧同色: 异常, 不采信
    except Exception:
        pass
    return my


def read_avatar_indicators():
    """官方头像角标读取: 返回 (我方颜色, 对方颜色) 或 None。
    按"帧对"判定: 任一帧两框都读出且一黑一白即可采信(角标为官方绘制,
    单帧即可靠); 多帧矛盾才放弃(抗瞬时遮挡/动画帧)。"""
    from PIL import ImageGrab
    import numpy as np
    rect = None
    try:
        import board_reader as br
        rect = br.window_rect(br.PID)
    except SystemExit:
        return None
    boxes = avatar_calib_boxes()
    if boxes is None:
        return None
    pairs = []
    for _ in range(2):
        img = ImageGrab.grab(bbox=rect).convert('RGB')
        cls = []
        for idx in range(2):
            b = boxes[idx]
            # avatar_calib_boxes 给的是屏幕坐标, 截图是窗口相对 -> 换算
            x0, y0 = b[0] - rect[0], b[1] - rect[1]
            crop = img.crop((x0, y0, b[2] - rect[0], b[3] - rect[1]))
            c = classify_avatar_box(crop)
            if c is None:
                # 邻域自愈: 角标位置随对局布局漂移(可达±20px), 滑动找最强单色块
                bw, bh = b[2] - b[0], b[3] - b[1]
                best, best_score = None, 0.0
                for dy in range(-24, 25, 6):
                    for dx in range(-24, 25, 6):
                        cx0, cy0 = x0 + dx, y0 + dy
                        crop2 = img.crop((cx0, cy0, cx0 + bw, cy0 + bh))
                        sc = _disc_score(crop2)
                        c2 = classify_avatar_box(crop2)
                        if c2 and sc > best_score:
                            best, best_score = c2, sc
                if best is not None:
                    c = best
            cls.append(c)
        if cls[0] is not None and cls[1] is not None and cls[0] != cls[1]:
            pairs.append((cls[0], cls[1]))
        time.sleep(0.15)
    if not pairs:
        return None
    # 多帧一致取多数; 两帧给出矛盾结果才放弃(理论不可能, 防呆)
    if len(pairs) >= 2 and pairs[0] != pairs[1]:
        return None
    return pairs[0]


def _disc_score(img):
    """分类置信: 优势色的占比(用于邻域滑动选最优)"""
    import numpy as np
    a = np.asarray(img.convert('RGB')).astype(np.int16)
    lum = a.mean(axis=2)
    chroma = a.max(axis=2) - a.min(axis=2)
    h, w = lum.shape
    if h < 6 or w < 6:
        return 0.0
    border = np.concatenate([lum[:2, :].ravel(), lum[-2:, :].ravel(),
                             lum[:, :2].ravel(), lum[:, -2:].ravel()])
    bg = float(np.median(border))
    dark = float(((lum < bg - 55) & (chroma < 50)).sum()) / lum.size
    bright = float((lum > bg + 18).sum()) / lum.size
    return max(dark, bright)


def _avatar_centers(rect, a, y_top=60, y_bot=None):
    """顶带内找头像大圆: 返回 [(cx, cy, r)]"""
    import cv2
    import numpy as np
    if y_bot is None:
        y_bot = a.shape[0] - 20
    gray = cv2.cvtColor(np.asarray(a, dtype=np.uint8), cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray[y_top:y_bot, :], 40, 120)
    circ = cv2.HoughCircles(edges, cv2.HOUGH_GRADIENT, dp=1.1, minDist=110,
                            param1=80, param2=22, minRadius=18, maxRadius=55)
    out = []
    if circ is not None:
        for cx, cy, r in np.round(circ[0]).astype(int):
            out.append((cx, cy + y_top, int(r)))
    return out


def _my_color_by_geometry(rect):
    """几何法(用户规格): 我方棋子 = 头像在其负 x 侧(左侧)的那颗。
    扫描整条顶带找紧凑棋子段(记录中心), 与头像圆配对:
    存在 头像.cx < 棋子.x 且 距离/垂直差 合理 => 该棋子为我方, 取其颜色。
    对方棋子(头像在其右侧)自然被排除; 无头像/无棋子返回 None。"""
    import numpy as np
    from PIL import ImageGrab
    img = ImageGrab.grab(bbox=rect).convert('RGB')
    a = np.asarray(img).astype(np.int16)
    lum = a.mean(axis=2)
    chroma = a.max(axis=2) - a.min(axis=2)
    try:
        import board_reader as _br
        g = _br.read_current(do_align=False, use_calib=False)
        y_bot = min(a.shape[0] - 10,
                    int(g['ys'][0] - g.get('step', 27) * 0.6)) if g else 250
    except Exception:
        y_bot = 250
    # 1) 全宽扫列, 记录每列最强棋子段 (x, 颜色, 高, 中心y)
    cols = []
    for x in range(15, a.shape[1] - 15, 3):
        best = None
        cur = None
        y_start = 0
        for y in range(55, max(56, y_bot - 4)):
            wl = lum[y, x - 9:x + 9]
            c2 = chroma[y, x - 9:x + 9]
            if float(((wl < 90) & (c2 < 60)).mean()) > 0.45:
                col = 'black'
            elif float(((wl > 246) & (c2 < 40)).mean()) > 0.45:
                col = 'white'
            else:
                cur = None
                continue
            if cur and cur[0] == col:
                cur = (col, cur[1] + 1)
            else:
                cur = (col, 1)
                y_start = y
            if 8 <= cur[1] <= 32 and (best is None or cur[1] > best[1]):
                best = (col, cur[1], (y_start + y) // 2)
        if best:
            cols.append((x, best[0], best[1], best[2]))
    # 2) 聚类相邻列(同一颗棋子)
    stones = []
    for x, col, h, sy in cols:
        if stones and x - stones[-1][0] < 26:
            if h > stones[-1][2]:
                stones[-1] = (x, col, h, sy)
        else:
            stones.append([x, col, h, sy])
    if not stones:
        return None
    # 3) 头像圆
    avs = _avatar_centers(rect, a, y_bot=y_bot)
    if not avs:
        return None
    # 4) 我方角标在"头像右上角": 棋子相对头像 向右(dx>0)且向上(dy<0),
    #    距离约为头像半径量级(贴边); 不符合则不是我方角标(返回 None 更安全)
    for sx, col, h, sy in stones:
        for ax, ay, ar in avs:
            dx = sx - ax
            dy = sy - ay
            if dx <= 0 or dy >= 0:
                continue
            dist = (dx * dx + dy * dy) ** 0.5
            if dist > ar * 1.45 or dist < ar * 0.35:
                continue
            if abs(dx) > 110 or abs(dy) > 110:
                continue
            return col
    return None


def _my_stone_color_only(rect):
    """只识别我方执子 —— 严格按"中心竖线穿过持子"的几何:
    仅取校准中心 xc±6px 的窄竖条, 沿 y 扫描该色连续段(8-26px)。
    头像/昵称不在竖条内, 不会参与判定; 多帧投票取多数。"""
    import numpy as np
    from PIL import ImageGrab
    try:
        myb = avatar_calib_boxes()[0]
    except Exception:
        return None
    xc = (myb[0] + myb[2]) // 2 - rect[0]
    try:
        import board_reader as _br
        _g = _br.read_current(do_align=False, use_calib=False)
        _top = int(_g['ys'][0] - _g.get('step', 27) * 0.7) - 2 if _g else None
    except Exception:
        _top = None
    y_lo = max(135, (myb[1] - rect[1]) - 60)
    votes = []
    for _f in range(3):
        img = ImageGrab.grab(bbox=rect).convert('RGB')
        a = np.asarray(img).astype(np.int16)
        lum = a.mean(axis=2)
        chroma = a.max(axis=2) - a.min(axis=2)
        y_hi = min(a.shape[0] - 4,
                   max(y_lo + 60, (myb[3] - rect[1]) + 60))
        if _top is not None:
            y_hi = max(y_lo, min(y_hi, _top))
        x0 = max(0, xc - 6)
        x1 = min(a.shape[1], xc + 6)
        if x1 - x0 < 8:
            continue
        best = None
        cur = None
        y0c = 0
        for y in range(y_lo, y_hi):
            wl = lum[y, x0:x1]
            c2 = chroma[y, x0:x1]
            if float(((wl < 92) & (c2 < 60)).mean()) > 0.55:
                col = 'black'
            elif float(((wl > 238) & (c2 < 40)).mean()) > 0.50:
                col = 'white'
            else:
                cur = None
                continue
            if cur and cur[0] == col:
                cur = (col, cur[1] + 1)
            else:
                cur = (col, 1)
                y0c = y
            if 8 <= cur[1] <= 26:
                # 上下方须非同色(真棋子悬浮; 排除延长的头像色块)
                iso = True
                for py in (y0c - 6, y + 6):
                    if py < 4 or py >= a.shape[0]:
                        continue
                    w3 = lum[py, x0:x1]
                    c4 = chroma[py, x0:x1]
                    if col == 'black':
                        cont = float(((w3 < 100) & (c4 < 65)).mean()) > 0.5
                    else:
                        cont = float((w3 > 240).mean()) > 0.5
                    if cont:
                        iso = False
                        break
                if iso and (best is None or cur[1] > best[1]):
                    best = (col, cur[1])
        if best:
            votes.append(best[0])
    if not votes:
        return None
    nb = votes.count('black')
    nw = votes.count('white')
    if nb == nw:
        return votes[-1] if len(votes) >= 2 else None
    return 'black' if nb > nw else 'white'


_top_cache = [None, 0.0]   # 棋盘上沿缓存 [值, 时间戳](无网格提示时复用)



def _chip_bounds(rect, grid, H):
    """绿框竖线带几何: 返回 (x0, x1, y_lo, y_hi, top, xc)。
    x = 0.458*W ±8; y = 棋盘上沿往上 ~150px, 下限 y135 避开页面白色头部/
    明亮带(板顶过矮的短窗口才放宽)。板顶来源: 主循环网格提示 -> 5s 缓存
    -> 现读一次。"""
    import time as _t
    top = None
    try:
        if grid and 'ys' in grid and 'rect' in grid and rect:
            _dy = grid['rect'][1] - rect[1]
            top = (int(grid['ys'][0] - grid.get('step', 27) * 0.7) - 2) + _dy
        elif _t.time() - _top_cache[1] < 5.0:
            top = _top_cache[0]
        if top is None:
            import board_reader as _br
            _g = _br.read_current(do_align=False, use_calib=False)
            _dy2 = _g['rect'][1] - rect[1] if (_g and 'rect' in _g) else 0
            top = (int(_g['ys'][0] - _g.get('step', 27) * 0.7) - 2
                   + _dy2) if _g and 'ys' in _g else None
            _top_cache[0] = top
            _top_cache[1] = _t.time()
    except Exception:
        top = None
    W = rect[2] - rect[0]
    xc = int(W * 0.458)
    x0, x1 = max(0, xc - 8), min(W, xc + 8)
    if top is not None and top > 60:
        y_hi = min(H - 2, top - 2)
        y_lo = max(135, top - 150)
        if y_hi - y_lo < 12:
            y_lo = max(60, top - 150)
    else:
        # 板顶不可用: 扫窗高中上部, 保证区间非空
        y_lo, y_hi = int(H * 0.15), min(H - 2, int(H * 0.42))
    return x0, x1, y_lo, y_hi, top, xc




def _wedge_band(R, G, B, xc, Wa, Ha):
    """徽章楔形扫描: 窗口上部(ys0..min(H,280))内定位圆环主体。
    徽章固定在窗口上部(xc±45), 与棋盘路数无关——不用 chip_bounds
    推导带(9 路板顶不同, 会截掉楔形)。返回 (band, ys0, b0, b1, xa)
    或 None。"""
    import numpy as np
    orange = (R > 185) & ((R - G) > 45) & ((G - B) > 15)
    ys0 = 60
    ys1 = min(Ha, 280)
    xa, xb = max(0, xc - 45), min(Wa, xc + 45)
    band = orange[ys0:ys1, xa:xb]
    if int(band.sum()) < 20:
        return None
    rowsum = band.sum(axis=1)
    if int(rowsum.max()) < 4:
        return None
    on = rowsum >= 3
    segs = []
    s = None
    for i, v in enumerate(list(on) + [False]):
        if v and s is None:
            s = i
        elif not v and s is not None:
            segs.append((s, i))
            s = None
    merged = []
    for seg in segs:
        if merged and seg[0] - merged[-1][1] <= 4:
            merged[-1] = (merged[-1][0], seg[1])
        else:
            merged.append(list(seg))
    merged = [tuple(m) for m in merged]
    if not merged:
        return None
    b0, b1 = max(merged, key=lambda m: m[1] - m[0])
    h = b1 - b0
    if h < 18 or h > 90:
        return None
    return band, ys0, b0, b1, xa


def wedge_feature(rect, grid=None, arr=None):
    """绿框内行棋徽章橙色楔形得分(诊断/标定用):
    徽章区(窗口上部 xc±45, y60-280)定位圆环主体后取中段,
    返回 (宽列数, 橙色总量, 主体y0, 主体y1) 或 None。
    标定: 我方行棋(箭头左, 楔形入框) => 宽列>=4 且总量>=25;
          对方行棋(箭头右) => 宽列<=2 且总量小。"""
    try:
        import numpy as np
        if arr is None:
            from PIL import ImageGrab
            img = ImageGrab.grab(bbox=rect).convert('RGB')
            a = np.asarray(img).astype(np.int16)
        else:
            a = arr
        H = a.shape[0]
        W = a.shape[1]
        x0, x1, y_lo, y_hi, _t, xc = _chip_bounds(rect, grid, H)
        R = a[:, :, 0].astype(int)
        G = a[:, :, 1].astype(int)
        B = a[:, :, 2].astype(int)
        b2 = _wedge_band(R, G, B, xc, W, H)
        if b2 is None:
            return None
        _b, _ys0, b0, b1, _xa = b2
        m0 = b0 + (b1 - b0) // 3
        m1 = b0 + (b1 - b0) * 2 // 3
        midsum = _b[m0:m1].sum(axis=0)
        wide_cols = int((midsum >= 5).sum())
        mx = int(midsum.sum())
        return wide_cols, mx, _ys0 + b0, _ys0 + b1
    except Exception:
        return None


def turn_arrow_geo(rect, grid=None, arr=None):
    """窗口上部行棋徽章箭头楔形检测(官方真值, 几何模板, 无学习):
    徽章圆环 + 箭头(同橙色), 箭头指向行棋方(左=我方(头像在左),
    右=对方)。我方行棋(箭头左)时楔形块顶入圆环左缘(绿框条带内);
    对方行棋(箭头右)时楔形在右缘外, 徽章区仅弧与文字笔画。
    在窗口上部固定区(xc±45, y60-280)定位圆环主体(与棋盘路数无关),
    取中段列投影: 宽列>=4 且橙量>=25 -> 楔形在框 -> 'white'(白方行棋);
    宽列<=2 -> 仅弧 -> 'black'(黑方行棋); 中间 -> None。
    返回 'white'/'black'/None(行棋方颜色, 非 mine/opp)。"""
    try:
        import numpy as np
        if arr is None:
            from PIL import ImageGrab
            img = ImageGrab.grab(bbox=rect).convert('RGB')
            a = np.asarray(img).astype(np.int16)
        else:
            a = arr
        H = a.shape[0]
        W = a.shape[1]
        x0, x1, y_lo, y_hi, _t, xc = _chip_bounds(rect, grid, H)
        R = a[:, :, 0].astype(int)
        G = a[:, :, 1].astype(int)
        B = a[:, :, 2].astype(int)
        b2 = _wedge_band(R, G, B, xc, W, H)
        if b2 is None:
            return None
        band, ys0, b0, b1, xa = b2
        m0 = b0 + (b1 - b0) // 3
        m1 = b0 + (b1 - b0) * 2 // 3
        midsum = band[m0:m1].sum(axis=0)
        wide_cols = int((midsum >= 5).sum())
        mx = int(midsum.sum())
        if wide_cols >= 4 and mx >= 25:
            return 'white'   # 楔形 = 箭头左 = 白方行棋(真值图语义)
        if wide_cols <= 2:
            return 'black'   # 仅弧 = 箭头右 = 黑方行棋
        return None
    except Exception:
        return None


def strip_box_stats(rect, grid=None, arr=None):
    """绿框整块统计: 返回 (异占比 frac, 均值RGB) 或 None。
    frac = 偏离框背景色(通道中位数)的像素占比——色块出现在框内任意
    y 段都会推高它; 判定: 色块在=我方行棋(阈值由调用方定)。"""
    try:
        import numpy as np
        if arr is None:
            from PIL import ImageGrab
            img = ImageGrab.grab(bbox=rect).convert('RGB')
            a = np.asarray(img).astype(np.int16)
        else:
            a = arr
        H = a.shape[0]
        x0, x1, y_lo, y_hi, _t, _x = _chip_bounds(rect, grid, H)
        if x1 - x0 < 4 or y_hi - y_lo < 4:
            return None
        blk = a[y_lo:y_hi, x0:x1].reshape(-1, 3)
        mean = blk.mean(axis=0)
        bg = np.median(blk, axis=0)
        dist = np.abs(blk - bg).sum(axis=1)
        frac = float((dist > 90).mean())
        return frac, (int(round(mean[0])), int(round(mean[1])),
                      int(round(mean[2])))
    except Exception:
        return None


def strip_rgb_sig(rect, grid=None, arr=None):
    """绿框整块颜色状态字符串(供日志): '均(r,g,b) 异0.xx'"""
    st = strip_box_stats(rect, grid, arr)
    if st is None:
        return ''
    frac, mean = st
    return '均(%d,%d,%d) 异%.2f' % (mean[0], mean[1], mean[2], frac)



def _is_game_page():
    """对局页判定(宽松): 顶部出现 名字行(段/级) 或 第N手 或 行棋/行犋 即视为
    对局页。按钮/横幅 OCR 会因布局漂移漏读, 不能作为硬性条件;
    房间邀请页/首页/大厅无 段/级名字行与 第N手, 仍会被排除。"""
    try:
        items = ocr_buttons()
        txt = ''.join(l for l, *_ in items)
        return (('段' in txt or '级' in txt)
                or ('第' in txt and '手' in txt)
                or '行棋' in txt or '行犋' in txt)
    except Exception:
        return False


def avatar_my_color():
    """官方角标判我方执色: 只扫我方校准位(多帧投票), 与对方无关。
    仅在对局页(_is_game_page)执行; 失败再退回竖条/校准框配对。"""
    import board_reader as br
    try:
        rect = br.window_rect(br.PID)
    except SystemExit:
        return None
    if not _is_game_page():
        return None
    c = _my_stone_color_only(rect)
    if c is not None:
        return c
    v = avatar_indicators_vstrip(rect)
    if v is not None:
        return v
    r = read_avatar_indicators()
    if r:
        return r[0]
    return None


def detect_my_color():
    """识别我方执色 —— 只以官方头像角标颜色为准(avatar_calib.json)。
    不做任何文本/名字/圆点推断; 读不到返回 None。"""
    return avatar_my_color()


def _match_turn_items(items):
    """从横幅区 OCR items 判行棋方: 单块匹配 -> 拆块就近配对 -> None。
    常见误读归一: 臼/曰->白, 里->黑(OCR 小字易混)。"""
    norm = []
    for it in items:
        t = it.get('text', '')
        t = t.replace('臼方', '白方').replace('曰方', '白方')
        t = t.replace('里方', '黑方')
        for _q in ('犋', '其', '模', '具', '冀', '典', '莫'):
            t = t.replace('行' + _q, '行棋')
        it = dict(it)
        it['text'] = t
        norm.append(it)
    # 1) 单个文本块内完整包含指示
    for it in norm:
        t = it['text']
        if '行棋' in t:
            if '黑方' in t and '白方' not in t:
                return 'black'
            if '白方' in t and '黑方' not in t:
                return 'white'
    # 2) 颜色词与'行棋'被拆开: 按空间距离就近配对
    moves = [it for it in norm if '行棋' in it['text']]
    colors = [it for it in norm
              if ('黑方' in it['text']) != ('白方' in it['text'])]
    if moves and colors:
        for m in moves:
            mx = m['x'] + m['w'] / 2.0
            my = m['y'] + m['h'] / 2.0
            best, bd = None, 1e18
            for c in colors:
                cx2 = c['x'] + c['w'] / 2.0
                cy2 = c['y'] + c['h'] / 2.0
                d = abs(my - cy2) * 2 + abs(mx - cx2)
                if d < bd:
                    bd, best = d, c
            if best is not None:
                return 'black' if '黑方' in best['text'] else 'white'
    return None


def ocr_turn():
    """读顶部'黑方/白方行棋'横幅 -> 'black'/'white'/None(官方轮次指示)。
    横幅字号小: 先 2x 放大 OCR, 偶发漏读'行棋'时用 3x 再试一次。"""
    import board_reader as br
    from PIL import ImageGrab
    try:
        rect = br.window_rect(br.PID)
    except SystemExit:
        return None
    base = ImageGrab.grab(bbox=(rect[0], rect[1],
                                rect[2], rect[1] + int((rect[3]-rect[1])*0.5)))
    # 分级尝试: 整区2x(历史多数页面可读) -> 行棋行带裁剪3x(当前渲染最稳)
    #           -> 整区4x(兜底)。仅失败时递增, 常驻守护下每级 ~0.1-0.3s
    cands = [base.resize((base.width * 2, base.height * 2), 1)]
    bh = base.height
    band = base.crop((0, min(120, bh - 1), base.width,
                      min(270, bh))) if bh > 270 else base
    cands.append(band.resize((band.width * 3, band.height * 3), 1))
    cands.append(base.resize((base.width * 4, base.height * 4), 1))
    for img in cands:
        png = save_tmp(img, 'turn')
        r = _match_turn_items(ocr_items(png))
        if r is not None:
            return r
    return None


def ocr_board_size():
    """从顶部标题读路数(如 '9路对局') -> 9/13/19 或 None"""
    import re
    import board_reader as br
    from PIL import ImageGrab
    try:
        rect = br.window_rect(br.PID)
    except SystemExit:
        return None
    img = ImageGrab.grab(bbox=(rect[0], rect[1], rect[2],
                               rect[1] + int((rect[3]-rect[1])*0.35)))
    img = img.resize((img.width * 2, img.height * 2), 1).convert('RGB')
    png = save_tmp(img, 'size')
    items = ocr_items(png)
    for it in items:
        m = re.search(r'([0-9]+)路', it.get('text', ''))
        if m and int(m.group(1)) in (9, 13, 19):
            return int(m.group(1))
    return None


_size_cache = [None, 0.0]  # [值, 时间戳]


def ocr_board_size_cached(ttl=5.0):
    """OCR 路数(带缓存, ttl 秒内不重复 OCR)"""
    import time as _t
    now = _t.time()
    if _size_cache[0] is not None and now - _size_cache[1] < ttl:
        return _size_cache[0]
    sz = ocr_board_size()
    if sz is not None:
        _size_cache[0] = sz
        _size_cache[1] = now
    return sz


def clear_size_cache():
    _size_cache[0] = None
