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

# ---- 对局模式 -> 网格整格偏移 -----------------------------------------
# 不同对局模式(匹配/AI对战/友谊赛/挑战赛)的棋盘在屏幕上的位置/缩放存在细微
# 偏差, 自动拟合会系统性偏一格。按模式套用一组固定整格偏移(相对自动拟合结果),
# 各模式棋盘几何一致, 默认无偏移即对齐。若后续发现某模式系统性偏移, 再按
# 模式单独标定(目前 match/ai/friend/challenge 实测均无需偏移)。
GAME_MODE = 'challenge'   # 默认挑战赛
MODE_GRID_OFFSET = {
    # mode: (dcol, drow)  整格偏移(列方向, 行方向), 正=右/下
    'match':     (0, 0),   # 匹配: 实测无偏移即对齐(顶行读真实棋子, 偏移反把网格推出棋盘)
    'ai':        (0, 0),   # AI对战(待标定)
    'friend':    (0, 0),   # 友谊赛(待标定)
    'challenge': (0, 0),   # 挑战赛: 实测无偏移即对齐(同匹配, 偏移(-1,-1)会把网格推到顶部UI致顶行全白)
}


def set_game_mode(mode):
    """设置对局模式, 影响 find_board_in_window 的网格偏移校准。"""
    global GAME_MODE
    if mode in MODE_GRID_OFFSET:
        GAME_MODE = mode
    else:
        print('!! 未知对局模式 %r, 沿用 %r' % (mode, GAME_MODE))


def _mode_grid_offset():
    """返回当前模式的整格偏移 (dcol, drow)。"""
    return MODE_GRID_OFFSET.get(GAME_MODE, (0, 0))


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


def _dedup_peaks(peaks, runlen, gap=14):
    """合并间距过近的候选峰(保留 run-length 最强者)。

    木框/UI 的密集细线簇(间距~3px, 如棋盘木框上边框)会被塌缩成一条, 使其
    与真实棋盘线(间距~27px)错开约半格; fit_grid_axis 拟合时该簇产生的网格
    各线都偏离真实棋盘线 -> 命中数骤降, 真实棋盘线胜出, 避免网格整行上移。
    """
    peaks = np.asarray(peaks, int)
    if len(peaks) == 0:
        return np.array([], float)
    keep = []
    for p in sorted(peaks.tolist()):
        if keep and p - keep[-1] < gap:
            if runlen[p] > runlen[keep[-1]]:
                keep[-1] = p
        else:
            keep.append(p)
    return np.array(keep, float)


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
    rows = _dedup_peaks(rows.astype(int), rr)
    cols = _dedup_peaks(cols.astype(int), cr)
    return rows, cols, dark


def _hit_ratio(fit_vals, peaks):
    if len(fit_vals) == 0:
        return 0.0
    hits = sum(1 for v in fit_vals
               if np.any(np.abs(peaks - v) < 3.0))
    return hits / len(fit_vals)


def _inset_to_lines(a, xs, ys, steps=(0.3, 0.5, 0.7, 1.0)):
    """防木框过冲: fit_grid_axis 可能把棋盘木框当成最外网格线, 使整网格被压缩、
    底部行采样点落到暗色木框(白子丢失/下半盘偏暗、只有白子受影响)。

    用星位(star_ratio)作对齐真值: 把网格两端向内收缩若干步长(使外缘线从木框
    退回到真实棋线), 仅当收缩明显提升星位匹配才采用。星位不可检(返回 None)时
    保持原网格, 故对准的网格完全不受影响, 不会回归。
    """
    xs = np.asarray(xs, float)
    ys = np.asarray(ys, float)
    s0 = star_ratio(a, xs, ys)
    if s0 is None:
        return xs, ys
    best = (s0, xs, ys)
    sy = float(np.mean(np.diff(ys)))
    sx = float(np.mean(np.diff(xs)))
    k = len(xs)
    for d in steps:
        ny = ys[0] + d * sy + np.arange(k) * ((ys[-1] - ys[0] - 2 * d * sy) / (k - 1))
        nx = xs[0] + d * sx + np.arange(k) * ((xs[-1] - xs[0] - 2 * d * sx) / (k - 1))
        s = star_ratio(a, nx, ny)
        if s is not None and s > best[0] + 0.02:
            best = (s, nx, ny)
    return best[1], best[2]


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
        # 越界惩罚封顶: 真实棋盘边缘线被 UI/信息栏遮挡时, 大尺寸 fit 会
        # 外推到遮挡区, 不应因此重罚而误选小尺寸(诊断#_diag_size: 19路
        # oh=76.7 被压到 17.65, 13路 oh=0 反以 30.80 胜出, 致19路误判13路)
        oh = min(oh, 40.0)
        score = (cov + 6.0 * star_score + (8.0 if exact else 0.0)
                 - 0.15 * oh)
        cands.append((score, k, (np.array(xs), np.array(ys)), sy, star))
    if not cands:
        return None
    cands.sort(key=lambda c: -c[0])
    score, k, (xs, ys), step, star = cands[0]
    xs, ys = _inset_to_lines(a, xs, ys)
    # 按对局模式套用整格偏移: 不同模式棋盘位置/缩放存在细微偏差, 自动拟合
    # 会系统性偏一格。偏移由 GAME_MODE 决定(挑战赛实测左移1格+上移1格)。
    # 约定: dcol/drow 为"期望的整格位移", 负值=左移/上移, 故用 +dcol*sx。
    _dcol, _drow = _mode_grid_offset()
    if _dcol or _drow:
        _sx = float(xs[1] - xs[0])
        _sy = float(ys[1] - ys[0])
        _xs2, _ys2 = xs + _dcol * _sx, ys + _drow * _sy
        if _xs2[0] > 0 and _xs2[-1] < a.shape[1] - 1:
            xs = _xs2
        if _ys2[0] > 0 and _ys2[-1] < a.shape[0] - 1:
            ys = _ys2
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


