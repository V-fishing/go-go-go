"""腾讯围棋窗口棋盘读取器 v2（全自动 + 星位校验 + 每帧校正）。

自动模式(find_board_in_window):
  1. 截取窗口可见区域
  2. 候选网格: 对 19/13/9 路分别做抗遮挡等差拟合
  3. 校验: 横竖步长一致(正方形) + 木色背景 + 星位指纹匹配
  4. 打分取最优 -> (xs, ys)
  5. 每帧漂移校正 refine_grid: 在预测线附近找真实线位置,
     小幅整体微调; 偏移过大自动重新定位

用法:
    python board_reader.py              # 自动模式(窗口自适应)
    python board_reader.py --png P      # 分析截图文件
    python board_reader.py --no-calib   # 忽略校准, 强制自动检测
"""
import argparse
import ctypes
import ctypes.wintypes
import json
import os

import numpy as np
from PIL import Image, ImageGrab

TOOLS = os.path.dirname(os.path.abspath(__file__))
PID = None    # WeChatAppEx 进程号会随小程序重启变化, 运行时动态解析
             # (window_rect 遇失配自动重找并刷新本值)
SCREEN_H = 1080

# 各尺寸星位(0-based 行列号): 19路9星, 13路5星, 9路天元
STAR_POINTS = {
    19: [(3, 3), (3, 9), (3, 15), (9, 3), (9, 9), (9, 15),
         (15, 3), (15, 9), (15, 15)],
    13: [(3, 3), (3, 9), (6, 6), (9, 3), (9, 9)],
    9: [(4, 4)],
}


def _proc_name(pid):
    """进程可执行文件名(小写)"""
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
    if not h:
        return ''
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = ctypes.c_ulong(512)
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value.lower()
        return ''
    finally:
        k32.CloseHandle(h)


def _host_ok(name):
    """宿主进程是否承载腾讯围棋(微信/QQ 小程序均基于 Chromium)。
    可用环境变量 ZCODE_GO_HOST 追加额外进程名(逗号分隔)。"""
    n = (name or '').lower()
    base = ('wechatappex', 'qqappex', 'qqmini', 'qq.exe')
    extra = os.environ.get('ZCODE_GO_HOST', '').lower().split(',')
    cands = base + tuple(x.strip() for x in extra if x.strip())
    return any(k in n for k in cands)


def _find_game_window():
    """动态找腾讯围棋窗口(微信/QQ 小程序的可见 Chromium 大窗口):
    返回 (pid, rect) 或 None"""
    user32 = ctypes.windll.user32
    wins = []
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def cb(hwnd, _):
        p = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if not user32.IsWindowVisible(hwnd):
            return True
        r = ctypes.wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        if r.right - r.left < 300 or r.bottom - r.top < 300:
            return True
        wins.append((p.value, (r.left, r.top, r.right, r.bottom)))
        return True

    user32.EnumWindows(proto(cb), 0)
    # 限定微信/QQ 宿主进程且取最大
    best = None
    for pid, rect in wins:
        if not _host_ok(_proc_name(pid)):
            continue
        if best is None or (rect[2]-rect[0])*(rect[3]-rect[1]) >                 (best[1][2]-best[1][0])*(best[1][3]-best[1][1]):
            best = (pid, rect)
    return best


def window_rect(pid):
    global PID
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
    if found:
        return max(found, key=lambda r: (r[2] - r[0]) * (r[3] - r[1]))
    # pid 失配(旧进程号/小程序重启): 动态重找并刷新, 之后调用走新 pid
    g = _find_game_window()
    if g is not None:
        PID = g[0]
        return g[1]
    raise SystemExit('window not found')


# ---------------- 网格线检测 ----------------

def longest_runs(mask, axis):
    n = mask.shape[0] if axis == 1 else mask.shape[1]
    out = np.zeros(n, int)
    for i in range(n):
        row = mask[i] if axis == 1 else mask[:, i]
        if not row.any():
            continue
        d = np.diff(row.astype(np.int8), prepend=0, append=0)
        s = np.where(d == 1)[0]
        e = np.where(d == -1)[0]
        if len(s):
            out[i] = (e - s).max()
    return out


def line_peaks(runlen, thr):
    out = []
    for i, v in enumerate(runlen):
        if v >= thr:
            if out and i - out[-1] < 3:
                if v > runlen[out[-1]]:
                    out[-1] = i
                continue
            out.append(i)
    return out


