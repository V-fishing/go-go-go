"""KataGo analysis 常驻进程客户端(JSON 行协议)。

复用引擎进程, 避免每步重新加载模型(4s -> 0.2s)。
用法:
    from katago_client import KataClient
    cli = KataClient()
    out = cli.query(req_dict)   # 返回响应 dict 或 None(进程异常, 自动重启一次)
    cli.close()
"""
import json
import os
import subprocess
import threading

KATAGO = r'E:\game\GoAI\katago\katago.exe'
MODEL19 = r'E:\game\GoAI\networks\kata1-b18c384nbt-s9996604416-d4316597426.bin.gz'
MODEL9 = r'E:\game\GoAI\networks\kata9x9-b18c384nbt-20231025.bin.gz'
CFG = r'E:\game\GoAI\katago\analysis_example.cfg'


def model_for_size(n):
    return MODEL9 if n == 9 else MODEL19


class KataClient:
    def __init__(self):
        self.proc = None
        self.model = None
        self.warm_done = False
        self._lock = threading.Lock()

    def _ensure(self, model):
        if self.proc is None or self.model != model:
            self.close()
            self.proc = subprocess.Popen(
                [KATAGO, 'analysis', '-model', model, '-config', CFG],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.model = model

    def query(self, req):
        """发送请求并读取一行 JSON 响应; 进程异常时重启重试一次"""
        model = model_for_size(req.get('boardXSize', 19))
        with self._lock:      # 预热/主查询串行, 防协议串扰
            return self._query_locked(req, model)

    def _query_locked(self, req, model):
        for attempt in range(2):
            try:
                self._ensure(model)
                self.proc.stdin.write((json.dumps(req) + '\n').encode())
                self.proc.stdin.flush()
                line = self.proc.stdout.readline()
                if not line:
                    raise IOError('engine closed')
                out = json.loads(line.decode('utf-8', 'ignore'))
                self.warm_done = True
                return out
            except Exception:
                self.close()
                if attempt == 1:
                    return None
        return None

    def close(self):
        if self.proc is not None:
            try:
                self.proc.kill()
            except Exception:
                pass
            self.proc = None
            self.model = None