def align_by_variance(a, xs, ys, span=10, max_shift=3.0):
    """网格微对齐: 找使交叉点采样亮度方差最大的 (dx,dy)。

    网格正对棋子时每个交叉点采样最纯粹(黑/白/木, 方差大);
    偏移时混入相邻子/线, 采样值趋中, 方差小。

    max_shift: 只接受 |dx|,|dy| <= max_shift 的**微调**。
    网格定位(refine 后残差 <1px)本身已足够准, 全域(±span)搜索会在
    密集盘面锁定错误局部峰(实测把网格推偏 ~5.6px ≈ 1/4 格, 导致黑子
    成片漏检: 实测同帧 B43W47 -> B21W36), 故严格限制为小幅校正。
    需要大范围纠偏时由 locate_board / refine_grid 负责(它们有物理意义
    更强的线命中与星位校验)。
    """
    n = len(xs)
    step = float(np.mean([xs[1] - xs[0], ys[1] - ys[0]]))
    r = max(4, min(30, int(step * 0.28)))
    lim = int(min(span, max_shift))
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
    for dy in range(-lim, lim + 1):
        for dx in range(-lim, lim + 1):
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


def align_checked(a, xs, ys, span=10, max_shift=3.0):
    """对齐 + 合理性校验: 不通过则回退对齐前网格。

    校验(任一不满足即回退):
      1. 可见性: 棋盘区域仍为木色基调(board_visible);
      2. 线贴合度: 预测线位置的暗像素占比不得明显下降
         (_line_coverage, 网格贴合真实棋盘线时应更高或持平)。
    方差是纯统计量, 密集盘面下会把网格推到"采样更极端"的错位处,
    物理校验能拦下这类假对齐; 宁可用未对齐的原网格也不读错盘。
    """
    try:
        nx, ny = align_by_variance(a, xs, ys, span=span,
                                   max_shift=max_shift)
    except Exception:
        return xs, ys
    if float(np.mean(np.abs(np.asarray(nx, float) - np.asarray(xs, float)))
             + np.mean(np.abs(np.asarray(ny, float)
                              - np.asarray(ys, float)))) < 1e-6:
        return xs, ys           # 未发生位移, 无需校验
    try:
        if not board_visible(a, nx, ny):
            return xs, ys
        c0 = _line_coverage(a, xs, ys)
        c1 = _line_coverage(a, nx, ny)
        if c1 < c0 * 0.85:      # 线贴合度明显变差 -> 假对齐
            return xs, ys
    except Exception:
        return xs, ys
    return nx, ny


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



def refine_subpix(a, xs, ys):
    """亚像素网格精修: 每条线在 ±2px 带内按"相对带内中位数的暗度"加权质心,
    再对整轴做等距模型最小二乘(残差<0.8px 才采用模型)。
    坐标从 ±0.5px 整数量化提升到 ~0.1px 且帧间稳定(悬停方块四角验证、
    采样芯、点击坐标等下游全部受益; 它们是"坐标系精度"这种共通地基)。"""
    try:
        n = len(xs)
        if n < 9:
            return xs, ys
        lum = a.mean(axis=2).astype(np.float64)
        hgh, wid = lum.shape
        out_x = np.asarray(xs, float).copy()
        out_y = np.asarray(ys, float).copy()
        for axis in (0, 1):
            coords = out_x if axis == 0 else out_y
            for i, c in enumerate(coords):
                c0 = int(round(c))
                if c0 < 2:
                    continue
                if axis == 0:
                    if c0 > wid - 3:
                        continue
                    band = lum[:, c0 - 2:c0 + 3]
                else:
                    if c0 > hgh - 3:
                        continue
                    band = lum[c0 - 2:c0 + 3, :]
                bg = float(np.median(band))
                w = np.maximum(0.0, bg - band)
                wsum = w.sum(axis=0)
                den = float(wsum.sum())
                if den < 6.0:
                    continue
                pos = np.arange(c0 - 2, c0 + 3)
                coords[i] = float((wsum * pos).sum() / den)
            # 等距模型 + 残差门控(棋子压线会把个别质心带偏, 模型吸收)
            idx = np.arange(n)
            A = np.vstack([idx, np.ones(n)]).T
            coef, *_ = np.linalg.lstsq(A, coords, rcond=None)
            model = coef[0] * idx + coef[1]
            if np.abs(coords - model).max() < 0.8:
                coords[:] = model
        return out_x, out_y
    except Exception:
        return xs, ys


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
_rowdiag_t = [0.0]   # 按行诊断打印节流(BR_ROWDIAG)
_top_bad = 0    # 首行异常连续帧计数: 单帧(动画瞬态)只丢不缓存, 连续2帧才清缓存


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


