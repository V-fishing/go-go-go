# -*- coding: utf-8 -*-
"""临时文件清理: 截图/OCR 临时 png、引擎日志目录、大日志轮转。

规则(保守, 不碰任何配置/棋谱/趋势数据):
  1. 本目录下 _*.png / win_now*.png: 删除 30 分钟前的(进行中会话的文件都是新的)
  2. analysis_logs/: 删除 2 天前的; 始终保留最新 10 个
  3. 其他 *.png 临时文件(如 screen_base.png): 30 分钟前则删
用法:
    from cleanup_files import cleanup_now
    cleanup_now()          # 返回 {'removed': n, 'freed_kb': x, ...}
"""
import glob
import os
import time

TOOLS = os.path.dirname(os.path.abspath(__file__))
PNG_AGE_S = 30 * 60          # 临时 png 保留 30 分钟
ENGINE_LOG_AGE_S = 2 * 86400  # 引擎日志保留 2 天
KEEP_ENGINE_LOGS = 10         # 引擎日志始终保留最新 N 个
# 常见无下划线前缀的临时文件也清理
EXTRA_PNGS = ('screen_base.png', 'win_now.png')


def _age(f):
    try:
        return time.time() - os.path.getmtime(f)
    except Exception:
        return -1


def cleanup_now(verbose=False):
    """执行一次清理, 返回统计 dict"""
    stat = {'removed': 0, 'freed_kb': 0, 'kept_new': 0, 'log_trim': 0}

    def remove(f):
        try:
            sz = os.path.getsize(f)
            os.remove(f)
            stat['removed'] += 1
            stat['freed_kb'] += sz / 1024
            return True
        except Exception:
            return False

    # 1) 临时截图/OCR png(含 win_now*)
    pats = ['_*.png', 'win_now*.png'] + list(EXTRA_PNGS)
    for pat in pats:
        for f in glob.glob(os.path.join(TOOLS, pat)):
            if not os.path.isfile(f):
                continue
            if _age(f) > PNG_AGE_S:
                remove(f)
            else:
                stat['kept_new'] += 1

    # 2) 引擎日志目录(analysis_logs/)
    edir = os.path.join(TOOLS, 'analysis_logs')
    if os.path.isdir(edir):
        logs = [f for f in glob.glob(os.path.join(edir, '*'))
                if os.path.isfile(f)]
        logs.sort(key=lambda f: os.path.getmtime(f), reverse=True)
        for f in logs[KEEP_ENGINE_LOGS:]:
            if _age(f) > ENGINE_LOG_AGE_S:
                remove(f)

    # 3) 系统 temp 中的 goai_* 临时截图(崩溃遗留; 正常用完即删)
    import tempfile
    for f in glob.glob(os.path.join(tempfile.gettempdir(), 'goai_*.png')):
        if _age(f) > PNG_AGE_S:
            remove(f)

    if verbose:
        print(f'[清理] 删除 {stat["removed"]} 个临时文件, '
              f'释放 {stat["freed_kb"]:.0f}KB'
              + (f', 保留进行中 {stat["kept_new"]} 个新文件'
                 if stat['kept_new'] else ''))
    return stat


if __name__ == '__main__':
    cleanup_now(verbose=True)