def fit_grid_axis(ps, k):
    """抗遮挡等差拟合: 找最吻合的 k 条等距线 -> (lines, step) 或 None"""
    ps = np.asarray(ps, float)
    if len(ps) < max(4, k // 2):
        return None
    # 用候选线相邻间距中位数锁定步长扫描范围, 排除半间距假拟合
    ds = np.diff(np.sort(ps))
    ds = ds[(ds > 5) & (ds < 130)]
    if len(ds):
        med = float(np.median(ds))
        steps = np.arange(max(14.0, med * 0.75), med * 1.4, 0.5)
        if steps.size == 0:      # 间距过小时区间为空, 回退默认范围
            steps = np.arange(20.0, 60.0, 0.5)
    else:
        steps = np.arange(20.0, 60.0, 0.5)
    best = None
    for anchor in ps:
        d0 = ps - anchor
        for step in steps:
            d = d0 / step
            n = np.round(d)
            # 只统计落在本网格内的命中(自锚点向下共 k 线):
            # 若把锚点上方/下方远处的线也算命中, 棋盘上方多出一条 UI 分隔线
            # 时, 稍偏步长的网格可"跨区间命中"全部线(含多余线), 顶掉正确拟合
            hit = int(np.sum((np.abs(d - n) < 0.28) & (n >= 0) & (n < k)))
            if best is None or hit > best[0]:
                best = (hit, anchor, step)
    if best is None:
        return None
    _, anchor, step = best
    d = (ps - anchor) / step
    n = np.round(d)
    m = np.abs(d - n) < 0.28
    ns, vals = n[m], ps[m]
    if len(ns) < max(4, k // 2):
        return None
    A = np.vstack([ns, np.ones_like(ns)]).T
    coef, *_ = np.linalg.lstsq(A, vals, rcond=None)
    step_fit, base = coef
    if not (15 < step_fit < 65):
        return None
    return base + step_fit * np.arange(k), step_fit


def _lum_at(a, cx, cy, r):
    h, w = a.shape[:2]
    pts = []
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dx * dx + dy * dy <= r * r:
                xx, yy = int(round(cx)) + dx, int(round(cy)) + dy
                if 0 <= xx < w and 0 <= yy < h:
                    pts.append(a[yy, xx])
    if not pts:
        return 255.0
    return float(np.mean(np.array(pts).mean(axis=1)))


def star_ratio(a, xs, ys):
    """空星位处中心暗点+外围木色 -> 匹配比例; 无可检星位返回 None"""
    n = len(xs)
    pts = STAR_POINTS.get(n, [])
    checked = matched = 0
    step = float(np.mean([xs[1] - xs[0], ys[1] - ys[0]]))
    for (i, j) in pts:
        cx, cy = xs[j], ys[i]
        if _lum_at(a, cx, cy, int(step * 0.30)) < 150:
            continue  # 有子压住, 跳过
        checked += 1
        center = _lum_at(a, cx, cy, max(2, int(step * 0.10)))
        ring = _lum_at(a, cx, cy, int(step * 0.32))  # 含线, 稍暗亦可
        if center < 135 and ring > 150:
            matched += 1
    return (matched / checked) if checked else None


def cell_bg_is_wood(a, xs, ys):
    n = len(xs)
    if n < 9:
        return False
    samples = []
    for i, j in [(0, 0), (n // 2, n // 2), (n - 2, n - 2), (1, n // 2),
                 (n // 2, 1)]:
        cx = int(round((xs[j] + xs[j + 1]) / 2))
        cy = int(round((ys[i] + ys[i + 1]) / 2))
        if 0 <= cx < a.shape[1] and 0 <= cy < a.shape[0]:
            samples.append(a[cy, cx])
    if len(samples) < 3:
        return False
    r, g, b = np.mean(samples, axis=0)
    return r > 180 and r > g > b and b < 200


def _line_candidates(a):
    """行长检测候选线(局部峰值): (rows, cols, dark)"""
    lum = a.mean(axis=2)
    R, G, B = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    wood = (R > 180) & (R > G) & (G > B) & (B < 200) & (lum > 150)
    bg = float(np.median(lum[wood])) if wood.any() else 192.0
    # 用棕褐色掩码找线: 棋盘线是棕色(R>G>B); 黑色/白色文字与深绿 UI 均排除
    dark = ((lum > 45) & (lum < min(185.0, bg - 15.0))
            & (R > G) & (G > B) & (B > 40) & (R < 250))
    dark[:, :2] = False
    dark[:, -2:] = False
    hgt, wid = dark.shape
    rr = longest_runs(dark, 1)
    cr = longest_runs(dark, 0)
    pos = rr[rr > 0]
    thr_r = max(25, int(np.percentile(pos, 90) * 0.6)) if len(pos) else 40
    pos = cr[cr > 0]
    thr_c = max(25, int(np.percentile(pos, 90) * 0.6)) if len(pos) else 40
    rows = np.array(line_peaks(rr, thr_r), float)
    cols = np.array(line_peaks(cr, thr_c), float)
    rows = rows[(rows > 18) & (rows < hgt - 18)]
    cols = cols[(cols > 18) & (cols < wid - 18)]
    return rows, cols, dark


def _hit_ratio(fit_vals, peaks):
    if len(fit_vals) == 0:
        return 0.0
    hits = sum(1 for v in fit_vals
               if np.any(np.abs(peaks - v) < 3.0))
    return hits / len(fit_vals)


def find_board_in_window(a, size_only=0):
    """在窗口图像中自动定位棋盘 -> (xs, ys, meta) 或 None。

    meta: {size, step, src='auto', confidence, star}
    """
    rows, cols, dark = _line_candidates(a)

    cands = []
    for k in ((size_only,) if size_only else (19, 13, 9)):
        rf = fit_grid_axis(rows, k)
        cf = fit_grid_axis(cols, k)
        if rf is None or cf is None:
            continue
        (ys, sy), (xs, sx) = rf, cf
        if abs(sy - sx) > 3.0:
            continue
        if not cell_bg_is_wood(a, xs, ys):
            continue

        def cover(fit, ps):
            return sum(1 for p in ps if np.any(np.abs(fit - p) < 3.0))

        cov = cover(ys, rows) + cover(xs, cols)
        if cov < k:
            continue
        # 越界惩罚: 拟合线明显超出候选线范围 = 外推假拟合
        oh = 0.0
        if len(rows):
            oh += max(0.0, ys[-1] - rows.max()) + max(0.0,
                                                      rows.min() - ys[0])
        if len(cols):
            oh += max(0.0, xs[-1] - cols.max()) + max(0.0,
                                                      cols.min() - xs[0])
        # 精确匹配加分: 任一轴候选数恰等于路数更可信
        exact = (len(rows) == k or len(cols) == k)
        star = star_ratio(a, xs, ys)
        star_score = star if star is not None else 0.5
        score = (cov + 6.0 * star_score + (8.0 if exact else 0.0)
                 - 0.4 * oh)
        cands.append((score, k, (np.array(xs), np.array(ys)), sy, star))
    if not cands:
        return None
    cands.sort(key=lambda c: -c[0])
    score, k, (xs, ys), step, star = cands[0]
    return xs, ys, {'size': k, 'step': step, 'src': 'auto',
                    'confidence': round(score, 1), 'star': star}


# ---------------- 星位直接定位(抗满盘) ----------------

def _strip_lines(m):
    """去掉窗口内长暗段(网格线, 贯穿窗口>=8px), 保留星点短斑"""
    out = m.copy()
    for r in range(m.shape[0]):
        row = m[r]
        d = np.diff(row.astype(np.int8), prepend=0, append=0)
        s = np.where(d == 1)[0]
        e = np.where(d == -1)[0]
        for a, b in zip(s, e):
            if b - a >= 8:
                out[r, a:b] = False
    for c in range(m.shape[1]):
        col = m[:, c]
        d = np.diff(col.astype(np.int8), prepend=0, append=0)
        s = np.where(d == 1)[0]
        e = np.where(d == -1)[0]
        for a, b in zip(s, e):
            if b - a >= 8:
                out[a:b, c] = False
    return out


def find_star_dots(a, bg_lum):
    """找星位小点: 剥离网格线后的孤立近圆暗斑 + 外围木色环。
    星位恰在横竖线交叉点上, 必须先剔除线像素再做形状判断。
    """
    lum = a.mean(axis=2)
    R, G, B = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    wood = ((R > 180) & (R > G) & (G > B) & (B < 200) & (lum > 150))
    dark = lum < bg_lum - 35
    h, w = dark.shape
    ys, xs = np.where(dark)
    cand = []
    for x, y in zip(xs.tolist(), ys.tolist()):
        if x < 13 or y < 13 or x >= w - 13 or y >= h - 13:
            continue
        # 快速预筛: 中心 5x5 够暗且 4 个对角方位半径~9 是木色
        # (注意: 星位在线的交叉点上, 上下左右会打到网格线, 必须取对角)
        c5 = dark[y - 2:y + 3, x - 2:x + 3]
        if c5.sum() < 4:
            continue
        if not (wood[y - 9, x - 9] and wood[y - 9, x + 9]
                and wood[y + 9, x - 9] and wood[y + 9, x + 9]):
            continue
        blob = _strip_lines(dark[y - 6:y + 7, x - 6:x + 7])
        core = blob[3:10, 3:10]
        if core.sum() < 4:
            continue
        dy, dx = np.where(core)
        bh = dy.max() - dy.min() + 1
        bw = dx.max() - dx.min() + 1
        if not (3 <= bh <= 9 and 3 <= bw <= 9):
            continue
        if max(bh, bw) / min(bh, bw) > 1.9:
            continue
        if core.sum() / (bh * bw) < 0.3:
            continue
        # 外圈(半径8~12)木色占优(线贡献已计入, 阈值放宽)
        ring = wood[y - 12:y + 13, x - 12:x + 13].copy()
        ring[5:-5, 5:-5] = False
        if ring.sum() < 0.45 * ring.size:
            continue
        cand.append((x, y))
    out = []
    for c in cand:
        if all((c[0] - o[0]) ** 2 + (c[1] - o[1]) ** 2 > 36 for o in out):
            out.append(c)
    return out


def _grid_from_origin(a, k, ox, oy, s):
    xs = ox + np.arange(k) * s
    ys = oy + np.arange(k) * s
    if not cell_bg_is_wood(a, xs, ys):
        return None
    return xs, ys


def _line_coverage(a, xs, ys):
    """预测线位置上的暗像素占比(校验网格是否贴合真实棋盘线)"""
    lum = a.mean(axis=2)
    R, G, B = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    wood = (R > 180) & (R > G) & (G > B) & (B < 200) & (lum > 150)
    bg = float(np.median(lum[wood])) if wood.any() else 192.0
    dark = lum < min(175.0, bg - 22.0)
    h, w = dark.shape
    x0 = max(0, int(xs[0]) - 2)
    x1 = min(w, int(xs[-1]) + 2)
    y0 = max(0, int(ys[0]) - 2)
    y1 = min(h, int(ys[-1]) + 2)
    if x1 - x0 < 30 or y1 - y0 < 30:
        return 0.0
    vals = []
    for v in ys:
        yy = int(round(v))
        if y0 <= yy < y1:
            vals.append(dark[yy, x0:x1].mean())
    for v in xs:
        xx = int(round(v))
        if x0 <= xx < x1:
            vals.append(dark[y0:y1, xx].mean())
    return float(np.mean(vals)) if vals else 0.0


def find_board_by_stars(a, bg_lum, size_only=0):
    """用星位直接推网格(19/13 路); 失败返回 None -> (xs, ys, meta)。

    对每个(检测点对 × 理论星位对)枚举假设: 两点距离须吻合星位间距,
    由此推出 原点+步长, 建网格后校验:
      线命中率(网格线须是行长检测峰值) + 木色 + 星位匹配。
    """
    dots = find_star_dots(a, bg_lum)
    if len(dots) < 3:
        return None
    row_peaks, col_peaks, _ = _line_candidates(a)
    best = None
    cands = []
    for k, stars in STAR_POINTS.items():
        pairs = []
        for i in range(len(stars)):
            for j in range(i + 1, len(stars)):
                (r1, c1), (r2, c2) = stars[i], stars[j]
                dr, dc = r2 - r1, c2 - c1
                if dr == 0 or dc == 0:  # 同行或同列
                    pairs.append(((r1, c1), (r2, c2)))
        for (x1, y1) in dots:
            for (x2, y2) in dots:
                if (x2, y2) <= (x1, y1):
                    continue
                dx, dy = x2 - x1, y2 - y1
                for (s1, s2) in pairs:
                    (r1, c1), (r2, c2) = s1, s2
                    dr, dc = r2 - r1, c2 - c1
                    if dc != 0 and dr == 0:      # 同行星对
                        s = dx / dc
                        if abs(dy) > 6 or not (12 < s < 70):
                            continue
                    elif dr != 0 and dc == 0:    # 同列星对
                        s = dy / dr
                        if abs(dx) > 6 or not (12 < s < 70):
                            continue
                    else:                        # 斜对(一般不用)
                        continue
                    ox = x1 - c1 * s
                    oy = y1 - r1 * s
                    g = _grid_from_origin(a, k, ox, oy, s)
                    if g is None:
                        continue
                    xs, ys = g
                    if (xs[0] < -15 or ys[0] < -15
                            or xs[-1] > a.shape[1] + 15
                            or ys[-1] > a.shape[0] + 15):
                        continue
                    hr = (_hit_ratio(ys, row_peaks)
                          + _hit_ratio(xs, col_peaks)) / 2
                    if hr < 0.55:
                        continue  # 线命中率不足 -> 假网格
                    star = star_ratio(a, xs, ys)
                    sc = hr * 10 + (star if star is not None else 0)
                    best = (sc, k, (xs, ys), s, hr, star)
                    cands.append(best)
    # 子网格消除: 同一步长且小网格的线几乎全在大网格上 -> 丢弃小的
    keep = []
    for c in cands:
        drop = False
        for d in cands:
            if c is d:
                continue
            kc, kd = c[1], d[1]
            if kd <= kc or abs(c[3] - d[3]) > 1.2:
                continue
            xsc, ysc = c[2]
            xsd, ysd = d[2]
            if (sum(1 for v in xsc if np.any(np.abs(xsd - v) < 3)) / kc
                    >= 0.9 and
                    sum(1 for v in ysc if np.any(np.abs(ysd - v) < 3)) / kc
                    >= 0.9):
                drop = True
                break
        if not drop:
            keep.append(c)
    best = max(keep, key=lambda t: t[0]) if keep else None
    if best is None:
        return None
    sc, k, (xs, ys), s, hr, star = best
    if hr < 0.55:
        return None
    return xs, ys, {'size': k, 'step': s, 'src': 'auto-star',
                    'confidence': round(sc, 2), 'star': star}


def locate_board(a, size_only=0):
    """窗口图像 -> (xs, ys, meta): 先线检测, 失败转星位定位"""
    found = find_board_in_window(a, size_only=size_only)
    if found is not None:
        return found
    lum = a.mean(axis=2)
    R, G, B = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    wood = (R > 180) & (R > G) & (G > B) & (B < 200) & (lum > 150)
    bg = float(np.median(lum[wood])) if wood.any() else 192.0
    return find_board_by_stars(a, bg, size_only=size_only)

def _line_color_mask(a):
    lum = a.mean(axis=2)
    r, g, b = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    # 棋盘线: 棕褐色细线(非纯黑子/非亮白子)
    return ((lum > 55) & (lum < 185) & (r > g) & (g > b) & (b > 55)
            & (r < 250)).astype(np.int8)


def align_by_variance(a, xs, ys, span=10):
    """网格微对齐: 找使交叉点采样亮度方差最大的 (dx,dy)。

    网格正对棋子时每个交叉点采样最纯粹(黑/白/木, 方差大);
    偏移时混入相邻子/线, 采样值趋中, 方差小。
    """
    n = len(xs)
    step = float(np.mean([xs[1] - xs[0], ys[1] - ys[0]]))
    r = max(4, min(30, int(step * 0.28)))
    lum = a.mean(axis=2).astype(np.float64)
    h, w = lum.shape
    Xg = np.round(np.asarray(xs))[None, :] + np.zeros((n, n))
    Yg = np.round(np.asarray(ys))[:, None] + np.zeros((n, n))
    offs = np.array([(dy, dx) for dy in range(-r, r + 1)
                     for dx in range(-r, r + 1)
                     if dx * dx + dy * dy <= r * r])
    ody = offs[:, 0]
    odx = offs[:, 1]
    best = None
    for dy in range(-span, span + 1):
        for dx in range(-span, span + 1):
            yy = (Yg + dy)[..., None] + ody[None, None, :]
            xx = (Xg + dx)[..., None] + odx[None, None, :]
            ok = ((yy >= 0) & (yy < h) & (xx >= 0) & (xx < w))
            if not ok.all():
                continue
            yy = yy.clip(0, h - 1).astype(np.int64)
            xx = xx.clip(0, w - 1).astype(np.int64)
            vals = lum[yy, xx].mean(axis=2)
            v = float(vals.var())
            if best is None or v > best[0]:
                best = (v, dx, dy)
    if best is None:
        return xs, ys  # 全部偏移越界(网格贴边/超窗), 不做对齐
    _, dx, dy = best
    if best[0] < 1200:
        return xs, ys  # 盘面太疏(如空盘), 方差无区分度, 不做对齐
    return xs + dx, ys + dy


def coarse_align(a, xs, ys, span=12):
    """整体偏移粗对齐: 在 ±span 内找使线位置暗像素最多的 (dx, dy)"""
    lum = a.mean(axis=2)
    R, G, B = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    wood = (R > 180) & (R > G) & (G > B) & (B < 200) & (lum > 150)
    bg = float(np.median(lum[wood])) if wood.any() else 192.0
    dark = lum < min(175.0, bg - 22.0)
    h, w = dark.shape
    x0, x1 = max(0, int(xs[0]) - 3), min(w - 1, int(xs[-1]) + 3)
    y0, y1 = max(0, int(ys[0]) - 3), min(h - 1, int(ys[-1]) + 3)
    if x1 - x0 < 60 or y1 - y0 < 60:
        return xs, ys
    # 每条线取最多 48 个采样点
    xs_s = np.linspace(x0, x1, 48).astype(int)
    ys_s = np.linspace(y0, y1, 48).astype(int)
    best = None
    for dy in range(-span, span + 1):
        for dx in range(-span, span + 1):
            # 横向线采样: 行 y+dy 上 x 采样点
            rows_ok = 0
            yy = np.round(ys + dy).astype(int)
            yy = yy[(yy >= 1) & (yy < h - 1)]
            if len(yy):
                rows_ok = int(dark[np.ix_(yy, xs_s)].sum())
            xx = np.round(xs + dx).astype(int)
            xx = xx[(xx >= 1) & (xx < w - 1)]
            cols_ok = int(dark[np.ix_(ys_s, xx)].sum()) if len(xx) else 0
            sc = rows_ok + cols_ok
            if best is None or sc > best[0]:
                best = (sc, dx, dy)
    _, dx, dy = best
    return xs + dx, ys + dy


def refine_grid_safe(a, xs, ys, tol=4):
    """refine_grid 容错版: 异常帧直接返回原网格(读盘继续, 不崩溃)"""
    try:
        return refine_grid(a, xs, ys, tol=tol)
    except Exception:
        return xs, ys, 999.0   # 大 drift 使调用方知道本次细化失败


def refine_grid(a, xs, ys, tol=4):
    """在预测线附近±tol 找真实线位置, 小幅校正; 返回 (xs, ys, drift)"""
    lc = _line_color_mask(a)
    hgt, wid = lc.shape
    n = len(xs)
    x0i = max(0, int(xs[0]) - 3)
    x1i = min(wid, int(xs[-1]) + 3)
    y0i = max(0, int(ys[0]) - 3)
    y1i = min(hgt, int(ys[-1]) + 3)
    if x1i - x0i < 50 or y1i - y0i < 50:
        return xs, ys, 0.0
    band = lc[y0i:y1i, x0i:x1i]

    def refine_axis(fit_vals, band_axis, span0, span1):
        out = []
        for v in fit_vals:
            lo = max(0, int(v) - tol - span0)
            hi = min(band_axis, int(v) + tol + span0 + 1)
            if hi - lo < 5:
                out.append(v)
                continue
            if span0 == 0:  # 水平线: 每行统计 band 中的线色占比
                prof = band[lo:hi, :].mean(axis=1)
            else:
                prof = band[:, lo:hi].mean(axis=0)
            if prof.size == 0:      # 异常帧下 refine 区域退化: 保留原值
                out.append(v)
                continue
            idx = int(np.argmax(prof))
            best = lo + idx
            if prof[idx] > 0.05 and abs(best - v) <= tol:
                out.append(float(best))
            else:
                out.append(v)
        return np.array(out)

    ys2 = refine_axis(ys, y1i - y0i, 0, 0)
    xs2 = refine_axis(xs, x1i - x0i, 0, 0)
    # 用最小二乘把校正后的线拟合回等距序列(抗个别误检), 限制修正幅度
    def snap(fit):
        idx = np.arange(len(fit))
        A = np.vstack([idx, np.ones_like(idx)]).T
        coef, *_ = np.linalg.lstsq(A, fit, rcond=None)
        lined = coef[0] * idx + coef[1]
        resid = np.abs(lined - fit)
        good = resid < 2.5
        if good.sum() >= max(6, len(fit) // 2):
            A2 = np.vstack([idx[good], np.ones(good.sum())]).T
            coef, *_ = np.linalg.lstsq(A2, fit[good], rcond=None)
            return coef[0] * idx + coef[1], float(np.abs(fit - (coef[0] * idx + coef[1])).mean())
        return fit, float(resid.mean())

    xs3, dx = snap(xs2)
    ys3, dy = snap(ys2)
    drift = max(dx, dy)
    return xs3, ys3, drift


# ---------------- 读盘 ----------------

def classify_px(px):
    """对采样盘像素投票: 返回 'X'/'O'/'.'
    白像素: 亮(>165)且近中性(色度<60); 黑像素: 暗(<105)且近中性(色度<30)。
    黑子必须中性: 棋盘木色(R238 G201 B137, 色度~100)即使被提示遮罩暗化到
    亮度<105, 色度仍有 ~50, 不会误判成黑子(倒计时遮罩假黑子根除)。
    互斥强门槛: 黑须 暗>0.45 且 白<0.25; 白须 白>0.35 且 暗<0.25。
    白子边缘阴影/网格微偏导致的"半黑半白"判 '.'(宁空勿错, 下轮再读)。"""
    lum = px.mean(axis=1)
    chroma = px.max(axis=1) - px.min(axis=1)
    wf = float(((lum > 165) & (chroma < 60)).mean())
    bf = float(((lum < 105) & (chroma < 30)).mean())
    if bf > 0.45 and wf < 0.30:
        return 'X'
    if wf > 0.35 and bf < 0.30:
        return 'O'
    return '.'


_prev_board = None
_flog = [0.0]   # 闪动诊断打印节流


def _flash_check(a, xs, ys, stone_r, board):
    """诊断: 上一轮还是棋子、本轮消失的交点(UI 棋子闪进/闪出的数据源),
    打印该点 整盘/中心核/右下象限 的暗亮占比, 定位是什么装饰在干扰。
    每 2s 至多记一个消失点(提子动画也会触发, 属正常)。"""
    global _prev_board, _flog
    pb, _prev_board = _prev_board, board
    if pb is None or len(pb) != len(board):
        return
    if pb == board:
        return
    import time as _t
    now = _t.time()
    if now - _flog[0] < 2:
        return
    n = len(board)
    for i in range(n):
        for j in range(n):
            if pb[i][j] in 'XO' and board[i][j] == '.':
                cx, cy = int(round(xs[j])), int(round(ys[i]))
                lum = a.mean(axis=2)
                chm = a.max(axis=2) - a.min(axis=2)
                dk = lum < 105
                wh = (lum > 165) & (chm < 60)

                def fracs(dx0, dx1, dy0, dy1, r2):
                    tot = nb = nw = 0
                    for dy in range(dy0, dy1):
                        for dx in range(dx0, dx1):
                            if dx * dx + dy * dy <= r2:
                                tot += 1
                                if dk[cy + dy, cx + dx]:
                                    nb += 1
                                if wh[cy + dy, cx + dx]:
                                    nw += 1
                    return (nb / tot, nw / tot) if tot else (0.0, 0.0)

                r = stone_r
                db, dw = fracs(-r, r + 1, -r, r + 1, r * r)
                cb, cw = fracs(-r, r + 1, -r, r + 1, (0.45 * r) ** 2)
                qb, qw = fracs(1, r + 1, 1, r + 1, r * r)
                print('[读盘闪动] r%d c%d 原为%s: '
                      '暗 整%.2f/核%.2f/右下%.2f | '
                      '亮 整%.2f/核%.2f/右下%.2f' % (
                          i, j, pb[i][j],
                          db, cb, qb, dw, cw, qw))
                _flog[0] = now
                return


_QD_CACHE = {}   # stone_r -> (全盘偏移, 内核偏移, 环带偏移)


def _quarter_disk(stone_r):
    """左上四分之一圆盘采样模板(含 内核对 与 外环带):
    全盘: 半径 stone_r, 左上象限(最后一手标记在右下, 永不入样);
    内核: 0.45r(棋子中心/方块中心/星位点均含);
    环带: 0.55r~0.95r(真圆棋子半径>9px 覆盖环带; 悬停预览方块仅
          ~5px 半径 → 环带为木色)。环带是"圆(真子) vs 方(预览块)"
          形状直读判据, 用户真值图实测: 子=18-23px 圆, 方块=10-12px。"""
    if stone_r in _QD_CACHE:
        return _QD_CACHE[stone_r]
    offs, cores, ring = [], [], []
    r2 = stone_r * stone_r
    r2c = int((stone_r * 0.45) ** 2)
    r2a = int((stone_r * 0.55) ** 2)
    r2b = int((stone_r * 0.95) ** 2)
    for dy in range(-stone_r, 1):
        for dx in range(-stone_r, 1):
            dd = dy * dy + dx * dx
            if dd <= r2:
                offs.append((dy, dx))
                if dd <= r2c:
                    cores.append((dy, dx))
                elif r2a <= dd <= r2b:
                    ring.append((dy, dx))
    _QD_CACHE[stone_r] = (offs, cores, ring)
    return _QD_CACHE[stone_r]


def read_board(a, xs, ys, stone_r):
    """向量化读盘: 整盘一次 numpy 批量取点/统计, 开销 ~ms 级。
    真子判定附加环带测试: 采样外环须同为子色(圆棋子到边缘 8-9px),
    悬停预览方块(10-12px, 半径~5px)外环为木色 -> 不被认作棋子。
    """
    n = len(xs)
    H, W = a.shape[:2]
    offs = _quarter_disk(stone_r)
    if not offs or len(offs) < 2:
        return ['.' * n for _ in range(n)]
    offs_all, offs_core, offs_ring = offs

    def stats(offlist):
        dy_o = np.array([o[0] for o in offlist], int)
        dx_o = np.array([o[1] for o in offlist], int)
        CYY, CXX = np.meshgrid(np.round(ys).astype(int),
                               np.round(xs).astype(int), indexing='ij')
        R = CYY[:, :, None] + dy_o[None, None, :]
        C = CXX[:, :, None] + dx_o[None, None, :]
        valid = (R >= 0) & (R < H) & (C >= 0) & (C < W)
        nv = valid.sum(axis=2)
        px = a[np.clip(R, 0, H - 1), np.clip(C, 0, W - 1)]
        lum = px.mean(axis=3)
        chm = px.max(axis=3) - px.min(axis=3)
        wf = ((lum > 165) & (chm < 60) & valid).sum(axis=2) / np.maximum(nv, 1)
        # 黑子须中性(色度<30): 木色即使被遮罩暗化仍带色度, 不会误判成黑子
        bf = ((lum < 105) & (chm < 30) & valid).sum(axis=2) / np.maximum(nv, 1)
        return bf, wf, nv

    bf, wf, nv = stats(offs_all)
    X = (bf > 0.45) & (wf < 0.30)
    O = (wf > 0.35) & (bf < 0.30)
    # 形状直读(尺寸): 过交叉点水平线上 同色连续"宽度":
    #   真圆棋子直径 18-20px(宽>=15); 悬停预览方块 12px(宽<15);
    #   星位小点 4-6px。宽 <15 的判 X/O 候选一律视为方块/星点。
    # 用户真值图实测: 子=18-23px 圆, 预览方块=10-12px 方。
    step = float(np.mean([xs[1] - xs[0], ys[1] - ys[0]]))
    hw = int(min(W - 1, max(6, step * 0.52)))
    iy = np.round(np.asarray(ys, float)).astype(int)
    ix = np.round(np.asarray(xs, float)).astype(int)
    R2 = a[:, :, 0].astype(int)
    G2 = a[:, :, 1].astype(int)
    B2 = a[:, :, 2].astype(int)
    lum2 = a.mean(axis=2)
    chm2 = a.max(axis=2) - a.min(axis=2)
    blk = (lum2 < 105) & (chm2 < 30)
    wht = (lum2 > 165) & (chm2 < 60)
    runw = np.zeros((n, n), int)
    for i in range(n):
        yy = iy[i]
        lr = lum2[yy]
        bc = blk[yy]
        wc = wht[yy]
        for j in range(n):
            xx = ix[j]
            xl, xr_ = max(0, xx - hw), min(W, xx + hw + 1)
            if not bc[xl:xr_].any() and not wc[xl:xr_].any():
                continue
            # 从中心向左右扩展连续同色段
            tgt = 'b' if bc[xx] else ('w' if wc[xx] else None)
            if tgt is None:
                continue
            msk = bc if tgt == 'b' else wc
            L = 0
            while xx - L > xl and msk[xx - L - 1]:
                L += 1
            Rr = 0
            while xx + Rr < xr_ - 1 and msk[xx + Rr + 1]:
                Rr += 1
            runw[i, j] = L + Rr + 1
    small = runw > 0
    X = X & (~small | (runw >= 15))
    O = O & (~small | (runw >= 15))
    grid = np.where(nv < 8, '?', np.where(X, 'X', np.where(O, 'O', '.')))
    board = [''.join(r) for r in grid.tolist()]
    _flash_check(a, xs, ys, stone_r, board)
    return board


def load_calib():
    p = os.path.join(TOOLS, 'grid_calib.json')
    if not os.path.exists(p):
        return None, None
    d = json.load(open(p))
    return d.get('mode', 'window'), d


def calib_grid_screen(d):
    """屏幕坐标校准(含窗口位移跟随); 尺寸与校准时不同则返回 None"""
    wr = window_rect(PID)
    if 'win_w' in d and (wr[2] - wr[0] != d['win_w']
                         or wr[3] - wr[1] != d['win_h']):
        return None
    n = d['size']
    dx = wr[0] - d.get('win_x0', wr[0])
    dy = wr[1] - d.get('win_y0', wr[1])
    x0, x1 = d['x0'] + dx, d['x1'] + dx
    y0, y1 = d['y0'] + dy, d['y1'] + dy
    xs = [x0 + (x1 - x0) * j / (n - 1) for j in range(n)]
    ys = [y0 + (y1 - y0) * i / (n - 1) for i in range(n)]
    return xs, ys


def board_visible(a, xs, ys):
    """棋盘区域可见性门禁: 采样 6 个格心(含四边/底部), 须为木色基调。
    弹窗/结算页/远程画面撕裂导致棋盘被盖时返回 False, 整帧丢弃。"""
    n = len(xs)
    if n < 9:
        return True
    pts = [(2, 2), (n - 3, 2), (2, n - 3), (n - 3, n - 3),
           (n // 2, 2), (n // 2, n - 3)]
    ok = 0
    for i, j in pts:
        cx = int(round((xs[j] + xs[j + 1]) / 2))
        cy = int(round((ys[i] + ys[i + 1]) / 2))
        if not (0 <= cx < a.shape[1] and 0 <= cy < a.shape[0]):
            continue
        r, g, b = a[cy, cx]
        if r > 150 and r > g > b and b < 220:
            ok += 1
    return ok >= 4


_chk_cnt = 0
_size_hint = 0
_grid_cache = {}  # (winW,winH) -> [(xs,ys,n,src), ...] 最多2份
_align_cache = {}   # (winW, vis_h, n) -> (已对齐 xs, ys): 自适应对齐缓存
_align_cnt = 0      # 对齐帧计数(每 25 次强制重对齐一次, 防慢漂移)


def clear_grid_cache():
    _grid_cache.clear()
    _align_cache.clear()


def read_current(use_calib=False, force_auto=False, do_align=True,
                 force_size=0):
    """读当前窗口棋局: 截屏后交给 read_img 处理。

    网格来源优先级: 校准(尺寸匹配) -> 全自动定位 -> 失败返回 None。
    窗口改尺寸时校准自动失效并转全自动; 每帧做漂移校正。
    force_size=9/13/19: 显式指定路数, 跳过校准, 强制按该尺寸自动检测。
    返回 dict 或 None。
    """
    rect = window_rect(PID)
    vis_h = min(rect[3] - rect[1], SCREEN_H - rect[1])
    img = ImageGrab.grab(bbox=(rect[0], rect[1],
                               rect[2], rect[1] + vis_h)).convert('RGB')
    a = np.asarray(img).astype(np.int16)
    res = read_img(a, rect, vis_h, use_calib=use_calib,
                   force_auto=force_auto, do_align=do_align,
                   force_size=force_size)
    if res is not None:
        res['img'] = img
    return res


def read_img(a, rect, vis_h, use_calib=False, force_auto=False,
             do_align=True, force_size=0):
    """对给定窗口帧(a: 窗口相对坐标的 RGB 数组)做完整读盘
    (线路检测/尺寸选择/逐点分类)。与窗口截屏解耦, 供离线回放。
    返回 dict(含 n/board/xs/ys/rect/src/step/stone_r/drift)或 None。
    """
    xs = ys = None
    src = ''
    if force_size:
        found = locate_board(a, size_only=force_size)
        if found is not None:
            xs, ys, _meta = found
            src = 'auto-size'
    elif not force_auto and use_calib:
        mode_calib, calib = load_calib()
        if mode_calib == 'screen' and calib is not None:
            g = calib_grid_screen(calib)  # 尺寸不匹配时返回 None
            if g is not None:
                xs, ys = g
                src = 'calib'
    if xs is None and not force_size:
        found = locate_board(a)
        if found is not None:
            xs, ys, _meta = found
            src = 'auto'
    if xs is None:
        # 密集盘面线检测易失败: 复用同窗口尺寸下本局最近成功的网格
        key = (rect[2] - rect[0], vis_h)
        entries = _grid_cache.get(key, [])
        best = None
        for (cx, cy, _n, _s) in entries:
            fx, fy, dr = refine_grid(a, np.array(cx), np.array(cy))
            if dr <= 3.5 and board_visible(a, fx, fy):
                if best is None or dr < best[0]:
                    best = (dr, fx, fy)
        if best is not None:
            xs, ys = best[1], best[2]
            src = 'grid-cache'
        else:
            return None
    xs = np.asarray(xs, float)
    ys = np.asarray(ys, float)
    if src == 'calib':
        # 校准存的是屏幕坐标, 统一换算成窗口相对坐标
        xs = xs - rect[0]
        ys = ys - rect[1]
        # 尺寸交叉校验: 每 25 次读盘自动检测一次, 若实际棋盘路数与
        # 校准不符(同窗口下开了 9/13 路棋)则改用自动网格
        global _chk_cnt, _size_hint
        _chk_cnt += 1
        if _chk_cnt % 25 == 0:
            det = find_board_in_window(a)
            if det is not None and len(det[0]) != len(xs):
                # 尺寸切换需连续两次一致, 防脏帧误判
                if _size_hint == len(det[0]):
                    xs, ys, _m = det
                    src = 'auto-cross'
                    print('board_reader: 检测到棋盘尺寸变化, 改用自动网格 '
                          f'(n={len(xs)})')
                else:
                    _size_hint = len(det[0])
            else:
                _size_hint = 0
    _pre_xy = (np.asarray(xs, float), np.asarray(ys, float))  # 对齐前网格
    if do_align:
        # 自适应微对齐: align_by_variance 开销大(~280ms)。当前检测网格与
        # 上次对齐结果一致(均差<=2px)且未到强制周期时直接复用已对齐网格,
        # 跳过高价对齐(窗口静止时对齐结果本就比逐帧检测更准);
        # 位移超限(窗口拖动/换局/画面切换)立即重对齐;
        # 每 250 次强制重对齐一次, 防亚像素级慢漂移累积。
        # 防周期性闪烁: 重对齐结果与缓存网格相差 <=1px 时保留缓存——
        # 方差对齐会在 ±1px 局部最优间跳动, 会让临界棋子分类周期性翻转
        global _align_cnt
        _align_cnt += 1
        _ak = (rect[2] - rect[0], vis_h, len(xs))
        _ent = _align_cache.get(_ak)
        _reuse = False
        if _ent is not None and len(_ent[0]) == len(xs):
            _dx = float(np.mean(np.abs(np.asarray(xs, float) - _ent[0])))
            _dy = float(np.mean(np.abs(np.asarray(ys, float) - _ent[1])))
            _reuse = (_dx + _dy) / 2 <= 2.0
        if _reuse and _align_cnt % 250 != 0:
            xs, ys = _ent[0], _ent[1]   # 复用上次对齐结果
        else:
            _xs2, _ys2 = align_by_variance(a, xs, ys)
            if _ent is not None and len(_ent[0]) == len(_xs2):
                _dx2 = float(np.mean(np.abs(np.asarray(_xs2, float)
                                            - _ent[0])))
                _dy2 = float(np.mean(np.abs(np.asarray(_ys2, float)
                                            - _ent[1])))
                if (_dx2 + _dy2) / 2 <= 1.0:
                    xs, ys = _ent[0], _ent[1]   # 微跳: 保留缓存网格
                else:
                    xs, ys = _xs2, _ys2
                    _align_cache[_ak] = (np.array(xs, float),
                                         np.array(ys, float))
            else:
                xs, ys = _xs2, _ys2
                _align_cache[_ak] = (np.array(xs, float),
                                     np.array(ys, float))

    xs, ys, drift = refine_grid(a, xs, ys)
    if not board_visible(a, xs, ys):
        # 对齐把网格推到不可见位置(密集盘面下方差对齐偶发偏 ~4px, 导致
        # 整读失败 = UI 周期性闪烁/play 周期掉读): 回退到对齐前网格
        _px, _py = _pre_xy
        _xs2, _ys2, _dr2 = refine_grid(a, _px, _py)
        if board_visible(a, _xs2, _ys2):
            xs, ys, drift = _xs2, _ys2, _dr2
            if do_align:
                _align_cache.pop(_ak, None)   # 不缓存坏对齐结果
        else:
            return None  # 棋盘被盖/画面撕裂 -> 整帧丢弃
    n = len(xs)
    step = float(np.mean([xs[1] - xs[0], ys[1] - ys[0]]))
    stone_r = max(6, min(40, int(step * 0.32)))
    board = read_board(a, xs, ys, stone_r)
    key = (rect[2] - rect[0], vis_h)
    entry = (np.array(xs), np.array(ys), n, src)
    entries = [e for e in _grid_cache.get(key, [])
               if not (len(e[0]) == len(xs)
                       and abs(e[0][0] - xs[0]) < 2
                       and abs(e[1][0] - ys[0]) < 2)]
    entries.append(entry)
    _grid_cache[key] = entries[-2:]
    return {'n': n, 'board': board, 'xs': xs, 'ys': ys, 'src': src,
            'step': step, 'stone_r': stone_r, 'rect': rect,
            'drift': drift}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--png', default=None)
    ap.add_argument('--no-calib', action='store_true',
                    help='强制全自动检测(忽略校准)')
    args = ap.parse_args()

    if args.png:
        img = Image.open(args.png).convert('RGB')
        a = np.asarray(img).astype(np.int16)
        found = locate_board(a)
        if found is None:
            print('NO BOARD FOUND')
            return
        xs, ys, meta = found
        xs, ys, drift = refine_grid_safe(a, xs, ys)
        src = 'auto(png)'
        rect = (0, 0, 0, 0)
    else:
        res = read_current(use_calib=not args.no_calib,
                           force_auto=args.no_calib)
        if res is None:
            print('NO BOARD FOUND - 未在窗口内找到棋盘')
            return
        n, board = res['n'], res['board']
        xs, ys = res['xs'], res['ys']
        src, rect = res['src'], res['rect']
        print(f'grid source: {src} | drift={res.get("drift",0):.1f}px')
        print(f'board size: {n}x{n}   black='
              f'{sum(r.count("X") for r in board)}  white='
              f'{sum(r.count("O") for r in board)}')
        letters = 'ABCDEFGHJKLMNOPQRST'[:n]
        print('    ' + ' '.join(letters))
        for i in range(n):
            print(f'{n - i:2d}  ' + ' '.join(board[i]))
        print('    ' + ' '.join(letters))
        return

    # png 分支
    n = len(xs)
    step = float(np.mean([xs[1] - xs[0], ys[1] - ys[0]]))
    stone_r = max(6, min(40, int(step * 0.32)))
    board = read_board(a, xs, ys, stone_r)
    print(f'grid source: {src} | drift={drift:.1f}px')
    print(f'board size: {n}x{n}   black='
          f'{sum(r.count("X") for r in board)}  white='
          f'{sum(r.count("O") for r in board)}')
    letters = 'ABCDEFGHJKLMNOPQRST'[:n]
    print('    ' + ' '.join(letters))
    for i in range(n):
        print(f'{n - i:2d}  ' + ' '.join(board[i]))
    print('    ' + ' '.join(letters))


if __name__ == '__main__':
    main()


def stones_legal(n, board):
    """按规则剔除零气组(已被提/死子)后返回落子序列 [[b/w, coord], ...]"""
    from collections import deque
    grid = [[None] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            c = board[i][j]
            if c in 'XO':
                grid[i][j] = c
    alive = set()
    seen = set()
    for i in range(n):
        for j in range(n):
            if grid[i][j] and (i, j) not in seen:
                col = grid[i][j]
                q = deque([(i, j)])
                grp = set()
                libs = set()
                while q:
                    y, x = q.popleft()
                    if (y, x) in grp:
                        continue
                    grp.add((y, x))
                    for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                        yy, xx = y + dy, x + dx
                        if 0 <= yy < n and 0 <= xx < n:
                            if grid[yy][xx] is None:
                                libs.add((yy, xx))
                            elif grid[yy][xx] == col and (yy, xx) not in grp:
                                q.append((yy, xx))
                seen |= grp
                if libs:
                    alive |= grp
    letters = 'ABCDEFGHJKLMNOPQRST'[:n]
    stones = []
    for (i, j) in sorted(alive):
        c = grid[i][j]
        stones.append(['b' if c == 'X' else 'w', letters[j] + str(n - i)])
    return stones