_QD_CACHE = {}   # stone_r -> 左上四分之一内芯偏移


def _quarter_disk(stone_r):
    """左上四分之一内芯采样(半径 0.55r)。
    最后一手标记 + 光晕是**全环描边/呼吸**(黑子白描边、白子黑描边,
    占 0.8-1.0r 外缘, 实测动画帧可顶破占比阈值), 内芯远离外缘不受
    影响; 悬停方块(12px)/星位点由宽度直读(>=15px)排除。"""
    if stone_r in _QD_CACHE:
        return _QD_CACHE[stone_r]
    offs = []
    # 采样芯定位在棋子"左上角区域": 以交点左上侧 0.55r 处为中心的小圆
    # (半径 0.3r)。棋体覆盖 0-0.8r, 芯远离网格十字轴(不入采样)且远离
    # 最后手标记/光晕外环(0.8-1.0r 全环描边), 只在纯子色区采样。
    # 采样芯定位在棋子左上角区域(偏移 0.68*stone_r 的左上方向):
    # 距圆心约 0.96*stone_r, 子半径≈1.25*stone_r -> 仍在子体内且
    # 在标记/光晕内缘(0.85 倍子半径)之外
    c = -max(3, round(stone_r * 0.68))
    rr = max(2, round(stone_r * 0.28))
    r2 = rr * rr
    for dy in range(c - rr, c + rr + 1):
        for dx in range(c - rr, c + rr + 1):
            if (dy - c) ** 2 + (dx - c) ** 2 <= r2:
                offs.append((dy, dx))
    _QD_CACHE[stone_r] = offs
    return _QD_CACHE[stone_r]


_FULL_CACHE = {}


def _full_disk(stone_r):
    """整盘采样(半径 stone_r 实心圆): 用于"满盘填充"判定。
    空交叉点必有十字网格线穿过 -> 盘内必留暗像素; 棋子覆盖线条后整盘纯子色
    (无暗线)。这是区分白子与亮木纹最稳的判据, 且不受 last-move 呼吸/标记影响
    (标记在盘外或仅外缘, 不增盘内暗线)。仅当砖石块半径合法时返回。"""
    if stone_r in _FULL_CACHE:
        return _FULL_CACHE[stone_r]
    r = max(2, int(round(stone_r)))
    r2 = r * r
    offs = []
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dy * dy + dx * dx <= r2:
                offs.append((dy, dx))
    _FULL_CACHE[stone_r] = offs
    return offs


_INNER_CACHE = {}


def _inner_disk(stone_r):
    """内盘采样(半径=stone_r*0.6 实心圆): 用于抗 last-move 标记环的填充判定。
    标记环仅在外缘 0.8-1.0r, 内盘不含环 -> 白子内盘纯亮/黑子内盘纯暗/空点内盘
    必含十字暗线。比整盘填充更抗浓标记(整盘填充会被外缘黑环抬高暗占比而失效)。"""
    if stone_r in _INNER_CACHE:
        return _INNER_CACHE[stone_r]
    r = max(2, int(round(stone_r * 0.6)))
    r2 = r * r
    offs = []
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dy * dy + dx * dx <= r2:
                offs.append((dy, dx))
    _INNER_CACHE[stone_r] = offs
    return offs


def _ui_flat_block(a, cx, cy, color, step):
    """UI 覆盖物判定(鼠标悬停实心方块/瞄准框) -> True 表示非棋子。

    判据 = **整颗棋子盘**纯色平坦: 真棋子是立体渲染(中心高亮->边缘渐暗的
    径向明暗), UI 纯色填充则整盘方差≈0。
      【关键修复】原实现只测中心 4px 小区域方差; 但当白子中心被最后一手标记
      (小黑三角/点)占据时, 中心小区域只剩一圈均匀白环 -> 方差<8 -> 真白子
      被误当 UI 块剔除(即\"对方最后一手白子偶发丢\"的根因)。改用整颗棋子盘
      (半径=stone_r)估方差后, 真子因边缘渐暗方差很大(白子整盘更高), 纯色 UI
      块整盘≈0 -> 二者稳定区分, 且标记不再误剔。
      实测(19路, 整盘, 60 帧扫描): 真白子方差 min 64.1; 真黑子 min 16.7;
      悬停框方差 0.0。阈值 8 对黑子留 2.1 倍余量, 对纯色 UI 判别不变。
      即使网格跟随误差 18px, 真子方差仍 62~173, 判据稳健。
    曾经加过\"矩形剖面比\"判据, 但网格偏移 4~7px 时真白子剖面比 32%~50% 升到
    >0.95 与矩形难分 -> 大量误杀真白子, 已删除。形状判据在密集盘面不可靠。
    """
    H, W = a.shape[:2]
    cx, cy = int(round(cx)), int(round(cy))
    r = max(3, int(round(step * 0.32)))   # 棋子半径(整颗盘)
    y0, y1 = max(0, cy - r), min(H, cy + r + 1)
    x0, x1 = max(0, cx - r), min(W, cx + r + 1)
    if y1 - y0 < 5 or x1 - x0 < 5:
        return False
    patch = a[y0:y1, x0:x1]
    lum = patch.mean(axis=2)
    chm = patch.max(axis=2) - patch.min(axis=2)
    m = ((lum > 165) & (chm < 60)) if color == 'O' \
        else ((lum < 105) & (chm < 30))
    # 盘内同色像素占比: 真棋子/实心 UI 块都占整盘 ~0.7-0.9; 仅小悬停块/残影
    # 占比低 -> 直接放行(留给主逻辑)。用占比而非整盘均值, 避免背景木色稀释。
    mfrac = float(m.mean())
    if mfrac < 0.45:
        return False
    # 同色像素的方差: 真子径向渐变(中心亮->边缘暗)方差大; 纯色 UI 块≈0。
    # 标记占中心一小块的白子, 同色(白)像素仍含边缘渐变 -> 方差大 -> 不被误剔。
    return float(lum[m].var()) < 8.0


