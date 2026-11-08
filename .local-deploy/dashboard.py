"""Loopback-only UI for the fixed CZSC three-index task (Python stdlib)."""
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from chan_runner import BASE, ROOT, read_json

HOST, PORT = "127.0.0.1", 8793
ORIGIN = f"http://{HOST}:{PORT}"
TOKEN = secrets.token_urlsafe(32)
SPAWN_LOCK = threading.Lock()
PROCESS = None


def snapshot():
    state = read_json(ROOT / "state.json", {})
    if state.get("status") == "running":
        try:
            os.kill(int(state["pid"]), 0)
        except (ProcessLookupError, KeyError, ValueError):
            state = {**state, "status": "error", "message": "上次任务中断；保留上次成功结果，可手动重跑"}
    pointer = read_json(ROOT / "latest.json", {})
    report = None
    if pointer:
        report_path = Path(pointer["path"]) / "report.json"
        if report_path.parent.parent.resolve() == ROOT.resolve():
            report = read_json(report_path)
    return dict(state=state, report=report, token=TOKEN, schedule=read_json(BASE / "schedule.json", {}))


class Handler(BaseHTTPRequestHandler):
    def respond(self, body, status=200, content_type="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.end_headers()
        self.wfile.write(body)

    def valid_host(self):
        if self.headers.get("Host") != f"{HOST}:{PORT}":
            self.respond({"error": "仅允许本机固定地址"}, 403)
            return False
        return True

    def do_GET(self):
        if not self.valid_host():
            return
        path = urlsplit(self.path).path
        if path == "/":
            self.respond((BASE / "dashboard.html").read_bytes(), content_type="text/html; charset=utf-8")
        elif path == "/api/status":
            self.respond(snapshot())
        elif path == "/favicon.ico":
            self.respond(b"", 204, "image/x-icon")
        elif re.fullmatch(r"/artifacts/\d{8}_\d{6}_\d{6}/(?:sz399006|sh000001|sh000688)/(?:chart|m30_chart)\.html", path):
            file = ROOT / path.removeprefix("/artifacts/")
            if file.is_file():
                self.respond(file.read_bytes(), content_type="text/html; charset=utf-8")
            else:
                self.respond({"error": "图表不存在"}, 404)
        else:
            self.respond({"error": "页面不存在"}, 404)

    def do_POST(self):
        global PROCESS
        if not self.valid_host():
            return
        if self.path != "/api/run":
            self.respond({"error": "接口不存在"}, 404)
            return
        if self.headers.get("Origin") != ORIGIN or not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), TOKEN):
            self.respond({"error": "请从本机页面点击重跑"}, 403)
            return
        if self.headers.get("Content-Type") != "application/json" or int(self.headers.get("Content-Length", "0")) > 128:
            self.respond({"error": "无效请求"}, 400)
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            if body != {}:
                raise ValueError("参数不支持")
        except (ValueError, TypeError):
            self.respond({"error": "本页面仅运行固定三个指数"}, 400)
            return
        with SPAWN_LOCK:
            if (PROCESS is not None and PROCESS.poll() is None) or snapshot()["state"].get("status") == "running":
                self.respond({"message": "已有任务正在运行"}, 409)
                return
            ROOT.mkdir(parents=True, exist_ok=True)
            with (ROOT / "runner.log").open("ab") as log:
                PROCESS = subprocess.Popen([sys.executable, "-u", str(BASE / "chan_runner.py")],
                    cwd=BASE.parent, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        self.respond({"message": "已开始更新三个指数", "pid": PROCESS.pid}, 202)


if __name__ == "__main__":
    print(f"CZSC dashboard: {ORIGIN}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