def read_board(a, xs, ys, stone_r):
    """向量化读盘: 整盘一次 numpy 批量取点/统计, 开销 ~ms 级。
    真子判定附加环带测试: 采样外环须同为子色(圆棋子到边缘 8-9px),
    UI 覆盖物(悬停框等)由半段宽度 + _ui_flat_block 形状/纹理双重排除。
    """
    n = len(xs)
    H, W = a.shape[:2]
    offs = _quarter_disk(stone_r)
    if not offs or len(offs) < 2:
        return ['.' * n for _ in range(n)]
    offs_all = offs

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
    # 整盘填充判定用满盘采样(半径=stone_r 实心圆)
    bf_all, wf_all, nv_all = stats(_full_disk(stone_r))
    # 内盘填充判定用内盘采样(半径=stone_r*0.6): 标记黑环/白环仅在外缘 0.8-1.0r,
    # 内盘不含环 -> 抗"浓标记"相位。整盘填充会被外缘黑环抬高暗占比而失效, 故
    # 以"内盘纯亮/纯暗"作更稳的填充判定。空点内盘必含十字暗线 -> 不会被误判。
    bf_in, wf_in, nv_in = stats(_inner_disk(stone_r))
    # 互斥门槛: 最后一手对色标记=黑子白描边/白子黑描边, 占外缘 0.8-1.0r;
    # 采样点(0.96r)正落在环上 -> 白子的黑环抬升 bf、黑子的白环抬升 wf。
    # 原 0.45 会把"带标记的白子"判丢(用户实测: 只有白子漏检)。放宽到 0.60:
    # 环只在外缘, 占比通常不超 0.6; 真异色子(bf/wf≈0.9)不会被误判。真子对色
    # 占比 <0.15, 半段形状(halfb/halfw)作二次把关避免木纹误判。
    X = (bf > 0.45) & (wf < 0.60)
    O = (wf > 0.35) & (bf < 0.60)
    # 形状直读(左半边/上段宽度): 量半段内"最长同色连续段"——
    # 不要求从交点中心起算: 最后一手反色三角(白三角盖黑子)尖角过棋心
    # 会把中心像素染成中间调(非黑非白), 从中心起算的逻辑连测都不测,
    # 带标记真子整格读空; 而子体在中心对侧仍有 >=8px 纯色连续段,
    # 最长段 >=8 依然是真子铁证:
    #   真子 ≈9-12px(直径 18-23); 悬停方块 12px -> 左半 6px; 星位 4-6px
    # 判据: 左半段或上段最长同色连续 >=8px -> 真子; 排除方块/星点。
    step = float(np.mean([xs[1] - xs[0], ys[1] - ys[0]]))
    hw = int(min(W - 1, max(6, step * 0.52)))
    iy = np.round(np.asarray(ys, float)).astype(int)
    ix = np.round(np.asarray(xs, float)).astype(int)
    h, w = a.shape[:2]
    # 边界钳制 + 有效性掩码: 网格端点因 UI 遮挡/漂移超出图像时, 钳制防
    # 采样越界崩溃(IndexError); 越界点不判子(被遮挡列不可读, 不能用边框
    # /面板像素误判棋子)。_diag_size 诊断: 19路最右列 xs[-1]=519.4 超出
    # 窗口宽 519, 原采样直接崩; 右侧 2 列被 UI 面板遮挡。
    iyc = iy.clip(0, h - 1)
    ixc = ix.clip(0, w - 1)
    _row_ok = (iy >= 0) & (iy < h)
    _col_ok = (ix >= 0) & (ix < w)
    lum2 = a.mean(axis=2)
    chm2 = a.max(axis=2) - a.min(axis=2)
    blk = (lum2 < 105) & (chm2 < 30)
    wht = (lum2 > 165) & (chm2 < 60)

    def _best_run(seg):
        best = cur = 0
        for v in seg:
            cur = cur + 1 if v else 0
            if cur > best:
                best = cur
        return best

    runbw = np.zeros((n, n), int)   # 左半段最长黑连续
    runww = np.zeros((n, n), int)   # 左半段最长白连续
    runbv = np.zeros((n, n), int)   # 上段最长黑连续
    runwv = np.zeros((n, n), int)   # 上段最长白连续
    for i in range(n):
        yy = iyc[i]
        yl = max(0, yy - hw)
        for j in range(n):
            xx = ixc[j]
            xl = max(0, xx - hw)
            runbw[i, j] = _best_run(blk[yy, xl:xx + 1])
            runww[i, j] = _best_run(wht[yy, xl:xx + 1])
            runbv[i, j] = _best_run(blk[yl:yy + 1, xx])
            runwv[i, j] = _best_run(wht[yl:yy + 1, xx])
    # 半段直读可独立成立(与芯判定 OR): 网格交点与子心错位/漂移时,
    # 采样芯可能偏出子体, 但左/上 半段最长同色连续 >=8px 仍是真子证据;
    # 悬停方块(6px)/星位点(2-3px)在半段上依然 <8 被排除
    cen_b = blk[iyc, ixc]
    cen_w = wht[iyc, ixc]
    halfb = (runbw >= 8) | (runbv >= 8)
    halfw = (runww >= 8) | (runwv >= 8)
    # 芯判子须 AND 相应色半段>=8(方块: 芯判黑但半段仅6px -> 排除);
    # 半段+中心同色 独立 OR 兜底(错位子: 芯偏出体, 半段仍纯子色)
    X = (X & halfb) | (halfb & cen_b & (wf < 0.60))
    O = (O & halfw) | (halfw & cen_w & (bf < 0.60))
    # 满盘填充判定(抗 last-move 呼吸/标记致半段连读断裂): 盘内无暗线
    # (bf_all<0.10)必为棋子(空点十字网格线必留暗像素); 整盘亮->白、整盘
    # 暗->黑。与半段/芯判定 OR; 纯色 UI 覆盖物由后续 _ui_flat_block 剔除。
    # 内盘填充(0.6r)抗外缘环标记: 内盘不含环 -> 白子内盘纯亮/黑子内盘纯暗/
    # 空点内盘必含十字暗线。整盘填充会被外缘黑环抬高暗占比而失效, 故以内盘为主。
    # 【实测 r9c8】真实最后一手标记会侵入内盘(中心标记/呼吸高亮), 强相位时
    # wf_in 掉到 0.75、bf_in 升到 0.19, 内盘填充因 wf_in<0.80 失效、半段连读
    # 被中心暗标记打断 -> 判空。故新增"边缘锚定"fallback: 只要四分之一盘(偏心
    # 0.96r, 在标记内缘外)仍纯白, 且内盘白占优/暗不主导, 即认白子, 不再被中心
    # 标记打断 halfw。(整片覆盖型 UI 如打吃警告会让 wf_q 一起沦陷从而不触发,
    # 交 _ui_flat_block 处理)
    fill_O = ((wf_in > 0.80) & (bf_in < 0.15)) \
        | ((wf_all > 0.85) & (bf_all < 0.10)) \
        | ((wf > 0.90) & (bf < 0.30) & (wf_in > 0.55)
           & (bf_in < 0.45) & (wf_in - bf_in > 0.25))
    fill_X = ((bf_in > 0.80) & (wf_in < 0.15)) \
        | ((bf_all > 0.85) & (wf_all < 0.10)) \
        | ((bf > 0.90) & (wf < 0.30) & (bf_in > 0.55)
           & (wf_in < 0.45) & (bf_in - wf_in > 0.25))
    O = O | fill_O
    X = X | fill_X
    # 边缘互斥守门(抗强标记致\"白子带黑标记\"误翻成黑/X): 边缘(四分之一盘)
    # 明显白(wf>0.85)必为白子, 不可判黑; 明显黑(bf>0.85)必为黑子, 不可判白。
    # 真实最后一手标记只居中/外缘细环, 不侵入边缘采样(wf_q 实测恒=1.00), 故仅
    # 在异常强标记相位兜底; 真黑子边缘暗(wf 低)不受影响。
    X = X & ~(wf > 0.85)
    O = O & ~(bf > 0.85)
    # UI 覆盖物剔除(鼠标悬停实心方块/瞄准框): 尺寸 >=14px 时半段判据
    # (最长同色连续 >=8px)已拦不住, 必须靠形状/纹理区分, 见 _ui_flat_block
    _flat = np.zeros((n, n), bool)
    for _i in range(n):
        for _j in range(n):
            if not (X[_i, _j] or O[_i, _j]):
                continue
            _flat[_i, _j] = _ui_flat_block(
                a, ixc[_j], iyc[_i], 'X' if X[_i, _j] else 'O', step)
    if _flat.any():
        X = X & ~_flat
        O = O & ~_flat
    # 内芯(离轴)19路仅 4 采样点, nv 下限 3 即足够占比统计
    _bad = ~(np.outer(_row_ok, _col_ok))   # 越界交点(被遮挡)不判子
    X = X & ~_bad
    O = O & ~_bad
    grid = np.where(nv < 3, '?', np.where(X, 'X', np.where(O, 'O', '.')))
    board = [''.join(r) for r in grid.tolist()]
    _flash_check(a, xs, ys, stone_r, board)
    # 按行诊断(BR_ROWDIAG): 每行中心点中位亮度 + 白/黑子计数。
    # 下半行 L 明显低于上半 -> 亮度梯度(阴影/反光); L 正常却仍少 O ->
    # 网格 y 向漂移/透视斜切导致采样点偏出子体。两路均优先丢白(白检测
    # 需整片亮像素, 比黑脆弱)。用于定位"下半部分白子大量丢失"。
    if os.environ.get('BR_ROWDIAG'):
        import time as _t
        if _t.time() - _rowdiag_t[0] > 2:
            _rowdiag_t[0] = _t.time()
            _parts = []
            for _i in range(n):
                _rl = lum2[iyc[_i], ixc]
                _med = int(np.median(_rl))
                _no = sum(1 for _j in range(n) if board[_i][_j] == 'O')
                _nx = sum(1 for _j in range(n) if board[_i][_j] == 'X')
                _parts.append(f'r{_i}:L{_med:3d}O{_no}X{_nx}')
            print('[ROWDIAG]', ' '.join(_parts))
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
_grid_lock = {}     # (winW, vis_h, n) -> 冻结网格(xs, ys): 静止窗口下
                    # 坐标完全锁定, 防逐帧 refine 微抖跨采样边界
_align_cnt = 0      # 对齐帧计数(每 25 次强制重对齐一次, 防慢漂移)


def clear_grid_cache():
    _grid_cache.clear()
    _align_cache.clear()
    _grid_lock.clear()


def align_reset():
    """仅重置对齐/冻结缓存(保留网格缓存): 疑似网格错位时强制下次
    重新对齐, 但保留线检测兜底, 不会退化成清缓存后永久读不到。"""
    _align_cache.clear()
    _grid_lock.clear()


# ---------------- 光标悬停守卫 ----------------
# 行棋预览方块(悬停实心框)跟随真实光标, 只出现在**空交叉点**上,
# 颜色=行棋方, 且黑框带渐变/坐标文字(方差 25~209), 平坦判据拦不住。
# 根治: 光标所在交点若"上帧空、本帧有子"则本帧置空 —— 真子不会因
# 悬停而出现; 真落子下一帧自然保留(仅延迟 1 个读盘周期)。
# PostMessage 注入的合成悬停只发生在点击目标, 点击后该点立即变为真子,
# 不产生持续幻影, 无需处理。
_cursor_nobuf = {}     # key -> {(i,j): char} 最近一次"光标不在该点"时的真实读数


def _cursor_pos():
    """真实光标屏幕坐标(失败 None)"""
    pt = ctypes.wintypes.POINT()
    if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
        return (pt.x, pt.y)
    return None


def _cursor_guard(board, xs, ys, cursor_pt, step, key):
    """光标悬停守卫: 冻结"光标所在交点"为它**最近一次无光标时**的读数。

    行棋预览方块/落子提示跟随真实光标、只出现在空交叉点、颜色=行棋方;
    黑框带渐变与坐标文字(实测方差 25~209), 平坦度判据拦不住, 故按位置
    排除。

    【修复】base 不再取"上一帧原始读数"。旧逻辑在光标进入交点的过渡帧
    里, 上一帧光标已在交点内、该点已画预览方块 -> base 被方块色污染 ->
    之后基准锁成错误值, 预览方块持续被当成棋子(星位因小黑点叠加更易
    触发, 即"鼠标经过星位实心方块被识别为子")。

    改为维护"无光标读数缓冲": 每帧对**光标影响圈外**的交点写入其真实 raw
    读数; 光标在 P 及附近(半径 excl=1.2*step, 覆盖预览方块±0.4step + 棋子
    半径 0.32step)时不写入, 保留进入 P 前最后的干净读数 -> base 恒来自"光标
    不在 P"的帧, 且方块侵入 P 邻点的过渡帧也不会污染 P 的 base。任意空交叉
    点的预览方块都稳定被冻结回真实空('.')/真子。
    代价: 光标停留期间 P 不更新, 对手恰落子于 P(概率 1/361)需待光标移开
    后下一帧才识别, 与旧逻辑一致(移开即恢复)。
    """
    n = len(xs)
    buf = _cursor_nobuf.setdefault(key, {})
    idx = None
    excl = step * 1.2   # 光标影响圈: 预览方块±0.4step + 棋子半径0.32step 留余量
    if cursor_pt is not None:
        j = int(np.argmin(np.abs(np.asarray(xs, float) - cursor_pt[0])))
        i = int(np.argmin(np.abs(np.asarray(ys, float) - cursor_pt[1])))
        if (abs(xs[j] - cursor_pt[0]) <= step * 0.55
                and abs(ys[i] - cursor_pt[1]) <= step * 0.55):
            idx = (i, j)
    # 写缓冲: 仅光标影响圈外(圈内保留进入前真实读数, 防方块侵入过渡污染)
    for ii in range(n):
        for jj in range(n):
            if cursor_pt is None:
                buf[(ii, jj)] = board[ii][jj]
            else:
                d = ((xs[jj] - cursor_pt[0]) ** 2
                     + (ys[ii] - cursor_pt[1]) ** 2) ** 0.5
                if d >= excl:
                    buf[(ii, jj)] = board[ii][jj]
    out = board
    if idx is not None:
        i, j = idx
        base = buf.get((i, j), '.')
        if board[i][j] != base and base in 'XO.':
            out = list(board)
            out[i] = board[i][:j] + base + board[i][j + 1:]
    return out


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
    cp = _cursor_pos()
    cursor_pt = None
    if cp is not None and (rect[0] <= cp[0] < rect[2]
                           and rect[1] <= cp[1] < rect[1] + vis_h):
        cursor_pt = (cp[0] - rect[0], cp[1] - rect[1])
    res = read_img(a, rect, vis_h, use_calib=use_calib,
                   force_auto=force_auto, do_align=do_align,
                   force_size=force_size, cursor_pt=cursor_pt)
    if res is not None:
        res['img'] = img
    return res


def read_img(a, rect, vis_h, use_calib=False, force_auto=False,
             do_align=True, force_size=0, cursor_pt=None):
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
        # 自适应微对齐: align_checked 开销较大(限制偏移后已大幅下降)。
        # 当前检测网格与
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
            _xs2, _ys2 = align_checked(a, xs, ys)
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
    xs, ys = refine_subpix(a, xs, ys)
    # ---- 网格冻结: 静止窗口下坐标完全锁定(防逐帧 refine 微抖把采样芯
    # 跨过棋子边缘/光晕带); 检测到真实移动(均差>1.5px)才更新锁定 ----
    _lk = _grid_lock.get((rect[2] - rect[0], vis_h, len(xs)))
    if _lk is not None and len(_lk[0]) == len(xs):
        _dxk = float(np.mean(np.abs(np.asarray(xs, float) - _lk[0])))
        _dyk = float(np.mean(np.abs(np.asarray(ys, float) - _lk[1])))
        if (_dxk + _dyk) / 2 <= 1.5:
            xs, ys = _lk[0], _lk[1]   # 冻结: 复用锁定网格
        else:
            _grid_lock[(rect[2] - rect[0], vis_h, len(xs))] = (
                np.array(xs, float), np.array(ys, float))
    else:
        _grid_lock[(rect[2] - rect[0], vis_h, len(xs))] = (
            np.array(xs, float), np.array(ys, float))
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
    # 防对齐退化: 方差/暗度对齐在密盘下偶发把网格推离白子(~1.5px), 而白子检测
    # 依赖四分之一盘采样(wf>0.35)极脆弱 -> 大批量漏白, 黑子靠中心暗点耐受而
    # 幸存。若"未精修的原始网格直接读盘"比当前最终网格多识别子(尤其白子),
    # 判为对齐/精修退化, 回退到原始网格(隔离 flash 状态以免污染后续读盘基准)。
    global _prev_board
    _xs_r, _ys_r = refine_subpix(a, np.asarray(_pre_xy[0], float),
                                  np.asarray(_pre_xy[1], float))
    _dr_r = 0.0
    _prev_save = _prev_board
    _b_raw = read_board(a, _xs_r, _ys_r, stone_r)
    _prev_board = _prev_save
    _b_align = read_board(a, xs, ys, stone_r)
    _prev_board = _prev_save
    _wa = sum(r.count('O') for r in _b_align); _xa = sum(r.count('X') for r in _b_align)
    _wr = sum(r.count('O') for r in _b_raw); _xr = sum(r.count('X') for r in _b_raw)
    if (_wr > _wa + 1) or (_wr + _xr > _wa + _xa + 3):
        xs, ys, drift = _xs_r, _ys_r, _dr_r
        if do_align:
            _align_cache.pop(_ak, None)
        _grid_lock[(rect[2] - rect[0], vis_h, len(xs))] = (
            np.array(xs, float), np.array(ys, float))
        board = _b_raw
    else:
        board = _b_align
    # 光标悬停守卫: 真实光标停在空交叉点时的行棋预览方块
    board = _cursor_guard(board, xs, ys, cursor_pt, step,
                          (rect[2] - rect[0], vis_h, n))
    # 首行异常防护: 棋盘首行(靠边)几乎不会同时落 6+ 子; 一行子里
    # >=6 且大部分是同一色 = 网格首行被锚到棋盘上沿以外的 UI 点阵
    # (头像圆点/段位/计时等白色元素) -> 整帧丢弃并清网格缓存,
    # 防幻影盘面污染 UI 镜像与对局基准。
    # 单帧(动画/UI瞬态)只丢帧不清缓存——密集盘面依赖缓存兜底做
    # 线路检测, 一次性清空会"闪动一帧后永久读不到"; 连续2帧异常
    # (真幻影锚定)才清缓存并强制重定位。
    global _top_bad
    _top = board[0]
    _top_n = sum(1 for c in _top if c in 'XO')
    _top_same = max(_top.count('X'), _top.count('O'))
    # 改进判据: 仅"顶行单色占比极高(规则 UI 点阵特征) 且 网格首行被推到窗口
    # 顶部 UI 带"才判异常; 边线正常布局(混色散落 / 首行不在 UI 带)放行,
    # 避免实战边线落 6+ 子被误丢成 None。
    if _top_n >= 6 and _top_same >= _top_n * 0.85 and ys[0] < 150:
        # 顶行大量同色子: 多为"模式偏移套错"把网格顶行推到页面顶部白色
        # UI(头像/计时/段位)所致。临时用无偏移模式重跑整条读盘流程兜底
        # (复用对齐/退化检测, 与手动选对应模式等价); 仍异常才计为坏帧丢弃。
        if _mode_grid_offset() != (0, 0):
            _old = GAME_MODE
            try:
                set_game_mode('ai')   # ai 偏移 (0,0) = 无偏移基准
                _retry = read_img(a, rect, vis_h, use_calib=use_calib,
                                  force_auto=force_auto, do_align=do_align,
                                  force_size=force_size, cursor_pt=cursor_pt)
            finally:
                set_game_mode(_old)
            if _retry is not None:
                _top_bad = 0
                return _retry
        _top_bad += 1
        if _top_bad >= 2:
            clear_grid_cache()
        return None
    _top_bad = 0
    key = (rect[2] - rect[0], vis_h)
    entry = (np.array(xs), np.array(ys), n, src)
    entries = [e for e in _grid_cache.get(key, [])
               if not (len(e[0]) == len(xs)
                       and abs(e[0][0] - xs[0]) < 2
                       and abs(e[1][0] - ys[0]) < 2)]
    entries.append(entry)
    _grid_cache[key] = entries[-2:]
    # 截断标记: 网格端点超出图像边界 = 棋盘被窗口边缘/UI 面板遮挡,
    # 该侧若干列/行不可读(见 _diag_size 诊断: 19路最右列 xs[-1]=519.4
    # 超出窗口宽 519, 右侧 2 列被遮挡)。主循环可据此提示用户调整窗口。
    _h, _w = a.shape[:2]
    truncated = (xs[0] < -0.5 or xs[-1] > _w - 0.5
                 or ys[0] < -0.5 or ys[-1] > _h - 0.5)
    return {'n': n, 'board': board, 'xs': xs, 'ys': ys, 'src': src,
            'step': step, 'stone_r': stone_r, 'rect': rect,
            'cursor_pt': cursor_pt, 'drift': drift, 'truncated': truncated}


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


def detect_ko(n, board, my_char):
    """检测当前局面我方即将提劫的'劫'形, 返回劫点 GTP 串(如 'G3')或 None。

    劫形特征(标准2x2交替): 空点 P 只有一个正交邻子, 且其为对方单子、仅
    此1气(=P, 处于被打吃); 同时 P 的两个对角为我方子。满足即判定为劫点——
    该点本回合被劫规禁止, 须先找劫材, 下一回合(对手已应)才合法。

    仅在'我方即将提劫'场景调用; 普通吃子(梯子/边角)因劫点会有≥2个正交
    邻子(我方), 不会被误判。
    """
    opp = 'O' if my_char == 'X' else 'X'
    letters = 'ABCDEFGHJKLMNOPQRST'[:n]

    def _grp(y, x):
        col = board[y][x]
        if col not in 'XO':
            return 0, set()
        q = [(y, x)]
        seen = {(y, x)}
        grp = set()
        libs = set()
        while q:
            yy, xx = q.pop()
            grp.add((yy, xx))
            for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                ny, nx = yy + dy, xx + dx
                if 0 <= ny < n and 0 <= nx < n:
                    if board[ny][nx] == '.':
                        libs.add((ny, nx))
                    elif board[ny][nx] == col and (ny, nx) not in seen:
                        seen.add((ny, nx))
                        q.append((ny, nx))
        return len(grp), libs

    for i in range(n):
        for j in range(n):
            if board[i][j] != '.':
                continue
            orth = []
            for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                ny, nx = i + dy, j + dx
                if 0 <= ny < n and 0 <= nx < n and board[ny][nx] in 'XO':
                    orth.append((ny, nx))
            if len(orth) != 1:        # 劫点仅1个正交邻子(对方单子)
                continue
            oy, ox = orth[0]
            if board[oy][ox] != opp:
                continue
            size, libs = _grp(oy, ox)
            if size != 1 or libs != {(i, j)}:
                continue              # 必须是对方单子且仅此1气
            diag = 0
            for dy, dx in ((-1, -1), (-1, 1), (1, -1), (1, 1)):
                ny, nx = i + dy, j + dx
                if 0 <= ny < n and 0 <= nx < n and board[ny][nx] == my_char:
                    diag += 1
            if diag >= 2:             # 两对角为我方子 -> 2x2交替劫形
                return letters[j] + str(n - i)
    return None

